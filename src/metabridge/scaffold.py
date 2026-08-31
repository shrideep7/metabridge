"""Pipeline scaffolding: source system + table manifest -> ready-to-run assets.

The onboarding path for "get SAP (or any connector) into a cloud warehouse":
declare the tables once in YAML, get a full ingestion layer on whichever stack
the customer runs — a dbt project, warehouse-native landing DDL with bulk
export/import scripts, an IDMC bundle, PowerCenter XML — plus connection
artifacts (secrets as env-var references), a conversion report, and a
governance report, in one command.

Table manifest format:

    tables:
      - name: MARA                # source table / extractor
        schema: SAPSR3            # optional
        target_name: material     # optional (defaults to stg_<name>)
        incremental_column: AEDAT # optional -> MERGE load with $$LAST_RUN_TS
        unique_key: [MATNR]       # optional (required for merge)
        columns:                  # optional but recommended
          - {name: MATNR, type: nvarchar(18)}
          - {name: AEDAT, type: dats}

    procedures:                   # optional — the curated layer's LOGIC
      - name: LOAD_MATERIAL_DIM
        schema: SILVER
        language: PL/SQL
        definition: |
          PROCEDURE load_material_dim IS
          BEGIN
            INSERT INTO material_dim (...) SELECT ... FROM mara ...;
          END;

Tables alone produce a landing layer: every model a `select` over its source.
The `procedures:` section is what carries the transformation — its set-based
statements become models with real SQL (see `metabridge.procedures`).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .connectors.base import ConnectorSpec, get_registry
from .connectors.emit import dbt_profile, idmc_connection, powercenter_connection
from .ir.model import (
    IssueSeverity, Link, LoadStrategy, Mapping, Pipeline, Port, SourceTable,
    Transformation, TransformationType, canonical_type,
)


def _canonical(spec: ConnectorSpec, native_type: str) -> str:
    base = native_type.strip().lower().split("(")[0].strip()
    return spec.type_map.get(base) or canonical_type(native_type)


def _precision_scale(native_type: str):
    """Parse (precision, scale) from a native type like 'decimal(12,2)' or
    'varchar(18)'. Returns (0, 0) when none is declared so the generators use
    their documented fallback. Preserving this is what stops every numeric
    column collapsing to decimal(38,6)."""
    mo = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", native_type or "")
    if not mo:
        return 0, 0
    return int(mo.group(1)), int(mo.group(2) or 0)


def _port(spec: ConnectorSpec, col: dict) -> Port:
    native = str(col.get("type", "string"))
    prec, scale = _precision_scale(native)
    declared = bool(str(col.get("type", "")).strip())
    return Port(name=str(col["name"]),
                datatype=_canonical(spec, native),
                precision=prec, scale=scale,
                type_declared=declared,
                # Nullability is catalog fact, the same as the type beside
                # it — introspection reads it off the real constraint. Left
                # on the default every column landed nullable, so a NOT NULL
                # column in the source arrived as one that merely happened
                # to have no nulls yet.
                nullable=col.get("nullable", True) is not False,
                # Carry the source's own type through to the generators. The
                # canonical above is 9 values wide and cannot distinguish
                # TIME from VARCHAR or keep a time-zone offset; the DDL is
                # resolved from this instead, via sqlx.type_engine. Only set
                # when a type was really declared, so an undeclared column
                # still lands on the documented fallback.
                native_type=native if declared else "")


def build_pipeline(project: str, source: ConnectorSpec,
                   tables: List[dict]) -> Pipeline:
    pipeline = Pipeline(name=project, source_format="scaffold")
    pipeline.metadata["dialect"] = source.dialect or ""
    pipeline.metadata["source_connector"] = source.key

    for spec in tables:
        tname = str(spec["name"])
        model = str(spec.get("target_name") or ("stg_" + tname.lower()))
        cols = [_port(source, c) for c in spec.get("columns", []) or []]
        if not cols:
            cols = [Port(name="ROW_DATA")]
        db = str(spec.get("database", "") or "")
        # A dbt source resolves to exactly ONE schema, so the source name has
        # to be 1:1 with a schema. Naming it after the CONNECTOR collapsed
        # every schema on that connection into one source pinned to whichever
        # schema sorted first — so an Oracle estate with RAW_SCHEMA,
        # SILVER_SCHEMA and GOLD_SCHEMA got one `oracle` source claiming
        # RAW_SCHEMA, and two thirds of its tables resolved to a schema they
        # are not in. The connector key is only the fallback, for a source
        # that declares no schema at all.
        schema = str(spec.get("schema", "") or "")
        src_table = SourceTable(name=tname, schema=schema, database=db,
                                system=schema or source.key, columns=cols)
        if all(s.name != tname for s in pipeline.sources):
            pipeline.sources.append(src_table)

        m = Mapping(name=model, description="Ingestion of %s.%s via %s"
                    % (spec.get("schema", ""), tname, source.name))
        src_t = Transformation(name="SRC_" + tname, type=TransformationType.SOURCE,
                               ports=list(cols),
                               properties={"table": tname,
                                           "schema": spec.get("schema", ""),
                                           "database": db})
        sq = Transformation(name="SQ_" + tname,
                            type=TransformationType.SOURCE_QUALIFIER,
                            ports=list(cols), properties={"source": src_t.name})
        m.transformations += [src_t, sq]
        m.links.append(Link(src_t.name, sq.name))
        terminal = sq.name

        # The declared key is a fact about the TABLE — introspection reads it
        # off the real primary key — so it holds whatever the load strategy
        # is. Applying it only in the incremental branch dropped it for every
        # full-load table, and with it the pk_uniqueness reconciliation test
        # and the unique/not_null dbt tests on that column: a manifest could
        # declare `unique_key: [CUSTOMER_ID]` and the generated project would
        # test nothing.
        m.unique_key = [str(k) for k in spec.get("unique_key", []) or []]

        inc_col = str(spec.get("incremental_column", "") or "")
        if inc_col:
            fil = Transformation(
                name="FIL_INCREMENTAL", type=TransformationType.FILTER,
                ports=list(cols),
                properties={"condition": "%s > $$LAST_RUN_TS" % inc_col})
            m.transformations.append(fil)
            m.links.append(Link(terminal, fil.name))
            terminal = fil.name
            m.load_strategy = LoadStrategy.MERGE
            if not m.unique_key:
                m.load_strategy = LoadStrategy.APPEND
                m.add_issue(IssueSeverity.WARNING, "NO_UNIQUE_KEY",
                            "incremental_column without unique_key — falling "
                            "back to append loads",
                            suggestion="Declare unique_key for merge semantics.")
        else:
            # No watermark: every run reloads the whole table. That is always
            # CORRECT, so it is the safe default — but on a large table it is
            # also the most expensive thing this generator can emit, and an
            # introspected manifest carries no watermark, so the choice is
            # easy to inherit without ever deciding it. Say so.
            m.load_strategy = LoadStrategy.FULL
            m.add_issue(IssueSeverity.MANUAL, "FULL_RELOAD_NO_WATERMARK",
                        "Table %s has no incremental_column — every run "
                        "reloads the entire table" % tname,
                        suggestion="Declare incremental_column (a change "
                                   "timestamp) and unique_key for MERGE "
                                   "loads. Intentional for small reference "
                                   "tables — verify for large ones.")

        out = Transformation(name="__OUTPUT__", type=TransformationType.EXPRESSION,
                             ports=list(cols),
                             properties={"virtual": True, "upstream": terminal})
        m.transformations.append(out)
        m.links.append(Link(terminal, "__OUTPUT__"))
        # `landed_from` records that this target is SYNTHESISED: nothing in
        # the source estate is called stg_customer, so anything looking for
        # this model's legacy counterpart — reconciliation above all — has to
        # be pointed at the table it lands, not at its own name.
        tgt = Transformation(name="TGT_" + model, type=TransformationType.TARGET,
                             ports=list(cols),
                             properties={"table": model,
                                         "landed_from": tname,
                                         "landed_from_schema": schema})
        m.transformations.append(tgt)
        m.links.append(Link("__OUTPUT__", tgt.name))
        if not spec.get("columns"):
            m.add_issue(IssueSeverity.MANUAL, "NO_COLUMN_METADATA",
                        "Table %s scaffolded without column metadata" % tname,
                        suggestion="Add columns to the manifest or point the "
                                   "connector at the live system to introspect.")
        pipeline.mappings.append(m)
    return pipeline


_SAMPLE_MANIFEST = """tables:
  - name: CUSTOMERS            # or a plain list of table names
    schema: SALES              # optional
    incremental_column: UPDATED_AT   # optional
    unique_key: [CUSTOMER_ID]        # optional
    columns:                          # optional
      - {name: CUSTOMER_ID, type: integer}
      - {name: NAME, type: nvarchar}"""


_SPLIT_TYPE_RE = re.compile(r"\(\s*\d+\s*$")
_TYPE_TAIL_RE = re.compile(r"^\s*\d+\s*\)\s*$")


def _repair_flow_split_type(c: dict, native: str) -> str:
    """``{name: id, type: number(38,0)}`` in YAML *flow* style is not one
    value: the comma is the flow separator, so PyYAML yields
    ``{'type': 'number(38', '0)': None}`` and the scale is silently lost —
    every numeric then falls back to decimal(38,6).

    The damage is unambiguous (an unclosed paren beside a bare ``<digits>)``
    key), so rejoin it rather than lose the precision. Block style and
    quoted values never take this path.
    """
    if not _SPLIT_TYPE_RE.search(native):
        return native
    for k, v in c.items():
        if v is None and isinstance(k, str) and _TYPE_TAIL_RE.match(k):
            return "%s,%s" % (native, k.strip())
    return native


def _norm_column(c) -> Optional[dict]:
    if isinstance(c, str):
        return {"name": c}
    if isinstance(c, dict):
        if "name" in c:
            out = {"name": str(c["name"])}
            for k in ("type", "data_type", "datatype"):
                if c.get(k):
                    out["type"] = _repair_flow_split_type(c, str(c[k]))
                    break
            # NOT NULL is the one constraint most cloud targets actually
            # enforce, and a default is part of the column definition — both
            # have to survive the handoff or the generated DDL silently
            # accepts rows the source would have rejected. Only the
            # non-default readings are carried: nullable is true unless said
            # otherwise, so `nullable: true` would be noise.
            if c.get("nullable") is False:
                out["nullable"] = False
            for k in ("default", "generated"):
                if c.get(k):
                    out[k] = str(c[k])
            return out
        if len(c) == 1:                       # {KUNNR: numc}
            (k, v), = c.items()
            return {"name": str(k), **({"type": str(v)} if v else {})}
    return None


def _norm_table(item) -> Optional[dict]:
    if isinstance(item, str) and item.strip():
        return {"name": item.strip()}
    if isinstance(item, dict):
        name = item.get("name") or item.get("table") or \
            item.get("table_name") or item.get("identifier")
        if not name:
            return None
        spec = dict(item)
        spec["name"] = str(name)
        cols = [c for c in map(_norm_column, item.get("columns") or [])
                if c]
        spec["columns"] = cols
        return spec
    return None


def _looks_like_connection_profile(doc: dict) -> bool:
    if any(k in doc for k in ("outputs", "target")) and \
            ("outputs" in doc or isinstance(doc.get("target"), str)):
        return True
    return any(isinstance(v, dict) and "outputs" in v
               for v in doc.values())


def load_table_manifest(tables_file: str):
    """Read ANY reasonable table-manifest YAML — never demand one exact
    shape. Accepted: {tables:[...]} (dicts or names), dbt sources.yml,
    dbt schema.yml models, a plain YAML list, or a {table: [columns]}
    mapping. A file that is recognizably something ELSE (a connection
    profile, a dbt_project.yml) gets an error saying what it is and what
    a manifest looks like. -> (tables, notes)"""
    text = Path(tables_file).read_text(encoding="utf-8")
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as e:
        raise ValueError(
            "%s is not valid YAML (%s). A table manifest looks like:\n%s"
            % (Path(tables_file).name, str(e)[:120], _SAMPLE_MANIFEST))
    tables: List[dict] = []
    notes: List[str] = []
    seen = set()

    def add(spec: Optional[dict]):
        if spec and spec["name"].lower() not in seen:
            seen.add(spec["name"].lower())
            tables.append(spec)

    for doc in docs:
        if isinstance(doc, list):
            for item in doc:
                add(_norm_table(item))
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("tables"):
            for item in doc["tables"]:
                add(_norm_table(item))
            continue
        if doc.get("sources"):                       # dbt sources.yml
            for src in doc["sources"] or []:
                schema = str(src.get("schema") or src.get("name") or "")
                for item in src.get("tables") or []:
                    spec = _norm_table(item)
                    if spec is not None:
                        spec.setdefault("schema", schema)
                        add(spec)
            notes.append("manifest read as a dbt sources.yml")
            continue
        if doc.get("models"):                        # dbt schema.yml
            for item in doc["models"] or []:
                add(_norm_table(item))
            notes.append("manifest read as a dbt schema.yml — models "
                         "treated as tables")
            continue
        if _looks_like_connection_profile(doc):
            raise ValueError(
                "%s looks like a CONNECTION PROFILE (dbt profiles/"
                "connection artifact), not a table manifest — the "
                "scaffold needs the TABLES to build pipelines. Provide "
                "a manifest like:\n%s"
                % (Path(tables_file).name, _SAMPLE_MANIFEST))
        if "model-paths" in doc or "profile" in doc and "version" in doc:
            raise ValueError(
                "%s looks like a dbt_project.yml, not a table manifest. "
                "Provide a manifest like:\n%s"
                % (Path(tables_file).name, _SAMPLE_MANIFEST))
        # {table_name: [cols] | {..} | None} mapping shape
        mapped = 0
        for k, v in doc.items():
            if not isinstance(k, str) or not k.strip():
                continue
            if isinstance(v, list):
                add({"name": k, "columns":
                     [c for c in map(_norm_column, v) if c]})
                mapped += 1
            elif isinstance(v, dict):
                spec = _norm_table({**v, "name": v.get("name", k)})
                if spec is not None:
                    add(spec)
                    mapped += 1
            elif v is None:
                add({"name": k, "columns": []})
                mapped += 1
        if mapped:
            notes.append("manifest read as a {table: columns} mapping")

    if not tables:
        raise ValueError(
            "No tables found in %s. Accepted shapes: {tables: [...]}, a "
            "dbt sources.yml/schema.yml, a plain list of table names, or "
            "a {table: [columns]} mapping. Example:\n%s"
            % (Path(tables_file).name, _SAMPLE_MANIFEST))
    for spec in tables:
        spec.setdefault("columns", [])
    return tables, notes


def load_procedures(tables_file: str) -> List[dict]:
    """Stored-procedure bodies carried by a manifest (`procedures:`).

    Optional and purely additive: a manifest without the section scaffolds
    exactly as it did before. With it, the logic that builds the curated layer
    travels WITH the tables it reads, instead of being left behind in the
    source system for someone to port by hand. Bad YAML is not reported here —
    `load_table_manifest` runs first and says so properly.
    """
    try:
        docs = [d for d in yaml.safe_load_all(
            Path(tables_file).read_text(encoding="utf-8")) if d is not None]
    except (yaml.YAMLError, OSError):
        return []
    out: List[dict] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for item in doc.get("procedures") or []:
            if isinstance(item, dict) and (item.get("name")
                                           or item.get("object_name")):
                out.append(dict(item))
    return out


def scaffold(source_key: str, target_key: str, tables_file: str, out_dir: str,
             project: str = "", source_params: Optional[Dict[str, str]] = None,
             target_params: Optional[Dict[str, str]] = None,
             source_region: str = "", target_region: str = "",
             governance: bool = True,
             movement: Optional[dict] = None,
             procedures: Optional[List[dict]] = None,
             etl_bundle: str = "", etl_format: str = "",
             land_etl_targets: bool = False) -> dict:
    """Generate the target stacks from a table manifest.

    ``governance`` is opt-out: with it False the residency/classification scan
    is skipped entirely — no governance report is written and the returned
    report carries no ``governance`` key. The source/target REGIONS only feed
    that scan, so they are irrelevant when it is off.

    ``procedures`` overrides the manifest's own `procedures:` section (the CLI
    passes a directory of PL/SQL sources this way). Pass an empty list to
    convert the tables only.

    ``etl_bundle`` is an optional PowerCenter/IDMC/DataStage/SSIS/Talend
    project whose mappings become the curated layer of THIS project, so one
    run yields landing DDL, staging and the transformation models together.
    Tables that bundle produces are then not landed — they would otherwise be
    both copied and rebuilt — unless ``land_etl_targets`` says to keep them,
    which is only useful while running the two sides in parallel to compare
    them. Omit the bundle and every generator sees exactly the pipeline it
    saw before this argument existed."""
    reg = get_registry()
    source = reg.get(source_key)
    target = reg.get(target_key)
    if source is None:
        raise ValueError("Unknown source connector: %s (see `metabridge connectors`)"
                         % source_key)
    if target is None:
        raise ValueError("Unknown target connector: %s" % target_key)

    tables, manifest_notes = load_table_manifest(tables_file)
    procs = list(procedures) if procedures is not None \
        else load_procedures(tables_file)

    project = project or "%s_to_%s" % (source.key, target.key)
    pipeline = build_pipeline(project, source, tables)
    # target dialect drives expression rendering for warehouse-native SQL
    if target.dialect:
        pipeline.metadata["dialect"] = target.dialect
    # ONE canonical source/target platform value flows to every generator,
    # artifact, manifest and report — so changing the target genuinely
    # changes the output (and its labels), never a hardcoded default.
    pipeline.metadata["source_platform"] = source.name or source.key
    pipeline.metadata["target_platform"] = target.name or target.key
    # the SOURCE's own dialect, kept apart from the target's. Generators that
    # resolve a landed column's type need both ends: what the source declared
    # it as, and what the landing DDL creates it as.
    pipeline.metadata["source_dialect"] = source.dialect or ""
    pipeline.metadata["source_pc_dbtype"] = source.powercenter_dbtype or ""
    pipeline.metadata["target_pc_dbtype"] = target.powercenter_dbtype or ""
    # Cross-platform landing: the manifest's `database` names the SOURCE
    # database (the unload needs it), but the dbt sources must resolve in
    # the TARGET database — so the sources.yml writer must not pin it.
    if source.key != target.key:
        pipeline.metadata["landing_target"] = True

    # Stored-procedure logic joins the pipeline BEFORE anything is generated,
    # so every generator downstream — dbt, landing DDL, IDMC, PowerCenter, the
    # conversion and governance reports — sees the curated layer too, not just
    # the raw one. Tables alone would produce a project of pass-through models.
    # The staging map has to be taken BEFORE any logic merges: afterwards it
    # reports the incoming mappings' own sources as staged, and the unlanding
    # below would then look for the wrong models to remove.
    from .procedures import _staging_map
    stage_of = _staging_map(pipeline)

    logic: dict = {}
    if procs:
        from .procedures import merge_procedure_logic
        logic = merge_procedure_logic(pipeline, procs,
                                      dialect=source.dialect or "")

    # An ETL project carries the same curated layer a procedure estate does,
    # just held in another tool. It merges AFTER procedures so that when both
    # are supplied a name collision renames the ETL side deterministically,
    # and — like them — before any generator runs.
    etl: dict = {}
    if etl_bundle:
        from .etl_logic import merge_etl_logic
        etl = merge_etl_logic(pipeline, etl_bundle, fmt=etl_format,
                              land_targets=land_etl_targets,
                              stage_of=stage_of)

    # A table rebuilt by a PROCEDURE is duplicated exactly as one rebuilt by
    # an ETL mapping — the source of the logic changes nothing. Procedures
    # only warned about it, which left the landing layer still copying a
    # table the generated models rebuild.
    if procs and not land_etl_targets:
        from .etl_logic import _target_tables, unland_built_tables
        names = {m["model"] for m in (logic.get("models") or [])}
        if names:
            logic["not_landed"] = unland_built_tables(
                pipeline, _target_tables(pipeline, only=names), stage_of,
                origin="the converted stored-procedure logic")

    # A manifest declares the key for every table it introspected, including
    # the curated ones. A mapping lifted out of an ETL bundle or a procedure
    # arrives with no key of its own, so a table whose PK we already know
    # ended up with no pk_uniqueness check and no unique/not_null test on the
    # column — the key was in the manifest the whole time, just never handed
    # to the mapping that rebuilds it.
    _reconcile_with_manifest(pipeline, source, tables)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1. dbt project (when the target has a dbt adapter)
    from .generators.dbt_generator import generate_dbt_project
    generate_dbt_project(pipeline, str(out / "dbt"))
    if target.dbt_adapter:
        (out / "dbt" / "profiles.yml").write_text(
            dbt_profile(target, target_params or {}, _safe(project)), encoding="utf-8")

    # 2. landing-layer DDL + bulk movement for the target warehouse.
    # dbt transforms inside ONE warehouse; without this the models have
    # nothing to select from and `dbt run` fails on the first relation.
    from .generators.ddl_generator import generate_target_ddl
    ddl = generate_target_ddl(pipeline, str(out / "ddl"), source=source,
                              target=target,
                              source_params=source_params or {},
                              movement=movement or {},
                              target_params=target_params or {})

    # 3. Informatica assets
    from .generators.idmc_generator import generate_idmc
    from .generators.powercenter_generator import generate_powercenter
    generate_idmc(pipeline, str(out / "idmc"))
    (out / ("wf_%s.xml" % _safe(project))).write_text(generate_powercenter(pipeline), encoding="utf-8")

    # 4. connection artifacts (secrets as env-var references only)
    conns = out / "connections"
    conns.mkdir(exist_ok=True)
    import json as _json
    (conns / ("idmc_%s.json" % source.key)).write_text(_json.dumps(
        idmc_connection(source, source_params or {}, "conn_" + source.key), indent=2), encoding="utf-8")
    (conns / ("idmc_%s.json" % target.key)).write_text(_json.dumps(
        idmc_connection(target, target_params or {}, "conn_" + target.key), indent=2), encoding="utf-8")
    (conns / "powercenter_connections.sh").write_text(
        "#!/bin/sh\n" +
        powercenter_connection(source, source_params or {}, "conn_" + source.key) +
        "\n" +
        powercenter_connection(target, target_params or {}, "conn_" + target.key) +
        "\n", encoding="utf-8")

    # 5. reports: conversion + governance
    from .report.reporter import write_report
    report = write_report(pipeline, "scaffold (dbt + ddl + idmc + powercenter)",
                          str(out))
    if manifest_notes:
        report["manifest_notes"] = manifest_notes
    report["ddl"] = ddl

    # Lineage, as every conversion gets. A scaffold produces a dbt project
    # exactly like `convert` does, and had no lineage document at all — so the
    # one output that answers "where does this column come from" depended on
    # which entry point the user happened to take.
    from .report.lineage import build_lineage, write_lineage
    write_lineage(build_lineage(pipeline), str(out))
    report["lineage_generated"] = True

    # The validation suite and the five-layer verdict, for the same reason.
    # A scaffold IS a conversion — it parses an estate and generates a dbt
    # project — but it shipped no reconciliation SQL, no dbt tests and no
    # PASS/FAIL verdict, so whether the output could be trusted depended
    # entirely on which entry point produced it.
    from .report.testgen import generate_tests, write_tests
    tests_doc = generate_tests(pipeline,
                               source_platform=source.dialect or "",
                               target_platform=target.dialect or "",
                               target_format="dbt")
    write_tests(tests_doc, str(out))
    report["validation_tests"] = tests_doc["summary"]

    from .validate.conversion_validator import (validate_conversion,
                                                write_validation_report)
    mv = validate_conversion(pipeline, str(out), "dbt",
                             dialect=target.dialect or "")
    write_validation_report(mv, str(out))
    report["migration_validation"] = {
        "verdict": mv["verdict"],
        "layers": {L["name"]: L["status"] for L in mv["layers"]},
        "totals": mv["totals"],
        "ai_reviewed": mv["ai_reviewed"],
    }

    if procs:
        from .procedures import write_logic_pack
        pack = write_logic_pack(logic, str(out), procs)
        # write_logic_pack builds its own summary, so what the merge decided
        # about landing has to be carried across or it never reaches the UI
        if logic.get("not_landed"):
            pack["not_landed"] = logic["not_landed"]
        report["procedures"] = pack
    if etl:
        report["etl"] = etl
    if governance:
        from .governance.engine import govern, write_governance_report
        gov = govern(pipeline, source_region=source_region,
                     target_region=target_region or _default_region(target))
        write_governance_report(gov, str(out))
        report["governance"] = gov["summary"]
    return report


def _reconcile_with_manifest(pipeline, source: ConnectorSpec, tables) -> None:
    """Give every mapping what the MANIFEST knows about the tables it touches.

    The manifest is introspected straight from the source catalog, so it is
    the best evidence available about a table. A mapping lifted out of an ETL
    bundle or a procedure knows only what that export chose to record, which
    is routinely less:

    * the KEY — an ETL mapping carries none, so a curated table whose primary
      key the manifest already knew ended up with no pk_uniqueness check and
      no unique/not_null test on the column;
    * the column TYPES — IDMC normalises the catalog into its own platform
      typesystem before exporting, so Oracle's ``NUMBER`` arrives as
      ``double`` and the generated project documented ``float`` for a column
      the landing DDL creates as ``DECIMAL(38,6)``. Three different answers
      for one column, none of them checkable against the others.

    Only fills gaps in: a key the mapping already carries came from the source
    object itself and wins, and a derived column the manifest never saw
    (EMAIL_LOWER, say) is left exactly as the mapping computed it.
    """
    from .ir.model import TransformationType
    by_table = {str(t.get("name", "")).lower(): t for t in tables}
    keys = {name: [str(k) for k in (t.get("unique_key") or [])]
            for name, t in by_table.items()}
    types = {name: {str(c["name"]).lower(): _port(source, c)
                    for c in (t.get("columns") or []) if c.get("name")}
             for name, t in by_table.items()}

    for m in pipeline.mappings:
        tgts = m.by_type(TransformationType.TARGET)
        for node in tgts + m.by_type(TransformationType.SOURCE):
            declared = types.get(
                str(node.properties.get("table", "")).lower())
            if not declared:
                continue
            for port in node.ports:
                col = declared.get(port.name.lower())
                if col is None or not col.type_declared:
                    continue
                port.datatype = col.datatype
                port.precision = col.precision
                port.scale = col.scale
                port.native_type = col.native_type
                port.type_declared = True

        if m.unique_key or not tgts:
            continue
        table = str(tgts[0].properties.get("table", "")).lower()
        declared_key = keys.get(table)
        if not declared_key:
            continue
        ports = {p.name.lower() for t in tgts for p in t.ports}
        # only if the model actually projects the key: a mapping that drops it
        # cannot be tested on it, and claiming otherwise would fail at run time
        if all(k.lower() in ports for k in declared_key):
            m.unique_key = list(declared_key)


def _default_region(spec: ConnectorSpec) -> str:
    return "" if spec.deployment == "on_prem" else ""


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)

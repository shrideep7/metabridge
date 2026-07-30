"""Pipeline scaffolding: source system + table manifest -> ready-to-run assets.

The onboarding path for "get SAP (or any connector) into a cloud warehouse":
declare the tables once in YAML, get a full ingestion layer on whichever stack
the customer runs — a dbt project, an IDMC bundle, PowerCenter XML — plus
connection artifacts (secrets as env-var references), a conversion report, and
a governance report, in one command.

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
"""
from __future__ import annotations

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
    import re
    mo = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", native_type or "")
    if not mo:
        return 0, 0
    return int(mo.group(1)), int(mo.group(2) or 0)


def _port(spec: ConnectorSpec, col: dict) -> Port:
    native = str(col.get("type", "string"))
    prec, scale = _precision_scale(native)
    return Port(name=str(col["name"]),
                datatype=_canonical(spec, native),
                precision=prec, scale=scale,
                type_declared=bool(str(col.get("type", "")).strip()))


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
        src_table = SourceTable(name=tname, schema=str(spec.get("schema", "")),
                                database=db, columns=cols)
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
            m.unique_key = [str(k) for k in spec.get("unique_key", []) or []]
            if not m.unique_key:
                m.load_strategy = LoadStrategy.APPEND
                m.add_issue(IssueSeverity.WARNING, "NO_UNIQUE_KEY",
                            "incremental_column without unique_key — falling "
                            "back to append loads",
                            suggestion="Declare unique_key for merge semantics.")
        else:
            m.load_strategy = LoadStrategy.FULL

        out = Transformation(name="__OUTPUT__", type=TransformationType.EXPRESSION,
                             ports=list(cols),
                             properties={"virtual": True, "upstream": terminal})
        m.transformations.append(out)
        m.links.append(Link(terminal, "__OUTPUT__"))
        tgt = Transformation(name="TGT_" + model, type=TransformationType.TARGET,
                             ports=list(cols), properties={"table": model})
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


def _norm_column(c) -> Optional[dict]:
    if isinstance(c, str):
        return {"name": c}
    if isinstance(c, dict):
        if "name" in c:
            out = {"name": str(c["name"])}
            for k in ("type", "data_type", "datatype"):
                if c.get(k):
                    out["type"] = str(c[k])
                    break
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


def scaffold(source_key: str, target_key: str, tables_file: str, out_dir: str,
             project: str = "", source_params: Optional[Dict[str, str]] = None,
             target_params: Optional[Dict[str, str]] = None,
             source_region: str = "", target_region: str = "",
             governance: bool = True) -> dict:
    """Generate the target stacks from a table manifest.

    ``governance`` is opt-out: with it False the residency/classification scan
    is skipped entirely — no governance report is written and the returned
    report carries no ``governance`` key. The source/target REGIONS only feed
    that scan, so they are irrelevant when it is off."""
    reg = get_registry()
    source = reg.get(source_key)
    target = reg.get(target_key)
    if source is None:
        raise ValueError("Unknown source connector: %s (see `metabridge connectors`)"
                         % source_key)
    if target is None:
        raise ValueError("Unknown target connector: %s" % target_key)

    tables, manifest_notes = load_table_manifest(tables_file)

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
    pipeline.metadata["source_pc_dbtype"] = source.powercenter_dbtype or ""
    pipeline.metadata["target_pc_dbtype"] = target.powercenter_dbtype or ""

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1. dbt project (when the target has a dbt adapter)
    from .generators.dbt_generator import generate_dbt_project
    generate_dbt_project(pipeline, str(out / "dbt"))
    if target.dbt_adapter:
        (out / "dbt" / "profiles.yml").write_text(
            dbt_profile(target, target_params or {}, _safe(project)), encoding="utf-8")

    # 2. Informatica assets
    from .generators.idmc_generator import generate_idmc
    from .generators.powercenter_generator import generate_powercenter
    generate_idmc(pipeline, str(out / "idmc"))
    (out / ("wf_%s.xml" % _safe(project))).write_text(generate_powercenter(pipeline), encoding="utf-8")

    # 3. connection artifacts (secrets as env-var references only)
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

    # 4. reports: conversion + governance
    from .report.reporter import write_report
    report = write_report(pipeline, "scaffold (dbt + idmc + powercenter)", str(out))
    if manifest_notes:
        report["manifest_notes"] = manifest_notes
    if governance:
        from .governance.engine import govern, write_governance_report
        gov = govern(pipeline, source_region=source_region,
                     target_region=target_region or _default_region(target))
        write_governance_report(gov, str(out))
        report["governance"] = gov["summary"]
    return report


def _default_region(spec: ConnectorSpec) -> str:
    return "" if spec.deployment == "on_prem" else ""


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)

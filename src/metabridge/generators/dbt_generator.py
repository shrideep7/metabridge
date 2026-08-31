"""Generate a dbt project from the IR.

Each IR mapping becomes one dbt model: the transformation graph is walked in
topological order and every node is rendered as a CTE, so the generated SQL
mirrors the original dataflow and stays reviewable (a design goal: SI teams
must be able to diff the model against the Informatica mapping).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..ir.model import (
    ConversionIssue, IssueSeverity, LoadStrategy, Mapping, Pipeline,
    SourceTable, Transformation, TransformationType,
)


def _source_name(s: "SourceTable") -> str:
    """dbt source() name for a SourceTable.

    Delegated to dbt_naming so the name used in a model's source() call, the
    name written into sources.yml, and the staging subfolder can never drift
    apart. Under the standard layout that name is the source SYSTEM; under the
    legacy layered one it stays the schema.
    """
    from .dbt_naming import source_system
    return source_system(s, _LAYOUT.get())

_CANONICAL_TO_DBT = {
    "string": "varchar", "integer": "integer", "bigint": "bigint",
    "decimal": "decimal", "double": "double precision", "date": "date",
    "timestamp": "timestamp", "boolean": "boolean", "binary": "binary",
}

# Documented fallback when a decimal column declares no precision/scale.
# Emitted with a warning so it is never mistaken for the real source scale.
from ..sqlx.type_engine import DECIMAL_FALLBACK as _DECIMAL_FALLBACK


def _dbt_type(port) -> str:
    """dbt column type from an IR Port, or "" when the type is UNKNOWN.

    PRESERVES declared numeric precision/scale (never collapsing everything to
    decimal(38,6)); a decimal with no declared precision uses the documented
    fallback.

    The empty result is a deliberate answer, not a failure. `data_type` is
    optional in dbt's property files, and omitting it says "we were not told"
    — where the old blanket `varchar` fallback asserted "this column is text"
    about columns nobody had ever typed. `Port.type_declared` exists precisely
    to tell those apart and was not being consulted, so a genuine VARCHAR and
    an unread type documented identically.
    """
    if not getattr(port, "type_declared", True) \
            and not getattr(port, "native_type", ""):
        return ""
    landed = _LANDED_IN.get()
    if landed:
        # Cross-platform landing: this source is read from the table the
        # generated DDL creates on the TARGET, so the type to document is the
        # one that table actually has. Documenting the SOURCE's own type said
        # `dats` and `numc` for columns the landing DDL creates as DATE and
        # VARCHAR — describing a table that does not exist anywhere.
        #
        # Resolved through the DDL generator's own function so the two cannot
        # disagree: whatever it writes into CREATE TABLE is what is documented
        # here.
        try:
            from .ddl_generator import _type_of
            rendered = _type_of(port, landed[0], landed[1])
        except Exception:                                # noqa: BLE001
            rendered = ""
        if rendered:
            return rendered.strip().lower()
    # sources.yml documents each column as the SOURCE declares it — that is
    # the contract stated in the generated ddl/README. When the source's own
    # type is on hand it IS the answer, and it is the only form that can
    # describe a column the coarse canonical cannot: a TIME(6) column was
    # documented as varchar(6), which is neither its source type nor its
    # landed type.
    if getattr(port, "native_type", ""):
        return str(port.native_type).strip().lower()
    base = _CANONICAL_TO_DBT.get(port.datatype, "varchar")
    if port.datatype == "decimal":
        if port.precision:
            return "decimal(%d,%d)" % (port.precision, port.scale or 0)
        return "decimal(%d,%d)" % _DECIMAL_FALLBACK
    if port.datatype == "string" and port.precision:
        return "varchar(%d)" % port.precision
    return base


def _decimal_needs_fallback(port) -> bool:
    return port.datatype == "decimal" and not port.precision


def _column_entry(port) -> dict:
    """One `columns:` entry, with `data_type` omitted when unknown.

    `quote: true` is set for a name that cannot be written bare. dbt injects a
    generic test's column into SQL RAW — `select {{ column_name }} as
    unique_field` — and only quotes it when this flag says to, so a
    `unique`/`not_null` test on a column called ORDER or USER-ID would compile
    to a syntax error without it. It matters just as much on a source, where
    dbt applies the same rule.
    """
    from ..sqlx.identifiers import needs_quoting
    entry = {"name": port.name}
    data_type = _dbt_type(port)
    if data_type:
        entry["data_type"] = data_type
    if needs_quoting(port.name):
        entry["quote"] = True
    return entry


def _undeclared(ports) -> List[str]:
    """Names of columns whose type was never declared anywhere upstream."""
    return [p.name for p in ports if not _dbt_type(p)]


def _layer_of(m: Mapping, pipeline: Pipeline) -> str:
    """staging: reads physical sources only; marts: nothing depends on it;
    intermediate: everything else."""
    if not m.depends_on:
        return "staging"
    dependents = any(m.name in other.depends_on for other in pipeline.mappings)
    return "intermediate" if dependents else "marts"


def generate_dbt_project(pipeline: Pipeline, out_dir: str,
                         layout: str = "standard") -> None:
    """A complete dbt project, in dbt Labs' own structure.

        source table  -> models/staging/<system>/stg_<system>__<entity>.sql
        helper logic  -> models/intermediate/<domain>/int_<entity>_<verb>.sql
        curated table -> models/marts/<area>/dim_<entity>|fct_<process>.sql
        SCD2 mapping  -> snapshots/snap_<entity>.sql

    Each folder carries its own property file (`_<group>__models.yml`), each
    source system its own `_<system>__sources.yml` and `_<system>__docs.md`,
    and the project gets the files a hand-built dbt repo always has:
    packages.yml, selectors.yml, .gitignore, seeds/, analyses/.

    `migration_manifest.json` traces source object -> CIR object -> dbt
    object(s), and every ref()/source() emitted is verified against what was
    actually written before this returns.

    Pass ``layout="layered"`` for the flat pre-0.2 shape (one
    models/schema.yml, `stg_<base>`/`int_<base>`, and a leaf mapping split into
    a logic view plus a thin mart) — kept so an already-deployed generated
    project keeps resolving to the same relations while it is migrated.
    """
    from .dbt_naming import DEFAULT_LAYOUT, LAYOUTS
    layout = layout if layout in LAYOUTS else DEFAULT_LAYOUT
    token = _TARGET_DIALECT.set(str(pipeline.metadata.get("dialect", "") or ""))
    gtoken = _GRAPH.set(_new_graph())
    ltoken = _LAYOUT.set(layout)
    meta = pipeline.metadata or {}
    landed = (str(meta.get("dialect", "") or "").lower(),
              str(meta.get("source_dialect", "") or "").lower())         if meta.get("landing_target") else ()
    dtoken = _LANDED_IN.set(landed)
    stoken = _SCHEMA_OF.set({})
    atoken = _ALIAS_OF.set({})
    try:
        return _generate_dbt_project(pipeline, out_dir, layout)
    finally:
        _ALIAS_OF.reset(atoken)
        _SCHEMA_OF.reset(stoken)
        _LANDED_IN.reset(dtoken)
        _LAYOUT.reset(ltoken)
        _GRAPH.reset(gtoken)
        _TARGET_DIALECT.reset(token)


def _schema_placement(pipeline: Pipeline, plan: dict, stg_plan: dict):
    """Where each model is BUILT — which is not the folder it sits in.

    dbt derives nothing from a directory name. Without `+schema:` every model
    in the project materialises into the one schema the profile names, so a
    tree of staging/ intermediate/ marts/ folders is pure decoration and an
    estate that kept RAW_SCHEMA, SILVER_SCHEMA and GOLD_SCHEMA apart arrives
    as a single flat schema.

    The estate already stated where each target belongs, so that statement is
    what we reproduce: a folder whose models all agree gets one `+schema:` in
    dbt_project.yml, and a model that disagrees with its neighbours carries
    `schema=` itself.

    -> (folder cfg, per-model cfg, snapshot schema, every schema named)
    """
    members: dict = {}
    snaps: list = []
    for m in pipeline.mappings:
        entry = plan.get(m.name)
        if not entry:
            continue
        schema = str(entry.get("schema") or "").strip()
        if entry.get("kind") == "snapshot":
            snaps.append(schema)
            continue
        members.setdefault((entry["layer"], entry["group"]), []).append(
            (m.name, schema))
    # a staging model over a RAW source declares no target schema of its own
    for info in stg_plan.values():
        members.setdefault(("staging", info.get("group", "")), []).append(
            (None, ""))

    folder: dict = {}
    per_model: dict = {}
    for key, group in members.items():
        declared = {s for _, s in group if s}
        if len(declared) == 1 and all(s for _, s in group):
            folder[key] = declared.pop()
        else:
            # a mixed folder cannot be settled in one line, so the models that
            # do declare a schema say so individually and the rest fall back
            # to the profile's default
            for name, schema in group:
                if name and schema:
                    per_model[name] = schema
    snap_schema = snaps[0] if snaps and len(set(snaps)) == 1 else ""

    named = set(folder.values()) | set(per_model.values())
    if snap_schema:
        named.add(snap_schema)
    return folder, per_model, snap_schema, sorted(n for n in named if n)


def _generate_dbt_project(pipeline: Pipeline, out_dir: str,
                          layout: str = "standard") -> None:
    from .dbt_naming import plan_names
    root = Path(out_dir)
    plan, stg_plan = plan_names(pipeline, layout)
    standard = layout != "layered"

    for d in _PROJECT_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)

    folder_schema, model_schema, snap_schema, all_schemas = _schema_placement(
        pipeline, plan, stg_plan)
    _SCHEMA_OF.set(model_schema)
    # The relation keeps the name the estate already uses, so anything still
    # reading GOLD_SCHEMA.ANALYSIS keeps finding it after cutover. The model
    # file stays fct_analysis — dbt naming inside the project, the estate's
    # naming in the warehouse.
    _ALIAS_OF.set({name: entry["alias"] for name, entry in plan.items()
                   if entry.get("alias")})
    # named for the MODEL, which is what a reader finds in the project, and
    # for the relation it would otherwise have taken
    blocked = sorted("%s (would be %s)"
                     % (entry["ref"], entry["alias_blocked_by_landing"])
                     for entry in plan.values()
                     if entry.get("alias_blocked_by_landing"))
    if blocked:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.INFO, code="RELATION_ALIAS_DEFERRED",
            message="%s: the table it would take the name of is being "
                    "LANDED from the source as well, so both would be the "
                    "same relation — and the model rebuilds it on every "
                    "run, so the landed copy would not survive the first "
                    "one." % "; ".join(blocked),
            obj=pipeline.name,
            suggestion="This is what a parallel run needs: the legacy copy "
                       "and the rebuilt table side by side under different "
                       "names, which is what the reconciliation pairs "
                       "compare. Stop landing them and the models take the "
                       "estate's names — that is the cutover."))

    # Only layers that actually receive a model are configured. dbt warns on
    # every run about "configuration paths ... which do not apply to any
    # resources", and a project whose first run prints a warning teaches
    # whoever inherits it to ignore warnings.
    used = {tgt["dir"].split("/")[0]
            for entry in plan.values() if entry["kind"] == "model"
            for tgt in entry["targets"]}
    used |= {info["dir"].split("/")[0] for info in stg_plan.values()}
    models_cfg: Dict[str, dict] = {
        layer: {"+materialized": mat}
        for layer, mat in (("staging", "view"), ("intermediate", "view"),
                           ("marts", "table"))
        if layer in used
    }
    for (layer, group), schema in sorted(folder_schema.items()):
        node = models_cfg.setdefault(layer, {})
        if group:
            node = node.setdefault(group, {})
        node["+schema"] = schema

    proj = {
        "name": _safe(pipeline.name), "version": "1.0.0", "config-version": 2,
        "profile": _safe(pipeline.name),
        "model-paths": ["models"], "snapshot-paths": ["snapshots"],
        "macro-paths": ["macros"], "test-paths": ["tests"],
        "seed-paths": ["seeds"], "analysis-paths": ["analyses"],
        "models": {_safe(pipeline.name): models_cfg},
    }
    if snap_schema:
        proj["snapshots"] = {_safe(pipeline.name): {"+schema": snap_schema}}
    if all_schemas:
        # dbt's own generate_schema_name CONCATENATES: `+schema: SILVER_SCHEMA`
        # against a profile pointed at ANALYTICS builds ANALYTICS_SILVER_SCHEMA.
        # Taking the name literally is the only way the generated project lands
        # where the landing DDL created the schemas.
        (root / "macros" / "generate_schema_name.sql").write_text(
            render_template("generate_schema_name.sql.j2"), encoding="utf-8")
    # dbt_project.yml is written LAST: the `vars:` block can only be filled in
    # once every model has been rendered and the parameters it actually
    # references are known. A project that declares no var for a var its models
    # use does not compile.

    if pipeline.sources:
        _write_sources_yaml(pipeline, root, layout)

    manifest: List[dict] = []
    # Model properties are grouped by the folder their models live in, so each
    # folder documents itself. One flat models/schema.yml covering all three
    # layers meant the description of a mart lived nowhere near the mart.
    props: Dict[str, List[dict]] = {}

    # classify manifest objects by the REAL source platform, not
    # "PowerCenter" by default — a Snowflake schema is not a PC object
    src_fmt = (pipeline.source_format or "").lower()
    platform_label = {"powercenter": "PowerCenter", "idmc": "IDMC",
                      "dbt": "dbt", "scaffold": "scaffold"}.get(
                          src_fmt, src_fmt or "unknown")
    legacy_pc = src_fmt == "powercenter"

    # ref-resolution map: raw source tables -> stg models; mapping names AND
    # their target tables -> the node a downstream model must ref (which for an
    # SCD2 mapping is its SNAPSHOT, since no model is written for it)
    names: Dict[str, str] = {k: v["name"] for k, v in stg_plan.items()}
    for m in pipeline.mappings:
        final = plan[m.name]["ref"]
        names[m.name] = final
        names[m.name.lower()] = final
        tgts = m.by_type(TransformationType.TARGET)
        if tgts and tgts[0].properties.get("table"):
            names[str(tgts[0].properties["table"]).lower()] = final

    # A mapping in the STAGING layer is the one place its source table is read
    # from source(). Anything downstream that reads the same table — logic
    # lifted out of a stored procedure, typically — refs that staging model
    # instead, so the raw relation is named once and whatever the staging layer
    # does to it is not quietly bypassed. setdefault: an explicit mapping or
    # target-table entry above always wins.
    for m in pipeline.mappings:
        if plan[m.name]["layer"] != "staging":
            continue
        for t in m.by_type(TransformationType.SOURCE):
            table = str(t.properties.get("table", "") or "").lower()
            if table:
                names.setdefault(table, plan[m.name]["ref"])

    # staging models over raw source tables (see plan_names for the two cases
    # that are deliberately skipped)
    for s in pipeline.sources:
        spec = stg_plan.get(s.name.lower())
        if not spec:
            continue
        name = spec["name"]
        cols = [c.name for c in s.columns
                if c.name not in ("*", "ROW_DATA")]
        if not cols:
            # schema genuinely unavailable — the template emits SELECT * with a
            # VISIBLE warning so nobody mistakes a passthrough for a real
            # projection
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.WARNING, code="SELECT_STAR_PASSTHROUGH",
                message="Staging model '%s' emits SELECT * — no column "
                        "metadata for source %s.%s" % (name, s.schema or "raw",
                                                       s.name),
                obj=name,
                suggestion="Provide columns in the manifest or introspect the "
                           "source so the model projects explicit columns."))
        # the column list is joined here rather than looped in the template:
        # Jinja's whitespace control around a per-item comma buys nothing, and
        # getting it wrong silently collapsed the projection onto one line
        projection = "select\n    %s" % ",\n    ".join(
            _ident(c) for c in cols) if cols else ""
        body = render_template("staging_model.sql.j2", projection=projection,
                               source=_source_name(s), table=s.name)
        rel = "models/%s/%s.sql" % (spec["dir"], name)
        (root / "models" / spec["dir"]).mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
        _emitted("model", name, layer="staging", group=spec["group"],
                 path=rel, source_object=s.name,
                 source_type="source_definition")
        _props_for(props, spec["dir"], {
            "name": name,
            "description": "Staging view over source %s.%s"
            % (s.schema or "raw", s.name)})
        entry_src: Dict[str, object] = {
            "source_object": s.name, "source_type": "source_definition",
            "source_platform": platform_label,
            "cir_object": s.name, "cir_type": "SourceTable",
            "dbt_objects": [{"name": name, "role": "staging", "path": rel}]}
        if legacy_pc:
            entry_src["powercenter_object"] = s.name
            entry_src["powercenter_type"] = "source_definition"
        manifest.append(entry_src)

    for m in pipeline.mappings:
        unresolved = [t.name for t in m.by_type(TransformationType.SOURCE)
                      if not str(t.properties.get("table", "")).strip()]
        entry = {"source_object": _origin_label(m),
                 "source_type": "mapping",
                 "source_platform": platform_label,
                 "source_folder": m.properties.get("folder", ""),
                 "cir_object": m.name, "cir_type": "Mapping",
                 "dbt_objects": []}
        if legacy_pc:
            entry["powercenter_object"] = _origin_label(m)
            entry["powercenter_type"] = "mapping"
            entry["powercenter_folder"] = m.properties.get("folder", "")
        manifest.append(entry)
        if unresolved:
            # never emit a model reading FROM <nothing> — manual queue
            m.add_issue(IssueSeverity.MANUAL, "SOURCE_UNRESOLVED",
                        "Source reference(s) could not be resolved (%s) — "
                        "model NOT generated; convert manually from the "
                        "preserved origin SQL" % ", ".join(unresolved),
                        detail=(m.origin or "")[:400],
                        suggestion="Check for dynamic/templated table "
                                   "references in the original statement.")
            entry["status"] = "manual_review"
            continue

        p = plan[m.name]
        if m.load_strategy == LoadStrategy.SCD2 and p["kind"] != "snapshot":
            # a snapshot cannot mint the per-version surrogate key —
            # emit an incremental model and declare what it does NOT do
            scd2 = m.properties.get("scd2_cir") or {}
            if not any(i.code == "SCD2_INCREMENTAL_CLOSE" for i in m.issues):
                m.add_issue(
                    IssueSeverity.MANUAL, "SCD2_INCREMENTAL_CLOSE",
                    "SCD2 with surrogate key '%s': generated as an "
                    "incremental model that INSERTS new versions; "
                    "expiring the previous version (set %s / %s) is "
                    "NOT part of the model and must run as a post-load "
                    "step" % (scd2.get("surrogate_key"),
                              scd2.get("effective_end_column") or "-",
                              scd2.get("current_flag_column") or "-"),
                    suggestion="Use the warehouse SQL output's "
                               "MERGE-based SCD2 statement, or add a "
                               "post-hook that closes superseded "
                               "versions per business key (%s)."
                    % ", ".join(scd2.get("business_key") or []))
        if p["kind"] == "snapshot":
            snap = p["ref"]
            (root / "snapshots" / (snap + ".sql")).write_text(
                render_snapshot_sql(m, pipeline, names, snap),
                encoding="utf-8")
            _emitted("snapshot", snap, layer="snapshots", group="",
                     path="snapshots/%s.sql" % snap,
                     source_object=_origin_label(m), source_type="mapping")
            entry["dbt_objects"].append(
                {"name": snap, "role": "snapshot",
                 "path": "snapshots/%s.sql" % snap})
            continue

        desc = ("Converted from %s '%s' by MetaBridge"
                % ("Informatica mapping" if legacy_pc else "source mapping",
                   _origin_label(m)))

        def _meta():
            # nested under `config:`, not a top-level `meta:` property — dbt
            # deprecated the top-level form (PropertyMovedToConfigDeprecation)
            # and a deprecation becomes an error in a later release
            md = {"source_mapping": _origin_label(m),
                  "source_platform": platform_label}
            if legacy_pc:
                md["powercenter_mapping"] = _origin_label(m)
            return {"meta": dict(md)}

        prev = ""
        for idx, tgt in enumerate(p["targets"]):
            layer = tgt["dir"].split("/")[0]
            if tgt["body"] == "passthrough":
                sql = render_template("model_passthrough.sql.j2",
                                      header=_config_block(m, layer),
                                      upstream=prev)
            else:
                # a decomposed mapping (layered only) keeps its logic in a
                # view and the real config on the terminal model
                cfg = ("{{ config(materialized='view') }}\n\n"
                       if len(p["targets"]) > 1 and idx == 0 else None)
                sql = render_model_sql(m, pipeline, names, config=cfg,
                                       layer=layer)
            rel = "models/%s/%s.sql" % (tgt["dir"], tgt["name"])
            (root / "models" / tgt["dir"]).mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(sql, encoding="utf-8")
            # `relation` only when it differs from the node name: the
            # lineage names the consumption boundary, and a consumer attaches
            # to GOLD_SCHEMA.ANALYSIS, not to the model called fct_analysis.
            _emitted("model", tgt["name"], layer=p["layer"],
                     group=p["group"], path=rel, role=tgt["role"],
                     relation=(p.get("alias") or "")
                     if idx == len(p["targets"]) - 1 else "",
                     source_object=_origin_label(m), source_type="mapping")
            entry["dbt_objects"].append(
                {"name": tgt["name"], "role": tgt["role"], "path": rel})
            pentry = {"name": tgt["name"], "description": desc,
                      "config": _meta()}
            if tgt["columns"]:
                cols = _target_columns(m)
                if cols:
                    pentry["columns"] = cols
            _props_for(props, tgt["dir"], pentry)
            prev = tgt["name"]

    _write_property_files(root, props, standard)

    import json as _json
    (root / "migration_manifest.json").write_text(_json.dumps(
        {"project": pipeline.name, "generator": "MetaBridge",
         "layout": layout,
         "source_platform": platform_label,
         "target_platform": str(pipeline.metadata.get("target_platform", "")
                                 or pipeline.metadata.get("dialect", "")),
         "objects": manifest}, indent=2) + "\n", encoding="utf-8")

    # workflow orchestration: model dependencies live in dbt's own DAG;
    # everything dbt cannot express becomes a job spec (module 25)
    dags = pipeline.metadata.get("workflow_dags") or []
    if dags:
        from .orchestration import write_orchestration
        model_names = {m.name: names[m.name] for m in pipeline.mappings}
        write_orchestration(root, dags, "dbt", model_names=model_names)

    # shared macros for reused mapplets (module 21)
    reuse = pipeline.metadata.get("mapplet_reuse") or {}
    comps = pipeline.metadata.get("mapplet_components") or {}
    shared = sorted(k for k, v in reuse.items() if v.get("shared_artifact"))
    if shared:
        from ..parsers.pc_mapplet import render_dbt_macro
        documented = []
        for name in shared:
            (root / "macros" / ("mapplet_%s.sql" % _safe(name))
             ).write_text(render_dbt_macro(comps[name]), encoding="utf-8")
            documented.append({
                "name": "mapplet_%s" % _safe(name),
                "description": "Reusable mapplet '%s', shared by %d mapping(s)"
                               " in the source estate: %s."
                               % (name, len(reuse[name].get("used_by") or []),
                                  ", ".join(reuse[name].get("used_by") or [])),
                "arguments": [{
                    "name": "relation", "type": "string",
                    "description": "the upstream relation to apply it over"}]})
        # a property file for the macros, as dbt's own layout has — written
        # only when there ARE macros, since an empty one documents nothing
        (root / "macros" / "_macros.yml").write_text(
            render_template("macros.yml.j2", macros=documented),
            encoding="utf-8")

    if standard:
        _write_project_files(root, pipeline)

    # every var the rendered models reference, with the default the source
    # declared for it; a var with no known default is NOT declared (a declared
    # null renders as the string "None" in SQL — silently wrong is worse than a
    # loud failure) and is raised as a MANUAL item instead
    declared, required = _collected_vars()
    if declared:
        proj["vars"] = declared
    if required:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.MANUAL, code="DBT_VAR_REQUIRED",
            message="dbt var(s) with no default value in the source metadata: "
                    "%s — the project will not compile until they are supplied"
                    % ", ".join(required),
            obj=pipeline.name,
            suggestion="Add them to the vars: block in dbt_project.yml, or "
                       "pass `dbt run --vars '{%s}'`."
                       % ", ".join("%s: <value>" % v for v in required)))
    (root / "dbt_project.yml").write_text(
        yaml.safe_dump(proj, sort_keys=False), encoding="utf-8")

    # git does not track an empty directory, so a layer that happens to be
    # empty would vanish on commit and the project's shape would change
    # between the machine that generated it and the one that cloned it
    for d in _PROJECT_DIRS:
        here = root / d
        if not any(f for f in here.iterdir() if f.name != ".gitkeep"):
            (here / ".gitkeep").write_text("", encoding="utf-8")

    # the ref graph the models actually emitted, checked against what was
    # actually written (see _validate_graph)
    _finish_graph(pipeline)


# every directory dbt looks in, created up front so the project's shape is
# legible even where a layer came out empty
_PROJECT_DIRS = ("models/staging", "models/intermediate", "models/marts",
                 "macros", "tests", "tests/generic", "snapshots", "seeds",
                 "analyses")


def _props_for(props: Dict[str, List[dict]], directory: str,
               entry: dict) -> None:
    props.setdefault(directory, []).append(entry)


def _props_filename(directory: str) -> str:
    """dbt's own convention for a folder's property file.

    The leading underscore sorts it to the top of the folder, and the name says
    which group it documents: `_crm__models.yml`, `_int_finance__models.yml`.
    """
    parts = directory.split("/")
    group = parts[1] if len(parts) > 1 else parts[0]
    if parts[0] == "intermediate":
        return "_int_%s__models.yml" % group
    return "_%s__models.yml" % group


def _write_property_files(root: Path, props: Dict[str, List[dict]],
                          standard: bool) -> None:
    if not standard:
        # the legacy layered layout kept one flat file for all three layers
        models = [e for _d, entries in sorted(props.items()) for e in entries]
        (root / "models" / "schema.yml").write_text(
            yaml.safe_dump({"version": 2, "models": models}, sort_keys=False),
            encoding="utf-8")
        return
    for directory, entries in sorted(props.items()):
        d = root / "models" / directory
        d.mkdir(parents=True, exist_ok=True)
        (d / _props_filename(directory)).write_text(
            yaml.safe_dump({"version": 2, "models": entries},
                           sort_keys=False), encoding="utf-8")


def _write_project_files(root: Path, pipeline: Pipeline) -> None:
    """The project files a hand-built dbt repo always has and we never wrote.

    packages.yml matters beyond convention: the generator already recommends
    dbt_utils.generate_surrogate_key in its SEQUENCE_AS_ROW_NUMBER warning, so
    the package it points at should be declared.
    """
    (root / ".gitignore").write_text(
        render_template("gitignore.j2"), encoding="utf-8")
    (root / "seeds" / "_seeds.yml").write_text(
        render_template("seeds.yml.j2"), encoding="utf-8")
    if not (root / "packages.yml").exists():
        # The template leaves every package COMMENTED OUT on purpose: dbt
        # refuses to parse a project whose packages.yml names a package that is
        # not installed, so declaring one would make the generated project
        # unusable until someone ran `dbt deps` with network access — wrong for
        # a product with an air-gapped install path, and wrong when no
        # generated model calls a package macro anyway.
        (root / "packages.yml").write_text(
            render_template("packages.yml.j2"), encoding="utf-8")
    # selectors for a staged cutover: bring the raw layer up first, then the
    # curated one, rather than running the whole project on the first attempt
    (root / "selectors.yml").write_text(yaml.safe_dump({"selectors": [
        {"name": "staging_only",
         "description": "Raw landing layer only — run this first on a new "
                        "environment.",
         "definition": {"method": "path", "value": "models/staging"}},
        {"name": "curated",
         "description": "Everything downstream of staging.",
         "definition": {"union": [
             {"method": "path", "value": "models/intermediate"},
             {"method": "path", "value": "models/marts"}]}},
    ]}, sort_keys=False), encoding="utf-8")


def _write_sources_yaml(pipeline: Pipeline, root: Path,
                        layout: str = "standard") -> None:
    """One dbt source per source system, in that system's staging folder.

    A table THIS PROJECT BUILDS is excluded. The IR records a downstream
    mapping's input as a SourceTable whether or not another mapping produces
    it, so declaring those as sources told the reader a table the project
    rebuilds every run was raw input — and pointed source() at a relation in
    the wrong database.

    The source database and schema are preserved rather than flattened to
    raw/raw. A dbt source name resolves to exactly one database, so where one
    name genuinely spans several we keep the first and say so.
    """
    from .dbt_naming import built_tables
    built = built_tables(pipeline)
    standard = layout != "layered"
    kept, dropped = [], []
    for s in pipeline.sources:
        (dropped if s.name.lower() in built else kept).append(s)
    if dropped:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.INFO, code="SOURCE_IS_BUILT_HERE",
            message="%d source definition(s) name a table this project "
                    "builds, so they are not declared as dbt sources: %s"
                    % (len(dropped),
                       ", ".join(sorted(s.name for s in dropped))),
            obj=pipeline.name,
            suggestion="Downstream models ref() the model that builds the "
                       "table instead."))
    if not kept:
        return

    by_group: Dict[str, List] = {}
    for s in kept:
        by_group.setdefault(_source_name(s), []).append(s)
    groups = [(g, tables, _source_entry(g, tables, pipeline))
              for g, tables in sorted(by_group.items())]

    if standard:
        for group, tables, entry in groups:
            d = root / "models" / "staging" / group
            d.mkdir(parents=True, exist_ok=True)
            (d / ("_%s__sources.yml" % group)).write_text(yaml.safe_dump(
                {"version": 2, "sources": [entry]}, sort_keys=False),
                encoding="utf-8")
            (d / ("_%s__docs.md" % group)).write_text(
                _source_docs(group, tables, pipeline), encoding="utf-8")
        return
    # legacy layered layout: one models/staging/sources.yml for everything
    (root / "models" / "staging").mkdir(parents=True, exist_ok=True)
    (root / "models" / "staging" / "sources.yml").write_text(
        yaml.safe_dump({"version": 2, "sources": [e for _g, _t, e in groups]},
                       sort_keys=False), encoding="utf-8")


def _source_entry(group: str, tables: List, pipeline: Pipeline) -> dict:
    """One `sources:` entry: name, schema, database, tables and columns."""
    dbs = sorted({t.database for t in tables if t.database})
    schemas = sorted({t.schema for t in tables if t.schema})
    entry: Dict[str, object] = {"name": group,
                                "schema": schemas[0] if schemas else group}
    if pipeline.metadata.get("landing_target"):
        # cross-platform landing: the tables' database is the SOURCE database,
        # but the dbt source has to resolve in the profile's TARGET database,
        # so no database is pinned here
        dbs = []
    if dbs:
        entry["database"] = dbs[0]                 # preserved from the manifest
        if len(dbs) > 1:
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.WARNING, code="SCHEMA_MULTI_DATABASE",
                message="Source '%s' spans multiple databases (%s); the dbt "
                        "source uses '%s'. Split it per database to model the "
                        "others." % (group, ", ".join(dbs), dbs[0]),
                obj=group))
    if len(schemas) > 1:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.WARNING, code="SOURCE_MULTI_SCHEMA",
            message="Source '%s' spans schemas %s; the dbt source uses '%s'."
                    % (group, ", ".join(schemas), schemas[0]), obj=group))
    out = []
    for t in tables:
        tbl: Dict[str, object] = {"name": t.name}
        if t.columns:
            tbl["columns"] = [_column_entry(c) for c in t.columns]
            unknown = _undeclared(t.columns)
            if unknown:
                pipeline.issues.append(ConversionIssue(
                    severity=IssueSeverity.WARNING, code="TYPE_UNDECLARED",
                    message="Source %s has %d column(s) with no declared type "
                            "(%s) — documented without a data_type rather "
                            "than assumed to be text"
                            % (t.name, len(unknown), ", ".join(unknown[:8])),
                    obj=t.name,
                    suggestion="Add the type to the source manifest so the "
                               "landing DDL and validation tests can use it."))
            if any(_decimal_needs_fallback(c) for c in t.columns):
                pipeline.issues.append(ConversionIssue(
                    severity=IssueSeverity.WARNING,
                    code="NUMERIC_PRECISION_FALLBACK",
                    message="Source %s has decimal column(s) without declared "
                            "precision — using documented fallback "
                            "decimal(%d,%d)"
                            % (t.name, *_DECIMAL_FALLBACK), obj=t.name,
                    suggestion="Declare precision/scale in the manifest to "
                               "preserve exact numeric types."))
        out.append(tbl)
    entry["tables"] = out
    return entry


def _source_docs(group: str, tables: List, pipeline: Pipeline) -> str:
    """A doc block per source system.

    dbt renders these into its docs site, and the facts in them — origin
    platform, database, schema, table and column counts — are ones we already
    hold and used to discard.
    """
    dbs = sorted({t.database for t in tables if t.database}) or ["(unset)"]
    schemas = sorted({t.schema for t in tables if t.schema}) or ["(unset)"]
    return render_template(
        "source_docs.md.j2", group=group, project=pipeline.name,
        platform=pipeline.source_format or "an unnamed platform",
        databases=", ".join(dbs), schemas=", ".join(schemas),
        table_count=len(tables),
        column_count=sum(len(t.columns) for t in tables))


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


# Longest text still plausible as an object NAME. Above this it is an artifact.
_MAX_LABEL = 128


def _origin_label(m: Mapping) -> str:
    """The source object's own name, for descriptions and traceability meta.

    ``Mapping.origin`` is documented as the ORIGINAL ARTIFACT — raw SQL or an
    XML fragment — and only happens to be a name for PowerCenter, where it is
    the mapping name. Using it unguarded put a whole SQL statement into a dbt
    model's ``description:`` whenever the source was a warehouse script, and
    made the generated project differ depending on whether it had been through
    the CIR (which does not carry ``origin``).
    """
    origin = str(m.origin or "").strip()
    if origin and len(origin) <= _MAX_LABEL \
            and not origin.splitlines()[1:] and " " not in origin:
        return origin
    return m.name


def _target_columns(m: Mapping) -> List[dict]:
    tgts = m.by_type(TransformationType.TARGET)
    if not tgts or not tgts[0].ports:
        return []
    out = []
    fallback = False
    for p in tgts[0].ports:
        col = _column_entry(p)
        fallback = fallback or _decimal_needs_fallback(p)
        if p.name in m.unique_key:
            col["tests"] = ["unique", "not_null"]
        out.append(col)
    unknown = _undeclared(tgts[0].ports)
    if unknown:
        m.add_issue(IssueSeverity.WARNING, "TYPE_UNDECLARED",
                    "Target of '%s' has %d column(s) with no declared type "
                    "(%s) — documented without a data_type rather than "
                    "assumed to be text"
                    % (m.name, len(unknown), ", ".join(unknown[:8])),
                    suggestion="Declare them on the target definition, or add "
                               "the type to the source manifest, so the "
                               "generated DDL and tests can use it.")
    if fallback:
        m.add_issue(IssueSeverity.WARNING, "NUMERIC_PRECISION_FALLBACK",
                    "Target of '%s' has decimal column(s) without declared "
                    "precision — using documented fallback decimal(%d,%d)"
                    % (m.name, *_DECIMAL_FALLBACK),
                    suggestion="Declare precision/scale in the source "
                               "manifest to preserve exact numeric types.")
    return out


# ---------------------------------------------------------------------------
# Model SQL rendering
# ---------------------------------------------------------------------------

def render_snapshot_sql(m: Mapping, pipeline: Pipeline, mapping_names,
                        name: str = "") -> str:
    """SCD Type 2 mapping -> dbt {% snapshot %} block.

    `name` is the snapshot's NODE name and is what the block tag declares.
    dbt reads a snapshot's identity from that tag, not from the filename, so
    passing it in — rather than deriving it here a second time — is what keeps
    the tag, the file and every downstream ref() on one value. They diverged
    once already: the file became snap_<entity> while the tag still said the
    mapping name, and `dbt parse` rejected the project over a ref to a node
    that did not exist.
    """
    name = name or _safe(m.name)
    scd = dict(m.properties.get("scd", {}) or {})
    strategy = str(scd.get("strategy", "timestamp"))
    key = m.unique_key[0] if m.unique_key else "id"
    cfg = ["target_schema='%s'" % scd.get("target_schema", "snapshots"),
           "unique_key='%s'" % key, "strategy='%s'" % strategy]
    if strategy == "timestamp":
        cfg.append("updated_at='%s'" % (scd.get("updated_at") or "updated_at"))
    else:
        cols = scd.get("check_cols") or "all"
        cfg.append("check_cols=%s" % (repr(list(cols)) if isinstance(cols, list)
                                      else "'all'"))
    body, terminal = _render_graph(m, pipeline, mapping_names)
    select = ("with %s\n\nselect * from %s" % (",\n\n".join(body), terminal)) \
        if body else "select 1 as placeholder"
    return render_template("snapshot.sql.j2", name=name,
                           config=", ".join(cfg), select=select)


_PARAM_RE = None  # lazily compiled


def render_model_sql(m: Mapping, pipeline: Pipeline, mapping_names,
                     config: Optional[str] = None, layer: str = "") -> str:
    clauses = m.properties.get("merge_clauses") or {}
    if clauses.get("delete"):
        m.add_issue(IssueSeverity.WARNING, "MERGE_DELETE_DBT",
                    "The update strategy routes DD_DELETE rows, but "
                    "dbt-core's merge strategy cannot express a DELETE "
                    "clause",
                    detail=str(clauses["delete"])[:150],
                    suggestion="Use the adapter's custom merge "
                               "(Databricks: incremental_strategy='merge' "
                               "+ skip_matched_step tuning) or a post_hook "
                               "DELETE — hooks only where necessary.")
    header = config if config is not None else _config_block(m, layer)
    body, terminal = _render_graph(m, pipeline, mapping_names)
    if not body:
        return render_template("model_empty.sql.j2", header=header)
    sql = render_template("model.sql.j2", header=header,
                          ctes=",\n\n".join(body), terminal=terminal)

    # parameter/variable engine (module 26): classification-aware
    # substitution — runtime params become vars (with defaults), stateful
    # variables are marked in-line (never silently made static), system
    # variables map to dbt run context, env config becomes env_var
    import re
    global _PARAM_RE
    if _PARAM_RE is None:
        _PARAM_RE = re.compile(r"\$\$(\w+)")
    registry = {e["name"]: e for e in
                (pipeline.metadata.get("parameter_registry") or {})
                .get("parameters", [])}
    stateful, params = [], []

    def _dd(mo):
        name = mo.group(1)
        e = registry.get("$$" + name) or {}
        default = str(e.get("default", "") or "")
        if e.get("classification") == "stateful_variable":
            # A stateful variable is a WATERMARK, and dbt expresses that as
            # is_incremental() + max(), which the FILTER renderer now emits.
            # Anything still arriving here is a stateful value we could not
            # place, so it stays a var — but deliberately with NO default.
            # PowerCenter persisted this value across runs; substituting the
            # variable's declared default would turn a moving watermark into a
            # fixed date and silently reload from it forever. Left undeclared,
            # dbt refuses to compile and names the var, which is the honest
            # outcome (see the STATEFUL_VARIABLE review item for the recipe).
            #
            # It also gets no inline /* ... */ note: the substitution point is
            # frequently already inside a comment, and a nested block comment
            # is a syntax error on every engine that does not nest them
            # (BigQuery, plain ANSI).
            stateful.append(name)
            _want_var(name, "")
            return "{{ var('%s') }}" % name
        params.append(name)
        _want_var(name, default)
        return "{{ var('%s'%s) }}" % (
            name, ", '%s'" % default if default else "")

    sql = _PARAM_RE.sub(_dd, sql)
    if params:
        m.add_issue(IssueSeverity.WARNING, "MAPPING_PARAMETER_AS_VAR",
                    "Informatica mapping parameter(s) converted to dbt vars: %s"
                    % ", ".join("$$" + p for p in sorted(set(params))),
                    suggestion="Set these in dbt_project.yml vars: or --vars at run time.")

    sysvars = []

    def _pm(mo):
        name = mo.group(1)
        sysvars.append(name)
        e = registry.get("$" + name) or {}
        if e.get("classification") == "environment_configuration":
            return "{{ env_var('%s', '') }}" % name
        if name in ("PMWorkflowRunId", "PMWorkflowRunInstanceName"):
            return "{{ invocation_id }}"
        if name in ("PMSessionName", "PMMappingName"):
            return "'%s'" % _safe(m.name)
        default = str(e.get("default", "") or "")
        _want_var(name, default)
        return "{{ var('%s'%s) }}" % (
            name, ", '%s'" % default if default else "")

    sql = re.sub(r"\$(PM\w+)", _pm, sql)
    if sysvars:
        m.add_issue(IssueSeverity.INFO, "SYSTEM_VARIABLE",
                    "PowerCenter system variable(s) mapped to dbt run "
                    "context: %s"
                    % ", ".join("$" + v for v in sorted(set(sysvars))),
                    suggestion="$PMWorkflowRunId -> invocation_id; "
                               "session/mapping names -> model name; "
                               "directories -> env_var; timestamps -> "
                               "run_started_at.")
    return sql


# What dbt_project.yml declares per folder. A model config that repeats one of
# these is pure noise — and worse, it is what made those declarations
# decorative: a model-level config always wins, so nobody could change a
# layer's materialization in the one place dbt intends.
_LAYER_DEFAULT = {"staging": "view", "intermediate": "view", "marts": "table"}


def _config_block(m: Mapping, layer: str = "") -> str:
    scd2 = m.properties.get("scd2_cir") or {}
    if m.load_strategy == LoadStrategy.SCD2 and \
            scd2.get("dbt_strategy") == "incremental_scd2" and \
            scd2.get("surrogate_key"):
        # incremental SCD2 model: each VERSION is a row, so the merge key
        # is the per-version surrogate — merging on the business key would
        # overwrite history (that would be SCD1)
        base = ("materialized='incremental', unique_key='%s', "
                "incremental_strategy='merge'" % scd2["surrogate_key"])
    elif m.load_strategy == LoadStrategy.MERGE and m.unique_key:
        keys = ", ".join("'%s'" % k for k in m.unique_key)
        key_part = "unique_key=[%s], " % keys if len(m.unique_key) > 1 \
            else "unique_key='%s', " % m.unique_key[0]
        base = "materialized='incremental', %sincremental_strategy='merge'" % key_part
    elif m.load_strategy == LoadStrategy.DELETE_INSERT:
        base = "materialized='incremental', incremental_strategy='delete+insert'"
    elif m.load_strategy == LoadStrategy.APPEND:
        base = "materialized='incremental', incremental_strategy='append'"
    elif m.load_strategy == LoadStrategy.VIEW:
        base = "materialized='view'"
    else:
        base = "materialized='table'"
    # Source Qualifier Pre/Post SQL carries over as dbt hooks
    import json as _json
    extra = ""
    for prop, opt in (("pre_sql", "pre_hook"), ("post_sql", "post_hook")):
        v = m.properties.get(prop)
        if v:
            extra += ", %s=%s" % (opt, _json.dumps(str(v)))
    # A folder-level `+schema:` covers most models; one whose schema
    # disagrees with its neighbours' has to say so itself, or dbt builds
    # it alongside them.
    schema = (_SCHEMA_OF.get() or {}).get(m.name, "")
    if schema:
        extra += ", schema='%s'" % schema.replace("'", "''")
    alias = (_ALIAS_OF.get() or {}).get(m.name, "")
    if alias:
        extra += ", alias='%s'" % alias.replace("'", "''")
    if not extra and layer and \
            base == "materialized='%s'" % _LAYER_DEFAULT.get(layer, ""):
        return ""                       # dbt_project.yml already says this
    return "{{ config(%s) }}\n\n" % (base + extra)


def _topo_order(m: Mapping) -> List[Transformation]:
    """Topological order over links; SOURCE and TARGET excluded."""
    skip = {TransformationType.SOURCE, TransformationType.TARGET}
    # __OUTPUT__ is normally just a marker naming where the mapping's output
    # is. When it carries the TARGET's field map it is a real projection —
    # `GENDER_STD as GENDER` — and skipping it leaves both the raw column and
    # the cleaned one in the result, with the original name still holding the
    # value the logic was written to replace.
    nodes = [t for t in m.transformations
             if t.type not in skip
             and (t.name != "__OUTPUT__" or t.properties.get("projection"))]
    name_set = {t.name for t in nodes}
    incoming: Dict[str, set] = {t.name: set() for t in nodes}
    for l in m.links:
        if l.to_transformation in name_set and l.from_transformation in name_set:
            incoming[l.to_transformation].add(l.from_transformation)
    ordered, done = [], set()
    pool = list(nodes)
    while pool:
        progress = False
        for t in list(pool):
            if incoming[t.name] <= done:
                ordered.append(t)
                done.add(t.name)
                pool.remove(t)
                progress = True
        if not progress:  # cycle — emit remaining as-is
            ordered.extend(pool)
            break
    return ordered


# SQL targets where RANK renders as QUALIFY instead of a filtered subquery.
# Databricks supports QUALIFY too but the spec keeps it on CTE + filter;
# dbt output stays portable (adapter-agnostic) CTE + filter.
QUALIFY_DIALECTS = {"snowflake", "teradata", "bigquery"}

import contextvars as _contextvars

# Models whose schema cannot be stated at the folder level (their folder holds
# a mix) carry `schema=` in their own config instead. Keyed by mapping name.
_SCHEMA_OF = _contextvars.ContextVar("mb_dbt_schema_of", default={})

# Models whose RELATION keeps the legacy table's name while the model file
# keeps the dbt convention. Keyed by mapping name.
_ALIAS_OF = _contextvars.ContextVar("mb_dbt_alias_of", default={})

_QUALIFY_DIALECT = _contextvars.ContextVar("mb_qualify", default=False)

# Which project layout is being emitted. Read by _source_name (and so by every
# source() call, sources.yml entry and staging subfolder), which is called from
# closures too deep to thread a parameter through.
_LAYOUT = _contextvars.ContextVar("mb_dbt_layout", default="standard")

# (target dialect, source platform) when this project's sources are LANDED on
# another platform before dbt reads them — see _dbt_type. Empty otherwise.
_LANDED_IN = _contextvars.ContextVar("mb_dbt_landed", default=())

# The warehouse-SQL generator transpiles its statements to the target dialect
# (`sql_generator._transpile`); the dbt generator wrote model bodies verbatim,
# so models came out in CANONICAL SQL. That is fine until a function's
# SIGNATURE differs per dialect — `DATEDIFF(a, b, year)` is generic, Snowflake
# wants `DATEDIFF(YEAR, b, a)` and reads the generic form as a column named
# YEAR, failing with "invalid identifier 'YEAR'" at run time rather than at
# generate time. Model bodies cannot be transpiled whole (they carry Jinja);
# the EXPRESSIONS inside them are pure SQL and can.
_TARGET_DIALECT = _contextvars.ContextVar("mb_dbt_dialect", default="")


def _dialect_expr(sql: str) -> str:
    """A bare SQL expression rendered for the target dialect."""
    dialect = _TARGET_DIALECT.get()
    if not dialect or not sql:
        return sql
    try:
        import sqlglot
        return sqlglot.parse_one(sql, read=None).sql(dialect=dialect)
    except Exception:  # noqa: BLE001
        # An expression sqlglot cannot parse is left exactly as it was: the
        # canonical form is likelier to be right than a half-converted one.
        return sql


def render_plain_select(m: Mapping, pipeline: Pipeline, mapping_names: set,
                        dialect: str = "") -> str:
    """The mapping as one plain SELECT (CTE per node) with real table names —
    used by the warehouse-SQL generator, which wraps it in DDL/DML."""
    token = _QUALIFY_DIALECT.set(dialect in QUALIFY_DIALECTS)
    try:
        body, terminal = _render_graph(m, pipeline, mapping_names,
                                       plain=True)
    finally:
        _QUALIFY_DIALECT.reset(token)
    if not body:
        return "select 1 as placeholder -- REVIEW: empty mapping"
    return "with %s\n\nselect * from %s" % (",\n\n".join(body), terminal)


def _render_graph(m: Mapping, pipeline: Pipeline, mapping_names: set,
                  plain: bool = False):
    tx_by_name = {t.name: t for t in m.transformations}
    source_ref_cache: Dict[str, str] = {}
    # The model THIS mapping renders as. A model may not ref() itself: a
    # staging model reads its own source table, and a table read by the same
    # statement that rebuilds it is a dbt cycle, not a dependency.
    self_model = ""
    if isinstance(mapping_names, dict):
        self_model = mapping_names.get(m.name) or \
            mapping_names.get(m.name.lower()) or ""

    def model_for(table: str) -> str:
        if isinstance(mapping_names, dict):
            return mapping_names.get(table) or \
                mapping_names.get(table.lower()) or ""
        if table in mapping_names or \
                table.lower() in {n.lower() for n in mapping_names}:
            return _safe(table)
        return ""

    def dbt_relation(table: str) -> str:
        """The dbt relation for a raw table name, or "" when there is none.

        Shared by the SOURCE renderer and the SQL-override rewriter, so both
        resolve a table the same way and both land on the ref graph. Returns
        "" in plain mode: the warehouse-SQL generator wants real table names,
        and rewriting them there would be wrong, not incomplete."""
        if plain or not table:
            return ""
        model = model_for(table)
        if model and model != self_model:
            # deterministic-name plan (module 27): stg_/int_/dim_/fct_
            _ref(self_model or m.name, model)
            return "{{ ref('%s') }}" % model
        s = next((s for s in pipeline.sources
                  if s.name.lower() == table.lower()), None)
        if s is not None:
            _src(self_model or m.name, _source_name(s), s.name)
            return "{{ source('%s', '%s') }}" % (_source_name(s), s.name)
        # Neither a generated model nor a declared source: a physical relation
        # dbt does not manage. Recorded HERE, once, so every caller reports it
        # against the same MODEL name — recording it against the mapping name
        # instead left the node on the lineage graph with no edge into it,
        # which is precisely the silence this is meant to break.
        _unmanaged(self_model or m.name, table)
        return ""

    def source_relation(src: Transformation) -> str:
        table = str(src.properties.get("table", src.name))
        if table in source_ref_cache:
            return source_ref_cache[table]
        if plain:
            schema = str(src.properties.get("schema", "") or "")
            rel = "%s.%s" % (schema, table) if schema else table
        else:
            # an unresolved relation is left physical and recorded by
            # dbt_relation, so it shows on the lineage graph as a VISIBLE
            # break rather than as a missing edge
            rel = dbt_relation(table) or table
        source_ref_cache[table] = rel
        return rel

    def upstream_names(t: Transformation) -> List[str]:
        ups = []
        for l in m.links:
            if l.to_transformation == t.name and l.from_transformation not in ups:
                ups.append(l.from_transformation)
        return ups

    def upstream_cte(t: Transformation) -> Optional[str]:
        """Rendered FROM-clause for t's (single) upstream."""
        for un in upstream_names(t):
            ut = tx_by_name.get(un)
            if ut is None:
                continue
            if ut.type == TransformationType.SOURCE:
                return source_relation(ut)
            return _cte_name(ut)
        return None

    # reused mapplets: call the shared macro instead of repeating logic
    # (dbt mode only; instances with folded pass-through extras keep the
    # inline form — the macro would drop their columns)
    skip: set = set()
    macro_at: Dict[str, tuple] = {}
    if not plain:
        reuse = pipeline.metadata.get("mapplet_reuse") or {}
        comps = pipeline.metadata.get("mapplet_components") or {}
        groups: Dict[str, List[Transformation]] = {}
        for t in m.transformations:
            inst = t.properties.get("mapplet_instance")
            mpname = t.properties.get("from_mapplet")
            if inst and reuse.get(mpname, {}).get("shared_artifact"):
                groups.setdefault(str(inst), []).append(t)
        for inst, members in groups.items():
            mpname = str(members[0].properties["from_mapplet"])
            comp = comps.get(mpname) or {}
            names = {t.name for t in members}
            exits = [t for t in members
                     if any(l.from_transformation == t.name and
                            l.to_transformation not in names
                            for l in m.links)]
            entries = [t for t in members
                       if any(l.to_transformation == t.name and
                              l.from_transformation not in names
                              for l in m.links)]
            if len(exits) != 1 or len(entries) != 1:
                continue
            outs = {c.lower() for c in
                    (comp.get("interface") or {}).get("outputs", [])}
            if {p.name.lower() for p in exits[0].ports} != outs:
                continue          # bypass-folded instance: keep inline
            skip |= names - {exits[0].name}
            macro_at[exits[0].name] = (mpname, entries[0])

    ctes: List[str] = []
    terminal = None
    for t in _topo_order(m):
        if t.name in skip:
            continue
        if t.name in macro_at:
            mpname, entry = macro_at[t.name]
            up = upstream_cte(entry) or ""
            ctes.append("%s as (\n    {{ mapplet_%s('%s') }}\n)"
                        % (_cte_name(t), _safe(mpname), up))
            terminal = _cte_name(t)
            continue
        body = _render_node(t, m, tx_by_name, upstream_cte, upstream_names,
                            source_relation, dbt_relation, plain, pipeline)
        if body is None:
            continue
        ctes.append("%s as (\n%s\n)" % (_cte_name(t), _indent(body)))
        terminal = _cte_name(t)

    out = m.transformation("__OUTPUT__")
    # A projecting __OUTPUT__ was rendered as a CTE of its own and IS the
    # terminal; redirecting to its upstream would discard the projection.
    if out is not None and out.properties.get("upstream") \
            and not out.properties.get("projection"):
        up = tx_by_name.get(str(out.properties["upstream"]))
        if up is not None:
            terminal = _cte_name(up)
    return ctes, terminal


def _ident(name: str) -> str:
    """A column name as it must appear in generated SQL.

    Quoted only where it has to be — a reserved word, or a character illegal
    in a bare identifier. Everything else stays bare, because an unquoted
    identifier folds to UPPER case on Snowflake and Oracle while a quoted one
    does not: quoting indiscriminately would stop `"customer_id"` matching a
    column stored as CUSTOMER_ID and break far more than it rescued.

    The landing DDL applies the same rule (ddl_generator._quote delegates to
    the same function), so a table created with a bare `ORDER` column and a
    model selecting it cannot disagree about a column that exists.
    """
    from ..sqlx.identifiers import quote_identifier
    return quote_identifier(_TARGET_DIALECT.get(), name)


def _cte_name(t: Transformation) -> str:
    return _safe(t.name.lower())


def _indent(s: str, pad: str = "    ") -> str:
    return "\n".join(pad + l for l in s.split("\n"))


def _dedup_order(t: Transformation, tx_by_name, upstream_names,
                 group_by, hops: int = 4) -> str:
    """ORDER BY for a keep-first-row dedup, taken from an upstream Sorter.

    A Sorter renders as a pass-through CTE because ORDER BY is meaningless
    inside a model — but its keys are the only record of which row the source
    mapping intended to keep, so they have to be recovered here or the choice
    becomes arbitrary. Group-by columns are dropped from the ordering: they
    are constant within a partition and only add noise."""
    seen: set = set()
    frontier = list(upstream_names(t))
    lowered = {g.lower() for g in group_by}
    while frontier and hops > 0:
        nxt: List[str] = []
        for n in frontier:
            if n in seen:
                continue
            seen.add(n)
            up_t = tx_by_name.get(n)
            if up_t is None:
                continue
            keys = up_t.properties.get("sort_keys") or []
            if up_t.type == TransformationType.SORTER and keys:
                parts = ["%s %s" % (k.get("port"),
                                    str(k.get("order", "ASC")).lower())
                         for k in keys
                         if k.get("port") and k["port"].lower() not in lowered]
                if parts:
                    return ", ".join(parts)
            nxt.extend(upstream_names(up_t))
        frontier, hops = nxt, hops - 1
    return ""


def _render_node(t: Transformation, m: Mapping, tx_by_name, upstream_cte,
                 upstream_names, source_relation, dbt_relation=None,
                 plain: bool = True,
                 pipeline: Optional[Pipeline] = None) -> Optional[str]:
    up = upstream_cte(t)

    if t.type == TransformationType.SOURCE_QUALIFIER:
        override = str(t.properties.get("sql_override", "") or "").strip()
        if override:
            return _override_sql(override.rstrip(";"), m, t,
                                 dbt_relation, plain)
        # A Source Qualifier passes the source TABLE's columns through. When
        # another mapping rebuilds that table, the relation underneath is
        # that mapping's model and projects only what it populates — so the
        # pass-through has to be narrowed to what is really there, or it
        # names a column the warehouse rejects.
        have = _relation_columns(t, pipeline, tx_by_name, upstream_names)
        usable = [q for q in t.ports if not have or q.name.lower() in have]
        lost = [q.name for q in t.ports if q not in usable]
        if lost:
            m.add_issue(
                IssueSeverity.WARNING, "JOIN_COLUMN_NOT_IN_UPSTREAM",
                "'%s' reads %s from a relation that does not project %s — "
                "the source TABLE has the column, but the model that "
                "rebuilds that table does not carry it through. Dropped "
                "from the projection; keeping it would compile and then "
                "fail on the warehouse."
                % (t.name, ", ".join(lost), "them" if len(lost) > 1 else "it"),
                suggestion="Add %s to the target of the mapping that builds "
                           "the upstream table if it is needed downstream."
                           % ", ".join(lost))
        cols = ", ".join(_ident(q.name) for q in usable) or "*"
        src = up
        if src is None:
            srcs = t.properties.get("sources") or []
            src = str(srcs[0]) if srcs else "MISSING_SOURCE"
        return "select %s\nfrom %s" % (cols, src)

    if t.type == TransformationType.FILTER:
        raw_cond = str(t.properties.get("condition", "TRUE") or "").strip()
        if not raw_cond:
            # The property exists but is empty — the source declared a filter
            # whose condition we could not read. `.get(..., "TRUE")` does not
            # cover this: the key IS there, so the default never applies, and
            # the model came out with a bare `where` and no predicate, which
            # is a syntax error rather than a wrong result.
            #
            # Emitting TRUE keeps the SQL valid but the model then passes rows
            # the source filtered out, so that has to be said plainly.
            m.add_issue(IssueSeverity.MANUAL, "FILTER_CONDITION_UNREADABLE",
                        "Filter '%s' declares a condition the parser could "
                        "not read — the model does NOT filter and will pass "
                        "rows the source excluded" % t.name,
                        suggestion="Recover the predicate from the source "
                                   "object and add it to this model's WHERE "
                                   "clause before running it.")
            raw_cond = "TRUE"
        inc = None if plain else _incremental_filter(raw_cond, t, m)
        if inc is not None:
            return "select *\nfrom %s\n%s" % (up, inc)
        cond = _dialect_expr(raw_cond)
        return "select *\nfrom %s\nwhere %s" % (up, cond)

    if t.type == TransformationType.EXPRESSION:
        items = []
        for p in t.ports:
            if p.direction == "VARIABLE":
                continue
            if p.expression and p.expression.lower() != p.name.lower():
                items.append("%s as %s" % (_dialect_expr(p.expression),
                                           _ident(p.name)))
            else:
                items.append(_ident(p.name))
        # An IDMC Expression lists only the fields it ADDS; everything arriving
        # passes through by field rule. Projecting the listed ports alone drops
        # every inherited column — including ones the target maps and ones a
        # downstream dedup needs to order by.
        if t.properties.get("passthrough"):
            if not items:
                return "select *\nfrom %s" % up
            return "select *,\n    %s\nfrom %s" % (",\n    ".join(items), up)
        return "select\n    %s\nfrom %s" % (",\n    ".join(items), up)

    if t.type == TransformationType.AGGREGATOR:
        group_by = [str(g) for g in t.properties.get("group_by", [])]
        aggregates = [p for p in t.ports if p.expression]

        # Group-by with NO aggregate expression is not an aggregation — it is
        # Informatica's "keep one row per group" idiom, which relies on the
        # upstream Sorter to decide WHICH row survives. Emitting `group by`
        # here produced a select list with nothing in it: invalid SQL that
        # never reached a warehouse to be found wrong.
        if group_by and not aggregates:
            order = _dedup_order(t, tx_by_name, upstream_names, group_by)
            if order:
                m.add_issue(IssueSeverity.INFO, "AGGREGATOR_KEEPS_FIRST_ROW",
                            "Aggregator '%s' groups by %s with no aggregate "
                            "function — converted to a deduplication keeping "
                            "the first row per group, ordered by %s from the "
                            "upstream sorter"
                            % (t.name, ", ".join(group_by), order))
                order_by = order
            else:
                m.add_issue(IssueSeverity.MANUAL,
                            "AGGREGATOR_DEDUP_NONDETERMINISTIC",
                            "Aggregator '%s' groups by %s with no aggregate "
                            "function and nothing upstream sorts its input, so "
                            "which row survives each group is arbitrary"
                            % (t.name, ", ".join(group_by)),
                            suggestion="Add the column that should decide the "
                                       "surviving row to the generated "
                                       "ORDER BY, or add a Sorter upstream in "
                                       "the source mapping.")
                order_by = group_by[0]
            window = "row_number() over (partition by %s order by %s)" \
                % (", ".join(group_by), order_by)
            if _QUALIFY_DIALECT.get():
                return "select *\nfrom %s\nqualify %s = 1" % (up, window)
            return ("select * from (\n"
                    "    select *, %s as _mb_row\n"
                    "    from %s\n) deduped\nwhere _mb_row = 1"
                    % (window, up))

        items = []
        for p in t.ports:
            if p.expression:
                items.append("%s as %s" % (_dialect_expr(p.expression),
                                           _ident(p.name)))
            else:
                items.append(_ident(p.name))
        # Group-by columns must survive an aggregate projection: a port list
        # that names only the aggregates loses the very keys the rows are
        # grouped on.
        if group_by:
            named = {i.rsplit(" as ", 1)[-1].lower() for i in items}
            for g in group_by:
                if g.lower() not in named:
                    items.insert(0, g)
        sql = "select\n    %s\nfrom %s" % (",\n    ".join(items) or "*", up)
        if group_by:
            sql += "\ngroup by %s" % ", ".join(
                _ident(g) for g in group_by)
        return sql

    if t.type == TransformationType.JOINER:
        left_name = str(t.properties.get("left", ""))
        right_name = str(t.properties.get("right", ""))
        ups = upstream_names(t)
        if not left_name and len(ups) >= 1:
            left_name = ups[0]
        if not right_name and len(ups) >= 2:
            right_name = ups[1]

        def rel(n):
            tt = tx_by_name.get(n)
            if tt is None:
                return n
            if tt.type == TransformationType.SOURCE:
                return source_relation(tt)
            return _cte_name(tt)

        jt = {"INNER": "inner join", "LEFT": "left join", "RIGHT": "right join",
              "FULL": "full outer join"}.get(str(t.properties.get("join_type", "INNER")),
                                             "inner join")
        # Bulk-rename field rules resolve a column-name clash between the two
        # inputs, and the join condition is written against the RENAMED name.
        # The rename has to be carried into the projection and undone in the
        # condition, or the generated SQL references a column that exists on
        # neither side.
        lp = str(t.properties.get("left_prefix", "") or "")
        rp = str(t.properties.get("right_prefix", "") or "")
        cond_src = str(t.properties.get("condition", ""))
        if lp or rp:
            cond_src = _strip_join_prefixes(cond_src, lp, rp)

        def _cols_of(name):
            side = tx_by_name.get(name)
            return [p.name for p in side.ports] if side is not None else []

        cond = _qualify_join_condition(cond_src, "l", "r",
                                       _cols_of(left_name),
                                       _cols_of(right_name))

        left_cols = _relation_columns(tx_by_name.get(left_name), pipeline,
                                      tx_by_name, upstream_names)
        right_cols = _relation_columns(tx_by_name.get(right_name), pipeline,
                                       tx_by_name, upstream_names)
        have = left_cols | right_cols
        declared = set()
        for nm in (left_name, right_name):
            side = tx_by_name.get(nm)
            if side is not None:
                declared |= {q.name.lower() for q in side.ports}
        # Two different things look alike here and only one is a defect.
        #
        # A column DECLARED by a side whose relation does not project it is
        # the bug: the source table has it, the model that rebuilds that
        # table does not, and referencing it compiles and then fails on the
        # warehouse. Drop it.
        #
        # A column no side declares at all is not that. It may be provenance
        # this generator cannot see, and guessing a side for it would invent
        # one — so it stays bare, exactly as before, and the validator's
        # column check reports it if it is genuinely unresolvable.
        drop = {p.name.lower() for p in t.ports
                if have and p.name.lower() in declared
                and p.name.lower() not in have}
        keep = [p for p in t.ports if p.name.lower() not in drop]
        gone = [p.name for p in t.ports if p.name.lower() in drop]
        if gone:
            m.add_issue(
                IssueSeverity.WARNING, "JOIN_COLUMN_NOT_IN_UPSTREAM",
                "Join '%s' reads %s from a relation that does not project "
                "%s — the source TABLE has the column, but the model that "
                "rebuilds that table does not carry it through. Dropped "
                "from the projection; keeping it would compile and then "
                "fail on the warehouse."
                % (t.name, ", ".join(gone),
                   "them" if len(gone) > 1 else "it"),
                suggestion="Add %s to the target of the mapping that builds "
                           "the upstream table if it is needed downstream."
                           % ", ".join(gone))
        cols = ", ".join(
            _join_projection(p.name, t, m, tx_by_name, left_name, right_name,
                             str(t.properties.get("join_type", "INNER")),
                             left_cols, right_cols)
            for p in keep) or "*"
        if lp or rp:
            proj = []
            for alias, name, pfx, avail in (("l", left_name, lp, left_cols),
                                            ("r", right_name, rp, right_cols)):
                side = tx_by_name.get(name)
                if pfx and side is not None and side.ports:
                    # `side.ports` is the source TABLE's column list. When the
                    # relation is a model that rebuilds that table it projects
                    # fewer, and a prefixed rename of a column that is not
                    # there fails on the warehouse exactly like an unprefixed
                    # one — this branch just used to skip the check.
                    usable = [q for q in side.ports
                              if not avail or q.name.lower() in avail]
                    lost = [q.name for q in side.ports if q not in usable]
                    if lost:
                        m.add_issue(
                            IssueSeverity.WARNING,
                            "JOIN_COLUMN_NOT_IN_UPSTREAM",
                            "Join '%s' renames %s from a relation that does "
                            "not project %s — the source TABLE has the "
                            "column, but the model that rebuilds that table "
                            "does not carry it through. Dropped from the "
                            "projection; keeping it would compile and then "
                            "fail on the warehouse."
                            % (t.name, ", ".join(lost),
                               "them" if len(lost) > 1 else "it"),
                            suggestion="Add %s to the target of the mapping "
                                       "that builds the upstream table if it "
                                       "is needed downstream." % ", ".join(lost))
                    proj.extend("%s.%s as %s%s" % (alias, q.name, pfx, q.name)
                                for q in usable)
                else:
                    proj.append("%s.*" % alias)
            cols = ",\n    ".join(proj)
            return ("select\n    %s\nfrom %s as l\n%s %s as r\n    on %s"
                    % (cols, rel(left_name), jt, rel(right_name),
                       cond or "1 = 1"))

        return ("select %s\nfrom %s as l\n%s %s as r\n    on %s"
                % (cols, rel(left_name), jt, rel(right_name), cond or "1 = 1"))

    if t.type == TransformationType.SORTER:
        if t.properties.get("distinct"):
            return "select distinct *\nfrom %s" % up
        return "select *\nfrom %s" % up  # ORDER BY is meaningless inside a model

    if t.type == TransformationType.UNION:
        inputs = [str(i) for i in t.properties.get("inputs", [])] or upstream_names(t)

        def rel(n):
            tt = tx_by_name.get(n)
            if tt is None:
                return n
            return source_relation(tt) if tt.type == TransformationType.SOURCE \
                else _cte_name(tt)

        # explicit projection: PowerCenter unions POSITIONALLY, and CTE
        # column order can drift (folded passthroughs) — never `select *`
        out_cols = [f["name"] for f in
                    (t.properties.get("union_cir") or {}).get(
                        "output_columns", [])] or \
            [p.name for p in t.ports]
        if not out_cols:
            parts = ["select * from %s" % rel(i) for i in inputs]
            return "\nunion all\n".join(parts)
        parts = []
        renamed = False
        for i in inputs:
            side = tx_by_name.get(i)
            side_cols = [p.name for p in side.ports] if side is not None else []
            if len(side_cols) >= len(out_cols):
                # POSITIONAL: input k's k-th column becomes output column k.
                # Projecting the OUTPUT names off every input was wrong the
                # moment two inputs named a column differently — which is the
                # normal case for a union, and produced SQL naming a column
                # that side does not have.
                proj = ", ".join(
                    _ident(src) if src == out
                    else "%s as %s" % (_ident(src), _ident(out))
                    for src, out in zip(side_cols, out_cols))
                renamed = renamed or side_cols[:len(out_cols)] != out_cols
            else:
                proj = ", ".join(_ident(c) for c in out_cols)
            parts.append("select %s from %s" % (proj, rel(i)))
        if renamed:
            m.add_issue(IssueSeverity.INFO, "UNION_POSITIONAL_ALIASES",
                        "Union '%s' combines inputs whose columns are named "
                        "differently — each branch is aliased positionally to "
                        "the output columns (%s), which is what PowerCenter's "
                        "union does" % (t.name, ", ".join(out_cols[:6])))
        # PC Union NEVER deduplicates: UNION ALL, never bare UNION
        return "\nunion all\n".join(parts)

    if t.type == TransformationType.LOOKUP:
        table = str(t.properties.get("table", ""))
        cond = _qualify_join_condition(str(t.properties.get("condition", "")), "l", "lkp")
        cols = ", ".join(p.name for p in t.ports) or "*"
        override = str(t.properties.get("sql_override", "") or "")
        rel = "(\n    %s\n)" % override.rstrip(";") if override else table
        dedup_keys = [str(k) for k in t.properties.get("dedup_keys", [])]
        if dedup_keys:
            # match policy Use First/Last/Any: a lookup returns ONE row —
            # deduplicate before joining so cardinality is preserved
            direction = "desc" if t.properties.get("dedup_last") else "asc"
            keys = ", ".join(dedup_keys)
            rel = ("(\n    select * from (\n"
                   "        select *, row_number() over (partition by %s "
                   "order by %s %s) as _mb_lkp_rn\n"
                   "        from %s\n    ) d where _mb_lkp_rn = 1\n)"
                   % (keys, keys, direction, rel))
        # lookup_keys know which side is the lookup column — qualify
        # correctly even when key names differ (PC: <lookup col> = <input>)
        cir = t.properties.get("lookup_cir") or {}
        keys = cir.get("lookup_keys", [])
        pairs = [(k.get("input_port"), k.get("lookup_column"))
                 for k in keys if k.get("input_port")]
        if pairs:
            cond = " and ".join("l.%s = lkp.%s" % (i, c) for i, c in pairs)
        returns = {str(c).lower() for c in cir.get("return_columns", [])}
        if returns:
            cols = ", ".join(
                ("lkp.%s" % p.name if p.name.lower() in returns
                 else "l.%s" % p.name) for p in t.ports) or "*"
        return ("select %s\nfrom %s as l\nleft join %s as lkp\n    on %s"
                % (cols, up, rel, cond or "1 = 1"))

    if t.type == TransformationType.RANK:
        group_by = [str(g) for g in t.properties.get("group_by", [])]
        order_port = str(t.properties.get("order_port", "") or
                         (t.ports[0].name if t.ports else "1"))
        direction = "desc" if t.properties.get("top", True) else "asc"
        n = int(t.properties.get("number_of_ranks", 1) or 1)
        fn = str(t.properties.get("rank_function",
                                  "ROW_NUMBER")).lower()
        alias = str(t.properties.get("rank_index_port", "") or "_mb_rank")
        partition = ("partition by %s " % ", ".join(group_by)) if group_by else ""
        window = "%s() over (%sorder by %s %s)" % (fn, partition,
                                                   order_port, direction)
        m.add_issue(IssueSeverity.INFO, "RANK_AS_WINDOW",
                    "Rank '%s' converted to a %s() window (top %d by %s)"
                    % (t.name, fn.upper(), n, order_port))
        if _QUALIFY_DIALECT.get():
            # QUALIFY-capable target: no subquery needed
            extra = ", %s as %s" % (window, alias) \
                if alias != "_mb_rank" else ""
            return ("select *%s\nfrom %s\nqualify %s <= %d"
                    % (extra, up, window, n))
        return ("select * from (\n"
                "    select *, %s as %s\n"
                "    from %s\n) ranked\nwhere %s <= %d"
                % (window, alias, up, alias, n))

    if t.type == TransformationType.ROUTER:
        groups = t.properties.get("groups", []) or []
        if groups:
            whens = "\n        ".join(
                "when %s then '%s'" % (g.get("condition", "TRUE"), g.get("name", "G"))
                for g in groups if g.get("condition"))
            m.add_issue(IssueSeverity.WARNING, "ROUTER_AS_ROUTE_COLUMN",
                        "Router '%s' converted to a route-group column (%d groups)"
                        % (t.name, len(groups)),
                        suggestion="Split downstream targets into one model per "
                                   "group filtering on _mb_route_group.")
            return ("select *,\n    case\n        %s\n        else 'DEFAULT'\n"
                    "    end as _mb_route_group\nfrom %s" % (whens, up))
        m.add_issue(IssueSeverity.MANUAL, "ROUTER_WITHOUT_GROUPS",
                    "Router '%s' has no parsed groups — review" % t.name)
        return "select *  -- REVIEW: router groups unavailable\nfrom %s" % up

    if t.type == TransformationType.SEQUENCE:
        m.add_issue(IssueSeverity.WARNING, "SEQUENCE_AS_ROW_NUMBER",
                    "Sequence '%s' converted to row_number() surrogate keys" % t.name,
                    suggestion="For persistent keys use the target platform's "
                               "IDENTITY/SEQUENCE or dbt_utils.generate_surrogate_key.")
        seq_port = next((p.name for p in t.ports
                         if "NEXTVAL" in p.name.upper()), "_mb_surrogate_key")
        return ("select *, row_number() over (order by 1) as %s\nfrom %s"
                % (seq_port, up))

    if t.type == TransformationType.UPDATE_STRATEGY:
        m.add_issue(IssueSeverity.INFO, "UPDATE_STRATEGY_AS_INCREMENTAL",
                    "Update strategy '%s' expressed by the model's incremental "
                    "materialization" % t.name)
        return "select *\nfrom %s" % up

    return None


def _output_columns(m: Mapping) -> set:
    """Lowercased names of the columns a mapping's model actually projects.

    __OUTPUT__ first, and the order matters. The TARGET's ports describe the
    target TABLE; __OUTPUT__ is the mapping's own field map — what it really
    populates. They differ whenever the table has a column the mapping does
    not fill (an audit LOAD_DATE defaulted by the load), and it is __OUTPUT__
    that the model renders. Reading the TARGET here would report a column the
    relation does not have, which is the very mistake this is here to catch.
    """
    out = m.transformation("__OUTPUT__")
    ports = out.ports if out is not None and out.ports else []
    if not ports:
        tgts = m.by_type(TransformationType.TARGET)
        ports = tgts[0].ports if tgts and tgts[0].ports else []
    return {p.name.lower() for p in ports}


def _relation_columns(side, pipeline: Optional[Pipeline] = None,
                      tx_by_name=None, upstream_names=None) -> set:
    """The columns the RELATION a join side resolves to actually has.

    A SOURCE transformation's ports describe the source TABLE. When another
    mapping in the estate BUILDS that table, the relation this join reads is
    that mapping's model — and a model projects its target's columns, which
    are routinely fewer than the table's. ACCOUNT carries LOAD_DATE; the
    model that rebuilds it projects nine columns and LOAD_DATE is not one of
    them, so `l.LOAD_DATE` is a reference to a column that does not exist.

    Nothing catches that before the warehouse does: the SQL is well-formed,
    every ref() resolves, and `dbt parse` is clean. It fails on `dbt run`,
    against the customer's data, in front of the customer.
    """
    if side is None:
        return set()
    declared = {p.name.lower() for p in side.ports}
    if pipeline is None:
        return declared
    # A Source Qualifier normally sits between the source and everything
    # else — PowerCenter always emits one — and it is a pass-through, so the
    # relation behind it is still the source's. Not walking through it left
    # the commonest mapping shape unchecked.
    src = side
    if side.type == TransformationType.SOURCE_QUALIFIER and tx_by_name:
        # a Source Qualifier names its source either by property or by link,
        # depending on which parser built the mapping
        up = tx_by_name.get(str(side.properties.get("source", "") or ""))
        if up is None and upstream_names is not None:
            up = next((tx_by_name.get(u) for u in upstream_names(side)
                       if tx_by_name.get(u) is not None
                       and tx_by_name[u].type == TransformationType.SOURCE),
                      None)
        if up is not None and up.type == TransformationType.SOURCE:
            src = up
    if src.type != TransformationType.SOURCE:
        return declared
    table = str(src.properties.get("table", "") or "").lower()
    if not table:
        return declared
    from .dbt_naming import built_tables
    producer = built_tables(pipeline).get(table)
    if not producer:
        return declared              # a raw source really does have them all
    built = next((x for x in pipeline.mappings if x.name == producer), None)
    emitted = _output_columns(built) if built is not None else set()
    # An empty answer means "could not tell", not "projects nothing" — the
    # declared list is the safer of the two to fall back to.
    return (declared & emitted) if emitted else declared


def _join_projection(col: str, t: Transformation, m: Mapping, tx_by_name,
                     left_name: str, right_name: str, join_type: str,
                     left_cols: Optional[set] = None,
                     right_cols: Optional[set] = None) -> str:
    """One projected column of a join, qualified to the side it comes from.

    A joiner's output ports are plain names, and projecting them bare produced
    `select customer_id ... from a as l left join b as r` — ambiguous the
    moment both inputs carry that column, which is exactly when a join key is
    involved. Every engine rejects it ("column reference is ambiguous"), and
    it only ever surfaced at run time on the customer's warehouse.

    WHICH side to take is not arbitrary once the join can produce NULLs:

        inner   either — the join condition makes them equal
        left    the LEFT copy; the right is NULL for unmatched left rows
        right   the RIGHT copy; the left is NULL for unmatched right rows
        full    neither is safe alone, so COALESCE both
    """
    def side_columns(name, given):
        if given is not None:
            return given
        side = tx_by_name.get(name)
        return {p.name.lower() for p in side.ports} if side is not None \
            else set()

    low = col.lower()
    on_left = low in side_columns(left_name, left_cols)
    on_right = low in side_columns(right_name, right_cols)
    if on_left and on_right:
        kind = (join_type or "INNER").upper()
        if kind == "FULL":
            m.add_issue(
                IssueSeverity.INFO, "JOIN_COLUMN_COALESCED",
                "Column '%s' is on both sides of the FULL OUTER join '%s' — "
                "projected as coalesce(l.%s, r.%s), since either side can be "
                "NULL for an unmatched row" % (col, t.name, col, col))
            return "coalesce(l.%s, r.%s) as %s" % (col, col, col)
        if kind == "RIGHT":
            return "r.%s" % col
        return "l.%s" % col
    if on_right:
        return "r.%s" % col
    if on_left:
        return "l.%s" % col
    # neither input declares it: leave it bare rather than guess a side. If it
    # really is unresolvable the validator's column check says so.
    return col


def _strip_join_prefixes(cond: str, left_prefix: str, right_prefix: str) -> str:
    """Undo bulk-rename prefixes inside a join condition.

    The renamed name exists only downstream of the join; the relation being
    joined still has the original column, so `BR_BRANCH_ID = BRANCH_ID` has to
    become `BRANCH_ID = BRANCH_ID` before the sides are aliased."""
    import re
    for pfx in (left_prefix, right_prefix):
        if pfx:
            cond = re.sub(r"\b%s(\w+)" % re.escape(pfx), r"\1", cond)
    return cond


def _qualify_join_condition(cond: str, left_alias: str, right_alias: str,
                            left_cols=None, right_cols=None) -> str:
    """'a = b AND c = d' -> 'l.a = r.b and l.c = r.d'.

    Each side of an equality is qualified by the input that DECLARES that
    column, not by the order it happens to be written in. Assuming the first
    token was always the left one silently produced `l.order_id = r.order_id`
    for a condition written the other way round — naming a column on neither
    relation, or worse, joining on the wrong pair.

    Written order is only the tie-break, for the ordinary case where both
    inputs carry the column (a join key with the same name on both sides) and
    for anything the inputs do not declare at all.
    """
    if not cond:
        return ""
    import re
    left_cols = {c.lower() for c in (left_cols or ())}
    right_cols = {c.lower() for c in (right_cols or ())}

    def sides(a: str, b: str):
        """(alias for a, alias for b), by ownership where it is decisive."""
        a_low, b_low = a.lower(), b.lower()
        a_only_l = a_low in left_cols and a_low not in right_cols
        a_only_r = a_low in right_cols and a_low not in left_cols
        b_only_l = b_low in left_cols and b_low not in right_cols
        b_only_r = b_low in right_cols and b_low not in left_cols
        if a_only_r or b_only_l:
            return right_alias, left_alias
        if a_only_l or b_only_r:
            return left_alias, right_alias
        return left_alias, right_alias          # ambiguous: keep written order

    out = []
    for part in re.split(r"\s+(?:AND|and)\s+", cond.strip()):
        m2 = re.match(r"^\s*([\w\"\.]+)\s*=\s*([\w\"\.]+)\s*$", part)
        if m2 and "." not in m2.group(1) and "." not in m2.group(2):
            a, b = m2.group(1), m2.group(2)
            alias_a, alias_b = sides(a, b)
            out.append("%s.%s = %s.%s" % (alias_a, a, alias_b, b))
        else:
            out.append(part)
    return " and ".join(out)


# ---------------------------------------------------------------------------
# Snapshot identity
# ---------------------------------------------------------------------------

def _is_snapshot_mapping(m: Mapping) -> bool:
    """True when this mapping is emitted as a {% snapshot %} block and NOT as
    a model.

    This is the one case where the name the plan reserved (int_/dim_) is never
    written, so it is also the one case where the ref-resolution map has to
    point somewhere else. Kept as a single predicate so the map and the
    emitting branch can never disagree about which mappings those are.
    """
    if m.load_strategy != LoadStrategy.SCD2:
        return False
    scd2 = m.properties.get("scd2_cir") or {}
    return scd2.get("dbt_strategy") != "incremental_scd2"


def _snapshot_name(m: Mapping) -> str:
    """The snapshot's node name — what `{% snapshot %}` declares, what the
    file is called, and what a downstream model must ref()."""
    return _safe(m.name)


# ---------------------------------------------------------------------------
# Incremental (watermark) filters
# ---------------------------------------------------------------------------

_REVIEW_COND_RE = None
_WATERMARK_RE = None


def _recovered_condition(cond: str) -> str:
    """The real predicate behind a `TRUE -- REVIEW: <predicate>` placeholder.

    A condition MetaBridge cannot express in the target tool's own syntax is
    round-tripped as `TRUE` with the predicate preserved in a trailing comment
    (`powercenter_generator` writes exactly that form). Reading it back is what
    turns the placeholder into a real filter again instead of a permanent
    no-op that silently passes every row.
    """
    import re
    global _REVIEW_COND_RE
    if _REVIEW_COND_RE is None:
        _REVIEW_COND_RE = re.compile(
            r"^\s*TRUE\s*(?:--|/\*)\s*REVIEW:\s*(.+?)\s*(?:\*/)?\s*$",
            re.IGNORECASE | re.DOTALL)
    mo = _REVIEW_COND_RE.match(cond or "")
    return mo.group(1).strip() if mo else (cond or "")


def _incremental_filter(cond: str, t: Transformation,
                        m: Mapping) -> Optional[str]:
    """A watermark FILTER rendered as dbt's own incremental idiom.

    ``$$LAST_RUN_TS`` is the IR's canonical watermark token: `dbt_parser`
    normalizes `(select max(col) from {{ this }})` into it, and `scaffold`
    emits it for a manifest's ``incremental_column``. Rendering it back out as
    a plain ``{{ var(...) }}`` broke that round trip twice over — the project
    asked for a value nobody supplies, and where the predicate had been parked
    in a REVIEW comment the model quietly rescanned its whole source on every
    run while still calling itself incremental.

    Returns the WHERE clause wrapped in ``{% if is_incremental() %}``, or None
    when this filter is not a watermark filter.
    """
    import re
    predicate = _recovered_condition(cond)
    if "$$LAST_RUN_TS" not in predicate.upper() and \
            t.name.upper() != "FIL_INCREMENTAL":
        return None
    global _WATERMARK_RE
    if _WATERMARK_RE is None:
        _WATERMARK_RE = re.compile(
            r"([A-Za-z_][\w.$#]*)\s*(>=?)\s*\$\$LAST_RUN_TS", re.IGNORECASE)
    mo = _WATERMARK_RE.search(predicate)
    if mo is None:
        # named like a watermark filter but carrying something else — leave it
        # to the ordinary filter path rather than guessing at a column
        return None
    column, op = mo.group(1), mo.group(2)
    predicate = (predicate[:mo.start()]
                 + "%s %s (select max(%s) from {{ this }})" % (column, op,
                                                              column)
                 + predicate[mo.end():])
    m.add_issue(IssueSeverity.INFO, "INCREMENTAL_FILTER_AS_JINJA",
                "Watermark filter '%s' rendered as dbt's incremental idiom "
                "(is_incremental() over max(%s) from this model)"
                % (t.name, column),
                suggestion="The watermark column has to exist on the model "
                           "itself; where it does not, point the max() at the "
                           "relation that carries it instead of {{ this }}.")
    # never hand a predicate containing Jinja to sqlglot — it is not SQL yet
    # an embedded FRAGMENT, not a file: it is spliced into a CTE body and
    # indented with it, so the template's trailing newline comes back off
    return render_template("incremental_where.sql.j2",
                           predicate=predicate).rstrip("\n")


# ---------------------------------------------------------------------------
# SQL-override relations
# ---------------------------------------------------------------------------

def _override_sql(sql: str, m: Mapping, t: Transformation,
                  dbt_relation, plain: bool) -> str:
    """A Source Qualifier SQL override with its relations resolved to dbt.

    The override used to be emitted verbatim, which put the model OUTSIDE
    dbt's DAG: it read a physical relation dbt does not manage, so dbt neither
    ordered it after its upstream nor drew the lineage edge, and the model
    could run against a table that had not been rebuilt yet.

    Only the FROM/JOIN relations are rewritten — the statement is not
    re-rendered through sqlglot. sqlglot is used to *find* the tables (an
    accurate answer a regex cannot give), then each one is replaced in the
    original text, so the override keeps its own formatting and its recovered
    line structure.
    """
    if plain or dbt_relation is None:
        return sql
    import re
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read=None)
    except Exception:  # noqa: BLE001
        m.add_issue(IssueSeverity.MANUAL, "SQL_OVERRIDE_UNPARSED",
                    "Source Qualifier SQL override could not be parsed, so "
                    "its relations were left physical — this model is outside "
                    "dbt's DAG and dbt will not order it",
                    detail=sql[:200],
                    suggestion="Rewrite the FROM/JOIN relations as ref() or "
                               "source() by hand.")
        # deliberately not recorded on the lineage graph: we could not parse
        # the statement, so we do not know WHICH relations it reads and must
        # not invent a node for them. The MANUAL item above is the channel.
        return sql
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    found: List[str] = []
    for tbl in tree.find_all(exp.Table):
        parts = [p for p in (tbl.catalog, tbl.db, tbl.name) if p]
        if parts and parts[-1].lower() not in ctes:
            found.append(".".join(parts))
    resolved: List[str] = []
    unmanaged: List[str] = []
    # longest first: a qualified `crm.customers` has to be replaced before a
    # bare `customers` pattern can match half of it
    for full in sorted(set(found), key=len, reverse=True):
        rel = dbt_relation(full.split(".")[-1])
        if not rel:
            unmanaged.append(full)
            continue
        pattern = re.compile(r"(?i)(\b(?:from|join)\s+)" + re.escape(full)
                             + r"(?![\w.$#])")
        sql, n = pattern.subn(lambda mo, r=rel: mo.group(1) + r, sql)
        if n:
            resolved.append("%s -> %s" % (full, rel))
        else:
            unmanaged.append(full)
    if resolved:
        m.add_issue(IssueSeverity.INFO, "SQL_OVERRIDE_REFS_RESOLVED",
                    "SQL override relations resolved into the dbt DAG: %s"
                    % ", ".join(sorted(resolved)))
    for u in sorted(set(unmanaged)):
        m.add_issue(IssueSeverity.MANUAL, "SQL_OVERRIDE_UNMANAGED_RELATION",
                    "SQL override in '%s' reads '%s', which is neither a "
                    "generated model nor a declared source — dbt does not "
                    "manage it, so this model has no dependency edge and may "
                    "run before that relation is rebuilt" % (t.name, u),
                    suggestion="Add the table to the source manifest, or "
                               "convert the object that builds it.")
    return sql


# ---------------------------------------------------------------------------
# Ref graph — recorded while rendering, checked against what was written
# ---------------------------------------------------------------------------

_GRAPH = _contextvars.ContextVar("mb_dbt_graph", default=None)


def _new_graph() -> dict:
    return {"models": [], "snapshots": [], "refs": [], "sources": [],
            "unmanaged": [], "vars": {}, "nodes": {}}


def _g():
    """The active collector, or None when a renderer is called on its own —
    the warehouse-SQL generator and the unit tests both do that, and neither
    is emitting a dbt project."""
    return _GRAPH.get()


def _emitted(kind: str, name: str, **attrs) -> None:
    """Record an artifact that was actually written, with what it is.

    The attributes are what makes the lineage graph readable rather than a
    bag of names: which layer a node sits in, which folder, which source
    object it came from, and where the file is.
    """
    g = _g()
    if g is None or not name:
        return
    g["models" if kind == "model" else "snapshots"].append(name)
    g["nodes"][name] = dict(attrs, name=name, kind=kind)


def _ref(frm: str, to: str) -> None:
    g = _g()
    if g is not None and to:
        g["refs"].append([frm, to])


def _src(frm: str, source: str, table: str) -> None:
    g = _g()
    if g is not None:
        g["sources"].append([frm, source, table])


def _unmanaged(frm: str, relation: str) -> None:
    g = _g()
    if g is not None and relation:
        g["unmanaged"].append([frm, relation])


def _want_var(name: str, default: str) -> None:
    g = _g()
    if g is not None and name and not g["vars"].get(name):
        # first non-empty default wins; a later blank must not erase it
        g["vars"][name] = default


def _collected_vars():
    """-> ({name: default} we can declare, [names] we cannot).

    A var with no known default is deliberately NOT declared. Declaring it as
    null makes `var()` render the string "None" straight into the SQL, which is
    silently wrong; leaving it undeclared makes dbt refuse to compile and say
    which var is missing.
    """
    g = _g()
    if g is None:
        return {}, []
    declared = {k: v for k, v in sorted(g["vars"].items()) if v}
    required = sorted(k for k, v in g["vars"].items() if not v)
    return declared, required


def _finish_graph(pipeline: Pipeline) -> None:
    """Publish the emitted ref graph, and fail loudly on a dangling edge.

    Every ref()/source() the models actually emitted is checked against what
    was actually written. A ref to a node that does not exist is not a
    warning — `dbt parse` refuses the entire project over one of them — so it
    is recorded as an ERROR here, at generate time, instead of being found on
    the customer's warehouse.

    The graph itself is published on the pipeline so lineage can be drawn from
    what was emitted rather than re-derived from the IR and hope the two agree.
    """
    g = _g()
    if g is None:
        return
    nodes = set(g["models"]) | set(g["snapshots"])
    declared = {(_source_name(s), s.name) for s in pipeline.sources}
    for frm, to in g["refs"]:
        if to not in nodes:
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.ERROR, code="DBT_REF_DANGLING",
                message="model '%s' refs '%s', which was not generated — "
                        "dbt fails to parse the whole project on this"
                        % (frm, to), obj=frm,
                suggestion="Generator defect: the ref target and the emitted "
                           "artifact disagree."))
    for frm, source, table in g["sources"]:
        if (source, table) not in declared:
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.ERROR, code="DBT_SOURCE_UNDECLARED",
                message="model '%s' reads source('%s', '%s'), which is not "
                        "declared in sources.yml" % (frm, source, table),
                obj=frm))
    pipeline.metadata["dbt_graph"] = _typed_graph(g)


# dbt's own resource kinds, plus one of ours. `unmanaged` is not a dbt concept:
# it is a relation a model reads that the project neither builds nor declares
# as a source, so dbt has no node for it and draws no edge. Naming it here is
# what turns a silently missing edge into a visible break on the graph.
NODE_KINDS = ("source", "model", "snapshot", "unmanaged")

_LAYER_ORDER = {"sources": 0, "staging": 1, "intermediate": 2, "marts": 3,
                "snapshots": 4, "unmanaged": 5}


def _typed_graph(g: dict) -> dict:
    """The emitted ref graph, as typed nodes and directed edges.

    Built from what the generator actually WROTE rather than re-derived from
    the IR, so it cannot drift from the artifacts it describes — the failure
    mode of every lineage diagram that is computed twice.

    Edges point the way data flows (upstream -> downstream), which is the
    opposite of how a ref is recorded (a model names what it reads).
    """
    nodes = {}
    for name, meta in g["nodes"].items():
        nodes[name] = dict(meta)

    for _frm, source, table in g["sources"]:
        nid = "source:%s.%s" % (source, table)
        nodes.setdefault(nid, {"name": nid, "kind": "source",
                               "layer": "sources", "group": source,
                               "label": "%s.%s" % (source, table),
                               "source": source, "table": table})
    for _frm, relation in g["unmanaged"]:
        nodes.setdefault(relation, {"name": relation, "kind": "unmanaged",
                                    "layer": "unmanaged", "group": "",
                                    "label": relation})

    edges = []
    seen = set()

    def edge(frm, to, kind):
        key = (frm, to, kind)
        if key not in seen and frm in nodes and to in nodes:
            seen.add(key)
            edges.append({"from": frm, "to": to, "kind": kind})

    for frm, to in g["refs"]:
        edge(to, frm, "ref")
    for frm, source, table in g["sources"]:
        edge("source:%s.%s" % (source, table), frm, "source")
    for frm, relation in g["unmanaged"]:
        edge(relation, frm, "unmanaged")

    # A mart nothing downstream reads is where this project stops and its
    # consumers begin. That is a fact about topology, so it is recorded as
    # one — it is NOT turned into a dbt exposure, which is a record about a
    # real downstream asset we have no evidence of.
    has_consumer = {e["from"] for e in edges}
    for name, meta in nodes.items():
        if meta["kind"] in ("model", "snapshot"):
            meta["terminal"] = name not in has_consumer

    ordered = sorted(nodes.values(),
                     key=lambda n: (_LAYER_ORDER.get(n.get("layer"), 9),
                                    n["name"]))
    return {
        "nodes": ordered,
        "edges": sorted(edges, key=lambda e: (e["from"], e["to"])),
        "counts": {k: sum(1 for n in ordered if n["kind"] == k)
                   for k in NODE_KINDS},
        "unmanaged_relations": sorted(
            {r for _f, r in g["unmanaged"]}),
    }


# ---------------------------------------------------------------------------
# Templates
#
# Every TEXT artifact (model SQL, snapshot blocks, doc blocks, .gitignore,
# packages.yml) is rendered from a Jinja template rather than assembled by
# string formatting. Two reasons, in order of weight:
#
#   * the shape of a generated file is legible, and a delivery team can
#     override it — a house header comment, a standard config block, a
#     different doc format — without patching this module;
#   * SQL that itself contains Jinja was previously written with %%-escaped
#     format strings ("{%% snapshot %s %%}"), which is hard to read and easy
#     to get wrong.
#
# The STRUCTURED artifacts (dbt_project.yml, sources.yml, the per-folder
# property files, selectors.yml) are deliberately NOT templated. They are
# built as dicts and serialised with yaml.safe_dump, which cannot emit invalid
# YAML; a template can. That safety is worth more than the symmetry.
# ---------------------------------------------------------------------------

# Where a project can override the packaged templates. Any file present here
# wins; anything absent falls back to the packaged one, so an override set may
# be partial.
TEMPLATE_OVERRIDE_ENV = "METABRIDGE_DBT_TEMPLATES"

_ENV = None
_ENV_KEY = None


def _template_env():
    """The Jinja environment used for the generated artifacts.

    The delimiters are swapped to ``<< >>`` / ``<% %>`` because we are
    generating Jinja WITH Jinja: dbt's own ``{{ ref() }}`` and
    ``{% snapshot %}`` have to pass through untouched. Doing it with custom
    delimiters rather than wrapping every template line in ``{% raw %}`` means
    a template reads exactly like the file it produces.

    ``StrictUndefined`` so a typo in a template name or variable fails loudly
    instead of quietly rendering an empty string into a customer's SQL.
    """
    global _ENV, _ENV_KEY
    import os

    override = os.environ.get(TEMPLATE_OVERRIDE_ENV, "")
    if _ENV is not None and _ENV_KEY == override:
        return _ENV

    from jinja2 import (ChoiceLoader, Environment, FileSystemLoader,
                        PackageLoader, StrictUndefined)
    packaged = PackageLoader("metabridge.generators", "templates/dbt")
    loader = ChoiceLoader([FileSystemLoader(override), packaged]) \
        if override else packaged
    _ENV = Environment(
        loader=loader,
        variable_start_string="<<", variable_end_string=">>",
        block_start_string="<%", block_end_string="%>",
        comment_start_string="<#", comment_end_string="#>",
        trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
        undefined=StrictUndefined)
    _ENV_KEY = override
    return _ENV


def render_template(template: str, /, **context) -> str:
    """Render one artifact template. See _template_env for the delimiters.

    The template name is positional-ONLY: several templates take a variable
    called `name` (a snapshot's node name, for one), and a keyword parameter
    here would collide with it.

    The result is normalised to LF. Jinja's loaders do not translate newlines,
    so a template file checked out with CRLF (the default on Windows) would
    otherwise put carriage returns into generated SQL — which happened, and is
    invisible until someone diffs the output on another platform. A
    `.gitattributes` entry keeps the templates themselves LF; this makes the
    artifact LF regardless of how they arrived.
    """
    return _template_env().get_template(template).render(**context).replace(
        "\r\n", "\n")

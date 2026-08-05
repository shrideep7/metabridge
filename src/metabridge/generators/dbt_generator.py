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
    """dbt source() name for a SourceTable. The name is the schema; the
    database is preserved separately via the source's `database:` property
    (see _write_sources_yaml), matching dbt's own source() resolution."""
    return s.schema or "raw"

_CANONICAL_TO_DBT = {
    "string": "varchar", "integer": "integer", "bigint": "bigint",
    "decimal": "decimal", "double": "double precision", "date": "date",
    "timestamp": "timestamp", "boolean": "boolean", "binary": "binary",
}

# Documented fallback when a decimal column declares no precision/scale.
# Emitted with a warning so it is never mistaken for the real source scale.
_DECIMAL_FALLBACK = (38, 6)


def _dbt_type(port) -> str:
    """dbt column type from an IR Port, PRESERVING declared numeric
    precision/scale (never collapsing everything to decimal(38,6)). When a
    decimal has no declared precision, use the documented fallback."""
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


def _layer_of(m: Mapping, pipeline: Pipeline) -> str:
    """staging: reads physical sources only; marts: nothing depends on it;
    intermediate: everything else."""
    if not m.depends_on:
        return "staging"
    dependents = any(m.name in other.depends_on for other in pipeline.mappings)
    return "intermediate" if dependents else "marts"


def generate_dbt_project(pipeline: Pipeline, out_dir: str,
                         layout: str = "layered") -> None:
    """Complete dbt project (module 27): staging/intermediate/marts with
    DETERMINISTIC model naming and per-mapping decomposition —

        source table  -> models/staging/stg_<base>.sql  (select from source())
        mapping logic -> models/intermediate/int_<base>.sql
        mart mapping  -> models/marts/dim_|fct_<base>.sql (config + ref(int))

    plus snapshots/, macros/, tests/, schema.yml, sources.yml under
    staging, and migration_manifest.json tracing PowerCenter object ->
    CIR object -> dbt object(s)."""
    from .dbt_naming import plan_names
    root = Path(out_dir)
    for layer in ("staging", "intermediate", "marts"):
        (root / "models" / layer).mkdir(parents=True, exist_ok=True)
    for aux in ("macros", "tests", "snapshots"):
        (root / aux).mkdir(exist_ok=True)
    (root / "macros" / ".gitkeep").write_text("", encoding="utf-8")
    (root / "tests" / ".gitkeep").write_text("", encoding="utf-8")

    proj = {
        "name": _safe(pipeline.name), "version": "1.0.0", "config-version": 2,
        "profile": _safe(pipeline.name),
        "model-paths": ["models"], "snapshot-paths": ["snapshots"],
        "macro-paths": ["macros"], "test-paths": ["tests"],
        "models": {_safe(pipeline.name): {
            "staging": {"+materialized": "view"},
            "intermediate": {"+materialized": "view"},
            "marts": {"+materialized": "table"},
        }},
    }
    (root / "dbt_project.yml").write_text(yaml.safe_dump(proj, sort_keys=False), encoding="utf-8")

    if pipeline.sources:
        _write_sources_yaml(pipeline, root)

    plan, stg_names = plan_names(pipeline)
    manifest: List[dict] = []
    schema_models: List[dict] = []

    # classify manifest objects by the REAL source platform, not
    # "PowerCenter" by default — a Snowflake schema is not a PC object
    src_fmt = (pipeline.source_format or "").lower()
    platform_label = {"powercenter": "PowerCenter", "idmc": "IDMC",
                      "dbt": "dbt", "scaffold": "scaffold"}.get(
                          src_fmt, src_fmt or "unknown")
    legacy_pc = src_fmt == "powercenter"

    # ref-resolution map: raw source tables -> stg models; mapping names
    # AND their target tables -> the mapping's FINAL model (mart if split)
    names: Dict[str, str] = dict(stg_names)
    for m in pipeline.mappings:
        final = plan[m.name]["mart"] or plan[m.name]["int"]
        names[m.name] = final
        names[m.name.lower()] = final
        tgts = m.by_type(TransformationType.TARGET)
        if tgts and tgts[0].properties.get("table"):
            names[str(tgts[0].properties["table"]).lower()] = final

    # staging models: one per source table (skipped when a kept stg_
    # mapping already covers the source — see plan_names)
    for s in pipeline.sources:
        name = stg_names.get(s.name.lower())
        if not name:
            continue
        cols = [c.name for c in s.columns
                if c.name not in ("*", "ROW_DATA")]
        if cols:
            select = "select\n    %s" % ",\n    ".join(cols)
        else:
            # schema genuinely unavailable — SELECT * with a VISIBLE warning
            # so nobody mistakes a passthrough for a real projection
            select = ("-- WARNING: no column metadata for this source; using\n"
                      "-- SELECT * passthrough. Add columns to the manifest or\n"
                      "-- introspect the live system for an explicit projection.\n"
                      "select *")
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.WARNING, code="SELECT_STAR_PASSTHROUGH",
                message="Staging model '%s' emits SELECT * — no column "
                        "metadata for source %s.%s" % (name, s.schema or "raw",
                                                       s.name),
                obj=name,
                suggestion="Provide columns in the manifest or introspect the "
                           "source so the model projects explicit columns."))
        body = "%s\nfrom {{ source('%s', '%s') }}\n" % (
            select, _source_name(s), s.name)
        (root / "models" / "staging" / (name + ".sql")).write_text(body, encoding="utf-8")
        schema_models.append({
            "name": name,
            "description": "Staging view over source %s.%s"
            % (s.schema or "raw", s.name)})
        entry_src: Dict[str, object] = {
            "source_object": s.name, "source_type": "source_definition",
            "source_platform": platform_label,
            "cir_object": s.name, "cir_type": "SourceTable",
            "dbt_objects": [{"name": name, "role": "staging",
                             "path": "models/staging/%s.sql" % name}]}
        if legacy_pc:
            entry_src["powercenter_object"] = s.name
            entry_src["powercenter_type"] = "source_definition"
        manifest.append(entry_src)

    for m in pipeline.mappings:
        unresolved = [t.name for t in m.by_type(TransformationType.SOURCE)
                      if not str(t.properties.get("table", "")).strip()]
        entry = {"source_object": m.origin or m.name,
                 "source_type": "mapping",
                 "source_platform": platform_label,
                 "source_folder": m.properties.get("folder", ""),
                 "cir_object": m.name, "cir_type": "Mapping",
                 "dbt_objects": []}
        if legacy_pc:
            entry["powercenter_object"] = m.origin or m.name
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
        if m.load_strategy == LoadStrategy.SCD2:
            scd2 = m.properties.get("scd2_cir") or {}
            if scd2.get("dbt_strategy") == "incremental_scd2":
                # a snapshot cannot mint the per-version surrogate key —
                # emit an incremental model and declare what it does NOT do
                if not any(i.code == "SCD2_INCREMENTAL_CLOSE"
                           for i in m.issues):
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
            else:
                snap = _safe(m.name)
                (root / "snapshots" / (snap + ".sql")).write_text(
                    render_snapshot_sql(m, pipeline, names), encoding="utf-8")
                entry["dbt_objects"].append(
                    {"name": snap, "role": "snapshot",
                     "path": "snapshots/%s.sql" % snap})
                continue
        p = plan[m.name]
        split = bool(p["mart"])
        int_dir = "intermediate" if split else p["layer"]
        int_sql = render_model_sql(
            m, pipeline, names,
            config="{{ config(materialized='view') }}\n\n" if split
            else None)
        (root / "models" / int_dir / (p["int"] + ".sql")
         ).write_text(int_sql, encoding="utf-8")
        entry["dbt_objects"].append(
            {"name": p["int"], "role": "transformation_logic",
             "path": "models/%s/%s.sql" % (int_dir, p["int"])})
        desc = ("Converted from Informatica mapping '%s' by MetaBridge AI"
                % (m.origin or m.name))

        def _meta():
            md = {"source_mapping": m.origin or m.name,
                  "source_platform": platform_label}
            if legacy_pc:
                md["powercenter_mapping"] = m.origin or m.name
            return dict(md)

        schema_models.append({"name": p["int"], "description": desc,
                              "meta": _meta()})
        if split:
            mart_sql = (_config_block(m) +
                        "select * from {{ ref('%s') }}\n" % p["int"])
            (root / "models" / "marts" / (p["mart"] + ".sql")
             ).write_text(mart_sql, encoding="utf-8")
            entry["dbt_objects"].append(
                {"name": p["mart"], "role": "mart",
                 "path": "models/marts/%s.sql" % p["mart"]})
            mart_entry = {"name": p["mart"], "description": desc,
                          "meta": _meta()}
            cols = _target_columns(m)
            if cols:
                mart_entry["columns"] = cols
            schema_models.append(mart_entry)

    (root / "models" / "schema.yml").write_text(
        yaml.safe_dump({"version": 2, "models": schema_models}, sort_keys=False), encoding="utf-8")

    import json as _json
    (root / "migration_manifest.json").write_text(_json.dumps(
        {"project": pipeline.name, "generator": "MetaBridge AI",
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
        for name in shared:
            (root / "macros" / ("mapplet_%s.sql" % _safe(name))
             ).write_text(render_dbt_macro(comps[name]), encoding="utf-8")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def _target_columns(m: Mapping) -> List[dict]:
    tgts = m.by_type(TransformationType.TARGET)
    if not tgts or not tgts[0].ports:
        return []
    out = []
    fallback = False
    for p in tgts[0].ports:
        col = {"name": p.name, "data_type": _dbt_type(p)}
        fallback = fallback or _decimal_needs_fallback(p)
        if p.name in m.unique_key:
            col["tests"] = ["unique", "not_null"]
        out.append(col)
    if fallback:
        m.add_issue(IssueSeverity.WARNING, "NUMERIC_PRECISION_FALLBACK",
                    "Target of '%s' has decimal column(s) without declared "
                    "precision — using documented fallback decimal(%d,%d)"
                    % (m.name, *_DECIMAL_FALLBACK),
                    suggestion="Declare precision/scale in the source "
                               "manifest to preserve exact numeric types.")
    return out


def _write_sources_yaml(pipeline: Pipeline, root: Path) -> None:
    # One dbt source per schema (its source() name — kept consistent with the
    # staging refs). PRESERVE the source database and schema — never silently
    # flatten to raw/raw. Supports multiple source databases: each distinct
    # schema keeps its own database. A dbt source name resolves to exactly one
    # database, so if one schema name genuinely spans several databases we keep
    # the first and note the others rather than emit a ref that won't compile.
    by_schema: Dict[str, List] = {}
    for s in pipeline.sources:
        by_schema.setdefault(s.schema or "raw", []).append(s)
    src_entries = []
    for schema, tables in sorted(by_schema.items()):
        dbs = sorted({t.database for t in tables if t.database})
        entry: Dict[str, object] = {"name": schema, "schema": schema}
        if pipeline.metadata.get("landing_target"):
            # cross-platform landing: the tables' database is the SOURCE
            # database; the dbt source must resolve in the profile's TARGET
            # database, so no database is pinned here
            dbs = []
        if dbs:
            entry["database"] = dbs[0]             # preserved from the manifest
            if len(dbs) > 1:
                pipeline.issues.append(ConversionIssue(
                    severity=IssueSeverity.WARNING, code="SCHEMA_MULTI_DATABASE",
                    message="Schema '%s' spans multiple source databases (%s); "
                            "dbt source '%s' uses '%s'. Split the schema per "
                            "database to model the others."
                            % (schema, ", ".join(dbs), schema, dbs[0]),
                    obj=schema))
        entry["tables"] = []
        for t in tables:
            tbl = {"name": t.name}
            if t.columns:
                tbl["columns"] = [{"name": c.name, "data_type": _dbt_type(c)}
                                  for c in t.columns]
                if any(_decimal_needs_fallback(c) for c in t.columns):
                    pipeline.issues.append(ConversionIssue(
                        severity=IssueSeverity.WARNING,
                        code="NUMERIC_PRECISION_FALLBACK",
                        message="Source %s has decimal column(s) without "
                                "declared precision — using documented "
                                "fallback decimal(%d,%d)"
                                % (t.name, *_DECIMAL_FALLBACK), obj=t.name,
                        suggestion="Declare precision/scale in the manifest to "
                                   "preserve exact numeric types."))
            entry["tables"].append(tbl)  # type: ignore[attr-defined]
        src_entries.append(entry)
    (root / "models" / "staging").mkdir(parents=True, exist_ok=True)
    (root / "models" / "staging" / "sources.yml").write_text(
        yaml.safe_dump({"version": 2, "sources": src_entries}, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Model SQL rendering
# ---------------------------------------------------------------------------

def render_snapshot_sql(m: Mapping, pipeline: Pipeline, mapping_names: set) -> str:
    """SCD Type 2 mapping -> dbt {% snapshot %} block."""
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
    return ("{%% snapshot %s %%}\n{{ config(%s) }}\n\n%s\n\n{%% endsnapshot %%}\n"
            % (_safe(m.name), ", ".join(cfg), select))


_PARAM_RE = None  # lazily compiled


def render_model_sql(m: Mapping, pipeline: Pipeline, mapping_names,
                     config: Optional[str] = None) -> str:
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
    header = config if config is not None else _config_block(m)
    body, terminal = _render_graph(m, pipeline, mapping_names)
    if not body:
        return header + "select 1 as placeholder -- REVIEW: empty mapping\n"
    ctes = ",\n\n".join(body)
    sql = "%swith %s\n\nselect * from %s\n" % (header, ctes, terminal)

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
        if e.get("classification") == "stateful_variable":
            stateful.append(name)
            return ("{{ var('%s') }} /* STATEFUL PowerCenter variable — "
                    "feed from a watermark/state query, never a static "
                    "value (see STATEFUL_VARIABLE) */" % name)
        params.append(name)
        default = e.get("default", "")
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
        return "{{ var('%s') }}" % name

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


def _config_block(m: Mapping) -> str:
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
    for prop, opt in (("pre_sql", "pre_hook"), ("post_sql", "post_hook")):
        v = m.properties.get(prop)
        if v:
            base += ", %s=%s" % (opt, _json.dumps(str(v)))
    return "{{ config(%s) }}\n\n" % base


def _topo_order(m: Mapping) -> List[Transformation]:
    """Topological order over links; SOURCE and TARGET excluded."""
    skip = {TransformationType.SOURCE, TransformationType.TARGET}
    nodes = [t for t in m.transformations if t.type not in skip and t.name != "__OUTPUT__"]
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

_QUALIFY_DIALECT = _contextvars.ContextVar("mb_qualify", default=False)


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

    def source_relation(src: Transformation) -> str:
        table = str(src.properties.get("table", src.name))
        if table in source_ref_cache:
            return source_ref_cache[table]
        if plain:
            schema = str(src.properties.get("schema", "") or "")
            rel = "%s.%s" % (schema, table) if schema else table
        elif isinstance(mapping_names, dict) and (
                table in mapping_names or table.lower() in mapping_names):
            # deterministic-name plan (module 27): stg_/int_/dim_/fct_
            rel = "{{ ref('%s') }}" % mapping_names.get(
                table, mapping_names.get(table.lower()))
        elif table in mapping_names or table.lower() in {n.lower() for n in mapping_names}:
            rel = "{{ ref('%s') }}" % _safe(table)
        elif any(s.name.lower() == table.lower() for s in pipeline.sources):
            s = next(s for s in pipeline.sources if s.name.lower() == table.lower())
            rel = "{{ source('%s', '%s') }}" % (_source_name(s), s.name)
        else:
            rel = table
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
                            source_relation)
        if body is None:
            continue
        ctes.append("%s as (\n%s\n)" % (_cte_name(t), _indent(body)))
        terminal = _cte_name(t)

    out = m.transformation("__OUTPUT__")
    if out is not None and out.properties.get("upstream"):
        up = tx_by_name.get(str(out.properties["upstream"]))
        if up is not None:
            terminal = _cte_name(up)
    return ctes, terminal


def _cte_name(t: Transformation) -> str:
    return _safe(t.name.lower())


def _indent(s: str, pad: str = "    ") -> str:
    return "\n".join(pad + l for l in s.split("\n"))


def _render_node(t: Transformation, m: Mapping, tx_by_name, upstream_cte,
                 upstream_names, source_relation) -> Optional[str]:
    up = upstream_cte(t)

    if t.type == TransformationType.SOURCE_QUALIFIER:
        override = str(t.properties.get("sql_override", "") or "").strip()
        if override:
            return override.rstrip(";")
        cols = ", ".join(p.name for p in t.ports) or "*"
        src = up
        if src is None:
            srcs = t.properties.get("sources") or []
            src = str(srcs[0]) if srcs else "MISSING_SOURCE"
        return "select %s\nfrom %s" % (cols, src)

    if t.type == TransformationType.FILTER:
        cond = str(t.properties.get("condition", "TRUE"))
        return "select *\nfrom %s\nwhere %s" % (up, cond)

    if t.type == TransformationType.EXPRESSION:
        items = []
        for p in t.ports:
            if p.direction == "VARIABLE":
                continue
            if p.expression and p.expression.lower() != p.name.lower():
                items.append("%s as %s" % (p.expression, p.name))
            else:
                items.append(p.name)
        return "select\n    %s\nfrom %s" % (",\n    ".join(items), up)

    if t.type == TransformationType.AGGREGATOR:
        group_by = [str(g) for g in t.properties.get("group_by", [])]
        items = []
        for p in t.ports:
            if p.expression:
                items.append("%s as %s" % (p.expression, p.name))
            else:
                items.append(p.name)
        sql = "select\n    %s\nfrom %s" % (",\n    ".join(items), up)
        if group_by:
            sql += "\ngroup by %s" % ", ".join(group_by)
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
        cond = _qualify_join_condition(str(t.properties.get("condition", "")),
                                       "l", "r")
        cols = ", ".join(p.name for p in t.ports) or "*"
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
        proj = ", ".join(out_cols) if out_cols else "*"
        parts = ["select %s from %s" % (proj, rel(i)) for i in inputs]
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


def _qualify_join_condition(cond: str, left_alias: str, right_alias: str) -> str:
    """'a = b AND c = d' -> 'l.a = r.b and l.c = r.d' (heuristic, flagged upstream)."""
    if not cond:
        return ""
    import re
    parts = re.split(r"\s+(?:AND|and)\s+", cond.strip())
    out = []
    for part in parts:
        m2 = re.match(r"^\s*([\w\"\.]+)\s*=\s*([\w\"\.]+)\s*$", part)
        if m2 and "." not in m2.group(1) and "." not in m2.group(2):
            out.append("%s.%s = %s.%s" % (left_alias, m2.group(1),
                                          right_alias, m2.group(2)))
        else:
            out.append(part)
    return " and ".join(out)

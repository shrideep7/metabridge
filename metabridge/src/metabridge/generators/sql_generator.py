"""Generate native warehouse SQL scripts (Snowflake, Databricks, BigQuery, ...)
from the IR.

Every mapping becomes one numbered .sql file in DAG order:

  * VIEW strategy          -> CREATE OR REPLACE VIEW  x AS <select>
  * FULL load              -> CREATE OR REPLACE TABLE x AS <select>
  * APPEND                 -> INSERT INTO x <select>
  * MERGE / DELETE_INSERT  -> MERGE INTO x USING (<select>) ON <keys> ...

Statements are rendered in canonical SQL and transpiled to the requested
dialect with sqlglot, so functions/types come out warehouse-native. A
deploy_all.sql runs everything in dependency order, and sources_ddl.sql
recreates typed source tables when the IR knows them.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

import sqlglot

from ..ir.model import IssueSeverity, LoadStrategy, Mapping, Pipeline, TransformationType
from ..parsers.sql_parser import SQL_DIALECT_FORMATS
from .dbt_generator import render_plain_select

_CANONICAL_TO_SQL = {
    "string": "VARCHAR", "integer": "INT", "bigint": "BIGINT",
    "decimal": "DECIMAL(38,6)", "double": "DOUBLE", "date": "DATE",
    "timestamp": "TIMESTAMP", "boolean": "BOOLEAN", "binary": "BINARY",
}


def generate_sql_scripts(pipeline: Pipeline, out_dir: str, format_name: str,
                         dialect: str = "") -> None:
    dialect = dialect or SQL_DIALECT_FORMATS.get(format_name, "")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mapping_names = {m.name for m in pipeline.mappings}

    ordered: List[Mapping] = []
    for wave in pipeline.execution_order():
        for name in wave:
            m = pipeline.mapping(name)
            if m is not None:
                ordered.append(m)

    files: List[str] = []
    if pipeline.sources:
        ddl = _sources_ddl(pipeline, dialect)
        if ddl:
            (out / "00_sources_ddl.sql").write_text(ddl)
            files.append("00_sources_ddl.sql")

    import re
    param_re = re.compile(r"\$\$(\w+)")
    statements: Dict[str, str] = {}
    for i, m in enumerate(ordered, 1):
        unresolved = [t.name for t in m.by_type(TransformationType.SOURCE)
                      if not str(t.properties.get("table", "")).strip()]
        if unresolved:
            # a statement reading FROM <nothing> is broken output — route
            # the whole mapping to the manual queue instead of emitting it
            m.add_issue(IssueSeverity.MANUAL, "SOURCE_UNRESOLVED",
                        "Source reference(s) could not be resolved (%s) — "
                        "statement NOT generated; convert manually from the "
                        "preserved origin SQL" % ", ".join(unresolved),
                        detail=(m.origin or "")[:400],
                        suggestion="Check for dynamic/templated table "
                                   "references in the original statement.")
            continue
        stmt = _statement_for(m, pipeline, mapping_names, dialect, format_name)
        params = sorted(set(param_re.findall(stmt)))
        if params:
            stmt = param_re.sub(lambda mo: ":" + mo.group(1), stmt)
            stmt = ("-- parameters (bind before running): %s\n"
                    % ", ".join(":" + p for p in params)) + stmt
            m.add_issue(IssueSeverity.INFO, "BIND_PARAMETER",
                        "Mapping parameter(s) emitted as bind variables: %s"
                        % ", ".join(params),
                        suggestion="Supply values via your scheduler/session "
                                   "(e.g. Snowflake session variables).")
        # system variables ($PM...) -> binds fed from the scheduler's run
        # context (module 26)
        sys_re = re.compile(r"\$(PM\w+)")
        sysvars = sorted(set(sys_re.findall(stmt)))
        if sysvars:
            stmt = sys_re.sub(lambda mo: ":" + mo.group(1), stmt)
            stmt = ("-- system variables (bind from the scheduler run "
                    "context): %s\n"
                    % ", ".join(":" + v for v in sysvars)) + stmt
            m.add_issue(IssueSeverity.INFO, "SYSTEM_VARIABLE_BIND",
                        "PowerCenter system variable(s) emitted as bind "
                        "variables: %s"
                        % ", ".join("$" + v for v in sysvars),
                        suggestion="Feed run id/name from the orchestrator "
                                   "(Databricks {{job.run_id}}, Airflow "
                                   "run_id, Snowflake task graph run id).")
        pre = str(m.properties.get("pre_sql", "") or "")
        post = str(m.properties.get("post_sql", "") or "")
        if pre:
            stmt = "-- pre-SQL (from the Source Qualifier)\n%s;\n\n%s" \
                % (pre.rstrip(";"), stmt)
        if post:
            stmt = "%s\n\n-- post-SQL (from the Source Qualifier)\n%s;\n" \
                % (stmt.rstrip("\n"), post.rstrip(";"))
        fname = "%02d_%s.sql" % (i, _safe(m.name))
        (out / fname).write_text(stmt)
        files.append(fname)
        statements[m.name] = stmt

    files += _native_extras(pipeline, ordered, out, format_name)

    # shared transformation modules for reused mapplets (module 21) —
    # documentation templates, deliberately not part of deploy_all
    reuse = pipeline.metadata.get("mapplet_reuse") or {}
    comps = pipeline.metadata.get("mapplet_components") or {}
    shared = sorted(k for k, v in reuse.items() if v.get("shared_artifact"))
    if shared:
        from ..parsers.pc_mapplet import render_sql_template
        (out / "shared").mkdir(exist_ok=True)
        for name in shared:
            (out / "shared" / ("mapplet_%s_template.sql" % _safe(name))
             ).write_text(render_sql_template(comps[name]))

    header = ("-- MetaBridge AI deployment script — %s (%s dialect)\n"
              "-- Statements are ordered by pipeline dependencies.\n\n"
              % (pipeline.name, format_name))
    def _chunk(f: str) -> str:
        body = (out / f).read_text().rstrip()
        if body and not body.endswith(";"):
            body += ";"   # a missing terminator would merge into the next file
        return "-- @%s\n%s\n" % (f, body)

    (out / "deploy_all.sql").write_text(
        header + "\n".join(_chunk(f) for f in files))

    # workflow orchestration specs (module 25) — Databricks Workflows job
    # JSON, or an engine-neutral DAG spec for every other warehouse
    dags = pipeline.metadata.get("workflow_dags") or []
    if dags:
        from .orchestration import write_orchestration
        sql_files = {m.name: "%02d_%s.sql" % (i, _safe(m.name))
                     for i, m in enumerate(ordered, 1)}
        write_orchestration(out, dags, format_name, sql_files=sql_files)

    # Databricks Asset Bundle project (module 28)
    if format_name == "databricks":
        from .databricks_bundle import generate_databricks_bundle
        generate_databricks_bundle(pipeline, out.parent / "databricks",
                                   statements)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def _qualify_condition(cond: str, cols, prefix: str) -> str:
    """Qualify bare column references in a merge-clause condition with the
    source alias (word-boundary, known columns only)."""
    import re
    known = {c.lower() for c in cols}

    def sub(mo):
        word = mo.group(0)
        return "%s.%s" % (prefix, word) if word.lower() in known else word

    return re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\b", sub, cond)


def _native_extras(pipeline: Pipeline, ordered: List[Mapping], out: Path,
                   format_name: str) -> List[str]:
    """Platform-native capability files — never generic SQL with a renamed
    extension. Real, runnable statements, clearly labeled as optional."""
    files: List[str] = []
    keyed = [(m, m.unique_key[0]) for m in ordered if m.unique_key]
    incremental = [m for m in ordered if m.load_strategy in
                   (LoadStrategy.MERGE, LoadStrategy.DELETE_INSERT,
                    LoadStrategy.SCD2)]

    if format_name == "databricks" and (keyed or incremental):
        lines = ["-- Delta Lake maintenance (run after loads; optional but",
                 "-- recommended for merge-heavy tables)."]
        for m, key in keyed:
            lines.append("OPTIMIZE %s ZORDER BY (%s);" % (m.name, key))
        for m in ordered:
            if m not in [k[0] for k in keyed]:
                lines.append("OPTIMIZE %s;" % m.name)
        lines.append("-- Partition recommendation: partition large fact "
                     "tables by their date column (PARTITIONED BY (dt)).")
        (out / "90_delta_maintenance.sql").write_text("\n".join(lines) + "\n")
        files.append("90_delta_maintenance.sql")

    if format_name == "snowflake":
        if keyed:
            lines = ["-- Clustering recommendations for keyed/merged tables."]
            for m, key in keyed:
                lines.append("ALTER TABLE %s CLUSTER BY (%s);" % (m.name, key))
            (out / "90_clustering_recommendations.sql").write_text(
                "\n".join(lines) + "\n")
            files.append("90_clustering_recommendations.sql")
        if incremental:
            lines = ["-- Optional automation: stream + task per incremental "
                     "pipeline.", "-- Review WAREHOUSE and SCHEDULE before "
                     "enabling (ALTER TASK ... RESUME)."]
            for m in incremental:
                srcs = [str(t2.properties.get("table", t2.name))
                        for t2 in m.by_type(TransformationType.SOURCE)]
                src = srcs[0] if srcs else m.name
                lines += [
                    "CREATE OR REPLACE STREAM %s_stream ON TABLE %s;"
                    % (m.name, src),
                    "CREATE OR REPLACE TASK %s_task" % m.name,
                    "  WAREHOUSE = <warehouse>",
                    "  SCHEDULE = '60 MINUTE'",
                    "  WHEN SYSTEM$STREAM_HAS_DATA('%s_STREAM')" % m.name.upper(),
                    "AS",
                    "  -- run the %s load (see %s statement file)" % (
                        m.load_strategy.value, m.name),
                    "  CALL SYSTEM$WAIT(0);  -- replace with the MERGE body",
                    "", ]
            (out / "91_streams_tasks_template.sql").write_text(
                "\n".join(lines) + "\n")
            files.append("91_streams_tasks_template.sql")

    recs = {
        "bigquery": ("-- Physical design recommendations (apply in DDL):",
                     "PARTITION BY DATE(<date_column>) and CLUSTER BY (%s)"),
        "redshift": ("-- Physical design recommendations (apply in DDL):",
                     "DISTKEY(%s) SORTKEY(%s)"),
        "synapse": ("-- Physical design recommendations (apply in CTAS WITH):",
                    "DISTRIBUTION = HASH(%s)"),
        "teradata": ("-- Physical design recommendations (apply in DDL):",
                     "PRIMARY INDEX (%s)"),
    }
    if format_name in recs and keyed:
        head, tpl = recs[format_name]
        lines = [head]
        for m, key in keyed:
            advice = tpl % ((key, key) if tpl.count("%s") == 2 else key)
            lines.append("-- %s: %s" % (m.name, advice))
        (out / "90_physical_design_recommendations.sql").write_text(
            "\n".join(lines) + "\n")
        files.append("90_physical_design_recommendations.sql")
    return files


def _transpile(sql: str, dialect: str, m: Mapping, context: str) -> str:
    if not dialect:
        return sql
    try:
        return sqlglot.transpile(sql, read=None, write=dialect, pretty=True)[0]
    except Exception as e:  # noqa: BLE001
        m.add_issue(IssueSeverity.WARNING, "DIALECT_TRANSPILE_FAILED",
                    "Statement for %s emitted in canonical SQL — dialect "
                    "transpile failed" % context, detail=str(e)[:200],
                    suggestion="Review dialect-specific syntax manually.")
        return sql


def _statement_for(m: Mapping, pipeline: Pipeline, mapping_names: set,
                   dialect: str, format_name: str) -> str:
    select = render_plain_select(m, pipeline, mapping_names, dialect)
    tgts = m.by_type(TransformationType.TARGET)
    # write to the TARGET node's physical table, not the mapping name
    target = str(tgts[0].properties.get("table") or m.name) if tgts \
        else m.name
    out_cols = [p.name for p in (tgts[0].ports if tgts and tgts[0].ports else [])]

    if m.load_strategy == LoadStrategy.SCD2 and m.unique_key:
        return _scd2_statement(m, select, target, out_cols, dialect)

    if m.load_strategy == LoadStrategy.VIEW:
        stmt = "CREATE OR REPLACE VIEW %s AS\n%s" % (target, select)
        return _transpile(stmt, dialect, m, target) + "\n"

    if m.load_strategy == LoadStrategy.APPEND:
        stmt = "INSERT INTO %s\n%s" % (target, select)
        return _transpile(stmt, dialect, m, target) + "\n"

    if m.load_strategy in (LoadStrategy.MERGE, LoadStrategy.DELETE_INSERT) \
            and m.unique_key:
        keys = m.unique_key
        cols = out_cols or keys
        on = " AND ".join("t.%s = s.%s" % (k, k) for k in keys)
        set_cols = [c for c in cols if c not in keys] or cols
        update = ", ".join("t.%s = s.%s" % (c, c) for c in set_cols)
        insert_cols = ", ".join(cols)
        insert_vals = ", ".join("s.%s" % c for c in cols)
        # transpile the inner select; MERGE scaffolding stays canonical (widely portable)
        inner = _transpile(select, dialect, m, target)

        # DML routing from an Update Strategy: per-clause conditions,
        # identical ANSI MERGE shape on EVERY warehouse dialect
        clauses = m.properties.get("merge_clauses") or {}

        def _s(cond):
            return _qualify_condition(cond, cols, "s") if cond else ""

        parts = ["MERGE INTO %s t" % target, "USING (",
                 _indent(inner), ") s", "ON %s" % on]
        if clauses.get("delete"):        # DELETE first: it takes precedence
            parts.append("WHEN MATCHED AND %s THEN DELETE"
                         % _s(clauses["delete"]))
        upd_cond = clauses.get("update")
        parts.append("WHEN MATCHED%s THEN UPDATE SET %s"
                     % (" AND %s" % _s(upd_cond) if upd_cond else "",
                        update))
        ins_cond = clauses.get("insert")
        parts.append("WHEN NOT MATCHED%s THEN INSERT (%s) VALUES (%s);"
                     % (" AND %s" % _s(ins_cond) if ins_cond else "",
                        insert_cols, insert_vals))
        return "\n".join(parts) + "\n"

    # FULL (and MERGE without keys, flagged)
    if m.load_strategy in (LoadStrategy.MERGE, LoadStrategy.DELETE_INSERT) \
            and not m.unique_key:
        m.add_issue(IssueSeverity.WARNING, "MERGE_WITHOUT_KEY",
                    "No unique key available — emitted as full rebuild",
                    suggestion="Declare a key to get a MERGE statement.")
    if format_name == "databricks":
        stmt = ("CREATE OR REPLACE TABLE %s USING DELTA AS\n%s"
                % (target, _transpile(select, dialect, m, target)))
        return stmt + ";\n"
    stmt = "CREATE OR REPLACE TABLE %s AS\n%s" % (target, select)
    if dialect == "tsql":
        # T-SQL has no CREATE OR REPLACE TABLE — emit drop-and-select-into
        stmt = ("IF OBJECT_ID('%s') IS NOT NULL DROP TABLE %s;\n"
                "SELECT * INTO %s FROM (\n%s\n) src"
                % (target, target, target, _indent(select)))
        return stmt + ";\n"
    return _transpile(stmt, dialect, m, target) + "\n"


def _scd2_merge_statement(m: Mapping, select: str, target: str,
                          out_cols: List[str], dialect: str) -> str:
    """Detected SCD Type 2 (scd2_cir) as ONE MERGE using the mapping's OWN
    versioning columns — the Delta Lake SCD2 pattern, portable to every
    MERGE-capable warehouse: changed business keys are staged twice (once
    keyed, to expire the current version; once with a NULL merge key, to
    insert the new version)."""
    cir = dict(m.properties["scd2_cir"])
    # merge on the BUSINESS key from the pattern — a declared PK is often
    # the per-version surrogate, which must never be the change-merge key
    keys = list(cir.get("business_key") or m.unique_key)
    vfrom = cir.get("effective_start_column")
    vto = cir.get("effective_end_column")
    flag = cir.get("current_flag_column")
    surrogate = cir.get("surrogate_key")
    tracking = [c for c in (vfrom, vto, flag) if c]
    cols = [c for c in out_cols if c and c not in tracking] or keys

    scd = dict(m.properties.get("scd", {}) or {})
    check = scd.get("check_cols")
    change_cols = [c for c in check if c in cols] \
        if isinstance(check, list) else []
    if not change_cols:
        change_cols = [c for c in cols
                       if c not in keys and c != surrogate]

    def diff(a: str, b: str) -> str:
        parts = ["(%(a)s.%(c)s <> %(b)s.%(c)s OR (%(a)s.%(c)s IS NULL AND "
                 "%(b)s.%(c)s IS NOT NULL) OR (%(a)s.%(c)s IS NOT NULL AND "
                 "%(b)s.%(c)s IS NULL))" % {"a": a, "b": b, "c": c}
                 for c in change_cols]
        return "\n       OR ".join(parts) if parts else "1 = 0"

    def current(alias: str) -> str:
        return "%s.%s = 'Y'" % (alias, flag) if flag \
            else "%s.%s IS NULL" % (alias, vto)

    on = " AND ".join("t.%s = staged.mb_merge_key_%d" % (k, i)
                      for i, k in enumerate(keys))
    cur_join = " AND ".join("cur.%s = s.%s" % (k, k) for k in keys)
    s_cols = ", ".join("s.%s" % c for c in cols)
    keyed = ", ".join("s.%s AS mb_merge_key_%d" % (k, i)
                      for i, k in enumerate(keys))
    nulled = ", ".join("NULL AS mb_merge_key_%d" % i
                       for i in range(len(keys)))
    close_set = ("t.%s = CURRENT_TIMESTAMP" % vto) if vto else ""
    if flag:
        close_set += (", " if close_set else "") + "t.%s = 'N'" % flag
    ins_cols = ", ".join(cols) + "".join(", %s" % c for c in tracking)
    ins_vals = ", ".join("staged.%s" % c for c in cols)
    for c in tracking:
        ins_vals += ", " + ("CURRENT_TIMESTAMP" if c == vfrom
                            else "NULL" if c == vto else "'Y'")
    inner = _transpile(select, dialect, m, target)
    m.add_issue(IssueSeverity.INFO, "SCD_TYPE_2_MERGE",
                "Emitted as a MERGE-based SCD2 load using the mapping's own "
                "versioning columns (start=%s end=%s flag=%s); change "
                "detection over: %s"
                % (vfrom or "-", vto or "-", flag or "-",
                   ", ".join(change_cols)))
    return ("""-- SCD Type 2 MERGE for %(t)s
-- business key: %(bk)s%(sk)s
-- changed keys are staged twice: keyed (expires the current version)
-- and NULL-keyed (inserts the new version)
MERGE INTO %(t)s t
USING (
  SELECT %(cols_s)s, %(keyed)s
  FROM (
%(sel)s
  ) s
  UNION ALL
  SELECT %(cols_s)s, %(nulled)s
  FROM (
%(sel)s
  ) s
  JOIN %(t)s cur
    ON %(cur_join)s AND %(cur_current)s
  WHERE %(diff_cur)s
) staged
ON %(on)s AND %(t_current)s
WHEN MATCHED AND (%(diff_t)s) THEN UPDATE
  SET %(close)s
WHEN NOT MATCHED THEN INSERT (%(ins_cols)s)
  VALUES (%(ins_vals)s);
""" % {"t": target, "bk": ", ".join(keys),
       "sk": ("; surrogate key: %s (minted per version)" % surrogate)
       if surrogate else "",
       "cols_s": s_cols, "keyed": keyed, "nulled": nulled,
       "sel": _indent(_indent(inner)), "cur_join": cur_join,
       "cur_current": current("cur"), "diff_cur": diff("cur", "s"),
       "on": on, "t_current": current("t"), "diff_t": diff("t", "staged"),
       "close": close_set, "ins_cols": ins_cols, "ins_vals": ins_vals})


def _scd2_statement(m: Mapping, select: str, target: str,
                    out_cols: List[str], dialect: str) -> str:
    """SCD Type 2 as two portable statements: close changed versions, then
    insert new versions. Tracking columns: mb_valid_from / mb_valid_to /
    mb_is_current."""
    if m.properties.get("scd2_cir"):
        return _scd2_merge_statement(m, select, target, out_cols, dialect)
    scd = dict(m.properties.get("scd", {}) or {})
    key = m.unique_key[0]
    updated = str(scd.get("updated_at") or "updated_at")
    cols = [c for c in out_cols if c] or [key]
    col_list = ", ".join(cols)
    src_cols = ", ".join("s.%s" % c for c in cols)
    inner = _transpile(select, dialect, m, target)
    m.add_issue(IssueSeverity.INFO, "SCD_TYPE_2",
                "Emitted as a two-statement SCD2 load (close + insert) with "
                "mb_valid_from/mb_valid_to/mb_is_current tracking columns")
    return ("""-- SCD Type 2 load for %(t)s (key: %(k)s, change signal: %(u)s)
-- Statement 1: close current versions that changed
UPDATE %(t)s
SET mb_valid_to = src.%(u)s, mb_is_current = FALSE
FROM (
%(sel)s
) src
WHERE %(t)s.%(k)s = src.%(k)s
  AND %(t)s.mb_is_current = TRUE
  AND %(t)s.%(u)s < src.%(u)s;

-- Statement 2: insert new current versions
INSERT INTO %(t)s (%(cols)s, mb_valid_from, mb_valid_to, mb_is_current)
SELECT %(scols)s, s.%(u)s, NULL, TRUE
FROM (
%(sel)s
) s
LEFT JOIN %(t)s cur
  ON cur.%(k)s = s.%(k)s AND cur.mb_is_current = TRUE
WHERE cur.%(k)s IS NULL OR cur.%(u)s < s.%(u)s;
""" % {"t": target, "k": key, "u": updated, "sel": _indent(inner),
       "cols": col_list, "scols": src_cols})


def _indent(s: str, pad: str = "    ") -> str:
    return "\n".join(pad + l for l in s.split("\n"))


def _sources_ddl(pipeline: Pipeline, dialect: str) -> str:
    parts = []
    for s in pipeline.sources:
        if not s.columns or all(c.name == "ROW_DATA" for c in s.columns):
            continue
        cols = ",\n".join("    %s %s" % (c.name, _CANONICAL_TO_SQL.get(c.datatype, "VARCHAR"))
                          for c in s.columns)
        qualified = "%s.%s" % (s.schema, s.name) if s.schema else s.name
        stmt = "CREATE TABLE IF NOT EXISTS %s (\n%s\n)" % (qualified, cols)
        try:
            if dialect:
                stmt = sqlglot.transpile(stmt, read=None, write=dialect, pretty=True)[0]
        except Exception:  # noqa: BLE001
            pass
        parts.append("-- source: %s\n%s;\n" % (s.name, stmt))
    return "\n".join(parts)

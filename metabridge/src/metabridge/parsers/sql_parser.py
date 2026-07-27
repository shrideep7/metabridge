"""Parse warehouse SQL scripts (Snowflake, Databricks, BigQuery, ...) into the IR.

Turns a folder (or single file) of .sql scripts into a Pipeline:

  * CREATE [OR REPLACE] VIEW  x AS SELECT ...   -> mapping (VIEW strategy)
  * CREATE TABLE x AS SELECT ... (CTAS)         -> mapping (FULL load)
  * INSERT [OVERWRITE] INTO x SELECT ...        -> mapping (APPEND / FULL)
  * MERGE INTO x USING (...) ON a = b ...       -> mapping (MERGE, keys from ON)
  * CREATE TABLE x (col type, ...)              -> source-table definition

The SELECT bodies go through the same decomposer as dbt models, so everything
downstream (PowerCenter/IDMC generation, reports, governance) works unchanged.
Statements that don't fit (procedures, tasks, grants...) are inventoried as
issues rather than dropped silently.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp

from ..ir.model import (
    ConversionIssue, IssueSeverity, LoadStrategy, Pipeline, Port, SourceTable,
    TransformationType, canonical_type,
)
from ..sqlx.decompose import decompose_model

# format name (what users pick) -> sqlglot dialect
SQL_DIALECT_FORMATS: Dict[str, str] = {
    "snowflake": "snowflake",
    "databricks": "databricks",
    "bigquery": "bigquery",
    "redshift": "redshift",
    "synapse": "tsql",
    "sqlserver": "tsql",
    "oracle": "oracle",
    "postgres": "postgres",
    "teradata": "teradata",
    "sql": "",  # generic ANSI
}


import re

# schemachange / SnowSQL / liquibase-style template variables: &{var} or &var
_TEMPLATE_VAR_RE = re.compile(r"&\{(\w+)\}|&(\w+)\b(?=[.\s;,)])")


def _shield_template_vars(text: str) -> Tuple[str, List[str]]:
    found: List[str] = []

    def sub(mo):
        name = mo.group(1) or mo.group(2)
        found.append(name)
        return "MBVAR_" + name

    return _TEMPLATE_VAR_RE.sub(sub, text), sorted(set(found))


def parse_sql_scripts(path: str, format_name: str = "sql",
                      dialect: str = "") -> Pipeline:
    dialect = dialect or SQL_DIALECT_FORMATS.get(format_name, "")
    p = Path(path)
    exts = (".sql", ".btq", ".bteq", ".pls", ".pks", ".pkb", ".prc",
            ".tsql", ".ddl")
    files = [p] if p.is_file() else sorted(
        f for suf in exts for f in p.rglob("*" + suf))
    if not files:
        raise FileNotFoundError("No SQL script files found under %s" % path)

    pipeline = Pipeline(name=p.stem, source_format=format_name)
    pipeline.metadata["dialect"] = dialect

    sources: Dict[str, SourceTable] = {}
    pending: List[Tuple[str, exp.Expression, LoadStrategy, List[str], str]] = []

    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError as e:
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.ERROR, code="FILE_UNREADABLE",
                message="Could not read %s" % f.name, detail=str(e)[:200]))
            continue
        text, tpl_vars = _shield_template_vars(text)
        if tpl_vars:
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.WARNING, code="TEMPLATE_VARIABLES",
                message="%s uses deployment template variables — carried "
                        "through as identifiers" % f.name,
                detail=", ".join("&{%s}" % v for v in tpl_vars),
                suggestion="Bind these (MBVAR_*) via your deployment tooling "
                           "or dbt vars after conversion."))
        from .legacy_script import is_legacy_dialect
        if is_legacy_dialect(dialect):
            # phase 3: BTEQ commands / GO batches / procedural blocks are
            # split out BEFORE AST parsing, all line-tracked
            _ingest_legacy_file(text, f.name, dialect, pipeline, sources,
                                pending)
            continue
        try:
            statements = sqlglot.parse(text, read=dialect or None)
        except Exception as e:  # noqa: BLE001
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.ERROR, code="SQL_PARSE_ERROR",
                message="Could not parse %s" % f.name, detail=str(e)[:300]))
            continue
        for stmt in statements:
            if stmt is None:
                continue
            try:
                _classify_statement(stmt, f.name, pipeline, sources, pending)
            except Exception as e:  # noqa: BLE001 — one statement must never kill the run
                pipeline.issues.append(ConversionIssue(
                    severity=IssueSeverity.MANUAL, code="STATEMENT_PARSE_FAILED",
                    message="A statement in %s could not be interpreted" % f.name,
                    detail="%s: %s" % (type(e).__name__, str(e)[:200]),
                    suggestion="Review this statement manually; the rest of "
                               "the project converted normally."))

    inv = pipeline.metadata.setdefault(
        "inventory", {"dataflow": 0, "administrative": 0, "manual": 0})
    inv["dataflow"] = len(pending)

    # Derived tables (views/CTAS/MERGE targets) are readable by later
    # statements — register their output schemas so every consumer resolves
    # the same column set (otherwise shared source definitions diverge).
    for name, body, _strategy, _keys, _origin in pending:
        cols = _output_columns(body)
        if cols and name and name.lower() not in sources:
            sources[name.lower()] = SourceTable(
                name=name, columns=[Port(name=c) for c in cols])

    # register DDL-defined tables as pipeline sources
    pipeline.sources = list(sources.values())

    # build mappings (decomposer needs the source dict for column resolution)
    target_names = {name.lower() for name, *_ in pending}
    from .legacy_script import is_legacy_dialect as _is_legacy
    for name, select, strategy, keys, origin in pending:
        local_sources = dict(sources)
        if _is_legacy(dialect):
            # legacy bodies were normalized on the dialect-parsed AST —
            # canonicalize ONCE here so no downstream step re-renders
            # TOP/ISNULL/&co in the source dialect
            mapping = decompose_model(name, select.sql(), "",
                                      local_sources)
        else:
            mapping = decompose_model(name,
                                      select.sql(dialect=dialect or None),
                                      dialect, local_sources)
        mapping.load_strategy = strategy
        mapping.unique_key = keys
        mapping.origin = origin
        _attach_target(mapping, name)
        # dependencies: reads from tables that other statements produce
        for t in mapping.by_type(TransformationType.SOURCE):
            ref = str(t.properties.get("table", "")).lower()
            if ref in target_names and ref != name.lower():
                real = next(n for n, *_ in pending if n.lower() == ref)
                if real not in mapping.depends_on:
                    mapping.depends_on.append(real)
        if strategy == LoadStrategy.VIEW:
            mapping.add_issue(IssueSeverity.INFO, "VIEW_SOURCE",
                              "Originated from a CREATE VIEW — converted as a "
                              "view/full-load pipeline.")
        pipeline.mappings.append(mapping)

    # phase 3: temp-table chain analysis over the assembled dependency graph
    from .legacy_script import is_legacy_dialect
    if is_legacy_dialect(dialect):
        from .legacy_semantics import analyze_temp_chains
        analyze_temp_chains(pipeline)
        from ..detection.sql_dialect import detect_sql_dialect
        pipeline.metadata["dialect_detection"] = detect_sql_dialect(
            "\n".join((f.read_text(errors="replace")[:100_000]
                       for f in files))[:400_000])
    return pipeline


def _ingest_legacy_file(text: str, filename: str, dialect: str,
                        pipeline: Pipeline,
                        sources: Dict[str, SourceTable],
                        pending: list) -> None:
    """One legacy script -> typed units -> IR (phase 3)."""
    from .legacy_script import split_legacy_script
    split = split_legacy_script(text, dialect)

    # runtime commands (BTEQ / GO / BT-ET): orchestration, never SQL logic
    cmds = pipeline.metadata.setdefault("runtime_commands", [])
    for c in split.commands:
        cmds.append({"command": c.command, "category": c.category,
                     "file": filename, "line": c.line,
                     "strategy": c.strategy,
                     "manual_review": c.manual_review})
        if c.category == "batch_separator":
            continue                       # GO needs no report noise
        sev = IssueSeverity.MANUAL if c.manual_review else IssueSeverity.INFO
        pipeline.issues.append(ConversionIssue(
            severity=sev, code="RUNTIME_COMMAND",
            message="%s (%s:%d) is runtime configuration, not "
                    "transformation logic — %s"
            % (c.command, filename, c.line, c.category),
            detail=c.text[:200], suggestion=c.strategy))

    for unit in split.units:
        if unit.kind == "procedural":
            _ingest_procedural_unit(unit, filename, dialect, pipeline,
                                    sources, pending)
            continue
        try:
            statements = sqlglot.parse(unit.text, read=dialect or None)
        except Exception as e:  # noqa: BLE001
            pipeline.issues.append(ConversionIssue(
                severity=IssueSeverity.MANUAL, code="SQL_PARSE_ERROR",
                message="Statement at %s:%d could not be parsed — routed "
                        "to manual review" % (filename, unit.line),
                detail="%s | %s" % (str(e)[:150], unit.text[:250]),
                suggestion="Review the original statement; the rest of "
                           "the script converted normally."))
            continue
        for stmt in statements:
            if stmt is None:
                continue
            where = "%s:%d" % (filename, unit.line)
            from ..sqlx.legacy_normalize import (classify_command,
                                                 normalize_legacy_statement)
            if isinstance(stmt, exp.Command) or \
                    type(stmt).__name__ in ("Collect", "Analyze"):
                fd = classify_command(stmt, dialect)
                if fd is not None:
                    pipeline.issues.append(ConversionIssue(
                        severity=IssueSeverity.INFO, code=fd["code"],
                        message="%s (%s)" % (fd["message"], where),
                        suggestion=fd["suggestion"]))
                    inv = pipeline.metadata.setdefault(
                        "inventory", {"dataflow": 0, "administrative": 0,
                                      "manual": 0})
                    inv["administrative"] += 1
                    continue
            try:
                stmt, findings, flags = \
                    normalize_legacy_statement(stmt, dialect)
            except Exception:  # noqa: BLE001 — normalization must not kill
                findings, flags = [], {"temporary": False}
            _SEV = {"INFO": IssueSeverity.INFO,
                    "WARNING": IssueSeverity.WARNING,
                    "MANUAL": IssueSeverity.MANUAL}
            for fd in findings:
                pipeline.issues.append(ConversionIssue(
                    severity=_SEV[fd["severity"]], code=fd["code"],
                    message="%s (%s) [%s]" % (fd["message"], where,
                                              fd["automation"]),
                    detail=fd.get("detail", ""),
                    suggestion=fd.get("suggestion", "")))
            # T-SQL SELECT ... INTO #temp -> CTAS marked temporary
            if isinstance(stmt, exp.Select) and stmt.args.get("into"):
                into = stmt.args["into"]
                tname = _table_name(into.this)
                temp = bool(into.args.get("temporary")) or \
                    tname.startswith("#")
                tname = tname.lstrip("#")
                stmt.set("into", None)
                pending.append((tname, stmt, LoadStrategy.FULL,
                                [], "SELECT INTO %s (%s)" % (tname, where)))
                if temp:
                    pipeline.metadata.setdefault(
                        "temp_objects", []).append(tname)
                continue
            if flags.get("temporary") and isinstance(stmt, exp.Create):
                pipeline.metadata.setdefault("temp_objects", []).append(
                    _table_name(stmt.this))
            try:
                _classify_statement(stmt, where, pipeline, sources, pending)
            except Exception as e:  # noqa: BLE001
                pipeline.issues.append(ConversionIssue(
                    severity=IssueSeverity.MANUAL,
                    code="STATEMENT_PARSE_FAILED",
                    message="A statement at %s could not be interpreted"
                    % where,
                    detail="%s: %s" % (type(e).__name__, str(e)[:200])))


def _ingest_procedural_unit(unit, filename: str, dialect: str,
                            pipeline: Pipeline,
                            sources: Dict[str, SourceTable],
                            pending: list) -> None:
    """CREATE PROCEDURE/FUNCTION/PACKAGE/TRIGGER/MACRO — recorded with
    full provenance; the decomposition engine takes it from here."""
    units = pipeline.metadata.setdefault("procedural_units", [])
    entry = {"object_type": unit.object_type,
             "object_name": unit.object_name or "(unnamed)",
             "file": filename, "line": unit.line,
             "dialect": dialect,
             "sql": unit.text[:20000]}
    units.append(entry)

    # section 12: decompose and classify — never blind translation
    from .legacy_procedures import decompose_procedure
    try:
        deco = decompose_procedure(entry)
    except Exception as e:  # noqa: BLE001 — decomposition must not kill
        deco = None
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.MANUAL, code="PROCEDURAL_OBJECT",
            message="%s '%s' (%s:%d) could not be decomposed (%s) — "
                    "manual conversion"
            % (unit.object_type, unit.object_name or "?", filename,
               unit.line, str(e)[:80]),
            obj=unit.object_name, detail=unit.text[:400]))
    if deco is None:
        return
    pipeline.metadata.setdefault("procedure_decompositions", []).append(deco)

    # set-based DML inside the body becomes candidate mappings — the
    # provenance keeps every model linked to its procedure
    extracted = 0
    for st in deco["statements"]:
        if st["classification"] not in ("DATA_TRANSFORMATION",):
            continue
        try:
            parsed = sqlglot.parse_one(st["sql"], read=dialect or None)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed, (exp.Insert, exp.Create, exp.Merge)):
            from ..sqlx.legacy_normalize import normalize_legacy_statement
            try:
                parsed, _f, _fl = normalize_legacy_statement(parsed, dialect)
            except Exception:  # noqa: BLE001
                pass
            before = len(pending)
            try:
                _classify_statement(parsed, "%s:%d (in %s)"
                                    % (filename, unit.line,
                                       deco["object_name"]),
                                    pipeline, sources, pending)
                extracted += len(pending) - before
            except Exception:  # noqa: BLE001
                pass

    counts = deco["statement_counts"]
    risky = deco["detections"]["dynamic_sql"] or \
        deco["detections"]["cursor_loops"] or \
        deco["detections"]["autonomous_transaction"] or \
        counts.get("MANUAL_REVIEW", 0) > 0
    sev = IssueSeverity.MANUAL if risky or not extracted \
        else IssueSeverity.WARNING
    pipeline.issues.append(ConversionIssue(
        severity=sev, code="PROCEDURAL_OBJECT",
        message="%s '%s' (%s:%d) decomposed: %s%s"
        % (unit.object_type, unit.object_name or "?", filename, unit.line,
           ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())),
           "; %d set-based statement(s) extracted as pipeline mappings"
           % extracted if extracted else ""),
        obj=unit.object_name,
        detail="; ".join("%s=%s" % (k, v) for k, v in
                         deco["detections"].items() if v),
        suggestion="dbt: %s | Databricks: %s | Snowflake: %s"
        % (deco["recommendations"]["dbt"],
           deco["recommendations"]["databricks"],
           deco["recommendations"]["snowflake"])))


def _output_columns(body: exp.Expression) -> List[str]:
    """Output column names of a SELECT/UNION body ([] when SELECT * hides them)."""
    node = body
    while isinstance(node, (exp.Subquery, exp.Paren)):
        node = node.this
    if isinstance(node, exp.Union):
        node = node.this  # first branch defines the shape
    if not isinstance(node, exp.Select):
        return []
    cols: List[str] = []
    for i, e in enumerate(node.expressions):
        if isinstance(e, exp.Star):
            return []
        if isinstance(e, exp.Alias):
            cols.append(e.alias)
        elif isinstance(e, exp.Column):
            cols.append(e.name)
        else:
            cols.append("col_%d" % i)
    return cols


def _table_name(t: Optional[exp.Expression]) -> str:
    if isinstance(t, exp.Schema):
        t = t.this
    if t is None:
        return ""
    if isinstance(t, exp.Table):
        try:
            return t.name
        except Exception:  # noqa: BLE001 — dynamic identifiers can fail to render
            pass
    try:
        return str(t)
    except Exception:  # noqa: BLE001
        return str(getattr(t, "alias_or_name", "") or
                   getattr(t, "this", "") or "unresolved_object")[:80]


def _classify_statement(stmt: exp.Expression, filename: str, pipeline: Pipeline,
                        sources: Dict[str, SourceTable],
                        pending: list) -> None:
    if isinstance(stmt, exp.Create):
        kind = (stmt.kind or "").upper()
        name = _table_name(stmt.this)
        body = stmt.expression
        if body is not None and isinstance(body, (exp.Select, exp.Union, exp.Subquery)):
            if not name.strip():
                # dynamic tables / exotic DDL can defeat name extraction —
                # a mapping must never exist without a name (it would emit
                # 'CREATE TABLE <nothing>' downstream)
                stem = re.sub(r"\W+", "_", Path(filename).stem).strip("_")
                name = "unnamed_%s_%d" % (stem or "object", len(pending) + 1)
                pipeline.issues.append(ConversionIssue(
                    severity=IssueSeverity.WARNING, code="TARGET_NAME_SYNTHESIZED",
                    message="Could not determine the target name of a CREATE %s "
                            "in %s — generated as '%s'; rename before deploying"
                            % (kind or "?", filename, name),
                    detail=stmt.sql()[:200]))
            strategy = LoadStrategy.VIEW if kind == "VIEW" else LoadStrategy.FULL
            pending.append((name, body, strategy, [], stmt.sql()[:400]))
            return
        if kind == "TABLE" and isinstance(stmt.this, exp.Schema):
            cols = []
            for cd in stmt.this.expressions:
                if isinstance(cd, exp.ColumnDef):
                    cols.append(Port(
                        name=cd.name,
                        datatype=canonical_type(cd.args["kind"].sql()
                                                if cd.args.get("kind") else "string")))
            if name:
                sources[name.lower()] = SourceTable(name=name, columns=cols)
            return
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.INFO, code="DDL_SKIPPED",
            message="CREATE %s '%s' in %s has no query body — recorded only"
            % (kind or "?", name, filename)))
        return

    if isinstance(stmt, exp.Insert):
        name = _table_name(stmt.this)
        body = stmt.expression
        if body is not None and name:
            strategy = LoadStrategy.FULL if stmt.args.get("overwrite") \
                else LoadStrategy.APPEND
            pending.append((name, body, strategy, [], stmt.sql()[:400]))
        return

    if isinstance(stmt, exp.Merge):
        _classify_merge(stmt, filename, pipeline, pending)
        return

    inv = pipeline.metadata.setdefault(
        "inventory", {"dataflow": 0, "administrative": 0, "manual": 0})
    if isinstance(stmt, (exp.Grant, exp.Comment, exp.Use, exp.Set,
                         exp.Drop, exp.Alter)):
        inv["administrative"] += 1
        return  # non-dataflow statements — nothing to convert

    inv["manual"] += 1
    try:
        original = stmt.sql(pretty=True)
    except Exception:  # noqa: BLE001
        original = str(getattr(stmt, "this", ""))[:2000]
    pipeline.issues.append(ConversionIssue(
        severity=IssueSeverity.MANUAL, code="STATEMENT_UNSUPPORTED",
        message="Statement type %s in %s is not converted"
        % (type(stmt).__name__, filename),
        detail=original[:4000],
        suggestion="Procedures/tasks need manual conversion to orchestration."))


def _classify_merge(stmt: exp.Merge, filename: str, pipeline: Pipeline,
                    pending: list) -> None:
    target = _table_name(stmt.this)
    using = stmt.args.get("using")
    body: Optional[exp.Expression] = None
    if isinstance(using, exp.Subquery):
        body = using.this
    elif isinstance(using, exp.Table):
        body = sqlglot.parse_one("SELECT * FROM %s" % using.name)
    if body is None or not target:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.MANUAL, code="MERGE_UNSUPPORTED",
            message="MERGE in %s could not be interpreted" % filename,
            detail=stmt.sql()[:200]))
        return
    keys: List[str] = []
    on = stmt.args.get("on")
    if on is not None:
        for eq in on.find_all(exp.EQ):
            for side in (eq.this, eq.expression):
                if isinstance(side, exp.Column) and \
                        (side.table or "").lower() in ("", "t", "tgt", "target",
                                                       target.lower()):
                    if side.name not in keys:
                        keys.append(side.name)
            break  # first equality pair is the business key heuristic
    pending.append((target, body, LoadStrategy.MERGE, keys, stmt.sql()[:400]))


def _attach_target(mapping, name: str) -> None:
    from ..ir.model import Link, Transformation
    out = mapping.transformation("__OUTPUT__")
    ports = [Port(name=p.name, datatype=p.datatype) for p in (out.ports if out else [])] \
        or [Port(name="ROW_DATA")]
    tgt = Transformation(name="TGT_" + name, type=TransformationType.TARGET,
                         ports=ports, properties={"table": name})
    mapping.transformations.append(tgt)
    if out is not None:
        mapping.links.append(Link("__OUTPUT__", tgt.name))

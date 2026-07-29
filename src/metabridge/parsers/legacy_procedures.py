"""Stored-procedure decomposition (Phase 3, section 12).

Procedures are never blindly translated. Each procedural block is
decomposed into:

    parameters (direction + type), variables, cursor loops, temp tables,
    statements — each classified:

    DATA_TRANSFORMATION  INSERT/UPDATE/DELETE/MERGE/CTAS with real logic
    DATA_LOAD            plain INSERT ... VALUES / COPY-style loads
    CONTROL_FLOW         IF/LOOP/WHILE/FOR/RETURN/GOTO/CASE blocks
    AUDIT_LOGGING        writes to *audit*/*log* tables, PRINT,
                         DBMS_OUTPUT
    ERROR_HANDLING       EXCEPTION WHEN / TRY-CATCH / RAISERROR / THROW
    DYNAMIC_SQL          EXECUTE IMMEDIATE / sp_executesql / EXEC(@sql)
    DDL                  CREATE/DROP/TRUNCATE/ALTER
    SECURITY             GRANT/REVOKE
    EXTERNAL_CALL        DBMS_*/UTL_*/xp_* / CALL other_proc
    TRANSACTION          COMMIT/ROLLBACK/SAVEPOINT/BEGIN TRAN
    MANUAL_REVIEW        anything unrecognized

Set-based DATA_TRANSFORMATION statements that parse cleanly are handed
back to the SQL pipeline as candidate mappings (provenance kept), so a
procedure whose body is really an ELT chain converts like one. The
control-flow skeleton, dynamic SQL and cursors stay declared for review
with per-target recommendations (dbt model/macro/operation; Databricks
SQL/notebook/job; Snowflake procedure/task/stream/dynamic table).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

_PARAM_ORACLE = re.compile(
    r"(\w+)\s+(IN\s+OUT|IN|OUT)?\s*(\w+(?:\s*\(\s*[\d,\s]+\))?)"
    r"\s*(?::=|DEFAULT\s+[^,)]+)?\s*(?:,|\)|$)", re.IGNORECASE)
_PARAM_TSQL = re.compile(
    r"(@\w+)\s+([\w()\d,\s]+?)(?:\s*=\s*[^,)]+)?\s*(OUTPUT|OUT)?\s*(?:,|\bAS\b|$)",
    re.IGNORECASE)
_VAR_TSQL = re.compile(r"^\s*DECLARE\s+(@\w+)\s+([\w()\d,]+)",
                       re.IGNORECASE | re.MULTILINE)
_VAR_ORACLE = re.compile(r"^\s*(\w+)\s+(?:CONSTANT\s+)?"
                         r"(NUMBER|VARCHAR2|DATE|TIMESTAMP|INTEGER|CLOB|"
                         r"BOOLEAN|PLS_INTEGER)[^;]*;",
                         re.IGNORECASE | re.MULTILINE)

_CLASSIFIERS: List[Tuple[str, re.Pattern]] = [
    ("DYNAMIC_SQL", re.compile(
        r"^\s*(EXECUTE\s+IMMEDIATE|EXEC(UTE)?\s*\(|EXEC(UTE)?\s+sp_executesql)",
        re.IGNORECASE)),
    ("ERROR_HANDLING", re.compile(
        r"^\s*(EXCEPTION\b|BEGIN\s+TRY|BEGIN\s+CATCH|END\s+TRY|END\s+CATCH|"
        r"RAISERROR|THROW\b|RAISE\b|RAISE_APPLICATION_ERROR|WHEN\s+OTHERS)",
        re.IGNORECASE)),
    ("TRANSACTION", re.compile(
        r"^\s*(COMMIT|ROLLBACK|SAVEPOINT|BEGIN\s+TRAN(SACTION)?|BT|ET)\b",
        re.IGNORECASE)),
    ("SECURITY", re.compile(r"^\s*(GRANT|REVOKE)\b", re.IGNORECASE)),
    ("DDL", re.compile(
        r"^\s*(CREATE|DROP|TRUNCATE|ALTER)\b", re.IGNORECASE)),
    ("EXTERNAL_CALL", re.compile(
        r"^\s*(CALL\s+\w|EXEC(UTE)?\s+\w)|\b(DBMS_\w+|UTL_\w+|xp_\w+)\s*[.(]",
        re.IGNORECASE)),
    ("CONTROL_FLOW", re.compile(
        r"^\s*(IF\b|ELSIF|ELSE\b|END\s+IF|LOOP\b|END\s+LOOP|WHILE\b|"
        r"FOR\b|RETURN\b|GOTO\b|CASE\b|BEGIN\b|END\b|BREAK\b|CONTINUE\b|"
        r"SET\s+@?\w+\s*=|DECLARE\b|OPEN\b|FETCH\b|CLOSE\b|EXIT\b)",
        re.IGNORECASE)),
]
_AUDIT = re.compile(r"(DBMS_OUTPUT|PRINT\b|_?(audit|log|etl_run)\w*\b)",
                    re.IGNORECASE)
_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE|SELECT)\b",
                  re.IGNORECASE)
_CURSOR = re.compile(r"\bCURSOR\b|\bFOR\s+\w+\s+IN\b.*\bLOOP\b|\bFETCH\b",
                     re.IGNORECASE)
_BULK = re.compile(r"\bBULK\s+COLLECT\b|\bFORALL\b", re.IGNORECASE)
_AUTONOMOUS = re.compile(r"PRAGMA\s+AUTONOMOUS_TRANSACTION", re.IGNORECASE)
_TEMP_REF = re.compile(r"(?<![\w@])#(\w+)|\bVOLATILE\s+TABLE\s+(\w+)|"
                       r"\bGLOBAL\s+TEMPORARY\s+TABLE\s+(\w+)",
                       re.IGNORECASE)


def _split_statements(body: str) -> List[str]:
    """Split a procedure body on top-level semicolons (strings shielded)."""
    shielded = re.sub(r"'(?:[^']|'')*'", lambda m: "'" + "x" * (
        len(m.group(0)) - 2) + "'", body)
    parts, start = [], 0
    for i, ch in enumerate(shielded):
        if ch == ";":
            parts.append(body[start:i + 1])
            start = i + 1
    if body[start:].strip():
        parts.append(body[start:])
    return [s.strip() for s in parts if s.strip()]


def _classify(stmt: str) -> str:
    # a block's bare BEGIN prefixes its first statement — strip it so the
    # statement classifies as itself (BEGIN TRY/TRAN are kept)
    while True:
        m = re.match(r"^\s*BEGIN\b(?!\s+(TRY|CATCH|TRAN))\s*",
                     stmt, re.IGNORECASE)
        if not m or not stmt[m.end():].strip():
            break
        stmt = stmt[m.end():]
    if _DML.match(stmt) and _AUDIT.search(stmt.split("\n")[0]) or \
            re.match(r"^\s*(PRINT|DBMS_OUTPUT)", stmt, re.IGNORECASE):
        return "AUDIT_LOGGING"
    for label, rx in _CLASSIFIERS:
        if rx.search(stmt) and rx.match(stmt) or \
                (label == "EXTERNAL_CALL" and rx.search(stmt)
                 and not _DML.match(stmt)):
            if label == "DDL" and re.match(
                    r"^\s*CREATE\s+(GLOBAL\s+TEMPORARY|VOLATILE|TABLE\s+#)",
                    stmt, re.IGNORECASE):
                return "DDL"     # temp DDL is still DDL, chain pass tags it
            return label
    if _DML.match(stmt):
        head = stmt.upper().lstrip()
        if head.startswith("INSERT") and "SELECT" not in head:
            return "DATA_LOAD"
        return "DATA_TRANSFORMATION"
    return "MANUAL_REVIEW"


def _parameters(header: str, dialect: str) -> List[dict]:
    mo = re.search(r"\(", header)
    params: List[dict] = []
    if dialect == "tsql":
        # T-SQL params may come without parentheses, before AS
        seg = header[mo.start() + 1:] if mo else \
            header.split(None, 3)[-1] if "@" in header else ""
        for m in _PARAM_TSQL.finditer(seg):
            params.append({"name": m.group(1),
                           "datatype": m.group(2).strip(),
                           "direction": "OUT" if m.group(3) else "IN"})
        return params
    if not mo:
        return params
    depth, end = 0, len(header)
    for i in range(mo.start(), len(header)):
        if header[i] == "(":
            depth += 1
        elif header[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    seg = header[mo.start() + 1:end]
    for m in _PARAM_ORACLE.finditer(seg):
        if m.group(1).upper() in ("IN", "OUT"):
            continue
        params.append({"name": m.group(1),
                       "direction": (m.group(2) or "IN").upper()
                       .replace("IN OUT", "INOUT"),
                       "datatype": (m.group(3) or "").strip()})
    return params


_TARGET_RECOMMENDATIONS = {
    "dbt": {
        "set_based": "models (one per DATA_TRANSFORMATION statement) + "
                     "on-run hooks for audit statements",
        "procedural": "macro/operation (run-operation) for the skeleton; "
                      "orchestrate step order outside dbt",
        "dynamic": "manual review — dbt cannot express dynamic SQL safely",
    },
    "databricks": {
        "set_based": "SQL script per statement, wired as Workflow job "
                     "tasks (or Delta Live Tables for pure pipelines)",
        "procedural": "notebook workflow preserving the control flow",
        "dynamic": "notebook with explicit parameterization — review",
    },
    "snowflake": {
        "set_based": "SQL statements wired as tasks (streams for CDC "
                     "inputs; dynamic tables for pure derivations)",
        "procedural": "Snowflake Scripting procedure (BEGIN...END port)",
        "dynamic": "Snowflake Scripting with IDENTIFIER()/EXECUTE "
                   "IMMEDIATE — review the constructed SQL",
    },
}


def decompose_procedure(unit: dict) -> dict:
    """unit: an entry of pipeline.metadata['procedural_units'] — dict with
    object_type/object_name/file/line/dialect/sql."""
    sql = unit["sql"]
    dialect = unit["dialect"]
    header_end = re.search(r"\b(IS|AS|BEGIN)\b", sql, re.IGNORECASE)
    header = sql[:header_end.start()] if header_end else sql[:200]
    body = sql[header_end.start():] if header_end else sql

    variables: List[dict] = []
    if dialect == "tsql":
        variables = [{"name": m.group(1), "datatype": m.group(2)}
                     for m in _VAR_TSQL.finditer(body)]
    else:
        mo_begin = re.search(r"\bBEGIN\b", body, re.IGNORECASE)
        decl_zone = body[:mo_begin.start()] if mo_begin else ""
        variables = [{"name": m.group(1), "datatype": m.group(2)}
                     for m in _VAR_ORACLE.finditer(decl_zone)
                     if m.group(1).upper() not in ("BEGIN", "END")]

    statements = []
    counts: Dict[str, int] = {}
    for stmt in _split_statements(body):
        # skip pure block tokens
        if re.fullmatch(r"(BEGIN|END\s*\w*|IS|AS)\s*;?", stmt.strip(),
                        re.IGNORECASE):
            continue
        cls = _classify(stmt)
        counts[cls] = counts.get(cls, 0) + 1
        statements.append({"classification": cls, "sql": stmt[:1500]})

    detections = {
        "cursor_loops": bool(_CURSOR.search(body)),
        "bulk_collect": bool(_BULK.search(body)),
        "autonomous_transaction": bool(_AUTONOMOUS.search(body)),
        "dynamic_sql": counts.get("DYNAMIC_SQL", 0) > 0,
        "temp_tables": sorted({g for m in _TEMP_REF.finditer(body)
                               for g in m.groups() if g}),
    }

    transform = counts.get("DATA_TRANSFORMATION", 0) + \
        counts.get("DATA_LOAD", 0)
    control = counts.get("CONTROL_FLOW", 0)
    if detections["dynamic_sql"]:
        shape = "dynamic"
    elif transform and control <= max(2, transform):
        shape = "set_based"
    else:
        shape = "procedural"

    return {
        "object_type": unit["object_type"],
        "object_name": unit["object_name"],
        "file": unit["file"], "line": unit["line"],
        "dialect": dialect,
        "parameters": _parameters(header, dialect),
        "variables": variables,
        "statements": statements,
        "statement_counts": counts,
        "detections": detections,
        "shape": shape,
        "recommendations": {t: recs[shape] for t, recs in
                            _TARGET_RECOMMENDATIONS.items()},
    }

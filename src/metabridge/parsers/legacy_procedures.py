"""Stored-procedure decomposition (Phase 3, section 12).

Procedures are never blindly translated. Each procedural block is
decomposed into:

    parameters (direction + type), variables, cursor loops, temp tables,
    statements — each classified:

    DATA_TRANSFORMATION  INSERT/UPDATE/DELETE/MERGE/CTAS with real logic
    DATA_LOAD            plain INSERT ... VALUES / COPY-style loads
    DECLARATION          variable/cursor declarations (the zone before the
                         body's first BEGIN) — already reported as
                         `variables`, so counting them as unrecognized
                         statements only made every procedure look riskier
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

# IF blocks, counted per statement so a converted statement can say whether it
# ran conditionally. `END IF` has to be removed before the opens are counted or
# it counts as one of them; ELSE/ELSIF continue a block rather than opening or
# closing one, and `\bIF\b` does not match inside ELSIF.
_IF_OPEN = re.compile(r"\bIF\b", re.IGNORECASE)
_IF_CLOSE = re.compile(r"\bEND\s+IF\b", re.IGNORECASE)

# A load that CLEARS its target before inserting is a full refresh, whatever
# the INSERT alone looks like. Oracle writes the clear as TRUNCATE (often via
# EXECUTE IMMEDIATE, since TRUNCATE is DDL) or as an unfiltered DELETE.
_CLEARS_TARGET = re.compile(
    r"\b(?:TRUNCATE\s+TABLE|DELETE\s+FROM)\s+"
    r"([A-Za-z_][\w$#]*(?:\s*\.\s*[A-Za-z_][\w$#]*)*)", re.IGNORECASE)

# Per-statement cap. This is not a display limit: a DATA_TRANSFORMATION
# statement is re-parsed from this text to become a mapping, so anything cut
# here fails to parse and the transformation is lost. The unit body is already
# capped at the same figure by the caller, so this costs no extra memory — at
# 1500 a real ETL statement (long column list + joins) fell off the end.
_MAX_STATEMENT = 20000


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


_BEGIN_PREFIX = re.compile(r"^\s*BEGIN\b(?!\s+(TRY|CATCH|TRAN))\s*",
                           re.IGNORECASE)
# A leading comment is not a statement, but every classifier here anchors on
# `^\s*` — so `-- Load transformed data\nINSERT INTO dim SELECT ...` matched
# nothing and became MANUAL_REVIEW. Commented PL/SQL is the norm, not the
# exception, so this silently dropped whole procedures' worth of logic.
_LEADING_COMMENT = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)+",
                              re.DOTALL)


def strip_block_prefix(stmt: str) -> str:
    """Drop the bare BEGIN that opens a block and therefore prefixes its first
    statement (BEGIN TRY/CATCH/TRAN are statements in their own right and are
    kept).

    The text is stripped for the RECORD, not just for classification: a
    DATA_TRANSFORMATION statement is re-parsed from what is stored here to
    become a mapping, and `BEGIN MERGE INTO ...` parses as neither.
    """
    while True:
        m = _BEGIN_PREFIX.match(stmt)
        if not m or not stmt[m.end():].strip():
            return stmt
        stmt = stmt[m.end():]


_BRANCH_PREFIX = re.compile(
    r"^\s*(?:ELSIF\b.*?\bTHEN\b|ELSE\s+IF\b.*?\bTHEN\b|IF\b.*?\bTHEN\b|ELSE\b)"
    r"\s*", re.IGNORECASE | re.DOTALL)


def strip_branch_prefix(stmt: str) -> Tuple[str, bool]:
    """-> (the statement without its IF/ELSE prefix, whether one was there).

    `IF p_mode = 'FULL' THEN INSERT INTO t SELECT ...;` is ONE statement to a
    semicolon split, and it starts with IF — so the INSERT that is the whole
    point of it classified as CONTROL_FLOW and was never converted. Same for
    the `ELSE ... MERGE INTO t ...` on the other side of the branch.

    The flag matters as much as the text: the statement ran CONDITIONALLY, and
    a model built from it does not. The caller records that so the condition
    is declared rather than quietly dropped.
    """
    m = _BRANCH_PREFIX.match(stmt)
    if not m:
        return stmt, False
    rest = stmt[m.end():]
    # Strip ONLY when a data statement is hiding behind the keyword. Otherwise
    # the prefix IS the substance — `IF v_cnt = 0 THEN NULL;` is control flow
    # and reducing it to `NULL;` would trade a classified statement for an
    # unrecognized one.
    if not _DML.match(strip_leading_comments(rest)):
        return stmt, False
    return rest, True


def strip_leading_comments(stmt: str) -> str:
    """The statement with any comment block in front of it removed.

    Used for CLASSIFICATION only — the comment stays in the recorded text,
    because "-- Optional: clear existing data" is exactly the context a
    reviewer needs, and sqlglot parses a leading comment without help.
    """
    while True:
        m = _LEADING_COMMENT.match(stmt)
        if not m or not stmt[m.end():].strip():
            return stmt.strip()
        stmt = stmt[m.end():]


def _classify(stmt: str) -> str:
    stmt = strip_leading_comments(strip_block_prefix(
        strip_leading_comments(stmt)))
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


def tables_cleared(statements: List[dict]) -> Dict[str, str]:
    """{table (lower, unqualified): how it was cleared} for every table the
    body empties outright.

    A procedure that TRUNCATEs (or DELETEs unfiltered) and then INSERTs is a
    FULL REFRESH. Read the INSERT on its own and it is an append — which is
    what the generated model became, so a second run duplicated every row of
    the table the procedure had been REPLACING. The clear is usually not even
    adjacent to the INSERT: Oracle writes it as EXECUTE IMMEDIATE (TRUNCATE is
    DDL), so it lands in a different statement, and often in a different
    branch of an IF.
    """
    out: Dict[str, str] = {}
    for st in statements:
        sql = str(st.get("sql", ""))
        # EXECUTE IMMEDIATE 'TRUNCATE TABLE x' hides the clear in a literal
        for quoted in re.findall(r"'([^']*)'", sql):
            sql += "\n" + quoted
        for m in _CLEARS_TARGET.finditer(sql):
            kind = m.group(0).split()[0].upper()
            if kind == "DELETE":
                # a filtered delete removes rows, it does not clear the table
                tail = sql[m.end():m.end() + 400]
                if re.search(r"\bWHERE\b", tail, re.IGNORECASE):
                    continue
            table = re.sub(r"\s+", "", m.group(1)).split(".")[-1].lower()
            out.setdefault(table, kind)
    return out


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
    if header_end is None:
        header, body = sql[:200], sql
    else:
        header = sql[:header_end.start()]
        # IS/AS ENDS the header; BEGIN opens the body and belongs to it.
        # Leaving the IS glued to what follows is what made a procedure with
        # no declarations (`... IS BEGIN INSERT INTO ...`) classify its FIRST
        # statement — frequently the whole transformation — as MANUAL_REVIEW,
        # because "IS BEGIN INSERT ..." matches no classifier.
        body = sql[header_end.start():] \
            if header_end.group(1).upper() == "BEGIN" else sql[header_end.end():]

    mo_begin = re.search(r"\bBEGIN\b", body, re.IGNORECASE)
    variables: List[dict] = []
    if dialect == "tsql":
        # T-SQL declares inside the executable body, so it has no declaration
        # zone to separate out
        decl_end = 0
        variables = [{"name": m.group(1), "datatype": m.group(2)}
                     for m in _VAR_TSQL.finditer(body)]
    else:
        decl_end = mo_begin.start() if mo_begin else 0
        variables = [{"name": m.group(1), "datatype": m.group(2)}
                     for m in _VAR_ORACLE.finditer(body[:decl_end])
                     if m.group(1).upper() not in ("BEGIN", "END")]

    statements: List[dict] = []
    counts: Dict[str, int] = {}

    branch_depth = [0]           # open IF/CASE blocks, in statement order

    def collect(text: str, forced: str = "") -> None:
        for raw in _split_statements(text):
            # Depth is tracked on the ORIGINAL text and BEFORE the block-token
            # skip below: `END IF;` is filtered out as a pure token, and
            # letting it be skipped before it was counted left the block open
            # forever — every statement after the first IF then reported as
            # conditional, including the ones past END IF.
            depth_here = branch_depth[0]
            closes = len(_IF_CLOSE.findall(raw))
            opens = len(_IF_OPEN.findall(_IF_CLOSE.sub(" ", raw)))
            branch_depth[0] = max(0, depth_here + opens - closes)
            # skip pure block tokens
            if re.fullmatch(r"(BEGIN|END\s*\w*|IS|AS)\s*;?", raw.strip(),
                            re.IGNORECASE):
                continue
            stmt, branched = strip_branch_prefix(strip_block_prefix(raw))
            cls = forced or _classify(stmt)
            counts[cls] = counts.get(cls, 0) + 1
            entry = {"classification": cls, "sql": stmt[:_MAX_STATEMENT]}
            if len(stmt) > _MAX_STATEMENT:
                entry["truncated"] = True
            # A statement inside IF/ELSE ran CONDITIONALLY. Converting it to a
            # model makes it unconditional, and the condition is nowhere in the
            # output — so the fact has to travel with the statement.
            if branched or depth_here > 0:
                entry["in_branch"] = True
            statements.append(entry)

    collect(body[:decl_end], "DECLARATION")
    collect(body[decl_end:])

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

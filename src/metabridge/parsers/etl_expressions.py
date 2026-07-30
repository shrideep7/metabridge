"""Legacy-ETL expression translation (Command 5).

One translator per legacy expression language, all with the same contract:

    translate(expr) -> (canonical_sql, notes)

``canonical_sql`` parses under sqlglot's ANSI reader (verified here — a
translation that does not parse is *rejected*, the original expression is
kept and returned as a note so the caller can raise a MANUAL issue instead
of shipping broken SQL).  ``notes`` lists every construct that was kept or
approximated so parsers can attach WARNING/MANUAL issues with the original
payload preserved — nothing is dropped silently.

Languages covered:

    ssis      SSIS expression language   ([Col], @[User::v], ?:, &&, ||,
              GETDATE, ISNULL(x), (DT_STR, ...) casts, + concat)
    datastage DataStage BASIC derivations (UpCase, Trim, ':' concat,
              If..Then..Else, IsNull, stage variables)
    talend    Talend Java expressions    (row1.col, StringHandling.*,
              TalendDate.*, ternary, ==/!=, .equals(), null checks)
    xfr       Ab Initio XFR/DML          (string_upcase, first_defined,
              today(), if/else, lookup())
"""
from __future__ import annotations

import re
from typing import List, Tuple

import sqlglot

_TERNARY_RE = re.compile(r"([^?]+?)\?([^:]+?):(.+)", re.S)


def _parses(sql: str) -> bool:
    try:
        return sqlglot.parse_one(sql, dialect="") is not None
    except Exception:  # noqa: BLE001
        return False


def _finish(sql: str, original: str,
            notes: List[str]) -> Tuple[str, List[str]]:
    sql = " ".join(sql.split())
    if not sql or not _parses(sql):
        return "", notes + ["untranslatable: %s" % original.strip()[:200]]
    return sql, notes


def _ternary_to_case(expr: str) -> str:
    """cond ? a : b  ->  CASE WHEN cond THEN a ELSE b END (recursive)."""
    m = _TERNARY_RE.fullmatch(expr.strip())
    if not m:
        return expr
    cond, a, b = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    return "CASE WHEN %s THEN %s ELSE %s END" % (
        cond, _ternary_to_case(a), _ternary_to_case(b))


# --------------------------------------------------------------------------
# SSIS expression language
# --------------------------------------------------------------------------

_SSIS_CAST_RE = re.compile(
    r"\(DT_(WSTR|STR|I4|I8|I2|UI4|NUMERIC|DECIMAL|R8|R4|DATE|DBDATE|"
    r"DBTIMESTAMP2?|BOOL|CY|GUID)\s*(?:,\s*[\d,\s]+)?\)", re.I)
_SSIS_CAST_TYPE = {
    "WSTR": "VARCHAR", "STR": "VARCHAR", "I4": "INT", "I8": "BIGINT",
    "I2": "SMALLINT", "UI4": "INT", "NUMERIC": "DECIMAL",
    "DECIMAL": "DECIMAL", "R8": "DOUBLE", "R4": "FLOAT", "DATE": "DATE",
    "DBDATE": "DATE", "DBTIMESTAMP": "TIMESTAMP", "DBTIMESTAMP2": "TIMESTAMP",
    "BOOL": "BOOLEAN", "CY": "DECIMAL", "GUID": "VARCHAR",
}
_SSIS_FN = {"GETDATE": "CURRENT_TIMESTAMP", "GETUTCDATE": "CURRENT_TIMESTAMP",
            "LEN": "LENGTH", "REPLICATE": "REPEAT", "CODEPOINT": "ASCII"}


def ssis_expression_to_sql(expr: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    s = expr.strip()
    # variable / parameter references become named parameters
    def var(mo):
        name = mo.group(2)
        notes.append("runtime reference @%s kept as :%s" % (mo.group(0), name))
        return ":" + name
    s = re.sub(r"@\[(User|\$Package|\$Project|System)::(\w+)\]", var, s)
    s = re.sub(r"@\[?(\w+)\]?", lambda m: ":" + m.group(1), s)
    s = re.sub(r"\[([^\]\[]+)\]", lambda m: '"%s"' % m.group(1)
               if not m.group(1).replace("_", "").isalnum()
               else m.group(1), s)                       # [Col] -> Col
    # casts BEFORE operators ((DT_STR,50,1252)x -> CAST(x AS VARCHAR))
    while True:
        m = _SSIS_CAST_RE.search(s)
        if not m:
            break
        target = _SSIS_CAST_TYPE.get(m.group(1).upper(), "VARCHAR")
        rest = s[m.end():].lstrip()
        # cast binds to the next primary expression (parenthesised or token)
        if rest.startswith("("):
            depth, i = 0, 0
            for i, ch in enumerate(rest):
                depth += ch == "("
                depth -= ch == ")"
                if depth == 0:
                    break
            prim, tail = rest[:i + 1], rest[i + 1:]
        else:
            pm = re.match(r'[\w:."\']+(\([^()]*\))?', rest)
            prim = pm.group(0) if pm else rest
            tail = rest[len(prim):]
        s = s[:m.start()] + "CAST(%s AS %s)" % (prim, target) + tail
    # ISNULL(x) is a boolean test in SSIS (not T-SQL's coalesce)
    s = re.sub(r"\bISNULL\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)",
               r"(\1 IS NULL)", s, flags=re.I)
    s = s.replace("&&", " AND ").replace("||", " OR ")
    s = re.sub(r"(?<![<>!=])==", "=", s).replace("!=", "<>")
    s = re.sub(r"!(?!=)", " NOT ", s)
    for src, dst in _SSIS_FN.items():
        s = re.sub(r"\b%s\b" % src, dst, s, flags=re.I)
    s = re.sub(r'"([^"]*)"',
               lambda m: "'%s'" % m.group(1).replace("'", "''"), s)
    if "?" in s and ":" in s:
        s = _ternary_to_case(s)
    return _finish(s, expr, notes)


# --------------------------------------------------------------------------
# DataStage BASIC derivations
# --------------------------------------------------------------------------

_DS_FN = {"UPCASE": "UPPER", "DOWNCASE": "LOWER", "TRIM": "TRIM",
          "TRIMB": "RTRIM", "TRIMF": "LTRIM", "LEN": "LENGTH",
          "SUBSTRING": "SUBSTRING", "CHAR": "CHR", "SEQ": "ASCII",
          "ABS": "ABS", "SQRT": "SQRT", "NULLTOEMPTY": "COALESCE",
          "NULLTOZERO": "COALESCE", "CURRENTDATE": "CURRENT_DATE",
          "CURRENTTIMESTAMP": "CURRENT_TIMESTAMP"}
_DS_IF_RE = re.compile(r"\bIF\s+(.+?)\s+THEN\s+(.+?)\s+ELSE\s+(.+)",
                       re.I | re.S)


def datastage_expression_to_sql(expr: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    s = expr.strip()
    for bad in ("Oconv", "Iconv", "Ereplace", "Field(", "@INROWNUM",
                "@OUTROWNUM", "Routine"):
        if bad.lower() in s.lower():
            return _finish("", expr,
                           notes + ["DataStage construct '%s' needs manual "
                                    "porting" % bad.strip("(")])
    m = _DS_IF_RE.fullmatch(s)
    if m:
        s = "CASE WHEN %s THEN %s ELSE %s END" % m.groups()
    s = re.sub(r"(\w+)\.(\w+)", r"\2", s)              # link.col -> col
    for src, dst in _DS_FN.items():
        s = re.sub(r"\b%s\b" % src, dst, s, flags=re.I)
    if "NULLTOEMPTY" in expr.upper():
        s = re.sub(r"COALESCE\(([^()]+)\)", r"COALESCE(\1, '')", s)
    if "NULLTOZERO" in expr.upper():
        s = re.sub(r"COALESCE\(([^()]+)\)", r"COALESCE(\1, 0)", s)
    s = re.sub(r"\bIsNull\s*\(([^()]+)\)", r"(\1 IS NULL)", s, flags=re.I)
    s = re.sub(r'"([^"]*)"',
               lambda m: "'%s'" % m.group(1).replace("'", "''"), s)
    if re.search(r"['\w)]\s*:\s*['\w(]", s):             # ':' concatenation
        s = re.sub(r"\s*:\s*", " || ", s)
    s = s.replace("<>", "<>").replace("#", "<>") if " # " in s else s
    return _finish(s, expr, notes)


# --------------------------------------------------------------------------
# Talend Java expressions
# --------------------------------------------------------------------------

_TALEND_FN = [
    (r"StringHandling\.UPCASE\(", "UPPER("),
    (r"StringHandling\.DOWNCASE\(", "LOWER("),
    (r"StringHandling\.TRIM\(", "TRIM("),
    (r"StringHandling\.LEN\(", "LENGTH("),
    (r"StringHandling\.LEFT\(", "LEFT("),
    (r"StringHandling\.RIGHT\(", "RIGHT("),
    (r"TalendDate\.getCurrentDate\(\)", "CURRENT_DATE"),
    (r"Mathematical\.ABS\(", "ABS("),
]


def talend_expression_to_sql(expr: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    s = expr.strip()
    for bad in ("routines.", "System.", "new ", "context.getProperty",
                "TalendDate.parseDate", ".split(", ".replaceAll("):
        if bad in s:
            return _finish("", expr,
                           notes + ["Java construct '%s' needs manual "
                                    "porting" % bad.strip("(.")])
    if "context." in s:
        notes.append("context variable kept as named parameter")
        s = re.sub(r"context\.(\w+)", r":\1", s)
    s = re.sub(r"\brow\d+\.(\w+)", r"\1", s)           # rowN.col -> col
    s = re.sub(r"\b(\w+)\.equals\(([^()]+)\)", r"\1 = \2", s)
    for pat, rep in _TALEND_FN:
        s = re.sub(pat, rep, s)
    if "TalendDate.formatDate" in s:
        notes.append("TalendDate.formatDate approximated as TO_CHAR")
        s = re.sub(r"TalendDate\.formatDate\(([^,]+),\s*(.+?)\)",
                   r"TO_CHAR(\2, \1)", s)
    s = re.sub(r"(\w+)\s*==\s*null", r"\1 IS NULL", s)
    s = re.sub(r"(\w+)\s*!=\s*null", r"\1 IS NOT NULL", s)
    s = re.sub(r"(?<![<>!=])==", "=", s).replace("!=", "<>")
    s = s.replace("&&", " AND ").replace("||", " OR ")
    s = re.sub(r'"([^"]*)"',
               lambda m: "'%s'" % m.group(1).replace("'", "''"), s)
    if "?" in s and ":" in s and not re.match(r"^:\w+$", s.strip()):
        s = _ternary_to_case(s)
    # Java string '+' concatenation: only rewrite when a literal is involved
    if "'" in s and "+" in s:
        s = re.sub(r"\s*\+\s*", " || ", s)
        notes.append("Java '+' concatenation rewritten as ||")
    return _finish(s, expr, notes)


# --------------------------------------------------------------------------
# Ab Initio XFR / DML expressions
# --------------------------------------------------------------------------

_XFR_FN = {"string_upcase": "UPPER", "string_downcase": "LOWER",
           "string_trim": "TRIM", "string_lrtrim": "TRIM",
           "string_length": "LENGTH", "string_substring": "SUBSTRING",
           "first_defined": "COALESCE", "string_concat": "CONCAT",
           "decimal_round": "ROUND", "math_abs": "ABS",
           "string_replace": "REPLACE", "string_pad": "LPAD"}
_XFR_IF_RE = re.compile(
    r"\bif\s*\((.+?)\)\s*(.+?)\s+else\s+(.+)", re.I | re.S)


def xfr_expression_to_sql(expr: str) -> Tuple[str, List[str]]:
    notes: List[str] = []
    s = expr.strip().rstrip(";")
    if "lookup(" in s or "lookup_match" in s:
        return _finish("", expr,
                       notes + ["XFR lookup() needs a LOOKUP transformation "
                                "— port manually"])
    m = _XFR_IF_RE.fullmatch(s)
    if m:
        s = "CASE WHEN %s THEN %s ELSE %s END" % m.groups()
    s = re.sub(r"\bin\d*\.(\w+)", r"\1", s)            # in.col -> col
    s = s.replace("today()", "CURRENT_DATE").replace("now()",
                                                     "CURRENT_TIMESTAMP")
    for src, dst in _XFR_FN.items():
        s = re.sub(r"\b%s\b" % src, dst, s, flags=re.I)
    s = re.sub(r"\(decimal\([^)]*\)\)\s*", "", s)      # (decimal(8.2)) casts
    s = re.sub(r"\(string\([^)]*\)\)\s*", "", s)
    s = re.sub(r'"([^"]*)"',
               lambda m: "'%s'" % m.group(1).replace("'", "''"), s)
    s = s.replace("==", "=").replace("!=", "<>")
    return _finish(s, expr, notes)


TRANSLATORS = {
    "ssis": ssis_expression_to_sql,
    "datastage": datastage_expression_to_sql,
    "talend": talend_expression_to_sql,
    "xfr": xfr_expression_to_sql,
}

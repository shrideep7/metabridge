"""Function-mapping registry: Informatica expression language ⇄ ANSI SQL.

This registry drives both the transpiler and the "supported functions" section
of the coverage report. Keep it data, not code, so the supported surface is
auditable by customers.
"""
from __future__ import annotations

# Informatica function name -> how it maps to SQL.
# "sql" is the ANSI SQL function when it is a plain 1:1 rename (or same name).
# Structural rewrites (IIF, DECODE, ISNULL...) are handled in expressions.py
# and marked here as "structural" for reporting purposes.
INFA_FUNCTIONS = {
    # --- conditional / null handling (structural rewrites) ---
    "IIF": {"sql": "CASE WHEN", "kind": "structural"},
    "DECODE": {"sql": "CASE", "kind": "structural"},
    "ISNULL": {"sql": "IS NULL", "kind": "structural"},
    "IN": {"sql": "IN", "kind": "structural"},
    # --- string ---
    "SUBSTR": {"sql": "SUBSTRING", "kind": "rename"},
    "INSTR": {"sql": "POSITION", "kind": "structural"},
    "LENGTH": {"sql": "LENGTH", "kind": "same"},
    "UPPER": {"sql": "UPPER", "kind": "same"},
    "LOWER": {"sql": "LOWER", "kind": "same"},
    "INITCAP": {"sql": "INITCAP", "kind": "same"},
    "LTRIM": {"sql": "LTRIM", "kind": "same"},
    "RTRIM": {"sql": "RTRIM", "kind": "same"},
    "LPAD": {"sql": "LPAD", "kind": "same"},
    "RPAD": {"sql": "RPAD", "kind": "same"},
    "CONCAT": {"sql": "CONCAT", "kind": "same"},
    "REPLACESTR": {"sql": "REPLACE", "kind": "structural"},
    "REPLACECHR": {"sql": "REPLACE", "kind": "structural"},
    "REG_MATCH": {"sql": "REGEXP_LIKE", "kind": "rename"},
    "REG_EXTRACT": {"sql": "REGEXP_SUBSTR", "kind": "rename"},
    "REG_REPLACE": {"sql": "REGEXP_REPLACE", "kind": "rename"},
    "CHR": {"sql": "CHR", "kind": "same"},
    "ASCII": {"sql": "ASCII", "kind": "same"},
    "REVERSE": {"sql": "REVERSE", "kind": "same"},
    # --- numeric ---
    "ABS": {"sql": "ABS", "kind": "same"},
    "ROUND": {"sql": "ROUND", "kind": "same"},
    "TRUNC": {"sql": "TRUNC", "kind": "same"},  # numeric or date TRUNC
    "FLOOR": {"sql": "FLOOR", "kind": "same"},
    "CEIL": {"sql": "CEIL", "kind": "same"},
    "MOD": {"sql": "MOD", "kind": "same"},
    "POWER": {"sql": "POWER", "kind": "same"},
    "SQRT": {"sql": "SQRT", "kind": "same"},
    "EXP": {"sql": "EXP", "kind": "same"},
    "LN": {"sql": "LN", "kind": "same"},
    "LOG": {"sql": "LOG", "kind": "same"},
    "SIGN": {"sql": "SIGN", "kind": "same"},
    # --- conversion ---
    "TO_CHAR": {"sql": "CAST/TO_CHAR", "kind": "structural"},
    "TO_DATE": {"sql": "TO_DATE", "kind": "same"},
    "TO_DECIMAL": {"sql": "CAST AS DECIMAL", "kind": "structural"},
    "TO_INTEGER": {"sql": "CAST AS INTEGER", "kind": "structural"},
    "TO_FLOAT": {"sql": "CAST AS DOUBLE", "kind": "structural"},
    "TO_BIGINT": {"sql": "CAST AS BIGINT", "kind": "structural"},
    # --- date ---
    "SYSDATE": {"sql": "CURRENT_TIMESTAMP", "kind": "rename"},
    "SESSSTARTTIME": {"sql": "CURRENT_TIMESTAMP", "kind": "rename"},
    "SYSTIMESTAMP": {"sql": "CURRENT_TIMESTAMP", "kind": "rename"},
    "ADD_TO_DATE": {"sql": "DATEADD", "kind": "structural"},
    "DATE_DIFF": {"sql": "DATEDIFF", "kind": "structural"},
    "GET_DATE_PART": {"sql": "EXTRACT", "kind": "structural"},
    "SET_DATE_PART": {"sql": "", "kind": "unsupported"},
    "LAST_DAY": {"sql": "LAST_DAY", "kind": "same"},
    "DATE_COMPARE": {"sql": "CASE WHEN", "kind": "structural"},
    "IS_DATE": {"sql": "", "kind": "unsupported"},
    # --- aggregate (valid inside Aggregator transformations) ---
    "SUM": {"sql": "SUM", "kind": "same"},
    "AVG": {"sql": "AVG", "kind": "same"},
    "MIN": {"sql": "MIN", "kind": "same"},
    "MAX": {"sql": "MAX", "kind": "same"},
    "COUNT": {"sql": "COUNT", "kind": "same"},
    "MEDIAN": {"sql": "MEDIAN", "kind": "same"},
    "STDDEV": {"sql": "STDDEV", "kind": "same"},
    "VARIANCE": {"sql": "VARIANCE", "kind": "same"},
    "FIRST": {"sql": "", "kind": "unsupported"},
    "LAST": {"sql": "", "kind": "unsupported"},
    # --- misc ---
    "MD5": {"sql": "MD5", "kind": "same"},
    "CRC32": {"sql": "", "kind": "unsupported"},
    "UUID4": {"sql": "UUID_STRING", "kind": "rename"},
    "GREATEST": {"sql": "GREATEST", "kind": "same"},
    "LEAST": {"sql": "LEAST", "kind": "same"},
    "NVL": {"sql": "COALESCE", "kind": "rename"},  # seen in IDMC / advanced mode
}

# Date-part tokens: Informatica format code <-> SQL date part keyword.
INFA_DATEPART_TO_SQL = {
    "YYYY": "year", "YYY": "year", "YY": "year", "Y": "year",
    "MM": "month", "MON": "month", "MONTH": "month",
    "DD": "day", "DDD": "day", "DAY": "day", "D": "day",
    "HH": "hour", "HH12": "hour", "HH24": "hour",
    "MI": "minute",
    "SS": "second",
    "MS": "millisecond", "US": "microsecond", "NS": "nanosecond",
}

SQL_DATEPART_TO_INFA = {
    "year": "YYYY", "quarter": "MM", "month": "MM", "week": "DD",
    "day": "DD", "hour": "HH24", "minute": "MI", "second": "SS",
    "millisecond": "MS", "microsecond": "US",
}


def supported_infa_functions() -> list:
    return sorted(k for k, v in INFA_FUNCTIONS.items() if v["kind"] != "unsupported")


def unsupported_infa_functions() -> list:
    return sorted(k for k, v in INFA_FUNCTIONS.items() if v["kind"] == "unsupported")

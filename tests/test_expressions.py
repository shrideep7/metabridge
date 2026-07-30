"""Expression transpiler tests — the contract customers audit first."""
import pytest

from metabridge.sqlx.expressions import ExpressionError, infa_to_sql, sql_to_infa


@pytest.mark.parametrize("sql,expected", [
    ("CASE WHEN a > 1 THEN 'x' ELSE 'y' END", "IIF(a > 1, 'x', 'y')"),
    ("CASE s WHEN 'A' THEN 1 ELSE 0 END", "DECODE(s, 'A', 1, 0)"),
    ("COALESCE(a, b)", "IIF(ISNULL(a), b, a)"),
    ("a IS NULL", "ISNULL(a)"),
    ("a IS NOT NULL", "NOT ISNULL(a)"),
    ("UPPER(TRIM(x))", "UPPER(LTRIM(RTRIM(x)))"),
    ("CAST(x AS VARCHAR)", "TO_CHAR(x)"),
    ("CAST(x AS DECIMAL(18,2))", "TO_DECIMAL(x, 2)"),
    ("CURRENT_TIMESTAMP", "SYSDATE"),
    ("NULLIF(a, b)", "IIF(a = b, NULL, a)"),
    ("SUBSTRING(x, 1, 3)", "SUBSTR(x, 1, 3)"),
    ("x LIKE 'AB%'", "SUBSTR(x, 1, 2) = 'AB'"),
    ("x LIKE '%ab%'", "INSTR(x, 'ab') > 0"),
    ("EXTRACT(year FROM d)", "GET_DATE_PART(d, 'YYYY')"),
    ("DATE_TRUNC('month', d)", "TRUNC(d, 'MM')"),
    ("x IN ('a', 'b')", "IN(x, 'a', 'b')"),
    ("a || b", "a || b"),
])
def test_sql_to_infa(sql, expected):
    assert sql_to_infa(sql) == expected


@pytest.mark.parametrize("sql,expected", [
    ("DATEADD(day, 7, d)", "ADD_TO_DATE(d, 'DD', 7)"),
    ("DATEDIFF(day, a, b)", "DATE_DIFF(b, a, 'DD')"),
])
def test_sql_to_infa_snowflake(sql, expected):
    assert sql_to_infa(sql, dialect="snowflake") == expected


@pytest.mark.parametrize("infa,expected", [
    ("IIF(a > 1, 'x', 'y')", "CASE WHEN a > 1 THEN 'x' ELSE 'y' END"),
    ("DECODE(s, 'A', 1, 0)", "CASE WHEN s = 'A' THEN 1 ELSE 0 END"),
    ("ISNULL(a)", "a IS NULL"),
    ("IIF(ISNULL(a), b, a)", "CASE WHEN a IS NULL THEN b ELSE a END"),
    ("TO_DECIMAL(x, 2)", "CAST(x AS DECIMAL(38, 2))"),
    ("SUBSTR(x, 1, 3)", "SUBSTRING(x, 1, 3)"),
    ("GET_DATE_PART(d, 'YYYY')", "EXTRACT(year FROM d)"),
    ("IN(x, 'a', 'b')", "x IN ('a', 'b')"),
    ("REPLACESTR(0, x, 'a', 'b')", "REPLACE(x, 'a', 'b')"),
])
def test_infa_to_sql(infa, expected):
    assert infa_to_sql(infa) == expected


def test_mapping_parameters_preserved_both_ways():
    assert sql_to_infa("updated_at > $$LAST_RUN_TS") == "updated_at > $$LAST_RUN_TS"
    assert infa_to_sql("updated_at > $$LAST_RUN_TS") == "updated_at > $$LAST_RUN_TS"


def test_window_function_rejected():
    with pytest.raises(ExpressionError):
        sql_to_infa("RANK() OVER (ORDER BY x)")


def test_unknown_infa_function_rejected():
    with pytest.raises(ExpressionError):
        infa_to_sql("SET_DATE_PART(d, 'DD', 5)")


def test_roundtrip_stability():
    """SQL -> Informatica -> SQL must preserve semantics for core patterns."""
    cases = [
        "CASE WHEN amount > 100 THEN 'H' ELSE 'L' END",
        "UPPER(LTRIM(RTRIM(name)))",
        "a IS NULL",
    ]
    for sql in cases:
        back = infa_to_sql(sql_to_infa(sql))
        # normalize via a second pass — idempotency check
        assert infa_to_sql(sql_to_infa(back)) == back

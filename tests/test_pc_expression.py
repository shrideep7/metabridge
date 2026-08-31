"""Expression handler: dependency graph, variable inlining, stateful
detection, and the Informatica function registry pinned to the engine."""
import json
from pathlib import Path

import pytest

from metabridge.ir.model import Mapping, Port
from metabridge.parsers.pc_expression import (
    analyze_expression_ports, inline_variable_ports,
)
from metabridge.sqlx.expressions import infa_to_sql
from metabridge.sqlx.infa_registry import get_infa_function_registry
from conftest import model_sql

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _p(name, direction="OUTPUT", expression=""):
    return Port(name=name, direction=direction, expression=expression)


# ---------------------------------------------------------------------------
# spec examples convert semantically (AST, not string replacement)
# ---------------------------------------------------------------------------

def test_concat_operator_example():
    assert infa_to_sql("FIRST_NAME || ' ' || LAST_NAME") == \
        "FIRST_NAME || ' ' || LAST_NAME"


def test_iif_isnull_example():
    assert infa_to_sql("IIF(ISNULL(STATUS), 'UNKNOWN', STATUS)") == \
        "CASE WHEN STATUS IS NULL THEN 'UNKNOWN' ELSE STATUS END"


def test_to_char_format_is_preserved():
    """sqlglot's generic ToChar drops the format — ours must not."""
    assert infa_to_sql("TO_CHAR(d, 'YYYY')") == "TO_CHAR(d, 'YYYY')"


def test_crc32_supported():
    assert infa_to_sql("CRC32(s)") == "CRC32(s)"


# ---------------------------------------------------------------------------
# port classification + dependency graph
# ---------------------------------------------------------------------------

def test_port_classification_and_graph():
    ports = [
        _p("QTY", "INPUT"), _p("PRICE", "INPUT"),
        _p("DISCOUNT_PCT", "INPUT"),
        _p("VAR_TOTAL", "VARIABLE", "QTY * PRICE"),
        _p("VAR_DISCOUNT", "VARIABLE", "VAR_TOTAL * DISCOUNT_PCT"),
        _p("NET_AMOUNT", "OUTPUT", "VAR_TOTAL - VAR_DISCOUNT"),
    ]
    a = analyze_expression_ports(ports)
    assert a.inputs == ["QTY", "PRICE", "DISCOUNT_PCT"]
    assert a.variables == ["VAR_TOTAL", "VAR_DISCOUNT"]
    assert a.outputs == ["NET_AMOUNT"]
    assert a.dependency_graph["VAR_DISCOUNT"] == ["var_total",
                                                  "discount_pct"]
    # execution order preserved: variables top-to-bottom, then outputs
    assert a.execution_order == ["VAR_TOTAL", "VAR_DISCOUNT", "NET_AMOUNT"]
    assert a.stateful_variables == []


def test_spec_variable_chain_inlines_in_dependency_order():
    m = Mapping(name="t")
    ports = [
        _p("QTY", "INPUT"), _p("PRICE", "INPUT"),
        _p("DISCOUNT_PCT", "INPUT"),
        _p("VAR_TOTAL", "VARIABLE", "QTY * PRICE"),
        _p("VAR_DISCOUNT", "VARIABLE", "VAR_TOTAL * DISCOUNT_PCT"),
        _p("NET_AMOUNT", "OUTPUT", "VAR_TOTAL - VAR_DISCOUNT"),
    ]
    inline_variable_ports(m, "EXP", ports)
    net = ports[-1].expression
    assert net == "(QTY * PRICE) - ((QTY * PRICE) * DISCOUNT_PCT)"
    assert m.issues == []                       # pure chain, no flags


def test_stateful_self_reference_flagged_manual():
    m = Mapping(name="t")
    ports = [
        _p("AMT", "INPUT"),
        _p("V_RUNNING", "VARIABLE", "V_RUNNING + AMT"),   # running total
        _p("TOTAL_OUT", "OUTPUT", "V_RUNNING"),
    ]
    inline_variable_ports(m, "EXP", ports)
    codes = [i.code for i in m.issues]
    assert codes.count("STATEFUL_VARIABLE_PORT") == 2   # var + dependent out
    assert any("window function" in i.suggestion for i in m.issues)
    # the output was NOT silently rewritten
    assert ports[-1].expression == "V_RUNNING"


def test_forward_reference_is_stateful():
    """A variable referencing a LATER variable reads the previous row."""
    ports = [
        _p("A", "INPUT"),
        _p("V1", "VARIABLE", "V2 + A"),      # V2 declared later!
        _p("V2", "VARIABLE", "A * 2"),
        _p("OUT1", "OUTPUT", "V2"),
    ]
    a = analyze_expression_ports(ports)
    assert a.stateful_variables == ["V1"]
    m = Mapping(name="t")
    inline_variable_ports(m, "EXP", ports)
    # V2 is pure and still inlines
    assert ports[-1].expression == "(A * 2)"


# ---------------------------------------------------------------------------
# end-to-end through the parser and generator
# ---------------------------------------------------------------------------

def test_parser_inlines_variables_and_records_analysis():
    from metabridge.engine import parse_input
    p = parse_input(str(EXAMPLES / "powercenter_repo" / "repo_export.xml"),
                    "powercenter")
    exp = p.mapping("customer_enrich").transformation("EXP_CLEAN")
    clean = exp.port("name_clean")
    assert clean.expression == "UPPER((LTRIM(RTRIM(name))))"
    var = exp.port("v_name_trim")
    assert var.direction == "VARIABLE"          # normalized port type
    analysis = exp.properties["expression_analysis"]
    assert analysis["execution_order"][0] == "v_name_trim"
    json.dumps(analysis)


def test_generated_sql_never_references_variable_ports(tmp_path,
                                                       monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    convert(str(EXAMPLES / "powercenter_repo" / "repo_export.xml"),
            str(tmp_path / "out"), source_format="powercenter",
            target_format="dbt")
    sql = model_sql(tmp_path / "out" / "dbt", "customer_enrich")
    assert "v_name_trim" not in sql
    assert "UPPER((LTRIM(RTRIM(name))))" in sql


# ---------------------------------------------------------------------------
# the function registry — pinned to the engine, entry by entry
# ---------------------------------------------------------------------------

def test_registry_has_all_spec_functions():
    reg = get_infa_function_registry()
    for fn in ("IIF", "DECODE", "ISNULL", "NVL", "TO_DATE", "TO_CHAR",
               "TO_INTEGER", "TO_DECIMAL", "TO_FLOAT", "ADD_TO_DATE",
               "DATE_DIFF", "LAST_DAY", "TRUNC", "ROUND", "ABS", "CEIL",
               "FLOOR", "MOD", "POWER", "SQRT", "LENGTH", "LTRIM",
               "RTRIM", "TRIM", "LOWER", "UPPER", "INITCAP", "SUBSTR",
               "INSTR", "REPLACECHR", "REPLACESTR", "REG_REPLACE",
               "REG_MATCH", "CONCAT", "LPAD", "RPAD", "ASCII", "CHR",
               "MD5", "CRC32", "SOUNDEX", "METAPHONE"):
        assert reg.get(fn) is not None, fn


def test_registry_validates_clean():
    assert get_infa_function_registry().validate() == []


def test_every_supported_entry_is_pinned_to_the_engine():
    """No drift: the registry's expected_sql must be EXACTLY what the
    AST engine produces for the example — blind string replacement or a
    silent behavior change breaks this immediately."""
    reg = get_infa_function_registry()
    for name, row in reg.all().items():
        if not row.get("supported"):
            continue
        assert infa_to_sql(row["example"]) == row["expected_sql"], name


def test_semantic_not_textual_conversion():
    """Nested calls restructure the AST — impossible with string
    replacement."""
    out = infa_to_sql("IIF(ISNULL(NVL(a, b)), DECODE(x, 1, 'one', 'many'), "
                      "UPPER(y))")
    assert out.startswith("CASE WHEN COALESCE(a, b) IS NULL")
    assert "CASE WHEN x = 1 THEN 'one' ELSE 'many' END" in out


def test_coverage_counts():
    cov = get_infa_function_registry().coverage()
    assert cov["functions"] >= 42
    assert cov["supported"] >= 42
    assert cov["by_category"]["string"] >= 15

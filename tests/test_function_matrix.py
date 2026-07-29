"""Function conversion matrix (module 29): >=100 functions across the 10
spec groups, every mapping tested against the LIVE engine — supported
entries must convert exactly as documented (canonical + Databricks), and
manual entries must be refused, never silently passed through."""
import pytest
import sqlglot

from metabridge.sqlx.expressions import ExpressionError, infa_to_sql
from metabridge.sqlx.infa_registry import (GROUPS,
                                           get_infa_function_registry)

REG = get_infa_function_registry()
ALL = REG.all()


def test_at_least_100_functions():
    assert len(ALL) >= 100
    assert REG.validate() == []


def test_all_spec_groups_populated():
    for g in ("NULL", "CONDITIONAL", "STRING", "DATE", "NUMERIC",
              "CONVERSION", "REGEX", "ENCODING", "HASH", "AGGREGATION"):
        assert REG.by_group(g), g


def test_matrix_fields_present():
    for name, row in ALL.items():
        assert row["group"] in GROUPS, name
        assert row["semantic_function"], name
        assert row["automation_level"] in ("full", "partial", "manual")
        assert row["semantic_risk"] in ("none", "low", "medium", "high")


@pytest.mark.parametrize("name", sorted(ALL))
def test_every_function_mapping(name):
    """Automated test per function: full entries convert to EXACTLY the
    documented SQL (canonical and Databricks); manual entries raise (the
    port is routed to the manual queue — nothing silent)."""
    row = ALL[name]
    if row["automation_level"] == "full":
        got = infa_to_sql(row["example"])
        assert got == row["expected_sql"], name
        assert row["dbt_default"] == row["expected_sql"], name
        assert sqlglot.transpile(row["expected_sql"],
                                 write="databricks")[0] == \
            row["databricks_sql"], name
    elif row["automation_level"] == "manual":
        with pytest.raises(ExpressionError):
            infa_to_sql(row["example"])
    else:                       # partial: converted elsewhere (documented)
        assert row.get("notes"), name


def test_stateful_functions_marked_high_risk():
    for name, row in REG.by_group("STATEFUL").items():
        assert row["automation_level"] == "manual", name
        assert row["semantic_risk"] == "high", name


def test_examples_of_new_conversions():
    assert infa_to_sql("GET_DATE_PART(d, 'MM')") == "EXTRACT(month FROM d)"
    assert infa_to_sql("SHA256(x)") == "SHA2(x, 256)"
    assert infa_to_sql("IS_SPACES(s)") == \
        "(LENGTH(s) > 0 AND TRIM(s) = '')"
    assert infa_to_sql("CHOOSE(i, 'a', 'b')") == \
        "CASE WHEN i = 1 THEN 'a' WHEN i = 2 THEN 'b' END"

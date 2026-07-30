"""Automatic test generation: 11 test types, dbt tests, reconciliation SQL."""
import json
from pathlib import Path

import pytest

from metabridge.parsers.base import get_parser
from metabridge.report.testgen import (
    TEST_TYPES, _P, generate_tests, write_tests,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def retail():
    return get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))


@pytest.fixture(scope="module")
def doc(retail):
    return generate_tests(retail, source_platform="snowflake",
                          target_platform="databricks", target_format="dbt")


def _tests(doc, mapping, test_type=None):
    pm = next(p for p in doc["mappings"] if p["mapping"] == mapping)
    return [t for t in pm["tests"]
            if test_type is None or t["test_type"] == test_type]


# ---------------------------------------------------------------------------
# catalog contract
# ---------------------------------------------------------------------------

def test_all_twelve_types_in_summary(doc):
    assert set(doc["summary"]["by_type"]) == set(TEST_TYPES)
    assert len(TEST_TYPES) == 12


def test_every_mapping_gets_core_tests(doc):
    for pm in doc["mappings"]:
        types = {t["test_type"] for t in pm["tests"]}
        assert {"row_count", "null_comparison", "duplicate_comparison",
                "checksum_comparison", "column_level_comparison",
                "schema_comparison"} <= types


def test_serializable(doc):
    json.dumps(doc)


# ---------------------------------------------------------------------------
# evidence-derived tests
# ---------------------------------------------------------------------------

def test_pk_uniqueness_from_unique_key(doc):
    (t,) = _tests(doc, "stg_orders", "pk_uniqueness")
    assert t["key_columns"] == ["order_id"]
    assert "HAVING COUNT(*) > 1" in t["target_sql"]
    # stg_customers has no declared key -> no fabricated pk test
    assert not _tests(doc, "stg_customers", "pk_uniqueness")


def test_business_rule_from_filter(doc):
    rules = _tests(doc, "stg_customers", "business_rule_validation")
    not_null = [t for t in rules if t["rule"]["kind"] == "not_null"]
    assert any(t["rule"]["column"] == "email" for t in not_null)
    t = not_null[0]
    assert "email IS NULL" in t["target_sql"]
    assert t["expectation"] == "violations == 0"


def test_watermark_filter_is_not_a_business_rule(doc):
    rules = _tests(doc, "stg_orders", "business_rule_validation")
    assert not any("$$" in json.dumps(t) or "updated_at >" in
                   t.get("target_sql", "") for t in rules)


def test_case_literals_become_accepted_values(doc):
    rules = _tests(doc, "stg_customers", "business_rule_validation")
    av = next(t for t in rules if t["rule"]["kind"] == "accepted_values"
              and t["rule"]["column"] == "status_desc")
    assert set(av["rule"]["values"]) == {"ACTIVE", "INACTIVE", "UNKNOWN"}
    # dialects render the negation as either "x NOT IN" or "NOT x IN"
    assert "NOT" in av["target_sql"] and "IN (" in av["target_sql"]


def test_untestable_rule_is_recorded_not_faked(doc):
    """daily_revenue filters on order_status, which aggregation drops —
    no test against a nonexistent column, but the gap is visible."""
    assert not _tests(doc, "daily_revenue", "business_rule_validation")
    pm = next(p for p in doc["mappings"] if p["mapping"] == "daily_revenue")
    (u,) = pm["untestable_rules"]
    assert u["rule"]["column"] == "order_status"
    assert "cannot be validated" in u["reason"]
    assert doc["summary"]["untestable_rules"] >= 1


def test_referential_integrity_from_join(doc):
    (t,) = _tests(doc, "customer_orders", "referential_integrity")
    rel = t["relationship"]
    assert rel["child_table"] == "stg_orders"
    assert rel["parent_table"] == "stg_customers"
    assert rel["child_column"] == "customer_id"
    assert "LEFT JOIN" in t["target_sql"]
    assert "p.customer_id IS NULL" in t["target_sql"]
    assert rel["parent_evidence"]           # the choice is explained


def test_schema_comparison_expected_columns(doc):
    (t,) = _tests(doc, "stg_orders", "schema_comparison")
    cols = {c["column"]: c["canonical_type"] for c in t["expected_columns"]}
    assert cols["order_id"] == "integer"
    assert cols["amount"] == "decimal"
    assert "information_schema" in t["target_sql"].lower() or \
        "describe" in t["target_sql"].lower()


def test_aggregate_and_min_max_cover_numeric_columns(doc):
    (agg,) = _tests(doc, "stg_orders", "aggregate_comparison")
    assert "amount" in agg["columns"]
    assert "SUM(amount)" in agg["target_sql"]
    (mm,) = _tests(doc, "stg_orders", "min_max_comparison")
    assert "order_date" in mm["columns"]        # temporal columns included
    assert "MIN(order_date)" in mm["target_sql"]


# ---------------------------------------------------------------------------
# platform dialects + checksum honesty
# ---------------------------------------------------------------------------

def test_checksum_is_platform_specific_and_paired(doc):
    (t,) = _tests(doc, "stg_orders", "checksum_comparison")
    assert "MD5" in t["source_sql"] and "TO_NUMBER" in t["source_sql"]  # snowflake
    assert "CONV(" in t["target_sql"]                                   # databricks
    assert "native checksums" in t["native_note"]
    assert "HASH_AGG" in t["native_source_sql"]
    assert "XXHASH64" in t["native_target_sql"]


def test_teradata_md5_gap_is_declared(retail):
    d = generate_tests(retail, source_platform="teradata",
                       target_platform="snowflake")
    (t,) = _tests(d, "stg_orders", "checksum_comparison")
    assert t["source_sql"] is None
    assert any("no native MD5" in x for x in t["limitations"])


def test_oracle_uses_minus(retail):
    d = generate_tests(retail, source_platform="snowflake",
                       target_platform="oracle")
    (t,) = _tests(d, "stg_orders", "column_level_comparison")
    assert "MINUS" in t["target_sql"]


def test_platform_table_is_complete():
    for name, spec in _P.items():
        for key in ("concat", "cast", "except_op", "native", "md5num",
                    "schema_q"):
            assert key in spec, (name, key)


def test_unknown_platform_falls_back_to_ansi(retail):
    d = generate_tests(retail, source_platform="db2",
                       target_platform="databricks")
    assert d["source_platform"] == "ansi"


# ---------------------------------------------------------------------------
# dbt-native tests
# ---------------------------------------------------------------------------

def test_dbt_schema_yml(doc):
    yml = doc["dbt"]["schema_yml"]
    assert "version: 2" in yml
    # unique_key -> unique + not_null
    assert "- unique" in yml and "- not_null" in yml
    # CASE literals -> accepted_values
    assert "accepted_values" in yml and "ACTIVE" in yml
    # join -> relationships on the child model
    assert "relationships" in yml
    assert "ref('stg_customers')" in yml
    # untestable rule's model never appears with a wrong test
    assert "daily_revenue" not in yml


def test_dbt_block_only_for_dbt_target(retail):
    d = generate_tests(retail, "snowflake", "databricks",
                       target_format="databricks")
    assert "dbt" not in d


# ---------------------------------------------------------------------------
# reconciliation files
# ---------------------------------------------------------------------------

def test_write_tests(doc, tmp_path):
    root = Path(write_tests(doc, str(tmp_path)))
    assert (root / "tests.json").exists()
    assert (root / "README.md").exists()
    legacy = root / "reconciliation" / "stg_orders.legacy_snowflake.sql"
    migrated = root / "reconciliation" / "stg_orders.migrated_databricks.sql"
    checks = root / "reconciliation" / "stg_orders.checks_databricks.sql"
    assert legacy.exists() and migrated.exists() and checks.exists()
    # paired sections line up for diffing
    lsec = [ln for ln in legacy.read_text().splitlines()
            if ln.startswith("-- [")]
    msec = [ln for ln in migrated.read_text().splitlines()
            if ln.startswith("-- [")]
    assert [s.split("—")[0] for s in lsec] == [s.split("—")[0] for s in msec]
    assert "row_count" in lsec[0]
    # single-sided checks return zero rows when healthy
    assert "expect: 0" in checks.read_text() or \
        "expect: violations == 0" in checks.read_text()
    # dbt artifacts
    assert (root / "dbt" / "schema.yml").exists()
    readme = (root / "README.md").read_text()
    assert "| checksum_comparison |" in readme
    assert "Honest limits" in readme


def test_convert_ships_validation_suite(tmp_path):
    from metabridge.engine import convert
    report = convert(str(EXAMPLES / "dbt_retail"), str(tmp_path),
                     source_format="dbt", target_format="powercenter")
    assert report["validation_tests"]["total_tests"] > 0
    assert (tmp_path / "validation_tests" / "tests.json").exists()
    saved = json.loads((tmp_path / "conversion_report.json").read_text())
    assert saved["validation_tests"] == report["validation_tests"]


def test_pseudo_columns_excluded():
    """'*' / ROW_DATA sentinel ports must never become column tests —
    SUM(CASE WHEN * IS NULL ...) is not a test, it is a syntax error."""
    from metabridge.ir.model import (
        Mapping, Pipeline, Port, Transformation, TransformationType,
    )
    m = Mapping(name="opaque")
    m.transformations = [
        Transformation(name="TGT_opaque", type=TransformationType.TARGET,
                       properties={"table": "opaque"},
                       ports=[Port(name="*"), Port(name="ROW_DATA"),
                              Port(name="real_col", datatype="integer")])]
    doc = generate_tests(Pipeline(name="p", mappings=[m]),
                         "snowflake", "databricks")
    dump = json.dumps(doc)
    assert "WHEN * IS NULL" not in dump
    assert "ROW_DATA" not in dump
    (nc,) = [t for pm in doc["mappings"] for t in pm["tests"]
             if t["test_type"] == "null_comparison"]
    assert nc["columns"] == ["real_col"]

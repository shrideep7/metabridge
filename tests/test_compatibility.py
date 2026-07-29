"""Format catalog + dynamic compatibility: parser -> CIR -> generator."""
import json
from pathlib import Path

import pytest

from metabridge.engine import (
    FORMAT_LABELS, FORMATS, PROJECT_FORMATS, SOURCE_GROUPS, TARGET_GROUPS,
    WAREHOUSE_FORMATS, compatibility_matrix, evaluate_compatibility,
)

CONSOLE = (Path(__file__).resolve().parent.parent / "web" / "templates" /
           "console.html").read_text()


# ---------------------------------------------------------------------------
# catalog per spec
# ---------------------------------------------------------------------------

def test_eighteen_formats_with_exact_labels():
    assert len(FORMATS) == 18
    assert FORMAT_LABELS["sap"] == "SAP (BW / HANA / S4 / Datasphere)"
    assert FORMAT_LABELS["ssis"] == "Microsoft SSIS"
    assert FORMAT_LABELS["datastage"] == "IBM DataStage"
    assert FORMAT_LABELS["abinitio"] == "Ab Initio"
    assert FORMAT_LABELS["synapse"] == "Azure Synapse / Fabric"
    assert FORMAT_LABELS["sqlserver"] == "SQL Server (T-SQL)"
    assert FORMAT_LABELS["sql"] == "Generic ANSI SQL"
    assert FORMAT_LABELS["bigquery"] == "Google BigQuery"
    assert FORMAT_LABELS["redshift"] == "Amazon Redshift"
    assert set(FORMAT_LABELS) == set(FORMATS)


def test_groups_per_spec():
    assert PROJECT_FORMATS == ("dbt", "powercenter", "idmc")
    assert len(WAREHOUSE_FORMATS) == 10
    assert SOURCE_GROUPS[0][0] == "Projects"
    assert SOURCE_GROUPS[1][0] == "Warehouse SQL scripts"
    # the TARGET group is platforms, not scripts — the spec names them apart
    assert TARGET_GROUPS[1][0] == "Warehouse / data platforms"


# ---------------------------------------------------------------------------
# compatibility: nothing gated except same-format
# ---------------------------------------------------------------------------

def test_matrix_is_complete_and_only_diagonal_unsupported():
    m = compatibility_matrix()
    assert set(m["pairs"]) == set(FORMATS)
    for s in FORMATS:
        assert set(m["pairs"][s]) == set(FORMATS)
        for t in FORMATS:
            pair = m["pairs"][s][t]
            etl = ("ssis", "datastage", "talend", "abinitio", "sap")
            if t in etl:
                # legacy ETL platforms are modernization SOURCES only
                assert pair["supported"] is False
            elif s == t:
                assert pair["supported"] is False
                assert "nothing to convert" in pair["reason"]
            else:
                assert pair["supported"] is True, (s, t)
                assert pair["route"] == [
                    "%s parser" % FORMAT_LABELS[s], "CIR",
                    "%s generator" % FORMAT_LABELS[t]], (s, t)
    json.dumps(m)


def test_exotic_pairs_are_supported_not_disabled():
    # no "direct converter" exists for these — they flow through CIR
    for s, t in (("oracle", "teradata"), ("teradata", "idmc"),
                 ("idmc", "bigquery"), ("sqlserver", "dbt")):
        assert evaluate_compatibility(s, t)["supported"] is True


def test_autodetect_source_is_evaluated_after_detection():
    r = evaluate_compatibility("", "databricks")
    assert r["supported"] is True
    assert r["route"][0] == "auto-detected parser"
    assert "detect" in r["reason"].lower()


def test_reason_explains_conversion_class():
    assert "re-platformed" in \
        evaluate_compatibility("powercenter", "databricks")["reason"]
    assert "modernization" in \
        evaluate_compatibility("snowflake", "dbt")["reason"].lower()
    assert "graph preserved" in \
        evaluate_compatibility("dbt", "idmc")["reason"]


def test_unknown_formats_rejected():
    with pytest.raises(ValueError):
        evaluate_compatibility("dbt", "db2")
    with pytest.raises(ValueError):
        evaluate_compatibility("db2", "dbt")


def test_exotic_pair_actually_converts(tmp_path):
    """The matrix's promise is real: a pair with no direct converter
    (warehouse -> warehouse) converts through parser -> CIR -> generator."""
    from metabridge.engine import convert
    examples = Path(__file__).resolve().parent.parent / "examples"
    report = convert(str(examples / "snowflake_sql"), str(tmp_path),
                     source_format="snowflake", target_format="oracle")
    assert report["migration_validation"]["verdict"] != "FAIL"
    assert list((tmp_path / "sql").glob("*.sql"))


# ---------------------------------------------------------------------------
# console UI
# ---------------------------------------------------------------------------

def test_console_source_dropdown_per_spec():
    assert '<option value="">Auto-detect</option>' in CONSOLE
    for v in ("dbt", "powercenter", "idmc", "snowflake", "databricks",
              "bigquery", "redshift", "synapse", "sqlserver", "oracle",
              "postgres", "teradata", "sql"):
        assert '<option value="%s">' % v in CONSOLE, v
    assert 'label="Warehouse SQL scripts"' in CONSOLE


def test_console_target_dropdown_per_spec():
    assert 'label="Warehouse / data platforms"' in CONSOLE
    for label in ("Azure Synapse / Fabric", "SQL Server (T-SQL)",
                  "Generic ANSI SQL", "Google BigQuery", "Amazon Redshift"):
        assert label in CONSOLE, label


def test_console_dynamic_compatibility_wiring():
    assert "compatHint" in CONSOLE
    assert "updateCompat" in CONSOLE
    assert "/api/v1/compatibility" in CONSOLE
    # only the same-format option is ever disabled
    assert "o.value === src" in CONSOLE

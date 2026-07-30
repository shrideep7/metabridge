"""Conversion output contract (module 19): 14 fields on every conversion."""
import json
from pathlib import Path

import pytest

from metabridge.engine import (
    CONVERSION_STATUSES, build_conversion_output, convert, parse_input,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

SPEC_FIELDS = (
    "migration_id", "detected_source", "target_format", "conversion_status",
    "complexity_score", "conversion_confidence", "automation_percentage",
    "converted_assets", "manual_review_assets", "warnings", "errors",
    "lineage", "validation_summary", "output_package",
)


@pytest.fixture(autouse=True)
def _no_host_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    out = tmp_path_factory.mktemp("co")
    report = convert(str(EXAMPLES / "dbt_retail"), str(out),
                     source_format="dbt", target_format="powercenter",
                     migration_id="mig_test_0001")
    return report["conversion_output"]


def test_every_spec_field_present(result):
    for field in SPEC_FIELDS:
        assert field in result, field
    json.dumps(result)


def test_field_values(result):
    assert result["migration_id"] == "mig_test_0001"
    assert result["detected_source"] == "dbt"
    assert result["target_format"] == "powercenter"
    assert result["conversion_status"] in CONVERSION_STATUSES
    assert 0 <= result["complexity_score"] <= 100
    assert 0 <= result["conversion_confidence"] <= 100
    assert 0 <= result["automation_percentage"] <= 100
    assert len(result["converted_assets"]) == 5
    assert result["manual_review_assets"] == []
    assert result["errors"]["count"] == 0
    assert result["warnings"]["count"] > 0
    assert result["warnings"]["items"][0]["code"]
    assert result["lineage"]["tables"] >= 6
    assert result["validation_summary"]["verdict"] == "PASS_WITH_WARNINGS"
    assert result["validation_summary"]["reconciliation_tests"] > 0
    assert result["output_package"]["artifacts"]["workflow_xml"] == 1
    assert "conversion_report.html" in result["output_package"]["reports"]


def test_migration_id_minted_when_absent(tmp_path):
    report = convert(str(EXAMPLES / "dbt_retail"), str(tmp_path),
                     source_format="dbt", target_format="databricks")
    mid = report["conversion_output"]["migration_id"]
    assert len(mid) == 12 and mid.isalnum()


def test_contract_is_persisted(tmp_path):
    convert(str(EXAMPLES / "dbt_retail"), str(tmp_path),
            source_format="dbt", target_format="databricks")
    saved = json.loads((tmp_path / "conversion_report.json").read_text())
    for field in SPEC_FIELDS:
        assert field in saved["conversion_output"], field


# ---------------------------------------------------------------------------
# conversion_status derivation
# ---------------------------------------------------------------------------

def _mini_report(verdict, manual_queue=0, warning_issues=0, mappings=None):
    mappings = mappings or [{"name": "m1", "status": "CONVERTED",
                             "issues": []}]
    issues = [{"severity": "WARNING", "code": "W", "message": "w",
               "object": "m1"} for _ in range(warning_issues)]
    return {
        "source_format": "dbt", "target_format": "powercenter",
        "summary": {"status_counts": {"CONVERTED": len(mappings),
                                      "CONVERTED_WITH_WARNINGS": 0,
                                      "NEEDS_MANUAL_WORK": 0, "FAILED": 0},
                    "workload": {"manual_queue": manual_queue,
                                 "coverage_rate": 90.0},
                    "automated_conversion_rate": 90.0},
        "complexity": {"complexity_score": 10, "conversion_confidence": 95},
        "migration_validation": {"verdict": verdict, "layers": {},
                                 "totals": {}},
        "project_issues": issues, "mappings": mappings,
    }


@pytest.fixture(scope="module")
def retail():
    return parse_input(str(EXAMPLES / "dbt_retail"), "dbt")


@pytest.mark.parametrize("verdict,manual,warns,expected", [
    ("PASS", 0, 0, "COMPLETED"),
    ("PASS_WITH_WARNINGS", 0, 0, "COMPLETED_WITH_WARNINGS"),
    ("PASS", 0, 3, "COMPLETED_WITH_WARNINGS"),
    ("MANUAL_REVIEW", 0, 0, "NEEDS_MANUAL_REVIEW"),
    ("PASS", 4, 0, "NEEDS_MANUAL_REVIEW"),
    ("FAIL", 0, 0, "FAILED"),
])
def test_status_derivation(retail, tmp_path, verdict, manual, warns,
                           expected):
    co = build_conversion_output(
        retail, _mini_report(verdict, manual, warns), str(tmp_path))
    assert co["conversion_status"] == expected


def test_project_level_manual_items_are_visible(retail, tmp_path):
    """Manual queue larger than per-mapping manual assets shows the
    project-level remainder — the honest count is never hidden."""
    co = build_conversion_output(
        retail, _mini_report("MANUAL_REVIEW", manual_queue=7),
        str(tmp_path))
    assert any("project-level manual item" in x
               for x in co["manual_review_assets"])

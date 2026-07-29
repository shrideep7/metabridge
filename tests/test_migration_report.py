"""Migration Report: 15 sections, executive block, professional renderers."""
import json
from pathlib import Path

import pytest

from metabridge.engine import convert, parse_input
from metabridge.report.migration_report import (
    SECTIONS, _display, _n, build_migration_report, render_html,
    render_markdown, write_migration_report,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(autouse=True)
def _no_host_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("mr")
    convert(str(EXAMPLES / "dbt_retail"), str(d),
            source_format="dbt", target_format="powercenter")
    return d


@pytest.fixture(scope="module")
def doc(out):
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    return build_migration_report(pipeline, str(out), "powercenter")


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------

def test_fifteen_sections_in_order(doc):
    assert len(SECTIONS) == 15
    assert doc["sections_order"] == list(SECTIONS)
    assert set(doc["sections"]) == set(SECTIONS)
    json.dumps(doc)


def test_executive_summary_matches_spec_example_shape(doc):
    e = doc["sections"]["executive_summary"]
    for key in ("source", "target", "mappings_analysed",
                "automatically_converted", "manual_review",
                "automation_rate_objects", "average_confidence",
                "complexity_level", "validation_verdict"):
        assert key in e, key
    assert e["source"] == "dbt + Snowflake"       # profile-aware display
    assert e["target"] == "Informatica PowerCenter"
    assert e["mappings_analysed"] == 5
    assert e["automatically_converted"] + e["manual_review"] >= 5 or \
        e["manual_review"] == 0
    assert 0 <= e["average_confidence"] <= 100


def test_display_names():
    assert _display("powercenter") == "Informatica PowerCenter"
    assert _display("dbt", "databricks") == "dbt + Databricks"
    assert _display("bigquery") == "Google BigQuery"
    assert _n(1240) == "1,240"


def test_every_number_is_engine_backed(doc):
    sec = doc["sections"]
    assert sec["assets_analysed"]["mappings"] == 5
    assert sec["assets_analysed"]["transformations_by_type"].get(
        "JOINER", 0) >= 1
    assert sec["migration_complexity"]["complexity_level"] in (
        "LOW", "MEDIUM", "HIGH", "VERY_HIGH", "MANUAL_REVIEW_REQUIRED")
    assert sec["migration_complexity"]["manual_effort_estimate_hours"] >= 0
    cov = sec["function_conversion_risks"]["registry_coverage"]
    assert cov["platform"] == "informatica"
    assert cov["supported"] > 0
    assert sec["lineage_summary"]["tables"] >= 6
    assert sec["lineage_summary"]["longest_chain"] >= 2


def test_validation_and_tests_sections_read_stored_artifacts(doc):
    v = doc["sections"]["validation_results"]
    assert v["verdict"] in ("PASS", "PASS_WITH_WARNINGS", "MANUAL_REVIEW",
                            "FAIL")
    assert "syntax_validation" in v["layers"]
    assert v["reconciliation_tests"]["total_tests"] > 0


def test_recommended_actions_are_prioritized_and_evidence_based(doc):
    actions = doc["sections"]["recommended_actions"]
    assert actions
    assert all(a["priority"].startswith("P") and a["why"] for a in actions)
    # PowerCenter target -> sandbox import recommendation appears
    assert any("PowerCenter" in a["action"] for a in actions)


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------

def test_markdown_contains_spec_example_block(doc):
    md = render_markdown(doc)
    for line in ("Source: dbt + Snowflake",
                 "Target: Informatica PowerCenter",
                 "Mappings Analysed: 5", "Automation Rate:",
                 "Average Confidence:"):
        assert line in md, line
    for i in range(1, 16):
        assert "## %d." % i in md          # all 15 numbered sections


def test_html_is_selfcontained_and_professional(doc):
    html = render_html(doc)
    assert html.startswith("<!doctype html>")
    assert "http://" not in html and "https://" not in html   # no CDN
    for text in ("Mappings analysed", "Workload coverage",
                 "Recommended Actions", "Migration Complexity"):
        assert text in html, text


def test_write_files(doc, tmp_path):
    path = write_migration_report(doc, str(tmp_path))
    assert Path(path).name == "migration_report.html"
    assert (tmp_path / "migration_report.md").exists()
    assert (tmp_path / "migration_report.json").exists()


def test_every_convert_ships_the_migration_report(tmp_path):
    report = convert(str(EXAMPLES / "dbt_retail"), str(tmp_path),
                     source_format="dbt", target_format="databricks")
    e = report["migration_report"]
    assert e["target"] == "Databricks"
    assert (tmp_path / "migration_report.html").exists()
    assert (tmp_path / "migration_report.md").exists()
    saved = json.loads((tmp_path / "migration_report.json").read_text())
    assert saved["sections"]["executive_summary"] == e

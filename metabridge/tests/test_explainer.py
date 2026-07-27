"""Business logic explainer: semantic narratives, all sections, AI path."""
from pathlib import Path

import pytest

from metabridge.parsers.base import get_parser
from metabridge.report.explainer import (
    explain_mapping, explain_pipeline, humanize_condition,
    write_documentation,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def retail():
    return get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))


# ---------------------------------------------------------------------------
# Semantic phrasing — intent, not SQL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cond,expected", [
    ("NOT email IS NULL", "keeps records that have a value for email"),
    ("email IS NOT NULL", "keeps records that have a value for email"),
    ("mgr_id IS NULL", "keeps records missing mgr_id"),
    ("status = 'A'", "keeps records where status is 'A'"),
    ("amount > 100", "keeps records where amount exceeds 100"),
    ("amount <= 5", "keeps records where amount is at most 5"),
    ("region IN ('US', 'EU')", "keeps records where region is one of 'US', 'EU'"),
    ("updated_at > $$LAST_RUN_TS",
     "processes only records changed since the last run (watermark on updated_at)"),
])
def test_humanize_condition(cond, expected):
    assert humanize_condition(cond) == expected


def test_business_summary_reads_like_the_spec_example(retail):
    doc = explain_mapping(retail.mapping("customer_orders"), retail)
    text = doc["business_logic_summary"]
    assert text.startswith("This pipeline reads")
    assert "stg_customers" in text and "stg_orders" in text
    assert "enriched (where available) with" in text     # LEFT join semantics
    assert "summarized per" in text                      # aggregation intent
    assert "fully rebuilds the target" in text           # load strategy
    # no SQL echoes in the business narrative
    lowered = text.lower()
    for token in ("select ", "join ", "group by", "left join", "cast("):
        assert token not in lowered, token


def test_incremental_and_filter_semantics(retail):
    doc = explain_mapping(retail.mapping("stg_orders"), retail)
    text = doc["business_logic_summary"]
    assert "changed since the last run" in text          # watermark meaning
    assert "incrementally upserts" in text               # MERGE meaning
    stg_c = explain_mapping(retail.mapping("stg_customers"), retail)
    assert "have a value for email" in stg_c["business_logic_summary"]


# ---------------------------------------------------------------------------
# All required sections
# ---------------------------------------------------------------------------

def test_all_sections_present(retail):
    doc = explain_mapping(retail.mapping("customer_orders"), retail)
    for key in ("technical_summary", "business_logic_summary",
                "source_systems", "target_systems", "transformation_rules",
                "filters", "joins", "aggregations", "data_quality_rules",
                "dependencies", "potential_risks"):
        assert key in doc, key
    assert doc["dependencies"] == ["stg_customers", "stg_orders"]
    assert doc["joins"][0]["join_type"] == "LEFT"
    assert doc["aggregations"][0]["group_by"]
    assert any("customer_id" in r for r in
               [d["column"] for d in doc["transformation_rules"]] +
               doc["data_quality_rules"])
    assert doc["generated_by"] == "rules"


def test_project_level_and_markdown(retail, tmp_path):
    result = explain_pipeline(retail, use_ai=False)
    assert len(result["pipelines"]) == 5
    assert result["ai_used"] is False
    path = write_documentation(result, str(tmp_path))
    md = Path(path).read_text()
    assert "# Pipeline documentation — retail_analytics" in md
    assert "## customer_orders" in md
    assert "**Business logic**" in md
    assert "**Data quality**" in md


# ---------------------------------------------------------------------------
# Agent path (stubbed client — no network)
# ---------------------------------------------------------------------------

def test_agent_narrative_used_when_available(retail, monkeypatch):
    class FakeContent:
        type = "text"
        text = ('{"technical_summary": "Three-step flow.", '
                '"business_logic_summary": "Builds the curated customer '
                'dimension for reporting."}')

    class FakeMsg:
        content = [FakeContent()]

    class FakeClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):
                return FakeMsg()

    import metabridge.llm.assist as assist
    monkeypatch.setattr(assist, "llm_available", lambda: True)
    monkeypatch.setattr(assist, "make_client",
                        lambda: (FakeClient(), {"model": "fake"}))
    doc = explain_mapping(retail.mapping("customer_orders"), retail,
                          use_ai=True)
    assert doc["generated_by"] == "agent"
    assert "curated customer dimension" in doc["business_logic_summary"]


def test_agent_failure_falls_back_to_rules(retail, monkeypatch):
    import metabridge.llm.assist as assist
    monkeypatch.setattr(assist, "llm_available", lambda: True)

    def boom():
        raise RuntimeError("no credentials")
    monkeypatch.setattr(assist, "make_client", boom)
    doc = explain_mapping(retail.mapping("customer_orders"), retail,
                          use_ai=True)
    assert doc["generated_by"] == "rules"
    assert doc["business_logic_summary"].startswith("This pipeline reads")

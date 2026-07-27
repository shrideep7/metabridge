"""Auto-fix: approve-and-apply pipeline for the manual queue."""
from pathlib import Path

import pytest

from metabridge.engine import convert
from metabridge.report import autofix
from metabridge.report.autofix import apply_fixes, plan_fixes

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DBT_PROJECT = str(EXAMPLES / "dbt_retail")


@pytest.fixture()
def keyless_merge_job(tmp_path):
    """Convert with a merge override that lacks a key — creates the fixable state."""
    out = tmp_path / "out"
    report = convert(DBT_PROJECT, str(out), target_format="snowflake",
                     overrides={"customer_orders": {"strategy": "merge"}})
    meta = {"source_format": "dbt", "target_format": "snowflake",
            "options": {"dialect": "", "llm_assist": False, "models": None,
                        "overrides": {"customer_orders": {"strategy": "merge"}}}}
    return report, meta, out


def test_plan_detects_key_fix_deduped(keyless_merge_job):
    report, _meta, _out = keyless_merge_job
    from metabridge.engine import parse_input
    pipeline = parse_input(DBT_PROJECT, "dbt")
    plan = plan_fixes(report, pipeline)
    items = plan["groups"]["key_fix"]["items"]
    assert [(i["model"], i["proposed_key"]) for i in items] == \
        [("customer_orders", "customer_id")]
    assert plan["groups"]["key_fix"]["ready"] is True


def test_apply_key_fix_produces_keyed_merge(keyless_merge_job, tmp_path):
    report, meta, _out = keyless_merge_job
    out2 = tmp_path / "fixed"
    new_report = apply_fixes(DBT_PROJECT, str(out2), meta, ["key_fix"], [],
                             prior_report=report)
    # aggregate model: the GROUP BY grain becomes the merge key
    assert new_report["autofix"]["key_overrides"] == \
        ["customer_orders -> customer_id, customer_name, email, status_desc"]
    co = next(m for m in new_report["mappings"] if m["name"] == "customer_orders")
    assert not any(i["code"] == "MERGE_WITHOUT_KEY" for i in co["issues"])
    sql = next((out2 / "sql").glob("*customer_orders.sql")).read_text()
    assert "MERGE INTO customer_orders" in sql
    assert "t.customer_id = s.customer_id" in sql
    assert "t.status_desc = s.status_desc" in sql  # full grain in the ON clause


def test_apply_statement_drafts_with_stubbed_llm(tmp_path, monkeypatch):
    src = tmp_path / "in"
    src.mkdir()
    (src / "x.sql").write_text("CREATE VIEW v1 AS SELECT a FROM t1;\n"
                               "CALL legacy_proc('x');")
    out = tmp_path / "out"
    report = convert(str(src), str(out), source_format="snowflake",
                     target_format="dbt")
    stmt_items = [{"object": "(project)", "code": "STATEMENT_UNSUPPORTED",
                   "original": "CALL legacy_proc('x')"}]

    monkeypatch.setattr(autofix.LLMDrafter, "draft",
                        lambda self, original, s, t, c="":
                        "-- drafted equivalent\nSELECT 1 AS converted")
    meta = {"source_format": "snowflake", "target_format": "dbt",
            "options": {}}
    out2 = tmp_path / "fixed"
    new_report = apply_fixes(str(src), str(out2), meta, ["llm_statement"],
                             stmt_items, prior_report=report)
    assert new_report["autofix"]["drafts_written"] == 1
    draft = next((out2 / "manual_drafts").glob("draft_*.sql")).read_text()
    assert "REVIEW REQUIRED" in draft
    assert "drafted equivalent" in draft
    assert "CALL legacy_proc" in draft  # original embedded for review


def test_plan_llm_groups_blocked_without_key(keyless_merge_job, monkeypatch,
                                             tmp_path):
    report, _meta, _out = keyless_merge_job
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))  # no settings.json
    plan = plan_fixes(report)
    assert plan["llm_available"] is False
    assert plan["groups"]["llm_expression"]["ready"] is False

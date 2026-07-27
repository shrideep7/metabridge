"""Phase 8 — AI cost governance (§10).

Covers cost estimation from the seeded rate card, append-only idempotent usage
records that roll into the AI_TOKENS meters, budget evaluation (and the
invariant that a budget breach gates AI only — never a deterministic engine),
and the per-model rollup.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from metabridge_control import ai_cost, db, metering, schema, tenancy
from metabridge_control.context import staff_context
from metabridge_control.migrations import runner


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    eng = db.get_engine("sqlite:///" + str(tmp_path / "cp.db"))
    runner.migrate(eng)
    c = staff_context("COMMERCIAL_ADMIN", "ops")
    tid = tenancy.create_tenant(eng, slug="acme", legal_name="Acme",
                                system=True)
    return {"eng": eng, "c": c, "tid": tid}


def _window():
    now = schema.utcnow()
    return now - timedelta(days=1), now + timedelta(days=1)


def test_estimate_from_seeded_rate_card_and_meter_rollup(env):
    eng, tid = env["eng"], env["tid"]
    r = ai_cost.record_ai_usage(
        eng, tenant_id=tid, provider="ANTHROPIC", model_id="claude-sonnet-5",
        input_tokens=1_000_000, output_tokens=1_000_000, idempotency_key="k1")
    assert r["created"] and r["est_cost"] == Decimal("18.0000")   # 3 + 15

    dup = ai_cost.record_ai_usage(
        eng, tenant_id=tid, provider="ANTHROPIC", model_id="claude-sonnet-5",
        input_tokens=1_000_000, output_tokens=1_000_000, idempotency_key="k1")
    assert not dup["created"] and dup["record_id"] == r["record_id"]

    s, e = _window()
    summ = metering.usage_summary(eng, tenant_id=tid, period_start=s,
                                  period_end=e)
    by = {m["meter_code"]: m["quantity"] for m in summ["meters"]}
    assert by["AI_TOKENS_IN"] == 1_000_000 and by["AI_TOKENS_OUT"] == 1_000_000


def test_unknown_model_records_tokens_with_zero_est(env):
    eng, tid = env["eng"], env["tid"]
    r = ai_cost.record_ai_usage(
        eng, tenant_id=tid, provider="ANTHROPIC", model_id="unpriced-model",
        input_tokens=500, output_tokens=100, idempotency_key="k2")
    assert r["created"] and r["est_cost"] == Decimal("0.0000")   # tokens still fact


def test_budget_block_is_ai_only(env):
    eng, c, tid = env["eng"], env["c"], env["tid"]
    ai_cost.record_ai_usage(eng, tenant_id=tid, provider="ANTHROPIC",
                            model_id="claude-sonnet-5", input_tokens=1_000_000,
                            output_tokens=1_000_000, idempotency_key="k1")
    ai_cost.set_ai_budget(eng, c, tid, limit_tokens=1_000_000,
                          action_on_breach="BLOCK_AI_ONLY")
    s, e = _window()
    chk = ai_cost.check_ai_budget(eng, tenant_id=tid, period_start=s,
                                  period_end=e)
    # 2,000,000 consumed > 1,000,000 limit -> AI blocked, but this decision
    # only ever gates the AI path; deterministic engines are never consulted.
    assert chk["decision"] == "BLOCKED" and chk["ai_allowed"] is False
    assert chk["consumed_tokens"] == 2_000_000


def test_budget_warn_does_not_block(env):
    eng, c, tid = env["eng"], env["c"], env["tid"]
    ai_cost.record_ai_usage(eng, tenant_id=tid, provider="ANTHROPIC",
                            model_id="claude-sonnet-5", input_tokens=2_000_000,
                            output_tokens=0, idempotency_key="k1")
    ai_cost.set_ai_budget(eng, c, tid, limit_tokens=1_000_000,
                          action_on_breach="WARN")
    s, e = _window()
    chk = ai_cost.check_ai_budget(eng, tenant_id=tid, period_start=s,
                                  period_end=e)
    assert chk["decision"] == "WARN" and chk["ai_allowed"] is True

    # a generous limit is simply ALLOW
    ai_cost.set_ai_budget(eng, c, tid, limit_tokens=10_000_000,
                          action_on_breach="WARN")
    assert ai_cost.check_ai_budget(eng, tenant_id=tid, period_start=s,
                                   period_end=e)["decision"] == "ALLOW"


def test_cost_report_rolls_up_by_model(env):
    eng, c, tid = env["eng"], env["c"], env["tid"]
    ai_cost.record_ai_usage(eng, tenant_id=tid, provider="ANTHROPIC",
                            model_id="claude-sonnet-5", input_tokens=1_000_000,
                            output_tokens=1_000_000, idempotency_key="k1")
    ai_cost.record_ai_usage(eng, tenant_id=tid, provider="ANTHROPIC",
                            model_id="claude-sonnet-5", input_tokens=0,
                            output_tokens=1_000_000, idempotency_key="k2")
    s, e = _window()
    rep = ai_cost.ai_cost_report(eng, c, tenant_id=tid, period_start=s,
                                 period_end=e)
    assert rep["total_est_cost"] == Decimal("33.0000")   # 18 + 15
    assert rep["total_tokens"] == 3_000_000
    assert len(rep["by_model"]) == 1 and rep["by_model"][0]["calls"] == 2


def test_rate_card_update_supersedes(env):
    eng, c, tid = env["eng"], env["c"], env["tid"]
    ai_cost.create_rate_card(eng, c, provider="ANTHROPIC",
                             model_id="claude-sonnet-5", input_rate="6.0000",
                             output_rate="30.0000")
    r = ai_cost.record_ai_usage(
        eng, tenant_id=tid, provider="ANTHROPIC", model_id="claude-sonnet-5",
        input_tokens=1_000_000, output_tokens=0, idempotency_key="k1")
    assert r["est_cost"] == Decimal("6.0000")            # new rate, not seeded 3

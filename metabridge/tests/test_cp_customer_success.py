"""Phase 8 — customer-success analytics (§12).

Covers adoption-signal idempotency, the lifecycle history, risk-flag dedup, the
deterministic risk rules against real data (overdue invoice, expiring license,
usage decline), and the explainable, versioned health score.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from metabridge_control import (catalog, customer_success as cs, db, metering,
                                 schema, subscriptions as S, tenancy)
from metabridge_control.context import staff_context
from metabridge_control.errors import ValidationError
from metabridge_control.migrations import runner


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    eng = db.get_engine("sqlite:///" + str(tmp_path / "cp.db"))
    runner.migrate(eng)
    c = staff_context("COMMERCIAL_ADMIN", "ops")
    prod = catalog.create_product(eng, c, code="p", name="P")
    plan = catalog.create_plan(eng, c, prod, code="c", name="C")
    ver = catalog.create_plan_version(eng, c, plan)
    catalog.create_feature(eng, c, code="feature.core", name="Core",
                           value_kind="BOOLEAN")
    catalog.set_plan_feature(eng, c, ver, "feature.core", bool_value=True)
    catalog.publish_plan_version(eng, c, ver)
    tid = tenancy.create_tenant(eng, slug="acme", legal_name="Acme",
                                system=True)
    sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
    acct = S.create_customer_account(eng, sctx, name="Acme")
    sid = S.create_subscription(eng, sctx, account_id=acct, plan_version_id=ver)
    S.activate(eng, sctx, sid)
    return {"eng": eng, "c": c, "tid": tid, "sid": sid}


def test_adoption_signal_is_recorded_once(env):
    a = cs.record_adoption_signal(env["eng"], env["c"], env["tid"],
                                  signal_code="FIRST_ASSESSMENT")
    b = cs.record_adoption_signal(env["eng"], env["c"], env["tid"],
                                  signal_code="FIRST_ASSESSMENT")
    assert a["created"] and not b["created"] and a["signal_id"] == b["signal_id"]


def test_lifecycle_history_and_current_stage(env):
    eng, c, tid = env["eng"], env["c"], env["tid"]
    assert cs.current_stage(eng, tid) == "PROSPECT"          # default
    cs.transition_stage(eng, c, tid, to_stage="ONBOARDING")
    cs.transition_stage(eng, c, tid, to_stage="ADOPTING")
    assert cs.current_stage(eng, tid) == "ADOPTING"
    with pytest.raises(ValidationError):
        cs.transition_stage(eng, c, tid, to_stage="ADOPTING")   # already there
    with pytest.raises(ValidationError):
        cs.transition_stage(eng, c, tid, to_stage="NOPE")       # unknown


def test_risk_flag_dedup_and_resolve(env):
    eng, c, tid = env["eng"], env["c"], env["tid"]
    r1 = cs.open_risk_flag(eng, c, tid, code="USAGE_DECLINE", severity="MEDIUM")
    r2 = cs.open_risk_flag(eng, c, tid, code="USAGE_DECLINE", severity="HIGH")
    assert r1["created"] and not r2["created"]               # deduped while open
    cs.resolve_risk_flag(eng, c, r1["flag_id"])
    # once resolved, the same code can open again
    r3 = cs.open_risk_flag(eng, c, tid, code="USAGE_DECLINE")
    assert r3["created"]


def _insert_overdue_invoice(env):
    with env["eng"].begin() as conn:
        conn.execute(schema.invoices.insert().values(
            id=schema.new_id(), tenant_id=env["tid"], number="INV-OD-1",
            state="OVERDUE", total=Decimal("100.0000"), currency="USD",
            created_by="test"))


def _insert_expiring_license(env, days=10):
    with env["eng"].begin() as conn:
        conn.execute(schema.licenses.insert().values(
            id=schema.new_id(), tenant_id=env["tid"],
            subscription_id=env["sid"], state="ACTIVE",
            not_after=schema.utcnow() + timedelta(days=days),
            offline_grace_days=7, created_by="test"))


def test_evaluate_risks_opens_deterministic_flags(env):
    eng, c, tid, sid = env["eng"], env["c"], env["tid"], env["sid"]
    _insert_overdue_invoice(env)
    _insert_expiring_license(env, days=10)
    now = schema.utcnow()
    metering.record_usage(eng, tenant_id=tid, meter_code="API_CALLS",
                          quantity=100, idempotency_key="prior",
                          occurred_at=now - timedelta(days=45),
                          subscription_id=sid)
    metering.record_usage(eng, tenant_id=tid, meter_code="API_CALLS",
                          quantity=5, idempotency_key="recent",
                          occurred_at=now - timedelta(days=5),
                          subscription_id=sid)
    opened = cs.evaluate_risks(eng, c, tid)["opened"]
    assert set(opened) == {"UNPAID_INVOICE", "LICENSE_EXPIRING", "USAGE_DECLINE"}
    # rerunning does not duplicate (all still open)
    assert cs.evaluate_risks(eng, c, tid)["opened"] == []


def test_health_score_is_explainable_and_reflects_risk(env):
    eng, c, tid = env["eng"], env["c"], env["tid"]
    cs.open_risk_flag(eng, c, tid, code="UNPAID_INVOICE", severity="HIGH")
    cs.open_risk_flag(eng, c, tid, code="LICENSE_EXPIRING", severity="MEDIUM")
    snap = cs.compute_health(eng, c, tid)
    assert snap["score"] == 65                    # 100 - 25(HIGH) - 10(MEDIUM)
    assert snap["formula_version"] == "cs.health/1"
    assert snap["inputs"]["open_risk_flags"] == 2


def test_health_reflects_lifecycle_stage(env):
    eng, c, tid = env["eng"], env["c"], env["tid"]
    cs.transition_stage(eng, c, tid, to_stage="AT_RISK")
    assert cs.compute_health(eng, c, tid)["score"] == 50    # capped
    cs.transition_stage(eng, c, tid, to_stage="CHURNED")
    assert cs.compute_health(eng, c, tid)["score"] == 0     # floored


def test_cs_overview_aggregates(env):
    eng, c, tid = env["eng"], env["c"], env["tid"]
    cs.record_adoption_signal(eng, c, tid, signal_code="AI_ENABLED")
    cs.transition_stage(eng, c, tid, to_stage="ADOPTING")
    cs.open_risk_flag(eng, c, tid, code="UNPAID_INVOICE", severity="HIGH")
    cs.compute_health(eng, c, tid)
    ov = cs.cs_overview(eng, c, tid)
    assert ov["stage"] == "ADOPTING"
    assert ov["health"]["score"] == 75           # 100 - 25(HIGH)
    assert "AI_ENABLED" in ov["adoption_signals"]
    assert any(r["code"] == "UNPAID_INVOICE" for r in ov["open_risks"])

"""Regression tests for the Phase-2 adversarial-review findings."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from metabridge_control import catalog, db, schema, subscriptions as S, tenancy
from metabridge_control import entitlements as E
from metabridge_control.context import staff_context
from metabridge_control.errors import ValidationError
from metabridge_control.migrations import runner


def _plan(eng, staff, feats):
    prod = catalog.create_product(eng, staff, code="p", name="P")
    plan = catalog.create_plan(eng, staff, prod, code="c", name="C")
    ver = catalog.create_plan_version(eng, staff, plan)
    for code, val in feats.items():
        catalog.create_feature(eng, staff, code=code, name=code,
                               value_kind="BOOLEAN" if val == "b" else "LIMIT")
    for code, val in feats.items():
        if val == "b":
            catalog.set_plan_feature(eng, staff, ver, code, bool_value=True)
        else:
            catalog.set_plan_feature(eng, staff, ver, code, limit_value=val)
    catalog.publish_plan_version(eng, staff, ver)
    return ver


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    eng = db.get_engine("sqlite:///" + str(tmp_path / "p2.db"))
    runner.migrate(eng)
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    ver = _plan(eng, staff, {"feature.twin": "b", "limit.users": 5,
                             "quota.ai_credits": 100})
    tid = tenancy.create_tenant(eng, slug="acme", legal_name="Acme",
                                system=True)
    sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
    acct = S.create_customer_account(eng, sctx, name="Acme")
    return {"eng": eng, "tid": tid, "ver": ver, "sctx": sctx, "acct": acct,
            "staff": staff}


def _active_sub(env, billing="MONTHLY"):
    sid = S.create_subscription(env["eng"], env["sctx"], account_id=env["acct"],
                                plan_version_id=env["ver"], billing_period=billing)
    S.activate(env["eng"], env["sctx"], sid)
    return sid


# --------- HIGH: state + entitlements are one transaction (atomicity) -------
def test_activation_state_and_entitlements_commit_together(env):
    sid = _active_sub(env)
    with env["eng"].connect() as conn:
        state = conn.execute(select(schema.subscriptions.c.state).where(
            schema.subscriptions.c.id == sid)).scalar_one()
        n = conn.execute(select(schema.entitlements).where(
            schema.entitlements.c.subscription_id == sid)).mappings().all()
    assert state == "ACTIVE" and len(n) == 3   # never ACTIVE with zero ents


def test_failed_resolution_rolls_back_the_transition(env, monkeypatch):
    """If entitlement resolution throws, the state change must roll back too
    (single transaction) — no committed state paired with stale entitlements."""
    sid = S.create_subscription(env["eng"], env["sctx"], account_id=env["acct"],
                                plan_version_id=env["ver"])
    # force resolve_in_conn to blow up mid-activation
    monkeypatch.setattr(E, "resolve_in_conn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        S.activate(env["eng"], env["sctx"], sid)
    with env["eng"].connect() as conn:
        state = conn.execute(select(schema.subscriptions.c.state).where(
            schema.subscriptions.c.id == sid)).scalar_one()
        ents = conn.execute(select(schema.entitlements).where(
            schema.entitlements.c.subscription_id == sid)).mappings().all()
    # the resolve-bearing ACTIVE transition rolled back atomically: the
    # subscription is NOT ACTIVE and has no (stale/empty) entitlement set.
    assert state != "ACTIVE"
    assert len(ents) == 0


# --------- MEDIUM: healthiest subscription wins, not newest-created ----------
def test_active_subscription_prefers_health_over_recency(env):
    old_active = _active_sub(env)
    # a newer subscription that goes SUSPENDED must not shadow the ACTIVE one
    newer = S.create_subscription(env["eng"], env["sctx"],
                                  account_id=env["acct"],
                                  plan_version_id=env["ver"])
    S.activate(env["eng"], env["sctx"], newer)
    S.mark_past_due(env["eng"], env["sctx"], newer)
    S.suspend(env["eng"], env["sctx"], newer)
    d = E.check_access(env["eng"], tenant_id=env["tid"], code="feature.twin")
    assert d.decision == "ALLOW"               # served by the ACTIVE sub
    assert d.subscription_id == old_active


# --------- override expiry self-heals in real time --------------------------
def test_expired_override_falls_back_to_plan_value(env):
    sid = _active_sub(env)
    now = schema.utcnow()
    # staff grants a time-boxed bump of limit.users 5 -> 50 for 1 hour
    S.add_override(env["eng"], env["sctx"], sid, code="limit.users",
                   value={"unlimited": False, "limit": 50},
                   reason="temporary expansion",
                   expires_at=now + timedelta(hours=1))
    # within the window: 40 allowed
    d1 = E.check_access(env["eng"], tenant_id=env["tid"], code="limit.users",
                        current_usage=40, quantity=1, now=now)
    assert d1.decision == "ALLOW" and d1.limit == 50
    # after expiry: self-heals back to the plan limit of 5 -> 40 exceeds it
    later = now + timedelta(hours=2)
    d2 = E.check_access(env["eng"], tenant_id=env["tid"], code="limit.users",
                        current_usage=40, quantity=1, now=later)
    assert d2.decision == "DENY" and d2.limit == 5


# --------- MEDIUM #4: tenant scope guard ------------------------------------
def test_expected_tenant_mismatch_is_rejected(env):
    _active_sub(env)
    with pytest.raises(ValidationError):
        E.check_access(env["eng"], tenant_id=env["tid"], code="feature.twin",
                       expected_tenant="some-other-tenant")
    with pytest.raises(ValidationError):
        E.consume(env["eng"], tenant_id=env["tid"], code="quota.ai_credits",
                  quantity=1, expected_tenant="some-other-tenant")
    # matching scope is fine
    ok = E.check_access(env["eng"], tenant_id=env["tid"], code="feature.twin",
                        expected_tenant=env["tid"])
    assert ok.decision == "ALLOW"


# --------- idempotency key cannot be reused across meters -------------------
def test_idempotency_key_scoped_to_meter(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    eng = db.get_engine("sqlite:///" + str(tmp_path / "idem.db"))
    runner.migrate(eng)
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    ver = _plan(eng, staff, {"quota.a": 100, "quota.b": 100})   # two meters
    tid = tenancy.create_tenant(eng, slug="acme", legal_name="Acme",
                                system=True)
    sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
    acct = S.create_customer_account(eng, sctx, name="Acme")
    sid = S.create_subscription(eng, sctx, account_id=acct, plan_version_id=ver,
                                billing_period="MONTHLY")
    S.activate(eng, sctx, sid)

    r1 = E.check_access(eng, tenant_id=tid, code="quota.a", quantity=5,
                        mode=E.Mode.RESERVE, idempotency_key="K1")
    assert r1.decision == "ALLOW"
    # same key, different meter -> rejected (would otherwise bypass b's quota)
    with pytest.raises(ValidationError):
        E.check_access(eng, tenant_id=tid, code="quota.b", quantity=5,
                       mode=E.Mode.RESERVE, idempotency_key="K1")
    # same key, same meter -> idempotent replay returns the same reservation
    r2 = E.check_access(eng, tenant_id=tid, code="quota.a", quantity=5,
                        mode=E.Mode.RESERVE, idempotency_key="K1")
    assert r2.reservation_id == r1.reservation_id

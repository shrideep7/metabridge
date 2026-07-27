"""Phase 2 — entitlement engine: value kinds, states, reserve/commit/release,
enforcement points."""
from datetime import timedelta

import pytest

from metabridge_control import (catalog, db, enforcement, schema,
                                 subscriptions as S, tenancy)
from metabridge_control import entitlements as E
from metabridge_control.context import staff_context
from metabridge_control.errors import ValidationError
from metabridge_control.migrations import runner


def _plan(eng, staff, feats):
    prod = catalog.create_product(eng, staff, code="p", name="P")
    plan = catalog.create_plan(eng, staff, prod, code="c", name="C")
    ver = catalog.create_plan_version(eng, staff, plan)
    for code, vkind in feats.items():
        catalog.create_feature(eng, staff, code=code, name=code,
                               value_kind="BOOLEAN" if vkind == "b" else "LIMIT")
    for code, val in feats.items():
        if val == "b":
            catalog.set_plan_feature(eng, staff, ver, code, bool_value=True)
        elif val == "unlimited":
            catalog.set_plan_feature(eng, staff, ver, code, unlimited=True)
        else:
            catalog.set_plan_feature(eng, staff, ver, code, limit_value=val)
    catalog.publish_plan_version(eng, staff, ver)
    return ver


@pytest.fixture()
def sub(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    eng = db.get_engine("sqlite:///" + str(tmp_path / "e.db"))
    runner.migrate(eng)
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    ver = _plan(eng, staff, {"feature.twin": "b", "limit.users": 3,
                             "quota.assessments": 2, "quota.ai_credits": 100,
                             "limit.storage_gb": "unlimited"})
    tid = tenancy.create_tenant(eng, slug="acme", legal_name="Acme",
                                system=True)
    sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
    acct = S.create_customer_account(eng, sctx, name="Acme")
    sid = S.create_subscription(eng, sctx, account_id=acct,
                                plan_version_id=ver, billing_period="MONTHLY")
    S.activate(eng, sctx, sid)
    return {"eng": eng, "tid": tid, "sid": sid, "sctx": sctx}


# ------------------------------------------------------------- value kinds
def test_boolean_feature(sub):
    assert enforcement.feature_enabled(sub["eng"], tenant_id=sub["tid"],
                                       feature_code="feature.twin") is True
    assert enforcement.feature_enabled(sub["eng"], tenant_id=sub["tid"],
                                       feature_code="feature.absent") is False


def test_numeric_limit(sub):
    ok = enforcement.check(sub["eng"], tenant_id=sub["tid"], resource="users",
                           current_usage=2, quantity=1)
    assert ok.decision == "ALLOW" and ok.remaining == 1
    deny = enforcement.check(sub["eng"], tenant_id=sub["tid"], resource="users",
                             current_usage=3, quantity=1)
    assert deny.decision == "DENY" and deny.reason_code == "LIMIT_EXCEEDED"


def test_unlimited_limit(sub):
    d = enforcement.check(sub["eng"], tenant_id=sub["tid"],
                          resource="storage_gb", current_usage=10 ** 9,
                          quantity=1)
    assert d.decision == "ALLOW" and d.remaining is None


def test_metered_quota_consume_and_exhaust(sub):
    a = enforcement.consume(sub["eng"], tenant_id=sub["tid"],
                            resource="assessments", idempotency_key="a1")
    b = enforcement.consume(sub["eng"], tenant_id=sub["tid"],
                            resource="assessments", idempotency_key="a2")
    c = enforcement.consume(sub["eng"], tenant_id=sub["tid"],
                            resource="assessments", idempotency_key="a3")
    assert a.allowed and b.allowed
    assert c.decision == "DENY" and c.reason_code == "QUOTA_EXHAUSTED"


def test_consume_is_idempotent(sub):
    d1 = enforcement.consume(sub["eng"], tenant_id=sub["tid"],
                             resource="assessments", idempotency_key="same")
    d2 = enforcement.consume(sub["eng"], tenant_id=sub["tid"],
                             resource="assessments", idempotency_key="same")
    assert d1.allowed and d2.allowed
    # only ONE unit consumed despite two calls -> a third distinct key still ok
    d3 = enforcement.consume(sub["eng"], tenant_id=sub["tid"],
                             resource="assessments", idempotency_key="other")
    assert d3.allowed
    d4 = enforcement.consume(sub["eng"], tenant_id=sub["tid"],
                             resource="assessments", idempotency_key="third")
    assert d4.reason_code == "QUOTA_EXHAUSTED"   # limit 2 reached


# ----------------------------------------------------- reserve/commit/release
def test_reserve_commit_returns_delta(sub):
    r = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=60,
                               idempotency_key="j1")
    assert r.allowed and r.reservation_id
    # while reserved, remaining is 40; a 50 reserve must fail
    r2 = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=50,
                                idempotency_key="j2")
    assert r2.reason_code == "QUOTA_EXHAUSTED"
    # commit only 20 -> 40 of the hold returns
    c = enforcement.commit_ai(sub["eng"], tenant_id=sub["tid"],
                              reservation_id=r.reservation_id, actual=20)
    assert c.allowed and c.extra["charged"] == 20
    # now 80 remain
    r3 = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=80,
                                idempotency_key="j3")
    assert r3.allowed


def test_reserve_release_returns_hold(sub):
    r = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=100,
                               idempotency_key="jr")
    assert r.allowed
    rel = enforcement.release_ai(sub["eng"], tenant_id=sub["tid"],
                                 reservation_id=r.reservation_id)
    assert rel.allowed
    r2 = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=100,
                                idempotency_key="jr2")
    assert r2.allowed          # full quota available again


def test_reserve_is_idempotent(sub):
    a = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=30,
                               idempotency_key="dup")
    b = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=30,
                               idempotency_key="dup")
    assert a.reservation_id == b.reservation_id     # same reservation


def test_commit_overage_tolerance(sub):
    r = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=50,
                               idempotency_key="ov")
    c = enforcement.commit_ai(sub["eng"], tenant_id=sub["tid"],
                              reservation_id=r.reservation_id, actual=54)
    assert c.allowed and c.extra["overage"] == 4     # within 10% tolerance


def test_expired_reservation_swept(sub):
    r = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=40,
                               idempotency_key="exp", ttl_minutes=1)
    n = E.sweep_expired_reservations(
        sub["eng"], now=schema.utcnow() + timedelta(minutes=5))
    assert n == 1
    # hold released -> full quota available
    r2 = enforcement.reserve_ai(sub["eng"], tenant_id=sub["tid"], quantity=100,
                                idempotency_key="exp2")
    assert r2.allowed
    # committing an expired reservation is refused
    c = enforcement.commit_ai(sub["eng"], tenant_id=sub["tid"],
                              reservation_id=r.reservation_id, actual=10)
    assert c.reason_code == "RESERVATION_EXPIRED"


# --------------------------------------------------------- state-derived gates
def test_grace_when_past_due(sub):
    S.mark_past_due(sub["eng"], sub["sctx"], sub["sid"])
    d = enforcement.check(sub["eng"], tenant_id=sub["tid"], resource="users",
                          current_usage=0, quantity=1)
    assert d.decision == "ALLOW_GRACE" and d.reason_code == "OK_GRACE"


def test_suspended_blocks_writes(sub):
    S.mark_past_due(sub["eng"], sub["sctx"], sub["sid"])
    S.suspend(sub["eng"], sub["sctx"], sub["sid"])
    d = enforcement.consume(sub["eng"], tenant_id=sub["tid"],
                            resource="assessments", idempotency_key="s1")
    assert d.reason_code == "SUBSCRIPTION_SUSPENDED"


def test_expired_term_denies(sub):
    # force the term into the past
    with sub["eng"].begin() as conn:
        conn.execute(schema.subscriptions.update()
                     .where(schema.subscriptions.c.id == sub["sid"])
                     .values(term_end=schema.utcnow() - timedelta(days=1)))
    d = enforcement.check(sub["eng"], tenant_id=sub["tid"], resource="users",
                          current_usage=0, quantity=1)
    assert d.reason_code == "LICENSE_EXPIRED"


def test_no_active_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    eng = db.get_engine("sqlite:///" + str(tmp_path / "empty.db"))
    runner.migrate(eng)
    tid = tenancy.create_tenant(eng, slug="none", legal_name="None",
                                system=True)
    d = enforcement.check(eng, tenant_id=tid, resource="users")
    assert d.reason_code == "NO_ACTIVE_SUBSCRIPTION"


def test_override_raises_a_limit_and_is_resolved(sub):
    # limit.users is 3 in the plan; override to 50
    staff = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=sub["tid"])
    S.add_override(sub["eng"], staff, sub["sid"], code="limit.users",
                   value={"unlimited": False, "limit": 50},
                   reason="enterprise expansion")
    d = enforcement.check(sub["eng"], tenant_id=sub["tid"], resource="users",
                          current_usage=40, quantity=1)
    assert d.decision == "ALLOW" and d.limit == 50


def test_unknown_resource_rejected(sub):
    with pytest.raises(KeyError):
        enforcement.check(sub["eng"], tenant_id=sub["tid"], resource="teleport")

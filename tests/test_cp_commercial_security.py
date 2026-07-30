"""Phase 2 — cross-tenant isolation, override authorization, admin-API auth,
and concurrency (no oversell)."""
import concurrent.futures as cf

import pytest
from sqlalchemy import select

from metabridge_control import (catalog, db, enforcement, licensing, schema,
                                 subscriptions as S, tenancy)
from metabridge_control.context import staff_context
from metabridge_control.errors import NotFoundError, PermissionDenied
from metabridge_control.migrations import runner


def _published_plan(eng, staff, users=3, ai=100):
    prod = catalog.create_product(eng, staff, code="p", name="P")
    catalog.create_feature(eng, staff, code="limit.users", name="U",
                           value_kind="LIMIT")
    catalog.create_feature(eng, staff, code="quota.ai_credits", name="AI",
                           value_kind="LIMIT")
    plan = catalog.create_plan(eng, staff, prod, code="c", name="C")
    ver = catalog.create_plan_version(eng, staff, plan)
    catalog.set_plan_feature(eng, staff, ver, "limit.users", limit_value=users)
    catalog.set_plan_feature(eng, staff, ver, "quota.ai_credits",
                             limit_value=ai)
    catalog.publish_plan_version(eng, staff, ver)
    return ver


@pytest.fixture()
def two_tenants(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    eng = db.get_engine("sqlite:///" + str(tmp_path / "sec.db"))
    runner.migrate(eng)
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    ver = _published_plan(eng, staff)
    out = {"eng": eng, "ver": ver}
    for slug in ("alpha", "beta"):
        tid = tenancy.create_tenant(eng, slug=slug, legal_name=slug,
                                    system=True)
        sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
        acct = S.create_customer_account(eng, sctx, name=slug)
        sid = S.create_subscription(eng, sctx, account_id=acct,
                                    plan_version_id=ver)
        S.activate(eng, sctx, sid)
        out[slug] = {"tid": tid, "sid": sid, "sctx": sctx, "acct": acct}
    return out


def test_subscription_not_visible_across_tenants(two_tenants):
    eng, a, b = two_tenants["eng"], two_tenants["alpha"], two_tenants["beta"]
    with pytest.raises(NotFoundError):
        S.get_subscription(eng, a["sctx"], b["sid"])   # scoped by ctx tenant


def test_transition_cannot_cross_tenants(two_tenants):
    eng, a, b = two_tenants["eng"], two_tenants["alpha"], two_tenants["beta"]
    with pytest.raises(NotFoundError):
        S.cancel(eng, a["sctx"], b["sid"])
    assert S.get_subscription(eng, b["sctx"], b["sid"])["state"] == "ACTIVE"


def test_metered_consumption_isolated(two_tenants):
    eng, a, b = two_tenants["eng"], two_tenants["alpha"], two_tenants["beta"]
    enforcement.reserve_ai(eng, tenant_id=a["tid"], quantity=100,
                           idempotency_key="a-all")
    # A exhausted; B still has full quota
    da = enforcement.reserve_ai(eng, tenant_id=a["tid"], quantity=1,
                                idempotency_key="a-more")
    db_ = enforcement.reserve_ai(eng, tenant_id=b["tid"], quantity=100,
                                 idempotency_key="b-all")
    assert da.reason_code == "QUOTA_EXHAUSTED" and db_.allowed


def test_override_requires_staff_permission(two_tenants):
    eng, a = two_tenants["eng"], two_tenants["alpha"]
    # a customer owner context lacks entitlements:override
    weak = staff_context("SUPPORT_ADMIN", "support", tenant_id=a["tid"])
    with pytest.raises(PermissionDenied):
        S.add_override(eng, weak, a["sid"], code="limit.users",
                       value={"limit": 999}, reason="nope")


def test_override_is_audited(two_tenants):
    eng, a = two_tenants["eng"], two_tenants["alpha"]
    staff = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=a["tid"])
    S.add_override(eng, staff, a["sid"], code="limit.users",
                   value={"unlimited": False, "limit": 99},
                   reason="contract expansion")
    with eng.connect() as conn:
        ev = conn.execute(select(schema.audit_events).where(
            (schema.audit_events.c.tenant_id == a["tid"])
            & (schema.audit_events.c.action == "entitlement.override"))) \
            .mappings().all()
    assert ev and ev[-1]["reason"] == "contract expansion"
    assert ev[-1]["after_state"]["value"]["limit"] == 99


def test_license_file_signature_binds_tenant(two_tenants):
    eng, a = two_tenants["eng"], two_tenants["alpha"]
    lid = licensing.issue_license(eng, a["sctx"], a["sid"])
    lf = licensing.generate_license_file(eng, a["sctx"], lid)
    assert licensing.verify_license_file(lf["payload"], lf["signature"]) is True
    forged = dict(lf["payload"]); forged["tenant_id"] = "someone-else"
    assert licensing.verify_license_file(forged, lf["signature"]) is False


# ------------------------------------------------------------- concurrency
def test_concurrent_reserves_do_not_oversell(two_tenants):
    eng, a = two_tenants["eng"], two_tenants["alpha"]
    # ai quota is 100; fire 20 concurrent reserves of 10 -> at most 10 succeed
    def one(i):
        return enforcement.reserve_ai(eng, tenant_id=a["tid"], quantity=10,
                                      idempotency_key=f"c{i}").allowed
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(one, range(20)))
    allowed = sum(1 for r in results if r)
    assert allowed == 10        # exactly the quota, never more (no oversell)


def test_concurrent_consume_respects_limit(two_tenants):
    eng, a = two_tenants["eng"], two_tenants["alpha"]
    # users limit is 3 but consume path uses a metered code; use ai_credits=100
    # consume 1 credit 150 times concurrently -> exactly 100 succeed
    def one(i):
        from metabridge_control import entitlements as E
        return E.consume(eng, tenant_id=a["tid"], code="quota.ai_credits",
                         quantity=1, idempotency_key=f"u{i}").allowed
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(one, range(150)))
    assert sum(1 for r in results if r) == 100


# ------------------------------------------------------------- admin API auth
def test_admin_api_requires_key(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTROLPLANE_ADMIN_KEY", "s3cret")
    monkeypatch.setenv("CONTROLPLANE_DATABASE_URL",
                       "sqlite:///" + str(tmp_path / "api.db"))
    from metabridge_control import db as cpdb
    runner.migrate(cpdb.get_engine())
    import importlib
    import web.commercial_app as ca
    importlib.reload(ca)
    client = TestClient(ca.commercial_app)
    assert client.get("/tenants").status_code == 401         # no key
    assert client.get("/tenants",
                      headers={"X-Commercial-Key": "wrong"}).status_code == 401
    ok = client.get("/tenants", headers={"X-Commercial-Key": "s3cret"})
    assert ok.status_code == 200 and "tenants" in ok.json()


def test_admin_api_fails_closed_when_unconfigured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_ADMIN_KEY", raising=False)
    import importlib
    import web.commercial_app as ca
    importlib.reload(ca)
    client = TestClient(ca.commercial_app)
    r = client.get("/tenants", headers={"X-Commercial-Key": "anything"})
    assert r.status_code == 503        # never open by default

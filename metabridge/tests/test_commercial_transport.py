"""Phase 5 — connected-mode transport, end to end against the control plane.

Drives the real data-plane client against the real control-plane FastAPI app
(via a TestClient adapter): enroll with a minted token, poll entitlements into
a bridge, drain a usage spool to the instance endpoint, and prove the offline
TTL/grace behaviour of the entitlement cache.
"""
import importlib

import pytest

from metabridge.commercial import (ControlPlaneClient, ControlPlaneUnavailable,
                                    EntitlementDenied, UsageSpool,
                                    connected_bridge, drain_usage)
from metabridge.platform.flags import FeatureFlags


class _TCHttp:
    """Adapter: route the client's HTTP calls through a Starlette TestClient."""

    def __init__(self, client):
        self._c = client

    def request(self, method, path, headers, body=None):
        r = self._c.request(method, path, headers=headers or {}, json=body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "server"))
    monkeypatch.setenv("CONTROLPLANE_ADMIN_KEY", "adm")
    monkeypatch.setenv("CONTROLPLANE_DATABASE_URL",
                       "sqlite:///" + str(tmp_path / "cp.db"))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    from metabridge_control import (catalog, db, schema, subscriptions as S,
                                     tenancy)
    from metabridge_control.context import staff_context
    from metabridge_control.migrations import runner
    eng = db.get_engine()
    runner.migrate(eng)
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    prod = catalog.create_product(eng, staff, code="p", name="P")
    plan = catalog.create_plan(eng, staff, prod, code="c", name="C")
    ver = catalog.create_plan_version(eng, staff, plan)
    catalog.create_feature(eng, staff, code="feature.core", name="Core",
                           value_kind="BOOLEAN")
    catalog.set_plan_feature(eng, staff, ver, "feature.core", bool_value=True)
    catalog.publish_plan_version(eng, staff, ver)
    tid = tenancy.create_tenant(eng, slug="acme", legal_name="Acme",
                                system=True)
    sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
    acct = S.create_customer_account(eng, sctx, name="Acme")
    sid = S.create_subscription(eng, sctx, account_id=acct, plan_version_id=ver)
    S.activate(eng, sctx, sid)
    import web.commercial_app as ca
    importlib.reload(ca)
    tc = TestClient(ca.commercial_app)
    token = tc.post("/instances/enroll-token", headers={"X-Commercial-Key": "adm"},
                    json={"tenant_id": tid, "subscription_id": sid}).json()["token"]
    client = ControlPlaneClient(http=_TCHttp(tc),
                                data_dir=str(tmp_path / "instance"))
    return {"tc": tc, "client": client, "token": token, "tid": tid, "sid": sid,
            "eng": eng, "idir": tmp_path / "instance"}


def test_enroll_then_fetch_entitlements_and_gate(wired):
    client, tid, sid = wired["client"], wired["tid"], wired["sid"]
    ident = client.enroll(wired["token"], name="edge-1")
    assert ident["tenant_id"] == tid and client.enrolled

    ents = client.fetch_entitlements()
    assert any(e["code"] == "feature.core" for e in ents)

    # a connected bridge built from live entitlements enforces correctly
    flags = FeatureFlags(str(wired["idir"]))
    flags.set("commercial_enforcement", enabled=True)
    flags.set("commercial_enforcement_deny", enabled=True)
    bridge = connected_bridge(client, flags)
    assert bridge.enforce("feature.core").allowed
    with pytest.raises(EntitlementDenied):
        bridge.enforce("feature.premium")


def test_usage_spool_drains_to_control_plane(wired):
    client = wired["client"]
    client.enroll(wired["token"])
    spool = UsageSpool(wired["idir"] / "commercial" / "spool.json")
    for i in range(3):
        spool.add("API_CALLS", 2, f"k{i}")
    res = drain_usage(client, spool)
    assert res["drained"] and res["sent"] == 3 and spool.count() == 0

    # server actually recorded them (usage summary over a wide window)
    from datetime import timedelta
    from metabridge_control import metering, schema
    now = schema.utcnow()
    summ = metering.usage_summary(wired["eng"], tenant_id=wired["tid"],
                                  period_start=now - timedelta(days=1),
                                  period_end=now + timedelta(days=1))
    api = next(m for m in summ["meters"] if m["meter_code"] == "API_CALLS")
    assert api["quantity"] == 6                     # 3 events x qty 2


def test_entitlement_cache_offline_grace(wired):
    client = wired["client"]
    client.enroll(wired["token"])
    t0 = 1_000_000.0
    live = client.fetch_entitlements(now=t0)        # caches at t0
    assert live

    # control plane goes dark
    class _Dead:
        def request(self, *a, **k):
            raise ControlPlaneUnavailable("down")
    client._http = _Dead()

    # within grace: serve the cached snapshot (force a live attempt w/ max_age=0)
    served = client.fetch_entitlements(max_age_s=0, grace_s=1000, now=t0 + 500)
    assert served == live

    # beyond grace: fail closed
    with pytest.raises(ControlPlaneUnavailable):
        client.fetch_entitlements(max_age_s=0, grace_s=1000, now=t0 + 5000)

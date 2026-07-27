"""Phase 5 — instance enrollment (control plane).

Covers the token→enroll→authenticate handshake, tenant derivation from the
credential, fail-closed rejection of bad/expired/reused tokens, revocation, and
the air-gap trust loop: a statement signed by the data-plane ``airgap`` module
verifies on ingest against the instance's *registered* public key (proving
metering._resolve_trust_key now pins per-instance keys, EB-505).
"""
import importlib

import pytest

from metabridge.commercial import airgap
from metabridge_control import (catalog, db, enrollment, metering, schema,
                                 subscriptions as S, tenancy)
from metabridge_control.context import staff_context
from metabridge_control.errors import TenantAccessDenied, ValidationError
from metabridge_control.migrations import runner


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTROLPLANE_ADMIN_KEY", "adm")
    monkeypatch.setenv("CONTROLPLANE_DATABASE_URL",
                       "sqlite:///" + str(tmp_path / "cp.db"))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
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
    return {"eng": eng, "tid": tid, "sid": sid, "staff": staff, "sctx": sctx,
            "tmp": tmp_path}


# ------------------------------------------------------------- handshake
def test_enroll_authenticate_derives_tenant(env):
    eng, tid, sid = env["eng"], env["tid"], env["sid"]
    tok = enrollment.create_enrollment_token(
        eng, env["sctx"], tenant_id=tid, subscription_id=sid)["token"]
    res = enrollment.enroll_instance(eng, token=tok, name="prod-1")
    assert res["tenant_id"] == tid and res["credential"].startswith("ins_")

    ident = enrollment.authenticate_instance(eng, res["credential"])
    assert ident["tenant_id"] == tid            # tenant derived from credential
    assert ident["instance_id"] == res["instance_id"]
    assert ident["subscription_id"] == sid


def test_bad_expired_and_reused_tokens_fail_closed(env):
    eng, tid = env["eng"], env["tid"]
    with pytest.raises(TenantAccessDenied):
        enrollment.enroll_instance(eng, token="enr_nope")

    tok = enrollment.create_enrollment_token(eng, env["sctx"], tenant_id=tid,
                                             ttl_hours=0)["token"]
    with pytest.raises(TenantAccessDenied):      # already expired (ttl 0)
        enrollment.enroll_instance(eng, token=tok)

    tok2 = enrollment.create_enrollment_token(eng, env["sctx"],
                                              tenant_id=tid)["token"]
    enrollment.enroll_instance(eng, token=tok2)  # consume it
    with pytest.raises(TenantAccessDenied):      # single-use
        enrollment.enroll_instance(eng, token=tok2)


def test_airgapped_requires_public_key(env):
    eng, tid = env["eng"], env["tid"]
    tok = enrollment.create_enrollment_token(
        eng, env["sctx"], tenant_id=tid,
        delivery_model="MODEL_C_AIRGAPPED")["token"]
    with pytest.raises(ValidationError):
        enrollment.enroll_instance(eng, token=tok)          # no key


def test_revoke_invalidates_credential(env):
    eng, tid = env["eng"], env["tid"]
    tok = enrollment.create_enrollment_token(eng, env["sctx"],
                                             tenant_id=tid)["token"]
    res = enrollment.enroll_instance(eng, token=tok)
    enrollment.revoke_instance(eng, env["sctx"], res["instance_id"],
                               reason="decommissioned")
    with pytest.raises(TenantAccessDenied):
        enrollment.authenticate_instance(eng, res["credential"])


# ------------------------------------------------- air-gap trust loop (EB-505)
def test_airgap_statement_verifies_against_registered_key(env):
    eng, tid = env["eng"], env["tid"]
    priv, pub = airgap.load_or_create_instance_key(str(env["tmp"]))
    tok = enrollment.create_enrollment_token(
        eng, env["sctx"], tenant_id=tid,
        delivery_model="MODEL_C_AIRGAPPED")["token"]
    res = enrollment.enroll_instance(eng, token=tok, public_key=pub)
    assert enrollment.instance_public_key(eng, tid, res["instance_id"]) == pub

    stmt = airgap.build_usage_statement(
        tenant_id=tid, instance_id=res["instance_id"], serial="2026-07",
        period_start="2026-07-01 00:00:00", period_end="2026-08-01 00:00:00",
        lines=[{"meter_code": "API_CALLS", "usage": 5}])
    signed = airgap.sign_statement(stmt, priv)
    # If _resolve_trust_key fell back to the control-plane signer this would
    # FAIL — success proves the per-instance registered key is pinned.
    out = metering.ingest_signed_statement(
        eng, payload=signed["payload"], signature=signed["signature"])
    assert out["state"] == "INGESTED" and out["ingested"] == 1


# ------------------------------------------------------------- HTTP surface
@pytest.fixture()
def api(env):
    from fastapi.testclient import TestClient
    import web.commercial_app as ca
    importlib.reload(ca)
    return {"c": TestClient(ca.commercial_app), **env,
            "adm": {"X-Commercial-Key": "adm"}}


def test_enroll_flow_over_http(api):
    c, tid, sid, adm = api["c"], api["tid"], api["sid"], api["adm"]
    tok = c.post("/instances/enroll-token", headers=adm,
                 json={"tenant_id": tid, "subscription_id": sid}).json()["token"]
    # enroll uses the token, NOT the admin key
    res = c.post("/instances/enroll", json={"token": tok, "name": "edge-1"})
    assert res.status_code == 200
    cred = res.json()["credential"]

    ikey = {"X-Instance-Key": cred}
    ent = c.get("/instance/entitlements", headers=ikey).json()
    assert ent["tenant_id"] == tid and ent["subscription_id"] == sid
    assert any(e["code"] == "feature.core" for e in ent["entitlements"])

    # instance can report usage; tenant is derived from the credential
    r = c.post("/instance/usage/batch", headers=ikey,
               json={"events": [{"meter_code": "API_CALLS", "quantity": 3,
                                 "idempotency_key": "e1"}]})
    assert r.status_code == 200 and r.json()["created"] == 1

    # no / bad instance key => 401
    assert c.get("/instance/entitlements").status_code == 401
    assert c.get("/instance/entitlements",
                 headers={"X-Instance-Key": "ins_bogus"}).status_code == 401


def test_enroll_with_bad_token_over_http_is_rejected(api):
    r = api["c"].post("/instances/enroll", json={"token": "enr_bad"})
    assert r.status_code == 403          # TenantAccessDenied -> 403, no oracle

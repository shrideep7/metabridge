"""Phase 4 — pricing & billing HTTP surface (/commercial).

Drives the endpoints end to end via FastAPI TestClient: rate-card setup,
rating, invoicing, payment; the two-key segregation-of-duties override flow;
and a signature-verified provider webhook. Also asserts money serializes as an
exact string (never a float) across the wire.
"""
import hashlib
import hmac
import importlib
from datetime import timedelta

import pytest

from metabridge_control import (billing_providers as bp, catalog, db, metering,
                                 schema, subscriptions as S, tenancy)
from metabridge_control.context import staff_context
from metabridge_control.migrations import runner


@pytest.fixture()
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTROLPLANE_ADMIN_KEY", "adm")
    monkeypatch.setenv("CONTROLPLANE_FINANCE_KEY", "fin")
    monkeypatch.setenv("CONTROLPLANE_DATABASE_URL",
                       "sqlite:///" + str(tmp_path / "api.db"))
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
    sid = S.create_subscription(eng, sctx, account_id=acct, plan_version_id=ver,
                                billing_period="MONTHLY")
    S.activate(eng, sctx, sid)
    metering.record_usage(eng, tenant_id=tid, meter_code="API_CALLS",
                          quantity=1000, idempotency_key="u1",
                          subscription_id=sid)
    import web.commercial_app as ca
    importlib.reload(ca)
    client = TestClient(ca.commercial_app)
    now = schema.utcnow()
    return {"c": client, "tid": tid, "sid": sid,
            "adm": {"X-Commercial-Key": "adm"},
            "fin": {"X-Finance-Key": "fin"},
            "win": {"period_start": (now - timedelta(days=1)).isoformat(),
                    "period_end": (now + timedelta(days=1)).isoformat()}}


def _book(api, unit="0.0100", floor=None, code="std"):
    c, h = api["c"], api["adm"]
    pid = c.post("/pricing/price-books", headers=h,
                 json={"code": code, "name": code}).json()["price_book_id"]
    c.post(f"/pricing/price-books/{pid}/entries", headers=h,
           json={"meter_code": "API_CALLS", "unit_amount": unit,
                 "floor_price": floor})
    c.post(f"/pricing/price-books/{pid}/activate", headers=h)
    return pid


def test_rate_invoice_pay_flow_money_is_string(api):
    c, h, tid, sid = api["c"], api["adm"], api["tid"], api["sid"]
    pid = _book(api)
    r = c.post("/pricing/rate", headers=h,
               json={"tenant_id": tid, "subscription_id": sid,
                     "price_book_id": pid, **api["win"]})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "COMPLETE"
    assert body["total"] == "10.0000"           # exact string, not a float
    assert isinstance(body["total"], str)

    inv = c.post("/billing/invoices", headers=h,
                 json={"tenant_id": tid,
                       "rating_run_id": body["rating_run_id"]}).json()
    assert inv["total"] == "10.0000" and inv["created"] is True
    iid = inv["invoice_id"]

    pay = c.post(f"/billing/invoices/{iid}/payments", headers=h,
                 json={"tenant_id": tid, "amount": "10.0000",
                       "idempotency_key": "p1"}).json()
    assert pay["invoice_state"] == "PAID"
    got = c.get(f"/billing/invoices/{iid}?tenant_id={tid}", headers=h).json()
    assert got["invoice"]["state"] == "PAID"
    assert got["invoice"]["total"] == "10.0000"


def test_floor_failure_is_409(api):
    c, h, tid, sid = api["c"], api["adm"], api["tid"], api["sid"]
    pid = _book(api, unit="0.0100", floor="0.0200", code="floor")
    r = c.post("/pricing/rate", headers=h,
               json={"tenant_id": tid, "subscription_id": sid,
                     "price_book_id": pid, **api["win"]})
    assert r.status_code == 409
    assert r.json()["error"] == "RatingError"


def test_override_requires_finance_key_sod(api):
    c, adm, fin, tid, sid = (api["c"], api["adm"], api["fin"], api["tid"],
                             api["sid"])
    oid = c.post("/pricing/overrides", headers=adm,
                 json={"tenant_id": tid, "subscription_id": sid,
                       "meter_code": "API_CALLS", "floor_price": "0.0200",
                       "proposed_price": "0.0100",
                       "justification": "logo deal"}).json()["override_id"]
    # commercial key cannot approve — the finance header is required
    assert c.post(f"/pricing/overrides/{oid}/approve", headers=adm,
                  json={"tenant_id": tid}).status_code == 401
    # finance key approves
    assert c.post(f"/pricing/overrides/{oid}/approve", headers=fin,
                  json={"tenant_id": tid}).status_code == 200
    # now rating clears the floor
    pid = _book(api, unit="0.0100", floor="0.0200", code="ovr")
    r = c.post("/pricing/rate", headers=adm,
               json={"tenant_id": tid, "subscription_id": sid,
                     "price_book_id": pid, **api["win"]})
    assert r.status_code == 200 and r.json()["total"] == "10.0000"


def test_webhook_signature_verified_records_payment(api, monkeypatch):
    c, h, tid = api["c"], api["adm"], api["tid"]
    secret = "whsec"
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", secret)
    pid = _book(api, code="wh")
    run = c.post("/pricing/rate", headers=h,
                 json={"tenant_id": tid, "subscription_id": api["sid"],
                       "price_book_id": pid, **api["win"]}).json()
    iid = c.post("/billing/invoices", headers=h,
                 json={"tenant_id": tid,
                       "rating_run_id": run["rating_run_id"]}).json()["invoice_id"]
    payload = {"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": "pay_1", "amount": 1000, "currency": "USD",
        "notes": {"invoice_id": iid}}}}}
    raw = bp.canonical_body(payload)
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    ok = c.post("/billing/webhooks/razorpay", content=raw,
                headers={"X-Razorpay-Signature": sig,
                         "Content-Type": "application/json"})
    assert ok.status_code == 200 and ok.json()["status"] == "PAYMENT_RECORDED"
    assert ok.json()["invoice_state"] == "PAID"
    # a forged signature is rejected fail-closed
    bad = c.post("/billing/webhooks/razorpay", content=raw,
                 headers={"X-Razorpay-Signature": "deadbeef",
                          "Content-Type": "application/json"})
    assert bad.status_code == 400

"""Commercial administration API + UI (control plane).

A self-contained FastAPI sub-application mounted at ``/commercial`` by the main
web app. It has its OWN authentication (a staff admin key) and never relies on
the product's customer-facing access guard, keeping the two planes cleanly
separated. If ``CONTROLPLANE_ADMIN_KEY`` is unset the whole surface fails
closed (503) — it is never open by default.

Scope: Phase 2 commercial lifecycle — tenants, accounts, contracts,
subscriptions (+plan assignment, state machine, trials), entitlements,
audited overrides, licenses, and the entitlement gate; Phase 3 usage & metering;
Phase 4 pricing/rating (rate cards, discounts, SoD overrides, the rating
waterfall) and billing (invoices, payments, credit notes, dunning, provider
webhooks). No partner/commission or marketplace endpoints (later phases).

Segregation of duties surfaces at the API boundary: a below-floor override is
*requested* with the commercial admin key and *approved* with a separate
finance key (``CONTROLPLANE_FINANCE_KEY``). Provider webhooks are the one
unauthenticated-by-key surface — they are authenticated by HMAC signature
instead (verified fail-closed inside ``billing.ingest_webhook``).
"""
from __future__ import annotations

import json as _json
import os
from pathlib import Path as _Path
from decimal import Decimal as _Decimal
from typing import Optional

from datetime import datetime, timedelta

from fastapi import (Body, Depends, FastAPI, Header, HTTPException, Path, Query,
                     Request)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from metabridge_control import db, licensing, subscriptions as subs
from metabridge_control import (ai_cost, billing, customer_success as cs,
                                enrollment, entitlements as ent, enforcement,
                                metering, partners, pricing, tenancy)
from metabridge_control.context import staff_context
from metabridge_control.errors import (ControlPlaneError, NotFoundError,
                                        PermissionDenied, PlanImmutableError,
                                        RatingError, TenantAccessDenied,
                                        ValidationError)

commercial_app = FastAPI(title="MetaBridge Commercial Admin", docs_url="/api")

_STAFF_ROLE = "COMMERCIAL_ADMIN"
_ACTOR = "commercial-admin-key"


def _engine():
    return db.get_engine()


def _configured_key() -> str:
    """The admin key from env, or a 0600 key file in the data dir (operator-
    provisioned — same pattern as the audit/license keys). Never auto-generated,
    so an unconfigured deployment fails closed rather than opening with an
    unknown key."""
    env = os.environ.get("CONTROLPLANE_ADMIN_KEY", "")
    if env:
        return env
    kf = _Path(os.environ.get("METABRIDGE_DATA_DIR",
                              str(_Path.home() / ".metabridge"))) \
        / "controlplane_admin.key"
    if kf.exists():
        return kf.read_text(encoding="utf-8").strip()
    return ""


def require_key(x_commercial_key: str = Header(default="")) -> str:
    configured = _configured_key()
    if not configured:
        raise HTTPException(503, "commercial admin is not configured "
                                 "(set CONTROLPLANE_ADMIN_KEY or the "
                                 "controlplane_admin.key file)")
    if not x_commercial_key or x_commercial_key != configured:
        raise HTTPException(401, "invalid or missing X-Commercial-Key")
    return _ACTOR


def _ctx(tenant_id: str):
    return staff_context(_STAFF_ROLE, _ACTOR, tenant_id=tenant_id)


# A distinct finance identity/key so a below-floor override is approved by a
# DIFFERENT actor than the one who requested it — segregation of duties made
# structural at the API edge (the requester only ever holds the commercial key).
_FINANCE_ROLE = "FINANCE_ADMIN"
_FINANCE_ACTOR = "finance-admin-key"


def _finance_key() -> str:
    env = os.environ.get("CONTROLPLANE_FINANCE_KEY", "")
    if env:
        return env
    kf = _Path(os.environ.get("METABRIDGE_DATA_DIR",
                              str(_Path.home() / ".metabridge"))) \
        / "controlplane_finance.key"
    if kf.exists():
        return kf.read_text(encoding="utf-8").strip()
    return ""


def require_finance_key(x_finance_key: str = Header(default="")) -> str:
    configured = _finance_key()
    if not configured:
        raise HTTPException(503, "finance approver is not configured (set "
                                 "CONTROLPLANE_FINANCE_KEY or the "
                                 "controlplane_finance.key file) — segregation "
                                 "of duties requires a second key")
    if not x_finance_key or x_finance_key != configured:
        raise HTTPException(401, "invalid or missing X-Finance-Key")
    return _FINANCE_ACTOR


def _fctx(tenant_id: str):
    return staff_context(_FINANCE_ROLE, _FINANCE_ACTOR, tenant_id=tenant_id)


def _encode(obj):
    """Recursively stringify Decimal so money serializes exactly (never as a
    float) in JSON responses; datetimes are left to FastAPI's encoder."""
    if isinstance(obj, _Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    return obj


@commercial_app.exception_handler(ControlPlaneError)
async def _cp_error(_req, exc: ControlPlaneError):
    status = {ValidationError: 400, PlanImmutableError: 409,
              PermissionDenied: 403, TenantAccessDenied: 403,
              NotFoundError: 404, RatingError: 409}.get(type(exc), 400)
    return JSONResponse({"error": type(exc).__name__, "detail": str(exc)},
                        status_code=status)


# ------------------------------------------------------------------ health
@commercial_app.get("/health")
def health():
    return {"status": "ok", "configured": bool(_configured_key())}


# ------------------------------------------------------------------ tenants
@commercial_app.get("/tenants")
def list_tenants(_actor: str = Depends(require_key)):
    from sqlalchemy import select
    from metabridge_control import schema
    with _engine().connect() as conn:
        rows = conn.execute(select(schema.tenants)
                            .order_by(schema.tenants.c.slug)).mappings().all()
    return {"tenants": [dict(r) for r in rows]}


@commercial_app.post("/tenants")
def create_tenant(actor: str = Depends(require_key), body: dict = Body(...)):
    ctx = staff_context(_STAFF_ROLE, actor)   # no tenant scope for creation
    tid = tenancy.create_tenant(
        _engine(), slug=body["slug"], legal_name=body["legal_name"],
        home_region=body.get("home_region"), staff_ctx=ctx)
    return {"tenant_id": tid}


# ---------------------------------------------------------- accounts/contracts
@commercial_app.post("/accounts")
def create_account(actor: str = Depends(require_key), body: dict = Body(...)):
    aid = subs.create_customer_account(
        _engine(), _ctx(body["tenant_id"]), name=body["name"],
        billing_email=body.get("billing_email"),
        currency=body.get("currency"))
    return {"account_id": aid}


@commercial_app.post("/contracts")
def create_contract(actor: str = Depends(require_key), body: dict = Body(...)):
    cid = subs.create_contract(
        _engine(), _ctx(body["tenant_id"]), body["account_id"],
        purchase_order_ref=body.get("purchase_order_ref"),
        payment_terms_days=body.get("payment_terms_days"),
        governing_law=body.get("governing_law"),
        document_refs=body.get("document_refs"))
    return {"contract_id": cid}


# -------------------------------------------------------------- subscriptions
@commercial_app.post("/subscriptions")
def create_subscription(actor: str = Depends(require_key),
                        body: dict = Body(...)):
    sid = subs.create_subscription(
        _engine(), _ctx(body["tenant_id"]), account_id=body["account_id"],
        plan_version_id=body["plan_version_id"],
        contract_id=body.get("contract_id"),
        billing_period=body.get("billing_period", "ANNUAL"))
    return {"subscription_id": sid}


_ACTIONS = {
    "activate": lambda e, c, s, r: subs.activate(e, c, s, reason=r),
    "trial": lambda e, c, s, r: subs.start_trial(e, c, s, reason=r),
    "convert": lambda e, c, s, r: subs.convert_trial(e, c, s, reason=r),
    "past_due": lambda e, c, s, r: subs.mark_past_due(e, c, s, reason=r),
    "suspend": lambda e, c, s, r: subs.suspend(e, c, s, reason=r),
    "reinstate": lambda e, c, s, r: subs.reinstate(e, c, s, reason=r),
    "cancel": lambda e, c, s, r: subs.cancel(e, c, s, reason=r),
    "rescind": lambda e, c, s, r: subs.rescind_cancellation(e, c, s, reason=r),
    "expire": lambda e, c, s, r: subs.expire(e, c, s, reason=r),
    "terminate": lambda e, c, s, r: subs.terminate(e, c, s, reason=r),
}


@commercial_app.post("/subscriptions/{sid}/transition")
def transition(sid: str = Path(...), actor: str = Depends(require_key),
               body: dict = Body(...)):
    action = body.get("action", "")
    if action not in _ACTIONS:
        raise HTTPException(400, f"unknown action: {action}")
    _ACTIONS[action](_engine(), _ctx(body["tenant_id"]), sid,
                     body.get("reason", action))
    return {"ok": True, "action": action}


@commercial_app.post("/subscriptions/{sid}/items")
def add_item(sid: str = Path(...), actor: str = Depends(require_key),
             body: dict = Body(...)):
    iid = subs.add_item(_engine(), _ctx(body["tenant_id"]), sid,
                        kind=body["kind"], ref_code=body["ref_code"],
                        quantity=int(body["quantity"]),
                        committed_quantity=body.get("committed_quantity"))
    return {"item_id": iid}


@commercial_app.get("/subscriptions/{sid}")
def get_subscription(sid: str = Path(...), tenant_id: str = "",
                     _actor: str = Depends(require_key)):
    return subs.get_subscription(_engine(), _ctx(tenant_id), sid)


@commercial_app.get("/subscriptions/{sid}/entitlements")
def get_entitlements(sid: str = Path(...), _actor: str = Depends(require_key)):
    return {"entitlements": ent.entitlements_for(_engine(), sid)}


@commercial_app.post("/subscriptions/{sid}/overrides")
def add_override(sid: str = Path(...), actor: str = Depends(require_key),
                 body: dict = Body(...)):
    oid = subs.add_override(
        _engine(), _ctx(body["tenant_id"]), sid, code=body["code"],
        value=body["value"], reason=body["reason"],
        value_kind=body.get("value_kind"), period=body.get("period"))
    return {"override_id": oid}


# -------------------------------------------------------------------- licenses
@commercial_app.post("/licenses/issue")
def issue_license(actor: str = Depends(require_key), body: dict = Body(...)):
    lid = licensing.issue_license(
        _engine(), _ctx(body["tenant_id"]), body["subscription_id"],
        instance_id=body.get("instance_id"),
        offline_grace_days=int(body.get("offline_grace_days", 7)))
    return {"license_id": lid}


@commercial_app.post("/licenses/{lid}/file")
def license_file(lid: str = Path(...), actor: str = Depends(require_key),
                 body: dict = Body(...)):
    return licensing.generate_license_file(_engine(), _ctx(body["tenant_id"]),
                                           lid)


@commercial_app.post("/licenses/{lid}/revoke")
def revoke_license(lid: str = Path(...), actor: str = Depends(require_key),
                   body: dict = Body(...)):
    licensing.revoke_license(_engine(), _ctx(body["tenant_id"]), lid,
                             reason=body["reason"])
    return {"ok": True}


# --------------------------------------------------------------- entitlement gate
@commercial_app.post("/access/check")
def access_check(actor: str = Depends(require_key), body: dict = Body(...)):
    resource = body.get("resource")
    if resource:
        d = enforcement.check(_engine(), tenant_id=body["tenant_id"],
                              resource=resource,
                              current_usage=int(body.get("current_usage", 0)),
                              quantity=int(body.get("quantity", 1)),
                              principal=actor)
    else:
        d = ent.check_access(_engine(), tenant_id=body["tenant_id"],
                             code=body["code"],
                             quantity=int(body.get("quantity", 1)),
                             current_usage=int(body.get("current_usage", 0)),
                             principal=actor)
    return {"decision": d.decision, "reason_code": d.reason_code,
            "remaining": d.remaining, "limit": d.limit,
            "subscription_id": d.subscription_id, "extra": d.extra}


# ------------------------------------------------------------- usage & metering
def _period(body_or_days) -> tuple:
    """Resolve a [start, end) window. Accepts explicit ISO strings or a
    trailing-days integer (default 30)."""
    if isinstance(body_or_days, dict) and body_or_days.get("period_start"):
        start = datetime.fromisoformat(body_or_days["period_start"])
        end = datetime.fromisoformat(body_or_days["period_end"]) \
            if body_or_days.get("period_end") else datetime.utcnow()
        return start, end
    days = int((body_or_days or {}).get("days", 30)) if isinstance(
        body_or_days, dict) else int(body_or_days or 30)
    end = datetime.utcnow()
    return end - timedelta(days=days), end


@commercial_app.get("/usage/meters")
def usage_meters(_actor: str = Depends(require_key)):
    return {"meters": metering.list_meters(_engine())}


@commercial_app.post("/usage/events")
def record_usage(actor: str = Depends(require_key), body: dict = Body(...)):
    _ctx(body["tenant_id"]).require("usage:write")
    return metering.record_usage(
        _engine(), tenant_id=body["tenant_id"], meter_code=body["meter_code"],
        quantity=int(body.get("quantity", 1)),
        idempotency_key=body["idempotency_key"],
        source=body.get("source", "CONTROL_PLANE"),
        dimensions=body.get("dimensions"),
        subscription_id=body.get("subscription_id"),
        instance_id=body.get("instance_id"),
        workspace_id=body.get("workspace_id"),
        environment_id=body.get("environment_id"), actor=actor)


@commercial_app.post("/usage/batch")
def ingest_usage_batch(actor: str = Depends(require_key), body: dict = Body(...)):
    _ctx(body["tenant_id"]).require("usage:ingest")
    return metering.ingest_batch(
        _engine(), tenant_id=body["tenant_id"], events=body["events"],
        source=body.get("source", "INSTANCE_BATCH"), actor=actor)


@commercial_app.post("/usage/adjust")
def adjust_usage(actor: str = Depends(require_key), body: dict = Body(...)):
    aid = metering.adjust_usage(
        _engine(), _ctx(body["tenant_id"]), body["original_event_id"],
        quantity_delta=int(body["quantity_delta"]),
        reason_code=body["reason_code"], evidence_ref=body.get("evidence_ref"))
    return {"adjustment_id": aid}


@commercial_app.post("/usage/aggregate")
def run_aggregate(actor: str = Depends(require_key), body: dict = Body(...)):
    _ctx(body["tenant_id"]).require("usage:write")
    start, end = _period(body)
    return metering.aggregate(
        _engine(), tenant_id=body["tenant_id"], meter_code=body["meter_code"],
        period_start=start, period_end=end,
        level=body.get("level", "BILLING_PERIOD"),
        finalize=bool(body.get("finalize", False)), actor=actor)


@commercial_app.get("/usage/summary")
def usage_summary(tenant_id: str = Query(...), days: int = Query(30),
                  _actor: str = Depends(require_key)):
    _ctx(tenant_id).require("usage:read")
    start, end = _period({"days": days})
    return metering.usage_summary(_engine(), tenant_id=tenant_id,
                                  period_start=start, period_end=end)


@commercial_app.get("/usage/overage")
def usage_overage(tenant_id: str = Query(...), days: int = Query(30),
                  _actor: str = Depends(require_key)):
    _ctx(tenant_id).require("usage:read")
    start, end = _period({"days": days})
    return metering.overage_report(_engine(), tenant_id=tenant_id,
                                   period_start=start, period_end=end)


@commercial_app.get("/usage/statement")
def usage_statement(tenant_id: str = Query(...), days: int = Query(30),
                    subscription_id: str = Query(""),
                    fmt: str = Query("json", alias="format"),
                    _actor: str = Depends(require_key)):
    _ctx(tenant_id).require("usage:read")
    start, end = _period({"days": days})
    stmt = metering.usage_statement(
        _engine(), tenant_id=tenant_id, period_start=start, period_end=end,
        subscription_id=subscription_id or None)
    if fmt == "csv":
        return PlainTextResponse(metering.statement_csv(stmt),
                                 media_type="text/csv")
    return stmt


@commercial_app.post("/usage/statements/ingest")
def ingest_statement(actor: str = Depends(require_key), body: dict = Body(...)):
    _ctx(body.get("payload", {}).get("tenant_id", "*")).require("usage:ingest")
    # trust anchor is server-pinned inside metering — never caller-supplied.
    return metering.ingest_signed_statement(
        _engine(), payload=body["payload"], signature=body["signature"],
        actor=actor)


@commercial_app.get("/audit/{tenant_id}")
def get_audit(tenant_id: str = Path(...), _actor: str = Depends(require_key)):
    from sqlalchemy import select
    from metabridge_control import audit, schema
    with _engine().connect() as conn:
        rows = conn.execute(select(schema.audit_events)
                            .where(schema.audit_events.c.tenant_id == tenant_id)
                            .order_by(schema.audit_events.c.seq)).mappings().all()
        ok, bad = audit.verify_chain(conn, tenant_id)
    return {"chain_ok": ok, "first_bad_seq": bad,
            "events": [dict(r) for r in rows]}


# =============================================================== pricing (§8.1)
@commercial_app.post("/pricing/price-books")
def create_price_book(actor: str = Depends(require_key), body: dict = Body(...)):
    pid = pricing.create_price_book(
        _engine(), staff_context(_STAFF_ROLE, actor), code=body["code"],
        name=body["name"], currency=body.get("currency", "USD"),
        region=body.get("region"), version=int(body.get("version", 1)))
    return {"price_book_id": pid}


@commercial_app.post("/pricing/price-books/{pid}/entries")
def add_price_entry(pid: str = Path(...), actor: str = Depends(require_key),
                    body: dict = Body(...)):
    eid = pricing.add_price_entry(
        _engine(), staff_context(_STAFF_ROLE, actor), pid,
        meter_code=body["meter_code"],
        pricing_model=body.get("pricing_model", "PER_UNIT"),
        unit_amount=body.get("unit_amount", "0"), tiers=body.get("tiers"),
        floor_price=body.get("floor_price"), min_commit=body.get("min_commit"),
        included_quantity=int(body.get("included_quantity", 0)))
    return {"entry_id": eid}


@commercial_app.post("/pricing/price-books/{pid}/activate")
def activate_price_book(pid: str = Path(...), actor: str = Depends(require_key)):
    pricing.activate_price_book(_engine(), staff_context(_STAFF_ROLE, actor),
                                pid)
    return {"ok": True}


@commercial_app.post("/pricing/discounts")
def create_discount(actor: str = Depends(require_key), body: dict = Body(...)):
    did = pricing.create_discount_rule(
        _engine(), staff_context(_STAFF_ROLE, actor), code=body["code"],
        name=body["name"], percent=int(body["percent"]),
        priority=int(body.get("priority", 100)),
        exclusive=bool(body.get("exclusive", False)),
        max_percent=int(body.get("max_percent", 100)),
        applies_to=body.get("applies_to"), tenant_id=body.get("tenant_id"))
    return {"discount_rule_id": did}


@commercial_app.post("/pricing/overrides")
def request_override(actor: str = Depends(require_key), body: dict = Body(...)):
    """Request a below-floor price override (commercial key). Approval is a
    separate call with the finance key — the requester cannot approve it."""
    oid = pricing.request_price_override(
        _engine(), _ctx(body["tenant_id"]),
        subscription_id=body["subscription_id"], meter_code=body["meter_code"],
        floor_price=body["floor_price"], proposed_price=body["proposed_price"],
        justification=body["justification"])
    return {"override_id": oid}


@commercial_app.post("/pricing/overrides/{oid}/approve")
def approve_override(oid: str = Path(...),
                     actor: str = Depends(require_finance_key),
                     body: dict = Body(default={})):
    """Approve/reject a below-floor override — FINANCE key only (SoD)."""
    pricing.approve_price_override(
        _engine(), _fctx((body or {}).get("tenant_id", "*")), oid,
        approve=bool((body or {}).get("approve", True)))
    return {"ok": True}


@commercial_app.post("/pricing/rate")
def rate_subscription(actor: str = Depends(require_key), body: dict = Body(...)):
    """Run the deterministic rating waterfall for a subscription/period. A
    below-floor line with no approved override fails closed (409)."""
    start, end = _period(body)
    return _encode(pricing.rate_subscription(
        _engine(), _ctx(body["tenant_id"]),
        subscription_id=body["subscription_id"],
        price_book_id=body["price_book_id"], period_start=start,
        period_end=end))


@commercial_app.get("/pricing/rating-runs/{rid}")
def get_rating_run(rid: str = Path(...), tenant_id: str = Query("*"),
                   _actor: str = Depends(require_key)):
    return _encode(pricing.get_rating_run(_engine(), _ctx(tenant_id), rid))


# =============================================================== billing (§8.2)
@commercial_app.post("/billing/accounts")
def create_billing_account(actor: str = Depends(require_key),
                           body: dict = Body(...)):
    aid = billing.create_billing_account(
        _engine(), _ctx(body["tenant_id"]), tenant_id=body["tenant_id"],
        provider=body.get("provider", "MANUAL"),
        external_customer_ref=body.get("external_customer_ref"),
        payment_terms_days=int(body.get("payment_terms_days", 30)),
        currency=body.get("currency", "USD"), tax_ids=body.get("tax_ids"))
    return {"billing_account_id": aid}


@commercial_app.post("/billing/invoices")
def issue_invoice(actor: str = Depends(require_key), body: dict = Body(...)):
    return _encode(billing.issue_invoice(
        _engine(), _ctx(body.get("tenant_id", "*")),
        rating_run_id=body["rating_run_id"],
        billing_account_id=body.get("billing_account_id"),
        due_days=body.get("due_days")))


@commercial_app.get("/billing/invoices/{iid}")
def get_invoice(iid: str = Path(...), tenant_id: str = Query("*"),
                _actor: str = Depends(require_key)):
    return _encode(billing.get_invoice(_engine(), _ctx(tenant_id), iid))


@commercial_app.post("/billing/invoices/{iid}/void")
def void_invoice(iid: str = Path(...), actor: str = Depends(require_key),
                 body: dict = Body(default={})):
    billing.void_invoice(_engine(), _ctx((body or {}).get("tenant_id", "*")),
                         iid, reason=(body or {}).get("reason", ""))
    return {"ok": True}


@commercial_app.post("/billing/invoices/{iid}/payments")
def record_payment(iid: str = Path(...), actor: str = Depends(require_key),
                   body: dict = Body(...)):
    return _encode(billing.record_payment(
        _engine(), _ctx(body.get("tenant_id", "*")), invoice_id=iid,
        amount=body["amount"], idempotency_key=body["idempotency_key"],
        provider=body.get("provider", "MANUAL"),
        provider_ref=body.get("provider_ref"), method=body.get("method")))


@commercial_app.post("/billing/invoices/{iid}/credit-notes")
def issue_credit_note(iid: str = Path(...), actor: str = Depends(require_key),
                      body: dict = Body(...)):
    cid = billing.issue_credit_note(
        _engine(), _ctx(body.get("tenant_id", "*")), invoice_id=iid,
        amount=body["amount"], reason=body["reason"])
    return {"credit_note_id": cid}


@commercial_app.post("/billing/invoices/{iid}/dunning")
def record_dunning(iid: str = Path(...), actor: str = Depends(require_key),
                   body: dict = Body(default={})):
    did = billing.record_dunning_attempt(
        _engine(), _ctx((body or {}).get("tenant_id", "*")), invoice_id=iid,
        step=int((body or {}).get("step", 1)),
        channel=(body or {}).get("channel", "EMAIL"),
        outcome=(body or {}).get("outcome"))
    return {"dunning_id": did}


@commercial_app.post("/billing/overdue-sweep")
def overdue_sweep(actor: str = Depends(require_key), body: dict = Body(default={})):
    n = billing.mark_overdue(_engine(),
                             _ctx((body or {}).get("tenant_id", "*")))
    return {"marked_overdue": n}


@commercial_app.post("/billing/webhooks/{provider}")
async def billing_webhook(
        provider: str = Path(...),
        stripe_signature: str = Header(default="", alias="Stripe-Signature"),
        x_razorpay_signature: str = Header(
            default="", alias="X-Razorpay-Signature"),
        request: Request = None):
    """Inbound provider webhook — NOT key-authenticated; authenticated by HMAC
    signature over the RAW body (verified fail-closed). Idempotent per event."""
    raw = await request.body()
    try:
        payload = _json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "invalid JSON body")
    signature = stripe_signature or x_razorpay_signature
    return billing.ingest_webhook(_engine(), provider=provider,
                                  payload=payload, signature=signature,
                                  raw_body=raw)


# ========================================================= instance enrollment
# Staff-mediated token minting + instance management (admin key).
@commercial_app.post("/instances/enroll-token")
def create_enroll_token(actor: str = Depends(require_key), body: dict = Body(...)):
    return enrollment.create_enrollment_token(
        _engine(), _ctx(body["tenant_id"]), tenant_id=body["tenant_id"],
        workspace_id=body.get("workspace_id"),
        environment_id=body.get("environment_id"),
        delivery_model=body.get("delivery_model", "MODEL_B_CONNECTED"),
        subscription_id=body.get("subscription_id"),
        ttl_hours=int(body.get("ttl_hours", 72)))


@commercial_app.get("/instances")
def list_instances(tenant_id: str = Query(...), _actor: str = Depends(require_key)):
    return {"instances": enrollment.list_instances(_engine(), _ctx(tenant_id),
                                                    tenant_id)}


@commercial_app.post("/instances/{iid}/register-key")
def register_instance_key(iid: str = Path(...), actor: str = Depends(require_key),
                          body: dict = Body(...)):
    enrollment.register_public_key(
        _engine(), _ctx(body.get("tenant_id", "*")), iid,
        public_key=body["public_key"])
    return {"ok": True}


@commercial_app.post("/instances/{iid}/revoke")
def revoke_instance(iid: str = Path(...), actor: str = Depends(require_key),
                    body: dict = Body(...)):
    enrollment.revoke_instance(_engine(), _ctx(body.get("tenant_id", "*")), iid,
                               reason=body["reason"])
    return {"ok": True}


# Instance bootstrap — authenticated by the one-time enrollment token itself
# (NOT the admin key). Returns the long-lived instance credential once.
@commercial_app.post("/instances/enroll")
def enroll_instance(body: dict = Body(...)):
    return enrollment.enroll_instance(
        _engine(), token=body["token"], name=body.get("name", ""),
        public_key=body.get("public_key"), fingerprint=body.get("fingerprint"))


def require_instance(x_instance_key: str = Header(default="")) -> dict:
    """Resolve an instance credential to its identity (tenant derived from the
    credential, never the request). 401 on any failure — no instance oracle."""
    try:
        return enrollment.authenticate_instance(_engine(), x_instance_key)
    except TenantAccessDenied:
        raise HTTPException(401, "invalid or missing X-Instance-Key")


# Instance-authenticated data-plane surface (X-Instance-Key).
@commercial_app.get("/instance/entitlements")
def instance_entitlements(identity: dict = Depends(require_instance)):
    from sqlalchemy import select
    from metabridge_control import schema
    sid = identity["subscription_id"]
    if not sid:
        with _engine().connect() as conn:
            sub = ent._active_subscription(conn, identity["tenant_id"])
            sid = sub["id"] if sub else None
    ents = ent.entitlements_for(_engine(), sid) if sid else []
    return {"instance_id": identity["instance_id"],
            "tenant_id": identity["tenant_id"], "subscription_id": sid,
            "entitlements": ents}


@commercial_app.post("/instance/usage/batch")
def instance_usage_batch(identity: dict = Depends(require_instance),
                         body: dict = Body(...)):
    return metering.ingest_batch(
        _engine(), tenant_id=identity["tenant_id"], events=body["events"],
        source="INSTANCE_BATCH", expected_tenant=identity["tenant_id"],
        actor="instance:" + identity["instance_id"])


@commercial_app.post("/instance/statements/ingest")
def instance_statement_ingest(identity: dict = Depends(require_instance),
                              body: dict = Body(...)):
    # tenant is derived from the credential; a payload claiming another tenant
    # is rejected by metering (it verifies the tenant exists) and by the pinned
    # per-instance trust key resolved server-side.
    payload = dict(body.get("payload", {}))
    payload.setdefault("tenant_id", identity["tenant_id"])
    payload.setdefault("instance_id", identity["instance_id"])
    if payload.get("tenant_id") != identity["tenant_id"]:
        raise HTTPException(403, "statement tenant does not match instance")
    return metering.ingest_signed_statement(
        _engine(), payload=payload, signature=body["signature"],
        actor="instance:" + identity["instance_id"])


@commercial_app.get("/instance/heartbeat")
def instance_heartbeat(identity: dict = Depends(require_instance)):
    return {"ok": True, "instance_id": identity["instance_id"],
            "tenant_id": identity["tenant_id"]}


@commercial_app.post("/instance/ai-usage")
def instance_ai_usage(identity: dict = Depends(require_instance),
                      body: dict = Body(...)):
    """An instance reports one model invocation; tenant derived from the
    credential. Rolls into the AI_TOKENS meters + an estimated cost."""
    return _encode(ai_cost.record_ai_usage(
        _engine(), tenant_id=identity["tenant_id"],
        instance_id=identity["instance_id"], provider=body["provider"],
        model_id=body["model_id"], input_tokens=int(body.get("input_tokens", 0)),
        output_tokens=int(body.get("output_tokens", 0)),
        idempotency_key=body["idempotency_key"], purpose=body.get("purpose"),
        job_ref=body.get("job_ref"),
        actor="instance:" + identity["instance_id"]))


# ============================================== AI cost governance (§10)
@commercial_app.post("/ai/rate-cards")
def create_ai_rate_card(actor: str = Depends(require_key), body: dict = Body(...)):
    rid = ai_cost.create_rate_card(
        _engine(), _gctx(actor), provider=body["provider"],
        model_id=body["model_id"], input_rate=body["input_rate"],
        output_rate=body["output_rate"], currency=body.get("currency", "USD"))
    return {"rate_card_id": rid}


@commercial_app.post("/ai/{tenant_id}/budget")
def set_ai_budget(tenant_id: str = Path(...), actor: str = Depends(require_key),
                  body: dict = Body(...)):
    bid = ai_cost.set_ai_budget(
        _engine(), _ctx(tenant_id), tenant_id, scope=body.get("scope", "TENANT"),
        scope_id=body.get("scope_id", "*"), period=body.get("period", "MONTHLY"),
        limit_tokens=body.get("limit_tokens"),
        limit_est_cost=body.get("limit_est_cost"),
        action_on_breach=body.get("action_on_breach", "WARN"),
        currency=body.get("currency", "USD"))
    return {"budget_id": bid}


@commercial_app.get("/ai/{tenant_id}/budget-check")
def ai_budget_check(tenant_id: str = Path(...), days: int = Query(30),
                    _actor: str = Depends(require_key)):
    _ctx(tenant_id).require("ai:read")
    start, end = _period({"days": days})
    return _encode(ai_cost.check_ai_budget(_engine(), tenant_id=tenant_id,
                                           period_start=start, period_end=end))


@commercial_app.get("/ai/{tenant_id}/cost-report")
def ai_cost_report(tenant_id: str = Path(...), days: int = Query(30),
                   _actor: str = Depends(require_key)):
    start, end = _period({"days": days})
    return _encode(ai_cost.ai_cost_report(_engine(), _ctx(tenant_id),
                                          tenant_id=tenant_id,
                                          period_start=start, period_end=end))


# ============================================== partners & commissions (§9)
def _gctx(actor: str):
    """Vendor-global commercial-admin context (partners are vendor-owned)."""
    return staff_context(_STAFF_ROLE, actor)


@commercial_app.post("/partners")
def create_partner(actor: str = Depends(require_key), body: dict = Body(...)):
    pid = partners.create_partner(
        _engine(), _gctx(actor), kind=body["kind"], name=body["name"],
        country_code=body.get("country_code"),
        tier_code=body.get("tier_code", "REGISTERED"),
        partner_tenant_id=body.get("partner_tenant_id"))
    return {"partner_id": pid}


@commercial_app.post("/partners/{pid}/tier")
def assign_partner_tier(pid: str = Path(...), actor: str = Depends(require_key),
                        body: dict = Body(...)):
    partners.assign_tier(_engine(), _gctx(actor), pid,
                         tier_code=body["tier_code"],
                         review_ref=body.get("review_ref"))
    return {"ok": True}


@commercial_app.post("/commission-plans")
def create_commission_plan(actor: str = Depends(require_key),
                           body: dict = Body(...)):
    plan = partners.create_commission_plan(
        _engine(), _gctx(actor), code=body["code"],
        basis=body.get("basis", "FIRST_YEAR"), rate_table=body["rate_table"],
        clawback_window_days=int(body.get("clawback_window_days", 90)))
    return {"commission_plan_id": plan}


@commercial_app.post("/partners/{pid}/agreements")
def create_agreement(pid: str = Path(...), actor: str = Depends(require_key),
                     body: dict = Body(default={})):
    aid = partners.create_agreement(
        _engine(), _gctx(actor), pid,
        commission_plan_id=(body or {}).get("commission_plan_id"))
    return {"agreement_id": aid}


@commercial_app.post("/agreements/{aid}/verify")
def verify_agreement(aid: str = Path(...), actor: str = Depends(require_key),
                     body: dict = Body(default={})):
    partners.verify_agreement(_engine(), _gctx(actor), aid,
                              banking=(body or {}).get("banking"),
                              tax_docs=(body or {}).get("tax_docs"))
    return {"ok": True}


@commercial_app.post("/agreements/{aid}/activate")
def activate_agreement(aid: str = Path(...), actor: str = Depends(require_key)):
    partners.activate_agreement(_engine(), _gctx(actor), aid)
    return {"ok": True}


@commercial_app.post("/partners/{pid}/deals")
def register_deal(pid: str = Path(...), actor: str = Depends(require_key),
                  body: dict = Body(...)):
    did = partners.register_deal(
        _engine(), _gctx(actor), pid, prospect_name=body["prospect_name"],
        estimated_value=body.get("estimated_value"),
        currency=body.get("currency", "USD"),
        customer_tenant_id=body.get("customer_tenant_id"))
    return {"deal_id": did}


@commercial_app.post("/deals/{did}/transition")
def deal_transition(did: str = Path(...), actor: str = Depends(require_key),
                    body: dict = Body(...)):
    action = body.get("action", "")
    g = _gctx(actor)
    if action == "approve":
        partners.approve_deal(_engine(), g, did,
                              protection_days=body.get("protection_days"))
    elif action == "reject":
        partners.reject_deal(_engine(), g, did, reason=body.get("reason", ""))
    elif action == "win":
        partners.win_deal(_engine(), g, did, order_ref=body["order_ref"])
    elif action == "lose":
        partners.lose_deal(_engine(), g, did)
    elif action == "expire":
        partners.expire_deal(_engine(), g, did)
    else:
        raise HTTPException(400, f"unknown deal action: {action}")
    return {"ok": True, "action": action}


@commercial_app.post("/commissions/accrue")
def accrue_commission(actor: str = Depends(require_key), body: dict = Body(...)):
    cid = partners.accrue_commission(
        _engine(), _gctx(actor), partner_id=body["partner_id"],
        agreement_id=body["agreement_id"], basis_amount=body["basis_amount"],
        currency=body.get("currency", "USD"), invoice_id=body.get("invoice_id"),
        deal_id=body.get("deal_id"), order_ref=body.get("order_ref"),
        customer_tenant_id=body.get("customer_tenant_id"))
    return {"commission_id": cid}


# Commission approval / payout / clawback require the FINANCE key (SoD): the
# accruer (commercial admin) is never the approver.
@commercial_app.post("/commissions/{cid}/approve")
def approve_commission(cid: str = Path(...),
                       actor: str = Depends(require_finance_key)):
    partners.approve_commission(_engine(), _fctx("*"), cid)
    return {"ok": True}


@commercial_app.post("/commissions/{cid}/payable")
def commission_payable(cid: str = Path(...),
                       actor: str = Depends(require_finance_key)):
    partners.mark_payable(_engine(), _fctx("*"), cid)
    return {"ok": True}


@commercial_app.post("/partners/{pid}/pay")
def pay_partner(pid: str = Path(...), actor: str = Depends(require_finance_key),
                body: dict = Body(default={})):
    return _encode(partners.pay_partner(_engine(), _fctx("*"), pid,
                                        provider_ref=(body or {}).get("provider_ref")))


@commercial_app.post("/commissions/{cid}/reverse")
def reverse_commission(cid: str = Path(...), actor: str = Depends(require_key),
                       body: dict = Body(...)):
    comp = partners.reverse_commission(_engine(), _gctx(actor), cid,
                                       reason=body["reason"])
    return {"compensating_id": comp}


@commercial_app.post("/commissions/{cid}/clawback")
def clawback_commission(cid: str = Path(...),
                        actor: str = Depends(require_finance_key),
                        body: dict = Body(...)):
    comp = partners.clawback_commission(_engine(), _fctx("*"), cid,
                                        reason=body["reason"])
    return {"compensating_id": comp}


@commercial_app.get("/partners/{pid}/statement")
def partner_statement(pid: str = Path(...), _actor: str = Depends(require_key)):
    return _encode(partners.partner_statement(_engine(), _gctx(_ACTOR), pid))


# ============================================== customer success (§12)
@commercial_app.post("/cs/{tenant_id}/signals")
def cs_signal(tenant_id: str = Path(...), actor: str = Depends(require_key),
              body: dict = Body(...)):
    return cs.record_adoption_signal(
        _engine(), _ctx(tenant_id), tenant_id, signal_code=body["signal_code"],
        instance_id=body.get("instance_id"),
        evidence_ref=body.get("evidence_ref"))


@commercial_app.post("/cs/{tenant_id}/stage")
def cs_stage(tenant_id: str = Path(...), actor: str = Depends(require_key),
             body: dict = Body(...)):
    tid = cs.transition_stage(_engine(), _ctx(tenant_id), tenant_id,
                              to_stage=body["to_stage"],
                              trigger=body.get("trigger", "MANUAL"),
                              reason=body.get("reason"))
    return {"transition_id": tid}


@commercial_app.post("/cs/{tenant_id}/risks")
def cs_open_risk(tenant_id: str = Path(...), actor: str = Depends(require_key),
                 body: dict = Body(...)):
    return cs.open_risk_flag(_engine(), _ctx(tenant_id), tenant_id,
                             code=body["code"],
                             severity=body.get("severity", "MEDIUM"),
                             detail=body.get("detail"),
                             owner_membership_id=body.get("owner_membership_id"))


@commercial_app.post("/cs/risks/{flag_id}/resolve")
def cs_resolve_risk(flag_id: str = Path(...), actor: str = Depends(require_key),
                    body: dict = Body(default={})):
    cs.resolve_risk_flag(_engine(), _ctx((body or {}).get("tenant_id", "*")),
                         flag_id, note=(body or {}).get("note", ""))
    return {"ok": True}


@commercial_app.post("/cs/{tenant_id}/evaluate-risks")
def cs_evaluate_risks(tenant_id: str = Path(...),
                      actor: str = Depends(require_key)):
    return cs.evaluate_risks(_engine(), _ctx(tenant_id), tenant_id)


@commercial_app.post("/cs/{tenant_id}/health")
def cs_health(tenant_id: str = Path(...), actor: str = Depends(require_key)):
    return cs.compute_health(_engine(), _ctx(tenant_id), tenant_id)


@commercial_app.get("/cs/{tenant_id}/overview")
def cs_overview(tenant_id: str = Path(...), _actor: str = Depends(require_key)):
    return cs.cs_overview(_engine(), _ctx(tenant_id), tenant_id)


# --------------------------------------------------------------------- UI
@commercial_app.get("/", response_class=HTMLResponse)
def ui():
    return _UI


_UI = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MetaBridge · Commercial Admin</title><style>
:root{--bg:#070d19;--panel:#101d33;--line:#26364f;--txt:#e8eefb;--mut:#9db0d2;
--blue:#4da3ff;--teal:#3ddad0;--amber:#ffb454;--rose:#ff8095;}
*{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,sans-serif;}
body{margin:0;background:var(--bg);color:var(--txt);}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;}
header b{color:var(--blue);} .key{margin-left:auto;}
input,select,button{background:#0b1526;color:var(--txt);border:1px solid var(--line);
border-radius:8px;padding:8px 10px;font-size:13px;}
button{cursor:pointer;background:linear-gradient(120deg,#2f7bbd,#4da3ff);border:0;font-weight:600;}
button.sec{background:#18263f;}
main{display:grid;grid-template-columns:300px 1fr;gap:0;height:calc(100vh - 53px);}
aside{border-right:1px solid var(--line);overflow:auto;padding:14px;}
section{overflow:auto;padding:18px 22px;}
.t{padding:9px 11px;border:1px solid var(--line);border-radius:9px;margin-bottom:7px;cursor:pointer;font-size:13px;}
.t:hover{border-color:var(--blue);} .t b{color:#fff;} .t span{color:var(--mut);font-size:11px;}
h2{font-size:15px;margin:18px 0 8px;} pre{background:#0b1526;border:1px solid var(--line);
border-radius:10px;padding:12px;overflow:auto;font-size:12px;color:#cfe0ff;}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700;}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0;}
label{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px;}
table{border-collapse:collapse;width:100%;font-size:12.5px;} td,th{border:1px solid var(--line);padding:6px 9px;text-align:left;}
th{color:var(--mut);} .muted{color:var(--mut);font-size:12px;}
</style></head><body>
<header><div style="font-weight:800">Meta<b>Bridge</b> · Commercial Admin</div>
<div class="key"><input id="key" placeholder="X-Commercial-Key" size="30">
<button class="sec" onclick="saveKey()">Use key</button></div></header>
<main>
<aside>
  <div class="row"><b>Tenants</b><button class="sec" style="margin-left:auto" onclick="loadTenants()">↻</button></div>
  <div id="tenants"></div>
</aside>
<section id="detail"><p class="muted">Enter your admin key, then pick a tenant. This console is the control-plane surface — the product data plane is unchanged.</p></section>
</main>
<script>
const $=s=>document.querySelector(s);
function key(){return localStorage.getItem('mbck')||''}
function saveKey(){localStorage.setItem('mbck',$('#key').value.trim());loadTenants();}
async function api(path,opts={}){opts.headers=Object.assign({'X-Commercial-Key':key(),'Content-Type':'application/json'},opts.headers||{});
 const r=await fetch('/commercial'+path,opts);const j=await r.json().catch(()=>({}));if(!r.ok)throw (j.detail||j.error||('HTTP '+r.status));return j;}
async function loadTenants(){try{const j=await api('/tenants');
 $('#tenants').innerHTML=j.tenants.map(t=>`<div class="t" onclick="openTenant('${t.id}','${t.slug}')"><b>${t.slug}</b><br><span>${t.legal_name} · ${t.status}</span></div>`).join('')||'<p class=muted>No tenants</p>';}
 catch(e){$('#tenants').innerHTML='<p class=muted>'+e+'</p>';}}
async function openTenant(tid,slug){
 let sub='';try{const j=await api('/audit/'+tid);sub=`<div class=card><h2>Audit chain</h2><div class="muted">events: ${j.events.length} · chain_ok: <b style="color:${j.chain_ok?'#3ddad0':'#ff8095'}">${j.chain_ok}</b></div></div>`;}catch(e){}
 $('#detail').innerHTML=`<h2>${slug}</h2><div class="muted">${tid}</div>
  <div class=card><h2>Entitlement check</h2>
   <div class=row><input id=sub placeholder="subscription tenant = this"><select id=res>
   ${['users','programs','assessments','objects_assessed','ai_credits','storage_gb','connectors','api_calls','report_exports'].map(r=>`<option>${r}</option>`).join('')}
   </select><input id=cu type=number value=0 style=width:80px placeholder=usage><input id=qty type=number value=1 style=width:70px>
   <button onclick="doCheck('${tid}')">Check</button></div><pre id=chk>—</pre></div>
  <div class=card><div class=row><h2 style="margin:0">Usage &amp; metering (30 days)</h2>
   <button class=sec style="margin-left:auto" onclick="loadUsage('${tid}')">↻</button></div>
   <div id=usage class=muted style="margin-top:8px">loading…</div></div>
  ${sub}`;
 loadUsage(tid);
}
async function doCheck(tid){try{const d=await api('/access/check',{method:'POST',body:JSON.stringify(
 {tenant_id:tid,resource:$('#res').value,current_usage:+$('#cu').value,quantity:+$('#qty').value})});
 $('#chk').textContent=JSON.stringify(d,null,2);}catch(e){$('#chk').textContent=e;}}
async function loadUsage(tid){try{
 const [s,o]=await Promise.all([api('/usage/summary?tenant_id='+tid+'&days=30'),
   api('/usage/overage?tenant_id='+tid+'&days=30').catch(()=>({lines:[]}))]);
 const over={};(o.lines||[]).forEach(l=>over[l.meter_code]=l);
 if(!s.meters.length){$('#usage').innerHTML='<span class=muted>No usage recorded in this window.</span>';return;}
 $('#usage').innerHTML='<table><tr><th>Meter</th><th>Usage</th><th>Unit</th><th>Limit</th><th>Overage</th><th>Billable</th></tr>'+
  s.meters.map(m=>{const ov=over[m.meter_code]||{};return `<tr><td>${m.meter_code}</td><td>${m.quantity}</td><td>${m.unit}</td>`+
   `<td>${ov.limit==null?'—':ov.limit}</td><td style="color:${ov.overage?'#ff8095':'#9db0d2'}">${ov.overage||0}</td>`+
   `<td>${m.billable?'yes':'no'}</td></tr>`;}).join('')+'</table>'+
  `<div class=row style="margin-top:8px"><a class=sec style="padding:8px 10px;border-radius:8px;text-decoration:none" href="/commercial/usage/statement?tenant_id=${tid}&days=30&format=csv&x=1" onclick="return dl(event,'${tid}')">Download statement CSV</a></div>`;
 }catch(e){$('#usage').innerHTML='<span class=muted>'+e+'</span>';}}
function dl(ev,tid){ev.preventDefault();
 fetch('/commercial/usage/statement?tenant_id='+tid+'&days=30&format=csv',{headers:{'X-Commercial-Key':key()}})
  .then(r=>r.text()).then(t=>{const b=new Blob([t],{type:'text/csv'});const u=URL.createObjectURL(b);
   const a=document.createElement('a');a.href=u;a.download='usage-'+tid+'.csv';a.click();URL.revokeObjectURL(u);});
 return false;}
if(key()){$('#key').value=key();loadTenants();}
</script></body></html>"""

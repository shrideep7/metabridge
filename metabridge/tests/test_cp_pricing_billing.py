"""Phase 4 — pricing, rating & billing integration.

Covers: rate-card lifecycle/immutability, the deterministic replayable rating
waterfall (per-unit / included / tiered / volume), discount stacking + cap +
exclusivity, the fail-closed floor check and its segregation-of-duties override,
invoice issuance idempotency, payment idempotency + state transitions, webhook
signature verification (fail-closed) + at-most-once processing, exact money, and
cross-tenant isolation.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from metabridge_control import (billing, billing_providers as bp, catalog, db,
                                 metering, pricing, schema,
                                 subscriptions as S, tenancy)
from metabridge_control.context import staff_context
from metabridge_control.errors import (NotFoundError, PermissionDenied,
                                        RatingError, ValidationError)
from metabridge_control.migrations import runner


def _new_subscription(eng, staff, slug):
    prod = catalog.create_product(eng, staff, code="p_" + slug, name="P")
    plan = catalog.create_plan(eng, staff, prod, code="c", name="C")
    ver = catalog.create_plan_version(eng, staff, plan)
    catalog.create_feature(eng, staff, code=f"feature.{slug}", name="Core",
                           value_kind="BOOLEAN")
    catalog.set_plan_feature(eng, staff, ver, f"feature.{slug}",
                             bool_value=True)
    catalog.publish_plan_version(eng, staff, ver)
    tid = tenancy.create_tenant(eng, slug=slug, legal_name=slug.title(),
                                system=True)
    sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
    acct = S.create_customer_account(eng, sctx, name=slug)
    sid = S.create_subscription(eng, sctx, account_id=acct, plan_version_id=ver,
                                billing_period="MONTHLY")
    S.activate(eng, sctx, sid)
    return tid, sid


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    eng = db.get_engine("sqlite:///" + str(tmp_path / "pb.db"))
    runner.migrate(eng)
    ops = staff_context("COMMERCIAL_ADMIN", "ops")       # requests overrides
    fin = staff_context("FINANCE_ADMIN", "fin")          # approves overrides
    fin2 = staff_context("FINANCE_ADMIN", "fin2")
    tid, sid = _new_subscription(eng, ops, "acme")
    # baseline billable usage: 1000 API_CALLS in the window
    metering.record_usage(eng, tenant_id=tid, meter_code="API_CALLS",
                          quantity=1000, idempotency_key="u1",
                          subscription_id=sid)
    return {"eng": eng, "tid": tid, "sid": sid, "ops": ops, "fin": fin,
            "fin2": fin2}


def _window():
    now = schema.utcnow()
    return now - timedelta(days=1), now + timedelta(days=1)


def _book(eng, ctx, *, unit="0.0100", floor=None, model="PER_UNIT",
          tiers=None, included=0, code="std"):
    pb = pricing.create_price_book(eng, ctx, code=code, name="Std")
    pricing.add_price_entry(eng, ctx, pb, meter_code="API_CALLS",
                            pricing_model=model, unit_amount=unit, tiers=tiers,
                            floor_price=floor, included_quantity=included)
    pricing.activate_price_book(eng, ctx, pb)
    return pb


# --------------------------------------------------------------- rate cards
def test_active_price_book_is_immutable(env):
    e, ops = env["eng"], env["ops"]
    pb = pricing.create_price_book(e, ops, code="b1", name="B1")
    with pytest.raises(ValidationError):        # no entries yet
        pricing.activate_price_book(e, ops, pb)
    pricing.add_price_entry(e, ops, pb, meter_code="API_CALLS",
                            unit_amount="0.01")
    pricing.activate_price_book(e, ops, pb)
    with pytest.raises(ValidationError):        # frozen once ACTIVE
        pricing.add_price_entry(e, ops, pb, meter_code="ASSESSMENTS",
                                unit_amount="0.01")


def test_price_entry_rejects_float(env):
    e, ops = env["eng"], env["ops"]
    pb = pricing.create_price_book(e, ops, code="b2", name="B2")
    with pytest.raises(ValidationError):
        pricing.add_price_entry(e, ops, pb, meter_code="API_CALLS",
                                unit_amount=0.01)   # float, not str/Decimal


# --------------------------------------------------------------- rating math
def test_per_unit_with_included_quantity(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    pb = _book(e, ops, unit="0.0100", included=200)     # 1000-200=800 billable
    s, en = _window()
    r = pricing.rate_subscription(e, ops, subscription_id=sid, price_book_id=pb,
                                  period_start=s, period_end=en)
    assert r["total"] == Decimal("8.0000")              # 800 * 0.01


def test_tiered_vs_volume(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    s, en = _window()
    tiers = [{"up_to": 100, "amount": "0.0200"}, {"up_to": None,
                                                  "amount": "0.0100"}]
    pb_t = _book(e, ops, model="TIERED", tiers=tiers, code="tier")
    rt = pricing.rate_subscription(e, ops, subscription_id=sid,
                                   price_book_id=pb_t, period_start=s,
                                   period_end=en)
    assert rt["total"] == Decimal("11.0000")            # 100*.02 + 900*.01
    pb_v = _book(e, ops, model="VOLUME", tiers=tiers, code="vol")
    rv = pricing.rate_subscription(e, ops, subscription_id=sid,
                                   price_book_id=pb_v, period_start=s,
                                   period_end=en)
    assert rv["total"] == Decimal("10.0000")            # all 1000 @ .01


def test_sub_cent_money_is_exact(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    pb = _book(e, ops, unit="0.0001")                   # 1/100 of a cent
    s, en = _window()
    r = pricing.rate_subscription(e, ops, subscription_id=sid, price_book_id=pb,
                                  period_start=s, period_end=en)
    assert r["total"] == Decimal("0.1000")              # 1000 * 0.0001, exact


# --------------------------------------------------------------- determinism
def test_rating_is_deterministic_and_supersedes(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    pb = _book(e, ops)
    s, en = _window()
    r1 = pricing.rate_subscription(e, ops, subscription_id=sid,
                                   price_book_id=pb, period_start=s,
                                   period_end=en)
    r2 = pricing.rate_subscription(e, ops, subscription_id=sid,
                                   price_book_id=pb, period_start=s,
                                   period_end=en)
    assert r1["inputs_digest"] == r2["inputs_digest"]
    assert r1["total"] == r2["total"] == Decimal("10.0000")
    prior = pricing.get_rating_run(e, ops, r1["rating_run_id"])
    assert prior["run"]["state"] == "SUPERSEDED"


# --------------------------------------------------------------- discounts
def test_discount_stacking_cap_and_exclusive(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    s, en = _window()
    # stacking: 10% + 5% = 15% off 10.0000 -> 8.5000
    pricing.create_discount_rule(e, ops, code="d10", name="d10", percent=10,
                                 priority=1)
    pricing.create_discount_rule(e, ops, code="d5", name="d5", percent=5,
                                 priority=2)
    pb = _book(e, ops, code="disc")
    r = pricing.rate_subscription(e, ops, subscription_id=sid, price_book_id=pb,
                                  period_start=s, period_end=en)
    assert r["total"] == Decimal("8.5000")


def test_exclusive_terminates_stacking(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    s, en = _window()
    pricing.create_discount_rule(e, ops, code="ex", name="ex", percent=20,
                                 priority=1, exclusive=True)
    pricing.create_discount_rule(e, ops, code="also", name="also", percent=10,
                                 priority=2)
    pb = _book(e, ops, code="exc")
    r = pricing.rate_subscription(e, ops, subscription_id=sid, price_book_id=pb,
                                  period_start=s, period_end=en)
    assert r["total"] == Decimal("8.0000")              # only the 20% applies


def test_discount_capped_by_max_percent(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    s, en = _window()
    pricing.create_discount_rule(e, ops, code="big", name="big", percent=25,
                                 priority=1, max_percent=10)
    pb = _book(e, ops, code="cap")
    r = pricing.rate_subscription(e, ops, subscription_id=sid, price_book_id=pb,
                                  period_start=s, period_end=en)
    assert r["total"] == Decimal("9.0000")              # capped to 10%


# --------------------------------------------------------------- floor / SoD
def test_floor_fails_closed_and_persists_failed_run(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    s, en = _window()
    pb = _book(e, ops, unit="0.0100", floor="0.0200", code="floor")
    with pytest.raises(RatingError) as ei:
        pricing.rate_subscription(e, ops, subscription_id=sid,
                                  price_book_id=pb, period_start=s,
                                  period_end=en)
    run = pricing.get_rating_run(e, ops, ei.value.rating_run_id)
    assert run["run"]["state"] == "FAILED"
    assert run["run"]["total"] == Decimal("0.0000")


def test_commercial_admin_cannot_approve_override(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    oid = pricing.request_price_override(e, ops, subscription_id=sid,
                                         meter_code="API_CALLS",
                                         floor_price="0.0200",
                                         proposed_price="0.0100",
                                         justification="strategic logo")
    with pytest.raises(PermissionDenied):
        pricing.approve_price_override(e, ops, oid)     # ops lacks the perm


def test_override_sod_and_unblocks_rating(env):
    e, ops, fin, fin2, sid = (env["eng"], env["ops"], env["fin"], env["fin2"],
                              env["sid"])
    s, en = _window()
    # a finance user who both requests and approves is blocked (SoD)
    oid = pricing.request_price_override(e, fin, subscription_id=sid,
                                         meter_code="API_CALLS",
                                         floor_price="0.0200",
                                         proposed_price="0.0100",
                                         justification="deal desk")
    with pytest.raises(ValidationError):
        pricing.approve_price_override(e, fin, oid)     # requester == approver
    pricing.approve_price_override(e, fin2, oid)        # a second party approves
    pb = _book(e, ops, unit="0.0100", floor="0.0200", code="ovr")
    r = pricing.rate_subscription(e, ops, subscription_id=sid, price_book_id=pb,
                                  period_start=s, period_end=en)
    assert r["state"] == "COMPLETE" and r["total"] == Decimal("10.0000")
    # an override is not consumed by rating: a replay of the same period must
    # still succeed (determinism), not fail because the override "ran out".
    r2 = pricing.rate_subscription(e, ops, subscription_id=sid,
                                   price_book_id=pb, period_start=s,
                                   period_end=en)
    assert r2["state"] == "COMPLETE" and r2["total"] == Decimal("10.0000")


# --------------------------------------------------------------- billing
def _complete_run(env, code="inv"):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    pb = _book(e, ops, code=code)
    s, en = _window()
    return pricing.rate_subscription(e, ops, subscription_id=sid,
                                     price_book_id=pb, period_start=s,
                                     period_end=en)


def test_invoice_issue_is_idempotent(env):
    e, ops = env["eng"], env["ops"]
    run = _complete_run(env)
    i1 = billing.issue_invoice(e, ops, rating_run_id=run["rating_run_id"])
    i2 = billing.issue_invoice(e, ops, rating_run_id=run["rating_run_id"])
    assert i1["created"] is True and i2["created"] is False
    assert i1["invoice_id"] == i2["invoice_id"]
    inv = billing.get_invoice(e, ops, i1["invoice_id"])
    assert inv["invoice"]["total"] == Decimal("10.0000")
    assert len(inv["lines"]) == 1


def test_payment_idempotency_and_state(env):
    e, ops = env["eng"], env["ops"]
    run = _complete_run(env, code="pay")
    inv = billing.issue_invoice(e, ops, rating_run_id=run["rating_run_id"])
    iid = inv["invoice_id"]
    p1 = billing.record_payment(e, ops, invoice_id=iid, amount="4.0000",
                                idempotency_key="k-part")
    assert p1["invoice_state"] == "PARTIALLY_PAID"
    # replay of the same key does not double-pay
    p1b = billing.record_payment(e, ops, invoice_id=iid, amount="4.0000",
                                 idempotency_key="k-part")
    assert p1b["created"] is False
    p2 = billing.record_payment(e, ops, invoice_id=iid, amount="6.0000",
                                idempotency_key="k-final")
    assert p2["invoice_state"] == "PAID"
    with pytest.raises(ValidationError):        # already fully paid
        billing.record_payment(e, ops, invoice_id=iid, amount="1.0000",
                               idempotency_key="k-extra")


def test_cannot_invoice_incomplete_run(env):
    e, ops, sid = env["eng"], env["ops"], env["sid"]
    s, en = _window()
    pb = _book(e, ops, unit="0.0100", floor="0.0200", code="bad")
    with pytest.raises(RatingError) as ei:
        pricing.rate_subscription(e, ops, subscription_id=sid,
                                  price_book_id=pb, period_start=s,
                                  period_end=en)
    with pytest.raises(ValidationError):
        billing.issue_invoice(e, ops, rating_run_id=ei.value.rating_run_id)


# --------------------------------------------------------------- webhooks
def _razorpay_event(iid, amount_minor=1000, pid="pay_1"):
    return {"event": "payment.captured",
            "payload": {"payment": {"entity": {
                "id": pid, "amount": amount_minor, "currency": "USD",
                "notes": {"invoice_id": iid}}}}}


def test_webhook_bad_signature_fails_closed(env, monkeypatch):
    e, ops = env["eng"], env["ops"]
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec")
    run = _complete_run(env, code="wh1")
    inv = billing.issue_invoice(e, ops, rating_run_id=run["rating_run_id"])
    payload = _razorpay_event(inv["invoice_id"])
    with pytest.raises(ValidationError):
        billing.ingest_webhook(e, provider="RAZORPAY", payload=payload,
                               signature="deadbeef")
    # invoice stays ISSUED — nothing was processed
    assert billing.get_invoice(e, ops, inv["invoice_id"])["invoice"]["state"] \
        == "ISSUED"


def test_webhook_verified_records_payment_once(env, monkeypatch):
    import hashlib
    import hmac
    e, ops = env["eng"], env["ops"]
    secret = "whsec"
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", secret)
    run = _complete_run(env, code="wh2")
    inv = billing.issue_invoice(e, ops, rating_run_id=run["rating_run_id"])
    payload = _razorpay_event(inv["invoice_id"], amount_minor=1000)
    body = bp.canonical_body(payload)
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r1 = billing.ingest_webhook(e, provider="RAZORPAY", payload=payload,
                                signature=sig)
    assert r1["status"] == "PAYMENT_RECORDED"
    assert r1["invoice_state"] == "PAID"
    # replay of the same provider event is a no-op (at-most-once)
    r2 = billing.ingest_webhook(e, provider="RAZORPAY", payload=payload,
                                signature=sig)
    assert r2["status"] == "DUPLICATE"


def test_webhook_secret_absent_fails_closed(env):
    # no RAZORPAY_WEBHOOK_SECRET set -> cannot verify -> reject
    e, ops = env["eng"], env["ops"]
    run = _complete_run(env, code="wh3")
    inv = billing.issue_invoice(e, ops, rating_run_id=run["rating_run_id"])
    payload = _razorpay_event(inv["invoice_id"])
    with pytest.raises(ValidationError):
        billing.ingest_webhook(e, provider="RAZORPAY", payload=payload,
                               signature="anything")


# --------------------------------------------------------------- isolation
def test_cross_tenant_rating_run_is_invisible(env):
    e, ops = env["eng"], env["ops"]
    run = _complete_run(env, code="iso")
    tid_b, _ = _new_subscription(e, ops, "beta")
    b_ctx = staff_context("FINANCE_ADMIN", "finB", tenant_id=tid_b)
    with pytest.raises(NotFoundError):
        pricing.get_rating_run(e, b_ctx, run["rating_run_id"])

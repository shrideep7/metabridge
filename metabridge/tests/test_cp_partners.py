"""Phase 8 — partner registry & commission engine.

Focuses on where the financial risk lives: commission computation, the deal and
commission state machines, segregation of duties (accrue vs approve/pay), the
five §9.3 payability preconditions checked against a REAL paid invoice built
through the Phase-4 pricing/billing chain, and compensating reverse/clawback
rows (never silent netting).
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from metabridge_control import (billing, catalog, db, metering, partners,
                                 pricing, schema, subscriptions as S, tenancy)
from metabridge_control.context import staff_context
from metabridge_control.errors import PermissionDenied, ValidationError
from metabridge_control.migrations import runner


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    eng = db.get_engine("sqlite:///" + str(tmp_path / "cp.db"))
    runner.migrate(eng)
    c = staff_context("COMMERCIAL_ADMIN", "ops")     # accrues, manages partners
    f = staff_context("FINANCE_ADMIN", "fin")        # approves + pays (SoD)
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
    return {"eng": eng, "c": c, "f": f, "tid": tid, "sid": sid}


def _paid_invoice(env, unit="0.0100", qty=1000, ikey="u"):
    """Build a genuinely PAID invoice through pricing -> rating -> billing."""
    eng, c, tid, sid = env["eng"], env["c"], env["tid"], env["sid"]
    pb = pricing.create_price_book(eng, c, code="std" + ikey, name="std")
    pricing.add_price_entry(eng, c, pb, meter_code="API_CALLS", unit_amount=unit)
    pricing.activate_price_book(eng, c, pb)
    metering.record_usage(eng, tenant_id=tid, meter_code="API_CALLS",
                          quantity=qty, idempotency_key=ikey, subscription_id=sid)
    now = schema.utcnow()
    run = pricing.rate_subscription(
        eng, c, subscription_id=sid, price_book_id=pb,
        period_start=now - timedelta(days=1), period_end=now + timedelta(days=1))
    inv = billing.issue_invoice(eng, c, rating_run_id=run["rating_run_id"])
    return inv


def _commission_setup(env, *, clawback_days=0, verify=True, activate=True,
                      basis_amount="10.0000", invoice_id=None, deal_state="WON"):
    eng, c = env["eng"], env["c"]
    partner = partners.create_partner(eng, c, kind="SI", name="Acme SI",
                                      tier_code="PREMIER")
    plan = partners.create_commission_plan(
        eng, c, code="cp" + str(clawback_days), basis="ALL_INVOICED",
        rate_table={"PREMIER": 10}, clawback_window_days=clawback_days)
    ag = partners.create_agreement(eng, c, partner, commission_plan_id=plan)
    if verify:
        partners.verify_agreement(eng, c, ag, banking=True, tax_docs=True)
    if activate:
        partners.activate_agreement(eng, c, ag)
    deal = partners.register_deal(eng, c, partner, prospect_name="BigCo")
    if deal_state in ("APPROVED", "WON"):
        partners.approve_deal(eng, c, deal)
    if deal_state == "WON":
        partners.win_deal(eng, c, deal, order_ref="ord1")
    comm = partners.accrue_commission(
        eng, c, partner_id=partner, agreement_id=ag,
        basis_amount=basis_amount, invoice_id=invoice_id, deal_id=deal)
    return {"partner": partner, "agreement": ag, "deal": deal, "commission": comm}


# ------------------------------------------------------------- compute
def test_compute_commission_variants():
    assert partners.compute_commission({"PREMIER": 10}, "PREMIER", "SI",
                                       "1000.0000") == Decimal("100.0000")
    # per-kind table
    assert partners.compute_commission({"SELECT": {"OEM": 15, "SI": 5}},
                                       "SELECT", "OEM", "200") == Decimal("30.0000")
    # wildcard tier fallback
    assert partners.compute_commission({"*": 8}, "REGISTERED", "SI",
                                       "50") == Decimal("4.0000")


def test_accrue_amount_from_tier(env):
    s = _commission_setup(env, basis_amount="10.0000")
    stmt = partners.partner_statement(env["eng"], env["c"], s["partner"])
    comm = next(x for x in stmt["commissions"] if x["id"] == s["commission"])
    assert comm["amount"] == Decimal("1.0000")           # 10.0000 x 10%


# ------------------------------------------------------------- SoD
def test_segregation_accrue_vs_approve(env):
    s = _commission_setup(env)
    # the commercial admin who accrued CANNOT approve (needs commissions:approve)
    with pytest.raises(PermissionDenied):
        partners.approve_commission(env["eng"], env["c"], s["commission"])
    # finance can
    partners.approve_commission(env["eng"], env["f"], s["commission"])


# ------------------------------------------------------------- deal machine
def test_illegal_deal_transition(env):
    eng, c = env["eng"], env["c"]
    p = partners.create_partner(eng, c, kind="SI", name="X")
    d = partners.register_deal(eng, c, p, prospect_name="Y")
    with pytest.raises(ValidationError):
        partners.win_deal(eng, c, d, order_ref="o")      # must be APPROVED first


# ------------------------------------------------------ payability (real revenue)
def test_full_payout_flow_reconciles_to_paid_invoice(env):
    eng, c, f = env["eng"], env["c"], env["f"]
    inv = _paid_invoice(env)                              # total 10.0000
    billing.record_payment(eng, c, invoice_id=inv["invoice_id"],
                           amount=inv["total"], idempotency_key="pay")
    s = _commission_setup(env, clawback_days=0, invoice_id=inv["invoice_id"])
    partners.approve_commission(eng, f, s["commission"])
    partners.mark_payable(eng, f, s["commission"])       # all 5 preconditions hold
    res = partners.pay_partner(eng, f, s["partner"])
    assert res["commissions"] == 1 and res["total"] == Decimal("1.0000")
    stmt = partners.partner_statement(eng, c, s["partner"])
    assert stmt["net_paid"] == Decimal("1.0000")


def test_payable_blocked_when_invoice_unpaid(env):
    eng, f = env["eng"], env["f"]
    inv = _paid_invoice(env)                              # issued but NOT paid
    s = _commission_setup(env, invoice_id=inv["invoice_id"])
    partners.approve_commission(eng, f, s["commission"])
    with pytest.raises(ValidationError, match="INVOICE_NOT_PAID"):
        partners.mark_payable(eng, f, s["commission"])


def test_payable_blocked_when_agreement_unverified(env):
    eng, c, f = env["eng"], env["c"], env["f"]
    inv = _paid_invoice(env)
    billing.record_payment(eng, c, invoice_id=inv["invoice_id"],
                           amount=inv["total"], idempotency_key="pay")
    s = _commission_setup(env, verify=False, activate=False,
                          invoice_id=inv["invoice_id"])
    partners.approve_commission(eng, f, s["commission"])
    with pytest.raises(ValidationError, match="AGREEMENT_NOT_ACTIVE"):
        partners.mark_payable(eng, f, s["commission"])


def test_payable_blocked_when_clawback_window_open(env):
    eng, c, f = env["eng"], env["c"], env["f"]
    inv = _paid_invoice(env)
    billing.record_payment(eng, c, invoice_id=inv["invoice_id"],
                           amount=inv["total"], idempotency_key="pay")
    s = _commission_setup(env, clawback_days=365, invoice_id=inv["invoice_id"])
    partners.approve_commission(eng, f, s["commission"])
    with pytest.raises(ValidationError, match="CLAWBACK_WINDOW_OPEN"):
        partners.mark_payable(eng, f, s["commission"])


def test_open_credit_note_blocks_payability(env):
    eng, c, f = env["eng"], env["c"], env["f"]
    inv = _paid_invoice(env)
    billing.record_payment(eng, c, invoice_id=inv["invoice_id"],
                           amount=inv["total"], idempotency_key="pay")
    billing.issue_credit_note(eng, c, invoice_id=inv["invoice_id"],
                              amount="1.0000", reason="dispute")
    s = _commission_setup(env, clawback_days=0, invoice_id=inv["invoice_id"])
    partners.approve_commission(eng, f, s["commission"])
    with pytest.raises(ValidationError, match="OPEN_CREDIT_NOTE"):
        partners.mark_payable(eng, f, s["commission"])


# ------------------------------------------------------------- compensating rows
def test_reverse_writes_compensating_row_not_a_mutation(env):
    eng, c = env["eng"], env["c"]
    s = _commission_setup(env, basis_amount="10.0000")   # amount 1.0000
    comp = partners.reverse_commission(eng, c, s["commission"],
                                       reason="order cancelled")
    stmt = partners.partner_statement(eng, c, s["partner"])
    states = {x["id"]: x["state"] for x in stmt["commissions"]}
    assert states[s["commission"]] == "REVERSED"         # original preserved
    assert states[comp] == "REVERSED"
    comp_row = next(x for x in stmt["commissions"] if x["id"] == comp)
    assert comp_row["amount"] == Decimal("-1.0000")      # explicit negative
    assert comp_row["clawback_of_id"] == s["commission"]

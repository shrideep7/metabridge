"""Phase 3 — usage metering & reporting.

Covers ingestion immutability/idempotency, adjustments (append-only), the four
aggregation rules, overage vs. entitlements, finance statements, signed
statement round-trip, concurrency-safe idempotency, and cross-tenant isolation.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from metabridge_control import (catalog, db, metering, schema,
                                 subscriptions as S, tenancy)
from metabridge_control.context import staff_context
from metabridge_control.errors import (NotFoundError, PermissionDenied,
                                        ValidationError)
from metabridge_control.migrations import runner


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    eng = db.get_engine("sqlite:///" + str(tmp_path / "m.db"))
    runner.migrate(eng)
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    # a plan with an objects_assessed quota so overage/statement can compare
    prod = catalog.create_product(eng, staff, code="p", name="P")
    plan = catalog.create_plan(eng, staff, prod, code="c", name="C")
    ver = catalog.create_plan_version(eng, staff, plan)
    catalog.create_feature(eng, staff, code="quota.objects_assessed",
                           name="Objects", value_kind="LIMIT")
    catalog.set_plan_feature(eng, staff, ver, "quota.objects_assessed",
                             limit_value=100)
    catalog.publish_plan_version(eng, staff, ver)
    tid = tenancy.create_tenant(eng, slug="acme", legal_name="Acme Corp",
                                system=True)
    sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
    acct = S.create_customer_account(eng, sctx, name="Acme")
    sid = S.create_subscription(eng, sctx, account_id=acct, plan_version_id=ver,
                                billing_period="MONTHLY")
    S.activate(eng, sctx, sid)
    return {"eng": eng, "tid": tid, "sid": sid, "sctx": sctx, "staff": staff}


def _window(env):
    now = schema.utcnow()
    return now - timedelta(days=1), now + timedelta(days=1)


# ------------------------------------------------------------- meter catalogue
def test_seeded_meters_present(env):
    codes = {m["meter_code"] for m in metering.list_meters(env["eng"])}
    assert {"OBJECTS_ASSESSED", "ASSESSMENTS", "AI_CREDITS", "API_CALLS",
            "REPORT_EXPORTS"} <= codes


def test_unknown_meter_rejected(env):
    with pytest.raises(ValidationError):
        metering.record_usage(env["eng"], tenant_id=env["tid"],
                              meter_code="NOPE", quantity=1,
                              idempotency_key="k")


# ------------------------------------------------------------- ingestion
def test_record_is_idempotent(env):
    r1 = metering.record_usage(env["eng"], tenant_id=env["tid"],
                               meter_code="API_CALLS", quantity=5,
                               idempotency_key="k1")
    r2 = metering.record_usage(env["eng"], tenant_id=env["tid"],
                               meter_code="API_CALLS", quantity=5,
                               idempotency_key="k1")
    assert r1["created"] is True and r2["created"] is False
    assert r1["event_id"] == r2["event_id"]
    with env["eng"].connect() as conn:
        n = conn.execute(select(schema.usage_events).where(
            schema.usage_events.c.tenant_id == env["tid"])).mappings().all()
    assert len(n) == 1                       # replay did not double-insert


def test_idempotency_key_cannot_switch_meter(env):
    metering.record_usage(env["eng"], tenant_id=env["tid"],
                          meter_code="API_CALLS", quantity=1,
                          idempotency_key="dup")
    with pytest.raises(ValidationError):
        metering.record_usage(env["eng"], tenant_id=env["tid"],
                              meter_code="REPORT_EXPORTS", quantity=1,
                              idempotency_key="dup")


def test_batch_ingest_counts_new_vs_duplicate(env):
    events = [{"meter_code": "API_CALLS", "quantity": 2, "idempotency_key": f"b{i}"}
              for i in range(5)]
    r = metering.ingest_batch(env["eng"], tenant_id=env["tid"], events=events)
    assert r["created"] == 5 and r["duplicates"] == 0
    # resubmit with one new
    events2 = events + [{"meter_code": "API_CALLS", "quantity": 2,
                         "idempotency_key": "b-new"}]
    r2 = metering.ingest_batch(env["eng"], tenant_id=env["tid"], events=events2)
    assert r2["created"] == 1 and r2["duplicates"] == 5


# ------------------------------------------------------------- aggregation
def test_sum_aggregation_with_adjustment(env):
    s, e = _window(env)
    ev = metering.record_usage(env["eng"], tenant_id=env["tid"],
                               meter_code="API_CALLS", quantity=100,
                               idempotency_key="a1")
    # an append-only correction of -30
    metering.adjust_usage(env["eng"], env["staff"], ev["event_id"],
                          quantity_delta=-30, reason_code="double_count")
    summ = metering.usage_summary(env["eng"], tenant_id=env["tid"],
                                  period_start=s, period_end=e)
    api = next(m for m in summ["meters"] if m["meter_code"] == "API_CALLS")
    assert api["quantity"] == 70             # 100 - 30, original event intact
    with env["eng"].connect() as conn:
        original = conn.execute(select(schema.usage_events.c.quantity).where(
            schema.usage_events.c.id == ev["event_id"])).scalar_one()
    assert original == 100                    # never mutated


def test_distinct_count_dedup(env):
    s, e = _window(env)
    # OBJECTS_ASSESSED dedups on the 'project' dimension per period
    for i, (proj, qty) in enumerate([("A", 40), ("A", 40), ("B", 40)]):
        metering.record_usage(env["eng"], tenant_id=env["tid"],
                              meter_code="OBJECTS_ASSESSED", quantity=qty,
                              idempotency_key=f"oa{i}",
                              dimensions={"project": proj})
    summ = metering.usage_summary(env["eng"], tenant_id=env["tid"],
                                  period_start=s, period_end=e)
    oa = next(m for m in summ["meters"] if m["meter_code"] == "OBJECTS_ASSESSED")
    assert oa["quantity"] == 2                # distinct projects A,B — not 3


def test_finalize_blocks_recompute(env):
    s, e = _window(env)
    metering.record_usage(env["eng"], tenant_id=env["tid"],
                          meter_code="API_CALLS", quantity=10,
                          idempotency_key="f1")
    metering.aggregate(env["eng"], tenant_id=env["tid"], meter_code="API_CALLS",
                       period_start=s, period_end=e, finalize=True)
    with pytest.raises(ValidationError):
        metering.aggregate(env["eng"], tenant_id=env["tid"],
                           meter_code="API_CALLS", period_start=s,
                           period_end=e, finalize=True)


# ------------------------------------------------------------- overage & statement
def test_overage_vs_entitlement(env):
    s, e = _window(env)
    for i in range(3):
        metering.record_usage(env["eng"], tenant_id=env["tid"],
                              meter_code="OBJECTS_ASSESSED", quantity=50,
                              idempotency_key=f"ov{i}",
                              dimensions={"project": f"p{i}"})
    rep = metering.overage_report(env["eng"], tenant_id=env["tid"],
                                  period_start=s, period_end=e)
    oa = next(l for l in rep["lines"]
              if l["meter_code"] == "OBJECTS_ASSESSED")
    # 3 distinct projects vs limit 100 -> no overage on distinct-count of 3
    assert oa["used"] == 3 and oa["limit"] == 100 and oa["overage"] == 0


def test_statement_shows_entitlement_usage_overage(env):
    s, e = _window(env)
    metering.record_usage(env["eng"], tenant_id=env["tid"],
                          meter_code="API_CALLS", quantity=7,
                          idempotency_key="st1")
    stmt = metering.usage_statement(env["eng"], tenant_id=env["tid"],
                                    period_start=s, period_end=e)
    assert stmt["tenant"] == "Acme Corp"
    api = next(l for l in stmt["lines"] if l["meter_code"] == "API_CALLS")
    assert api["usage"] == 7 and api["source_events"] == 1
    csv = metering.statement_csv(stmt)
    assert "API_CALLS" in csv and "meter_code" in csv.splitlines()[0]


# ------------------------------------------------------------- adjustments authz
def test_adjust_requires_permission_and_reason(env):
    ev = metering.record_usage(env["eng"], tenant_id=env["tid"],
                               meter_code="API_CALLS", quantity=1,
                               idempotency_key="p1")
    partner = staff_context("PARTNER_ADMIN", "pa")
    with pytest.raises(PermissionDenied):
        metering.adjust_usage(env["eng"], partner, ev["event_id"],
                              quantity_delta=-1, reason_code="x")
    with pytest.raises(ValidationError):
        metering.adjust_usage(env["eng"], env["staff"], ev["event_id"],
                              quantity_delta=-1, reason_code="")
    with pytest.raises(NotFoundError):
        metering.adjust_usage(env["eng"], env["staff"], "missing",
                              quantity_delta=-1, reason_code="x")


def test_adjustment_is_audited(env):
    ev = metering.record_usage(env["eng"], tenant_id=env["tid"],
                               meter_code="API_CALLS", quantity=1,
                               idempotency_key="au1")
    metering.adjust_usage(env["eng"], env["staff"], ev["event_id"],
                          quantity_delta=-1, reason_code="correction")
    with env["eng"].connect() as conn:
        ev_row = conn.execute(select(schema.audit_events).where(
            (schema.audit_events.c.action == "usage.adjust")
            & (schema.audit_events.c.tenant_id == env["tid"]))).mappings().all()
    assert ev_row and ev_row[-1]["reason"] == "correction"


# ------------------------------------------------------------- signed statement
def test_signed_statement_roundtrip(env):
    s, e = _window(env)
    metering.record_usage(env["eng"], tenant_id=env["tid"],
                          meter_code="API_CALLS", quantity=9,
                          idempotency_key="sig1")
    signed = metering.export_signed_statement(
        env["eng"], tenant_id=env["tid"], instance_id="inst-1",
        period_start=s, period_end=e, serial="S-001")
    res = metering.ingest_signed_statement(
        env["eng"], payload=signed["payload"], signature=signed["signature"])
    assert res["state"] == "INGESTED"
    # idempotent on serial
    res2 = metering.ingest_signed_statement(
        env["eng"], payload=signed["payload"], signature=signed["signature"])
    assert res2["state"] == "DUPLICATE"


def test_tampered_statement_rejected(env):
    s, e = _window(env)
    signed = metering.export_signed_statement(
        env["eng"], tenant_id=env["tid"], instance_id="inst-1",
        period_start=s, period_end=e, serial="S-002")
    signed["payload"]["lines"].append({"meter_code": "API_CALLS",
                                       "usage": 999999})   # forge more usage
    with pytest.raises(ValidationError):
        metering.ingest_signed_statement(
            env["eng"], payload=signed["payload"],
            signature=signed["signature"])


# ------------------------------------------------------------- cross-tenant
def test_usage_is_tenant_isolated(env):
    s, e = _window(env)
    other = tenancy.create_tenant(env["eng"], slug="evil", legal_name="Evil",
                                  system=True)
    metering.record_usage(env["eng"], tenant_id=env["tid"],
                          meter_code="API_CALLS", quantity=42,
                          idempotency_key="iso1")
    summ_other = metering.usage_summary(env["eng"], tenant_id=other,
                                        period_start=s, period_end=e)
    assert summ_other["meters"] == []        # no leakage into another tenant


def test_expected_tenant_guard(env):
    with pytest.raises(ValidationError):
        metering.record_usage(env["eng"], tenant_id=env["tid"],
                              meter_code="API_CALLS", quantity=1,
                              idempotency_key="g1",
                              expected_tenant="someone-else")

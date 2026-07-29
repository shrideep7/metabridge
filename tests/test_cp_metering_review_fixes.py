"""Regression tests for the Phase-3 metering adversarial-review findings."""
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from metabridge_control import db, licensing, metering, schema, tenancy
from metabridge_control.context import staff_context
from metabridge_control.errors import ValidationError
from metabridge_control.migrations import runner


@pytest.fixture()
def eng(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    e = db.get_engine("sqlite:///" + str(tmp_path / "mr.db"))
    runner.migrate(e)
    return e


def _tenant(eng, slug):
    return tenancy.create_tenant(eng, slug=slug, legal_name=slug, system=True)


# ---- CRITICAL: adjust_usage cross-tenant guard --------------------------
def test_adjust_cannot_touch_foreign_tenant_event(eng):
    t1, t2 = _tenant(eng, "t1"), _tenant(eng, "t2")
    ev = metering.record_usage(eng, tenant_id=t2, meter_code="API_CALLS",
                               quantity=100, idempotency_key="e1")
    staff_t1 = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=t1)
    with pytest.raises(ValidationError):        # scoped to t1, event is t2
        metering.adjust_usage(eng, staff_t1, ev["event_id"],
                              quantity_delta=-50, reason_code="x")
    # a global (SUPER_ADMIN) context may cross tenants
    su = staff_context("SUPER_ADMIN", "root")   # tenant '*'
    metering.adjust_usage(eng, su, ev["event_id"], quantity_delta=-50,
                          reason_code="ok")


# ---- CRITICAL: finalized aggregate is served, not recomputed ------------
def test_finalized_period_is_frozen_for_statements(eng):
    t = _tenant(eng, "frozen")
    start = datetime(2026, 6, 1)
    end = datetime(2026, 7, 1)
    metering.record_usage(eng, tenant_id=t, meter_code="API_CALLS",
                          quantity=100, idempotency_key="f1",
                          occurred_at=datetime(2026, 6, 15))
    metering.aggregate(eng, tenant_id=t, meter_code="API_CALLS",
                       period_start=start, period_end=end, finalize=True)
    # a late backdated event lands inside the frozen period
    metering.record_usage(eng, tenant_id=t, meter_code="API_CALLS",
                          quantity=999, idempotency_key="late",
                          occurred_at=datetime(2026, 6, 20))
    summ = metering.usage_summary(eng, tenant_id=t, period_start=start,
                                  period_end=end)
    api = next(m for m in summ["meters"] if m["meter_code"] == "API_CALLS")
    assert api["quantity"] == 100               # frozen, not 1099
    stmt = metering.usage_statement(eng, tenant_id=t, period_start=start,
                                    period_end=end)
    line = next(l for l in stmt["lines"] if l["meter_code"] == "API_CALLS")
    assert line["usage"] == 100                 # statement honors the freeze


# ---- HIGH: adjustments only for SUM meters ------------------------------
def test_adjust_rejected_for_non_sum_meter(eng):
    t = _tenant(eng, "nonsum")
    ev = metering.record_usage(eng, tenant_id=t, meter_code="OBJECTS_ASSESSED",
                               quantity=10, idempotency_key="o1",
                               dimensions={"project": "p"})
    su = staff_context("SUPER_ADMIN", "root")
    with pytest.raises(ValidationError):        # DISTINCT_COUNT: no delta
        metering.adjust_usage(eng, su, ev["event_id"], quantity_delta=-1,
                              reason_code="x")


# ---- HIGH: adjustment lands in the corrected event's period -------------
def test_adjustment_uses_event_period(eng):
    t = _tenant(eng, "period")
    june = datetime(2026, 6, 10)
    ev = metering.record_usage(eng, tenant_id=t, meter_code="API_CALLS",
                               quantity=100, idempotency_key="p1",
                               occurred_at=june)
    su = staff_context("SUPER_ADMIN", "root")
    metering.adjust_usage(eng, su, ev["event_id"], quantity_delta=-40,
                          reason_code="correction")
    # June total reflects the delta (delta carries the event's occurred_at)
    jsum = metering.usage_summary(eng, tenant_id=t,
                                  period_start=datetime(2026, 6, 1),
                                  period_end=datetime(2026, 7, 1))
    assert next(m for m in jsum["meters"]
                if m["meter_code"] == "API_CALLS")["quantity"] == 60
    # a different (July) period does NOT see the correction
    july = metering.usage_summary(eng, tenant_id=t,
                                  period_start=datetime(2026, 7, 1),
                                  period_end=datetime(2026, 8, 1))
    assert not [m for m in july["meters"] if m["meter_code"] == "API_CALLS"]


# ---- MEDIUM: distinct-count doesn't silently drop missing-dim events ----
def test_distinct_count_missing_dimension_bucketed(eng):
    t = _tenant(eng, "distinct")
    s, e = datetime(2026, 6, 1), datetime(2026, 7, 1)
    o = datetime(2026, 6, 5)
    metering.record_usage(eng, tenant_id=t, meter_code="OBJECTS_ASSESSED",
                          quantity=1, idempotency_key="d1",
                          dimensions={"project": "A"}, occurred_at=o)
    metering.record_usage(eng, tenant_id=t, meter_code="OBJECTS_ASSESSED",
                          quantity=1, idempotency_key="d2",
                          dimensions={}, occurred_at=o)   # no project
    summ = metering.usage_summary(eng, tenant_id=t, period_start=s, period_end=e)
    oa = next(m for m in summ["meters"]
              if m["meter_code"] == "OBJECTS_ASSESSED")
    assert oa["quantity"] == 2                  # A + one "missing" bucket


# ---- CRITICAL: signed statement rejects a forged (unpinned) key ---------
def _payload(t, inst="inst-1", serial="S1"):
    return {"tenant_id": t, "instance_id": inst, "serial": serial,
            "period_start": "2026-06-01", "period_end": "2026-07-01",
            "lines": [{"meter_code": "API_CALLS", "usage": 5}]}


def test_signed_statement_rejects_foreign_key(eng):
    t = _tenant(eng, "sig")
    payload = _payload(t)
    attacker = Ed25519PrivateKey.generate()     # not the control-plane key
    forged = attacker.sign(licensing._canonical(payload)).hex()
    with pytest.raises(ValidationError):
        metering.ingest_signed_statement(eng, payload=payload, signature=forged)


def test_signed_statement_requires_instance_and_tenant(eng):
    t = _tenant(eng, "sig2")
    # missing instance_id
    bad = _payload(t); bad.pop("instance_id")
    priv, _ = licensing._load_signer()
    with pytest.raises(ValidationError):
        metering.ingest_signed_statement(
            eng, payload=bad, signature=priv.sign(
                licensing._canonical(bad)).hex())
    # unknown tenant
    ghost = _payload("no-such-tenant")
    with pytest.raises(ValidationError):
        metering.ingest_signed_statement(
            eng, payload=ghost, signature=priv.sign(
                licensing._canonical(ghost)).hex())


def test_signed_statements_dedup_per_instance_not_just_serial(eng):
    t = _tenant(eng, "sig3")
    priv, _ = licensing._load_signer()
    # two DIFFERENT instances re-using the same serial must both ingest
    for inst in ("inst-A", "inst-B"):
        p = _payload(t, inst=inst, serial="SHARED-1")
        r = metering.ingest_signed_statement(
            eng, payload=p, signature=priv.sign(licensing._canonical(p)).hex())
        assert r["state"] == "INGESTED"
    # a true duplicate (same instance + serial) is deduped
    p = _payload(t, inst="inst-A", serial="SHARED-1")
    r = metering.ingest_signed_statement(
        eng, payload=p, signature=priv.sign(licensing._canonical(p)).hex())
    assert r["state"] == "DUPLICATE"

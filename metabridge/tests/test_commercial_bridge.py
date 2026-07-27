"""Phase 5 — commercial enforcement bridge (data-plane side).

Covers offline license verification (incl. a real control-plane-signed
round-trip and canonical byte-parity), entitlement resolution, the flag-gated
enforcement modes (off/warn/deny), the durable idempotent usage spool +
reporter (including restart survival), and air-gapped signed-statement export.

None of this touches an existing product code path — the bridge is additive and
inert until an engine calls it and a flag is set.
"""
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from metabridge.commercial import (CommercialBridge, EntitlementDenied,
                                    EntitlementSet, UsageReporter, UsageSpool,
                                    build_usage_statement, load_license_file,
                                    sign_statement)
from metabridge.commercial import airgap, bridge as bridgemod
from metabridge.commercial.licensefile import (EXPIRED, GRACE, LicenseError,
                                               NOT_YET_VALID, VALID, _canonical)
from metabridge.platform.flags import FeatureFlags


# --------------------------------------------------------------- helpers
def _mk_key():
    p = Ed25519PrivateKey.generate()
    return p, p.public_key().public_bytes_raw().hex()


def _sign_license(payload, priv):
    return {"payload": payload, "signature": priv.sign(_canonical(payload)).hex()}


def _license_payload(**over):
    base = {"schema": "metabridge.license/1", "license_id": "L1", "serial": 1,
            "tenant_id": "t1", "subscription_id": "s1", "instance_id": "i1",
            "not_before": None, "not_after": None, "offline_grace_days": 7,
            "entitlements": [], "signer_key_id": "abc"}
    base.update(over)
    return base


# --------------------------------------------------------------- license verify
def test_offline_verify_accepts_good_rejects_tamper_and_wrong_key():
    priv, pub = _mk_key()
    signed = _sign_license(_license_payload(), priv)
    lf = load_license_file(signed, pub)
    assert lf.verified and lf.tenant_id == "t1"

    # tampered payload no longer matches the signature
    tampered = {"payload": {**signed["payload"], "tenant_id": "attacker"},
                "signature": signed["signature"]}
    with pytest.raises(LicenseError):
        load_license_file(tampered, pub)

    # a different (untrusted) key fails closed
    _, other_pub = _mk_key()
    with pytest.raises(LicenseError):
        load_license_file(signed, other_pub)


def test_license_validity_window():
    priv, pub = _mk_key()
    now = datetime(2026, 7, 18, 12, 0, 0)

    future = _sign_license(_license_payload(
        not_before=str(now + timedelta(days=1))), priv)
    assert load_license_file(future, pub).status(now) == NOT_YET_VALID

    live = _sign_license(_license_payload(
        not_after=str(now + timedelta(days=5))), priv)
    assert load_license_file(live, pub).status(now) == VALID

    grace = _sign_license(_license_payload(
        not_after=str(now - timedelta(days=2)), offline_grace_days=7), priv)
    assert load_license_file(grace, pub).status(now) == GRACE

    dead = _sign_license(_license_payload(
        not_after=str(now - timedelta(days=30)), offline_grace_days=7), priv)
    assert load_license_file(dead, pub).status(now) == EXPIRED


def test_canonical_parity_with_control_plane():
    """If the two planes' canonicalization ever drifts, every real signature
    breaks — pin them together."""
    from metabridge_control import licensing as cp_lic
    payload = _license_payload(entitlements=[{"code": "feature.x",
                                              "value_kind": "BOOLEAN",
                                              "value": {"enabled": True},
                                              "period": None}])
    assert _canonical(payload) == cp_lic._canonical(payload)


# --------------------------------------------------------- real cross-plane
@pytest.fixture()
def cp_license(tmp_path, monkeypatch):
    """A genuine control-plane-issued, signed license file + its trust anchor."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    from metabridge_control import (catalog, db, licensing, schema,
                                     subscriptions as S, tenancy)
    from metabridge_control.context import staff_context
    from metabridge_control.migrations import runner
    eng = db.get_engine("sqlite:///" + str(tmp_path / "cp.db"))
    runner.migrate(eng)
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    prod = catalog.create_product(eng, staff, code="p", name="P")
    plan = catalog.create_plan(eng, staff, prod, code="c", name="C")
    ver = catalog.create_plan_version(eng, staff, plan)
    catalog.create_feature(eng, staff, code="feature.twin", name="Twin",
                           value_kind="BOOLEAN")
    catalog.create_feature(eng, staff, code="limit.users", name="Users",
                           value_kind="LIMIT")
    catalog.set_plan_feature(eng, staff, ver, "feature.twin", bool_value=True)
    catalog.set_plan_feature(eng, staff, ver, "limit.users", limit_value=3)
    catalog.publish_plan_version(eng, staff, ver)
    tid = tenancy.create_tenant(eng, slug="acme", legal_name="Acme",
                                system=True)
    sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
    acct = S.create_customer_account(eng, sctx, name="Acme")
    sid = S.create_subscription(eng, sctx, account_id=acct, plan_version_id=ver)
    S.activate(eng, sctx, sid)
    lid = licensing.issue_license(eng, sctx, sid, instance_id="inst-1")
    lf = licensing.generate_license_file(eng, sctx, lid)
    return {"file": lf, "trust": licensing.public_key_hex()}


def test_control_plane_license_verifies_and_resolves_offline(cp_license):
    lf = load_license_file(cp_license["file"], cp_license["trust"])
    assert lf.verified
    ent = EntitlementSet(lf.entitlements)
    assert ent.decide("feature.twin").allowed                       # BOOLEAN on
    assert ent.decide("limit.users", quantity=3, current_usage=0).allowed
    over = ent.decide("limit.users", quantity=1, current_usage=3)   # 4 > 3
    assert not over.allowed and over.reason_code == "LIMIT_EXCEEDED"
    assert not ent.decide("feature.unknown").allowed                # fail closed


# --------------------------------------------------------------- entitlements
def test_entitlement_shapes():
    ent = EntitlementSet([
        {"code": "feature.on", "value_kind": "BOOLEAN", "value": {"enabled": True}},
        {"code": "feature.off", "value_kind": "BOOLEAN", "value": {"enabled": False}},
        {"code": "limit.seats", "value_kind": "NUMERIC_LIMIT",
         "value": {"unlimited": False, "limit": 5}},
        {"code": "quota.calls", "value_kind": "METERED_QUOTA",
         "value": {"unlimited": True}},
    ])
    assert ent.decide("feature.on").allowed
    assert not ent.decide("feature.off").allowed
    assert ent.decide("limit.seats", quantity=5).allowed
    assert not ent.decide("limit.seats", quantity=6).allowed
    assert ent.decide("quota.calls", quantity=10**9).allowed        # unlimited


# --------------------------------------------------------------- bridge modes
def _flags_with(tmp_path, **on):
    ff = FeatureFlags(str(tmp_path))
    for k, v in on.items():
        ff.set(k, enabled=v)
    return ff


def test_bridge_off_is_noop(tmp_path):
    b = CommercialBridge(EntitlementSet([]), FeatureFlags(str(tmp_path)))
    d = b.check("feature.anything")            # unseeded flag => OFF
    assert d.allowed and d.mode == "off" and d.entitled


def test_bridge_warn_allows_but_flags(tmp_path):
    logs = []
    ff = _flags_with(tmp_path, commercial_enforcement=True)   # deny flag unset
    b = CommercialBridge(EntitlementSet([]), ff, logger=logs.append)
    d = b.enforce("feature.premium")           # not entitled, but WARN
    assert d.allowed and d.mode == "warn" and not d.entitled and d.would_block
    assert logs and logs[0]["enforced"] is False


def test_bridge_deny_blocks(tmp_path):
    ff = _flags_with(tmp_path, commercial_enforcement=True,
                     commercial_enforcement_deny=True)
    ent = EntitlementSet([{"code": "feature.core", "value_kind": "BOOLEAN",
                           "value": {"enabled": True}}])
    b = CommercialBridge(ent, ff)
    assert b.enforce("feature.core").allowed                 # entitled -> ok
    with pytest.raises(EntitlementDenied) as ei:
        b.enforce("feature.premium")                         # not entitled
    assert ei.value.decision.reason_code == "NO_SUCH_ENTITLEMENT"


def test_bridge_expired_license_denies_expansion(tmp_path):
    priv, pub = _mk_key()
    now = datetime.utcnow()
    dead = _sign_license(_license_payload(
        not_after=str(now - timedelta(days=60)), offline_grace_days=1,
        entitlements=[{"code": "feature.core", "value_kind": "BOOLEAN",
                       "value": {"enabled": True}}]), priv)
    lf = load_license_file(dead, pub)
    ff = _flags_with(tmp_path, commercial_enforcement=True,
                     commercial_enforcement_deny=True)
    b = CommercialBridge(EntitlementSet(lf.entitlements), ff, license_file=lf)
    with pytest.raises(EntitlementDenied) as ei:
        b.enforce("feature.core")             # entitled, but license expired
    assert ei.value.decision.reason_code == bridgemod.R_LICENSE_INVALID


# --------------------------------------------------------------- usage spool
def test_spool_dedup_backpressure_and_restart(tmp_path):
    path = tmp_path / "spool.json"
    sp = UsageSpool(path, max_events=3)
    assert sp.add("API_CALLS", 1, "k1")
    assert not sp.add("API_CALLS", 1, "k1")        # duplicate key
    sp.add("API_CALLS", 1, "k2")
    sp.add("API_CALLS", 1, "k3")
    assert not sp.add("API_CALLS", 1, "k4")        # full -> backpressure
    assert sp.count() == 3 and sp.dropped() == 1
    # survives "restart": a fresh object over the same file sees the events
    assert UsageSpool(path).count() == 3


def test_reporter_drains_retries_and_leaves_on_failure(tmp_path):
    sp = UsageSpool(tmp_path / "s.json")
    for i in range(5):
        sp.add("API_CALLS", 1, f"k{i}")
    sent_batches = []

    calls = {"n": 0}

    def flaky_sink(events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("control plane unreachable")
        sent_batches.append([e["idempotency_key"] for e in events])

    rep = UsageReporter(sp, flaky_sink, batch_size=10, sleep=lambda s: None)
    res = rep.flush(max_attempts=3)
    assert res["drained"] and res["remaining"] == 0 and res["sent"] == 5
    assert calls["n"] == 2                          # failed once, then succeeded

    # a sink that always fails leaves everything spooled (no loss)
    sp2 = UsageSpool(tmp_path / "s2.json")
    sp2.add("API_CALLS", 1, "z1")

    def dead_sink(_):
        raise RuntimeError("down")
    rep2 = UsageReporter(sp2, dead_sink, sleep=lambda s: None)
    res2 = rep2.flush(max_attempts=2)
    assert not res2["drained"] and sp2.count() == 1


# --------------------------------------------------------------- air-gap export
def test_airgap_statement_sign_and_verify_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    priv_hex, pub_hex = airgap.load_or_create_instance_key(str(tmp_path))
    stmt = build_usage_statement(
        tenant_id="t1", instance_id="i1", serial="2026-07",
        period_start="2026-07-01 00:00:00", period_end="2026-08-01 00:00:00",
        lines=[{"meter_code": "API_CALLS", "usage": 42},
               {"meter_code": "OBJECTS_ASSESSED", "usage": 7, "extra": "x"}])
    env = sign_statement(stmt, priv_hex)
    assert env["signer_public_key"] == pub_hex
    assert airgap.verify_statement(env["payload"], env["signature"], pub_hex)
    # tamper detection
    bad = {**env["payload"], "lines": [{"meter_code": "API_CALLS", "usage": 999}]}
    assert not airgap.verify_statement(bad, env["signature"], pub_hex)
    # the key is stable across reloads (same instance identity)
    assert airgap.load_or_create_instance_key(str(tmp_path))[0] == priv_hex

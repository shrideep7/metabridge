"""Regression tests for the Phase-1 adversarial-review findings.

Each test pins a confirmed vulnerability closed so it cannot silently return.
"""
import pytest
from sqlalchemy import select

from metabridge_control import audit, catalog, db, flags, schema, tenancy
from metabridge_control.context import resolve_context, staff_context
from metabridge_control.errors import (PermissionDenied, PlanImmutableError,
                                       ValidationError)
from metabridge_control.migrations import runner


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    eng = db.get_engine("sqlite:///" + str(tmp_path / "fix.db"))
    runner.migrate(eng)
    return eng


def _tenant_with_roles(engine, slug="acme"):
    tid = tenancy.create_tenant(engine, slug=slug, legal_name=slug,
                                system=True)
    owner = tenancy.create_user(engine, email=f"owner@{slug}.example",
                                system=True)
    with engine.begin() as conn:
        conn.execute(schema.memberships.insert().values(
            id=schema.new_id(), tenant_id=tid, user_id=owner,
            role_code="cp_owner", state="ACTIVE", created_by="test"))
    with engine.connect() as conn:
        octx = resolve_context(conn, user_id=owner, tenant_id=tid)
    admin_u = tenancy.create_user(engine, email=f"admin@{slug}.example",
                                  system=True)
    admin_mid = tenancy.add_membership(engine, octx, user_id=admin_u,
                                       role_code="cp_admin")
    with engine.connect() as conn:
        actx = resolve_context(conn, user_id=admin_u, tenant_id=tid)
    return {"tid": tid, "owner": owner, "octx": octx,
            "admin_u": admin_u, "admin_mid": admin_mid, "actx": actx}


# ---- HIGH: tenant takeover via membership escalation / owner revocation ----
def test_admin_cannot_grant_owner_role(engine):
    w = _tenant_with_roles(engine)
    victim = tenancy.create_user(engine, email="p@acme.example", system=True)
    with pytest.raises(PermissionDenied):
        tenancy.add_membership(engine, w["actx"], user_id=victim,
                               role_code="cp_owner")     # escalation blocked


def test_admin_cannot_revoke_owner(engine):
    w = _tenant_with_roles(engine)
    owner_mid = None
    with engine.connect() as conn:
        owner_mid = conn.execute(select(schema.memberships.c.id).where(
            (schema.memberships.c.tenant_id == w["tid"])
            & (schema.memberships.c.role_code == "cp_owner"))).scalar_one()
    with pytest.raises(PermissionDenied):
        tenancy.revoke_membership(engine, w["actx"], owner_mid)  # outranks


def test_cannot_revoke_last_owner(engine):
    w = _tenant_with_roles(engine)
    with engine.connect() as conn:
        owner_mid = conn.execute(select(schema.memberships.c.id).where(
            (schema.memberships.c.tenant_id == w["tid"])
            & (schema.memberships.c.role_code == "cp_owner"))).scalar_one()
    with pytest.raises(ValidationError):
        tenancy.revoke_membership(engine, w["octx"], owner_mid)  # last owner


def test_owner_can_revoke_a_second_owner(engine):
    w = _tenant_with_roles(engine)
    u2 = tenancy.create_user(engine, email="own2@acme.example", system=True)
    mid2 = tenancy.add_membership(engine, w["octx"], user_id=u2,
                                  role_code="cp_owner")
    tenancy.revoke_membership(engine, w["octx"], mid2)   # ok: not the last


# ---------- HIGH: audit tail truncation / full deletion detection ----------
def test_deleting_the_last_event_is_detected(engine):
    w = _tenant_with_roles(engine)
    tenancy.create_organization(engine, w["octx"], slug="o", name="O")
    with engine.connect() as conn:
        n = audit.count_events(conn, w["tid"])
    with engine.begin() as conn:
        conn.execute(schema.audit_events.delete().where(
            (schema.audit_events.c.tenant_id == w["tid"])
            & (schema.audit_events.c.seq == n)))       # chop the tail
    with engine.connect() as conn:
        ok, _ = audit.verify_chain(conn, w["tid"])
    assert ok is False                                  # anchor catches it


def test_full_deletion_with_orphan_anchor_is_detected(engine):
    w = _tenant_with_roles(engine)
    with engine.begin() as conn:
        conn.execute(schema.audit_events.delete().where(
            schema.audit_events.c.tenant_id == w["tid"]))   # wipe all events
    with engine.connect() as conn:
        ok, _ = audit.verify_chain(conn, w["tid"])
    assert ok is False                                  # head anchor remains


# --------------------- MEDIUM: flag empty allowed_roles --------------------
def test_empty_allowed_roles_denies_all(engine):
    w = _tenant_with_roles(engine)
    flags.set_flag(engine, w["octx"], "locked", enabled=True,
                   allowed_roles=[])
    with engine.connect() as conn:
        assert flags.is_enabled(conn, "locked", tenant_id=w["tid"],
                                role="cp_owner") is False
        assert flags.is_enabled(conn, "locked", tenant_id=w["tid"]) is False


# ------------------ MEDIUM: provisioning-primitive authorization ------------
def test_provisioning_requires_system_or_staff(engine):
    with pytest.raises(PermissionDenied):
        tenancy.create_tenant(engine, slug="x", legal_name="X")   # open call
    with pytest.raises(PermissionDenied):
        tenancy.create_user(engine, email="a@b.com")              # open call
    # a staff context with the right permission is allowed
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    tid = tenancy.create_tenant(engine, slug="viastaff", legal_name="S",
                                staff_ctx=staff)
    assert tid
    # a customer inviting a user (member:manage) is allowed
    w = _tenant_with_roles(engine, slug="cust")
    uid = tenancy.create_user(engine, email="invitee@cust.example",
                              ctx=w["octx"])
    assert uid


# ------------------------- MEDIUM: get_tenant scoping ----------------------
def test_get_tenant_is_scoped(engine):
    a = _tenant_with_roles(engine, slug="alpha")
    b = _tenant_with_roles(engine, slug="bravo")
    # customer sees own tenant, not a foreign one (foreign reads as absent)
    assert tenancy.get_tenant(engine, a["octx"], a["tid"])["slug"] == "alpha"
    assert tenancy.get_tenant(engine, a["octx"], b["tid"]) is None
    # staff with tenant:read may read any
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    assert tenancy.get_tenant(engine, staff, b["tid"])["slug"] == "bravo"


# --------------- HIGH: published plan-version immutability ------------------
def test_publish_is_idempotent_guarded(engine):
    staff = staff_context("COMMERCIAL_ADMIN", "cat")
    prod = catalog.create_product(engine, staff, code="p", name="P")
    catalog.create_feature(engine, staff, code="f.x", name="X",
                           value_kind="BOOLEAN")
    plan = catalog.create_plan(engine, staff, prod, code="c", name="C")
    ver = catalog.create_plan_version(engine, staff, plan)
    catalog.set_plan_feature(engine, staff, ver, "f.x", bool_value=True)
    catalog.publish_plan_version(engine, staff, ver)
    # re-publishing / mutating a PUBLISHED version is refused
    with pytest.raises(PlanImmutableError):
        catalog.publish_plan_version(engine, staff, ver)
    with pytest.raises(PlanImmutableError):
        catalog.set_plan_feature(engine, staff, ver, "f.x", bool_value=False)

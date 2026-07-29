"""Control plane Phase 1 — migrations, tenancy services, RBAC (unit +
integration)."""
import pytest
from sqlalchemy import select

from metabridge_control import db, rbac, schema, tenancy
from metabridge_control.context import resolve_context, staff_context
from metabridge_control.errors import (NotFoundError, PermissionDenied,
                                       TenantAccessDenied, ValidationError)
from metabridge_control.migrations import runner


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    eng = db.get_engine("sqlite:///" + str(tmp_path / "cp.db"))
    runner.migrate(eng)
    return eng


def _mk_tenant(engine, slug="acme"):
    tid = tenancy.create_tenant(engine, slug=slug, legal_name=slug.title(),
                                system=True)
    uid = tenancy.create_user(engine, email=f"owner@{slug}.example", system=True)
    ctx0 = staff_context("SUPER_ADMIN", "test-staff", tenant_id=tid)
    # bootstrap first membership directly (owner)
    with engine.begin() as conn:
        conn.execute(schema.memberships.insert().values(
            id=schema.new_id(), tenant_id=tid, user_id=uid,
            role_code="cp_owner", state="ACTIVE", created_by="test"))
    with engine.connect() as conn:
        ctx = resolve_context(conn, user_id=uid, tenant_id=tid)
    return tid, uid, ctx, ctx0


# ---------------------------------------------------------------- migrations
def test_migrate_is_idempotent_and_creates_schema(engine):
    assert runner.migrate(engine) == []            # second run: nothing to do
    st = runner.status(engine)
    assert st and all(applied for _v, _n, applied in st)
    with engine.connect() as conn:
        regions = conn.execute(select(schema.regions)).mappings().all()
    codes = {r["code"] for r in regions}
    assert {"us-east-1", "eu-central-1", "ap-southeast-1"} <= codes


def test_rollback_and_reapply(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    eng = db.get_engine("sqlite:///" + str(tmp_path / "roll.db"))
    applied = runner.migrate(eng)
    assert applied == sorted(applied) and applied[0] == 1   # all, in order
    assert runner.rollback(eng, to=0) == list(reversed(applied))  # LIFO
    st = dict((v, ap) for v, _n, ap in runner.status(eng))
    assert all(ap is False for ap in st.values())
    assert runner.migrate(eng) == applied           # re-apply cleanly


# ------------------------------------------------------------------- tenancy
def test_tenant_slug_validation_and_uniqueness(engine):
    with pytest.raises(ValidationError):
        tenancy.create_tenant(engine, slug="Bad Slug!", legal_name="X", system=True)
    tenancy.create_tenant(engine, slug="dup", legal_name="One", system=True)
    with pytest.raises(ValidationError):
        tenancy.create_tenant(engine, slug="dup", legal_name="Two", system=True)


def test_hierarchy_inherits_tenant_from_parent(engine):
    tid, _uid, ctx, _ = _mk_tenant(engine)
    org = tenancy.create_organization(engine, ctx, slug="hq", name="HQ")
    bu = tenancy.create_business_unit(engine, ctx, org, name="Data")
    ws = tenancy.create_workspace(engine, ctx, bu, slug="mod", name="Modern")
    env = tenancy.create_environment(engine, ctx, ws, name="prod",
                                     kind="PROD", region="eu-central-1")
    with engine.connect() as conn:
        for table, row_id in ((schema.organizations, org),
                              (schema.business_units, bu),
                              (schema.workspaces, ws),
                              (schema.environments, env)):
            row = conn.execute(select(table).where(table.c.id == row_id)) \
                .mappings().one()
            assert row["tenant_id"] == tid
            assert len(row["id"]) == 32            # server-side uuid4 hex


def test_environment_rejects_unknown_region(engine):
    _tid, _uid, ctx, _ = _mk_tenant(engine)
    org = tenancy.create_organization(engine, ctx, slug="o", name="O")
    bu = tenancy.create_business_unit(engine, ctx, org, name="B")
    ws = tenancy.create_workspace(engine, ctx, bu, slug="w", name="W")
    with pytest.raises(ValidationError):
        tenancy.create_environment(engine, ctx, ws, name="x",
                                   region="mars-north-1")


def test_membership_unique_and_revoke(engine):
    _tid, _uid, ctx, _ = _mk_tenant(engine)
    u2 = tenancy.create_user(engine, email="dev@acme.example", system=True)
    mid = tenancy.add_membership(engine, ctx, user_id=u2,
                                 role_code="cp_member")
    with pytest.raises(ValidationError):
        tenancy.add_membership(engine, ctx, user_id=u2,
                               role_code="cp_viewer")
    tenancy.revoke_membership(engine, ctx, mid)
    with engine.connect() as conn:
        with pytest.raises(TenantAccessDenied):
            resolve_context(conn, user_id=u2, tenant_id=ctx.tenant_id)


def test_create_user_is_idempotent_by_email(engine):
    a = tenancy.create_user(engine, email="Same@Case.Example", system=True)
    b = tenancy.create_user(engine, email="same@case.example", system=True)
    assert a == b


# ------------------------------------------------------------ context / RBAC
def test_resolve_context_requires_active_membership(engine):
    tid, uid, _ctx, _ = _mk_tenant(engine)
    outsider = tenancy.create_user(engine, email="mallory@evil.example", system=True)
    with engine.connect() as conn:
        with pytest.raises(TenantAccessDenied) as e:
            resolve_context(conn, user_id=outsider, tenant_id=tid)
        assert e.value.reason == "ACCESS_DENIED"            # uniform, no oracle
        assert e.value.audit_reason == "NO_ACTIVE_MEMBERSHIP"  # internal only
        ok = resolve_context(conn, user_id=uid, tenant_id=tid)
        assert ok.role == "cp_owner" and ok.tenant_id == tid


def test_resolve_context_blocks_suspended_tenant(engine):
    tid, uid, _ctx, staff = _mk_tenant(engine)
    tenancy.set_tenant_status(engine, staff, tid, "SUSPENDED")
    with engine.connect() as conn:
        with pytest.raises(TenantAccessDenied) as e:
            resolve_context(conn, user_id=uid, tenant_id=tid)
        assert e.value.reason == "ACCESS_DENIED"            # uniform, no oracle
        assert e.value.audit_reason == "TENANT_SUSPENDED"   # internal only


def test_rbac_permission_matrix():
    assert rbac.has_permission("cp_owner", "member:manage")
    assert not rbac.has_permission("cp_viewer", "member:manage")
    assert rbac.has_permission("SUPER_ADMIN", "anything:at-all")
    assert not rbac.has_permission("nonexistent-role", "flags:read")  # closed
    assert rbac.map_product_role("owner") == "cp_owner"
    assert rbac.map_product_role("weird") == "cp_viewer"              # closed
    with pytest.raises(PermissionDenied):
        rbac.require("cp_viewer", "org:manage")


def test_viewer_cannot_mutate(engine):
    tid, _uid, ctx, _ = _mk_tenant(engine)
    u2 = tenancy.create_user(engine, email="viewer@acme.example", system=True)
    tenancy.add_membership(engine, ctx, user_id=u2, role_code="cp_viewer")
    with engine.connect() as conn:
        viewer = resolve_context(conn, user_id=u2, tenant_id=tid)
    with pytest.raises(PermissionDenied):
        tenancy.create_organization(engine, viewer, slug="nope", name="N")


def test_staff_status_change_requires_permission(engine):
    tid, _uid, _ctx, _ = _mk_tenant(engine)
    support = staff_context("SUPPORT_ADMIN", "helpdesk")
    with pytest.raises(PermissionDenied):
        tenancy.set_tenant_status(engine, support, tid, "SUSPENDED")
    with pytest.raises(ValidationError):
        tenancy.set_tenant_status(
            engine, staff_context("SUPER_ADMIN", "root"), tid, "BOGUS")
    with pytest.raises(NotFoundError):
        tenancy.set_tenant_status(
            engine, staff_context("SUPER_ADMIN", "root"), "missing", "ACTIVE")

"""Control plane Phase 1 — cross-tenant isolation security tests.

The contract: no service or repository path may read, update, delete or even
*reveal the existence of* another tenant's rows, regardless of what
identifiers the caller supplies.
"""
import pytest
from sqlalchemy import select

from metabridge_control import db, flags, schema, tenancy
from metabridge_control.context import (fetch_scoped, resolve_context,
                                        update_scoped)
from metabridge_control.errors import NotFoundError, TenantAccessDenied
from metabridge_control.migrations import runner


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """Two fully-populated tenants."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    engine = db.get_engine("sqlite:///" + str(tmp_path / "iso.db"))
    runner.migrate(engine)
    out = {"engine": engine}
    for slug in ("alpha", "beta"):
        tid = tenancy.create_tenant(engine, slug=slug, legal_name=slug, system=True)
        uid = tenancy.create_user(engine, email=f"owner@{slug}.example", system=True)
        with engine.begin() as conn:
            conn.execute(schema.memberships.insert().values(
                id=schema.new_id(), tenant_id=tid, user_id=uid,
                role_code="cp_owner", state="ACTIVE", created_by="test"))
        with engine.connect() as conn:
            ctx = resolve_context(conn, user_id=uid, tenant_id=tid)
        org = tenancy.create_organization(engine, ctx, slug="org", name="Org")
        bu = tenancy.create_business_unit(engine, ctx, org, name="BU")
        ws = tenancy.create_workspace(engine, ctx, bu, slug="ws", name="WS")
        out[slug] = {"tid": tid, "uid": uid, "ctx": ctx, "org": org,
                     "bu": bu, "ws": ws}
    return out


def test_cross_tenant_read_is_indistinguishable_from_absent(world):
    engine, a, b = world["engine"], world["alpha"], world["beta"]
    with engine.connect() as conn:
        # foreign row reads as None — same as a genuinely missing row
        assert fetch_scoped(conn, schema.workspaces, a["ctx"], b["ws"]) is None
        assert fetch_scoped(conn, schema.workspaces, a["ctx"],
                            "does-not-exist") is None
    assert tenancy.get_workspace(engine, a["ctx"], b["ws"]) is None


def test_cross_tenant_update_and_delete_refused(world):
    engine, a, b = world["engine"], world["alpha"], world["beta"]
    with engine.begin() as conn:
        with pytest.raises(NotFoundError):
            update_scoped(conn, schema.workspaces, a["ctx"], b["ws"],
                          {"name": "pwned"})
    with pytest.raises(NotFoundError):
        tenancy.rename_workspace(engine, a["ctx"], b["ws"], name="pwned")
    # victim unchanged
    with world["engine"].connect() as conn:
        row = conn.execute(select(schema.workspaces).where(
            schema.workspaces.c.id == b["ws"])).mappings().one()
    assert row["name"] == "WS"


def test_listings_are_tenant_scoped(world):
    engine, a, b = world["engine"], world["alpha"], world["beta"]
    ws_a = {w["id"] for w in tenancy.list_workspaces(engine, a["ctx"])}
    ws_b = {w["id"] for w in tenancy.list_workspaces(engine, b["ctx"])}
    assert a["ws"] in ws_a and b["ws"] not in ws_a
    assert b["ws"] in ws_b and a["ws"] not in ws_b
    orgs_a = {o["id"] for o in tenancy.list_organizations(engine, a["ctx"])}
    assert b["org"] not in orgs_a


def test_cannot_attach_children_to_foreign_parents(world):
    engine, a, b = world["engine"], world["alpha"], world["beta"]
    with pytest.raises(NotFoundError):
        tenancy.create_business_unit(engine, a["ctx"], b["org"], name="X")
    with pytest.raises(NotFoundError):
        tenancy.create_workspace(engine, a["ctx"], b["bu"], slug="x",
                                 name="X")
    with pytest.raises(NotFoundError):
        tenancy.create_environment(engine, a["ctx"], b["ws"], name="x")


def test_client_supplied_tenant_id_is_not_trusted(world):
    """Resolving a context *for another tenant* fails without membership —
    the tenant id the client names is only an input to verification."""
    engine, a, b = world["engine"], world["alpha"], world["beta"]
    with engine.connect() as conn:
        with pytest.raises(TenantAccessDenied):
            resolve_context(conn, user_id=a["uid"], tenant_id=b["tid"])


def test_membership_management_cannot_cross_tenants(world):
    engine, a, b = world["engine"], world["alpha"], world["beta"]
    # A's owner cannot revoke a membership row belonging to B's tenant
    with engine.connect() as conn:
        b_membership = conn.execute(select(schema.memberships.c.id).where(
            schema.memberships.c.tenant_id == b["tid"])).scalar_one()
    with pytest.raises(NotFoundError):
        tenancy.revoke_membership(engine, a["ctx"], b_membership)


def test_flags_do_not_leak_between_tenants(world):
    engine, a, b = world["engine"], world["alpha"], world["beta"]
    flags.set_flag(engine, a["ctx"], "beta.exports", enabled=True,
                   description="tenant A only")
    with engine.connect() as conn:
        assert flags.is_enabled(conn, "beta.exports",
                                tenant_id=a["tid"]) is True
        assert flags.is_enabled(conn, "beta.exports",
                                tenant_id=b["tid"]) is False

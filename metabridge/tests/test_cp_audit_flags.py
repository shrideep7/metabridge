"""Control plane Phase 1 — tamper-evident audit chain + feature flags."""
import pytest
from sqlalchemy import select, update

from metabridge_control import audit, db, flags, schema, tenancy
from metabridge_control.context import resolve_context, staff_context
from metabridge_control.errors import ValidationError
from metabridge_control.migrations import runner


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTROLPLANE_AUDIT_KEY", raising=False)
    eng = db.get_engine("sqlite:///" + str(tmp_path / "audit.db"))
    runner.migrate(eng)
    return eng


@pytest.fixture()
def ctx(engine):
    tid = tenancy.create_tenant(engine, slug="audited", legal_name="Audited", system=True)
    uid = tenancy.create_user(engine, email="owner@audited.example", system=True)
    with engine.begin() as conn:
        conn.execute(schema.memberships.insert().values(
            id=schema.new_id(), tenant_id=tid, user_id=uid,
            role_code="cp_owner", state="ACTIVE", created_by="test"))
    with engine.connect() as conn:
        return resolve_context(conn, user_id=uid, tenant_id=tid)


# --------------------------------------------------------------------- audit
def test_every_commercial_mutation_is_audited_in_transaction(engine, ctx):
    org = tenancy.create_organization(engine, ctx, slug="o", name="O")
    bu = tenancy.create_business_unit(engine, ctx, org, name="B")
    tenancy.create_workspace(engine, ctx, bu, slug="w", name="W")
    with engine.connect() as conn:
        rows = conn.execute(
            select(schema.audit_events)
            .where(schema.audit_events.c.tenant_id == ctx.tenant_id)
            .order_by(schema.audit_events.c.seq)).mappings().all()
    actions = [r["action"] for r in rows]
    assert actions[0] == "tenant.create"
    assert {"organization.create", "business_unit.create",
            "workspace.create"} <= set(actions)
    # contiguous sequence starting at 1
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    with engine.connect() as conn:
        ok, bad = audit.verify_chain(conn, ctx.tenant_id)
    assert ok and bad is None


def test_audit_records_before_and_after_states(engine, ctx):
    org = tenancy.create_organization(engine, ctx, slug="o", name="O")
    bu = tenancy.create_business_unit(engine, ctx, org, name="B")
    ws = tenancy.create_workspace(engine, ctx, bu, slug="w", name="Old name")
    tenancy.rename_workspace(engine, ctx, ws, name="New name")
    with engine.connect() as conn:
        ev = conn.execute(select(schema.audit_events).where(
            (schema.audit_events.c.action == "workspace.rename")
            & (schema.audit_events.c.tenant_id == ctx.tenant_id))) \
            .mappings().one()
    assert ev["before_state"] == {"name": "Old name"}
    assert ev["after_state"] == {"name": "New name"}
    assert ev["resource_id"] == ws


def test_tampering_with_any_field_is_detected(engine, ctx):
    tenancy.create_organization(engine, ctx, slug="o", name="O")
    with engine.begin() as conn:
        target = conn.execute(
            select(schema.audit_events)
            .where(schema.audit_events.c.tenant_id == ctx.tenant_id)
            .order_by(schema.audit_events.c.seq)).mappings().first()
        conn.execute(update(schema.audit_events)
                     .where(schema.audit_events.c.event_id
                            == target["event_id"])
                     .values(actor_id="attacker@forged.example"))
    with engine.connect() as conn:
        ok, first_bad = audit.verify_chain(conn, ctx.tenant_id)
    assert ok is False and first_bad == target["seq"]


def test_deleting_an_event_breaks_contiguity(engine, ctx):
    tenancy.create_organization(engine, ctx, slug="o", name="O")
    tenancy.create_organization(engine, ctx, slug="p", name="P")
    with engine.begin() as conn:
        conn.execute(schema.audit_events.delete().where(
            (schema.audit_events.c.tenant_id == ctx.tenant_id)
            & (schema.audit_events.c.seq == 2)))
    with engine.connect() as conn:
        ok, first_bad = audit.verify_chain(conn, ctx.tenant_id)
    assert ok is False and first_bad == 2


def test_chains_are_per_tenant(engine, ctx):
    tid2 = tenancy.create_tenant(engine, slug="other", legal_name="Other", system=True)
    with engine.connect() as conn:
        assert audit.count_events(conn, ctx.tenant_id) >= 1
        ok1, _ = audit.verify_chain(conn, ctx.tenant_id)
        ok2, _ = audit.verify_chain(conn, tid2)
    assert ok1 and ok2


# --------------------------------------------------------------------- flags
def test_flags_fail_closed(engine, ctx):
    with engine.connect() as conn:
        assert flags.is_enabled(conn, "never.defined",
                                tenant_id=ctx.tenant_id) is False


def test_flag_set_requires_strict_types(engine, ctx):
    with pytest.raises(ValidationError):
        flags.set_flag(engine, ctx, "bad", enabled="yes")       # not a bool
    with pytest.raises(ValidationError):
        flags.set_flag(engine, ctx, "bad", enabled=True, rollout_pct=250)
    with pytest.raises(ValidationError):
        flags.set_flag(engine, ctx, "bad", enabled=True,
                       allowed_roles="cp_owner")                # not a list


def test_tenant_override_beats_global_default(engine, ctx):
    staff = staff_context("SUPER_ADMIN", "root")
    flags.set_flag(engine, staff, "exports.enabled", enabled=True,
                   tenant_scope=schema.GLOBAL_TENANT)
    flags.set_flag(engine, ctx, "exports.enabled", enabled=False)
    with engine.connect() as conn:
        assert flags.is_enabled(conn, "exports.enabled",
                                tenant_id=ctx.tenant_id) is False   # override
        assert flags.is_enabled(conn, "exports.enabled",
                                tenant_id="some-other-tenant") is True


def test_customers_cannot_write_global_or_foreign_scopes(engine, ctx):
    with pytest.raises(ValidationError):
        flags.set_flag(engine, ctx, "g", enabled=True,
                       tenant_scope=schema.GLOBAL_TENANT)
    with pytest.raises(ValidationError):
        flags.set_flag(engine, ctx, "g", enabled=True,
                       tenant_scope="another-tenant-id")


def test_rollout_is_deterministic_and_bounded(engine, ctx):
    flags.set_flag(engine, ctx, "gradual", enabled=True, rollout_pct=50)
    with engine.connect() as conn:
        first = [flags.is_enabled(conn, "gradual", tenant_id=ctx.tenant_id,
                                  subject=f"user-{i}") for i in range(40)]
        second = [flags.is_enabled(conn, "gradual", tenant_id=ctx.tenant_id,
                                   subject=f"user-{i}") for i in range(40)]
    assert first == second                    # deterministic, never random
    assert any(first) and not all(first)      # 50% actually splits
    with engine.connect() as conn:
        assert flags.is_enabled(conn, "gradual", tenant_id=ctx.tenant_id,
                                subject="") is not None  # subjectless is safe


def test_role_restriction(engine, ctx):
    flags.set_flag(engine, ctx, "admin.only", enabled=True,
                   allowed_roles=["cp_owner", "cp_admin"])
    with engine.connect() as conn:
        assert flags.is_enabled(conn, "admin.only", tenant_id=ctx.tenant_id,
                                role="cp_owner") is True
        assert flags.is_enabled(conn, "admin.only", tenant_id=ctx.tenant_id,
                                role="cp_viewer") is False
        assert flags.is_enabled(conn, "admin.only",
                                tenant_id=ctx.tenant_id) is False  # closed


def test_flag_writes_are_audited(engine, ctx):
    flags.set_flag(engine, ctx, "tracked", enabled=True)
    with engine.connect() as conn:
        ev = conn.execute(select(schema.audit_events).where(
            (schema.audit_events.c.action == "flag.set")
            & (schema.audit_events.c.tenant_id == ctx.tenant_id))) \
            .mappings().all()
    assert ev and ev[-1]["after_state"]["enabled"] is True


def test_backdating_occurred_at_is_detected(engine, ctx):
    """The timestamp is hash-bound: altering it breaks verification."""
    import datetime
    tenancy.create_organization(engine, ctx, slug="t", name="T")
    with engine.begin() as conn:
        row = conn.execute(
            select(schema.audit_events)
            .where(schema.audit_events.c.tenant_id == ctx.tenant_id)
            .order_by(schema.audit_events.c.seq)).mappings().first()
        conn.execute(update(schema.audit_events)
                     .where(schema.audit_events.c.event_id == row["event_id"])
                     .values(occurred_at=datetime.datetime(2020, 1, 1)))
    with engine.connect() as conn:
        ok, first_bad = audit.verify_chain(conn, ctx.tenant_id)
    assert ok is False and first_bad == row["seq"]

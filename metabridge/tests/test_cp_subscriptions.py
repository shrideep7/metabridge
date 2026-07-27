"""Phase 2 — subscription & contract lifecycle (state machine)."""
import pytest
from sqlalchemy import select

from metabridge_control import catalog, db, schema, subscriptions as S
from metabridge_control.context import staff_context
from metabridge_control.errors import (NotFoundError, PlanImmutableError,
                                        ValidationError)
from metabridge_control.migrations import runner


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    eng = db.get_engine("sqlite:///" + str(tmp_path / "s.db"))
    runner.migrate(eng)
    from metabridge_control import tenancy
    staff = staff_context("COMMERCIAL_ADMIN", "ops")
    prod = catalog.create_product(eng, staff, code="p", name="P")
    catalog.create_feature(eng, staff, code="limit.users", name="U",
                           value_kind="LIMIT")
    plan = catalog.create_plan(eng, staff, prod, code="ent", name="Ent")
    ver = catalog.create_plan_version(eng, staff, plan)
    catalog.set_plan_feature(eng, staff, ver, "limit.users", limit_value=10)
    catalog.publish_plan_version(eng, staff, ver)
    draft = catalog.create_plan_version(eng, staff, plan)  # a DRAFT version
    tid = tenancy.create_tenant(eng, slug="acme", legal_name="Acme",
                                system=True)
    sctx = staff_context("COMMERCIAL_ADMIN", "ops", tenant_id=tid)
    acct = S.create_customer_account(eng, sctx, name="Acme")
    return {"eng": eng, "sctx": sctx, "tid": tid, "acct": acct,
            "ver": ver, "draft": draft, "plan": plan}


def _state(env, sid):
    with env["eng"].connect() as conn:
        return conn.execute(select(schema.subscriptions.c.state).where(
            schema.subscriptions.c.id == sid)).scalar_one()


def test_subscription_must_pin_published_plan(env):
    with pytest.raises(PlanImmutableError):
        S.create_subscription(env["eng"], env["sctx"], account_id=env["acct"],
                              plan_version_id=env["draft"])


def test_full_activation_flow(env):
    sid = S.create_subscription(env["eng"], env["sctx"],
                                account_id=env["acct"],
                                plan_version_id=env["ver"])
    assert _state(env, sid) == "DRAFT"
    S.activate(env["eng"], env["sctx"], sid)
    assert _state(env, sid) == "ACTIVE"
    sub = S.get_subscription(env["eng"], env["sctx"], sid)
    assert sub["term_start"] and sub["term_end"]
    # transitions history recorded
    tr = S.list_transitions(env["eng"], sid)
    assert [t["to_state"] for t in tr] == ["DRAFT", "PENDING_ACTIVATION",
                                           "ACTIVE"]


def test_illegal_transition_rejected(env):
    sid = S.create_subscription(env["eng"], env["sctx"],
                                account_id=env["acct"],
                                plan_version_id=env["ver"])
    # DRAFT -> SUSPENDED is not a legal edge
    with pytest.raises(ValidationError):
        S.suspend(env["eng"], env["sctx"], sid)


def test_trial_then_convert(env):
    sid = S.create_subscription(env["eng"], env["sctx"],
                                account_id=env["acct"],
                                plan_version_id=env["ver"])
    S.start_trial(env["eng"], env["sctx"], sid, days=14)
    assert _state(env, sid) == "TRIALING"
    sub = S.get_subscription(env["eng"], env["sctx"], sid)
    assert sub["is_trial"] and sub["trial_end"]
    S.convert_trial(env["eng"], env["sctx"], sid)
    assert _state(env, sid) == "ACTIVE"
    assert S.get_subscription(env["eng"], env["sctx"], sid)["is_trial"] is False


def test_dunning_and_reinstate(env):
    sid = S.create_subscription(env["eng"], env["sctx"],
                                account_id=env["acct"],
                                plan_version_id=env["ver"])
    S.activate(env["eng"], env["sctx"], sid)
    S.mark_past_due(env["eng"], env["sctx"], sid)
    assert _state(env, sid) == "PAST_DUE"
    S.suspend(env["eng"], env["sctx"], sid)
    assert _state(env, sid) == "SUSPENDED"
    S.reinstate(env["eng"], env["sctx"], sid)
    assert _state(env, sid) == "ACTIVE"


def test_cancel_and_rescind(env):
    sid = S.create_subscription(env["eng"], env["sctx"],
                                account_id=env["acct"],
                                plan_version_id=env["ver"])
    S.activate(env["eng"], env["sctx"], sid)
    S.cancel(env["eng"], env["sctx"], sid)
    assert _state(env, sid) == "CANCELLED"
    assert S.get_subscription(env["eng"], env["sctx"], sid)["auto_renew"] is False
    S.rescind_cancellation(env["eng"], env["sctx"], sid)
    assert _state(env, sid) == "ACTIVE"


def test_contract_with_po_ref(env):
    cid = S.create_contract(env["eng"], env["sctx"], env["acct"],
                            purchase_order_ref="PO-4471",
                            payment_terms_days=45)
    with env["eng"].connect() as conn:
        row = conn.execute(select(schema.contracts).where(
            schema.contracts.c.id == cid)).mappings().one()
    assert row["purchase_order_ref"] == "PO-4471"
    assert row["payment_terms_days"] == 45 and row["state"] == "DRAFT"
    S.execute_contract(env["eng"], env["sctx"], cid)
    with env["eng"].connect() as conn:
        assert conn.execute(select(schema.contracts.c.state).where(
            schema.contracts.c.id == cid)).scalar_one() == "EXECUTED"


def test_subscription_items(env):
    sid = S.create_subscription(env["eng"], env["sctx"],
                                account_id=env["acct"],
                                plan_version_id=env["ver"])
    S.add_item(env["eng"], env["sctx"], sid, kind="SEAT", ref_code="limit.users",
               quantity=25)
    with pytest.raises(ValidationError):
        S.add_item(env["eng"], env["sctx"], sid, kind="SEAT",
                   ref_code="limit.users", quantity=5)   # duplicate ref
    sub = S.get_subscription(env["eng"], env["sctx"], sid)
    assert sub["items"][0]["quantity"] == 25


def test_terminal_states_have_no_exit(env):
    sid = S.create_subscription(env["eng"], env["sctx"],
                                account_id=env["acct"],
                                plan_version_id=env["ver"])
    S.activate(env["eng"], env["sctx"], sid)
    S.expire(env["eng"], env["sctx"], sid)
    assert _state(env, sid) == "EXPIRED"
    with pytest.raises(ValidationError):
        S.reinstate(env["eng"], env["sctx"], sid)   # no edge out of EXPIRED

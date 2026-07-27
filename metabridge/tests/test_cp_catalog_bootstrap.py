"""Control plane Phase 1 — versioned product catalog + instance bootstrap."""
import json

import pytest
from sqlalchemy import select

from metabridge_control import bootstrap, catalog, db, schema
from metabridge_control.context import resolve_context, staff_context
from metabridge_control.errors import (NotFoundError, PermissionDenied,
                                       PlanImmutableError, ValidationError)
from metabridge_control.migrations import runner


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    eng = db.get_engine("sqlite:///" + str(tmp_path / "cat.db"))
    runner.migrate(eng)
    return eng


@pytest.fixture()
def staff():
    return staff_context("COMMERCIAL_ADMIN", "catalog-admin")


@pytest.fixture()
def plan_draft(engine, staff):
    """product + features + plan + open draft version."""
    prod = catalog.create_product(engine, staff, code="platform",
                                  name="MetaBridge Platform")
    catalog.create_feature(engine, staff, code="feature.sso", name="SSO",
                           value_kind="BOOLEAN")
    catalog.create_feature(engine, staff, code="limit.users",
                           name="Licensed users", value_kind="LIMIT",
                           unit="users")
    plan = catalog.create_plan(engine, staff, prod, code="enterprise",
                               name="Enterprise")
    ver = catalog.create_plan_version(engine, staff, plan)
    return {"product": prod, "plan": plan, "version": ver}


# ------------------------------------------------------------------- catalog
def test_catalog_writes_require_staff_permission(engine):
    viewer = staff_context("READ_ONLY_AUDITOR", "aud")
    with pytest.raises(PermissionDenied):
        catalog.create_product(engine, viewer, code="x", name="X")


def test_product_and_feature_codes_unique(engine, staff, plan_draft):
    with pytest.raises(ValidationError):
        catalog.create_product(engine, staff, code="platform", name="Dup")
    with pytest.raises(ValidationError):
        catalog.create_feature(engine, staff, code="feature.sso", name="Dup",
                               value_kind="BOOLEAN")


def test_feature_values_are_typed(engine, staff, plan_draft):
    ver = plan_draft["version"]
    with pytest.raises(ValidationError):   # BOOLEAN feature given a limit
        catalog.set_plan_feature(engine, staff, ver, "feature.sso",
                                 limit_value=5)
    with pytest.raises(ValidationError):   # LIMIT feature given a bool
        catalog.set_plan_feature(engine, staff, ver, "limit.users",
                                 bool_value=True)
    with pytest.raises(ValidationError):   # negative limit
        catalog.set_plan_feature(engine, staff, ver, "limit.users",
                                 limit_value=-1)
    with pytest.raises(NotFoundError):     # unknown feature code
        catalog.set_plan_feature(engine, staff, ver, "limit.unknown",
                                 limit_value=1)


def test_publish_requires_features_and_freezes_version(engine, staff,
                                                       plan_draft):
    ver = plan_draft["version"]
    with pytest.raises(ValidationError):
        catalog.publish_plan_version(engine, staff, ver)   # empty version
    catalog.set_plan_feature(engine, staff, ver, "feature.sso",
                             bool_value=True)
    catalog.set_plan_feature(engine, staff, ver, "limit.users",
                             limit_value=100)
    catalog.publish_plan_version(engine, staff, ver)
    # PUBLISHED == immutable
    with pytest.raises(PlanImmutableError):
        catalog.set_plan_feature(engine, staff, ver, "limit.users",
                                 limit_value=500)
    row = catalog.get_plan_version(engine, ver)
    assert row["status"] == "PUBLISHED" and row["published_at"] is not None
    assert row["features"]["limit.users"]["limit"] == 100


def test_new_version_copies_published_features_and_pins_old(engine, staff,
                                                            plan_draft):
    ver1 = plan_draft["version"]
    catalog.set_plan_feature(engine, staff, ver1, "feature.sso",
                             bool_value=True)
    catalog.set_plan_feature(engine, staff, ver1, "limit.users",
                             limit_value=100)
    catalog.publish_plan_version(engine, staff, ver1)

    ver2 = catalog.create_plan_version(engine, staff, plan_draft["plan"])
    catalog.set_plan_feature(engine, staff, ver2, "limit.users",
                             limit_value=250)
    catalog.publish_plan_version(engine, staff, ver2)

    v1 = catalog.get_plan_version(engine, ver1)
    v2 = catalog.get_plan_version(engine, ver2)
    assert v1["features"]["limit.users"]["limit"] == 100   # pinned, unchanged
    assert v2["features"]["limit.users"]["limit"] == 250
    assert v2["features"]["feature.sso"]["enabled"] is True  # copied forward
    assert v2["version"] == v1["version"] + 1


def test_single_open_draft_and_retire_rules(engine, staff, plan_draft):
    with pytest.raises(ValidationError):    # draft already open
        catalog.create_plan_version(engine, staff, plan_draft["plan"])
    ver = plan_draft["version"]
    with pytest.raises(ValidationError):    # can't retire a draft
        catalog.retire_plan_version(engine, staff, ver)
    catalog.set_plan_feature(engine, staff, ver, "feature.sso",
                             bool_value=True)
    catalog.publish_plan_version(engine, staff, ver)
    catalog.retire_plan_version(engine, staff, ver)
    assert catalog.get_plan_version(engine, ver)["status"] == "RETIRED"


def test_unlimited_limits(engine, staff, plan_draft):
    ver = plan_draft["version"]
    catalog.set_plan_feature(engine, staff, ver, "limit.users",
                             unlimited=True)
    feats = catalog.get_plan_version(engine, ver)["features"]
    assert feats["limit.users"]["unlimited"] is True
    assert feats["limit.users"]["limit"] is None


def test_publish_requires_publish_permission(engine, staff, plan_draft):
    ver = plan_draft["version"]
    catalog.set_plan_feature(engine, staff, ver, "feature.sso",
                             bool_value=True)
    partner_admin = staff_context("PARTNER_ADMIN", "pa")
    with pytest.raises(PermissionDenied):
        catalog.publish_plan_version(engine, partner_admin, ver)


# ----------------------------------------------------------------- bootstrap
def _write_instance_users(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    users = {
        "owner@customer.example": {"role": "owner", "name": "Owner"},
        "eng@customer.example": {"role": "engineer", "name": "Engineer"},
        "viewer@customer.example": {"role": "viewer", "name": "Viewer"},
    }
    (tmp_path / "users.json").write_text(json.dumps(users))
    return tmp_path


def test_bootstrap_from_instance_is_idempotent_and_readonly(engine, tmp_path):
    auth_dir = _write_instance_users(tmp_path / "instance")
    before = (auth_dir / "users.json").read_text()

    r1 = bootstrap.bootstrap_from_instance(
        engine, auth_dir=str(auth_dir), tenant_slug="customer",
        legal_name="Customer GmbH", home_region="eu-central-1")
    r2 = bootstrap.bootstrap_from_instance(
        engine, auth_dir=str(auth_dir), tenant_slug="customer")

    assert r1["tenant_id"] == r2["tenant_id"]          # idempotent
    assert r1["members_imported"] == 3 and r2["members_imported"] == 0
    assert (auth_dir / "users.json").read_text() == before   # read-only

    # roles mapped through the fail-closed product-role map
    with engine.connect() as conn:
        owner = conn.execute(select(schema.users.c.id).where(
            schema.users.c.email == "owner@customer.example")).scalar_one()
        ctx = resolve_context(conn, user_id=owner,
                              tenant_id=r1["tenant_id"])
        assert ctx.role == "cp_owner"
        viewer = conn.execute(select(schema.users.c.id).where(
            schema.users.c.email == "viewer@customer.example")).scalar_one()
        vctx = resolve_context(conn, user_id=viewer,
                               tenant_id=r1["tenant_id"])
        assert vctx.role == "cp_viewer"


def test_bootstrap_without_users_file(engine, tmp_path):
    empty = tmp_path / "no-users"
    empty.mkdir()
    r = bootstrap.bootstrap_from_instance(engine, auth_dir=str(empty),
                                          tenant_slug="fresh")
    assert r["members_imported"] == 0 and r["workspace_id"]

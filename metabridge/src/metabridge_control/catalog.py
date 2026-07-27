"""Product catalog — products, features, plans and *versioned* plan features.

Normative rules (docs/commercialization/03-domain-model.md §4):

- The catalog is vendor-owned: writes require staff permissions
  (``catalog:manage``; publishing additionally requires ``plans:publish``).
- A plan version in DRAFT is mutable. **A PUBLISHED version is immutable** —
  any change happens on a new DRAFT version. Existing customers stay pinned
  to the version they bought (subscriptions arrive in Phase 2).
- Feature values are typed: BOOLEAN features carry ``bool_value``; LIMIT
  features carry ``limit_value`` or ``unlimited`` — mismatches are rejected.
- No prices anywhere in Phase 1: pricing is a later phase and must never be
  hardcoded.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine

from . import audit, schema
from .context import TenantContext
from .errors import NotFoundError, PlanImmutableError, ValidationError

PRODUCT_TYPES = ("PLATFORM", "ADDON", "PACK")
VALUE_KINDS = ("BOOLEAN", "LIMIT")


def _get(conn: Connection, table, row_id: str) -> dict:
    row = conn.execute(select(table).where(table.c.id == row_id)) \
        .mappings().first()
    if row is None:
        raise NotFoundError(f"{table.name}:{row_id}")
    return dict(row)


# ------------------------------------------------------------------ products
def create_product(engine: Engine, ctx: TenantContext, *, code: str,
                   name: str, product_type: str = "PLATFORM",
                   description: str = "") -> str:
    ctx.require("catalog:manage")
    code = (code or "").strip().lower()
    if not code:
        raise ValidationError("product code is required")
    if product_type not in PRODUCT_TYPES:
        raise ValidationError(f"invalid product type: {product_type}")
    pid = schema.new_id()
    with engine.begin() as conn:
        dup = conn.execute(select(schema.products.c.id).where(
            schema.products.c.code == code)).first()
        if dup:
            raise ValidationError(f"product code exists: {code}")
        conn.execute(schema.products.insert().values(
            id=pid, code=code, name=name, description=description,
            product_type=product_type, created_by=ctx.user_id))
        audit.record(conn, ctx, action="product.create",
                     resource_type="product", resource_id=pid,
                     after={"code": code, "name": name,
                            "product_type": product_type},
                     tenant_id=schema.GLOBAL_TENANT)
    return pid


def create_feature(engine: Engine, ctx: TenantContext, *, code: str,
                   name: str, value_kind: str, unit: Optional[str] = None,
                   description: str = "") -> str:
    ctx.require("catalog:manage")
    code = (code or "").strip().lower()
    if not code:
        raise ValidationError("feature code is required")
    if value_kind not in VALUE_KINDS:
        raise ValidationError(f"invalid value kind: {value_kind}")
    fid = schema.new_id()
    with engine.begin() as conn:
        dup = conn.execute(select(schema.features.c.id).where(
            schema.features.c.code == code)).first()
        if dup:
            raise ValidationError(f"feature code exists: {code}")
        conn.execute(schema.features.insert().values(
            id=fid, code=code, name=name, value_kind=value_kind, unit=unit,
            description=description, created_by=ctx.user_id))
        audit.record(conn, ctx, action="feature.create",
                     resource_type="feature", resource_id=fid,
                     after={"code": code, "value_kind": value_kind},
                     tenant_id=schema.GLOBAL_TENANT)
    return fid


# --------------------------------------------------------------------- plans
def create_plan(engine: Engine, ctx: TenantContext, product_id: str, *,
                code: str, name: str, description: str = "") -> str:
    ctx.require("catalog:manage")
    code = (code or "").strip().lower()
    if not code:
        raise ValidationError("plan code is required")
    plid = schema.new_id()
    with engine.begin() as conn:
        _get(conn, schema.products, product_id)
        dup = conn.execute(select(schema.plans.c.id).where(
            (schema.plans.c.product_id == product_id)
            & (schema.plans.c.code == code))).first()
        if dup:
            raise ValidationError(f"plan code exists for product: {code}")
        conn.execute(schema.plans.insert().values(
            id=plid, product_id=product_id, code=code, name=name,
            description=description, created_by=ctx.user_id))
        audit.record(conn, ctx, action="plan.create", resource_type="plan",
                     resource_id=plid,
                     after={"code": code, "product_id": product_id},
                     tenant_id=schema.GLOBAL_TENANT)
    return plid


def create_plan_version(engine: Engine, ctx: TenantContext, plan_id: str, *,
                        notes: str = "", copy_from_latest: bool = True) -> str:
    """Open a new DRAFT version (next sequential number). By default the
    feature set of the latest PUBLISHED version is copied forward."""
    ctx.require("catalog:manage")
    pvid = schema.new_id()
    pv = schema.plan_versions
    with engine.begin() as conn:
        _get(conn, schema.plans, plan_id)
        current = conn.execute(select(func.max(pv.c.version)).where(
            pv.c.plan_id == plan_id)).scalar()
        version = (current or 0) + 1
        open_draft = conn.execute(select(pv.c.id).where(
            (pv.c.plan_id == plan_id) & (pv.c.status == "DRAFT"))).first()
        if open_draft:
            raise ValidationError("plan already has an open DRAFT version")
        conn.execute(pv.insert().values(
            id=pvid, plan_id=plan_id, version=version, status="DRAFT",
            notes=notes, created_by=ctx.user_id))
        if copy_from_latest and current:
            latest_pub = conn.execute(
                select(pv.c.id).where((pv.c.plan_id == plan_id)
                                      & (pv.c.status == "PUBLISHED"))
                .order_by(pv.c.version.desc()).limit(1)).first()
            if latest_pub:
                rows = conn.execute(select(schema.plan_version_features).where(
                    schema.plan_version_features.c.plan_version_id
                    == latest_pub[0])).mappings().all()
                for r in rows:
                    conn.execute(schema.plan_version_features.insert().values(
                        id=schema.new_id(), plan_version_id=pvid,
                        feature_code=r["feature_code"],
                        bool_value=r["bool_value"],
                        limit_value=r["limit_value"],
                        unlimited=r["unlimited"]))
        audit.record(conn, ctx, action="plan_version.create",
                     resource_type="plan_version", resource_id=pvid,
                     after={"plan_id": plan_id, "version": version},
                     tenant_id=schema.GLOBAL_TENANT)
    return pvid


def _require_draft(conn: Connection, plan_version_id: str) -> dict:
    """Load a plan version and assert it is DRAFT, holding a row lock where
    the dialect supports it so a concurrent publish cannot race a feature
    write (published versions must stay immutable)."""
    q = select(schema.plan_versions).where(
        schema.plan_versions.c.id == plan_version_id)
    if conn.engine.dialect.name not in ("sqlite",):
        q = q.with_for_update()
    row = conn.execute(q).mappings().first()
    if row is None:
        raise NotFoundError(f"plan_versions:{plan_version_id}")
    if row["status"] != "DRAFT":
        raise PlanImmutableError(
            f"plan version {row['version']} is {row['status']} — "
            "published versions are immutable; open a new draft")
    return dict(row)


def set_plan_feature(engine: Engine, ctx: TenantContext,
                     plan_version_id: str, feature_code: str, *,
                     bool_value: Optional[bool] = None,
                     limit_value: Optional[int] = None,
                     unlimited: bool = False) -> None:
    """Attach or update one feature value on a DRAFT version, typed against
    the feature's value kind."""
    ctx.require("catalog:manage")
    pvf = schema.plan_version_features
    with engine.begin() as conn:
        _require_draft(conn, plan_version_id)
        feat = conn.execute(select(schema.features).where(
            schema.features.c.code == feature_code)).mappings().first()
        if feat is None:
            raise NotFoundError(f"features:{feature_code}")
        if feat["value_kind"] == "BOOLEAN":
            if not isinstance(bool_value, bool) or limit_value is not None \
                    or unlimited:
                raise ValidationError(
                    f"feature {feature_code} is BOOLEAN — set bool_value only")
        else:  # LIMIT
            if bool_value is not None:
                raise ValidationError(
                    f"feature {feature_code} is LIMIT — set limit_value or "
                    "unlimited")
            if not unlimited and (not isinstance(limit_value, int)
                                  or limit_value < 0):
                raise ValidationError(
                    "limit_value must be a non-negative integer")
        existing = conn.execute(select(pvf).where(
            (pvf.c.plan_version_id == plan_version_id)
            & (pvf.c.feature_code == feature_code))).mappings().first()
        values = {"bool_value": bool_value, "limit_value":
                  (None if unlimited else limit_value),
                  "unlimited": bool(unlimited)}
        if existing:
            conn.execute(pvf.update().where(pvf.c.id == existing["id"])
                         .values(**values))
            before = {k: existing[k] for k in values}
        else:
            conn.execute(pvf.insert().values(
                id=schema.new_id(), plan_version_id=plan_version_id,
                feature_code=feature_code, **values))
            before = None
        audit.record(conn, ctx, action="plan_version.set_feature",
                     resource_type="plan_version", resource_id=plan_version_id,
                     before=before, after={"feature": feature_code, **values},
                     tenant_id=schema.GLOBAL_TENANT)


def publish_plan_version(engine: Engine, ctx: TenantContext,
                         plan_version_id: str) -> None:
    ctx.require("plans:publish")
    pv = schema.plan_versions
    with engine.begin() as conn:
        row = _require_draft(conn, plan_version_id)
        n = conn.execute(select(func.count()).select_from(
            schema.plan_version_features).where(
            schema.plan_version_features.c.plan_version_id
            == plan_version_id)).scalar_one()
        if n == 0:
            raise ValidationError("cannot publish a version with no features")
        # status-conditional update: only a DRAFT can transition, and the
        # rowcount check closes the publish/publish race
        res = conn.execute(pv.update().where(
            (pv.c.id == plan_version_id) & (pv.c.status == "DRAFT")).values(
            status="PUBLISHED", published_at=schema.utcnow(),
            updated_at=schema.utcnow()))
        if res.rowcount != 1:
            raise PlanImmutableError("version is no longer DRAFT")
        audit.record(conn, ctx, action="plan_version.publish",
                     resource_type="plan_version", resource_id=plan_version_id,
                     before={"status": "DRAFT"},
                     after={"status": "PUBLISHED", "version": row["version"]},
                     tenant_id=schema.GLOBAL_TENANT)


def retire_plan_version(engine: Engine, ctx: TenantContext,
                        plan_version_id: str) -> None:
    ctx.require("plans:publish")
    pv = schema.plan_versions
    with engine.begin() as conn:
        row = _get(conn, pv, plan_version_id)
        if row["status"] != "PUBLISHED":
            raise ValidationError("only PUBLISHED versions can be retired")
        conn.execute(pv.update().where(pv.c.id == plan_version_id).values(
            status="RETIRED", retired_at=schema.utcnow(),
            updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="plan_version.retire",
                     resource_type="plan_version", resource_id=plan_version_id,
                     before={"status": "PUBLISHED"}, after={"status": "RETIRED"},
                     tenant_id=schema.GLOBAL_TENANT)


def effective_features(conn: Connection, plan_version_id: str) -> dict:
    """The resolved feature map of a plan version:
    ``{code: {kind, bool_value|limit_value|unlimited, unit}}``."""
    pvf, feats = schema.plan_version_features, schema.features
    rows = conn.execute(
        select(pvf, feats.c.value_kind, feats.c.unit)
        .join(feats, feats.c.code == pvf.c.feature_code)
        .where(pvf.c.plan_version_id == plan_version_id)).mappings().all()
    out = {}
    for r in rows:
        entry = {"kind": r["value_kind"], "unit": r["unit"]}
        if r["value_kind"] == "BOOLEAN":
            entry["enabled"] = r["bool_value"] is True
        else:
            entry["unlimited"] = bool(r["unlimited"])
            entry["limit"] = None if r["unlimited"] else r["limit_value"]
        out[r["feature_code"]] = entry
    return out


def get_plan_version(engine: Engine, plan_version_id: str) -> dict:
    with engine.connect() as conn:
        row = _get(conn, schema.plan_versions, plan_version_id)
        row["features"] = effective_features(conn, plan_version_id)
        return row

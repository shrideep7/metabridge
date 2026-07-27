"""Instance enrollment — the data-plane ↔ control-plane trust anchor (Phase 5).

An ``Instance`` is a data-plane deployment bound to a tenant. Enrollment is a
two-step, secret-hashed handshake:

1. **Staff mints a one-time enrollment token** for a tenant (optionally scoped
   to a workspace/environment and a subscription), with a TTL. Only its SHA-256
   hash is stored; the plaintext is shown once and placed on the instance.
2. **The instance enrolls** with that token (plus its Ed25519 statement public
   key). The control plane creates the ``Instance`` row and returns a long-lived
   **instance credential** — again stored only as a hash. The token is consumed.

Thereafter the instance authenticates with its credential; the tenant is
**derived** from the credential (never sent by the caller), satisfying the
tenancy mandate (§1). The registered public key is the trust anchor
``metering`` uses to verify that instance's signed usage statements.

Every secret is compared by hash lookup; plaintext secrets are never stored,
logged, or audited (only ids and hashes are).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from . import audit, schema
from .context import TenantContext
from .errors import NotFoundError, TenantAccessDenied, ValidationError

DELIVERY_MODELS = ("MODEL_A_SAAS", "MODEL_B_CONNECTED", "MODEL_C_AIRGAPPED")
_TOKEN_PREFIX = "enr_"
_CRED_PREFIX = "ins_"


def _hash(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def _valid_pubkey(pub_hex: Optional[str]) -> Optional[str]:
    """Accept a 32-byte Ed25519 public key as 64 hex chars, or None."""
    if pub_hex is None:
        return None
    pub_hex = pub_hex.strip().lower()
    try:
        if len(bytes.fromhex(pub_hex)) != 32:
            raise ValueError
    except ValueError:
        raise ValidationError("public_key must be a 32-byte Ed25519 key (hex)")
    return pub_hex


# --------------------------------------------------------------- staff: tokens
def create_enrollment_token(engine: Engine, ctx: TenantContext, *,
                            tenant_id: str, workspace_id: Optional[str] = None,
                            environment_id: Optional[str] = None,
                            delivery_model: str = "MODEL_B_CONNECTED",
                            subscription_id: Optional[str] = None,
                            ttl_hours: int = 72) -> dict:
    """Mint a single-use enrollment token. Returns the plaintext token **once**;
    only its hash is persisted."""
    ctx.require("instances:manage")
    if ctx.tenant_id not in (schema.GLOBAL_TENANT, tenant_id):
        raise ValidationError("tenant scope mismatch")
    if delivery_model not in DELIVERY_MODELS:
        raise ValidationError(f"invalid delivery_model: {delivery_model}")
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    tid = schema.new_id()
    expires = schema.utcnow() + timedelta(hours=int(ttl_hours))
    with engine.begin() as conn:
        conn.execute(schema.instance_enrollment_tokens.insert().values(
            id=tid, tenant_id=tenant_id, workspace_id=workspace_id,
            environment_id=environment_id, token_hash=_hash(token),
            delivery_model=delivery_model, subscription_id=subscription_id,
            state="PENDING", expires_at=expires, created_by=ctx.user_id))
        audit.record(conn, ctx, action="instance.enroll_token.create",
                     resource_type="instance_enrollment_token", resource_id=tid,
                     after={"delivery_model": delivery_model,
                            "expires_at": str(expires)}, tenant_id=tenant_id)
    return {"token_id": tid, "token": token, "expires_at": str(expires)}


# --------------------------------------------------------------- enroll
def enroll_instance(engine: Engine, *, token: str, name: str = "",
                    public_key: Optional[str] = None,
                    fingerprint: Optional[str] = None) -> dict:
    """Consume an enrollment token and provision an Instance. Authenticated by
    the token itself (no staff key). Returns the instance credential **once**.

    A MODEL_C (air-gapped) instance must register a public key so its signed
    statements can be verified; connected models may omit it."""
    pub = _valid_pubkey(public_key)
    th = _hash(token)
    credential = _CRED_PREFIX + secrets.token_urlsafe(32)
    iid = schema.new_id()
    with engine.begin() as conn:
        tok = conn.execute(select(schema.instance_enrollment_tokens).where(
            schema.instance_enrollment_tokens.c.token_hash == th)) \
            .mappings().first()
        # uniform failure: an invalid, expired, or already-used token are
        # indistinguishable to the caller (no token oracle).
        if tok is None or tok["state"] != "PENDING":
            raise TenantAccessDenied("ENROLL_TOKEN_INVALID")
        if tok["expires_at"] is not None and tok["expires_at"] <= schema.utcnow():
            conn.execute(schema.instance_enrollment_tokens.update().where(
                schema.instance_enrollment_tokens.c.id == tok["id"]).values(
                state="EXPIRED", updated_at=schema.utcnow()))
            raise TenantAccessDenied("ENROLL_TOKEN_INVALID")
        if tok["delivery_model"] == "MODEL_C_AIRGAPPED" and pub is None:
            raise ValidationError(
                "an air-gapped instance must register a public_key at enrollment")
        conn.execute(schema.instances.insert().values(
            id=iid, tenant_id=tok["tenant_id"], workspace_id=tok["workspace_id"],
            environment_id=tok["environment_id"], name=name or iid,
            delivery_model=tok["delivery_model"], state="ACTIVE",
            public_key=pub, fingerprint=fingerprint,
            credential_hash=_hash(credential),
            subscription_id=tok["subscription_id"],
            last_seen_at=schema.utcnow(), enrolled_at=schema.utcnow(),
            created_by="instance:" + iid))
        conn.execute(schema.instance_enrollment_tokens.update().where(
            schema.instance_enrollment_tokens.c.id == tok["id"]).values(
            state="CONSUMED", consumed_by=iid, updated_at=schema.utcnow()))
        audit.record(conn, None, action="instance.enroll",
                     resource_type="instance", resource_id=iid,
                     after={"delivery_model": tok["delivery_model"],
                            "has_public_key": pub is not None},
                     tenant_id=tok["tenant_id"], actor_type="INSTANCE",
                     actor_id="instance:" + iid)
    return {"instance_id": iid, "credential": credential,
            "tenant_id": tok["tenant_id"],
            "subscription_id": tok["subscription_id"],
            "delivery_model": tok["delivery_model"]}


# --------------------------------------------------------------- authenticate
def authenticate_instance(engine: Engine, credential: str) -> dict:
    """Resolve a credential to its instance identity, deriving the tenant. Fails
    closed (TenantAccessDenied) for unknown/suspended/revoked instances. Updates
    ``last_seen_at`` (a heartbeat on every authenticated call)."""
    if not (credential or "").strip():
        raise TenantAccessDenied("INSTANCE_AUTH_FAILED")
    ch = _hash(credential)
    with engine.begin() as conn:
        inst = conn.execute(select(schema.instances).where(
            schema.instances.c.credential_hash == ch)).mappings().first()
        if inst is None or inst["state"] != "ACTIVE":
            raise TenantAccessDenied("INSTANCE_AUTH_FAILED")
        conn.execute(schema.instances.update().where(
            schema.instances.c.id == inst["id"]).values(
            last_seen_at=schema.utcnow()))
    return {"instance_id": inst["id"], "tenant_id": inst["tenant_id"],
            "subscription_id": inst["subscription_id"],
            "delivery_model": inst["delivery_model"],
            "workspace_id": inst["workspace_id"],
            "environment_id": inst["environment_id"]}


# --------------------------------------------------------------- trust / mgmt
def instance_public_key(engine: Engine, tenant_id: str,
                        instance_id: str) -> Optional[str]:
    """The pinned Ed25519 public key for an instance, or None. This is the
    server-side trust anchor for verifying that instance's signed statements —
    used by ``metering._resolve_trust_key``."""
    with engine.connect() as conn:
        row = conn.execute(select(schema.instances.c.public_key).where(
            (schema.instances.c.tenant_id == tenant_id)
            & (schema.instances.c.id == instance_id))).first()
    return row[0] if row and row[0] else None


def register_public_key(engine: Engine, ctx: TenantContext, instance_id: str, *,
                        public_key: str) -> None:
    """Register/rotate an instance's statement public key (staff-mediated)."""
    ctx.require("instances:manage")
    pub = _valid_pubkey(public_key)
    with engine.begin() as conn:
        inst = conn.execute(select(schema.instances).where(
            schema.instances.c.id == instance_id)).mappings().first()
        if inst is None:
            raise NotFoundError(f"instances:{instance_id}")
        if ctx.tenant_id not in (schema.GLOBAL_TENANT, inst["tenant_id"]):
            raise NotFoundError(f"instances:{instance_id}")
        conn.execute(schema.instances.update().where(
            schema.instances.c.id == instance_id).values(
            public_key=pub, updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="instance.key.register",
                     resource_type="instance", resource_id=instance_id,
                     after={"key_registered": True}, tenant_id=inst["tenant_id"])


def revoke_instance(engine: Engine, ctx: TenantContext, instance_id: str, *,
                    reason: str) -> None:
    ctx.require("instances:manage")
    if not (reason or "").strip():
        raise ValidationError("revocation requires a reason")
    with engine.begin() as conn:
        inst = conn.execute(select(schema.instances).where(
            schema.instances.c.id == instance_id)).mappings().first()
        if inst is None:
            raise NotFoundError(f"instances:{instance_id}")
        if ctx.tenant_id not in (schema.GLOBAL_TENANT, inst["tenant_id"]):
            raise NotFoundError(f"instances:{instance_id}")
        conn.execute(schema.instances.update().where(
            schema.instances.c.id == instance_id).values(
            state="REVOKED", credential_hash=None,   # invalidate the credential
            updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="instance.revoke",
                     resource_type="instance", resource_id=instance_id,
                     before={"state": inst["state"]}, after={"state": "REVOKED"},
                     reason=reason, tenant_id=inst["tenant_id"])


def list_instances(engine: Engine, ctx: TenantContext, tenant_id: str) -> list:
    ctx.require("instances:manage")
    if ctx.tenant_id not in (schema.GLOBAL_TENANT, tenant_id):
        raise ValidationError("tenant scope mismatch")
    with engine.connect() as conn:
        rows = conn.execute(select(schema.instances).where(
            schema.instances.c.tenant_id == tenant_id)
            .order_by(schema.instances.c.enrolled_at)).mappings().all()
    # never expose credential_hash
    return [{k: v for k, v in dict(r).items() if k != "credential_hash"}
            for r in rows]

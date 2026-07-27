"""License lifecycle + signed license files.

A License ties a Subscription's resolved entitlements to an Instance, with a
validity window and offline grace. The LicenseFile is the signed artifact the
data plane verifies (the air-gap path, wired up in Phase 5). Signing reuses
the marketplace's proven pattern — Ed25519 over canonical JSON binding the
*entire* payload — but is implemented self-contained here (the control plane
never imports the product package).

Lifecycle: issue -> (file) -> activate -> renew / upgrade-downgrade (via
subscription) -> suspend/revoke/expire. Every action is audited.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from . import audit, entitlements as ent, schema
from .context import TenantContext, require_scoped
from .errors import NotFoundError, ValidationError

_KEY_ENV = "CONTROLPLANE_LICENSE_KEY"          # hex-encoded 32-byte seed
_KEY_FILE = "controlplane_license.key"


def _load_signer() -> Tuple[Ed25519PrivateKey, str]:
    seed_hex = os.environ.get(_KEY_ENV, "")
    if seed_hex:
        seed = bytes.fromhex(seed_hex)
    else:
        base = Path(os.environ.get("METABRIDGE_DATA_DIR",
                                   str(Path.home() / ".metabridge")))
        base.mkdir(parents=True, exist_ok=True)
        kf = base / _KEY_FILE
        if not kf.exists():
            kf.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
            kf.chmod(0o600)
        seed = kf.read_bytes()
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    key_id = _key_id(priv.public_key())
    return priv, key_id


def _key_id(pub: Ed25519PublicKey) -> str:
    return hashlib.sha256(pub.public_bytes_raw()).hexdigest()[:16]


def public_key_hex() -> str:
    """The signer's public key (hex) — the trust anchor an instance pins."""
    priv, _ = _load_signer()
    return priv.public_key().public_bytes_raw().hex()


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


# --------------------------------------------------------------- lifecycle
def issue_license(engine: Engine, ctx: TenantContext, subscription_id: str, *,
                  instance_id: Optional[str] = None,
                  offline_grace_days: int = 7) -> str:
    """Snapshot the subscription's resolved entitlements into a new License."""
    ctx.require("licenses:issue")
    lid = schema.new_id()
    with engine.begin() as conn:
        sub = require_scoped(conn, schema.subscriptions, ctx, subscription_id)
        ents = conn.execute(select(schema.entitlements).where(
            schema.entitlements.c.subscription_id == subscription_id)) \
            .mappings().all()
        snapshot = [{"code": e["code"], "value_kind": e["value_kind"],
                     "value": e["value"], "period": e["period"]} for e in ents]
        conn.execute(schema.licenses.insert().values(
            id=lid, tenant_id=ctx.tenant_id, subscription_id=subscription_id,
            instance_id=instance_id, state="ISSUED",
            not_before=sub["term_start"], not_after=sub["term_end"],
            offline_grace_days=offline_grace_days,
            entitlement_snapshot=snapshot, created_by=ctx.user_id))
        audit.record(conn, ctx, action="license.issue",
                     resource_type="license", resource_id=lid,
                     after={"subscription_id": subscription_id,
                            "entitlements": len(snapshot)})
    return lid


def generate_license_file(engine: Engine, ctx: TenantContext,
                          license_id: str) -> dict:
    """Produce the next signed LicenseFile for a license (supersedes prior)."""
    ctx.require("licenses:manage")
    priv, key_id = _load_signer()
    with engine.begin() as conn:
        lic = require_scoped(conn, schema.licenses, ctx, license_id)
        last = conn.execute(select(func.max(schema.license_files.c.serial))
                            .where(schema.license_files.c.license_id
                                   == license_id)).scalar()
        serial = (last or 0) + 1
        payload = {
            "schema": "metabridge.license/1",
            "license_id": license_id, "serial": serial,
            "tenant_id": lic["tenant_id"],
            "subscription_id": lic["subscription_id"],
            "instance_id": lic["instance_id"],
            "not_before": str(lic["not_before"]) if lic["not_before"] else None,
            "not_after": str(lic["not_after"]) if lic["not_after"] else None,
            "offline_grace_days": lic["offline_grace_days"],
            "entitlements": lic["entitlement_snapshot"] or [],
            "signer_key_id": key_id,
        }
        signature = priv.sign(_canonical(payload)).hex()
        fid = schema.new_id()
        conn.execute(schema.license_files.insert().values(
            id=fid, tenant_id=lic["tenant_id"], license_id=license_id,
            serial=serial, canonical_payload=payload, signature=signature,
            signer_key_id=key_id, supersedes_serial=(last or None),
            issued_at=schema.utcnow()))
        audit.record(conn, ctx, action="license.file.generate",
                     resource_type="license", resource_id=license_id,
                     after={"serial": serial, "signer_key_id": key_id})
    return {"id": fid, "serial": serial, "payload": payload,
            "signature": signature, "signer_key_id": key_id}


def verify_license_file(payload: dict, signature_hex: str,
                        trusted_public_key_hex: Optional[str] = None) -> bool:
    """Verify a signed license payload. Fail-closed on any error. An instance
    passes its pinned trust anchor; when omitted the local signer key is used
    (server-side self-check)."""
    try:
        pub_hex = trusted_public_key_hex or public_key_hex()
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        pub.verify(bytes.fromhex(signature_hex), _canonical(payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def activate_license(engine: Engine, ctx: TenantContext, license_id: str, *,
                     instance_id: str, fingerprint: str = "") -> None:
    ctx.require("licenses:manage")
    with engine.begin() as conn:
        lic = require_scoped(conn, schema.licenses, ctx, license_id)
        if lic["state"] not in ("ISSUED", "ACTIVE"):
            raise ValidationError(f"cannot activate a {lic['state']} license")
        conn.execute(schema.licenses.update()
                     .where(schema.licenses.c.id == license_id)
                     .values(state="ACTIVE", instance_id=instance_id,
                             updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="license.activate",
                     resource_type="license", resource_id=license_id,
                     before={"state": lic["state"]},
                     after={"state": "ACTIVE", "instance_id": instance_id})


def revoke_license(engine: Engine, ctx: TenantContext, license_id: str, *,
                   reason: str) -> None:
    ctx.require("licenses:manage")
    if not (reason or "").strip():
        raise ValidationError("revocation requires a reason")
    with engine.begin() as conn:
        lic = require_scoped(conn, schema.licenses, ctx, license_id)
        conn.execute(schema.licenses.update()
                     .where(schema.licenses.c.id == license_id)
                     .values(state="REVOKED", updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="license.revoke",
                     resource_type="license", resource_id=license_id,
                     before={"state": lic["state"]}, after={"state": "REVOKED"},
                     reason=reason)


def renew_license(engine: Engine, ctx: TenantContext, license_id: str, *,
                  not_after) -> None:
    ctx.require("licenses:manage")
    with engine.begin() as conn:
        lic = require_scoped(conn, schema.licenses, ctx, license_id)
        if lic["state"] == "REVOKED":
            raise ValidationError("cannot renew a revoked license")
        conn.execute(schema.licenses.update()
                     .where(schema.licenses.c.id == license_id)
                     .values(not_after=not_after, state="ACTIVE",
                             updated_at=schema.utcnow()))
        audit.record(conn, ctx, action="license.renew",
                     resource_type="license", resource_id=license_id,
                     before={"not_after": str(lic["not_after"])},
                     after={"not_after": str(not_after)})


def get_license(engine: Engine, ctx: TenantContext, license_id: str) -> dict:
    with engine.connect() as conn:
        lic = require_scoped(conn, schema.licenses, ctx, license_id)
        files = conn.execute(select(schema.license_files).where(
            schema.license_files.c.license_id == license_id)
            .order_by(schema.license_files.c.serial)).mappings().all()
    out = dict(lic)
    out["files"] = [dict(f) for f in files]
    return out

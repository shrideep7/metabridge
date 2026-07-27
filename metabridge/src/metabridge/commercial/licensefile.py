"""Offline license-file verification (EB-502).

The control plane issues a signed ``metabridge.license/1`` file (see
``metabridge_control.licensing.generate_license_file``): a canonical JSON
payload plus an Ed25519 signature over it. An air-gapped instance verifies that
file **fully offline** against a pinned public key — no network, no control
plane, no shared code.

Two things are re-derived here to keep the product free of any control-plane
import:

- ``_canonical`` — byte-identical to the control plane's canonicalization
  (``json.dumps(sort_keys=True, separators=(",",":"), default=str)``); if these
  drift, every signature fails, so the round-trip test pins it.
- Ed25519 verification via ``cryptography`` (already a product dependency, used
  by the marketplace signer).

The trust anchor (the signer's public key) is supplied by the caller — it is
provisioned at enrollment / install time and pinned, **never** taken from the
license file itself (a self-vouching key proves nothing).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

LICENSE_SCHEMA = "metabridge.license/1"

# validity statuses
VALID = "VALID"
GRACE = "GRACE"                # past not_after but inside offline grace window
EXPIRED = "EXPIRED"
NOT_YET_VALID = "NOT_YET_VALID"


class LicenseError(Exception):
    """A license file is malformed, untrusted, or its signature is invalid."""


def _canonical(payload: dict) -> bytes:
    """Byte-for-byte identical to the control plane's license canonicalization.
    Any divergence here breaks every signature — covered by the round-trip
    test against a real control-plane-issued file."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def _parse_dt(value) -> Optional[datetime]:
    """Parse a control-plane timestamp string (``str(datetime)`` form) to a
    naive datetime, or None. Unparseable => None (treated as unbounded)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class LicenseFile:
    """A verified (or verifiable) license file. Construct via
    ``load_license_file`` so the signature is always checked."""
    payload: dict
    signature: str
    verified: bool

    # --- identity / metadata -------------------------------------------------
    @property
    def tenant_id(self) -> str:
        return self.payload.get("tenant_id", "")

    @property
    def subscription_id(self) -> str:
        return self.payload.get("subscription_id", "")

    @property
    def instance_id(self) -> Optional[str]:
        return self.payload.get("instance_id")

    @property
    def serial(self) -> int:
        return int(self.payload.get("serial", 0))

    @property
    def signer_key_id(self) -> str:
        return self.payload.get("signer_key_id", "")

    @property
    def offline_grace_days(self) -> int:
        return int(self.payload.get("offline_grace_days", 0) or 0)

    @property
    def entitlements(self) -> list:
        return list(self.payload.get("entitlements", []) or [])

    # --- validity ------------------------------------------------------------
    def status(self, now: Optional[datetime] = None) -> str:
        """Time-based validity, accounting for offline grace. Revocation is a
        *connected*-path concern; an offline file is trusted until it expires."""
        now = now or datetime.utcnow()
        nb = _parse_dt(self.payload.get("not_before"))
        na = _parse_dt(self.payload.get("not_after"))
        if nb is not None and now < nb:
            return NOT_YET_VALID
        if na is not None:
            if now <= na:
                return VALID
            if now <= na + timedelta(days=self.offline_grace_days):
                return GRACE
            return EXPIRED
        return VALID

    def is_currently_valid(self, now: Optional[datetime] = None) -> bool:
        return self.status(now) in (VALID, GRACE)


def load_license_file(source, trusted_public_key_hex: str,
                      *, require_verified: bool = True) -> LicenseFile:
    """Load and verify a license file.

    ``source`` is a dict (``{"payload": ..., "signature": ...}``), a raw
    payload+signature via that shape, a JSON string, or a filesystem path.
    ``trusted_public_key_hex`` is the pinned signer key. Fails closed: a bad
    signature raises ``LicenseError`` unless ``require_verified=False`` (which
    returns an unverified file for diagnostics only — never for enforcement).
    """
    data = _coerce(source)
    payload = data.get("payload")
    signature = data.get("signature", "")
    if not isinstance(payload, dict) or not signature:
        raise LicenseError("license file needs a 'payload' object and "
                           "'signature'")
    if payload.get("schema") != LICENSE_SCHEMA:
        raise LicenseError(
            f"unexpected license schema: {payload.get('schema')!r}")
    verified = _verify(payload, signature, trusted_public_key_hex)
    if require_verified and not verified:
        raise LicenseError("license signature verification failed "
                           "(untrusted key or tampered payload)")
    return LicenseFile(payload=payload, signature=signature, verified=verified)


def _coerce(source) -> dict:
    if isinstance(source, dict):
        return source
    text = None
    if hasattr(source, "read"):                       # file-like
        text = source.read()
    elif isinstance(source, (str, bytes)):
        s = source.decode() if isinstance(source, bytes) else source
        stripped = s.lstrip()
        if stripped.startswith("{"):                  # JSON document
            text = s
        else:                                         # filesystem path
            with open(s, "r", encoding="utf-8") as fh:
                text = fh.read()
    if text is None:
        raise LicenseError("unsupported license source")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise LicenseError(f"license file is not valid JSON: {exc}") from exc


def _verify(payload: dict, signature_hex: str,
            trusted_public_key_hex: str) -> bool:
    """Ed25519 verify, fail-closed on any error (bad hex, wrong key, tamper)."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(trusted_public_key_hex))
        pub.verify(bytes.fromhex(signature_hex), _canonical(payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False

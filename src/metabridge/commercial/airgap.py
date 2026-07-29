"""Air-gapped usage export (EB-505, instance half).

A Model-C (air-gapped) instance cannot POST usage. Instead it exports a signed
usage statement to a file, which is carried out-of-band and imported by the
control plane (``metabridge_control.metering.ingest_signed_statement``). The
payload shape and canonicalization match that importer exactly, so the artifact
this produces verifies and ingests without any control-plane change to the
*format*.

Signing uses an **instance** Ed25519 key (created and stored 0600 under the
data dir, like the control plane's own keys). The one remaining paired task
lives in the control plane: pinning this instance's public key as the trust
anchor at enrollment — ``metering._resolve_trust_key`` currently pins the
control-plane signer (a documented Phase-5 placeholder). Until that lands the
round-trip is provable instance-side (``verify_statement`` with the instance's
own public key); the wire format is already final.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

_INSTANCE_KEY_FILE = "instance_statement.key"


def _canonical(payload: dict) -> bytes:
    """Identical to the control plane's statement canonicalization."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def build_usage_statement(*, tenant_id: str, instance_id: str, serial: str,
                          period_start, period_end, lines: List[dict]) -> dict:
    """Assemble the canonical statement payload.

    ``lines`` is an iterable of ``{"meter_code": str, "usage": int}`` (extra
    keys are dropped so the artifact carries only what the importer reads)."""
    return {
        "tenant_id": tenant_id,
        "instance_id": instance_id,
        "serial": serial,
        "period_start": str(period_start),
        "period_end": str(period_end),
        "lines": [{"meter_code": ln["meter_code"],
                   "usage": int(ln.get("usage", 0))} for ln in lines],
    }


def sign_statement(payload: dict, private_key_hex: str) -> dict:
    """Sign a statement payload with an instance private key (hex). Returns the
    envelope the control plane imports: ``{payload, signature, signer_public_key}``."""
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    signature = priv.sign(_canonical(payload)).hex()
    return {"payload": payload, "signature": signature,
            "signer_public_key": priv.public_key().public_bytes_raw().hex()}


def verify_statement(payload: dict, signature_hex: str,
                     trusted_public_key_hex: str) -> bool:
    """Fail-closed verification against a pinned public key — mirrors the
    control-plane importer so instance-side round-trip tests are authoritative."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(trusted_public_key_hex))
        pub.verify(bytes.fromhex(signature_hex), _canonical(payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def load_or_create_instance_key(data_dir: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(private_hex, public_hex)`` for this instance's statement key,
    creating a fresh 0600 key file under the data dir on first use. The public
    half is what an operator registers with the control plane at enrollment."""
    import os
    base = Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR", ".")) \
        / "commercial"
    base.mkdir(parents=True, exist_ok=True)
    kf = base / _INSTANCE_KEY_FILE
    if not kf.exists():
        kf.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
        kf.chmod(0o600)
    priv = Ed25519PrivateKey.from_private_bytes(kf.read_bytes())
    return (priv.private_bytes_raw().hex(),
            priv.public_key().public_bytes_raw().hex())

"""Marketplace item model + Ed25519 package signing.

Signing is real asymmetric crypto: a publisher signs the canonical
package bytes with an Ed25519 PRIVATE key; the marketplace verifies with
the publisher's PUBLIC key held in a trust store. Any tampering with the
payload, a forged signature, or an untrusted/unknown publisher fails
verification — an unsigned package is `unsigned` and blocked from
install by default.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

from ..plugins.spec import version_compatible, METABRIDGE_API_VERSION

try:                                    # the running MetaBridge version
    from .. import __version__ as MB_VERSION
except Exception:                       # pragma: no cover - defensive
    MB_VERSION = "0"

ITEM_TYPES = (
    "connector", "validator", "ai_skill", "pipeline_template",
    "industry_accelerator", "migration_template", "business_rules",
    "transformation_library",
)
# marketplace item type -> the plugin type it registers as (if any)
_PLUGIN_TYPE = {
    "connector": "source_connector", "validator": "validator",
    "ai_skill": "ai_reviewer",
}

LICENSES = ("MIT", "Apache-2.0", "BSD-3-Clause", "Commercial",
            "Enterprise-EULA", "Proprietary")


class MarketplaceError(Exception):
    """Marketplace / packaging / install error."""


# ---------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------

def canonical_bytes(payload) -> bytes:
    """Deterministic byte serialization the signature is computed over."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")


# The exact set of fields BOUND by the signature. It is not enough to sign
# only the payload bytes: identity (id/type/name/version/publisher) and the
# gate-driving metadata (dependencies/compatibility/license) must be signed
# too, otherwise a validly-signed payload can be relabelled onto any item id
# / version, or have its license/compatibility stripped, and still verify
# (impersonation, downgrade, license-gate bypass, dependency injection).
# ``versions`` (a catalog aggregation) and the checksum/signature themselves
# are intentionally excluded.
_SIGNED_FIELDS = ("id", "type", "name", "version", "publisher",
                  "dependencies", "compatibility", "license", "payload")


def manifest_of(item) -> dict:
    """The dict whose canonical bytes are signed / verified for an item."""
    return {k: getattr(item, k) for k in _SIGNED_FIELDS}


def item_signing_bytes(item) -> bytes:
    return canonical_bytes(manifest_of(item))


def generate_keypair() -> Dict[str, str]:
    """A fresh Ed25519 keypair for a publisher (hex-encoded)."""
    priv = Ed25519PrivateKey.generate()
    return {"private_key": priv.private_bytes_raw().hex(),
            "public_key": priv.public_key().public_bytes_raw().hex()}


def _key_from_seed(seed: bytes) -> Ed25519PrivateKey:
    """Deterministic private key from a seed (first-party publisher /
    reproducible tests)."""
    s = (seed + b"\x00" * 32)[:32]
    return Ed25519PrivateKey.from_private_bytes(s)


def public_key_of(private_key_hex: str) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(private_key_hex))
    return priv.public_key().public_bytes_raw().hex()


def _sign_bytes(body: bytes, private_key_hex: str) -> Dict[str, str]:
    priv = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(private_key_hex))
    return {"checksum": hashlib.sha256(body).hexdigest(),
            "signature": priv.sign(body).hex()}


def sign_payload(payload, private_key_hex: str) -> Dict[str, str]:
    """Low-level: sign an arbitrary canonical object. Prefer ``sign_item``
    for marketplace items — it binds identity and metadata, not just the
    payload."""
    return _sign_bytes(canonical_bytes(payload), private_key_hex)


def sign_item(item, private_key_hex: str):
    """Sign the item's full manifest (identity + metadata + payload) and
    set its checksum + signature in place. Returns the item."""
    sig = _sign_bytes(item_signing_bytes(item), private_key_hex)
    item.checksum, item.signature = sig["checksum"], sig["signature"]
    return item


class TrustStore:
    """publisher id -> Ed25519 public key (hex). Only packages signed by
    a trusted publisher's private key verify."""

    def __init__(self, keys: Optional[Dict[str, str]] = None) -> None:
        self._keys: Dict[str, str] = dict(keys or {})

    def trust(self, publisher: str, public_key_hex: str) -> None:
        self._keys[publisher] = public_key_hex

    def revoke(self, publisher: str) -> bool:
        return self._keys.pop(publisher, None) is not None

    def public_key(self, publisher: str) -> Optional[str]:
        return self._keys.get(publisher)

    def publishers(self) -> List[str]:
        return sorted(self._keys)


def verify_item(item: "MarketplaceItem", trust: TrustStore) -> dict:
    """Verify a package's signature against the trust store.

    Returns {verified, status, publisher}. status is one of
    ``verified``, ``unsigned``, ``untrusted_publisher``,
    ``checksum_mismatch``, ``signature_invalid``.
    """
    if not item.signature:
        return {"verified": False, "status": "unsigned",
                "publisher": item.publisher}
    pub_hex = trust.public_key(item.publisher)
    if pub_hex is None:
        return {"verified": False, "status": "untrusted_publisher",
                "publisher": item.publisher}
    body = item_signing_bytes(item)
    # cheap tamper check first, over the full manifest (identity + metadata
    # + payload). A signed item always carries a checksum, so a missing one
    # fails closed rather than skipping the integrity check.
    if not item.checksum or hashlib.sha256(body).hexdigest() != item.checksum:
        return {"verified": False, "status": "checksum_mismatch",
                "publisher": item.publisher}
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        pub.verify(bytes.fromhex(item.signature), body)
    except (InvalidSignature, ValueError):
        return {"verified": False, "status": "signature_invalid",
                "publisher": item.publisher}
    return {"verified": True, "status": "verified",
            "publisher": item.publisher}


# ---------------------------------------------------------------------------
# item model
# ---------------------------------------------------------------------------

@dataclass
class MarketplaceItem:
    id: str
    type: str
    name: str
    version: str                              # this package's version
    publisher: str = ""
    description: str = ""
    versions: List[str] = field(default_factory=list)   # all published
    license: dict = field(default_factory=dict)         # {type, requires_acceptance,url}
    compatibility: dict = field(default_factory=dict)   # {metabridge, plugin_api}
    dependencies: List[dict] = field(default_factory=list)  # [{id,version}]
    payload: dict = field(default_factory=dict)         # files / plugin spec
    checksum: str = ""
    signature: str = ""

    def validate(self) -> "MarketplaceItem":
        if not self.id:
            raise MarketplaceError("item id is required")
        if self.type not in ITEM_TYPES:
            raise MarketplaceError("unknown item type %r — one of %s"
                                   % (self.type, ", ".join(ITEM_TYPES)))
        if not self.name or not self.version:
            raise MarketplaceError("item %s: name and version required"
                                   % self.id)
        if not isinstance(self.dependencies, list):
            raise MarketplaceError("item %s: dependencies must be a list"
                                   % self.id)
        for d in self.dependencies:
            if not isinstance(d, dict) or "id" not in d:
                raise MarketplaceError("item %s: each dependency needs an "
                                       "'id'" % self.id)
        if self.version not in self.versions:
            self.versions = sorted(set(self.versions) | {self.version})
        return self

    def plugin_type(self) -> Optional[str]:
        return _PLUGIN_TYPE.get(self.type)

    def platform_compatible(self, api: str = METABRIDGE_API_VERSION,
                            mb_version: str = MB_VERSION) -> dict:
        """Check declared compatibility. Returns {compatible, reasons}.

        A declared MetaBridge-version requirement is enforced against the
        running version (defaulting to the host version), failing closed if
        the host version is somehow unknown — an unenforceable requirement
        must refuse, not silently install."""
        reasons = []
        api_req = self.compatibility.get("plugin_api", "")
        if api_req and not version_compatible(api_req, api):
            reasons.append("requires plugin API %s (have %s)"
                           % (api_req, api))
        mb_req = self.compatibility.get("metabridge", "")
        if mb_req:
            if not mb_version:
                reasons.append("requires MetaBridge %s but the host version "
                               "is unknown" % mb_req)
            elif not version_compatible(mb_req, mb_version):
                reasons.append("requires MetaBridge %s (have %s)"
                               % (mb_req, mb_version))
        return {"compatible": not reasons, "reasons": reasons}

    def requires_license_acceptance(self) -> bool:
        return bool(self.license.get("requires_acceptance"))

    def to_dict(self, trust: Optional[TrustStore] = None) -> dict:
        d = {"id": self.id, "type": self.type, "name": self.name,
             "version": self.version, "publisher": self.publisher,
             "description": self.description,
             "versions": sorted(set(self.versions) | {self.version}),
             "license": self.license, "compatibility": self.compatibility,
             "dependencies": self.dependencies,
             "signed": bool(self.signature),
             "checksum": self.checksum[:16] + "…" if self.checksum else ""}
        if trust is not None:
            d["signature_status"] = verify_item(self, trust)["status"]
            d["compatible"] = self.platform_compatible()["compatible"]
        return d


# the first-party publisher signs the built-in catalog with a fixed
# deterministic key; the trust store below holds its public key
FIRST_PARTY_PUBLISHER = "metabridge"
_FIRST_PARTY_PRIV = _key_from_seed(b"metabridge-marketplace-ed25519!!")
FIRST_PARTY_PRIVATE_HEX = _FIRST_PARTY_PRIV.private_bytes_raw().hex()
FIRST_PARTY_PUBLIC_HEX = \
    _FIRST_PARTY_PRIV.public_key().public_bytes_raw().hex()


def default_trust_store() -> TrustStore:
    return TrustStore({FIRST_PARTY_PUBLISHER: FIRST_PARTY_PUBLIC_HEX})

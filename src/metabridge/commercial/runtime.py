"""Ambient bridge accessor — the process-wide seam engines call (EB-503/504).

Engines don't construct a ``CommercialBridge``; they call ``runtime.allows`` /
``runtime.enforce`` / ``runtime.report_usage``. Those resolve one bridge built
from the data dir (feature flags + an optional pinned license file).

The load-bearing property: **when nothing commercial is configured, this is a
zero-side-effect no-op.** If there is no flags file and no ``commercial/``
directory, ``get_bridge`` returns a bridge backed by ``_NullFlags`` (which
never touches the filesystem and always evaluates OFF), so an ordinary product
run — and every existing test — behaves byte-for-byte as before. Only an
operator who provisions flags/license opts in.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

from .bridge import BridgeDecision, CommercialBridge
from .entitlements import EntitlementSet
from .licensefile import LicenseError, load_license_file
from .usage import UsageSpool


class _NullFlags:
    """Stand-in used when nothing commercial is configured: always OFF, never
    reads or creates any file."""

    def evaluate(self, key: str, subject: str = "", role: str = "") -> bool:
        return False


_cache: dict = {"dir": None, "bridge": None, "spool": None}


def _data_dir() -> Path:
    return Path(os.environ.get("METABRIDGE_DATA_DIR", "."))


def _configured(dd: Path) -> bool:
    return (dd / "platform" / "flags.json").exists() or (dd / "commercial").is_dir()


def _load_license(dd: Path):
    lic = dd / "commercial" / "license.json"
    trust = dd / "commercial" / "trust_key"
    if lic.exists() and trust.exists():
        try:
            return load_license_file(str(lic), trust.read_text(encoding="utf-8").strip())
        except (LicenseError, OSError):
            return None                       # unusable license => no entitlements
    return None


def get_bridge(refresh: bool = False) -> CommercialBridge:
    dd = _data_dir()
    key = str(dd)
    if not _configured(dd):
        # unconfigured: fresh no-op bridge, nothing cached, nothing written
        return CommercialBridge(EntitlementSet([]), _NullFlags())
    if not refresh and _cache["dir"] == key and _cache["bridge"] is not None:
        return _cache["bridge"]
    from ..platform.flags import FeatureFlags
    lf = _load_license(dd)
    ents = EntitlementSet(lf.entitlements) if lf else EntitlementSet([])
    subject = (lf.instance_id if lf else "") or ""
    bridge = CommercialBridge(ents, FeatureFlags(str(dd)), subject=subject,
                              license_file=lf)
    _cache.update(dir=key, bridge=bridge, spool=None)
    return bridge


def _spool() -> UsageSpool:
    if _cache.get("spool") is None:
        _cache["spool"] = UsageSpool(
            _data_dir() / "commercial" / "usage_spool.json")
    return _cache["spool"]


def reset() -> None:
    """Drop cached state (tests, or after (re)provisioning license/flags)."""
    _cache.update(dir=None, bridge=None, spool=None)


# -- enforcement seam -------------------------------------------------------
def check(capability: str, **kw) -> BridgeDecision:
    return get_bridge().check(capability, **kw)


def enforce(capability: str, **kw) -> BridgeDecision:
    return get_bridge().enforce(capability, **kw)


def allows(capability: str, *, quantity: int = 1, current_usage: int = 0) -> bool:
    """Fail-soft check for advisory features: True unless enforcement is on AND
    the capability is not entitled. OFF (default) => True."""
    return get_bridge().check(capability, quantity=quantity,
                              current_usage=current_usage).allowed


# -- usage emission (EB-504) ------------------------------------------------
def report_usage(meter_code: str, quantity: int, *,
                 idempotency_key: Optional[str] = None,
                 dimensions: Optional[dict] = None,
                 subscription_id: Optional[str] = None) -> bool:
    """Spool one usage event. No-op (returns False, writes nothing) unless the
    ``commercial_usage_reporting`` flag is on."""
    if not get_bridge().usage_reporting_enabled():
        return False
    return _spool().add(meter_code, int(quantity),
                        idempotency_key or uuid.uuid4().hex,
                        dimensions=dimensions, subscription_id=subscription_id)


def report_assessment(*, objects: int, project: str = "") -> None:
    """Record that a deterministic assessment ran (ASSESSMENTS + OBJECTS_ASSESSED).
    Never gates the engine — emission only."""
    dims = {"project": project} if project else None
    report_usage("ASSESSMENTS", 1, dimensions=dims)
    if objects:
        report_usage("OBJECTS_ASSESSED", int(objects), dimensions=dims)


def report_export(kind: str = "report") -> None:
    report_usage("REPORT_EXPORTS", 1, dimensions={"kind": kind})

"""Feature Flags — a platform service for gating capabilities.

Deterministic evaluation: a flag is on for a subject iff it is enabled,
the subject's role is in the (optional) role allow-list, and the
subject falls inside the rollout bucket. Rollout bucketing is a stable
SHA-256 hash of (key, subject) — never random — so the same subject
always gets the same answer and evaluation is reproducible and testable.
File-backed under ``DATA_DIR/platform/flags.json``.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from ._util import atomic_write_json, file_lock

# seeded capability flags (all on by default; operators can flip/roll out)
DEFAULT_FLAGS = {
    "agent_orchestration": "Run the 12-agent orchestration.",
    "observability": "Operational observability dashboards.",
    "marketplace_publishing": "Publish/sign packages to the marketplace.",
    "ai_llm_assist": "Advisory LLM assist (off by default in engines).",
    "auto_apply_fixes": "Approve-and-apply auto-fix queue.",
}
_LLM_DEFAULT_OFF = {"ai_llm_assist"}


class FeatureFlags:
    def __init__(self, data_dir: str = "") -> None:
        base = Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR",
                                               ".")) / "platform"
        base.mkdir(parents=True, exist_ok=True)
        self._file = base / "flags.json"
        self._lock = base / "flags.lock"

    def _now(self) -> float:
        return time.time()

    @staticmethod
    def _norm(rec: dict) -> dict:
        """Coerce a (possibly hand-edited / externally-written) flag record
        to safe types so evaluation can NEVER be tricked by a stored string
        (fail-closed): only real JSON ``true`` enables; roles must be a
        list; rollout is an int in [0,100]."""
        rec["enabled"] = rec.get("enabled") is True
        roles = rec.get("roles")
        rec["roles"] = [str(r) for r in roles] \
            if isinstance(roles, (list, tuple)) else []
        rp = rec.get("rollout_pct", 100)
        rec["rollout_pct"] = max(0, min(100, int(rp))) \
            if isinstance(rp, (int, float)) and not isinstance(rp, bool) \
            else 100
        return rec

    def _load(self) -> dict:
        stored = {}
        if self._file.exists():
            try:
                stored = json.loads(self._file.read_text(encoding="utf-8")) or {}
            except (ValueError, OSError):
                stored = {}
        # merge seeded defaults so a fresh instance has the known flags
        flags = {}
        for key, desc in DEFAULT_FLAGS.items():
            flags[key] = {"key": key, "description": desc,
                          "enabled": key not in _LLM_DEFAULT_OFF,
                          "rollout_pct": 100, "roles": [], "seeded": True}
        for key, rec in (stored.items() if isinstance(stored, dict) else []):
            if isinstance(rec, dict):
                base_rec = flags.get(key, {"key": key, "description": "",
                                           "enabled": False,
                                           "rollout_pct": 100, "roles": [],
                                           "seeded": False})
                base_rec.update(rec)
                base_rec["key"] = key
                flags[key] = base_rec
        # normalize EVERY record so a stored non-bool/non-list value can
        # never fail open in evaluate()
        for key in flags:
            flags[key] = self._norm(flags[key])
            flags[key]["key"] = key
        return flags

    def _save_overrides(self, key: str, patch: dict) -> None:
        with file_lock(self._lock):
            stored = {}
            if self._file.exists():
                try:
                    stored = json.loads(self._file.read_text(encoding="utf-8")) or {}
                except (ValueError, OSError):
                    stored = {}
            if not isinstance(stored, dict):
                stored = {}
            rec = stored.get(key, {}) \
                if isinstance(stored.get(key), dict) else {}
            rec.update(patch)
            rec["updated"] = self._now()
            stored[key] = rec
            atomic_write_json(self._file, stored)

    # -- read ---------------------------------------------------------------
    def all(self) -> List[dict]:
        return sorted(self._load().values(), key=lambda f: f["key"])

    def get(self, key: str) -> Optional[dict]:
        return self._load().get(key)

    # -- write --------------------------------------------------------------
    def set(self, key: str, enabled: Optional[bool] = None,
            rollout_pct: Optional[int] = None,
            roles: Optional[List[str]] = None,
            description: str = "") -> dict:
        patch = {}
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if rollout_pct is not None:
            patch["rollout_pct"] = max(0, min(100, int(rollout_pct)))
        if roles is not None:
            patch["roles"] = [str(r) for r in roles]
        if description:
            patch["description"] = description
        if not patch:
            raise ValueError("no flag fields to update")
        self._save_overrides(key, patch)
        return self.get(key)

    # -- evaluate -----------------------------------------------------------
    def _bucket(self, key: str, subject: str) -> int:
        h = hashlib.sha256(("%s:%s" % (key, subject)).encode("utf-8"))
        return int(h.hexdigest(), 16) % 100

    def evaluate(self, key: str, subject: str = "",
                 role: str = "") -> bool:
        """Deterministic: enabled AND role-allowed AND inside the rollout."""
        f = self.get(key)
        if f is None or not f.get("enabled"):
            return False
        allow = f.get("roles") or []
        if allow and role not in allow:
            return False
        pct = int(f.get("rollout_pct", 100))
        if pct >= 100:
            return True
        if pct <= 0:
            return False
        return self._bucket(key, subject or "") < pct

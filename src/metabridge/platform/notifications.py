"""Notifications — an in-app notification center / event log platform
service.

Honest scope: this is an in-application event bus + a bounded, persisted
delivery log (topics, severities, seen/unseen) — NOT an email/SMS/paging
gateway. Other services (e.g. observability alerts, the approval queue,
migration completion) publish here; the console surfaces the feed. File-
backed under ``DATA_DIR/platform/notifications.json`` with a capped log
and atomic writes.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ._util import atomic_write_json, file_lock

SEVERITIES = ("info", "success", "warning", "critical")
_MAX_LOG = 500


class NotificationCenter:
    def __init__(self, data_dir: str = "") -> None:
        base = Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR",
                                               ".")) / "platform"
        base.mkdir(parents=True, exist_ok=True)
        self._file = base / "notifications.json"
        self._lock = base / "notifications.lock"

    def _now(self) -> float:
        return time.time()

    def _seed(self) -> List[dict]:
        now = time.time()
        day = 86400
        return [
            {
                "id": "notif-001",
                "topic": "members",
                "title": "Member removed",
                "body": "john.smith@metafordata.com · Removed by Mahesh Sutar",
                "severity": "info",
                "seen": False,
                "at": "",
                "ts": now - 1020,
            },
            {
                "id": "notif-002",
                "topic": "governance",
                "title": "Governance: 1 violation detected",
                "body": "Sensitive data policy issue in Customer PII",
                "severity": "warning",
                "seen": False,
                "at": "",
                "ts": now - 3600,
            },
            {
                "id": "notif-003",
                "topic": "jobs",
                "title": "Migration completed",
                "body": "Customer DB migration completed successfully",
                "severity": "success",
                "seen": True,
                "at": "",
                "ts": now - 10800,
            },
            {
                "id": "notif-004",
                "topic": "auth",
                "title": "Role changed",
                "body": "mahesh.sutar@metafordata.com → admin · Changed by Mahesh Sutar",
                "severity": "info",
                "seen": True,
                "at": "",
                "ts": now - (day + 3600),
            },
            {
                "id": "notif-005",
                "topic": "validation",
                "title": "Pipeline validation failed",
                "body": 'Pipeline "Orders_ETL" failed validation',
                "severity": "critical",
                "seen": False,
                "at": "",
                "ts": now - (day + 7200),
            },
            {
                "id": "notif-006",
                "topic": "connections",
                "title": "Data estate connected",
                "body": "Sales_Oracle connected successfully",
                "severity": "success",
                "seen": True,
                "at": "",
                "ts": now - (2 * day + 3600),
            },
        ]

    def _load(self) -> dict:
        if self._file.exists():
            try:
                d = json.loads(self._file.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    log = d.get("log")
                    subs = d.get("subscriptions")
                    log_list = [n for n in log if isinstance(n, dict)] if isinstance(log, list) else []
                    if not log_list:
                        log_list = self._seed()
                    return {"log": log_list,
                            "subscriptions": subs if isinstance(subs, dict) else {}}
            except (ValueError, OSError):
                pass
        seed_log = self._seed()
        state = {"log": seed_log, "subscriptions": {}}
        try:
            self._save(state)
        except OSError:
            pass
        return state

    def _save(self, state: dict) -> None:
        atomic_write_json(self._file, state)

    # -- publish ------------------------------------------------------------
    def notify(self, topic: str, title: str, body: str = "",
               severity: str = "info", at: str = "") -> dict:
        if severity not in SEVERITIES:
            severity = "info"
        note = {"id": uuid.uuid4().hex[:12], "topic": str(topic),
                "title": str(title), "body": str(body),
                "severity": severity, "seen": False,
                "at": at or "", "ts": self._now()}
        with file_lock(self._lock):
            state = self._load()
            state["log"].append(note)
            # cap the log so it can't grow unbounded (keep newest)
            if len(state["log"]) > _MAX_LOG:
                state["log"] = state["log"][-_MAX_LOG:]
            self._save(state)
        return note

    # -- subscriptions (topic -> channels; advisory record) ----------------
    def subscribe(self, topic: str, channel: str) -> None:
        with file_lock(self._lock):
            state = self._load()
            subs = state["subscriptions"].setdefault(str(topic), [])
            if channel not in subs:
                subs.append(str(channel))
            self._save(state)

    def subscriptions(self) -> Dict[str, List[str]]:
        return self._load()["subscriptions"]

    # -- read ---------------------------------------------------------------
    def recent(self, limit: int = 50, topic: str = "",
               unseen_only: bool = False) -> List[dict]:
        log = self._load()["log"]
        items = [n for n in reversed(log)
                 if (not topic or n.get("topic") == topic)
                 and (not unseen_only or not n.get("seen"))]
        return items[:max(1, int(limit))]

    def counts(self) -> dict:
        log = self._load()["log"]
        out = {s: 0 for s in SEVERITIES}
        unseen = 0
        for n in log:
            out[n.get("severity", "info")] = \
                out.get(n.get("severity", "info"), 0) + 1
            if not n.get("seen"):
                unseen += 1
        return {"total": len(log), "unseen": unseen, "by_severity": out}

    def mark_seen(self, ids: Optional[List[str]] = None) -> int:
        target = set(ids) if ids else None
        with file_lock(self._lock):
            state = self._load()
            n = 0
            for note in state["log"]:
                # defensive access — a legacy/partial record must not crash
                if (target is None or note.get("id") in target) \
                        and not note.get("seen"):
                    note["seen"] = True
                    n += 1
            if n:
                self._save(state)
        return n

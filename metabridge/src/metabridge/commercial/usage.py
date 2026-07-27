"""Usage reporting from the instance (EB-504).

Connected instances batch-report usage to the control plane. Two pieces:

- ``UsageSpool`` — a durable, bounded, file-backed queue of usage events. It
  survives process restart (the whole point: usage recorded just before a crash
  must not be lost) and is atomic (lock + atomic rewrite) so a concurrent writer
  or an interrupted flush cannot corrupt it. Bounded so a long control-plane
  outage can never grow the spool without limit — once full it applies
  backpressure (refuses new events, counts the drops) rather than evicting
  unsent billing data.
- ``UsageReporter`` — drains the spool to an injectable sink (the thing that
  POSTs ``/commercial/usage/batch``) with retry + backoff. Every event carries
  an ``idempotency_key``; the control plane dedups on ``(tenant, idempotency_key)``,
  so a batch re-sent after a mid-flush crash is applied **exactly once**. That
  server-side guarantee is what makes "no loss and no duplication" hold even if
  the instance resends.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional

from ..platform._util import atomic_write_json, file_lock

try:
    import json
except ImportError:                                   # pragma: no cover
    json = None


class UsageSpool:
    def __init__(self, path, max_events: int = 10000) -> None:
        self._path = Path(path)
        self._lock = self._path.with_suffix(self._path.suffix + ".lock")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max = max(1, int(max_events))

    def _read(self) -> dict:
        if not self._path.exists():
            return {"events": [], "dropped": 0}
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"events": [], "dropped": 0}
        if not isinstance(doc, dict):
            return {"events": [], "dropped": 0}
        doc.setdefault("events", [])
        doc.setdefault("dropped", 0)
        return doc

    def add(self, meter_code: str, quantity: int, idempotency_key: str, *,
            occurred_at: Optional[str] = None, dimensions: Optional[dict] = None,
            subscription_id: Optional[str] = None) -> bool:
        """Enqueue one usage event. Returns True if stored, False if it was a
        duplicate key already pending or the spool is full (backpressure)."""
        if not (idempotency_key or "").strip():
            raise ValueError("usage event requires an idempotency_key")
        with file_lock(self._lock):
            doc = self._read()
            events = doc["events"]
            if any(e["idempotency_key"] == idempotency_key for e in events):
                return False                          # already pending
            if len(events) >= self._max:
                doc["dropped"] = int(doc.get("dropped", 0)) + 1
                atomic_write_json(self._path, doc)
                return False                          # backpressure, counted
            events.append({
                "meter_code": meter_code, "quantity": int(quantity),
                "idempotency_key": idempotency_key,
                "occurred_at": occurred_at, "dimensions": dimensions or {},
                "subscription_id": subscription_id,
                "spooled_at": time.time()})
            atomic_write_json(self._path, doc)
            return True

    def pending(self) -> List[dict]:
        return list(self._read()["events"])

    def count(self) -> int:
        return len(self._read()["events"])

    def dropped(self) -> int:
        return int(self._read().get("dropped", 0))

    def ack(self, idempotency_keys) -> int:
        """Remove successfully-sent events by key. Atomic. Returns how many
        were removed."""
        keys = set(idempotency_keys)
        if not keys:
            return 0
        with file_lock(self._lock):
            doc = self._read()
            before = len(doc["events"])
            doc["events"] = [e for e in doc["events"]
                             if e["idempotency_key"] not in keys]
            atomic_write_json(self._path, doc)
            return before - len(doc["events"])


class UsageReporter:
    """Drains a spool to a sink. ``sink(events)`` must POST the batch and raise
    on any non-success; only then are the events acked (removed)."""

    def __init__(self, spool: UsageSpool, sink: Callable[[List[dict]], None], *,
                 batch_size: int = 100,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._spool = spool
        self._sink = sink
        self._batch = max(1, int(batch_size))
        self._sleep = sleep

    def drain_once(self) -> dict:
        """One pass: send pending events in batches until the spool is empty or
        a batch fails. A failed batch stops the pass (its events stay spooled).
        Returns ``{sent, remaining, batches, ok}``."""
        sent = batches = 0
        ok = True
        while True:
            pending = self._spool.pending()
            if not pending:
                break
            batch = pending[:self._batch]
            keys = [e["idempotency_key"] for e in batch]
            try:
                self._sink(batch)
            except Exception:                          # sink failed; leave spooled
                ok = False
                break
            self._spool.ack(keys)
            sent += len(batch)
            batches += 1
        return {"sent": sent, "remaining": self._spool.count(),
                "batches": batches, "ok": ok}

    def flush(self, *, max_attempts: int = 5, backoff: float = 0.5,
              max_backoff: float = 30.0) -> dict:
        """Drain with retry + exponential backoff across the whole spool. Safe
        to re-run: server-side idempotency dedups any batch that a prior attempt
        sent but could not ack before failing."""
        attempt = 0
        delay = backoff
        total_sent = 0
        while True:
            res = self.drain_once()
            total_sent += res["sent"]
            if res["remaining"] == 0 or res["ok"]:
                return {"sent": total_sent, "remaining": res["remaining"],
                        "attempts": attempt + 1, "drained": res["remaining"] == 0}
            attempt += 1
            if attempt >= max_attempts:
                return {"sent": total_sent, "remaining": res["remaining"],
                        "attempts": attempt, "drained": False}
            self._sleep(min(delay, max_backoff))
            delay *= 2

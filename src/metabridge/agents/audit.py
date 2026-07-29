"""Audit trail — an append-only, tamper-evident log of every agent action.

Each event is chained to the previous one and sealed with an HMAC-SHA256
keyed by a server-held secret (``entry_hash = HMAC(key, prev_hash +
canonical(event))``). Because the key is held by the server and is NOT
stored in the persisted run report, an editor of the report JSON cannot
recompute a valid chain — so an altered event, a reordered chain, or an
appended forgery is detectable via ``verify()``. ``verify()`` also
checks sequence contiguity, so a deleted or inserted MIDDLE event is
caught. The trail records the governance decision behind every action
(successes, denials, skips, failures AND human approve/reject decisions)
so the record is complete, not curated.

Honest scope: this detects tampering by anyone without the server key;
it is not a substitute for an external, independently-anchored ledger
against an attacker who also holds the key. It also does NOT detect
truncation of the chain's TAIL — deleting the most recent N events
leaves every remaining event internally consistent, since nothing here
pins an externally-expected event count or head against the persisted
one (contrast ``metabridge_control/audit.py``, which anchors the head
in a separate table for exactly this reason). Callers that need
tail-truncation detection should use that DB-backed chain instead.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import List, Optional

_GENESIS = "0" * 64


@dataclass
class AuditEvent:
    seq: int
    timestamp: float
    agent_id: str
    task_type: str
    action_class: str
    decision: str
    status: str
    confidence: float
    confidence_level: str
    evidence_digest: str
    summary: str
    detail: dict = field(default_factory=dict)
    prev_hash: str = _GENESIS
    entry_hash: str = ""

    def _body(self) -> bytes:
        return json.dumps({
            "seq": self.seq, "timestamp": round(self.timestamp, 3),
            "agent_id": self.agent_id, "task_type": self.task_type,
            "action_class": self.action_class, "decision": self.decision,
            "status": self.status, "confidence": self.confidence,
            "confidence_level": self.confidence_level,
            "evidence_digest": self.evidence_digest, "summary": self.summary,
            "detail": self.detail, "prev_hash": self.prev_hash,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def compute_hash(self, key: bytes = b"") -> str:
        body = self._body()
        if key:
            return hmac.new(key, body, hashlib.sha256).hexdigest()
        return hashlib.sha256(body).hexdigest()

    def to_dict(self) -> dict:
        return {"seq": self.seq, "timestamp": self.timestamp,
                "agent_id": self.agent_id, "task_type": self.task_type,
                "action_class": self.action_class, "decision": self.decision,
                "status": self.status, "confidence": self.confidence,
                "confidence_level": self.confidence_level,
                "evidence_digest": self.evidence_digest,
                "summary": self.summary, "detail": self.detail,
                "prev_hash": self.prev_hash, "entry_hash": self.entry_hash}


class AuditTrail:
    def __init__(self, key: bytes = b"") -> None:
        self._events: List[AuditEvent] = []
        self._key = key or b""

    def _now(self) -> float:
        return time.time()

    def record(self, agent_id: str, task_type: str, action_class: str,
               decision: str, status: str, confidence: float,
               confidence_level: str, evidence_digest: str = "",
               summary: str = "", detail: Optional[dict] = None,
               timestamp: Optional[float] = None) -> AuditEvent:
        prev = self._events[-1].entry_hash if self._events else _GENESIS
        ev = AuditEvent(
            seq=len(self._events) + 1,
            timestamp=self._now() if timestamp is None else timestamp,
            agent_id=agent_id, task_type=task_type,
            action_class=action_class, decision=decision, status=status,
            confidence=confidence, confidence_level=confidence_level,
            evidence_digest=evidence_digest, summary=summary,
            detail=detail or {}, prev_hash=prev)
        ev.entry_hash = ev.compute_hash(self._key)
        self._events.append(ev)
        return ev

    def events(self) -> List[dict]:
        return [e.to_dict() for e in self._events]

    def head(self) -> str:
        return self._events[-1].entry_hash if self._events else _GENESIS

    def verify(self) -> dict:
        """Recompute the keyed chain and report whether it is intact.
        Detects altered events, broken links, non-contiguous sequence
        (deleted middle event) and forged entries lacking the key."""
        prev = _GENESIS
        for i, ev in enumerate(self._events):
            if ev.seq != i + 1:
                return {"intact": False, "broken_at": i + 1,
                        "reason": "sequence gap (event added/removed)"}
            if ev.prev_hash != prev:
                return {"intact": False, "broken_at": i + 1,
                        "reason": "prev_hash mismatch"}
            if ev.compute_hash(self._key) != ev.entry_hash:
                return {"intact": False, "broken_at": i + 1,
                        "reason": "entry_hash mismatch (event altered or "
                                  "forged without the server key)"}
            prev = ev.entry_hash
        return {"intact": True, "events": len(self._events), "head": prev}

    @classmethod
    def from_events(cls, events: List[dict], key: bytes = b"") -> "AuditTrail":
        """Reconstruct a trail from persisted event dicts so the chain can
        be RE-VERIFIED on read (never trust a persisted 'intact' flag)."""
        t = cls(key=key)
        for e in events or []:
            t._events.append(AuditEvent(
                seq=int(e.get("seq", 0)), timestamp=float(e.get("timestamp", 0)),
                agent_id=e.get("agent_id", ""), task_type=e.get("task_type", ""),
                action_class=e.get("action_class", ""),
                decision=e.get("decision", ""), status=e.get("status", ""),
                confidence=float(e.get("confidence", 0) or 0),
                confidence_level=e.get("confidence_level", ""),
                evidence_digest=e.get("evidence_digest", ""),
                summary=e.get("summary", ""), detail=e.get("detail", {}) or {},
                prev_hash=e.get("prev_hash", _GENESIS),
                entry_hash=e.get("entry_hash", "")))
        return t

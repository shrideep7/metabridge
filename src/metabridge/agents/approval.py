"""Approval workflow — a file-backed queue of agent actions held for human
sign-off.

Follows the same allow-list shape as MetaBridge's existing approve-and-
apply queues (``llm.review_agent.apply_corrections`` /
``report.autofix.apply_fixes``): an action that the governance gate marks
``needs_approval`` becomes a pending request; a human approves or rejects
it by id, and the decision is recorded. Approving does NOT auto-apply a
change to any real system — it records the governance sign-off and lets a
subsequent run pre-authorize that task type so the agent's proposal can
commit.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import List, Optional


class ApprovalError(Exception):
    """Approval-queue error."""


class ApprovalQueue:
    def __init__(self, data_dir: str = "") -> None:
        base = Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR",
                                               ".")) / "agents"
        base.mkdir(parents=True, exist_ok=True)
        self._file = base / "approvals.json"
        self._lock = base / "approvals.lock"

    # -- persistence --------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        """Serialize the whole load->mutate->save cycle across concurrent
        requests so a read-modify-write can't lose records."""
        fh = open(self._lock, "w")
        try:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass                             # best-effort on platforms w/o flock
            yield
        finally:
            fh.close()

    def _load(self) -> dict:
        if self._file.exists():
            try:
                return json.loads(self._file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return {}
        return {}

    def _save(self, state: dict) -> None:
        # atomic: write to a temp file then replace, so a crash mid-write
        # never truncates the queue
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self._file)

    def _now(self) -> float:
        return time.time()

    # -- open requests ------------------------------------------------------
    def open_request(self, run_id: str, result, reasons: List[str],
                     created_at: str = "", requested_by: str = "") -> str:
        """Record a pending approval for one agent result. The approval id
        is deterministic per (run, agent). Re-opening an ALREADY-DECIDED
        request is a no-op (never resets a decision back to pending)."""
        approval_id = "%s:%s" % (run_id, result.agent_id)
        with self._locked():
            state = self._load()
            existing = state.get(approval_id)
            if existing is not None and existing.get("status") != "pending":
                return approval_id           # decided — do not clobber
            state[approval_id] = {
                "id": approval_id, "run_id": run_id,
                "agent_id": result.agent_id, "task_type": result.task_type,
                "action_class": result.action_class,
                "confidence": result.confidence,
                "confidence_level": result.level(),
                "summary": result.summary, "reasons": list(reasons),
                "status": "pending", "approver": "", "decided_at": "",
                "requested_by": requested_by,
                "created_at": created_at or "", "created_ts": self._now()}
            self._save(state)
        return approval_id

    def pending(self) -> List[dict]:
        return [r for r in self._load().values()
                if r.get("status") == "pending"]

    def all(self) -> List[dict]:
        return sorted(self._load().values(),
                      key=lambda r: r.get("created_ts", 0))

    def for_run(self, run_id: str) -> List[dict]:
        return [r for r in self._load().values()
                if r.get("run_id") == run_id]

    def get(self, approval_id: str) -> Optional[dict]:
        return self._load().get(approval_id)

    # -- decide -------------------------------------------------------------
    def _decide(self, approval_id: str, status: str, approver: str,
                note: str, decided_at: str) -> dict:
        with self._locked():
            state = self._load()
            rec = state.get(approval_id)
            if rec is None:
                raise ApprovalError("unknown approval: %s" % approval_id)
            if rec["status"] != "pending":
                raise ApprovalError("%s already %s" % (approval_id,
                                                       rec["status"]))
            # segregation of duties: the run's requester may not
            # self-approve their own consequential action
            if status == "approved" and rec.get("requested_by") and \
                    approver == rec.get("requested_by"):
                raise ApprovalError(
                    "%s cannot approve their own run's action — a different "
                    "approver is required" % approver)
            rec["status"] = status
            rec["approver"] = approver
            rec["note"] = note
            rec["decided_at"] = decided_at or ""
            rec["decided_ts"] = self._now()
            self._save(state)
            return rec

    def claim(self, approval_id: str, claimer: str) -> dict:
        """Soft-lock a pending request so two approvers don't work the same
        item. Claiming is advisory — approve/reject stay atomic — and the
        requester may not claim their own request (segregation of duties)."""
        if not claimer:
            raise ApprovalError("claimer is required")
        with self._locked():
            state = self._load()
            rec = state.get(approval_id)
            if rec is None:
                raise ApprovalError("unknown approval: %s" % approval_id)
            if rec["status"] != "pending":
                raise ApprovalError("%s already %s" % (approval_id,
                                                       rec["status"]))
            if rec.get("requested_by") and claimer == rec["requested_by"]:
                raise ApprovalError(
                    "%s cannot claim their own run's action — a different "
                    "approver is required" % claimer)
            rec["claimed_by"] = claimer
            rec["claimed_ts"] = self._now()
            self._save(state)
            return rec

    def approve(self, approval_id: str, approver: str,
                note: str = "", decided_at: str = "") -> dict:
        if not approver:
            raise ApprovalError("approver is required")
        return self._decide(approval_id, "approved", approver, note,
                             decided_at)

    def reject(self, approval_id: str, approver: str,
               note: str = "", decided_at: str = "") -> dict:
        if not approver:
            raise ApprovalError("approver is required")
        return self._decide(approval_id, "rejected", approver, note,
                             decided_at)

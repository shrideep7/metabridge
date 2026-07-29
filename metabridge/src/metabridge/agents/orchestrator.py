"""Task orchestrator — schedules the agent swarm over the shared context.

Responsibilities:
* order agents by their declared dependencies (topological; cycle-safe);
* run each agent, then pass its scored proposal through the governance
  gate BEFORE anything is published to the shared CIR;
* commit allowed/approved outputs to shared memory (so downstream agents
  build only on governed artifacts); hold ``needs_approval`` proposals in
  the approval queue without committing; skip agents whose dependencies
  were not satisfied;
* record every action — success, approval, denial, skip or failure — in
  the tamper-evident audit trail;
* persist the run so its report, audit and approvals can be reviewed
  later.

Every AI action is therefore scored and governed, and the whole run is
reconstructable from the audit chain.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .approval import ApprovalQueue
from .audit import AuditTrail
from .base import Agent, AgentResult, Status
from .context import KEY_CIR, KEY_PIPELINES, KEY_TWIN, SharedContext
from .governance import AgentGovernance, Decision

# heavy canonical artifacts kept in shared memory but never serialized into
# a run report (they are Python objects, not JSON)
_HEAVY_KEYS = {KEY_TWIN, KEY_PIPELINES, KEY_CIR}


class OrchestratorError(Exception):
    """Orchestration / planning error."""


def _toposort(agents: List[Agent]) -> List[Agent]:
    """Dependency order (deps before dependents). Deterministic; raises on
    a cycle. Dependencies on agents not in the set are ignored for
    ordering (they surface as unmet dependencies at run time)."""
    by_id = {a.id: a for a in agents}
    order: List[Agent] = []
    state: Dict[str, int] = {}          # 0 = visiting, 1 = done

    def visit(a: Agent, chain: List[str]):
        st = state.get(a.id)
        if st == 1:
            return
        if st == 0:
            raise OrchestratorError(
                "dependency cycle: %s" % " -> ".join(chain + [a.id]))
        state[a.id] = 0
        for dep in sorted(a.depends_on):
            if dep in by_id:
                visit(by_id[dep], chain + [a.id])
        state[a.id] = 1
        order.append(a)

    for a in sorted(agents, key=lambda x: x.id):
        visit(a, [])
    return order


class TaskOrchestrator:
    def __init__(self, agents: Optional[List[Agent]] = None,
                 data_dir: str = "", governance: Optional[AgentGovernance] = None,
                 approvals: Optional[ApprovalQueue] = None) -> None:
        if agents is None:
            from .agents import default_agents
            agents = default_agents()
        self.agents = list(agents)
        self._by_id = {a.id: a for a in self.agents}
        self.governance = governance or AgentGovernance()
        self.approvals = approvals or ApprovalQueue(data_dir)
        self._base = Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR",
                                                     ".")) / "agents"
        self._runs_dir = self._base / "runs"
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._key = self._load_audit_key()

    def _load_audit_key(self) -> bytes:
        """Server-held HMAC key sealing the audit chain — stored 0600 under
        the data dir, NEVER in a run report, so a report-file editor cannot
        forge a valid chain."""
        kf = self._base / "audit_key"
        if kf.exists():
            try:
                return bytes.fromhex(kf.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass
        key = os.urandom(32)
        try:
            kf.write_text(key.hex(), encoding="utf-8")
            os.chmod(kf, 0o600)
        except OSError:
            pass
        return key

    # -- roster / plan ------------------------------------------------------
    def roster(self) -> List[dict]:
        return [{"id": a.id, "task_type": a.task_type,
                 "action_class": a.action_class,
                 "depends_on": list(a.depends_on),
                 "description": a.description} for a in self.agents]

    def _select(self, task_types: Optional[Iterable[str]]) -> List[Agent]:
        if not task_types:
            return list(self.agents)
        wanted = set(task_types)
        sel = [a for a in self.agents if a.id in wanted or
               a.task_type in wanted]
        if not sel:
            raise OrchestratorError("no agents match: %s"
                                    % ", ".join(sorted(wanted)))
        return sel

    def plan(self, task_types: Optional[Iterable[str]] = None) -> List[dict]:
        ordered = _toposort(self._select(task_types))
        return [{"id": a.id, "task_type": a.task_type,
                 "action_class": a.action_class,
                 "depends_on": list(a.depends_on)} for a in ordered]

    # -- run ----------------------------------------------------------------
    def run(self, ctx: SharedContext,
            task_types: Optional[Iterable[str]] = None,
            preapproved: Optional[Iterable[str]] = None,
            approver: str = "", created_at: str = "",
            run_id: str = "", requested_by: str = "") -> dict:
        ordered = _toposort(self._select(task_types))
        run_id = run_id or uuid.uuid4().hex[:12]
        # seal this run's audit chain with the server key
        ctx.audit = AuditTrail(self._key)
        pre = set(preapproved or ())
        committed: set = set()          # agent ids whose outputs committed
        results: List[dict] = []
        summary = {"committed": 0, "flagged": 0, "needs_approval": 0,
                   "denied": 0, "failed": 0, "skipped": 0}

        for agent in ordered:
            unmet = [d for d in agent.depends_on
                     if d in self._by_id and d not in committed]
            if unmet:
                res = AgentResult(
                    agent_id=agent.id, task_type=agent.task_type,
                    action_class=agent.action_class, status=Status.SKIPPED,
                    confidence=0.0,
                    summary="skipped: unmet dependency %s"
                            % ", ".join(unmet))
                reasons = ["unmet dependencies: %s" % ", ".join(unmet)]
                self._record(ctx, res, Decision.SKIPPED, reasons)
                summary["skipped"] += 1
                results.append(self._result_row(res, Decision.SKIPPED,
                                                 reasons))
                continue

            res = agent.run(ctx)
            gd = self.governance.decide(res, preapproved=pre,
                                        approver=approver)

            if gd.commits:
                for key, value in (res.outputs or {}).items():
                    ctx.memory.put(key, value, agent.id)
                committed.add(agent.id)
                res.status = Status.OK
                summary["committed"] += 1
                if gd.decision == Decision.FLAG_REVIEW:
                    summary["flagged"] += 1
            elif gd.decision == Decision.NEEDS_APPROVAL:
                res.status = Status.NEEDS_APPROVAL
                self.approvals.open_request(run_id, res, gd.reasons,
                                            created_at,
                                            requested_by=requested_by)
                summary["needs_approval"] += 1
            elif gd.decision == Decision.DENY:
                res.status = Status.BLOCKED
                summary["denied"] += 1
            elif gd.decision == Decision.FAILED:
                summary["failed"] += 1

            self._record(ctx, res, gd.decision, gd.reasons,
                         approver=gd.approver)
            results.append(self._result_row(res, gd.decision, gd.reasons,
                                             gd.approver))

        report = {
            "run_id": run_id, "project": ctx.project,
            "created_at": created_at, "requested_by": requested_by,
            "params": ctx.params(),
            "order": [a.id for a in ordered], "results": results,
            "summary": summary,
            "audit": {"head": ctx.audit.head(),
                      "verification": ctx.audit.verify(),
                      "events": ctx.audit.events()},
            "approvals": self.approvals.for_run(run_id),
        }
        self._persist(run_id, report)
        return report

    # -- helpers ------------------------------------------------------------
    def _record(self, ctx: SharedContext, res: AgentResult, decision: str,
                reasons: List[str], approver: str = "") -> None:
        ctx.audit.record(
            agent_id=res.agent_id, task_type=res.task_type,
            action_class=res.action_class, decision=decision,
            status=res.status, confidence=res.confidence,
            confidence_level=res.level(),
            evidence_digest=res.evidence_digest(), summary=res.summary,
            detail={"reasons": reasons, "output_keys":
                    sorted((res.outputs or {}).keys()),
                    "warnings": res.warnings, "approver": approver,
                    "error": res.error})

    def _result_row(self, res: AgentResult, decision: str,
                    reasons: List[str], approver: str = "") -> dict:
        row = res.to_dict()
        row["decision"] = decision
        row["decision_reasons"] = reasons
        row["approver"] = approver
        # carry the agent's JSON-safe outputs (its proposal or committed
        # result) so the UI can show it, minus the heavy canonical objects
        safe = {}
        for k, v in (res.outputs or {}).items():
            if k in _HEAVY_KEYS:
                continue
            try:
                json.dumps(v)
                safe[k] = v
            except (TypeError, ValueError):
                continue
        row["outputs"] = safe
        return row

    # -- run persistence ----------------------------------------------------
    def _persist(self, run_id: str, report: dict) -> None:
        safe = "".join(c for c in run_id if c.isalnum() or c in "._-")
        if not safe:
            return
        (self._runs_dir / ("%s.json" % safe)).write_text(
            json.dumps(report, indent=1, default=str), encoding="utf-8")

    def get_run(self, run_id: str) -> Optional[dict]:
        safe = "".join(c for c in run_id if c.isalnum() or c in "._-")
        f = self._runs_dir / ("%s.json" % safe)
        if not f.exists():
            return None
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        # NEVER trust the persisted 'intact' flag — reconstruct the chain
        # from the stored events and RE-VERIFY with the server key on read
        try:
            trail = AuditTrail.from_events(
                (r.get("audit") or {}).get("events", []), self._key)
            r.setdefault("audit", {})
            r["audit"]["verification"] = trail.verify()
            r["audit"]["head"] = trail.head()
        except Exception:                        # noqa: BLE001
            pass
        return r

    def record_decision(self, run_id: str, agent_id: str, decision: str,
                        approver: str, summary: str = "",
                        detail: Optional[dict] = None,
                        timestamp: Optional[float] = None) -> Optional[dict]:
        """Append a human approve/reject decision to the run's audit chain
        so the trail records the most consequential governance action, and
        re-persist. Returns the recorded event dict or None."""
        safe = "".join(c for c in run_id if c.isalnum() or c in "._-")
        f = self._runs_dir / ("%s.json" % safe)
        if not f.exists():
            return None
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        trail = AuditTrail.from_events((r.get("audit") or {}).get("events", []),
                                       self._key)
        det = dict(detail or {})
        det.setdefault("approver", approver)
        ev = trail.record(agent_id=agent_id, task_type="approval",
                          action_class="approval", decision=decision,
                          status="decided", confidence=1.0,
                          confidence_level="high", summary=summary,
                          detail=det, timestamp=timestamp)
        r.setdefault("audit", {})
        r["audit"]["events"] = trail.events()
        r["audit"]["head"] = trail.head()
        r["audit"]["verification"] = trail.verify()
        self._persist(run_id, r)
        return ev.to_dict()

    def list_runs(self) -> List[dict]:
        out = []
        for f in sorted(self._runs_dir.glob("*.json")):
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            out.append({"run_id": r.get("run_id"), "project": r.get("project"),
                        "created_at": r.get("created_at"),
                        "summary": r.get("summary"),
                        "agents": len(r.get("results", []))})
        return out

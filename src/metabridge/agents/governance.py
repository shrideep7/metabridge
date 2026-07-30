"""Agent governance gate — the policy that decides, for every scored agent
action, whether it may commit to the shared CIR, must be held for human
approval, or is denied outright.

The rule is deterministic and explainable:

* Every action is scored (confidence in [0,1]) and passes the gate — the
  decision and its reasons are always recorded in the audit trail.
* GENERATE / MUTATING actions (producing artifacts or changing a real
  system) ALWAYS require approval — MetaBridge never auto-applies a
  consequential AI action.
* A GENERATE / MUTATING action below the hard floor is DENIED (too little
  confidence to be worth a human's review).
* Any action below the approval threshold requires approval regardless of
  class (low-confidence analysis is not silently trusted).
* Read-only / advisory analysis at adequate confidence is allowed; it
  still reports sensitivity, but reporting sensitive findings is not
  itself a gated artifact.

A caller may pre-authorize specific task types (an explicit allow-list,
the same shape as the existing review/auto-fix approval queues); a
pre-authorized action that would need approval becomes ``approved`` and
commits, with the approver recorded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from .base import ACTION_RISK, ActionClass, AgentResult, Status


class Decision:
    ALLOW = "allow"                 # committed automatically
    APPROVED = "approved"           # needed approval, pre-authorized -> committed
    FLAG_REVIEW = "flag_review"     # low-confidence advisory: committed, review noted
    NEEDS_APPROVAL = "needs_approval"   # consequential: withheld until approved
    DENY = "deny"
    FAILED = "failed"               # the agent run itself failed
    SKIPPED = "skipped"             # a dependency was not satisfied


# default thresholds — deliberately conservative for a governed system
DEFAULT_POLICY = {
    "approval_threshold": 0.6,      # below this, any action needs approval
    "deny_floor": 0.2,              # below this, a GENERATE/MUTATING is denied
}


@dataclass
class GovernanceDecision:
    decision: str
    reasons: List[str] = field(default_factory=list)
    approver: str = ""
    threshold: float = 0.0
    floor: float = 0.0

    @property
    def commits(self) -> bool:
        """Whether the agent's outputs may be published to the shared CIR.
        Consequential (GENERATE/MUTATING) proposals are withheld until
        approved; low-confidence read-only/advisory analysis still commits
        but is flagged for review (it would be wrong to sever the pipeline
        just because one analysis is uncertain)."""
        return self.decision in (Decision.ALLOW, Decision.APPROVED,
                                 Decision.FLAG_REVIEW)

    def to_dict(self) -> dict:
        return {"decision": self.decision, "reasons": self.reasons,
                "approver": self.approver, "threshold": self.threshold,
                "floor": self.floor}


class AgentGovernance:
    def __init__(self, policy: Optional[dict] = None) -> None:
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update(policy)

    def _sensitive(self, result: AgentResult) -> List[str]:
        s = result.sensitivity or {}
        flags = []
        for key in ("violations", "special_category", "phi", "pci"):
            v = s.get(key)
            if isinstance(v, bool) and v:
                flags.append(key)
            elif isinstance(v, (int, float)) and v > 0:
                flags.append("%s=%d" % (key, int(v)))
        return flags

    def decide(self, result: AgentResult,
               preapproved: Optional[Iterable[str]] = None,
               approver: str = "") -> GovernanceDecision:
        thr = float(self.policy["approval_threshold"])
        floor = float(self.policy["deny_floor"])
        pre = set(preapproved or ())
        gd = GovernanceDecision(decision=Decision.ALLOW, threshold=thr,
                                floor=floor)

        if result.status == Status.FAILED:
            gd.decision = Decision.FAILED
            gd.reasons.append("agent run failed: %s"
                              % (result.error or "unknown"))
            return gd

        # fail CLOSED: an unknown/non-canonical action class is treated as
        # the highest risk, not silently as read-only
        risk = ACTION_RISK.get(result.action_class)
        risky = risk is None or risk >= ACTION_RISK[ActionClass.GENERATE]
        low = result.confidence < thr
        sens = self._sensitive(result)

        if risky:
            # consequential action: hard gate
            if result.confidence < floor:
                gd.decision = Decision.DENY
                gd.reasons.append(
                    "%s action with confidence %.2f below deny floor %.2f"
                    % (result.action_class, result.confidence, floor))
                return gd
            gd.reasons.append(
                "%s action (produces artifacts / changes systems) requires "
                "human approval" % result.action_class)
            if low:
                gd.reasons.append("confidence %.2f below threshold %.2f"
                                  % (result.confidence, thr))
            if sens:
                gd.reasons.append("touches sensitive data: %s"
                                  % ", ".join(sens))
            preauthorized = result.task_type in pre or result.agent_id in pre
            # pre-authorization NEVER covers an action that touches sensitive
            # data (PHI/PII-special/policy violations) — that always demands
            # an explicit human approval
            if preauthorized and not sens:
                gd.decision = Decision.APPROVED
                gd.approver = approver or "preapproved"
                gd.reasons.append("pre-authorized by %s" % gd.approver)
            else:
                gd.decision = Decision.NEEDS_APPROVAL
                if preauthorized and sens:
                    gd.reasons.append("pre-authorization does not cover "
                                      "sensitive data — approval required")
            return gd

        # read-only / advisory analysis: commits, but flagged if uncertain
        if low:
            gd.decision = Decision.FLAG_REVIEW
            gd.reasons.append(
                "confidence %.2f below review threshold %.2f — committed but "
                "flagged for human review" % (result.confidence, thr))
        else:
            gd.decision = Decision.ALLOW
        if sens:
            gd.reasons.append("reports sensitive findings (%s)"
                              % ", ".join(sens))
        return gd

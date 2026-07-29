"""Agentic AI core — the agent contract, tasks, results and confidence.

MetaBridge's agents are deterministic, bounded workers: each wraps an
existing engine behind one uniform contract. Their "intelligence" is
orchestration, evidence-based confidence scoring and governance — NOT
free-form generation. Confidence is COMPUTED from measurable evidence
(coverage, parse success, validation pass rate, sensitivity, ...), never
invented, so every AI action is explainable, gate-able and auditable.

An agent produces a *proposal* (an ``AgentResult``). The orchestrator —
not the agent — decides, via governance, whether that proposal is
committed to the shared CIR/memory, held for human approval, or denied.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# --- the twelve task types ------------------------------------------------
class TaskType:
    DISCOVERY = "discovery"
    METADATA = "metadata"
    PARSE = "parse"
    SEMANTIC = "semantic"
    MIGRATION = "migration"
    VALIDATION = "validation"
    TESTING = "testing"
    GOVERNANCE = "governance"
    SECURITY = "security"
    OPTIMIZATION = "optimization"
    DOCUMENTATION = "documentation"
    EXECUTIVE_REPORTING = "executive_reporting"


TASK_TYPES: Tuple[str, ...] = (
    TaskType.DISCOVERY, TaskType.METADATA, TaskType.PARSE,
    TaskType.SEMANTIC, TaskType.MIGRATION, TaskType.VALIDATION,
    TaskType.TESTING, TaskType.GOVERNANCE, TaskType.SECURITY,
    TaskType.OPTIMIZATION, TaskType.DOCUMENTATION,
    TaskType.EXECUTIVE_REPORTING,
)


# --- action risk classes (drive governance) ------------------------------
class ActionClass:
    """How consequential an agent's action is — the higher the class, the
    stronger the governance gate."""
    READ_ONLY = "read_only"     # observes the estate, produces analysis
    ADVISORY = "advisory"       # produces recommendations / scores
    GENERATE = "generate"       # produces artifacts (code, docs, tests)
    MUTATING = "mutating"       # would change a real system (applied)


ACTION_RISK = {ActionClass.READ_ONLY: 0, ActionClass.ADVISORY: 1,
               ActionClass.GENERATE: 2, ActionClass.MUTATING: 3}


# --- agent run status -----------------------------------------------------
class Status:
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"          # a dependency was unmet
    BLOCKED = "blocked"          # governance denied
    NEEDS_APPROVAL = "needs_approval"


# --- confidence -----------------------------------------------------------
class ConfidenceError(Exception):
    """A confidence signal was malformed (e.g. out of the [0,1] range)."""


def clamp01(x: float) -> float:
    import math
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ConfidenceError("confidence signal is not numeric: %r" % x)
    if not math.isfinite(v):                     # NaN or +/-inf
        raise ConfidenceError("confidence signal is not finite: %r" % x)
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def score_confidence(signals: Dict[str, Tuple[float, float]]) -> float:
    """Weighted mean of named evidence signals.

    ``signals`` maps a signal name -> (value in [0,1], weight >= 0). The
    value MUST already be a real measured/derived ratio (parse success
    rate, coverage, ...). Returns 0.0 when there is no evidence rather
    than a fabricated default — a scoreless action is a no-confidence
    action, and the governance gate treats it accordingly.
    """
    num = 0.0
    den = 0.0
    for name, pair in signals.items():
        try:
            value, weight = pair
        except (TypeError, ValueError):
            raise ConfidenceError(
                "signal %r must be a (value, weight) pair" % name)
        w = float(weight)
        if w < 0:
            raise ConfidenceError("signal %r has negative weight" % name)
        num += clamp01(value) * w
        den += w
    if den <= 0:
        return 0.0
    return round(num / den, 4)


class ConfidenceLevel:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# the MEDIUM/LOW boundary is pinned to the governance approval threshold
# (governance.DEFAULT_POLICY["approval_threshold"]) so a "medium" label can
# never describe a result the gate treats as low-confidence / flagged.
APPROVAL_THRESHOLD = 0.6


def confidence_level(score: float) -> str:
    if score >= 0.8:
        return ConfidenceLevel.HIGH
    if score >= APPROVAL_THRESHOLD:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


# --- data model -----------------------------------------------------------
@dataclass
class AgentTask:
    """A unit of work assigned to an agent by the orchestrator."""
    agent_id: str
    task_type: str
    action_class: str
    depends_on: Tuple[str, ...] = ()
    params: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    """An agent's proposal. ``confidence`` is derived from ``evidence`` via
    ``score_confidence`` and is never hand-set to mask weak evidence."""
    agent_id: str
    task_type: str
    action_class: str
    status: str = Status.OK
    confidence: float = 0.0
    summary: str = ""
    # measurable signals that PRODUCED the confidence (name -> value/weight)
    evidence: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    # values this agent proposes to publish to the shared CIR / memory
    outputs: Dict[str, object] = field(default_factory=dict)
    # sensitivity flags surfaced for the governance gate (e.g. pii=True)
    sensitivity: Dict[str, object] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    error: str = ""
    duration_ms: int = 0

    def level(self) -> str:
        return confidence_level(self.confidence)

    def evidence_digest(self) -> str:
        body = json.dumps(
            {k: list(v) if isinstance(v, (tuple, list)) else v
             for k, v in sorted(self.evidence.items())},
            sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id, "task_type": self.task_type,
            "action_class": self.action_class, "status": self.status,
            "confidence": self.confidence, "confidence_level": self.level(),
            "summary": self.summary,
            "evidence": {k: list(v) for k, v in self.evidence.items()},
            "output_keys": sorted(self.outputs.keys()),
            "sensitivity": self.sensitivity, "warnings": self.warnings,
            "error": self.error, "duration_ms": self.duration_ms,
        }


class Agent:
    """Base class for every agent. Subclasses implement ``_execute`` which
    returns an ``AgentResult`` with derived confidence and proposed
    outputs. ``run`` handles timing and turns any failure into a
    first-class ``failed`` result so a single agent can never crash the
    orchestrator."""

    id: str = "agent"
    task_type: str = ""
    action_class: str = ActionClass.READ_ONLY
    depends_on: Tuple[str, ...] = ()
    description: str = ""

    def _execute(self, ctx) -> AgentResult:      # pragma: no cover
        raise NotImplementedError

    def _result(self, **kw) -> AgentResult:
        kw.setdefault("agent_id", self.id)
        kw.setdefault("task_type", self.task_type)
        kw.setdefault("action_class", self.action_class)
        r = AgentResult(**kw)
        # confidence is ALWAYS recomputed from the declared evidence — an
        # agent cannot report a confidence its evidence does not support,
        # and no evidence means no confidence (0.0), never a hand-set value
        r.confidence = score_confidence(r.evidence)
        return r

    def run(self, ctx) -> AgentResult:
        start = time.time()
        try:
            result = self._execute(ctx)
        except Exception as exc:                 # noqa: BLE001
            result = AgentResult(
                agent_id=self.id, task_type=self.task_type,
                action_class=self.action_class, status=Status.FAILED,
                confidence=0.0, summary="agent raised an exception",
                error="%s: %s" % (type(exc).__name__, exc))
        result.duration_ms = int((time.time() - start) * 1000)
        return result

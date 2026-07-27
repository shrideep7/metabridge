"""MetaBridge Agentic AI Architecture.

Twelve deterministic agents — Discovery, Metadata, Parser, Semantic,
Migration, Validation, Testing, Governance, Security, Optimization,
Documentation and Executive Reporting — collaborate through a shared CIR
+ blackboard memory, are scheduled by a task orchestrator, gated by
confidence scoring and governance, held for approval when consequential
or low-confidence, and recorded in a tamper-evident audit trail.
"""
from .base import (ActionClass, Agent, AgentResult, AgentTask,
                   ConfidenceLevel, Status, TaskType, TASK_TYPES,
                   confidence_level, score_confidence)
from .memory import SharedMemory
from .audit import AuditTrail, AuditEvent
from .context import SharedContext
from .governance import AgentGovernance, Decision, GovernanceDecision
from .approval import ApprovalQueue, ApprovalError
from .orchestrator import TaskOrchestrator, OrchestratorError
from .agents import default_agents

__all__ = [
    "Agent", "AgentResult", "AgentTask", "ActionClass", "Status",
    "TaskType", "TASK_TYPES", "ConfidenceLevel", "confidence_level",
    "score_confidence", "SharedMemory", "AuditTrail", "AuditEvent",
    "SharedContext", "AgentGovernance", "Decision", "GovernanceDecision",
    "ApprovalQueue", "ApprovalError", "TaskOrchestrator",
    "OrchestratorError", "default_agents",
]

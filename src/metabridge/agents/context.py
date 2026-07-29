"""Shared context — the CIR + blackboard + audit trail agents collaborate
through.

The "Shared CIR" is the set of canonical artifacts the agent swarm builds
up together: the Digital Twin (estate graph), the parsed IR pipelines,
and the enriched CIR ``Project``. They live in the shared memory
blackboard (with provenance) so every downstream agent's inputs are
traceable, and the audit trail records who produced what. Agents READ the
context; only the orchestrator WRITES to it (after the governance gate),
so no agent can silently publish an unapproved artifact for another to
build on.
"""
from __future__ import annotations

from typing import Any, List, Optional

from .audit import AuditTrail
from .memory import SharedMemory

# canonical shared-CIR keys on the blackboard
KEY_TWIN = "twin"
KEY_PIPELINES = "pipelines"
KEY_CIR = "cir"


class SharedContext:
    def __init__(self, paths: Optional[List[str]] = None,
                 source_format: str = "", dialect: str = "",
                 target_region: str = "", project: str = "estate") -> None:
        self.paths: List[str] = list(paths or [])
        self.source_format = source_format
        self.dialect = dialect
        self.target_region = target_region
        self.project = project
        self.memory = SharedMemory()
        self.audit = AuditTrail()

    # -- canonical shared-CIR accessors (read-only views over memory) -----
    @property
    def twin(self):
        return self.memory.get(KEY_TWIN)

    @property
    def pipelines(self) -> list:
        return self.memory.get(KEY_PIPELINES, []) or []

    @property
    def cir(self) -> list:
        return self.memory.get(KEY_CIR, []) or []

    def params(self) -> dict:
        return {"paths": self.paths, "source_format": self.source_format,
                "dialect": self.dialect, "target_region": self.target_region,
                "project": self.project}

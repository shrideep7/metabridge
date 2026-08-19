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
                 target_region: str = "", project: str = "estate",
                 connection_ids: Optional[List[str]] = None) -> None:
        self.paths: List[str] = list(paths or [])
        self.source_format = source_format
        self.dialect = dialect
        self.target_region = target_region
        self.project = project
        # Saved connections (Snowflake, SAP HANA, Oracle…) this run is scoped
        # to. A run may carry paths, connections, or both: the folder supplies
        # the pipelines to convert, the connection supplies the live estate
        # they land in. Discovery reads them into the twin; the view SQL a
        # connection exposes is materialized into `paths` by the caller so the
        # parse-dependent agents score on real evidence either way.
        self.connection_ids: List[str] = list(connection_ids or [])
        # Capture warnings from reading those live systems ("unreachable",
        # "no inventory — discovered as a system only"). They ride in params()
        # so they are persisted with the run and still readable when someone
        # reopens it, rather than only in the response to the request that
        # started it.
        self.connection_notes: List[str] = []
        # Whether a human uploaded a project tree for this run. Recorded
        # explicitly rather than inferred from `paths`, because one of those
        # paths may be the SQL materialized out of a connection — and "did
        # this read my warehouse or a folder someone uploaded" changes what
        # the run's numbers mean.
        self.has_upload: bool = False
        self.memory = SharedMemory()
        self.audit = AuditTrail()

    def has_sources(self) -> bool:
        return bool(self.paths or self.connection_ids)

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
                "project": self.project,
                "connection_ids": self.connection_ids,
                "connection_notes": self.connection_notes,
                "has_upload": self.has_upload}

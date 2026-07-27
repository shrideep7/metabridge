"""Shared canonical models — the backbone of the MetaBridge OS.

Every core engine reads and writes these canonical models rather than
talking to each other point-to-point: the 18 source parsers all produce
the IR ``Pipeline``; the semantic engine lifts that into the CIR
``Project``; discovery builds the ``DigitalTwin`` graph that the
intelligence engines (AI-readiness, security, tech-debt, FinOps, docs,
observability) consume; streaming and orchestration converge on the CER
and COR models; SAP lowers into the same IR. This module is the registry
of those models, with the producers/consumers declared so the platform
data-flow is navigable instead of implicit.

Each entry names a REAL class; ``resolve()`` imports it so the manifest
reports whether the model is actually present, never a fiction.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class CanonicalModel:
    id: str
    name: str
    module: str
    symbol: str
    description: str
    produced_by: Tuple[str, ...] = field(default_factory=tuple)
    consumed_by: Tuple[str, ...] = field(default_factory=tuple)

    def resolve(self) -> dict:
        """Import the backing class so presence is verified, not assumed."""
        try:
            mod = importlib.import_module(self.module)
        except Exception as exc:                 # noqa: BLE001
            return {"available": False, "reason": "import error: %s" % exc}
        if not hasattr(mod, self.symbol):
            return {"available": False,
                    "reason": "missing symbol %s" % self.symbol}
        return {"available": True}

    def to_dict(self) -> dict:
        r = self.resolve()
        return {"id": self.id, "name": self.name,
                "ref": "%s.%s" % (self.module, self.symbol),
                "description": self.description,
                "produced_by": list(self.produced_by),
                "consumed_by": list(self.consumed_by),
                "available": r["available"],
                "reason": r.get("reason", "")}


CANONICAL_MODELS: Tuple[CanonicalModel, ...] = (
    CanonicalModel(
        "ir_pipeline", "Parsed IR (Pipeline)", "metabridge.ir.model",
        "Pipeline",
        "The canonical intermediate representation every source parser "
        "produces (dbt, PowerCenter, IDMC, SAP, 14 more).",
        produced_by=("data_estate", "migration"),
        consumed_by=("semantic", "migration", "validation", "governance",
                     "ai_readiness", "security", "technical_debt", "finops",
                     "documentation")),
    CanonicalModel(
        "cir_project", "Semantic CIR (Project)", "metabridge.cir.model",
        "Project",
        "Platform-neutral semantic representation (intent, not syntax) "
        "lifted from the IR by the Semantic Intelligence Engine.",
        produced_by=("semantic",),
        consumed_by=("migration", "validation", "documentation",
                     "agent_orchestration")),
    CanonicalModel(
        "digital_twin", "Digital Twin (estate graph)",
        "metabridge.twin.model", "DigitalTwin",
        "One typed property graph of the whole estate, discovered from "
        "uploads, connections and prior jobs.",
        produced_by=("data_estate", "digital_twin"),
        consumed_by=("technical_debt", "finops", "security", "documentation",
                     "observability", "ai_readiness")),
    CanonicalModel(
        "cer_estate", "Streaming estate (CER)", "metabridge.events.cer",
        "CER",
        "Canonical event/streaming representation (brokers, topics, "
        "consumers, CDC, IoT) for real-time modernization.",
        produced_by=("data_estate",),
        consumed_by=("migration", "pipeline_studio", "observability")),
    CanonicalModel(
        "cor_orchestration", "Orchestration (COR)",
        "metabridge.orchestration.cor", "COR",
        "Canonical orchestration representation (workflows, tasks, "
        "dependencies, schedules) across schedulers.",
        produced_by=("pipeline_studio",),
        consumed_by=("migration", "pipeline_studio", "observability")),
    CanonicalModel(
        "sap_landscape", "SAP landscape", "metabridge.sap.model",
        "SAPLandscape",
        "SAP-native semantic model (CDS, calc views, BW, ABAP, process "
        "chains) before lowering into the shared IR.",
        produced_by=("data_estate",),
        consumed_by=("semantic", "migration")),
    CanonicalModel(
        "agent_context", "Agent shared context",
        "metabridge.agents.context", "SharedContext",
        "The shared CIR + blackboard memory + audit the agent swarm "
        "collaborates through.",
        produced_by=("agent_orchestration",),
        consumed_by=("agent_orchestration", "observability")),
)


def canonical_registry() -> list:
    return [m.to_dict() for m in CANONICAL_MODELS]


def canonical_ids() -> Tuple[str, ...]:
    return tuple(m.id for m in CANONICAL_MODELS)

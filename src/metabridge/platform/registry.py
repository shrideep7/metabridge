"""Core engine + platform-service registry.

Every one of the sixteen core engines and nine platform services is
described here as a descriptor over its EXISTING implementation — the OS
is a uniform, self-describing registry, not a rewrite. Each descriptor
declares the canonical models it consumes/produces (so the platform
data-flow is navigable) and an honest health probe that actually imports
the backing module and checks the entrypoint symbol, reporting
``available`` / ``degraded`` / ``error`` (or ``external`` for services
provided by the web application layer) — never a hardcoded "healthy".
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import List, Tuple

from .canonical import CANONICAL_MODELS, canonical_ids


class Category:
    ESTATE = "Estate"
    MODERNIZATION = "Modernization"
    GOVERNANCE = "Governance"
    INTELLIGENCE = "Intelligence"
    EXTENSIBILITY = "Extensibility"
    OPERATIONS = "Operations"


CATEGORY_ORDER = (Category.ESTATE, Category.MODERNIZATION,
                  Category.GOVERNANCE, Category.INTELLIGENCE,
                  Category.EXTENSIBILITY, Category.OPERATIONS)


def _probe(module: str, symbol: str, web_ok: bool = False) -> dict:
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        if web_ok:
            return {"status": "external",
                    "detail": "provided by the web application layer"}
        return {"status": "error", "detail": "import error: %s" % exc}
    except Exception as exc:                      # noqa: BLE001
        return {"status": "error", "detail": "load error: %s" % exc}
    if symbol and not hasattr(mod, symbol):
        return {"status": "degraded",
                "detail": "missing entrypoint %s" % symbol}
    return {"status": "available", "detail": ""}


@dataclass(frozen=True)
class EngineDescriptor:
    id: str
    name: str
    category: str
    description: str
    module: str
    entrypoint: str
    consumes: Tuple[str, ...] = field(default_factory=tuple)
    produces: Tuple[str, ...] = field(default_factory=tuple)
    capabilities: Tuple[str, ...] = field(default_factory=tuple)
    api_prefix: str = ""

    def health(self) -> dict:
        return _probe(self.module, self.entrypoint)

    def to_dict(self) -> dict:
        h = self.health()
        return {"id": self.id, "name": self.name, "category": self.category,
                "description": self.description,
                "entrypoint": "%s.%s" % (self.module, self.entrypoint),
                "consumes": list(self.consumes),
                "produces": list(self.produces),
                "capabilities": list(self.capabilities),
                "api_prefix": self.api_prefix,
                "health": h["status"], "detail": h["detail"]}


@dataclass(frozen=True)
class ServiceDescriptor:
    id: str
    name: str
    description: str
    module: str
    symbol: str
    layer: str = "platform"          # platform | web

    def health(self) -> dict:
        return _probe(self.module, self.symbol, web_ok=(self.layer == "web"))

    def to_dict(self) -> dict:
        h = self.health()
        return {"id": self.id, "name": self.name,
                "description": self.description, "layer": self.layer,
                "backing": "%s.%s" % (self.module, self.symbol),
                "health": h["status"], "detail": h["detail"]}


# --- the sixteen core engines --------------------------------------------
ENGINES: Tuple[EngineDescriptor, ...] = (
    EngineDescriptor(
        "data_estate", "Enterprise Data Estate", Category.ESTATE,
        "Multi-source discovery and inventory of the whole estate.",
        "metabridge.twin.discover", "build_twin",
        produces=("digital_twin", "cer_estate", "sap_landscape"),
        capabilities=("discover", "inventory", "connections"),
        api_prefix="/api/twin"),
    EngineDescriptor(
        "digital_twin", "Digital Twin", Category.ESTATE,
        "One typed graph of the estate + deterministic analytics "
        "(blast radius, lineage, impact, migration waves).",
        "metabridge.twin.analyze", "impact_analysis",
        consumes=("digital_twin",), produces=("digital_twin",),
        capabilities=("graph", "blast_radius", "impact", "simulate"),
        api_prefix="/api/twin"),
    EngineDescriptor(
        "semantic", "Semantic Intelligence Engine", Category.MODERNIZATION,
        "Lifts parsed IR into the platform-neutral semantic CIR.",
        "metabridge.cir.builder", "build_cir",
        consumes=("ir_pipeline", "sap_landscape"), produces=("cir_project",),
        capabilities=("semantic_parse", "expression_intent"),
        api_prefix="/api/jobs"),
    EngineDescriptor(
        "migration", "Migration Engine", Category.MODERNIZATION,
        "Converts sources to targets (dbt/PowerCenter/IDMC/…) with an "
        "AI review + approve-and-apply queue.",
        "metabridge.engine", "convert",
        consumes=("ir_pipeline", "cir_project"),
        produces=("ir_pipeline",),
        capabilities=("parse", "convert", "review", "apply"),
        api_prefix="/api/jobs"),
    EngineDescriptor(
        "pipeline_studio", "Pipeline Studio", Category.MODERNIZATION,
        "Generates governed pipelines and orchestration across "
        "schedulers.",
        "metabridge.orchestration.cor", "COR",
        consumes=("cir_project", "digital_twin"),
        produces=("cor_orchestration",),
        capabilities=("scaffold", "orchestration"), api_prefix="/api/scaffold"),
    EngineDescriptor(
        "validation", "Validation Engine", Category.MODERNIZATION,
        "Scores conversion confidence and validates parse/semantic "
        "integrity and generated artifacts.",
        "metabridge.report.confidence", "score_pipeline_confidence",
        consumes=("ir_pipeline", "cir_project"),
        capabilities=("confidence", "structural_validation"),
        api_prefix="/api/validate"),
    EngineDescriptor(
        "governance", "Governance Engine", Category.GOVERNANCE,
        "Classifies PII/PHI/financial data and evaluates residency & "
        "masking policy.",
        "metabridge.governance.engine", "govern",
        consumes=("ir_pipeline",),
        capabilities=("classify", "policy", "residency"),
        api_prefix="/api/governance"),
    EngineDescriptor(
        "ai_readiness", "AI Readiness", Category.INTELLIGENCE,
        "Scores 15 AI-readiness dimensions and prescribes an AI roadmap.",
        "metabridge.ai_readiness.engine", "assess_ai_readiness",
        consumes=("ir_pipeline", "digital_twin"),
        capabilities=("readiness", "rag", "roadmap"),
        api_prefix="/api/ai-readiness"),
    EngineDescriptor(
        "security", "Security Intelligence", Category.INTELLIGENCE,
        "Security posture + compliance coverage (GDPR/HIPAA/SOX/ISO/NIST).",
        "metabridge.security.engine", "analyze_security",
        consumes=("digital_twin", "ir_pipeline"),
        capabilities=("posture", "compliance", "secrets_scan"),
        api_prefix="/api/security"),
    EngineDescriptor(
        "technical_debt", "Technical Debt", Category.INTELLIGENCE,
        "Detects unused/duplicate/dead assets and costs the cleanup.",
        "metabridge.debt.engine", "assess_technical_debt",
        consumes=("digital_twin", "ir_pipeline"),
        capabilities=("debt", "reachability", "cleanup_plan"),
        api_prefix="/api/tech-debt"),
    EngineDescriptor(
        "finops", "FinOps", Category.INTELLIGENCE,
        "Models cloud cost, migration ROI and warehouse/cluster sizing.",
        "metabridge.finops.engine", "analyze_finops",
        consumes=("digital_twin", "ir_pipeline"),
        capabilities=("cost_model", "roi", "sizing"),
        api_prefix="/api/finops"),
    EngineDescriptor(
        "documentation", "Documentation", Category.OPERATIONS,
        "Generates the enterprise documentation set (PDF/Word/MD/HTML).",
        "metabridge.docs.generate", "generate_all",
        consumes=("ir_pipeline", "digital_twin", "cir_project"),
        capabilities=("docs", "export"), api_prefix="/api/docs"),
    EngineDescriptor(
        "marketplace", "Marketplace", Category.EXTENSIBILITY,
        "Install signed connectors, validators, AI skills, templates and "
        "accelerators with dependency resolution.",
        "metabridge.marketplace.catalog", "get_catalog",
        capabilities=("catalog", "install", "signing"),
        api_prefix="/api/marketplace"),
    EngineDescriptor(
        "plugin_sdk", "Plugin SDK", Category.EXTENSIBILITY,
        "Uniform plugin contract + registry + hot loading for every "
        "capability.",
        "metabridge.plugins.registry", "PluginRegistry",
        capabilities=("register", "hot_load", "scaffold"),
        api_prefix="/api/plugins"),
    EngineDescriptor(
        "agent_orchestration", "Agent Orchestration", Category.OPERATIONS,
        "Twelve governed agents over the shared CIR with confidence "
        "scoring, approval and a tamper-evident audit trail.",
        "metabridge.agents.orchestrator", "TaskOrchestrator",
        consumes=("agent_context", "digital_twin", "ir_pipeline",
                  "cir_project"),
        produces=("agent_context",),
        capabilities=("orchestrate", "govern", "approve"),
        api_prefix="/api/agents"),
    EngineDescriptor(
        "observability", "Observability", Category.OPERATIONS,
        "Operational monitoring of run history + modeled resource/cloud "
        "with SLA, alerting and health scoring.",
        "metabridge.observability.engine", "observe",
        consumes=("agent_context", "digital_twin"),
        capabilities=("monitor", "sla", "alerting", "trends"),
        api_prefix="/api/observability"),
)


# --- the nine common platform services -----------------------------------
SERVICES: Tuple[ServiceDescriptor, ...] = (
    ServiceDescriptor(
        "authentication", "Authentication",
        "Accounts + server-side sessions (PBKDF2-SHA256).",
        "web.auth", "AuthStore", layer="web"),
    ServiceDescriptor(
        "rbac", "RBAC",
        "Role-based permission guard (owner/admin/engineer/viewer + "
        "agents:approve) enforced on every API route.",
        "web.auth", "PERMISSIONS", layer="web"),
    ServiceDescriptor(
        "audit", "Audit",
        "Tamper-evident, HMAC-keyed, re-verified-on-read audit chains.",
        "metabridge.agents.audit", "AuditTrail"),
    ServiceDescriptor(
        "reporting", "Reporting",
        "Document & report generation and export (PDF/Word/MD/HTML).",
        "metabridge.docs.generate", "generate_all"),
    ServiceDescriptor(
        "notifications", "Notifications",
        "In-app notification center / event log (topics, severities).",
        "metabridge.platform.notifications", "NotificationCenter"),
    ServiceDescriptor(
        "secrets", "Secrets",
        "Write-only secret storage (0600) with param/secret separation; "
        "secrets resolve only when opted in (package signing is provided "
        "by the Marketplace engine).",
        "metabridge.connections_store", "resolve_params"),
    ServiceDescriptor(
        "version_management", "Version Management",
        "Component version registry + compatibility checks.",
        "metabridge.platform.versions", "VersionRegistry"),
    ServiceDescriptor(
        "feature_flags", "Feature Flags",
        "Deterministic capability gating (enable / role / rollout).",
        "metabridge.platform.flags", "FeatureFlags"),
    ServiceDescriptor(
        "plugin_registry", "Plugin Registry",
        "First-party + installed plugin registry with health.",
        "metabridge.plugins.registry", "PluginRegistry"),
)


class PlatformRegistry:
    def __init__(self) -> None:
        self._engines = {e.id: e for e in ENGINES}
        self._services = {s.id: s for s in SERVICES}

    def engines(self) -> List[EngineDescriptor]:
        return list(ENGINES)

    def services(self) -> List[ServiceDescriptor]:
        return list(SERVICES)

    def engine(self, engine_id: str):
        return self._engines.get(engine_id)

    def by_category(self) -> dict:
        out = {c: [] for c in CATEGORY_ORDER}
        for e in ENGINES:
            out.setdefault(e.category, []).append(e.to_dict())
        return out

    def validate_canonical_refs(self) -> List[str]:
        """Every consumes/produces must name a real canonical model."""
        valid = set(canonical_ids())
        problems = []
        for e in ENGINES:
            for ref in tuple(e.consumes) + tuple(e.produces):
                if ref not in valid:
                    problems.append("%s references unknown canonical "
                                    "model %r" % (e.id, ref))
        return problems

    def health(self) -> dict:
        eng = [e.to_dict() for e in ENGINES]
        svc = [s.to_dict() for s in SERVICES]
        summary = {}
        for item in eng + svc:
            summary[item["health"]] = summary.get(item["health"], 0) + 1
        canon_ok = all(m.resolve()["available"] for m in CANONICAL_MODELS)
        # operational spans engines AND services AND canonical models — a
        # broken service or unresolvable model flips it (external web-layer
        # services are acceptable, not a fault)
        engines_ok = all(e["health"] == "available" for e in eng)
        services_ok = all(s["health"] in ("available", "external")
                          for s in svc)
        return {"engines_total": len(eng), "services_total": len(svc),
                "canonical_total": len(CANONICAL_MODELS),
                "summary": summary,
                "engines_operational": engines_ok,
                "services_operational": services_ok,
                "canonical_resolvable": canon_ok,
                "operational": engines_ok and services_ok and canon_ok}

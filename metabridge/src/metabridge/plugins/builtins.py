"""First-party plugin registration.

Every MetaBridge engine is registered here as a built-in plugin, so the
platform's own capabilities are discovered through the same registry
third-party plugins use. Invokers import their engine lazily (keeping
registry import cheap and avoiding import cycles), and each plugin's
health check confirms its backing module actually imports.
"""
from __future__ import annotations

import importlib
from typing import Callable, Dict, List

from .spec import Plugin, PluginManifest


def _health_for(module_path: str) -> Callable[[], dict]:
    def check():
        try:
            importlib.import_module(module_path)
            return {"status": "ok", "detail": "backing module imports"}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "detail": "import failed: %s" % e}
    return check


def _mk(reg, plugin_id: str, name: str, ptype: str,
        impls: Dict[str, Callable], module_path: str,
        description: str = "", deps: List[dict] = None,
        version: str = "1.0.0") -> None:
    manifest = PluginManifest(
        id=plugin_id, name=name, version=version, type=ptype,
        capabilities=sorted(impls), supported_versions=">=1.0,<2.0",
        dependencies=deps or [], description=description,
        author="MetaBridge", builtin=True)
    try:
        plugin = Plugin(manifest, impls, health_fn=_health_for(module_path))
        reg.register(plugin, replace=True)
    except Exception:  # noqa: BLE001 — one bad builtin must not break all
        pass


# --- lazy engine accessors -------------------------------------------------

def _engine():
    from .. import engine
    return engine


# ---------------------------------------------------------------------------

def register_builtins(reg) -> int:
    """Register all first-party engines as plugins. Idempotent (replaces)."""
    before = len(reg.all()) if reg._plugins else 0

    # -- parsers (one per supported source format) --------------------------
    try:
        formats = list(_engine().FORMATS)
    except Exception:  # noqa: BLE001
        formats = []
    for fmt in formats:
        _mk(reg, "parser.%s" % fmt, "%s parser" % fmt, "parser",
            {"parse": (lambda path, _f=fmt:
                       _engine().parse_input(path, _f)),
             "detect_format": (lambda path: _engine().detect_format(path))},
            "metabridge.engine",
            "Parses %s projects into the canonical IR." % fmt)

    # -- target generators --------------------------------------------------
    _gens = {
        "dbt": ("metabridge.generators.dbt_generator",
                "generate_dbt_project"),
        "powercenter": ("metabridge.generators.powercenter_generator",
                        "generate_powercenter"),
        "idmc": ("metabridge.generators.idmc_generator", "generate_idmc"),
    }
    for tgt, (mod, fn) in _gens.items():
        _mk(reg, "generator.%s" % tgt, "%s generator" % tgt,
            "target_generator",
            {"generate": _bind(mod, fn)}, mod,
            "Generates a %s project from the canonical IR." % tgt)

    # -- source connectors (from the marketplace connector registry) --------
    try:
        from ..connectors.base import get_registry as _conn_reg
        specs = _conn_reg().all()
    except Exception:  # noqa: BLE001
        specs = []
    for spec in specs:
        key = spec.key
        _mk(reg, "connector.%s" % key, "%s connector" % spec.name,
            "source_connector",
            {"describe": (lambda _s=spec: _s.to_dict()),
             "introspect": _bind_introspect(key)},
            "metabridge.connectors.base",
            "Live source connector: %s (%s)." % (spec.name, spec.category))

    # -- validators ---------------------------------------------------------
    _mk(reg, "validator.conversion", "Conversion validator", "validator",
        {"validate": _bind("metabridge.validate.conversion_validator",
                           "validate_conversion")},
        "metabridge.validate.conversion_validator",
        "Schema / data / transformation / semantic equivalence checks.")
    _mk(reg, "validator.powercenter", "PowerCenter XML validator",
        "validator",
        {"validate": _bind("metabridge.validate.powercenter_validator",
                           "validate_powercenter_xml")},
        "metabridge.validate.powercenter_validator",
        "Structural + optional DTD validation of PowerCenter XML.")

    # -- lineage provider ---------------------------------------------------
    _mk(reg, "lineage.default", "Column & table lineage",
        "lineage_provider",
        {"table_lineage": _bind("metabridge.report.lineage",
                                "table_lineage"),
         "column_lineage": _bind("metabridge.report.lineage",
                                 "column_lineage")},
        "metabridge.report.lineage",
        "Table- and column-level lineage from the canonical IR.")

    # -- documentation generator (exposes all 14 document types) ------------
    try:
        from ..docs.generate import DOC_TYPES, generate_document
        doc_caps = {slug: (lambda ctx, _s=slug:
                           generate_document(_s, ctx)) for slug in DOC_TYPES}
        if doc_caps:
            _mk(reg, "docs.generator", "Documentation generator",
                "documentation_generator", doc_caps,
                "metabridge.docs.generate",
                "Generates 14 document types, each exportable to "
                "PDF/Word/Markdown/HTML.",
                deps=[{"name": "reportlab"}, {"name": "docx"}])
    except Exception:  # noqa: BLE001
        pass

    # -- AI reviewer --------------------------------------------------------
    _mk(reg, "ai.reviewer", "AI conversion reviewer", "ai_reviewer",
        {"review": _bind("metabridge.llm.review_agent",
                         "review_migration")},
        "metabridge.llm.review_agent",
        "Advisory AI review of a conversion (never overwrites "
        "deterministic output).")

    # -- report generators --------------------------------------------------
    _reports = {
        "assessment": ("metabridge.assessment.engine", "assess",
                       "Board-grade migration assessment (parse-only)."),
        "ai_readiness": ("metabridge.ai_readiness.engine",
                         "assess_ai_readiness",
                         "Enterprise AI readiness assessment."),
        "tech_debt": ("metabridge.debt.engine", "assess_technical_debt",
                      "Technical debt intelligence."),
        "finops": ("metabridge.finops.engine", "analyze_finops",
                   "Enterprise FinOps cost model."),
        "security": ("metabridge.security.engine", "analyze_security",
                     "Security & compliance intelligence."),
        "event_intelligence": ("metabridge.events.insight",
                               "analyze_topology",
                               "Event/streaming topology intelligence."),
    }
    for name, (mod, fn, desc) in _reports.items():
        _mk(reg, "report.%s" % name, "%s report" % name.replace("_", " "),
            "report_generator", {"generate": _bind(mod, fn)}, mod, desc)

    # -- pipeline scaffold --------------------------------------------------
    _mk(reg, "scaffold.default", "Pipeline scaffold", "pipeline_scaffold",
        {"build_pipeline": _bind("metabridge.scaffold", "build_pipeline")},
        "metabridge.scaffold",
        "Scaffolds a modernization pipeline from a source descriptor.")

    # -- security analyzers -------------------------------------------------
    _mk(reg, "security.analyzer", "Security analyzer", "security_analyzer",
        {"analyze": _bind("metabridge.security.engine",
                          "analyze_security")},
        "metabridge.security.engine",
        "Estate security posture + framework control-gap analysis.")
    _mk(reg, "security.governance", "Governance classifier",
        "security_analyzer",
        {"govern": _bind("metabridge.governance.engine", "govern"),
         "classify": _bind("metabridge.governance.engine",
                           "classify_pipeline")},
        "metabridge.governance.engine",
        "GDPR/CCPA/HIPAA classification, policy & processing register.")

    return len(reg.all()) - before


def _bind(module_path: str, func: str) -> Callable:
    def call(*args, **kwargs):
        mod = importlib.import_module(module_path)
        return getattr(mod, func)(*args, **kwargs)
    return call


def _bind_introspect(key: str) -> Callable:
    def call(params, **kwargs):
        from ..livecheck import introspect
        return introspect(key, params, **kwargs)
    return call

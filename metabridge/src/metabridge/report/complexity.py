"""Migration complexity engine.

Scores every asset (mapping/pipeline) on the factors that actually drive
migration effort, then rolls up to a project view:

    complexity_score        0-100 (weighted factor points, capped)
    complexity_level        LOW | MEDIUM | HIGH | VERY_HIGH |
                            MANUAL_REVIEW_REQUIRED
    conversion_confidence   0-100 (how sure the automated conversion is)
    automation_percentage   0-100 (how much of the asset converts untouched)
    manual_effort_estimate  hours (triage baseline for factory planning)
    migration_risks         concrete, named risks with evidence

Everything is computed from evidence — the IR graph, the origin SQL (via the
typed AST), and the conversion findings — never from asset names or vibes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from ..ir.model import (
    IssueSeverity, LoadStrategy, Mapping, Pipeline, TransformationType,
)

LEVELS = ("LOW", "MEDIUM", "HIGH", "VERY_HIGH", "MANUAL_REVIEW_REQUIRED")

# factor -> (points per unit, cap, risk template)
_FACTORS = {
    "transformations":     (1.5, 18, ""),
    "nested_queries":      (4.0, 20, "Deeply nested queries (%d) complicate "
                                     "native graph conversion"),
    "stored_procedures":   (18.0, 36, "%d stored procedure(s) require manual "
                                      "or LLM-drafted decomposition"),
    "unsupported_functions": (6.0, 24, "%d expression(s) had no rule-based "
                                       "translation"),
    "dynamic_sql":         (15.0, 30, "Dynamic SQL detected — behavior depends "
                                      "on runtime values"),
    "procedural_logic":    (12.0, 24, "Procedural logic (loops/branching) "
                                      "needs set-based redesign"),
    "external_dependencies": (2.0, 10, "External inputs outside the project "
                                       "boundary — confirm availability at "
                                       "the target"),
    "complex_lookups":     (4.0, 12, "%d lookup(s) — verify multi-match "
                                     "semantics after conversion"),
    "scd_logic":           (8.0, 8, "Slowly-changing-dimension logic — "
                                    "validate history behavior post-migration"),
    "cdc_logic":           (10.0, 10, "CDC/stream logic — replay and ordering "
                                      "semantics differ across platforms"),
    "recursive_sql":       (12.0, 12, "Recursive SQL — support varies by "
                                      "target platform"),
    "platform_specific":   (5.0, 20, "%d platform-specific construct(s) "
                                     "(fallbacks/overrides) in play"),
}

_DYNAMIC_SQL_RE = re.compile(
    r"EXECUTE\s+IMMEDIATE|sp_executesql|\bEXEC\s*\(|IDENTIFIER\s*\(",
    re.IGNORECASE)
_RECURSIVE_RE = re.compile(r"WITH\s+RECURSIVE|CONNECT\s+BY", re.IGNORECASE)
_PROCEDURAL_RE = re.compile(
    r"\bBEGIN\b[\s\S]{0,2000}?\bEND\b|\bLOOP\b|\bCURSOR\b|\bWHILE\b",
    re.IGNORECASE)
_CDC_RE = re.compile(r"\bCDC\b|CHANGE_TRACKING|CREATE\s+STREAM|\bCHANGES\s*\(",
                     re.IGNORECASE)


@dataclass
class AssetComplexity:
    name: str
    complexity_score: int
    complexity_level: str
    conversion_confidence: int
    automation_percentage: int
    manual_effort_estimate: float          # hours
    factors: Dict[str, int] = field(default_factory=dict)
    migration_risks: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _count_factors(m: Mapping) -> Dict[str, int]:
    origin = m.origin or ""
    f: Dict[str, int] = {}
    f["transformations"] = len([t for t in m.transformations
                                if t.name != "__OUTPUT__"])
    # nested queries via the typed AST (never regex on structure)
    nested = 0
    if origin.strip():
        try:
            from ..sqlx.ast import parse_statements
            for stmt in parse_statements(origin):
                if stmt.select is not None:
                    nested += len(stmt.select.subqueries)
                    nested += len(stmt.select.ctes)
        except Exception:  # noqa: BLE001 — scoring must never crash
            pass
    f["nested_queries"] = nested

    procs = sum(1 for i in m.issues if i.code in
                ("STATEMENT_UNSUPPORTED", "STATEMENT_PARSE_FAILED")
                and re.search(r"PROCEDURE|FUNCTION|CALL|TASK", i.detail or "",
                              re.IGNORECASE))
    f["stored_procedures"] = procs
    f["unsupported_functions"] = sum(
        1 for i in m.issues if i.code == "EXPRESSION_UNCONVERTED")
    f["dynamic_sql"] = 1 if _DYNAMIC_SQL_RE.search(origin) else 0
    f["procedural_logic"] = 1 if (procs or _PROCEDURAL_RE.search(origin)) else 0
    f["external_dependencies"] = len(m.depends_on) + len(
        m.by_type(TransformationType.SOURCE))
    f["complex_lookups"] = len(m.by_type(TransformationType.LOOKUP)) + sum(
        1 for i in m.issues if i.code == "LOOKUP_AS_JOIN")
    f["scd_logic"] = 1 if m.load_strategy == LoadStrategy.SCD2 or \
        m.properties.get("scd") else 0
    f["cdc_logic"] = 1 if _CDC_RE.search(origin) else 0
    f["recursive_sql"] = 1 if _RECURSIVE_RE.search(origin) else 0
    f["platform_specific"] = sum(
        1 for i in m.issues if i.code in
        ("SQL_OVERRIDE_FALLBACK", "TEMPLATE_VARIABLES", "JINJA_UNSUPPORTED",
         "MAPPING_PARAMETER_AS_VAR"))
    return f


def score_mapping(m: Mapping) -> AssetComplexity:
    factors = _count_factors(m)

    score = 0.0
    risks: List[str] = []
    for name, count in factors.items():
        weight, cap, risk_tpl = _FACTORS[name]
        if count <= 0:
            continue
        score += min(count * weight, cap)
        if risk_tpl and name not in ("transformations",):
            risks.append(risk_tpl % count if "%d" in risk_tpl else risk_tpl)
    score = int(round(min(100.0, score)))

    manual_issues = sum(1 for i in m.issues
                        if i.severity == IssueSeverity.MANUAL)
    warning_issues = sum(1 for i in m.issues
                         if i.severity == IssueSeverity.WARNING)
    error_issues = sum(1 for i in m.issues
                       if i.severity == IssueSeverity.ERROR)

    # module 31: weighted transformation impact, damped by the worst
    # critical-path node — never a simple average (issue deductions are
    # applied inside the model)
    from .confidence import score_mapping_confidence
    confidence = score_mapping_confidence(m)["conversion_confidence"]
    confidence -= (10 if factors["dynamic_sql"] else 0) \
        + (5 if factors["platform_specific"] else 0)
    confidence = max(5, min(100, confidence))

    # automation: how much converts untouched
    automation = 100
    automation -= manual_issues * 12 + error_issues * 40
    automation -= 25 if factors["stored_procedures"] else 0
    automation -= 10 if factors["platform_specific"] else 0
    automation = max(0, min(100, automation))

    # effort baseline (hours): base by score band + per-manual-item
    effort = 0.5 + score / 25.0 + manual_issues * 1.0 \
        + factors["stored_procedures"] * 2.0
    effort = round(effort, 1)

    if error_issues or factors["dynamic_sql"] or \
            (manual_issues >= 3 and score >= 50) or score >= 85:
        level = "MANUAL_REVIEW_REQUIRED"
    elif score >= 65:
        level = "VERY_HIGH"
    elif score >= 40:
        level = "HIGH"
    elif score >= 20:
        level = "MEDIUM"
    else:
        level = "LOW"

    return AssetComplexity(
        name=m.name, complexity_score=score, complexity_level=level,
        conversion_confidence=confidence, automation_percentage=automation,
        manual_effort_estimate=effort, factors=factors,
        migration_risks=risks)


def score_pipeline(pipeline: Pipeline) -> dict:
    """Project rollup + per-asset scores."""
    assets = [score_mapping(m) for m in pipeline.mappings]

    # project-level factors from project-level findings (procedures etc.)
    project_procs = sum(1 for i in pipeline.issues if i.code in
                        ("STATEMENT_UNSUPPORTED", "STATEMENT_PARSE_FAILED"))
    distribution = {level: 0 for level in LEVELS}
    for a in assets:
        distribution[a.complexity_level] += 1

    weights = [max(1, a.factors["transformations"]) for a in assets] or [1]
    total_w = sum(weights)
    overall = int(round(sum(a.complexity_score * w for a, w in
                            zip(assets, weights)) / total_w)) if assets else 0
    if project_procs:
        overall = min(100, overall + min(project_procs, 20))

    confidence = int(round(sum(a.conversion_confidence * w for a, w in
                               zip(assets, weights)) / total_w)) if assets else 100
    automation = int(round(sum(a.automation_percentage * w for a, w in
                               zip(assets, weights)) / total_w)) if assets else 100
    effort = round(sum(a.manual_effort_estimate for a in assets)
                   + project_procs * 2.0, 1)

    risk_counts: Dict[str, int] = {}
    for a in assets:
        for r in a.migration_risks:
            risk_counts[r] = risk_counts.get(r, 0) + 1
    top_risks = [{"risk": r, "assets_affected": n} for r, n in
                 sorted(risk_counts.items(), key=lambda x: -x[1])[:10]]
    if project_procs:
        top_risks.insert(0, {
            "risk": "%d project-level stored procedures / unsupported "
                    "statements need decomposition" % project_procs,
            "assets_affected": project_procs})

    return {
        "complexity_score": overall,
        "complexity_level": next(l for s, l in
                                 ((85, "MANUAL_REVIEW_REQUIRED"),
                                  (65, "VERY_HIGH"), (40, "HIGH"),
                                  (20, "MEDIUM"), (-1, "LOW"))
                                 if overall >= s or s == -1),
        "conversion_confidence": confidence,
        "automation_percentage": automation,
        "manual_effort_estimate_hours": effort,
        "level_distribution": distribution,
        "migration_risks": top_risks,
        "assets": [a.to_dict() for a in assets],
    }

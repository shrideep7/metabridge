"""Migration confidence scoring (Phase 2, module 31).

Every transformation gets a conversion-confidence score reflecting how
completely its semantics survive automated conversion (the phase-2
handler for each type earns its number — Lookup loses points for
match-policy/cache subtleties, Update Strategy for DML routing edge
cases, Java has no conversion at all):

    Source Qualifier 98   Expression 95   Filter 98   Joiner 95
    Lookup 88             Aggregator 95   Router 85   Update Strategy 80
    Sorter 97  Union 96  Rank 92  Sequence 85  Normalizer 88
    Stored Procedure 55   SQL Transformation 60   Java 35

Mapping confidence is NOT a simple average. Each node is weighted by
IMPACT (critical path — an ancestor of the target — weighs 1.0, side
branches 0.3), and the weighted mean is then DAMPED by the worst
critical-path node:

    confidence = weighted_mean * sqrt(min(1, bottleneck / weighted_mean))

so one Java transformation (35) on the critical path drags an otherwise
95-ish mapping into the 50s — visible, not averaged away.

Returned per object: mapping_complexity, conversion_confidence,
automation_percentage, critical_risks, manual_review_items.
"""
from __future__ import annotations

import math
from typing import Dict, List, Set

from ..ir.model import IssueSeverity, Mapping, Pipeline, TransformationType

TYPE_CONFIDENCE: Dict[str, int] = {
    "SOURCE": 100, "TARGET": 100,
    "SOURCE_QUALIFIER": 98, "EXPRESSION": 95, "FILTER": 98,
    "JOINER": 95, "LOOKUP": 88, "AGGREGATOR": 95, "ROUTER": 85,
    "UPDATE_STRATEGY": 80, "SORTER": 97, "UNION": 96, "RANK": 92,
    "SEQUENCE": 85,
}

# original PowerCenter types that survive only as placeholders
PC_TYPE_CONFIDENCE: Dict[str, int] = {
    "JAVA TRANSFORMATION": 35, "JAVA": 35,
    "CUSTOM TRANSFORMATION": 40, "CUSTOM": 40,
    "STORED PROCEDURE": 55, "SQL TRANSFORMATION": 60,
    "TRANSACTION CONTROL": 50, "EXTERNAL PROCEDURE": 35,
    "UNSTRUCTURED DATA": 35, "XML PARSER": 45, "XML GENERATOR": 45,
}

CRITICAL_THRESHOLD = 70


def node_confidence(t) -> int:
    """Confidence for one transformation node, adjusted by what the
    handlers actually recorded about it."""
    pc_type = str(t.properties.get("unconverted_pc_type", "")).upper()
    if pc_type:
        return PC_TYPE_CONFIDENCE.get(pc_type, 45)
    score = TYPE_CONFIDENCE.get(t.type.value, 70)
    sqlt = t.properties.get("sql_transformation_cir") or {}
    if sqlt:
        score = 45 if sqlt.get("requires_manual_review") else 60
    if t.properties.get("stored_procedure_cir"):
        score = min(score, 55)
    lkp = t.properties.get("lookup_cir") or {}
    if lkp.get("dynamic_lookup"):
        score = min(score, 55)
    if t.properties.get("normalizer_cir"):
        score = min(score, 88)
    if t.properties.get("was_update_strategy"):
        score = min(score, 80)
    override = t.properties.get("sql_override")
    if override:
        # a passthrough hides an entire unparsed query behind one node —
        # cap by how much SQL got dumped raw instead of decomposed, so a
        # harder query that defeated decomposition can't outscore a
        # simpler one the engine actually understood
        score = min(score, max(40, 80 - len(override) // 15))
    return score


def _critical_nodes(m: Mapping) -> Set[str]:
    """Names of nodes on a source->target path (ancestors of any TARGET)."""
    upstream: Dict[str, List[str]] = {}
    for l in m.links:
        upstream.setdefault(l.to_transformation, []).append(
            l.from_transformation)
    critical: Set[str] = set()
    stack = [t.name for t in m.by_type(TransformationType.TARGET)]
    while stack:
        n = stack.pop()
        if n in critical:
            continue
        critical.add(n)
        stack.extend(upstream.get(n, []))
    return critical


def score_mapping_confidence(m: Mapping) -> dict:
    nodes = [t for t in m.transformations if t.name != "__OUTPUT__"]
    critical = _critical_nodes(m)
    scored = []
    for t in nodes:
        s = node_confidence(t)
        on_path = t.name in critical or not critical
        scored.append((t, s, 1.0 if on_path else 0.3))

    if scored:
        total_w = sum(w for _, _, w in scored)
        weighted_mean = sum(s * w for _, s, w in scored) / total_w
        crit_scores = [s for t, s, w in scored if w == 1.0
                       and t.type not in (TransformationType.SOURCE,
                                          TransformationType.TARGET)]
        bottleneck = min(crit_scores) if crit_scores else 100
        damp = math.sqrt(min(1.0, bottleneck / weighted_mean)) \
            if weighted_mean else 1.0
        confidence = weighted_mean * damp
    else:
        confidence, bottleneck = 100.0, 100

    # conversion findings still count — but lighter than before, because
    # risky TYPES are already priced in
    manual = [i for i in m.issues if i.severity == IssueSeverity.MANUAL]
    errors = [i for i in m.issues if i.severity == IssueSeverity.ERROR]
    warnings = [i for i in m.issues if i.severity == IssueSeverity.WARNING]
    confidence -= len(manual) * 6 + len(errors) * 20 + len(warnings) * 2
    confidence = int(round(max(5, min(100, confidence))))

    critical_risks = [
        {"transformation": t.name, "type":
         str(t.properties.get("unconverted_pc_type") or t.type.value),
         "confidence": s,
         "reason": "low-confidence transformation on the critical path"}
        for t, s, w in sorted(scored, key=lambda x: x[1])
        if w == 1.0 and s < CRITICAL_THRESHOLD]
    return {
        "conversion_confidence": confidence,
        "bottleneck_confidence": bottleneck,
        "transformation_scores": {
            t.name: s for t, s, _ in scored
            if t.type not in (TransformationType.SOURCE,
                              TransformationType.TARGET)},
        "critical_risks": critical_risks,
        "manual_review_items": [
            {"code": i.code, "message": i.message[:200]} for i in manual],
    }


def score_pipeline_confidence(pipeline: Pipeline) -> dict:
    """Per-mapping module-31 contract + project rollup, layered on top of
    the complexity engine (mapping_complexity / automation stay there)."""
    from .complexity import score_mapping
    out = []
    for m in pipeline.mappings:
        cx = score_mapping(m)
        conf = score_mapping_confidence(m)
        out.append({
            "mapping": m.name,
            "mapping_complexity": cx.complexity_score,
            "complexity_level": cx.complexity_level,
            "conversion_confidence": conf["conversion_confidence"],
            "automation_percentage": cx.automation_percentage,
            "bottleneck_confidence": conf["bottleneck_confidence"],
            "transformation_scores": conf["transformation_scores"],
            "critical_risks": conf["critical_risks"],
            "manual_review_items": conf["manual_review_items"],
        })
    if out:
        avg = int(round(sum(o["conversion_confidence"] for o in out)
                        / len(out)))
    else:
        avg = 100
    return {"mappings": out, "average_confidence": avg,
            "critical_risks_total": sum(len(o["critical_risks"])
                                        for o in out)}

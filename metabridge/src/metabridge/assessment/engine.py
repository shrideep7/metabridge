"""Enterprise Migration Assessment Engine.

Analyzes uploaded projects WITHOUT converting them: parse-only through
the existing source parsers (all 18 formats), then aggregates the
platform's deterministic scoring engines (complexity, confidence,
governance classification, remediation effort) into a board-grade
assessment. NOTHING here calls an LLM — every figure derives from
repository metadata or an explicitly labelled planning assumption; AI
explanation is a separate, later step.

Sections produced (spec order):

    executive_summary        headline counts + the six key scores
    application_inventory    applications grouped from object origins
    data_estate_inventory    sources, tables, columns, connections
    object_inventory         per-object type/strategy/complexity/
                             confidence/status rows
    automation_potential     project + per-object automation figures
    migration_complexity     project + distribution by level
    technical_debt           deterministic debt markers from issues +
                             structure (overrides, opaque logic, single-
                             use sources, missing keys)
    manual_review_estimate   manual queue + effort hours (workbook
                             heuristic)
    resource_estimation      role mix derived from effort hours
    timeline_estimation      phased plan + weeks heuristic
    cost_estimation          labor cost at labelled blended rates
    cloud_cost_comparison    run-cost comparison across target families
                             at labelled assumptions
    business_impact          PII/governance exposure + dependency
                             criticality
    critical_dependencies    execution waves + fan-in hotspots
    unsupported_features     MANUAL/ERROR issues grouped by rule code
    migration_risks          risk matrix rows with severity + evidence
"""
from __future__ import annotations

import re
from typing import Dict, List

from ..engine import FORMAT_LABELS, detect_format, parse_input
from ..ir.model import IssueSeverity, Pipeline

# --- labelled planning assumptions (every derived cost/time references) ----
ASSUMPTIONS = {
    "engineer_hours_per_week": 30,          # productive migration hours
    "blended_rate_usd_per_hour": 95.0,
    "review_rate_usd_per_hour": 120.0,
    "objects_per_engineer_week_automated": 25,
    "run_cost_usd_per_object_month": {      # comparative run-rate figures
        "snowflake": 14.0, "databricks": 12.0, "bigquery": 11.0,
        "redshift": 13.0, "synapse": 13.5, "postgres": 6.0,
    },
    "legacy_run_cost_usd_per_object_month": 22.0,
    "note": "planning figures for comparison only — replace with "
            "negotiated rates and measured volumes before budgeting",
}


def _sev_counts(pipeline: Pipeline) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i in pipeline.all_issues():
        out[i.severity.value] = out.get(i.severity.value, 0) + 1
    return out


def assess(path: str, source_format: str = "") -> dict:
    """Parse-only assessment. Never converts, never calls an LLM."""
    fmt = source_format or detect_format(path)
    pipeline = parse_input(path, fmt)

    from ..report.complexity import score_pipeline
    from ..report.confidence import score_pipeline_confidence
    cx = score_pipeline(pipeline)
    assets = {a["name"]: a for a in cx.pop("assets", [])}
    conf = score_pipeline_confidence(pipeline)
    conf_by = {o["mapping"]: o for o in conf.get("mappings", [])}

    issues = pipeline.all_issues()
    manual_issues = [i for i in issues
                     if i.severity == IssueSeverity.MANUAL]
    error_issues = [i for i in issues
                    if i.severity == IssueSeverity.ERROR]

    # ---- object inventory --------------------------------------------------
    objects = []
    for m in pipeline.mappings:
        a = assets.get(m.name, {})
        c = conf_by.get(m.name, {})
        n_manual = sum(1 for i in m.issues
                       if i.severity == IssueSeverity.MANUAL)
        objects.append({
            "object": m.name,
            "kind": (m.origin.split(":")[0] if ":" in (m.origin or "")
                     else "mapping"),
            "load_strategy": m.load_strategy.value,
            "transformations": len([t for t in m.transformations
                                    if t.name != "__OUTPUT__"]),
            "depends_on": list(m.depends_on),
            "complexity_score": a.get("complexity_score", 0),
            "complexity_level": a.get("complexity_level", "LOW"),
            "automation_percentage": a.get("automation_percentage", 100),
            "conversion_confidence": c.get(
                "conversion_confidence",
                a.get("conversion_confidence", 100)),
            "manual_items": n_manual,
            "status": ("MANUAL_REVIEW" if n_manual else "AUTOMATED"),
        })

    # ---- application inventory (group by origin/prefix) --------------------
    apps: Dict[str, dict] = {}
    for o in objects:
        base = o["kind"] if o["kind"] != "mapping" else \
            re.split(r"[_.]", o["object"])[0]
        app = apps.setdefault(base, {"application": base, "objects": 0,
                                     "manual_items": 0,
                                     "complexity_scores": []})
        app["objects"] += 1
        app["manual_items"] += o["manual_items"]
        app["complexity_scores"].append(o["complexity_score"])
    application_inventory = [
        {"application": a["application"], "objects": a["objects"],
         "manual_items": a["manual_items"],
         "avg_complexity": round(sum(a["complexity_scores"])
                                 / len(a["complexity_scores"]), 1)}
        for a in sorted(apps.values(), key=lambda x: -x["objects"])]

    # ---- data estate --------------------------------------------------------
    data_estate = {
        "sources": len(pipeline.sources),
        "columns": sum(len(s.columns) for s in pipeline.sources),
        "tables": [{"table": s.name, "schema": s.schema,
                    "columns": len(s.columns)}
                   for s in pipeline.sources],
        "connections": pipeline.metadata.get("connections", []),
        "workflows": [d.get("workflow")
                      for d in pipeline.metadata.get("workflow_dags",
                                                     [])],
        "parameters": len(pipeline.metadata.get("parameters", [])),
    }

    # ---- technical debt ------------------------------------------------------
    debt = []
    overrides = sum(1 for m in pipeline.mappings
                    for t in m.transformations
                    if t.properties.get("sql_override"))
    if overrides:
        debt.append({"marker": "sql_overrides", "count": overrides,
                     "detail": "hand-written SQL bypassing the "
                               "transformation graph — opaque to "
                               "impact analysis"})
    opaque = sum(1 for m in pipeline.mappings
                 for t in m.transformations
                 if t.properties.get("unconverted_pc_type"))
    if opaque:
        debt.append({"marker": "opaque_logic", "count": opaque,
                     "detail": "script/custom transformations with no "
                               "declarative definition"})
    no_keys = [m.name for m in pipeline.mappings
               if m.load_strategy.value in ("MERGE", "DELETE_INSERT")
               and not m.unique_key]
    if no_keys:
        debt.append({"marker": "incremental_without_keys",
                     "count": len(no_keys),
                     "detail": "incremental loads with no declared "
                               "unique key: %s" % ", ".join(no_keys[:6])})
    used = {d for m in pipeline.mappings for d in m.depends_on} | \
        {str(t.properties.get("table", "")) for m in pipeline.mappings
         for t in m.transformations}
    unused_sources = [s.name for s in pipeline.sources
                      if s.name not in used]
    if unused_sources:
        debt.append({"marker": "unreferenced_sources",
                     "count": len(unused_sources),
                     "detail": ", ".join(unused_sources[:8])})
    by_code: Dict[str, int] = {}
    for i in issues:
        by_code[i.code] = by_code.get(i.code, 0) + 1
    debt_score = min(100, 4 * overrides + 5 * opaque
                     + 6 * len(no_keys) + 2 * len(unused_sources)
                     + 3 * len(manual_issues))

    # ---- effort / resources / timeline / cost -------------------------------
    a = ASSUMPTIONS
    n_objects = len(objects) or 1
    automated = [o for o in objects if o["status"] == "AUTOMATED"]
    auto_weeks = -(-len(automated)
                   // a["objects_per_engineer_week_automated"])
    manual_hours = sum(
        {"LOW": 4, "MEDIUM": 8, "HIGH": 16, "VERY_HIGH": 32,
         "MANUAL_REVIEW_REQUIRED": 32}.get(o["complexity_level"], 8)
        for o in objects if o["manual_items"]) + 4 * len(manual_issues)
    validation_hours = 2 * n_objects
    total_hours = (auto_weeks * a["engineer_hours_per_week"]
                   + manual_hours + validation_hours)
    engineers = max(1, min(6, -(-total_hours
                                // (a["engineer_hours_per_week"] * 8))))
    duration_weeks = max(2, -(-total_hours
                              // (engineers
                                  * a["engineer_hours_per_week"])))
    resource_estimation = {
        "engineers": engineers,
        "reviewers": max(1, engineers // 3),
        "effort_hours": {
            "automated_conversion": auto_weeks
            * a["engineer_hours_per_week"],
            "manual_porting": manual_hours,
            "validation_and_reconciliation": validation_hours,
            "total": total_hours},
        "basis": "%d automated objects at %d/engineer-week; manual "
                 "hours by complexity band; 2h validation per object"
                 % (len(automated),
                    a["objects_per_engineer_week_automated"]),
    }
    phases = [
        {"phase": "Assess & foundation", "weeks": 1,
         "detail": "target landing zones, connections, CI"},
        {"phase": "Automated conversion", "weeks": max(1, auto_weeks),
         "detail": "%d objects through the engine" % len(automated)},
        {"phase": "Manual porting",
         "weeks": max(1, -(-manual_hours
                           // a["engineer_hours_per_week"])),
         "detail": "%d manual item(s)" % len(manual_issues)},
        {"phase": "Validation & parallel run",
         "weeks": max(1, -(-validation_hours
                           // (engineers
                               * a["engineer_hours_per_week"]))),
         "detail": "reconciliation + sign-off"},
        {"phase": "Cutover & decommission", "weeks": 1,
         "detail": "traffic switch, legacy freeze"},
    ]
    timeline_estimation = {
        "phases": phases,
        "critical_path_weeks": int(duration_weeks) + 2,
        "elapsed_weeks_with_team": int(duration_weeks) + 2,
        "basis": "total effort / (engineers x %dh weeks) + fixed "
                 "foundation and cutover phases"
                 % a["engineer_hours_per_week"],
    }
    labor = round(total_hours * a["blended_rate_usd_per_hour"]
                  + validation_hours * 0.5
                  * a["review_rate_usd_per_hour"], 0)
    cost_estimation = {
        "labor_usd": labor,
        "breakdown": {
            "engineering": round(total_hours
                                 * a["blended_rate_usd_per_hour"], 0),
            "review_and_signoff": round(validation_hours * 0.5
                                        * a["review_rate_usd_per_hour"],
                                        0)},
        "assumptions": {k: a[k] for k in
                        ("blended_rate_usd_per_hour",
                         "review_rate_usd_per_hour", "note")},
    }
    legacy_run = round(n_objects
                       * a["legacy_run_cost_usd_per_object_month"], 0)
    cloud_cost_comparison = {
        "legacy_run_usd_per_month": legacy_run,
        "targets": {
            t: {"run_usd_per_month": round(n_objects * rate, 0),
                "annual_saving_vs_legacy_usd":
                    round(12 * (legacy_run - n_objects * rate), 0)}
            for t, rate in a["run_cost_usd_per_object_month"].items()},
        "basis": "per-object run-rate comparison at labelled planning "
                 "figures — volumes, credits and discounts change this "
                 "materially",
    }

    # ---- business impact -----------------------------------------------------
    pii = []
    try:
        from ..governance.engine import classify_pipeline
        for c in classify_pipeline(pipeline):
            d = c.to_dict() if hasattr(c, "to_dict") else dict(
                c.__dict__)
            if d.get("categories") or d.get("category"):
                pii.append(d)
    except Exception:  # noqa: BLE001 — governance optional per format
        pass
    fan_in: Dict[str, int] = {}
    for m in pipeline.mappings:
        for d in m.depends_on:
            fan_in[d] = fan_in.get(d, 0) + 1
    hotspots = sorted(fan_in.items(), key=lambda x: -x[1])[:8]
    business_impact = {
        "pii_findings": len(pii),
        "pii_objects": sorted({str(d.get("model",
                                         d.get("mapping", "")))
                               for d in pii})[:12],
        "critical_objects": [{"object": k, "downstream_consumers": v}
                             for k, v in hotspots if v > 1],
        "note": "objects with PII or high fan-in need business sign-off "
                "windows during cutover",
    }
    critical_dependencies = {
        "execution_order": pipeline.execution_order(),
        "fan_in_hotspots": [{"object": k, "dependents": v}
                            for k, v in hotspots],
        "workflows": data_estate["workflows"],
    }

    unsupported = {}
    for i in manual_issues + error_issues:
        u = unsupported.setdefault(i.code, {"code": i.code, "count": 0,
                                            "example": i.message,
                                            "severity":
                                                i.severity.value})
        u["count"] += 1
    unsupported_features = sorted(unsupported.values(),
                                  key=lambda x: -x["count"])

    risks = []
    if error_issues:
        risks.append({"risk": "Unparseable objects", "level": "HIGH",
                      "evidence": "%d ERROR issue(s)"
                                  % len(error_issues),
                      "mitigation": "re-export or exclude before "
                                    "conversion"})
    if manual_issues:
        risks.append({"risk": "Manual porting queue",
                      "level": "HIGH" if len(manual_issues) > 10
                      else "MEDIUM",
                      "evidence": "%d manual item(s) across %d code(s)"
                                  % (len(manual_issues),
                                     len(unsupported_features)),
                      "mitigation": "workbook-driven porting with "
                                    "per-item validation"})
    if debt_score >= 40:
        risks.append({"risk": "Technical debt", "level": "MEDIUM",
                      "evidence": "debt score %d/100" % debt_score,
                      "mitigation": "remediate overrides/keys during "
                                    "conversion, not after"})
    if business_impact["critical_objects"]:
        risks.append({"risk": "Shared dependency cutover",
                      "level": "MEDIUM",
                      "evidence": "%d object(s) feed multiple consumers"
                                  % len(business_impact[
                                      "critical_objects"]),
                      "mitigation": "cut over hotspot objects first "
                                    "with parallel-run reconciliation"})
    if pii:
        risks.append({"risk": "Regulated data in scope",
                      "level": "MEDIUM",
                      "evidence": "%d PII finding(s)" % len(pii),
                      "mitigation": "carry governance policies + "
                                    "masking before business data "
                                    "lands in the target"})
    if not risks:
        risks.append({"risk": "None identified beyond baseline",
                      "level": "LOW",
                      "evidence": "no errors, no manual queue",
                      "mitigation": "standard validation gates"})

    automation_pct = cx.get("automation_percentage", 0)
    _result = {
        "tool": "MetaBridge AI — Migration Assessment",
        "source_format": fmt,
        "source_label": FORMAT_LABELS.get(fmt, fmt),
        "project": pipeline.name,
        "executive_summary": {
            "headline": "%d object(s) across %d application group(s); "
                        "%s%% automation potential, complexity %s, "
                        "estimated %d engineer(s) x %d week(s), labor "
                        "~$%s."
                        % (n_objects, len(application_inventory),
                           automation_pct,
                           cx.get("complexity_level", "LOW"),
                           engineers,
                           timeline_estimation["elapsed_weeks_with_team"],
                           format(int(labor), ",")),
            "objects_total": n_objects,
            "automation_potential": automation_pct,
            "migration_complexity": cx.get("complexity_score", 0),
            "complexity_level": cx.get("complexity_level", "LOW"),
            "average_confidence": conf.get("average_confidence", 0),
            "manual_review_items": len(manual_issues),
            "technical_debt_score": debt_score,
            "estimated_weeks":
                timeline_estimation["elapsed_weeks_with_team"],
            "estimated_labor_usd": labor,
        },
        "application_inventory": application_inventory,
        "data_estate_inventory": data_estate,
        "object_inventory": objects,
        "automation_potential": {
            "project_percentage": automation_pct,
            "automated_objects": len(automated),
            "manual_objects": n_objects - len(automated),
            "by_object": {o["object"]: o["automation_percentage"]
                          for o in objects}},
        "migration_complexity": {
            "score": cx.get("complexity_score", 0),
            "level": cx.get("complexity_level", "LOW"),
            "distribution": _dist(objects, "complexity_level")},
        "technical_debt": {"score": debt_score, "markers": debt,
                           "issues_by_code": dict(sorted(
                               by_code.items(), key=lambda x: -x[1]))},
        "manual_review_estimate": {
            "items": len(manual_issues),
            "hours": manual_hours,
            "by_code": {u["code"]: u["count"]
                        for u in unsupported_features}},
        "resource_estimation": resource_estimation,
        "timeline_estimation": timeline_estimation,
        "cost_estimation": cost_estimation,
        "cloud_cost_comparison": cloud_cost_comparison,
        "business_impact": business_impact,
        "critical_dependencies": critical_dependencies,
        "unsupported_features": unsupported_features,
        "migration_risks": risks,
        "assumptions": ASSUMPTIONS,
        "determinism_note": "generated deterministically from "
                            "repository metadata — no conversion "
                            "performed, no AI in the numbers",
    }
    # Commercial usage emission (EB-504) — no-op unless usage reporting is on.
    # This deterministic engine is NEVER gated; we only record that it ran.
    from ..commercial import runtime as _commercial
    _project = (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    _commercial.report_assessment(objects=len(objects), project=_project)
    return _result


def _dist(objects: List[dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for o in objects:
        out[o[key]] = out.get(o[key], 0) + 1
    return out

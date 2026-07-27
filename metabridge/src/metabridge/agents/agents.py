"""The twelve MetaBridge agents.

Each agent wraps an existing, deterministic engine behind the uniform
``Agent`` contract and reports an evidence-based confidence. No agent
generates numbers; they read what the engines measure/model and score
their own confidence from measurable signals (source recognition, parse
health, semantic/conversion confidence, classification coverage, ...).
Consequential agents (Migration, Testing, Documentation) are ``GENERATE``
actions and are therefore always held for approval by the governance
gate.
"""
from __future__ import annotations

from typing import List

from .base import Agent, ActionClass, Status, TaskType
from .context import KEY_CIR, KEY_PIPELINES, KEY_TWIN


def _mean(xs: List[float]) -> float:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


# --- 1. Discovery ---------------------------------------------------------
class DiscoveryAgent(Agent):
    id = TaskType.DISCOVERY
    task_type = TaskType.DISCOVERY
    action_class = ActionClass.READ_ONLY
    depends_on = ()
    description = ("Builds the Digital Twin of the estate from the given "
                   "sources (multi-format discovery).")

    def _execute(self, ctx):
        from ..twin.discover import build_twin
        twin = build_twin(paths=ctx.paths, include_connections=False,
                          name=ctx.project)
        counts = twin.counts()
        nodes = sum(v for k, v in counts.items() if k != "edges")
        built = list(twin.built_from)
        recognized = [b for b in built if not b.endswith("(unrecognized)")]
        recog = (len(recognized) / len(built)) if built else 0.0
        return self._result(
            status=Status.OK,
            summary="Discovered %d node(s), %d edge(s) from %d source(s)"
                    % (nodes, counts.get("edges", 0), len(built)),
            evidence={"source_recognition": (recog, 2.0),
                      "graph_populated": (1.0 if nodes else 0.0, 1.0)},
            outputs={KEY_TWIN: twin,
                     "discovery": {"nodes": nodes,
                                   "edges": counts.get("edges", 0),
                                   "sources": built, "counts": counts}})


# --- 2. Parser ------------------------------------------------------------
class ParserAgent(Agent):
    id = TaskType.PARSE
    task_type = TaskType.PARSE
    action_class = ActionClass.READ_ONLY
    depends_on = ()
    description = ("Detects each source's format and parses it into the "
                   "canonical IR.")

    def _execute(self, ctx):
        from ..engine import parse_input, detect_format_detailed
        from ..ir.model import IssueSeverity
        pipelines = []
        det_confs = []
        warnings = []
        tx_total = 0
        errors = manual = 0
        for path in ctx.paths:
            try:
                det = detect_format_detailed(path)
                det_confs.append(float(getattr(det, "confidence_score", 0.0)
                                       or 0.0))
            except Exception:                    # noqa: BLE001
                det_confs.append(0.5)
            try:
                pipe = parse_input(path, source_format=ctx.source_format,
                                  dialect=ctx.dialect)
            except Exception as exc:             # noqa: BLE001
                warnings.append("parse failed for %s: %s" % (path, exc))
                continue
            pipelines.append(pipe)
            for m in pipe.mappings:
                tx_total += len(m.transformations)
            for iss in pipe.all_issues():
                if iss.severity == IssueSeverity.ERROR:
                    errors += 1
                elif iss.severity == IssueSeverity.MANUAL:
                    manual += 1
        # a run that parsed NOTHING has no parse health (0.0), not a
        # vacuous 1.0; and coverage reflects sources that failed to parse
        health = (1.0 - min(1.0, (errors + 0.5 * manual) / tx_total)
                  if tx_total else 0.0)
        coverage = (len(pipelines) / len(ctx.paths) if ctx.paths
                    else (1.0 if pipelines else 0.0))
        return self._result(
            status=Status.OK if pipelines else Status.FAILED,
            error="" if pipelines else "no source parsed",
            summary="Parsed %d/%d source(s): %d transformation(s), "
                    "%d error / %d manual issue(s)"
                    % (len(pipelines), len(ctx.paths) or len(pipelines),
                       tx_total, errors, manual),
            evidence={"detection": (_mean(det_confs) if det_confs else 0.5,
                                    1.0),
                      "parse_health": (health, 2.0),
                      "coverage": (coverage, 2.0)},
            warnings=warnings,
            outputs={KEY_PIPELINES: pipelines,
                     "parse": {"projects": len(pipelines),
                               "transformations": tx_total,
                               "errors": errors, "manual": manual}})


# --- 3. Metadata ----------------------------------------------------------
class MetadataAgent(Agent):
    id = TaskType.METADATA
    task_type = TaskType.METADATA
    action_class = ActionClass.READ_ONLY
    depends_on = (TaskType.DISCOVERY, TaskType.PARSE)
    description = ("Catalogs technologies, domains, ownership and column "
                   "metadata across the estate.")

    def _execute(self, ctx):
        from ..twin.analyze import technology_inventory
        twin = ctx.twin
        tech = technology_inventory(twin) if twin else {"technologies": []}
        techs = tech.get("technologies", [])
        total = sum(t.get("total", 0) for t in techs)
        tagged = sum(t.get("total", 0) for t in techs
                     if t.get("technology") != "(untagged)")
        tag_ratio = (tagged / total) if total else 0.0
        nodes = list(twin.nodes.values()) if twin else []
        with_domain = sum(1 for n in nodes if getattr(n, "domain", ""))
        with_owner = sum(1 for n in nodes if getattr(n, "owner", ""))
        domain_ratio = (with_domain / len(nodes)) if nodes else 0.0
        columns = 0
        for pipe in ctx.pipelines:
            for src in pipe.sources:
                columns += len(src.columns)
        return self._result(
            summary="%d technolog(y/ies); %.0f%% nodes tagged, "
                    "%.0f%% domained; %d column(s) catalogued"
                    % (len([t for t in techs
                            if t.get("technology") != "(untagged)"]),
                       tag_ratio * 100, domain_ratio * 100, columns),
            evidence={"technology_tagged": (tag_ratio, 2.0),
                      "domain_assigned": (domain_ratio, 1.0)},
            outputs={"metadata": {
                "technologies": techs, "nodes": total,
                "technology_tagged_pct": round(tag_ratio * 100, 1),
                "domain_pct": round(domain_ratio * 100, 1),
                "owner_pct": round((with_owner / len(nodes) * 100)
                                   if nodes else 0.0, 1),
                "columns_catalogued": columns}})


# --- 4. Semantic ----------------------------------------------------------
class SemanticAgent(Agent):
    id = TaskType.SEMANTIC
    task_type = TaskType.SEMANTIC
    action_class = ActionClass.ADVISORY
    depends_on = (TaskType.PARSE,)
    description = ("Enriches parsed IR into the semantic CIR (platform-"
                   "neutral intent).")

    def _execute(self, ctx):
        from ..cir.builder import build_cir
        projects = []
        confs = []
        tx = 0
        enriched_pipelines = 0
        for pipe in ctx.pipelines:
            proj = build_cir(pipe)
            projects.append(proj)
            s = proj.summary()
            n_pl = len(getattr(proj, "pipelines", []) or [])
            enriched_pipelines += n_pl
            # only a project that actually enriched >=1 pipeline contributes
            # its confidence — an empty project reports a sentinel 1.0 that
            # must not be mistaken for high-confidence enrichment
            if n_pl > 0:
                confs.append(float(s.get("avg_confidence", 0.0) or 0.0))
            tx += int(s.get("transformations", 0) or 0)
        avg = _mean(confs) or 0.0
        return self._result(
            summary="Enriched %d pipeline(s) across %d CIR project(s): "
                    "%d transformation(s), avg semantic confidence %.2f"
                    % (enriched_pipelines, len(projects), tx, avg),
            evidence={"semantic_confidence": (avg, 2.0),
                      "enriched": (1.0 if enriched_pipelines else 0.0, 1.0)},
            outputs={KEY_CIR: projects,
                     "semantic": {"projects": len(projects),
                                  "transformations": tx,
                                  "avg_confidence": round(avg, 3)}})


# --- 5. Validation --------------------------------------------------------
class ValidationAgent(Agent):
    id = TaskType.VALIDATION
    task_type = TaskType.VALIDATION
    action_class = ActionClass.READ_ONLY
    depends_on = (TaskType.PARSE, TaskType.SEMANTIC)
    description = ("Validates parse/semantic integrity and scores "
                   "conversion confidence per pipeline.")

    def _execute(self, ctx):
        from ..report.confidence import score_pipeline_confidence
        per = []
        confs = []
        cycles = 0
        manual_items = 0
        for pipe in ctx.pipelines:
            sc = score_pipeline_confidence(pipe)
            confs.append(float(sc.get("average_confidence", 0)) / 100.0)
            for m in sc.get("mappings", []):
                manual_items += len(m.get("manual_review_items", []))
            try:
                order = pipe.execution_order()
                placed = sum(len(w) for w in order)
                if placed < len(pipe.mappings):
                    cycles += 1                  # unplaceable => cyclic dep
            except Exception:                    # noqa: BLE001
                cycles += 1
            per.append({"pipeline": pipe.name,
                        "confidence": sc.get("average_confidence", 0)})
        avg = _mean(confs)
        return self._result(
            summary="Validated %d pipeline(s); avg conversion confidence "
                    "%.0f%%, %d manual item(s), %d cyclic pipeline(s)"
                    % (len(per), avg * 100, manual_items, cycles),
            evidence={"conversion_confidence": (avg, 2.0),
                      "acyclic": (1.0 if cycles == 0 else 0.0, 1.0)},
            outputs={"validation": {
                "pipelines": per, "avg_confidence": round(avg * 100),
                "manual_review_items": manual_items,
                "cyclic_pipelines": cycles}})


# --- 6. Governance --------------------------------------------------------
class GovernanceAgent(Agent):
    id = TaskType.GOVERNANCE
    task_type = TaskType.GOVERNANCE
    action_class = ActionClass.ADVISORY
    depends_on = (TaskType.PARSE,)
    description = ("Classifies PII/PHI/financial data and evaluates data-"
                   "residency & masking policy.")

    def _execute(self, ctx):
        from ..governance.engine import (classify_pipeline, evaluate_policy,
                                         load_policy)
        policy = load_policy()
        classified = special = violations = warnings_n = unknown = 0
        findings = []
        for pipe in ctx.pipelines:
            cls = classify_pipeline(pipe)
            classified += len(cls)
            for c in cls:
                cat = getattr(c, "category", "") or ""
                if cat.startswith("pii.special") or getattr(c, "hipaa", False):
                    special += 1
            for f in evaluate_policy(pipe, cls, policy,
                                     target_region=ctx.target_region):
                sev = getattr(f, "severity", "")
                code = getattr(f, "code", "")
                if sev == "VIOLATION":
                    violations += 1
                elif sev == "WARNING":
                    warnings_n += 1
                if code == "RESIDENCY_UNKNOWN":
                    unknown += 1
                if len(findings) < 100:
                    findings.append({
                        "severity": sev, "code": code,
                        "message": getattr(f, "message", ""),
                        "mapping": getattr(f, "mapping", ""),
                        "column": getattr(f, "column", ""),
                        "category": getattr(f, "category", "")})
        # residency confidence reflects CLASSIFICATION COMPLETENESS (how
        # many classified targets have a known region), not the mix of
        # findings — otherwise more violations would dilute the ratio and
        # spuriously RAISE confidence
        residency_known = (max(0.0, 1.0 - unknown / classified)
                           if classified else 1.0)
        return self._result(
            summary="Classified %d column(s); %d special-category; "
                    "%d violation(s), %d warning(s)"
                    % (classified, special, violations, warnings_n),
            evidence={"classification_ran":
                      (1.0 if ctx.pipelines else 0.0, 1.0),
                      "residency_known": (residency_known, 1.0)},
            sensitivity={"violations": violations,
                         "special_category": special,
                         "pii": classified > 0},
            outputs={"governance": {
                "classified_columns": classified,
                "special_category_columns": special,
                "violations": violations, "warnings": warnings_n,
                "findings": findings}})


# --- 7. Security ----------------------------------------------------------
class SecurityAgent(Agent):
    id = TaskType.SECURITY
    task_type = TaskType.SECURITY
    action_class = ActionClass.ADVISORY
    depends_on = (TaskType.DISCOVERY, TaskType.PARSE)
    description = ("Assesses security posture and compliance coverage "
                   "(GDPR/HIPAA/SOX/ISO/NIST).")

    def _execute(self, ctx):
        from ..security.engine import analyze_security
        twin = ctx.twin
        res = analyze_security(twin, pipelines=ctx.pipelines)
        sec = res.get("security_score", {}) or {}
        comp = res.get("compliance_score", {}) or {}
        nodes = len(twin.nodes) if twin else 0
        analyses = res.get("analyses", {}) or {}

        def _flagged(dim):
            d = analyses.get(dim, {}) or {}
            f = d.get("findings")
            return bool(f) if isinstance(f, list) else bool(d.get("count"))
        return self._result(
            summary="Security posture %s/%s; compliance %s"
                    % (sec.get("score", "?"), sec.get("band", "?"),
                       comp.get("score", "?")),
            evidence={"estate_observed": (1.0 if nodes else 0.0, 1.0),
                      "pipelines_analyzed":
                      (1.0 if ctx.pipelines else 0.0, 1.0)},
            sensitivity={"phi": _flagged("phi"), "pci": _flagged("pci"),
                         "pii": _flagged("pii")},
            outputs={"security": {"security_score": sec,
                                  "compliance_score": comp,
                                  "frameworks": res.get("frameworks", {})}})


# --- 8. Optimization ------------------------------------------------------
class OptimizationAgent(Agent):
    id = TaskType.OPTIMIZATION
    task_type = TaskType.OPTIMIZATION
    action_class = ActionClass.ADVISORY
    depends_on = (TaskType.DISCOVERY, TaskType.PARSE)
    description = ("Models cloud cost / FinOps optimization and migration "
                   "ROI (modeled, not billed).")

    def _execute(self, ctx):
        from ..finops.engine import analyze_finops
        twin = ctx.twin
        res = analyze_finops(twin, pipelines=ctx.pipelines)
        nodes = len(twin.nodes) if twin else 0
        cur = res.get("current_cost", {}) or {}
        fut = res.get("future_cost", {}) or {}
        roi = res.get("roi", {}) or {}
        pay = res.get("payback_period", {}) or {}
        # figures are MODELED from topology (data_basis) unless real
        # telemetry is fed — the honesty caveat is surfaced in the output,
        # not hidden as a confidence penalty (the analysis itself is
        # complete for what it observed)
        db = res.get("data_basis")
        measured = bool(isinstance(db, dict) and
                        db.get("measured_from_telemetry"))
        basis = "measured+modeled" if measured else "modeled"
        return self._result(
            summary="FinOps (%s): current $%s/mo, %s%% 3-yr ROI, "
                    "payback %s" % (basis, cur.get("monthly_usd", "?"),
                                    roi.get("three_year_pct", "?"),
                                    pay.get("text", "?")),
            evidence={"estate_observed": (1.0 if nodes else 0.0, 2.0),
                      "pipelines_analyzed":
                      (1.0 if ctx.pipelines else 0.0, 1.0)},
            outputs={"optimization": {
                "basis": basis,
                "current_monthly_usd": cur.get("monthly_usd"),
                "annual_savings_usd": fut.get("annual_savings_usd"),
                "three_year_roi_pct": roi.get("three_year_pct"),
                "payback": pay.get("text"),
                "migration_cost_usd": (res.get("migration_cost", {}) or {})
                .get("one_time_usd")}})


# --- 9. Migration (GENERATE — always gated) -------------------------------
class MigrationAgent(Agent):
    id = TaskType.MIGRATION
    task_type = TaskType.MIGRATION
    action_class = ActionClass.GENERATE
    depends_on = (TaskType.PARSE, TaskType.SEMANTIC, TaskType.VALIDATION,
                  TaskType.GOVERNANCE)
    description = ("Proposes the modernization migration (governed — "
                   "requires approval before generation).")

    def _execute(self, ctx):
        from ..report.confidence import score_pipeline_confidence
        confs = []
        autom = []
        manual_items = 0
        per = []
        for pipe in ctx.pipelines:
            sc = score_pipeline_confidence(pipe)
            confs.append(float(sc.get("average_confidence", 0)) / 100.0)
            maps = sc.get("mappings", [])
            for m in maps:
                autom.append(float(m.get("automation_percentage", 0)))
                manual_items += len(m.get("manual_review_items", []))
            per.append({"pipeline": pipe.name,
                        "confidence": sc.get("average_confidence", 0)})
        avg = _mean(confs)
        automation = _mean([a / 100.0 for a in autom]) if autom else 0.0
        return self._result(
            summary="Migration proposal: avg confidence %.0f%%, "
                    "%.0f%% automatable, %d manual item(s) — awaiting approval"
                    % (avg * 100, automation * 100, manual_items),
            evidence={"conversion_confidence": (avg, 2.0),
                      "automation": (automation, 1.0)},
            outputs={"migration": {
                "avg_confidence": round(avg * 100),
                "automation_pct": round(automation * 100),
                "manual_review_items": manual_items, "pipelines": per,
                "note": "proposal only — generation requires approval"}})


# --- 10. Testing (GENERATE — always gated) --------------------------------
class TestingAgent(Agent):
    id = TaskType.TESTING
    task_type = TaskType.TESTING
    action_class = ActionClass.GENERATE
    depends_on = (TaskType.SEMANTIC, TaskType.GOVERNANCE)
    description = ("Proposes data tests (unique/not-null from keys, masking "
                   "from PII) — requires approval.")

    def _execute(self, ctx):
        tests = []
        testable = 0
        pipelines_seen = 0
        for proj in ctx.cir:
            for pl in getattr(proj, "pipelines", []):
                pipelines_seen += 1
                uk = list(getattr(pl, "unique_key", []) or [])
                if uk:
                    testable += 1
                    tests.append({"pipeline": pl.name, "type": "unique",
                                  "columns": uk})
                    for col in uk:
                        tests.append({"pipeline": pl.name,
                                      "type": "not_null", "column": col})
        gov = ctx.memory.get("governance", {}) or {}
        for f in gov.get("findings", []):
            if f.get("category", "").startswith("pii") and f.get("column"):
                tests.append({"pipeline": f.get("mapping", ""),
                              "type": "masking", "column": f["column"],
                              "category": f["category"]})
        ratio = (testable / pipelines_seen) if pipelines_seen else 0.0
        return self._result(
            summary="Proposed %d test(s) across %d pipeline(s) — "
                    "awaiting approval" % (len(tests), pipelines_seen),
            evidence={"testable_coverage": (ratio, 2.0),
                      "has_cir": (1.0 if ctx.cir else 0.0, 1.0)},
            outputs={"tests": {"count": len(tests), "tests": tests[:200]}})


# --- 11. Documentation (GENERATE — always gated) --------------------------
class DocumentationAgent(Agent):
    id = TaskType.DOCUMENTATION
    task_type = TaskType.DOCUMENTATION
    action_class = ActionClass.GENERATE
    depends_on = (TaskType.DISCOVERY, TaskType.PARSE, TaskType.SEMANTIC)
    description = ("Generates the enterprise documentation set — requires "
                   "approval before publication.")

    def _execute(self, ctx):
        from ..docs.generate import build_context, generate_all
        dctx = build_context(pipelines=ctx.pipelines, twin=ctx.twin,
                            project=ctx.project)
        docs = generate_all(dctx)
        completeness = (0.5 if ctx.pipelines else 0.0) + \
            (0.5 if (ctx.twin and ctx.twin.nodes) else 0.0)
        return self._result(
            summary="Generated %d document(s) — awaiting approval to publish"
                    % len(docs),
            evidence={"source_completeness": (completeness, 2.0),
                      "docs_generated": (min(1.0, len(docs) / 14.0), 1.0)},
            outputs={"documents": {
                "count": len(docs),
                "documents": [{"slug": slug, "title": doc.title,
                               "blocks": len(doc.blocks)}
                              for slug, doc in docs.items()]}})


# --- 12. Executive Reporting ----------------------------------------------
class ExecutiveReportingAgent(Agent):
    id = TaskType.EXECUTIVE_REPORTING
    task_type = TaskType.EXECUTIVE_REPORTING
    action_class = ActionClass.ADVISORY
    depends_on = (TaskType.DISCOVERY, TaskType.PARSE, TaskType.METADATA,
                  TaskType.VALIDATION, TaskType.GOVERNANCE, TaskType.SECURITY,
                  TaskType.OPTIMIZATION)
    description = ("Synthesizes the run into an executive summary with a "
                   "governed recommendation.")

    def _execute(self, ctx):
        gov = ctx.memory.get("governance", {}) or {}
        sec = ctx.memory.get("security", {}) or {}
        opt = ctx.memory.get("optimization", {}) or {}
        val = ctx.memory.get("validation", {}) or {}
        # coverage/confidence over THIS agent's DECLARED dependencies only
        # (not every 'ok' event in the run — extra selected agents must not
        # inflate the executive's coverage)
        deps = set(self.depends_on)
        prior = [e for e in ctx.audit.events()
                 if e.get("agent_id") in deps and e.get("status") == "ok"]
        mean_conf = _mean([e.get("confidence", 0.0) for e in prior]) or 0.0
        committed = len(set(e.get("agent_id") for e in prior))
        expected = len(self.depends_on)
        coverage = committed / expected if expected else 0.0

        blockers = []
        if gov.get("violations"):
            blockers.append("%d data-policy violation(s)"
                            % gov["violations"])
        if val.get("cyclic_pipelines"):
            blockers.append("%d cyclic pipeline(s)"
                            % val["cyclic_pipelines"])
        if val.get("avg_confidence", 100) < 60:
            blockers.append("low conversion confidence (%d%%)"
                            % val.get("avg_confidence", 0))
        recommendation = ("remediate before migration" if blockers
                          else "cleared to plan migration")
        return self._result(
            summary="Executive summary: %d agent(s) contributed, overall "
                    "confidence %.0f%%, %d blocker(s) — %s"
                    % (committed, mean_conf * 100, len(blockers),
                       recommendation),
            evidence={"coverage": (coverage, 2.0),
                      "mean_upstream_confidence": (mean_conf, 1.0)},
            outputs={"executive_report": {
                "overall_confidence": round(mean_conf * 100),
                "agents_contributing": committed,
                "security_score": (sec.get("security_score", {}) or {})
                .get("score"),
                "conversion_confidence": val.get("avg_confidence"),
                "modeled_three_year_roi_pct": opt.get("three_year_roi_pct"),
                "policy_violations": gov.get("violations", 0),
                "blockers": blockers,
                "recommendation": recommendation}})


def default_agents() -> List[Agent]:
    return [DiscoveryAgent(), ParserAgent(), MetadataAgent(),
            SemanticAgent(), ValidationAgent(), GovernanceAgent(),
            SecurityAgent(), OptimizationAgent(), MigrationAgent(),
            TestingAgent(), DocumentationAgent(),
            ExecutiveReportingAgent()]

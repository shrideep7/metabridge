"""Technical Debt Intelligence.

Finds the debt a data estate accumulates — assets nobody consumes,
logic copied instead of shared, lineage that points at nothing — and
turns it into a costed, prioritized cleanup plan. It is deterministic
and evidence-based: reachability detections run over the Digital Twin
graph (the estate MetaBridge already discovered), and the finer
column / SQL / mapping detections run over the parsed IR. NOTHING here
calls an LLM; every cost / effort figure references an explicitly
labelled planning assumption.

Detected (spec order):

    unused_tables            table with no path to any consumer
    unused_columns           source column never referenced downstream
    dead_etl                 pipeline whose output nothing consumes
    duplicate_mappings       structurally identical transformation graphs
    duplicate_sql            byte-identical (normalized) SQL blocks
    duplicate_business_logic same derivation expression in many places
    unused_dashboards        dashboard with no data lineage
    broken_lineage           reference to an undefined upstream object
    orphan_datasets          asset with no edges at all
    unused_apis              API that reads nothing and serves no one
    unused_kafka_topics      topic produced to but never consumed
    unused_process_chains    workflow that orchestrates nothing live

Generated: technical debt score, engineering cleanup plan, cloud cost
savings, estimated refactoring effort, prioritized remediation roadmap.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..twin.analyze import _flow_adj, _bfs
from ..twin.model import DigitalTwin, twin_from_dict

# consumption sinks — an asset that can reach one of these is "used"
ENDPOINT_KINDS = {"dashboard", "api", "data_product", "consumer",
                  "application"}
_MART_RE = re.compile(r"^(fct_|fact_|dim_|mart_|rpt_|agg_)", re.I)
_DLQ_RE = re.compile(r"dlq|dead[._-]?letter|\.error$|_error$", re.I)

# --- labelled planning assumptions -----------------------------------------
DEBT_ASSUMPTIONS = {
    "storage_usd_per_table_month": 9.0,
    "compute_usd_per_pipeline_month": 22.0,
    "streaming_usd_per_topic_month": 30.0,
    "dashboard_license_usd_month": 12.0,
    "api_hosting_usd_month": 18.0,
    "engineer_hours_per_week": 30,
    "blended_rate_usd_per_hour": 95.0,
    "effort_hours": {                       # to remediate one item
        "unused_table": 1.0, "unused_column": 0.4, "dead_etl": 3.0,
        "duplicate_mapping": 6.0, "duplicate_sql": 4.0,
        "duplicate_business_logic": 4.0, "unused_dashboard": 2.0,
        "broken_lineage": 5.0, "orphan_dataset": 1.0, "unused_api": 2.0,
        "unused_kafka_topic": 1.5, "unused_process_chain": 4.0,
    },
    "note": "planning figures only — replace with measured storage, "
            "compute credits and negotiated rates before budgeting; "
            "and confirm 'unused' against runtime access logs before "
            "deleting (the twin is topology, not telemetry)",
}

# points each category adds to the (capped) debt index
_SCORE_WEIGHTS = {
    "broken_lineage": 6, "duplicate_mappings": 5, "duplicate_sql": 4,
    "duplicate_business_logic": 4, "dead_etl": 3, "orphan_datasets": 3,
    "unused_process_chains": 3, "unused_tables": 2,
    "unused_kafka_topics": 2, "unused_dashboards": 2, "unused_apis": 2,
    "unused_columns": 1,
}
_MONTHLY_COST = {
    "unused_tables": "storage_usd_per_table_month",
    "dead_etl": "compute_usd_per_pipeline_month",
    "duplicate_mappings": "compute_usd_per_pipeline_month",
    "unused_kafka_topics": "streaming_usd_per_topic_month",
    "unused_dashboards": "dashboard_license_usd_month",
    "unused_apis": "api_hosting_usd_month",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _band(score: int) -> str:
    return ("Severe" if score >= 65 else "High" if score >= 40
            else "Moderate" if score >= 20 else "Low")


# ---------------------------------------------------------------------------
# reachability detections (over the twin graph)
# ---------------------------------------------------------------------------

def _used_set(twin: DigitalTwin):
    """Every node that can reach a consumption endpoint via data flow,
    plus the endpoints themselves. Returns (used_set, endpoints_exist).
    When no endpoints are declared, returns (None, False) so callers
    fall back to the weaker 'no downstream consumer' signal."""
    endpoints = [n.id for n in twin.nodes.values()
                 if n.kind in ENDPOINT_KINDS]
    if not endpoints:
        return None, False
    radj = _flow_adj(twin, reverse=True)     # consumer -> producer
    # a data product consumes its members via 'includes' (product ->
    # table), which is not a data-flow edge — seed those tables (and
    # their upstreams) as used so a curated table consumed ONLY through
    # its product isn't falsely flagged unused
    seeds = set(endpoints)
    for n in twin.nodes.values():
        if n.kind == "data_product":
            for e in twin.out_edges(n.id):
                if e.kind == "includes":
                    seeds.add(e.to_id)
    used = set(seeds)
    for s in seeds:
        used |= set(_bfs(radj, s).keys())
    return used, True


def _reachability_debt(twin: DigitalTwin) -> Dict[str, list]:
    fwd = _flow_adj(twin)
    used, endpoints_exist = _used_set(twin)

    def out_deg(nid):
        return len(fwd.get(nid, []))

    def is_used(n):
        if used is not None:
            return n.id in used
        # weak mode: "used" == something downstream consumes it
        return out_deg(n.id) > 0

    unused_tables, unused_topics, unused_dash, unused_apis = [], [], [], []
    orphans, broken, dead_etl, unused_chains = [], [], [], []

    for n in twin.nodes.values():
        deg = len(twin.out_edges(n.id)) + len(twin.in_edges(n.id))
        # orphans: no edges at all (data assets only)
        if deg == 0 and n.kind in ("table", "topic", "data_product"):
            orphans.append(_ev(n, "no lineage in or out — disconnected "
                                 "dataset"))
            continue
        if n.kind == "table":
            if _MART_RE.search(n.name) or n.metadata.get("is_product"):
                continue                     # intended output, not debt
            if not is_used(n):
                unused_tables.append(_ev(
                    n, "no path to any consumer" if endpoints_exist
                    else "nothing downstream reads it"))
        elif n.kind == "topic":
            consumers = [e for e in twin.out_edges(n.id)
                         if e.kind in ("consumes", "feeds")]
            if not consumers and not _DLQ_RE.search(n.name):
                unused_topics.append(_ev(n, "produced to but no consumer "
                                            "or downstream job reads it"))
        elif n.kind == "dashboard":
            if not any(e.kind == "feeds" for e in twin.in_edges(n.id)):
                unused_dash.append(_ev(n, "no data source in lineage — "
                                          "broken or abandoned dashboard"))
        elif n.kind == "api":
            reads = any(e.kind == "feeds" for e in twin.in_edges(n.id))
            serves = any(e.kind == "serves" for e in twin.out_edges(n.id))
            if not reads and not serves:
                unused_apis.append(_ev(n, "reads no data and serves no "
                                          "consumer"))
        elif n.kind in ("pipeline", "streaming_job"):
            outs = [twin.nodes[e.to_id] for e in twin.out_edges(n.id)
                    if e.kind in ("writes", "feeds")]
            if outs and not any(is_used(o) for o in outs):
                dead_etl.append(_ev(n, "writes only outputs that nothing "
                                       "consumes"))
        elif n.kind == "workflow":
            orch = [e for e in twin.out_edges(n.id)
                    if e.kind == "orchestrates"]
            if not orch:
                unused_chains.append(_ev(n, "orchestrates nothing"))
            elif all(twin.nodes[e.to_id].inferred for e in orch):
                unused_chains.append(_ev(n, "orchestrates only undefined "
                                            "targets"))

    # broken lineage: placeholder (inferred) assets that something
    # references — an edge whose endpoint was never actually defined
    for n in twin.nodes.values():
        if n.inferred and n.kind in ("table", "pipeline") and \
                (twin.out_edges(n.id) or twin.in_edges(n.id)):
            refs = sorted({twin.nodes[e.from_id].name
                           for e in twin.in_edges(n.id)} |
                          {twin.nodes[e.to_id].name
                           for e in twin.out_edges(n.id)})
            broken.append(_ev(n, "referenced by %s but never defined "
                                 "as a real object"
                                 % ", ".join(refs[:4])))
    return {
        "unused_tables": unused_tables, "unused_kafka_topics": unused_topics,
        "unused_dashboards": unused_dash, "unused_apis": unused_apis,
        "orphan_datasets": orphans, "broken_lineage": broken,
        "dead_etl": dead_etl, "unused_process_chains": unused_chains,
        "_endpoints_declared": endpoints_exist,
    }


def _ev(node, why: str) -> dict:
    d = {"object": node.name, "kind": node.kind, "reason": why}
    if node.technology:
        d["technology"] = node.technology
    if node.domain:
        d["domain"] = node.domain
    return d


# ---------------------------------------------------------------------------
# IR detections (over parsed pipelines)
# ---------------------------------------------------------------------------

def _ir_debt(pipelines: List) -> Dict[str, list]:
    from ..ir.model import TransformationType
    unused_columns, dup_maps, dup_sql, dup_logic = [], [], [], []

    for p in pipelines:
        # referenced identifiers across every expression / condition /
        # override / port name in the project
        referenced = set()
        for m in p.mappings:
            for t in m.transformations:
                for key in ("sql_override", "condition"):
                    v = t.properties.get(key)
                    if v:
                        referenced |= _idents(str(v))
                for port in t.ports:
                    if port.expression:
                        referenced |= _idents(port.expression)
                    if t.type not in (TransformationType.SOURCE,):
                        referenced.add(port.name.lower())
        for s in p.sources:
            src_used = {c.name.lower() for c in s.columns} & referenced
            for c in s.columns:
                if c.name.lower() not in referenced:
                    unused_columns.append({
                        "object": "%s.%s" % (s.name, c.name),
                        "kind": "column", "table": s.name,
                        "reason": "source column never referenced by any "
                                  "transformation or SQL"})
            _ = src_used

    # duplicate detection across ALL pipelines
    map_fp: Dict[str, list] = {}
    sql_fp: Dict[str, list] = {}
    logic_fp: Dict[str, list] = {}
    for p in pipelines:
        for m in p.mappings:
            fp = _mapping_fingerprint(m)
            if fp:
                map_fp.setdefault(fp, []).append(m.name)
            for t in m.transformations:
                ov = t.properties.get("sql_override")
                if ov and len(_norm(ov)) >= 40:
                    sql_fp.setdefault(_norm(ov), []).append(m.name)
                for port in t.ports:
                    e = port.expression
                    if e and _is_logic(e):
                        logic_fp.setdefault(_norm(e), []).append(
                            "%s.%s" % (m.name, port.name))

    for fp, names in map_fp.items():
        if len(names) > 1:
            dup_maps.append({"object": ", ".join(sorted(names)),
                             "kind": "mapping", "count": len(names),
                             "reason": "structurally identical "
                                       "transformation graph"})
    for sql, names in sql_fp.items():
        uniq = sorted(set(names))
        if len(uniq) > 1:
            dup_sql.append({"object": ", ".join(uniq), "kind": "sql",
                            "count": len(uniq),
                            "reason": "byte-identical SQL in %d mappings"
                                      % len(uniq),
                            "snippet": sql[:120]})
    for expr, places in logic_fp.items():
        uniq = sorted(set(places))
        distinct_maps = {pl.split(".")[0] for pl in uniq}
        if len(distinct_maps) > 1:
            dup_logic.append({"object": ", ".join(uniq[:8]),
                              "kind": "expression",
                              "count": len(uniq),
                              "reason": "same derivation in %d places — "
                                        "extract a shared model/macro"
                                        % len(uniq),
                              "snippet": expr[:120]})
    return {"unused_columns": unused_columns, "duplicate_mappings": dup_maps,
            "duplicate_sql": dup_sql, "duplicate_business_logic": dup_logic}


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _idents(text: str) -> set:
    """Every identifier token in a piece of SQL/expression text. We do
    NOT filter SQL keywords here: this set is only used to decide
    whether a source COLUMN is referenced, and many real columns are
    named `order`, `count`, `end`, `min`, ... — filtering keywords
    would drop them and falsely flag a used column as unused (advising
    its deletion). Keeping keywords can only under-report unused
    columns, which is the safe direction."""
    return {t.lower() for t in _IDENT_RE.findall(text or "")}


def _is_logic(expr: str) -> bool:
    """Non-trivial derivation worth de-duplicating — not a bare
    pass-through column or a lone literal."""
    e = expr.strip()
    if len(e) < 12:
        return False
    return bool(re.search(r"case|when|\|\||concat|\+|-|\*|/|coalesce|"
                          r"nullif|substr|upper|lower|trim|cast|round|"
                          r"\bthen\b", e, re.I))


def _mapping_fingerprint(m) -> str:
    """A per-transformation signature that captures the semantics which
    make two same-typed transformations different — a JOINER's join
    type, a FILTER's condition, an AGGREGATOR's grouping — so an INNER
    vs FULL join, or a year=2023 vs year=2024 partition load, do NOT
    collide and get advised to 'consolidate into one' (which would drop
    rows / change results)."""
    from ..ir.model import TransformationType
    srcs, tgts, sigs, has_expr = [], [], [], False
    for t in m.transformations:
        if t.name == "__OUTPUT__":
            continue
        tbl = str(t.properties.get("table", ""))
        if t.type == TransformationType.SOURCE and tbl:
            srcs.append(tbl.lower())
        elif t.type == TransformationType.TARGET and tbl:
            tgts.append(tbl.lower())
        parts = [t.type.value]
        for key in ("condition", "join_type", "group_by", "sort_keys",
                    "strategy", "sql_override"):
            v = t.properties.get(key)
            if v not in (None, "", [], {}):
                parts.append("%s=%s" % (key, _norm(str(v))))
        exprs = sorted(_norm(p.expression) for p in t.ports
                       if p.expression)
        if exprs:
            has_expr = True
            parts.append("x=" + ",".join(exprs))
        sigs.append("~".join(parts))
    if len(sigs) < 2 and not has_expr:
        return ""                            # too thin to fingerprint
    return "|".join(["S:" + ",".join(sorted(srcs)),
                     "T:" + ",".join(sorted(tgts)),
                     "G:" + ";".join(sorted(sigs))])


# ---------------------------------------------------------------------------
# scoring + generated artifacts
# ---------------------------------------------------------------------------

_CAT_LABELS = {
    "unused_tables": "Unused tables", "unused_columns": "Unused columns",
    "dead_etl": "Dead ETL", "duplicate_mappings": "Duplicate mappings",
    "duplicate_sql": "Duplicate SQL",
    "duplicate_business_logic": "Duplicate business logic",
    "unused_dashboards": "Unused dashboards",
    "broken_lineage": "Broken lineage",
    "orphan_datasets": "Orphan datasets", "unused_apis": "Unused APIs",
    "unused_kafka_topics": "Unused Kafka topics",
    "unused_process_chains": "Unused process chains",
}


def assess_technical_debt(twin, pipelines: Optional[list] = None) -> dict:
    """Deterministic, evidence-based technical-debt analysis.

    twin      a DigitalTwin or its to_dict() form (estate reachability)
    pipelines parsed IR Pipelines (column / SQL / mapping detail)
    """
    if isinstance(twin, dict):
        twin = twin_from_dict(twin)
    pipelines = pipelines or []

    findings = _reachability_debt(twin)
    endpoints_declared = findings.pop("_endpoints_declared")
    findings.update(_ir_debt(pipelines))

    counts = {k: len(findings.get(k, [])) for k in _CAT_LABELS}
    score = min(100, sum(_SCORE_WEIGHTS[k] * counts[k] for k in counts))
    # the twin already carries tables/pipelines/etc. as nodes; the IR
    # only adds column-level granularity — so count nodes once and add
    # columns (never double-count a table that is both a twin node and
    # a parsed source)
    total_columns = sum(len(s.columns) for p in pipelines
                        for s in p.sources)
    total_objects = len(twin.nodes) + total_columns
    debt_objects = sum(counts.values())
    debt_ratio = round(debt_objects / total_objects, 3) \
        if total_objects else 0.0

    savings = _cost_savings(counts)
    effort = _effort(counts)
    roadmap = _roadmap(findings, counts, savings)
    plan = _cleanup_plan(findings, counts)

    top = sorted(((k, counts[k]) for k in counts if counts[k]),
                 key=lambda x: -_SCORE_WEIGHTS[x[0]] * x[1])[:5]
    return {
        "tool": "MetaBridge AI — Technical Debt Intelligence",
        "estate": twin.name,
        "technical_debt_score": {
            "score": score, "band": _band(score),
            "debt_objects": debt_objects,
            "total_objects": total_objects, "debt_ratio": debt_ratio,
            "headline": "%d/100 debt pressure (%s): %d debt object(s) "
                        "across %d category/ies of %d estate object(s)."
                        % (score, _band(score), debt_objects,
                           len([k for k in counts if counts[k]]),
                           total_objects),
            "by_category": {_CAT_LABELS[k]: counts[k] for k in counts},
            "top_categories": [{"category": _CAT_LABELS[k], "count": c}
                               for k, c in top],
            "index_note": "weighted, capped index (not a percentage) — "
                          "see debt_ratio for the raw proportion",
        },
        "findings": {k: findings.get(k, []) for k in _CAT_LABELS},
        "engineering_cleanup_plan": plan,
        "cloud_cost_savings": savings,
        "estimated_refactoring_effort": effort,
        "prioritized_remediation_roadmap": roadmap,
        "coverage_note": (
            "reachability detections use the Digital Twin graph; column/"
            "SQL/mapping detections use the parsed IR"
            + ("" if endpoints_declared else
               " — NO consumption endpoints (dashboards/APIs/products) "
               "are declared, so 'unused' falls back to 'nothing "
               "downstream reads it' (lower confidence; add an "
               "estate.yml to sharpen this)")),
        "determinism_note": "generated deterministically from repository "
                            "metadata and the estate graph — confirm "
                            "against runtime access logs before deleting",
        "assumptions": DEBT_ASSUMPTIONS,
    }


def _cost_savings(counts: Dict[str, int]) -> dict:
    a = DEBT_ASSUMPTIONS
    lines = {}
    monthly = 0.0
    for cat, key in _MONTHLY_COST.items():
        if counts[cat]:
            m = counts[cat] * a[key]
            lines[_CAT_LABELS[cat]] = round(m, 0)
            monthly += m
    return {
        "monthly_usd": round(monthly, 0),
        "annual_usd": round(monthly * 12, 0),
        "by_category_monthly_usd": lines,
        "basis": "decommissioning unused storage/compute/streaming/"
                 "licenses at labelled planning rates; consolidating "
                 "duplicate pipelines recovers their run cost",
        "assumptions": {k: a[k] for k in (
            "storage_usd_per_table_month", "compute_usd_per_pipeline_month",
            "streaming_usd_per_topic_month", "dashboard_license_usd_month",
            "api_hosting_usd_month", "note")},
    }


def _effort(counts: Dict[str, int]) -> dict:
    a = DEBT_ASSUMPTIONS
    eh = a["effort_hours"]
    per = {"unused_tables": "unused_table", "unused_columns": "unused_column",
           "dead_etl": "dead_etl", "duplicate_mappings": "duplicate_mapping",
           "duplicate_sql": "duplicate_sql",
           "duplicate_business_logic": "duplicate_business_logic",
           "unused_dashboards": "unused_dashboard",
           "broken_lineage": "broken_lineage",
           "orphan_datasets": "orphan_dataset", "unused_apis": "unused_api",
           "unused_kafka_topics": "unused_kafka_topic",
           "unused_process_chains": "unused_process_chain"}
    by_cat, total = {}, 0.0
    for cat, hkey in per.items():
        if counts[cat]:
            h = counts[cat] * eh[hkey]
            by_cat[_CAT_LABELS[cat]] = round(h, 1)
            total += h
    total_hours = round(total, 1)
    weeks = round(total_hours / a["engineer_hours_per_week"], 1)
    return {
        "total_hours": total_hours,
        "engineer_weeks": weeks,
        # derive labor from the DISPLAYED hours so the two headline
        # figures reconcile for a reader recomputing hours x rate
        "labor_usd": round(total_hours * a["blended_rate_usd_per_hour"], 0),
        "by_category_hours": by_cat,
        "basis": "per-item remediation hours by category x count, at a "
                 "%d-hour productive week and $%d/hr blended"
                 % (a["engineer_hours_per_week"],
                    a["blended_rate_usd_per_hour"]),
    }


def _roadmap(findings: dict, counts: Dict[str, int],
             savings: dict) -> dict:
    a = DEBT_ASSUMPTIONS
    eh = a["effort_hours"]

    def phase(title, cats, risk, note):
        items, hours, monthly = [], 0.0, 0.0
        hkey = {"unused_tables": "unused_table", "orphan_datasets":
                "orphan_dataset", "unused_kafka_topics": "unused_kafka_topic",
                "unused_dashboards": "unused_dashboard", "dead_etl":
                "dead_etl", "unused_apis": "unused_api",
                "duplicate_mappings": "duplicate_mapping", "duplicate_sql":
                "duplicate_sql", "duplicate_business_logic":
                "duplicate_business_logic", "broken_lineage":
                "broken_lineage", "unused_columns": "unused_column",
                "unused_process_chains": "unused_process_chain"}
        for c in cats:
            if counts.get(c):
                items.append({"category": _CAT_LABELS[c], "count": counts[c]})
                hours += counts[c] * eh[hkey[c]]
                if c in _MONTHLY_COST:
                    monthly += counts[c] * a[_MONTHLY_COST[c]]
        return {"phase": title, "risk": risk, "note": note,
                "items": items, "effort_hours": round(hours, 1),
                "monthly_savings_usd": round(monthly, 0)}

    phases = [
        phase("1 — Quick wins (delete-safe)",
              ["orphan_datasets", "unused_kafka_topics",
               "unused_dashboards", "unused_apis", "dead_etl",
               "unused_tables"], "LOW",
              "disconnected or unconsumed assets — remove after a "
              "confirming access-log check"),
        phase("2 — Consolidation",
              ["duplicate_mappings", "duplicate_sql",
               "duplicate_business_logic"], "MEDIUM",
              "collapse copied logic into shared models/macros; "
              "regression-test each merge"),
        phase("3 — Structural",
              ["broken_lineage", "unused_columns",
               "unused_process_chains"], "MEDIUM",
              "schema/orchestration changes — fix references, prune "
              "columns, retire dead chains with validation"),
    ]
    phases = [p for p in phases if p["items"]]
    # order quick-win items by savings-per-hour is implicit in phasing;
    # phases themselves are already risk-ordered
    return {"phases": phases,
            "sequencing_note": "phase 1 is safe, high-yield decommissioning; "
                               "phase 2 removes drift; phase 3 needs schema/"
                               "orchestration validation. Always confirm "
                               "'unused' against runtime access logs first.",
            "total_effort_hours": round(
                sum(p["effort_hours"] for p in phases), 1)}


def _cleanup_plan(findings: dict, counts: Dict[str, int]) -> list:
    a = DEBT_ASSUMPTIONS
    eh = a["effort_hours"]
    actions = {
        "orphan_datasets": ("Delete disconnected datasets", "orphan_dataset"),
        "unused_tables": ("Drop unused tables", "unused_table"),
        "unused_kafka_topics": ("Decommission unconsumed topics",
                                "unused_kafka_topic"),
        "unused_dashboards": ("Retire dashboards with no data source",
                              "unused_dashboard"),
        "unused_apis": ("Retire unused APIs", "unused_api"),
        "dead_etl": ("Remove pipelines that feed nothing", "dead_etl"),
        "duplicate_mappings": ("Consolidate duplicate mappings into one",
                               "duplicate_mapping"),
        "duplicate_sql": ("De-duplicate identical SQL into a shared model",
                          "duplicate_sql"),
        "duplicate_business_logic": ("Extract repeated logic into a macro/"
                                     "shared model", "duplicate_business_logic"),
        "broken_lineage": ("Fix references to undefined upstreams",
                           "broken_lineage"),
        "unused_columns": ("Prune never-referenced columns", "unused_column"),
        "unused_process_chains": ("Retire process chains that orchestrate "
                                  "nothing", "unused_process_chain"),
    }
    plan = []
    for cat, (action, hkey) in actions.items():
        n = counts.get(cat, 0)
        if not n:
            continue
        examples = [f["object"] for f in findings.get(cat, [])][:8]
        plan.append({
            "action": action, "category": _CAT_LABELS[cat], "count": n,
            "estimated_hours": round(n * eh[hkey], 1),
            "examples": examples,
        })
    plan.sort(key=lambda x: -x["count"])
    return plan


# ---------------------------------------------------------------------------
# convenience: build the twin + parse IR from paths
# ---------------------------------------------------------------------------

def _parse_pipelines(paths: List[str]) -> list:
    """Parse only genuine pipeline projects into IR. Event and
    orchestration folders are handled by the twin graph, not the IR —
    routing here MUST match build_twin's so a broker/DAG folder is never
    force-parsed as a pipeline (which would fabricate bogus IR debt)."""
    from ..engine import detect_format, parse_input
    from ..ir.model import Pipeline
    out = []
    for path in paths or []:
        try:
            from ..events.parsers import detect_event_platform
            if detect_event_platform(path)["detected_platform"]:
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            from ..orchestration.parsers import \
                detect_orchestration_platform
            det = detect_orchestration_platform(path)
            if det["detected_platform"] and det["detected_platform"] \
                    not in ("powercenter", "ssis", "datastage",
                            "talend", "cron"):
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            fmt = detect_format(path)
            p = parse_input(path, fmt)
            if isinstance(p, Pipeline):
                out.append(p)
        except Exception:  # noqa: BLE001 — non-pipeline paths -> twin only
            pass
    return out


def assess_from_paths(paths: Optional[List[str]] = None,
                      estate_docs: Optional[List[dict]] = None,
                      include_connections: bool = False,
                      jobs_dir: Optional[str] = None) -> dict:
    from ..twin.discover import build_twin
    twin = build_twin(paths=paths, estate_docs=estate_docs,
                      include_connections=include_connections,
                      jobs_dir=jobs_dir)
    return assess_technical_debt(twin, _parse_pipelines(paths))

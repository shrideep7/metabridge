"""Professional Migration Report — the document an SI hands the client.

Fifteen sections, every number traceable to an engine:

    1  Executive Summary          headline metrics (honest workload rate)
    2  Source Platform            what we read
    3  Target Platform            what we emitted
    4  Assets Analysed            full census
    5  Assets Converted           status breakdown
    6  Automation Percentage      object rate AND workload coverage
    7  Migration Complexity       module 9
    8  Conversion Confidence      module 9
    9  Unsupported Components     declared gaps with workarounds
    10 Manual Review Items        the queue + effort estimate
    11 Data Type Risks            module 6 rendering warnings
    12 Function Conversion Risks  module 5 coverage + recorded gaps
    13 Lineage Summary            module 11
    14 Validation Results         modules 13 + 14
    15 Recommended Actions        prioritized, evidence-derived

Output: migration_report.json (machine), migration_report.md (docs),
migration_report.html (client-facing, self-contained).
"""
from __future__ import annotations

import datetime
import html as _html
import json
from pathlib import Path
from typing import Dict, List, Optional

from ..ir.model import IssueSeverity, LoadStrategy, Pipeline, TransformationType
from ..parsers.sql_parser import SQL_DIALECT_FORMATS

SECTIONS = (
    "executive_summary", "source_platform", "target_platform",
    "assets_analysed", "assets_converted", "automation_percentage",
    "migration_complexity", "conversion_confidence",
    "unsupported_components", "manual_review_items", "data_type_risks",
    "function_conversion_risks", "lineage_summary", "validation_results",
    "recommended_actions",
)

_DISPLAY = {
    "powercenter": "Informatica PowerCenter",
    "idmc": "Informatica IDMC (Cloud Data Integration)",
    "dbt": "dbt",
    "snowflake": "Snowflake", "databricks": "Databricks",
    "bigquery": "Google BigQuery", "redshift": "Amazon Redshift",
    "synapse": "Azure Synapse", "sqlserver": "Microsoft SQL Server",
    "oracle": "Oracle", "postgres": "PostgreSQL", "teradata": "Teradata",
    "sql": "ANSI SQL",
}


def _display(fmt: str, dialect: str = "") -> str:
    base = _DISPLAY.get((fmt or "").lower(), fmt or "unknown")
    if fmt == "dbt" and dialect:
        return "%s + %s" % (base, _DISPLAY.get(dialect.lower(), dialect))
    return base


def _n(x) -> str:
    return "{:,}".format(int(x))


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# section builders                                                             #
# --------------------------------------------------------------------------- #

def _census(pipeline: Pipeline) -> dict:
    tx: Dict[str, int] = {}
    expressions = 0
    for m in pipeline.mappings:
        for t in m.transformations:
            if t.name == "__OUTPUT__":
                continue
            tx[t.type.value] = tx.get(t.type.value, 0) + 1
            expressions += sum(1 for p in t.ports if p.expression)
    strategies: Dict[str, int] = {}
    for m in pipeline.mappings:
        strategies[m.load_strategy.value] = \
            strategies.get(m.load_strategy.value, 0) + 1
    return {"mappings": len(pipeline.mappings),
            "source_tables": len(pipeline.sources),
            "transformations_by_type": dict(sorted(tx.items(),
                                                   key=lambda x: -x[1])),
            "derived_expressions": expressions,
            "load_strategies": strategies}


def _artifacts_emitted(out: Path, target_format: str) -> dict:
    a: Dict[str, int] = {}
    if target_format == "powercenter":
        a["workflow_xml"] = len(list(out.glob("wf_*.xml")))
    elif target_format == "idmc" and (out / "idmc").exists():
        a["idmc_bundle_files"] = len(list((out / "idmc").rglob("*.json")))
    elif target_format == "dbt" and (out / "dbt").exists():
        a["dbt_models"] = len(list((out / "dbt" / "models").rglob("*.sql")))
        a["dbt_snapshots"] = len(list((out / "dbt").rglob("snapshots/*.sql")))
        a["yaml_files"] = len(list((out / "dbt").rglob("*.yml")))
    elif (out / "sql").exists():
        a["sql_scripts"] = len(list((out / "sql").glob("*.sql")))
    if (out / "validation_tests").exists():
        tests = _read_json(out / "validation_tests" / "tests.json") or {}
        a["validation_tests"] = tests.get("summary", {}).get("total_tests", 0)
    if (out / "manual_workbook").exists():
        a["manual_workbook_items"] = len(list(
            (out / "manual_workbook").glob("*")))
    return a


def _datatype_risks(pipeline: Pipeline, target_format: str) -> dict:
    try:
        from ..llm.review_agent import _type_platform
        from ..sqlx.type_engine import CanonicalType, TypeMappingEngine
        engine = TypeMappingEngine()
        platform = _type_platform(target_format)
    except Exception:  # noqa: BLE001
        return {"platform": "", "risks": [], "ports_checked": 0}
    risks: List[dict] = []
    checked = 0
    for m in pipeline.mappings:
        tgts = m.by_type(TransformationType.TARGET)
        for p in (tgts[0].ports if tgts else []):
            ct = None
            if p.datatype == "decimal" and p.precision:
                ct = CanonicalType("DECIMAL", precision=p.precision,
                                   scale=p.scale)
            elif p.datatype == "string" and p.precision:
                ct = CanonicalType("STRING", length=p.precision)
            elif p.datatype == "timestamp" and "tz" in p.name.lower():
                ct = CanonicalType("TIMESTAMP_TZ")
            if ct is None:
                continue
            checked += 1
            try:
                _, warns = engine.render_type(ct, platform)
            except Exception:  # noqa: BLE001
                continue
            for w in warns:
                risks.append({"mapping": m.name, "column": p.name,
                              "code": w.code, "message": w.message})
    return {"platform": platform, "ports_checked": checked,
            "risks": risks[:30], "total_risks": len(risks)}


def _function_risks(pipeline: Pipeline, target_format: str) -> dict:
    gaps = []
    for m in pipeline.mappings:
        for i in m.issues:
            if i.code in ("EXPRESSION_UNCONVERTED", "EXPRESSION_UNSUPPORTED"):
                gaps.append({"mapping": m.name,
                             "detail": (i.detail or i.message)[:160]})
    coverage = {}
    try:
        from ..sqlx.registry import get_function_registry
        matrix = get_function_registry().coverage_matrix()
        platform = "informatica" if target_format in ("powercenter", "idmc") \
            else target_format if target_format in matrix["platforms"] \
            else "ansi"
        row = matrix["platforms"].get(platform, {})
        coverage = {"platform": platform,
                    "functions_in_registry": matrix["functions"],
                    "supported": row.get("supported", 0),
                    "workaround": row.get("workaround", 0)}
    except Exception:  # noqa: BLE001
        pass
    return {"registry_coverage": coverage,
            "unconverted_expressions": len(gaps), "samples": gaps[:15]}


def _lineage_summary(pipeline: Pipeline) -> dict:
    try:
        from .lineage import table_lineage
        tl = table_lineage(pipeline)
    except Exception:  # noqa: BLE001
        return {}
    kinds: Dict[str, int] = {}
    for node in tl["nodes"]:
        kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
    children: Dict[str, List[str]] = {}
    for e in tl["edges"]:
        children.setdefault(e["from"], []).append(e["to"])
    depth = 0
    for start in [n["id"] for n in tl["nodes"] if n["kind"] == "source"]:
        frontier, d, seen = [start], 0, set()
        while frontier and d < 50:
            nxt = [c for f in frontier for c in children.get(f, [])
                   if c not in seen]
            seen.update(nxt)
            if not nxt:
                break
            d += 1
            frontier = nxt
        depth = max(depth, d)
    return {"tables": len(tl["nodes"]), "edges": len(tl["edges"]),
            "by_kind": kinds, "longest_chain": depth,
            "detail": "lineage.json / lineage.md (tables, columns, "
                      "transformations + Mermaid graphs)"}


def _recommended_actions(base: dict, validation: Optional[dict],
                         tests: Optional[dict], pipeline: Pipeline,
                         target_format: str) -> List[dict]:
    actions: List[dict] = []

    def add(priority: str, action: str, why: str) -> None:
        actions.append({"priority": priority, "action": action, "why": why})

    verdict = (validation or {}).get("verdict", "")
    if verdict == "FAIL":
        add("P0", "Fix the validation errors before anything else "
                  "(migration_validation_report.md, layers marked FAIL).",
            "Generated output is structurally broken or silently lost "
            "objects.")
    manual_q = base["summary"]["workload"]["manual_queue"]
    if manual_q:
        add("P1", "Work the manual queue (%s items) through "
                  "manual_workbook/ — or run the auto-fix flow for the "
                  "fixable groups." % _n(manual_q),
            "These objects are not migrated until someone converts them.")
    merge_no_key = [m.name for m in pipeline.mappings
                    if m.load_strategy in (LoadStrategy.MERGE,
                                           LoadStrategy.DELETE_INSERT)
                    and not m.unique_key]
    if merge_no_key:
        add("P1", "Declare unique keys for incremental pipelines: %s."
            % ", ".join(merge_no_key[:5]),
            "Without a key, merge loads behave as appends and cannot be "
            "verified.")
    low_conf = [mm["name"] for mm in base["mappings"]
                if mm.get("complexity", {}).get("conversion_confidence",
                                                100) < 70]
    if low_conf:
        add("P2", "Run the AI review on the low-confidence mappings "
                  "(metabridge ai-review ... -m %s)."
            % ",".join(low_conf[:4]),
            "Confidence below 70 means the conversion made assumptions "
            "worth a second pair of eyes.")
    if tests:
        add("P2", "Execute the reconciliation suite after the first "
                  "parallel load (%s tests in validation_tests/)."
            % _n(tests.get("summary", {}).get("total_tests", 0)),
            "Row counts, checksums and business rules prove the migration "
            "on real data — generation alone cannot.")
        if tests.get("summary", {}).get("untestable_rules"):
            add("P3", "Review the %d business rule(s) marked untestable in "
                      "validation_tests/tests.json."
                % tests["summary"]["untestable_rules"],
                "They are enforced by transformations and invisible in "
                "target data.")
    if validation and not validation.get("ai_reviewed", False):
        add("P3", "Configure an AI provider (Settings → AI) and rerun "
                  "ai-review for the agent semantic pass.",
            "Layers 1–4 are deterministic; the agent adds business-intent "
            "review of the riskiest mappings.")
    if target_format == "powercenter":
        add("P2", "Import the generated XML into a PowerCenter sandbox "
                  "repository (pmrep) before scheduling.",
            "Repository import is the only version-exact certification.")
    elif target_format == "idmc":
        add("P2", "Run 'metabridge deploy' (dry-run first) against a "
                  "non-production IDMC org.",
            "Org-level policies can reject bundles that validate locally.")
    if not actions:
        add("P3", "Proceed to side-by-side parallel run and execute the "
                  "reconciliation suite.",
            "No blocking findings — prove parity on production data next.")
    return actions


# --------------------------------------------------------------------------- #
# public API                                                                   #
# --------------------------------------------------------------------------- #

def build_migration_report(pipeline: Pipeline, output_dir: str,
                           target_format: str, dialect: str = "") -> dict:
    from .reporter import build_report
    out = Path(output_dir)
    base = build_report(pipeline, target_format)
    validation = _read_json(out / "migration_validation_report.json")
    tests = _read_json(out / "validation_tests" / "tests.json")

    s = base["summary"]
    counts = s["status_counts"]
    auto = counts["CONVERTED"] + counts["CONVERTED_WITH_WARNINGS"]
    manual = s["workload"]["manual_queue"]
    complexity = base["complexity"]

    src_disp = _display(pipeline.source_format,
                        str(pipeline.metadata.get("dialect", "")))
    tgt_disp = _display(target_format, dialect or
                        str(pipeline.metadata.get("dialect", "")))

    executive = {
        "source": src_disp,
        "target": tgt_disp,
        "mappings_analysed": len(pipeline.mappings),
        "automatically_converted": auto,
        "manual_review": manual,
        "automation_rate_objects": s["automated_conversion_rate"],
        "automation_rate_workload": s["workload"]["coverage_rate"],
        "average_confidence": complexity["conversion_confidence"],
        "complexity_level": complexity["complexity_level"],
        "estimated_manual_effort_hours": complexity.get(
            "manual_effort_estimate_hours", 0),
        "validation_verdict": (validation or {}).get("verdict",
                                                     "NOT_VALIDATED"),
    }

    doc = {
        "tool": "MetaBridge AI",
        "title": "Migration Report — %s" % pipeline.name,
        "generated_at": datetime.datetime.now().isoformat(
            timespec="seconds"),
        "project": pipeline.name,
        "migration_id": pipeline.metadata.get("migration_id", ""),
        "source_snapshot": pipeline.metadata.get("source_snapshot", ""),
        "sections_order": list(SECTIONS),
        "sections": {
            "executive_summary": executive,
            "source_platform": {
                "name": src_disp,
                "format": pipeline.source_format,
                "dialect": str(pipeline.metadata.get("dialect", "")),
                "source_tables": len(pipeline.sources),
                "inventory": pipeline.metadata.get("inventory"),
            },
            "target_platform": {
                "name": tgt_disp,
                "format": target_format,
                "sql_dialect": dialect or SQL_DIALECT_FORMATS.get(
                    target_format, ""),
                "artifacts_emitted": _artifacts_emitted(out, target_format),
            },
            "assets_analysed": _census(pipeline),
            "assets_converted": {
                "status_counts": counts,
                "converted": auto,
                "needs_manual_work": counts["NEEDS_MANUAL_WORK"],
                "failed": counts["FAILED"],
                "native_graph_rate": s["native_graph_rate"],
            },
            "automation_percentage": {
                "object_rate": s["automated_conversion_rate"],
                "workload_coverage_rate": s["workload"]["coverage_rate"],
                "workload": s["workload"],
                "note": "Workload coverage counts every unit the source "
                        "contained, including statements routed to the "
                        "manual queue — it is the honest headline number.",
            },
            "migration_complexity": complexity,
            "conversion_confidence": {
                "average": complexity["conversion_confidence"],
                "per_mapping": sorted(
                    ({"mapping": mm["name"],
                      "confidence": mm.get("complexity", {}).get(
                          "conversion_confidence")}
                     for mm in base["mappings"]
                     if mm.get("complexity")),
                    key=lambda x: x["confidence"] or 0)[:10],
                "low_confidence_threshold": 70,
            },
            "unsupported_components": {
                "count": sum(n for c, n in s["issues_by_code"].items()
                             if c in _UNSUPPORTED_CODES),
                "by_code": {c: n for c, n in s["issues_by_code"].items()
                            if c in _UNSUPPORTED_CODES},
                "detail": "Every unsupported component carries a workaround "
                          "in the manual workbook (manual_workbook/).",
            },
            "manual_review_items": {
                "queue_size": manual,
                "estimated_effort_hours": complexity.get(
                    "manual_effort_estimate_hours", 0),
                "top_items": [
                    {"object": i.get("object", ""), "code": i["code"],
                     "message": i["message"][:160]}
                    for i in (base["project_issues"] +
                              [x for mm in base["mappings"]
                               for x in mm["issues"]])
                    if i["severity"] in ("MANUAL", "ERROR")][:15],
            },
            "data_type_risks": _datatype_risks(pipeline, target_format),
            "function_conversion_risks": _function_risks(pipeline,
                                                         target_format),
            "lineage_summary": _lineage_summary(pipeline),
            "validation_results": {
                "verdict": (validation or {}).get("verdict",
                                                  "NOT_VALIDATED"),
                "layers": {L["name"]: L["status"]
                           for L in (validation or {}).get("layers", [])},
                "totals": (validation or {}).get("totals", {}),
                "reconciliation_tests": (tests or {}).get("summary", {}),
            },
            "recommended_actions": _recommended_actions(
                base, validation, tests, pipeline, target_format),
        },
    }
    return doc


_UNSUPPORTED_CODES = {
    "STATEMENT_UNSUPPORTED", "STATEMENT_PARSE_FAILED", "MERGE_UNSUPPORTED",
    "EXPRESSION_UNCONVERTED", "EXPRESSION_UNSUPPORTED", "SOURCE_UNRESOLVED",
    "SQL_PARSE_ERROR", "JINJA_UNSUPPORTED", "SCD2_MANUAL",
}


# --------------------------------------------------------------------------- #
# renderers                                                                    #
# --------------------------------------------------------------------------- #

def _md_exec(e: dict) -> List[str]:
    return [
        "Source: %s" % e["source"],
        "Target: %s" % e["target"],
        "",
        "Mappings Analysed: %s" % _n(e["mappings_analysed"]),
        "Automatically Converted: %s" % _n(e["automatically_converted"]),
        "Manual Review: %s" % _n(e["manual_review"]),
        "Automation Rate: %.1f%%" % e["automation_rate_objects"],
        "Workload Coverage: %.1f%%" % e["automation_rate_workload"],
        "Average Confidence: %d%%" % e["average_confidence"],
        "Complexity: %s" % e["complexity_level"],
        "Validation: %s" % e["validation_verdict"],
    ]


def render_markdown(doc: dict) -> str:
    sec = doc["sections"]
    lines = ["# %s" % doc["title"], "",
             "_Generated by MetaBridge AI on %s_" % doc["generated_at"], ""]
    lines += ["## 1. Executive Summary", "", "```"]
    lines += _md_exec(sec["executive_summary"])
    lines += ["```", ""]

    titles = {
        "source_platform": "2. Source Platform",
        "target_platform": "3. Target Platform",
        "assets_analysed": "4. Assets Analysed",
        "assets_converted": "5. Assets Converted",
        "automation_percentage": "6. Automation Percentage",
        "migration_complexity": "7. Migration Complexity",
        "conversion_confidence": "8. Conversion Confidence",
        "unsupported_components": "9. Unsupported Components",
        "manual_review_items": "10. Manual Review Items",
        "data_type_risks": "11. Data Type Risks",
        "function_conversion_risks": "12. Function Conversion Risks",
        "lineage_summary": "13. Lineage Summary",
        "validation_results": "14. Validation Results",
    }

    def kv_block(d: dict, indent: str = "") -> List[str]:
        out = []
        for k, v in d.items():
            if isinstance(v, dict):
                out.append("%s- **%s**:" % (indent, k))
                out += kv_block(v, indent + "  ")
            elif isinstance(v, list):
                out.append("%s- **%s**: %d item(s)" % (indent, k, len(v)))
                for item in v[:10]:
                    out.append("%s  - %s" % (indent, json.dumps(item)
                               if isinstance(item, dict) else str(item)))
            elif v is not None and v != "":
                out.append("%s- **%s**: %s" % (indent, k, v))
        return out

    for key, title in titles.items():
        lines += ["## %s" % title, ""]
        lines += kv_block(sec[key])
        lines.append("")

    lines += ["## 15. Recommended Actions", ""]
    for a in sec["recommended_actions"]:
        lines.append("- **%s** %s" % (a["priority"], a["action"]))
        lines.append("  - _%s_" % a["why"])
    return "\n".join(lines) + "\n"


_CSS = """
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;margin:0;
 color:#1b2733;background:#f4f6f9}
.band{background:linear-gradient(120deg,#0d2b45,#173a5e 60%,#1f4d7a);
 color:#fff;padding:38px 48px}
.band h1{margin:0 0 6px;font-size:26px;font-weight:600}
.band .sub{opacity:.75;font-size:13px}
.wrap{max-width:1080px;margin:0 auto;padding:28px 48px 64px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
 gap:14px;margin:-34px 0 30px}
.card{background:#fff;border-radius:10px;padding:16px 18px;
 box-shadow:0 2px 10px rgba(13,43,69,.12)}
.card .v{font-size:24px;font-weight:700;color:#0d2b45}
.card .k{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;
 color:#5b6b7b;margin-top:2px}
h2{font-size:16px;color:#0d2b45;border-bottom:2px solid #dde5ee;
 padding-bottom:6px;margin:34px 0 12px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13.5px;
 border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(13,43,69,.08)}
th{background:#eef2f7;text-align:left;padding:8px 12px;color:#31465c;
 font-size:12px;text-transform:uppercase;letter-spacing:.04em}
td{padding:8px 12px;border-top:1px solid #eef1f5;vertical-align:top}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;
 font-size:12px;font-weight:600;color:#fff}
.note{font-size:12.5px;color:#5b6b7b;margin:6px 0 0}
.action{background:#fff;border-left:4px solid #1f4d7a;border-radius:6px;
 padding:10px 14px;margin:8px 0;box-shadow:0 1px 4px rgba(13,43,69,.08)}
.action b{color:#0d2b45}
.action .why{font-size:12.5px;color:#5b6b7b;margin-top:3px}
"""

_VERDICT_COLORS = {"PASS": "#1e8449", "PASS_WITH_WARNINGS": "#c77d0a",
                   "MANUAL_REVIEW": "#d35400", "FAIL": "#c0392b",
                   "NOT_VALIDATED": "#5b6b7b"}


def render_html(doc: dict) -> str:
    e = doc["sections"]["executive_summary"]
    sec = doc["sections"]
    esc = _html.escape

    def table(rows: List[List[str]], headers: List[str]) -> str:
        body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % esc(str(c))
                                               for c in r) for r in rows)
        head = "".join("<th>%s</th>" % esc(h) for h in headers)
        return "<table><tr>%s</tr>%s</table>" % (head, body)

    verdict = e["validation_verdict"]
    cards = [
        (_n(e["mappings_analysed"]), "Mappings analysed"),
        (_n(e["automatically_converted"]), "Automatically converted"),
        (_n(e["manual_review"]), "Manual review"),
        ("%.1f%%" % e["automation_rate_objects"], "Automation rate"),
        ("%.1f%%" % e["automation_rate_workload"], "Workload coverage"),
        ("%d%%" % e["average_confidence"], "Average confidence"),
    ]
    cards_html = "".join('<div class="card"><div class="v">%s</div>'
                         '<div class="k">%s</div></div>' % (esc(v), esc(k))
                         for v, k in cards)

    parts = ["<style>%s</style>" % _CSS,
             '<div class="band"><h1>%s</h1><div class="sub">%s &rarr; %s '
             '&middot; generated %s &middot; verdict '
             '<span class="badge" style="background:%s">%s</span>'
             '</div></div>'
             % (esc(doc["title"]), esc(e["source"]), esc(e["target"]),
                esc(doc["generated_at"]),
                _VERDICT_COLORS.get(verdict, "#5b6b7b"), esc(verdict)),
             '<div class="wrap">',
             '<div class="cards">%s</div>' % cards_html]

    parts.append("<h2>Source &amp; Target Platforms</h2>")
    parts.append(table(
        [["Source", sec["source_platform"]["name"],
          "%s tables declared" % _n(sec["source_platform"]["source_tables"])],
         ["Target", sec["target_platform"]["name"],
          ", ".join("%s: %s" % (k, _n(v)) for k, v in
                    sec["target_platform"]["artifacts_emitted"].items())
          or "—"]], ["Side", "Platform", "Detail"]))

    a = sec["assets_analysed"]
    parts.append("<h2>Assets Analysed</h2>")
    parts.append(table(
        [[k.replace("_", " "), _n(v)] for k, v in
         [("mappings", a["mappings"]),
          ("source tables", a["source_tables"]),
          ("derived expressions", a["derived_expressions"])]] +
        [["transformations: %s" % k, _n(v)]
         for k, v in a["transformations_by_type"].items()],
        ["Asset", "Count"]))

    c = sec["assets_converted"]
    parts.append("<h2>Assets Converted &amp; Automation</h2>")
    parts.append(table(
        [[k.replace("_", " "), _n(v)]
         for k, v in c["status_counts"].items()] +
        [["object automation rate",
          "%.1f%%" % sec["automation_percentage"]["object_rate"]],
         ["workload coverage (honest)",
          "%.1f%%" % sec["automation_percentage"]["workload_coverage_rate"]]],
        ["Status", "Count"]))
    parts.append('<p class="note">%s</p>'
                 % esc(sec["automation_percentage"]["note"]))

    cx = sec["migration_complexity"]
    parts.append("<h2>Migration Complexity &amp; Confidence</h2>")
    parts.append(table(
        [["complexity score", cx["complexity_score"]],
         ["complexity level", cx["complexity_level"]],
         ["conversion confidence", "%d%%" % cx["conversion_confidence"]],
         ["estimated manual effort",
          "%s h" % cx.get("manual_effort_estimate_hours", 0)]] +
        [[("level %s" % k), _n(v)] for k, v in
         cx.get("level_distribution", {}).items() if v],
        ["Metric", "Value"]))

    u = sec["unsupported_components"]
    m = sec["manual_review_items"]
    parts.append("<h2>Unsupported Components &amp; Manual Review</h2>")
    rows = [[code, _n(n)] for code, n in u["by_code"].items()] or \
        [["(none)", "0"]]
    parts.append(table(rows, ["Component / code", "Count"]))
    parts.append('<p class="note">Manual queue: %s items, estimated %s '
                 'hours. Workbook: manual_workbook/.</p>'
                 % (_n(m["queue_size"]), m["estimated_effort_hours"]))

    dt = sec["data_type_risks"]
    fx = sec["function_conversion_risks"]
    parts.append("<h2>Data Type &amp; Function Conversion Risks</h2>")
    dt_rows = [[r["mapping"], r["column"], r["code"], r["message"][:90]]
               for r in dt.get("risks", [])[:10]] or \
        [["(no datatype risks detected on %s)" % dt.get("platform", "?"),
          "", "", ""]]
    parts.append(table(dt_rows, ["Mapping", "Column", "Risk", "Detail"]))
    cov = fx.get("registry_coverage") or {}
    parts.append('<p class="note">Function registry: %s of %s functions '
                 'native on %s (+%s with workarounds). Unconverted '
                 'expressions recorded: %s.</p>'
                 % (cov.get("supported", "?"),
                    cov.get("functions_in_registry", "?"),
                    cov.get("platform", "?"), cov.get("workaround", 0),
                    _n(fx["unconverted_expressions"])))

    ln = sec["lineage_summary"]
    v = sec["validation_results"]
    parts.append("<h2>Lineage &amp; Validation</h2>")
    parts.append(table(
        [["tables in lineage graph", _n(ln.get("tables", 0))],
         ["lineage edges", _n(ln.get("edges", 0))],
         ["longest source→target chain", ln.get("longest_chain", 0)]] +
        [["validation: %s" % k.replace("_", " "), s2]
         for k, s2 in v.get("layers", {}).items()] +
        [["reconciliation tests generated",
          _n(v.get("reconciliation_tests", {}).get("total_tests", 0))]],
        ["Item", "Value"]))

    parts.append("<h2>Recommended Actions</h2>")
    for act in sec["recommended_actions"]:
        parts.append('<div class="action"><b>%s</b> %s'
                     '<div class="why">%s</div></div>'
                     % (esc(act["priority"]), esc(act["action"]),
                        esc(act["why"])))

    parts.append('<p class="note">Every figure in this report is computed '
                 'from evidence by the MetaBridge AI engines (complexity, '
                 'lineage, validation, reconciliation) — see '
                 'migration_report.json for the machine-readable form.</p>')
    parts.append("</div>")
    return "<!doctype html><meta charset='utf-8'><title>%s</title>%s" \
        % (esc(doc["title"]), "".join(parts))


def write_migration_report(doc: dict, out_dir: str) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "migration_report.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    (out / "migration_report.md").write_text(render_markdown(doc), encoding="utf-8")
    path = out / "migration_report.html"
    path.write_text(render_html(doc), encoding="utf-8")
    return str(path)

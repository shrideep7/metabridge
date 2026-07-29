"""Conversion audit report: JSON (machine-readable) + self-contained HTML.

The report is a first-class deliverable: migration factories run on coverage
numbers and exception queues, so every conversion produces one.
"""
from __future__ import annotations

import datetime
import html
import json
from typing import Dict, List

from ..ir.model import IssueSeverity, Mapping, Pipeline, TransformationType


def build_report(pipeline: Pipeline, target_format: str) -> dict:
    mappings = []
    for m in pipeline.mappings:
        native = not any(i.code == "SQL_OVERRIDE_FALLBACK" for i in m.issues)
        manual = [i for i in m.issues if i.severity == IssueSeverity.MANUAL]
        warn = [i for i in m.issues if i.severity == IssueSeverity.WARNING]
        err = [i for i in m.issues if i.severity == IssueSeverity.ERROR]
        if err:
            status = "FAILED"
        elif manual:
            status = "NEEDS_MANUAL_WORK"
        elif warn:
            status = "CONVERTED_WITH_WARNINGS"
        else:
            status = "CONVERTED"
        mappings.append({
            "name": m.name,
            "status": status,
            "native_graph": native,
            "load_strategy": m.load_strategy.value,
            "transformations": len([t for t in m.transformations
                                    if t.name != "__OUTPUT__"]),
            "depends_on": m.depends_on,
            "issues": [i.to_dict() for i in m.issues],
        })

    counts: Dict[str, int] = {"CONVERTED": 0, "CONVERTED_WITH_WARNINGS": 0,
                              "NEEDS_MANUAL_WORK": 0, "FAILED": 0}
    for mm in mappings:
        counts[mm["status"]] += 1
    total = len(mappings) or 1
    auto = counts["CONVERTED"] + counts["CONVERTED_WITH_WARNINGS"]

    all_issues = [i.to_dict() for i in pipeline.issues] + \
        [i for mm in mappings for i in mm["issues"]]
    by_code: Dict[str, int] = {}
    for i in all_issues:
        by_code[i["code"]] = by_code.get(i["code"], 0) + 1

    # Workload coverage counts EVERYTHING the source contained — including
    # project-level manual items (procedures, unparseable files) that are not
    # attached to any converted object. This is the honest headline number:
    # "objects 100% / 546 manual items" cannot happen; the manual queue drags
    # the coverage down where it belongs.
    project_manual = sum(1 for i in pipeline.issues
                         if i.severity in (IssueSeverity.MANUAL, IssueSeverity.ERROR))
    workload_total = len(mappings) + project_manual
    workload_auto = auto
    manual_queue = project_manual + counts["NEEDS_MANUAL_WORK"] + counts["FAILED"]

    from .complexity import score_pipeline
    complexity = score_pipeline(pipeline)
    asset_scores = {a["name"]: a for a in complexity.pop("assets")}
    for mm in mappings:
        a = asset_scores.get(mm["name"])
        if a:
            mm["complexity"] = {k: a[k] for k in
                                ("complexity_score", "complexity_level",
                                 "conversion_confidence",
                                 "automation_percentage",
                                 "manual_effort_estimate")}

    # module 31: per-object weighted-impact confidence with critical
    # risks and manual review items
    from .confidence import score_pipeline_confidence
    confidence_scoring = score_pipeline_confidence(pipeline)
    by_name = {o["mapping"]: o for o in confidence_scoring["mappings"]}
    for mm in mappings:
        o = by_name.get(mm["name"])
        if o:
            mm["confidence"] = {k: o[k] for k in
                                ("conversion_confidence",
                                 "bottleneck_confidence",
                                 "critical_risks",
                                 "manual_review_items")}

    return {
        "tool": "MetaBridge AI",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "project": pipeline.name,
        "migration_id": pipeline.metadata.get("migration_id", ""),
        "source_snapshot": pipeline.metadata.get("source_snapshot", ""),
        "source_format": pipeline.source_format,
        "target_format": target_format,
        "summary": {
            "objects_total": len(mappings),
            "automated_conversion_rate": round(100.0 * auto / total, 1),
            "workload": {
                "total_units": workload_total,
                "automated_units": workload_auto,
                "manual_queue": manual_queue,
                "coverage_rate": round(100.0 * workload_auto / (workload_total or 1), 1),
                "inventory": pipeline.metadata.get("inventory"),
            },
            "native_graph_rate": round(
                100.0 * sum(1 for mm in mappings if mm["native_graph"]) / total, 1),
            "status_counts": counts,
            "issues_by_severity": {
                s.value: sum(1 for i in all_issues if i["severity"] == s.value)
                for s in IssueSeverity},
            "issues_by_code": dict(sorted(by_code.items(), key=lambda x: -x[1])),
            "llm_assisted": sum(1 for i in all_issues if i.get("resolved_by_llm")),
        },
        "complexity": complexity,
        "project_issues": [i.to_dict() for i in pipeline.issues],
        "mappings": sorted(mappings, key=lambda x: x["name"]),
    }


_SEV_COLORS = {"INFO": "#2f7bbd", "WARNING": "#c77d0a", "MANUAL": "#c0392b",
               "ERROR": "#7b241c"}
_STATUS_COLORS = {"CONVERTED": "#1e8449", "CONVERTED_WITH_WARNINGS": "#c77d0a",
                  "NEEDS_MANUAL_WORK": "#c0392b", "FAILED": "#7b241c"}


def render_html(report: dict) -> str:
    # Commercial gate (EB-503): exporting a report is a licensed feature. OFF by
    # default => no-op; in DENY mode with the report-export quota exhausted this
    # raises EntitlementDenied before any rendering happens.
    from ..commercial import runtime as _commercial
    _commercial.enforce("quota.report_exports")
    _commercial.report_export("html")
    s = report["summary"]
    e = html.escape

    def badge(text: str, color: str) -> str:
        return ('<span style="background:%s;color:#fff;padding:2px 8px;'
                'border-radius:10px;font-size:12px;white-space:nowrap">%s</span>'
                % (color, e(text)))

    rows = []
    for m in report["mappings"]:
        issues_html = ""
        for i in m["issues"]:
            issues_html += (
                '<div style="margin:4px 0">%s <b>%s</b> — %s'
                % (badge(i["severity"], _SEV_COLORS.get(i["severity"], "#666")),
                   e(i["code"]), e(i["message"])))
            if i.get("detail"):
                issues_html += ('<div style="font-family:monospace;font-size:12px;'
                                'color:#555;margin-left:12px">%s</div>' % e(i["detail"][:300]))
            if i.get("suggestion"):
                issues_html += ('<div style="font-size:12px;color:#1e6091;'
                                'margin-left:12px">→ %s</div>' % e(i["suggestion"]))
            issues_html += "</div>"
        rows.append(
            "<tr><td><b>%s</b></td><td>%s</td><td style='text-align:center'>%s</td>"
            "<td style='text-align:center'>%d</td><td>%s</td><td>%s</td></tr>"
            % (e(m["name"]),
               badge(m["status"].replace("_", " "),
                     _STATUS_COLORS.get(m["status"], "#666")),
               "native" if m["native_graph"] else "SQL override",
               m["transformations"], e(m["load_strategy"]),
               issues_html or '<span style="color:#999">—</span>'))

    code_rows = "".join(
        "<tr><td style='font-family:monospace'>%s</td>"
        "<td style='text-align:right'>%d</td></tr>" % (e(c), n)
        for c, n in s["issues_by_code"].items())

    wl = s.get("workload") or {}
    coverage = wl.get("coverage_rate", s["automated_conversion_rate"])
    wb = wl.get("workbook") or {}
    manual_count = s["issues_by_severity"].get("MANUAL", 0)
    workbook_note = ""
    if wb:
        workbook_note = ('<div style="margin:0 40px;padding:12px 18px;background:'
                         '#fff8e6;border:1px solid #eddaa0;border-radius:10px;'
                         'font-size:13.5px">Every manual item has a generated '
                         'starting point: see <b>manual_workbook/</b> (%d '
                         'skeleton files, README with triage order) and '
                         '<b>manual_queue.csv</b> for Jira/Excel import.</div>'
                         % wb.get("items", 0))

    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>MetaBridge AI Conversion Report — %(proj)s</title>
<style>
 body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;background:#f5f6f8;color:#222}
 .head{background:#101d33;color:#fff;padding:28px 40px}
 .head h1{margin:0 0 4px;font-size:22px} .head .sub{color:#9fb3d1;font-size:14px}
 .cards{display:flex;gap:16px;padding:24px 40px;flex-wrap:wrap}
 .card{background:#fff;border-radius:10px;padding:18px 26px;box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:150px}
 .card .n{font-size:30px;font-weight:700} .card .l{font-size:12px;color:#667;text-transform:uppercase;letter-spacing:.5px}
 section{padding:8px 40px 32px} h2{font-size:16px;color:#334}
 table{border-collapse:collapse;width:100%%;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 th{background:#eef1f6;text-align:left;padding:10px 14px;font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:#556}
 td{padding:10px 14px;border-top:1px solid #eef1f6;font-size:14px;vertical-align:top}
 .foot{padding:16px 40px;color:#889;font-size:12px}
</style></head><body>
<div class="head"><h1>MetaBridge AI Conversion Report</h1>
<div class="sub">%(src)s → %(tgt)s &nbsp;|&nbsp; project: <b>%(proj)s</b> &nbsp;|&nbsp; %(ts)s</div></div>
<div class="cards">
 <div class="card"><div class="n" style="color:%(covcolor)s">%(coverage).1f%%</div><div class="l">Workload coverage</div>
   <div style="font-size:11px;color:#889">%(autounits)d of %(units)d units automated</div></div>
 <div class="card"><div class="n">%(total)d</div><div class="l">Pipelines converted</div></div>
 <div class="card"><div class="n" style="color:#1e8449">%(auto).1f%%</div><div class="l">Of pipelines automated</div></div>
 <div class="card"><div class="n" style="color:#c0392b">%(manual)d</div><div class="l">Manual queue</div>
   <div style="font-size:11px;color:#889">%(effort)s</div></div>
 <div class="card"><div class="n" style="color:#c77d0a">%(warn)d</div><div class="l">Warnings</div></div>
 <div class="card"><div class="n">%(llm)d</div><div class="l">LLM-assisted</div></div>
</div>
%(workbook_note)s
<section><h2>Objects</h2>
<table><tr><th>Object</th><th>Status</th><th>Graph</th><th>Transformations</th><th>Load</th><th>Findings</th></tr>
%(rows)s</table></section>
<section><h2>Findings by rule</h2>
<table><tr><th>Rule code</th><th style="text-align:right">Count</th></tr>%(code_rows)s</table></section>
<div class="foot">Generated by MetaBridge AI %(ts)s — automated conversion figures reflect
rule-based translation; items marked MANUAL require engineering review before deployment.</div>
</body></html>""" % {
        "proj": e(report["project"]), "src": e(report["source_format"]),
        "tgt": e(report["target_format"]), "ts": e(report["generated_at"]),
        "total": s["objects_total"], "auto": s["automated_conversion_rate"],
        "coverage": coverage,
        "covcolor": "#1e8449" if coverage >= 90 else
                    "#c77d0a" if coverage >= 60 else "#c0392b",
        "units": wl.get("total_units", s["objects_total"]),
        "autounits": wl.get("automated_units", 0),
        "effort": ("~%s h est. effort" % wb["estimated_hours"]) if wb else "",
        "manual": manual_count,
        "warn": s["issues_by_severity"].get("WARNING", 0),
        "llm": s["llm_assisted"], "rows": "".join(rows),
        "code_rows": code_rows, "workbook_note": workbook_note,
    }


def write_report(pipeline: Pipeline, target_format: str, out_dir: str) -> dict:
    from pathlib import Path

    from .remediation import build_workbook
    report = build_report(pipeline, target_format)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    workbook = build_workbook(pipeline, report, out_dir, target_format)
    if workbook:
        report["summary"]["workload"]["workbook"] = workbook
    (out / "conversion_report.json").write_text(json.dumps(report, indent=2))
    (out / "conversion_report.html").write_text(render_html(report))
    return report

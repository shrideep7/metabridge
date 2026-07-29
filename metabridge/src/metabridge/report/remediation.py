"""Remediation engine: turn every manual finding into an actionable work item
with an actual code skeleton.

Two artifacts are added to every conversion output:

  * ``manual_workbook/`` — one numbered file per MANUAL/ERROR finding
    containing the original code, why it could not be converted, a generated
    skeleton to start from, and a review checklist. Engineers work the queue
    file by file instead of re-reading a report.
  * ``manual_queue.csv`` — the same queue as a spreadsheet (object, rule,
    severity, effort, file) for Jira/ADO import and factory planning.

Skeleton generators are rule-code-specific and source-agnostic: they work for
any project kind (dbt, PowerCenter, IDMC, warehouse SQL) because they operate
on the finding + IR, not on the input format.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import List, Optional

from ..ir.model import Mapping, Pipeline

# rough triage effort per rule code (hours) — factory-planning starting point
_EFFORT = {
    "STATEMENT_UNSUPPORTED": 2.0, "STATEMENT_PARSE_FAILED": 1.0,
    "EXPRESSION_UNCONVERTED": 0.5, "JINJA_UNSUPPORTED": 0.5,
    "SQL_PARSE_ERROR": 1.0, "MERGE_UNSUPPORTED": 1.5,
    "UNSUPPORTED_TRANSFORMATION": 3.0, "PASSTHROUGH_TRANSFORMATION": 2.0,
    "OUTPUT_SCHEMA_UNKNOWN": 0.5, "MERGE_WITHOUT_KEY": 0.25,
    "NO_COLUMN_METADATA": 0.5,
}
_DEFAULT_EFFORT = 1.0

_KEY_HINT = re.compile(r"(_id|_key|_no|_num|number)$", re.IGNORECASE)


def build_workbook(pipeline: Pipeline, report: dict, out_dir: str,
                   target_format: str) -> Optional[dict]:
    """Write manual_workbook/ + manual_queue.csv; returns queue stats."""
    items = _collect_items(report)
    if not items:
        return None
    out = Path(out_dir)
    wb = out / "manual_workbook"
    wb.mkdir(parents=True, exist_ok=True)

    total_effort = 0.0
    rows = []
    for i, item in enumerate(items, 1):
        effort = _EFFORT.get(item["code"], _DEFAULT_EFFORT)
        total_effort += effort
        mapping = pipeline.mapping(item.get("object", "")) if pipeline else None
        skeleton = _skeleton_for(item, mapping, target_format)
        fname = "%03d_%s_%s.sql" % (i, _safe(item.get("object") or "project"),
                                    item["code"].lower())
        (wb / fname).write_text(_render_item(i, item, skeleton, effort), encoding="utf-8")
        rows.append({
            "item": i, "file": fname, "severity": item["severity"],
            "rule": item["code"], "object": item.get("object", ""),
            "estimated_hours": effort,
            "summary": (item.get("message") or "")[:180],
        })

    with (out / "manual_queue.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["item", "severity", "rule", "object",
                                          "estimated_hours", "summary", "file"])
        w.writeheader()
        w.writerows(rows)

    by_rule = {}
    for r in rows:
        by_rule[r["rule"]] = by_rule.get(r["rule"], 0) + 1
    (wb / "000_README.md").write_text(_render_readme(rows, by_rule, total_effort), encoding="utf-8")
    return {"items": len(rows), "estimated_hours": round(total_effort, 1),
            "by_rule": dict(sorted(by_rule.items(), key=lambda x: -x[1]))}


def _collect_items(report: dict) -> List[dict]:
    items = []
    for i in report.get("project_issues", []):
        if i["severity"] in ("MANUAL", "ERROR"):
            items.append(dict(i))
    for m in report.get("mappings", []):
        for i in m.get("issues", []):
            if i["severity"] in ("MANUAL", "ERROR"):
                d = dict(i)
                d.setdefault("object", m["name"])
                items.append(d)
    return items


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)[:60]


# ---------------------------------------------------------------------------
# Skeleton generators (source-agnostic — keyed by rule code)
# ---------------------------------------------------------------------------

def _skeleton_for(item: dict, mapping: Optional[Mapping], target: str) -> str:
    code = item["code"]
    detail = item.get("detail", "") or ""

    if code in ("STATEMENT_UNSUPPORTED", "STATEMENT_PARSE_FAILED",
                "MERGE_UNSUPPORTED"):
        return _statement_skeleton(detail, target)
    if code == "EXPRESSION_UNCONVERTED":
        return _expression_skeleton(detail, target)
    if code == "JINJA_UNSUPPORTED":
        return ("-- This model uses Jinja MetaBridge AI cannot expand statically.\n"
                "-- FASTEST FIX (no hand-porting): compile the project first, "
                "then re-convert:\n--\n--     dbt compile --project-dir .\n"
                "--     metabridge convert . --source dbt ...\n--\n"
                "-- MetaBridge AI will use target/manifest.json (fully expanded SQL).")
    if code == "MERGE_WITHOUT_KEY":
        return _key_suggestion_skeleton(mapping)
    if code == "OUTPUT_SCHEMA_UNKNOWN":
        return ("-- Output columns could not be inferred (SELECT *).\n"
                "-- Declare them once and re-convert — schema.yml snippet to "
                "fill in:\n--\n-- models:\n--   - name: %s\n--     columns:\n"
                "--       - {name: <col>, data_type: <type>}\n"
                % (item.get("object", "<model>")))
    if code in ("UNSUPPORTED_TRANSFORMATION", "PASSTHROUGH_TRANSFORMATION"):
        return ("-- This transformation type has no automatic equivalent.\n"
                "-- Typical ports:\n"
                "--   Sequence   -> surrogate key: row_number()/identity or "
                "dbt_utils.generate_surrogate_key\n"
                "--   Router     -> one model/branch per output group (WHERE per group)\n"
                "--   Rank       -> qualify row_number() over (partition ... order ...) <= N\n"
                "--   Update Strategy -> incremental config (merge/delete+insert)\n")
    if code == "SQL_PARSE_ERROR":
        return ("-- The file did not parse in the declared dialect.\n"
                "-- 1. Confirm the source dialect (--source snowflake|tsql|oracle...).\n"
                "-- 2. If it contains procedural blocks, split DDL/DML from "
                "procedures.\n-- 3. Re-run; anything still failing lands back "
                "in this queue.")
    return "-- Review the finding above and port manually."


def _statement_skeleton(original: str, target: str) -> str:
    head = original.strip().split("\n", 1)[0][:80].upper()
    is_proc = any(k in head for k in ("PROCEDURE", "FUNCTION", "CALL", "TASK",
                                      "EXECUTE", "BEGIN"))
    if target == "dbt" or target in ("snowflake", "databricks", "bigquery",
                                     "redshift", "synapse", "sqlserver",
                                     "oracle", "postgres", "teradata", "sql"):
        note = ("-- PROCEDURAL LOGIC — decompose into set-based steps:\n"
                "--   1. Each cursor loop / temp table  ->  a CTE or intermediate model\n"
                "--   2. Each UPDATE/DELETE sequence     ->  incremental model "
                "(merge / delete+insert)\n"
                "--   3. Scheduling (TASK/CALL chains)   ->  your orchestrator "
                "(dbt Cloud, Airflow, ADF)\n") if is_proc else \
               ("-- Port this statement's intent as a model/script:\n")
        return (note +
                "\nWITH step_1 AS (\n    -- TODO: first set-based step\n"
                "    SELECT 1 AS placeholder\n)\n"
                "SELECT * FROM step_1\n-- TODO: replace with the real logic; "
                "keep the ORIGINAL below as the spec.")
    # Informatica targets
    return ("-- PROCEDURAL LOGIC — in %s this typically becomes:\n"
            "--   * a mapping per set-based step (source qualifier + "
            "expression/filter/aggregator)\n"
            "--   * update strategy for UPDATE/DELETE behavior\n"
            "--   * a workflow/taskflow for the CALL/TASK chain\n"
            "-- Start from a generated mapping of a similar shape and adapt."
            % target)


def _expression_skeleton(detail: str, target: str) -> str:
    original = detail.split(" | ")[0] if " | " in detail else detail
    return ("-- Original expression (canonical SQL):\n--   %s\n--\n"
            "-- Port it manually, or re-run with --llm-assist to let Claude "
            "translate it\n-- (LLM translations are flagged resolved_by_llm "
            "for review).\n-- Placeholder used in the generated asset: "
            "pass-through of the input column." % original[:300])


def _key_suggestion_skeleton(mapping: Optional[Mapping]) -> str:
    candidates = []
    if mapping is not None:
        from ..ir.model import TransformationType
        for t in mapping.by_type(TransformationType.TARGET):
            candidates = [p.name for p in t.ports if _KEY_HINT.search(p.name)]
    if candidates:
        return ("-- Likely business key column(s) detected on the target:\n"
                "--   %s\n-- Set it and re-convert:\n"
                "--   UI: unique key field in the conversion plan\n"
                "--   CLI: --override \"%s=incremental:%s\""
                % (", ".join(candidates),
                   mapping.name if mapping else "<model>", candidates[0]))
    return ("-- No obvious key column found. Confirm the business key with "
            "the data owner,\n-- then set it in the conversion plan (UI) or "
            "--override \"<model>=incremental:<key>\" (CLI).")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_item(n: int, item: dict, skeleton: str, effort: float) -> str:
    original = item.get("detail", "") or "(no source snippet captured)"
    return """-- ============================================================
-- MetaBridge AI manual work item #%03d
-- Object:    %s
-- Rule:      %s   |   Severity: %s   |   Est. effort: %.2g h
-- Why:       %s
-- Suggested: %s
-- ============================================================

-- ---------------------------- SKELETON ----------------------------
%s

-- ---------------------------- ORIGINAL ----------------------------
/*
%s
*/

-- ---------------------------- CHECKLIST ---------------------------
-- [ ] Logic ported and reviewed against the original above
-- [ ] Data types / null handling verified
-- [ ] Row counts reconciled against the source system
-- [ ] Finding closed in the audit report queue
""" % (n, item.get("object") or "(project)", item["code"], item["severity"],
       effort,
       (item.get("message") or "").strip(),
       (item.get("suggestion") or "see skeleton").strip(),
       skeleton.strip(), original.strip()[:4000])


def _render_readme(rows: List[dict], by_rule: dict, total_effort: float) -> str:
    lines = ["# Manual work queue",
             "",
             "%d items · estimated %.1f engineering hours (triage baseline "
             "— adjust per team)" % (len(rows), total_effort),
             "",
             "| Rule | Items |",
             "|---|---|"]
    lines += ["| %s | %d |" % (k, v) for k, v in by_rule.items()]
    lines += ["",
              "Work the numbered files in order; each contains the original "
              "code, a skeleton,",
              "and a checklist. `manual_queue.csv` (one level up) imports "
              "into Jira/Excel.",
              "Tip: re-running the conversion with `--llm-assist` can clear "
              "EXPRESSION_UNCONVERTED items automatically (flagged for review)."]
    return "\n".join(lines) + "\n"

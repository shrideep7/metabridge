"""Auto-fix: turn manual-queue items into applied fixes — with user approval.

Flow (driven by the console's findings explorer or the API):

  1. ``plan_fixes(report, pipeline)`` — inspect the manual queue and propose
     what can be fixed automatically, grouped by mechanism:
       * ``key_fix``        — MERGE_WITHOUT_KEY where a business-key candidate
                              was detected on the target (pure rules, no AI)
       * ``llm_expression`` — EXPRESSION_UNCONVERTED items; re-converting with
                              LLM assist translates them engine-level
       * ``llm_statement``  — STATEMENT_UNSUPPORTED / parse failures; Claude
                              drafts target-platform code as reviewable files
  2. The user approves groups (and sees exactly what will happen).
  3. ``apply_fixes(...)`` — re-runs the conversion with the approved key
     overrides and LLM assist, writes statement drafts under
     ``manual_drafts/``, and returns the fresh report.

Nothing is changed without approval, and every LLM-produced artifact is
flagged (``resolved_by_llm`` / draft files) so review obligations are visible
in the audit trail.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from ..ir.model import Pipeline, TransformationType
from ..llm.assist import LLMDrafter, llm_available

_KEY_HINT = re.compile(r"(_id|_key|_no|_num|number)$", re.IGNORECASE)


def plan_fixes(report: dict, pipeline: Optional[Pipeline] = None) -> dict:
    """Inventory what the auto-fixer could do for this report."""
    key_fixes: List[dict] = []
    expr_items: List[dict] = []
    stmt_items: List[dict] = []

    seen_key_models = set()
    for m in report.get("mappings", []):
        for i in m.get("issues", []):
            if i["code"] == "MERGE_WITHOUT_KEY":
                if m["name"] in seen_key_models:
                    continue
                seen_key_models.add(m["name"])
                candidates = _key_candidates(pipeline, m["name"]) if pipeline else []
                key_fixes.append({"model": m["name"], "candidates": candidates,
                                  "keys": candidates,
                                  "proposed_key": candidates[0] if candidates else None})
            elif i["code"] == "EXPRESSION_UNCONVERTED":
                expr_items.append({"object": i.get("object") or m["name"],
                                   "detail": (i.get("detail") or "")[:200]})

    for i in report.get("project_issues", []) + \
            [i for m in report.get("mappings", []) for i in m.get("issues", [])]:
        if i["code"] in ("STATEMENT_UNSUPPORTED", "STATEMENT_PARSE_FAILED",
                         "MERGE_UNSUPPORTED"):
            stmt_items.append({"object": i.get("object", "") or "(project)",
                               "code": i["code"],
                               "original": i.get("detail", "") or "",
                               "message": i.get("message", "")})

    ai = llm_available()
    return {
        "llm_available": ai,
        "groups": {
            "key_fix": {
                "label": "Set detected merge keys and re-convert",
                "mechanism": "rules",
                "ready": bool([k for k in key_fixes if k["proposed_key"]]),
                "items": key_fixes,
            },
            "llm_expression": {
                "label": "Translate unconverted expressions with Claude "
                         "(flagged resolved_by_llm)",
                "mechanism": "llm",
                "ready": ai and bool(expr_items),
                "items": expr_items[:50],
                "total": len(expr_items),
            },
            "llm_statement": {
                "label": "Draft target code for procedures/unsupported "
                         "statements (reviewable files)",
                "mechanism": "llm",
                "ready": ai and bool(stmt_items),
                "items": [{"object": s["object"], "code": s["code"],
                           "preview": s["original"][:160]} for s in stmt_items[:50]],
                "total": len(stmt_items),
            },
        },
    }


def _key_candidates(pipeline: Pipeline, model: str) -> List[str]:
    m = pipeline.mapping(model)
    if m is None:
        return []
    # aggregated models: the GROUP BY columns are the grain — the true key
    for t in m.by_type(TransformationType.AGGREGATOR):
        group_by = [str(g) for g in t.properties.get("group_by", [])]
        if group_by:
            return group_by
    for t in m.by_type(TransformationType.TARGET):
        hits = [p.name for p in t.ports if _KEY_HINT.search(p.name)]
        if hits:
            return hits
    return []


def apply_fixes(input_path: str, output_dir: str, meta: dict,
                accepted_groups: List[str], stmt_items: List[dict],
                prior_report: Optional[dict] = None,
                max_drafts: int = 100) -> dict:
    """Re-run the conversion with approved fixes; returns the fresh report.

    ``meta`` is the job's stored convert parameters (source/target/options).
    ``prior_report`` is the job's stored report — the source of truth for what
    needs fixing (findings may only exist under the job's overrides, so a
    fresh parse cannot be used to find them).
    """
    from ..engine import convert as run_convert

    options = meta.get("options", {}) or {}
    overrides = dict(options.get("overrides") or {})
    use_llm = bool(options.get("llm_assist"))

    key_info: List[str] = []
    if "key_fix" in accepted_groups and prior_report:
        # candidates need port metadata — parse fresh, but find the findings
        # in the job's stored report
        from ..engine import parse_input
        pipeline = None
        try:
            pipeline = parse_input(input_path, meta.get("source_format", ""),
                                   str(options.get("dialect", "") or ""))
        except Exception:  # noqa: BLE001
            pass
        for fix in plan_fixes(prior_report, pipeline)["groups"]["key_fix"]["items"]:
            if fix["proposed_key"]:
                keys = fix.get("keys") or [fix["proposed_key"]]
                spec = dict(overrides.get(fix["model"], {}))
                spec["strategy"] = spec.get("strategy") or "merge"
                spec["unique_key"] = keys
                overrides[fix["model"]] = spec
                key_info.append("%s -> %s" % (fix["model"], ", ".join(keys)))

    from ..llm.assist import llm_available
    ai = llm_available()

    # key_fix is deterministic and NEVER depends on LLM availability. When it
    # detected no key it applied nothing — that's a known no-op up front.
    if "llm_expression" in accepted_groups:
        use_llm = True   # drive the assist; a missing provider simply resolves nothing

    prior_expr = _count_code(prior_report or {}, "EXPRESSION_UNCONVERTED")

    report = run_convert(
        input_path, output_dir,
        meta.get("source_format", ""), meta.get("target_format", ""),
        str(options.get("dialect", "") or ""), llm_assist=use_llm,
        models=options.get("models"), overrides=overrides or None)

    # drafting goes through LLMDrafter, which honours a real provider OR a
    # test stub — so we never pre-gate on llm_available(); a no-op is detected
    # from whether drafts were actually written.
    drafts = []
    if "llm_statement" in accepted_groups and stmt_items:
        drafts = _write_drafts(stmt_items[:max_drafts], meta, output_dir)

    new_expr = _count_code(report, "EXPRESSION_UNCONVERTED")
    expr_resolved = max(0, prior_expr - new_expr) if "llm_expression" in accepted_groups else 0

    # A change was genuinely applied iff a merge key was set, an expression was
    # actually resolved, or a reviewable draft was written — measured from the
    # real result, not a pre-check. Anything approved that produced nothing is
    # reported as a skipped no-op, never as a success.
    changed = bool(key_info) or expr_resolved > 0 or len(drafts) > 0
    no_ai = " No AI provider is configured — set one under Settings → AI Runtime." \
        if not ai else ""
    skipped: List[dict] = []
    if "key_fix" in accepted_groups and not key_info:
        skipped.append({"group": "key_fix",
                        "reason": "No mergeable key could be detected for the "
                                  "selected model(s). Set a unique key manually "
                                  "in the plan, then re-convert."})
    if "llm_expression" in accepted_groups and expr_resolved == 0:
        skipped.append({"group": "llm_expression",
                        "reason": "No unconverted expressions were translated." + no_ai})
    if "llm_statement" in accepted_groups and not drafts:
        skipped.append({"group": "llm_statement",
                        "reason": "No target-code drafts were produced." + no_ai})

    report["autofix"] = {
        "applied_groups": accepted_groups,
        "key_overrides": key_info,
        "llm_assist_used": use_llm,
        "expressions_resolved": expr_resolved,
        "drafts_written": len(drafts),
        "draft_files": drafts[:50],
        "changed": changed,
        "skipped": skipped,
        "note": "LLM output is flagged for review — check resolved_by_llm "
                "findings and every file under manual_drafts/.",
    }
    import json
    (Path(output_dir) / "conversion_report.json").write_text(
        json.dumps(report, indent=2))
    return report


def _count_code(report: dict, code: str) -> int:
    n = 0
    for i in report.get("project_issues", []) or []:
        if i.get("code") == code:
            n += 1
    for m in report.get("mappings", []) or []:
        for i in m.get("issues", []) or []:
            if i.get("code") == code:
                n += 1
    return n


def _write_drafts(stmt_items: List[dict], meta: dict, output_dir: str) -> List[str]:
    drafter = LLMDrafter()
    out = Path(output_dir) / "manual_drafts"
    out.mkdir(parents=True, exist_ok=True)
    src = meta.get("source_format", "sql")
    tgt = meta.get("target_format", "sql")
    written: List[str] = []
    for i, item in enumerate(stmt_items, 1):
        original = item.get("original") or ""
        if not original.strip():
            continue
        draft = drafter.draft(original, src, tgt, item.get("object", ""))
        if not draft:
            continue
        header = ("-- MetaBridge AI LLM draft (REVIEW REQUIRED) — %s / %s\n"
                  "-- Source statement is embedded below the draft.\n\n"
                  % (item.get("object", ""), item.get("code", "")))
        body = "%s%s\n\n/* ORIGINAL:\n%s\n*/\n" % (header, draft.strip(),
                                                   original.strip()[:6000])
        fname = "draft_%03d_%s.sql" % (i, _safe(item.get("object") or "stmt"))
        (out / fname).write_text(body)
        written.append("manual_drafts/" + fname)
    return written


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)[:60]

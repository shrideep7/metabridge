"""AI migration review agent.

The reviewer receives, per mapping: the SOURCE ARTIFACT (original SQL/XML),
the CIR (canonical semantic representation), the GENERATED TARGET (the
actual emitted artifact), the CONVERSION WARNINGS, and the UNSUPPORTED
FEATURES — plus a deterministic diff computed by the engines (round-trip
join/filter/aggregation/window comparison, datatype rendering warnings),
so the agent reasons over evidence, not vibes.

Review dimensions (exactly these — findings are pinned to one of them):

    business_logic_preservation
    missing_transformations
    incorrect_function_conversions
    datatype_risks
    join_semantic_changes
    filter_semantic_changes
    aggregation_changes
    window_function_changes

Safety contract:
  * the reviewer NEVER writes to the generated output — it returns
    proposed corrections (file + exact current_code + proposed_code +
    rationale) stored in ai_review/review.json with status "proposed";
  * corrections are applied ONLY through apply_corrections() with an
    explicit list of approved ids;
  * every application takes a pre-image, requires the current_code to
    match exactly once, syntax-checks the modified file, and reverts
    automatically if the file no longer parses;
  * without an AI provider the review still runs — rule-based findings
    from the deterministic diff, labeled generated_by="rules", and no
    corrections (proposals require the agent).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sqlglot

from ..ir.model import (
    IssueSeverity, Mapping, Pipeline, TransformationType,
)
from ..parsers.sql_parser import SQL_DIALECT_FORMATS

# the NINE review questions (module 32) — each is a dimension
DIMENSIONS = (
    "business_logic_preservation",          # 1. business logic preserved?
    "transformation_semantic_equivalence",  # 2. semantics equivalent?
    "null_semantics",                       # 3. NULL semantics preserved?
    "join_semantics",                       # 4. join semantics preserved?
    "lookup_semantics",                     # 5. lookup semantics preserved?
    "router_multimatch_semantics",          # 6. router multi-match kept?
    "stateful_variable_handling",           # 7. stateful vars correct?
    "scd_logic_preservation",               # 8. SCD logic preserved?
    "target_specific_risks",                # 9. target-specific risks?
)

QUESTIONS = (
    "Has business logic been preserved?",
    "Are transformation semantics equivalent?",
    "Are NULL semantics preserved?",
    "Are join semantics preserved?",
    "Are lookup semantics preserved?",
    "Are Router multi-match semantics preserved?",
    "Are stateful variables handled correctly?",
    "Is SCD logic preserved?",
    "Are there target-specific risks?",
)

_SYSTEM = """You are a senior data-migration reviewer. You receive the \
original source artifact, its canonical intermediate representation (CIR), \
the generated target artifact, the conversion warnings, the unsupported \
features, and a deterministic diff computed by the conversion engine.

Answer EXACTLY these nine questions (each maps to a dimension: %s):
%s

Be skeptical and concrete: cite the code you are worried about. Do NOT \
praise. Do NOT invent problems the evidence does not support.

If (and only if) you can propose a safe textual correction to the generated \
file, include it with the EXACT text to replace (current_code must be \
copied verbatim from the GENERATED TARGET) and the replacement. You cannot \
apply anything yourself — a human approves each correction.

Respond with JSON only:
{"review_status": "PASS" | "WARNING" | "MANUAL_REVIEW",
 "confidence": 0-100,
 "semantic_risks": [{"dimension": "<one of the dimensions>",
                     "severity": "HIGH"|"MEDIUM"|"LOW",
                     "description": "...", "evidence": "..."}],
 "proposed_corrections": [{"file": "<relative path given in the input>",
                           "description": "...",
                           "current_code": "<verbatim from generated>",
                           "proposed_code": "...",
                           "rationale": "..."}],
 "reasoning_summary": "<3-5 sentences>"}""" % (
    ", ".join(DIMENSIONS),
    "\n".join("%d. %s" % (i, q) for i, q in enumerate(QUESTIONS, 1)))


# --------------------------------------------------------------------------- #
# evidence bundle                                                              #
# --------------------------------------------------------------------------- #

def _norm_sql(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _generated_artifact(out: Path, target_format: str,
                        m: Mapping) -> Tuple[str, str]:
    """(relative file path, content excerpt) of the generated artifact."""
    if target_format == "dbt":
        for f in sorted((out / "dbt").rglob("*.sql")):
            if f.stem == m.name:
                return str(f.relative_to(out)), f.read_text()
    elif target_format in SQL_DIALECT_FORMATS:
        for f in sorted((out / "sql").glob("*.sql")):
            if f.stem.split("_", 1)[-1] == m.name:
                return str(f.relative_to(out)), f.read_text()
    elif target_format == "powercenter":
        xmls = sorted(out.glob("wf_*.xml")) or sorted(out.glob("*.xml"))
        if xmls:
            text = xmls[0].read_text()
            start = text.find('<MAPPING NAME="%s"' % m.name)
            if start >= 0:
                end = text.find("</MAPPING>", start)
                return str(xmls[0].relative_to(out)), \
                    text[start:end + len("</MAPPING>")]
            return str(xmls[0].relative_to(out)), text[:3000]
    elif target_format == "idmc":
        for f in sorted((out / "idmc").rglob("*.json")):
            if m.name.lower() in f.stem.lower():
                return str(f.relative_to(out)), f.read_text()
    return "", ""


def _cir_excerpt(pipeline: Pipeline, m: Mapping) -> dict:
    """Trimmed CIR of one mapping — semantic structure, not port noise."""
    try:
        from ..cir.builder import build_cir
        project = build_cir(pipeline)
        for p in project.pipelines:
            if p.name.lower() != m.name.lower():
                continue
            doc = p.to_dict()
            txt = json.dumps(doc)
            if len(txt) > 4000:      # trim column lists first, keep semantics
                for t in doc.get("transformations", []):
                    if isinstance(t, dict) and len(t.get("columns", [])) > 6:
                        t["columns"] = t["columns"][:6] + ["..."]
            return doc
    except Exception:  # noqa: BLE001 — the review runs even if CIR chokes
        pass
    return {}


def _issue_lists(m: Mapping) -> Tuple[List[dict], List[dict]]:
    warnings, unsupported = [], []
    for i in m.issues:
        entry = {"code": i.code, "message": i.message,
                 "detail": (i.detail or "")[:200]}
        if i.severity in (IssueSeverity.MANUAL, IssueSeverity.ERROR):
            unsupported.append(entry)
        elif i.severity == IssueSeverity.WARNING:
            warnings.append(entry)
    return warnings, unsupported


def _type_platform(target_format: str) -> str:
    if target_format in ("powercenter", "idmc"):
        return "informatica"
    from ..sqlx.type_engine import TYPE_PLATFORMS
    return target_format if target_format in TYPE_PLATFORMS else "ansi"


def _deterministic_diff(m: Mapping, twin: Optional[Mapping],
                        target_format: str,
                        generated: str) -> Dict[str, List[str]]:
    """Per-dimension evidence from the engines — feeds the agent and IS the
    rules-only fallback review."""
    ev: Dict[str, List[str]] = {d: [] for d in DIMENSIONS}

    # missing transformations (round-trip type census)
    if twin is not None:
        def census(x: Mapping) -> Dict[str, int]:
            out: Dict[str, int] = {}
            for t in x.transformations:
                if t.name == "__OUTPUT__" or t.type in (
                        TransformationType.SOURCE_QUALIFIER,):
                    continue
                out[t.type.value] = out.get(t.type.value, 0) + 1
            return out
        src_c, twin_c = census(m), census(twin)
        for ttype, n in src_c.items():
            if twin_c.get(ttype, 0) < n and ttype not in (
                    "SOURCE", "TARGET", "EXPRESSION"):
                ev["transformation_semantic_equivalence"].append(
                    "source has %d %s node(s), generated output shows %d"
                    % (n, ttype, twin_c.get(ttype, 0)))

        # join semantics
        sj = [(str(j.properties.get("join_type", "INNER")).upper(),
               _norm_sql(j.properties.get("condition", "")))
              for j in m.by_type(TransformationType.JOINER)]
        tj = [(str(j.properties.get("join_type", "INNER")).upper(),
               _norm_sql(j.properties.get("condition", "")))
              for j in twin.by_type(TransformationType.JOINER)]
        if sorted(sj) != sorted(tj) and sj:
            ev["join_semantics"].append(
                "source joins %s vs generated joins %s" % (sj, tj or "none"))

        # filter semantics (watermarks excluded — they parametrize a run)
        sf = {_norm_sql(f.properties.get("condition", ""))
              for f in m.by_type(TransformationType.FILTER)
              if "$$" not in str(f.properties.get("condition", ""))}
        tf = {_norm_sql(f.properties.get("condition", ""))
              for f in twin.by_type(TransformationType.FILTER)}
        lost = sf - tf
        if lost:
            ev["transformation_semantic_equivalence"].append(
                "filter condition(s) not visible in generated output: %s"
                % "; ".join(sorted(lost)))

        # aggregation grain
        def grains(x: Mapping) -> List[frozenset]:
            return [frozenset(_norm_sql(g) for g in
                              (a.properties.get("group_by") or []))
                    for a in x.by_type(TransformationType.AGGREGATOR)]
        if sorted(map(sorted, grains(m))) != sorted(map(sorted,
                                                        grains(twin))) \
                and grains(m):
            ev["transformation_semantic_equivalence"].append(
                "aggregation grain differs: source %s vs generated %s"
                % ([sorted(g) for g in grains(m)],
                   [sorted(g) for g in grains(twin)]))

    # window functions: source expressions/origin vs generated text
    src_windows = len(re.findall(r"\bOVER\s*\(", m.origin or "",
                                 re.IGNORECASE)) + \
        len(m.by_type(TransformationType.RANK))
    gen_windows = len(re.findall(r"\bOVER\s*\(", generated or "",
                                 re.IGNORECASE))
    if src_windows and target_format in SQL_DIALECT_FORMATS.keys() | {"dbt"} \
            and gen_windows < src_windows:
        ev["transformation_semantic_equivalence"].append(
            "source uses %d window function(s)/rank node(s) but the "
            "generated artifact shows %d OVER(...) clause(s)"
            % (src_windows, gen_windows))

    # function conversions: recorded expression gaps
    for i in m.issues:
        if i.code in ("EXPRESSION_UNCONVERTED", "EXPRESSION_UNSUPPORTED"):
            ev["transformation_semantic_equivalence"].append(
                "%s: %s" % (i.code, (i.detail or i.message)[:160]))

    # questions 3, 5, 6, 7, 8: evidence the phase-2 handlers recorded
    _Q_CODES = (
        ("null_semantics", ("NULL", "3VL", "CASE_SENS")),
        ("lookup_semantics", ("LOOKUP", "LKP")),
        ("router_multimatch_semantics", ("ROUTER",)),
        ("stateful_variable_handling", ("STATEFUL", "SEQUENCE_STATE")),
        ("scd_logic_preservation", ("SCD",)),
    )
    for i in m.issues:
        if i.severity not in (IssueSeverity.WARNING, IssueSeverity.MANUAL):
            continue
        for dim, tokens in _Q_CODES:
            if any(tok in i.code for tok in tokens):
                ev[dim].append("%s: %s" % (i.code, i.message[:160]))
                break
    # SCD strategy must survive the round trip
    if twin is not None and ("scd1_cir" in m.properties or
                             "scd2_cir" in m.properties):
        if m.load_strategy != twin.load_strategy:
            ev["scd_logic_preservation"].append(
                "detected SCD mapping: load strategy %s became %s in the "
                "generated output"
                % (m.load_strategy.value, twin.load_strategy.value))
    # router multi-match: every source branch must survive
    src_routes = [x for x in m.transformations
                  if x.properties.get("synthesized_from") == "router_group"]
    if twin is not None and src_routes:
        twin_routes = [x for x in twin.transformations
                       if "route" in x.name.lower()
                       or x.type == TransformationType.FILTER]
        if len(twin_routes) < len(src_routes):
            ev["router_multimatch_semantics"].append(
                "source has %d router branch(es) but only %d survive in "
                "the generated output — multi-match may be collapsed"
                % (len(src_routes), len(twin_routes)))

    # datatype risks: render each typed decimal/tz port on the target
    try:
        from ..sqlx.type_engine import CanonicalType, TypeMappingEngine
        engine = TypeMappingEngine()
        platform = _type_platform(target_format)
        tgts = m.by_type(TransformationType.TARGET)
        for p in (tgts[0].ports if tgts else []):
            ct = None
            if p.datatype == "decimal" and p.precision:
                ct = CanonicalType("DECIMAL", precision=p.precision,
                                   scale=p.scale)
            elif p.datatype == "timestamp" and "tz" in (p.name or "").lower():
                ct = CanonicalType("TIMESTAMP_TZ")
            if ct is None:
                continue
            _, warns = engine.render_type(ct, platform)
            for w in warns:
                ev["target_specific_risks"].append("%s: %s" % (p.name, w.message))
    except Exception:  # noqa: BLE001
        pass

    return ev


def build_review_bundle(pipeline: Pipeline, out: Path, target_format: str,
                        m: Mapping,
                        twin: Optional[Mapping]) -> dict:
    """Everything the reviewer receives, per the spec — plus the diff."""
    gen_file, gen_content = _generated_artifact(out, target_format, m)
    warnings, unsupported = _issue_lists(m)
    return {
        "mapping": m.name,
        "source_artifact": (m.origin or "").strip()[:3000],
        "cir": _cir_excerpt(pipeline, m),
        "generated_file": gen_file,
        "generated_target": gen_content[:3000],
        "conversion_warnings": warnings[:15],
        "unsupported_features": unsupported[:15],
        "deterministic_diff": _deterministic_diff(m, twin, target_format,
                                                  gen_content),
    }


# --------------------------------------------------------------------------- #
# the review                                                                   #
# --------------------------------------------------------------------------- #

def _rules_only_review(bundle: dict) -> dict:
    findings = []
    for dim, items in bundle["deterministic_diff"].items():
        for item in items:
            findings.append({"dimension": dim, "severity": "MEDIUM",
                             "description": item, "evidence": "engine diff"})
    status = "PASS" if not findings else (
        "MANUAL_REVIEW" if any(f["severity"] == "HIGH" for f in findings)
        else "WARNING")
    return {
        "mapping": bundle["mapping"],
        "review_status": status,
        "business_logic_preserved": not findings,
        "confidence": 60 if not findings else 40,
        "findings": findings,
        "semantic_risks": findings,
        "corrections": [],
        "reasoning_summary": "Deterministic engine diff only (no AI "
                             "provider): %d evidence item(s) across the "
                             "nine review questions." % len(findings),
    }


def _agent_review(client, cfg, bundle: dict) -> Optional[dict]:
    user = (
        "SOURCE ARTIFACT:\n%s\n\nCIR:\n%s\n\nGENERATED TARGET (file: %s):\n"
        "%s\n\nCONVERSION WARNINGS:\n%s\n\nUNSUPPORTED FEATURES:\n%s\n\n"
        "DETERMINISTIC DIFF (engine-computed — verify, refine, or refute):\n"
        "%s"
        % (bundle["source_artifact"] or "(not preserved)",
           json.dumps(bundle["cir"])[:4000],
           bundle["generated_file"] or "(not found)",
           bundle["generated_target"] or "(not found)",
           json.dumps(bundle["conversion_warnings"], indent=0),
           json.dumps(bundle["unsupported_features"], indent=0),
           json.dumps({k: v for k, v in
                       bundle["deterministic_diff"].items() if v}, indent=0)))
    msg = client.messages.create(
        model=cfg.get("model"), max_tokens=1500, system=_SYSTEM,
        messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in msg.content
                   if getattr(b, "type", "") == "text").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    doc = json.loads(text)

    findings = []
    for f in (doc.get("semantic_risks") or doc.get("findings") or []):
        dim = str(f.get("dimension", ""))
        if dim not in DIMENSIONS:
            dim = "business_logic_preservation"
        findings.append({
            "dimension": dim,
            "severity": str(f.get("severity", "MEDIUM")).upper(),
            "description": str(f.get("description", ""))[:500],
            "evidence": str(f.get("evidence", ""))[:500]})
    corrections = []
    for c in doc.get("proposed_corrections", []) or []:
        if not c.get("current_code") or not c.get("file"):
            continue
        corrections.append({
            "file": str(c["file"]),
            "description": str(c.get("description", ""))[:300],
            "current_code": str(c["current_code"]),
            "proposed_code": str(c.get("proposed_code", "")),
            "rationale": str(c.get("rationale", ""))[:400],
            "status": "proposed"})
    status = str(doc.get("review_status", "")).upper()
    if status not in ("PASS", "WARNING", "MANUAL_REVIEW"):
        status = "MANUAL_REVIEW" if any(
            f["severity"] == "HIGH" for f in findings) \
            else ("WARNING" if findings else "PASS")
    return {
        "mapping": bundle["mapping"],
        "review_status": status,
        "business_logic_preserved": bool(
            doc.get("business_logic_preserved", status == "PASS")),
        "confidence": int(doc.get("confidence", 50)),
        "findings": findings,
        "semantic_risks": findings,
        "corrections": corrections,
        "reasoning_summary": str(doc.get("reasoning_summary", ""))[:1000],
    }


def _select_mappings(pipeline: Pipeline, names) -> List[Mapping]:
    if names:
        wanted = {str(n).lower() for n in names}
        return [m for m in pipeline.mappings if m.name.lower() in wanted]
    # default: everything with recorded issues, ranked by complexity, cap 5
    from ..report.complexity import score_mapping
    ranked = sorted(pipeline.mappings,
                    key=lambda m: score_mapping(m).complexity_score,
                    reverse=True)
    with_issues = [m for m in ranked if m.issues]
    return (with_issues or ranked)[:5]


def review_migration(pipeline: Pipeline, output_dir: str, target_format: str,
                     dialect: str = "", mappings=None,
                     use_ai: Optional[bool] = None) -> dict:
    """Run the review. NEVER modifies the generated output."""
    out = Path(output_dir)
    selected = _select_mappings(pipeline, mappings)

    # round-trip twins power the deterministic diff
    twins: Dict[str, Mapping] = {}
    try:
        from ..validate.conversion_validator import _reparse_output
        back = _reparse_output(out, target_format, dialect)
        if back is not None:
            for bm in back.mappings:
                twins[bm.name.lower()] = bm
    except Exception:  # noqa: BLE001
        pass

    client = cfg = None
    note = ""
    if use_ai is not False:
        try:
            from .assist import llm_available, make_client
            if llm_available():
                client, cfg = make_client()
            else:
                note = ("No AI provider configured — deterministic engine "
                        "diff only, and no corrections can be proposed. "
                        "Configure Anthropic or Bedrock in Settings.")
        except Exception as e:  # noqa: BLE001
            note = "AI provider unavailable (%s)" % str(e)[:120]
    else:
        note = "AI review disabled for this run"

    reviews: List[dict] = []
    generated_by = "rules"
    cid = 0
    for m in selected:
        bundle = build_review_bundle(pipeline, out, target_format, m,
                                     twins.get(m.name.lower()))
        review = None
        if client is not None:
            try:
                review = _agent_review(client, cfg, bundle)
                generated_by = "agent"
            except Exception as e:  # noqa: BLE001 — degrade, never die
                note = note or "agent call failed (%s) — rule-based " \
                    "findings only" % str(e)[:120]
        if review is None:
            review = _rules_only_review(bundle)
        for c in review["corrections"]:
            cid += 1
            c["id"] = "%s~%d" % (m.name, cid)
        reviews.append(review)

    by_dim = {d: 0 for d in DIMENSIONS}
    for r in reviews:
        for f in r["findings"]:
            by_dim[f["dimension"]] += 1
    return {
        "project": pipeline.name,
        "target_format": target_format,
        "generated_by": generated_by,
        "model": (cfg or {}).get("model", "") if generated_by == "agent"
        else "",
        "note": note,
        "reviews": reviews,
        "summary": {
            "review_status": ("MANUAL_REVIEW" if any(
                r.get("review_status") == "MANUAL_REVIEW" for r in reviews)
                else "WARNING" if any(
                    r.get("review_status") == "WARNING" for r in reviews)
                else "PASS"),
            "mappings_reviewed": len(reviews),
            "findings": sum(len(r["findings"]) for r in reviews),
            "findings_by_dimension": by_dim,
            "corrections_proposed": sum(len(r["corrections"])
                                        for r in reviews),
            "approval_required": "Corrections are never applied "
                                 "automatically — approve ids via "
                                 "apply_corrections / --approve / the "
                                 "console.",
        },
    }


def write_review(result: dict, output_dir: str) -> str:
    root = Path(output_dir) / "ai_review"
    root.mkdir(parents=True, exist_ok=True)
    (root / "review.json").write_text(json.dumps(result, indent=2))
    s = result["summary"]
    lines = [
        "# AI Migration Review — %s" % result["project"],
        "",
        "generated by: **%s**%s" % (
            result["generated_by"],
            " (%s)" % result["model"] if result["model"] else ""),
        "",
    ]
    if result["note"]:
        lines += ["> %s" % result["note"], ""]
    lines += ["%d finding(s) across %d mapping(s); %d proposed "
              "correction(s) awaiting approval."
              % (s["findings"], s["mappings_reviewed"],
                 s["corrections_proposed"]), ""]
    for r in result["reviews"]:
        lines += ["## %s — %s (confidence %d)"
                  % (r["mapping"],
                     "business logic preserved" if
                     r["business_logic_preserved"] else
                     "⚠ business logic AT RISK", r["confidence"]), ""]
        for f in r["findings"]:
            lines.append("- **%s** [%s] %s" % (f["severity"],
                                               f["dimension"],
                                               f["description"]))
        for c in r["corrections"]:
            lines += ["", "### Proposed correction `%s` (%s)" % (c["id"],
                                                                 c["file"]),
                      c["description"], "",
                      "```", "-- current", c["current_code"], "",
                      "-- proposed", c["proposed_code"], "```",
                      "_%s_" % c["rationale"]]
        lines.append("")
    (root / "review.md").write_text("\n".join(lines) + "\n")
    return str(root / "review.md")


# --------------------------------------------------------------------------- #
# approve & apply                                                              #
# --------------------------------------------------------------------------- #

def _file_parses(path: Path, target_format: str, dialect: str) -> bool:
    try:
        if path.suffix == ".xml":
            ET.parse(str(path))
        elif path.suffix == ".json":
            json.loads(path.read_text())
        elif path.suffix == ".sql":
            text = path.read_text()
            if target_format == "dbt":
                from ..validate.conversion_validator import _shield_jinja
                text = _shield_jinja(text)
                dia = ""
            else:
                dia = dialect or SQL_DIALECT_FORMATS.get(target_format, "")
            sqlglot.parse(text, read=dia or None,
                          error_level=sqlglot.ErrorLevel.RAISE)
        return True
    except Exception:  # noqa: BLE001
        return False


def apply_corrections(output_dir: str, approved_ids: List[str],
                      target_format: str, dialect: str = "") -> dict:
    """Apply ONLY the approved corrections. Exact-match replacement with a
    pre-image; the file must still parse afterwards or it is reverted."""
    out = Path(output_dir).resolve()
    review_file = out / "ai_review" / "review.json"
    if not review_file.exists():
        raise FileNotFoundError("No review found — run the AI review first")
    review = json.loads(review_file.read_text())
    by_id = {c["id"]: c for r in review["reviews"] for c in r["corrections"]}

    backups = out / "ai_review" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    results: List[dict] = []
    for cid in approved_ids:
        c = by_id.get(str(cid))
        if c is None:
            results.append({"id": str(cid), "status": "unknown_id"})
            continue
        target = (out / c["file"]).resolve()
        if not str(target).startswith(str(out)) or not target.exists():
            results.append({"id": c["id"], "status": "file_not_found",
                            "file": c["file"]})
            continue
        text = target.read_text()
        n = text.count(c["current_code"])
        if n == 0:
            results.append({"id": c["id"], "status": "failed_not_found",
                            "file": c["file"],
                            "detail": "current_code not present verbatim"})
            continue
        if n > 1:
            results.append({"id": c["id"], "status": "failed_ambiguous",
                            "file": c["file"],
                            "detail": "current_code occurs %d times" % n})
            continue
        archive = backups / (c["file"].replace("/", "__") + ".orig")
        if not archive.exists():
            archive.write_text(text)          # first-touch original
        pre_image = text
        target.write_text(text.replace(c["current_code"],
                                       c["proposed_code"], 1))
        if not _file_parses(target, target_format, dialect):
            target.write_text(pre_image)      # never leave broken output
            results.append({"id": c["id"], "status": "reverted_syntax_error",
                            "file": c["file"],
                            "detail": "proposed code broke the file — "
                                      "reverted automatically"})
            continue
        c["status"] = "applied"
        results.append({"id": c["id"], "status": "applied",
                        "file": c["file"]})

    review_file.write_text(json.dumps(review, indent=2))
    audit_file = out / "ai_review" / "applied.json"
    audit = json.loads(audit_file.read_text()) if audit_file.exists() else []
    audit.extend(results)
    audit_file.write_text(json.dumps(audit, indent=2))
    return {"applied": sum(1 for r in results if r["status"] == "applied"),
            "results": results}

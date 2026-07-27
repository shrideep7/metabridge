"""Conversion validation engine — five layers, one verdict.

    LAYER 1  Syntax          every generated artifact parses in its own
                             grammar (SQL per dialect, PowerCenter XML,
                             IDMC JSON, dbt Jinja-shielded SQL + YAML)
    LAYER 2  Dependency      the generated project is closed: refs resolve,
                             the DAG is acyclic, links are wired, external
                             inputs are called out
    LAYER 3  Semantics       the generated output is RE-PARSED back to IR
                             and diffed against the source IR (targets,
                             columns, keys, strategies, expression counts) —
                             plus an audit of recorded conversion issues
    LAYER 4  Reconciliation  the migration validation suite (module 13) is
                             complete and its SQL is itself valid; merge
                             pipelines without a key test are flagged
    LAYER 5  AI review       optional agent pass over the riskiest mappings
                             (semantic equivalence opinion) — advisory only,
                             deterministic layers decide the verdict; when
                             no provider is configured the layer is SKIPPED
                             and labeled, never silently green

Verdict: PASS | PASS_WITH_WARNINGS | MANUAL_REVIEW | FAIL — the worst
layer wins. Every conversion writes a Migration Validation Report
(migration_validation_report.json + .md) next to its output.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import sqlglot

import yaml

from ..ir.model import (
    IssueSeverity, LoadStrategy, Mapping, Pipeline, TransformationType,
)
from ..parsers.sql_parser import SQL_DIALECT_FORMATS

VERDICTS = ("PASS", "PASS_WITH_WARNINGS", "MANUAL_REVIEW", "FAIL")
_RANK = {v: i for i, v in enumerate(VERDICTS)}

LAYER_NAMES = (
    "syntax_validation",
    "dependency_validation",
    "semantic_transformation_validation",
    "source_target_reconciliation_validation",
    "ai_semantic_review",
)

_JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_JINJA_STMT_RE = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.DOTALL)


def _shield_jinja(sql: str) -> str:
    """Make dbt SQL parseable: inline {{ ... }} becomes an identifier,
    {% ... %} blocks vanish, and lines that were ONLY jinja (config
    headers) are dropped entirely."""
    text = _JINJA_STMT_RE.sub(" ", _JINJA_EXPR_RE.sub(" __jinja__ ", sql))
    lines = [ln for ln in text.splitlines()
             if ln.strip() and ln.replace("__jinja__", "").strip()]
    return "\n".join(lines)


def _finding(severity: str, code: str, message: str, obj: str = "",
             detail: str = "") -> dict:
    return {"severity": severity, "code": code, "message": message,
            "object": obj, "detail": detail[:400]}


def _layer_status(findings: List[dict]) -> str:
    sevs = {f["severity"] for f in findings}
    if "ERROR" in sevs:
        return "FAIL"
    if "MANUAL" in sevs:
        return "MANUAL_REVIEW"
    if "WARNING" in sevs:
        return "PASS_WITH_WARNINGS"
    return "PASS"


def _parse_sql_file(path: Path, dialect: str) -> Optional[str]:
    """None when the file parses; otherwise the error text."""
    text = path.read_text()
    if not text.strip():
        return None
    try:
        sqlglot.parse(text, read=dialect or None,
                      error_level=sqlglot.ErrorLevel.RAISE)
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


def _target_table(m: Mapping) -> str:
    tgts = m.by_type(TransformationType.TARGET)
    return str(tgts[0].properties.get("table", m.name)) if tgts else m.name


def _target_columns(m: Mapping) -> List[str]:
    tgts = m.by_type(TransformationType.TARGET)
    ports = tgts[0].ports if tgts and tgts[0].ports else []
    if not ports:
        out = m.transformation("__OUTPUT__")
        ports = out.ports if out else []
    return [p.name for p in ports]


def _expression_count(p: Pipeline) -> int:
    return sum(1 for m in p.mappings for t in m.transformations
               for port in t.ports if port.expression)


# --------------------------------------------------------------------------- #
# LAYER 1 — syntax                                                             #
# --------------------------------------------------------------------------- #

def _layer1_syntax(out: Path, target_format: str, dialect: str) -> List[dict]:
    findings: List[dict] = []

    if target_format == "powercenter":
        xmls = sorted(out.glob("wf_*.xml")) or sorted(out.glob("*.xml"))
        if not xmls:
            return [_finding("ERROR", "ARTIFACT_MISSING",
                             "No PowerCenter XML found in the output")]
        from .powercenter_validator import validate_powercenter_xml
        for xml in xmls:
            r = validate_powercenter_xml(str(xml)).to_dict()
            for f in r.get("findings", []):
                if f["severity"] in ("ERROR", "WARNING"):
                    findings.append(_finding(
                        f["severity"], f["code"], f["message"],
                        obj=f.get("location") or xml.name))

    elif target_format == "idmc":
        root = out / "idmc"
        files = sorted(root.rglob("*.json")) if root.exists() else []
        if not files:
            return [_finding("ERROR", "ARTIFACT_MISSING",
                             "No IDMC bundle found in the output")]
        for f in files:
            try:
                json.loads(f.read_text())
            except Exception as e:  # noqa: BLE001
                findings.append(_finding("ERROR", "JSON_INVALID", str(e),
                                         obj=str(f.relative_to(out))))

    elif target_format == "dbt":
        root = out / "dbt"
        sqls = sorted(root.rglob("*.sql")) if root.exists() else []
        if not sqls:
            return [_finding("ERROR", "ARTIFACT_MISSING",
                             "No dbt project found in the output")]
        for f in sqls:
            shielded = _shield_jinja(f.read_text())
            err = None
            try:
                sqlglot.parse(shielded,
                              error_level=sqlglot.ErrorLevel.RAISE)
            except Exception as e:  # noqa: BLE001
                err = str(e)
            if err:
                findings.append(_finding("ERROR", "SQL_SYNTAX", err,
                                         obj=str(f.relative_to(out))))
        for f in sorted(root.rglob("*.yml")):
            try:
                yaml.safe_load(f.read_text())
            except Exception as e:  # noqa: BLE001
                findings.append(_finding("ERROR", "YAML_INVALID", str(e),
                                         obj=str(f.relative_to(out))))

    elif target_format in SQL_DIALECT_FORMATS:
        root = out / "sql"
        files = sorted(root.rglob("*.sql")) if root.exists() else []
        if not files:
            return [_finding("ERROR", "ARTIFACT_MISSING",
                             "No SQL scripts found in the output")]
        wd = dialect or SQL_DIALECT_FORMATS.get(target_format, "")
        for f in files:
            err = _parse_sql_file(f, wd)
            if err:
                # 9x_* files are physical-design recommendations, advisory
                sev = "WARNING" if f.name[:1] == "9" else "ERROR"
                findings.append(_finding(sev, "SQL_SYNTAX", err,
                                         obj=str(f.relative_to(out))))
    return findings


# --------------------------------------------------------------------------- #
# LAYER 2 — dependencies                                                       #
# --------------------------------------------------------------------------- #

def _layer2_dependencies(pipeline: Pipeline, out: Path,
                         target_format: str) -> List[dict]:
    findings: List[dict] = []
    names = {m.name for m in pipeline.mappings}
    lower = {n.lower() for n in names}

    # DAG closure + cycles
    for m in pipeline.mappings:
        for d in m.depends_on:
            if d not in names and d.lower() not in lower:
                findings.append(_finding(
                    "ERROR", "DEP_MISSING",
                    "depends on '%s' which is not in the project" % d,
                    obj=m.name))
    deps = {m.name: [d for d in m.depends_on if d in names]
            for m in pipeline.mappings}
    done: set = set()
    remaining = set(names)
    while remaining:
        wave = {n for n in remaining if all(d in done for d in deps[n])}
        if not wave:
            findings.append(_finding(
                "ERROR", "DEP_CYCLE",
                "Circular dependency among: %s"
                % ", ".join(sorted(remaining))))
            break
        done |= wave
        remaining -= wave

    # every read resolves to a project source, another mapping's target,
    # or is an explicit external input
    produced = {_target_table(m).lower() for m in pipeline.mappings}
    declared = {s.name.lower() for s in pipeline.sources}
    for m in pipeline.mappings:
        for src in m.by_type(TransformationType.SOURCE):
            tbl = str(src.properties.get("table", src.name))
            if tbl.lower() not in produced | declared:
                findings.append(_finding(
                    "WARNING", "EXTERNAL_INPUT",
                    "reads '%s', which the project neither declares nor "
                    "produces — confirm it exists at the target" % tbl,
                    obj=m.name))
        # dataflow links must reference real transformations
        known = {t.name for t in m.transformations}
        for link in m.links:
            for endpoint in (link.from_transformation,
                             link.to_transformation):
                if endpoint not in known:
                    findings.append(_finding(
                        "ERROR", "LINK_BROKEN",
                        "link references missing transformation '%s'"
                        % endpoint, obj=m.name))

    # generated dbt project: ref()/source() must resolve
    if target_format == "dbt" and (out / "dbt").exists():
        model_names = {f.stem for f in (out / "dbt").rglob("*.sql")}
        declared_sources = set()
        for yml in (out / "dbt").rglob("*.yml"):
            try:
                doc = yaml.safe_load(yml.read_text()) or {}
            except Exception:  # noqa: BLE001
                continue
            for s in doc.get("sources", []) or []:
                for t in s.get("tables", []) or []:
                    declared_sources.add((str(s.get("name", "")).lower(),
                                          str(t.get("name", "")).lower()))
        for f in (out / "dbt").rglob("*.sql"):
            body = f.read_text()
            for ref in re.findall(r"ref\(\s*'([^']+)'\s*\)", body):
                if ref not in model_names:
                    findings.append(_finding(
                        "ERROR", "REF_UNRESOLVED",
                        "ref('%s') has no generated model" % ref,
                        obj=str(f.relative_to(out))))
            for a, b in re.findall(
                    r"source\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", body):
                if (a.lower(), b.lower()) not in declared_sources:
                    findings.append(_finding(
                        "ERROR", "SOURCE_UNDECLARED",
                        "source('%s', '%s') is not declared in any "
                        "sources.yml" % (a, b),
                        obj=str(f.relative_to(out))))
    return findings


# --------------------------------------------------------------------------- #
# LAYER 3 — semantic transformation validation (round-trip diff)               #
# --------------------------------------------------------------------------- #

def _reparse_output(out: Path, target_format: str,
                    dialect: str) -> Optional[Pipeline]:
    from ..parsers.base import get_parser
    if target_format == "powercenter":
        xmls = sorted(out.glob("wf_*.xml")) or sorted(out.glob("*.xml"))
        return get_parser("powercenter").parse_project(str(xmls[0])) \
            if xmls else None
    if target_format == "idmc":
        return get_parser("idmc").parse_project(str(out / "idmc")) \
            if (out / "idmc").exists() else None
    if target_format == "dbt":
        return get_parser("dbt").parse_project(str(out / "dbt")) \
            if (out / "dbt").exists() else None
    if target_format in SQL_DIALECT_FORMATS:
        return get_parser(target_format).parse_project(
            str(out / "sql"), dialect) if (out / "sql").exists() else None
    return None


def _layer3_semantics(pipeline: Pipeline, out: Path, target_format: str,
                      dialect: str) -> List[dict]:
    findings: List[dict] = []

    # 3a. audit of recorded conversion issues, aggregated per code
    by_code: Dict[str, List] = {}
    for i in pipeline.all_issues():
        by_code.setdefault("%s|%s" % (i.severity.value, i.code),
                           []).append(i)
    for key, items in sorted(by_code.items()):
        sev, code = key.split("|", 1)
        if sev == "INFO":
            continue
        # conversion-time ERROR means "object skipped to the manual queue" —
        # the generated artifacts are consistent, the migration is incomplete.
        # That is MANUAL_REVIEW; FAIL is reserved for broken generated output.
        if sev == "ERROR":
            sev = "MANUAL"
        sample = items[0]
        findings.append(_finding(
            sev, code,
            "%s (×%d)" % (sample.message, len(items)) if len(items) > 1
            else sample.message,
            obj=sample.obj if len(items) == 1 else
            "%d object(s)" % len({x.obj for x in items}),
            detail=sample.suggestion or sample.detail))

    # 3b. round-trip: re-parse the generated output and diff against source
    try:
        back = _reparse_output(out, target_format, dialect)
    except Exception as e:  # noqa: BLE001
        findings.append(_finding(
            "ERROR", "ROUNDTRIP_PARSE_FAILED",
            "Generated output could not be re-parsed for verification: %s"
            % e))
        return findings
    if back is None:
        findings.append(_finding(
            "ERROR", "ROUNDTRIP_PARSE_FAILED",
            "Generated output not found for re-parse verification"))
        return findings

    back_by_table = {_target_table(m).lower(): m for m in back.mappings}
    back_by_name = {m.name.lower(): m for m in back.mappings}
    dbt_plan: Dict[str, dict] = {}
    if target_format == "dbt":
        # deterministic model naming (module 27): a mapping may have been
        # decomposed into int_/dim_/fct_ models — match through the plan
        from ..generators.dbt_naming import plan_names
        dbt_plan, _ = plan_names(pipeline)
    for m in pipeline.mappings:
        if m.load_strategy == LoadStrategy.EPHEMERAL:
            continue
        table = _target_table(m).lower()
        twin = back_by_table.get(table) or back_by_name.get(m.name.lower())
        if twin is None and m.name in dbt_plan:
            p = dbt_plan[m.name]
            # prefer the logic model (explicit column list) over the thin
            # mart (select *)
            twin = back_by_name.get(p["int"].lower()) or \
                back_by_name.get((p["mart"] or "").lower())
        if twin is None:
            declared = [i for i in m.issues if i.severity in
                        (IssueSeverity.MANUAL, IssueSeverity.ERROR)]
            if declared:
                # the generator routed this mapping to the manual queue and
                # said so — declared loss is manual work, not a silent break
                findings.append(_finding(
                    "MANUAL", "TARGET_NOT_GENERATED",
                    "target '%s' was not generated — routed to the manual "
                    "queue (%s)" % (_target_table(m), declared[0].code),
                    obj=m.name, detail=declared[0].message))
            else:
                findings.append(_finding(
                    "ERROR", "TARGET_LOST",
                    "target table '%s' silently missing from the generated "
                    "output" % _target_table(m), obj=m.name))
            continue
        src_cols = {c.lower() for c in _target_columns(m)}
        twin_cols = {c.lower() for c in _target_columns(twin)}
        # the re-parser could not resolve the output schema (SELECT * or
        # opaque statement) — unverifiable is a warning, never "lost"
        unresolvable = twin_cols <= {"row_data"} or any(
            i.code == "OUTPUT_SCHEMA_UNKNOWN" for i in twin.issues)
        missing = sorted(src_cols - twin_cols)
        if missing and unresolvable:
            findings.append(_finding(
                "WARNING", "COLUMNS_UNVERIFIABLE",
                "target columns could not be verified by round-trip "
                "re-parse (the generated statement selects *) — spot-check "
                "%s" % ", ".join(missing[:6]), obj=m.name))
        elif missing and twin_cols:
            findings.append(_finding(
                "MANUAL", "COLUMNS_LOST",
                "column(s) missing from the generated target: %s"
                % ", ".join(missing), obj=m.name))
        if m.load_strategy != twin.load_strategy:
            findings.append(_finding(
                "WARNING", "STRATEGY_CHANGED",
                "load strategy %s became %s in the generated output — "
                "verify load behavior"
                % (m.load_strategy.value, twin.load_strategy.value),
                obj=m.name))
        if m.load_strategy in (LoadStrategy.MERGE,
                               LoadStrategy.DELETE_INSERT) and \
                m.unique_key and not twin.unique_key:
            findings.append(_finding(
                "MANUAL", "MERGE_KEY_NOT_PRESERVED",
                "incremental key (%s) is not visible in the generated "
                "output — incremental matching must be verified manually"
                % ", ".join(m.unique_key), obj=m.name))

    # 3c. expression fidelity — loose count comparison, warning only
    src_exprs, back_exprs = _expression_count(pipeline), \
        _expression_count(back)
    if src_exprs and back_exprs < src_exprs * 0.5:
        findings.append(_finding(
            "WARNING", "EXPRESSION_FIDELITY",
            "source has %d derived expressions but only %d are visible "
            "in the generated output — some logic may have been folded "
            "or dropped; review the riskiest mappings"
            % (src_exprs, back_exprs)))
    return findings


# --------------------------------------------------------------------------- #
# LAYER 4 — reconciliation suite validation                                    #
# --------------------------------------------------------------------------- #

def _layer4_reconciliation(pipeline: Pipeline, out: Path,
                           target_format: str) -> List[dict]:
    findings: List[dict] = []
    tests_file = out / "validation_tests" / "tests.json"
    if tests_file.exists():
        doc = json.loads(tests_file.read_text())
    else:
        from ..report.testgen import generate_tests
        doc = generate_tests(pipeline, target_format=target_format)

    core = {"row_count", "checksum_comparison", "column_level_comparison",
            "schema_comparison"}
    strategies = {m.name: m.load_strategy for m in pipeline.mappings}
    for pm in doc["mappings"]:
        if pm.get("skipped"):
            continue
        types = {t["test_type"] for t in pm["tests"]}
        gap = core - types
        if gap:
            findings.append(_finding(
                "WARNING", "RECON_COVERAGE_GAP",
                "reconciliation suite lacks %s" % ", ".join(sorted(gap)),
                obj=pm["mapping"]))
        strat = strategies.get(pm["mapping"])
        if strat in (LoadStrategy.MERGE, LoadStrategy.DELETE_INSERT,
                     LoadStrategy.SCD2) and "pk_uniqueness" not in types:
            findings.append(_finding(
                "MANUAL", "MERGE_UNVERIFIABLE",
                "%s load without a unique-key test — merge correctness "
                "cannot be verified; declare a unique key"
                % strat.value, obj=pm["mapping"]))
        for u in pm.get("untestable_rules", []):
            findings.append(_finding(
                "WARNING", "RULE_UNTESTABLE", u["reason"],
                obj=pm["mapping"]))
        for t in pm["tests"]:
            for lim in t.get("limitations", []) or []:
                findings.append(_finding("WARNING", "RECON_LIMITATION",
                                         lim, obj=t["name"]))

    # our own reconciliation SQL must parse in its declared dialects
    sp = SQL_DIALECT_FORMATS.get(doc.get("source_platform", ""), "")
    tp = SQL_DIALECT_FORMATS.get(doc.get("target_platform", ""), "")
    for pm in doc["mappings"]:
        for t in pm.get("tests", []):
            for key, dia in (("source_sql", sp), ("target_sql", tp)):
                sql = t.get(key)
                if not sql:
                    continue
                sql = re.sub(r"\{\{[^}]+\}\}", "_mb_placeholder", sql)
                try:
                    sqlglot.parse(sql, read=dia or None,
                                  error_level=sqlglot.ErrorLevel.RAISE)
                except Exception as e:  # noqa: BLE001
                    findings.append(_finding(
                        "ERROR", "RECON_SQL_INVALID",
                        "generated validation SQL does not parse: %s" % e,
                        obj=t["name"]))
    return findings


# --------------------------------------------------------------------------- #
# LAYER 5 — AI semantic review                                                 #
# --------------------------------------------------------------------------- #

_AI_SYSTEM = (
    "You are reviewing a data pipeline migration. Given the ORIGINAL "
    "definition and the CONVERTED artifact, judge whether they are "
    "semantically equivalent (same rows, same values, same load behavior). "
    "Answer with JSON only: {\"equivalent\": true|false, "
    "\"confidence\": 0-100, \"concerns\": [\"...\"]}. Be skeptical; list "
    "concrete concerns, not compliments.")


def _generated_snippet(out: Path, target_format: str, m: Mapping) -> str:
    if target_format == "dbt":
        hits = list((out / "dbt").rglob("%s.sql" % m.name)) or \
            list((out / "dbt").rglob("int_*%s*.sql" % m.name)) or \
            list((out / "dbt").rglob("*%s*.sql" % m.name))
        if hits:
            return hits[0].read_text()[:2500]
    if target_format in SQL_DIALECT_FORMATS:
        hits = sorted((out / "sql").rglob("*%s*.sql" % m.name))
        if hits:
            return hits[0].read_text()[:2500]
    # PowerCenter / IDMC: a structural summary reads better than raw XML
    lines = []
    for t in m.transformations:
        if t.name == "__OUTPUT__":
            continue
        props = {k: str(v)[:80] for k, v in t.properties.items()
                 if k in ("condition", "join_type", "group_by", "table",
                          "sql_override")}
        exprs = ["%s = %s" % (p.name, p.expression)
                 for p in t.ports if p.expression][:6]
        lines.append("%s (%s) %s %s" % (t.name, t.type.value, props,
                                        exprs or ""))
    return "\n".join(lines)[:2500]


def _layer5_ai(pipeline: Pipeline, out: Path, target_format: str,
               use_ai: Optional[bool]) -> tuple:
    """-> (findings, status_override, note)"""
    if use_ai is False:
        return [], "SKIPPED", "AI review disabled for this run"
    try:
        from ..llm.assist import llm_available, make_client
        if not llm_available():
            return [], "SKIPPED", ("No AI provider configured — layers 1-4 "
                                   "decide the verdict. Configure Anthropic "
                                   "or Bedrock in Settings to enable the "
                                   "agent review.")
        from ..report.complexity import score_mapping
        ranked = sorted(pipeline.mappings,
                        key=lambda m: score_mapping(m).complexity_score,
                        reverse=True)[:3]
        client, cfg = make_client()
        findings: List[dict] = []
        reviewed = []
        for m in ranked:
            origin = (m.origin or "").strip()[:2500]
            if not origin:
                continue
            msg = client.messages.create(
                model=cfg.get("model"), max_tokens=500, system=_AI_SYSTEM,
                messages=[{"role": "user", "content":
                           "ORIGINAL (%s):\n%s\n\nCONVERTED (%s):\n%s"
                           % (pipeline.source_format, origin, target_format,
                              _generated_snippet(out, target_format, m))}])
            text = "".join(b.text for b in msg.content
                           if getattr(b, "type", "") == "text").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            verdict = json.loads(text)
            reviewed.append(m.name)
            if not verdict.get("equivalent", True):
                findings.append(_finding(
                    "MANUAL", "AI_EQUIVALENCE_DOUBT",
                    "agent review (confidence %s) doubts semantic "
                    "equivalence" % verdict.get("confidence", "?"),
                    obj=m.name,
                    detail="; ".join(str(c) for c in
                                     verdict.get("concerns", []))[:400]))
            else:
                for c in verdict.get("concerns", [])[:3]:
                    findings.append(_finding("WARNING", "AI_CONCERN",
                                             str(c), obj=m.name))
        note = "agent reviewed %d highest-complexity mapping(s): %s" \
            % (len(reviewed), ", ".join(reviewed)) if reviewed else \
            "no mappings had original SQL to review"
        return findings, None, note
    except Exception as e:  # noqa: BLE001 — advisory layer, never blocks
        return [], "SKIPPED", ("AI review unavailable (%s) — layers 1-4 "
                               "decide the verdict" % str(e)[:120])


# --------------------------------------------------------------------------- #
# public API                                                                   #
# --------------------------------------------------------------------------- #

def validate_conversion(pipeline: Pipeline, output_dir: str,
                        target_format: str, dialect: str = "",
                        use_ai: Optional[bool] = None) -> dict:
    """Run all five layers against a finished conversion output."""
    out = Path(output_dir)
    layers: List[dict] = []

    def add(name: str, findings: List[dict], status: Optional[str] = None,
            note: str = "") -> None:
        layers.append({
            "layer": len(layers) + 1, "name": name,
            "status": status or _layer_status(findings),
            "findings": findings, "note": note,
            "errors": sum(1 for f in findings if f["severity"] == "ERROR"),
            "manual": sum(1 for f in findings if f["severity"] == "MANUAL"),
            "warnings": sum(1 for f in findings
                            if f["severity"] == "WARNING"),
        })

    add("syntax_validation",
        _layer1_syntax(out, target_format, dialect))
    add("dependency_validation",
        _layer2_dependencies(pipeline, out, target_format))
    add("semantic_transformation_validation",
        _layer3_semantics(pipeline, out, target_format, dialect))
    add("source_target_reconciliation_validation",
        _layer4_reconciliation(pipeline, out, target_format))
    ai_findings, ai_status, ai_note = _layer5_ai(pipeline, out,
                                                 target_format, use_ai)
    add("ai_semantic_review", ai_findings, status=ai_status, note=ai_note)

    verdict = "PASS"
    for layer in layers:
        if layer["status"] in _RANK and \
                _RANK[layer["status"]] > _RANK[verdict]:
            verdict = layer["status"]

    return {
        "project": pipeline.name,
        "source_format": pipeline.source_format,
        "target_format": target_format,
        "verdict": verdict,
        "ai_reviewed": layers[-1]["status"] not in ("SKIPPED",),
        "layers": layers,
        "totals": {
            "errors": sum(l["errors"] for l in layers),
            "manual": sum(l["manual"] for l in layers),
            "warnings": sum(l["warnings"] for l in layers),
        },
    }


_VERDICT_BADGE = {
    "PASS": "✅ PASS",
    "PASS_WITH_WARNINGS": "🟡 PASS WITH WARNINGS",
    "MANUAL_REVIEW": "🟠 MANUAL REVIEW",
    "FAIL": "🔴 FAIL",
}


def write_validation_report(result: dict, out_dir: str) -> str:
    """Write migration_validation_report.json + .md into the output."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "migration_validation_report.json").write_text(
        json.dumps(result, indent=2))

    t = result["totals"]
    lines = [
        "# Migration Validation Report — %s" % result["project"],
        "",
        "**Verdict: %s**" % _VERDICT_BADGE.get(result["verdict"],
                                               result["verdict"]),
        "",
        "%s → %s | errors: %d | manual: %d | warnings: %d"
        % (result["source_format"] or "?", result["target_format"],
           t["errors"], t["manual"], t["warnings"]),
        "",
        "| # | layer | status | errors | manual | warnings |",
        "|---|---|---|---|---|---|",
    ]
    for L in result["layers"]:
        lines.append("| %d | %s | %s | %d | %d | %d |"
                     % (L["layer"], L["name"].replace("_", " "),
                        L["status"], L["errors"], L["manual"],
                        L["warnings"]))
    for L in result["layers"]:
        if not L["findings"] and not L["note"]:
            continue
        lines += ["", "## Layer %d — %s (%s)"
                  % (L["layer"], L["name"].replace("_", " "), L["status"])]
        if L["note"]:
            lines.append("_%s_" % L["note"])
        for f in L["findings"]:
            lines.append("- **%s** `%s` %s%s%s"
                         % (f["severity"], f["code"],
                            ("[%s] " % f["object"]) if f["object"] else "",
                            f["message"],
                            (" — %s" % f["detail"]) if f["detail"] else ""))
    path = out / "migration_validation_report.md"
    path.write_text("\n".join(lines) + "\n")
    return str(path)

"""AI business logic explainer.

For every pipeline, produce documentation a business analyst can read:
technical summary, business-logic summary, source/target systems,
transformation rules, filters, joins, aggregations, data-quality rules,
dependencies, and potential risks.

Two layers, by design:

  * **Fact extraction + rule-based narrative** — deterministic, offline,
    always available. Conditions, joins and aggregations are translated into
    business English ("keeps records with a value for email", "enriched with
    order history", "summarized per region") — semantic intent, never SQL
    echoes.
  * **AI enhancement** — when an AI provider is configured (Settings /
    Bedrock / ANTHROPIC_API_KEY), the meta-bridge agent rewrites the two
    summaries from the structured facts (it never sees raw SQL dumps).
    Output is labeled ``generated_by: agent``; any failure falls back to the
    rule-based narrative silently and safely.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from ..ir.model import LoadStrategy, Mapping, Pipeline, TransformationType
from .complexity import score_mapping

# ---------------------------------------------------------------------------
# Business-English helpers (semantic intent, not SQL)
# ---------------------------------------------------------------------------

_NOT_NULL_RE = re.compile(r"^(?:NOT\s+(\w+)\s+IS\s+NULL|(\w+)\s+IS\s+NOT\s+NULL)$",
                          re.IGNORECASE)
_IS_NULL_RE = re.compile(r"^(\w+)\s+IS\s+NULL$", re.IGNORECASE)
_EQ_RE = re.compile(r"^(\w+)\s*=\s*'([^']*)'$")
_CMP_RE = re.compile(r"^(\w+)\s*(>=|<=|>|<)\s*(.+)$")
_IN_RE = re.compile(r"^(\w+)\s+IN\s+\((.+)\)$", re.IGNORECASE)


def humanize_condition(cond: str) -> str:
    c = (cond or "").strip()
    if "$$" in c or ":LAST_RUN" in c.upper():
        col = c.split(">")[0].strip() if ">" in c else "the change timestamp"
        return "processes only records changed since the last run " \
               "(watermark on %s)" % col
    m = _NOT_NULL_RE.match(c)
    if m:
        col = m.group(1) or m.group(2)
        return "keeps records that have a value for %s" % col
    m = _IS_NULL_RE.match(c)
    if m:
        return "keeps records missing %s" % m.group(1)
    m = _EQ_RE.match(c)
    if m:
        return "keeps records where %s is '%s'" % (m.group(1), m.group(2))
    m = _CMP_RE.match(c)
    if m:
        word = {">": "exceeds", ">=": "is at least",
                "<": "is below", "<=": "is at most"}[m.group(2)]
        return "keeps records where %s %s %s" % (m.group(1), word,
                                                 m.group(3).strip())
    m = _IN_RE.match(c)
    if m:
        return "keeps records where %s is one of %s" % (m.group(1),
                                                        m.group(2))
    return "keeps records satisfying: %s" % c


_JOIN_PHRASE = {"LEFT": "enriched (where available) with",
                "INNER": "matched with",
                "RIGHT": "matched against",
                "FULL": "combined with"}

_STRATEGY_PHRASE = {
    LoadStrategy.FULL: "fully rebuilds the target on every run",
    LoadStrategy.VIEW: "is exposed as a view (no data copy)",
    LoadStrategy.APPEND: "appends new records incrementally",
    LoadStrategy.MERGE: "incrementally upserts changed records",
    LoadStrategy.DELETE_INSERT: "replaces changed partitions incrementally",
    LoadStrategy.SCD2: "maintains full change history (SCD Type 2)",
    LoadStrategy.EPHEMERAL: "is computed inline by downstream models",
}


# ---------------------------------------------------------------------------
# Fact extraction (deterministic)
# ---------------------------------------------------------------------------

def _facts(m: Mapping, pipeline: Pipeline) -> dict:
    sources = [{"table": str(t.properties.get("table", t.name)),
                "schema": str(t.properties.get("schema", "") or ""),
                "database": str(t.properties.get("database", "") or "")}
               for t in m.by_type(TransformationType.SOURCE)]
    targets = [{"table": str(t.properties.get("table", t.name)),
                "load": m.load_strategy.value, "unique_key": m.unique_key}
               for t in m.by_type(TransformationType.TARGET)]

    filters, joins, aggregations, rules = [], [], [], []
    for t in m.transformations:
        if t.name == "__OUTPUT__":
            continue
        if t.type == TransformationType.FILTER:
            cond = str(t.properties.get("condition", ""))
            filters.append({"condition": cond,
                            "meaning": humanize_condition(cond)})
        elif t.type == TransformationType.JOINER:
            jt = str(t.properties.get("join_type", "INNER"))
            right = str(t.properties.get("right", "another input"))
            # resolve internal node names (SQ_x) to the business relation
            rt = m.transformation(right)
            while rt is not None and rt.type in (
                    TransformationType.SOURCE_QUALIFIER,):
                ups = m.upstream_of(rt.name)
                rt = ups[0] if ups else None
            if rt is not None and rt.type == TransformationType.SOURCE:
                right = str(rt.properties.get("table", right))
            elif right.startswith("SQ_"):
                right = right[3:].rstrip("_0123456789")
            joins.append({"join_type": jt,
                          "condition": str(t.properties.get("condition", "")),
                          "meaning": "%s %s" % (
                              _JOIN_PHRASE.get(jt, "joined with"), right)})
        elif t.type == TransformationType.AGGREGATOR:
            group = [str(g) for g in t.properties.get("group_by", [])]
            measures = [{"column": p.name, "expression": p.expression}
                        for p in t.ports if p.expression]
            aggregations.append({
                "group_by": group,
                "measures": measures,
                "meaning": "summarized per %s producing %s" % (
                    ", ".join(group) or "the full set",
                    ", ".join(mm["column"] for mm in measures) or "counts")})
        for p in t.ports:
            if p.expression:
                rules.append({"column": p.name, "expression": p.expression,
                              "transformation": t.name})

    dq = [i.message.split("'")[1] if "'" in i.message else i.message
          for i in m.issues if i.code == "DBT_TEST"]
    risks = score_mapping(m).migration_risks
    return {"sources": sources, "targets": targets, "filters": filters,
            "joins": joins, "aggregations": aggregations,
            "transformation_rules": rules, "data_quality_rules": dq,
            "dependencies": list(m.depends_on), "risks": risks}


# ---------------------------------------------------------------------------
# Rule-based narrative
# ---------------------------------------------------------------------------

def _rule_based_summaries(m: Mapping, facts: dict,
                          pipeline: Pipeline) -> Dict[str, str]:
    src_names = [s["table"] for s in facts["sources"]]
    dep_names = facts["dependencies"]
    reads = src_names + [d for d in dep_names if d not in src_names]
    platform = pipeline.source_format or "the source system"

    parts: List[str] = []
    if reads:
        parts.append("This pipeline reads %s from %s" %
                     (", ".join(reads[:4]) + (" and others" if len(reads) > 4
                                              else ""), platform))
    for f in facts["filters"]:
        parts.append(f["meaning"])
    for j in facts["joins"]:
        parts.append(j["meaning"])
    for a in facts["aggregations"]:
        parts.append(a["meaning"])
    tgt = facts["targets"][0]["table"] if facts["targets"] else m.name
    parts.append("and loads %s, which %s" %
                 (tgt, _STRATEGY_PHRASE.get(m.load_strategy, "is loaded")))
    business = ". ".join([parts[0]] +
                         [p for p in parts[1:-1]]) + ", " + parts[-1] + "." \
        if len(parts) > 1 else parts[0] + "."
    business = business.replace("..", ".")

    tx_count = len([t for t in m.transformations if t.name != "__OUTPUT__"])
    technical = ("%d-step dataflow: %d source(s), %d filter(s), %d join(s), "
                 "%d aggregation(s), %d derived column(s); %s load%s."
                 % (tx_count, len(facts["sources"]), len(facts["filters"]),
                    len(facts["joins"]), len(facts["aggregations"]),
                    len(facts["transformation_rules"]),
                    m.load_strategy.value,
                    " keyed on %s" % ", ".join(m.unique_key)
                    if m.unique_key else ""))
    return {"technical_summary": technical, "business_logic_summary": business}


# ---------------------------------------------------------------------------
# AI enhancement (the meta-bridge agent)
# ---------------------------------------------------------------------------

_AGENT_SYSTEM = """You are MetaBridge AI's documentation agent. You receive
structured facts about one data pipeline (sources, filters with plain-English
meanings, joins, aggregations, load strategy, data-quality rules).

Write JSON with exactly two keys:
  technical_summary: 2-3 sentences for a data engineer (structure and flow).
  business_logic_summary: 2-3 sentences for a business stakeholder explaining
    WHAT the pipeline achieves and WHY it matters — plain language, no SQL,
    no column-by-column repetition, semantic intent only.

Return ONLY the JSON object."""


def _agent_summaries(name: str, facts: dict) -> Optional[Dict[str, str]]:
    try:
        from ..llm.assist import llm_available, make_client
        if not llm_available():
            return None
        client, cfg = make_client()
        msg = client.messages.create(
            model=cfg.get("model"), max_tokens=600, system=_AGENT_SYSTEM,
            messages=[{"role": "user", "content":
                       "Pipeline: %s\nFacts:\n%s"
                       % (name, json.dumps(facts, indent=1)[:6000])}])
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        doc = json.loads(text)
        if doc.get("technical_summary") and doc.get("business_logic_summary"):
            return {"technical_summary": str(doc["technical_summary"]),
                    "business_logic_summary": str(doc["business_logic_summary"])}
    except Exception:  # noqa: BLE001 — the agent must never break docs
        return None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def explain_mapping(m: Mapping, pipeline: Pipeline,
                    use_ai: bool = False) -> dict:
    facts = _facts(m, pipeline)
    summaries = _rule_based_summaries(m, facts, pipeline)
    generated_by = "rules"
    if use_ai:
        ai = _agent_summaries(m.name, facts)
        if ai:
            summaries = ai
            generated_by = "agent"
    return {
        "pipeline": m.name,
        "generated_by": generated_by,
        "technical_summary": summaries["technical_summary"],
        "business_logic_summary": summaries["business_logic_summary"],
        "source_systems": facts["sources"],
        "target_systems": facts["targets"],
        "transformation_rules": facts["transformation_rules"],
        "filters": facts["filters"],
        "joins": facts["joins"],
        "aggregations": facts["aggregations"],
        "data_quality_rules": facts["data_quality_rules"],
        "dependencies": facts["dependencies"],
        "potential_risks": facts["risks"],
    }


def explain_pipeline(pipeline: Pipeline, use_ai: Optional[bool] = None,
                     max_ai_pipelines: int = 25) -> dict:
    if use_ai is None:
        from ..llm.assist import llm_available
        use_ai = llm_available()
    docs = []
    for i, m in enumerate(pipeline.mappings):
        docs.append(explain_mapping(m, pipeline,
                                    use_ai=use_ai and i < max_ai_pipelines))
    return {"project": pipeline.name,
            "source_platform": pipeline.source_format,
            "pipelines": docs,
            "ai_used": any(d["generated_by"] == "agent" for d in docs)}


def write_documentation(result: dict, out_dir: str) -> str:
    """Markdown deliverable: one section per pipeline."""
    from pathlib import Path
    lines = ["# Pipeline documentation — %s" % result["project"],
             "",
             "_Generated by MetaBridge AI from the %s project. Narratives "
             "labeled 'agent' were drafted by the meta-bridge agent and "
             "should be reviewed._" % (result["source_platform"] or "source"),
             ""]
    for d in result["pipelines"]:
        lines += ["## %s" % d["pipeline"], "",
                  "**Business logic** (%s): %s" % (d["generated_by"],
                                                   d["business_logic_summary"]),
                  "",
                  "**Technical**: %s" % d["technical_summary"], ""]
        if d["source_systems"]:
            lines.append("**Sources**: " + ", ".join(
                "%s.%s" % (s["schema"], s["table"]) if s["schema"] else s["table"]
                for s in d["source_systems"]))
        if d["dependencies"]:
            lines.append("**Depends on**: " + ", ".join(d["dependencies"]))
        if d["target_systems"]:
            t = d["target_systems"][0]
            lines.append("**Target**: %s (%s%s)" % (
                t["table"], t["load"],
                ", key: " + ", ".join(t["unique_key"]) if t["unique_key"] else ""))
        if d["filters"]:
            lines.append("**Filters**:")
            lines += ["- %s" % f["meaning"] for f in d["filters"]]
        if d["joins"]:
            lines.append("**Joins**:")
            lines += ["- %s (%s)" % (j["meaning"], j["join_type"])
                      for j in d["joins"]]
        if d["aggregations"]:
            lines.append("**Aggregations**:")
            lines += ["- %s" % a["meaning"] for a in d["aggregations"]]
        if d["data_quality_rules"]:
            lines.append("**Data quality**: " +
                         ", ".join(d["data_quality_rules"]))
        if d["potential_risks"]:
            lines.append("**Potential risks**:")
            lines += ["- %s" % r for r in d["potential_risks"]]
        lines.append("")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "pipeline_documentation.md"
    path.write_text("\n".join(lines) + "\n")
    return str(path)

"""SQL Transformation handler (Phase 2, module 20).

Detects active/passive, connected, SCRIPT vs QUERY mode, parses the query
into the AST and extracts inputs, outputs, parameters and dynamic SQL:

    ?port?   parameter BINDING — the query structure is static; shielded,
             parsed, tables extracted, convertible with review
    ~port~   STRING SUBSTITUTION — the query STRUCTURE changes at
             runtime: true dynamic SQL

Dynamic SQL contract (per spec):
    * requires_manual_review = true — always
    * Claude explains the dynamic construction logic when an AI provider
      is configured (labeled generated_by=agent; silent rules fallback
      offline) — the explanation covers what varies and what stays fixed
    * dynamic SQL is NEVER executed — analysis is static only
"""
from __future__ import annotations

import re
from typing import Dict, List

from ..ir.model import IssueSeverity, Mapping, Port
from .pc_source_qualifier import normalize_override
from .pc_stored_procedure import classify_procedure_sql

_PARAM_RE = re.compile(r"\?(\w+)\?")
_SUBST_RE = re.compile(r"~(\w+)~")


def _explain_dynamic(template: str, substitutions: List[str],
                     inputs: List[str]) -> dict:
    """Two-layer explanation of the dynamic construction logic. The
    template is ONLY read — never executed."""
    rules_text = (
        "The query template substitutes port value(s) %s directly into "
        "the SQL text, so the statement STRUCTURE (table names, "
        "predicates, or clauses) is decided per row at runtime. Static "
        "analysis can validate only the fixed skeleton; the runtime "
        "variants must be enumerated from the data feeding %s."
        % (", ".join("~%s~" % s for s in substitutions) or "(query port)",
           ", ".join(inputs) or "the input ports"))
    try:
        from ..llm.assist import llm_available, make_client
        if llm_available():
            client, cfg = make_client()
            msg = client.messages.create(
                model=cfg.get("model"), max_tokens=400,
                system="You explain dynamic SQL construction logic for a "
                       "migration review. Describe what parts of the "
                       "statement vary at runtime and what the intent "
                       "is. NEVER execute or complete the SQL. Plain "
                       "prose, max 5 sentences.",
                messages=[{"role": "user", "content":
                           "Template:\n%s\n\nSubstitution ports: %s\n"
                           "Input ports: %s"
                           % (template[:1500],
                              ", ".join(substitutions), ", ".join(inputs))}])
            text = "".join(b.text for b in msg.content
                           if getattr(b, "type", "") == "text").strip()
            if text:
                return {"generated_by": "agent", "text": text}
    except Exception:  # noqa: BLE001 — agent optional, never blocking
        pass
    return {"generated_by": "rules", "text": rules_text}


def enrich_sql_transformation(mapping: Mapping, iname: str,
                              props: Dict[str, object],
                              attrs: Dict[str, str],
                              ports: List[Port]) -> None:
    query = (attrs.get("SQL Query") or attrs.get("Sql Query") or "").strip()
    script_mode = (attrs.get("Script Mode") or "NO").upper() == "YES" or \
        any(p.name.lower() in ("scriptname", "scriptresult", "scripterror")
            for p in ports)
    active = (attrs.get("Active") or attrs.get("Is Active") or
              "NO").upper() == "YES"

    inputs = [p.name for p in ports
              if (p.direction or "").upper() in ("INPUT", "INPUT_OUTPUT")]
    outputs = [p.name for p in ports
               if (p.direction or "").upper() in ("OUTPUT", "INPUT_OUTPUT")]
    parameters = sorted(set(_PARAM_RE.findall(query)))
    substitutions = sorted(set(_SUBST_RE.findall(query)))
    dynamic = bool(substitutions) or (not query and not script_mode)

    cir: Dict[str, object] = {
        "mode": "SCRIPT" if script_mode else "QUERY",
        "active": active,
        "connected": True,               # resolved in the post-pass
        "query_template": query,
        "static": not dynamic and not script_mode,
        "inputs": inputs,
        "outputs": outputs,
        "parameters": parameters,
        "substitution_ports": substitutions,
        "requires_manual_review": True,  # narrowed below for static SELECTs
        "ast": None,
    }

    if script_mode:
        mapping.add_issue(
            IssueSeverity.MANUAL, "SQLT_SCRIPT_MODE",
            "SQL transformation '%s' runs EXTERNAL SCRIPT FILES per row — "
            "the scripts are outside the repository export" % iname,
            suggestion="Collect the script files and port them to the "
                       "target (Databricks notebook/workflow task; dbt "
                       "operation) — per-row script execution has no "
                       "set-based equivalent.")
    elif dynamic:
        explanation = _explain_dynamic(query, substitutions, inputs)
        cir["dynamic_explanation"] = explanation
        mapping.add_issue(
            IssueSeverity.MANUAL, "SQLT_DYNAMIC",
            "SQL transformation '%s' builds DYNAMIC SQL (%s) — flagged "
            "for manual review; the dynamic SQL was analyzed statically, "
            "NEVER executed" % (
                iname,
                "string substitution: %s" % ", ".join(
                    "~%s~" % s for s in substitutions)
                if substitutions else "query constructed at runtime"),
            detail="[%s] %s" % (explanation["generated_by"],
                                explanation["text"][:300]),
            suggestion="Enumerate the runtime variants and generate one "
                       "model/statement per variant, or re-express the "
                       "varying part as a parameter (WHERE col = ?) "
                       "instead of substituted SQL.")
    else:
        shielded = _PARAM_RE.sub(lambda m: ":%s" % m.group(1), query)
        ast_info = normalize_override(shielded)
        cir["ast"] = ast_info
        if not ast_info["parsed"]:
            mapping.add_issue(
                IssueSeverity.MANUAL, "SQLT_QUERY_UNPARSEABLE",
                "SQL transformation '%s' query does not parse" % iname,
                detail="%s | %s" % (query[:150],
                                    ast_info.get("error", "")))
        else:
            classes = classify_procedure_sql(shielded)["classes"]
            cir["classification"] = classes
            if classes == ["business_transformation"] or \
                    ast_info["statements"] == 1 and "DML" not in classes \
                    and "DDL" not in classes:
                cir["requires_manual_review"] = False
                mapping.add_issue(
                    IssueSeverity.WARNING, "SQLT_STATIC_QUERY",
                    "SQL transformation '%s' runs a static %sSELECT per "
                    "row (tables: %s) — convertible to a JOIN/CTE"
                    % (iname, "parameterized " if parameters else "",
                       ", ".join(ast_info["tables"]) or "?"),
                    suggestion="Replace the per-row query with a join to "
                               "(%s) on the parameter column(s) %s."
                    % (", ".join(ast_info["tables"]) or "the query",
                       ", ".join(parameters) or "(none)"))
            else:
                mapping.add_issue(
                    IssueSeverity.MANUAL, "SQLT_PER_ROW_DML",
                    "SQL transformation '%s' executes %s per input row "
                    "(%s) — per-row side effects have no set-based "
                    "equivalent" % (iname, "/".join(classes),
                                    query[:80]),
                    suggestion="Re-express as one set-based statement "
                               "over the input rows (INSERT..SELECT / "
                               "MERGE), or move to an orchestrated task.")

    props["sql_transformation_cir"] = cir


def apply_sql_transformation_semantics(mapping: Mapping) -> None:
    """Post-pass: resolve the connected flag from actual links."""
    for t in mapping.transformations:
        cir = t.properties.get("sql_transformation_cir")
        if not cir:
            continue
        wired = any(t.name in (l.from_transformation, l.to_transformation)
                    for l in mapping.links)
        cir["connected"] = wired
        if not wired:
            mapping.add_issue(
                IssueSeverity.WARNING, "SQLT_UNCONNECTED",
                "SQL transformation '%s' has no dataflow connections — "
                "it may be invoked for side effects only" % t.name)

"""Temp-table chain analysis (Phase 3, section 13).

Legacy pipelines stage work through temp objects (Oracle GTTs, SQL
Server #temp, Teradata volatile tables). After ingestion each temp
object is a mapping flagged temporary; this pass builds the dependency
chains

    temp_1 -> temp_2 -> final_table

and picks a modernization strategy per chain from reuse count and
depth — never flattening blindly:

    reused by 2+ consumers  materialize once (transient table /
                            intermediate model / temp view)
    depth >= 3              staged models (dbt) / stepwise scripts —
                            keep the stages reviewable
    single-use shallow      fold into the consumer as a CTE where the
                            target favors it (still generated as a
                            model/table by default: correctness first)

A temp object written by more than one statement is procedural state —
declared, never silently flattened.
"""
from __future__ import annotations

from typing import Dict, List

from ..ir.model import IssueSeverity, Pipeline


def analyze_temp_chains(pipeline: Pipeline) -> None:
    temps = {str(t).lower() for t in
             pipeline.metadata.get("temp_objects", [])}
    if not temps:
        return
    by_name = {m.name.lower(): m for m in pipeline.mappings}

    # multiple writers to one temp object = procedural state
    writer_counts: Dict[str, int] = {}
    for m in pipeline.mappings:
        writer_counts[m.name.lower()] = \
            writer_counts.get(m.name.lower(), 0) + 1
    for t in sorted(temps):
        if writer_counts.get(t, 0) > 1:
            pipeline.issues.append(__import__(
                "metabridge.ir.model", fromlist=["ConversionIssue"]
            ).ConversionIssue(
                severity=IssueSeverity.WARNING,
                code="TEMP_PROCEDURAL_STATE",
                message="Temp object '%s' is written by %d statements — "
                        "it carries procedural state and must not be "
                        "flattened into a single model" %
                        (t, writer_counts[t]),
                suggestion="Keep the write steps as separate staged "
                           "models/statements in original order."))

    dependents: Dict[str, List[str]] = {t: [] for t in temps}
    for m in pipeline.mappings:
        for d in m.depends_on:
            if d.lower() in temps:
                dependents[d.lower()].append(m.name)

    for t in sorted(temps):
        m = by_name.get(t)
        if m is None:
            continue
        m.properties["temporary_object"] = True
        reuse = len(dependents.get(t, []))
        if reuse >= 2:
            strategy = ("materialize ONCE and share: Snowflake transient "
                        "table, Databricks temp view/Delta staging, dbt "
                        "intermediate model — %d consumers" % reuse)
        else:
            strategy = ("single consumer: staged model (dbt), temp view "
                        "(Databricks), transient table (Snowflake); a "
                        "CTE fold is possible but kept explicit for "
                        "reviewability")
        m.add_issue(IssueSeverity.INFO, "TEMP_OBJECT",
                    "'%s' was a temporary object (volatile/#temp/GTT) — "
                    "modernized as a first-class staged relation"
                    % m.name, suggestion=strategy)

    # chains: final (non-temp) mapping <- its transitive temp ancestors
    chains: List[dict] = []
    for m in pipeline.mappings:
        if m.name.lower() in temps:
            continue
        order: List[str] = []
        stack = list(m.depends_on)
        seen = set()
        while stack:
            d = stack.pop()
            dl = d.lower()
            if dl in seen:
                continue
            seen.add(dl)
            if dl in temps:
                order.append(d)
                dm = by_name.get(dl)
                if dm is not None:
                    stack.extend(dm.depends_on)
        if not order:
            continue
        depth = len(order)
        chains.append({
            "final": m.name,
            "temp_chain": list(reversed(order)),
            "depth": depth,
            "reuse": {t: len(dependents.get(t.lower(), []))
                      for t in order},
            "strategy": "staged models in dependency order"
            if depth >= 3 else "staged model(s); CTE fold optional",
        })
        m.add_issue(IssueSeverity.INFO, "TEMP_CHAIN",
                    "'%s' is fed by a %d-step temp chain: %s"
                    % (m.name, depth,
                       " -> ".join(reversed(order)) + " -> " + m.name),
                    suggestion="Converted as staged relations in "
                               "dependency order (see temp_table_chains "
                               "in the report).")
    if chains:
        pipeline.metadata["temp_table_chains"] = chains

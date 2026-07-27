"""Aggregator transformation handler (Phase 2, module 11).

Parses GROUP BY ports, aggregate expressions, Sorted Input and (via the
session) Incremental Aggregation into a CIR AGGREGATOR contract:

    props["aggregator_cir"] = {
        "group_by":   [...ports...],
        "aggregates": [{"port", "function", "expression"}],
        "computed":   [non-aggregate derived ports],
        "sorted_input": bool,
    }

Function support (converted semantically by sqlx.expressions):
SUM, AVG, MIN, MAX, COUNT, STDDEV, VARIANCE, MEDIAN,
PERCENTILE(x, p) -> PERCENTILE_CONT(p/100) WITHIN GROUP (ORDER BY x),
FIRST/LAST -> MIN/MAX (PC picks by CACHE ORDER, which SQL cannot
reproduce — the conversion is deterministic and the caveat is flagged).

Sorted Input is a cache optimization: recommendation only, no semantics.
Incremental Aggregation (a SESSION property) caches prior aggregate
state across runs — a migration warning with target-specific incremental
strategies is generated (dbt incremental model re-aggregating affected
partitions; Databricks MERGE / materialized view / DLT).
"""
from __future__ import annotations

import re
from typing import Dict, List

import sqlglot
from sqlglot import exp

from ..ir.model import IssueSeverity, Mapping, Port

SUPPORTED_AGGREGATES = ("SUM", "AVG", "MIN", "MAX", "COUNT", "STDDEV",
                        "VARIANCE", "MEDIAN", "PERCENTILE_CONT")

_FIRST_LAST_RE = re.compile(r"\b(FIRST|LAST)\s*\(", re.IGNORECASE)

_AGG_NODES = (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count, exp.Stddev,
              exp.Variance, exp.Median, exp.PercentileCont)


def _aggregate_function(sql: str) -> str:
    """Top-level aggregate function name of a converted expression,
    '' when the expression is a plain (non-aggregate) derivation."""
    try:
        tree = sqlglot.parse_one(sql)
    except Exception:  # noqa: BLE001
        return ""
    node = tree
    while isinstance(node, (exp.Paren, exp.WithinGroup)):
        node = node.this
    if isinstance(node, _AGG_NODES):
        return node.key.upper() if node.key != "percentilecont" \
            else "PERCENTILE_CONT"
    for agg in tree.find_all(*_AGG_NODES):
        _ = agg
        return "(nested)"
    return ""


def enrich_aggregator(mapping: Mapping, iname: str,
                      props: Dict[str, object], attrs: Dict[str, str],
                      ports: List[Port],
                      raw_expressions: Dict[str, str]) -> None:
    group_by = [str(g) for g in props.get("group_by", [])]
    aggregates: List[dict] = []
    computed: List[str] = []
    for p in ports:
        if not p.expression or p.name in group_by:
            continue
        fn = _aggregate_function(p.expression)
        if fn:
            aggregates.append({"port": p.name,
                               "function": fn if fn != "(nested)"
                               else "EXPRESSION_OVER_AGGREGATES",
                               "expression": p.expression})
        else:
            computed.append(p.name)

    sorted_input = (attrs.get("Sorted Input") or "NO").upper() == "YES"
    props["aggregator_cir"] = {
        "group_by": group_by,
        "aggregates": aggregates,
        "computed": computed,
        "sorted_input": sorted_input,
    }

    # FIRST/LAST in the ORIGINAL expressions: cache-order semantics
    for pname, raw in raw_expressions.items():
        if _FIRST_LAST_RE.search(raw or ""):
            mapping.add_issue(
                IssueSeverity.WARNING, "AGG_FIRST_LAST_ORDER",
                "Aggregator '%s' port '%s' used FIRST/LAST — PowerCenter "
                "picks the row by input (cache) order, which SQL cannot "
                "reproduce; converted deterministically to MIN/MAX"
                % (iname, pname),
                detail=raw[:150],
                suggestion="If 'first by arrival' mattered, order by an "
                           "explicit column instead: MIN_BY/MAX_BY "
                           "(Databricks/Snowflake) or a ROW_NUMBER "
                           "window.")

    if sorted_input:
        mapping.add_issue(
            IssueSeverity.INFO, "AGG_SORTED_INPUT",
            "Aggregator '%s' used Sorted Input — a cache optimization, "
            "not a semantic change" % iname,
            suggestion="GROUP BY on the target is already set-based; "
                       "optionally cluster the input on the group keys.")


def apply_incremental_aggregation(mapping: Mapping,
                                  session_name: str) -> None:
    """Session-level Incremental Aggregation: prior aggregate state is
    cached across runs — a full-refresh GROUP BY changes cost, not
    results, but the strategy must be chosen deliberately."""
    mapping.properties["incremental_aggregation"] = True
    aggs = [t.name for t in mapping.transformations
            if t.properties.get("aggregator_cir")]
    mapping.add_issue(
        IssueSeverity.WARNING, "AGG_INCREMENTAL",
        "Session '%s' used INCREMENTAL AGGREGATION — PowerCenter merged "
        "new rows into a persisted aggregate cache%s"
        % (session_name,
           " (aggregators: %s)" % ", ".join(aggs) if aggs else ""),
        suggestion="Pick a target-specific incremental strategy: dbt — "
                   "incremental model re-aggregating only affected "
                   "partitions (insert_overwrite on the grain) or a full "
                   "GROUP BY if volumes allow; Databricks — MERGE INTO "
                   "the aggregate on the group keys, a materialized "
                   "view, or a DLT/Lakeflow streaming aggregate. A plain "
                   "full re-aggregation is CORRECT but may change run "
                   "cost/time.")

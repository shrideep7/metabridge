"""Lookup transformation handler (Phase 2, module 10).

Connected / unconnected, cached / uncached, static / dynamic / persistent
cache, and Lookup Override SQL — parsed into a CIR LOOKUP contract:

    lookup_dataset, lookup_keys, return_columns, match_policy,
    cache_strategy, dynamic_lookup, override_query (+ connected flag)

Multiple-match policy drives the conversion strategy (a lookup returns at
most one row; a plain join does not):

    Use First/Last/Any   -> deduplicated lookup CTE (ROW_NUMBER over the
                            lookup keys) — cardinality preserved
                            structurally; First/Last additionally warn
                            that PC's ordering is cache-build order, so an
                            explicit ORDER BY should be chosen
    Use All Values       -> plain LEFT JOIN; row growth is INTENTIONAL
    Report Error / unset -> plain LEFT JOIN + cardinality warning: if the
                            lookup keys are not unique the output row
                            count changes (the reconciliation row_count
                            test catches drift)

Cache semantics are honest: static/persistent/uncached caches are RUNTIME
performance choices with no SQL semantics (recommendation only — e.g.
Databricks BROADCAST hint for small dimensions); a DYNAMIC cache is
intra-run upsert state and re-platforms as a Delta MERGE / incremental
merge model — flagged MANUAL, never silently joined.
"""
from __future__ import annotations

from typing import Dict, List

from ..ir.model import IssueSeverity, Mapping, Port
from .pc_source_qualifier import condition_to_cir, normalize_override

MATCH_POLICIES = {
    "use first value": "USE_FIRST",
    "use last value": "USE_LAST",
    "use any value": "USE_ANY",
    "report error": "REPORT_ERROR",
    "use all values": "USE_ALL",
}

DEDUP_POLICIES = ("USE_FIRST", "USE_LAST", "USE_ANY")


def _lookup_keys(condition_cir: dict) -> List[dict]:
    """Equality pairs from the lookup condition: PC convention is
    <lookup column> <op> <input port>."""
    def _pairs(node: dict) -> List[dict]:
        if node.get("operator") == "AND":
            return [p for c in node.get("conditions", [])
                    for p in _pairs(c)]
        if "other_column" in node:
            return [{"lookup_column": node["column"],
                     "input_port": node["other_column"],
                     "operator": node["operator"]}]
        if node.get("column"):
            return [{"lookup_column": node["column"],
                     "input_port": "", "operator": node["operator"]}]
        return []
    return _pairs(condition_cir or {})


def enrich_lookup(mapping: Mapping, iname: str, props: Dict[str, object],
                  attrs: Dict[str, str], ports: List[Port]) -> None:
    """Build the CIR LOOKUP contract on the node and surface the strategy
    and risk issues."""
    table = str(props.get("table", "") or "")
    raw_policy = (attrs.get("Lookup policy on multiple match") or "").strip()
    policy = MATCH_POLICIES.get(raw_policy.lower(), "UNSPECIFIED")

    caching = (attrs.get("Lookup caching enabled") or "YES").upper() == "YES"
    dynamic = (attrs.get("Dynamic Lookup Cache") or "NO").upper() == "YES"
    persistent = (attrs.get("Lookup cache persistent") or
                  "NO").upper() == "YES"
    if dynamic:
        cache_strategy = "DYNAMIC"
    elif not caching:
        cache_strategy = "UNCACHED"
    elif persistent:
        cache_strategy = "PERSISTENT"
    else:
        cache_strategy = "STATIC"

    override = (attrs.get("Lookup Sql Override") or "").strip()
    source_filter = (attrs.get("Lookup source filter") or "").strip()

    connected = not any((p.direction or "").upper() == "RETURN"
                        for p in ports)
    condition_cir = condition_to_cir(str(props.get("condition", "") or "")) \
        if props.get("condition") else {}
    keys = _lookup_keys(condition_cir)
    key_cols = [k["lookup_column"] for k in keys if k["lookup_column"]]
    return_columns = [p.name for p in ports
                      if (p.direction or "").upper() in ("OUTPUT", "RETURN")]

    props["condition_cir"] = condition_cir
    props["lookup_cir"] = {
        "lookup_dataset": table,
        "lookup_keys": keys,
        "return_columns": return_columns,
        "match_policy": policy,
        "pc_match_policy": raw_policy,
        "cache_strategy": cache_strategy,
        "dynamic_lookup": dynamic,
        "override_query": override,
        "source_filter": source_filter,
        "connected": connected,
    }

    # ---- override SQL: AST, never blind ---------------------------------- #
    if override:
        ast_info = normalize_override(override)
        props["sql_override"] = override
        props["sql_override_ast"] = ast_info
        if not ast_info["parsed"]:
            mapping.add_issue(
                IssueSeverity.MANUAL, "LOOKUP_OVERRIDE_UNPARSEABLE",
                "Lookup Override SQL on '%s' does not parse" % iname,
                detail="%s | %s" % (override[:150],
                                    ast_info.get("error", "")),
                suggestion="Fix the override or convert the lookup "
                           "manually.")

    # ---- strategy + risk issues ------------------------------------------ #
    if not connected:
        mapping.add_issue(
            IssueSeverity.MANUAL, "LOOKUP_UNCONNECTED",
            "Unconnected lookup '%s' is invoked from expressions (:LKP) — "
            "call sites need scalar subqueries or a pre-joined column"
            % iname,
            suggestion="dbt: correlated subquery or pre-joined CTE; "
                       "Databricks: scalar subquery. Each :LKP call site "
                       "is flagged separately.")

    if dynamic:
        mapping.add_issue(
            IssueSeverity.MANUAL, "LOOKUP_DYNAMIC_CACHE",
            "Lookup '%s' uses a DYNAMIC cache — intra-run insert/update "
            "state, not a plain join" % iname,
            suggestion="Databricks: express the load as MERGE INTO "
                       "(Delta) with ROW_NUMBER pre-dedup of the batch; "
                       "dbt: incremental merge model. Verify "
                       "first-vs-last-row policy on duplicates within "
                       "one batch.")
    elif policy in DEDUP_POLICIES and key_cols:
        props["dedup_keys"] = key_cols
        props["dedup_last"] = policy == "USE_LAST"
        if policy in ("USE_FIRST", "USE_LAST"):
            mapping.add_issue(
                IssueSeverity.WARNING, "LOOKUP_ORDER_NONDETERMINISTIC",
                "Lookup '%s' uses '%s' — PowerCenter picks the row by "
                "CACHE BUILD ORDER, which SQL cannot reproduce; the "
                "deduplicated CTE orders by the lookup keys" % (iname,
                                                                raw_policy),
                suggestion="Choose an explicit ORDER BY (e.g. an "
                           "updated_at column) in the generated dedup to "
                           "make row selection deterministic.")
        else:
            mapping.add_issue(
                IssueSeverity.INFO, "LOOKUP_DEDUPLICATED",
                "Lookup '%s' (Use Any Value) converted as a deduplicated "
                "lookup CTE — output cardinality preserved" % iname)
    elif policy == "USE_ALL":
        mapping.add_issue(
            IssueSeverity.INFO, "LOOKUP_MULTI_MATCH_INTENTIONAL",
            "Lookup '%s' uses 'Use All Values' — multiple matches "
            "intentionally multiply rows; converted as a plain LEFT JOIN"
            % iname)
    else:
        mapping.add_issue(
            IssueSeverity.WARNING, "LOOKUP_CARDINALITY",
            "Lookup '%s' converted to a LEFT JOIN — if %s is not unique "
            "on (%s) the output ROW COUNT CHANGES (a lookup returns one "
            "row, a join returns every match)"
            % (iname, table or "the lookup table",
               ", ".join(key_cols) or "the lookup keys"),
            suggestion="Confirm key uniqueness or set a dedup policy; "
                       "the reconciliation row_count/checksum tests "
                       "catch drift on real data.")

    if cache_strategy in ("STATIC", "PERSISTENT", "UNCACHED"):
        mapping.add_issue(
            IssueSeverity.INFO, "LOOKUP_CACHE_RUNTIME_ONLY",
            "Lookup '%s' cache setting (%s) is runtime performance, not "
            "semantics" % (iname, cache_strategy),
            suggestion="Databricks: add a BROADCAST hint for small "
                       "dimension tables (SELECT /*+ BROADCAST(lkp) */); "
                       "warehouses handle small-table joins natively.")

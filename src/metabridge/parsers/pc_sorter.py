"""Sorter handler (Phase 2, module 18).

Parses sort keys (ascending/descending), Case Sensitive and Distinct
Output into a CIR SORT contract:

    props["sorter_cir"] = {"sort_keys": [{"port", "order"}],
                           "case_sensitive": bool, "distinct": bool,
                           "ordering_required": bool}

Conversion rules:

  * ORDER BY is NOT generated — inside set-based models row order is
    meaningless, and the optimizer would drop it anyway. The sort keys
    are preserved as an OPTIMIZATION HINT (clustering / sort keys on the
    target) instead.
  * ordering IS semantically required when a downstream node carries
    stateful expression logic (previous-row variables) — that case is
    flagged loudly, because dropping the sort there changes results.
  * DISTINCT output -> duplicate elimination (SELECT DISTINCT). PC's
    case-INSENSITIVE distinct dedupes 'ABC'/'abc' as one row; SQL
    DISTINCT does not — flagged with the UPPER()-normalization fix.
"""
from __future__ import annotations

from typing import Dict

from ..ir.model import IssueSeverity, Mapping, TransformationType


def enrich_sorter(mapping: Mapping, iname: str, props: Dict[str, object],
                  attrs: Dict[str, str]) -> None:
    case_sensitive = (attrs.get("Case Sensitive") or "YES").upper() == "YES"
    props["case_sensitive"] = case_sensitive
    props["sorter_cir"] = {
        "sort_keys": list(props.get("sort_keys") or []),
        "case_sensitive": case_sensitive,
        "distinct": bool(props.get("distinct")),
        "ordering_required": False,        # resolved in the post-pass
    }
    if props.get("distinct") and not case_sensitive:
        mapping.add_issue(
            IssueSeverity.WARNING, "SORTER_CASE_INSENSITIVE_DISTINCT",
            "Sorter '%s' deduplicates CASE-INSENSITIVELY — SQL DISTINCT "
            "is case-sensitive, so 'ABC' and 'abc' stay separate rows "
            "after conversion" % iname,
            suggestion="Normalize the compared columns first "
                       "(UPPER(col)) or deduplicate with ROW_NUMBER() "
                       "OVER (PARTITION BY UPPER(col) ...).")


def apply_sorter_semantics(mapping: Mapping) -> None:
    """Post-pass: decide per sorter whether ordering is semantically
    required downstream; otherwise keep the keys as an optimization hint."""
    for srt in mapping.by_type(TransformationType.SORTER):
        cir = srt.properties.get("sorter_cir")
        if cir is None:
            continue
        keys = cir["sort_keys"]

        # ordering matters only when downstream logic is row-order
        # dependent: stateful variable ports (previous-row semantics)
        stateful_consumers = []
        frontier = [l.to_transformation for l in mapping.links
                    if l.from_transformation == srt.name]
        seen = set(frontier)
        while frontier:
            name = frontier.pop()
            node = mapping.transformation(name)
            if node is None:
                continue
            analysis = node.properties.get("expression_analysis") or {}
            if analysis.get("stateful_variables"):
                stateful_consumers.append(name)
            for l in mapping.links:
                if l.from_transformation == name and \
                        l.to_transformation not in seen:
                    seen.add(l.to_transformation)
                    frontier.append(l.to_transformation)

        if stateful_consumers:
            cir["ordering_required"] = True
            mapping.add_issue(
                IssueSeverity.WARNING, "SORTER_ORDER_REQUIRED",
                "Sorter '%s' feeds stateful logic (%s) that depends on "
                "ROW ORDER — the sort cannot simply be dropped"
                % (srt.name, ", ".join(stateful_consumers)),
                suggestion="Re-express the stateful logic with window "
                           "functions ordered by (%s) — the window ORDER "
                           "BY replaces the sorter."
                % (", ".join("%s %s" % (k.get("port"), k.get("order"))
                             for k in keys) or "the sort keys"))
        elif keys and not cir["distinct"]:
            mapping.add_issue(
                IssueSeverity.INFO, "SORTER_HINT_PRESERVED",
                "Sorter '%s' ordering (%s) is not semantically required "
                "downstream — preserved as an optimization hint, no "
                "ORDER BY generated"
                % (srt.name,
                   ", ".join("%s %s" % (k.get("port"), k.get("order"))
                             for k in keys)),
                suggestion="Apply as physical design if useful: "
                           "clustering keys (Snowflake), ZORDER/liquid "
                           "clustering (Databricks), sort keys "
                           "(Redshift).")
        if cir["distinct"]:
            mapping.add_issue(
                IssueSeverity.INFO, "SORTER_DISTINCT",
                "Sorter '%s' Distinct Output converted to duplicate "
                "elimination (SELECT DISTINCT)" % srt.name)

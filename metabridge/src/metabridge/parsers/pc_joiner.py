"""Joiner transformation handler (Phase 2, module 9).

Parses master input, detail input, Join Condition, Join Type and Sorted
Input — and maps the SEMANTICS correctly. PowerCenter's outer-join naming
is famously counterintuitive:

    Normal Join        matched rows only            -> INNER JOIN
    Master Outer Join  keeps ALL DETAIL rows        -> DETAIL LEFT JOIN MASTER
    Detail Outer Join  keeps ALL MASTER rows        -> DETAIL RIGHT JOIN MASTER
    Full Outer Join    keeps both                   -> FULL OUTER JOIN

MetaBridge AI orients every generated join as left = DETAIL, right = MASTER,
so the IR's LEFT/RIGHT/FULL types are true SQL semantics on every target
(dbt and Databricks render from the same node). The master side is
identified from the port flags (PORTTYPE ".../MASTER") and the connectors
wiring each input; when an export carries no master flags, INNER joins
proceed silently (orientation is irrelevant) and OUTER joins get a
JOINER_ORIENTATION_ASSUMED warning — assumed, never silently guessed.

Sorted Input is a cache/performance option: it produces an optimization
recommendation and NEVER changes join semantics.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from ..ir.model import IssueSeverity, Mapping, TransformationType
from .pc_source_qualifier import condition_to_cir

# orientation contract: left = DETAIL, right = MASTER
PC_JOIN_TYPES = {
    "Normal Join": "INNER",
    "Master Outer Join": "LEFT",     # all detail (left) preserved
    "Detail Outer Join": "RIGHT",    # all master (right) preserved
    "Full Outer Join": "FULL",
}


def apply_joiner_semantics(mapping: Mapping,
                           edge_fields: Dict[Tuple[str, str],
                                             List[Tuple[str, str]]]) -> None:
    """Post-pass: identify master/detail inputs per joiner from port flags
    + connectors, orient left/right, and handle Sorted Input."""
    for jnr in mapping.by_type(TransformationType.JOINER):
        props = jnr.properties
        master_ports = {p.lower() for p in props.pop("_master_ports", [])}
        cond = str(props.get("condition", "") or "")
        if cond:
            props["condition_cir"] = condition_to_cir(cond)

        ups = []
        for link in mapping.links:
            if link.to_transformation == jnr.name and \
                    link.from_transformation not in ups:
                ups.append(link.from_transformation)

        master_input = detail_input = ""
        if master_ports and len(ups) >= 2:
            scores = {}
            for u in ups:
                fields = edge_fields.get((u, jnr.name), [])
                to_fields = {t.lower() for _, t in fields}
                scores[u] = len(to_fields & master_ports)
            ranked = sorted(ups, key=lambda u: -scores.get(u, 0))
            if scores.get(ranked[0], 0) > 0:
                master_input = ranked[0]
                detail_input = next(u for u in ups if u != master_input)

        if master_input:
            props["master_input"] = master_input
            props["detail_input"] = detail_input
            props["left"] = detail_input      # left = DETAIL
            props["right"] = master_input     # right = MASTER
        elif len(ups) >= 2:
            props.setdefault("left", ups[0])
            props.setdefault("right", ups[1])
            if props.get("join_type", "INNER") != "INNER":
                mapping.add_issue(
                    IssueSeverity.WARNING, "JOINER_ORIENTATION_ASSUMED",
                    "Joiner '%s' is an outer join but the export carries "
                    "no master-port flags — input order was assumed "
                    "(left=%s, right=%s)"
                    % (jnr.name, props["left"], props["right"]),
                    suggestion="Verify which input was the MASTER: "
                               "PowerCenter's Master Outer keeps all "
                               "DETAIL rows, Detail Outer keeps all "
                               "MASTER rows.")

        if props.pop("_sorted_input", False):
            props["sorted_input"] = True
            mapping.add_issue(
                IssueSeverity.INFO, "JOINER_SORTED_INPUT",
                "Joiner '%s' used Sorted Input — a cache optimization, "
                "not a semantic change" % jnr.name,
                suggestion="Optimization for the target: cluster/sort the "
                           "join inputs on the join keys (Databricks: "
                           "ZORDER/liquid clustering; warehouses: "
                           "clustering keys). The generated join is "
                           "already semantically correct without it.")

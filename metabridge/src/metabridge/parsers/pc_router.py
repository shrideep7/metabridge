"""Router transformation handler (Phase 2, module 12).

Parses the input group, output groups, group filter conditions and the
DEFAULT group into a CIR ROUTER:

    routes:
      - name:      HIGH_VALUE
        condition: AMOUNT > 100000
        output:    [downstream consumers]

and restructures the mapping graph into INDEPENDENT FILTERED BRANCHES —
one FILTER (+ rename EXPRESSION) chain per output group:

    input -> FIL_<router>_<group> (WHERE condition)
          -> RTE_<router>_<group> (suffixed ports renamed to base names)
          -> the group's downstream consumers

This is the only conversion that preserves Router semantics:

  * MULTI-MATCH — PowerCenter evaluates every group independently; one
    row can be routed to SEVERAL groups. Independent branches reproduce
    that exactly. A single CASE statement (first match wins) silently
    drops the extra routes — that shape is never generated here.
  * DEFAULT — receives rows matched by NO group. NULL conditions do not
    match, so the negation is NULL-safe:
    NOT COALESCE(c1, FALSE) AND NOT COALESCE(c2, FALSE).
  * dead groups (no downstream connectors) are skipped with a note;
    two branches converging on one consumer get a synthesized UNION.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from ..ir.model import (
    IssueSeverity, Link, Mapping, Port, Transformation, TransformationType,
)


def _default_condition(groups: List[dict]) -> str:
    conds = [g["condition"] for g in groups
             if g.get("condition") and not g.get("default")]
    if not conds:
        return "TRUE"
    return " AND ".join("NOT COALESCE((%s), FALSE)" % c for c in conds)


def apply_router_semantics(mapping: Mapping,
                           edge_fields: Dict[Tuple[str, str],
                                             List[Tuple[str, str]]]) -> None:
    for r in list(mapping.by_type(TransformationType.ROUTER)):
        groups = list(r.properties.get("groups") or [])
        port_groups = {k: v for k, v in
                       (r.properties.get("port_groups") or {}).items()}
        port_refs = {k: v for k, v in
                     (r.properties.get("port_refs") or {}).items()}
        if not groups or not port_groups:
            continue          # fallback CASE renderer handles bare routers

        upstreams = [l.from_transformation for l in mapping.links
                     if l.to_transformation == r.name]
        upstreams = sorted(set(upstreams))
        if len(upstreams) != 1:
            mapping.add_issue(
                IssueSeverity.MANUAL, "ROUTER_MULTI_INPUT",
                "Router '%s' has %d input streams — branches cannot be "
                "synthesized automatically" % (r.name, len(upstreams)),
                suggestion="Merge the inputs upstream (union/join) so the "
                           "router reads one stream.")
            continue
        upstream = upstreams[0]

        # which groups actually feed downstream consumers, and where
        consumers_by_group: Dict[str, List[Tuple[str, List[Tuple[str,
                                                                 str]]]]] = {}
        for (frm, to), fields in list(edge_fields.items()):
            if frm != r.name:
                continue
            by_group: Dict[str, List[Tuple[str, str]]] = {}
            for from_f, to_f in fields:
                g = port_groups.get(from_f, "")
                by_group.setdefault(g, []).append((from_f, to_f))
            for g, flds in by_group.items():
                consumers_by_group.setdefault(g, []).append((to, flds))

        base_ports = [p for p in r.ports if p.name not in port_groups]
        routes_cir: List[dict] = []
        branch_of_consumer: Dict[str, List[str]] = {}

        for g in groups:
            gname = g["name"]
            consumers = consumers_by_group.get(gname, [])
            condition = _default_condition(groups) if g.get("default") \
                else g["condition"]
            routes_cir.append({
                "name": gname,
                "condition": condition,
                "default": bool(g.get("default")),
                "output": sorted({c for c, _ in consumers}),
            })
            if not consumers:
                mapping.add_issue(
                    IssueSeverity.INFO, "ROUTER_DEAD_GROUP",
                    "Router '%s' group '%s' has no downstream connectors "
                    "— branch not generated" % (r.name, gname))
                continue

            fil = Transformation(
                name="FIL_%s_%s" % (r.name, gname),
                type=TransformationType.FILTER,
                ports=[Port(name=p.name, datatype=p.datatype,
                            precision=p.precision, scale=p.scale)
                       for p in base_ports],
                properties={"condition": condition or "TRUE",
                            "synthesized_from": "router_group",
                            "router": r.name, "group": gname})
            group_ports = [p for p in r.ports
                           if port_groups.get(p.name) == gname]
            rte = Transformation(
                name="RTE_%s_%s" % (r.name, gname),
                type=TransformationType.EXPRESSION,
                ports=[Port(name=p.name, datatype=p.datatype,
                            precision=p.precision, scale=p.scale,
                            expression=port_refs.get(p.name, ""))
                       for p in group_ports],
                properties={"synthesized_from": "router_group",
                            "router": r.name, "group": gname})
            mapping.transformations.extend([fil, rte])
            mapping.links.append(Link(upstream, fil.name))
            mapping.links.append(Link(fil.name, rte.name))
            for consumer, _flds in consumers:
                mapping.links.append(Link(rte.name, consumer))
                branch_of_consumer.setdefault(consumer, []).append(rte.name)

        # convergence: two branches feeding one consumer become a UNION
        for consumer, branches in branch_of_consumer.items():
            if len(branches) < 2:
                continue
            un = Transformation(
                name="UN_%s_%s" % (r.name, consumer),
                type=TransformationType.UNION,
                ports=[Port(name=p.name, datatype=p.datatype)
                       for p in mapping.transformation(branches[0]).ports],
                properties={"inputs": list(branches),
                            "synthesized_from": "router_convergence"})
            mapping.transformations.append(un)
            mapping.links = [l for l in mapping.links
                             if not (l.from_transformation in branches and
                                     l.to_transformation == consumer)]
            for b in branches:
                mapping.links.append(Link(b, un.name))
            mapping.links.append(Link(un.name, consumer))

        # retire the router node — branches carry its semantics now
        mapping.links = [l for l in mapping.links
                         if r.name not in (l.from_transformation,
                                           l.to_transformation)]
        mapping.transformations = [t for t in mapping.transformations
                                   if t.name != r.name]
        mapping.properties.setdefault("routers", {})[r.name] = {
            "routes": routes_cir}

        live = [x for x in routes_cir if x["output"]]
        mapping.add_issue(
            IssueSeverity.INFO, "ROUTER_BRANCHED",
            "Router '%s' converted to %d independent filtered branch(es) "
            "— multi-match semantics preserved (a row matching several "
            "group conditions flows to every matching branch)"
            % (r.name, len(live)),
            suggestion="Each group is its own WHERE-filtered model/CTE; "
                       "the DEFAULT branch uses a NULL-safe negation of "
                       "all group conditions.")

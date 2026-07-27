"""Normalizer handler (Phase 2, module 17).

Parses repeating groups (OCCURS metadata) and the generated identifiers
into CIR UNPIVOT/NORMALIZE:

    props["normalizer_cir"] = {
        "groups": [{"name": "SALES", "occurs": 4,
                    "instances": ["SALES1", ..., "SALES4"],
                    "gcid": "GCID_SALES"}],
        "passthrough": ["STORE_ID", ...],
        "generated_keys": ["GK_SALES"],
    }

and RESTRUCTURES the graph into the portable unpivot shape — one
EXPRESSION branch per occurrence feeding a UNION ALL node:

    input -> (gk stamping) -> NRM_<n>_1 .. NRM_<n>_N -> UN_<n> -> downstream

Because the shape is ordinary IR, every target renders it natively:
dbt gets UNION ALL (or the adapter's unpivot if the team prefers —
noted), every warehouse gets the same UNION ALL, and Databricks
additionally gets the compact STACK() alternative as a recommendation.

Generated column identifiers are PRESERVED: GCID_<group> carries the
occurrence index (1..N) exactly as PowerCenter produced it, and
GK_<group> becomes a per-input-row key (ROW_NUMBER stamped BEFORE the
split so all N occurrence rows share it).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List

from ..ir.model import (
    IssueSeverity, Link, Mapping, Port, Transformation, TransformationType,
)


def parse_normalizer_fields(xml_t: ET.Element) -> dict:
    """OCCURS groups, instance columns, GCID/GK identifiers."""
    fields = [(f.get("NAME", ""), int(f.get("OCCURS") or 0))
              for f in xml_t.findall("TRANSFORMFIELD")]
    names = [n for n, _ in fields]
    lower = {n.lower() for n in names}

    groups: List[dict] = []
    consumed: set = set()
    for name, occurs in fields:
        if occurs and occurs > 1:
            instances = ["%s%d" % (name, i) for i in range(1, occurs + 1)]
            resolved = [i for i in instances if i.lower() in lower]
            gcid = next((n for n in names
                         if n.lower() == "gcid_%s" % name.lower()), "")
            groups.append({"name": name, "occurs": occurs,
                           "instances": resolved, "gcid": gcid})
            consumed.update(x.lower() for x in resolved)
            consumed.add(name.lower())
            if gcid:
                consumed.add(gcid.lower())
    generated_keys = [n for n in names if n.upper().startswith("GK_")]
    consumed.update(k.lower() for k in generated_keys)
    passthrough = [n for n, _ in fields if n.lower() not in consumed]
    return {"groups": groups, "passthrough": passthrough,
            "generated_keys": generated_keys}


def apply_normalizer_semantics(mapping: Mapping) -> None:
    """Post-pass: replace each Normalizer placeholder with occurrence
    branches + UNION ALL."""
    for nrm in list(mapping.transformations):
        cir = nrm.properties.get("normalizer_cir")
        if not cir:
            continue
        groups = [g for g in cir["groups"] if g["instances"]]
        bad = [g["name"] for g in cir["groups"]
               if g["occurs"] > 1 and
               len(g["instances"]) != g["occurs"]]
        if bad or not groups:
            mapping.add_issue(
                IssueSeverity.MANUAL, "NORMALIZER_INSTANCES_UNRESOLVED",
                "Normalizer '%s': occurrence columns for group(s) %s "
                "could not be resolved — not restructured"
                % (nrm.name, ", ".join(bad) or "(none declared)"),
                suggestion="COBOL OCCURS sources need the flattened "
                           "column list; re-export with source "
                           "definitions or unpivot manually.")
            continue
        if len(groups) > 1:
            mapping.add_issue(
                IssueSeverity.MANUAL, "NORMALIZER_MULTI_GROUP",
                "Normalizer '%s' has %d repeating groups — multi-group "
                "unpivot multiplies rows (%s) and needs a deliberate "
                "design" % (nrm.name, len(groups),
                            " x ".join(str(g["occurs"]) for g in groups)),
                suggestion="Split into one normalizer per group upstream, "
                           "or model the cross-join intent explicitly.")
            continue
        group = groups[0]

        ups = sorted({l.from_transformation for l in mapping.links
                      if l.to_transformation == nrm.name})
        if len(ups) != 1:
            mapping.add_issue(
                IssueSeverity.MANUAL, "NORMALIZER_MULTI_INPUT",
                "Normalizer '%s' has %d input streams" % (nrm.name,
                                                          len(ups)))
            continue
        upstream = ups[0]
        passthrough = list(cir["passthrough"])
        gks = list(cir["generated_keys"])

        # stamp the generated key BEFORE the split so every occurrence
        # row of one input row shares it
        branch_source = upstream
        if gks:
            gk_node = Transformation(
                name="EXP_%s_GK" % nrm.name,
                type=TransformationType.EXPRESSION,
                ports=[Port(name=c) for c in passthrough +
                       group["instances"]] +
                [Port(name=k, datatype="bigint",
                      expression="ROW_NUMBER() OVER (ORDER BY 1)")
                 for k in gks],
                properties={"synthesized_from": "normalizer_gk"})
            mapping.transformations.append(gk_node)
            mapping.links.append(Link(upstream, gk_node.name))
            branch_source = gk_node.name

        out_ports = passthrough + [group["name"]] + \
            ([group["gcid"]] if group["gcid"] else []) + gks
        branches: List[str] = []
        for i in range(1, group["occurs"] + 1):
            bports = [Port(name=c) for c in passthrough + gks]
            bports.append(Port(name=group["name"],
                               expression=group["instances"][i - 1]))
            if group["gcid"]:
                bports.append(Port(name=group["gcid"], datatype="integer",
                                   expression=str(i)))
            branch = Transformation(
                name="NRM_%s_%d" % (nrm.name, i),
                type=TransformationType.EXPRESSION,
                ports=bports,
                properties={"synthesized_from": "normalizer_occurrence",
                            "occurrence": i})
            mapping.transformations.append(branch)
            mapping.links.append(Link(branch_source, branch.name))
            branches.append(branch.name)

        union = Transformation(
            name="UN_%s" % nrm.name,
            type=TransformationType.UNION,
            ports=[Port(name=c) for c in out_ports],
            properties={"inputs": branches,
                        "union_cir": {"operation": "UNION_ALL",
                                      "output_columns": [
                                          {"name": c} for c in out_ports]},
                        "synthesized_from": "normalizer"})
        mapping.transformations.append(union)
        for b in branches:
            mapping.links.append(Link(b, union.name))
        for link in mapping.links:
            if link.from_transformation == nrm.name:
                link.from_transformation = union.name
        mapping.links = [l for l in mapping.links
                         if l.to_transformation != nrm.name]
        mapping.transformations = [t for t in mapping.transformations
                                   if t.name != nrm.name]

        stack_args = ", ".join(
            "%d, %s" % (i + 1, col)
            for i, col in enumerate(group["instances"]))
        mapping.add_issue(
            IssueSeverity.INFO, "NORMALIZER_UNPIVOTED",
            "Normalizer '%s' repeating group %s (occurs %d) unpivoted to "
            "rows via UNION ALL — %s preserved as the occurrence index"
            % (nrm.name, group["name"], group["occurs"],
               group["gcid"] or "no GCID"),
            suggestion="Compact alternatives if preferred: Databricks "
                       "STACK(%d, %s) AS (%s, %s) or native UNPIVOT; "
                       "dbt: dbt_utils.unpivot. The generated UNION ALL "
                       "is correct on every target."
            % (group["occurs"], stack_args,
               group["gcid"] or "occurrence", group["name"]))
        if gks:
            mapping.add_issue(
                IssueSeverity.INFO, "NORMALIZER_GK",
                "Generated key(s) %s stamped per input row before the "
                "split — all %d occurrence rows share the key"
                % (", ".join(gks), group["occurs"]),
                suggestion="Per-run numbering: for durable keys use an "
                           "identity column or hash of the natural key.")

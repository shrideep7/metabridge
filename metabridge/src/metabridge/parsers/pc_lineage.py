"""Port-level lineage engine (Phase 2, module 4).

True column/port lineage over the PowerCenter domain model, recursively
traced through the mapping graph (module 3):

    SRC_CUSTOMER.CUST_ID -> SQ_CUSTOMER.CUST_ID -> EXP_CLEAN.CUST_ID
        -> LKP_ACCOUNT.CUST_ID -> RTR_CUSTOMER.CUST_ID1
        -> TGT_CUSTOMER.CUSTOMER_ID

For every target field the engine returns: target_table, target_column,
source_tables, source_columns, transformations_applied,
expressions_applied, lookup_dependencies, business_rules,
lineage_confidence (+ the paths as evidence).

Handled port kinds: renamed ports (connector field mapping), expression
ports, variable ports (expanded transitively — they have no connectors),
lookup outputs (origin = the lookup table, honestly labeled), aggregator
outputs (SUM(x) -> x), router groups (REF_FIELD + group condition as a
business rule), union inputs (every branch traced), mapplet boundaries
(internal-graph port maps), and sequence-generated values (origin =
generator, no source table pretended).

lineage_confidence is evidence-based: 100 minus deductions, each recorded
in confidence_basis — never a silent guess.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Set

from .pc_graph import MappingGraph, build_mapping_graph
from .pc_model import PCFolder, PCMapping, PCRepository

_json = __import__("json")


def _defs_index(pc_mapping: PCMapping, folder: Optional[PCFolder]) -> dict:
    """instance name -> its transformation definition (local or reusable)."""
    local = {t.name: t for t in pc_mapping.transformations}
    reusable = {t.name: t for t in
                (folder.transformations if folder else [])}
    out = {}
    for inst in pc_mapping.instances:
        t = local.get(inst.transformation_name) or \
            local.get(inst.name) or reusable.get(inst.transformation_name)
        if t is not None:
            out[inst.name] = t
    return out


def _field_of(txdef, name: str):
    if txdef is None:
        return None
    low = name.lower()
    return next((f for f in txdef.fields if f.name.lower() == low), None)


def _variable_expressions(txdef, expression: str,
                          collected: List[str], inst: str,
                          depth: int = 0) -> None:
    """Variable ports referenced by an expression contribute their own
    expressions to the applied-logic record."""
    if txdef is None or depth > 8:
        return
    from .pc_graph import _IDENT_RE
    from .pc_model import PCPortType
    for token in _IDENT_RE.findall(expression or ""):
        f = _field_of(txdef, token)
        if f is not None and f.port_type == PCPortType.VARIABLE \
                and f.expression:
            entry = "%s.%s = %s" % (inst, f.name, f.expression)
            if entry not in collected:
                collected.append(entry)
                _variable_expressions(txdef, f.expression, collected,
                                      inst, depth + 1)


def _router_group_rule(txdef, out_field: str) -> Optional[dict]:
    """The group condition gating a router output port."""
    if txdef is None:
        return None
    f = _field_of(txdef, out_field)
    group = (f.attributes.get("GROUP") or "") if f is not None else ""
    for grp in (txdef.metadata.get("groups") or []):
        if grp["name"] == group and grp.get("expression"):
            return {"type": "router_group", "group": group,
                    "condition": grp["expression"]}
    return None


def trace_target_field(g: MappingGraph, pc_mapping: PCMapping,
                       folder: Optional[PCFolder], target_instance: str,
                       target_column: str) -> dict:
    """The full lineage record for one target field."""
    defs = _defs_index(pc_mapping, folder)
    tgt_node = g.nodes.get(target_instance)
    paths = g.trace_column_lineage(target_instance, target_column,
                                   direction="upstream")
    # a "path" consisting only of the target step means nothing feeds it
    paths = [p for p in paths
             if p != ["%s.%s" % (target_instance, target_column)]]

    source_tables: List[str] = []
    source_columns: List[str] = []
    origin_types: Set[str] = set()
    transformations: List[dict] = []
    expressions: List[str] = []
    lookups: List[dict] = []
    rules: List[dict] = []
    confidence = 100
    basis: List[str] = []
    seq_generated = False

    def _add_tx(inst: str) -> None:
        node = g.nodes.get(inst)
        if node is None or node.node_type in ("SOURCE", "TARGET"):
            return
        entry = {"instance": inst,
                 "type": node.transformation_type or node.node_type}
        if entry not in transformations:
            transformations.append(entry)

    def _deduct(points: int, reason: str) -> None:
        nonlocal confidence
        if reason not in basis:
            confidence -= points
            basis.append(reason)

    for path in paths:
        first_inst, first_field = path[0].rsplit(".", 1)
        first = g.nodes.get(first_inst)
        ftype = (first.transformation_type if first else "").lower()
        if first is not None and first.node_type == "SOURCE":
            origin_types.add("source")
            table = first.transformation_name or first_inst
            if table not in source_tables:
                source_tables.append(table)
            col = "%s.%s" % (table, first_field)
            if col not in source_columns:
                source_columns.append(col)
        elif "lookup" in ftype:
            origin_types.add("lookup")
            txdef = defs.get(first_inst)
            table = (txdef.table_attributes.get("Lookup table name", "")
                     if txdef else "") or first.transformation_name
            col = "%s.%s (lookup)" % (table, first_field)
            if table not in source_tables:
                source_tables.append(table)
            if col not in source_columns:
                source_columns.append(col)
        elif "sequence" in ftype:
            origin_types.add("sequence")
            seq_generated = True
        else:
            origin_types.add("unresolved")
            _deduct(40, "a lineage path stops at '%s' (%s) without "
                        "reaching a source, lookup table, or generator"
                    % (first_inst, ftype or "unknown"))

        # walk the path: transformations, expressions, rules
        for step in path:
            inst, fld = step.rsplit(".", 1)
            node = g.nodes.get(inst)
            if node is None:
                continue
            _add_tx(inst)
            txdef = defs.get(inst)
            ttype = (node.transformation_type or "").lower()

            if node.node_type == "TRANSFORMATION" and not node.fields:
                _deduct(20, "instance '%s' has no definition in the "
                            "export — pass-through by name assumed" % inst)

            f = _field_of(txdef, fld)
            if f is not None and f.expression:
                entry = "%s = %s" % (step, f.expression)
                if entry not in expressions:
                    expressions.append(entry)
                _variable_expressions(txdef, f.expression, expressions,
                                      inst)
            if node.node_type == "MAPPLET":
                for e in (node.metadata.get("mapplet_expressions", {})
                          .get(fld.lower(), [])):
                    tagged = "%s (inside mapplet %s)" % (
                        e, node.transformation_name)
                    if tagged not in expressions:
                        expressions.append(tagged)
                if fld.lower() in node.metadata.get(
                        "mapplet_fallback_ports", []):
                    _deduct(15, "mapplet '%s' port '%s' could not be "
                                "traced internally — all inputs assumed"
                            % (node.transformation_name, fld))

            if "lookup" in ttype and txdef is not None:
                dep = {"instance": inst,
                       "table": txdef.table_attributes.get(
                           "Lookup table name", ""),
                       "condition": txdef.table_attributes.get(
                           "Lookup condition", "")}
                if dep not in lookups:
                    lookups.append(dep)
            elif "filter" in ttype and txdef is not None:
                cond = txdef.table_attributes.get("Filter Condition", "")
                rule = {"type": "filter", "instance": inst,
                        "condition": cond}
                if cond and rule not in rules:
                    rules.append(rule)
            elif "router" in ttype:
                rule = _router_group_rule(txdef, fld)
                if rule is not None:
                    rule["instance"] = inst
                    if rule not in rules:
                        rules.append(rule)
            elif "update strategy" in ttype and txdef is not None:
                cond = txdef.table_attributes.get(
                    "Update Strategy Expression", "")
                rule = {"type": "update_strategy", "instance": inst,
                        "condition": cond}
                if cond and rule not in rules:
                    rules.append(rule)
            elif "joiner" in ttype and txdef is not None:
                cond = txdef.table_attributes.get("Join Condition", "")
                rule = {"type": "join", "instance": inst,
                        "condition": cond}
                if cond and rule not in rules:
                    rules.append(rule)
            elif "aggregator" in ttype and txdef is not None:
                grain = [x.name for x in txdef.fields
                         if (x.expression_type or "").upper() == "GROUPBY"]
                rule = {"type": "aggregation_grain", "instance": inst,
                        "condition": ", ".join(grain)}
                if grain and rule not in rules:
                    rules.append(rule)

    if not paths:
        _deduct(60, "no connector path reaches %s.%s"
                % (target_instance, target_column))

    return {
        "mapping": pc_mapping.name,
        "target_instance": target_instance,
        "target_table": (tgt_node.transformation_name
                         if tgt_node else target_instance),
        "target_column": target_column,
        "source_tables": source_tables,
        "source_columns": source_columns,
        "origin_types": sorted(origin_types),
        "sequence_generated": seq_generated,
        "transformations_applied": transformations,
        "expressions_applied": expressions,
        "lookup_dependencies": lookups,
        "business_rules": rules,
        "lineage_confidence": max(5, min(100, confidence)),
        "confidence_basis": basis or ["all paths fully resolved"],
        "paths": [" -> ".join(p) for p in paths],
    }


def lineage_to_mermaid(entry: dict) -> str:
    """One target field as a Mermaid graph (vertical, like the spec)."""
    def nid(step: str) -> str:
        return step.replace(".", "_").replace(" ", "_").replace("-", "_")

    lines = ["graph TD"]
    seen: Set[str] = set()
    for path in entry["paths"]:
        steps = path.split(" -> ")
        for a, b in zip(steps, steps[1:]):
            edge = '%s["%s"] --> %s["%s"]' % (nid(a), a, nid(b), b)
            if edge not in seen:
                seen.add(edge)
                lines.append("    " + edge)
    if len(lines) == 1:
        lines.append('    %s["%s.%s (no path)"]'
                     % (nid(entry["target_column"]),
                        entry["target_instance"], entry["target_column"]))
    first = entry["paths"][0].split(" -> ")[0] if entry["paths"] else ""
    if first:
        lines.append("    style %s fill:#e8f5e9,stroke:#1e8449" % nid(first))
    lines.append("    style %s fill:#eef3fa,stroke:#2f5b8d"
                 % nid("%s.%s" % (entry["target_instance"],
                                  entry["target_column"])))
    return "\n".join(lines)


def build_port_lineage(pc_mapping: PCMapping,
                       folder: Optional[PCFolder] = None) -> dict:
    """Every target field of one mapping, fully traced."""
    g = build_mapping_graph(pc_mapping, folder)
    fields: List[dict] = []
    for tgt in g.get_target_nodes():
        cols = tgt.fields or sorted(
            {e.to_field for e in g.edges if e.to_instance == tgt.name})
        for col in cols:
            fields.append(trace_target_field(g, pc_mapping, folder,
                                             tgt.name, col))
    return {
        "mapping": pc_mapping.name,
        "folder": folder.name if folder else "",
        "target_fields": fields,
        "summary": {
            "fields_traced": len(fields),
            "fully_resolved": sum(1 for f in fields
                                  if f["lineage_confidence"] == 100),
            "average_confidence": int(round(
                sum(f["lineage_confidence"] for f in fields)
                / len(fields))) if fields else 0,
        },
        "mermaid": {"%s.%s" % (f["target_instance"], f["target_column"]):
                    lineage_to_mermaid(f) for f in fields},
    }


def build_repository_lineage(model: PCRepository) -> dict:
    mappings = []
    for folder in model.folders:
        for m in folder.mappings:
            mappings.append(build_port_lineage(m, folder))
    total = [f for m in mappings for f in m["target_fields"]]
    return {
        "repository": model.name,
        "mappings": mappings,
        "summary": {
            "mappings": len(mappings),
            "fields_traced": len(total),
            "fully_resolved": sum(1 for f in total
                                  if f["lineage_confidence"] == 100),
            "average_confidence": int(round(
                sum(f["lineage_confidence"] for f in total)
                / len(total))) if total else 0,
        },
    }


def write_port_lineage(doc: dict, out_dir: str) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "port_lineage.json").write_text(_json.dumps(doc, indent=2))
    lines = ["# Port-level lineage — %s" % doc.get("repository",
                                                   doc.get("mapping", "")),
             ""]
    mappings = doc.get("mappings") or [doc]
    for m in mappings:
        lines += ["## %s%s" % (("%s/" % m["folder"]) if m.get("folder")
                               else "", m["mapping"]), ""]
        for f in m["target_fields"]:
            lines += ["### %s.%s  (confidence %d)"
                      % (f["target_table"], f["target_column"],
                         f["lineage_confidence"]),
                      "",
                      "- sources: %s" % (", ".join(f["source_columns"])
                                         or "(none — %s)"
                                         % "/".join(f["origin_types"])),
                      ]
            if f["expressions_applied"]:
                lines.append("- logic: %s"
                             % "; ".join(f["expressions_applied"]))
            if f["business_rules"]:
                lines.append("- rules: %s" % "; ".join(
                    "%s: %s" % (r["type"], r["condition"])
                    for r in f["business_rules"]))
            lines += ["", "```mermaid",
                      m["mermaid"]["%s.%s" % (f["target_instance"],
                                              f["target_column"])],
                      "```", ""]
    path = out / "port_lineage.md"
    path.write_text("\n".join(lines) + "\n")
    return str(out / "port_lineage.json")

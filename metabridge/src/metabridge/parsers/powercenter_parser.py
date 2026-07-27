"""Parse PowerCenter repository XML (POWERMART export) into the IR.

Handles the transformation types the IR models natively; anything else is
preserved as an issue on the mapping so the report shows exactly what needs
manual attention. Port expressions and conditions are translated from the
Informatica expression language to canonical SQL at parse time (late failures
become MANUAL issues with the original expression preserved).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..ir.model import (
    ConversionIssue, IssueSeverity, Link, LoadStrategy, Mapping, Pipeline,
    Port, SourceTable, Transformation, TransformationType,
)
from ..sqlx.expressions import ExpressionError, infa_to_sql

_PC_TO_IR_TYPE = {
    "Source Qualifier": TransformationType.SOURCE_QUALIFIER,
    "Expression": TransformationType.EXPRESSION,
    "Filter": TransformationType.FILTER,
    "Joiner": TransformationType.JOINER,
    "Aggregator": TransformationType.AGGREGATOR,
    "Sorter": TransformationType.SORTER,
    "Union Transformation": TransformationType.UNION,
    "Lookup Procedure": TransformationType.LOOKUP,
    "Router": TransformationType.ROUTER,
    "Rank": TransformationType.RANK,
    "Sequence": TransformationType.SEQUENCE,
    "Update Strategy": TransformationType.UPDATE_STRATEGY,
    # mapplet boundary nodes become passthrough expressions when inlined
    "Input Transformation": TransformationType.EXPRESSION,
    "Output Transformation": TransformationType.EXPRESSION,
    "Mapplet Input": TransformationType.EXPRESSION,
    "Mapplet Output": TransformationType.EXPRESSION,
}

_PC_TYPE_TO_CANONICAL = {
    "string": "string", "nstring": "string", "text": "string", "ntext": "string",
    "varchar": "string", "varchar2": "string", "char": "string", "nvarchar2": "string",
    "integer": "integer", "small integer": "integer", "int": "integer", "number(p,s)": "decimal",
    "bigint": "bigint",
    "decimal": "decimal", "number": "decimal", "numeric": "decimal", "money": "decimal",
    "double": "double", "real": "double", "float": "double",
    "date/time": "timestamp", "date": "date", "datetime": "timestamp",
    "timestamp": "timestamp",
    "binary": "binary", "raw": "binary", "image": "binary",
}


def parse_powercenter(path: str) -> Pipeline:
    """Parse a POWERMART XML file (or a directory of them) into a Pipeline.

    Routed through the streaming ingestion engine (powercenter_ingest):
    namespace-agnostic, memory-bounded via iterparse, and covering the full
    repository grammar (folders, mapplets, reusable transformations,
    mapping variables, workflows/worklets/sessions/tasks/configs)."""
    from .powercenter_ingest import PowerCenterRepositoryParser
    return PowerCenterRepositoryParser().parse(path)


def _parse_file(path: Path, pipeline: Pipeline) -> None:
    try:
        tree = ET.parse(str(path))
    except ET.ParseError as e:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.ERROR, code="XML_PARSE_ERROR",
            message="Could not parse %s" % path.name, detail=str(e)))
        return
    root = tree.getroot()
    for folder in root.iter("FOLDER"):
        if not pipeline.name or pipeline.name == path.stem:
            pipeline.name = folder.get("NAME", pipeline.name)
        source_defs = _collect_source_defs(folder, pipeline)
        target_keys = _collect_target_keys(folder)
        target_columns = _collect_target_columns(folder)
        for mxml in folder.findall("MAPPING"):
            pipeline.mappings.append(
                _parse_mapping(mxml, source_defs, target_keys, pipeline,
                               target_columns=target_columns))
        _parse_workflow_order(folder, pipeline)


def _canonical(dt: str) -> str:
    return _PC_TYPE_TO_CANONICAL.get((dt or "").lower(), "string")


def _collect_source_defs(folder: ET.Element, pipeline: Pipeline) -> Dict[str, SourceTable]:
    defs: Dict[str, SourceTable] = {}
    for s in folder.findall("SOURCE"):
        cols = [Port(name=f.get("NAME", ""), datatype=_canonical(f.get("DATATYPE")),
                     precision=int(f.get("PRECISION") or 0), scale=int(f.get("SCALE") or 0))
                for f in s.findall("SOURCEFIELD")]
        st = SourceTable(name=s.get("NAME", ""), schema=s.get("OWNERNAME", ""),
                         database=s.get("DBDNAME", ""), columns=cols)
        defs[st.name.lower()] = st
        if all(x.name != st.name for x in pipeline.sources):
            pipeline.sources.append(st)
    return defs


def _collect_target_keys(folder: ET.Element) -> Dict[str, List[str]]:
    keys: Dict[str, List[str]] = {}
    for t in folder.findall("TARGET"):
        pks = [f.get("NAME", "") for f in t.findall("TARGETFIELD")
               if "PRIMARY KEY" in (f.get("KEYTYPE") or "")]
        keys[t.get("NAME", "").lower()] = pks
    return keys


def _collect_target_columns(folder: ET.Element) -> Dict[str, List[str]]:
    """ALL declared TARGETFIELD names per target — the TARGET node's ports
    only carry the CONNECTED columns, but pattern detection (SCD) needs the
    full declared shape (effective dates and flags are often unmapped)."""
    cols: Dict[str, List[str]] = {}
    for t in folder.findall("TARGET"):
        cols[t.get("NAME", "").lower()] = [
            f.get("NAME", "") for f in t.findall("TARGETFIELD")]
    return cols


def _ports_from_fields(x: ET.Element, tag: str) -> List[Port]:
    out = []
    for f in x.findall(tag):
        out.append(Port(name=f.get("NAME", ""), datatype=_canonical(f.get("DATATYPE")),
                        precision=int(f.get("PRECISION") or 0),
                        scale=int(f.get("SCALE") or 0)))
    return out


def _inline_mapplet(iname: str, mp: ET.Element, instances: Dict[str, dict],
                    tx_by_name: Dict[str, ET.Element], extra_links: list,
                    mapping: Mapping) -> dict:
    """Expand a mapplet instance into the mapping graph. Internal nodes get
    '<instance>_<node>' names; returns the port->owner rewire maps used to
    reconnect the mapping-level connectors that referenced the instance."""
    mp_tx = {t.get("NAME", ""): t for t in mp.findall("TRANSFORMATION")}
    mp_instances = mp.findall("INSTANCE")
    rewire = {"in": {}, "out": {}, "in_default": "", "out_default": ""}

    mapping.properties.setdefault("mapplet_instances", {})[iname] = \
        mp.get("NAME", "")

    def register(internal_name: str, tx_name: str, tx_type: str) -> str:
        syn = "%s_%s" % (iname, internal_name)
        xml_t = mp_tx.get(tx_name) or mp_tx.get(internal_name)
        instances[syn] = {"type": "TRANSFORMATION", "tx_name": syn,
                          "tx_type": tx_type,
                          "mapplet": mp.get("NAME", ""),
                          "mapplet_instance": iname}
        if xml_t is not None:
            tx_by_name[syn] = xml_t
            ttype = (tx_type or xml_t.get("TYPE", "")).lower()
            ports = [f.get("NAME", "") for f in
                     xml_t.findall("TRANSFORMFIELD")]
            if "input" in ttype:
                rewire["in_default"] = rewire["in_default"] or syn
                for p in ports:
                    rewire["in"].setdefault(p.lower(), syn)
            elif "output" in ttype:
                rewire["out_default"] = rewire["out_default"] or syn
                for p in ports:
                    rewire["out"].setdefault(p.lower(), syn)
        return syn

    if mp_instances:
        for inst in mp_instances:
            register(inst.get("NAME", ""),
                     inst.get("TRANSFORMATION_NAME", ""),
                     inst.get("TRANSFORMATION_TYPE", ""))
    else:  # some exports omit INSTANCE when definitions are used 1:1
        for tname, t in mp_tx.items():
            register(tname, tname, t.get("TYPE", ""))

    for c in mp.findall("CONNECTOR"):
        extra_links.append(Link("%s_%s" % (iname, c.get("FROMINSTANCE", "")),
                                "%s_%s" % (iname, c.get("TOINSTANCE", ""))))
    mapping.add_issue(
        IssueSeverity.WARNING, "MAPPLET_INLINED",
        "Mapplet '%s' (instance %s) was inlined into the mapping graph"
        % (mp.get("NAME", ""), iname),
        suggestion="Verify the expanded logic; mapplets have no direct "
                   "equivalent on most targets.")
    return rewire


def _parse_mapping(mxml: ET.Element, source_defs: Dict[str, SourceTable],
                   target_keys: Dict[str, List[str]], pipeline: Pipeline,
                   reusable_tx: Optional[Dict[str, ET.Element]] = None,
                   mapplets: Optional[Dict[str, ET.Element]] = None,
                   target_columns: Optional[Dict[str, List[str]]] = None,
                   ) -> Mapping:
    raw_name = mxml.get("NAME", "mapping")
    name = raw_name[2:] if raw_name.startswith("m_") else raw_name
    mapping = Mapping(name=name, origin=raw_name)

    instances: Dict[str, dict] = {}
    for inst in mxml.findall("INSTANCE"):
        instances[inst.get("NAME", "")] = {
            "type": inst.get("TYPE", ""),
            "tx_name": inst.get("TRANSFORMATION_NAME", ""),
            "tx_type": inst.get("TRANSFORMATION_TYPE", ""),
        }

    tx_by_name: Dict[str, ET.Element] = {
        t.get("NAME", ""): t for t in mxml.findall("TRANSFORMATION")}
    for tname, t in (reusable_tx or {}).items():
        tx_by_name.setdefault(tname, t)     # folder-level reusable defs

    # mapping variables / parameters ($$X) — carried for the generators
    variables = [{"name": v.get("NAME", ""),
                  "datatype": _canonical(v.get("DATATYPE")),
                  "default": v.get("DEFAULTVALUE", ""),
                  "is_param": (v.get("ISPARAM") or "NO").upper() == "YES",
                  "aggregation": v.get("AGGFUNCTION", "")}
                 for v in mxml.findall("MAPPINGVARIABLE")]
    if variables:
        mapping.properties["variables"] = variables

    # declared target load order
    tlo = sorted((int(t.get("ORDER") or 0), t.get("TARGETINSTANCE", ""))
                 for t in mxml.findall("TARGETLOADORDER"))
    if tlo:
        mapping.properties["target_load_order"] = [n for _, n in tlo]

    # mapplet instances are inlined before the normal graph pass
    extra_links: List[Link] = []
    mapplet_rewire: Dict[str, dict] = {}
    for iname, info in list(instances.items()):
        if info["type"] != "MAPPLET":
            continue
        mp = (mapplets or {}).get(info["tx_name"])
        if mp is None:
            mapping.add_issue(
                IssueSeverity.MANUAL, "MAPPLET_UNRESOLVED",
                "Mapplet '%s' (instance %s) is not present in the export — "
                "its logic is missing from the conversion"
                % (info["tx_name"], iname),
                suggestion="Re-export with dependent objects (pmrep "
                           "ObjectExport -dependents) or convert the "
                           "mapplet manually.")
            instances[iname] = {"type": "TRANSFORMATION", "tx_name": iname,
                                "tx_type": "Expression"}
            tx_by_name.setdefault(iname, ET.Element(
                "TRANSFORMATION", {"NAME": iname, "TYPE": "Expression"}))
            continue
        mapplet_rewire[iname] = _inline_mapplet(
            iname, mp, instances, tx_by_name, extra_links, mapping)
        del instances[iname]

    # SOURCE / TARGET instances
    for iname, info in instances.items():
        if info["type"] == "SOURCE":
            sdef = source_defs.get(info["tx_name"].lower())
            ports = [Port(name=c.name, datatype=c.datatype, precision=c.precision,
                          scale=c.scale) for c in sdef.columns] if sdef else []
            mapping.transformations.append(Transformation(
                name=iname, type=TransformationType.SOURCE, ports=ports,
                properties={"table": info["tx_name"],
                            "schema": sdef.schema if sdef else "",
                            "database": sdef.database if sdef else ""}))
        elif info["type"] == "TARGET":
            props = {"table": info["tx_name"]}
            declared = (target_columns or {}).get(info["tx_name"].lower())
            if declared:
                props["declared_columns"] = declared
            mapping.transformations.append(Transformation(
                name=iname, type=TransformationType.TARGET, ports=[],
                properties=props))
            mapping.unique_key = target_keys.get(info["tx_name"].lower(), [])

    # Transformations
    for iname, info in instances.items():
        if info["type"] != "TRANSFORMATION":
            continue
        xml_t = tx_by_name.get(info["tx_name"]) or tx_by_name.get(iname)
        if xml_t is None:
            mapping.add_issue(IssueSeverity.WARNING, "MISSING_TRANSFORMATION",
                              "Instance %s has no transformation definition" % iname)
            continue
        ir_type = _PC_TO_IR_TYPE.get(info["tx_type"] or xml_t.get("TYPE", ""))
        if ir_type is None and \
                (info["tx_type"] or xml_t.get("TYPE", "")) == \
                "SQL Transformation":
            # module 20: static AST vs dynamic SQL, never executed
            from .pc_sql_transformation import enrich_sql_transformation
            t = _parse_transformation(xml_t, iname,
                                      TransformationType.EXPRESSION,
                                      mapping)
            sqlt_attrs = {a.get("NAME", ""): a.get("VALUE", "")
                          for a in xml_t.findall("TABLEATTRIBUTE")}
            enrich_sql_transformation(mapping, iname, t.properties,
                                      sqlt_attrs, t.ports)
            mapping.transformations.append(t)
            continue
        if ir_type is None and \
                (info["tx_type"] or xml_t.get("TYPE", "")) == \
                "Stored Procedure":
            # module 19: procedure call contract + hook/strategy routing
            from .pc_stored_procedure import enrich_stored_procedure
            t = _parse_transformation(xml_t, iname,
                                      TransformationType.EXPRESSION,
                                      mapping)
            sp_attrs = {a.get("NAME", ""): a.get("VALUE", "")
                        for a in xml_t.findall("TABLEATTRIBUTE")}
            enrich_stored_procedure(mapping, iname, t.properties,
                                    sp_attrs, t.ports)
            mapping.transformations.append(t)
            continue
        if ir_type is None and \
                (info["tx_type"] or xml_t.get("TYPE", "")) == "Normalizer":
            # module 17: repeating groups restructure in a post-pass
            from .pc_normalizer import parse_normalizer_fields
            t = _parse_transformation(xml_t, iname,
                                      TransformationType.EXPRESSION,
                                      mapping)
            t.properties["normalizer_cir"] = parse_normalizer_fields(xml_t)
            mapping.transformations.append(t)
            continue
        if ir_type is None:
            # transformation knowledge lives in the semantic registry —
            # the issue carries the declared strategy, never a shrug
            from .pc_registry import get_pc_registry
            entry = get_pc_registry().classify(
                info["tx_type"] or xml_t.get("TYPE", ""))
            severity = IssueSeverity.MANUAL \
                if entry["requires_manual_review"] or \
                entry["automation_level"] in ("LOW", "MANUAL") \
                else IssueSeverity.WARNING
            mapping.add_issue(
                severity, "UNSUPPORTED_TRANSFORMATION",
                "Transformation type '%s' (%s) is not converted natively — "
                "automation level %s"
                % (entry["powercenter_type"] or info["tx_type"], iname,
                   entry["automation_level"]),
                detail="registry: %s" % entry.get("key", "UNKNOWN"),
                suggestion=entry.get("workaround") or
                "Target strategies — dbt: %s; Databricks: %s."
                % (entry["dbt_strategy"], entry["databricks_strategy"]))
            ir_type = TransformationType.EXPRESSION  # placeholder to keep the graph intact
            t = _parse_transformation(xml_t, iname, ir_type, mapping)
            # confidence scoring (module 31) needs the ORIGINAL type
            t.properties["unconverted_pc_type"] = \
                entry["powercenter_type"] or info["tx_type"] or \
                xml_t.get("TYPE", "")
            if info.get("mapplet"):
                t.properties["from_mapplet"] = info["mapplet"]
                t.properties["mapplet_instance"] = info["mapplet_instance"]
            mapping.transformations.append(t)
            continue
        t = _parse_transformation(xml_t, iname, ir_type, mapping)
        if info.get("mapplet"):
            t.properties["from_mapplet"] = info["mapplet"]
            t.properties["mapplet_instance"] = info["mapplet_instance"]
        mapping.transformations.append(t)

    # Connectors -> links (+ target ports from connectors). Endpoints that
    # referenced an inlined mapplet instance are rewired to the mapplet's
    # input/output boundary node that owns the port.
    def _endpoint(inst: str, field: str, side: str) -> str:
        rw = mapplet_rewire.get(inst)
        if rw is None:
            return inst
        owners = rw["out"] if side == "from" else rw["in"]
        default = rw["out_default"] if side == "from" else rw["in_default"]
        return owners.get((field or "").lower(), default) or inst

    tgt_ports: Dict[str, List[str]] = {}
    seen_links = set()
    edge_fields: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for c in mxml.findall("CONNECTOR"):
        frm = _endpoint(c.get("FROMINSTANCE", ""), c.get("FROMFIELD", ""),
                        "from")
        to = _endpoint(c.get("TOINSTANCE", ""), c.get("TOFIELD", ""), "to")
        key = (frm, to)
        edge_fields.setdefault(key, []).append(
            (c.get("FROMFIELD", ""), c.get("TOFIELD", "")))
        if key not in seen_links:
            seen_links.add(key)
            mapping.links.append(Link(frm, to))
        if instances.get(to, {}).get("type") == "TARGET":
            tgt_ports.setdefault(to, []).append(c.get("TOFIELD", ""))
    for link in extra_links:                    # mapplet-internal edges
        if (link.from_transformation, link.to_transformation) not in seen_links:
            seen_links.add((link.from_transformation, link.to_transformation))
            mapping.links.append(link)
    for tname, cols in tgt_ports.items():
        t = mapping.transformation(tname)
        if t is not None and not t.ports:
            t.ports = [Port(name=c) for c in cols]

    _fold_mapplet_bypasses(mapping, mapplet_rewire, edge_fields)

    # Joiner semantics: master/detail orientation + Sorted Input (module 9)
    from .pc_joiner import apply_joiner_semantics
    apply_joiner_semantics(mapping, edge_fields)

    _fold_passive_diamonds(mapping, edge_fields)
    _fold_sequence_generators(mapping, edge_fields)

    # Source Qualifier semantics: filters/joins/sort/distinct become real
    # graph nodes, pre/post SQL become mapping hooks (module 6)
    from .pc_source_qualifier import apply_sq_semantics
    apply_sq_semantics(mapping)

    # Router semantics: independent filtered branches per output group,
    # multi-match preserved (module 12)
    from .pc_router import apply_router_semantics
    apply_router_semantics(mapping, edge_fields)

    # Update Strategy semantics: DML routing -> merge clauses + rejects
    # on every warehouse target (module 16)
    from .pc_update_strategy import apply_update_strategy_semantics
    apply_update_strategy_semantics(mapping)

    # Normalizer semantics: OCCURS groups -> occurrence branches + UNION
    # ALL (module 17)
    from .pc_normalizer import apply_normalizer_semantics
    apply_normalizer_semantics(mapping)

    # Sorter semantics: ordering required vs optimization hint (module 18)
    from .pc_sorter import apply_sorter_semantics
    apply_sorter_semantics(mapping)

    # Stored procedure hooks: stage-ordered pre/post SQL (module 19)
    from .pc_stored_procedure import apply_stored_procedure_hooks
    apply_stored_procedure_hooks(mapping)

    # SQL transformation: connected-flag resolution (module 20)
    from .pc_sql_transformation import apply_sql_transformation_semantics
    apply_sql_transformation_semantics(mapping)

    # SCD recognition over the assembled CIR signals (modules 22-23);
    # Type 2 first — versioning columns make it the more specific pattern
    from .pc_scd import detect_scd_type1, detect_scd_type2
    detect_scd_type2(mapping)
    detect_scd_type1(mapping)
    return mapping


def _row_preserving(t: Transformation) -> bool:
    if t.type in (TransformationType.EXPRESSION, TransformationType.LOOKUP):
        return True
    if t.type == TransformationType.SORTER:
        return not t.properties.get("distinct")
    return False


def _fold_passive_diamonds(mapping: Mapping,
                           edge_fields: Dict[Tuple[str, str],
                                             List[Tuple[str, str]]]
                           ) -> None:
    """A node fed BOTH directly by U and through a row-preserving chain
    U -> ... -> V (a diamond) cannot render as row-wise SQL CTEs. Since
    the chain preserves the row set, the direct fields can ride the chain:
    add them as pass-through ports on each chain node and drop the direct
    edge. Same principle as mapplet bypass folding, generalized."""
    changed = True
    guard = 0
    while changed and guard < 10:
        changed = False
        guard += 1
        for x in list(mapping.transformations):
            ups = []
            for l in mapping.links:
                if l.to_transformation == x.name and \
                        l.from_transformation not in ups:
                    ups.append(l.from_transformation)
            if len(ups) < 2:
                continue
            for u in ups:
                for v in ups:
                    if u == v:
                        continue
                    path = _passive_path(mapping, u, v)
                    if not path:
                        continue
                    fields = edge_fields.get((u, x.name), [])
                    feeder = mapping.transformation(u)
                    for from_f, _to_f in fields:
                        src_port = feeder.port(from_f) if feeder else None
                        for node_name in path:
                            node = mapping.transformation(node_name)
                            if node is not None and \
                                    node.port(from_f) is None:
                                node.ports.append(Port(
                                    name=from_f,
                                    datatype=src_port.datatype
                                    if src_port else "string",
                                    precision=src_port.precision
                                    if src_port else 0,
                                    scale=src_port.scale
                                    if src_port else 0))
                    mapping.links = [l for l in mapping.links
                                     if not (l.from_transformation == u and
                                             l.to_transformation == x.name)]
                    changed = True
                    break
                if changed:
                    break
            if changed:
                break


def _passive_path(mapping: Mapping, start: str,
                  end: str) -> Optional[List[str]]:
    """Nodes strictly after `start` up to and including `end`, when a path
    exists whose intermediate nodes all preserve the row set."""
    frontier = [(start, [])]
    seen = {start}
    while frontier:
        current, path = frontier.pop(0)
        for l in mapping.links:
            if l.from_transformation != current or \
                    l.to_transformation in seen:
                continue
            nxt = l.to_transformation
            node = mapping.transformation(nxt)
            if node is None:
                continue
            if nxt == end:
                return path + [nxt]
            if _row_preserving(node):
                seen.add(nxt)
                frontier.append((nxt, path + [nxt]))
    return None


def _fold_sequence_generators(mapping: Mapping,
                              edge_fields: Dict[Tuple[str, str],
                                                List[Tuple[str, str]]]
                              ) -> None:
    """A Sequence Generator has no input rows — 'FROM <sequence>' is not
    renderable SQL. Fold each one into its consumer's stream: an EXPRESSION
    node computing ROW_NUMBER() OVER (ORDER BY 1) is spliced before the
    consumer, and the generator node disappears. Consumers with no other
    (or several) upstream streams keep the generator and go MANUAL."""
    for seq in list(mapping.by_type(TransformationType.SEQUENCE)):
        if any(l.to_transformation == seq.name for l in mapping.links):
            continue                       # fed sequences render inline
        cir = seq.properties.get("sequence_cir") or {}
        consumers = [l.to_transformation for l in mapping.links
                     if l.from_transformation == seq.name]
        # stateful shapes are NEVER folded silently (module 15)
        if cir.get("cycle"):
            continue                       # SEQUENCE_CYCLE already MANUAL
        currval_wired = any(
            from_f.upper() == "CURRVAL"
            for c in set(consumers)
            for from_f, _ in edge_fields.get((seq.name, c), []))
        if currval_wired:
            mapping.add_issue(
                IssueSeverity.MANUAL, "SEQUENCE_CURRVAL",
                "Sequence '%s' wires CURRVAL downstream — cross-port "
                "pairing state has no row-wise SQL equivalent; not "
                "folded" % seq.name,
                suggestion="Pair NEXTVAL/CURRVAL consumers on a shared "
                           "computed key instead.")
            continue
        ok = True
        for i, consumer in enumerate(sorted(set(consumers))):
            others = [l.from_transformation for l in mapping.links
                      if l.to_transformation == consumer and
                      l.from_transformation != seq.name]
            if len(set(others)) != 1:
                ok = False
                mapping.add_issue(
                    IssueSeverity.MANUAL, "SEQUENCE_UNROUTABLE",
                    "Sequence '%s' feeds '%s', which has %d other input "
                    "stream(s) — the surrogate cannot be folded "
                    "automatically" % (seq.name, consumer,
                                       len(set(others))),
                    suggestion="Compute the surrogate with ROW_NUMBER()/"
                               "an identity column in the consumer "
                               "manually.")
                continue
            upstream = others[0]
            fields = edge_fields.get((seq.name, consumer), [])
            u = mapping.transformation(upstream)
            exp_name = "EXP_%s_%d" % (seq.name, i + 1)
            ports = [Port(name=p.name, datatype=p.datatype,
                          precision=p.precision, scale=p.scale)
                     for p in (u.ports if u else [])]
            start = int(cir.get("start_value", 1) or 1)
            inc = int(cir.get("increment_by", 1) or 1)
            if start == 1 and inc == 1:
                seq_expr = "ROW_NUMBER() OVER (ORDER BY 1)"
            else:      # honor Start Value / Increment By faithfully
                seq_expr = "(ROW_NUMBER() OVER (ORDER BY 1) - 1) * %d " \
                    "+ %d" % (inc, start)
            for from_f, to_f in fields or [("NEXTVAL", "NEXTVAL")]:
                ports.append(Port(name=to_f or from_f, datatype="bigint",
                                  expression=seq_expr))
                from .pc_sequence import fold_note
                fold_note(mapping, seq.name, consumer, to_f or from_f,
                          cir)
            node = Transformation(
                name=exp_name, type=TransformationType.EXPRESSION,
                ports=ports,
                properties={"synthesized_from": "sequence_generator",
                            "sequence": seq.name})
            mapping.transformations.append(node)
            for l in mapping.links:
                if l.from_transformation == upstream and \
                        l.to_transformation == consumer:
                    l.from_transformation = exp_name
            mapping.links.append(Link(upstream, exp_name))
            mapping.links = [l for l in mapping.links
                             if not (l.from_transformation == seq.name and
                                     l.to_transformation == consumer)]
            mapping.add_issue(
                IssueSeverity.INFO, "SEQUENCE_AS_ROW_NUMBER",
                "Sequence '%s' folded into the dataflow as ROW_NUMBER() "
                "before '%s'" % (seq.name, consumer),
                suggestion="Row numbers restart per run and have no gap "
                           "guarantees — use an identity column or hash "
                           "key for durable surrogates.")
        if ok:
            mapping.transformations = [t for t in mapping.transformations
                                       if t.name != seq.name]


_PASSIVE_TYPES = {TransformationType.EXPRESSION, TransformationType.SEQUENCE,
                  TransformationType.LOOKUP, TransformationType.SORTER}


def _fold_mapplet_bypasses(mapping: Mapping, mapplet_rewire: Dict[str, dict],
                           edge_fields: Dict[Tuple[str, str], List[str]]
                           ) -> None:
    """A connector that goes around an inlined mapplet (feeder -> consumer
    while the mapplet sits between them) is a diamond the SQL generators
    cannot render row-wise. When every inlined node is PASSIVE the mapplet
    preserves the row set, so the bypassed columns can ride the inlined
    chain instead: add them as pass-through ports on each mapplet node and
    drop the diamond edge. Active mapplets keep the diamond and get a
    MANUAL flag — folding would change row semantics."""
    for iname, rw in mapplet_rewire.items():
        entry, exit_ = rw["in_default"], rw["out_default"]
        if not entry or not exit_:
            continue
        members = [t for t in mapping.transformations
                   if t.name.startswith(iname + "_")]
        feeders = {l.from_transformation for l in mapping.links
                   if l.to_transformation == entry}
        consumers = {l.to_transformation for l in mapping.links
                     if l.from_transformation == exit_}
        bypass = [(f, t) for (f, t) in edge_fields
                  if f in feeders and t in consumers]
        if not bypass:
            continue
        if any(t.type not in _PASSIVE_TYPES for t in members):
            mapping.add_issue(
                IssueSeverity.MANUAL, "MAPPLET_ACTIVE_BYPASS",
                "Columns bypass active mapplet instance '%s' — the row set "
                "changes inside the mapplet, so the bypass cannot be "
                "folded automatically" % iname,
                suggestion="Re-join the bypassed columns to the mapplet "
                           "output on the mapplet's key in the target.")
            continue
        for frm, to in bypass:
            feeder = mapping.transformation(frm)
            for field, _tofield in edge_fields[(frm, to)]:
                src_port = feeder.port(field) if feeder else None
                for node in members:
                    if node.port(field) is None:
                        node.ports.append(Port(
                            name=field,
                            datatype=src_port.datatype if src_port
                            else "string",
                            precision=src_port.precision if src_port else 0,
                            scale=src_port.scale if src_port else 0))
            mapping.links = [l for l in mapping.links
                             if (l.from_transformation,
                                 l.to_transformation) != (frm, to)]


_STMT_KW_RE = re.compile(r"\b(select|with|insert|update|delete|merge)\b",
                         re.IGNORECASE)


def _recover_flattened_override(q: str) -> "Tuple[str, bool]":
    """XML attribute-value normalization turns raw newlines into spaces, so
    a multi-line override starting with a `--` comment arrives as one line
    with the whole statement commented out. Recover by re-inserting a
    newline before a statement keyword — accepted only if the result
    actually parses. Returns (sql, recovered)."""
    if "\n" in q or "--" not in q:
        return q, False
    import sqlglot
    try:
        parsed = sqlglot.parse(q, error_level=sqlglot.ErrorLevel.RAISE)
        if any(p is not None for p in parsed):
            return q, False
    except Exception:  # noqa: BLE001 — that is the case we recover from
        pass
    dash = q.index("--")
    for mo in _STMT_KW_RE.finditer(q, dash):
        candidate = q[:mo.start()] + "\n" + q[mo.start():]
        try:
            parsed = sqlglot.parse(candidate,
                                   error_level=sqlglot.ErrorLevel.RAISE)
            if any(p is not None for p in parsed):
                return candidate, True
        except Exception:  # noqa: BLE001
            continue
    return q, False


def _parse_transformation(xml_t: ET.Element, iname: str,
                          ir_type: TransformationType, mapping: Mapping) -> Transformation:
    ports: List[Port] = []
    group_by: List[str] = []
    for f in xml_t.findall("TRANSFORMFIELD"):
        pname = f.get("NAME", "")
        from .pc_model import normalize_port_type
        port = Port(name=pname, datatype=_canonical(f.get("DATATYPE")),
                    precision=int(f.get("PRECISION") or 0),
                    scale=int(f.get("SCALE") or 0),
                    direction=normalize_port_type(
                        f.get("PORTTYPE") or "INPUT_OUTPUT").value)
        expr = f.get("EXPRESSION", "")
        if (f.get("EXPRESSIONTYPE") or "").upper() == "GROUPBY":
            group_by.append(pname)
        if expr and expr != pname:
            sql = _expr_to_sql(expr, mapping, "%s.%s" % (iname, pname))
            if sql is not None:
                port.expression = sql
        ports.append(port)

    props: Dict[str, object] = {}
    attrs = {a.get("NAME", ""): a.get("VALUE", "") for a in xml_t.findall("TABLEATTRIBUTE")}
    if ir_type == TransformationType.SOURCE_QUALIFIER:
        from .pc_source_qualifier import normalize_override, parse_sq_attributes
        cfg = parse_sq_attributes(attrs)
        if cfg.source_filter:
            props["source_filter"] = cfg.source_filter
        if cfg.user_defined_join:
            props["user_defined_join"] = cfg.user_defined_join
        if cfg.sorted_ports:
            props["sorted_ports"] = cfg.sorted_ports
        if cfg.select_distinct:
            props["select_distinct"] = True
        if cfg.pre_sql:
            props["pre_sql"] = cfg.pre_sql
        if cfg.post_sql:
            props["post_sql"] = cfg.post_sql
        q = cfg.sql_query
        if q:
            q, recovered = _recover_flattened_override(q)
            props["sql_override"] = q
            ast_info = normalize_override(q)
            props["sql_override_ast"] = ast_info
            if not ast_info["parsed"]:
                mapping.add_issue(
                    IssueSeverity.MANUAL, "SQL_OVERRIDE_UNPARSEABLE",
                    "Source Qualifier SQL override does not parse — it "
                    "cannot be normalized into the CIR",
                    detail="%s | %s" % (q[:150],
                                        ast_info.get("error", "")),
                    suggestion="Fix the statement in the source repository "
                               "or convert this model manually.")
            if recovered:
                mapping.add_issue(
                    IssueSeverity.WARNING, "SQL_OVERRIDE_NEWLINES_LOST",
                    "SQL override newlines were lost to XML attribute "
                    "normalization; a `--` comment would have swallowed the "
                    "statement. Line structure was re-broken heuristically — "
                    "verify the recovered SQL.",
                    detail=q[:200],
                    suggestion="Re-export with newlines encoded as &#10; "
                               "(pmrep does this) or remove -- comments "
                               "from overrides.")
            mapping.add_issue(IssueSeverity.WARNING, "SQL_OVERRIDE_PASSTHROUGH",
                              "Source Qualifier SQL override carried into the dbt model as-is",
                              detail=q[:200],
                              suggestion="Review dialect compatibility of the embedded SQL.")
    elif ir_type == TransformationType.FILTER:
        cond = attrs.get("Filter Condition", "")
        props["condition"] = _expr_to_sql(cond, mapping, "%s filter" % iname) or "TRUE"
        # module 8: 3VL validation + NULL-behavior semantic-change detection
        from .pc_filter import apply_filter_analysis
        apply_filter_analysis(mapping, iname, props)
    elif ir_type == TransformationType.JOINER:
        from .pc_joiner import PC_JOIN_TYPES
        jt = attrs.get("Join Type", "Normal Join")
        props["join_type"] = PC_JOIN_TYPES.get(jt, "INNER")
        props["pc_join_type"] = jt
        props["condition"] = _expr_to_sql(attrs.get("Join Condition", ""),
                                          mapping, "%s join" % iname) or ""
        # master-side ports (PORTTYPE '.../MASTER') — resolved to the
        # master INPUT in the mapping-level post-pass (module 9)
        masters = [f.get("NAME", "") for f in xml_t.findall("TRANSFORMFIELD")
                   if "MASTER" in (f.get("PORTTYPE") or "").upper()]
        if masters:
            props["_master_ports"] = masters
        if (attrs.get("Sorted Input") or "NO").upper() == "YES":
            props["_sorted_input"] = True
    elif ir_type == TransformationType.SORTER:
        keys = []
        for spec in (attrs.get("Sort Keys", "") or "").split(","):
            spec = spec.strip()
            if spec:
                parts = spec.split()
                keys.append({"port": parts[0],
                             "order": parts[1] if len(parts) > 1 else "ASC"})
        props["sort_keys"] = keys
        props["distinct"] = attrs.get("Distinct", "NO").upper() == "YES"
        # module 18: case sensitivity + CIR SORT contract
        from .pc_sorter import enrich_sorter
        enrich_sorter(mapping, iname, props, attrs)
    elif ir_type == TransformationType.AGGREGATOR:
        props["group_by"] = group_by
        # module 11: CIR AGGREGATOR + FIRST/LAST + Sorted Input handling
        from .pc_aggregator import enrich_aggregator
        raw_exprs = {f.get("NAME", ""): f.get("EXPRESSION", "")
                     for f in xml_t.findall("TRANSFORMFIELD")
                     if f.get("EXPRESSION")}
        enrich_aggregator(mapping, iname, props, attrs, ports, raw_exprs)
    elif ir_type == TransformationType.RANK:
        props["group_by"] = group_by
        props["number_of_ranks"] = int(attrs.get("Number of Ranks", "1") or 1)
        props["top"] = (attrs.get("Top/Bottom", "Top").strip().lower() != "bottom")
        rank_port = next((f.get("NAME", "") for f in xml_t.findall("TRANSFORMFIELD")
                          if (f.get("PORTTYPE") or "").upper().find("RANK") >= 0), "")
        props["order_port"] = rank_port or (ports[0].name if ports else "")
        # module 14: CIR RANK, ties semantics, RANKINDEX exposure
        from .pc_rank import enrich_rank
        enrich_rank(mapping, iname, props, xml_t, ports)
    elif ir_type == TransformationType.ROUTER:
        groups = []
        for g in xml_t.findall("GROUP"):
            expr = g.get("EXPRESSION", "")
            cond = _expr_to_sql(expr, mapping, "%s group %s"
                                % (iname, g.get("NAME", ""))) if expr else ""
            if g.get("NAME"):
                groups.append({"name": g.get("NAME"),
                               "condition": cond or "",
                               "default": "DEFAULT" in
                               (g.get("TYPE") or "").upper() or not expr})
        props["groups"] = groups
        # per-port group + mirrored input (module 12 branch restructuring)
        props["port_groups"] = {
            f.get("NAME", ""): f.get("GROUP", "")
            for f in xml_t.findall("TRANSFORMFIELD") if f.get("GROUP")}
        props["port_refs"] = {
            f.get("NAME", ""): f.get("REF_FIELD", "")
            for f in xml_t.findall("TRANSFORMFIELD") if f.get("REF_FIELD")}
    elif ir_type == TransformationType.EXPRESSION:
        # module 7: dependency-ordered variable-port inlining + analysis
        from .pc_expression import inline_variable_ports
        analysis = inline_variable_ports(mapping, iname, ports)
        if analysis.variables:
            props["expression_analysis"] = analysis.to_dict()
    elif ir_type == TransformationType.UPDATE_STRATEGY:
        # module 16: DD_* routing -> CIR DML_ROUTING
        raw = attrs.get("Update Strategy Expression", "")
        sql = _expr_to_sql(raw, mapping, "%s update strategy" % iname)
        if sql:
            from .pc_update_strategy import parse_dml_routing
            props["condition"] = sql
            props["dml_routing_cir"] = parse_dml_routing(sql)
    elif ir_type == TransformationType.SEQUENCE:
        # module 15: start/increment/cycle/cache semantics
        from .pc_sequence import enrich_sequence
        enrich_sequence(mapping, iname, props, attrs)
    elif ir_type == TransformationType.UNION:
        # module 13: input groups + count/type/order validation
        from .pc_union import enrich_union
        enrich_union(mapping, iname, props, xml_t)
    elif ir_type == TransformationType.LOOKUP:
        props["table"] = attrs.get("Lookup table name", "")
        props["condition"] = _expr_to_sql(attrs.get("Lookup condition", ""),
                                          mapping, "%s lookup" % iname) or ""
        # module 10: full CIR LOOKUP contract, match policy, cache strategy
        from .pc_lookup import enrich_lookup
        enrich_lookup(mapping, iname, props, attrs, ports)
        mapping.add_issue(IssueSeverity.WARNING, "LOOKUP_AS_JOIN",
                          "Lookup '%s' converted to a LEFT JOIN in dbt" % iname,
                          suggestion="Verify multi-match semantics (lookup returns "
                                     "one row; join may return several).")
    return Transformation(name=iname, type=ir_type, ports=ports, properties=props)


def _expr_to_sql(expr: str, mapping: Mapping, context: str) -> Optional[str]:
    if not expr:
        return None
    try:
        return infa_to_sql(expr)
    except ExpressionError as e:
        mapping.add_issue(IssueSeverity.MANUAL, "EXPRESSION_UNCONVERTED",
                          "Informatica expression could not be converted to SQL (%s)" % context,
                          detail="%s | %s" % (expr, e),
                          suggestion="Convert manually or enable --llm-assist.")
        return None


def _parse_workflow_order(folder: ET.Element, pipeline: Pipeline) -> None:
    """Session links define mapping-level dependencies."""
    session_to_mapping = {}
    for wf in folder.findall("WORKFLOW"):
        for s in wf.findall("SESSION"):
            mn = s.get("MAPPINGNAME", "")
            session_to_mapping[s.get("NAME", "")] = mn[2:] if mn.startswith("m_") else mn
        for l in wf.findall("WORKFLOWLINK"):
            frm = session_to_mapping.get(l.get("FROMTASK", ""))
            to = session_to_mapping.get(l.get("TOTASK", ""))
            if frm and to:
                m = pipeline.mapping(to)
                if m is not None and frm not in m.depends_on:
                    m.depends_on.append(frm)
        for s in wf.findall("SESSION"):
            mn = session_to_mapping.get(s.get("NAME", ""))
            m = pipeline.mapping(mn) if mn else None
            if m is None:
                continue
            for a in s.findall("ATTRIBUTE"):
                if a.get("NAME") == "Treat source rows as":
                    v = (a.get("VALUE") or "").lower()
                    if "update" in v:
                        m.load_strategy = LoadStrategy.MERGE
                    elif "data driven" in v:
                        m.load_strategy = LoadStrategy.DELETE_INSERT
                if a.get("NAME") == "Truncate target table option" and \
                        (a.get("VALUE") or "").upper() == "YES":
                    m.load_strategy = LoadStrategy.FULL


def _infer_dependencies(pipeline: Pipeline) -> None:
    """If no workflow links exist, match target tables to other mappings' sources."""
    target_of = {}
    for m in pipeline.mappings:
        for t in m.by_type(TransformationType.TARGET):
            target_of[str(t.properties.get("table", "")).lower()] = m.name
    for m in pipeline.mappings:
        for s in m.by_type(TransformationType.SOURCE):
            src_table = str(s.properties.get("table", "")).lower()
            producer = target_of.get(src_table)
            if producer and producer != m.name and producer not in m.depends_on:
                m.depends_on.append(producer)

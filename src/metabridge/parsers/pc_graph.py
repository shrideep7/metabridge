"""Mapping graph builder (Phase 2, module 3).

A directed graph over the PowerCenter domain model (pc_model):

    nodes  SOURCE / TRANSFORMATION / TARGET / MAPPLET instances
    edges  CONNECTORs, preserving FROMINSTANCE, FROMFIELD, TOINSTANCE,
           TOFIELD, FROMINSTANCETYPE, TOINSTANCETYPE

Column lineage is expression-aware: inside a transformation, an output
port's contributors are the ports its EXPRESSION references (name-set
match); mapplet nodes resolve through their INTERNAL graph, so a trace
through a mapplet lands on the exact input port, never "all inputs".

``MappingGraph.validate()`` returns diagnostics for: orphan
transformations, missing connectors, invalid fields (a connector naming a
field the instance definition does not have), duplicate connectors,
cycles, and disconnected graph components.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .pc_model import PCFolder, PCMapping, PCMapplet, PCRepository

MAX_PATHS = 16
MAX_DEPTH = 60

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class MappingNode:
    name: str                       # instance name
    node_type: str                  # SOURCE | TRANSFORMATION | TARGET | MAPPLET
    transformation_name: str = ""
    transformation_type: str = ""   # 'Source Definition', 'Expression', ...
    fields: List[str] = field(default_factory=list)
    # output port -> set of contributing input ports (expression-aware)
    contributors: Dict[str, Set[str]] = field(default_factory=dict)
    metadata: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "node_type": self.node_type,
                "transformation_name": self.transformation_name,
                "transformation_type": self.transformation_type,
                "fields": list(self.fields)}


@dataclass
class MappingEdge:
    from_instance: str
    from_field: str
    to_instance: str
    to_field: str
    from_instance_type: str = ""
    to_instance_type: str = ""

    def key(self) -> Tuple[str, str, str, str]:
        return (self.from_instance, self.from_field,
                self.to_instance, self.to_field)

    def to_dict(self) -> dict:
        return {"from_instance": self.from_instance,
                "from_field": self.from_field,
                "to_instance": self.to_instance,
                "to_field": self.to_field,
                "from_instance_type": self.from_instance_type,
                "to_instance_type": self.to_instance_type}


class MappingGraph:
    def __init__(self, mapping_name: str):
        self.mapping_name = mapping_name
        self.nodes: Dict[str, MappingNode] = {}
        self.edges: List[MappingEdge] = []
        self._out: Dict[str, List[MappingEdge]] = {}
        self._in: Dict[str, List[MappingEdge]] = {}

    # ---- construction ---------------------------------------------------- #

    def add_node(self, node: MappingNode) -> None:
        self.nodes[node.name] = node
        self._out.setdefault(node.name, [])
        self._in.setdefault(node.name, [])

    def add_edge(self, edge: MappingEdge) -> None:
        self.edges.append(edge)
        self._out.setdefault(edge.from_instance, []).append(edge)
        self._in.setdefault(edge.to_instance, []).append(edge)

    # ---- navigation ------------------------------------------------------ #

    def get_upstream_nodes(self, name: str) -> List[MappingNode]:
        seen, out = set(), []
        for e in self._in.get(name, []):
            if e.from_instance not in seen and e.from_instance in self.nodes:
                seen.add(e.from_instance)
                out.append(self.nodes[e.from_instance])
        return out

    def get_downstream_nodes(self, name: str) -> List[MappingNode]:
        seen, out = set(), []
        for e in self._out.get(name, []):
            if e.to_instance not in seen and e.to_instance in self.nodes:
                seen.add(e.to_instance)
                out.append(self.nodes[e.to_instance])
        return out

    def get_source_nodes(self) -> List[MappingNode]:
        return [n for n in self.nodes.values() if n.node_type == "SOURCE"]

    def get_target_nodes(self) -> List[MappingNode]:
        return [n for n in self.nodes.values() if n.node_type == "TARGET"]

    def get_topological_order(self) -> List[str]:
        """Kahn's algorithm. Nodes trapped in cycles are appended at the
        end (deterministically) rather than dropped — see detect_cycles()."""
        indeg = {n: 0 for n in self.nodes}
        for e in self.edges:
            if e.to_instance in indeg and e.from_instance in self.nodes:
                indeg[e.to_instance] += 1
        order: List[str] = []
        frontier = sorted(n for n, d in indeg.items() if d == 0)
        while frontier:
            n = frontier.pop(0)
            order.append(n)
            dropped = []
            for e in self._out.get(n, []):
                if e.to_instance in indeg and e.to_instance not in order:
                    indeg[e.to_instance] -= 1
                    if indeg[e.to_instance] == 0 and \
                            e.to_instance not in dropped:
                        dropped.append(e.to_instance)
            for d in sorted(set(dropped)):
                if d not in frontier:
                    frontier.append(d)
            frontier.sort()
        order += sorted(n for n in self.nodes if n not in set(order))
        return order

    def detect_cycles(self) -> List[List[str]]:
        cycles: List[List[str]] = []
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self.nodes}
        stack: List[str] = []

        def dfs(n: str) -> None:
            color[n] = GRAY
            stack.append(n)
            for e in self._out.get(n, []):
                m = e.to_instance
                if m not in color:
                    continue
                if color[m] == GRAY:
                    cyc = stack[stack.index(m):] + [m]
                    if sorted(cyc[:-1]) not in [sorted(c[:-1])
                                                for c in cycles]:
                        cycles.append(cyc)
                elif color[m] == WHITE:
                    dfs(m)
            stack.pop()
            color[n] = BLACK

        for n in sorted(self.nodes):
            if color[n] == WHITE:
                dfs(n)
        return cycles

    # ---- column lineage ---------------------------------------------------#

    def _contributors(self, node: MappingNode, out_field: str) -> Set[str]:
        c = node.contributors.get(out_field.lower())
        if c:
            return c
        return {out_field.lower()}          # plain pass-through by name

    def trace_column_lineage(self, instance: str, column: str,
                             direction: str = "downstream"
                             ) -> List[List[str]]:
        """Full field-level paths from instance.column. Each path is a list
        of 'instance.field' steps."""
        paths: List[List[str]] = []

        def fwd(inst: str, col: str, path: List[str], depth: int) -> None:
            if depth > MAX_DEPTH or len(paths) >= MAX_PATHS:
                return
            step = "%s.%s" % (inst, col)
            hops = []
            for e in self._out.get(inst, []):
                node = self.nodes.get(inst)
                if node is None:
                    continue
                if col.lower() in self._contributors(node, e.from_field):
                    hops.append(e)
            if not hops:
                paths.append(path + [step])
                return
            for e in hops:
                if "%s.%s" % (e.to_instance, e.to_field) in path:
                    continue                        # cycle guard
                fwd(e.to_instance, e.to_field, path + [step], depth + 1)

        def back(inst: str, col: str, path: List[str], depth: int) -> None:
            if depth > MAX_DEPTH or len(paths) >= MAX_PATHS:
                return
            step = "%s.%s" % (inst, col)
            node = self.nodes.get(inst)
            wanted = self._contributors(node, col) if node else {col.lower()}
            hops = [e for e in self._in.get(inst, [])
                    if e.to_field.lower() in wanted]
            if not hops:
                paths.append([step] + path)
                return
            for e in hops:
                if "%s.%s" % (e.from_instance, e.from_field) in path:
                    continue
                back(e.from_instance, e.from_field, [step] + path, depth + 1)

        if direction == "upstream":
            back(instance, column, [], 0)
        else:
            fwd(instance, column, [], 0)
        return paths

    def trace_target_column_origin(self, target_instance: str,
                                   column: str) -> List[dict]:
        """Where does a target column ultimately come from? Returns the
        SOURCE-level origins with the full path as evidence."""
        origins: List[dict] = []
        for path in self.trace_column_lineage(target_instance, column,
                                              direction="upstream"):
            first_inst, first_field = path[0].rsplit(".", 1)
            node = self.nodes.get(first_inst)
            origins.append({
                "source_instance": first_inst,
                "source_field": first_field,
                "is_true_source": bool(node and node.node_type == "SOURCE"),
                "path": path,
            })
        return origins

    # ---- validation -------------------------------------------------------#

    def _components(self) -> List[List[str]]:
        seen: Set[str] = set()
        comps: List[List[str]] = []
        neighbors: Dict[str, Set[str]] = {n: set() for n in self.nodes}
        for e in self.edges:
            if e.from_instance in neighbors and e.to_instance in neighbors:
                neighbors[e.from_instance].add(e.to_instance)
                neighbors[e.to_instance].add(e.from_instance)
        for start in sorted(self.nodes):
            if start in seen:
                continue
            comp, frontier = [], [start]
            while frontier:
                n = frontier.pop()
                if n in seen:
                    continue
                seen.add(n)
                comp.append(n)
                frontier.extend(neighbors[n] - seen)
            comps.append(sorted(comp))
        return comps

    def validate(self) -> dict:
        orphans = [n.name for n in self.nodes.values()
                   if n.node_type in ("TRANSFORMATION", "MAPPLET")
                   and not self._in.get(n.name) and not self._out.get(n.name)]

        missing = []
        for n in self.nodes.values():
            if n.node_type == "SOURCE" and not self._out.get(n.name):
                missing.append("%s (SOURCE) has no outgoing connectors"
                               % n.name)
            elif n.node_type == "TARGET" and not self._in.get(n.name):
                missing.append("%s (TARGET) has no incoming connectors"
                               % n.name)
            elif n.node_type in ("TRANSFORMATION", "MAPPLET") and \
                    n.name not in orphans:
                ttype = (n.transformation_type or "").lower()
                generates_rows = "sequence" in ttype or \
                    "input transformation" in ttype
                if not self._in.get(n.name) and not generates_rows:
                    missing.append("%s (%s) has no incoming connectors"
                                   % (n.name, n.transformation_type or
                                      n.node_type))
                if not self._out.get(n.name):
                    missing.append("%s (%s) has no outgoing connectors"
                                   % (n.name, n.transformation_type or
                                      n.node_type))

        invalid = []
        for e in self.edges:
            for inst, fld, side in ((e.from_instance, e.from_field, "FROM"),
                                    (e.to_instance, e.to_field, "TO")):
                node = self.nodes.get(inst)
                if node is None:
                    invalid.append("connector references unknown instance "
                                   "'%s'" % inst)
                elif node.fields and fld and \
                        fld.lower() not in (f.lower() for f in node.fields):
                    invalid.append("%s field '%s.%s' does not exist on the "
                                   "instance definition" % (side, inst, fld))

        seen_keys: Set[Tuple[str, str, str, str]] = set()
        duplicates = []
        for e in self.edges:
            k = e.key()
            if k in seen_keys:
                duplicates.append("%s.%s -> %s.%s" % k)
            seen_keys.add(k)

        cycles = self.detect_cycles()
        comps = self._components()

        issues = {
            "orphan_transformations": sorted(orphans),
            "missing_connectors": sorted(missing),
            "invalid_fields": sorted(set(invalid)),
            "duplicate_connectors": sorted(set(duplicates)),
            "cycles": [" -> ".join(c) for c in cycles],
            "disconnected_components": [c for c in comps]
            if len(comps) > 1 else [],
        }
        return {
            "mapping": self.mapping_name,
            "ok": not any(issues.values()),
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "sources": len(self.get_source_nodes()),
            "targets": len(self.get_target_nodes()),
            "components": len(comps),
            "issues": issues,
        }

    def to_dict(self) -> dict:
        return {"mapping": self.mapping_name,
                "nodes": [n.to_dict() for n in self.nodes.values()],
                "edges": [e.to_dict() for e in self.edges],
                "topological_order": self.get_topological_order()}


# --------------------------------------------------------------------------- #
# builders                                                                     #
# --------------------------------------------------------------------------- #

def _expr_contributors(expression: str,
                       known_fields: List[str]) -> Set[str]:
    known = {f.lower() for f in known_fields}
    return {t.lower() for t in _IDENT_RE.findall(expression or "")
            if t.lower() in known}


def _node_contributors(fields) -> Dict[str, Set[str]]:
    """output port -> contributing CONNECTABLE ports for one transformation.

    Handles the port kinds a naive expression scan misses:
      * router-style ports: REF_FIELD names the mirrored input port
      * variable ports: they have no connectors, so references to them are
        expanded transitively until only input ports remain
    """
    from .pc_model import PCPortType
    names = [f.name for f in fields]
    variables = {f.name.lower() for f in fields
                 if f.port_type == PCPortType.VARIABLE}
    raw: Dict[str, Set[str]] = {}
    for f in fields:
        ref = (f.attributes.get("REF_FIELD") or "").strip()
        if ref:
            raw[f.name.lower()] = {ref.lower()}
        elif f.expression:
            raw[f.name.lower()] = _expr_contributors(f.expression, names)

    def expand(ports: Set[str], depth: int = 0) -> Set[str]:
        out: Set[str] = set()
        for p in ports:
            if p in variables and depth < 12:
                inner = raw.get(p)
                out |= expand(inner, depth + 1) if inner else {p}
            else:
                out.add(p)
        return out

    return {k: expand(v) for k, v in raw.items() if k not in variables}


def _mapplet_port_map(mp: PCMapplet):
    """(boundary ports, output_port -> contributing input ports,
    output_port -> internal expressions) — resolved by tracing the
    mapplet's INTERNAL graph."""
    inner = MappingGraph("mapplet:%s" % mp.name)
    tx_by_name = {t.name: t for t in mp.transformations}
    in_ports: List[str] = []
    out_ports: List[str] = []
    instances = mp.instances or [
        # exports may omit INSTANCE when definitions are 1:1
        type("I", (), {"name": t.name, "transformation_name": t.name,
                       "transformation_type": t.transformation_type})()
        for t in mp.transformations]
    expr_of: Dict[Tuple[str, str], str] = {}
    for inst in instances:
        t = tx_by_name.get(inst.transformation_name) or \
            tx_by_name.get(inst.name)
        fields = [f.name for f in t.fields] if t else []
        contributors = _node_contributors(t.fields) if t else {}
        if t:
            for f in t.fields:
                if f.expression:
                    expr_of[(inst.name, f.name.lower())] = f.expression
        ttype = (getattr(inst, "transformation_type", "") or
                 (t.transformation_type if t else "")).lower()
        inner.add_node(MappingNode(
            name=inst.name, node_type="TRANSFORMATION",
            transformation_name=inst.transformation_name,
            transformation_type=ttype, fields=fields,
            contributors=contributors))
        if "input" in ttype:
            in_ports.extend(fields)
        elif "output" in ttype:
            out_ports.extend(fields)
    for c in mp.connectors:
        inner.add_edge(MappingEdge(c.from_instance, c.from_field,
                                   c.to_instance, c.to_field))
    port_map: Dict[str, Set[str]] = {}
    port_exprs: Dict[str, List[str]] = {}
    out_nodes = [n for n in inner.nodes.values()
                 if "output" in n.transformation_type]
    in_names = {f.lower() for f in in_ports}
    for node in out_nodes:
        for f in node.fields:
            contributing: Set[str] = set()
            exprs: List[str] = []
            for path in inner.trace_column_lineage(node.name, f,
                                                   direction="upstream"):
                first = path[0].rsplit(".", 1)[1].lower()
                if first in in_names:
                    contributing.add(first)
                for step in path:
                    sinst, sfield = step.rsplit(".", 1)
                    e = expr_of.get((sinst, sfield.lower()))
                    if e and "%s = %s" % (step, e) not in exprs:
                        exprs.append("%s = %s" % (step, e))
            if not contributing:
                port_exprs.setdefault("__fallback__", []).append(f.lower())
            port_map[f.lower()] = contributing or in_names
            if exprs:
                port_exprs[f.lower()] = exprs
    return in_ports + out_ports, port_map, port_exprs


def build_mapping_graph(pc_mapping: PCMapping,
                        folder: Optional[PCFolder] = None) -> MappingGraph:
    g = MappingGraph(pc_mapping.name)
    sources = {s.name.lower(): s for s in (folder.sources if folder else [])}
    targets = {t.name.lower(): t for t in (folder.targets if folder else [])}
    mapplets = {m.name.lower(): m for m in
                (folder.mapplets if folder else [])}
    local_tx = {t.name: t for t in pc_mapping.transformations}
    reusable_tx = {t.name: t for t in
                   (folder.transformations if folder else [])}

    itype_of: Dict[str, str] = {}
    for inst in pc_mapping.instances:
        itype = (inst.instance_type or "TRANSFORMATION").upper()
        itype_of[inst.name] = inst.transformation_type or itype
        fields: List[str] = []
        contributors: Dict[str, Set[str]] = {}
        if itype == "SOURCE":
            sdef = sources.get(inst.transformation_name.lower())
            fields = [f.name for f in sdef.fields] if sdef else []
        elif itype == "TARGET":
            tdef = targets.get(inst.transformation_name.lower())
            fields = [f.name for f in tdef.fields] if tdef else []
        metadata: Dict[str, object] = {}
        if itype == "MAPPLET":
            mp = mapplets.get(inst.transformation_name.lower())
            if mp is not None:
                fields, contributors, port_exprs = _mapplet_port_map(mp)
                metadata["mapplet_expressions"] = {
                    k: v for k, v in port_exprs.items()
                    if k != "__fallback__"}
                if "__fallback__" in port_exprs:
                    metadata["mapplet_fallback_ports"] = \
                        port_exprs["__fallback__"]
        elif itype not in ("SOURCE", "TARGET"):
            t = local_tx.get(inst.transformation_name) or \
                local_tx.get(inst.name) or \
                reusable_tx.get(inst.transformation_name)
            if t is not None:
                fields = [f.name for f in t.fields]
                contributors = _node_contributors(t.fields)
        g.add_node(MappingNode(
            name=inst.name, node_type=itype,
            transformation_name=inst.transformation_name,
            transformation_type=inst.transformation_type,
            fields=fields, contributors=contributors,
            metadata=metadata))

    for c in pc_mapping.connectors:
        g.add_edge(MappingEdge(
            from_instance=c.from_instance, from_field=c.from_field,
            to_instance=c.to_instance, to_field=c.to_field,
            from_instance_type=c.attributes.get("FROMINSTANCETYPE", "") or
            itype_of.get(c.from_instance, ""),
            to_instance_type=c.attributes.get("TOINSTANCETYPE", "") or
            itype_of.get(c.to_instance, "")))
    return g


def build_mapping_graphs(model: PCRepository) -> Dict[str, MappingGraph]:
    """{'folder/mapping': MappingGraph} for every mapping in the model."""
    out: Dict[str, MappingGraph] = {}
    for folder in model.folders:
        for m in folder.mappings:
            out["%s/%s" % (folder.name, m.name)] = \
                build_mapping_graph(m, folder)
    return out

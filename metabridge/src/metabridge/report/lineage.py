"""Automatic data lineage: table-level, column-level, transformation-level.

Computed from the IR graph, so it works identically for every source format.
Column lineage walks backward from each target column through the
transformation ports — pass-through ports keep their name, derived ports
resolve to the columns referenced by their expression (extracted from the
AST, never regex) — producing full chains like:

    CRM.CUSTOMER.CUSTOMER_ID -> SQ_CUSTOMER.CUSTOMER_ID
      -> EXP_STANDARDIZE.CUSTOMER_ID -> DIM_CUSTOMER.CUSTOMER_KEY

SQL-override pipelines get lineage from the override's own projections (the
typed AST supplies per-projection source columns); when even that is not
possible the lineage is emitted as ``derivation: coarse`` — visible, never
silently precise.

Output: JSON (the contract) + Mermaid graph definitions for every level.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from ..ir.model import Mapping, Pipeline, Transformation, TransformationType

MAX_PATHS_PER_COLUMN = 8
MAX_DEPTH = 40


def _qualified(t: Transformation, col: str) -> str:
    table = str(t.properties.get("table", t.name))
    schema = str(t.properties.get("schema", "") or "")
    return "%s.%s.%s" % (schema, table, col) if schema else "%s.%s" % (table, col)


def _expression_columns(expression: str) -> List[str]:
    """Columns referenced by a derived-port expression (AST, not regex)."""
    try:
        import sqlglot
        from sqlglot import exp
        from ..sqlx.expressions import _shield_params
        tree = sqlglot.parse_one(_shield_params(expression))
        return sorted({c.name for c in tree.find_all(exp.Column)})
    except Exception:  # noqa: BLE001
        return []


class _MappingGraph:
    """The mapping graph with __OUTPUT__ resolved away."""

    def __init__(self, m: Mapping):
        self.m = m
        out = m.transformation("__OUTPUT__")
        self.upstream_of_output = str(out.properties.get("upstream", "")) \
            if out else ""
        self.upstreams: Dict[str, List[str]] = {}
        for l in m.links:
            frm, to = l.from_transformation, l.to_transformation
            if frm == "__OUTPUT__":
                frm = self.upstream_of_output
            if to == "__OUTPUT__":
                continue
            if frm and m.transformation(frm) and m.transformation(to):
                self.upstreams.setdefault(to, [])
                if frm not in self.upstreams[to]:
                    self.upstreams[to].append(frm)

    def ups(self, name: str) -> List[Transformation]:
        return [self.m.transformation(u) for u in self.upstreams.get(name, [])
                if self.m.transformation(u)]


def _override_projection_columns(t: Transformation, col: str) -> List[str]:
    """For SQL-override qualifiers: source columns of the projection that
    produces *col*, from the typed AST."""
    override = str(t.properties.get("sql_override", "") or "")
    if not override:
        return []
    try:
        from ..sqlx.ast import parse_statements
        for stmt in parse_statements(override):
            if stmt.select is None:
                continue
            for p in stmt.select.projections:
                if (p.alias or "").lower() == col.lower():
                    return p.source_columns or []
    except Exception:  # noqa: BLE001
        pass
    return []


def _trace(g: _MappingGraph, node: Transformation, col: str,
           visited: Set[Tuple[str, str]], depth: int = 0) -> List[List[str]]:
    """All source->here paths ending at node.col."""
    key = (node.name, col.lower())
    if key in visited or depth > MAX_DEPTH:
        return []
    visited = visited | {key}

    step = "%s.%s" % (node.name, col)
    if node.type == TransformationType.SOURCE:
        return [[_qualified(node, col)]]

    port = node.port(col)
    if port is not None and port.expression:
        input_cols = _expression_columns(port.expression) or [col]
    elif node.type == TransformationType.SOURCE_QUALIFIER and \
            node.properties.get("sql_override"):
        input_cols = _override_projection_columns(node, col) or []
    else:
        input_cols = [col]

    ups = g.ups(node.name)
    paths: List[List[str]] = []
    if not ups:
        return [[step]]
    for up in ups:
        for c in input_cols:
            has = up.port(c) is not None or \
                up.type == TransformationType.SOURCE
            if not has:
                continue
            for p in _trace(g, up, c, visited, depth + 1):
                paths.append(p + [step])
                if len(paths) >= MAX_PATHS_PER_COLUMN:
                    return paths
    return paths or [[step]]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def column_lineage(m: Mapping) -> List[dict]:
    g = _MappingGraph(m)
    out: List[dict] = []
    is_override = any(t.properties.get("sql_override")
                      for t in m.by_type(TransformationType.SOURCE_QUALIFIER))
    for tgt in m.by_type(TransformationType.TARGET):
        for p in tgt.ports:
            paths = _trace(g, tgt, p.name, set())
            # classify derivation
            derivation = "passthrough"
            for t in m.transformations:
                port = t.port(p.name)
                if port is not None and port.expression:
                    derivation = "expression"
                    break
            if is_override and all(len(path) <= 2 for path in paths):
                derivation = "coarse"
            out.append({
                "target_column": _qualified(tgt, p.name),
                "derivation": derivation,
                "paths": [path for path in paths],
            })
    return out


def transformation_lineage(m: Mapping) -> dict:
    g = _MappingGraph(m)
    nodes = [{"name": t.name, "type": t.type.value}
             for t in m.transformations if t.name != "__OUTPUT__"]
    edges = [{"from": frm, "to": to}
             for to, frms in g.upstreams.items() for frm in frms]
    return {"nodes": nodes, "edges": edges}


def table_lineage(pipeline: Pipeline) -> dict:
    nodes: Dict[str, dict] = {}
    edges: List[dict] = []
    target_of: Dict[str, str] = {}

    def add_node(nid: str, name: str, kind: str) -> None:
        nodes.setdefault(nid, {"id": nid, "name": name, "kind": kind})

    for m in pipeline.mappings:
        tgt_tables = [str(t.properties.get("table", t.name))
                      for t in m.by_type(TransformationType.TARGET)]
        for tt in tgt_tables:
            target_of[m.name] = tt
            add_node(tt, tt, "target")
    for m in pipeline.mappings:
        tgt = target_of.get(m.name, m.name)
        for s in m.by_type(TransformationType.SOURCE):
            table = str(s.properties.get("table", s.name))
            schema = str(s.properties.get("schema", "") or "")
            label = "%s.%s" % (schema, table) if schema else table
            kind = "target" if table in target_of.values() else "source"
            add_node(table, label, kind)
            edges.append({"from": table, "to": tgt, "via": m.name})
        for dep in m.depends_on:
            dep_tgt = target_of.get(dep)
            if dep_tgt and not any(e["from"] == dep_tgt and e["to"] == tgt
                                   for e in edges):
                edges.append({"from": dep_tgt, "to": tgt, "via": m.name})
    # dedupe
    seen = set()
    unique_edges = []
    for e in edges:
        k = (e["from"], e["to"], e["via"])
        if k not in seen:
            seen.add(k)
            unique_edges.append(e)
    return {"nodes": list(nodes.values()), "edges": unique_edges}


# ---------------------------------------------------------------------------
# Mermaid
# ---------------------------------------------------------------------------

def _mid(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def mermaid_table(table_graph: dict) -> str:
    lines = ["graph LR"]
    for n in table_graph["nodes"]:
        if n["kind"] == "source":
            lines.append('  %s[("%s")]' % (_mid(n["id"]), n["name"]))
        else:
            lines.append('  %s["%s"]' % (_mid(n["id"]), n["name"]))
    for e in table_graph["edges"]:
        lines.append("  %s -->|%s| %s" % (_mid(e["from"]), e["via"],
                                          _mid(e["to"])))
    return "\n".join(lines)


_SHAPES = {
    "SOURCE": '[("%s")]', "TARGET": '[("%s")]', "FILTER": '{"%s"}',
    "JOINER": '{{"%s"}}', "AGGREGATOR": '[/"%s"/]', "ROUTER": '{"%s"}',
}


def mermaid_transformations(m: Mapping) -> str:
    tl = transformation_lineage(m)
    lines = ["graph LR"]
    for n in tl["nodes"]:
        shape = _SHAPES.get(n["type"], '["%s"]')
        lines.append("  %s%s" % (_mid(n["name"]), shape % n["name"]))
    for e in tl["edges"]:
        lines.append("  %s --> %s" % (_mid(e["from"]), _mid(e["to"])))
    return "\n".join(lines)


def mermaid_column(entry: dict) -> str:
    lines = ["graph LR"]
    for pi, path in enumerate(entry["paths"]):
        for i in range(len(path) - 1):
            lines.append('  p%d_%d["%s"] --> p%d_%d["%s"]'
                         % (pi, i, path[i], pi, i + 1, path[i + 1]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project-level document
# ---------------------------------------------------------------------------

def build_lineage(pipeline: Pipeline) -> dict:
    tables = table_lineage(pipeline)
    doc = {
        "project": pipeline.name,
        "source_platform": pipeline.source_format,
        "table_lineage": tables,
        "mermaid": {"table_lineage": mermaid_table(tables)},
        "pipelines": [],
    }
    for m in pipeline.mappings:
        doc["pipelines"].append({
            "name": m.name,
            "transformation_lineage": transformation_lineage(m),
            "column_lineage": column_lineage(m),
            "mermaid": mermaid_transformations(m),
        })
    return doc


def write_lineage(doc: dict, out_dir: str) -> str:
    """lineage.json + lineage.md with embedded Mermaid blocks."""
    import json
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "lineage.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    lines = ["# Data lineage — %s" % doc["project"], "",
             "## Table-level lineage", "", "```mermaid",
             doc["mermaid"]["table_lineage"], "```", ""]
    for p in doc["pipelines"]:
        lines += ["## %s — transformation lineage" % p["name"], "",
                  "```mermaid", p["mermaid"], "```", "",
                  "### Column lineage", ""]
        for c in p["column_lineage"]:
            lines.append("- **%s** (%s)" % (c["target_column"],
                                            c["derivation"]))
            for path in c["paths"][:3]:
                lines.append("  - `%s`" % " -> ".join(path))
        lines.append("")
    (out / "lineage.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out / "lineage.json")

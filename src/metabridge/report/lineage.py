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
    # the GENERATED project's own lineage, when one was generated. Present
    # only for a dbt target, and only after generation has run.
    dbt = dbt_lineage(pipeline)
    if dbt is not None:
        doc["dbt_project"] = dbt
    return doc


def _mmd_document(doc: dict) -> str:
    """The lineage diagram as a standalone .mmd file.

    Mermaid fenced inside markdown only renders where the VIEWER supports it.
    A .mmd file is the diagram itself: mermaid.live, the mermaid CLI, the VS
    Code extension and GitHub all take it directly, it diffs cleanly, and it
    needs no renderer at generate time — which matters for an air-gapped
    install where a headless browser is not on the table.

    `%%` comment lines carry the provenance with the picture, so a diagram
    that travels on its own still says what it is and what it does not claim.
    """
    dbt = doc.get("dbt_project") or {}
    graph = dbt.get("mermaid") or doc["mermaid"]["table_lineage"]
    scope = "the generated dbt project" if dbt else "the source estate"

    # `%` is Mermaid's comment character, so these lines are built by
    # concatenation: %-formatting would collapse the `%%` marker to a single
    # `%` and the comment would stop being one.
    notes = []

    def note(text):
        notes.append("%% " + text)

    note("MetaBridge lineage")
    note("Project : " + str(doc["project"]))
    note("Scope   : " + scope)
    note("Source  : " + str(doc.get("source_platform") or "unknown"))
    if dbt:
        note("Built from the ref()/source() calls the generator emitted —")
        note("not re-derived, so it cannot claim an edge the artifacts")
        note("do not have.")
        if dbt.get("terminal_models"):
            note("Heavy border = terminal model: nothing downstream in this")
            note("project reads it, so this is where the estate's consumers")
            note("attach. Topology, not telemetry — it says nothing about")
            note("who actually does.")
        if dbt.get("unmanaged_relations"):
            note("Dashed = a relation this project reads but neither builds")
            note("nor declares as a source: a HOLE in the DAG. dbt cannot")
            note("see these at all, since `from SOME_TABLE` is valid SQL.")
    # comments go AFTER the graph directive, which every renderer accepts
    head, _, rest = graph.partition("\n")
    return "\n".join([head] + notes + ([rest] if rest else [])) + "\n"


def write_lineage(doc: dict, out_dir: str) -> str:
    """lineage.json + lineage.md + lineage.mmd (the diagram on its own)."""
    import json
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "lineage.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    (out / "lineage.mmd").write_text(_mmd_document(doc), encoding="utf-8")
    # the diagram DRAWN, for a reader who wants the picture rather than the
    # code for one. Silently skipped when reportlab is absent — PDF export
    # lives in the `web` extra and a core install still gets the rest.
    try:
        from .lineage_pdf import write_lineage_pdf
        write_lineage_pdf(doc, str(out / "lineage.pdf"))
    except Exception:                                    # noqa: BLE001
        pass
    lines = ["# Data lineage — %s" % doc["project"], ""]
    # The generated project first: it is what the reader has in front of them.
    # The legacy estate's own lineage follows, in its own names.
    if doc.get("dbt_project"):
        lines += _dbt_section(doc["dbt_project"])
    lines += ["## Table-level lineage (source estate)", "", "```mermaid",
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


# ---------------------------------------------------------------------------
# Lineage over the GENERATED dbt project
#
# Distinct from everything above, which describes the LEGACY estate in its own
# names — PowerCenter mappings, source and target tables. Useful for the estate
# conversation, and useless to the engineer holding the converted project, who
# needs to know that stg_crm__customers feeds fct_customer_orders.
#
# Built from the refs the generator actually emitted
# (pipeline.metadata['dbt_graph']), not re-derived from the IR. A lineage
# diagram computed a second way drifts from the artifacts it claims to
# describe, and the drift is invisible: the picture still looks complete.
# ---------------------------------------------------------------------------

_DBT_LAYERS = (
    ("sources", "Sources — source()"),
    ("staging", "Staging — stg_"),
    ("intermediate", "Intermediate — int_"),
    ("marts", "Marts — dim_ / fct_"),
    ("snapshots", "Snapshots — snap_"),
    ("unmanaged", "Unmanaged relations"),
)

_DBT_SHAPES = {"source": '[("%s")]', "model": '["%s"]',
               "snapshot": '[/"%s"/]', "unmanaged": '{{"%s"}}'}


def mermaid_dbt(graph: dict) -> str:
    """The dbt DAG as layered swimlanes.

    Unmanaged relations get a dashed edge and a dashed border, so a model that
    reads something dbt does not manage reads as a BREAK in the graph rather
    than as an ordinary upstream. Terminal models get a heavy border: nothing
    downstream in the project reads them, so that is where the estate's
    consumers attach.
    """
    lines = ["graph LR"]
    by_layer: Dict[str, List[dict]] = {}
    for n in graph.get("nodes", []):
        by_layer.setdefault(n.get("layer") or "other", []).append(n)
    for layer, title in _DBT_LAYERS:
        members = by_layer.get(layer)
        if not members:
            continue
        # prefixed: a bare `unmanaged` subgraph id collides with the
        # classDef of the same name below
        lines.append('  subgraph layer_%s["%s"]' % (_mid(layer), title))
        for n in members:
            shape = _DBT_SHAPES.get(n["kind"], '["%s"]')
            lines.append("    %s%s" % (_mid(n["name"]),
                                       shape % (n.get("label") or n["name"])))
        lines.append("  end")
    for e in graph.get("edges", []):
        lines.append("  %s %s %s" % (_mid(e["from"]),
                                     "-.->" if e["kind"] == "unmanaged"
                                     else "-->", _mid(e["to"])))

    def _style(names, cls, style):
        if names:
            lines.append("  classDef %s %s" % (cls, style))
            lines.append("  class %s %s"
                         % (",".join(sorted(_mid(n) for n in names)), cls))

    _style([n["name"] for n in graph.get("nodes", []) if n.get("terminal")],
           "terminal", "stroke-width:3px")
    _style([n["name"] for n in graph.get("nodes", [])
            if n["kind"] == "unmanaged"],
           "unmanaged", "stroke-dasharray:4 3")
    return "\n".join(lines)


def dbt_lineage(pipeline: Pipeline) -> Optional[dict]:
    """The generated project's own lineage, or None when nothing was emitted.

    Returns the typed graph plus a Mermaid rendering, the terminal models (the
    consumption boundary), and any relation the project reads but does not
    manage.
    """
    graph = (pipeline.metadata or {}).get("dbt_graph") or {}
    if not graph.get("nodes"):
        return None
    doc = dict(graph)
    doc["mermaid"] = mermaid_dbt(graph)
    doc["terminal_models"] = sorted(n["name"] for n in graph["nodes"]
                                    if n.get("terminal"))
    # model name -> the relation it actually builds, where the two differ
    doc["relation_of"] = {n["name"]: n["relation"] for n in graph["nodes"]
                          if n.get("relation")
                          and n["relation"] != n["name"]}
    return doc


def _dbt_section(dbt: dict) -> List[str]:
    counts = dbt.get("counts", {})
    lines = [
        "## dbt project lineage",
        "",
        "%d source(s), %d model(s), %d snapshot(s) and %d edge(s), read from "
        "the ref() and source() calls the generator emitted."
        % (counts.get("source", 0), counts.get("model", 0),
           counts.get("snapshot", 0), len(dbt.get("edges", []))),
        "",
        "```mermaid",
        dbt["mermaid"],
        "```",
        "",
    ]
    if dbt.get("terminal_models"):
        lines += [
            "### Consumption boundary",
            "",
            "Nothing downstream in this project reads these models, so this "
            "is where the estate's own consumers — dashboards, extracts, "
            "downstream systems — attach:",
            "",
        ]
        relation_of = dbt.get("relation_of") or {}
        for name in dbt["terminal_models"]:
            relation = relation_of.get(name)
            lines.append(
                "- `%s` — builds the relation `%s`" % (name, relation)
                if relation else "- `%s`" % name)
        if any(relation_of.get(n) for n in dbt["terminal_models"]):
            lines += [
                "",
                "The model and the relation are named differently on "
                "purpose. The project keeps dbt's conventions in its own "
                "files; the warehouse keeps the name the estate already "
                "uses, so a consumer reading the legacy relation keeps "
                "working after cutover instead of being repointed on the "
                "same day everything else moves.",
            ]
        lines += [
            "",
            "This is **topology, not telemetry**: it says nothing about who "
            "actually reads them. No dbt `exposure` is generated for that "
            "reason — an exposure is a record about a real downstream asset, "
            "and inventing one per terminal model would fabricate consumers "
            "and owners that do not exist.",
            "",
        ]
    if dbt.get("unmanaged_relations"):
        lines += [
            "### Unmanaged relations",
            "",
            "These are read by a model but neither built by this project nor "
            "declared as a source, so dbt has no node for them and draws no "
            "edge. They are the **holes** in the DAG above, shown dashed:",
            "",
        ]
        lines += ["- `%s`" % r for r in dbt["unmanaged_relations"]]
        lines += [
            "",
            "Each one is resolved by adding it to the source manifest, "
            "converting the object that builds it, or — for a small static "
            "reference table — checking it in as a dbt seed.",
            "",
        ]
    return lines

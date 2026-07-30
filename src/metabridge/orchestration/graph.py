"""Execution graph exports for COR workflows (Command 6, §6/§7).

JSON (UI + API), Mermaid (docs/inline rendering), GraphML (enterprise
graph tools), plus workflow/task/cross-workflow lineage.
"""
from __future__ import annotations

from typing import Dict, List
from xml.sax.saxutils import escape

from .cor import COR, Workflow

_EDGE_STYLE = {"success": "-->", "failure": "-. failure .->",
               "always": "==>", "conditional": "-. if .->",
               "event": "-. event .->"}


def execution_graph(wf: Workflow) -> dict:
    """JSON execution graph: typed nodes, typed edges, parallel waves."""
    waves = wf.execution_waves()
    wave_of = {k: i for i, wave in enumerate(waves) for k in wave}
    return {
        "workflow": wf.name,
        "platform": wf.platform,
        "nodes": [{
            "id": t.key, "label": t.name or t.key, "type": t.type,
            "wave": wave_of.get(t.key, -1),
            "retries": t.retry.max_attempts,
            "timeout_seconds": t.timeout_seconds,
            **({"condition": t.condition} if t.condition else {}),
        } for t in wf.tasks],
        "edges": [d.to_dict() for d in wf.dependencies],
        "waves": waves,
        "parallel": [wave for wave in waves if len(wave) > 1],
        "failure_paths": [[d.from_task, d.to_task]
                          for d in wf.dependencies if d.kind == "failure"],
        "success_paths": [[d.from_task, d.to_task]
                          for d in wf.dependencies if d.kind == "success"],
    }


def to_mermaid(wf: Workflow) -> str:
    lines = ["flowchart TD"]
    shape = {"choice": ("{", "}"), "parallel": ("[[", "]]"),
             "sensor": ("([", "])"), "wait": ("([", "])"),
             "loop": ("[[", "]]")}
    for t in wf.tasks:
        o, c = shape.get(t.type, ("[", "]"))
        label = (t.name or t.key).replace('"', "'")
        lines.append('  %s%s"%s<br/><i>%s</i>"%s'
                     % (_mid(t.key), o, label, t.type, c))
    for d in wf.dependencies:
        lines.append("  %s %s %s" % (_mid(d.from_task),
                                     _EDGE_STYLE.get(d.kind, "-->"),
                                     _mid(d.to_task)))
    return "\n".join(lines)


def _mid(key: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in key)


def to_graphml(wf: Workflow) -> str:
    nodes = "\n".join(
        '    <node id="%s"><data key="type">%s</data>'
        '<data key="label">%s</data></node>'
        % (escape(t.key), escape(t.type), escape(t.name or t.key))
        for t in wf.tasks)
    edges = "\n".join(
        '    <edge source="%s" target="%s"><data key="kind">%s</data></edge>'
        % (escape(d.from_task), escape(d.to_task), escape(d.kind))
        for d in wf.dependencies)
    return """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <key id="type" for="node" attr.name="type" attr.type="string"/>
  <key id="label" for="node" attr.name="label" attr.type="string"/>
  <key id="kind" for="edge" attr.name="kind" attr.type="string"/>
  <graph id="%s" edgedefault="directed">
%s
%s
  </graph>
</graphml>
""" % (escape(wf.name), nodes, edges)


# ---------------------------------------------------------------------------
# lineage (§7)
# ---------------------------------------------------------------------------

def orchestration_lineage(cor: COR) -> dict:
    """Workflow / task / cross-workflow orchestration lineage."""
    wf_edges: List[dict] = []
    wf_names = {w.name for w in cor.workflows}
    for w in cor.workflows:
        for t in w.tasks:
            child = str(t.action.get("workflow", "")
                        or t.action.get("job", "")
                        or t.action.get("pipeline_ref", ""))
            if t.type == "subworkflow" and child:
                wf_edges.append({"from": w.name, "to": child,
                                 "via": t.key,
                                 "resolved": child in wf_names})
    return {
        "source_platform": cor.source_platform,
        "workflows": [w.name for w in cor.workflows],
        "workflow_lineage": wf_edges,
        "task_lineage": {
            w.name: [d.to_dict() for d in w.dependencies]
            for w in cor.workflows},
        "execution_lineage": {
            w.name: w.execution_waves() for w in cor.workflows},
        "external_references": sorted({
            e["to"] for e in wf_edges if not e["resolved"]}),
    }

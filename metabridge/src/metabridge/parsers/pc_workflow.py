"""Workflow parser -> WORKFLOW DAG CIR (Phase 2, module 25).

Every orchestration object PowerCenter has becomes a typed DAG node:

    session, worklet (nested DAG preserved), command, email, decision,
    timer, event wait, event raise, assignment, control, start

and every WORKFLOWLINK becomes a typed edge. Link conditions are
classified, never dropped:

    $s.Status = SUCCEEDED   ->  kind=success
    $s.Status = FAILED      ->  kind=failure
    <empty>                 ->  kind=always
    anything else           ->  kind=conditional (expression preserved)

The DAG preserves task dependencies, execution order (topological),
success paths and failure paths — the source of truth for the
orchestration generators (dbt job spec, Databricks Workflow spec,
engine-neutral spec for other warehouses).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# PC TASKTYPE / TASK TYPE -> canonical node type
_TASK_TYPE_NORM = {
    "session": "session", "worklet": "worklet", "start": "start",
    "command": "command", "email": "email", "decision": "decision",
    "timer": "timer", "event wait": "event_wait",
    "event raise": "event_raise", "event-wait": "event_wait",
    "event-raise": "event_raise", "assignment": "assignment",
    "control": "control",
}


def classify_link_condition(cond: Optional[str]) -> Tuple[str, str]:
    """WORKFLOWLINK condition -> (kind, preserved expression)."""
    c = (cond or "").strip()
    if not c:
        return "always", ""
    u = c.upper()
    if "SUCCEEDED" in u:
        return "success", c
    if "FAILED" in u:
        return "failure", c
    return "conditional", c


def _node_type(raw: str) -> str:
    return _TASK_TYPE_NORM.get((raw or "").strip().lower(), "task")


def _topo(keys: List[str], edges: List[dict]) -> List[str]:
    incoming: Dict[str, set] = {k: set() for k in keys}
    outgoing: Dict[str, List[str]] = {k: [] for k in keys}
    for e in edges:
        if e["from"] in incoming and e["to"] in incoming:
            incoming[e["to"]].add(e["from"])
            outgoing[e["from"]].append(e["to"])
    order, ready = [], [k for k in keys if not incoming[k]]
    while ready:
        k = ready.pop(0)
        order.append(k)
        for nxt in outgoing[k]:
            incoming[nxt].discard(k)
            if not incoming[nxt] and nxt not in order and nxt not in ready:
                ready.append(nxt)
    for k in keys:                      # cycles: append, flagged by caller
        if k not in order:
            order.append(k)
    return order


def build_workflow_dag(wf: dict,
                       folder_sessions: Optional[Dict[str, dict]] = None,
                       folder_task_defs: Optional[Dict[str, dict]] = None,
                       folder_worklets: Optional[Dict[str, dict]] = None,
                       ) -> dict:
    """One parsed WORKFLOW/WORKLET dict -> WORKFLOW DAG CIR."""
    sessions = dict(folder_sessions or {})
    sessions.update(wf.get("sessions") or {})
    task_defs = dict(folder_task_defs or {})
    task_defs.update(wf.get("task_defs") or {})
    worklets = dict(folder_worklets or {})
    worklets.update({k: v for k, v in (wf.get("worklets") or {}).items()
                     if v})

    nodes: List[dict] = []
    seen: Dict[str, dict] = {}

    def add_node(key: str, task: str, raw_type: str) -> dict:
        node = {"task_key": key, "task": task, "type": _node_type(raw_type)}
        d = task_defs.get(task)
        if node["type"] == "task" and d:
            node["type"] = _node_type(d.get("type", ""))
        if task in sessions and node["type"] in ("task", "session"):
            node["type"] = "session"
            node["mapping"] = sessions[task].get("mapping", "")
        elif task in worklets and node["type"] in ("task", "worklet"):
            node["type"] = "worklet"
            node["worklet_dag"] = build_workflow_dag(
                worklets[task], folder_sessions, folder_task_defs,
                folder_worklets)
        elif d:
            cfg = {k: v for k, v in (d.get("attributes") or {}).items()
                   if v}
            if d.get("values"):
                cfg["commands"] = list(d["values"])
            if cfg:
                node["config"] = cfg
        nodes.append(node)
        seen[key] = node
        return node

    for t in wf.get("tasks") or []:
        key = t.get("instance") or t.get("task") or ""
        if key and key not in seen:
            add_node(key, t.get("task") or key, t.get("type", ""))

    edges: List[dict] = []
    for l in wf.get("links") or []:
        frm, to = l.get("from", ""), l.get("to", "")
        for endpoint in (frm, to):
            if endpoint and endpoint not in seen:
                add_node(endpoint, endpoint,
                         "start" if endpoint.lower() == "start" else "")
        if frm and to:
            kind, cond = classify_link_condition(l.get("condition"))
            edges.append({"from": frm, "to": to,
                          "kind": kind, "condition": cond})

    order = _topo([n["task_key"] for n in nodes], edges)
    return {
        "workflow": wf.get("name", ""),
        "kind": wf.get("kind", "workflow"),
        "nodes": nodes,
        "edges": edges,
        "execution_order": order,
        "success_paths": [[e["from"], e["to"]] for e in edges
                          if e["kind"] == "success"],
        "failure_paths": [[e["from"], e["to"]] for e in edges
                          if e["kind"] == "failure"],
    }

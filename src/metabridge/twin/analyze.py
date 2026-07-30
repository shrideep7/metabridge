"""Digital Twin analytics — deterministic graph analysis.

    application_dependency_graph   estate collapsed to applications
    data_flow_graph                table -> pipeline -> table flows
    business_capability_map        domains -> applications/products
    technology_inventory           counts by technology and kind
    application_landscape          apps x domains with size/complexity
    blast_radius(node)             everything downstream, by depth+layer
    root_cause(node)               upstream candidates ranked by
                                   proximity and fan-out
    simulate_migration(selection)  waves, co-migration groups, external
                                   consumers affected per wave
    impact_analysis(node)          blast radius + criticality summary
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .model import DigitalTwin, TwinNode

_FLOW_KINDS = {"feeds", "writes", "reads", "produces", "consumes",
               "serves", "depends_on"}


def _flow_adj(twin: DigitalTwin, reverse: bool = False
              ) -> Dict[str, List[str]]:
    """Flow adjacency with each neighbour listed once — two objects can
    be joined by parallel edges of different kinds (a table both feeds
    and is read by a pipeline), and counting those twice would inflate
    fan-out scores and in-degrees."""
    adj: Dict[str, List[str]] = {n: [] for n in twin.nodes}
    seen: Dict[str, set] = {n: set() for n in twin.nodes}
    for e in twin.edges.values():
        if e.kind not in _FLOW_KINDS:
            continue
        a, b = (e.to_id, e.from_id) if reverse else (e.from_id, e.to_id)
        if b not in seen[a]:
            seen[a].add(b)
            adj[a].append(b)
    return adj


def _sccs(nodes: set, succ: Dict[str, List[str]]) -> List[List[str]]:
    """Tarjan's strongly-connected components (iterative) over the
    subgraph induced on ``nodes``. Used to break dependency cycles into
    minimal co-migration groups without dragging in downstream nodes."""
    idx: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on: set = set()
    stack: List[str] = []
    order = [0]
    out: List[List[str]] = []
    for root in sorted(nodes):
        if root in idx:
            continue
        work = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                idx[node] = low[node] = order[0]
                order[0] += 1
                stack.append(node)
                on.add(node)
            succs = [s for s in succ.get(node, []) if s in nodes]
            if pi < len(succs):
                work[-1] = (node, pi + 1)
                w = succs[pi]
                if w not in idx:
                    work.append((w, 0))
                elif w in on:
                    low[node] = min(low[node], idx[w])
            else:
                if low[node] == idx[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    out.append(comp)
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
    return out


def _bfs(adj: Dict[str, List[str]], start: str) -> Dict[str, int]:
    depth = {start: 0}
    frontier = [start]
    while frontier:
        nxt = []
        for nid in frontier:
            for o in adj.get(nid, []):
                if o not in depth:
                    depth[o] = depth[nid] + 1
                    nxt.append(o)
        frontier = nxt
    depth.pop(start, None)
    return depth


# ---------------------------------------------------------------------------
# graphs and maps
# ---------------------------------------------------------------------------

def application_dependency_graph(twin: DigitalTwin) -> dict:
    """Collapse to applications: A depends on B when any object in A
    consumes an object contained in B."""
    owner_app: Dict[str, str] = {}
    for e in twin.edges.values():
        if e.kind == "contains" and \
                twin.nodes[e.from_id].kind == "application":
            owner_app[e.to_id] = e.from_id
    deps: Dict[tuple, List[str]] = {}
    for e in twin.edges.values():
        if e.kind not in _FLOW_KINDS:
            continue
        a_from = owner_app.get(e.from_id, e.from_id
                               if twin.nodes[e.from_id].kind ==
                               "application" else "")
        a_to = owner_app.get(e.to_id, e.to_id
                             if twin.nodes[e.to_id].kind ==
                             "application" else "")
        if a_from and a_to and a_from != a_to:
            deps.setdefault((a_to, a_from), []).append(
                "%s -> %s" % (twin.nodes[e.from_id].name,
                              twin.nodes[e.to_id].name))
    apps = [n.to_dict() for n in twin.nodes.values()
            if n.kind == "application"]
    return {"applications": apps,
            "dependencies": [{"application": twin.nodes[a].name,
                              "depends_on": twin.nodes[b].name,
                              "via": v[:6]}
                             for (a, b), v in deps.items()]}


def data_flow_graph(twin: DigitalTwin) -> dict:
    keep = {"table", "pipeline", "topic", "streaming_job", "api",
            "dashboard"}
    nodes = [n.to_dict() for n in twin.nodes.values() if n.kind in keep]
    ids = {n["id"] for n in nodes}
    edges = [e.to_dict() for e in twin.edges.values()
             if e.kind in _FLOW_KINDS and e.from_id in ids
             and e.to_id in ids]
    return {"nodes": nodes, "edges": edges}


def business_capability_map(twin: DigitalTwin) -> dict:
    domains: Dict[str, dict] = {}
    for n in twin.nodes.values():
        if n.kind in ("domain", "owner"):
            continue
        d = n.domain or "(unassigned)"
        entry = domains.setdefault(d, {"domain": d, "applications": set(),
                                       "data_products": set(),
                                       "objects": 0, "inferred": True})
        entry["objects"] += 1
        if n.kind == "application":
            entry["applications"].add(n.name)
        if n.kind == "data_product":
            entry["data_products"].add(n.name)
    # a grouping is "declared" only when a descriptor domain node
    # backs it — non-inferred member objects don't promote it
    for dn in twin.nodes.values():
        if dn.kind == "domain" and not dn.inferred and \
                dn.name in domains:
            domains[dn.name]["inferred"] = False
            if dn.owner:
                domains[dn.name]["owner"] = dn.owner
    return {"domains": [
        {**d, "applications": sorted(d["applications"]),
         "data_products": sorted(d["data_products"])}
        for d in sorted(domains.values(), key=lambda x: -x["objects"])]}


def technology_inventory(twin: DigitalTwin) -> dict:
    by_tech: Dict[str, Dict[str, int]] = {}
    for n in twin.nodes.values():
        tech = n.technology or "(untagged)"
        by_tech.setdefault(tech, {})[n.kind] = \
            by_tech.setdefault(tech, {}).get(n.kind, 0) + 1
    return {"technologies": [
        {"technology": t, "total": sum(kinds.values()), "by_kind": kinds}
        for t, kinds in sorted(by_tech.items(),
                               key=lambda x: -sum(x[1].values()))]}


def application_landscape(twin: DigitalTwin) -> dict:
    rows = []
    for n in twin.nodes.values():
        if n.kind != "application":
            continue
        contained_ids = {e.to_id for e in twin.out_edges(n.id) if e.kind in ("contains", "writes")}
        fed_ids = {e.from_id for e in twin.in_edges(n.id) if e.kind == "feeds"}
        related = [twin.nodes[nid] for nid in (contained_ids | fed_ids) if nid in twin.nodes]
        rows.append({
            "application": n.name, "technology": n.technology,
            "domain": n.domain or "(unassigned)",
            "owner": n.owner,
            "pipelines": sum(1 for c in related
                             if c.kind in ("pipeline",
                                           "streaming_job")),
            "tables": sum(1 for c in related if c.kind == "table"),
            "topics": sum(1 for c in related if c.kind == "topic"),
            "workflows": sum(1 for c in related
                             if c.kind == "workflow"),
        })
    return {"applications": sorted(rows,
                                   key=lambda r: -(r["pipelines"]
                                                   + r["tables"]))}


# ---------------------------------------------------------------------------
# blast radius / root cause / impact / simulation
# ---------------------------------------------------------------------------

def blast_radius(twin: DigitalTwin, name_or_id: str) -> dict:
    node = twin.find(name_or_id)
    if node is None:
        return {"error": "unknown node: %s" % name_or_id}
    depth = _bfs(_flow_adj(twin), node.id)
    affected = [{"id": nid, "name": twin.nodes[nid].name,
                 "kind": twin.nodes[nid].kind, "depth": d}
                for nid, d in sorted(depth.items(),
                                     key=lambda x: (x[1], x[0]))]
    by_kind: Dict[str, int] = {}
    for a in affected:
        by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1
    return {"node": node.to_dict(), "affected": affected,
            "affected_total": len(affected), "by_kind": by_kind,
            "max_depth": max(depth.values()) if depth else 0,
            "business_endpoints": [a for a in affected
                                   if a["kind"] in ("dashboard", "api",
                                                    "data_product",
                                                    "consumer")]}


def root_cause(twin: DigitalTwin, name_or_id: str) -> dict:
    node = twin.find(name_or_id)
    if node is None:
        return {"error": "unknown node: %s" % name_or_id}
    depth = _bfs(_flow_adj(twin, reverse=True), node.id)
    fwd = _flow_adj(twin)
    candidates = []
    for nid, d in depth.items():
        n = twin.nodes[nid]
        fan_out = len(fwd.get(nid, []))
        # closer + wider-reaching upstream nodes rank higher
        score = round(fan_out / d, 2) if d else fan_out
        candidates.append({"id": nid, "name": n.name, "kind": n.kind,
                           "distance": d, "fan_out": fan_out,
                           "score": score})
    candidates.sort(key=lambda c: (-c["score"], c["distance"]))
    return {"node": node.to_dict(),
            "upstream_total": len(candidates),
            "candidates": candidates[:15],
            "note": "candidates ranked by fan-out/proximity — confirm "
                    "with runtime monitoring; the twin is topology, "
                    "not telemetry"}


def impact_analysis(twin: DigitalTwin, name_or_id: str) -> dict:
    br = blast_radius(twin, name_or_id)
    if "error" in br:
        return br
    endpoints = br["business_endpoints"]
    level = ("HIGH" if len(endpoints) >= 3 or br["affected_total"] >= 15
             else "MEDIUM" if endpoints or br["affected_total"] >= 5
             else "LOW")
    return {**br, "impact_level": level,
            "summary": "changing '%s' reaches %d object(s) across %d "
                       "kind(s); %d business endpoint(s) affected — "
                       "impact %s"
                       % (br["node"]["name"], br["affected_total"],
                          len(br["by_kind"]), len(endpoints), level)}


def simulate_migration(twin: DigitalTwin,
                       selection: Optional[List[str]] = None,
                       technology: str = "") -> dict:
    """Simulate migrating a set of nodes (or everything of one
    technology): dependency-ordered waves, co-migration groups, and the
    external consumers affected at each wave."""
    if technology and not selection:
        selected = {n.id for n in twin.nodes.values()
                    if str(n.technology).lower() == str(technology).lower()
                    and n.kind in ("pipeline", "streaming_job",
                                   "workflow", "table", "topic",
                                   "application", "api", "dashboard")}
    else:
        selected = set()
        for s in selection or []:
            n = twin.find(s)
            if n is not None:
                selected.add(n.id)
    if not selected:
        return {"error": "empty selection — pass node names or a "
                         "technology"}
    fwd = _flow_adj(twin)
    # waves: Kahn over dependency edges *within* the selection. Count
    # each dependency once (parallel edges must not double the degree,
    # or a node would never reach zero and be misread as a cycle).
    incoming: Dict[str, int] = {nid: 0 for nid in selected}
    counted: set = set()
    for nid in selected:
        for o in fwd.get(nid, []):
            if o in selected and (nid, o) not in counted:
                counted.add((nid, o))
                incoming[o] += 1
    waves: List[List[str]] = []
    remaining = dict(incoming)
    while remaining:
        ready = sorted(nid for nid, deg in remaining.items()
                       if deg == 0)
        if not ready:
            # a genuine cycle stalls Kahn: release the *source* SCCs
            # (cycles with no unmet dependency outside themselves) so a
            # knot co-migrates as one group while nodes merely
            # downstream of it still wait for their own wave
            rem = set(remaining)
            comps = _sccs(rem, fwd)
            comp_of = {n: i for i, c in enumerate(comps)
                       for n in c}
            blocked = set()
            for nid in rem:
                for o in fwd.get(nid, []):
                    if o in rem and comp_of[o] != comp_of[nid]:
                        blocked.add(comp_of[o])
            ready = sorted(n for c_i, c in enumerate(comps)
                           if c_i not in blocked for n in c)
        waves.append(ready)
        for nid in ready:
            remaining.pop(nid)
            for o in fwd.get(nid, []):
                if o in remaining:
                    remaining[o] -= 1
    wave_rows = []
    for i, wave in enumerate(waves):
        external = sorted({twin.nodes[o].name
                           for nid in wave for o in fwd.get(nid, [])
                           if o not in selected})
        wave_rows.append({
            "wave": i + 1,
            "objects": [twin.nodes[nid].name for nid in wave],
            "external_consumers_affected": external,
            "cutover_note": "parallel-run this wave; reconcile before "
                            "wave %d" % (i + 2) if i + 1 < len(waves)
            else "final wave — decommission after sign-off"})
    boundary_in = sorted({twin.nodes[e.from_id].name
                          for e in twin.edges.values()
                          if e.kind in _FLOW_KINDS
                          and e.to_id in selected
                          and e.from_id not in selected})
    return {"selection_size": len(selected),
            "technology": technology,
            "waves": wave_rows,
            "inbound_boundary": boundary_in,
            "co_migration_note": "objects sharing a wave have no "
                                 "internal ordering constraint; a "
                                 "cycle collapses into one wave and "
                                 "must move together",
            "estimated_waves": len(waves)}

"""Digital Twin of the data estate — graph model.

Everything MetaBridge understands about a customer's estate becomes ONE
typed property graph:

    nodes  application | database | warehouse | table | pipeline |
           workflow | topic | consumer | producer | streaming_job |
           api | dashboard | domain | data_product | owner | connection
    edges  contains | reads | writes | feeds | depends_on |
           orchestrates | produces | consumes | serves | owns |
           belongs_to | includes

Facts come from parsers, saved connections, prior analysis jobs and the
declarative estate descriptor; nothing is fabricated — heuristic
assignments (name-prefix domains, mart-naming data products) are marked
``inferred: true`` so a customer annotation always outranks them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

NODE_KINDS = ("application", "database", "warehouse", "table",
              "pipeline", "workflow", "topic", "consumer", "producer",
              "streaming_job", "api", "dashboard", "domain",
              "data_product", "owner", "connection")
EDGE_KINDS = ("contains", "reads", "writes", "feeds", "depends_on",
              "orchestrates", "produces", "consumes", "serves", "owns",
              "belongs_to", "includes")

# layers order the visualization left -> right
LAYER_OF = {"connection": 0, "database": 0, "warehouse": 0,
            "producer": 0, "application": 1, "table": 2, "topic": 2,
            "pipeline": 3, "streaming_job": 3, "workflow": 4,
            "consumer": 5, "api": 5, "dashboard": 6,
            "data_product": 6, "domain": 7, "owner": 7}


def node_id(kind: str, name: str) -> str:
    # collapse runs of whitespace to a single space (so "a  b" == "a b")
    # but never fold whitespace into "_", or "order items" and
    # "order_items" would silently become the same node
    return "%s:%s" % (kind, re.sub(r"\s+", " ", str(name).strip()))


# when a name resolves to several nodes of different kinds (a dbt model
# is both a pipeline and its output table), find() picks one
# deterministically: data/business facets before the process that
# produces them, so an estate.yml "reads: [fct_orders]" attaches to the
# table and impact/blast-radius answer for the on-screen node.
_FIND_PRIORITY = ("application", "data_product", "dashboard", "api",
                  "table", "topic", "warehouse", "database",
                  "pipeline", "streaming_job", "workflow", "consumer",
                  "producer", "connection", "domain", "owner")


@dataclass
class TwinNode:
    id: str
    kind: str
    name: str
    technology: str = ""
    domain: str = ""
    owner: str = ""
    inferred: bool = False          # heuristic classification marker
    sources: List[str] = field(default_factory=list)   # where seen
    metadata: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "name": self.name,
             "layer": LAYER_OF.get(self.kind, 3)}
        for k in ("technology", "domain", "owner", "inferred",
                  "sources", "metadata"):
            v = getattr(self, k)
            if v:
                d[k] = v
        return d


@dataclass
class TwinEdge:
    from_id: str
    to_id: str
    kind: str = "depends_on"
    inferred: bool = False
    sources: List[str] = field(default_factory=list)

    def key(self) -> tuple:
        return (self.from_id, self.to_id, self.kind)

    def to_dict(self) -> dict:
        d = {"from": self.from_id, "to": self.to_id, "kind": self.kind}
        if self.inferred:
            d["inferred"] = True
        if self.sources:
            d["sources"] = self.sources
        return d


class DigitalTwin:
    def __init__(self, name: str = "estate") -> None:
        self.name = name
        self.nodes: Dict[str, TwinNode] = {}
        self.edges: Dict[tuple, TwinEdge] = {}
        self.built_from: List[str] = []

    # -- construction --------------------------------------------------------
    def add_node(self, kind: str, name: str, source: str = "",
                 **attrs) -> TwinNode:
        nid = node_id(kind, name)
        node = self.nodes.get(nid)
        new = node is None
        if new:
            # id collapses whitespace; store the same normalized name so
            # find() by a padded name still lands on the node
            node = TwinNode(id=nid, kind=kind,
                            name=re.sub(r"\s+", " ", str(name).strip()))
            self.nodes[nid] = node
        heuristic = bool(attrs.pop("inferred", False))   # this sighting
        # the estate descriptor is the customer's own annotation — it
        # overrides values a parser guessed earlier
        authoritative = bool(attrs.pop("authoritative", False))
        for k, v in attrs.items():
            if v in (None, "", [], {}):
                continue
            if k == "metadata":
                node.metadata.update(v)
                continue
            cur = getattr(node, k, "")
            if cur in ("", None, False):
                setattr(node, k, v)                     # fill a blank
            elif authoritative or (node.inferred and not heuristic):
                setattr(node, k, v)          # higher authority overwrites
        # node-level confidence: it stays "inferred" only while *every*
        # sighting has been heuristic; one explicit sighting confirms it
        # for good (symmetric with how add_edge downgrades an edge)
        if heuristic:
            if new:
                node.inferred = True
        else:
            node.inferred = False
        if source and source not in node.sources:
            node.sources.append(source)
        return node

    def add_edge(self, from_id: str, to_id: str, kind: str,
                 source: str = "", inferred: bool = False) -> None:
        if from_id == to_id:
            return
        if from_id not in self.nodes or to_id not in self.nodes:
            return
        key = (from_id, to_id, kind)
        edge = self.edges.get(key)
        if edge is None:
            edge = TwinEdge(from_id, to_id, kind, inferred=inferred)
            self.edges[key] = edge
        elif not inferred:
            edge.inferred = False
        if source and source not in edge.sources:
            edge.sources.append(source)

    # -- traversal -----------------------------------------------------------
    def out_edges(self, nid: str) -> List[TwinEdge]:
        return [e for e in self.edges.values() if e.from_id == nid]

    def in_edges(self, nid: str) -> List[TwinEdge]:
        return [e for e in self.edges.values() if e.to_id == nid]

    def adjacency(self, reverse: bool = False) -> Dict[str, List[str]]:
        adj: Dict[str, List[str]] = {n: [] for n in self.nodes}
        for e in self.edges.values():
            if reverse:
                adj[e.to_id].append(e.from_id)
            else:
                adj[e.from_id].append(e.to_id)
        return adj

    def find_all(self, name_or_id: str) -> List[TwinNode]:
        """Every node carrying this name (or the single node with this
        id). A name like a dbt model resolves to more than one node."""
        if name_or_id in self.nodes:
            return [self.nodes[name_or_id]]
        want = re.sub(r"\s+", " ", str(name_or_id).strip())
        exact = [n for n in self.nodes.values() if n.name == want]
        if exact:
            return exact
        low = want.lower()
        return [n for n in self.nodes.values() if n.name.lower() == low]

    def find(self, name_or_id: str) -> Optional[TwinNode]:
        """The single best node for a name. When a name maps to several
        nodes (a model is both a pipeline and its table) it resolves
        deterministically by kind priority instead of giving up — a
        None here silently dropped estate.yml facts and 404'd blast
        radius for on-screen nodes."""
        matches = self.find_all(name_or_id)
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        return sorted(matches, key=lambda n: (
            _FIND_PRIORITY.index(n.kind) if n.kind in _FIND_PRIORITY
            else len(_FIND_PRIORITY), n.id))[0]

    # -- serialization ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {"name": self.name,
                "built_from": self.built_from,
                "counts": self.counts(),
                "nodes": [n.to_dict() for n in self.nodes.values()],
                "edges": [e.to_dict() for e in self.edges.values()]}

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for n in self.nodes.values():
            out[n.kind] = out.get(n.kind, 0) + 1
        out["edges"] = len(self.edges)
        return out


def twin_from_dict(doc: dict) -> DigitalTwin:
    twin = DigitalTwin(doc.get("name", "estate"))
    twin.built_from = doc.get("built_from", [])
    for nd in doc.get("nodes", []):
        node = TwinNode(id=nd["id"], kind=nd["kind"], name=nd["name"],
                        technology=nd.get("technology", ""),
                        domain=nd.get("domain", ""),
                        owner=nd.get("owner", ""),
                        inferred=bool(nd.get("inferred")),
                        sources=nd.get("sources", []),
                        metadata=nd.get("metadata", {}))
        twin.nodes[node.id] = node
    for ed in doc.get("edges", []):
        e = TwinEdge(ed["from"], ed["to"], ed.get("kind", "depends_on"),
                     inferred=bool(ed.get("inferred")),
                     sources=ed.get("sources", []))
        twin.edges[e.key()] = e
    return twin

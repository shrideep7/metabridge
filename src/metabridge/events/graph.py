"""Event lineage + streaming execution graph (Command 8, §9)."""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List

from .cer import CER

_SHAPE = {"producer": ("([", "])"), "consumer": ("([", "])"),
          "transform": ("{{", "}}"), "channel": ("[", "]"),
          "cdc": ("[(", ")]"), "iot": (">", "]"),
          "exchange": ("[[", "]]"),
          "external": ("[/", "/]")}


def event_lineage(cer: CER) -> dict:
    edges = cer.flow_edges()
    return {
        "source_platform": cer.source_platform,
        "event_flow": edges,
        "topic_lineage": [e for e in edges
                          if e["from"].startswith("channel:")
                          or e["to"].startswith("channel:")],
        "consumer_lineage": {
            c.name: {"group": c.group, "channels": c.channels,
                     "dead_letter": c.retry.dead_letter}
            for c in cer.consumers},
        "producer_lineage": {
            p.name: p.channels for p in cer.producers},
        "transformation_lineage": {
            t.name: {"inputs": t.inputs, "output": t.output,
                     "engine": t.engine,
                     "windowed": t.window is not None}
            for t in cer.transformations},
        "cdc_lineage": {
            c.name: {"tables": c.tables,
                     "channels": c.output_channels}
            for c in cer.cdc_sources},
        "iot_lineage": {
            i.name: i.topics for i in cer.iot_sources},
    }


def execution_graph(cer: CER) -> dict:
    nodes: List[dict] = []
    seen = set()

    def node(nid: str, label: str, kind: str, **extra) -> None:
        if nid in seen:
            return
        seen.add(nid)
        nodes.append({"id": nid, "label": label, "type": kind, **extra})

    for p in cer.producers:
        node("producer:" + p.name, p.name, "producer",
             acks=p.acks, idempotent=p.idempotent)
    for ch in cer.channels:
        node("channel:" + ch.name, ch.name, "channel",
             kind_detail=ch.kind, partitions=ch.partitions,
             delivery=ch.delivery, ordering=ch.ordering,
             **({"dead_letter": ch.dead_letter} if ch.dead_letter
                else {}))
    for t in cer.transformations:
        node("transform:" + t.name, t.name, "transform",
             engine=t.engine,
             **({"window": t.window.to_dict()} if t.window else {}))
    for c in cer.consumers:
        node("consumer:" + c.name, c.name, "consumer", group=c.group)
    for ex in cer.exchanges():
        node("exchange:" + ex.name, ex.name, "exchange",
             exchange_type=ex.condition.partition(":")[2])
    for c in cer.cdc_sources:
        node("cdc:" + c.name, c.name, "cdc", flavor=c.flavor,
             tables=c.tables, snapshot_mode=c.snapshot_mode)
    for i in cer.independent_iot_sources():
        node("iot:" + i.name, i.name, "iot", protocol=i.protocol)
    edges = cer.flow_edges()
    # An edge may point at something the import never declared as an
    # object — an IoT rule action, a sink outside the estate, a DLQ that
    # was never defined. Dropping those edges made the graph look tidier
    # than the estate actually is: the lineage JSON showed the hop and the
    # diagram silently did not. Declare the endpoint as external instead,
    # so the gap is visible rather than absent.
    ids = {n["id"] for n in nodes}
    # An endpoint can be unresolved simply because it was written with
    # the wrong type prefix — a NiFi processor referenced as channel:X
    # when the import declared it as transform:X. Fabricating an external
    # node for those drew the whole flow through phantoms while the real
    # nodes sat orphaned, so try to match a declared object by name
    # first, and rewrite the edge to point at it.
    by_label: Dict[str, str] = {}
    for n in nodes:
        by_label.setdefault(n["label"], n["id"])
    edges = [dict(e) for e in edges]
    for e in edges:
        for side in ("from", "to"):
            endpoint = e[side]
            if endpoint in ids:
                continue
            kind, _, label = endpoint.partition(":")
            match = by_label.get(label or endpoint)
            if match and match != e["from" if side == "to" else "to"]:
                e[side] = match
                continue
            node(endpoint, label or endpoint, "external",
                 declared_as=kind, resolved=False,
                 note="referenced by the estate but not present in the "
                      "import — verify it exists on the target")
            ids.add(endpoint)
    return {"platform": cer.source_platform, "nodes": nodes,
            "edges": edges}


def _mid(s: str) -> str:
    return re.sub(r"\W+", "_", s)


def _mid_map(ids: List[str]) -> Dict[str, str]:
    """Mermaid node ids that stay distinct.

    _mid() folds '.', '-', '_' and '/' onto the same character, so two
    real objects — 'orders.v1' and 'orders-v1' — sanitize to one id.
    Mermaid then draws a single node and keeps whichever label it read
    last, discarding the other silently: data loss with no error.

    Only ids that actually collide get a hash suffix, so a diagram whose
    names are already unambiguous renders exactly as it did before.
    """
    groups: Dict[str, List[str]] = {}
    seen = set()
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        groups.setdefault(_mid(i), []).append(i)
    out: Dict[str, str] = {}
    for safe, originals in groups.items():
        if len(originals) == 1:
            out[originals[0]] = safe
            continue
        for orig in originals:
            out[orig] = "%s_%s" % (safe, hashlib.sha1(
                orig.encode("utf-8")).hexdigest()[:6])
    return out


def to_mermaid(cer: CER) -> str:
    g = execution_graph(cer)
    ids = [n["id"] for n in g["nodes"]]
    for e in g["edges"]:
        ids += [e["from"], e["to"]]
    mid = _mid_map(ids)
    lines = ["flowchart LR"]
    for n in g["nodes"]:
        o, c = _SHAPE.get(n["type"], ("[", "]"))
        label = n["label"].replace('"', "'")
        lines.append('  %s%s"%s<br/><i>%s</i>"%s'
                     % (mid[n["id"]], o, label, n["type"], c))
    for e in g["edges"]:
        style = "-. DLQ .->" if e["kind"] == "dead_letter" else \
            "-. route .->" if e["kind"] in ("route", "binding",
                                            "iot_rule", "alias") else "-->"
        lines.append("  %s %s %s" % (mid[e["from"]], style,
                                     mid[e["to"]]))
    return "\n".join(lines)

"""Event lineage + streaming execution graph (Command 8, §9)."""
from __future__ import annotations

import re
from typing import Dict, List

from .cer import CER

_SHAPE = {"producer": ("([", "])"), "consumer": ("([", "])"),
          "transform": ("{{", "}}"), "channel": ("[", "]"),
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
    edges = cer.flow_edges()
    # An edge may point at something the import never declared as an
    # object — an IoT rule action, a sink outside the estate, a DLQ that
    # was never defined. Dropping those edges made the graph look tidier
    # than the estate actually is: the lineage JSON showed the hop and the
    # diagram silently did not. Declare the endpoint as external instead,
    # so the gap is visible rather than absent.
    ids = {n["id"] for n in nodes}
    for e in edges:
        for endpoint in (e["from"], e["to"]):
            if endpoint in ids:
                continue
            kind, _, label = endpoint.partition(":")
            node(endpoint, label or endpoint, "external",
                 declared_as=kind, resolved=False,
                 note="referenced by the estate but not present in the "
                      "import — verify it exists on the target")
            ids.add(endpoint)
    return {"platform": cer.source_platform, "nodes": nodes,
            "edges": edges}


def _mid(s: str) -> str:
    return re.sub(r"\W+", "_", s)


def to_mermaid(cer: CER) -> str:
    g = execution_graph(cer)
    lines = ["flowchart LR"]
    for n in g["nodes"]:
        o, c = _SHAPE.get(n["type"], ("[", "]"))
        label = n["label"].replace('"', "'")
        lines.append('  %s%s"%s<br/><i>%s</i>"%s'
                     % (_mid(n["id"]), o, label, n["type"], c))
    for e in g["edges"]:
        style = "-. DLQ .->" if e["kind"] == "dead_letter" else \
            "-. route .->" if e["kind"] in ("route", "binding",
                                            "iot_rule") else "-->"
        lines.append("  %s %s %s" % (_mid(e["from"]), style,
                                     _mid(e["to"])))
    return "\n".join(lines)

"""Canonical Event Representation — CER (Command 8, §4).

Every messaging / streaming / IoT / CDC platform normalizes into CER;
every target generates from CER. No pairwise broker converters.

    Event Platform -> Metadata Parser -> CER -> Semantic Analysis ->
    Target Generator -> Validation + AI Review + Governance

Semantics that must NEVER be lost ride as first-class fields: delivery
guarantees, ordering, retry behaviour, dead-letter routing, retention,
compression, replication, schema evolution mode, event-time/watermark
semantics, and security policies. What a target cannot express is
declared in the generated artifact — never dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

DELIVERY = ("at_most_once", "at_least_once", "exactly_once")
ORDERING = ("none", "per_key", "per_partition", "global", "fifo")
WINDOW_KINDS = ("tumbling", "hopping", "session", "sliding", "global")


@dataclass
class RetryPolicy:
    max_attempts: int = 0
    backoff_ms: int = 0
    backoff_multiplier: float = 1.0
    dead_letter: str = ""            # DLQ/DLX topic or queue name

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class RetentionPolicy:
    time_ms: int = 0                 # 0 = broker default / unlimited
    size_bytes: int = 0
    policy: str = "delete"           # delete | compact | compact,delete

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class EventSchema:
    name: str
    format: str = "json"             # avro | json | protobuf | xml | bytes
    definition: str = ""             # raw schema text (always preserved)
    fields: List[dict] = field(default_factory=list)
    compatibility: str = ""          # BACKWARD/FORWARD/FULL/NONE
    version: int = 1

    def to_dict(self) -> dict:
        return {"name": self.name, "format": self.format,
                "fields": self.fields,
                "compatibility": self.compatibility,
                "version": self.version,
                "definition": self.definition[:4000]}


@dataclass
class Channel:
    """Topic or queue — the unit events flow through."""
    name: str
    kind: str = "topic"              # topic | queue | stream | mqtt_topic
    partitions: int = 1
    replication: int = 1
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    compression: str = ""            # none|gzip|snappy|lz4|zstd
    ordering: str = "per_partition"  # ORDERING
    delivery: str = "at_least_once"  # DELIVERY
    schema: str = ""                 # EventSchema name
    dead_letter: str = ""
    key_fields: List[str] = field(default_factory=list)
    is_cdc: bool = False
    is_iot: bool = False
    properties: Dict[str, str] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind,
             "partitions": self.partitions,
             "replication": self.replication,
             "retention": self.retention.to_dict(),
             "ordering": self.ordering, "delivery": self.delivery}
        for k in ("compression", "schema", "dead_letter", "key_fields",
                  "is_cdc", "is_iot", "properties", "description"):
            v = getattr(self, k)
            if v:
                d[k] = v
        return d


@dataclass
class Producer:
    name: str
    channels: List[str] = field(default_factory=list)
    acks: str = ""                   # 0|1|all
    idempotent: bool = False
    transactional: bool = False
    properties: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class Consumer:
    name: str
    group: str = ""
    channels: List[str] = field(default_factory=list)
    offset_reset: str = ""           # earliest|latest
    max_in_flight: int = 0
    manual_commit: bool = False
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    properties: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()
             if v and k != "retry"}
        r = self.retry.to_dict()
        if r:
            d["retry"] = r
        return d


@dataclass
class Window:
    kind: str = "tumbling"           # WINDOW_KINDS
    size_ms: int = 0
    slide_ms: int = 0                # hopping advance
    gap_ms: int = 0                  # session inactivity gap
    watermark_delay_ms: int = 0
    time_semantics: str = "event_time"   # event_time | processing_time

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class StreamTransformation:
    """One streaming job / stream / SMT chain."""
    name: str
    kind: str = "stream"     # stream | table | smt | processor | rule
    inputs: List[str] = field(default_factory=list)
    output: str = ""
    sql: str = ""            # canonical SQL where extractable
    raw: str = ""            # original definition, always preserved
    window: Optional[Window] = None
    group_by: List[str] = field(default_factory=list)
    joins: List[dict] = field(default_factory=list)
    state_stores: List[str] = field(default_factory=list)
    engine: str = ""         # ksqldb | kafka_streams | flink | spark | nifi

    def to_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "inputs": self.inputs,
             "output": self.output, "engine": self.engine}
        for k in ("sql", "group_by", "joins", "state_stores"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.window is not None:
            d["window"] = self.window.to_dict()
        if self.raw:
            d["raw"] = self.raw[:2000]
        return d


@dataclass
class RoutingRule:
    name: str
    source: str = ""
    target: str = ""
    condition: str = ""      # routing predicate / binding key / IoT SQL
    kind: str = "route"      # route | binding | iot_rule | dlx

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class SecurityPolicy:
    principal: str
    resource: str = ""
    operations: List[str] = field(default_factory=list)
    kind: str = "acl"        # acl | permission | sas | tls | iam

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class CDCSource:
    name: str
    flavor: str = ""         # debezium|goldengate|slt|ibm_cdc|sqlserver...
    database: str = ""
    tables: List[str] = field(default_factory=list)
    snapshot_mode: str = ""
    output_channels: List[str] = field(default_factory=list)
    properties: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class IoTSource:
    name: str
    protocol: str = "mqtt"
    topics: List[str] = field(default_factory=list)
    telemetry_fields: List[dict] = field(default_factory=list)
    device_twin: bool = False
    qos: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v or k == "qos"}


@dataclass
class CER:
    """One imported event estate."""
    name: str
    source_platform: str = ""
    channels: List[Channel] = field(default_factory=list)
    producers: List[Producer] = field(default_factory=list)
    consumers: List[Consumer] = field(default_factory=list)
    schemas: List[EventSchema] = field(default_factory=list)
    transformations: List[StreamTransformation] = field(
        default_factory=list)
    routing: List[RoutingRule] = field(default_factory=list)
    security: List[SecurityPolicy] = field(default_factory=list)
    cdc_sources: List[CDCSource] = field(default_factory=list)
    iot_sources: List[IoTSource] = field(default_factory=list)
    connections: List[dict] = field(default_factory=list)
    issues: List[dict] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)

    # -- helpers -------------------------------------------------------------
    def channel(self, name: str) -> Optional[Channel]:
        for c in self.channels:
            if c.name == name:
                return c
        return None

    def add_issue(self, severity: str, code: str, message: str,
                  obj: str = "", detail: str = "",
                  suggestion: str = "") -> None:
        self.issues.append({"severity": severity, "code": code,
                            "message": message, "obj": obj,
                            "detail": detail, "suggestion": suggestion})

    def flow_edges(self) -> List[dict]:
        """End-to-end event flow: producer -> channel -> transformation
        -> channel -> consumer (+ routing + DLQ edges)."""
        edges: List[dict] = []
        for p in self.producers:
            for ch in p.channels:
                edges.append({"from": "producer:" + p.name,
                              "to": "channel:" + ch, "kind": "produce"})
        for t in self.transformations:
            for i in t.inputs:
                edges.append({"from": "channel:" + i,
                              "to": "transform:" + t.name,
                              "kind": "consume"})
            if t.output:
                edges.append({"from": "transform:" + t.name,
                              "to": "channel:" + t.output,
                              "kind": "produce"})
        for r in self.routing:
            if r.source and r.target:
                edges.append({"from": "channel:" + r.source,
                              "to": "channel:" + r.target,
                              "kind": r.kind,
                              "condition": r.condition})
        for c in self.consumers:
            for ch in c.channels:
                edges.append({"from": "channel:" + ch,
                              "to": "consumer:" + c.name,
                              "kind": "consume"})
            if c.retry.dead_letter:
                edges.append({"from": "consumer:" + c.name,
                              "to": "channel:" + c.retry.dead_letter,
                              "kind": "dead_letter"})
        for ch in self.channels:
            if ch.dead_letter:
                edges.append({"from": "channel:" + ch.name,
                              "to": "channel:" + ch.dead_letter,
                              "kind": "dead_letter"})
        return edges

    def inventory(self) -> dict:
        return {
            "channels": len(self.channels),
            "topics": sum(1 for c in self.channels if c.kind != "queue"),
            "queues": sum(1 for c in self.channels if c.kind == "queue"),
            "producers": len(self.producers),
            "consumers": len(self.consumers),
            "consumer_groups": len({c.group for c in self.consumers
                                    if c.group}),
            "schemas": len(self.schemas),
            "streaming_jobs": len(self.transformations),
            "routing_rules": len(self.routing),
            "security_policies": len(self.security),
            "cdc_sources": len(self.cdc_sources),
            "iot_sources": len(self.iot_sources),
            "windows": sum(1 for t in self.transformations
                           if t.window is not None),
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name, "source_platform": self.source_platform,
            "channels": [c.to_dict() for c in self.channels],
            "producers": [p.to_dict() for p in self.producers],
            "consumers": [c.to_dict() for c in self.consumers],
            "schemas": [s.to_dict() for s in self.schemas],
            "transformations": [t.to_dict()
                                for t in self.transformations],
            "routing": [r.to_dict() for r in self.routing],
            "security": [s.to_dict() for s in self.security],
            "cdc_sources": [c.to_dict() for c in self.cdc_sources],
            "iot_sources": [i.to_dict() for i in self.iot_sources],
            "connections": self.connections,
            "issues": self.issues, "metadata": self.metadata,
            "flow": self.flow_edges(), "inventory": self.inventory(),
        }


def cer_from_dict(doc: dict) -> CER:
    cer = CER(name=doc.get("name", ""),
              source_platform=doc.get("source_platform", ""),
              connections=doc.get("connections", []),
              issues=doc.get("issues", []),
              metadata=doc.get("metadata", {}))
    for cd in doc.get("channels", []):
        r = cd.get("retention", {})
        cer.channels.append(Channel(
            name=cd["name"], kind=cd.get("kind", "topic"),
            partitions=cd.get("partitions", 1),
            replication=cd.get("replication", 1),
            retention=RetentionPolicy(r.get("time_ms", 0),
                                      r.get("size_bytes", 0),
                                      r.get("policy", "delete")),
            compression=cd.get("compression", ""),
            ordering=cd.get("ordering", "per_partition"),
            delivery=cd.get("delivery", "at_least_once"),
            schema=cd.get("schema", ""),
            dead_letter=cd.get("dead_letter", ""),
            key_fields=cd.get("key_fields", []),
            is_cdc=bool(cd.get("is_cdc")), is_iot=bool(cd.get("is_iot")),
            properties=cd.get("properties", {}),
            description=cd.get("description", "")))
    for pd in doc.get("producers", []):
        cer.producers.append(Producer(
            name=pd["name"], channels=pd.get("channels", []),
            acks=pd.get("acks", ""),
            idempotent=bool(pd.get("idempotent")),
            transactional=bool(pd.get("transactional")),
            properties=pd.get("properties", {})))
    for cd in doc.get("consumers", []):
        r = cd.get("retry", {})
        cer.consumers.append(Consumer(
            name=cd["name"], group=cd.get("group", ""),
            channels=cd.get("channels", []),
            offset_reset=cd.get("offset_reset", ""),
            max_in_flight=cd.get("max_in_flight", 0),
            manual_commit=bool(cd.get("manual_commit")),
            retry=RetryPolicy(r.get("max_attempts", 0),
                              r.get("backoff_ms", 0),
                              r.get("backoff_multiplier", 1.0),
                              r.get("dead_letter", "")),
            properties=cd.get("properties", {})))
    for sd in doc.get("schemas", []):
        cer.schemas.append(EventSchema(
            name=sd["name"], format=sd.get("format", "json"),
            definition=sd.get("definition", ""),
            fields=sd.get("fields", []),
            compatibility=sd.get("compatibility", ""),
            version=sd.get("version", 1)))
    for td in doc.get("transformations", []):
        w = td.get("window")
        cer.transformations.append(StreamTransformation(
            name=td["name"], kind=td.get("kind", "stream"),
            inputs=td.get("inputs", []), output=td.get("output", ""),
            sql=td.get("sql", ""), raw=td.get("raw", ""),
            window=Window(**{k: v for k, v in w.items()
                             if k in Window().__dict__}) if w else None,
            group_by=td.get("group_by", []), joins=td.get("joins", []),
            state_stores=td.get("state_stores", []),
            engine=td.get("engine", "")))
    for rd in doc.get("routing", []):
        cer.routing.append(RoutingRule(
            name=rd.get("name", ""), source=rd.get("source", ""),
            target=rd.get("target", ""),
            condition=rd.get("condition", ""),
            kind=rd.get("kind", "route")))
    for sd in doc.get("security", []):
        cer.security.append(SecurityPolicy(
            principal=sd.get("principal", ""),
            resource=sd.get("resource", ""),
            operations=sd.get("operations", []),
            kind=sd.get("kind", "acl")))
    for cd in doc.get("cdc_sources", []):
        cer.cdc_sources.append(CDCSource(
            name=cd["name"], flavor=cd.get("flavor", ""),
            database=cd.get("database", ""),
            tables=cd.get("tables", []),
            snapshot_mode=cd.get("snapshot_mode", ""),
            output_channels=cd.get("output_channels", []),
            properties=cd.get("properties", {})))
    for idd in doc.get("iot_sources", []):
        cer.iot_sources.append(IoTSource(
            name=idd["name"], protocol=idd.get("protocol", "mqtt"),
            topics=idd.get("topics", []),
            telemetry_fields=idd.get("telemetry_fields", []),
            device_twin=bool(idd.get("device_twin")),
            qos=idd.get("qos", 0)))
    return cer

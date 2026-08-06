"""Event platform parsers (Command 8, §1–§3) — imports -> CER.

Platform families and the artifacts each adapter reads:

    kafka / confluent / msk   topics JSON/YAML exports, .properties
                              producer/consumer configs, consumer-group
                              JSON, Kafka Connect connector JSON (SMT
                              chains preserved), Schema Registry subject
                              exports + .avsc/.proto/JSON-schema files,
                              ksqlDB .sql (streams/tables, TUMBLING/
                              HOPPING/SESSION windows)
    pulsar                    pulsar-admin JSON (tenants/namespaces/
                              topics, retention, dedup)
    rabbitmq                  definitions.json (queues, exchanges,
                              bindings, DLX arguments, quorum queues,
                              users/permissions)
    ibmmq / activemq          MQSC scripts (DEFINE QLOCAL/QALIAS ...,
                              BOQNAME/BOTHRESH backout = retry+DLQ);
                              activemq.xml destinations + DLQ strategy
    kinesis                   describe-stream JSON
    eventhubs / servicebus    ARM template resources (partitions,
                              retention, sessions=FIFO, dead-lettering,
                              maxDeliveryCount)
    pubsub                    topics+subscriptions JSON (ordering,
                              deadLetterPolicy, retryPolicy)
    mqtt (mosquitto/hivemq/emqx)
                              broker conf + topic lists (QoS -> delivery)
    awsiot / iothub           AWS IoT topic-rule JSON (rule SQL ->
                              routing + transformation), IoT Hub ARM
                              routes, device-twin JSON
    nifi                      template XML (processors -> transformations,
                              connections -> flow, back-pressure)
    streamsets                pipeline JSON (stages + lanes)
    debezium                  connector JSON (flavor, tables, snapshot
                              mode, SMTs)
    goldengate                extract/replicat .prm (TABLE/MAP)
    slt / ibm_cdc             tolerant JSON metadata ({"slt": ...} /
                              {"ibm_cdc": ...})
    flink / spark SQL         .sql files (windowing TVFs / GROUP-BY
                              windows, WATERMARK declarations)

XML via ElementTree, JSON via json, SQL via sqlglot + structural window
extraction, .properties/MQSC/.prm via line/statement tokenizers. Binary
uploads and unknown shapes are declared, never guessed.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from .cer import (
    CDCSource, CER, Channel, Consumer, EventSchema, IoTSource, Producer,
    RetentionPolicy, RetryPolicy, RoutingRule, SecurityPolicy,
    StreamDeclaration, StreamTransformation, Window,
)

EVENT_PLATFORMS = ("kafka", "confluent", "pulsar", "rabbitmq", "ibmmq",
                   "activemq", "kinesis", "eventhubs", "servicebus",
                   "pubsub", "mqtt", "awsiot", "iothub", "nifi",
                   "streamsets", "debezium", "goldengate", "slt",
                   "ibm_cdc")


def _clean(name: str) -> str:
    return re.sub(r"[^\w./+-]+", "_", str(name)).strip("_")


_MS = {"ms": 1, "s": 1000, "second": 1000, "seconds": 1000,
       "minute": 60000, "minutes": 60000, "min": 60000,
       "hour": 3600000, "hours": 3600000, "day": 86400000,
       "days": 86400000}


def _dur_ms(qty: str, unit: str) -> int:
    return int(float(qty) * _MS.get(unit.lower().rstrip("s") + "s",
                                    _MS.get(unit.lower(), 1)))


# ===========================================================================
# Kafka family
# ===========================================================================

def _kafka_topic(doc: dict, cer: CER) -> None:
    cfg = doc.get("config", doc.get("configs", {})) or {}
    name = _clean(doc.get("name", ""))
    existing = cer.channel(name)
    if existing is not None:
        # a topic export is authoritative over a bare reference created
        # by streaming SQL — replace the placeholder
        cer.channels.remove(existing)
    cer.channels.append(Channel(
        name=_clean(doc.get("name", "")), kind="topic",
        partitions=int(doc.get("partitions",
                               doc.get("numPartitions", 1)) or 1),
        replication=int(doc.get("replication_factor",
                                doc.get("replicationFactor", 1)) or 1),
        retention=RetentionPolicy(
            time_ms=int(cfg.get("retention.ms", 0) or 0),
            size_bytes=int(cfg.get("retention.bytes", 0) or 0),
            policy=str(cfg.get("cleanup.policy", "delete"))),
        compression=str(cfg.get("compression.type", "")),
        ordering="per_partition",
        delivery="at_least_once",
        properties={k: str(v) for k, v in cfg.items()}))


def _parse_kafka_properties(text: str, name: str, cer: CER) -> bool:
    props: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            props[k.strip()] = v.strip()
    if not props:
        return False
    if "group.id" in props:
        cer.consumers.append(Consumer(
            name=_clean(name), group=props["group.id"],
            channels=[t.strip() for t in
                      props.get("topics", "").split(",") if t.strip()],
            offset_reset=props.get("auto.offset.reset", ""),
            max_in_flight=int(props.get(
                "max.poll.records", 0) or 0),
            manual_commit=props.get("enable.auto.commit",
                                    "true").lower() == "false",
            properties=props))
        return True
    if "acks" in props or "transactional.id" in props or \
            "enable.idempotence" in props:
        chans = [t.strip() for t in props.get("topics", "").split(",")
                 if t.strip()]
        cer.producers.append(Producer(
            name=_clean(name), channels=chans,
            acks=props.get("acks", ""),
            idempotent=props.get("enable.idempotence",
                                 "").lower() == "true",
            transactional="transactional.id" in props,
            properties=props))
        return True
    if "bootstrap.servers" in props:
        cer.connections.append({"name": name, "kind": "kafka",
                                "bootstrap": "configured"})
        return True
    return False


def _parse_connect(doc: dict, cer: CER, fname: str) -> bool:
    cfg = doc.get("config", doc if "connector.class" in doc else None)
    if not isinstance(cfg, dict) or "connector.class" not in cfg:
        return False
    name = _clean(doc.get("name", fname))
    cls = str(cfg["connector.class"])
    # SMT chains preserved as transformations
    smts = [s.strip() for s in str(cfg.get("transforms", "")).split(",")
            if s.strip()]
    if "debezium" in cls.lower():
        tables = [t.strip() for t in str(
            cfg.get("table.include.list",
                    cfg.get("table.whitelist", ""))).split(",")
            if t.strip()]
        flavor = cls.rsplit(".", 2)[-2].lower() if "." in cls else \
            "debezium"
        prefix = str(cfg.get("topic.prefix",
                             cfg.get("database.server.name", name)))
        outs = ["%s.%s" % (prefix, t) for t in tables] or [prefix]
        cer.cdc_sources.append(CDCSource(
            name=name, flavor="debezium:" + flavor,
            database=str(cfg.get("database.dbname",
                                 cfg.get("database.names", ""))),
            tables=tables,
            snapshot_mode=str(cfg.get("snapshot.mode", "")),
            output_channels=outs,
            topic_prefix=prefix,
            properties={k: str(v) for k, v in cfg.items()
                        if "password" not in k.lower()}))
        for out in outs:
            if cer.channel(out) is None:
                cer.channels.append(Channel(
                    name=out, kind="topic", is_cdc=True,
                    ordering="per_key",
                    key_fields=["primary_key"],
                    description="Debezium change events for %s" % out))
    else:
        cer.producers.append(Producer(
            name=name,
            channels=[t.strip() for t in str(
                cfg.get("topics", cfg.get("kafka.topic", ""))).split(",")
                if t.strip()],
            properties={"connector.class": cls}))
    for smt in smts:
        cer.transformations.append(StreamTransformation(
            name="%s_smt_%s" % (name, smt), kind="smt",
            inputs=[], engine="kafka_connect",
            raw=json.dumps({k: str(v) for k, v in cfg.items()
                            if k.startswith("transforms." + smt)})))
    return True


def _parse_schema(doc, cer: CER, fname: str) -> bool:
    if isinstance(doc, dict) and doc.get("type") == "record" and \
            "fields" in doc:                       # bare .avsc
        cer.schemas.append(EventSchema(
            name=_clean(doc.get("name", fname)), format="avro",
            definition=json.dumps(doc),
            fields=[{"name": f.get("name", ""),
                     "type": str(f.get("type", ""))}
                    for f in doc.get("fields", [])]))
        return True
    if isinstance(doc, dict) and "subject" in doc and "schema" in doc:
        fmt = str(doc.get("schemaType", "AVRO")).lower()
        fields = []
        try:
            inner = json.loads(doc["schema"])
            fields = [{"name": f.get("name", ""),
                       "type": str(f.get("type", ""))}
                      for f in inner.get("fields", [])]
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        cer.schemas.append(EventSchema(
            name=_clean(str(doc["subject"]).replace("-value", "")
                        .replace("-key", "")),
            format="protobuf" if fmt == "protobuf" else
            "json" if fmt == "json" else "avro",
            definition=str(doc["schema"]),
            fields=fields,
            compatibility=str(doc.get("compatibility", "")),
            version=int(doc.get("version", 1) or 1)))
        return True
    if isinstance(doc, dict) and doc.get("$schema") and \
            "properties" in doc:                   # JSON schema
        cer.schemas.append(EventSchema(
            name=_clean(doc.get("title", fname)), format="json",
            definition=json.dumps(doc),
            fields=[{"name": k, "type": str((v or {}).get("type", ""))}
                    for k, v in doc.get("properties", {}).items()]))
        return True
    return False


# ---- streaming SQL (ksqlDB / Flink / Spark) --------------------------------

_KSQL_WINDOW_RE = re.compile(
    r"WINDOW\s+(TUMBLING|HOPPING|SESSION)\s*\(([^)]*)\)", re.I)
_FLINK_WINDOW_RE = re.compile(
    r"\b(TUMBLE|HOP|SESSION)\s*\(", re.I)
_INTERVAL_RE = re.compile(r"(?:SIZE|ADVANCE\s+BY|INTERVAL)\s*'?(\d+)'?"
                          r"\s*(\w+)", re.I)
_WATERMARK_RE = re.compile(
    r"WATERMARK\s+FOR\s+\w+\s+AS\s+\w+\s*-\s*INTERVAL\s*'(\d+)'\s*(\w+)",
    re.I)


def parse_streaming_sql(text: str, fname: str, cer: CER) -> bool:
    import sqlglot
    stmts = []
    for chunk in re.split(r";\s*(?:\n|$)", text):
        # a leading comment must not hide the statement below it
        chunk = re.sub(r"^\s*(?:--[^\n]*\n)+", "", chunk).strip()
        if chunk and not chunk.startswith("--"):
            stmts.append(chunk)
    found = False
    default_watermark = 0
    for m in _WATERMARK_RE.finditer(text):
        default_watermark = _dur_ms(m.group(1), m.group(2))
    for stmt in stmts:
        m = re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?(STREAM|TABLE)\s+"
                     r"(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", stmt, re.I)
        if not m:
            continue
        found = True
        kind, name = m.group(1).lower(), _clean(m.group(2))
        engine = "flink" if _FLINK_WINDOW_RE.search(stmt) or \
            "WATERMARK" in stmt.upper() else "ksqldb"
        with_m = re.search(r"WITH\s*\(([^)]*)\)", stmt, re.I | re.S)
        topic = ""
        if with_m:
            t_m = re.search(r"(?:KAFKA_TOPIC|topic|connector\.topic)\s*"
                            r"=\s*'([^']+)'", with_m.group(1), re.I)
            if t_m:
                topic = t_m.group(1)
        # Window time semantics, claimed only where the source states it.
        # ksqlDB's ROWTIME defaults to the Kafka record timestamp, which
        # is producer- or broker-assigned depending on the topic's
        # message.timestamp.type — not knowable from a static import. The
        # old unconditional "event_time" default made every windowed job
        # look like it needed a watermark whether or not that was true.
        ts_col = ""
        if with_m:
            ts_m = re.search(r"\bTIMESTAMP\s*=\s*'([^']+)'",
                             with_m.group(1), re.I)
            if ts_m:
                ts_col = ts_m.group(1)
        semantics = ("event_time"
                     if ts_col or "WATERMARK" in stmt.upper()
                     else "unknown")
        window: Optional[Window] = None
        wm = _KSQL_WINDOW_RE.search(stmt)
        if wm:
            spec = wm.group(2)
            sizes = _INTERVAL_RE.findall(spec)
            kind_w = {"TUMBLING": "tumbling", "HOPPING": "hopping",
                      "SESSION": "session"}[wm.group(1).upper()]
            window = Window(kind=kind_w,
                            size_ms=_dur_ms(*sizes[0]) if sizes else 0,
                            slide_ms=_dur_ms(*sizes[1])
                            if kind_w == "hopping" and len(sizes) > 1
                            else 0,
                            gap_ms=_dur_ms(*sizes[0])
                            if kind_w == "session" and sizes else 0,
                            watermark_delay_ms=default_watermark,
                            time_semantics=semantics)
        else:
            fm = _FLINK_WINDOW_RE.search(stmt)
            if fm:
                sizes = _INTERVAL_RE.findall(stmt[fm.start():])
                kind_w = {"TUMBLE": "tumbling", "HOP": "hopping",
                          "SESSION": "session"}[fm.group(1).upper()]
                window = Window(kind=kind_w,
                                size_ms=_dur_ms(*sizes[-1])
                                if sizes else 0,
                                slide_ms=_dur_ms(*sizes[0])
                                if kind_w == "hopping" and len(sizes) > 1
                                else 0,
                                watermark_delay_ms=default_watermark,
                                time_semantics=semantics)
        sel = re.search(r"\bAS\s+(SELECT.+)$", stmt, re.I | re.S)
        sql, inputs, group_by, joins = "", [], [], []
        if sel:
            body = sel.group(1)
            cleaned = _KSQL_WINDOW_RE.sub("", body)
            cleaned = re.sub(r"EMIT\s+CHANGES", "", cleaned, flags=re.I)
            try:
                tree = sqlglot.parse_one(cleaned)
                sql = tree.sql()
                from sqlglot import exp
                inputs = [t.name for t in tree.find_all(exp.Table)]
                g = tree.find(exp.Group)
                if g:
                    group_by = [e.sql() for e in g.expressions
                                if "TUMBLE" not in e.sql().upper()
                                and "HOP" not in e.sql().upper()]
                joins = [{"table": j.this.sql(),
                          "kind": (j.side or "INNER").upper()}
                         for j in tree.find_all(exp.Join)]
            except Exception:  # noqa: BLE001 — raw preserved below
                cer.add_issue("MANUAL", "STREAMSQL_UNPARSEABLE",
                              "Statement defining %s could not be "
                              "parsed — raw SQL preserved" % name,
                              obj=name, detail=stmt[:300])
        # A job that declares no timestamp of its own still runs on event
        # time if the stream it READS declared one — that is the usual
        # ksqlDB shape, so judging each statement in isolation reported
        # "unknown" for jobs whose semantics the import does state.
        if window is not None and window.time_semantics == "unknown":
            if any(d.timestamp_column for i in inputs
                   for d in cer.declarations if d.name == i):
                window.time_semantics = "event_time"
        if sel is None:
            # No AS SELECT: this DDL declares a stream/table OVER a
            # topic, it does not process anything. Recording it as a job
            # made `output` the topic it reads, which reversed the
            # lineage arrow and counted a declaration as a streaming job.
            cer.declarations.append(StreamDeclaration(
                name=name, topic=topic, kind=kind, engine=engine,
                timestamp_column=ts_col, raw=stmt))
        else:
            tr = StreamTransformation(
                name=name, kind=kind, inputs=inputs,
                output=topic or name, sql=sql, raw=stmt, window=window,
                group_by=group_by, joins=joins, engine=engine)
            if window is not None:
                tr.state_stores.append(name + "_window_state")
            cer.transformations.append(tr)
        if topic and cer.channel(topic) is None:
            cer.channels.append(Channel(name=topic, kind="topic"))
    return found


# ===========================================================================
# RabbitMQ definitions.json
# ===========================================================================

def parse_rabbitmq(doc: dict, cer: CER) -> bool:
    if "queues" not in doc and "exchanges" not in doc:
        return False
    for q in doc.get("queues", []):
        args = q.get("arguments", {}) or {}
        cer.channels.append(Channel(
            name=_clean(q.get("name", "")), kind="queue",
            partitions=1,
            replication=3 if args.get("x-queue-type") == "quorum" else 1,
            retention=RetentionPolicy(
                time_ms=int(args.get("x-message-ttl", 0) or 0)),
            ordering="fifo",
            delivery="at_least_once",
            dead_letter=_clean(args.get("x-dead-letter-exchange", "")),
            properties={"durable": str(q.get("durable", False)),
                        "queue_type": str(args.get("x-queue-type",
                                                   "classic"))}))
    for ex in doc.get("exchanges", []):
        cer.routing.append(RoutingRule(
            name=_clean(ex.get("name", "")), kind="route",
            condition="exchange:%s" % ex.get("type", "direct")))
    for b in doc.get("bindings", []):
        cer.routing.append(RoutingRule(
            name="bind_%s_%s" % (_clean(b.get("source", "")),
                                 _clean(b.get("destination", ""))),
            source=_clean(b.get("source", "")),
            target=_clean(b.get("destination", "")),
            condition=b.get("routing_key", ""), kind="binding",
            # a binding always originates at an exchange; the
            # destination may be a queue or another exchange
            source_kind="exchange",
            target_kind=str(b.get("destination_type", "queue"))))
    for perm in doc.get("permissions", []):
        cer.security.append(SecurityPolicy(
            principal=perm.get("user", ""),
            resource=perm.get("vhost", "/"),
            operations=[k for k in ("configure", "write", "read")
                        if perm.get(k)], kind="permission"))
    return True


# ===========================================================================
# IBM MQ MQSC / ActiveMQ
# ===========================================================================

def parse_mqsc(text: str, cer: CER) -> bool:
    stmts = re.split(r"\n(?=DEFINE|ALTER)", text, flags=re.I)
    found = False
    for stmt in stmts:
        m = re.match(r"\s*DEFINE\s+(QLOCAL|QALIAS|QREMOTE|TOPIC)\s*"
                     r"\(\s*'?([\w.]+)'?\s*\)", stmt, re.I)
        if not m:
            continue
        found = True
        kind = "queue" if m.group(1).upper().startswith("Q") else "topic"
        props = dict(re.findall(r"(\w+)\s*\(\s*'?([^)']*)'?\s*\)", stmt))
        props.pop(m.group(1).upper(), None)
        retry = RetryPolicy()
        if props.get("BOTHRESH"):
            retry = RetryPolicy(max_attempts=int(props["BOTHRESH"]),
                                dead_letter=_clean(props.get("BOQNAME",
                                                             "")))
        ch = Channel(name=_clean(m.group(2)), kind=kind,
                     ordering="fifo", delivery="at_least_once",
                     dead_letter=_clean(props.get("BOQNAME", "")),
                     properties={k: v for k, v in props.items()
                                 if k in ("MAXDEPTH", "DEFPSIST",
                                          "CLUSTER", "TARGET")})
        cer.channels.append(ch)
        target = _clean(props.get("TARGET", ""))
        if target and target != ch.name:
            # A QALIAS resolves to a base queue. TARGET was kept as a
            # property but never became a link, so the alias appeared as
            # an unconnected queue and nothing recorded that applications
            # writing to it actually land on the base queue.
            cer.routing.append(RoutingRule(
                name="alias_%s_%s" % (ch.name, target),
                source=ch.name, target=target, kind="alias",
                condition="QALIAS resolves to %s" % target))
        if retry.max_attempts:
            cer.consumers.append(Consumer(
                name="backout_%s" % ch.name, channels=[ch.name],
                retry=retry))
    return found


def parse_activemq_xml(root: ET.Element, cer: CER) -> bool:
    found = False
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("queue", "topic") and el.get("physicalName"):
            cer.channels.append(Channel(
                name=_clean(el.get("physicalName")),
                kind="queue" if tag == "queue" else "topic",
                ordering="fifo" if tag == "queue" else "none",
                delivery="at_least_once"))
            found = True
        if tag == "individualDeadLetterStrategy":
            prefix = el.get("queuePrefix", "DLQ.")
            for ch in cer.channels:
                if not ch.dead_letter:
                    ch.dead_letter = _clean(prefix + ch.name)
    return found


# ===========================================================================
# Pulsar / Kinesis / Event Hubs / Service Bus / Pub/Sub
# ===========================================================================

def parse_pulsar(doc: dict, cer: CER) -> bool:
    if "namespaces" not in doc and "tenant" not in doc:
        return False
    for ns in doc.get("namespaces", []):
        pol = ns.get("policies", {}) or {}
        ret = pol.get("retention", {}) or {}
        for t in ns.get("topics", []):
            cer.channels.append(Channel(
                name=_clean(t.get("name", "")), kind="topic",
                partitions=int(t.get("partitions", 1) or 1),
                retention=RetentionPolicy(
                    time_ms=int(ret.get("retentionTimeInMinutes", 0)
                                or 0) * 60000),
                delivery="exactly_once"
                if pol.get("deduplicationEnabled") else "at_least_once",
                ordering="per_key",
                properties={"tenant": str(doc.get("tenant", ""))}))
        for sub in ns.get("subscriptions", []):
            cer.consumers.append(Consumer(
                name=_clean(sub.get("name", "")),
                group=sub.get("type", "Shared"),
                channels=[_clean(sub.get("topic", ""))]))
    return True


def parse_kinesis(doc: dict, cer: CER) -> bool:
    sd = doc.get("StreamDescription") or (
        doc if "StreamName" in doc else None)
    if not isinstance(sd, dict):
        return False
    cer.channels.append(Channel(
        name=_clean(sd.get("StreamName", "")), kind="stream",
        partitions=len(sd.get("Shards", [])) or int(
            sd.get("OpenShardCount", 1) or 1),
        retention=RetentionPolicy(time_ms=int(
            sd.get("RetentionPeriodHours", 24) or 24) * 3600000),
        ordering="per_partition", delivery="at_least_once",
        properties={"mode": str((sd.get("StreamModeDetails") or {})
                                .get("StreamMode", ""))}))
    return True


def parse_azure_arm(doc: dict, cer: CER) -> bool:
    res = doc.get("resources")
    if not isinstance(res, list):
        return False
    found = False
    for r in res:
        rtype = str(r.get("type", "")).lower()
        props = r.get("properties", {}) or {}
        name = _clean(str(r.get("name", "")).split("/")[-1])
        if "eventhub" in rtype and "authorizationrule" not in rtype and \
                "consumergroup" not in rtype:
            cer.channels.append(Channel(
                name=name, kind="topic",
                partitions=int(props.get("partitionCount", 1) or 1),
                retention=RetentionPolicy(time_ms=int(
                    props.get("messageRetentionInDays", 1) or 1)
                    * 86400000),
                ordering="per_partition", delivery="at_least_once"))
            found = True
        elif "consumergroup" in rtype:
            cer.consumers.append(Consumer(name=name, group=name,
                                          channels=[_clean(str(
                                              r.get("name", "")).split(
                                                  "/")[-2])]))
            found = True
        elif "servicebus" in rtype and ("/queues" in rtype or
                                        "/topics" in rtype):
            fifo = bool(props.get("requiresSession"))
            dl = bool(props.get("deadLetteringOnMessageExpiration"))
            cer.channels.append(Channel(
                name=name,
                kind="queue" if "/queues" in rtype else "topic",
                ordering="fifo" if fifo else "none",
                delivery="at_least_once",
                dead_letter=(name + "/$DeadLetterQueue") if dl else "",
                properties={"maxDeliveryCount":
                            str(props.get("maxDeliveryCount", ""))}))
            if dl:
                # Service Bus dead-letter sub-queues exist implicitly
                cer.channels.append(Channel(
                    name=name + "/$DeadLetterQueue", kind="queue",
                    ordering="fifo", delivery="at_least_once",
                    description="native Service Bus dead-letter "
                                "sub-queue"))
            if props.get("maxDeliveryCount"):
                cer.consumers.append(Consumer(
                    name=name + "_delivery", channels=[name],
                    retry=RetryPolicy(
                        max_attempts=int(props["maxDeliveryCount"]),
                        dead_letter=(name + "/$DeadLetterQueue")
                        if dl else "")))
            found = True
        elif "subscriptions" in rtype and "servicebus" in rtype:
            cer.consumers.append(Consumer(
                name=name, group=name,
                channels=[_clean(str(r.get("name", "")).split("/")[-2])]))
            found = True
        elif "iothub" in rtype:
            routes = (props.get("routing", {}) or {}).get("routes", [])
            for rt in routes:
                cer.routing.append(RoutingRule(
                    name=_clean(rt.get("name", "")),
                    source="DeviceMessages",
                    target=",".join(rt.get("endpointNames", [])),
                    condition=rt.get("condition", "true"),
                    kind="iot_rule"))
            cer.iot_sources.append(IoTSource(
                name=name, protocol="iothub",
                topics=["DeviceMessages"],
                device_twin=True))
            found = True
    return found


def parse_pubsub(doc: dict, cer: CER) -> bool:
    # subscriptions are the distinguishing Pub/Sub shape — a bare
    # "topics" list is a Kafka topic export
    if "subscriptions" not in doc:
        return False
    for t in doc.get("topics", []):
        tname = str(t.get("name", t) if isinstance(t, dict) else t)
        cer.channels.append(Channel(
            name=_clean(tname.rsplit("/", 1)[-1]), kind="topic",
            ordering="per_key" if isinstance(t, dict) and
            t.get("messageStoragePolicy") is not None or True else
            "none",
            delivery="at_least_once"))
    for s in doc.get("subscriptions", []):
        dlp = s.get("deadLetterPolicy", {}) or {}
        rp = s.get("retryPolicy", {}) or {}
        cer.consumers.append(Consumer(
            name=_clean(str(s.get("name", "")).rsplit("/", 1)[-1]),
            group=_clean(str(s.get("name", "")).rsplit("/", 1)[-1]),
            channels=[_clean(str(s.get("topic", "")).rsplit("/", 1)[-1])],
            retry=RetryPolicy(
                max_attempts=int(dlp.get("maxDeliveryAttempts", 0) or 0),
                backoff_ms=int(str(rp.get("minimumBackoff",
                                          "0s")).rstrip("s") or 0) * 1000,
                dead_letter=_clean(str(dlp.get("deadLetterTopic", ""))
                                   .rsplit("/", 1)[-1]))))
        if s.get("enableMessageOrdering"):
            ch = cer.channel(_clean(str(s.get("topic", ""))
                                    .rsplit("/", 1)[-1]))
            if ch is not None:
                ch.ordering = "per_key"
                ch.key_fields = ["ordering_key"]
    return True


# ===========================================================================
# MQTT / IoT
# ===========================================================================

def parse_mosquitto_conf(text: str, cer: CER) -> bool:
    if "listener" not in text and "allow_anonymous" not in text and \
            "persistence" not in text:
        return False
    props = dict(re.findall(r"^(\w+)\s+(.+)$", text, re.M))
    cer.connections.append({"name": "mqtt_broker", "kind": "mqtt",
                            "persistence": props.get("persistence", ""),
                            "max_qos": props.get("max_qos", "2")})
    if props.get("allow_anonymous", "true").lower() == "true":
        cer.add_issue("WARNING", "MQTT_ANONYMOUS",
                      "Broker allows anonymous connections — carry an "
                      "auth policy to the target",
                      suggestion="Map to SAS/IAM/mTLS on the target hub.")
    return True


def parse_aws_iot_rules(doc: dict, cer: CER) -> bool:
    rules = doc.get("rules") or ([doc] if "sql" in doc else None)
    if not rules:
        return False
    for r in rules:
        sql = str(r.get("sql", ""))
        m = re.search(r"FROM\s+'([^']+)'", sql, re.I)
        topic = m.group(1) if m else ""
        name = _clean(r.get("ruleName", r.get("name", "iot_rule")))
        # One rule can fan out to several destinations. Joining them into a
        # single "kinesis,sns" string made two destinations look like one
        # channel with a comma in its name — the lineage then carried a
        # target that could never resolve. Each action is its own edge.
        actions = []
        for a in r.get("actions", []):
            if not isinstance(a, dict) or not a:
                continue
            kind = list(a.keys())[0]
            cfg = a[kind] if isinstance(a[kind], dict) else {}
            ident = (cfg.get("streamName") or cfg.get("topic")
                     or cfg.get("queueUrl") or cfg.get("targetArn")
                     or cfg.get("functionArn") or cfg.get("tableName") or "")
            actions.append({"kind": kind,
                            "name": "%s:%s" % (kind, ident) if ident
                                    else kind})
        for a in actions:
            cer.routing.append(RoutingRule(
                name="%s->%s" % (name, a["kind"]), source=topic,
                target=a["name"], condition=sql, kind="iot_rule"))
        if not actions:
            cer.routing.append(RoutingRule(
                name=name, source=topic, target="", condition=sql,
                kind="iot_rule"))
        cer.transformations.append(StreamTransformation(
            name=name, kind="rule", inputs=[topic],
            output=actions[0]["name"] if actions else "",
            sql=sql, raw=json.dumps(r)[:1500], engine="aws_iot"))
        if len(actions) > 1:
            cer.add_issue(
                "WARNING", "IOT_RULE_FANOUT",
                "Rule '%s' delivers to %d destinations (%s) — a target "
                "with a single output cannot reproduce the fan-out"
                % (name, len(actions),
                   ", ".join(a["name"] for a in actions)), name)
        if topic and cer.channel(topic) is None:
            cer.channels.append(Channel(
                name=topic, kind="mqtt_topic", is_iot=True,
                delivery=("at_least_once"
                          if int(r.get("qos", 1) or 1) >= 1
                          else "at_most_once")))
            if re.search(r"[+#]", topic):
                # sensors/+/temperature matches many real topics; anything
                # keyed on a flattened identifier will collide.
                cer.add_issue(
                    "WARNING", "MQTT_WILDCARD_TOPIC",
                    "Topic '%s' is an MQTT wildcard, not a concrete topic "
                    "— targets that flatten it to an identifier will merge "
                    "every matching topic into one object" % topic, topic,
                    suggestion="Enumerate the concrete topics if they need "
                               "to stay separate on the target.")
        cer.iot_sources.append(IoTSource(
            name=name, protocol="mqtt", topics=[topic],
            qos=int(r.get("qos", 1) or 1)))
    return True


def parse_device_twin(doc: dict, cer: CER) -> bool:
    if "deviceId" not in doc or "properties" not in doc:
        return False
    rep = (doc.get("properties", {}) or {}).get("reported", {}) or {}
    cer.iot_sources.append(IoTSource(
        name=_clean(doc["deviceId"]), protocol="iothub",
        topics=["devices/%s/messages/events" % doc["deviceId"]],
        telemetry_fields=[{"name": k, "type": type(v).__name__}
                          for k, v in rep.items()
                          if not isinstance(v, dict)],
        device_twin=True))
    return True


# ===========================================================================
# NiFi / StreamSets
# ===========================================================================

def parse_nifi_template(root: ET.Element, cer: CER) -> bool:
    if root.tag.rsplit("}", 1)[-1] != "template" and \
            root.find(".//processors") is None:
        return False
    procs: Dict[str, str] = {}
    for pr in root.iter("processors"):
        pid = (pr.findtext("id") or "").strip()
        name = _clean(pr.findtext("name") or pid)
        ptype = (pr.findtext("type") or "").rsplit(".", 1)[-1]
        procs[pid] = name
        if ptype.startswith(("Consume", "Get", "Listen")):
            cer.producers.append(Producer(name=name,
                                          channels=["nifi_" + name]))
            cer.channels.append(Channel(name="nifi_" + name,
                                        kind="stream"))
        elif ptype.startswith(("Publish", "Put")):
            cer.consumers.append(Consumer(name=name,
                                          channels=[]))
        else:
            cer.transformations.append(StreamTransformation(
                name=name, kind="processor", engine="nifi",
                raw=ptype))
    for conn in root.iter("connections"):
        src = procs.get((conn.findtext("source/id") or "").strip(), "")
        dst = procs.get((conn.findtext("destination/id") or "").strip(),
                        "")
        if src and dst:
            cer.routing.append(RoutingRule(
                name="flow_%s_%s" % (src, dst), source=src, target=dst,
                condition=",".join(
                    r.text or "" for r in conn.iter(
                        "selectedRelationships")),
                kind="route"))
    return bool(procs)


def parse_streamsets(doc: dict, cer: CER) -> bool:
    cfg = doc.get("pipelineConfig")
    if not isinstance(cfg, dict) or "stages" not in cfg:
        return False
    for st in cfg.get("stages", []):
        name = _clean(st.get("instanceName", ""))
        stage = str(st.get("stageName", "")).rsplit("_", 1)[0]
        if not st.get("inputLanes"):
            cer.producers.append(Producer(name=name,
                                          channels=st.get("outputLanes",
                                                          [])))
        elif not st.get("outputLanes"):
            cer.consumers.append(Consumer(name=name,
                                          channels=st.get("inputLanes",
                                                          [])))
        else:
            cer.transformations.append(StreamTransformation(
                name=name, kind="processor",
                inputs=st.get("inputLanes", []),
                output=(st.get("outputLanes") or [""])[0],
                engine="streamsets", raw=stage))
        for lane in st.get("outputLanes", []):
            if cer.channel(_clean(lane)) is None:
                cer.channels.append(Channel(name=_clean(lane),
                                            kind="stream"))
    return True


# ===========================================================================
# GoldenGate / SLT / IBM CDC
# ===========================================================================

def parse_goldengate(text: str, fname: str, cer: CER) -> bool:
    if not re.search(r"^\s*(EXTRACT|REPLICAT)\s+\w+", text,
                     re.I | re.M):
        return False
    kind_m = re.search(r"^\s*(EXTRACT|REPLICAT)\s+(\w+)", text,
                       re.I | re.M)
    tables = [m.group(1) for m in re.finditer(
        r"^\s*TABLE\s+([\w.*]+)\s*[,;]", text, re.I | re.M)]
    maps = [(m.group(1), m.group(2)) for m in re.finditer(
        r"^\s*MAP\s+([\w.*]+)\s*,\s*TARGET\s+([\w.*]+)", text,
        re.I | re.M)]
    cer.cdc_sources.append(CDCSource(
        name=_clean(kind_m.group(2) if kind_m else fname),
        flavor="goldengate:" + (kind_m.group(1).lower() if kind_m
                                else "extract"),
        tables=tables or [s for s, _t in maps],
        output_channels=["gg_%s" % _clean(t)
                         for t in (tables or [t for _s, t in maps])],
        properties={"maps": json.dumps(maps)} if maps else {}))
    for t in (tables or [t for _s, t in maps]):
        name = "gg_%s" % _clean(t)
        if cer.channel(name) is None:
            cer.channels.append(Channel(name=name, kind="stream",
                                        is_cdc=True, ordering="per_key",
                                        key_fields=["primary_key"]))
    return True


def parse_cdc_json(doc: dict, cer: CER) -> bool:
    for key, flavor in (("slt", "sap_slt"), ("ibm_cdc", "ibm_cdc"),
                        ("cdc", "generic")):
        if key in doc:
            o = doc[key] or {}
            tables = o.get("tables", [])
            cer.cdc_sources.append(CDCSource(
                name=_clean(o.get("name", flavor)), flavor=flavor,
                database=str(o.get("source", o.get("database", ""))),
                tables=[str(t) for t in tables],
                snapshot_mode=str(o.get("initial_load",
                                        o.get("snapshot", ""))),
                output_channels=["%s_%s" % (flavor, _clean(str(t)))
                                 for t in tables]))
            for t in tables:
                name = "%s_%s" % (flavor, _clean(str(t)))
                if cer.channel(name) is None:
                    cer.channels.append(Channel(
                        name=name, kind="stream", is_cdc=True,
                        ordering="per_key",
                        key_fields=["primary_key"]))
            return True
    return False


# ===========================================================================
# detection + entry point
# ===========================================================================

def detect_event_platform(path: str) -> dict:
    p = Path(path)
    files = [p] if p.is_file() else sorted(
        f for f in p.rglob("*") if f.is_file())[:400]
    scores: Dict[str, int] = {}
    reasons: Dict[str, List[str]] = {}

    def add(fmt, pts, why):
        scores[fmt] = scores.get(fmt, 0) + pts
        reasons.setdefault(fmt, []).append(why)

    for f in files:
        try:
            head = f.read_text(errors="replace", encoding="utf-8")[:65536]
        except OSError:
            continue
        suf = f.suffix.lower()
        if suf == ".json":
            try:
                doc = json.loads(f.read_text(errors="replace", encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(doc, dict):
                continue
            if "connector.class" in str(doc.get("config", doc))[:2000]:
                if "debezium" in json.dumps(doc)[:3000].lower():
                    add("debezium", 30, "Debezium connector %s" % f.name)
                else:
                    add("kafka", 20, "Kafka Connect config %s" % f.name)
            elif "queues" in doc and "bindings" in doc:
                add("rabbitmq", 30, "RabbitMQ definitions %s" % f.name)
            elif "StreamDescription" in doc or "OpenShardCount" in doc:
                add("kinesis", 30, "Kinesis stream %s" % f.name)
            elif "resources" in doc and any(
                    m in head.lower() for m in ("servicebus", "eventhub",
                                                "iothub")):
                # one ARM template can mix Azure messaging resources —
                # score every family it contains
                if "eventhub" in head.lower():
                    add("eventhubs", 28, "Event Hubs ARM %s" % f.name)
                if "servicebus" in head.lower():
                    add("servicebus", 26, "Service Bus ARM %s" % f.name)
                if "iothub" in head.lower():
                    add("iothub", 27, "IoT Hub ARM %s" % f.name)
            elif "subscriptions" in doc and "topics" in doc:
                add("pubsub", 26, "Pub/Sub export %s" % f.name)
            elif "namespaces" in doc or "tenant" in doc:
                add("pulsar", 24, "Pulsar admin export %s" % f.name)
            elif "rules" in doc and "sql" in json.dumps(doc)[:2000]:
                add("awsiot", 26, "IoT topic rules %s" % f.name)
            elif "sql" in doc and "actions" in doc:
                add("awsiot", 26, "IoT topic rule %s" % f.name)
            elif "deviceId" in doc:
                add("iothub", 18, "device twin %s" % f.name)
            elif "pipelineConfig" in doc:
                add("streamsets", 30, "StreamSets pipeline %s" % f.name)
            elif "topics" in doc and isinstance(doc.get("topics"), list):
                add("kafka", 18, "topic export %s" % f.name)
            elif doc.get("type") == "record" or "subject" in doc:
                add("kafka", 10, "schema %s" % f.name)
            elif "slt" in doc or "ibm_cdc" in doc:
                add("slt" if "slt" in doc else "ibm_cdc", 26,
                    "CDC metadata %s" % f.name)
        elif suf in (".avsc",):
            add("kafka", 12, "Avro schema %s" % f.name)
        elif suf == ".sql" and re.search(
                r"CREATE\s+(STREAM|TABLE).+(KAFKA_TOPIC|WATERMARK|"
                r"TUMBLING|TUMBLE|EMIT\s+CHANGES)", head,
                re.I | re.S):
            add("confluent" if "EMIT CHANGES" in head.upper() or
                "KAFKA_TOPIC" in head.upper() else "kafka", 24,
                "streaming SQL %s" % f.name)
        elif suf == ".properties" and (
                "bootstrap.servers" in head or "group.id" in head):
            add("kafka", 14, "client properties %s" % f.name)
        elif suf == ".mqsc" or re.search(r"DEFINE\s+QLOCAL", head, re.I):
            add("ibmmq", 30, "MQSC script %s" % f.name)
        elif suf == ".xml":
            if "<template" in head and "processors" in head:
                add("nifi", 30, "NiFi template %s" % f.name)
            elif "activemq" in head.lower() or "<broker" in head:
                add("activemq", 24, "ActiveMQ config %s" % f.name)
        elif suf == ".prm" or re.search(r"^\s*(EXTRACT|REPLICAT)\s+\w+",
                                        head, re.I | re.M):
            add("goldengate", 30, "GoldenGate params %s" % f.name)
        elif suf == ".conf" and ("listener" in head or
                                 "allow_anonymous" in head):
            add("mqtt", 24, "Mosquitto conf %s" % f.name)
    if not scores:
        return {"detected_platform": "", "confidence": 0, "reasons": []}
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return {"detected_platform": ranked[0][0],
            "confidence": min(99, 40 + ranked[0][1]),
            "reasons": reasons[ranked[0][0]][:8],
            "alternatives": [{"platform": k, "score": v}
                             for k, v in ranked[1:4]]}


def parse_events(path: str, platform: str = "") -> CER:
    p = Path(path)
    files = [p] if p.is_file() else sorted(
        f for f in p.rglob("*") if f.is_file())
    if not platform:
        platform = detect_event_platform(path)["detected_platform"]
        if not platform:
            raise ValueError("Could not detect an event platform at %s"
                             % path)
    cer = CER(name=_clean(p.stem), source_platform=platform)
    seen = False
    for f in files:
        suf = f.suffix.lower()
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:2000]:
            cer.add_issue("ERROR", "EVENT_BINARY_ARTIFACT",
                          "%s is binary — export metadata as JSON/text "
                          "and re-run" % f.name)
            continue
        text = data.decode("utf-8", errors="replace")
        if suf == ".json" or suf == ".avsc":
            try:
                doc = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(doc, list):
                handled = all(
                    isinstance(d, dict) and
                    (_parse_schema(d, cer, f.stem) or
                     _parse_connect(d, cer, f.stem)) for d in doc) \
                    if doc else False
                seen = seen or handled
                continue
            if not isinstance(doc, dict):
                continue
            if isinstance(doc.get("topics"), list) and \
                    "subscriptions" not in doc:
                for t in doc["topics"]:
                    if isinstance(t, dict):
                        _kafka_topic(t, cer)
                seen = True
            for fn in (parse_rabbitmq, parse_pulsar, parse_kinesis,
                       parse_azure_arm, parse_pubsub,
                       parse_aws_iot_rules, parse_device_twin,
                       parse_streamsets, parse_cdc_json):
                if fn(doc, cer):
                    seen = True
                    break
            else:
                if _parse_connect(doc, cer, f.stem) or \
                        _parse_schema(doc, cer, f.stem):
                    seen = True
            # consumer group describe export
            if "groupId" in doc:
                cer.consumers.append(Consumer(
                    name=_clean(doc["groupId"]),
                    group=_clean(doc["groupId"]),
                    channels=[_clean(m.get("topic", "")) for m in
                              doc.get("members", []) if m.get("topic")]))
                seen = True
        elif suf == ".sql":
            if parse_streaming_sql(text, f.stem, cer):
                seen = True
        elif suf == ".properties":
            if _parse_kafka_properties(text, f.stem, cer):
                seen = True
        elif suf in (".mqsc", ".txt") and re.search(
                r"DEFINE\s+QLOCAL", text, re.I):
            if parse_mqsc(text, cer):
                seen = True
        elif suf == ".xml":
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                continue
            if parse_nifi_template(root, cer) or \
                    parse_activemq_xml(root, cer):
                seen = True
        elif suf == ".prm":
            if parse_goldengate(text, f.stem, cer):
                seen = True
        elif suf == ".conf":
            if parse_mosquitto_conf(text, cer):
                seen = True
    if not seen and not cer.issues:
        raise FileNotFoundError("No event platform artifacts under %s"
                                % path)
    cer.metadata["inventory"] = cer.inventory()
    return cer

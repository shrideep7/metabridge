"""Event target generators (Command 8, §5–§8) — everything FROM CER.

Broker/service targets:
    kafka / confluent    topics JSON + kafka-topics.sh + Connect configs
                         (CDC feeds re-emitted as Debezium configs) +
                         ksqlDB SQL for windowed jobs (confluent)
    pulsar               pulsar-admin script (retention, dedup, subs)
    rabbitmq             definitions.json (quorum queues, DLX, bindings,
                         permissions)
    eventhubs/servicebus ARM templates (partitions, retention, sessions
                         for FIFO, dead-lettering, maxDeliveryCount)
    kinesis              CloudFormation (shards from partitions)
    pubsub               Terraform (ordering, DLQ policy, retry)
    mqtt / iothub        topic map + IoT Hub ARM routes

Streaming engines:
    flink                Flink SQL (Kafka DDL + WATERMARK + TUMBLE/HOP/
                         SESSION windowed inserts)
    spark_streaming      PySpark Structured Streaming (withWatermark,
                         window(), checkpoints per state store)
    databricks_streaming Delta Live Tables (APPLY CHANGES INTO for CDC,
                         streaming tables for topics)
    snowflake_streaming  Snowpipe Streaming + dynamic tables SQL
    dbt_streaming        dbt models where the target supports streaming
                         materializations (limitations declared)

Semantics a target cannot express are written into the artifacts as
explicit _metabridge notes/comments (delivery downgrades, ordering,
retries) — never dropped. No pairwise converters: every generator reads
only the CER.

That "never dropped" promise is enforced by ``GenReport``: a generator
that changes a value calls ``adjust()`` and one that cannot emit
something at all calls ``skip()``. Both end up in the artifacts, in a
``_metabridge_generation_notes.md`` companion file, and in the scores —
so a lossy run cannot report full coverage.

Direction is ONE-WAY. Some platforms can be imported but never generated
to (see ``validate.SOURCE_ONLY_PLATFORMS``: IBM MQ, ActiveMQ, NiFi,
StreamSets, GoldenGate, Debezium) — modernization moves off them, never
onto them. And a target's ROLE matters as much as its name: only a
``broker`` target (see ``validate.TARGET_KIND``) can replace the source.
Processor and sink targets are additions to the estate — they need the
source broker, or a broker replacement, to keep running and feed them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .cer import CER, Channel, StreamTransformation
from .validate import TARGET_CAPS, TARGET_KIND, TARGET_LIMITS

EVENT_TARGETS = ("kafka", "confluent", "pulsar", "rabbitmq", "eventhubs",
                 "servicebus", "kinesis", "pubsub", "mqtt", "iothub",
                 "flink", "spark_streaming", "databricks_streaming",
                 "snowflake_streaming", "dbt_streaming", "scaffold")


def _ident(s: str) -> str:
    s = re.sub(r"\W+", "_", str(s)).strip("_").lower()
    return ("t_" + s) if s and s[0].isdigit() else (s or "channel")


@dataclass
class GenReport:
    """What generation actually did to the estate.

    Two kinds of entry, and the distinction matters:

    * ``adjust`` — a value was CHANGED to fit the target (a partition count
      clamped, a retention truncated). The artifact is complete but no
      longer identical to the source.
    * ``skip``   — CER content was NOT written at all. The artifact is
      incomplete.

    Both used to happen silently. Every generator now records them, they
    are written into the artifacts as target-native comments, and
    ``event_intelligence`` subtracts skips from the headline score so a
    lossy run cannot report 100%.
    """
    target: str = ""
    notes: List[dict] = field(default_factory=list)
    unemitted: List[dict] = field(default_factory=list)

    def adjust(self, obj: str, code: str, note: str) -> None:
        self.notes.append({"object": obj, "code": code, "note": note})

    def skip(self, kind: str, name: str, reason: str) -> None:
        self.unemitted.append({"kind": kind, "name": name,
                               "reason": reason})

    def to_dict(self) -> dict:
        return {"target": self.target,
                "target_role": TARGET_KIND.get(self.target, ""),
                "notes": self.notes, "unemitted": self.unemitted}

    def lines(self, prefix: str = "--") -> List[str]:
        """The report as comments, for embedding in a text artifact."""
        out: List[str] = []
        for n in self.notes:
            out.append("%s ADJUSTED %s: %s" % (prefix, n["object"],
                                               n["note"]))
        for s in self.unemitted:
            out.append("%s NOT EMITTED — %s '%s': %s"
                       % (prefix, s["kind"], s["name"], s["reason"]))
        return out

    def is_empty(self) -> bool:
        return not self.notes and not self.unemitted


def _rep(rep: Optional[GenReport], target: str = "") -> GenReport:
    """Generators are callable standalone (tests do), so a missing report
    is replaced by a throwaway rather than guarded at every call site."""
    if rep is None:
        return GenReport(target=target)
    if target and not rep.target:
        rep.target = target
    return rep


def _clamp(rep: GenReport, obj: str, what: str, src: int, limit: int,
           unit: str) -> int:
    """Clamp a value to a target ceiling and SAY SO. The silent version of
    this was losing three quarters of a channel's parallelism without a
    single line of output anywhere."""
    if limit and src > limit:
        rep.adjust(obj, what.upper() + "_CLAMPED",
                   "%s reduced from %d to %d %s — the target's ceiling"
                   % (what, src, limit, unit))
        return limit
    return src


def _declare_unemittable(cer: CER, rep: GenReport, target: str, *,
                         producers: bool = False, schemas: bool = False,
                         security: bool = False,
                         consumers: bool = False) -> None:
    """Record CER sections this target has no place to put. Passing a flag
    means "this generator does not express that section"."""
    if producers:
        for p in cer.producers:
            rep.skip("producer", p.name,
                     "%s has no producer-side configuration; acks=%s "
                     "idempotent=%s transactional=%s must be set in the "
                     "publishing application"
                     % (target, p.acks or "unset", p.idempotent,
                        p.transactional))
    if consumers:
        for c in cer.consumers:
            rep.skip("consumer", c.name,
                     "%s has no consumer-group configuration; group=%s "
                     "offset_reset=%s must be set in the subscribing "
                     "application" % (target, c.group or "unset",
                                      c.offset_reset or "unset"))
    if schemas:
        for s in cer.schemas:
            rep.skip("schema", s.name,
                     "%s has no schema registry; %s schema (compatibility "
                     "%s) must be registered separately"
                     % (target, s.format, s.compatibility or "unset"))
    if security:
        for s in cer.security:
            rep.skip("security", s.principal,
                     "%s access control is not generated; %s on '%s' must "
                     "be re-granted by hand"
                     % (target, "/".join(s.operations) or s.kind,
                        s.resource or "*"))


def _notes_for(ch: Channel, target: str) -> List[str]:
    caps = TARGET_CAPS.get(target, {})
    notes = []
    if ch.delivery == "exactly_once" and not caps.get("exactly_once"):
        notes.append("source guarantees exactly-once; %s is at-least-"
                     "once — deduplicate downstream (idempotency key)"
                     % target)
    if ch.ordering in ("fifo", "global") and not caps.get("fifo"):
        notes.append("source guarantees %s ordering — use one partition "
                     "or an ordering key on %s" % (ch.ordering, target))
    if ch.dead_letter and not caps.get("dlq_native"):
        notes.append("dead-letter route to '%s' must be implemented in "
                     "the consumer on %s" % (ch.dead_letter, target))
    return notes


# ---------------------------------------------------------------------------
# broker targets
# ---------------------------------------------------------------------------

def generate_kafka(cer: CER, flavor: str = "kafka",
                   rep: Optional[GenReport] = None) -> Dict[str, str]:
    rep = _rep(rep, flavor)
    topics, cmds = [], ["#!/bin/sh", "# generated by MetaBridge AI from "
                        + cer.source_platform]
    for ch in cer.channels:
        cfg: Dict[str, str] = dict(
            (k, v) for k, v in ch.properties.items() if "." in k)
        if ch.retention.time_ms:
            cfg["retention.ms"] = str(ch.retention.time_ms)
        if ch.retention.policy != "delete":
            cfg["cleanup.policy"] = ch.retention.policy
        if ch.compression:
            cfg["compression.type"] = ch.compression
        # Replication is reproduced FAITHFULLY. This used to quietly
        # rewrite rf=2 as rf=3: a well-meant durability bump that made the
        # artifact disagree with the source with nothing recording why.
        notes = _notes_for(ch, flavor)
        if 0 < ch.replication < 3 and ch.kind in ("topic", "stream"):
            notes.append("replication factor %d reproduced from the "
                         "source; 3 is the usual production minimum"
                         % ch.replication)
        entry = {"name": ch.name, "partitions": max(1, ch.partitions),
                 "replication_factor": max(1, ch.replication),
                 "config": cfg}
        if ch.ordering in ("fifo", "global"):
            entry["partitions"] = 1
            notes.append("partitions forced to 1 to preserve %s "
                         "ordering" % ch.ordering)
            if ch.partitions > 1:
                rep.adjust(ch.name, "PARTITIONS_SERIALIZED",
                           "%d partitions collapsed to 1 to keep %s "
                           "ordering — throughput is now single-consumer"
                           % (ch.partitions, ch.ordering))
        if notes:
            entry["_metabridge_notes"] = notes
        topics.append(entry)
        cmds.append("kafka-topics.sh --create --topic '%s' "
                    "--partitions %d --replication-factor %d %s"
                    % (ch.name, entry["partitions"],
                       entry["replication_factor"],
                       " ".join("--config %s=%s" % (k, v)
                                for k, v in cfg.items())))
    out = {"topics.json": json.dumps({"topics": topics}, indent=2) + "\n",
           "create_topics.sh": "\n".join(cmds) + "\n"}

    # Producer / consumer semantics. The CER has carried acks,
    # idempotence, transactionality, offset-reset and retry all along and
    # NO target was writing any of it out.
    for p in cer.producers:
        props = ["# producer '%s' — generated by MetaBridge AI" % p.name,
                 "bootstrap.servers=CHANGEME:9092",
                 "acks=%s" % (p.acks or "all"),
                 "enable.idempotence=%s" % str(p.idempotent).lower()]
        if p.transactional:
            props += ["transactional.id=%s-tx" % _ident(p.name),
                      "# source was transactional — keep exactly-once "
                      "semantics end to end"]
        if not p.acks:
            props.append("# NOTE acks not declared in the source; "
                         "defaulted to all (safest)")
        props += ["%s=%s" % (k, v) for k, v in p.properties.items()
                  if "." in k]
        out["producer_%s.properties" % _ident(p.name)] = \
            "\n".join(props) + "\n"
    for c in cer.consumers:
        props = ["# consumer '%s' — generated by MetaBridge AI" % c.name,
                 "bootstrap.servers=CHANGEME:9092",
                 "group.id=%s" % (c.group or _ident(c.name)),
                 "auto.offset.reset=%s" % (c.offset_reset or "earliest"),
                 "enable.auto.commit=%s"
                 % str(not c.manual_commit).lower()]
        if c.max_in_flight:
            props.append("max.poll.records=%d" % c.max_in_flight)
        if c.retry.max_attempts:
            props.append("# retry: %d attempts, %dms backoff (x%s)"
                         % (c.retry.max_attempts, c.retry.backoff_ms,
                            c.retry.backoff_multiplier))
            props.append("# Kafka has no broker-side retry — implement in "
                         "the consumer, or use a retry topic")
        if c.retry.dead_letter:
            props.append("# dead-letter destination: %s (produce to it "
                         "from the consumer)" % c.retry.dead_letter)
        out["consumer_%s.properties" % _ident(c.name)] = \
            "\n".join(props) + "\n"

    # Schema Registry subjects — parsed from .avsc all along, emitted
    # nowhere but Flink columns until now.
    if cer.schemas:
        reg = ["#!/bin/sh", "# Schema Registry subjects — generated by "
                            "MetaBridge AI", "SR=${SR:-http://localhost:8081}"]
        for s in cer.schemas:
            fname = "schemas/%s.%s" % (_ident(s.name),
                                       "avsc" if s.format == "avro"
                                       else "json")
            out[fname] = (s.definition.strip() or json.dumps(
                {"type": "record", "name": _ident(s.name),
                 "fields": s.fields}, indent=2)) + "\n"
            subject = "%s-value" % ch_for_schema(cer, s.name)
            if s.compatibility:
                reg.append('curl -sX PUT "$SR/config/%s" -H "Content-Type:'
                           ' application/json" -d \'{"compatibility":'
                           ' "%s"}\'' % (subject, s.compatibility))
            else:
                reg.append("# NOTE subject %s has no compatibility mode in "
                           "the source — registry default applies"
                           % subject)
            reg.append('curl -sX POST "$SR/subjects/%s/versions" -H '
                       '"Content-Type: application/vnd.schemaregistry.v1'
                       '+json" --data-binary @- <<\'EOF\'\n'
                       '{"schemaType": "%s", "schema": %s}\nEOF'
                       % (subject, s.format.upper(),
                          json.dumps(s.definition or json.dumps(
                              {"type": "record", "name": _ident(s.name),
                               "fields": s.fields}))))
        out["register_schemas.sh"] = "\n".join(reg) + "\n"

    # ACLs — previously emitted for RabbitMQ only, dropped everywhere else.
    if cer.security:
        acl = ["#!/bin/sh", "# ACLs — generated by MetaBridge AI",
               "# review every principal before running: names and realms "
               "rarely survive a platform move unchanged"]
        for s in cer.security:
            ops = [o.upper() for o in s.operations] or ["READ"]
            acl.append("kafka-acls.sh --add --allow-principal 'User:%s' %s "
                       "--topic '%s'"
                       % (s.principal,
                          " ".join("--operation %s" % o for o in ops),
                          s.resource or "*"))
        out["acls.sh"] = "\n".join(acl) + "\n"
    for cdc in cer.cdc_sources:
        cfgd = {"connector.class":
                "io.debezium.connector.%s.%sConnector" % (
                    ("postgresql", "Postgres")
                    if "postgres" in cdc.flavor else ("oracle", "Oracle")
                    if "oracle" in cdc.flavor or
                    "goldengate" in cdc.flavor else ("sqlserver",
                                                     "SqlServer")),
                "table.include.list": ",".join(cdc.tables),
                "topic.prefix": cdc.name,
                "snapshot.mode": cdc.snapshot_mode or "initial",
                "_metabridge_note": "re-platformed from %s — validate "
                "table list and credentials" % cdc.flavor}
        out["connect_%s.json" % _ident(cdc.name)] = json.dumps(
            {"name": cdc.name, "config": cfgd}, indent=2) + "\n"
    if flavor == "confluent":
        ksql = generate_ksql(cer)
        if ksql:
            out["streams.ksql"] = ksql
    else:
        # Plain Kafka is a broker: it transports, it does not compute. Say
        # so per job instead of leaving the reader to notice the absence.
        for t in cer.transformations:
            rep.skip("transformation", t.name,
                     "Kafka is a broker and does not execute stream "
                     "processing; port this %s job to ksqlDB, Kafka "
                     "Streams or Flink"
                     % (t.engine or t.kind or "streaming"))
    if rep.notes or rep.unemitted:
        out["_metabridge_generation_notes.md"] = _notes_markdown(cer, rep)
    return out


def ch_for_schema(cer: CER, schema_name: str) -> str:
    """Subject naming follows TopicNameStrategy, so a schema needs the
    channel that references it; fall back to its own name."""
    for c in cer.channels:
        if c.schema == schema_name:
            return c.name
    return schema_name


def _notes_markdown(cer: CER, rep: GenReport) -> str:
    """A human-readable companion to the artifacts. Written whenever
    anything was changed or omitted, so 'what did this run not do?' has an
    answer sitting next to the output instead of only in a JSON report."""
    role = TARGET_KIND.get(rep.target, "")
    lines = ["# Generation notes — %s -> %s"
             % (cer.source_platform or "source", rep.target or "target"),
             "",
             "Generated by MetaBridge AI. **Read this before deploying the "
             "artifacts.**", ""]
    if role in ("sink", "processor"):
        lines += ["> **This target does not replace %s.** It is a %s: the "
                  "source broker (or a broker replacement) must keep "
                  "running to feed it."
                  % (cer.source_platform or "the source", role), ""]
    if rep.unemitted:
        lines += ["## Not emitted (%d)" % len(rep.unemitted), "",
                  "These exist in the source and are **absent** from the "
                  "generated artifacts.", "",
                  "| Kind | Name | Why |", "|---|---|---|"]
        lines += ["| %s | `%s` | %s |" % (s["kind"], s["name"], s["reason"])
                  for s in rep.unemitted]
        lines.append("")
    if rep.notes:
        lines += ["## Changed to fit the target (%d)" % len(rep.notes), "",
                  "These were emitted, but **not identically** to the "
                  "source.", "",
                  "| Object | Change |", "|---|---|"]
        lines += ["| `%s` | %s |" % (n["object"], n["note"])
                  for n in rep.notes]
        lines.append("")
    return "\n".join(lines) + "\n"


def generate_ksql(cer: CER) -> str:
    lines = ["-- generated by MetaBridge AI"]
    for t in cer.transformations:
        if not t.sql and not t.raw:
            continue
        if t.window is not None:
            w = t.window
            win = {"tumbling": "TUMBLING (SIZE %d SECONDS)"
                   % (w.size_ms // 1000),
                   "hopping": "HOPPING (SIZE %d SECONDS, ADVANCE BY %d "
                   "SECONDS)" % (w.size_ms // 1000,
                                 max(1, w.slide_ms // 1000)),
                   "session": "SESSION (%d SECONDS)"
                   % (max(w.gap_ms, w.size_ms) // 1000)}.get(w.kind, "")
            body = t.sql or "SELECT * FROM %s" % (t.inputs[0]
                                                  if t.inputs else "src")
            grp = (" GROUP BY " + ", ".join(t.group_by)) \
                if t.group_by and "GROUP BY" not in body.upper() else ""
            lines.append(
                "CREATE TABLE %s WITH (KAFKA_TOPIC='%s') AS\n  %s\n"
                "  WINDOW %s%s EMIT CHANGES;"
                % (_ident(t.name), t.output or t.name,
                   _strip_group(body), win, grp))
            if w.time_semantics == "event_time" and \
                    not w.watermark_delay_ms:
                lines.append("-- MANUAL: no watermark declared in the "
                             "source — late-event behaviour undefined")
        elif t.sql:
            lines.append("CREATE STREAM %s WITH (KAFKA_TOPIC='%s') AS\n"
                         "  %s EMIT CHANGES;"
                         % (_ident(t.name), t.output or t.name, t.sql))
    return "\n\n".join(lines) + "\n" if len(lines) > 1 else ""


def _strip_group(sql: str) -> str:
    return re.sub(r"\s+GROUP\s+BY\s+.+$", "", sql, flags=re.I | re.S)


def generate_rabbitmq(cer: CER, rep: Optional[GenReport] = None) -> str:
    rep = _rep(rep, "rabbitmq")
    _declare_unemittable(cer, rep, "RabbitMQ", producers=True,
                         schemas=True)
    for t in cer.transformations:
        rep.skip("transformation", t.name,
                 "RabbitMQ is a broker and does not execute stream "
                 "processing; this %s job needs a processing engine"
                 % (t.engine or t.kind or "streaming"))
    queues, exchanges, bindings, permissions = [], [], [], []
    for ch in cer.channels:
        args: Dict[str, object] = {}
        if ch.replication > 1:
            args["x-queue-type"] = "quorum"
        if ch.retention.time_ms:
            args["x-message-ttl"] = ch.retention.time_ms
        if ch.dead_letter:
            args["x-dead-letter-exchange"] = ch.dead_letter
        q = {"name": ch.name, "vhost": "/", "durable": True,
             "arguments": args}
        notes = _notes_for(ch, "rabbitmq")
        if ch.partitions > 1:
            notes.append("source had %d partitions — RabbitMQ queues "
                         "are single-ordered; shard across %d queues "
                         "or accept serialized consumption"
                         % (ch.partitions, ch.partitions))
            rep.adjust(ch.name, "PARTITIONS_NOT_EXPRESSIBLE",
                       "%d partitions became 1 queue — RabbitMQ has no "
                       "partition concept; shard manually or accept "
                       "serialized consumption" % ch.partitions)
        if notes:
            q["_metabridge_notes"] = notes
        queues.append(q)
    for r in cer.routing:
        if r.kind == "binding":
            bindings.append({"source": r.source, "vhost": "/",
                             "destination": r.target,
                             "destination_type": "queue",
                             "routing_key": r.condition})
        elif r.kind == "route" and r.condition.startswith("exchange:"):
            exchanges.append({"name": r.name, "vhost": "/",
                              "type": r.condition.split(":", 1)[1],
                              "durable": True})
    for s in cer.security:
        if s.kind in ("acl", "permission"):
            permissions.append({"user": s.principal, "vhost": "/",
                                "configure": "",
                                "write": s.resource or ".*",
                                "read": s.resource or ".*"})
    return json.dumps({"queues": queues, "exchanges": exchanges,
                       "bindings": bindings,
                       "permissions": permissions}, indent=2) + "\n"


def generate_pulsar(cer: CER, rep: Optional[GenReport] = None) -> str:
    rep = _rep(rep, "pulsar")
    _declare_unemittable(cer, rep, "Pulsar", producers=True, schemas=True,
                         security=True)
    for t in cer.transformations:
        rep.skip("transformation", t.name,
                 "Pulsar transports events; this %s job needs Pulsar "
                 "Functions or Flink"
                 % (t.engine or t.kind or "streaming"))
    lines = ["#!/bin/sh", "# generated by MetaBridge AI",
             "pulsar-admin tenants create metabridge || true",
             "pulsar-admin namespaces create metabridge/migrated || true"]
    for ch in cer.channels:
        lines.append("pulsar-admin topics create-partitioned-topic "
                     "persistent://metabridge/migrated/%s -p %d"
                     % (_ident(ch.name), max(1, ch.partitions)))
        if ch.retention.time_ms:
            lines.append("pulsar-admin namespaces set-retention "
                         "metabridge/migrated --time %dm --size -1"
                         % (ch.retention.time_ms // 60000))
        if ch.delivery == "exactly_once":
            lines.append("pulsar-admin namespaces set-deduplication "
                         "metabridge/migrated --enable")
        for n in _notes_for(ch, "pulsar"):
            lines.append("# NOTE %s: %s" % (ch.name, n))
    for c in cer.consumers:
        for chn in c.channels:
            lines.append("# subscription: %s -> Key_Shared on %s"
                         % (c.name, chn))
    lines += rep.lines("#")
    return "\n".join(lines) + "\n"


def generate_azure(cer: CER, kind: str,
                   rep: Optional[GenReport] = None) -> str:
    rep = _rep(rep, kind)
    _declare_unemittable(cer, rep, kind.title(), producers=True,
                         schemas=True, security=True)
    for t in cer.transformations:
        rep.skip("transformation", t.name,
                 "%s is a broker and does not execute stream processing; "
                 "this %s job needs Stream Analytics or Databricks"
                 % (kind, t.engine or t.kind or "streaming"))
    limits = TARGET_LIMITS.get(kind, {})
    res = []
    for ch in cer.channels:
        notes = _notes_for(ch, kind)
        if kind == "eventhubs":
            # Both of these used to clamp in silence: 64 partitions became
            # 32 and a 12-hour retention became 1 day with no output
            # anywhere recording the loss.
            parts = _clamp(rep, ch.name, "partitions",
                           max(1, ch.partitions),
                           limits.get("max_partitions", 0), "partitions")
            days = ch.retention.time_ms // 86400000
            if ch.retention.time_ms and not days:
                rep.adjust(ch.name, "RETENTION_ROUNDED_UP",
                           "retention %dms is under one day; Event Hubs "
                           "granularity is whole days, so it becomes 1 day"
                           % ch.retention.time_ms)
                days = 1
            days = _clamp(rep, ch.name, "retention", days or 1,
                          limits.get("max_retention_days", 0), "days")
            res.append({
                "type": "Microsoft.EventHub/namespaces/eventhubs",
                "apiVersion": "2021-11-01",
                "name": "metabridge-ns/%s" % _ident(ch.name),
                "properties": {
                    "partitionCount": parts,
                    "messageRetentionInDays": max(1, days)},
                **({"_metabridge_notes": notes} if notes else {})})
        else:
            props: Dict[str, object] = {
                "maxDeliveryCount": 10, "requiresSession":
                ch.ordering in ("fifo", "global"),
                "deadLetteringOnMessageExpiration": bool(ch.dead_letter)}
            for c in cer.consumers:
                if ch.name in c.channels and c.retry.max_attempts:
                    props["maxDeliveryCount"] = c.retry.max_attempts
            res.append({
                "type": "Microsoft.ServiceBus/namespaces/queues"
                if ch.kind == "queue" else
                "Microsoft.ServiceBus/namespaces/topics",
                "apiVersion": "2021-11-01",
                "name": "metabridge-sb/%s" % _ident(ch.name),
                "properties": props,
                **({"_metabridge_notes": notes} if notes else {})})
    return json.dumps({"$schema": "https://schema.management.azure.com/"
                       "schemas/2019-04-01/deploymentTemplate.json#",
                       "contentVersion": "1.0.0.0",
                       "resources": res}, indent=2) + "\n"


def generate_kinesis(cer: CER, rep: Optional[GenReport] = None) -> str:
    rep = _rep(rep, "kinesis")
    _declare_unemittable(cer, rep, "Kinesis", producers=True, schemas=True,
                         security=True)
    for t in cer.transformations:
        rep.skip("transformation", t.name,
                 "Kinesis Data Streams does not execute stream "
                 "processing; this %s job needs Managed Flink or Lambda"
                 % (t.engine or t.kind or "streaming"))
    limits = TARGET_LIMITS.get("kinesis", {})
    res = {}
    for ch in cer.channels:
        notes = _notes_for(ch, "kinesis")
        hours = ch.retention.time_ms // 3600000
        if ch.retention.time_ms and hours < 24:
            rep.adjust(ch.name, "RETENTION_RAISED",
                       "retention %dms is below the Kinesis 24h minimum "
                       "and becomes 24h" % ch.retention.time_ms)
        hours = _clamp(rep, ch.name, "retention", hours or 24,
                       limits.get("max_retention_hours", 0), "hours")
        res["Stream" + _ident(ch.name).title().replace("_", "")] = {
            "Type": "AWS::Kinesis::Stream",
            "Properties": {"Name": _ident(ch.name),
                           "ShardCount": max(1, ch.partitions),
                           "RetentionPeriodHours": max(24, hours)},
            **({"Metadata": {"MetaBridgeNotes": notes}} if notes
               else {})}
    return json.dumps({"AWSTemplateFormatVersion": "2010-09-09",
                       "Resources": res}, indent=2) + "\n"


def generate_pubsub(cer: CER, rep: Optional[GenReport] = None) -> str:
    rep = _rep(rep, "pubsub")
    _declare_unemittable(cer, rep, "Pub/Sub", producers=True, schemas=True,
                         security=True)
    for t in cer.transformations:
        rep.skip("transformation", t.name,
                 "Pub/Sub transports events; this %s job needs Dataflow "
                 "or BigQuery" % (t.engine or t.kind or "streaming"))
    limits = TARGET_LIMITS.get("pubsub", {})
    lines = ["# generated by MetaBridge AI — terraform"]
    lines += rep.lines("#")
    for ch in cer.channels:
        days = _clamp(rep, ch.name, "retention",
                      ch.retention.time_ms // 86400000 or 0,
                      limits.get("max_retention_days", 0), "days")
        retention = ('  message_retention_duration = "%ds"\n'
                     % (days * 86400)) if days else ""
        lines.append('resource "google_pubsub_topic" "%s" {\n'
                     '  name = "%s"\n%s}' % (_ident(ch.name),
                                             _ident(ch.name), retention))
    for c in cer.consumers:
        for chn in c.channels:
            body = ['  name  = "%s"' % _ident(c.name),
                    '  topic = google_pubsub_topic.%s.name'
                    % _ident(chn)]
            ch = cer.channel(chn)
            if ch is not None and ch.ordering in ("per_key", "fifo",
                                                  "global"):
                body.append("  enable_message_ordering = true")
            if c.retry.dead_letter:
                body.append("  dead_letter_policy {\n"
                            "    dead_letter_topic = "
                            'google_pubsub_topic.%s.id\n'
                            "    max_delivery_attempts = %d\n  }"
                            % (_ident(c.retry.dead_letter),
                               max(5, c.retry.max_attempts)))
            if c.retry.backoff_ms:
                body.append('  retry_policy {\n    minimum_backoff = '
                            '"%ds"\n  }' % max(1,
                                               c.retry.backoff_ms
                                               // 1000))
            lines.append('resource "google_pubsub_subscription" "%s" '
                         "{\n%s\n}" % (_ident(c.name), "\n".join(body)))
    return "\n\n".join(lines) + "\n"


def generate_mqtt(cer: CER, kind: str = "mqtt",
                  rep: Optional[GenReport] = None) -> Dict[str, str]:
    """MQTT / IoT Hub. Both used to emit one bespoke JSON document that no
    tool could consume — every other target produces something deployable
    (shell, Terraform, CloudFormation, ARM), so these now do too."""
    rep = _rep(rep, kind)
    _declare_unemittable(cer, rep, kind, producers=True, schemas=True)
    out: Dict[str, str] = {}

    def qos_of(ch: Channel) -> int:
        return (2 if ch.delivery == "exactly_once"
                else 1 if ch.delivery == "at_least_once" else 0)

    if kind == "iothub":
        endpoints = sorted({r.target for r in cer.routing if r.target})
        routes = [{"name": _ident(r.name), "source": "DeviceMessages",
                   "condition": _iot_sql_to_route_condition(r.condition),
                   "endpointNames": [_ident(r.target) or "events"],
                   "isEnabled": True} for r in cer.routing]
        hub = {
            "type": "Microsoft.Devices/IotHubs",
            "apiVersion": "2021-07-02",
            "name": "metabridge-iothub",
            "location": "[resourceGroup().location]",
            "sku": {"name": "S1", "capacity": 1},
            "properties": {
                "routing": {
                    "endpoints": {"serviceBusQueues": [],
                                  "serviceBusTopics": [],
                                  "eventHubs": [], "storageContainers": []},
                    "routes": routes,
                    "fallbackRoute": {
                        "name": "$fallback", "source": "DeviceMessages",
                        "condition": "true",
                        "endpointNames": ["events"], "isEnabled": True}}},
            "_metabridge_notes": [
                "endpoint definitions are intentionally empty — the source "
                "named %d destination(s) (%s) whose connection strings are "
                "not in a metadata export and must be supplied"
                % (len(endpoints), ", ".join(endpoints) or "none")]}
        if endpoints:
            rep.skip("routing_endpoints", ", ".join(endpoints),
                     "IoT Hub routes need endpoint connection strings, "
                     "which no metadata export contains — declare the "
                     "endpoints then re-point these routes")
        out["iothub_arm.json"] = json.dumps(
            {"$schema": "https://schema.management.azure.com/schemas/"
                        "2019-04-01/deploymentTemplate.json#",
             "contentVersion": "1.0.0.0", "resources": [hub]},
            indent=2) + "\n"
        # Device twins were parsed and then used by nothing at all.
        for i in cer.iot_sources:
            if not i.device_twin:
                continue
            out["twins/%s.json" % _ident(i.name)] = json.dumps(
                {"deviceId": i.name,
                 "properties": {"desired": {
                     "_metabridge_note": "desired properties are device "
                                         "state, not metadata — confirm "
                                         "values before provisioning"}},
                 "tags": {"migratedFrom": cer.source_platform},
                 "telemetry": i.telemetry_fields}, indent=2) + "\n"
    else:
        conf = ["# mosquitto.conf — generated by MetaBridge AI from %s"
                % cer.source_platform,
                "listener 1883", "persistence true",
                "acl_file /mosquitto/config/aclfile", ""]
        for ch in cer.channels:
            q = qos_of(ch)
            conf.append("# topic %s (QoS %d from delivery=%s)"
                        % (ch.name, q, ch.delivery))
            if q == 2:
                conf.append("#   QoS 2 preserves exactly-once; it is the "
                            "slowest MQTT flow")
        conf += rep.lines("#")
        out["mosquitto.conf"] = "\n".join(conf) + "\n"
        acl = ["# aclfile — generated by MetaBridge AI"]
        for s in cer.security:
            acl.append("user %s" % s.principal)
            acl.append("topic readwrite %s" % (s.resource or "#"))
        if not cer.security:
            acl += ["# no ACLs in the source import — this file denies "
                    "nothing; add users before exposing the broker",
                    "topic readwrite #"]
        out["aclfile"] = "\n".join(acl) + "\n"
        for i in cer.iot_sources:
            if i.device_twin:
                rep.skip("device_twin", i.name,
                         "plain MQTT has no device-twin concept; retained "
                         "messages or a shadow service are needed to hold "
                         "the %d reported field(s)"
                         % len(i.telemetry_fields))
    # bridge/route table stays available as data alongside the deployable
    out["%s_topics.json" % kind] = json.dumps(
        {"generated_by": "MetaBridge AI",
         "source_platform": cer.source_platform,
         "topics": [{"topic": ch.name, "qos": qos_of(ch),
                     "is_iot": ch.is_iot} for ch in cer.channels],
         "routes": [{"name": r.name, "source": r.source,
                     "target": r.target, "condition": r.condition}
                    for r in cer.routing],
         "devices": [{"name": i.name, "topics": i.topics, "qos": i.qos,
                      "device_twin": i.device_twin,
                      "telemetry_fields": i.telemetry_fields}
                     for i in cer.iot_sources]}, indent=2) + "\n"
    if rep.notes or rep.unemitted:
        out["_metabridge_generation_notes.md"] = _notes_markdown(cer, rep)
    return out


def _iot_sql_to_route_condition(sql: str) -> str:
    """IoT Hub routing conditions are a WHERE-style expression, not SQL.
    Lift the source rule's predicate so the route is not silently 'true'."""
    m = re.search(r"\bWHERE\b(.+)$", sql or "", re.I | re.S)
    return m.group(1).strip() if m else "true"


# ---------------------------------------------------------------------------
# streaming engines
# ---------------------------------------------------------------------------

def generate_flink(cer: CER, rep: Optional[GenReport] = None) -> str:
    rep = _rep(rep, "flink")
    _declare_unemittable(cer, rep, "Flink SQL", security=True)
    lines = ["-- Flink SQL — generated by MetaBridge AI from %s"
             % cer.source_platform]
    schema_of = {s.name: s for s in cer.schemas}
    for ch in cer.channels:
        sch = schema_of.get(ch.schema) or schema_of.get(ch.name)
        cols = ["  `%s` %s" % (f["name"],
                               {"string": "STRING", "double": "DOUBLE",
                                "long": "BIGINT", "int": "INT",
                                "boolean": "BOOLEAN"}.get(
                                    str(f.get("type", "string")).lower(),
                                    "STRING"))
                for f in (sch.fields if sch else [])] or \
            ["  `payload` STRING"]
        wm = ""
        if any(f.get("name") == "event_ts"
               for f in (sch.fields if sch else [])):
            cols.append("  `ts` AS TO_TIMESTAMP_LTZ(event_ts, 3)")
            wm = ",\n  WATERMARK FOR `ts` AS `ts` - INTERVAL '30' SECOND"
        lines.append(
            "CREATE TABLE %s (\n%s%s\n) WITH (\n"
            "  'connector' = 'kafka',\n  'topic' = '%s',\n"
            "  'properties.group.id' = 'metabridge',\n"
            "  'format' = '%s'\n);"
            % (_ident(ch.name), ",\n".join(cols), wm, ch.name,
               (sch.format if sch else "json")))
    for t in cer.transformations:
        if not t.inputs:
            rep.skip("transformation", t.name,
                     "no input channel resolved in the import, so no "
                     "Flink source table can be bound to it")
            continue
        if t.window is None:
            # This branch did not exist: a job without a window was simply
            # skipped, so condition-triggered rules vanished from the
            # output while the run still reported full automation.
            body = t.sql or t.raw
            if not body:
                rep.skip("transformation", t.name,
                         "no SQL or definition body was extractable from "
                         "the source, only its name")
                continue
            lines.append(
                "-- unwindowed job %s (from %s) — continuous, no "
                "aggregation window\nCREATE VIEW %s AS\n%s;"
                % (t.name, t.engine or cer.source_platform,
                   _ident(t.name), body.rstrip().rstrip(";")))
            if t.output:
                lines.append("INSERT INTO %s SELECT * FROM %s;"
                             % (_ident(t.output), _ident(t.name)))
            unresolved = [i for i in t.inputs
                          if not any(c.name == i for c in cer.channels)]
            if unresolved:
                # The warning existed only in the validation JSON before —
                # anyone reading just the SQL saw nothing wrong.
                lines.append("-- MANUAL: input(s) %s are not in the import, "
                             "so the view above will not resolve until the "
                             "matching source table is declared"
                             % ", ".join(unresolved))
            continue
        w = t.window
        src = _ident(t.inputs[0])
        tvf = {"tumbling": "TUMBLE(TABLE %s, DESCRIPTOR(`ts`), "
               "INTERVAL '%d' SECOND)" % (src, w.size_ms // 1000),
               "hopping": "HOP(TABLE %s, DESCRIPTOR(`ts`), INTERVAL "
               "'%d' SECOND, INTERVAL '%d' SECOND)"
               % (src, max(1, w.slide_ms // 1000), w.size_ms // 1000),
               "session": "SESSION(TABLE %s, DESCRIPTOR(`ts`), "
               "INTERVAL '%d' SECOND)"
               % (src, max(w.gap_ms, w.size_ms) // 1000)}.get(w.kind)
        if not tvf:
            rep.skip("transformation", t.name,
                     "window kind '%s' has no Flink windowing table "
                     "function" % w.kind)
            continue
        grp = ", ".join(["window_start", "window_end"] + t.group_by)
        # `SELECT *` alongside GROUP BY is not valid SQL. With no grouping
        # keys the only legal projection is the window bounds themselves.
        aggs = ", ".join(["window_start", "window_end"] + t.group_by)
        lines.append(
            "-- windowed job %s (from %s)\n"
            "INSERT INTO %s\nSELECT %s, COUNT(*) AS event_count\n"
            "FROM TABLE(%s)\nGROUP BY %s;"
            % (t.name, t.engine or cer.source_platform,
               _ident(t.output or t.name), aggs, tvf, grp))
        if not w.watermark_delay_ms and \
                w.time_semantics == "event_time":
            lines.append("-- MANUAL: source declared no watermark — "
                         "the 30s default above needs business "
                         "confirmation")
    lines += rep.lines("--")
    return "\n\n".join(lines) + "\n"


def _cdc_keys(cdc) -> List[str]:
    """Best available primary key for a CDC feed. Emitting a literal
    `primary_key` placeholder produced code that could never run; a real
    key is used when the source declared one."""
    for k in ("primary_key", "primary.key", "message.key.columns",
              "keys", "key.columns"):
        v = (cdc.properties or {}).get(k)
        if v:
            return [p.strip() for p in str(v).replace(";", ",").split(",")
                    if p.strip()]
    return []


def generate_spark(cer: CER, databricks: bool = False,
                   rep: Optional[GenReport] = None) -> str:
    rep = _rep(rep, "databricks_streaming" if databricks
               else "spark_streaming")
    _declare_unemittable(cer, rep, "Databricks" if databricks else "Spark",
                         security=True)
    lines = ['"""%s — generated by MetaBridge AI from %s."""'
             % ("Databricks streaming"
                if databricks else "Spark Structured Streaming",
                cer.source_platform),
             "from pyspark.sql import functions as F", ""]
    if databricks and cer.cdc_sources:
        lines += ["import dlt", ""]
        for cdc in cer.cdc_sources:
            for t_name in cdc.tables or [cdc.name]:
                tbl = _ident(t_name.split(".")[-1])
                lines += [
                    "@dlt.view(name='%s_changes')" % tbl,
                    "def %s_changes():" % tbl,
                    "    return (spark.readStream.format('kafka')",
                    "        .option('subscribe', '%s')"
                    % (cdc.output_channels[0] if cdc.output_channels
                       else tbl),
                    "        .load())", "",
                    "dlt.create_streaming_table('%s')" % tbl,
                    "dlt.apply_changes(target='%s', "
                    "source='%s_changes'," % (tbl, tbl)]
                keys = _cdc_keys(cdc)
                if keys:
                    lines.append("    keys=%r," % (keys,))
                else:
                    lines.append("    keys=[],  # MANUAL: the source "
                                 "declared no key columns — dedup is "
                                 "undefined until these are filled in")
                    rep.skip("cdc_key", cdc.name,
                             "no primary-key columns were declared in the "
                             "source, so apply_changes cannot deduplicate "
                             "— supply keys before running")
                lines += [
                    "    sequence_by='ts',",
                    "    stored_as_scd_type=1)",
                    "# CDC ordering preserved per key; snapshot mode "
                    "was '%s'" % (cdc.snapshot_mode or "unset"), ""]
    for t in cer.transformations:
        if not t.inputs:
            rep.skip("transformation", t.name,
                     "no input channel resolved in the import, so no "
                     "readStream source can be bound to it")
            continue
        src = t.inputs[0]
        var = _ident(t.name)
        lines += ["%s_src = (spark.readStream.format('kafka')" % var,
                  "    .option('subscribe', '%s')" % src,
                  "    .option('startingOffsets', 'earliest')",
                  "    .load())"]
        if t.window is not None:
            w = t.window
            dur = "%d seconds" % (w.size_ms // 1000)
            wm = "%d seconds" % ((w.watermark_delay_ms or 30000) // 1000)
            win = "F.window(F.col('ts'), '%s')" % dur if \
                w.kind == "tumbling" else \
                "F.window(F.col('ts'), '%s', '%s')" % (
                    dur, "%d seconds" % (max(1, w.slide_ms // 1000))) \
                if w.kind == "hopping" else \
                "F.session_window(F.col('ts'), '%s')" % (
                    "%d seconds" % (max(w.gap_ms, w.size_ms) // 1000))
            if not w.watermark_delay_ms:
                lines.append("# MANUAL: watermark defaulted to 30s — "
                             "source declared none")
            lines += ["%s = (%s_src" % (var, var),
                      "    .withWatermark('ts', '%s')" % wm,
                      "    .groupBy(%s%s)" % (win, "".join(
                          ", F.col('%s')" % g for g in t.group_by)),
                      "    .agg(F.count('*').alias('event_count')))"]
        else:
            lines.append("%s = %s_src  # %s" % (var, var,
                                                (t.sql or t.raw)[:80]))
        ckpt = "/chk/%s" % var
        # was `.format('delta' if True else 'kafka')` — a dead conditional
        # that silently forced Delta even on plain open-source Spark
        fmt = "delta" if databricks else "parquet"
        if not databricks:
            lines.append("# sink format is parquet — plain Spark has no "
                         "Delta by default; switch to 'delta' if "
                         "delta-spark is installed")
        lines += ["(%s.writeStream" % var,
                  "    .option('checkpointLocation', '%s')" % ckpt,
                  "    .outputMode('update')",
                  "    .format('%s')" % fmt,
                  "    .toTable('%s'))" % _ident(t.output or t.name), ""]
    # Device twins reached no target before; a lakehouse can hold the
    # reported state as a table.
    for i in cer.iot_sources:
        if not i.device_twin:
            continue
        cols = ", ".join("'%s'" % f["name"] for f in i.telemetry_fields)
        lines += ["# device twin '%s' — reported state, %d field(s)"
                  % (i.name, len(i.telemetry_fields)),
                  "# columns: %s" % (cols or "none declared"),
                  "# twin state is a snapshot, not a stream — load it as a "
                  "dimension and join on device id", ""]
    if not cer.transformations and not cer.cdc_sources:
        lines.append("# no streaming transformations in the import — "
                     "channels land via autoloader/connect sinks")
    lines += ["# " + ln.lstrip("- ") for ln in rep.lines("--")]
    return "\n".join(lines) + "\n"


def generate_snowflake_streaming(cer: CER,
                                 rep: Optional[GenReport] = None) -> str:
    rep = _rep(rep, "snowflake_streaming")
    _declare_unemittable(cer, rep, "Snowflake", producers=True,
                         consumers=True, security=True)
    lines = ["-- Snowflake streaming — generated by MetaBridge AI",
             "-- NOTE Snowflake is a SINK: it has no publish/subscribe, so "
             "%s (or a broker replacement) must keep running to feed these "
             "tables." % (cer.source_platform or "the source")]
    channel_names = {c.name for c in cer.channels}
    for ch in cer.channels:
        tbl = _ident(ch.name)
        lines.append(
            "-- Snowpipe Streaming lands '%s' (client SDK or Kafka "
            "connector with snowflake.streaming.enable=true)\n"
            "CREATE TABLE IF NOT EXISTS raw_%s (record VARIANT, "
            "loaded_at TIMESTAMP_NTZ DEFAULT SYSDATE());"
            % (ch.name, tbl))
        # A view per channel named after the channel itself, so SQL lifted
        # from the source resolves. Without this, a dynamic table generated
        # from ksqlDB referenced a name that existed nowhere in the script.
        lines.append("CREATE OR REPLACE VIEW %s AS SELECT record, "
                     "loaded_at FROM raw_%s;  -- source-name alias"
                     % (tbl, tbl))
        if ch.is_iot:
            # Clustering keys on a VARIANT path must be cast to a concrete
            # type; the uncast form was rejected by Snowflake.
            lines.append("ALTER TABLE raw_%s CLUSTER BY "
                         "(TO_VARCHAR(record:deviceId), "
                         "TO_DATE(loaded_at));  -- time-series"
                         % tbl)
        if re.search(r"[+#]", ch.name):
            # sensors/+/temperature and sensors/x/temperature both flatten
            # to the same identifier — that used to happen in silence.
            rep.adjust(ch.name, "WILDCARD_FLATTENED",
                       "MQTT wildcard topic became table 'raw_%s'; any "
                       "other topic flattening to the same name would "
                       "collide" % tbl)
            lines.append("-- MANUAL: '%s' contains an MQTT wildcard; one "
                         "table now holds every matching topic" % ch.name)
    for t in cer.transformations:
        body = t.sql or ""
        unresolved = [i for i in t.inputs if i not in channel_names]
        if not body:
            rep.skip("transformation", t.name,
                     "no SQL body was extractable from the source (%s), so "
                     "no dynamic table can be built"
                     % (t.engine or t.kind or "unknown engine"))
            continue
        # Previously `if t.window is None: continue` — every rule-style job
        # (a condition, not a clock) was dropped from the output entirely.
        lag = max(60, t.window.size_ms // 1000) if t.window else 60
        lines.append(
            "CREATE OR REPLACE DYNAMIC TABLE %s\n  TARGET_LAG = '%d "
            "seconds'\n  WAREHOUSE = streaming_wh\nAS\n%s;"
            % (_ident(t.name), lag, body.rstrip().rstrip(";")))
        if t.window:
            lines.append("-- window semantics (%s %dms) approximated by "
                         "TARGET_LAG — Snowflake dynamic tables are "
                         "micro-batch, not event-time windows"
                         % (t.window.kind, t.window.size_ms))
        else:
            lines.append("-- source job '%s' was condition-triggered, not "
                         "windowed; a dynamic table re-evaluates on a "
                         "%ds lag instead of firing per event"
                         % (t.name, lag))
            rep.adjust(t.name, "TRIGGER_BECAME_POLL",
                       "condition-triggered rule became a %ds-lag dynamic "
                       "table — it no longer fires per event" % lag)
        if t.output:
            lines.append("-- source routed results to '%s'; Snowflake "
                         "cannot publish — read the dynamic table above, "
                         "or add an external function / task to notify"
                         % t.output)
            rep.skip("routing", t.output,
                     "Snowflake cannot publish events, so the fan-out from "
                     "'%s' to '%s' is not reproduced" % (t.name, t.output))
        if unresolved:
            lines.append("-- MANUAL: input(s) %s are not in the import, so "
                         "the statement above will not compile until the "
                         "matching table exists"
                         % ", ".join(unresolved))
    for cdc in cer.cdc_sources:
        keys = _cdc_keys(cdc) or ["<primary_key>"]
        lines.append("-- CDC '%s': land changes via Snowpipe Streaming, "
                     "then CREATE DYNAMIC TABLE ... AS SELECT * FROM "
                     "changes QUALIFY ROW_NUMBER() OVER (PARTITION BY %s "
                     "ORDER BY ts DESC) = 1"
                     % (cdc.name, ", ".join(keys)))
        if keys == ["<primary_key>"]:
            lines.append("-- MANUAL: no key columns declared in the source "
                         "— dedup above is a template, not runnable")
    lines += rep.lines("--")
    return "\n\n".join(lines) + "\n"


def generate_dbt_streaming(cer: CER,
                           rep: Optional[GenReport] = None) -> Dict[str, str]:
    rep = _rep(rep, "dbt_streaming")
    _declare_unemittable(cer, rep, "dbt", producers=True, consumers=True,
                         security=True)
    for t in cer.transformations:
        if not t.sql:
            rep.skip("transformation", t.name,
                     "dbt models are SQL; no SQL body was extractable from "
                     "this %s job" % (t.engine or t.kind or "source"))
    for ch in cer.channels:
        rep.skip("channel", ch.name,
                 "dbt transforms tables it is given; landing '%s' is the "
                 "job of ingestion tooling, not dbt" % ch.name)
    out: Dict[str, str] = {}
    notes = ["-- dbt streaming support depends on the adapter:",
             "--   Snowflake: dynamic tables (materialized='dynamic_"
             "table'); Databricks: streaming tables/materialized views",
             "-- Row-level windows are approximated by micro-batch "
             "refresh — event-time semantics are declared, not "
             "replicated"]
    for t in cer.transformations:
        if not t.sql:
            continue
        cfg = ("{{ config(materialized='dynamic_table', "
               "target_lag='%d seconds', "
               "snowflake_warehouse='streaming_wh') }}"
               % max(60, (t.window.size_ms // 1000)
                     if t.window else 60))
        out["models/streaming/%s.sql" % _ident(t.name)] = \
            "\n".join(notes) + "\n" + cfg + "\n\n" + t.sql + "\n"
    if not out:
        out["models/streaming/README.md"] = (
            "No SQL-bearing streaming jobs in the import — broker "
            "channels are landed by ingestion tooling, not dbt.\n")
    if rep.notes or rep.unemitted:
        out["_metabridge_generation_notes.md"] = _notes_markdown(cer, rep)
    return out


# ---------------------------------------------------------------------------
# scaffold + write-out
# ---------------------------------------------------------------------------

def generate_scaffold_yaml(cer: CER,
                           rep: Optional[GenReport] = None) -> str:
    """A pipeline scaffold — sources, models, sinks and quality gates.

    This used to be `yaml.safe_dump(cer.to_dict())`: the canonical model
    serialized verbatim, which is a CER export and not a scaffold at all.
    The full model is still included under `canonical:` so nothing is lost.
    """
    import yaml
    rep = _rep(rep, "scaffold")
    sources = [{"name": _ident(ch.name), "channel": ch.name,
                "kind": ch.kind,
                "partitions": ch.partitions,
                "ordering": ch.ordering, "delivery": ch.delivery,
                **({"schema": ch.schema} if ch.schema else {}),
                **({"dead_letter": ch.dead_letter} if ch.dead_letter
                   else {})}
               for ch in cer.channels]
    models = []
    for t in cer.transformations:
        m = {"name": _ident(t.name), "inputs": t.inputs,
             "engine": t.engine or "unknown",
             "materialization": "windowed_aggregate" if t.window
             else "continuous"}
        if t.sql:
            m["sql"] = t.sql
        else:
            m["sql"] = None
            m["todo"] = ("no SQL extractable from the source — implement "
                         "this model by hand")
        if t.output:
            m["output"] = t.output
        if t.window:
            m["window"] = t.window.to_dict()
        models.append(m)
    gates = [{"check": "row_count_parity", "against": "source",
              "note": "compare landed counts against the source broker "
                      "during parallel-run"}]
    if any(c.delivery == "exactly_once" for c in cer.channels):
        gates.append({"check": "duplicate_scan",
                      "note": "source guaranteed exactly-once; assert no "
                              "duplicates on the primary key"})
    if cer.cdc_sources:
        gates.append({"check": "cdc_ordering",
                      "note": "assert per-key ordering survived the move"})
    doc = {
        "version": 1,
        "pipeline": {"name": _ident(cer.name) or "event_estate",
                     "source_platform": cer.source_platform},
        "sources": sources,
        "models": models,
        "sinks": [{"name": _ident(t.output), "from": _ident(t.name)}
                  for t in cer.transformations if t.output],
        "quality_gates": gates,
        "canonical": cer.to_dict(),
    }
    if rep.notes or rep.unemitted:
        doc["generation_notes"] = rep.to_dict()
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def generate_events(cer: CER, target: str, out_dir: str) -> dict:
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files: Dict[str, str] = {}
    rep = GenReport(target=target)
    if target in ("kafka", "confluent"):
        files = generate_kafka(cer, target, rep)
    elif target == "pulsar":
        files = {"create_topics.sh": generate_pulsar(cer, rep)}
    elif target == "rabbitmq":
        files = {"definitions.json": generate_rabbitmq(cer, rep)}
    elif target in ("eventhubs", "servicebus"):
        files = {"%s_arm.json" % target: generate_azure(cer, target, rep)}
    elif target == "kinesis":
        files = {"kinesis_cfn.json": generate_kinesis(cer, rep)}
    elif target == "pubsub":
        files = {"pubsub.tf": generate_pubsub(cer, rep)}
    elif target in ("mqtt", "iothub"):
        files = generate_mqtt(cer, target, rep)
    elif target == "flink":
        files = {"streaming.sql": generate_flink(cer, rep)}
    elif target == "spark_streaming":
        files = {"streaming_job.py": generate_spark(cer, False, rep)}
    elif target == "databricks_streaming":
        files = {"dlt_pipeline.py": generate_spark(cer, True, rep)}
    elif target == "snowflake_streaming":
        files = {"snowflake_streaming.sql":
                 generate_snowflake_streaming(cer, rep)}
    elif target == "dbt_streaming":
        files = generate_dbt_streaming(cer, rep)
    elif target == "scaffold":
        files = {"event_estate.yml": generate_scaffold_yaml(cer, rep)}
    else:
        raise ValueError("Unknown event target: %s (one of %s)"
                         % (target, ", ".join(EVENT_TARGETS)))
    # Generators that build a single string embed the report as comments;
    # make sure EVERY target also ships it as a readable file, so "what did
    # this run change or leave out?" is answerable from the output alone.
    if not rep.is_empty() and "_metabridge_generation_notes.md" not in files:
        files["_metabridge_generation_notes.md"] = _notes_markdown(cer, rep)
    for name, content in files.items():
        p = out / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return {"target": target, "files": sorted(files),
            "report": rep.to_dict()}

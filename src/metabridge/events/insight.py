"""Enterprise Event Intelligence Layer (Command 8 extension).

Operates AFTER Event Parser -> CER -> Semantic Analysis. This module is
NOT a parser: it consumes the CER only, never modifies parser output,
and every number it produces is DETERMINISTIC — computed from the CER
or derived from an explicitly labelled planning assumption. Anything
that only exists at runtime (measured lag, skew, burst patterns,
offline devices) is declared ``runtime_only`` instead of invented.
MetaBridge AI may explain these findings elsewhere; it never produces
the metrics.

Sections (spec §1–§11):

    topology              producer/consumer topology, hierarchies,
                          fan-in/out, streaming DAG, DLQ/retry/replay
    partitions            counts, key quality, idle capacity, consumer
                          imbalance + partition/scaling recommendations
    schema_evolution      version diffs, breaking changes, nullable and
                          type changes, compatibility risk
    performance           throughput/latency/backpressure/queue-depth
                          estimates + cloud resource sizing
    quality               duplicates, ordering, loss, poison messages,
                          dead-letter loops, payload/serialization
    cdc                   snapshot/consistency/checkpoint analysis +
                          streaming/batch/hybrid recommendation
    iot                   cardinality, aggregation/sampling/compression/
                          retention recommendations
    security              authn/authz/encryption/permission coverage,
                          credential exposure
    cost                  assumption-labelled cost model + optimizations
    readiness             six scores, each with an explanation
    executive_report      markdown with diagrams, risk matrix, roadmap
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

from .cer import CER, Channel
from .graph import execution_graph, to_mermaid
from .validate import event_intelligence, validate_cer

# --- planning assumptions (every derived number references these) ----------
ASSUMPTIONS = {
    "events_per_partition_per_sec": 1000,
    "peak_burst_factor": 3.0,
    "avg_event_kb": 1.0,
    "storage_usd_per_gb_month": 0.10,
    "tiered_storage_usd_per_gb_month": 0.023,
    "streaming_usd_per_million_events": 0.11,
    "consumer_instance_usd_per_month": 73.0,
    "partitions_per_broker": 30,
    "broker_usd_per_month": 220.0,
    "note": "planning figures for comparison only — replace with "
            "measured rates and negotiated prices before budgeting",
}

_DLQ_NAME_RE = re.compile(r"(dlq|dead|dlx|backout|poison)", re.I)
_RETRY_NAME_RE = re.compile(r"retry", re.I)


# ===========================================================================
# §1 topology
# ===========================================================================

def analyze_topology(cer: CER) -> dict:
    producers_of: Dict[str, List[str]] = {}
    consumers_of: Dict[str, List[str]] = {}
    for p in cer.producers:
        for ch in p.channels:
            producers_of.setdefault(ch, []).append(p.name)
    for c in cer.consumers:
        for ch in c.channels:
            consumers_of.setdefault(ch, []).append(c.name)
    reads_of: Dict[str, List[str]] = {}
    for t in cer.transformations:
        for i in t.inputs:
            reads_of.setdefault(i, []).append(t.name)

    # hierarchy by name prefix (orders.v1 / orders.dlq -> "orders")
    hierarchy: Dict[str, List[str]] = {}
    for ch in cer.channels:
        prefix = re.split(r"[./]", ch.name)[0]
        hierarchy.setdefault(prefix, []).append(ch.name)

    fan_out = [{"channel": ch.name,
                "readers": len(consumers_of.get(ch.name, []))
                + len(reads_of.get(ch.name, []))}
               for ch in cer.channels
               if len(consumers_of.get(ch.name, []))
               + len(reads_of.get(ch.name, [])) > 1]
    fan_in = [{"transformation": t.name, "inputs": len(t.inputs)}
              for t in cer.transformations if len(t.inputs) > 1]

    dlqs = sorted({ch.dead_letter for ch in cer.channels
                   if ch.dead_letter}
                  | {c.retry.dead_letter for c in cer.consumers
                     if c.retry.dead_letter}
                  | {ch.name for ch in cer.channels
                     if _DLQ_NAME_RE.search(ch.name)})
    retry_queues = sorted(ch.name for ch in cer.channels
                          if _RETRY_NAME_RE.search(ch.name))

    # replay: log-based channel with retention and earliest-reset readers
    replayable = []
    for ch in cer.channels:
        if ch.kind in ("topic", "stream") and (
                ch.retention.time_ms or
                ch.retention.policy.startswith("compact")):
            readers = [c.name for c in cer.consumers
                       if ch.name in c.channels
                       and c.offset_reset == "earliest"]
            replayable.append({
                "channel": ch.name,
                "retention_ms": ch.retention.time_ms,
                "compacted": "compact" in ch.retention.policy,
                "replay_ready_consumers": readers})
    groups: Dict[str, List[str]] = {}
    for c in cer.consumers:
        if c.group:
            groups.setdefault(c.group, []).append(c.name)
    return {
        "producer_topology": producers_of,
        "consumer_topology": consumers_of,
        "streaming_readers": reads_of,
        "channel_hierarchy": {k: sorted(v)
                              for k, v in hierarchy.items()},
        "consumer_groups": groups,
        "fan_out": fan_out,
        "fan_in": fan_in,
        "dead_letter_queues": dlqs,
        "retry_queues": retry_queues,
        "replay_architecture": {
            "replayable_channels": replayable,
            "note": "queues are consume-once — replay requires the "
                    "log-based channels listed here"},
        "execution_graph": execution_graph(cer),
    }


# ===========================================================================
# §2 partitions
# ===========================================================================

def analyze_partitions(cer: CER) -> dict:
    per_channel = []
    recs: List[str] = []
    group_size: Dict[str, int] = {}
    for c in cer.consumers:
        if c.group:
            group_size[c.group] = group_size.get(c.group, 0) + 1
    for ch in cer.channels:
        if ch.kind == "queue":
            continue
        readers = [c for c in cer.consumers if ch.name in c.channels]
        group_consumers = max([group_size.get(c.group, 1)
                               for c in readers], default=0)
        keyed = bool(ch.key_fields)
        key_quality = ("keyed" if keyed and ch.key_fields !=
                       ["ordering_key"] else
                       "platform_key" if keyed else
                       "unkeyed_round_robin")
        idle = max(0, ch.partitions - group_consumers) \
            if group_consumers else 0
        skew_risk = "requires_runtime_metrics"
        if ch.ordering == "per_key" and not keyed:
            skew_risk = "undetectable_key_missing"
        # optimal partitions: enough for the consumer group and the
        # planning throughput, never below current unless idle-heavy
        optimal = max(group_consumers or 1,
                      min(ch.partitions, 64))
        if ch.ordering in ("fifo", "global"):
            optimal = 1
        per_channel.append({
            "channel": ch.name, "partitions": ch.partitions,
            "consumers_in_group": group_consumers,
            "idle_partitions": idle,
            "key_fields": ch.key_fields,
            "key_quality": key_quality,
            "skew": skew_risk,
            "hot_partitions": "requires_runtime_metrics",
            "optimal_partitions": optimal,
        })
        if idle >= 4:
            recs.append("'%s': %d of %d partitions have no consumer in "
                        "the group — scale the group to %d or shrink "
                        "the topic on the target"
                        % (ch.name, idle, ch.partitions, ch.partitions))
        if key_quality == "unkeyed_round_robin" and ch.partitions > 1 \
                and any(t.group_by for t in cer.transformations
                        if ch.name in t.inputs):
            recs.append("'%s' feeds keyed aggregations but has no "
                        "partition key — key by the group-by column(s) "
                        "to co-locate state" % ch.name)
    for g, n in group_size.items():
        chans = {ch for c in cer.consumers if c.group == g
                 for ch in c.channels}
        parts = sum(c.partitions for c in cer.channels
                    if c.name in chans)
        if parts and n > parts:
            recs.append("consumer group '%s': %d consumers over %d "
                        "partition(s) — %d idle; reduce the group or "
                        "add partitions" % (g, n, parts, n - parts))
    prefixes: Dict[str, int] = {}
    for ch in cer.channels:
        prefixes[re.split(r"[./]", ch.name)[0]] = \
            prefixes.get(re.split(r"[./]", ch.name)[0], 0) + 1
    for pfx, count in prefixes.items():
        if count >= 6:
            recs.append("topic family '%s.*' has %d channels — review "
                        "for consolidation into fewer topics with a "
                        "type field" % (pfx, count))
    return {"channels": per_channel, "recommendations": recs,
            "note": "skew and hot partitions are runtime metrics — "
                    "this analysis covers everything derivable from "
                    "static metadata"}


# ===========================================================================
# §3 schema evolution
# ===========================================================================

# Type changes an Avro reader resolves on its own (spec: "Schema
# Resolution"). Promoting inside this set is NOT a breaking change —
# flagging it would fail migrations that are actually safe. Anything
# outside it is breaking, because the reader cannot decode the old bytes.
_AVRO_PROMOTIONS = frozenset({
    ("int", "long"), ("int", "float"), ("int", "double"),
    ("long", "float"), ("long", "double"), ("float", "double"),
    ("string", "bytes"), ("bytes", "string"),
})


def _field_map(schema) -> Dict[str, dict]:
    out = {}
    for f in schema.fields:
        t = f.get("type", "")
        nullable = "null" in str(t).lower()
        out[f["name"]] = {"type": str(t), "nullable": nullable}
    return out


def analyze_schema_evolution(cer: CER) -> dict:
    by_name: Dict[str, list] = {}
    for s in cer.schemas:
        by_name.setdefault(s.name, []).append(s)
    subjects = []
    risks: List[str] = []
    for name, versions in by_name.items():
        versions = sorted(versions, key=lambda s: s.version)
        changes = []
        for old, new in zip(versions, versions[1:]):
            fo, fn = _field_map(old), _field_map(new)
            added = sorted(set(fn) - set(fo))
            removed = sorted(set(fo) - set(fn))
            type_changes = sorted(
                f for f in set(fo) & set(fn)
                if fo[f]["type"] != fn[f]["type"])
            nullable_changes = sorted(
                f for f in set(fo) & set(fn)
                if fo[f]["nullable"] != fn[f]["nullable"])
            breaking = []
            compat = (new.compatibility or old.compatibility or
                      "unset").upper()
            if removed and compat in ("BACKWARD", "FULL", "UNSET"):
                breaking += ["field '%s' removed" % f for f in removed]
            for f in type_changes:
                if (fo[f]["type"], fn[f]["type"]) in _AVRO_PROMOTIONS:
                    continue          # reader resolves this itself
                breaking.append("field '%s' type %s -> %s"
                                % (f, fo[f]["type"], fn[f]["type"]))
            for f in nullable_changes:
                if fo[f]["nullable"] and not fn[f]["nullable"]:
                    breaking.append("field '%s' became non-nullable"
                                    % f)
            changes.append({
                "from_version": old.version, "to_version": new.version,
                "added": added, "removed": removed,
                "type_changes": type_changes,
                "nullable_changes": nullable_changes,
                "breaking_changes": breaking,
                "compatibility_mode": compat})
            if breaking:
                risks.append("schema '%s' v%d->v%d has breaking "
                             "change(s): %s"
                             % (name, old.version, new.version,
                                "; ".join(breaking)))
        subjects.append({
            "schema": name, "format": versions[-1].format,
            "versions": [v.version for v in versions],
            "compatibility": versions[-1].compatibility or "unset",
            "backward_safe": not any(c["breaking_changes"]
                                     for c in changes),
            "changes": changes})
        if not versions[-1].compatibility:
            risks.append("schema '%s' has no compatibility mode — pin "
                         "BACKWARD before migrating consumers" % name)
    recs = []
    if risks:
        recs.append("Migrate consumers before producers for BACKWARD "
                    "subjects; introduce new subjects (name.v2) for "
                    "breaking type changes instead of mutating in "
                    "place.")
    if any(s["compatibility"] == "unset" for s in subjects):
        recs.append("Pin explicit compatibility on every subject; "
                    "'unset' inherits registry defaults that differ "
                    "between environments.")
    return {"subjects": subjects, "risks": risks,
            "recommendations": recs,
            "risk_level": "HIGH" if any(
                "breaking" in r for r in risks) else
            "MEDIUM" if risks else "LOW"}


# ===========================================================================
# §4 performance + resources
# ===========================================================================

def analyze_performance(cer: CER) -> dict:
    a = ASSUMPTIONS
    partitions = sum(c.partitions for c in cer.channels) or 1
    sustained = partitions * a["events_per_partition_per_sec"]
    peak = int(sustained * a["peak_burst_factor"])
    windows = [t.window for t in cer.transformations
               if t.window is not None]
    latency_floor = max([w.size_ms for w in windows], default=0)
    backpressure = []
    for t in cer.transformations:
        if len(t.inputs) > 1:
            backpressure.append("'%s' joins %d inputs — the slowest "
                                "input gates the window state"
                                % (t.name, len(t.inputs)))
    for ch in cer.channels:
        readers = sum(1 for c in cer.consumers
                      if ch.name in c.channels)
        if ch.partitions >= 8 and readers == 1:
            backpressure.append("'%s': one consumer drains %d "
                                "partitions — lag accumulates under "
                                "load" % (ch.name, ch.partitions))
    queue_depths = [{"queue": ch.name,
                     "max_depth": int(ch.properties.get("MAXDEPTH", 0))}
                    for ch in cer.channels
                    if ch.properties.get("MAXDEPTH")]
    retry_cfg = [c for c in cer.consumers if c.retry.max_attempts]
    brokers = max(1, -(-partitions // a["partitions_per_broker"]))
    return {
        "throughput": {
            "sustained_events_per_sec": sustained,
            "peak_events_per_sec": peak,
            "basis": "%d partitions x %d events/s planning figure, "
                     "burst x%.1f" % (partitions,
                                      a["events_per_partition_per_sec"],
                                      a["peak_burst_factor"])},
        "latency": {
            "floor_ms": latency_floor,
            "basis": "largest window size" if latency_floor
            else "transport-bound (no windowed jobs)"},
        "consumer_lag": "runtime_only — measure during parallel run",
        "backpressure_points": backpressure,
        "queue_depths": queue_depths,
        "retry_volume": {
            "configured_retriers": len(retry_cfg),
            "worst_case_amplification": max(
                [c.retry.max_attempts + 1 for c in retry_cfg],
                default=1),
            "note": "actual retry volume is runtime-only"},
        "dead_letter_volume": "runtime_only",
        "cloud_resources": {
            "estimated_brokers_or_units": brokers,
            "kinesis_shards_equivalent": partitions,
            "eventhub_throughput_units_equivalent": max(
                1, sustained // 1000),
            "consumer_instances": max(1, len(cer.consumers)),
            "basis": "%d partitions / %d per broker"
                     % (partitions, a["partitions_per_broker"])},
        "assumptions": a,
    }


# ===========================================================================
# §5 event quality
# ===========================================================================

def analyze_quality(cer: CER) -> dict:
    findings = []

    def f(kind, obj, detail, fix):
        findings.append({"kind": kind, "object": obj, "detail": detail,
                         "remediation": fix})

    for p in cer.producers:
        if p.acks in ("0", "1") and not p.idempotent:
            f("duplicate_or_loss", p.name,
              "acks=%s without idempotence — broker failover can "
              "duplicate or drop" % p.acks,
              "enable idempotence and acks=all on the target")
    for ch in cer.channels:
        if ch.delivery == "at_least_once":
            readers = [c for c in cer.consumers
                       if ch.name in c.channels]
            if readers and not any(c.manual_commit for c in readers):
                f("duplicate_events", ch.name,
                  "at-least-once with auto-commit consumers — "
                  "reprocessing after crash duplicates side effects",
                  "commit offsets after processing, or make the sink "
                  "idempotent (MERGE on a business key)")
        if ch.ordering == "none" and ch.partitions > 1 and any(
                t.window is not None for t in cer.transformations
                if ch.name in t.inputs):
            f("out_of_order", ch.name,
              "unordered multi-partition input feeds a windowed job",
              "key the topic by the window entity and rely on "
              "event-time + watermark")
        if ch.delivery == "at_most_once" or (
                ch.replication == 1 and ch.kind in ("topic", "stream")):
            f("missing_events", ch.name,
              "at-most-once delivery or replication factor 1",
              "raise replication to 3 and acks=all on the target")
        maxb = int(ch.properties.get("max.message.bytes", 0) or 0)
        if maxb > 1048576:
            f("large_payloads", ch.name,
              "max.message.bytes=%d (>1MiB)" % maxb,
              "move payloads to object storage and send references "
              "(claim-check pattern)")
    for c in cer.consumers:
        if c.retry.max_attempts and not c.retry.dead_letter:
            f("poison_messages", c.name,
              "retries %d times with no dead-letter destination — a "
              "poison message blocks the partition"
              % c.retry.max_attempts,
              "route exhausted retries to a DLQ with an alert")
    # dead-letter loops: follow dead_letter references
    dl = {ch.name: ch.dead_letter for ch in cer.channels
          if ch.dead_letter}
    for start in dl:
        seen, cur = set(), start
        while cur in dl and cur not in seen:
            seen.add(cur)
            cur = dl[cur]
        if cur in seen:
            f("dead_letter_loop", start,
              "dead-letter chain cycles back through '%s'" % cur,
              "terminate the chain in a parking-lot queue with manual "
              "review")
    # current_schemas(): the format to compare a converter against is the
    # one in force now, not whichever version sorted last in the history
    fmts = {s.name: s.format for s in cer.current_schemas()}
    for ch in cer.channels:
        if ch.schema and ch.schema in fmts and \
                ch.properties.get("value.converter", ""):
            conv = ch.properties["value.converter"].lower()
            if fmts[ch.schema] not in conv:
                f("serialization_mismatch", ch.name,
                  "schema is %s but converter is %s"
                  % (fmts[ch.schema], conv),
                  "align converter and registry format")
    return {"findings": findings,
            # sorted: a set comprehension here made key order vary with
            # the process hash seed, so identical input serialized to
            # different bytes run to run
            "counts": {k: sum(1 for x in findings if x["kind"] == k)
                       for k in sorted({x["kind"] for x in findings})}}


# ===========================================================================
# §6 CDC / §7 IoT
# ===========================================================================

def analyze_cdc(cer: CER) -> dict:
    out = []
    for cdc in cer.cdc_sources:
        n_tables = len(cdc.tables)
        snapshot = cdc.snapshot_mode or "unset"
        consistent = all(
            (cer.channel(ch) is None or
             cer.channel(ch).ordering in ("per_key", "per_partition",
                                          "global", "fifo"))
            for ch in cdc.output_channels)
        mode = ("streaming" if n_tables <= 25 and snapshot in
                ("initial", "never", "when_needed") else
                "batch" if snapshot == "initial_only" else "hybrid")
        out.append({
            "source": cdc.name, "flavor": cdc.flavor,
            "tables": n_tables,
            "snapshot_mode": snapshot,
            "snapshot_duration": "scales with table sizes — "
                                 "runtime_only; schedule the initial "
                                 "snapshot off-peak",
            "log_growth": "runtime_only — monitor source log/redo "
                          "retention during backfill",
            "transaction_consistency": "preserved (per-key ordering on "
            "all output channels)" if consistent else
            "AT RISK — an output channel lacks ordering guarantees",
            "checkpoint_quality": "offset-based (connector-managed)"
            if "debezium" in cdc.flavor else "trail/checkpoint files"
            if "goldengate" in cdc.flavor else "declared by platform",
            "recommended_mode": mode,
            "recommendation": {
                "streaming": "continuous log-based capture into the "
                             "stream, snapshot once",
                "batch": "periodic full/initial loads — no tailing "
                         "needed for this configuration",
                "hybrid": "batch the initial snapshot per table group, "
                          "then cut over to streaming tail",
            }[mode]})
    return {"sources": out,
            "note": "latency/lag/log-growth are runtime metrics — "
                    "static analysis covers configuration risk only"}


def analyze_iot(cer: CER) -> dict:
    if not cer.iot_sources:
        return {"sources": [], "recommendations": []}
    out, recs = [], []
    for i in cer.iot_sources:
        wildcard = any("+" in t or "#" in t for t in i.topics)
        out.append({
            "source": i.name, "protocol": i.protocol,
            "topics": i.topics, "qos": i.qos,
            "telemetry_fields": len(i.telemetry_fields),
            "sensor_cardinality": "unbounded (wildcard topics)"
            if wildcard else str(len(i.topics)),
            "device_throughput": "runtime_only",
            "burst_patterns": "runtime_only",
            "offline_devices": "runtime_only (device registry query)"})
        if wildcard:
            recs.append("'%s' subscribes wildcard topics — aggregate "
                        "at the edge (per-device rollup) before the "
                        "hub to bound cardinality" % i.name)
        if i.qos == 0:
            recs.append("'%s' uses QoS 0 — telemetry loss is accepted; "
                        "confirm or raise to QoS 1" % i.name)
    recs += [
        "sample high-frequency sensors (e.g. 1-in-N or on-change) "
        "before cloud ingestion",
        "enable payload compression (MQTT payloads compress 5-10x for "
        "JSON telemetry)",
        "tier retention: hot 7d in the stream, warm in the lakehouse "
        "clustered by (device_id, event_ts)"]
    return {"sources": out, "recommendations": recs}


# ===========================================================================
# §8 security
# ===========================================================================

def analyze_security(cer: CER) -> dict:
    findings, recs = [], []
    covered = set()
    for s in cer.security:
        covered.add(s.resource)
    if not cer.security:
        findings.append({"kind": "authorization", "object": "*",
                         "detail": "no ACLs/permissions in the import"})
        recs.append("Export and carry broker ACLs — every channel "
                    "should have an explicit principal list on the "
                    "target.")
    else:
        uncovered = [ch.name for ch in cer.channels
                     if not any(r and (r in ch.name or ch.name in r or
                                       r in ("/", ".*", "*"))
                                for r in covered)]
        if uncovered:
            findings.append({"kind": "authorization",
                             "object": ", ".join(uncovered[:8]),
                             "detail": "%d channel(s) match no "
                             "permission rule" % len(uncovered)})
    for i in cer.issues:
        if i.get("code") == "MQTT_ANONYMOUS":
            findings.append({"kind": "authentication", "object": "mqtt",
                             "detail": i.get("message", "")})
            recs.append("Disable anonymous MQTT; map devices to "
                        "per-device credentials or X.509 on the "
                        "target hub.")
    exposure = []
    secret_re = re.compile(r"(password|secret|sasl\.jaas)", re.I)
    for coll, kind in ((cer.producers, "producer"),
                       (cer.consumers, "consumer")):
        for obj in coll:
            for k, v in obj.properties.items():
                if secret_re.search(k) and v:
                    exposure.append("%s '%s' carries credential "
                                    "material in config key '%s'"
                                    % (kind, obj.name, k))
    for ch in cer.channels:
        for k, v in ch.properties.items():
            if secret_re.search(k) and v:
                exposure.append("channel '%s' config key '%s'"
                                % (ch.name, k))
    if exposure:
        findings.append({"kind": "credential_exposure",
                         "object": "configs",
                         "detail": "; ".join(exposure[:5])})
        recs.append("Move credentials to a secret manager reference — "
                    "never migrate configs containing raw secrets.")
    tls = any("ssl" in json.dumps(c).lower() or
              "tls" in json.dumps(c).lower() for c in cer.connections)
    if not tls:
        findings.append({"kind": "encryption", "object": "transport",
                         "detail": "no TLS/SSL markers in the imported "
                         "connection configs — encryption in transit "
                         "unverified"})
        recs.append("Enforce TLS on the target listeners; imported "
                    "metadata shows no transport-encryption settings.")
    return {"findings": findings, "recommendations": recs,
            "policies_imported": len(cer.security)}


# ===========================================================================
# §9 cost
# ===========================================================================

def analyze_cost(cer: CER) -> dict:
    a = ASSUMPTIONS
    partitions = sum(c.partitions for c in cer.channels) or 1
    sustained = partitions * a["events_per_partition_per_sec"]
    events_month = sustained * 3600 * 24 * 30
    gb_month = events_month * a["avg_event_kb"] / 1024 / 1024
    retained_gb = 0.0
    for ch in cer.channels:
        days = (ch.retention.time_ms or 7 * 86400000) / 86400000
        retained_gb += (ch.partitions *
                        a["events_per_partition_per_sec"] * 86400 *
                        days * a["avg_event_kb"] / 1024 / 1024)
    brokers = max(1, -(-partitions // a["partitions_per_broker"]))
    storage = round(retained_gb * a["storage_usd_per_gb_month"], 2)
    tiered = round(retained_gb * a["tiered_storage_usd_per_gb_month"],
                   2)
    streaming = round(events_month / 1e6 *
                      a["streaming_usd_per_million_events"], 2)
    compute = round(max(1, len(cer.consumers)) *
                    a["consumer_instance_usd_per_month"]
                    + brokers * a["broker_usd_per_month"], 2)
    optimizations = []
    if storage - tiered > 100:
        optimizations.append("tiered/object storage for retention "
                             "saves ~$%.0f/month at these assumptions"
                             % (storage - tiered))
    long_ret = [ch.name for ch in cer.channels
                if ch.retention.time_ms > 7 * 86400000]
    if long_ret:
        optimizations.append("channels %s retain >7 days — offload "
                             "history to the lakehouse"
                             % ", ".join(long_ret[:5]))
    idle_groups = sum(1 for ch in cer.channels
                      if ch.partitions >= 8 and
                      sum(1 for c in cer.consumers
                          if ch.name in c.channels) <= 1)
    if idle_groups:
        optimizations.append("%d over-partitioned channel(s) — "
                             "right-size partition counts to cut "
                             "broker units" % idle_groups)
    return {
        "monthly_estimates_usd": {
            "storage": storage,
            "storage_tiered_alternative": tiered,
            "streaming": streaming,
            "compute": compute,
            "total": round(storage + streaming + compute, 2)},
        "cross_region_traffic": "not derivable from the import — "
                                "depends on replication topology; "
                                "flag if consumers span regions",
        "consumer_scaling_cost_per_instance_usd":
            a["consumer_instance_usd_per_month"],
        "volume_basis": {
            "events_per_month": events_month,
            "ingest_gb_per_month": round(gb_month, 1),
            "retained_gb": round(retained_gb, 1)},
        "optimizations": optimizations,
        "assumptions": a,
    }


# ===========================================================================
# §10 readiness scores
# ===========================================================================

def readiness_scores(cer: CER, validation: dict, base: dict,
                     schema: dict, quality: dict,
                     security: dict) -> dict:
    warnings = sum(1 for f in validation["findings"]
                   if f["severity"] == "WARNING")
    errors = sum(1 for f in validation["findings"]
                 if f["severity"] == "ERROR")
    manual = len(base.get("manual_review_items", []))
    unknown_jobs = sum(1 for t in cer.transformations
                       if not t.sql and not t.raw)
    automation = base["automation_score"]
    readiness = max(0, 100 - 8 * errors - 3 * manual
                    - 5 * unknown_jobs
                    - (10 if schema["risk_level"] == "HIGH" else
                       5 if schema["risk_level"] == "MEDIUM" else 0)
                    - 2 * len(security["findings"]))
    complexity = base["streaming_complexity"]
    op_risk = min(100, 10 * errors + 4 * warnings
                  + 6 * len(quality["findings"]))
    biz_risk = min(100, 15 * sum(1 for c in cer.channels
                                 if c.delivery == "exactly_once")
                   + 10 * len(cer.cdc_sources)
                   + 5 * sum(1 for c in cer.channels
                             if c.ordering in ("fifo", "global")))
    # computed by event_intelligence(); recomputing it here let the two
    # definitions drift apart silently
    confidence = base["semantic_confidence"]

    def why(*parts):
        return " ".join(p for p in parts if p)

    return {
        "automation_score": {
            "value": automation,
            "explanation": "share of channels/jobs/CDC/IoT units with "
                           "no manual-review item attached"},
        "modernization_readiness": {
            "value": readiness,
            "explanation": why(
                "100 minus deductions:",
                "%d validation error(s)," % errors if errors else "",
                "%d manual item(s)," % manual if manual else "",
                "%d opaque streaming job(s)," % unknown_jobs
                if unknown_jobs else "",
                "schema risk %s," % schema["risk_level"],
                "%d security finding(s)" % len(security["findings"])
                if security["findings"] else "no security findings")},
        "migration_complexity": {
            "value": complexity,
            "explanation": "channels + weighted streaming jobs, "
                           "windows, CDC feeds, routing rules and "
                           "manual items"},
        "operational_risk": {
            "value": op_risk,
            "explanation": "validation errors/warnings plus event-"
                           "quality findings (duplicates, ordering, "
                           "loss, poison messages)"},
        "business_risk": {
            "value": biz_risk,
            "explanation": "exactly-once channels, CDC feeds and "
                           "strict-ordering channels carry business "
                           "correctness obligations through the "
                           "migration"},
        "semantic_confidence": {
            "value": confidence,
            "explanation": "100 minus manual-review items and "
                           "validation warnings — how much of the "
                           "estate parsed into unambiguous semantics"},
    }


# ===========================================================================
# §11 executive report
# ===========================================================================

def executive_report(cer: CER, intel: dict) -> str:
    inv = cer.inventory()
    scores = intel["readiness"]
    cost = intel["cost"]["monthly_estimates_usd"]
    lines = [
        "# Event modernization intelligence — %s" % cer.name, "",
        "## Executive summary", "",
        "The imported %s estate contains %d channel(s), %d consumer(s),"
        " %d streaming job(s), %d CDC feed(s) and %d IoT source(s). "
        "Automation score **%s%%**, modernization readiness **%s/100**,"
        " migration complexity **%s** — see the score explanations "
        "below. Estimated run cost at planning assumptions: "
        "**$%s/month** (optimizable: %d recommendation(s))."
        % (cer.source_platform, inv["channels"], inv["consumers"],
           inv["streaming_jobs"], inv["cdc_sources"],
           inv["iot_sources"],
           scores["automation_score"]["value"],
           scores["modernization_readiness"]["value"],
           scores["migration_complexity"]["value"],
           cost["total"], len(intel["cost"]["optimizations"])), "",
        "## Scores", ""]
    for k, s in scores.items():
        lines.append("- **%s: %s** — %s"
                     % (k.replace("_", " "), s["value"],
                        s["explanation"]))
    lines += ["", "## Current topology", "", "```mermaid",
              to_mermaid(cer), "```", "",
              "## Recommended target architecture", ""]
    if cer.cdc_sources:
        lines.append("- CDC feeds: log-based capture into the stream, "
                     "APPLY CHANGES / dynamic tables in the lakehouse "
                     "(see CDC intelligence).")
    if cer.iot_sources:
        lines.append("- IoT telemetry: edge aggregation -> hub -> "
                     "time-series-clustered lakehouse landing.")
    if any(t.window for t in cer.transformations):
        lines.append("- Windowed jobs: event-time engine (Flink/Spark) "
                     "with explicit watermarks — see generated jobs.")
    lines.append("- Broker channels: right-sized partitions per the "
                 "partition intelligence; DLQs on every retrying "
                 "consumer.")
    lines += ["", "## Risk matrix", "",
              "| Area | Level | Evidence |", "|---|---|---|"]
    lines.append("| Schema evolution | %s | %d risk(s) |"
                 % (intel["schema_evolution"]["risk_level"],
                    len(intel["schema_evolution"]["risks"])))
    lines.append("| Event quality | %s | %d finding(s) |"
                 % ("HIGH" if len(intel["quality"]["findings"]) > 5
                    else "MEDIUM" if intel["quality"]["findings"]
                    else "LOW", len(intel["quality"]["findings"])))
    lines.append("| Security | %s | %d finding(s) |"
                 % ("MEDIUM" if intel["security"]["findings"]
                    else "LOW", len(intel["security"]["findings"])))
    lines.append("| Operations | %s/100 | validation + quality |"
                 % scores["operational_risk"]["value"])
    lines.append("| Business | %s/100 | delivery/ordering/CDC "
                 "obligations |" % scores["business_risk"]["value"])
    lines += ["", "## Cost comparison (planning assumptions)", "",
              "| Item | $/month |", "|---|---|",
              "| Storage (broker) | %s |" % cost["storage"],
              "| Storage (tiered alternative) | %s |"
              % cost["storage_tiered_alternative"],
              "| Streaming | %s |" % cost["streaming"],
              "| Compute | %s |" % cost["compute"],
              "| **Total** | **%s** |" % cost["total"], "",
              "_%s_" % ASSUMPTIONS["note"], "",
              "## Migration roadmap", ""]
    phases = [
        "Phase 1 — foundation: provision target channels from the "
        "generated definitions; wire schema registry with pinned "
        "compatibility.",
        "Phase 2 — mirrored traffic: dual-produce or mirror topics; "
        "validate counts and ordering with the validation pack.",
    ]
    if cer.cdc_sources:
        phases.append("Phase 3 — CDC cutover: snapshot per the CDC "
                      "intelligence mode, then streaming tail; "
                      "reconcile row counts.")
    if cer.transformations:
        phases.append("Phase %d — streaming jobs: deploy generated "
                      "windowed jobs; parallel-run against legacy "
                      "output." % (len(phases) + 1))
    phases.append("Phase %d — consumer cutover and legacy "
                  "decommission." % (len(phases) + 1))
    lines += ["1. " + p.split("— ", 1)[1] if False else "- " + p
              for p in phases]
    weeks = max(4, inv["channels"] // 4 + inv["streaming_jobs"] * 2
                + inv["cdc_sources"] * 2 + len(phases))
    lines += ["", "## Estimated timeline", "",
              "~%d engineering weeks at these volumes (deterministic "
              "heuristic: channels/4 + 2/streaming job + 2/CDC feed + "
              "cutover phases — refine after Phase 1)." % weeks, ""]
    return "\n".join(lines)


# ===========================================================================
# entry point
# ===========================================================================

def analyze_event_intelligence(cer: CER) -> dict:
    """The complete Event Intelligence Layer — pure function over CER."""
    validation = validate_cer(cer)
    base = event_intelligence(cer, validation)
    schema = analyze_schema_evolution(cer)
    quality = analyze_quality(cer)
    security = analyze_security(cer)
    intel = {
        "source_platform": cer.source_platform,
        "inventory": cer.inventory(),
        "topology": analyze_topology(cer),
        "partitions": analyze_partitions(cer),
        "schema_evolution": schema,
        "performance": analyze_performance(cer),
        "quality": quality,
        "cdc": analyze_cdc(cer),
        "iot": analyze_iot(cer),
        "security": security,
        "cost": analyze_cost(cer),
        "readiness": readiness_scores(cer, validation, base, schema,
                                      quality, security),
        "determinism_note": "every figure derives from the CER or a "
                            "labelled assumption; runtime-only facts "
                            "are declared, never invented",
    }
    intel["executive_report"] = executive_report(cer, intel)
    return intel

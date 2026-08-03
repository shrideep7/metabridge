"""CER validation (Command 8, §10) + modernization intelligence (§11).

Deterministic checks, target-aware where a target is given:

    ordering               keyed/FIFO ordering vs. target capability
    delivery guarantees    exactly-once downgrades declared, never silent
    schema compatibility   evolution mode present + parseable schemas
    consumer lag           static import — declared as runtime-only
    partition integrity    partition counts vs. target limits/semantics
    duplicate events       acks/idempotence gaps that permit duplicates
    message loss           acks=0/1, replication=1, at_most_once flags
    dead-letter routing    DLQ references that resolve
    window correctness     event-time windows need watermarks
    CDC consistency        snapshot mode + per-key ordering preserved
"""
from __future__ import annotations

from typing import Dict, List

from .cer import CER

VERDICTS = ("PASS", "PASS_WITH_WARNINGS", "MANUAL_REVIEW", "FAIL")

# what each target can express natively (used for declared downgrades)
TARGET_CAPS = {
    "kafka": {"exactly_once": True, "fifo": False, "dlq_native": False},
    "confluent": {"exactly_once": True, "fifo": False,
                  "dlq_native": False},
    "pulsar": {"exactly_once": True, "fifo": True, "dlq_native": True},
    "rabbitmq": {"exactly_once": False, "fifo": True, "dlq_native": True},
    "eventhubs": {"exactly_once": False, "fifo": False,
                  "dlq_native": False},
    "kinesis": {"exactly_once": False, "fifo": False,
                "dlq_native": False},
    "pubsub": {"exactly_once": True, "fifo": True, "dlq_native": True},
    "servicebus": {"exactly_once": False, "fifo": True,
                   "dlq_native": True},
    "mqtt": {"exactly_once": True, "fifo": False, "dlq_native": False},
    "iothub": {"exactly_once": False, "fifo": False, "dlq_native": False},
    "flink": {"exactly_once": True, "fifo": False, "dlq_native": False},
    "spark_streaming": {"exactly_once": True, "fifo": False,
                        "dlq_native": False},
    "databricks_streaming": {"exactly_once": True, "fifo": False,
                             "dlq_native": False},
    "snowflake_streaming": {"exactly_once": True, "fifo": False,
                            "dlq_native": False},
    "dbt_streaming": {"exactly_once": False, "fifo": False,
                      "dlq_native": False},
}

# The ROLE a target plays relative to the source estate. Only a "broker"
# can replace the source — a processor computes on top of it and a sink
# lands events out of it, so both leave the original broker in place.
# Describing a warehouse purely in broker capabilities (FIFO, native DLQ)
# is a category error: those are not downgrades, they are concepts that
# do not apply. The role is reported so the caller can say so plainly.
TARGET_KIND = {
    "kafka": "broker", "confluent": "broker", "pulsar": "broker",
    "rabbitmq": "broker", "eventhubs": "broker", "servicebus": "broker",
    "kinesis": "broker", "pubsub": "broker", "mqtt": "broker",
    "iothub": "broker",
    "flink": "processor", "spark_streaming": "processor",
    "databricks_streaming": "sink", "snowflake_streaming": "sink",
    "dbt_streaming": "sink",
    "scaffold": "spec",
}

_ROLE_NOTE = {
    "processor": "computes on top of the estate — it does not transport "
                 "events, so the source broker (or a replacement) is "
                 "still required",
    "sink": "lands events into tables — it has no publish/subscribe, so "
            "the source broker (or a replacement) is still required to "
            "feed it",
    "spec": "is a specification artifact, not a running platform",
}

# Hard platform ceilings. Generation silently clamps to these; declaring
# them here means the clamp is reported BEFORE the artifact is written.
TARGET_LIMITS = {
    "eventhubs": {"max_partitions": 32, "max_retention_days": 7},
    "kinesis": {"max_retention_hours": 8760},
    "pubsub": {"max_retention_days": 31},
}

# Platforms that can be imported but not generated to — modernization is
# one-way out of these, by design.
SOURCE_ONLY_PLATFORMS = ("ibmmq", "activemq", "nifi", "streamsets",
                         "goldengate", "debezium")


def _f(severity: str, code: str, message: str, obj: str = "",
       suggestion: str = "") -> dict:
    return {"severity": severity, "code": code, "message": message,
            "object": obj, "suggestion": suggestion}


def validate_cer(cer: CER, target: str = "") -> dict:
    findings: List[dict] = []
    caps = TARGET_CAPS.get(target, {})
    limits = TARGET_LIMITS.get(target, {})
    names = {c.name for c in cer.channels}

    # State the target's ROLE up front. Without this, a broker -> warehouse
    # run reads as a like-for-like migration when the source broker in fact
    # has to stay running to feed the target.
    role = TARGET_KIND.get(target, "")
    if role in _ROLE_NOTE:
        findings.append(_f(
            "WARNING", "TARGET_NOT_A_REPLACEMENT",
            "Target %s %s — this is an addition to the estate, not a "
            "replacement for %s" % (target, _ROLE_NOTE[role],
                                    cer.source_platform or "the source"),
            target,
            "Plan to keep the source broker, or pick a broker target if "
            "you intend to decommission it."))

    for ch in cer.channels:
        # delivery guarantees
        if ch.delivery == "exactly_once" and caps and \
                not caps.get("exactly_once"):
            findings.append(_f(
                "WARNING", "DELIVERY_DOWNGRADE",
                "Channel '%s' requires exactly-once but target %s "
                "provides at-least-once — deduplicate on the consumer "
                "side" % (ch.name, target), ch.name,
                "Add an idempotency key / MERGE-based sink."))
        if ch.delivery == "at_most_once":
            findings.append(_f(
                "WARNING", "MESSAGE_LOSS_RISK",
                "Channel '%s' is at-most-once — message loss is "
                "accepted by design; confirm with the business owner"
                % ch.name, ch.name))
        # ordering
        if ch.ordering in ("global", "fifo") and caps and \
                not caps.get("fifo"):
            findings.append(_f(
                "WARNING", "ORDERING_DOWNGRADE",
                "Channel '%s' guarantees %s ordering; target %s orders "
                "per partition only — use a single partition or a key"
                % (ch.name, ch.ordering, target), ch.name,
                "Route through one partition (throughput cost) or key "
                "by the ordering attribute."))
        if ch.ordering == "per_key" and not ch.key_fields:
            findings.append(_f(
                "WARNING", "ORDERING_KEY_MISSING",
                "Channel '%s' claims per-key ordering but no key fields "
                "are defined" % ch.name, ch.name))
        # partition integrity
        if ch.partitions < 1:
            findings.append(_f("ERROR", "PARTITION_INVALID",
                               "Channel '%s' has %d partitions"
                               % (ch.name, ch.partitions), ch.name))
        # partition / retention ceilings — the generator clamps to these,
        # so they are reported here rather than discovered in the artifact
        max_p = limits.get("max_partitions", 0)
        if max_p and ch.partitions > max_p:
            findings.append(_f(
                "WARNING", "PARTITION_LIMIT_EXCEEDED",
                "Channel '%s' has %d partitions but target %s allows %d "
                "— parallelism will be reduced on migration"
                % (ch.name, ch.partitions, target, max_p), ch.name,
                "Re-plan consumer parallelism for %d partitions, or "
                "split the channel." % max_p))
        max_days = limits.get("max_retention_days", 0)
        max_hours = limits.get("max_retention_hours", 0)
        cap_ms = (max_days * 86400000) or (max_hours * 3600000)
        if cap_ms and ch.retention.time_ms > cap_ms:
            findings.append(_f(
                "WARNING", "RETENTION_LIMIT_EXCEEDED",
                "Channel '%s' retains %d ms but target %s caps at %d ms "
                "— older events will not be replayable"
                % (ch.name, ch.retention.time_ms, target, cap_ms),
                ch.name,
                "Offload history to object storage before cutover."))
        # replication / loss
        if ch.replication == 1 and ch.kind in ("topic", "stream"):
            findings.append(_f(
                "WARNING", "REPLICATION_SINGLE",
                "Channel '%s' has replication factor 1 — broker loss "
                "loses data" % ch.name, ch.name))
        # dead-letter routing resolves
        if ch.dead_letter and ch.dead_letter not in names:
            findings.append(_f(
                "ERROR", "DLQ_UNRESOLVED",
                "Channel '%s' routes dead letters to '%s' which is not "
                "in the import" % (ch.name, ch.dead_letter), ch.name))
        # schema
        if ch.schema and not any(s.name == ch.schema
                                 for s in cer.schemas):
            findings.append(_f(
                "WARNING", "SCHEMA_MISSING",
                "Channel '%s' references schema '%s' not present in "
                "the import" % (ch.name, ch.schema), ch.name))

    for s in cer.schemas:
        if not s.compatibility:
            findings.append(_f(
                "INFO", "SCHEMA_EVOLUTION_UNSET",
                "Schema '%s' has no compatibility mode — evolution "
                "behaviour is registry default" % s.name, s.name,
                "Pin BACKWARD (or FULL) before migrating consumers."))

    for p in cer.producers:
        if p.acks in ("0", "1") and not p.idempotent:
            findings.append(_f(
                "WARNING", "DUPLICATE_OR_LOSS_RISK",
                "Producer '%s' uses acks=%s without idempotence — "
                "retries can duplicate or drop events"
                % (p.name, p.acks), p.name,
                "Enable idempotence / acks=all in the target."))

    for c in cer.consumers:
        if c.retry.max_attempts and not c.retry.dead_letter:
            findings.append(_f(
                "WARNING", "RETRY_WITHOUT_DLQ",
                "Consumer '%s' retries %d times with no dead-letter "
                "destination — poison messages block the partition"
                % (c.name, c.retry.max_attempts), c.name))
        for ch in c.channels:
            if ch not in names:
                findings.append(_f(
                    "ERROR", "CONSUMER_CHANNEL_UNRESOLVED",
                    "Consumer '%s' reads '%s' which is not in the "
                    "import" % (c.name, ch), c.name))

    for t in cer.transformations:
        if t.window is not None:
            if t.window.time_semantics == "event_time" and \
                    not t.window.watermark_delay_ms:
                findings.append(_f(
                    "WARNING", "WATERMARK_MISSING",
                    "Windowed job '%s' uses event time without a "
                    "watermark — late events are undefined" % t.name,
                    t.name, "Declare an allowed lateness / watermark "
                            "delay before porting."))
            if t.window.kind == "hopping" and \
                    t.window.slide_ms > t.window.size_ms:
                findings.append(_f(
                    "WARNING", "WINDOW_GAPS",
                    "Hopping window in '%s' advances (%dms) beyond its "
                    "size (%dms) — events are skipped"
                    % (t.name, t.window.slide_ms, t.window.size_ms),
                    t.name))
        for i in t.inputs:
            if i not in names:
                findings.append(_f(
                    "WARNING", "TRANSFORM_INPUT_UNRESOLVED",
                    "Streaming job '%s' reads '%s' which is not in the "
                    "import" % (t.name, i), t.name))

    for cdc in cer.cdc_sources:
        if not cdc.snapshot_mode:
            findings.append(_f(
                "WARNING", "CDC_SNAPSHOT_UNSET",
                "CDC source '%s' has no snapshot mode — initial-load "
                "consistency is undefined" % cdc.name, cdc.name,
                "Choose initial/initial_only/never explicitly before "
                "cutover."))
        for ch_name in cdc.output_channels:
            ch = cer.channel(ch_name)
            if ch is not None and ch.ordering not in ("per_key",
                                                      "per_partition",
                                                      "global", "fifo"):
                findings.append(_f(
                    "ERROR", "CDC_ORDERING_BROKEN",
                    "CDC channel '%s' has no ordering guarantee — "
                    "out-of-order changes corrupt the replica"
                    % ch_name, ch_name))

    findings.append(_f(
        "INFO", "CONSUMER_LAG_RUNTIME_ONLY",
        "Consumer lag cannot be validated from a static import — "
        "measure it live during parallel-run."))

    sev = {f["severity"] for f in findings}
    verdict = ("FAIL" if "ERROR" in sev else
               "MANUAL_REVIEW" if any(
                   i.get("severity") == "MANUAL" for i in cer.issues)
               else "PASS_WITH_WARNINGS" if "WARNING" in sev else "PASS")
    return {"verdict": verdict, "target": target, "findings": findings,
            "checks_run": ["ordering", "delivery_guarantees",
                           "schema_compatibility", "consumer_lag",
                           "partition_integrity", "duplicate_events",
                           "message_loss", "dead_letter_routing",
                           "window_correctness", "cdc_consistency"]}


# ---------------------------------------------------------------------------
# modernization intelligence (§11)
# ---------------------------------------------------------------------------

_AUTOMATABLE_ENGINES = {"ksqldb", "flink", "spark", "", "rule",
                        "debezium"}


def event_intelligence(cer: CER, validation: dict,
                       generation: dict = None) -> dict:
    """`generation` is the GenReport dict returned by generate_events().

    Without it the scores describe the IMPORT only (how much of the source
    was understood). With it they describe the OUTPUT — anything a target
    could not express is subtracted, so a run that dropped content can no
    longer report 100%."""
    inv = cer.inventory()
    units = (inv["channels"] + inv["streaming_jobs"] +
             inv["cdc_sources"] + inv["iot_sources"]) or 1
    manual = [i for i in cer.issues if i.get("severity") == "MANUAL"]
    auto_units = units - len({m.get("obj") for m in manual if m.get("obj")})
    complexity = min(100, inv["channels"] + 3 * inv["streaming_jobs"]
                     + 4 * inv["windows"] + 3 * inv["cdc_sources"]
                     + 2 * inv["routing_rules"] + 5 * len(manual))
    partitions = sum(c.partitions for c in cer.channels) or 1
    # deterministic, assumption-labelled estimates — not promises
    throughput = {"assumed_events_per_partition_per_sec": 1000,
                  "estimated_capacity_events_per_sec": partitions * 1000,
                  "basis": "1k events/s per partition planning figure — "
                           "replace with measured produce rates"}
    windows = [t.window for t in cer.transformations
               if t.window is not None]
    latency_floor = max([w.size_ms for w in windows], default=0)
    latency = {"floor_ms": latency_floor,
               "basis": "largest window size — a windowed result cannot "
                        "be earlier than its window" if latency_floor
               else "no windowed jobs — latency is transport-bound"}
    scaling = []
    for c in cer.channels:
        if c.partitions == 1 and c.kind == "topic" and \
                c.ordering != "global":
            scaling.append("Channel '%s' has 1 partition without a "
                           "global-ordering need — increase partitions "
                           "for parallel consumers" % c.name)
    groups: Dict[str, int] = {}
    for c in cer.consumers:
        if c.group:
            groups[c.group] = groups.get(c.group, 0) + 1
    for g, n in groups.items():
        chans = {ch for c in cer.consumers if c.group == g
                 for ch in c.channels}
        parts = sum(c.partitions for c in cer.channels
                    if c.name in chans)
        if n > parts > 0:
            scaling.append("Consumer group '%s' has %d consumers for %d "
                           "partition(s) — %d will sit idle"
                           % (g, n, parts, n - parts))
    cloud = []
    if cer.cdc_sources:
        cloud.append("CDC feeds map to Delta Live Tables (Databricks) or "
                     "Snowpipe Streaming + dynamic tables (Snowflake) — "
                     "see the generated recommendations.")
    if cer.iot_sources:
        cloud.append("IoT telemetry benefits from time-series clustering "
                     "(cluster/partition by device_id, event_ts) in the "
                     "lakehouse landing tables.")
    if any(c.retention.time_ms > 7 * 86400000 for c in cer.channels):
        cloud.append("Channels retain >7 days — offload history to "
                     "object storage (tiered storage / lakehouse) "
                     "instead of broker disks.")
    risks = [f["code"] + ": " + f["message"]
             for f in validation["findings"]
             if f["severity"] in ("ERROR", "WARNING")][:20]

    # Import-side score: how much of the source was understood.
    score = round(100.0 * auto_units / units, 1)
    gen_block = None
    if generation:
        skipped = list(generation.get("unemitted") or [])
        # Coverage needs a denominator that counts everything a generator
        # could skip — `units` covers only channels/jobs/cdc/iot, so
        # subtracting a skipped producer from it would compare unlike
        # things and understate the score.
        emittable = (units + len(cer.producers) + len(cer.consumers)
                     + len(cer.schemas) + len(cer.security)) or 1
        lost = len({s.get("name") for s in skipped if s.get("name")})
        coverage = round(100.0 * max(0, emittable - lost) / emittable, 1)
        score = min(score, coverage)
        gen_block = {
            "target": generation.get("target", ""),
            "target_role": generation.get("target_role", ""),
            "coverage_percent": coverage,
            "unemitted": skipped,
            "adjustments": list(generation.get("notes") or []),
        }
        manual = manual + [
            {"severity": "MANUAL", "code": "NOT_EMITTED_FOR_TARGET",
             "message": "%s '%s' was not written to the %s artifacts — %s"
                        % (s.get("kind", "object"), s.get("name", "?"),
                           generation.get("target", "target"),
                           s.get("reason", "unsupported")),
             "obj": s.get("name", ""), "detail": "", "suggestion":
                 "Implement this by hand in the generated artifact."}
            for s in skipped]
        risks = risks + [
            "NOT_EMITTED_FOR_TARGET: %s '%s' — %s"
            % (s.get("kind", "object"), s.get("name", "?"),
               s.get("reason", "unsupported")) for s in skipped][:20]
    return {
        "automation_score": score,
        "import_understanding_score": round(100.0 * auto_units / units, 1),
        "generation": gen_block,
        "streaming_complexity": complexity,
        "complexity_level": ("LOW" if complexity < 20 else
                             "MEDIUM" if complexity < 45 else
                             "HIGH" if complexity < 75 else "VERY_HIGH"),
        "latency_estimate": latency,
        "throughput_estimate": throughput,
        "migration_risks": risks,
        "scaling_recommendations": scaling[:10],
        "cloud_optimizations": cloud,
        "manual_review_items": manual,
    }

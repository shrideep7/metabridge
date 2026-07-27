"""Command 8: CER model, parsers, validation, intelligence."""
from pathlib import Path

import pytest

from metabridge.events.cer import CER, Channel, cer_from_dict
from metabridge.events.parsers import detect_event_platform, parse_events
from metabridge.events.validate import event_intelligence, validate_cer

EV = Path(__file__).resolve().parent.parent / "examples" / "events"


@pytest.mark.parametrize("platform,folder", [
    ("kafka", "kafka"), ("rabbitmq", "rabbitmq"), ("ibmmq", "ibmmq"),
    ("kinesis", "kinesis"), ("eventhubs", "azure"), ("pubsub", "pubsub"),
    ("awsiot", "awsiot"), ("nifi", "nifi"), ("streamsets", "streamsets"),
    ("goldengate", "goldengate"), ("pulsar", "pulsar")])
def test_platform_auto_detected(platform, folder):
    det = detect_event_platform(str(EV / folder))
    assert det["detected_platform"] == platform, det


def test_kafka_family_semantics():
    cer = parse_events(str(EV / "kafka"))
    by = {c.name: c for c in cer.channels}
    orders = by["orders.v1"]
    assert orders.partitions == 12 and orders.replication == 3
    assert orders.retention.time_ms == 604800000
    assert orders.compression == "lz4"
    assert by["customers.compacted"].retention.policy == "compact"
    # producer / consumer configs
    prod = cer.producers[0]
    assert prod.acks == "1" and not prod.idempotent
    cons = next(c for c in cer.consumers if c.group ==
                "enrichment-workers")
    assert cons.offset_reset == "earliest" and cons.manual_commit
    # Debezium -> CDC source with per-key ordering channels
    cdc = cer.cdc_sources[0]
    assert cdc.flavor == "debezium:postgresql"
    assert cdc.snapshot_mode == "initial"
    assert "public.orders" in cdc.tables
    cdc_ch = cer.channel("cdc.sales.public.orders")
    assert cdc_ch is not None and cdc_ch.is_cdc and \
        cdc_ch.ordering == "per_key"
    # schema registry
    sch = next(s for s in cer.schemas if s.name == "orders.v1")
    assert sch.format == "avro" and sch.compatibility == "BACKWARD"
    assert {"order_id", "amount"} <= {f["name"] for f in sch.fields}
    # ksqlDB windowed job
    job = next(t for t in cer.transformations
               if t.name == "orders_per_customer")
    assert job.window is not None and job.window.kind == "tumbling"
    assert job.window.size_ms == 300000
    assert job.group_by == ["customer_id"]
    assert job.state_stores


def test_rabbitmq_semantics():
    cer = parse_events(str(EV / "rabbitmq"))
    q = cer.channel("payments.inbound")
    assert q.kind == "queue" and q.ordering == "fifo"
    assert q.replication == 3                       # quorum queue
    assert q.dead_letter == "payments.dlx"
    assert q.retention.time_ms == 86400000
    binding = next(r for r in cer.routing if r.kind == "binding"
                   and r.target == "payments.inbound")
    assert binding.condition == "payment.settled.#"
    assert cer.security and cer.security[0].principal == "payments_svc"


def test_ibmmq_backout_semantics():
    cer = parse_events(str(EV / "ibmmq"))
    q = cer.channel("PAY.REQUEST")
    assert q.dead_letter == "PAY.BACKOUT"
    backout = next(c for c in cer.consumers
                   if "PAY.REQUEST" in c.channels)
    assert backout.retry.max_attempts == 3
    assert backout.retry.dead_letter == "PAY.BACKOUT"


def test_cloud_services_semantics():
    k = parse_events(str(EV / "kinesis")).channels[0]
    assert k.partitions == 4 and k.retention.time_ms == 48 * 3600000
    az = parse_events(str(EV / "azure"))
    sb = az.channel("billing-commands")
    assert sb.ordering == "fifo"                    # requiresSession
    assert sb.dead_letter.endswith("$DeadLetterQueue")
    assert az.channel(sb.dead_letter) is not None   # implicit sub-queue
    ps = parse_events(str(EV / "pubsub"))
    sub = ps.consumers[0]
    assert sub.retry.max_attempts == 5
    assert sub.retry.dead_letter == "clickstream-dead"
    assert ps.channel("clickstream").ordering == "per_key"


def test_iot_and_cdc():
    iot = parse_events(str(EV / "awsiot"))
    rule = next(t for t in iot.transformations if t.kind == "rule")
    assert "temperature > 80" in rule.sql
    assert iot.channel("sensors/+/temperature").is_iot
    twin = next(i for i in iot.iot_sources if i.name == "sensor-42")
    assert twin.device_twin
    assert {"temperature", "humidity"} <= {f["name"]
                                           for f in
                                           twin.telemetry_fields}
    gg = parse_events(str(EV / "goldengate"))
    assert gg.cdc_sources[0].flavor.startswith("goldengate")
    assert "sales.orders" in gg.cdc_sources[0].tables


def test_validation_checks():
    cer = parse_events(str(EV / "kafka"))
    v = validate_cer(cer, "kafka")
    codes = {f["code"] for f in v["findings"]}
    assert "DUPLICATE_OR_LOSS_RISK" in codes        # acks=1 producer
    assert "REPLICATION_SINGLE" in codes            # orders.dlq rf=1
    assert "CONSUMER_LAG_RUNTIME_ONLY" in codes
    assert v["verdict"] == "PASS_WITH_WARNINGS"
    # exactly-once downgrade declared per target
    cer2 = parse_events(str(EV / "pulsar"))
    v2 = validate_cer(cer2, "kinesis")
    assert any(f["code"] == "DELIVERY_DOWNGRADE"
               for f in v2["findings"])


def test_watermark_missing_flagged():
    cer = parse_events(str(EV / "kafka"))
    v = validate_cer(cer)
    assert any(f["code"] == "WATERMARK_MISSING"
               for f in v["findings"])              # ksql has no watermark


def test_intelligence():
    cer = parse_events(str(EV / "kafka"))
    mi = event_intelligence(cer, validate_cer(cer, "kafka"))
    assert mi["automation_score"] == 100.0
    assert mi["latency_estimate"]["floor_ms"] == 300000
    assert mi["throughput_estimate"][
        "estimated_capacity_events_per_sec"] > 0
    assert mi["cloud_optimizations"]                # CDC present
    assert any("idle" in s or "partition" in s
               for s in mi["scaling_recommendations"])


def test_cer_round_trip():
    import json
    cer = parse_events(str(EV / "kafka"))
    clone = cer_from_dict(json.loads(json.dumps(cer.to_dict())))
    assert clone.inventory() == cer.inventory()
    job = next(t for t in clone.transformations
               if t.name == "orders_per_customer")
    assert job.window.size_ms == 300000
    assert clone.channel("orders.v1").retention.time_ms == 604800000

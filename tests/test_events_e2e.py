"""Command 8 E2E: generation matrix, semantics preservation, API."""
import ast
import json
import sys
from pathlib import Path

import pytest

from metabridge.events.generators import EVENT_TARGETS, generate_events
from metabridge.events.graph import event_lineage, execution_graph, to_mermaid
from metabridge.events.parsers import parse_events

EV = Path(__file__).resolve().parent.parent / "examples" / "events"
SOURCES = ("kafka", "kafka_retail", "rabbitmq", "ibmmq", "kinesis",
           "azure", "pubsub", "awsiot", "nifi", "streamsets",
           "goldengate", "pulsar")


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("target", EVENT_TARGETS)
def test_matrix_generates_valid_artifacts(tmp_path, source, target):
    cer = parse_events(str(EV / source))
    m = generate_events(cer, target, str(tmp_path))
    assert m["files"], (source, target)
    for f in m["files"]:
        text = (tmp_path / f).read_text()
        if f.endswith(".py"):
            ast.parse(text)
        if f.endswith(".json"):
            json.loads(text)


def test_semantics_preserved_kafka_to_rabbitmq(tmp_path):
    cer = parse_events(str(EV / "kafka"))
    generate_events(cer, "rabbitmq", str(tmp_path))
    doc = json.loads((tmp_path / "definitions.json").read_text())
    orders = next(q for q in doc["queues"] if q["name"] == "orders.v1")
    # retention + partition semantics declared, never dropped
    assert orders["arguments"]["x-message-ttl"] == 604800000
    assert any("12 partitions" in n
               for n in orders["_metabridge_notes"])


def test_semantics_preserved_rabbitmq_to_kafka(tmp_path):
    cer = parse_events(str(EV / "rabbitmq"))
    generate_events(cer, "kafka", str(tmp_path))
    doc = json.loads((tmp_path / "topics.json").read_text())
    q = next(t for t in doc["topics"]
             if t["name"] == "payments.inbound")
    assert q["partitions"] == 1                     # FIFO preserved
    notes = " ".join(q["_metabridge_notes"])
    assert "fifo" in notes and "dead-letter" in notes


def test_window_semantics_to_flink_and_spark(tmp_path):
    cer = parse_events(str(EV / "kafka"))
    generate_events(cer, "flink", str(tmp_path / "f"))
    flink = (tmp_path / "f" / "streaming.sql").read_text()
    assert "TUMBLE(TABLE orders_src" in flink
    assert "INTERVAL '300' SECOND" in flink
    assert "WATERMARK" in flink
    assert "MANUAL" in flink                        # missing watermark
    generate_events(cer, "spark_streaming", str(tmp_path / "s"))
    spark = (tmp_path / "s" / "streaming_job.py").read_text()
    assert "withWatermark" in spark and "F.window" in spark
    assert "checkpointLocation" in spark


def test_cdc_to_databricks_and_snowflake(tmp_path):
    cer = parse_events(str(EV / "kafka"))
    generate_events(cer, "databricks_streaming", str(tmp_path / "d"))
    dlt = (tmp_path / "d" / "dlt_pipeline.py").read_text()
    assert "dlt.apply_changes" in dlt and "snapshot mode" in dlt
    generate_events(cer, "snowflake_streaming", str(tmp_path / "sf"))
    sf = (tmp_path / "sf" / "snowflake_streaming.sql").read_text()
    assert "Snowpipe Streaming" in sf and "DYNAMIC TABLE" in sf


def test_pubsub_generation_keeps_dlq_and_ordering(tmp_path):
    cer = parse_events(str(EV / "pubsub"))
    generate_events(cer, "pubsub", str(tmp_path))
    tf = (tmp_path / "pubsub.tf").read_text()
    assert "enable_message_ordering = true" in tf
    assert "dead_letter_policy" in tf and "max_delivery_attempts" in tf


def test_lineage_and_graph():
    cer = parse_events(str(EV / "kafka"))
    lin = event_lineage(cer)
    assert lin["transformation_lineage"]["orders_per_customer"][
        "windowed"]
    assert lin["cdc_lineage"]
    g = execution_graph(cer)
    kinds = {n["type"] for n in g["nodes"]}
    assert {"producer", "channel", "transform", "consumer"} <= kinds
    assert any(e["kind"] == "produce" for e in g["edges"])
    assert to_mermaid(cer).startswith("flowchart LR")


def test_no_pairwise_converters():
    import re
    src = Path(__file__).resolve().parent.parent / "src" / "metabridge" \
        / "events"
    body = "\n".join(f.read_text() for f in src.glob("*.py"))
    assert not re.search(
        r"def\s+\w*(kafka_to_(rabbitmq|pulsar|kinesis)|rabbitmq_to_\w+|"
        r"pulsar_to_\w+)", body)


# --- API ---------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None) for m in ("web.app", "web.auth",
                                                   "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    yield TestClient(webapp.app)
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def _files(folder):
    return [{"name": f.name, "content": f.read_text(errors="replace")}
            for f in (EV / folder).iterdir() if f.is_file()]


def test_events_api_flow(client):
    r = client.post("/api/events/analyze",
                    json={"files": _files("kafka")})
    assert r.status_code == 200
    d = r.json()
    eid = d["event_id"]
    assert d["detected_platform"] in ("kafka", "debezium", "confluent")
    assert "orders.v1" in d["topics"]
    assert "orders_per_customer" in d["streaming_jobs"]
    assert d["automation_score"] is not None
    assert d["semantic_confidence"] is not None
    # FAIL: the kafka fixture carries a BACKWARD-breaking schema change
    # (orders.v1 amount double->string) that the compatibility check now
    # actually reports instead of only advertising in checks_run.
    assert d["validation_verdict"] == "FAIL"
    assert d["lineage"]["event_flow"]

    c = client.post("/api/events/convert",
                    json={"event_id": eid, "target": "flink"})
    assert c.status_code == 200 and c.json()["generated"]

    v = client.post("/api/events/validate",
                    json={"event_id": eid, "target": "kinesis"})
    assert v.status_code == 200
    assert any(f["code"] == "DUPLICATE_OR_LOSS_RISK"
               for f in v.json()["findings"])

    rev = client.post("/api/events/review",
                      json={"event_id": eid, "ai": False}).json()
    assert rev["engine"] == "rules"
    assert "never replaces" in rev["note"]

    ln = client.get("/api/events/%s/lineage" % eid).json()
    assert ln["mermaid"].startswith("flowchart")
    rp = client.get("/api/events/%s/report" % eid).json()
    assert rp["intelligence"]["automation_score"] is not None

    bad = client.post("/api/events/convert",
                      json={"event_id": eid, "target": "nonsense"})
    assert bad.status_code == 422


def test_marketplace_lists_event_connectors(client):
    d = client.get("/api/v1/connectors").json()
    keys = {c["key"] for c in d["connectors"]}
    assert {"kafka_events", "confluent", "pulsar", "rabbitmq", "ibmmq",
            "activemq", "kinesis_events", "eventhubs", "pubsub", "mqtt",
            "hivemq", "emqx", "mosquitto", "awsiot", "azureiot",
            "debezium", "goldengate", "nifi", "streamsets"} <= keys
    spec = next(c for c in d["connectors"] if c["key"] == "rabbitmq")
    assert spec["platform_type"] == "event_streaming"
    assert {"metadata_analysis", "streaming_lineage", "pipeline_scaffold",
            "modernization", "validation",
            "ai_review"} <= set(spec["capabilities"])

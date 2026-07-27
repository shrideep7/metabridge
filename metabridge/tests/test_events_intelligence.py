"""Event Intelligence Layer: deterministic analysis AFTER the CER."""
import json
import sys
import time
from pathlib import Path

import pytest

from metabridge.events.cer import (
    CER, Channel, Consumer, EventSchema, Producer, RetentionPolicy,
    RetryPolicy,
)
from metabridge.events.insight import (
    ASSUMPTIONS, analyze_cdc, analyze_cost, analyze_event_intelligence,
    analyze_partitions, analyze_quality, analyze_schema_evolution,
    analyze_security, analyze_topology,
)
from metabridge.events.parsers import parse_events

EV = Path(__file__).resolve().parent.parent / "examples" / "events"


@pytest.fixture(scope="module")
def kafka_cer():
    return parse_events(str(EV / "kafka"))


@pytest.fixture(scope="module")
def intel(kafka_cer):
    return analyze_event_intelligence(kafka_cer)


def test_consumes_cer_only_and_does_not_modify_it(kafka_cer):
    before = json.dumps(kafka_cer.to_dict(), sort_keys=True)
    analyze_event_intelligence(kafka_cer)
    assert json.dumps(kafka_cer.to_dict(), sort_keys=True) == before


def test_deterministic(kafka_cer):
    a = analyze_event_intelligence(kafka_cer)
    b = analyze_event_intelligence(kafka_cer)
    assert json.dumps(a, sort_keys=True) == json.dumps(b,
                                                       sort_keys=True)


def test_topology(intel):
    t = intel["topology"]
    assert t["producer_topology"]["orders.v1"] == ["producer"]
    assert "enrichment-workers" in t["consumer_groups"]
    assert "orders.dlq" in t["dead_letter_queues"]
    assert {"orders.v1", "orders.enriched", "orders.dlq"} <= set(
        t["channel_hierarchy"]["orders"])
    replay = {r["channel"] for r in
              t["replay_architecture"]["replayable_channels"]}
    assert "orders.v1" in replay and "customers.compacted" in replay
    assert t["execution_graph"]["nodes"]


def test_partitions(intel):
    p = intel["partitions"]
    orders = next(c for c in p["channels"]
                  if c["channel"] == "orders.v1")
    assert orders["partitions"] == 12
    assert orders["idle_partitions"] == 11         # one consumer
    assert orders["skew"] == "requires_runtime_metrics"   # honest
    assert any("11 of 12" in r for r in p["recommendations"])


def test_schema_evolution_breaking_change(intel):
    s = intel["schema_evolution"]
    assert s["risk_level"] == "HIGH"
    subj = next(x for x in s["subjects"] if x["schema"] == "orders.v1")
    assert subj["versions"] == [3, 4]
    change = subj["changes"][0]
    assert "amount" in change["type_changes"]
    assert any("double -> string" in b
               for b in change["breaking_changes"])
    assert change["added"] == ["channel"]          # nullable addition
    assert not subj["backward_safe"]


def test_performance_and_resources(intel):
    perf = intel["performance"]
    assert perf["throughput"]["sustained_events_per_sec"] == \
        sum(c["partitions"] for c in intel["partitions"]["channels"]) \
        * ASSUMPTIONS["events_per_partition_per_sec"]
    assert perf["latency"]["floor_ms"] == 300000   # 5-min window
    assert perf["consumer_lag"].startswith("runtime_only")
    assert perf["cloud_resources"]["estimated_brokers_or_units"] >= 1
    assert perf["backpressure_points"]             # 12p / 1 consumer


def test_quality_findings(intel):
    q = intel["quality"]
    kinds = {f["kind"] for f in q["findings"]}
    assert "duplicate_or_loss" in kinds            # acks=1 producer
    assert "missing_events" in kinds               # rf=1 dlq
    assert all(f["remediation"] for f in q["findings"])


def test_dead_letter_loop_detection():
    cer = CER(name="x", source_platform="test")
    cer.channels.append(Channel(name="a", dead_letter="b"))
    cer.channels.append(Channel(name="b", dead_letter="a"))
    q = analyze_quality(cer)
    assert any(f["kind"] == "dead_letter_loop" for f in q["findings"])


def test_cdc_mode_recommendation(intel):
    cdc = intel["cdc"]["sources"][0]
    assert cdc["recommended_mode"] == "streaming"
    assert cdc["transaction_consistency"].startswith("preserved")
    assert "runtime_only" in cdc["log_growth"]


def test_iot_recommendations():
    intel_iot = analyze_event_intelligence(
        parse_events(str(EV / "awsiot")))["iot"]
    src = next(s for s in intel_iot["sources"]
               if "unbounded" in str(s["sensor_cardinality"]))
    assert src["device_throughput"] == "runtime_only"
    assert any("edge" in r for r in intel_iot["recommendations"])
    assert any("compression" in r for r in intel_iot["recommendations"])


def test_security(intel):
    sec = intel["security"]
    kinds = {f["kind"] for f in sec["findings"]}
    assert "authorization" in kinds                # no ACLs in import
    assert "encryption" in kinds                   # no TLS markers
    rmq = analyze_security(parse_events(str(EV / "rabbitmq")))
    assert rmq["policies_imported"] == 1


def test_cost_model(intel):
    cost = intel["cost"]
    m = cost["monthly_estimates_usd"]
    assert m["total"] == round(m["storage"] + m["streaming"]
                               + m["compute"], 2)
    assert m["storage_tiered_alternative"] < m["storage"]
    assert cost["assumptions"]["note"]             # labelled assumptions
    assert cost["optimizations"]
    assert "not derivable" in cost["cross_region_traffic"]


def test_readiness_scores_have_explanations(intel):
    r = intel["readiness"]
    assert set(r) == {"automation_score", "modernization_readiness",
                      "migration_complexity", "operational_risk",
                      "business_risk", "semantic_confidence"}
    for s in r.values():
        assert isinstance(s["value"], (int, float))
        assert len(s["explanation"]) > 20
    # HIGH schema risk must depress readiness
    assert r["modernization_readiness"]["value"] < 100


def test_executive_report(intel):
    rep = intel["executive_report"]
    for section in ("Executive summary", "Scores", "Current topology",
                    "Recommended target architecture", "Risk matrix",
                    "Cost comparison", "Migration roadmap",
                    "Estimated timeline"):
        assert section in rep
    assert "mermaid" in rep
    assert ASSUMPTIONS["note"].split("—")[0].strip()[:20] in rep


def test_performance_gate_large_estate():
    """500 channels / 200 consumers / 50 jobs must analyze in < 2s."""
    cer = CER(name="big", source_platform="kafka")
    for i in range(500):
        cer.channels.append(Channel(
            name="topic_%d" % i, partitions=(i % 16) + 1,
            replication=3,
            retention=RetentionPolicy(time_ms=86400000 * (i % 14 + 1)),
            dead_letter="topic_%d_dlq" % i if i % 7 == 0 else ""))
    for i in range(200):
        cer.consumers.append(Consumer(
            name="c%d" % i, group="g%d" % (i % 40),
            channels=["topic_%d" % (i % 500)],
            retry=RetryPolicy(3, 1000, dead_letter="" if i % 3
                              else "topic_0")))
    for i in range(100):
        cer.producers.append(Producer(name="p%d" % i,
                                      channels=["topic_%d" % i],
                                      acks="1"))
    for i in range(20):
        cer.schemas.append(EventSchema(
            name="s%d" % (i // 2), version=i % 2 + 1,
            fields=[{"name": "f%d" % j,
                     "type": "string" if i % 2 else "double"}
                    for j in range(10)]))
    t0 = time.time()
    intel = analyze_event_intelligence(cer)
    elapsed = time.time() - t0
    assert elapsed < 2.0, "intelligence layer took %.2fs" % elapsed
    assert len(intel["partitions"]["channels"]) == 500
    assert intel["schema_evolution"]["subjects"]


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


def test_intelligence_api_flow(client):
    files = [{"name": f.name, "content": f.read_text(errors="replace")}
             for f in (EV / "kafka").iterdir() if f.is_file()]
    r = client.post("/api/events/intelligence", json={"files": files})
    assert r.status_code == 200
    d = r.json()
    eid = d["event_id"]
    assert d["determinism_note"]
    assert d["schema_evolution"]["risk_level"] == "HIGH"

    topo = client.get("/api/events/%s/topology" % eid)
    assert topo.status_code == 200
    assert "orders.dlq" in topo.json()["dead_letter_queues"]

    recs = client.get("/api/events/%s/recommendations" % eid).json()
    assert recs["partitions"] and recs["schema"] and recs["quality"]

    cost = client.get("/api/events/%s/cost-analysis" % eid).json()
    assert cost["monthly_estimates_usd"]["total"] > 0

    ready = client.get("/api/events/%s/readiness" % eid).json()
    assert ready["modernization_readiness"]["explanation"]

    # reuse via event_id from a prior analyze
    a = client.post("/api/events/analyze", json={"files": files}).json()
    r2 = client.post("/api/events/intelligence",
                     json={"event_id": a["event_id"]})
    assert r2.status_code == 200

"""Command 8: CER model, parsers, validation, intelligence."""
from pathlib import Path

import pytest

from metabridge.events.cer import CER, Channel, cer_from_dict
from metabridge.events.parsers import detect_event_platform, parse_events
from metabridge.events.validate import event_intelligence, validate_cer

EV = Path(__file__).resolve().parent.parent / "examples" / "events"


@pytest.mark.parametrize("platform,folder", [
    ("kafka", "kafka"), ("kafka", "kafka_retail"),
    ("rabbitmq", "rabbitmq"), ("ibmmq", "ibmmq"),
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
    # orders.v1 v3->v4 changes amount double->string under BACKWARD, so
    # the registry would reject the write — an estate-level error, not a
    # warning. (This asserted PASS_WITH_WARNINGS while the compatibility
    # check was advertised but never actually run.)
    assert "SCHEMA_INCOMPATIBLE" in codes
    assert v["verdict"] == "FAIL"
    # exactly-once downgrade declared per target
    cer2 = parse_events(str(EV / "pulsar"))
    v2 = validate_cer(cer2, "kinesis")
    assert any(f["code"] == "DELIVERY_DOWNGRADE"
               for f in v2["findings"])


def test_window_time_semantics_are_not_assumed():
    """B5: time_semantics defaulted to event_time for every window, so a
    definite WATERMARK_MISSING was reported on evidence the import never
    contained. The kafka ksql declares no TIMESTAMP column, so its
    semantics are genuinely undeterminable."""
    cer = parse_events(str(EV / "kafka"))
    job = next(t for t in cer.transformations if t.window)
    assert job.window.time_semantics == "unknown"
    codes = {f["code"] for f in validate_cer(cer)["findings"]}
    assert "WINDOW_TIME_SEMANTICS_UNKNOWN" in codes
    assert "WATERMARK_MISSING" not in codes


def test_watermark_missing_flagged_when_event_time_is_declared():
    """A source that DOES declare a timestamp column still gets the
    definite finding."""
    from metabridge.events.parsers import parse_streaming_sql
    cer = CER(name="t", source_platform="kafka")
    parse_streaming_sql(
        "CREATE STREAM s (id VARCHAR, ts BIGINT) "
        "WITH (KAFKA_TOPIC='t', TIMESTAMP='ts');\n"
        "CREATE TABLE agg WITH (KAFKA_TOPIC='o', TIMESTAMP='ts') AS "
        "SELECT id, COUNT(*) AS c FROM s WINDOW TUMBLING (SIZE 60 SECONDS) "
        "GROUP BY id EMIT CHANGES;", "w.sql", cer)
    job = next(t for t in cer.transformations if t.window)
    assert job.window.time_semantics == "event_time"
    codes = {f["code"] for f in validate_cer(cer)["findings"]}
    assert "WATERMARK_MISSING" in codes


def test_intelligence():
    cer = parse_events(str(EV / "kafka"))
    v = validate_cer(cer, "kafka")
    mi = event_intelligence(cer, v)
    # B6: the score deducted only MANUAL issues, which no example estate
    # produces — so it read 100.0 on every platform, including this one,
    # whose generated ksqlDB would not run. It must move with findings.
    assert mi["automation_score"] < 100.0
    assert mi["import_understanding_score"] == 100.0   # parsing is fine
    assert mi["semantic_confidence"] < 100
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


# --- Phase 0 regressions -------------------------------------------------

def test_mermaid_ids_survive_separator_collision():
    """G1: '.', '-' and '/' all fold to '_', so distinct topics used to
    share one Mermaid node and the last label silently won."""
    from metabridge.events.graph import to_mermaid
    cer = CER(name="t", source_platform="kafka")
    for n in ("orders.v1", "orders-v1", "orders/v1", "plain"):
        cer.channels.append(Channel(name=n, kind="topic"))
    ids = [ln.strip().split("[")[0]
           for ln in to_mermaid(cer).splitlines()[1:] if ln.strip()]
    assert len(set(ids)) == len(ids) == 4, ids
    # a name that never collides must not be given a suffix
    assert "channel_plain" in ids


def test_windowed_ksql_keeps_group_by():
    """B10: the re-add guard tested the un-stripped body, so the emitted
    aggregate had a bare non-aggregated column — invalid ksqlDB."""
    from metabridge.events.generators import generate_ksql
    cer = parse_events(str(EV / "kafka"))
    ksql = generate_ksql(cer)
    stmt = next(s for s in ksql.split(";") if "orders_per_customer" in s)
    assert "GROUP BY customer_id" in stmt, stmt
    assert stmt.index("GROUP BY") < stmt.index("EMIT CHANGES")


def test_quality_counts_are_deterministic():
    """N1: counts keys came from a set comprehension, so identical input
    serialized to different bytes depending on the process hash seed."""
    from metabridge.events.insight import analyze_quality
    cer = parse_events(str(EV / "azure"))
    counts = analyze_quality(cer)["counts"]
    assert list(counts) == sorted(counts)


# --- B12: schema compatibility is checked, not just advertised ----------

def _schema_pair(old_type, new_type, compat="BACKWARD"):
    from metabridge.events.cer import EventSchema
    cer = CER(name="t", source_platform="kafka")
    for ver, typ in ((1, old_type), (2, new_type)):
        cer.schemas.append(EventSchema(
            name="orders.v1", format="avro", definition="",
            fields=[{"name": "amount", "type": typ}],
            compatibility=compat, version=ver))
    return cer


def test_breaking_schema_change_fails_validation():
    cer = parse_events(str(EV / "kafka"))
    v = validate_cer(cer, "kafka")
    bad = [f for f in v["findings"] if f["code"] == "SCHEMA_INCOMPATIBLE"]
    assert len(bad) == 1, v["findings"]
    assert bad[0]["severity"] == "ERROR"
    assert "double -> string" in bad[0]["message"]
    assert v["verdict"] == "FAIL"
    # the advertised check must be the one that actually ran
    assert "schema_compatibility" in v["checks_run"]


@pytest.mark.parametrize("old,new", [
    ("int", "long"), ("int", "double"), ("float", "double"),
    ("string", "bytes")])
def test_legal_avro_promotion_is_not_breaking(old, new):
    """A reader resolves these itself — flagging them would fail
    migrations that are actually safe."""
    v = validate_cer(_schema_pair(old, new), "kafka")
    assert not [f for f in v["findings"]
                if f["code"] == "SCHEMA_INCOMPATIBLE"]


@pytest.mark.parametrize("compat,severity", [
    ("BACKWARD", "ERROR"), ("FULL", "ERROR"), ("NONE", "WARNING")])
def test_incompatibility_severity_follows_compatibility_mode(compat,
                                                             severity):
    """Under an enforced mode the registry would reject the write; under
    NONE the break is permitted but consumers still need porting."""
    v = validate_cer(_schema_pair("double", "string", compat), "kafka")
    bad = [f for f in v["findings"] if f["code"] == "SCHEMA_INCOMPATIBLE"]
    assert len(bad) == 1 and bad[0]["severity"] == severity


# --- B3: cer.schemas is version history; consumers need the current one --

def test_current_schemas_picks_newest_per_subject():
    from metabridge.events.cer import EventSchema
    cer = CER(name="t", source_platform="kafka")
    for name, ver in [("b", 1), ("a", 4), ("a", 3), ("b", 7), ("a", 2)]:
        cer.schemas.append(EventSchema(name=name, version=ver))
    got = [(s.name, s.version) for s in cer.current_schemas()]
    assert got == [("b", 7), ("a", 4)]          # first-appearance order
    assert [s.version for s in cer.schema_versions("a")] == [2, 3, 4]
    # the history itself must be left intact for evolution analysis
    assert len(cer.schemas) == 5


def test_registry_script_registers_one_current_version():
    """B7: every version POSTed to the same subject, in filename order,
    left the OLDER schema as the registry's newest version."""
    from metabridge.events.generators import generate_kafka
    cer = parse_events(str(EV / "kafka"))
    sh = generate_kafka(cer, "confluent")["register_schemas.sh"]
    assert sh.count('POST "$SR/subjects/orders.v1-value/versions"') == 1
    body = sh.split("versions")[1]
    assert '\\"amount\\":\\"' not in body or "double" not in body
    assert "registering v4 only" in sh          # supersession is declared


def test_generated_schema_file_is_the_current_version():
    """B8: both versions collided on schemas/orders_v1.avsc, so the file
    left on disk was whichever was written last — the stale one."""
    from metabridge.events.generators import generate_kafka
    files = generate_kafka(parse_events(str(EV / "kafka")), "confluent")
    avsc = files["schemas/orders_v1.avsc"]
    assert '"amount","type":"string"' in avsc.replace(" ", "")
    assert "channel" in avsc                    # v4-only field


def test_flink_columns_use_the_current_schema_version():
    """Latent sibling of B8: schema_of keyed off the version history, so
    a column could be declared with a superseded type."""
    from metabridge.events.generators import generate_flink
    sql = generate_flink(parse_events(str(EV / "kafka")))
    orders = next(b for b in sql.split("CREATE TABLE") if "`amount`" in b)
    assert "`amount` STRING" in orders and "`amount` DOUBLE" not in orders
    assert "`channel` STRING" in orders          # v4-only field


def test_schema_findings_are_raised_once_per_subject():
    """Iterating the version history raised the same advisory once for
    every registered version of the same schema."""
    from metabridge.events.review import _deterministic_findings
    cer = _schema_pair("double", "string", compat="")
    unset = [f for f in validate_cer(cer, "kafka")["findings"]
             if f["code"] == "SCHEMA_EVOLUTION_UNSET"]
    assert len(unset) == 1, unset
    eva = [f for f in _deterministic_findings(cer)
           if f["kind"] == "schema_evolution"]
    assert len(eva) == 1, eva


def test_unemittable_schema_reported_once_per_subject():
    from metabridge.events.generators import generate_events
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        rep = generate_events(parse_events(str(EV / "kafka")),
                              "pulsar", d)["report"]
    names = [s["name"] for s in rep["unemitted"] if s["kind"] == "schema"]
    assert names == ["orders.v1"], names


# --- Phase 1 / graph / scoring regressions ------------------------------

def test_bare_ddl_is_a_declaration_not_a_job():
    """B1/G3: `CREATE STREAM x WITH (KAFKA_TOPIC='t')` was recorded as a
    job whose output was the topic it reads."""
    cer = parse_events(str(EV / "kafka"))
    assert [d.name for d in cer.declarations] == ["orders_src"]
    assert cer.declarations[0].topic == "orders.v1"
    assert "orders_src" not in [t.name for t in cer.transformations]
    assert cer.inventory()["streaming_jobs"] == 2
    assert cer.inventory()["stream_declarations"] == 1
    # G3: no edge may point from the declaration back at its own source
    assert not [e for e in cer.flow_edges()
                if e["from"] == "transform:orders_src"]


def test_declared_stream_input_resolves_to_its_topic():
    """B2/B4: inputs are SQL names; checking them against channel names
    reported a resolvable stream as an unresolved channel."""
    cer = parse_events(str(EV / "kafka"))
    assert cer.resolve_stream("orders_src") == "orders.v1"
    assert not [f for f in validate_cer(cer, "kafka")["findings"]
                if f["code"] == "TRANSFORM_INPUT_UNRESOLVED"]
    assert {"from": "channel:orders.v1",
            "to": "transform:orders_per_customer",
            "kind": "consume"} in cer.flow_edges()


def test_ksql_declares_sources_before_using_them():
    """B9: the emitted script did FROM orders_src without ever declaring
    orders_src, so it failed on the first statement."""
    from metabridge.events.generators import generate_ksql
    ksql = generate_ksql(parse_events(str(EV / "kafka")))
    assert ksql.index("CREATE STREAM orders_src") < \
        ksql.index("FROM orders_src")


def test_unknown_endpoints_match_existing_nodes_before_inventing_them():
    """G2: NiFi drew its whole flow through phantom channel nodes while
    the real processor nodes sat orphaned."""
    from metabridge.events.graph import execution_graph
    g = execution_graph(parse_events(str(EV / "nifi")))
    assert not [n for n in g["nodes"] if n["type"] == "external"]
    used = {x for e in g["edges"] for x in (e["from"], e["to"])}
    assert not [n["id"] for n in g["nodes"] if n["id"] not in used]


@pytest.mark.parametrize("folder,cdc_node", [
    ("goldengate", "cdc:ext_sales"), ("kafka", "cdc:cdc-orders-pg")])
def test_cdc_sources_are_connected_to_their_channels(folder, cdc_node):
    """G4: cdc_lineage recorded the link while flow_edges emitted no
    edge, so goldengate drew two nodes and zero edges."""
    cer = parse_events(str(EV / folder))
    edges = [e for e in cer.flow_edges() if e["from"] == cdc_node]
    assert len(edges) == len(cer.cdc_sources[0].output_channels) > 0
    assert all(e["kind"] == "capture" for e in edges)


def test_iot_source_doubling_as_a_rule_is_not_duplicated():
    """AWS IoT parses one rule into both an IoTSource and a job; node and
    edge must agree on which represents it or the graph grows an orphan."""
    from metabridge.events.graph import execution_graph
    g = execution_graph(parse_events(str(EV / "awsiot")))
    used = {x for e in g["edges"] for x in (e["from"], e["to"])}
    assert not [n["id"] for n in g["nodes"] if n["id"] not in used]


def test_automation_score_varies_with_findings():
    """B6: it deducted only MANUAL issues, which no estate produces."""
    scores = {}
    for folder in ("kafka", "awsiot", "pulsar"):
        cer = parse_events(str(EV / folder))
        scores[folder] = event_intelligence(
            cer, validate_cer(cer, "kafka"))["automation_score"]
    assert len(set(scores.values())) > 1, scores
    assert scores["kafka"] < scores["awsiot"]      # kafka has an ERROR


def test_semantic_confidence_comes_from_the_engine():
    """B6b: the formula lived in the HTTP route, so CLI/REST consumers of
    event_intelligence() never received it."""
    cer = parse_events(str(EV / "kafka"))
    v = validate_cer(cer, "kafka")
    intel = event_intelligence(cer, v)
    assert "semantic_confidence" in intel
    warnings = sum(1 for f in v["findings"] if f["severity"] == "WARNING")
    assert intel["semantic_confidence"] == max(
        0, 100 - 5 * len(intel["manual_review_items"]) - 2 * warnings)


def test_cdc_topic_prefix_is_preserved():
    """B11: emitting cdc.name renamed every generated topic away from the
    CER channels the rest of the output references."""
    import tempfile, json as _json
    from metabridge.events.generators import generate_events
    cer = parse_events(str(EV / "kafka"))
    assert cer.cdc_sources[0].topic_prefix == "cdc.sales"
    with tempfile.TemporaryDirectory() as d:
        generate_events(cer, "confluent", d)
        cfg = _json.loads((Path(d) / "connect_cdc_orders_pg.json")
                          .read_text(encoding="utf-8"))
    prefix = cfg["config"]["topic.prefix"]
    assert prefix == "cdc.sales"
    assert all(c.startswith(prefix)
               for c in cer.cdc_sources[0].output_channels)


# --- N3 / N4: input facts that were parsed but never linked -------------

def test_mq_alias_resolves_to_its_base_queue():
    """N3: DEFINE QALIAS('PAY.IN') TARGET('PAY.REQUEST') kept TARGET as a
    property but never linked it, so the alias showed as an unconnected
    queue and nothing recorded where writers to it actually land."""
    cer = parse_events(str(EV / "ibmmq"))
    alias = next(r for r in cer.routing if r.kind == "alias")
    assert (alias.source, alias.target) == ("PAY.IN", "PAY.REQUEST")
    edge = next(e for e in cer.flow_edges() if e["kind"] == "alias")
    assert edge["from"] == "channel:PAY.IN"
    assert edge["to"] == "channel:PAY.REQUEST"
    # the alias must no longer sit unconnected in the diagram
    from metabridge.events.graph import execution_graph
    g = execution_graph(cer)
    used = {x for e in g["edges"] for x in (e["from"], e["to"])}
    assert "channel:PAY.IN" in used


def test_declared_exchange_is_not_reported_as_missing():
    """N4: exchanges are declared objects stored as routing rules, so a
    binding naming one resolved to nothing and the graph annotated it
    'not present in the import' — which was false."""
    from metabridge.events.graph import execution_graph
    cer = parse_events(str(EV / "rabbitmq"))
    assert [e.name for e in cer.exchanges()] == ["payments", "payments.dlx"]
    g = execution_graph(cer)
    assert not [n for n in g["nodes"] if n["type"] == "external"]
    ex = {n["id"] for n in g["nodes"] if n["type"] == "exchange"}
    assert ex == {"exchange:payments", "exchange:payments.dlx"}
    # an exchange and a queue may share a name — they must stay distinct
    assert "channel:payments.dlx" in {n["id"] for n in g["nodes"]}
    assert any(e["from"] == "exchange:payments"
               and e["to"] == "channel:payments.inbound"
               for e in g["edges"])
    # and the dlx binding must not become a self-loop
    assert not [e for e in g["edges"] if e["from"] == e["to"]]


# --- second fixture: the kafka-only fixes, on a second real estate ------
#
# Eight fixes were observable only in examples/events/kafka, which is the
# sole estate carrying schemas, ksqlDB, CDC and windows. kafka_retail is a
# second real estate that exercises the OPPOSITE branch of each, so the
# fixes are not just "whatever kafka happens to do".

RETAIL = "kafka_retail"


def test_retail_schema_promotions_are_backward_safe():
    """B12's negative case on real parsed input: int->long and
    float->double are legal Avro promotions, so a BACKWARD subject that
    only promotes must NOT be reported as incompatible."""
    from metabridge.events.insight import analyze_schema_evolution
    cer = parse_events(str(EV / RETAIL))
    change = analyze_schema_evolution(cer)["subjects"][0]["changes"][0]
    assert sorted(change["type_changes"]) == ["refund", "store_id"]
    assert change["breaking_changes"] == []
    v = validate_cer(cer, "kafka")
    assert not [f for f in v["findings"]
                if f["code"] == "SCHEMA_INCOMPATIBLE"]
    assert v["verdict"] != "FAIL"


def test_retail_window_inherits_event_time_from_its_source():
    """B5's positive case: the job declares no TIMESTAMP, but the stream
    it reads does — so its semantics ARE stated by the import."""
    cer = parse_events(str(EV / RETAIL))
    assert cer.declarations[0].timestamp_column == "event_ts"
    job = next(t for t in cer.transformations if t.window)
    assert job.window.time_semantics == "event_time"
    codes = {f["code"] for f in validate_cer(cer)["findings"]}
    assert "WATERMARK_MISSING" in codes
    assert "WINDOW_TIME_SEMANTICS_UNKNOWN" not in codes


def test_retail_schema_and_cdc_generation():
    """B7, B8 and B11 on a second estate."""
    import json
    from metabridge.events.generators import generate_kafka
    cer = parse_events(str(EV / RETAIL))
    files = generate_kafka(cer, "confluent")
    sh = files["register_schemas.sh"]
    assert sh.count('POST "$SR/subjects/returns.v2-value/versions"') == 1
    assert "registering v2 only" in sh
    assert '"store_id","type":"long"' in \
        files["schemas/returns_v2.avsc"].replace(" ", "")
    assert cer.cdc_sources[0].topic_prefix == "cdc.retail"
    cfg = json.loads(files["connect_cdc_returns_mysql.json"])
    assert cfg["config"]["topic.prefix"] == "cdc.retail"


def test_retail_declaration_split_and_ksql_order():
    """B1, B2/B4, B9, G3 on a second estate."""
    from metabridge.events.generators import generate_ksql
    cer = parse_events(str(EV / RETAIL))
    assert [d.name for d in cer.declarations] == ["returns_src"]
    assert cer.resolve_stream("returns_src") == "returns.v2"
    assert cer.inventory()["streaming_jobs"] == 2      # SMT + the job
    assert not [f for f in validate_cer(cer, "kafka")["findings"]
                if f["code"] == "TRANSFORM_INPUT_UNRESOLVED"]
    ksql = generate_ksql(cer)
    assert ksql.index("CREATE STREAM returns_src") < \
        ksql.index("FROM returns_src")
    assert "GROUP BY store_id" in ksql                 # B10


def test_retail_clean_producer_raises_no_duplicate_risk():
    """acks=all + idempotence: the opposite of the kafka fixture, so the
    finding is shown to depend on the input, not to fire unconditionally."""
    cer = parse_events(str(EV / RETAIL))
    assert cer.producers[0].acks == "all" and cer.producers[0].idempotent
    assert not [f for f in validate_cer(cer, "kafka")["findings"]
                if f["code"] == "DUPLICATE_OR_LOSS_RISK"]

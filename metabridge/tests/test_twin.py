"""Digital Twin: graph model, discovery, analytics, API."""
import json
import sys
from pathlib import Path

import pytest
import yaml

from metabridge.twin.discover import build_twin
from metabridge.twin.model import (DigitalTwin, node_id, twin_from_dict)
from metabridge.twin import analyze

ROOT = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


@pytest.fixture(scope="module")
def estate_doc():
    return yaml.safe_load((ROOT / "estate" / "estate.yml").read_text())


@pytest.fixture(scope="module")
def twin(estate_doc):
    return build_twin(
        paths=[str(ROOT / "etl_legacy" / "ssis"),
               str(ROOT / "events" / "kafka")],
        estate_docs=[estate_doc], include_connections=False)


# --- model ---------------------------------------------------------------

def test_node_merge_explicit_beats_inferred():
    t = DigitalTwin()
    t.add_node("table", "orders", "heuristic", inferred=True)
    n = t.add_node("table", "orders", "estate.yml", inferred=False,
                   owner="sales@")
    assert n.inferred is False
    assert n.owner == "sales@"
    assert n.sources == ["heuristic", "estate.yml"]
    assert len(t.nodes) == 1


def test_edges_dedupe_and_skip_dangling():
    t = DigitalTwin()
    a = t.add_node("table", "a")
    b = t.add_node("pipeline", "b")
    t.add_edge(a.id, b.id, "feeds", "s1")
    t.add_edge(a.id, b.id, "feeds", "s2")
    t.add_edge(a.id, a.id, "feeds")                 # self loop
    t.add_edge(a.id, node_id("table", "ghost"), "feeds")   # dangling
    assert len(t.edges) == 1
    assert t.edges[(a.id, b.id, "feeds")].sources == ["s1", "s2"]


def test_find_by_name_and_case():
    t = DigitalTwin()
    t.add_node("table", "Fact_Sales")
    assert t.find("fact_sales").name == "Fact_Sales"
    t.add_node("topic", "fact_sales")
    # exact name beats case-insensitive
    assert t.find("fact_sales").kind == "topic"
    # ambiguous now resolves deterministically by kind priority (table
    # before topic) instead of returning None
    assert t.find("FACT_SALES").kind == "table"
    assert t.find("topic:fact_sales") is not None   # id always works


def test_find_resolves_a_name_that_is_both_pipeline_and_table():
    """A dbt model becomes both pipeline:X and table:X. find() must
    still resolve X (a None here 404'd blast radius and silently
    dropped estate.yml facts)."""
    t = DigitalTwin()
    t.add_node("pipeline", "fct_orders")
    t.add_node("table", "fct_orders")
    got = t.find("fct_orders")
    assert got is not None and got.kind == "table"  # data facet wins
    assert {n.kind for n in t.find_all("fct_orders")} == {"pipeline",
                                                          "table"}


def test_explicit_facts_outrank_and_downgrade_heuristics():
    t = DigitalTwin()
    # heuristic sighting first
    t.add_node("table", "orders", "guess", technology="guessed_pg",
               inferred=True)
    n = t.nodes[node_id("table", "orders")]
    assert n.inferred is True
    # explicit sighting overwrites the guessed value AND clears the flag
    t.add_node("table", "orders", "parser", technology="snowflake")
    assert n.technology == "snowflake"
    assert n.inferred is False
    # a later heuristic must not re-taint a confirmed node
    t.add_node("table", "orders", "guess2", inferred=True)
    assert n.inferred is False


def test_descriptor_is_authoritative_over_parser_value():
    t = DigitalTwin()
    t.add_node("application", "shop", "parser", technology="dbt")
    t.add_node("application", "shop", "estate.yml",
               technology="Fivetran", authoritative=True)
    assert t.nodes[node_id("application", "shop")].technology == \
        "Fivetran"


def test_node_id_keeps_space_and_underscore_distinct():
    t = DigitalTwin()
    a = t.add_node("table", "order items")
    b = t.add_node("table", "order_items")
    assert a.id != b.id
    assert len(t.nodes) == 2


def test_find_strips_padded_name():
    t = DigitalTwin()
    t.add_node("table", "orders")
    assert t.find("  orders  ") is not None


def test_round_trip(twin):
    doc = twin.to_dict()
    again = twin_from_dict(doc)
    assert again.counts() == twin.counts()
    assert json.dumps(again.to_dict(), sort_keys=True) == \
        json.dumps(doc, sort_keys=True)


# --- discovery -----------------------------------------------------------

def test_discovers_all_requested_kinds(twin):
    c = twin.counts()
    for kind in ("application", "table", "pipeline", "workflow",
                 "topic", "streaming_job", "database", "domain",
                 "api", "dashboard", "data_product", "owner"):
        assert c.get(kind), "missing %s" % kind


def test_events_folder_is_not_claimed_by_pipeline_detector(twin):
    assert node_id("topic", "orders.v1") in twin.nodes
    kafka_apps = [n for n in twin.nodes.values()
                  if n.kind == "application" and "kafka" in
                  (n.technology or "")]
    assert kafka_apps


def test_stream_alias_resolves_to_topic(twin):
    # orders_per_customer reads STREAM orders_src == topic orders.v1
    job = node_id("streaming_job", "orders_per_customer")
    assert job in twin.nodes
    kinds = {(e.from_id, e.kind) for e in twin.in_edges(job)}
    assert (node_id("topic", "orders.v1"), "feeds") in kinds
    # the declaration-only stream is an alias, not a job node
    assert node_id("streaming_job", "orders_src") not in twin.nodes


def test_estate_descriptor_wins_over_inference(twin):
    fact = twin.find("fact_sales")
    assert fact.domain == "Sales"
    sales = twin.nodes[node_id("domain", "Sales")]
    assert sales.inferred is False
    assert sales.owner == "sales-data@metafordata.com"
    dash = twin.nodes[node_id("dashboard", "SalesDashboard")]
    assert dash.technology == "PowerBI"


def test_inferred_domains_require_two_members(twin):
    inferred = [n for n in twin.nodes.values()
                if n.kind == "domain" and n.inferred]
    for d in inferred:
        members = [e for e in twin.in_edges(d.id)
                   if e.kind == "belongs_to"]
        assert len(members) >= 2, d.name


def test_built_from_declares_unrecognized(tmp_path):
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "readme.bin").write_bytes(b"\x00\x01")
    t = build_twin(paths=[str(junk)], include_connections=False)
    assert any("unrecognized" in s for s in t.built_from)


# --- analytics -----------------------------------------------------------

def test_blast_radius_reaches_streaming_and_serving(twin):
    br = analyze.blast_radius(twin, "orders.v1")
    names = {a["name"] for a in br["affected"]}
    assert "orders_per_customer" in names        # streaming job
    assert "orders.enriched" in names            # derived topic
    assert {"orders-api"} <= {e["name"]
                              for e in br["business_endpoints"]}
    assert br["max_depth"] >= 1


def test_root_cause_ranks_direct_wide_upstream_first(twin):
    rc = analyze.root_cause(twin, "SalesDashboard")
    assert rc["candidates"][0]["name"] == "fact_sales"
    assert "topology" in rc["note"]


def test_impact_levels(twin):
    hi = analyze.impact_analysis(twin, "fact_sales")
    assert hi["impact_level"] in ("HIGH", "MEDIUM")
    assert hi["summary"]
    unknown = analyze.impact_analysis(twin, "no_such_node")
    assert "error" in unknown


def test_simulation_waves_and_boundary(twin):
    sim = analyze.simulate_migration(twin, technology="ssis")
    assert sim["estimated_waves"] >= 1
    all_objects = [o for w in sim["waves"] for o in w["objects"]]
    assert len(all_objects) == sim["selection_size"]
    ext = {x for w in sim["waves"]
           for x in w["external_consumers_affected"]}
    assert "fact_sales" in ext
    assert "error" in analyze.simulate_migration(twin, selection=[])


def test_simulation_cycle_collapses_into_one_wave():
    t = DigitalTwin()
    a = t.add_node("pipeline", "a", technology="x")
    b = t.add_node("pipeline", "b", technology="x")
    t.add_edge(a.id, b.id, "feeds")
    t.add_edge(b.id, a.id, "feeds")
    sim = analyze.simulate_migration(t, technology="x")
    assert sim["estimated_waves"] == 1
    assert sorted(sim["waves"][0]["objects"]) == ["a", "b"]


def test_simulation_cycle_does_not_lump_downstream_nodes():
    """A cycle a<->b feeding c must not drag c into the cycle's wave;
    c orders strictly after the knot breaks."""
    t = DigitalTwin()
    for n in ("a", "b", "c"):
        t.add_node("pipeline", n, technology="x")
    ia, ib, ic = (node_id("pipeline", n) for n in ("a", "b", "c"))
    t.add_edge(ia, ib, "feeds")
    t.add_edge(ib, ia, "feeds")
    t.add_edge(ib, ic, "feeds")
    sim = analyze.simulate_migration(t, technology="x")
    waves = [sorted(w["objects"]) for w in sim["waves"]]
    assert ["a", "b"] in waves
    assert ["c"] in waves            # c is its own later wave
    assert sim["estimated_waves"] == 2


def test_root_cause_fan_out_not_inflated_by_parallel_edges():
    """A table that both feeds and is read by a pipeline is one
    downstream neighbour, not two."""
    t = DigitalTwin()
    tbl = t.add_node("table", "lookup")
    pipe = t.add_node("pipeline", "load")
    sink = t.add_node("table", "out")
    t.add_edge(tbl.id, pipe.id, "feeds")
    t.add_edge(tbl.id, pipe.id, "reads")     # parallel edge, same pair
    t.add_edge(pipe.id, sink.id, "writes")
    rc = analyze.root_cause(t, "out")
    cand = {c["name"]: c for c in rc["candidates"]}
    assert cand["lookup"]["fan_out"] == 1


def test_capability_map_declared_vs_inferred(twin):
    m = analyze.business_capability_map(twin)
    by_name = {d["domain"]: d for d in m["domains"]}
    assert by_name["Sales"]["inferred"] is False
    assert by_name["Sales"]["owner"] == "sales-data@metafordata.com"
    for name, d in by_name.items():
        if name not in ("Sales", "Customer"):
            assert d["inferred"] is True, name


def test_inventory_and_landscape_and_flow(twin):
    inv = analyze.technology_inventory(twin)
    assert any(t["technology"] == "ssis" for t in inv["technologies"])
    land = analyze.application_landscape(twin)
    assert all(set(r) >= {"pipelines", "tables", "topics", "workflows"}
               for r in land["applications"])
    flow = analyze.data_flow_graph(twin)
    kinds = {n["kind"] for n in flow["nodes"]}
    assert kinds <= {"table", "pipeline", "topic", "streaming_job",
                     "api", "dashboard"}
    ids = {n["id"] for n in flow["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids
               for e in flow["edges"])


def test_dependency_graph_collapses_to_applications(estate_doc):
    t = build_twin(paths=[str(ROOT / "events" / "kafka")],
                   estate_docs=[estate_doc],
                   include_connections=False)
    dep = analyze.application_dependency_graph(t)
    assert {a["name"] for a in dep["applications"]}
    for d in dep["dependencies"]:
        assert d["application"] != d["depends_on"]
        assert d["via"]


# --- API -----------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None)
             for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    yield TestClient(webapp.app)
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def _files(*dirs):
    out = []
    for d in dirs:
        for f in sorted((ROOT / d).rglob("*")):
            if f.is_file():
                out.append({"name": "%s/%s" % (Path(d).name,
                                               f.relative_to(ROOT / d)),
                            "content": f.read_text(errors="replace")})
    return out


def test_twin_api_lifecycle(client):
    assert client.get("/api/twin").status_code == 404
    r = client.post("/api/twin/build", json={
        "files": _files("etl_legacy/ssis", "events/kafka"),
        "estate_yaml": (ROOT / "estate" / "estate.yml").read_text(),
        "include_connections": False, "include_jobs": False})
    assert r.status_code == 200
    d = r.json()
    assert d["twin_id"]
    assert d["counts"]["topic"] >= 6
    assert d["counts"]["application"] >= 3

    assert client.get("/api/twin").json()["counts"] == d["counts"]
    flow = client.get("/api/twin/graph?view=flow").json()
    assert {n["kind"] for n in flow["nodes"]} <= {
        "table", "pipeline", "topic", "streaming_job", "api",
        "dashboard"}

    br = client.get("/api/twin/blast-radius",
                    params={"node": "orders.v1"}).json()
    assert any(e["name"] == "orders-api"
               for e in br["business_endpoints"])
    assert client.get("/api/twin/blast-radius",
                      params={"node": "nope"}).status_code == 404

    rc = client.get("/api/twin/root-cause",
                    params={"node": "SalesDashboard"}).json()
    assert rc["candidates"][0]["name"] == "fact_sales"
    imp = client.get("/api/twin/impact",
                     params={"node": "fact_sales"}).json()
    assert imp["impact_level"] in ("HIGH", "MEDIUM", "LOW")

    sim = client.post("/api/twin/simulate",
                      json={"technology": "ssis"}).json()
    assert sim["estimated_waves"] >= 1
    assert client.post("/api/twin/simulate",
                       json={}).status_code == 422

    for path in ("dependencies", "capability-map", "inventory",
                 "landscape"):
        assert client.get("/api/twin/%s" % path).status_code == 200


def test_twin_api_rejects_bad_input_cleanly(client):
    # build something first so the read routes have a twin
    client.post("/api/twin/build", json={
        "files": _files("events/kafka"), "include_connections": False,
        "include_jobs": False})
    # malformed / non-object bodies -> 422, never 500
    r = client.post("/api/twin/build", content=b"not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert client.post("/api/twin/build", json=[1, 2]).status_code == 422
    assert client.post("/api/twin/simulate",
                       json={"selection": "orders.v1"}).status_code \
        == 422
    assert client.get("/api/twin/graph",
                      params={"view": "bogus"}).status_code == 422
    # non-dict entries in files must not crash the build
    ok = client.post("/api/twin/build", json={
        "files": [{"name": "topics.json",
                   "content": (ROOT / "events" / "kafka" /
                               "topics.json").read_text()},
                  "garbage", 42],
        "include_connections": False, "include_jobs": False})
    assert ok.status_code == 200


def test_twin_api_estate_file_in_upload(client):
    files = _files("events/kafka")
    files.append({"name": "estate.yml",
                  "content": (ROOT / "estate" /
                              "estate.yml").read_text()})
    r = client.post("/api/twin/build", json={
        "files": files, "include_connections": False,
        "include_jobs": False})
    assert r.status_code == 200
    d = r.json()
    kinds = {n["kind"] for n in d["nodes"]}
    assert {"dashboard", "api", "data_product"} <= kinds
    doms = [n for n in d["nodes"] if n["kind"] == "domain"
            and not n.get("inferred")]
    assert {"Sales", "Customer"} <= {n["name"] for n in doms}


def test_connection_analysis_table_count_does_not_crash(tmp_path):
    """Regression: a saved connection's last_analysis['tables'] is an int
    COUNT (as connections_store.record_analysis writes it), not a list of
    names — add_connections must record the count, not slice the int."""
    base = tmp_path / "iso"
    base.mkdir(parents=True, exist_ok=True)
    (base / "connections.json").write_text(json.dumps([
        {"id": "c1", "connector": "snowflake", "name": "Prod WH",
         "status": "active",
         "last_analysis": {"tables": 42, "views": 5, "database": "DB",
                           "schema": "PUBLIC", "verdict": "ok"}}]))
    t = build_twin(include_connections=True, name="e")   # must not raise
    wh = t.find("Prod WH")
    assert wh is not None and wh.metadata.get("tables") == 42

    # a real list of names/objects still expands into table nodes
    (base / "connections.json").write_text(json.dumps([
        {"id": "c2", "connector": "postgres", "name": "PG",
         "status": "active",
         "last_analysis": {"tables": ["public.orders",
                                      {"name": "public.users"}]}}]))
    t2 = build_twin(include_connections=True, name="e")
    assert t2.find("public.orders") is not None
    assert t2.find("public.users") is not None

"""Technical Debt Intelligence: deterministic detection over twin + IR."""
import json
import sys
from pathlib import Path

import pytest

from metabridge.debt.engine import (DEBT_ASSUMPTIONS, assess_from_paths,
                                    assess_technical_debt)
from metabridge.ir.model import (Pipeline, Mapping, Transformation, Port,
                                 TransformationType, LoadStrategy)
from metabridge.twin.model import DigitalTwin

ROOT = Path(__file__).resolve().parent.parent / "examples"

_CATS = ("unused_tables", "unused_columns", "dead_etl",
         "duplicate_mappings", "duplicate_sql",
         "duplicate_business_logic", "unused_dashboards",
         "broken_lineage", "orphan_datasets", "unused_apis",
         "unused_kafka_topics", "unused_process_chains")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


# --- reachability detections over a crafted twin -------------------------

def _twin_with_endpoints():
    """orders(raw)->load->fct_orders->dashboard  (all used);
    stale_stg written but read by nobody; orphan floating; a topic with
    a consumer and one without; a dashboard with no source; a workflow
    orchestrating a live pipeline and another orchestrating nothing."""
    t = DigitalTwin("estate")
    raw = t.add_node("table", "orders_raw")
    load = t.add_node("pipeline", "load_orders")
    fct = t.add_node("table", "customer_360")     # not mart-prefixed
    dash = t.add_node("dashboard", "SalesDash")
    t.add_edge(raw.id, load.id, "feeds")
    t.add_edge(load.id, fct.id, "writes")
    t.add_edge(fct.id, dash.id, "feeds")
    # dead branch: stale_stg written by dead_pipe, read by nobody
    dead = t.add_node("pipeline", "build_stale")
    stale = t.add_node("table", "stale_stg")
    t.add_edge(dead.id, stale.id, "writes")
    # orphan
    t.add_node("table", "leftover_import")
    # topics
    good = t.add_node("topic", "orders.events")
    cons = t.add_node("consumer", "billing")
    prod = t.add_node("producer", "svc")
    t.add_edge(prod.id, good.id, "produces")
    t.add_edge(good.id, cons.id, "consumes")
    # produced to but no consumer -> unused topic (not an orphan)
    aband = t.add_node("topic", "abandoned.events")
    t.add_edge(prod.id, aband.id, "produces")
    dlq = t.add_node("topic", "orders.dlq")       # DLQ excluded
    t.add_edge(prod.id, dlq.id, "produces")
    # dashboard with no source
    t.add_node("dashboard", "EmptyDash")
    # api that serves nothing and reads nothing
    t.add_node("api", "ghost-api")
    # workflows
    wf = t.add_node("workflow", "nightly")
    t.add_edge(wf.id, load.id, "orchestrates")
    t.add_node("workflow", "orphan_chain")        # orchestrates nothing
    return t


def test_reachability_detectors():
    t = _twin_with_endpoints()
    r = assess_technical_debt(t, [])
    f = r["findings"]
    names = lambda k: {x["object"] for x in f[k]}  # noqa: E731
    assert "stale_stg" in names("unused_tables")
    assert "customer_360" not in names("unused_tables")   # feeds a dash
    assert "leftover_import" in names("orphan_datasets")
    assert "abandoned.events" in names("unused_kafka_topics")
    assert "orders.events" not in names("unused_kafka_topics")
    assert "orders.dlq" not in names("unused_kafka_topics")   # DLQ exempt
    assert "EmptyDash" in names("unused_dashboards")
    assert "SalesDash" not in names("unused_dashboards")
    assert "ghost-api" in names("unused_apis")
    assert "build_stale" in names("dead_etl")
    assert "load_orders" not in names("dead_etl")
    assert "orphan_chain" in names("unused_process_chains")
    assert "nightly" not in names("unused_process_chains")


def test_broken_lineage_from_inferred_nodes():
    t = DigitalTwin("e")
    p = t.add_node("pipeline", "enrich")
    # a missing upstream recorded as an inferred placeholder table
    ghost = t.add_node("table", "missing_dim", inferred=True)
    t.add_edge(ghost.id, p.id, "feeds")
    r = assess_technical_debt(t, [])
    broken = {x["object"] for x in r["findings"]["broken_lineage"]}
    assert "missing_dim" in broken


def test_weak_mode_without_endpoints_is_declared():
    t = DigitalTwin("e")
    a = t.add_node("table", "a")
    b = t.add_node("pipeline", "load")
    t.add_edge(a.id, b.id, "feeds")           # a feeds load; load is a sink
    r = assess_technical_debt(t, [])
    assert "NO consumption endpoints" in r["coverage_note"]


# --- IR detections over crafted pipelines --------------------------------

def _dup_pipeline():
    p = Pipeline(name="proj", source_format="dbt")

    def mk(name):
        m = Mapping(name=name, load_strategy=LoadStrategy.FULL)
        m.transformations = [
            Transformation(name="SRC", type=TransformationType.SOURCE,
                           ports=[Port(name="amount", datatype="decimal")],
                           properties={"table": "raw_orders"}),
            Transformation(name="XF", type=TransformationType.EXPRESSION,
                           ports=[Port(name="net",
                                       expression="amount * 1.2 - discount")]),
            Transformation(name="TGT", type=TransformationType.TARGET,
                           ports=[Port(name="net", datatype="decimal")],
                           properties={"table": name}),
        ]
        return m
    # two structurally identical mappings (different target name only ->
    # different fingerprint) — make them truly identical target too
    m1 = mk("out_a")
    m2 = mk("out_a")            # same fingerprint as m1
    m2.name = "out_b"           # different mapping name, same graph+target
    # duplicated business logic in a third, structurally different map
    m3 = Mapping(name="report", load_strategy=LoadStrategy.FULL)
    m3.transformations = [
        Transformation(name="SRC", type=TransformationType.SOURCE,
                       ports=[Port(name="amount", datatype="decimal")],
                       properties={"table": "raw_x"}),
        Transformation(name="XF", type=TransformationType.EXPRESSION,
                       ports=[Port(name="net2",
                                   expression="amount * 1.2 - discount")]),
        Transformation(name="TGT", type=TransformationType.TARGET,
                       ports=[Port(name="net2")],
                       properties={"table": "report"}),
    ]
    # duplicate SQL override in two mappings
    sql = ("select customer_id, sum(amount) as total from raw_orders "
           "group by customer_id having sum(amount) > 1000")
    for nm in ("agg_a", "agg_b"):
        mm = Mapping(name=nm)
        mm.transformations = [
            Transformation(name="SQ",
                           type=TransformationType.SOURCE_QUALIFIER,
                           properties={"sql_override": sql}),
            Transformation(name="TGT", type=TransformationType.TARGET,
                           properties={"table": nm}),
        ]
        p.mappings.append(mm)
    p.mappings = [m1, m2, m3] + p.mappings
    return p


def test_duplicate_detectors():
    r = assess_technical_debt(DigitalTwin("e"), [_dup_pipeline()])
    f = r["findings"]
    assert f["duplicate_mappings"]          # out_a / out_b share a graph
    assert any("out_a" in d["object"] and "out_b" in d["object"]
               for d in f["duplicate_mappings"])
    assert f["duplicate_sql"]               # agg_a / agg_b same SQL
    assert f["duplicate_business_logic"]    # amount*1.2-discount reused
    logic = f["duplicate_business_logic"][0]
    assert logic["count"] >= 2


def test_data_product_member_not_unused_or_dead():
    # a curated table consumed ONLY through its data product (data-mesh
    # pattern) must not be flagged unused, nor its writer dead
    t = DigitalTwin("e")
    pipe = t.add_node("pipeline", "load_rev")
    gold = t.add_node("table", "gold_revenue")
    prod = t.add_node("data_product", "RevenueProduct")
    t.add_edge(pipe.id, gold.id, "writes")
    t.add_edge(prod.id, gold.id, "includes")
    r = assess_technical_debt(t, [])
    assert "gold_revenue" not in {x["object"]
                                  for x in r["findings"]["unused_tables"]}
    assert "load_rev" not in {x["object"]
                              for x in r["findings"]["dead_etl"]}


def test_duplicate_mappings_respects_join_and_filter_semantics():
    def joinmap(name, join_type):
        m = Mapping(name=name, load_strategy=LoadStrategy.FULL)
        m.transformations = [
            Transformation(name="A", type=TransformationType.SOURCE,
                           properties={"table": "raw.a"}),
            Transformation(name="B", type=TransformationType.SOURCE,
                           properties={"table": "raw.b"}),
            Transformation(name="J", type=TransformationType.JOINER,
                           properties={"join_type": join_type,
                                       "condition": "a.id=b.id"}),
            Transformation(name="T", type=TransformationType.TARGET,
                           properties={"table": "mart.j"},
                           ports=[Port(name="v", expression="a.v")]),
        ]
        return m
    p = Pipeline(name="p", source_format="powercenter")
    p.mappings = [joinmap("inner_load", "INNER"),
                  joinmap("full_load", "FULL")]
    r = assess_technical_debt(DigitalTwin("e"), [p])
    # INNER vs FULL into the same target are NOT duplicates
    assert not r["findings"]["duplicate_mappings"]
    # two identical INNER joins ARE duplicates
    p2 = Pipeline(name="p2", source_format="powercenter")
    p2.mappings = [joinmap("a", "INNER"), joinmap("b", "INNER")]
    r2 = assess_technical_debt(DigitalTwin("e"), [p2])
    assert r2["findings"]["duplicate_mappings"]


def test_unused_column_named_like_keyword_not_flagged():
    # a column named `count`, used only inside a FILTER condition, must
    # not be reported unused (keyword filtering used to drop it)
    p = Pipeline(name="p", source_format="dbt")
    from metabridge.ir.model import SourceTable
    m = Mapping(name="stg")
    m.transformations = [
        Transformation(name="SRC", type=TransformationType.SOURCE,
                       properties={"table": "raw"}),
        Transformation(name="F", type=TransformationType.FILTER,
                       properties={"condition": "count > 0"}),
        Transformation(name="TGT", type=TransformationType.TARGET,
                       ports=[Port(name="id")], properties={"table": "stg"}),
    ]
    p.mappings = [m]
    p.sources = [SourceTable(name="raw", columns=[
        Port(name="id"), Port(name="count"), Port(name="unused_x")])]
    r = assess_technical_debt(DigitalTwin("e"), [p])
    unused = {x["object"] for x in r["findings"]["unused_columns"]}
    assert "raw.count" not in unused        # referenced in the filter
    assert "raw.unused_x" in unused         # genuinely unused


def test_effort_reconciles_and_ratio_no_double_count():
    # labor must equal displayed hours x rate; total_objects must not
    # double-count a table that is both a twin node and a parsed source
    from metabridge.ir.model import SourceTable
    t = DigitalTwin("e")
    t.add_node("table", "raw_orders")
    p = Pipeline(name="p", source_format="dbt")
    p.sources = [SourceTable(name="raw_orders",
                             columns=[Port(name="a"), Port(name="b")])]
    p.mappings = [Mapping(name="m")]
    r = assess_technical_debt(t, [p])
    eff = r["estimated_refactoring_effort"]
    assert eff["labor_usd"] == round(
        eff["total_hours"] * DEBT_ASSUMPTIONS["blended_rate_usd_per_hour"], 0)
    # 1 twin node + 2 columns == 3 (raw_orders not counted twice)
    assert r["technical_debt_score"]["total_objects"] == 3


def test_unused_columns():
    p = Pipeline(name="proj", source_format="dbt")
    m = Mapping(name="stg")
    m.transformations = [
        Transformation(name="SRC", type=TransformationType.SOURCE,
                       properties={"table": "raw"}),
        Transformation(name="TGT", type=TransformationType.TARGET,
                       ports=[Port(name="id")], properties={"table": "stg"}),
    ]
    p.mappings = [m]
    from metabridge.ir.model import SourceTable
    p.sources = [SourceTable(name="raw", columns=[
        Port(name="id"), Port(name="never_used_col"),
        Port(name="legacy_flag")])]
    r = assess_technical_debt(DigitalTwin("e"), [p])
    unused = {x["object"] for x in r["findings"]["unused_columns"]}
    assert "raw.never_used_col" in unused
    assert "raw.legacy_flag" in unused
    assert "raw.id" not in unused           # referenced by the target port


# --- score / outputs -----------------------------------------------------

def test_score_and_outputs():
    t = _twin_with_endpoints()
    r = assess_technical_debt(t, [_dup_pipeline()])
    sc = r["technical_debt_score"]
    assert 0 <= sc["score"] <= 100
    assert sc["band"] in ("Low", "Moderate", "High", "Severe")
    assert sc["debt_objects"] == sum(v for v in sc["by_category"].values())
    assert 0 <= sc["debt_ratio"] <= 1
    assert sc["top_categories"]
    # cost / effort / roadmap / plan present and consistent
    assert r["cloud_cost_savings"]["annual_usd"] == \
        r["cloud_cost_savings"]["monthly_usd"] * 12
    eff = r["estimated_refactoring_effort"]
    assert eff["labor_usd"] == round(
        eff["total_hours"] * DEBT_ASSUMPTIONS["blended_rate_usd_per_hour"], 0)
    rm = r["prioritized_remediation_roadmap"]
    assert rm["phases"]
    assert rm["phases"][0]["risk"] == "LOW"     # quick wins first
    assert rm["total_effort_hours"] == round(
        sum(p["effort_hours"] for p in rm["phases"]), 1)
    assert r["engineering_cleanup_plan"]


def test_deterministic():
    a = assess_from_paths(paths=[str(ROOT / "dbt_retail")])
    b = assess_from_paths(paths=[str(ROOT / "dbt_retail")])
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_clean_estate_scores_low():
    # a fully-consumed tiny estate has little/no debt
    t = DigitalTwin("clean")
    raw = t.add_node("table", "raw")
    pipe = t.add_node("pipeline", "load")
    out = t.add_node("table", "fct_sales")      # mart -> intended output
    dash = t.add_node("dashboard", "Dash")
    t.add_edge(raw.id, pipe.id, "feeds")
    t.add_edge(pipe.id, out.id, "writes")
    t.add_edge(out.id, dash.id, "feeds")
    r = assess_technical_debt(t, [])
    assert r["technical_debt_score"]["score"] == 0
    assert r["technical_debt_score"]["band"] == "Low"


def test_from_paths_smoke():
    import yaml
    estate = yaml.safe_load((ROOT / "estate" / "estate.yml").read_text())
    r = assess_from_paths(
        paths=[str(ROOT / "etl_legacy" / "ssis"),
               str(ROOT / "events" / "kafka")],
        estate_docs=[estate])
    assert r["technical_debt_score"]["score"] >= 0
    assert set(r["findings"]) == set(_CATS)


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


def _build_twin(client):
    files = [{"name": "kafka/%s" % f.name,
              "content": f.read_text(errors="replace")}
             for f in (ROOT / "events" / "kafka").iterdir() if f.is_file()]
    r = client.post("/api/twin/build", json={
        "files": files,
        "estate_yaml": (ROOT / "estate" / "estate.yml").read_text(),
        "include_connections": False, "include_jobs": False})
    assert r.status_code == 200


def test_tech_debt_api(client):
    _build_twin(client)
    # analyze the workspace twin + uploaded IR
    files = [{"name": "ssis/%s" % f.name,
              "content": f.read_text(errors="replace")}
             for f in (ROOT / "etl_legacy" / "ssis").iterdir()
             if f.is_file()]
    r = client.post("/api/tech-debt", json={"files": files})
    assert r.status_code == 200
    d = r.json()
    did = d["debt_id"]
    assert 0 <= d["technical_debt_score"]["score"] <= 100
    assert len(d["exports"]) == 3
    assert set(d["findings"]) == set(_CATS)

    g = client.get("/api/tech-debt/%s" % did)
    assert g.status_code == 200
    for fmt, magic in (("pdf", b"%PDF-"), ("xlsx", b"PK"), ("json", b"{")):
        e = client.get("/api/tech-debt/%s/export?format=%s" % (did, fmt))
        assert e.status_code == 200
        assert e.content[:len(magic)] == magic
    assert client.get("/api/tech-debt/%s/export?format=exe"
                      % did).status_code == 422


def test_tech_debt_api_needs_a_source(client):
    # no workspace twin and no files -> clean 422, not 500
    r = client.post("/api/tech-debt", json={})
    assert r.status_code == 422
    assert client.post("/api/tech-debt", content=b"{bad",
                       headers={"Content-Type": "application/json"}
                       ).status_code == 422

"""End-to-end conversion tests over the example retail project."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from metabridge.engine import analyze, convert, detect_format
from metabridge.generators.dbt_generator import generate_dbt_project
from metabridge.generators.idmc_generator import generate_idmc
from metabridge.generators.powercenter_generator import generate_powercenter
from metabridge.parsers.dbt_parser import parse_dbt_project
from metabridge.parsers.idmc_parser import parse_idmc
from metabridge.parsers.powercenter_parser import parse_powercenter

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DBT_PROJECT = str(EXAMPLES / "dbt_retail")


@pytest.fixture(scope="module")
def pipeline():
    return parse_dbt_project(DBT_PROJECT)


def test_detect_format():
    assert detect_format(DBT_PROJECT) == "dbt"


def test_dbt_parse_shape(pipeline):
    names = {m.name for m in pipeline.mappings}
    assert names == {"stg_customers", "stg_orders", "customer_orders",
                     "daily_revenue", "customer_ranking"}
    assert [s.name for s in pipeline.sources] == ["raw_customers", "raw_orders"]
    order = pipeline.execution_order()
    assert order[0] == ["stg_customers", "stg_orders"]
    assert "customer_ranking" in order[-1]


def test_dbt_parse_strategies(pipeline):
    stg_orders = pipeline.mapping("stg_orders")
    assert stg_orders.load_strategy.value == "MERGE"
    assert stg_orders.unique_key == ["order_id"]


def test_native_decomposition_rate(pipeline):
    fallbacks = [m.name for m in pipeline.mappings
                 if any(i.code == "SQL_OVERRIDE_FALLBACK" for i in m.issues)]
    # only the deliberate window-function model may fall back
    assert fallbacks == ["customer_ranking"]


def test_powercenter_generation(pipeline, tmp_path):
    xml = generate_powercenter(pipeline)
    doc = ET.fromstring(xml.split("\n", 2)[2])
    assert len(list(doc.iter("MAPPING"))) == 5
    assert len(list(doc.iter("SESSION"))) == 5
    # every mapping has at least one connector into its target
    for mp in doc.iter("MAPPING"):
        tos = {c.get("TOINSTANCETYPE") for c in mp.iter("CONNECTOR")}
        assert "Target Definition" in tos, mp.get("NAME")


def test_powercenter_roundtrip_to_dbt(pipeline, tmp_path):
    xml = generate_powercenter(pipeline)
    xml_file = tmp_path / "wf.xml"
    xml_file.write_text(xml)
    p2 = parse_powercenter(str(xml_file))
    assert {m.name for m in p2.mappings} == {m.name for m in pipeline.mappings}
    assert p2.mapping("stg_orders").load_strategy.value == "MERGE"

    out = tmp_path / "dbt_out"
    generate_dbt_project(p2, str(out))
    stg_path = next(out.rglob("models/staging/**/stg_*customers.sql"))
    stg = stg_path.read_text()
    assert "{{ source(" in stg and "'raw_customers') }}" in stg
    assert "UPPER(LTRIM(RTRIM(first_name)))" in stg
    co = next(out.rglob("models/**/*customer_orders.sql")).read_text()
    assert "{{ ref('%s') }}" % stg_path.stem in co
    assert "left join" in co
    assert "group by" in co
    inc = next(out.rglob("models/staging/**/stg_*orders.sql")).read_text()
    assert "materialized='incremental'" in inc
    assert "unique_key='order_id'" in inc

    # regression: SQL overrides with `--` comments must survive the XML
    # attribute round-trip (newlines normalize to spaces on parse)
    import re
    import sqlglot
    ranking = next(out.rglob("models/**/*customer_ranking.sql")).read_text()
    stripped = re.sub(r"\{\{\s*config.*?\}\}", "", ranking, flags=re.DOTALL)
    stripped = re.sub(r"\{\{.*?\}\}", "placeholder_rel", stripped, flags=re.DOTALL)
    sqlglot.parse_one(stripped)  # raises if the override was mangled
    assert "rank()" in ranking.lower()


def test_idmc_roundtrip(pipeline, tmp_path):
    out = tmp_path / "idmc"
    generate_idmc(pipeline, str(out))
    manifest = json.loads((out / "manifest.json").read_text())
    assert len([o for o in manifest["objects"] if o["type"] == "mapping"]) == 5

    p3 = parse_idmc(str(out))
    assert {m.name for m in p3.mappings} == {m.name for m in pipeline.mappings}
    assert p3.mapping("stg_orders").load_strategy.value == "MERGE"
    # taskflow ordering recovered as dependencies
    assert set(p3.mapping("customer_orders").depends_on) >= {"stg_customers", "stg_orders"}


def test_engine_convert_and_report(tmp_path):
    report = convert(DBT_PROJECT, str(tmp_path), target_format="powercenter")
    assert report["summary"]["objects_total"] == 5
    assert report["summary"]["automated_conversion_rate"] == 100.0
    assert (tmp_path / "conversion_report.html").exists()
    assert (tmp_path / "conversion_report.json").exists()
    assert list(tmp_path.glob("wf_*.xml"))


def test_engine_analyze():
    report = analyze(DBT_PROJECT)
    assert report["target_format"] == "(analysis only)"
    assert report["summary"]["objects_total"] == 5


def test_convert_rejects_same_format(tmp_path):
    with pytest.raises(ValueError):
        convert(DBT_PROJECT, str(tmp_path), target_format="dbt")


def test_convert_selected_models_only(tmp_path):
    report = convert(DBT_PROJECT, str(tmp_path), target_format="powercenter",
                     models=["stg_orders", "daily_revenue"])
    assert report["summary"]["objects_total"] == 2
    names = {m["name"] for m in report["mappings"]}
    assert names == {"stg_orders", "daily_revenue"}
    # dependency on excluded model dropped from the DAG
    dr = next(m for m in report["mappings"] if m["name"] == "daily_revenue")
    assert dr["depends_on"] == ["stg_orders"]


def test_convert_with_load_override(tmp_path):
    report = convert(
        DBT_PROJECT, str(tmp_path), target_format="snowflake",
        models=["daily_revenue"],
        overrides={"daily_revenue": {"strategy": "incremental",
                                     "unique_key": "order_date"}})
    dr = next(m for m in report["mappings"] if m["name"] == "daily_revenue")
    assert dr["load_strategy"] == "MERGE"
    assert any(i["code"] == "LOAD_STRATEGY_OVERRIDE" for i in dr["issues"])
    stmt = next((tmp_path / "sql").glob("*daily_revenue.sql")).read_text()
    assert "MERGE INTO daily_revenue" in stmt
    assert "ON t.order_date = s.order_date" in stmt


def test_override_batch_alias_and_missing_key_warning(tmp_path):
    report = convert(
        DBT_PROJECT, str(tmp_path), target_format="snowflake",
        models=["stg_orders", "customer_orders"],
        overrides={"stg_orders": {"strategy": "batch"},
                   "customer_orders": {"strategy": "merge"}})
    so = next(m for m in report["mappings"] if m["name"] == "stg_orders")
    assert so["load_strategy"] == "FULL"
    co = next(m for m in report["mappings"] if m["name"] == "customer_orders")
    assert co["load_strategy"] == "MERGE"
    assert any(i["code"] == "MERGE_WITHOUT_KEY" for i in co["issues"])


def test_unknown_model_rejected(tmp_path):
    with pytest.raises(ValueError):
        convert(DBT_PROJECT, str(tmp_path), target_format="powercenter",
                models=["nope"])


def test_workload_coverage_clean_project(tmp_path):
    report = convert(DBT_PROJECT, str(tmp_path), target_format="powercenter")
    wl = report["summary"]["workload"]
    # clean project: coverage == object automation, no phantom queue
    assert wl["coverage_rate"] == 100.0
    assert wl["manual_queue"] == 0
    assert not (tmp_path / "manual_workbook").exists()


def test_workload_coverage_counts_project_manual_items(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "x.sql").write_text(
        "CREATE VIEW v1 AS SELECT a FROM t1;\n"
        "CALL proc_one('x');\nCALL proc_two('y');\nCALL proc_three('z');")
    out = tmp_path / "out"
    report = convert(str(src), str(out), source_format="snowflake",
                     target_format="dbt")
    wl = report["summary"]["workload"]
    assert wl["total_units"] == 4        # 1 view + 3 procedures
    assert wl["manual_queue"] == 3
    assert wl["coverage_rate"] == 25.0   # honest: not "100% automated"
    # workbook generated with skeletons + csv
    files = list((out / "manual_workbook").glob("0*.sql"))
    assert len(files) == 3
    body = files[0].read_text()
    assert "SKELETON" in body and "ORIGINAL" in body and "CHECKLIST" in body
    assert (out / "manual_queue.csv").exists()
    assert wl["workbook"]["items"] == 3

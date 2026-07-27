"""Modules 33+34: the ten realistic PowerCenter fixtures and the
end-to-end acceptance run (XML -> model -> graph -> CIR -> dbt AND
Databricks, every stage validated)."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input

TESTDATA = Path(__file__).resolve().parent.parent / "examples" / \
    "powercenter_testdata"
ALL = sorted(TESTDATA.glob("*.xml"))


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


def test_ten_fixtures_exist():
    assert len(ALL) == 10


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.stem)
def test_every_fixture_parses_without_errors(path):
    p = parse_input(str(path), "powercenter")
    assert p.mappings
    errs = [i for m in p.mappings for i in m.issues
            if i.severity.value == "ERROR"]
    errs += [i for i in p.issues if i.severity.value == "ERROR"]
    assert errs == []
    # realistic fixtures: every mapping has sources, targets and wiring
    from metabridge.ir.model import TransformationType as T
    for m in p.mappings:
        assert m.by_type(T.SOURCE), m.name
        assert m.by_type(T.TARGET), m.name
        assert m.links, m.name


def test_fixture_shapes():
    from metabridge.ir.model import LoadStrategy, TransformationType as T
    # T1: SQ -> Expression -> Filter -> Target
    m = parse_input(str(TESTDATA / "test01_sq_expression_filter.xml"),
                    "powercenter").mapping("stage_customers")
    assert m.by_type(T.FILTER) and m.by_type(T.EXPRESSION)
    # T2: two sources joined, master outer preserved
    m = parse_input(str(TESTDATA / "test02_two_sources_joiner.xml"),
                    "powercenter").mapping("join_orders")
    assert len(m.by_type(T.SOURCE)) == 2 and m.by_type(T.JOINER)
    # T3: lookup + router -> multiple targets
    m = parse_input(str(TESTDATA / "test03_lookup_router_multitarget.xml"),
                    "powercenter").mapping("route_txn")
    assert m.by_type(T.LOOKUP) and len(m.by_type(T.TARGET)) == 3
    # T4: aggregator + rank
    m = parse_input(str(TESTDATA / "test04_aggregator_rank.xml"),
                    "powercenter").mapping("top_regions")
    assert m.by_type(T.AGGREGATOR) and m.by_type(T.RANK)
    # T5/T6: SCD detection fires on the fixtures
    m = parse_input(str(TESTDATA / "test05_scd_type1.xml"),
                    "powercenter").mapping("scd1")
    assert "scd1_cir" in m.properties
    m = parse_input(str(TESTDATA / "test06_scd_type2.xml"),
                    "powercenter").mapping("scd2")
    assert "scd2_cir" in m.properties
    assert m.load_strategy == LoadStrategy.SCD2
    # T7: INSERT / UPDATE / DELETE routing
    m = parse_input(str(TESTDATA / "test07_update_strategy_iud.xml"),
                    "powercenter").mapping("order_updates")
    clauses = m.properties["merge_clauses"]
    assert set(clauses) >= {"insert", "update", "delete"}
    # T8: one mapplet reused by two mappings
    p = parse_input(str(TESTDATA / "test08_reusable_mapplet.xml"),
                    "powercenter")
    reuse = p.metadata["mapplet_reuse"]["mplt_standardize"]
    assert sorted(reuse["used_by"]) == ["clean_people_1", "clean_people_2"]
    # T9: workflow DAG with success and failure paths
    p = parse_input(str(TESTDATA / "test09_workflow_failure_paths.xml"),
                    "powercenter")
    dag = p.metadata["workflow_dags"][0]
    assert dag["success_paths"] and dag["failure_paths"]
    # T10: at least 20 transformations, realistic breadth
    m = parse_input(
        str(TESTDATA / "test10_complex_20_transformations.xml"),
        "powercenter").mapping("order_metrics")
    assert len(m.transformations) >= 20
    types = {t.type for t in m.transformations}
    assert {T.JOINER, T.LOOKUP, T.AGGREGATOR, T.FILTER, T.UNION,
            T.RANK} <= types


# ---------------------------------------------------------------------------
# module 34: end-to-end acceptance
# ---------------------------------------------------------------------------

def test_acceptance_complex_fixture(tmp_path):
    from metabridge.acceptance import run_acceptance
    r = run_acceptance(
        str(TESTDATA / "test10_complex_20_transformations.xml"),
        str(tmp_path / "acc"))
    assert r["all_stages_ok"], {k: v for k, v in r["stages"].items()
                                if not v["ok"]}
    for stage in ("xml_assets_parsed", "graph_nodes_created",
                  "connectors_preserved", "cir_generated",
                  "target_code_generated_dbt",
                  "target_code_generated_databricks",
                  "port_lineage_generated_dbt",
                  "validation_tests_generated_databricks",
                  "migration_report_generated_dbt"):
        assert r["stages"][stage]["ok"], stage
    rep = r["report"]
    assert rep["TOTAL_MAPPINGS"] == 1
    assert rep["TOTAL_TRANSFORMATIONS"] >= 20
    assert rep["AUTO_CONVERTED"] + rep["PARTIAL_CONVERTED"] + \
        rep["MANUAL_REVIEW"] + rep["FAILED"] == rep["TOTAL_MAPPINGS"]
    assert rep["FAILED"] == 0
    assert 0 <= rep["AUTOMATION_PERCENTAGE"] <= 100
    assert 0 <= rep["AVERAGE_CONFIDENCE"] <= 100
    assert (tmp_path / "acc" / "acceptance_report.json").exists()


def test_acceptance_clean_fixture_automates(tmp_path):
    from metabridge.acceptance import run_acceptance
    r = run_acceptance(str(TESTDATA / "test02_two_sources_joiner.xml"),
                       str(tmp_path / "acc"))
    assert r["all_stages_ok"]
    rep = r["report"]
    assert rep["FAILED"] == 0 and rep["MANUAL_REVIEW"] == 0
    assert rep["AUTOMATION_PERCENTAGE"] == 100.0
    assert rep["AVERAGE_CONFIDENCE"] >= 80

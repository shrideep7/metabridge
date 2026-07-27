"""Phase 3 §22/§26: end-to-end acceptance over the 21 legacy fixtures —
AST created, CIR created, target generated for 5 targets, warnings
captured, lineage + migration report generated."""
from pathlib import Path

import pytest

from metabridge.engine import convert, parse_input

LEGACY = Path(__file__).resolve().parent.parent / "examples" / "legacy_sql"
DIALECTS = ("oracle", "teradata", "sqlserver")
TARGETS = ("snowflake", "databricks", "bigquery", "postgres", "dbt")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


def test_twenty_one_fixtures_exist():
    assert sum(1 for _ in LEGACY.rglob("*.*")) == 21
    for d in DIALECTS:
        assert len(list((LEGACY / d).iterdir())) == 7


@pytest.mark.parametrize("dialect", DIALECTS)
def test_cir_created_without_errors(dialect):
    p = parse_input(str(LEGACY / dialect), dialect)
    assert p.mappings                       # CIR mappings created
    errs = [i for i in p.issues if i.severity.value == "ERROR"]
    errs += [i for m in p.mappings for i in m.issues
             if i.severity.value == "ERROR"]
    assert errs == []


def test_oracle_pipeline_shapes():
    p = parse_input(str(LEGACY / "oracle"), "oracle")
    names = {m.name for m in p.mappings}
    # NVL/DECODE/ROWNUM view converted with LIMIT and CASE
    v = p.mapping("v_active_customers")
    assert v is not None
    # CONNECT BY became a recursive CTE mapping
    assert "v_org_chart" in names
    assert any(i.code == "CONNECT_BY_TO_RECURSIVE_CTE" for i in p.issues)
    # MERGE preserved with keys
    assert p.mapping("dim_product").unique_key == ["product_id"]
    # procedures decomposed: DML extracted as a mapping
    assert "sales_summary" in names
    decos = {d["object_name"]: d
             for d in p.metadata["procedure_decompositions"]}
    assert "refresh_sales_summary" in decos
    assert decos["archive_partition"]["shape"] == "dynamic"
    assert any(i.code == "DYNAMIC_SQL" or
               ("PROCEDURAL_OBJECT" == i.code and "DYNAMIC" in
                (i.detail or "").upper())
               for i in p.issues)
    # materialized view strategy recorded
    assert any(i.code == "MATERIALIZED_VIEW" for i in p.issues)


def test_teradata_pipeline_shapes():
    p = parse_input(str(LEGACY / "teradata"), "teradata")
    # volatile chain analysed
    (chain,) = p.metadata["temp_table_chains"]
    assert chain["final"] == "fct_store_sales"
    assert chain["temp_chain"] == ["vt_raw", "vt_agg"]
    # BTEQ commands inventoried with strategies
    cmds = {c["command"] for c in p.metadata["runtime_commands"]}
    assert {".LOGON", ".IF ERRORCODE", ".IMPORT", ".EXPORT"} <= cmds
    # COLLECT STATISTICS answered with a strategy, not a shrug
    assert any(i.code == "STATISTICS_COLLECTION" for i in p.issues)
    # MULTISET + PRIMARY INDEX findings
    codes = {i.code for i in p.issues}
    assert {"MULTISET_TABLE", "PRIMARY_INDEX"} <= codes
    # macro recorded as procedural object
    assert any(u["object_type"] == "MACRO"
               for u in p.metadata["procedural_units"])


def test_sqlserver_pipeline_shapes():
    p = parse_input(str(LEGACY / "sqlserver"), "sqlserver")
    (chain,) = p.metadata["temp_table_chains"]
    assert chain["temp_chain"] == ["valid", "by_customer"]
    assert chain["final"] == "customer_totals"
    decos = {d["object_name"]: d
             for d in p.metadata["procedure_decompositions"]}
    assert decos["dbo.usp_rebuild_stats"]["shape"] == "dynamic"
    assert decos["dbo.usp_load_fct_orders"]["statement_counts"].get(
        "ERROR_HANDLING", 0) >= 1
    assert p.mapping("dim_customer").unique_key == ["customer_id"]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("target", TARGETS)
def test_convert_all_dialects_all_targets(tmp_path, dialect, target):
    r = convert(str(LEGACY / dialect), str(tmp_path / "out"),
                source_format=dialect, target_format=target,
                options={"generate_lineage": True})
    # never FAIL: declared loss is manual review, not broken output
    assert r["migration_validation"]["verdict"] != "FAIL"
    assert r["conversion_output"]["errors"]["count"] == 0
    # target code generated
    sub = "dbt" if target == "dbt" else "sql"
    assert list((tmp_path / "out" / sub).rglob("*.sql"))
    # lineage + validation + migration report generated
    assert (tmp_path / "out" / "lineage.json").exists()
    assert (tmp_path / "out" / "validation_plan.json").exists()
    assert (tmp_path / "out" / "migration_report.html").exists()


def test_qualify_transpiles_per_target(tmp_path):
    """§15: QUALIFY native on snowflake/bigquery; CTE/subquery filter
    elsewhere — never dropped."""
    for target, native in (("snowflake", True), ("postgres", False)):
        out = tmp_path / target
        convert(str(LEGACY / "teradata"), str(out),
                source_format="teradata", target_format=target)
        sql = " ".join(f.read_text()
                       for f in (out / "sql").glob("0*_v_top_stores.sql"))
        assert "rnk" in sql.lower()
        if not native:
            assert "QUALIFY" not in sql.upper()


def test_dialect_auto_detection_end_to_end(tmp_path):
    r = convert(str(LEGACY / "teradata"), str(tmp_path / "out"),
                target_format="snowflake")     # no source_format given
    assert r["source_format"] in ("teradata",)

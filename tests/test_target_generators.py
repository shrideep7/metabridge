"""Target generator engine: registry, CIR round-trip, platform-native output."""
from pathlib import Path

import pytest

from metabridge.cir.builder import build_cir
from metabridge.generators.base import (
    GENERATOR_CLASSES, get_generator, list_generators,
)
from metabridge.parsers.base import get_parser

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def retail_ir():
    return get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))


@pytest.fixture(scope="module")
def retail_cir(retail_ir):
    return build_cir(retail_ir)


# ---------------------------------------------------------------------------
# Registry + interface
# ---------------------------------------------------------------------------

def test_registry_has_all_thirteen():
    formats = {c.format_name for c in GENERATOR_CLASSES}
    assert formats == {"dbt", "powercenter", "idmc", "snowflake", "databricks",
                       "bigquery", "redshift", "synapse", "sqlserver",
                       "oracle", "postgres", "teradata", "sql"}
    assert len(list_generators()) == 13
    with pytest.raises(ValueError):
        get_generator("datastage")


# ---------------------------------------------------------------------------
# CIR in — the module contract
# ---------------------------------------------------------------------------

def test_generators_receive_cir_round_trip(retail_ir, retail_cir, tmp_path):
    """IR -> CIR -> (reverse) -> generate must equal direct generation."""
    direct = tmp_path / "direct"
    via_cir = tmp_path / "via_cir"
    from metabridge.generators.dbt_generator import generate_dbt_project
    generate_dbt_project(retail_ir, str(direct))          # flat, from IR
    from metabridge.cir.reverse import cir_to_ir
    generate_dbt_project(cir_to_ir(retail_cir), str(via_cir))
    for rel in ("models/staging/stg_customers.sql",
                "models/intermediate/int_customer_orders.sql",
                "models/staging/stg_orders.sql"):
        assert (direct / rel).read_text() == (via_cir / rel).read_text(), rel


def test_generate_accepts_cir_project(retail_cir, tmp_path):
    r = get_generator("snowflake").generate(retail_cir, str(tmp_path))
    assert r.format == "snowflake"
    assert any(f.endswith("deploy_all.sql") for f in r.files)


def test_generate_rejects_wrong_type(tmp_path):
    with pytest.raises(TypeError):
        get_generator("dbt").generate({"not": "a pipeline"}, str(tmp_path))


# ---------------------------------------------------------------------------
# dbt: complete layered project
# ---------------------------------------------------------------------------

def test_dbt_layered_project(retail_ir, tmp_path):
    r = get_generator("dbt").generate(retail_ir, str(tmp_path))
    files = set(r.files)
    assert "dbt_project.yml" in files
    assert "models/staging/sources.yml" in files
    assert "models/schema.yml" in files
    # layers by dependency position
    assert "models/staging/stg_customers.sql" in files
    assert "models/staging/stg_orders.sql" in files
    assert "models/intermediate/int_customer_orders.sql" in files   # has a dependent
    assert "models/marts/fct_customer_ranking.sql" in files     # terminal
    assert "macros/.gitkeep" in files and "tests/.gitkeep" in files
    # dependencies as ref()/source()
    co = (tmp_path / "models/intermediate/int_customer_orders.sql").read_text()
    assert "{{ ref('stg_customers') }}" in co
    stg = (tmp_path / "models/staging/stg_customers.sql").read_text()
    assert "{{ source('RAW', 'raw_customers') }}" in stg
    # staging materializes as views in project config
    proj = (tmp_path / "dbt_project.yml").read_text()
    assert "staging" in proj and "view" in proj


# ---------------------------------------------------------------------------
# Platform-native output — not generic SQL with a renamed extension
# ---------------------------------------------------------------------------

def test_databricks_delta_native(retail_ir, tmp_path):
    r = get_generator("databricks").generate(retail_ir, str(tmp_path))
    ctas = next(f for f in r.files if f.endswith("customer_orders.sql"))
    assert "USING DELTA" in (tmp_path / ctas).read_text()
    maint = (tmp_path / "90_delta_maintenance.sql").read_text()
    assert "OPTIMIZE stg_orders ZORDER BY (order_id);" in maint
    assert "partition" in maint.lower()


def test_snowflake_native_capabilities(retail_ir, tmp_path):
    get_generator("snowflake").generate(retail_ir, str(tmp_path))
    cluster = (tmp_path / "90_clustering_recommendations.sql").read_text()
    assert "ALTER TABLE stg_orders CLUSTER BY (order_id);" in cluster
    tasks = (tmp_path / "91_streams_tasks_template.sql").read_text()
    assert "CREATE OR REPLACE STREAM stg_orders_stream ON TABLE raw_orders;" in tasks
    assert "CREATE OR REPLACE TASK stg_orders_task" in tasks
    assert "SYSTEM$STREAM_HAS_DATA" in tasks


@pytest.mark.parametrize("fmt,marker", [
    ("bigquery", "PARTITION BY DATE"),
    ("redshift", "DISTKEY(order_id)"),
    ("synapse", "DISTRIBUTION = HASH(order_id)"),
    ("teradata", "PRIMARY INDEX (order_id)"),
])
def test_physical_design_recommendations(retail_ir, tmp_path, fmt, marker):
    get_generator(fmt).generate(retail_ir, str(tmp_path / fmt))
    recs = (tmp_path / fmt / "90_physical_design_recommendations.sql").read_text()
    assert marker in recs


def test_dialects_differ_for_same_input(retail_ir, tmp_path):
    """The anti-'rename the extension' guarantee."""
    get_generator("snowflake").generate(retail_ir, str(tmp_path / "sf"))
    get_generator("sqlserver").generate(retail_ir, str(tmp_path / "ms"))
    sf = next((tmp_path / "sf").glob("*customer_orders.sql")).read_text()
    ms = next((tmp_path / "ms").glob("*customer_orders.sql")).read_text()
    assert sf != ms
    assert "CREATE OR REPLACE TABLE" in sf
    assert "SELECT * INTO" in ms          # T-SQL has no CREATE OR REPLACE


# ---------------------------------------------------------------------------
# PowerCenter + IDMC contracts
# ---------------------------------------------------------------------------

def test_powercenter_generates_and_validates(retail_ir, tmp_path):
    r = get_generator("powercenter").generate(retail_ir, str(tmp_path))
    assert r.validation is not None and r.validation["ok"], r.validation
    xml = next((tmp_path).glob("wf_*.xml")).read_text()
    for element in ("MAPPING", "TRANSFORMATION", "CONNECTOR", "INSTANCE",
                    "SESSION", "WORKFLOW"):
        assert "<%s " % element in xml or "<%s>" % element in xml, element


def test_idmc_bundle_with_migration_spec(retail_ir, tmp_path):
    import json
    r = get_generator("idmc").generate(retail_ir, str(tmp_path))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["bundleType"].startswith("metabridge")
    assert "deployment" in manifest          # the migration specification
    assert any(o["type"] == "taskflow" for o in manifest["objects"])
    assert len([o for o in manifest["objects"] if o["type"] == "mapping"]) == 5

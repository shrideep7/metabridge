"""Databricks project generator (module 28): Asset Bundle structure,
job resources, Delta DDL, recommend-only optimizations."""
import json

import pytest
import yaml

from tests.test_pc_workflow import _xml as workflow_xml


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "wf.xml"
    f.write_text(workflow_xml())
    from metabridge.engine import convert
    convert(str(f), str(tmp_path / "out"),
            source_format="powercenter", target_format="databricks",
            options={"generate_lineage": True})
    return tmp_path / "out" / "databricks"


def test_bundle_structure(bundle):
    assert (bundle / "databricks.yml").exists()
    for d in ("resources/jobs", "src/sql/staging",
              "src/sql/transformations", "src/sql/marts",
              "tests", "validation", "lineage", "migration_report"):
        assert (bundle / d).is_dir(), d
    cfg = yaml.safe_load((bundle / "databricks.yml").read_text())
    assert cfg["bundle"]["name"]
    assert "dev" in cfg["targets"] and "prod" in cfg["targets"]
    assert "warehouse_id" in cfg["variables"]


def test_delta_ddl_per_source_plain(bundle):
    ddl = (bundle / "src" / "sql" / "staging" /
           "stg_src_customer.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS src_customer" in ddl
    assert "USING DELTA" in ddl
    # optimizations are NOT applied to the DDL
    assert "CLUSTER BY" not in ddl and "PARTITIONED BY" not in ddl
    assert "ZORDER" not in ddl


def test_statements_placed_by_deterministic_plan(bundle):
    marts = {p.name for p in (bundle / "src/sql/marts").glob("*.sql")}
    assert marts                        # terminal mappings land in marts
    body = next(iter(sorted(
        (bundle / "src/sql/marts").glob("*.sql")))).read_text()
    # auditability header links back to PowerCenter
    assert "-- PowerCenter mapping:" in body
    assert "-- CIR mapping:" in body


def test_job_resource_with_task_keys_and_run_if(bundle):
    spec = yaml.safe_load(
        (bundle / "resources/jobs/wf_daily.job.yml").read_text())
    job = spec["resources"]["jobs"]["wf_daily"]
    tasks = {t["task_key"]: t for t in job["tasks"]}
    acct = tasks["SESSION_ACCOUNT"]
    assert acct["depends_on"] == [{"task_key": "SESSION_CUSTOMER"}]
    assert acct["run_if"] == "ALL_SUCCESS"
    assert acct["sql_task"]["warehouse_id"] == "${var.warehouse_id}"
    assert acct["sql_task"]["file"]["path"].startswith("../src/sql/")
    assert tasks["EMAIL_FAILURE"]["run_if"] == "AT_LEAST_ONE_FAILED"


def test_recommendations_separate_and_not_applied(bundle):
    doc = json.loads((bundle / "recommendations.json").read_text())
    kinds = {r["kind"] for r in doc["optimizations"]}
    assert "liquid_clustering" in kinds
    assert "recommend-only" in doc["policy"]
    assert "Photon" in (bundle / "recommendations.md").read_text()
    # nothing from the recommendations leaked into generated statements
    for f in (bundle / "src" / "sql").rglob("*.sql"):
        body = f.read_text()
        assert "CLUSTER BY" not in body
        assert "/*+ BROADCAST" not in body


def test_conversion_artifacts_synced(bundle):
    assert (bundle / "validation" / "tests.json").exists()
    assert (bundle / "lineage" / "lineage.json").exists()
    assert (bundle / "migration_report" / "migration_report.html").exists()
    assert (bundle / "tests" / "README.md").exists()

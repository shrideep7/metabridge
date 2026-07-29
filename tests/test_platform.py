"""Tests for the platform layer: connectors, governance, scaffold."""
import json
from pathlib import Path

import pytest
import yaml

from metabridge.connectors.base import get_registry
from metabridge.connectors.emit import (
    dbt_profile, idmc_connection, powercenter_connection, split_secrets,
)
from metabridge.governance.engine import (
    classify_pipeline, evaluate_policy, govern, load_policy,
)
from metabridge.parsers.dbt_parser import parse_dbt_project
from metabridge.scaffold import scaffold

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------

def test_marketplace_has_core_coverage():
    reg = get_registry()
    keys = {s.key for s in reg.all()}
    assert {"snowflake", "bigquery", "databricks", "redshift", "synapse",
            "oracle", "sqlserver", "postgres", "teradata", "db2",
            "sap_hana", "sap_s4", "sap_bw", "salesforce", "servicenow"} <= keys
    assert len(reg.by_category("sap")) == 7   # 3 live + 4 import
    assert all(s.idmc_type for s in reg.all()
               if s.category not in ("etl", "orchestration", "events")
               and s.key not in ("sap_ecc", "sap_bw4", "sap_datasphere",
                                 "sap_di"))
    # ETL/orchestration/SAP-import connectors are import/export based


def test_secrets_never_appear_in_artifacts():
    reg = get_registry()
    sf = reg.get("snowflake")
    params = {"account": "acme-eu1", "user": "svc", "password": "SUPERSECRET",
              "warehouse": "WH", "database": "DB"}
    profile = dbt_profile(sf, params, "acme")
    conn = json.dumps(idmc_connection(sf, params, "conn_sf"))
    cmd = powercenter_connection(sf, params, "conn_sf")
    for artifact in (profile, conn, cmd):
        assert "SUPERSECRET" not in artifact
    assert "MB_SNOWFLAKE_PASSWORD" in profile  # env_var reference, YAML-quoted
    assert yaml.safe_load(profile)["acme"]["outputs"]["prod"]["password"] == \
        "{{ env_var('MB_SNOWFLAKE_PASSWORD') }}"
    safe, secrets = split_secrets(sf, params)
    assert secrets == {"MB_SNOWFLAKE_PASSWORD": "SUPERSECRET"}
    assert "password" not in safe


def test_dbt_profile_requires_adapter():
    reg = get_registry()
    with pytest.raises(ValueError):
        dbt_profile(reg.get("sap_s4"), {}, "x")


def test_sap_type_map():
    reg = get_registry()
    s4 = reg.get("sap_s4")
    assert s4.type_map["dats"] == "date"
    assert s4.type_map["curr"] == "decimal"


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def retail_pipeline():
    return parse_dbt_project(str(EXAMPLES / "dbt_retail"))


def test_classification_finds_pii(retail_pipeline):
    cls = classify_pipeline(retail_pipeline)
    cats = {c.category for c in cls}
    assert "pii.direct.email" in cats
    assert "pii.direct.name" in cats
    emails = [c for c in cls if c.category == "pii.direct.email"]
    assert any(c.node_kind == "target" for c in emails)


def test_residency_violation_us_target(retail_pipeline):
    cls = classify_pipeline(retail_pipeline)
    findings = evaluate_policy(retail_pipeline, cls, load_policy(),
                               target_region="us")
    violations = [f for f in findings if f.severity == "VIOLATION"
                  and f.code == "RESIDENCY"]
    assert violations, "EU-only PII landing in US must violate the baseline policy"


def test_residency_ok_eu_target(retail_pipeline):
    cls = classify_pipeline(retail_pipeline)
    findings = evaluate_policy(retail_pipeline, cls, load_policy(),
                               target_region="eu")
    assert not [f for f in findings if f.code == "RESIDENCY"]


def test_govern_full_run(retail_pipeline):
    result = govern(retail_pipeline, target_region="us", source_region="eu")
    assert result["summary"]["classified_columns"] > 0
    assert result["summary"]["violations"] > 0
    reg = result["processing_register"]
    assert all(a["cross_border_transfer"] for a in reg)
    assert any("pii.direct.email" in a["data_categories"] for a in reg)


def test_custom_policy_masking(retail_pipeline, tmp_path):
    policy = {"residency": {"rules": []},
              "masking": [{"match": "pii.direct.email", "require": "hash"}]}
    pf = tmp_path / "policy.yml"
    pf.write_text(yaml.safe_dump(policy))
    cls = classify_pipeline(retail_pipeline)
    findings = evaluate_policy(retail_pipeline, cls, load_policy(str(pf)), "eu")
    assert any(f.code == "MASKING_REQUIRED" for f in findings)


# ---------------------------------------------------------------------------
# Scaffold (SAP -> Snowflake end-to-end)
# ---------------------------------------------------------------------------

def test_scaffold_sap_to_snowflake(tmp_path):
    report = scaffold("sap_s4", "snowflake",
                      str(EXAMPLES / "sap_to_snowflake" / "tables.yml"),
                      str(tmp_path), source_region="eu", target_region="eu")
    assert report["summary"]["objects_total"] == 3

    # dbt side
    dbt_dir = tmp_path / "dbt"
    orders = (dbt_dir / "models" / "staging" / "stg_sales_orders.sql").read_text()
    assert "materialized='incremental'" in orders
    assert "unique_key='VBELN'" in orders
    assert "{{ var('LAST_RUN_TS') }}" in orders
    assert (dbt_dir / "profiles.yml").exists()
    profile = (dbt_dir / "profiles.yml").read_text()
    assert "snowflake" in profile

    # Informatica side
    assert (tmp_path / "idmc" / "manifest.json").exists()
    assert list(tmp_path.glob("wf_*.xml"))

    # connections with no secrets
    conns = list((tmp_path / "connections").iterdir())
    assert len(conns) == 3

    # governance caught SAP customer-master PII
    gov = json.loads((tmp_path / "governance_report.json").read_text())
    cats = {c["category"] for c in gov["classifications"]}
    assert "pii.direct.email" in cats     # SMTP_ADDR
    assert "pii.direct.phone" in cats     # TELF1
    assert gov["summary"]["violations"] == 0  # eu -> eu is compliant


def test_scaffold_us_target_flags_violations(tmp_path):
    report = scaffold("sap_s4", "snowflake",
                      str(EXAMPLES / "sap_to_snowflake" / "tables.yml"),
                      str(tmp_path), source_region="eu", target_region="us")
    assert report["governance"]["violations"] > 0


def test_scaffold_unknown_connector(tmp_path):
    with pytest.raises(ValueError):
        scaffold("nope", "snowflake",
                 str(EXAMPLES / "sap_to_snowflake" / "tables.yml"), str(tmp_path))


def test_connector_capability_metadata():
    """Marketplace redesign: capabilities/platform_type/deployment_model
    are first-class metadata — dialects are never capabilities."""
    from metabridge.connectors.base import get_registry
    sf = get_registry().get("snowflake").to_dict()
    assert sf["platform_type"] == "cloud_warehouse"
    assert sf["deployment_model"] == "cloud"
    caps = sf["capabilities"]
    assert {"pipeline_scaffold", "dbt", "idmc", "powercenter",
            "sql_modernization", "metadata_analysis", "lineage",
            "bidirectional"} <= set(caps)
    assert "snowflake" not in caps          # dialect is not a capability
    sap = get_registry().get("sap_s4").to_dict()
    assert sap["platform_type"] == "erp"
    ora = get_registry().get("oracle").to_dict()
    assert ora["platform_type"] == "rdbms"


def test_analysis_recorded_moves_connection_to_analyzed(tmp_path,
                                                        monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    from metabridge import connections_store as cs
    row = cs.save_connection("snowflake", {"account": "a", "user": "u"})
    cs.record_analysis(row["id"], {"tables": 31, "views": 2,
                                   "total_rows": 1000, "verdict": "READY",
                                   "database": "SF_SAMPLES_DB",
                                   "schema": "PUBLIC"})
    (listed,) = cs.list_connections()
    assert listed["last_analysis"]["tables"] == 31
    assert listed["last_analysis"]["database"] == "SF_SAMPLES_DB"

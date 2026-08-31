"""Tests for the platform layer: connectors, governance, scaffold."""
import json
import sys
from pathlib import Path

import pytest
import yaml

from metabridge.connectors.base import get_registry
from metabridge.connectors.emit import (
    dbt_profile, idmc_connection, powercenter_connection, split_secrets,
)
from metabridge.governance.engine import (
    DEFAULT_POLICY, POLICY_FINDING_CODES, RULES, classify_pipeline,
    evaluate_policy, govern, load_policy, policy_coverage,
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


# --- the policy document itself (GET /api/governance/policy) --------------

def test_policy_coverage_uses_the_evaluators_matcher():
    r"""Coverage must agree with evaluate_policy about what a glob reaches.

    `pii.*` compiles to `pii\..*`, so it reaches every dotted pii category and
    nothing outside pii. Asserting the counts rather than a hardcoded list
    keeps this honest if the taxonomy grows.
    """
    cov = {c["category"]: c for c in policy_coverage(DEFAULT_POLICY)}
    assert cov.keys() == {r.category for r in RULES}, "one row per category"
    assert all(cov[c]["residency"] for c in cov if c.startswith("pii.")),         "residency rule pii.* must reach every pii category"
    assert cov["pii.gov_id.ssn"]["masking"] is True
    assert cov["pii.direct.email"]["masking"] is False


def test_policy_coverage_reports_ungoverned_categories():
    """The baseline classifies financial data but governs almost none of it.

    No residency rule matches any `financial.*` category, so an account number
    can land in any region and produce no finding at all. This is a real gap in
    DEFAULT_POLICY, not a test artefact — if someone adds a financial residency
    rule this test should be updated, not deleted.
    """
    cov = {c["category"]: c for c in policy_coverage(DEFAULT_POLICY)}
    assert not any(cov[c]["residency"] for c in cov if c.startswith("financial."))
    ungoverned = [c for c in cov
                  if not cov[c]["residency"] and not cov[c]["masking"]]
    assert set(ungoverned) == {"financial.account", "financial.salary"}


def test_policy_coverage_follows_the_policy_given():
    """Coverage is computed from the argument, never from the baseline."""
    cov = {c["category"]: c
           for c in policy_coverage({"residency": {"rules": []},
                                     "masking": [{"match": "financial.*",
                                                  "require": "hash"}]})}
    assert cov["financial.account"]["masking"] is True
    assert cov["pii.gov_id.ssn"]["masking"] is False
    assert not any(v["residency"] for v in cov.values())


def test_policy_coverage_tolerates_an_empty_policy():
    cov = policy_coverage({})
    assert cov and not any(c["residency"] or c["masking"] for c in cov)


def test_govern_records_the_policy_it_applied(retail_pipeline):
    """A finding is only auditable if the rules that produced it travel with it."""
    result = govern(retail_pipeline, target_region="us")
    assert result["policy"]["residency"]["rules"], "policy must be in the report"
    assert any(f["code"] == "RESIDENCY" for f in result["policy_findings"])


def test_govern_does_not_alias_the_default_policy(retail_pipeline):
    """Mutating one report's policy must not rewrite the baseline."""
    result = govern(retail_pipeline, target_region="eu")
    result["policy"]["masking"].append({"match": "oops", "require": "nope"})
    assert not any(m["match"] == "oops" for m in DEFAULT_POLICY["masking"])
    assert not any(m["match"] == "oops"
                   for m in govern(retail_pipeline)["policy"]["masking"])


def test_govern_report_policy_survives_json(retail_pipeline):
    result = govern(retail_pipeline, target_region="us")
    assert json.loads(json.dumps(result))["policy"] == result["policy"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None)
             for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    c = TestClient(webapp.app)
    c.post("/auth/signup", json={"email": "o@x.com", "password": "Pw123456!",
                                 "name": "Owner"})
    yield c
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def test_api_governance_policy_shape(client):
    d = client.get("/api/governance/policy").json()
    assert d["source"] == "built-in"
    assert d["editable"] is False
    assert d["policy"]["residency"]["rules"]
    assert d["policy"]["masking"]
    assert [c["code"] for c in d["codes"]] ==         [c["code"] for c in POLICY_FINDING_CODES]


def test_api_governance_policy_yaml_is_the_real_document(client):
    """The viewer renders `yaml`; it must be the policy, not a lookalike."""
    d = client.get("/api/governance/policy").json()
    assert yaml.safe_load(d["yaml"]) == d["policy"]


def test_api_governance_policy_reports_the_coverage_gap(client):
    d = client.get("/api/governance/policy").json()
    cov = d["coverage"]
    assert set(cov["ungoverned"]) == {"financial.account", "financial.salary"}
    assert cov["categories"] == policy_coverage(DEFAULT_POLICY)


def test_api_governance_policy_is_not_the_agent_thresholds(client):
    """The bug this endpoint exists to fix: /api/agents' `governance` field is
    two numbers, and was the only thing a client could previously mistake for
    the policy document."""
    pol = client.get("/api/governance/policy").json()["policy"]
    assert "approval_threshold" not in pol and "deny_floor" not in pol
    agents = client.get("/api/agents").json()["governance"]
    assert "residency" not in agents and "masking" not in agents


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

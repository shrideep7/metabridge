"""Command 7 E2E: SAP -> all targets, artifacts, process-chain COR, API."""
import json
import sys
from pathlib import Path

import pytest

from metabridge.engine import convert
from metabridge.sap.artifacts import (
    business_documentation, business_lineage, validation_pack,
)
from metabridge.sap.parsers import parse_sap

SAP = Path(__file__).resolve().parent.parent / "examples" / "sap_landscape"
TARGETS = ("dbt", "snowflake", "databricks", "bigquery", "redshift",
           "synapse", "postgres", "idmc", "powercenter")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


@pytest.mark.parametrize("target", TARGETS)
def test_sap_converts_to_every_target(tmp_path, target):
    out = tmp_path / target
    r = convert(str(SAP), str(out), "sap", target)
    assert r["summary"]["status_counts"]["FAILED"] == 0
    assert r["summary"]["objects_total"] >= 7


def test_validation_pack_is_target_native(tmp_path):
    land = parse_sap(str(SAP))
    pack = validation_pack(land, "bigquery")
    assert {"reconciliation.sql", "master_data_validation.sql",
            "hierarchy_validation.sql", "currency_validation.sql",
            "unit_validation.sql",
            "business_rule_validation.sql"} <= set(pack)
    assert "rows_per_currency" in pack["currency_validation.sql"]
    assert "orphan_nodes" in pack["hierarchy_validation.sql"]
    assert "missing_texts" in pack["master_data_validation.sql"]


def test_business_documentation_content():
    land = parse_sap(str(SAP))
    doc = business_documentation(land)
    assert "TR_SALES" in doc and "ABAP routine" in doc
    assert "row-level security" in doc          # authorizations section
    assert "BEx queries" in doc and "Process chains" in doc


def test_business_lineage_edges():
    lin = business_lineage(parse_sap(str(SAP)))
    kinds = {}
    for e in lin["business_lineage"]:
        kinds.setdefault((e["from"], e["to"]), set()).add(e["kind"])
    assert {"bw_transformation", "dtp"} <= \
        kinds[("ZSALES_RAW", "ZSALES_CLEAN")]
    assert ("ZSALES_CLEAN", "ZCP_SALES") in kinds       # composite
    assert "bex_query" in kinds[("ZCP_SALES", "ZQ_SALES_BY_REGION")]
    assert lin["hierarchy_lineage"]
    assert "PC_SALES_DAILY" in lin["process_chain_lineage"]


def test_process_chain_modernizes_via_cor(tmp_path):
    from metabridge.orchestration.generators import generate_orchestration
    from metabridge.orchestration.parsers import parse_orchestration
    cor = parse_orchestration(str(SAP), "sap")
    assert [w.name for w in cor.workflows] == ["PC_SALES_DAILY"]
    wf = cor.workflows[0]
    assert ("RUN_DTP", "NOTIFY") in [
        (d.from_task, d.to_task) for d in wf.dependencies
        if d.kind == "failure"]
    for target in ("airflow", "adf", "idmc_taskflow", "powercenter"):
        m = generate_orchestration(cor, target, str(tmp_path / target))
        assert m["files"]


# --- API -------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None) for m in ("web.app", "web.auth",
                                                   "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    yield TestClient(webapp.app)
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def _files():
    return [{"name": f.name, "content": f.read_text(errors="replace")}
            for f in SAP.iterdir() if f.is_file()]


def test_sap_api_flow(client):
    r = client.post("/api/sap/analyze", json={"files": _files()})
    assert r.status_code == 200
    d = r.json()
    assert d["detected_platform"] == "bw4hana"
    assert "0CUSTOMER" in d["business_objects"]
    assert "2LIS_11_VAHDR" in d["extractors"]
    assert "ZCP_SALES" in d["infoproviders"]
    assert d["automation_score"] is not None
    assert d["semantic_confidence"] is not None
    assert d["manual_review_items"] >= 2
    assert d["business_lineage"]["business_lineage"]

    c = client.post("/api/sap/convert", json={
        "files": _files(), "target_format": "snowflake"})
    assert c.status_code == 200
    body = c.json()
    jid = body.get("job_id") or body.get("id")
    assert "sap_business_documentation.md" in body.get("sap_artifacts", [])

    v = client.post("/api/sap/validate", json={"migration_id": jid})
    assert v.status_code == 200
    assert v.json()["verdict"] != "FAIL"

    ln = client.get("/api/sap/%s/lineage" % jid)
    assert ln.status_code == 200
    assert ln.json()["business_lineage"]["business_lineage"]

    rp = client.get("/api/sap/%s/report" % jid)
    assert rp.status_code == 200


def test_marketplace_lists_sap_connectors(client):
    d = client.get("/api/v1/connectors").json()
    keys = {c["key"] for c in d["connectors"]}
    assert {"sap_ecc", "sap_s4", "sap_bw", "sap_bw4", "sap_hana",
            "sap_datasphere", "sap_di"} <= keys
    spec = next(c for c in d["connectors"] if c["key"] == "sap_bw4")
    assert {"metadata_analysis", "business_lineage", "pipeline_scaffold",
            "modernization", "validation",
            "ai_review"} <= set(spec["capabilities"])

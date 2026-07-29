"""Command 5: /api/etl/* endpoints — analyze, convert, validate, lineage,
report over an SSIS project."""
import sys
from pathlib import Path

import pytest

ETL = Path(__file__).resolve().parent.parent / "examples" / "etl_legacy"


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


def _files(platform: str):
    return [{"name": f.name, "content": f.read_text(errors="replace")}
            for f in (ETL / platform).rglob("*") if f.is_file()
            and f.suffix != ".mp"]      # binary .mp can't ride inline JSON


def test_analyze_reports_inventory(client):
    r = client.post("/api/etl/analyze",
                    json={"files": _files("ssis"), "source_format": "auto"})
    assert r.status_code == 200
    d = r.json()
    assert d["detected_platform"] == "ssis"
    assert "loadsales_dft_loadsales" in d["pipelines"]
    assert d["transformations"] >= 10
    assert set(d["workflows"]) == {"LoadSales", "MasterLoad"}
    assert d["automation_score"] is not None
    assert d["semantic_confidence"] is not None
    assert d["manual_review_items"] >= 1          # the script task
    assert any(i["code"] == "SSIS_SCRIPT_TASK"
               for i in d["unsupported_items"])


def test_analyze_rejects_non_etl(client):
    r = client.post("/api/etl/analyze", json={
        "files": [{"name": "model.sql", "content": "select 1 as x"}]})
    assert r.status_code == 422


def test_convert_validate_lineage_report(client):
    r = client.post("/api/etl/convert", json={
        "files": _files("datastage"), "target_format": "snowflake"})
    assert r.status_code == 200
    body = r.json()
    jid = body.get("job_id") or body.get("id")
    assert jid and body["detected_source"] == "datastage"

    v = client.post("/api/etl/validate", json={"migration_id": jid})
    assert v.status_code == 200
    assert v.json()["verdict"] in ("PASS", "PASS_WITH_WARNINGS",
                                   "MANUAL_REVIEW")   # FAIL is a defect

    ln = client.get("/api/etl/%s/lineage" % jid)
    assert ln.status_code == 200
    assert ln.json()["source_platform"] == "datastage"

    rp = client.get("/api/etl/%s/report" % jid)
    assert rp.status_code == 200


def test_talend_convert_via_api(client):
    r = client.post("/api/etl/convert", json={
        "files": _files("talend"), "source_format": "talend",
        "target_format": "dbt"})
    assert r.status_code == 200
    assert r.json()["conversion_status"] != "FAILED"


def test_marketplace_lists_etl_connectors(client):
    d = client.get("/api/v1/connectors").json()
    keys = {c["key"] for c in d["connectors"]}
    assert {"ssis", "datastage", "talend", "abinitio"} <= keys
    spec = next(c for c in d["connectors"] if c["key"] == "ssis")
    assert spec["platform_type"] == "legacy_etl"
    assert {"metadata_analysis", "pipeline_scaffold", "lineage",
            "modernization", "validation"} <= set(spec["capabilities"])

"""Command 6: /api/orchestration/* endpoints + marketplace registration."""
import sys
from pathlib import Path

import pytest

ORCH = Path(__file__).resolve().parent.parent / "examples" / "orchestration"


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


def _files(folder: str):
    return [{"name": f.name, "content": f.read_text()}
            for f in (ORCH / folder).iterdir() if f.is_file()]


def test_full_orchestration_flow(client):
    r = client.post("/api/orchestration/analyze",
                    json={"files": _files("adf")})
    assert r.status_code == 200
    d = r.json()
    oid = d["orchestration_id"]
    assert d["detected_platform"] == "adf"
    assert d["automation_score"] > 0
    assert d["validation_verdict"] in ("PASS", "PASS_WITH_WARNINGS",
                                       "MANUAL_REVIEW")

    g = client.get("/api/orchestration/%s/graph" % oid).json()
    wf = "pl_daily_sales"
    assert wf in g["workflows"]
    assert g["workflows"][wf]["mermaid"].startswith("flowchart")
    assert "<graphml" in g["workflows"][wf]["graphml"]

    ln = client.get("/api/orchestration/%s/lineage" % oid).json()
    assert {"from": "pl_daily_sales", "to": "pl_publish_marts",
            "via": "Publish", "resolved": True} in ln["workflow_lineage"]

    v = client.post("/api/orchestration/validate",
                    json={"orchestration_id": oid})
    assert v.status_code == 200 and "findings" in v.json()

    rev = client.post("/api/orchestration/review",
                      json={"orchestration_id": oid, "ai": False}).json()
    assert rev["engine"] == "rules"
    assert "never overwrites" in rev["note"]

    conv = client.post("/api/orchestration/convert",
                       json={"orchestration_id": oid,
                             "target": "stepfunctions"}).json()
    assert conv["generated"] and conv["target"] == "stepfunctions"

    rep = client.get("/api/orchestration/%s/report" % oid).json()
    assert rep["intelligence"]["automation_score"] > 0
    md = client.get("/api/orchestration/%s/report?format=md" % oid)
    assert md.status_code == 200 and "Execution order" in md.text


def test_dependency_editing_revalidates(client):
    r = client.post("/api/orchestration/analyze",
                    json={"files": _files("autosys")})
    oid = r.json()["orchestration_id"]
    wf = r.json()["workflows"][0]["name"]
    # adding a cycle must surface as FAIL
    e = client.post("/api/orchestration/%s/dependencies" % oid, json={
        "workflow": wf,
        "add": [{"from": "WH_LOAD", "to": "WH_EXTRACT",
                 "kind": "success"}]})
    assert e.status_code == 200
    assert e.json()["validation_verdict"] == "FAIL"
    # removing it heals the graph
    e2 = client.post("/api/orchestration/%s/dependencies" % oid, json={
        "workflow": wf,
        "remove": [{"from": "WH_LOAD", "to": "WH_EXTRACT"}]})
    assert e2.json()["validation_verdict"] != "FAIL"
    # unknown task rejected
    bad = client.post("/api/orchestration/%s/dependencies" % oid, json={
        "workflow": wf, "add": [{"from": "nope", "to": "WH_LOAD"}]})
    assert bad.status_code == 422


def test_bad_platform_and_target(client):
    r = client.post("/api/orchestration/analyze", json={
        "files": [{"name": "x.bin", "content": "gibberish"}]})
    assert r.status_code == 422
    r2 = client.post("/api/orchestration/convert", json={
        "files": _files("cron"), "target": "not_a_target"})
    assert r2.status_code == 422


def test_marketplace_lists_orchestration_connectors(client):
    d = client.get("/api/v1/connectors").json()
    keys = {c["key"] for c in d["connectors"]}
    assert {"airflow", "adf", "fabric", "stepfunctions", "controlm",
            "autosys", "pc_workflow", "idmc_taskflow"} <= keys
    spec = next(c for c in d["connectors"] if c["key"] == "airflow")
    assert spec["platform_type"] == "orchestration"
    assert {"metadata_analysis", "workflow_visualization",
            "pipeline_scaffold", "execution_graph", "modernization",
            "validation"} <= set(spec["capabilities"])

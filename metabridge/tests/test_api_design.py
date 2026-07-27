"""API design (module 18): detect/analyze/convert/validate/review +
/api/migrations resource, exercised over HTTP with the spec's JSON contract."""
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import pytest

# isolate the app's data dir BEFORE importing it (module-level state)
os.environ["METABRIDGE_DATA_DIR"] = tempfile.mkdtemp(prefix="mb_api_")

from fastapi.testclient import TestClient  # noqa: E402

from web.app import app  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
client = TestClient(app)


def _zip_project(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in path.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(path))
    return buf.getvalue()


@pytest.fixture(scope="module")
def retail_zip():
    return _zip_project(EXAMPLES / "dbt_retail")


@pytest.fixture(scope="module")
def project_id(retail_zip):
    """An uploaded project, stored server-side (analyze job)."""
    r = client.post("/api/analyze",
                    files={"file": ("retail.zip", retail_zip,
                                    "application/zip")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture(scope="module")
def migration(project_id):
    """A migration created with the spec's exact JSON conversion request."""
    r = client.post("/api/convert", json={
        "source_format": "auto",
        "target_format": "databricks",
        "project_id": project_id,
        "options": {"generate_tests": True, "generate_docs": True,
                    "generate_lineage": True, "ai_review": True},
    })
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# POST /api/detect
# ---------------------------------------------------------------------------

def test_detect_upload(retail_zip):
    r = client.post("/api/detect",
                    files={"file": ("retail.zip", retail_zip,
                                    "application/zip")})
    assert r.status_code == 200
    d = r.json()
    assert d["detected_format"] == "dbt"
    assert d["confidence_score"] > 0.5


def test_detect_by_project_id(project_id):
    r = client.post("/api/detect", json={"project_id": project_id})
    assert r.status_code == 200
    assert r.json()["detected_format"] == "dbt"


# ---------------------------------------------------------------------------
# POST /api/convert — the spec's JSON contract
# ---------------------------------------------------------------------------

def test_convert_json_contract(migration):
    assert migration["migration_id"]
    assert migration["status"] == "done"
    assert migration["target_format"] == "databricks"
    assert migration["source_format"] == "dbt"      # "auto" was detected
    assert migration["migration_url"].startswith("/api/migrations/")


def test_options_were_honored(migration):
    out = (Path(os.environ["METABRIDGE_DATA_DIR"]) / "jobs" /
           migration["migration_id"] / "output")
    assert (out / "validation_tests" / "tests.json").exists()   # tests
    assert (out / "lineage.json").exists()                      # lineage
    assert (out / "pipeline_documentation.md").exists()         # docs
    assert (out / "ai_review" / "review.json").exists()         # review


def test_convert_form_still_works(retail_zip):
    r = client.post("/api/convert",
                    files={"file": ("retail.zip", retail_zip,
                                    "application/zip")},
                    data={"target": "powercenter"})
    assert r.status_code == 200, r.text
    assert r.json()["target_format"] == "powercenter"


def test_convert_json_requires_project(migration):
    r = client.post("/api/convert", json={"source_format": "auto",
                                          "target_format": "databricks"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/migrations/{migration_id} (+ /lineage + /report)
# ---------------------------------------------------------------------------

def test_get_migration(migration):
    r = client.get("/api/migrations/%s" % migration["migration_id"])
    assert r.status_code == 200
    m = r.json()
    assert m["migration_id"] == migration["migration_id"]
    e = m["executive_summary"]
    assert e["target"] == "Databricks"
    assert e["mappings_analysed"] == 5
    assert set(m["links"]) == {"report", "lineage", "validate", "review",
                               "download"}


def test_non_migration_job_is_404(project_id):
    r = client.get("/api/migrations/%s" % project_id)   # an analyze job
    assert r.status_code == 404
    assert "Not a migration" in r.json()["detail"]


def test_migration_lineage(migration):
    r = client.get("/api/migrations/%s/lineage" % migration["migration_id"])
    assert r.status_code == 200
    doc = r.json()
    assert doc["table_lineage"]["edges"]
    assert doc["mermaid"]["table_lineage"].startswith("graph LR")


def test_migration_report_formats(migration):
    mid = migration["migration_id"]
    j = client.get("/api/migrations/%s/report" % mid)
    assert j.status_code == 200
    assert len(j.json()["sections_order"]) == 15
    h = client.get("/api/migrations/%s/report?format=html" % mid)
    assert h.status_code == 200
    assert h.text.startswith("<!doctype html>")
    md = client.get("/api/migrations/%s/report?format=md" % mid)
    assert md.status_code == 200
    assert "Executive Summary" in md.text


# ---------------------------------------------------------------------------
# POST /api/validate + POST /api/review
# ---------------------------------------------------------------------------

def test_validate_returns_stored_verdict(migration):
    r = client.post("/api/validate",
                    json={"migration_id": migration["migration_id"]})
    assert r.status_code == 200
    v = r.json()
    assert v["verdict"] in ("PASS", "PASS_WITH_WARNINGS", "MANUAL_REVIEW",
                            "FAIL")
    assert len(v["layers"]) == 5


def test_validate_requires_migration_id():
    assert client.post("/api/validate", json={}).status_code == 422


def test_review_runs_and_is_propose_only(migration):
    r = client.post("/api/review",
                    json={"migration_id": migration["migration_id"],
                          "ai": False})
    assert r.status_code == 200
    doc = r.json()
    assert doc["summary"]["corrections_proposed"] == 0   # rules fallback
    assert "approval" in doc["summary"]["approval_required"].lower() or \
        "approve" in doc["summary"]["approval_required"].lower()


def test_review_apply_unknown_id(migration):
    r = client.post("/api/review",
                    json={"migration_id": migration["migration_id"],
                          "approve": ["ghost~1"]})
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "unknown_id"


# ---------------------------------------------------------------------------
# conversion output contract on the wire (module 19)
# ---------------------------------------------------------------------------

def test_response_carries_conversion_output_contract(migration):
    for field in ("migration_id", "detected_source", "target_format",
                  "conversion_status", "complexity_score",
                  "conversion_confidence", "automation_percentage",
                  "converted_assets", "manual_review_assets", "warnings",
                  "errors", "lineage", "validation_summary",
                  "output_package"):
        assert field in migration, field
    # web layer stamps the job id and live links into the contract
    assert migration["migration_id"] == migration["id"]
    assert migration["output_package"]["download_url"].endswith("/download")
    assert migration["lineage"]["document"].startswith("/api/migrations/")

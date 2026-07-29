"""Phase 3 §23: /api/legacy-sql/* endpoints."""
import json
import os
import tempfile

os.environ.setdefault("METABRIDGE_DATA_DIR",
                      tempfile.mkdtemp(prefix="mb_legacy_api_"))

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web.app import app

client = TestClient(app)
LEGACY = Path(__file__).resolve().parent.parent / "examples" / "legacy_sql"


def _files(dialect):
    return [{"name": f.name, "content": f.read_text()}
            for f in sorted((LEGACY / dialect).iterdir())]


def test_detect_endpoint():
    r = client.post("/api/legacy-sql/detect",
                    json={"files": _files("teradata")})
    assert r.status_code == 200
    body = r.json()
    assert body["detected_dialect"] == "teradata"
    assert body["confidence_score"] >= 60
    assert body["detection_reasons"]


def test_analyze_endpoint():
    r = client.post("/api/legacy-sql/analyze",
                    json={"source_format": "auto",
                          "files": _files("sqlserver")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detected_dialect"] == "sqlserver"
    assert body["objects_found"] > 0
    assert body["procedures_found"] == 2
    assert body["temp_objects_found"] == 2
    assert body["temp_table_chains"][0]["final"] == "customer_totals"
    assert body["manual_review_items"] >= 1


def test_convert_validate_lineage_report_flow():
    r = client.post("/api/legacy-sql/convert", json={
        "source_format": "auto", "target_format": "snowflake",
        "files": _files("oracle"),
        "options": {"generate_lineage": True,
                    "generate_validation": True, "ai_review": False}})
    assert r.status_code == 200, r.text
    body = r.json()
    mid = body["migration_id"]
    assert body["detected_source"] == "oracle"
    assert body["target_format"] == "snowflake"
    assert body["errors"]["count"] == 0

    lin = client.get("/api/legacy-sql/%s/lineage" % mid)
    assert lin.status_code == 200
    rep = client.get("/api/legacy-sql/%s/report" % mid)
    assert rep.status_code == 200

    val = client.post("/api/legacy-sql/validate",
                      json={"migration_id": mid})
    assert val.status_code == 200
    assert val.json().get("verdict") or val.json().get("layers")

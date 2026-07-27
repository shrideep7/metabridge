"""Live connection check + real-time validation runner (mocked driver —
the real network path is exercised against the customer's account)."""
import json
import os
import sys
import tempfile
import types

import pytest

# isolate the app's data dir BEFORE web.app import (RBAC reads users.json)
os.environ.setdefault("METABRIDGE_DATA_DIR",
                      tempfile.mkdtemp(prefix="mb_live_"))

from metabridge import livecheck


class _FakeCursor:
    def __init__(self, fail_on=None):
        self._row = None
        self.fail_on = fail_on or ()

    def execute(self, sql, *a):
        for tok in self.fail_on:
            if tok in sql:
                raise RuntimeError("boom: %s" % tok)
        u = sql.upper()
        if u.startswith("USE "):
            self._row = ("ok",)
            return self
        if "INFORMATION_SCHEMA.TABLES" in u and "TABLE_TYPE" in u.upper():
            self._rows = [("PUBLIC", "CUSTOMERS", "BASE TABLE", 1200, 9000),
                          ("PUBLIC", "ORDERS", "BASE TABLE", 5400, 20000),
                          ("PUBLIC", "V_TOP", "VIEW", 0, 0)]
            self._row = self._rows[0]
            return self
        if "INFORMATION_SCHEMA.COLUMNS" in u:
            self._rows = [("PUBLIC", "CUSTOMERS", "ID", "NUMBER"),
                          ("PUBLIC", "CUSTOMERS", "NAME", "TEXT"),
                          ("PUBLIC", "ORDERS", "ID", "NUMBER")]
            self._row = self._rows[0]
            return self
        if "INFORMATION_SCHEMA.VIEWS" in u:
            self._rows = [("PUBLIC", "V_TOP",
                           "SELECT id, name FROM customers QUALIFY "
                           "ROW_NUMBER() OVER (ORDER BY id) <= 10"),
                          ("PUBLIC", "V_BROKEN", "SELECT FROM WHERE (((")]
            self._row = self._rows[0]
            return self
        if u.startswith("SHOW DATABASES"):
            self._rows = [("x", "SNOWFLAKE"),
                          ("x", "SNOWFLAKE_SAMPLE_DATA")]
            self._row = self._rows[0]
            return self
        if "CURRENT_VERSION" in u:
            self._row = ("9.17.2",)
        elif "CURRENT_ACCOUNT" in u:
            self._row = ("GZWSSAV", "DHIMANMUKUL5911", "ACCOUNTADMIN",
                         "SF_SAMPLES_WH", "SF_SAMPLE_DB", "PUBLIC")
        elif "CURRENT_TIMESTAMP" in u:
            self._row = ("2026-07-10 09:00:00",)
        elif "INFORMATION_SCHEMA.TABLES" in u:
            self._row = (42,)
        elif "VIOLATIONS" in u or "COUNT(*)" in u:
            self._row = (0,)
        else:
            self._row = (1,)
        return self

    def fetchone(self):
        return self._row

    def fetchall(self):
        return getattr(self, "_rows", [])


class _FakeConn:
    def __init__(self, fail_on=None):
        self._cur = _FakeCursor(fail_on)

    def cursor(self):
        return self._cur

    def close(self):
        pass


@pytest.fixture()
def fake_driver(monkeypatch):
    mod = types.ModuleType("snowflake.connector")
    holder = {"kwargs": None, "fail_on": ()}

    def connect(**kw):
        holder["kwargs"] = kw
        if not kw.get("password"):
            raise RuntimeError("no password")
        return _FakeConn(holder["fail_on"])

    mod.connect = connect
    pkg = types.ModuleType("snowflake")
    pkg.connector = mod
    monkeypatch.setitem(sys.modules, "snowflake", pkg)
    monkeypatch.setitem(sys.modules, "snowflake.connector", mod)
    return holder


PARAMS = {"account": "GZWSSAV-RH89300", "user": "DhimanMukul5911",
          "role": "ACCOUNTADMIN", "warehouse": "SF_SAMPLES_WH",
          "database": "SF_SAMPLE_DB", "schema": "Public"}


def test_connection_probes(fake_driver, monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "s3cret")
    r = livecheck.test_connection("snowflake", dict(PARAMS))
    assert r["ok"] is True
    assert r["context"]["warehouse"] == "SF_SAMPLES_WH"
    assert r["objects"]["tables_visible"] == 42
    assert {p["probe"] for p in r["probes"]} == {
        "server_version", "server_time"}
    # the driver received the full context, password from the env only
    kw = fake_driver["kwargs"]
    assert kw["account"] == "GZWSSAV-RH89300"
    assert kw["password"] == "s3cret"
    # and the report never contains the secret
    assert "s3cret" not in json.dumps(r)


def test_missing_password_is_actionable(fake_driver, monkeypatch):
    monkeypatch.delenv("MB_SNOWFLAKE_PASSWORD", raising=False)
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    r = livecheck.test_connection("snowflake", dict(PARAMS))
    assert r["ok"] is False
    assert "MB_SNOWFLAKE_PASSWORD" in r["error"]


def test_bad_database_reports_auth_ok_and_visible_dbs(fake_driver,
                                                      monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    fake_driver["fail_on"] = ('USE DATABASE "SF_SAMPLE_DB"',)
    r = livecheck.test_connection("snowflake", dict(PARAMS))
    assert r["ok"] is False
    assert r["authenticated"] is True          # auth is never masked
    bad = next(s for s in r["steps"] if not s["ok"])
    assert "SF_SAMPLE_DB" in bad["step"]
    assert r["databases_visible"] == ["SNOWFLAKE",
                                      "SNOWFLAKE_SAMPLE_DATA"]
    assert "context failed" in r["error"]


def test_connection_failure_reported(fake_driver, monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    fake_driver["fail_on"] = ("CURRENT_VERSION",)
    r = livecheck.test_connection("snowflake", dict(PARAMS))
    assert r["ok"] is False and "boom" in r["error"]


def test_unsupported_connector_is_honest():
    r = livecheck.test_connection("teradata", {})
    assert r["ok"] is False and "not implemented" in r["error"]


def test_live_validation_runs_generated_tests(fake_driver, tmp_path,
                                              monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    doc = {"mappings": [{"mapping": "orders", "tests": [
        {"name": "pk_uniqueness__orders", "test_type": "pk_uniqueness",
         "expectation": "violations == 0",
         "target_sql": "SELECT COUNT(*) AS violations FROM d"},
        {"name": "row_count__orders", "test_type": "row_count",
         "expectation": "source_value == target_value",
         "target_sql": "SELECT COUNT(*) AS row_count FROM fct_orders"},
    ]}]}
    f = tmp_path / "tests.json"
    f.write_text(json.dumps(doc))
    r = livecheck.run_live_validation(str(f), "snowflake", dict(PARAMS))
    assert r["ran"] == 2
    assert r["passed"] == 1          # violations == 0 met
    assert r["measured"] == 1        # reconciliation side recorded
    assert r["failed"] == 0 and r["ok"] is True


def test_api_endpoint_never_echoes_secret(fake_driver, monkeypatch,
                                           tmp_path):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "s3cret")
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    import sys as _s
    from pathlib import Path
    _s.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    resp = client.post("/api/v1/connectors/snowflake/test",
                       json={"params": PARAMS})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "s3cret" not in resp.text


# ---------------------------------------------------------------------------
# introspection: connected database -> conversion-ready analysis
# ---------------------------------------------------------------------------

def test_introspect_inventory_and_manifest(fake_driver, monkeypatch,
                                           tmp_path):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    assert r["ok"] is True
    rd = r["readiness"]
    assert rd["verdict"] == "READY"
    assert rd["tables"] == 2 and rd["views"] == 2
    assert rd["total_rows"] == 6600
    assert rd["views_convertible"] == 1            # V_TOP parses
    (review,) = rd["views_needing_review"]
    assert review["view"] == "V_BROKEN"
    by_name = {t["name"]: t for t in r["tables"]}
    assert by_name["ORDERS"]["rows"] == 5400
    assert by_name["CUSTOMERS"]["columns"][0] == {"name": "ID",
                                                  "type": "NUMBER"}
    # the manifest feeds the Pipeline scaffold directly
    from metabridge.scaffold import load_table_manifest
    f = tmp_path / "m.yml"
    f.write_text(r["manifest_yaml"])
    tables, _ = load_table_manifest(str(f))
    assert {t["name"] for t in tables} == {"CUSTOMERS", "ORDERS"}
    assert tables[0]["columns"]


def test_introspect_requires_database(fake_driver, monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS, database=""))
    assert r["ok"] is False and "database" in r["error"]

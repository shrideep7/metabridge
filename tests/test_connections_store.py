"""Saved connections: persistence across sessions, start/stop lifecycle
gate, secret handling (opt-in storage, never echoed)."""
import json
import os
import tempfile

os.environ.setdefault("METABRIDGE_DATA_DIR",
                      tempfile.mkdtemp(prefix="mb_conns_"))

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from metabridge import connections_store as cs
from web.app import app

client = TestClient(app)

PARAMS = {"account": "GZWSSAV-RH89300", "user": "DhimanMukul5911",
          "role": "ACCOUNTADMIN", "warehouse": "SF_SAMPLES_WH",
          "database": "SF_SAMPLES_DB", "schema": "PUBLIC",
          "password": "s3cret"}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))


def test_save_without_secret_by_default(tmp_path):
    row = cs.save_connection("snowflake", dict(PARAMS))
    assert row["has_secrets"] is False
    assert "password" not in row["params"]      # split out, not stored
    stored = json.loads((tmp_path / "connections.json").read_text())
    assert "s3cret" not in json.dumps(stored)
    # persists across "sessions" (fresh reads)
    (listed,) = cs.list_connections()
    assert listed["id"] == row["id"]
    assert listed["status"] == "active"


def test_opt_in_secret_stored_0600_never_listed(tmp_path):
    row = cs.save_connection("snowflake", dict(PARAMS), save_secrets=True)
    assert row["has_secrets"] is True
    assert "secrets" not in row                  # public view scrubbed
    mode = oct((tmp_path / "connections.json").stat().st_mode)[-3:]
    assert mode == "600"
    # resolve_params merges the stored secret for actual use
    resolved = cs.resolve_params(row["id"])
    assert resolved["password"] == "s3cret"
    (listed,) = cs.list_connections()
    assert "secrets" not in listed and listed["has_secrets"] is True


def test_stop_gate_blocks_use_until_started():
    row = cs.save_connection("snowflake", dict(PARAMS), save_secrets=True)
    cs.set_status(row["id"], "stopped")
    with pytest.raises(PermissionError) as e:
        cs.resolve_params(row["id"])
    assert "stopped" in str(e.value)
    cs.set_status(row["id"], "active")
    assert cs.resolve_params(row["id"])["account"] == PARAMS["account"]


def test_same_params_update_not_duplicate():
    a = cs.save_connection("snowflake", dict(PARAMS), name="first")
    b = cs.save_connection("snowflake", dict(PARAMS), name="renamed")
    assert a["id"] == b["id"]
    assert len(cs.list_connections()) == 1
    assert cs.list_connections()[0]["name"] == "renamed"


def test_api_lifecycle_and_secret_hygiene(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "api"))
    r = client.post("/api/v1/connections",
                    json={"connector": "snowflake", "params": PARAMS,
                          "save_secrets": True})
    assert r.status_code == 200, r.text
    assert "s3cret" not in r.text                # never echoed
    cid = r.json()["id"]

    stop = client.post("/api/v1/connections/%s/stop" % cid)
    assert stop.json()["status"] == "stopped"
    blocked = client.post("/api/v1/connections/%s/test" % cid)
    assert blocked.status_code == 409
    assert "start it before use" in blocked.json()["detail"]

    client.post("/api/v1/connections/%s/start" % cid)
    listed = client.get("/api/v1/connections").json()["connections"]
    assert listed[0]["status"] == "active"
    assert "s3cret" not in json.dumps(listed)

    assert client.delete("/api/v1/connections/%s" % cid).status_code == 200
    assert client.get("/api/v1/connections").json()["connections"] == []


def test_saved_connection_test_uses_stored_secret(tmp_path, monkeypatch):
    # fake driver from the livecheck suite
    import sys
    import types
    from tests.test_livecheck import _FakeConn
    holder = {}
    mod = types.ModuleType("snowflake.connector")

    def connect(**kw):
        holder["kwargs"] = kw
        return _FakeConn()

    mod.connect = connect
    pkg = types.ModuleType("snowflake")
    pkg.connector = mod
    monkeypatch.setitem(sys.modules, "snowflake", pkg)
    monkeypatch.setitem(sys.modules, "snowflake.connector", mod)
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "t"))

    row = cs.save_connection("snowflake", dict(PARAMS), save_secrets=True)
    r = client.post("/api/v1/connections/%s/test" % row["id"])
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert holder["kwargs"]["password"] == "s3cret"   # stored secret used
    assert "s3cret" not in r.text
    # last test recorded on the saved connection
    again = cs.get_connection(row["id"])
    assert again["last_test"]["ok"] is True


def test_load_endpoint_live_and_package_modes(tmp_path, monkeypatch):
    import sys
    import types

    executed = []

    class _Cur:
        def execute(self, sql, *a):
            executed.append(sql)
            self._row = (2,) if sql.upper().startswith("SELECT COUNT") \
                else ("ok",)
            return self

        def fetchone(self):
            return self._row

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    mod = types.ModuleType("snowflake.connector")
    mod.connect = lambda **kw: _Conn()
    pkg = types.ModuleType("snowflake")
    pkg.connector = mod
    monkeypatch.setitem(sys.modules, "snowflake", pkg)
    monkeypatch.setitem(sys.modules, "snowflake.connector", mod)
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "ld"))

    csv_bytes = b"id,city\n1,Pune\n2,Mumbai\n"
    sf = cs.save_connection("snowflake", dict(PARAMS), save_secrets=True)
    r = client.post("/api/v1/connections/%s/load" % sf["id"],
                    files={"file": ("cities.csv", csv_bytes, "text/csv")},
                    data={"table": "CITIES"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["rows_in_table"] == 2
    assert any("COPY INTO CITIES" in s for s in executed)

    # non-driver connector: the standard package is generated instead
    rs = cs.save_connection("redshift", {"host": "h", "database": "d",
                                         "user": "u"})
    r2 = client.post("/api/v1/connections/%s/load" % rs["id"],
                     files={"file": ("cities.csv", csv_bytes, "text/csv")},
                     data={"table": "CITIES"})
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2["mode"] == "package"
    assert "IAM_ROLE" in b2["load"]
    assert any("standard DDL + load script" in n for n in b2["notes"])

    # stopped connection refuses to load
    cs.set_status(sf["id"], "stopped")
    r3 = client.post("/api/v1/connections/%s/load" % sf["id"],
                     files={"file": ("cities.csv", csv_bytes, "text/csv")})
    assert r3.status_code == 409

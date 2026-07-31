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


# SHOW result shapes, keyed by the statement. Each entry is
# (column names, rows) — the names matter because definitions are read by
# NAME (_named), which is what makes the reader survive Snowflake shifting
# its SHOW column order between releases.
_SHOW_RESULTS = {
    "SHOW MATERIALIZED VIEWS": (
        ["created_on", "name", "reserved", "database_name", "schema_name",
         "text"],
        [("t", "MV_SALES", "", "SF_SAMPLES_DB", "PUBLIC",
          "SELECT * FROM orders")]),
    "SHOW DYNAMIC TABLES": (
        ["created_on", "name", "reserved", "database_name", "schema_name",
         "target_lag", "warehouse", "refresh_mode", "text"],
        [("t", "DT_DAILY", "", "SF_SAMPLES_DB", "PUBLIC", "1 hour", "WH",
          "AUTO", "SELECT 1")]),
    "SHOW STREAMS": (
        ["created_on", "name", "database_name", "schema_name", "owner",
         "comment", "table_name", "type", "stale", "mode", "stale_after"],
        [("t", "STR_ORDERS", "SF_SAMPLES_DB", "PUBLIC", "SYSADMIN", "",
          "ORDERS", "DELTA", "false", "APPEND_ONLY", "false")]),
    "SHOW TASKS": (
        ["created_on", "name", "id", "database_name", "schema_name", "owner",
         "comment", "warehouse", "schedule", "predecessors", "state",
         "definition"],
        [("t", "T_LOAD", "1", "SF_SAMPLES_DB", "PUBLIC", "SYSADMIN", "",
          "WH", "5 MINUTE", "[]", "started", "CALL sp_load()"),
         ("t", "T_AGG", "2", "SF_SAMPLES_DB", "PUBLIC", "SYSADMIN", "",
          "WH", "", '["DB.PUBLIC.T_LOAD"]', "started",
          "INSERT INTO agg SELECT 1")]),
    "SHOW PIPES": (
        ["created_on", "name", "database_name", "schema_name", "definition",
         "owner", "notification_channel"],
        [("t", "PIPE_IN", "SF_SAMPLES_DB", "PUBLIC",
          "COPY INTO t FROM @s CREDENTIALS=(password='hunter2secret')",
          "SYSADMIN", "arn:aws:sqs:x")]),
    "SHOW STAGES": (
        ["created_on", "name", "database_name", "schema_name", "url",
         "has_credentials", "comment", "region", "owner", "cloud", "type"],
        [("t", "STG_RAW", "SF_SAMPLES_DB", "PUBLIC", "s3://bucket/raw",
          "N", "", "us-east-1", "SYSADMIN", "AWS", "EXTERNAL")]),
    "SHOW MASKING POLICIES": (
        ["created_on", "name", "database_name", "schema_name", "kind"],
        [("t", "MP_EMAIL", "SF_SAMPLES_DB", "PUBLIC", "MASKING_POLICY")]),
    "SHOW ROW ACCESS POLICIES": (
        ["created_on", "name", "database_name", "schema_name", "kind"],
        [("t", "RAP_REGION", "SF_SAMPLES_DB", "PUBLIC",
          "ROW_ACCESS_POLICY")]),
    "SHOW TAGS": (
        ["created_on", "name", "database_name", "schema_name", "owner",
         "comment", "allowed_values"],
        [("t", "PII", "SF_SAMPLES_DB", "PUBLIC", "SYSADMIN", "", "[]")]),
    "SHOW ROLES": (
        ["created_on", "name", "is_default", "is_current", "is_inherited",
         "assigned_to_users", "granted_to_roles", "granted_roles", "owner",
         "comment"],
        [("t", "ANALYST", "N", "N", "N", "3", "1", "0", "USERADMIN", "ro")]),
    "SHOW SHARES": (
        ["created_on", "kind", "name", "database_name"],
        [("t", "INBOUND", "SF_SAMPLE_SHARE", "SNOWFLAKE_SAMPLE_DATA")]),
    "SHOW GRANTS ON": (
        ["created_on", "privilege", "granted_on", "name", "granted_to",
         "grantee_name"],
        [("t", "USAGE", "DATABASE", "SF_SAMPLES_DB", "ROLE", "ANALYST")]),
}


class _FakeCursor:
    def __init__(self, fail_on=None):
        self._row = None
        self.fail_on = fail_on or ()
        self.description = None

    def _show_result(self, u):
        for stmt, (cols, rows) in _SHOW_RESULTS.items():
            if u.startswith(stmt):
                self.description = [(c,) for c in cols]
                self._rows = rows
                return True
        return False

    def execute(self, sql, *a):
        for tok in self.fail_on:
            if tok in sql:
                # a dict supplies the REAL driver wording, which is what the
                # capability classifier reads; a bare tuple keeps the old
                # generic failure for tests that only care that it broke
                raise RuntimeError(self.fail_on[tok]
                                   if isinstance(self.fail_on, dict)
                                   else "boom: %s" % tok)
        u = " ".join(sql.split()).upper()
        self.description = None
        if u.startswith("USE "):
            self._row = ("ok",)
            return self
        if u.startswith("SHOW ") and not u.startswith("SHOW DATABASES"):
            if self._show_result(u):
                return self
            self._rows = []
            return self
        if "CURRENT_AVAILABLE_ROLES" in u:
            self._row = ('["ANALYST","SECURITYADMIN","SYSADMIN"]',)
            return self
        if "INFORMATION_SCHEMA.SEQUENCES" in u:
            self._rows = [("PUBLIC", "SEQ_ORDER_ID", "1", "1")]
            return self
        if "INFORMATION_SCHEMA.FILE_FORMATS" in u:
            self._rows = [("PUBLIC", "FF_CSV", "CSV")]
            return self
        if "INFORMATION_SCHEMA.FUNCTIONS" in u:
            self._rows = [("PUBLIC", "FN_TAX", "(X NUMBER)", "NUMBER",
                           "SQL", "SELECT x * 0.2")]
            return self
        if "INFORMATION_SCHEMA.PROCEDURES" in u:
            self._rows = [("PUBLIC", "SP_LOAD", "()", "VARCHAR",
                           "JAVASCRIPT",
                           "var c = 'password=hunter2secret'; return c;")]
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
        if u.startswith("SELECT CURRENT_USER(), CURRENT_ROLE()"):
            # the introspect context probe — a DIFFERENT column order from
            # test_connection's probe below, which is why it matches first
            self._row = ("DHIMANMUKUL5911", "ACCOUNTADMIN", "SF_SAMPLES_WH",
                         "SF_SAMPLE_DB", "PUBLIC", "AWS_US_EAST_1",
                         "GZWSSAV")
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


def test_snowflake_introspect_returns_the_whole_object_catalog(
        fake_driver, monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    assert r["ok"] is True
    got = {cls: [o["name"] for o in r[cls]] for cls in
           ("materialized_views", "dynamic_tables", "sequences",
            "file_formats", "functions", "procedures", "streams", "tasks",
            "pipes", "stages", "masking_policies", "row_access_policies",
            "tags", "roles", "shares")}
    assert got["materialized_views"] == ["MV_SALES"]
    assert got["dynamic_tables"] == ["DT_DAILY"]
    assert got["sequences"] == ["SEQ_ORDER_ID"]
    assert got["file_formats"] == ["FF_CSV"]
    assert got["functions"] == ["FN_TAX"]
    assert got["procedures"] == ["SP_LOAD"]
    assert got["streams"] == ["STR_ORDERS"]
    assert got["tasks"] == ["T_LOAD", "T_AGG"]
    assert got["pipes"] == ["PIPE_IN"]
    assert got["stages"] == ["STG_RAW"]
    assert got["masking_policies"] == ["MP_EMAIL"]
    assert got["row_access_policies"] == ["RAP_REGION"]
    assert got["tags"] == ["PII"]
    assert got["roles"] == ["ANALYST"]
    assert got["shares"] == ["SF_SAMPLE_SHARE"]
    assert r["grants"][0] == {"role": "ANALYST", "privilege": "USAGE",
                              "granted_on": "DATABASE",
                              "object": "SF_SAMPLES_DB"}
    # schema-bearing classes carry the Level 2 filter key
    for cls in ("materialized_views", "sequences", "functions", "streams",
                "tasks", "pipes", "stages", "tags"):
        assert all(o["schema"] for o in r[cls]), cls
    # readiness gains one count per class
    assert r["readiness"]["procedures"] == 1
    assert r["readiness"]["tasks"] == 2


def test_snowflake_reads_definitions_by_column_name_not_offset(
        fake_driver, monkeypatch):
    """SHOW column ORDER shifts between Snowflake releases; the names do
    not. A wrong offset would silently carry the wrong text into a
    conversion, so bodies are read by name."""
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    assert r["materialized_views"][0]["definition"] == \
        "SELECT * FROM orders"
    dt = r["dynamic_tables"][0]
    assert dt["definition"] == "SELECT 1"
    assert dt["target_lag"] == "1 hour" and dt["warehouse"] == "WH"


def test_snowflake_task_dag_is_built_from_predecessors(fake_driver,
                                                       monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    assert r["task_dag"] == [{"from": "T_LOAD", "to": "T_AGG"}]


def test_snowflake_bodies_are_redacted_everywhere_they_appear(
        fake_driver, monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    # a procedure body AND a pipe's COPY statement both carry credentials
    assert "hunter2secret" not in json.dumps(r)
    assert "***REDACTED***" in r["procedures"][0]["definition"]
    assert "***REDACTED***" in r["pipes"][0]["definition"]
    locations = {f["location"] for f in r["secret_findings"]}
    assert locations == {"procedure PUBLIC.SP_LOAD", "pipe PIPE_IN"}


def test_snowflake_context_and_edition_are_reported(fake_driver,
                                                    monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    ctx = livecheck.introspect(
        "snowflake", dict(PARAMS, database="SF_SAMPLES_DB"))["context"]
    assert ctx["current_role"] == "ACCOUNTADMIN"
    assert ctx["user"] == "DHIMANMUKUL5911"
    assert ctx["account"] == "GZWSSAV"
    assert ctx["available_roles"] == ["ANALYST", "SECURITYADMIN",
                                      "SYSADMIN"]
    # policies and tags resolved, so this account is Enterprise or higher
    assert ctx["edition"] == "Enterprise (or higher)"


def test_snowflake_standard_edition_is_inferred_not_reported_as_broken(
        fake_driver, monkeypatch):
    """An edition-gated class must not look like an error, and must not
    make the whole inventory fail."""
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    fake_driver["fail_on"] = {
        "SHOW MASKING POLICIES": "002040 (42601): SQL compilation error: "
                                 "Unsupported feature 'MASKING_POLICY'.",
        "SHOW ROW ACCESS POLICIES": "Unsupported feature "
                                    "'ROW_ACCESS_POLICY'.",
        "SHOW TAGS": "Unsupported feature 'TAG'."}
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    assert r["ok"] is True
    assert r["tables"] and r["procedures"]        # the rest still arrives
    assert r["masking_policies"] == []
    assert r["context"]["edition"] == "Standard"


def test_snowflake_blocked_role_yields_an_actionable_recommendation(
        fake_driver, monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    fake_driver["fail_on"] = {
        "SHOW ROLES": "003001 (42501): SQL access control error: "
                      "Insufficient privileges to operate on account"}
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    assert r["ok"] is True and r["roles"] == []
    assert r["capabilities"]["roles"]["status"] == "blocked_privilege"
    # this session can assume SECURITYADMIN, so name it rather than telling
    # the user to go and get a grant they already hold. SYSADMIN is NOT
    # offered — it does not carry account-security rights.
    (rec,) = r["recommendations"]
    assert "SECURITYADMIN" in rec and "SYSADMIN," not in rec
    assert "roles" in rec


def test_snowflake_without_a_database_offers_the_picker(fake_driver,
                                                        monkeypatch):
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS, database=""))
    assert r["ok"] is True and r["mode"] == "databases"
    assert [d["name"] for d in r["databases"]] == ["SNOWFLAKE",
                                                   "SNOWFLAKE_SAMPLE_DATA"]


# ---------------------------------------------------------------------------
# PostgreSQL / Redshift: the full object catalog, per-class capability
# ---------------------------------------------------------------------------

PG_PARAMS = {"host": "db.internal", "port": "5432", "user": "analyst",
             "database": "sales", "schema": ""}


class _PgCursor:
    """psycopg2 contract: execute() returns None, results come from
    fetchall()/fetchone(), and a failed statement poisons the transaction
    until rollback()."""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on or {}
        self._rows = []
        self._row = None
        self.aborted = False

    def execute(self, sql, args=None):
        u = " ".join(sql.split()).upper()
        for tok, msg in self.fail_on.items():
            if tok in u:
                self.aborted = True
                raise RuntimeError(msg)
        if self.aborted:
            raise RuntimeError("current transaction is aborted, commands "
                               "ignored until end of transaction block")
        self._rows = []
        self._row = None
        if "CURRENT_USER" in u:
            self._row = ("analyst", "sales", "public", "PostgreSQL 16.2")
        elif "INFORMATION_SCHEMA.TABLES" in u:
            self._rows = [("public", "customers", "BASE TABLE"),
                          ("public", "orders", "BASE TABLE"),
                          ("staging", "v_top", "VIEW")]
        elif "INFORMATION_SCHEMA.COLUMNS" in u:
            self._rows = [("public", "customers", "id", "integer",
                           None, 32, 0),
                          ("public", "customers", "name", "character varying",
                           80, None, None),
                          ("public", "orders", "id", "integer", None, 32, 0)]
        elif "PG_CLASS" in u:
            self._rows = [("public", "customers", 1200),
                          ("public", "orders", 5400)]
        elif "INFORMATION_SCHEMA.VIEWS" in u:
            self._rows = [("staging", "v_top", "SELECT id FROM customers")]
        elif "PG_DATABASE" in u:
            self._rows = [("sales", "postgres"), ("warehouse", "etl_owner")]
        elif "PG_MATVIEWS" in u:
            self._rows = [("public", "mv_daily_sales")]
        elif "INFORMATION_SCHEMA.SEQUENCES" in u:
            self._rows = [("public", "orders_id_seq", "1", "1")]
        elif "INFORMATION_SCHEMA.ROUTINES" in u:
            kind = (args or ("FUNCTION",))[0]
            if kind == "FUNCTION":
                self._rows = [("public", "fn_total", "numeric", "SQL",
                               "SELECT sum(amount) FROM orders")]
            else:
                self._rows = [("public", "sp_load", None, "PLPGSQL",
                               "COPY t FROM s3 CREDENTIALS "
                               "(password='hunter2secret')")]
        return None

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _PgConn:
    def __init__(self, fail_on=None):
        self._cur = _PgCursor(fail_on)
        self.autocommit = False
        self.rollbacks = 0

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rollbacks += 1
        self._cur.aborted = False

    def close(self):
        pass


@pytest.fixture()
def pg_driver(monkeypatch):
    mod = types.ModuleType("psycopg2")
    holder = {"kwargs": None, "fail_on": {}, "conn": None}

    def connect(**kw):
        holder["kwargs"] = kw
        if not kw.get("password"):
            raise RuntimeError("no password")
        holder["conn"] = _PgConn(holder["fail_on"])
        return holder["conn"]

    mod.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg2", mod)
    return holder


def test_pg_introspect_returns_every_object_class(pg_driver, monkeypatch):
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    r = livecheck.introspect("postgres", dict(PG_PARAMS))
    assert r["ok"] is True
    # tables/views keep working exactly as before
    assert {t["name"] for t in r["tables"]} == {"customers", "orders",
                                                "v_top"}
    assert r["readiness"]["total_rows"] == 6600
    # and the classes the filters need are all present, each with a schema
    assert [o["name"] for o in r["materialized_views"]] == ["mv_daily_sales"]
    assert [o["name"] for o in r["sequences"]] == ["orders_id_seq"]
    assert [o["name"] for o in r["functions"]] == ["fn_total"]
    assert [o["name"] for o in r["procedures"]] == ["sp_load"]
    for cls in ("materialized_views", "sequences", "functions",
                "procedures"):
        assert all(o["schema"] for o in r[cls]), cls
        assert r["capabilities"][cls]["status"] == "available"
    assert r["readiness"]["functions"] == 1
    assert r["readiness"]["procedures"] == 1
    # precision survives the enriched column path
    cols = {c["name"]: c["type"]
            for t in r["tables"] if t["name"] == "customers"
            for c in t["columns"]}
    assert cols["name"] == "character varying(80)"


def test_pg_introspect_reports_context(pg_driver, monkeypatch):
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    ctx = livecheck.introspect("postgres", dict(PG_PARAMS))["context"]
    assert ctx["current_role"] == "analyst"      # the UI banner reads this
    assert ctx["database"] == "sales"
    assert ctx["edition"] == "n/a"               # no edition concept here
    assert "PostgreSQL 16.2" in ctx["version"]


def test_pg_procedure_body_is_redacted(pg_driver, monkeypatch):
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    r = livecheck.introspect("postgres", dict(PG_PARAMS))
    (proc,) = r["procedures"]
    assert "hunter2secret" not in proc["definition"]
    assert "***REDACTED***" in proc["definition"]
    assert proc["language"] == "PLPGSQL"
    # the finding is aggregated at the top level for the UI banner
    assert r["secret_findings"][0]["location"] == "public.sp_load"
    assert "hunter2secret" not in json.dumps(r)


def test_pg_blocked_class_does_not_cost_the_inventory(pg_driver,
                                                      monkeypatch):
    """A role that may read tables but not routines still gets an
    inventory — and the matrix says WHY the class is empty."""
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    pg_driver["fail_on"] = {"INFORMATION_SCHEMA.ROUTINES":
                            "permission denied for schema public"}
    r = livecheck.introspect("postgres", dict(PG_PARAMS))
    assert r["ok"] is True                       # the run survives
    assert r["tables"] and r["sequences"]        # everything else is intact
    assert r["functions"] == [] and r["procedures"] == []
    assert r["capabilities"]["functions"]["status"] == "blocked_privilege"
    assert "permission denied" in r["capabilities"]["functions"]["reason"]
    # the poisoned transaction was cleared so later classes could run
    assert pg_driver["conn"].rollbacks >= 1


def test_pg_absent_catalog_is_not_reported_as_an_error(pg_driver,
                                                       monkeypatch):
    """Redshift has no pg_matviews. "This platform doesn't have it" and
    "this broke" are different facts."""
    monkeypatch.setenv("MB_REDSHIFT_PASSWORD", "x")
    pg_driver["fail_on"] = {"PG_MATVIEWS":
                            'relation "pg_matviews" does not exist'}
    r = livecheck.introspect("redshift", dict(PG_PARAMS))
    assert r["ok"] is True
    assert r["materialized_views"] == []
    assert r["capabilities"]["materialized_views"]["status"] \
        == "not_applicable"


# ---------------------------------------------------------------------------
# Databricks / Unity Catalog: its own object set, not Snowflake's
# ---------------------------------------------------------------------------

DBX_PARAMS = {"host": "adb.cloud.databricks.com",
              "http_path": "/sql/1.0/warehouses/abc", "catalog": "main",
              "schema": "sales"}


class _DbxCursor:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on or {}
        self._rows = []
        self._row = None
        self.description = None

    def execute(self, sql, args=None):
        u = " ".join(sql.split()).upper()
        for tok, msg in self.fail_on.items():
            if tok in u:
                raise RuntimeError(msg)
        self._rows, self._row = [], None
        if u.startswith("SHOW CATALOGS"):
            self._rows = [("main",), ("hive_metastore",), ("samples",)]
        elif "CURRENT_CATALOG()" in u:
            self._row = ("main", "sales", "dev@corp.com")
        elif "INFORMATION_SCHEMA.ROUTINES" in u:
            kind = (args or [None, "FUNCTION"])[1]
            self._rows = ([("sales", "fn_margin", "DOUBLE", "SQL",
                            "SELECT 1")] if kind == "FUNCTION"
                          else [("sales", "sp_reload", None, "PYTHON",
                                 "conn(password='hunter2secret')")])
        elif "INFORMATION_SCHEMA.VOLUMES" in u:
            self._rows = [("sales", "raw_files", "MANAGED")]
        elif "INFORMATION_SCHEMA.TABLE_TAGS" in u:
            self._rows = [("sales", "orders", "pii", "true")]
        elif "INFORMATION_SCHEMA.TABLE_PRIVILEGES" in u:
            self._rows = [("sales", "orders", "analysts", "SELECT")]
        elif "INFORMATION_SCHEMA.TABLE_STATISTICS" in u:
            self._rows = [("sales", "orders", 4200)]
        elif "INFORMATION_SCHEMA.TABLES" in u:
            self._rows = [("sales", "orders", "MANAGED"),
                          ("sales", "v_top", "VIEW")]
        elif "INFORMATION_SCHEMA.COLUMNS" in u:
            self._rows = [("sales", "orders", "id", "bigint", None, None,
                           None, "bigint")]
        elif "INFORMATION_SCHEMA.VIEWS" in u:
            self._rows = [("sales", "v_top", "SELECT * FROM orders")]
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _DbxConn:
    def __init__(self, fail_on=None):
        self._cur = _DbxCursor(fail_on)

    def cursor(self):
        return self._cur

    def close(self):
        pass


@pytest.fixture()
def dbx_driver(monkeypatch):
    holder = {"fail_on": {}}
    sqlmod = types.ModuleType("databricks.sql")
    sqlmod.connect = lambda **kw: _DbxConn(holder["fail_on"])
    pkg = types.ModuleType("databricks")
    pkg.sql = sqlmod
    monkeypatch.setitem(sys.modules, "databricks", pkg)
    monkeypatch.setitem(sys.modules, "databricks.sql", sqlmod)
    return holder


def test_databricks_returns_its_own_object_classes(dbx_driver, monkeypatch):
    monkeypatch.setenv("MB_DATABRICKS_TOKEN", "t")
    r = livecheck.introspect("databricks", dict(DBX_PARAMS))
    assert r["ok"] is True
    assert [o["name"] for o in r["functions"]] == ["fn_margin"]
    assert [o["name"] for o in r["procedures"]] == ["sp_reload"]
    # volumes have no Snowflake equivalent — a new type, not a special case
    assert r["volumes"] == [{"schema": "sales", "name": "raw_files",
                             "type": "MANAGED"}]
    assert r["tags"][0]["name"] == "pii"
    assert r["grants"][0] == {"role": "analysts", "privilege": "SELECT",
                              "granted_on": "TABLE",
                              "object": "sales.orders"}
    # Snowflake-only classes are ABSENT, not blocked or empty-with-a-reason
    for cls in ("streams", "tasks", "pipes", "stages"):
        assert cls not in r
        assert cls not in r["capabilities"]
    assert r["context"]["current_role"] == "dev@corp.com"
    assert r["readiness"]["volumes"] == 1


def test_databricks_offers_a_catalog_switcher_without_blocking(dbx_driver,
                                                               monkeypatch):
    """Databricks always resolves a default catalog, so it must still
    ANALYSE rather than stop at a picker — the list rides along so the
    console can offer a switcher."""
    monkeypatch.setenv("MB_DATABRICKS_TOKEN", "t")
    r = livecheck.introspect("databricks", dict(DBX_PARAMS, catalog=""))
    assert r.get("mode") != "databases"          # never a dead end
    assert r["tables"]                           # the default was analysed
    assert r["database"] == "main"
    assert r["available_databases"] == ["main", "hive_metastore", "samples"]


def test_databricks_catalog_list_failure_is_not_fatal(dbx_driver,
                                                      monkeypatch):
    monkeypatch.setenv("MB_DATABRICKS_TOKEN", "t")
    dbx_driver["fail_on"] = {"SHOW CATALOGS": "not authorized"}
    r = livecheck.introspect("databricks", dict(DBX_PARAMS))
    assert r["ok"] is True and r["available_databases"] == []


def test_databricks_procedure_body_is_redacted(dbx_driver, monkeypatch):
    monkeypatch.setenv("MB_DATABRICKS_TOKEN", "t")
    r = livecheck.introspect("databricks", dict(DBX_PARAMS))
    assert "hunter2secret" not in json.dumps(r)
    assert "***REDACTED***" in r["procedures"][0]["definition"]


def test_databricks_row_count_fallback_still_works(dbx_driver, monkeypatch):
    """The 3-tier row-count fix must survive the new object fetches."""
    monkeypatch.setenv("MB_DATABRICKS_TOKEN", "t")
    r = livecheck.introspect("databricks", dict(DBX_PARAMS))
    orders = next(t for t in r["tables"] if t["name"] == "orders")
    assert orders["rows"] == 4200
    assert r["readiness"]["total_rows"] == 4200


def test_databricks_missing_class_does_not_cost_the_inventory(dbx_driver,
                                                              monkeypatch):
    """Not every workspace exposes every information_schema view."""
    monkeypatch.setenv("MB_DATABRICKS_TOKEN", "t")
    dbx_driver["fail_on"] = {
        "INFORMATION_SCHEMA.VOLUMES":
            "[TABLE_OR_VIEW_NOT_FOUND] Table or view not found: "
            "information_schema.volumes does not exist"}
    r = livecheck.introspect("databricks", dict(DBX_PARAMS))
    assert r["ok"] is True and r["tables"] and r["functions"]
    assert r["volumes"] == []
    assert r["capabilities"]["volumes"]["status"] == "not_applicable"


# ---------------------------------------------------------------------------
# Level 1: the database picker (a connection saved without a database)
# ---------------------------------------------------------------------------

def test_pg_without_a_database_offers_the_picker(pg_driver, monkeypatch):
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    r = livecheck.introspect("postgres", dict(PG_PARAMS, database=""))
    assert r["ok"] is True                       # no longer a dead end
    assert r["mode"] == "databases"
    assert [d["name"] for d in r["databases"]] == ["sales", "warehouse"]
    assert r["databases"][1]["owner"] == "etl_owner"
    assert r["context"]["current_role"] == "analyst"
    # listing requires being connected to something first
    assert pg_driver["kwargs"]["dbname"] == "postgres"


def test_redshift_picker_uses_its_own_bootstrap_database(pg_driver,
                                                         monkeypatch):
    monkeypatch.setenv("MB_REDSHIFT_PASSWORD", "x")
    livecheck.introspect("redshift", dict(PG_PARAMS, database=""))
    assert pg_driver["kwargs"]["dbname"] == "dev"


def test_pg_picker_falls_back_when_owner_lookup_is_unavailable(
        pg_driver, monkeypatch):
    """Losing the owner column is a downgrade; losing the database list is
    an outage."""
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    pg_driver["fail_on"] = {"PG_GET_USERBYID":
                            "function pg_get_userbyid does not exist"}
    r = livecheck.introspect("postgres", dict(PG_PARAMS, database=""))
    assert r["ok"] is True
    assert [d["name"] for d in r["databases"]] == ["sales", "warehouse"]
    assert r["databases"][0]["owner"] == ""


def test_introspect_endpoint_drills_into_a_chosen_database(monkeypatch):
    """?database=X writes to the field that connector's driver reads, and a
    picker response is never recorded as an analysis."""
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import metabridge.connections_store as store
    import metabridge.livecheck as lc
    from web import app as webapp

    seen = {}

    def fake_introspect(key, params, **kw):
        seen["key"], seen["params"] = key, dict(params)
        return {"ok": True, "mode": "databases", "databases": []}

    monkeypatch.setattr(lc, "introspect", fake_introspect)
    monkeypatch.setattr(store, "resolve_params", lambda cid: {})
    recorded = []
    monkeypatch.setattr(store, "record_analysis",
                        lambda *a, **k: recorded.append(a))

    monkeypatch.setattr(store, "get_connection",
                        lambda cid: {"connector": "postgres"})
    webapp.v1_connection_introspect("c1", database="SALES")
    assert seen["params"]["database"] == "SALES"

    # Databricks calls it a CATALOG — the same query param, a different field
    monkeypatch.setattr(store, "get_connection",
                        lambda cid: {"connector": "databricks"})
    webapp.v1_connection_introspect("c2", database="MAIN")
    assert seen["params"]["catalog"] == "MAIN"
    assert "database" not in seen["params"]

    assert recorded == []            # a list of databases is not an analysis


def test_console_renders_the_multi_level_estate_filters():
    """The Data Estate page must carry every control the filter hierarchy
    needs. There is no browser in CI, so this asserts the markup and the
    wiring exist rather than the rendered behaviour."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "web" / "templates"
            / "console.html").read_text(encoding="utf-8")
    # Level 2 control + the context strip that hosts levels 1 and 3
    assert 'id="estateSchema"' in html
    assert 'id="estateContext"' in html
    for fn in ("function renderDatabaseList(",      # Level 1
               "async function introspectDatabase(",
               "function populateSchemaFilter(",    # Level 2
               "function renderEstateContext(",     # Level 3
               "function estateAssets(",
               "function estateInScope(",           # Level 4
               "function openObjectDrawer("):
        assert fn in html, fn
    # the drill-in must pass the chosen database to the endpoint
    assert "'/introspect?database=' + encodeURIComponent(dbName)" in html
    # every filter change resets to page 1, so the pager can never strand
    # the user on a page that no longer exists
    assert "sel.onchange = () => estateReRender(true);" in html


def test_picker_response_is_refused_where_a_catalog_is_required():
    """`ok` is not enough: a picker has no manifest, so analysing it would
    silently produce an empty assessment."""
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from fastapi import HTTPException
    from web.app import _require_a_database

    _require_a_database({"ok": True, "tables": [], "manifest_yaml": "x"})
    with pytest.raises(HTTPException) as e:
        _require_a_database({"ok": True, "mode": "databases",
                             "databases": [{"name": "sales"}]})
    assert e.value.status_code == 422
    assert "no database set" in e.value.detail


# --- fetched object bodies: redacted before they leave the backend --------

def test_with_body_redacts_and_reports_the_finding():
    o = livecheck._with_body({"schema": "SALES", "name": "LOAD_ORDERS"},
                             "COPY INTO t FROM s3://x "
                             "CREDENTIALS=(password='hunter2secret')",
                             "SALES.LOAD_ORDERS")
    assert "hunter2secret" not in o["definition"]
    assert "***REDACTED***" in o["definition"]
    (finding,) = o["secret_findings"]
    assert finding["location"] == "SALES.LOAD_ORDERS"
    assert "redacted" in finding["evidence"]
    assert "hunter2secret" not in json.dumps(o)


def test_with_body_clean_body_carries_no_findings():
    o = livecheck._with_body({}, "SELECT 1", "S.CLEAN")
    assert o["definition"] == "SELECT 1"
    assert "secret_findings" not in o


def test_with_body_caps_length_and_tolerates_none():
    assert livecheck._with_body({}, None, "x")["definition"] == ""
    long_body = livecheck._with_body({}, "a" * 20000, "x")["definition"]
    assert len(long_body) == livecheck._MAX_BODY


def test_with_body_withholds_the_body_when_it_cannot_be_scanned(monkeypatch):
    # the scanner is what makes a body safe to store; if it cannot run, the
    # RAW body must never be emitted
    monkeypatch.setitem(sys.modules, "metabridge.security.engine", None)
    o = livecheck._with_body({}, "password='hunter2secret'", "S.P")
    assert o["definition"] == ""
    assert o["secret_findings"][0]["type"] == "unscanned"
    assert "hunter2secret" not in json.dumps(o)

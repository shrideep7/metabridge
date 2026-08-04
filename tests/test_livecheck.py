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
# Oracle: ALL_* catalog views, PL/SQL bodies, enforced primary keys
# ---------------------------------------------------------------------------

ORA_PARAMS = {"host": "localhost", "port": "1521", "user": "sales",
              "database": "FREEPDB1", "schema": "SALES"}


class _OraCursor:
    """python-oracledb contract: execute(sql, binds) then fetchone()/
    fetchall(). Every statement issued is recorded so the tests can assert
    HOW the catalog was queried, not just what came back."""

    def __init__(self, fail_on=None, seen=None):
        self.fail_on = fail_on or {}
        self.seen = seen if seen is not None else []
        self._rows = []
        self._row = None

    def close(self):
        pass

    def execute(self, sql, args=None):
        u = " ".join(sql.split()).upper()
        self.seen.append(u)
        for tok, msg in self.fail_on.items():
            if tok in u:
                raise RuntimeError(msg)
        self._rows, self._row = [], None
        if u.startswith("SET TRANSACTION") or u.startswith("ALTER SESSION"):
            return self
        if "SYS_CONTEXT" in u:
            self._row = ("SALES", "SALES", "FREE", "FREEPDB1")
        elif "PRODUCT_COMPONENT_VERSION" in u:
            self._row = ("23.4.0.24.05",)
        elif "USER_ROLE_PRIVS" in u:
            self._rows = [("CONNECT",), ("RESOURCE",)]
        elif "CURRENT_TIMESTAMP" in u:
            self._row = ("2026-08-04 09:00:00",)
        elif "SELECT 1 FROM ALL_USERS" in u:
            self._row = (1,) if "SALES" in str(args or {}).upper() else None
        elif "ALL_USERS" in u:
            self._rows = [("HR",), ("SALES",)]
        elif "COUNT(*) FROM ALL_TABLES" in u:
            self._row = (2,)
        elif "ALL_TABLES" in u:
            self._rows = [("SALES", "CUSTOMERS", 1200),
                          ("SALES", "ORDERS", 5400)]
        elif "ALL_TAB_COLUMNS" in u:
            self._rows = [("SALES", "CUSTOMERS", "ID", "NUMBER", 0, 10, 0),
                          ("SALES", "CUSTOMERS", "NAME", "VARCHAR2", 80,
                           None, None),
                          ("SALES", "ORDERS", "ID", "NUMBER", 0, None, None),
                          ("SALES", "ORDERS", "NOTES", "CLOB", 0, None,
                           None)]
        elif "ALL_SEGMENTS" in u:
            self._rows = [("SALES", "CUSTOMERS", 65536)]
        elif "MAX(LENGTH(NOTES))" in u:
            self._rows = [(300,)]
        elif "ALL_VIEWS" in u:
            self._rows = [("SALES", "V_TOP",
                           "SELECT id, name FROM customers WHERE ROWNUM <= 10"),
                          ("SALES", "V_BROKEN", "SELECT FROM WHERE (((")]
        elif "ALL_CONSTRAINTS" in u:
            self._rows = [("SALES", "CUSTOMERS", "ID", 1)]
        elif "ALL_SOURCE" in u:
            self._rows = [
                ("SALES", "FN_TAX", "FUNCTION",
                 "FUNCTION fn_tax RETURN NUMBER IS BEGIN RETURN 0.2; END;"),
                ("SALES", "SP_LOAD", "PROCEDURE",
                 "PROCEDURE sp_load IS BEGIN\n"),
                ("SALES", "SP_LOAD", "PROCEDURE",
                 "  c := 'password=hunter2secret'; END;"),
                ("SALES", "PKG_ETL", "PACKAGE", "PACKAGE pkg_etl IS\n"),
                ("SALES", "PKG_ETL", "PACKAGE BODY",
                 "PACKAGE BODY pkg_etl IS END;")]
        elif "OBJECT_TYPE = 'FUNCTION'" in u:
            self._rows = [("SALES", "FN_TAX")]
        elif "OBJECT_TYPE = 'PROCEDURE'" in u:
            self._rows = [("SALES", "SP_LOAD")]
        elif "OBJECT_TYPE = 'PACKAGE'" in u:
            self._rows = [("SALES", "PKG_ETL")]
        elif "ALL_SEQUENCES" in u:
            self._rows = [("SALES", "SEQ_ORDER_ID", "1", "1")]
        elif "ALL_MVIEWS" in u:
            self._rows = [("SALES", "MV_SALES", "SELECT * FROM orders")]
        elif "ALL_TRIGGERS" in u:
            self._rows = [("SALES", "TRG_AUDIT", "ORDERS", "AFTER EACH ROW",
                           "INSERT", "BEGIN audit_row; END;")]
        elif "ALL_SYNONYMS" in u:
            self._rows = [("SALES", "SYN_CUST", "LEGACY", "CUSTOMERS", "")]
        elif "ALL_DB_LINKS" in u:
            self._rows = [("SALES", "DBL_LEGACY", "legacy.corp", "ETL")]
        elif "ALL_SCHEDULER_JOBS" in u:
            self._rows = [("SALES", "JOB_NIGHTLY", "CALENDAR",
                           "FREQ=DAILY", "SCHEDULED", "BEGIN pkg_etl.run; END;")]
        elif "ALL_TAB_PRIVS" in u:
            self._rows = [("ANALYST", "SALES", "ORDERS", "SELECT")]
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _OraConn:
    def __init__(self, fail_on=None):
        self.seen = []
        self._cur = _OraCursor(fail_on, self.seen)

    def cursor(self):
        return self._cur

    def close(self):
        pass


@pytest.fixture()
def ora_driver(monkeypatch):
    mod = types.ModuleType("oracledb")
    holder = {"kwargs": None, "fail_on": {}, "conn": None}

    def connect(**kw):
        holder["kwargs"] = kw
        if not kw.get("password"):
            raise RuntimeError("no password")
        holder["conn"] = _OraConn(holder["fail_on"])
        return holder["conn"]

    mod.connect = connect
    monkeypatch.setitem(sys.modules, "oracledb", mod)
    return holder


def test_oracle_test_connection_probes(ora_driver, monkeypatch):
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "s3cret")
    r = livecheck.test_connection("oracle", dict(ORA_PARAMS))
    assert r["ok"] is True and r["authenticated"] is True
    assert r["context"]["database"] == "FREEPDB1"   # the PDB, not the CDB
    assert r["context"]["version"] == "23.4.0.24.05"
    assert r["objects"]["tables_visible"] == 2
    assert {p["probe"] for p in r["probes"]} == {"server_version",
                                                 "server_time"}
    # the service name is what resolves the database, so it is the DSN
    assert ora_driver["kwargs"]["dsn"] == "localhost:1521/FREEPDB1"
    assert "s3cret" not in json.dumps(r)


def test_oracle_endpoint_pasted_into_host_is_split(ora_driver, monkeypatch):
    """The Database field is the one people miss, so an easy-connect string
    pasted whole into Host must still resolve."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.test_connection(
        "oracle", {"host": "localhost:1521/FREE", "user": "sales"})
    assert r["ok"] is True
    assert ora_driver["kwargs"]["dsn"] == "localhost:1521/FREE"


def test_oracle_missing_service_name_is_actionable(ora_driver, monkeypatch):
    """Oracle fixes the database at connect time, so there is no picker to
    fall back to — the error has to name the field and a real value."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.test_connection("oracle", dict(ORA_PARAMS, database=""))
    assert r["ok"] is False and r["needs_credential"] is False
    assert "service name" in r["error"].lower()
    assert "FREEPDB1" in r["error"]


def test_oracle_missing_password_is_actionable(ora_driver, monkeypatch):
    monkeypatch.delenv("MB_ORACLE_PASSWORD", raising=False)
    monkeypatch.delenv("ORACLE_PASSWORD", raising=False)
    r = livecheck.test_connection("oracle", dict(ORA_PARAMS))
    assert r["ok"] is False and r["needs_credential"] is True
    assert "MB_ORACLE_PASSWORD" in r["error"]


def test_oracle_listener_error_gets_the_fix_not_just_the_code(ora_driver,
                                                              monkeypatch):
    """ORA-12514 tells a non-DBA nothing; the connector knows what it means."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")

    def boom(**kw):
        raise RuntimeError("ORA-12514: Cannot connect to database. Service "
                           "orcl is not registered with the listener")
    ora_driver_mod = sys.modules["oracledb"]
    ora_driver_mod.connect = boom
    r = livecheck.test_connection("oracle", dict(ORA_PARAMS, database="orcl"))
    assert r["ok"] is False
    assert "SERVICE NAME" in r["error"] or "service name" in r["error"].lower()
    assert "FREEPDB1" in r["error"]


def test_oracle_bad_schema_reports_auth_ok_and_visible_schemas(ora_driver,
                                                               monkeypatch):
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.test_connection("oracle", dict(ORA_PARAMS, schema="NOPE"))
    assert r["ok"] is False
    assert r["authenticated"] is True            # auth is never masked
    bad = next(s for s in r["steps"] if not s["ok"])
    assert "NOPE" in bad["step"]
    assert r["schemas_visible"] == ["HR", "SALES"]
    assert "context failed" in r["error"]


def test_oracle_introspect_returns_every_object_class(ora_driver,
                                                      monkeypatch):
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    assert r["ok"] is True
    assert {t["name"] for t in r["tables"]} == {"CUSTOMERS", "ORDERS"}
    assert r["readiness"]["total_rows"] == 6600
    assert [o["name"] for o in r["functions"]] == ["FN_TAX"]
    assert [o["name"] for o in r["procedures"]] == ["SP_LOAD"]
    # classes with no Snowflake equivalent are simply more types in the estate
    assert [o["name"] for o in r["packages"]] == ["PKG_ETL"]
    assert [o["name"] for o in r["triggers"]] == ["TRG_AUDIT"]
    assert [o["name"] for o in r["synonyms"]] == ["SYN_CUST"]
    assert [o["name"] for o in r["db_links"]] == ["DBL_LEGACY"]
    assert [o["name"] for o in r["scheduler_jobs"]] == ["JOB_NIGHTLY"]
    assert [o["name"] for o in r["materialized_views"]] == ["MV_SALES"]
    assert [o["name"] for o in r["sequences"]] == ["SEQ_ORDER_ID"]
    assert r["grants"][0] == {"role": "ANALYST", "privilege": "SELECT",
                              "granted_on": "TABLE",
                              "object": "SALES.ORDERS"}
    # every schema-bearing class carries the Level 2 filter key
    for cls in ("functions", "procedures", "packages", "triggers",
                "synonyms", "db_links", "scheduler_jobs",
                "materialized_views", "sequences"):
        assert all(o["schema"] for o in r[cls]), cls
        assert r["readiness"][cls] == 1
    # the outbound edge is named, and its target is resolvable
    assert r["synonyms"][0]["target"] == "LEGACY.CUSTOMERS"
    assert r["db_links"][0]["host"] == "legacy.corp"
    assert r["context"]["current_role"] == "SALES"
    assert r["context"]["edition"] == "n/a"       # no edition concept here
    assert r["readiness"]["views_convertible"] == 1     # V_TOP parses
    (review,) = r["readiness"]["views_needing_review"]
    assert review["view"] == "V_BROKEN"


def test_oracle_declared_types_and_measured_widths_survive(ora_driver,
                                                           monkeypatch):
    """Oracle declares its lengths, so they must reach the manifest intact —
    and a CLOB, which declares none, gets a MEASURED width instead of a
    downstream guess."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    cols = {c["name"]: c["type"] for t in r["tables"]
            if t["name"] == "CUSTOMERS" for c in t["columns"]}
    assert cols["NAME"] == "VARCHAR2(80)"
    assert cols["ID"] == "NUMBER(10,0)"
    notes = {c["name"]: c["type"] for t in r["tables"]
             if t["name"] == "ORDERS" for c in t["columns"]}["NOTES"]
    assert notes == "CLOB(512)"                  # 300 chars + headroom
    assert r["readiness"]["column_sizes_measured"] == 1
    # size on disk comes from the segment, and a table without one stays 0
    by_name = {t["name"]: t for t in r["tables"]}
    assert by_name["CUSTOMERS"]["bytes"] == 65536
    assert by_name["ORDERS"]["bytes"] == 0


def test_oracle_enforced_primary_key_reaches_the_manifest(ora_driver,
                                                          monkeypatch,
                                                          tmp_path):
    """Unlike Snowflake, Oracle ENFORCES primary keys — so the key can be
    trusted as a MERGE key instead of falling through to a full reload."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    assert r["readiness"]["tables_with_primary_key"] == 1
    from metabridge.scaffold import load_table_manifest
    f = tmp_path / "m.yml"
    f.write_text(r["manifest_yaml"])
    tables, _ = load_table_manifest(str(f))
    by_name = {t["name"]: t for t in tables}
    assert by_name["CUSTOMERS"]["unique_key"] == ["ID"]
    # and the table with no declared key says so rather than inventing one
    assert "unique_key" not in by_name["ORDERS"]


def test_oracle_plsql_bodies_are_assembled_and_redacted(ora_driver,
                                                        monkeypatch):
    """ALL_SOURCE is one row per LINE, so a body only exists once the lines
    are joined — and a credential in it must never leave the backend."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    (proc,) = r["procedures"]
    assert proc["definition"].startswith("PROCEDURE sp_load IS BEGIN")
    assert "hunter2secret" not in json.dumps(r)
    assert "***REDACTED***" in proc["definition"]
    assert r["secret_findings"][0]["location"] == "procedure SALES.SP_LOAD"
    # a package carries BOTH halves — the spec declares the callable surface,
    # the body holds the logic a conversion has to read
    (pkg,) = r["packages"]
    assert "PACKAGE pkg_etl IS" in pkg["definition"]
    assert "PACKAGE BODY pkg_etl IS" in pkg["definition"]


def test_oracle_system_schemas_are_never_inventoried(ora_driver,
                                                     monkeypatch):
    """An unscoped Oracle inventory that included SYS would bury the estate
    the user asked about under thousands of internal objects."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    livecheck.introspect("oracle", dict(ORA_PARAMS, schema=""))
    tables_sql = next(s for s in ora_driver["conn"].seen
                      if "FROM ALL_TABLES" in s)
    assert "'SYS'" in tables_sql and "'SYSTEM'" in tables_sql
    assert "NOT LIKE 'APEX%'" in tables_sql


def test_oracle_long_columns_are_never_put_in_an_inline_view(ora_driver,
                                                            monkeypatch):
    """ALL_VIEWS.TEXT and friends are LONG, and a LONG in an inline view is
    ORA-00997 — so those queries must cap with ROWNUM, not a subquery."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    livecheck.introspect("oracle", dict(ORA_PARAMS))
    for view in ("ALL_VIEWS", "ALL_MVIEWS", "ALL_TRIGGERS"):
        sql = next(s for s in ora_driver["conn"].seen if view in s)
        assert not sql.startswith("SELECT * FROM ("), view
        assert "ROWNUM <=" in sql, view


def test_oracle_blocked_class_does_not_cost_the_inventory(ora_driver,
                                                          monkeypatch):
    """A user who may read tables but not the PL/SQL catalog still gets an
    inventory — and the matrix says WHY the class is empty."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    ora_driver["fail_on"] = {
        "OBJECT_TYPE = 'PROCEDURE'":
            "ORA-01031: insufficient privileges"}
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    assert r["ok"] is True                       # the run survives
    assert r["tables"] and r["functions"]        # everything else is intact
    assert r["procedures"] == []
    assert r["capabilities"]["procedures"]["status"] == "blocked_privilege"
    # and the advice names an ORACLE role, not Snowflake's SECURITYADMIN
    (rec,) = r["recommendations"]
    assert "SELECT_CATALOG_ROLE" in rec and "SECURITYADMIN" not in rec


def test_oracle_is_a_live_source_but_not_a_live_load_target():
    """Oracle is read and inventoried; MetaBridge never writes to it, and
    live_load must not claim otherwise."""
    sup = livecheck.live_support("oracle")
    assert sup["live_test"] is True and sup["introspect"] is True
    assert sup["live_load"] is False
    assert "oracle" in livecheck.LIVE_CONNECTORS


def test_console_renders_the_oracle_object_classes():
    """The estate asset list is an explicit allowlist, so a class the backend
    returns but the console does not push would silently vanish."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "web" / "templates"
            / "console.html").read_text(encoding="utf-8")
    for cls in ("d.packages", "d.triggers", "d.synonyms", "d.db_links",
                "d.scheduler_jobs"):
        assert "push(%s," % cls in html, cls


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

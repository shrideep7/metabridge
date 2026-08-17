"""Live connection check + real-time validation runner (mocked driver —
the real network path is exercised against the customer's account)."""
import json
import os
import re
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
        if "INFORMATION_SCHEMA.TABLE_CONSTRAINTS" in u:
            # Snowflake has no CHECK constraints; PKs come from SHOW
            self._rows = [
                ("PUBLIC", "ORDERS", "FK_ORDERS_CUST", "FOREIGN KEY",
                 "CUSTOMER_ID", 1, "PUBLIC", "CUSTOMERS", "ID", None)]
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
            self._rows = [("PUBLIC", "CUSTOMERS", "BASE TABLE", 1200, 9000,
                           None),
                          ("PUBLIC", "ORDERS", "BASE TABLE", 5400, 20000,
                           "LINEAR(O_ORDERDATE)"),
                          ("PUBLIC", "V_TOP", "VIEW", 0, 0, None)]
            self._row = self._rows[0]
            return self
        if "INFORMATION_SCHEMA.COLUMNS" in u:
            # enriched shape: ..., full_type, is_nullable, column_default
            self._rows = [
                ("PUBLIC", "CUSTOMERS", "ID", "NUMBER", None, None, None,
                 None, "NO", None),
                ("PUBLIC", "CUSTOMERS", "NAME", "TEXT", None, None, None,
                 None, "YES", "'unknown'"),
                ("PUBLIC", "ORDERS", "ID", "NUMBER", None, None, None,
                 None, "NO", None)]
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
    # The SUBJECT is derived, never hardcoded. This guard named Teradata,
    # then Oracle, and each in turn gained a driver and broke it — the test
    # was rewritten twice to chase a moving fact. What it actually asserts is
    # that a connector WITHOUT a driver says so instead of failing obscurely,
    # so ask the registry which one that currently is and the next driver to
    # land moves the subject rather than breaking the test.
    from metabridge.connectors.base import get_registry
    subject = next((s.key for s in get_registry().all()
                    if s.key not in livecheck.LIVE_CONNECTORS), "")
    assert subject, "every connector now has a live driver — retire this test"
    assert livecheck.live_support(subject)["live_test"] is False
    r = livecheck.test_connection(subject, {})
    assert r["ok"] is False and "not implemented" in r["error"]


def test_teradata_is_live_supported():
    caps = livecheck.live_support("teradata")
    assert caps["live_test"] is True and caps["introspect"] is True
    # Reading the catalog is not the same as writing data: live LOAD stays
    # certified for Snowflake/Databricks only, so this must not claim it.
    assert caps["live_load"] is False


@pytest.mark.parametrize("code,length,total,frac,chartype,udt,expect", [
    # Every case is a row observed in a real DBC.ColumnsV, checked against
    # what Teradata's own TYPE() reports for the same column. Which field
    # carries the parameter differs per type, and reading ColumnLength
    # uniformly is what turned TIME(6) into VARCHAR(6).
    ("I1", 1, None, None, 0, None, "BYTEINT"),
    ("I2", 2, None, None, 0, None, "SMALLINT"),
    ("I", 4, None, None, 0, None, "INTEGER"),
    ("I8", 8, None, None, 0, None, "BIGINT"),
    ("D", 8, 18, 2, 0, None, "DECIMAL(18,2)"),
    ("CF", 12, None, None, 1, None, "CHAR(12)"),
    ("CV", 30, None, None, 1, None, "VARCHAR(30)"),
    ("CO", 1048576, None, None, 1, None, "CLOB(1048576)"),
    ("BO", 65536, None, None, 0, None, "BLOB(65536)"),
    ("BV", 1024, None, None, 0, None, "VARBYTE(1024)"),
    ("DA", 4, None, None, 0, None, "DATE"),
    # ColumnLength is 15 (display width) and the precision is in
    # DecimalFractionalDigits — the case that produced VARCHAR(6).
    ("AT", 15, None, 6, 0, None, "TIME(6)"),
    ("TS", 26, None, 6, 0, None, "TIMESTAMP(6)"),
    ("SZ", 32, None, 6, 0, None, "TIMESTAMP(6) WITH TIME ZONE"),
    ("DS", 21, 4, 6, 0, None, "INTERVAL DAY(4) TO SECOND(6)"),
    ("HM", 6, 2, None, 0, None, "INTERVAL HOUR(2) TO MINUTE"),
    ("PD", 8, None, None, 0, None, "PERIOD(DATE)"),
    ("JN", 4096, None, None, 1, None, "JSON(4096)"),
    # ST_GEOMETRY has no dedicated code; it arrives as a generic UDT.
    ("UT", 16000, None, None, 1, "ST_GEOMETRY", "ST_GEOMETRY"),
])
def test_teradata_native_type_matches_catalog(code, length, total, frac,
                                              chartype, udt, expect):
    assert livecheck._td_native_type(code, length, total, frac,
                                     chartype, udt) == expect


def test_teradata_number_without_precision_stays_bare():
    """DBC reports -128 for "unspecified". Substituting it yields
    NUMBER(-128,-128); defaulting it to NUMBER(38,0) truncates every
    fraction. Bare NUMBER is what the source actually declares."""
    assert livecheck._td_native_type("N", 18, -128, -128, 0, None) == "NUMBER"


def test_teradata_unicode_length_is_characters_not_bytes():
    """ColumnLength is bytes, and a UNICODE column stores two per character,
    so a VARCHAR(120) UNICODE reports 240."""
    assert livecheck._td_native_type("CV", 240, None, None, 2,
                                     None) == "VARCHAR(120)"
    assert livecheck._td_native_type("CV", 30, None, None, 1,
                                     None) == "VARCHAR(30)"


def test_teradata_unknown_type_code_is_reported_not_guessed():
    """An unmapped code surfaces verbatim rather than silently becoming
    VARCHAR — the caller can then see what it was."""
    assert livecheck._td_native_type("ZZ", 10, None, None, 0, None) == "ZZ"


class _FakeTdCursor:
    """Minimal cursor returning one canned result set."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql):            # noqa: D102 — signature only
        self._sql = sql

    def fetchall(self):                # noqa: D102
        return self._rows


# (DatabaseName, CreatorName) exactly as DBC.DatabasesV reports on the trial
# where the leak was found.
_TD_DBS = [
    ("EDW_MASTER", "demo_user"), ("EDW_FACT", "demo_user"),
    ("EDW_REF", "demo_user"), ("EDW_STG", "demo_user"),
    ("demo_user", "DBC"),
    ("tdwm", "DBC"), ("TDaaS_DB", "DBC"), ("DBC", "DBC"),
    ("SYSLIB", "DBC"), ("SysAdmin", "DBC"), ("Crashdumps", "DBC"),
    ("TD_SERVER_DB", "DBC"), ("mldb", "DBC"),
]


def test_teradata_scan_excludes_system_databases():
    """The four EDW_* databases are user data; tdwm and TDaaS_DB are the
    server's. Missing them scanned 52 tables where 11 were real, and
    scaffolded a dbt model for each one."""
    keep, skipped = livecheck._td_user_databases(
        _FakeTdCursor(_TD_DBS), "demo_user")
    assert keep == ["EDW_FACT", "EDW_MASTER", "EDW_REF", "EDW_STG",
                    "demo_user"]
    for sys_db in ("tdwm", "TDaaS_DB", "DBC", "SYSLIB", "TD_SERVER_DB"):
        assert sys_db in skipped


def test_teradata_login_database_is_never_excluded():
    """demo_user is DBC-created, and on many installations it is exactly
    where the user's tables live — so the creator rule must not drop it."""
    keep, _ = livecheck._td_user_databases(
        _FakeTdCursor([("demo_user", "DBC"), ("tdwm", "DBC")]), "demo_user")
    assert keep == ["demo_user"]


def test_teradata_rowcount_accepts_the_decimal_the_catalog_returns():
    """DBC.StatsV.RowCount is a DECIMAL. It arrives as Decimal('2.0') over
    teradatasql and as '2.0' over other clients — and int('2.0') raises, so
    a naive parse reported a measured table as 0 rows."""
    from decimal import Decimal
    for value in (2, 2.0, Decimal("2.0"), Decimal("2"), "2", "2.0"):
        assert livecheck._td_int(value) == 2, value
    # genuinely absent stays at the caller's sentinel, NOT 0
    for value in (None, "", "not-a-number"):
        assert livecheck._td_int(value, -1) == -1, value


def test_teradata_scan_falls_back_when_everything_looks_system_owned():
    """A site where a DBA created every database while logged in as DBC must
    still get an inventory, not an empty estate."""
    keep, _ = livecheck._td_user_databases(
        _FakeTdCursor([("SALES_DW", "DBC"), ("FIN_DW", "DBC"),
                       ("tdwm", "DBC")]), "")
    assert keep == ["FIN_DW", "SALES_DW"]        # names alone decide


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
    assert by_name["CUSTOMERS"]["columns"][0] == {
        "name": "ID", "type": "NUMBER", "nullable": False,
        "default": "", "generated": ""}
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
            # ..., NULL AS full_type, is_nullable, column_default
            self._rows = [
                ("public", "customers", "id", "integer", None, 32, 0,
                 None, "NO", "nextval('customers_id_seq')"),
                ("public", "customers", "name", "character varying", 80,
                 None, None, None, "YES", None),
                ("public", "orders", "id", "integer", None, 32, 0,
                 None, "NO", "nextval('orders_id_seq')")]
        elif "RELTUPLES" in u:          # the row-estimate query, not just
            self._rows = [("public", "customers", 1200),   # any pg_class use
                          ("public", "orders", 5400)]
        elif "INFORMATION_SCHEMA.VIEWS" in u:
            self._rows = [("staging", "v_top", "SELECT id FROM customers")]
        elif "INFORMATION_SCHEMA.TRIGGERS" in u:
            self._rows = [("public", "trg_audit", "orders", "INSERT",
                           "AFTER")]
        elif "PG_INDEXES" in u:
            self._rows = [("public", "orders", "orders_dt_idx",
                           "CREATE INDEX orders_dt_idx ON orders (dt)")]
        elif "TABLE_PRIVILEGES" in u:
            self._rows = [("public", "orders", "analyst", "SELECT")]
        elif "PG_PARTITIONED_TABLE" in u:
            self._rows = [("public", "orders", "RANGE", "dt", 1)]
        elif "INFORMATION_SCHEMA.TABLE_CONSTRAINTS" in u:
            self._rows = [
                ("public", "customers", "customers_pkey", "PRIMARY KEY",
                 "id", 1, None, None, None, None),
                ("public", "orders", "orders_customer_fkey", "FOREIGN KEY",
                 "customer_id", 1, "public", "customers", "id", None),
                ("public", "orders", "orders_amount_check", "CHECK",
                 "amount", 1, None, None, None, "amount >= 0"),
                ("public", "orders", "2200_16385_1_not_null", "CHECK",
                 "id", 1, None, None, None, "id IS NOT NULL")]
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
        elif "INFORMATION_SCHEMA.TABLE_CONSTRAINTS" in u:
            self._rows = [
                ("sales", "orders", "orders_pk", "PRIMARY KEY", "id", 1,
                 None, None, None, None)]
        elif "INFORMATION_SCHEMA.TABLE_STATISTICS" in u:
            self._rows = [("sales", "orders", 4200)]
        elif "INFORMATION_SCHEMA.TABLES" in u:
            self._rows = [("sales", "orders", "MANAGED"),
                          ("sales", "v_top", "VIEW")]
        elif "PARTITION_INDEX IS NOT NULL" in u:
            self._rows = [("sales", "orders", "PARTITION BY", "dt", 1)]
        elif "INFORMATION_SCHEMA.COLUMNS" in u:
            self._rows = [("sales", "orders", "id", "bigint", None, None,
                           None, "bigint", "NO", None, None)]
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


def _like_to_rx(pat):
    """Oracle LIKE -> regex: % is any run, _ is one character.

    re.escape leaves % alone on modern Python but escaped it on older ones,
    so both spellings are substituted; _ it never escapes.
    """
    return ("^" + re.escape(pat).replace(r"\%", ".*").replace("%", ".*")
            .replace("_", ".") + "$")


def _ora_excluded_by(sql_upper, name):
    """Apply the query's own `TABLE_NAME NOT LIKE '...'` clauses, so the fake
    filters the way Oracle would. The test then genuinely verifies the SQL
    carries the exclusions instead of trusting a hand-curated row list."""
    return any(re.match(_like_to_rx(p), name.upper())
               for p in re.findall(r"TABLE_NAME NOT LIKE '([^']*)'",
                                   sql_upper))


def _ora_owner_ok(sql_upper, args, owner):
    """Apply the query's own OWNER predicate the way Oracle would: a bound
    :owner scopes to one schema, otherwise the SQL carries an explicit NOT IN
    list. The lookbehind keeps TABLE_OWNER from matching as OWNER."""
    bound = (args or {}).get("owner") if isinstance(args, dict) else None
    if bound:
        return owner == bound
    m = re.search(r"(?<![A-Z_])OWNER NOT IN \(([^)]*)\)", sql_upper)
    if not m:
        return True
    return owner not in {s.strip().strip("'") for s in m.group(1).split(",")}


def _ora_target_ok(sql_upper, table_owner):
    """A synonym's TARGET owner filter. NULL means the synonym resolves
    through a database link, and Oracle keeps it (the SQL says IS NULL OR)."""
    m = re.search(r"TABLE_OWNER NOT IN \(([^)]*)\)", sql_upper)
    if not m or table_owner is None:
        return True
    return table_owner not in {s.strip().strip("'")
                               for s in m.group(1).split(",")}


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
            # AQ$/MLOG$/BIN$ rows are what a real Oracle schema returns
            # alongside its business tables; the query must exclude them.
            rows = [("SALES", "CUSTOMERS", 1200), ("SALES", "ORDERS", 5400),
                    ("SALES", "AQ$_CLAIM_EVT_H", 0),
                    ("SALES", "AQ$_POLICY_EVT_S", 0),
                    ("SALES", "MLOG$_ORDERS", 0),
                    ("SALES", "BIN$abc123==$0", 0)]
            self._rows = [r for r in rows
                          if not _ora_excluded_by(u, r[1])]
        elif "ALL_TAB_COLS" in u:
            # enriched: ..., full_type, nullable, data_default, virtual expr.
            # TOTAL_INC_TAX is a VIRTUAL column — migrated as an ordinary one
            # it would silently become stored data instead of a derivation.
            self._rows = [
                ("SALES", "CUSTOMERS", "ID", "NUMBER", 0, 10, 0, None,
                 "N", None, None),
                ("SALES", "CUSTOMERS", "NAME", "VARCHAR2", 80, None, None,
                 None, "N", "'unknown'", None),
                ("SALES", "ORDERS", "ID", "NUMBER", 0, None, None, None,
                 "Y", None, None),
                ("SALES", "ORDERS", "NOTES", "CLOB", 0, None, None, None,
                 "Y", None, None),
                ("SALES", "ORDERS", "TOTAL_INC_TAX", "NUMBER", 0, 12, 2,
                 None, "Y", "amount*1.2", "amount*1.2")]
        elif "ALL_TAB_COLUMNS" in u:          # the plain fallback projection
            self._rows = [("SALES", "CUSTOMERS", "ID", "NUMBER"),
                          ("SALES", "CUSTOMERS", "NAME", "VARCHAR2"),
                          ("SALES", "ORDERS", "ID", "NUMBER"),
                          ("SALES", "ORDERS", "NOTES", "CLOB")]
        elif "ALL_SEGMENTS" in u:
            self._rows = [("SALES", "CUSTOMERS", 65536)]
        elif "MAX(LENGTH(NOTES))" in u:
            self._rows = [(300,)]
        elif "ALL_VIEWS" in u:
            self._rows = [("SALES", "V_TOP",
                           "SELECT id, name FROM customers WHERE ROWNUM <= 10"),
                          ("SALES", "V_BROKEN", "SELECT FROM WHERE (((")]
        elif "ALL_CONSTRAINTS" in u:
            # schema, table, name, kind, column, position,
            # ref_schema, ref_table, ref_column, check condition
            self._rows = [
                ("SALES", "CUSTOMERS", "PK_CUSTOMERS", "P", "ID", 1,
                 None, None, None, None),
                ("SALES", "ORDERS", "FK_ORDERS_CUST", "R", "CUSTOMER_ID", 1,
                 "SALES", "CUSTOMERS", "ID", None),
                ("SALES", "ORDERS", "UQ_ORDERS_REF", "U", "ORDER_REF", 1,
                 None, None, None, None),
                ("SALES", "ORDERS", "CK_ORDERS_AMT", "C", "AMOUNT", 1,
                 None, None, None, "amount >= 0"),
                # Oracle records every NOT NULL column as a CHECK of its own
                ("SALES", "ORDERS", "SYS_C0011", "C", "ID", 1,
                 None, None, None, '"ID" IS NOT NULL')]
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
            # A stock 23ai database files thousands of dictionary synonyms
            # under PUBLIC. Two of each kind here: Oracle's own, the user's
            # own public one, a private one, and one resolved via db_link
            # (NULL target owner).
            rows = [("SALES", "SYN_CUST", "LEGACY", "CUSTOMERS", ""),
                    ("PUBLIC", "APP_ORDERS", "SALES", "ORDERS", ""),
                    ("PUBLIC", "ALL_TABLES", "SYS", "ALL_TABLES", ""),
                    ("PUBLIC", "DBA_USERS", "SYS", "DBA_USERS", ""),
                    ("SALES", "SYN_REMOTE", None, "REMOTE_TAB", "LEGACY_LINK")]
            self._rows = [r for r in rows
                          if _ora_owner_ok(u, args, r[0])
                          and _ora_target_ok(u, r[2])]
        elif "ALL_DB_LINKS" in u:
            rows = [("SALES", "DBL_LEGACY", "legacy.corp", "ETL"),
                    ("PUBLIC", "DBL_SHARED", "shared.corp", "RPT")]
            self._rows = [r for r in rows if _ora_owner_ok(u, args, r[0])]
        elif "ALL_QUEUES" in u:
            rows = [("SALES", "CLAIM_EVT_Q", "CLAIM_EVT_QT", "OBJECT"),
                    ("SALES", "AQ$_CLAIM_EVT_QT_E", "CLAIM_EVT_QT",
                     "EXCEPTION")]
            self._rows = [r for r in rows
                          if not any(re.match(_like_to_rx(p), r[1])
                                     for p in re.findall(
                                         r"NAME NOT LIKE '([^']*)'", u))]
        elif "ALL_SCHEDULER_JOBS" in u:
            self._rows = [("SALES", "JOB_NIGHTLY", "CALENDAR",
                           "FREQ=DAILY", "SCHEDULED", "BEGIN pkg_etl.run; END;")]
        elif "ALL_TAB_PRIVS" in u:
            self._rows = [("ANALYST", "SALES", "ORDERS", "SELECT")]
        elif "ALL_TYPES" in u:
            self._rows = [("SALES", "ADDRESS_T"), ("SALES", "PHONE_LIST_T")]
        elif "ALL_SCHEDULER_PROGRAMS" in u:
            self._rows = [("SALES", "PRG_NIGHTLY", "STORED_PROCEDURE",
                           "pkg_etl.run")]
        elif "ALL_SCHEDULER_SCHEDULES" in u:
            self._rows = [("SALES", "SCH_DAILY", "CALENDAR", "FREQ=DAILY")]
        elif "ALL_SCHEDULER_CHAINS" in u:
            self._rows = [("SALES", "CHN_LOAD", 3, 4)]
        elif "ALL_RULES" in u:
            self._rows = [("SALES", "RUL_HIGH_VALUE", "amount > 10000")]
        elif "ALL_INDEXES" in u:
            self._rows = [("SALES", "IX_ORDERS_DT", "SALES", "ORDERS",
                           "NORMAL", "NONUNIQUE")]
        elif "ALL_PART_TABLES" in u:
            self._rows = [("SALES", "ORDERS", "RANGE", "ORDER_DATE", 1)]
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
    assert [o["name"] for o in r["synonyms"]] == ["SYN_CUST", "SYN_REMOTE"]
    assert [o["name"] for o in r["db_links"]] == ["DBL_LEGACY"]
    assert [o["name"] for o in r["scheduler_jobs"]] == ["JOB_NIGHTLY"]
    assert [o["name"] for o in r["materialized_views"]] == ["MV_SALES"]
    assert [o["name"] for o in r["sequences"]] == ["SEQ_ORDER_ID"]
    assert [o["name"] for o in r["queues"]] == ["CLAIM_EVT_Q"]
    assert r["grants"][0] == {"role": "ANALYST", "privilege": "SELECT",
                              "granted_on": "TABLE",
                              "object": "SALES.ORDERS"}
    # every schema-bearing class carries the Level 2 filter key
    for cls in ("functions", "procedures", "packages", "triggers",
                "synonyms", "db_links", "scheduler_jobs",
                "materialized_views", "sequences", "queues"):
        assert all(o["schema"] for o in r[cls]), cls
        assert r["readiness"][cls] == len(r[cls]), cls
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


def test_oracle_procedure_logic_reaches_the_scaffold_manifest(
        ora_driver, monkeypatch, tmp_path):
    """Analysis -> Pipeline Studio is a ONE-file handoff. A procedure that is
    not in that file is transformation logic the migration leaves behind: the
    tables land and every generated model is a pass-through."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    f = tmp_path / "m.yml"
    f.write_text(r["manifest_yaml"], encoding="utf-8")

    from metabridge.scaffold import load_procedures, load_table_manifest
    assert {t["name"] for t in load_table_manifest(str(f))[0]} == \
        {"CUSTOMERS", "ORDERS"}
    procs = {p["name"]: p for p in load_procedures(str(f))}
    # packages hold as much Oracle ETL as standalone procedures do
    assert set(procs) == {"SP_LOAD", "PKG_ETL"}
    assert procs["SP_LOAD"]["schema"] == "SALES"
    assert "BEGIN" in procs["SP_LOAD"]["definition"]
    # a manifest is a file people mail around: the redaction has to hold here
    # too, not only in the API response
    assert "hunter2secret" not in f.read_text(encoding="utf-8")


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


def test_oracle_feature_schemas_are_not_the_users_estate(ora_driver,
                                                         monkeypatch):
    """A stock 23ai database keeps 13 AI Vector Search tables in VECSYS. Read
    as estate they outnumbered a real five-table schema three to one, and every
    number computed from the inventory — object count, conversion rate,
    governance — was reported against Oracle's own index metadata."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    livecheck.introspect("oracle", dict(ORA_PARAMS, schema=""))
    tables_sql = next(s for s in ora_driver["conn"].seen
                      if "FROM ALL_TABLES" in s)
    for schema in ("VECSYS", "SYSMAN", "ORDS_METADATA", "OWBSYS"):
        assert "'%s'" % schema in tables_sql, schema


def test_oracle_generated_table_names_are_excluded_in_any_schema(ora_driver,
                                                                 monkeypatch):
    """`$` is Oracle's own marker for a generated name. Filtering on it holds
    when the scan is SCOPED to a schema, where the system-schema list cannot
    help — and it catches the next release's feature tables without a code
    change."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    livecheck.introspect("oracle", dict(ORA_PARAMS))       # scoped scan
    tables_sql = next(s for s in ora_driver["conn"].seen
                      if "FROM ALL_TABLES" in s)
    # VECTOR$INDEX, HNSW_IND_STATS$ and DV_HITCOUNTS$ all carry it
    assert "NOT LIKE '%$%'" in tables_sql
    # and the columns query filters identically, or it drags their columns
    # across the wire only to discard them on lookup
    cols_sql = next(s for s in ora_driver["conn"].seen
                    if "ALL_TAB_COLS" in s.upper())
    assert "NOT LIKE '%$%'" in cols_sql


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


def test_oracle_generated_tables_are_not_reported_as_estate(ora_driver,
                                                            monkeypatch):
    """AQ$/MLOG$/BIN$ tables are Oracle's own machinery living in the user's
    schema. No catalog flag marks them, so only the name does — and counting
    them inflates the table count and invents migration work that does not
    exist."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    assert {t["name"] for t in r["tables"]} == {"CUSTOMERS", "ORDERS"}
    assert r["readiness"]["tables"] == 2
    # the columns query is filtered too, so their columns never cross the wire
    cols_sql = next(s for s in ora_driver["conn"].seen
                    if "ALL_TAB_COLS" in s)
    assert "NOT LIKE 'AQ$%'" in cols_sql


def test_oracle_reports_the_queue_not_its_generated_tables(ora_driver,
                                                           monkeypatch):
    """A queue becomes a stream+task pair or an external broker, so it is real
    migration work — and it would be invisible if the AQ$ tables were filtered
    out and nothing took their place. Oracle's own exception queues stay out."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    assert r["queues"] == [{"schema": "SALES", "name": "CLAIM_EVT_Q",
                            "table": "CLAIM_EVT_QT", "type": "OBJECT"}]


def test_oracle_dictionary_synonyms_do_not_flood_the_inventory(ora_driver,
                                                               monkeypatch):
    """A stock 23ai database files THOUSANDS of synonyms under PUBLIC. Left
    in, they push the class past _MAX_OBJECTS, so it comes back flagged
    truncated and the console honestly reports a partial list — for an estate
    with a dozen real synonyms. They are identified by their TARGET owner, so
    the user's own public synonyms still show."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS, schema=""))
    names = [o["name"] for o in r["synonyms"]]
    assert "APP_ORDERS" in names          # PUBLIC, but points at SALES
    assert "SYN_CUST" in names
    assert "ALL_TABLES" not in names      # PUBLIC, points at SYS
    assert "DBA_USERS" not in names
    assert r["capabilities"]["synonyms"].get("truncated") is not True


def test_oracle_remote_synonym_survives_the_target_filter(ora_driver,
                                                          monkeypatch):
    """A synonym resolved through a database link has no local target owner,
    and `NULL NOT IN (...)` is NULL — which would silently drop precisely the
    cross-system references worth knowing about."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS, schema=""))
    remote = next(o for o in r["synonyms"] if o["name"] == "SYN_REMOTE")
    assert remote["db_link"] == "LEGACY_LINK"
    assert remote["target"] == "REMOTE_TAB"


def test_oracle_public_db_links_are_kept(ora_driver, monkeypatch):
    """Oracle ships no public database links, so every one is the user's own
    outbound edge — excluding PUBLIC wholesale would hide a whole system."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS, schema=""))
    assert [o["name"] for o in r["db_links"]] == ["DBL_LEGACY", "DBL_SHARED"]


def test_oracle_public_is_excluded_from_the_ordinary_classes(ora_driver,
                                                             monkeypatch):
    """PUBLIC is a pseudo-owner, not a schema. Only synonyms and database
    links re-admit it; everything else must keep it out."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    livecheck.introspect("oracle", dict(ORA_PARAMS, schema=""))
    tables_sql = next(s for s in ora_driver["conn"].seen
                      if "FROM ALL_TABLES" in s)
    assert "'PUBLIC'" in tables_sql
    syn_sql = next(s for s in ora_driver["conn"].seen if "ALL_SYNONYMS" in s)
    owners = re.search(r"(?<![A-Z_])OWNER NOT IN \(([^)]*)\)", syn_sql)
    assert "'PUBLIC'" not in owners.group(1)


def test_oracle_column_detail_reaches_the_manifest(ora_driver, monkeypatch,
                                                   tmp_path):
    """NOT NULL, defaults and virtual columns are the schema, not decoration.
    A virtual column loaded as ordinary data is how a migrated table ends up
    quietly wrong."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    cols = {c["name"]: c for t in r["tables"]
            if t["name"] == "CUSTOMERS" for c in t["columns"]}
    assert cols["ID"]["nullable"] is False
    assert cols["NAME"]["default"] == "'unknown'"
    virt = {c["name"]: c for t in r["tables"]
            if t["name"] == "ORDERS" for c in t["columns"]}["TOTAL_INC_TAX"]
    assert virt["generated"] == "amount*1.2"

    rd = r["readiness"]
    assert rd["columns"] == 5
    assert rd["columns_not_null"] == 2
    assert rd["columns_with_default"] == 2       # 'unknown' + the virtual one
    assert rd["generated_columns"] == 1

    # and it survives the handoff to the scaffold, which is the point
    from metabridge.scaffold import load_table_manifest
    f = tmp_path / "m.yml"
    f.write_text(r["manifest_yaml"])
    tables, _ = load_table_manifest(str(f))
    mcols = {c["name"]: c for t in tables if t["name"] == "CUSTOMERS"
             for c in t["columns"]}
    assert mcols["ID"]["nullable"] is False
    assert mcols["NAME"]["default"] == "'unknown'"
    # a plain nullable column with no default stays quiet, so the NOT NULLs
    # that matter are not buried under a `nullable: true` on every line
    ocols = {c["name"]: c for t in tables if t["name"] == "ORDERS"
             for c in t["columns"]}
    assert "nullable" not in ocols["NOTES"]
    assert "default" not in ocols["NOTES"]
    assert ocols["TOTAL_INC_TAX"]["generated"] == "amount*1.2"


def test_snowflake_column_detail_is_carried_too(fake_driver, monkeypatch):
    """The column projection is shared, so every connector gains this at
    once — this pins that the Snowflake path really does."""
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    cols = {c["name"]: c for t in r["tables"]
            if t["name"] == "CUSTOMERS" for c in t["columns"]}
    assert cols["ID"]["nullable"] is False
    assert cols["NAME"]["nullable"] is True
    assert cols["NAME"]["default"] == "'unknown'"
    assert r["readiness"]["columns_not_null"] == 2


def test_pg_column_detail_is_carried_too(pg_driver, monkeypatch):
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    r = livecheck.introspect("postgres", dict(PG_PARAMS))
    cols = {c["name"]: c for t in r["tables"]
            if t["name"] == "customers" for c in t["columns"]}
    assert cols["id"]["nullable"] is False
    assert cols["name"]["nullable"] is True
    assert cols["id"]["default"] == "nextval('customers_id_seq')"
    assert r["readiness"]["columns_not_null"] == 2


def test_unknown_nullability_reads_as_permissive():
    """NOT NULL is a CONSTRAINT. Inventing one the source does not have makes
    the target reject rows the source accepted; missing one only loses
    enforcement. So anything unrecognised has to mean "nullable"."""
    assert livecheck._is_nullable("N") is False
    assert livecheck._is_nullable("NO") is False
    assert livecheck._is_nullable(False) is False
    assert livecheck._is_nullable("Y") is True
    assert livecheck._is_nullable(None) is True        # unknown
    assert livecheck._is_nullable("") is True
    assert livecheck._is_nullable("whatever") is True


def test_the_word_null_is_not_a_default():
    """Catalogs write an absent default as the literal string NULL, which is
    the ABSENCE of a default, not a default OF null."""
    assert livecheck._clean_default("NULL") == ""
    assert livecheck._clean_default("  null  ") == ""
    assert livecheck._clean_default(None) == ""
    assert livecheck._clean_default("0") == "0"


def test_plain_column_fallback_keeps_the_tuple_shape(monkeypatch):
    """When the enriched projection fails, callers must not have to branch —
    the fallback returns the same 7-slot row with safe values."""
    def run(sql):
        if "character_maximum_length" in sql:
            raise RuntimeError("column does not exist")
        return [("s", "t", "c", "varchar")]

    (row,) = livecheck._fetch_columns(
        run,
        "SELECT a, b, c, d, character_maximum_length FROM x",
        "SELECT a, b, c, d FROM x")
    assert row == ("s", "t", "c", "varchar", True, "", "")


def test_oracle_constraints_carry_the_load_order_graph(ora_driver,
                                                       monkeypatch):
    """A foreign key is the only record of which table must load first, and
    nothing else in the inventory carries that dependency."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    by_name = {c["name"]: c for c in r["constraints"]}
    fk = by_name["FK_ORDERS_CUST"]
    assert fk["type"] == "FOREIGN KEY"
    assert fk["table"] == "ORDERS" and fk["columns"] == ["CUSTOMER_ID"]
    assert fk["ref_table"] == "SALES.CUSTOMERS"
    assert fk["ref_columns"] == ["ID"]
    assert by_name["UQ_ORDERS_REF"]["type"] == "UNIQUE"
    assert by_name["CK_ORDERS_AMT"]["expression"] == "amount >= 0"
    rd = r["readiness"]
    assert rd["primary_keys"] == 1 and rd["foreign_keys"] == 1
    assert rd["unique_constraints"] == 1 and rd["check_constraints"] == 1


def test_not_null_is_not_reported_as_a_check_constraint(ora_driver,
                                                        monkeypatch):
    """Oracle and PostgreSQL both record every NOT NULL column as a CHECK of
    its own. Nullability is already carried per column, so letting these
    through buries the real business rules under hundreds of restatements."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    assert "SYS_C0011" not in {c["name"] for c in r["constraints"]}
    assert r["readiness"]["check_constraints"] == 1


def test_pg_primary_keys_finally_reach_the_manifest(pg_driver, monkeypatch,
                                                    tmp_path):
    """PostgreSQL genuinely ENFORCES primary keys — the most trustworthy of
    any connector here — and they were not read at all, so every table fell
    through to a FULL reload on every run."""
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    r = livecheck.introspect("postgres", dict(PG_PARAMS))
    assert r["readiness"]["tables_with_primary_key"] == 1
    assert r["readiness"]["foreign_keys"] == 1
    from metabridge.scaffold import load_table_manifest
    f = tmp_path / "m.yml"
    f.write_text(r["manifest_yaml"])
    tables, _ = load_table_manifest(str(f))
    by_name = {t["name"]: t for t in tables}
    assert by_name["customers"]["unique_key"] == ["id"]
    # the NOT NULL check PostgreSQL generates is filtered out here too
    assert "2200_16385_1_not_null" not in {c["name"]
                                           for c in r["constraints"]}


def test_databricks_declared_keys_reach_the_manifest(dbx_driver, monkeypatch,
                                                     tmp_path):
    """Unity Catalog constraints are informational, but a declared primary
    key is still the difference between a MERGE and a full reload."""
    monkeypatch.setenv("MB_DATABRICKS_TOKEN", "t")
    r = livecheck.introspect("databricks", dict(DBX_PARAMS))
    assert r["readiness"]["tables_with_primary_key"] == 1
    from metabridge.scaffold import load_table_manifest
    f = tmp_path / "m.yml"
    f.write_text(r["manifest_yaml"])
    tables, _ = load_table_manifest(str(f))
    assert {t["name"]: t for t in tables}["orders"]["unique_key"] == ["id"]


def test_snowflake_keeps_show_primary_keys_and_gains_foreign_keys(
        fake_driver, monkeypatch):
    """SHOW PRIMARY KEYS reports keys INFORMATION_SCHEMA sometimes will not,
    so it stays the PK source; the ANSI query adds the rest beside it."""
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    kinds = {c["name"]: c["type"] for c in r["constraints"]}
    assert kinds["FK_ORDERS_CUST"] == "FOREIGN KEY"
    assert r["readiness"]["foreign_keys"] == 1


def test_a_composite_key_keeps_its_column_order():
    """A composite key arrives one row per column. Reordering it would
    produce a MERGE that matches on the wrong columns."""
    rows = [("s", "t", "pk", "P", "B", 2, None, None, None, None),
            ("s", "t", "pk", "P", "A", 1, None, None, None, None),
            ("s", "t", "fk", "R", "Y", 2, "s", "p", "PY", None),
            ("s", "t", "fk", "R", "X", 1, "s", "p", "PX", None)]
    by_name = {c["name"]: c for c in livecheck._group_constraints(rows)}
    assert by_name["pk"]["columns"] == ["A", "B"]
    assert by_name["fk"]["columns"] == ["X", "Y"]
    assert by_name["fk"]["ref_columns"] == ["PX", "PY"]
    assert livecheck._primary_keys_from(
        list(by_name.values()))[("s", "t")] == ["A", "B"]


def test_oracle_reports_the_classes_a_table_inventory_cannot_see(
        ora_driver, monkeypatch):
    """Types, the rest of the scheduler, rules and indexes all exist in a
    real estate and none of them appear in a table-and-view inventory —
    which is exactly how "26 tables, straightforward" hides a rewrite."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    assert [o["name"] for o in r["types"]] == ["ADDRESS_T", "PHONE_LIST_T"]
    assert [o["name"] for o in r["scheduler_programs"]] == ["PRG_NIGHTLY"]
    assert [o["name"] for o in r["scheduler_schedules"]] == ["SCH_DAILY"]
    assert [o["name"] for o in r["scheduler_chains"]] == ["CHN_LOAD"]
    assert [o["name"] for o in r["rules"]] == ["RUL_HIGH_VALUE"]
    assert r["rules"][0]["definition"] == "amount > 10000"
    assert [o["name"] for o in r["indexes"]] == ["IX_ORDERS_DT"]
    assert r["indexes"][0]["table"] == "SALES.ORDERS"
    # the scheduler classes that already worked are still there
    assert [o["name"] for o in r["scheduler_jobs"]] == ["JOB_NIGHTLY"]
    assert r["grants"][0]["role"] == "ANALYST"


def test_oracle_partitioning_is_visible_as_a_table_attribute(ora_driver,
                                                             monkeypatch,
                                                             tmp_path):
    """Partitioning is not an object, which is why it goes missing — and
    it is the strongest evidence for the target's clustering key."""
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    r = livecheck.introspect("oracle", dict(ORA_PARAMS))
    orders = {t["name"]: t for t in r["tables"]}["ORDERS"]
    assert orders["partition_strategy"] == "RANGE"
    assert orders["partition_key"] == ["ORDER_DATE"]
    assert r["readiness"]["partitioned_tables"] == 1
    # and it is carried to the scaffold as evidence, not applied
    from metabridge.scaffold import load_table_manifest
    f = tmp_path / "m.yml"
    f.write_text(r["manifest_yaml"])
    tables, _ = load_table_manifest(str(f))
    assert {t["name"]: t for t in tables}["ORDERS"]["partition_key"] \
        == ["ORDER_DATE"]


def test_pg_triggers_indexes_and_grants_are_finally_read(pg_driver,
                                                         monkeypatch):
    """PostgreSQL HAS triggers, and no cloud warehouse does — each one is
    logic that has to move. Grants were read for every connector but this."""
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    r = livecheck.introspect("postgres", dict(PG_PARAMS))
    assert [o["name"] for o in r["triggers"]] == ["trg_audit"]
    assert r["triggers"][0]["table"] == "orders"
    assert [o["name"] for o in r["indexes"]] == ["orders_dt_idx"]
    assert r["grants"][0] == {"role": "analyst", "privilege": "SELECT",
                              "granted_on": "TABLE",
                              "object": "public.orders"}
    assert r["readiness"]["triggers"] == 1
    assert r["readiness"]["grants"] == 1


def test_pg_declarative_partitioning_is_reported(pg_driver, monkeypatch):
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    r = livecheck.introspect("postgres", dict(PG_PARAMS))
    orders = {t["name"]: t for t in r["tables"]}["orders"]
    assert orders["partition_strategy"] == "RANGE"
    assert orders["partition_key"] == ["dt"]
    assert r["readiness"]["partitioned_tables"] == 1


def test_databricks_partitioning_is_reported(dbx_driver, monkeypatch):
    """Unity Catalog records partitioning on the COLUMN, not the table."""
    monkeypatch.setenv("MB_DATABRICKS_TOKEN", "t")
    r = livecheck.introspect("databricks", dict(DBX_PARAMS))
    orders = {t["name"]: t for t in r["tables"]}["orders"]
    assert orders["partition_key"] == ["dt"]
    assert r["readiness"]["partitioned_tables"] == 1


def test_snowflake_reports_its_existing_clustering_key(fake_driver,
                                                       monkeypatch):
    """A source estate's partition key MAPS to a clustering key, so the
    target's existing choice has to be visible to compare against."""
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")
    r = livecheck.introspect("snowflake", dict(PARAMS,
                                               database="SF_SAMPLES_DB"))
    orders = {t["name"]: t for t in r["tables"]}["ORDERS"]
    assert orders["partition_strategy"] == "CLUSTER BY"
    assert orders["partition_key"] == ["LINEAR(O_ORDERDATE)"]
    assert r["readiness"]["partitioned_tables"] == 1


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
    _web = Path(__file__).resolve().parent.parent / "web"
    html = ((_web / "templates" / "console.html").read_text(encoding="utf-8")
            + (_web / "static" / "js" / "console.js").read_text(encoding="utf-8"))
    for cls in ("d.packages", "d.triggers", "d.synonyms", "d.db_links",
                "d.scheduler_jobs", "d.queues", "d.types", "d.rules",
                "d.indexes", "d.scheduler_programs", "d.scheduler_schedules",
                "d.scheduler_chains"):
        assert "push(%s," % cls in html, cls
    # constraints render differently (they describe a table, not a schema
    # object) so they get their own branch rather than a push()
    assert "d.constraints || []" in html


def test_every_object_class_the_backend_returns_is_rendered(ora_driver,
                                                            pg_driver,
                                                            monkeypatch):
    """The estate list is an explicit allowlist, so a class the backend
    returns but the console never registers vanishes silently. This walks a
    real introspect result rather than a hand-kept list, so adding a class
    to the backend and forgetting the console fails HERE."""
    from pathlib import Path
    _web = Path(__file__).resolve().parent.parent / "web"
    html = ((_web / "templates" / "console.html").read_text(encoding="utf-8")
            + (_web / "static" / "js" / "console.js").read_text(encoding="utf-8"))
    # keys that are not object classes: scalars, tables/views (rendered by
    # their own branch), and the report's own metadata
    not_a_class = {
        "ok", "connector", "database", "schema", "elapsed_ms", "context",
        "capabilities", "recommendations", "tables", "views",
        "view_definitions", "secret_findings", "readiness", "manifest_yaml",
        "task_dag", "available_databases", "mode", "databases", "error",
    }
    monkeypatch.setenv("MB_ORACLE_PASSWORD", "x")
    monkeypatch.setenv("MB_POSTGRES_PASSWORD", "x")
    for report in (livecheck.introspect("oracle", dict(ORA_PARAMS)),
                   livecheck.introspect("postgres", dict(PG_PARAMS))):
        for key, value in report.items():
            if key in not_a_class or not isinstance(value, list):
                continue
            # either a plain push() or a dedicated branch for the classes
            # whose identity is not a schema-qualified name
            rendered = ("push(d.%s," % key in html
                        or "(d.%s || [])" % key in html)
            assert rendered, ("%s is returned by %s but never rendered"
                              % (key, report["connector"]))


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
    _web = Path(__file__).resolve().parent.parent / "web"
    html = ((_web / "templates" / "console.html").read_text(encoding="utf-8")
            + (_web / "static" / "js" / "console.js").read_text(encoding="utf-8"))
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

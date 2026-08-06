"""SAP HANA live connector (mocked driver — the real network path is
exercised against the customer's own HANA Cloud instance).

Three HANA facts shape the driver and are pinned here:
  * HANA has NO INFORMATION_SCHEMA — the catalog is SYS.SCHEMAS/TABLES/...
  * a bare SELECT needs FROM DUMMY (HANA's DUAL)
  * HANA Cloud is TLS-only on 443; on-prem instances usually are not

Plus the capability reporting: a live driver does not imply every live
action — Test connection and Analyze work, live LOAD does not, and each is
reported for what it is.
"""
import json
import os
import sys
import tempfile
import types

import pytest

os.environ.setdefault("METABRIDGE_DATA_DIR",
                      tempfile.mkdtemp(prefix="mb_hana_"))

from metabridge import livecheck


class _FakeHanaCursor:
    """Answers the SYS/DUMMY statements _hana_test issues, matched by
    fragment so the test breaks if a statement changes shape."""

    def __init__(self, schemas=("SAPABAP1", "SYS"), tables=4):
        self._schemas = list(schemas)
        self._tables = tables
        self._row = None
        self._rows = []
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append(sql)
        s = " ".join(sql.split())
        self._rows = []
        if "VERSION FROM SYS.M_DATABASE" in s:
            self._row = ("4.00.000.00.1234567890",)
        elif "DATABASE_NAME FROM SYS.M_DATABASE" in s:
            self._row = ("H00",)
        elif "CURRENT_TIMESTAMP" in s:
            self._row = ("2026-08-04 19:30:00",)
        elif "CURRENT_USER" in s:
            self._row = ("DBADMIN", "DBADMIN")
        elif "COUNT(*) FROM SYS.TABLES" in s:
            self._row = (self._tables,)
        elif "FROM SYS.SCHEMAS" in s and "WHERE" in s:
            want = (args or ("",))[0].upper()
            self._row = (want,) if want in self._schemas else None
        elif "FROM SYS.SCHEMAS" in s:
            self._rows = [(x,) for x in self._schemas]
            self._row = None
        else:
            self._row = (1,)
        return self

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeHanaConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


@pytest.fixture()
def fake_hana(monkeypatch):
    holder = {"kwargs": None, "cursor": _FakeHanaCursor(), "raise": None}

    def connect(**kw):
        holder["kwargs"] = kw
        if holder["raise"]:
            raise RuntimeError(holder["raise"])
        if not kw.get("password"):
            raise RuntimeError("authentication failed")
        return _FakeHanaConn(holder["cursor"])

    dbapi = types.ModuleType("hdbcli.dbapi")
    dbapi.connect = connect
    pkg = types.ModuleType("hdbcli")
    pkg.dbapi = dbapi
    monkeypatch.setitem(sys.modules, "hdbcli", pkg)
    monkeypatch.setitem(sys.modules, "hdbcli.dbapi", dbapi)
    return holder


HANA_PARAMS = {"host": "abc.hana.trial-us10.hanacloud.ondemand.com",
               "port": "443", "user": "DBADMIN", "password": "s3cret",
               "schema": "SAPABAP1"}


def test_test_connection_probes(fake_hana):
    r = livecheck.test_connection("sap_hana", dict(HANA_PARAMS))
    assert r["ok"] is True and r["authenticated"] is True
    assert r["context"]["user"] == "DBADMIN"
    assert r["context"]["database"] == "H00"
    assert r["objects"]["tables_visible"] == 4
    assert {p["probe"] for p in r["probes"]} == {"server_version",
                                                 "server_time"}
    assert r["steps"] == [{"step": "schema SAPABAP1", "ok": True}]
    assert "s3cret" not in json.dumps(r)


def test_bare_select_uses_from_dummy(fake_hana):
    """Omitting FROM DUMMY is a syntax error on HANA, not a style choice."""
    livecheck.test_connection("sap_hana", dict(HANA_PARAMS))
    checked = 0
    for sql in fake_hana["cursor"].executed:
        flat = " ".join(sql.split())
        if flat.startswith("SELECT CURRENT_"):
            assert "FROM DUMMY" in flat, flat
            checked += 1
    assert checked >= 2


def test_catalog_is_read_from_sys_not_information_schema(fake_hana):
    """HANA exposes no INFORMATION_SCHEMA; querying it would always fail."""
    livecheck.test_connection("sap_hana", dict(HANA_PARAMS))
    joined = " ".join(fake_hana["cursor"].executed).upper()
    assert "INFORMATION_SCHEMA" not in joined
    assert "SYS.SCHEMAS" in joined and "SYS.TABLES" in joined


def test_bad_schema_keeps_auth_ok_and_lists_visible(fake_hana):
    r = livecheck.test_connection("sap_hana",
                                  dict(HANA_PARAMS, schema="NOPE"))
    # authentication succeeded; only the context step failed
    assert r["authenticated"] is True and r["ok"] is False
    assert r["schemas_visible"] == ["SAPABAP1", "SYS"]
    assert "schema NOPE" in r["error"]


def test_wrong_password_names_the_right_password(fake_hana):
    r = livecheck.test_connection("sap_hana", dict(HANA_PARAMS, password=""))
    assert r["ok"] is False and r["authenticated"] is False
    # the commonest support ticket: the BTP cockpit login is a DIFFERENT
    # credential from the database's DBADMIN password
    assert "DBADMIN" in r["error"] and "cockpit" in r["error"]


def test_stopped_instance_is_actionable(fake_hana):
    fake_hana["raise"] = "Connection failed (RTE:[89013] Socket closed)"
    r = livecheck.test_connection("sap_hana", dict(HANA_PARAMS))
    assert "RUNNING" in r["error"] and "stop every evening" in r["error"]


def test_ssl_engine_error_points_at_the_stopped_instance_first(fake_hana):
    """MEASURED against a real free-tier HANA Cloud instance, both ways.

    Stopped, the driver raises "Cannot create SSL engine: The credentials
    supplied were not complete" — because SAP's edge keeps terminating TLS
    on the hostname after the tenant behind it is gone. Started, the very
    same call reached authentication. Plain Python TLS handshakes fine in
    BOTH states, so a successful TLS probe proves the endpoint answers, not
    that the database is there.

    That is why the hint must lead with instance state: an SSL-shaped error
    reads like a crypto problem and sends people hunting driver versions,
    crypto providers and trust stores for what is a one-click fix.
    """
    fake_hana["raise"] = (
        "(-10709, 'Connection failed (RTE:[300012] Cannot create SSL "
        "engine: The credentials supplied were not complete, and could not "
        "be verified.')")
    r = livecheck.test_connection("sap_hana", dict(HANA_PARAMS))
    assert r["ok"] is False
    err = r["error"]
    # the likely cause, stated first
    assert "NOT RUNNING" in err and "stop every evening" in err
    # the local-TLS case is kept, but as the FALLBACK
    assert err.index("NOT RUNNING") < err.index("If it IS running")
    # never advice that was tested and found not to work
    assert "hdbcli==" not in err


def test_unresolvable_host_says_so(fake_hana):
    fake_hana["raise"] = ("Connection failed (RTE:[89001] Cannot resolve "
                          "host name 'nope.invalid')")
    r = livecheck.test_connection("sap_hana", dict(HANA_PARAMS))
    assert "could not be resolved" in r["error"]


def test_host_pasted_with_port_is_split(fake_hana):
    """HANA Cloud Central hands the endpoint out as one host:port string."""
    livecheck.test_connection("sap_hana", {
        "host": "abc.hanacloud.ondemand.com:443",
        "user": "DBADMIN", "password": "x"})
    kw = fake_hana["kwargs"]
    assert kw["address"] == "abc.hanacloud.ondemand.com"
    assert kw["port"] == 443


@pytest.mark.parametrize("host,port,params,expect", [
    # HANA Cloud is TLS-only; a plain connection fails obscurely there
    ("x.hana.trial-us10.hanacloud.ondemand.com", "443", {}, True),
    ("x.hanacloud.ondemand.com", "30015", {}, True),
    # on-prem default is plain
    ("10.0.0.5", "30015", {}, False),
    # an explicit setting always wins over the inference
    ("10.0.0.5", "30015", {"encrypt": "true"}, True),
    ("x.hanacloud.ondemand.com", "443", {"encrypt": "false"}, False),
])
def test_tls_is_inferred_but_overridable(host, port, params, expect):
    assert livecheck._hana_encrypt(host, port, params) is expect


def test_cloud_connection_validates_the_certificate(fake_hana):
    livecheck.test_connection("sap_hana", dict(HANA_PARAMS))
    kw = fake_hana["kwargs"]
    assert kw["encrypt"] is True
    # verification stays ON — a silent downgrade would defeat the TLS
    assert kw["sslValidateCertificate"] is True


def test_missing_host_is_actionable(fake_hana):
    r = livecheck.test_connection("sap_hana", {"user": "u", "password": "p"})
    assert r["ok"] is False and "host is required" in r["error"]


def _hana_form():
    from metabridge.connectors.base import get_registry
    return {f.name: f for f in get_registry().get("sap_hana").fields}


def test_connection_form_matches_what_the_driver_needs():
    """The catalog form and _hana_connect must agree about what is required.
    They did not: the form pre-filled the ON-PREM port and demanded a tenant
    database the driver never asks for."""
    f = _hana_form()
    assert f["host"].required is True
    assert f["user"].required is True
    assert f["password"].required is True and f["password"].secret is True
    # _hana_connect passes databaseName only when set — a HANA Cloud endpoint
    # already resolves to its tenant, so the form must not demand one
    assert f["database"].required is False
    assert f["schema"].required is False
    # optional because it HAS a working default, not because it is ignorable
    assert f["port"].required is False
    assert f["port"].default == "443"


def test_form_port_default_is_the_port_the_driver_uses(fake_hana):
    """A form default that disagrees with the driver silently changes TLS
    behaviour, because _hana_encrypt keys off the port."""
    default = _hana_form()["port"].default
    livecheck.test_connection("sap_hana", {"host": "x.example.com",
                                           "user": "u", "password": "p"})
    assert fake_hana["kwargs"]["port"] == int(default)
    # ...and 443 must therefore also mean TLS on, with no port supplied
    assert fake_hana["kwargs"]["encrypt"] is True


def test_reports_read_capabilities_without_claiming_load():
    """Test connection and Analyze both work; live LOAD (writing data INTO
    the system) stays certified for Snowflake/Databricks only."""
    caps = livecheck.live_support("sap_hana")
    assert caps["live_test"] is True
    assert caps["introspect"] is True
    assert caps["live_load"] is False
    assert "sap_hana" in livecheck.LIVE_CONNECTORS


def test_other_sap_connectors_still_have_no_live_driver():
    """Only sap_hana gained a driver. The ABAP-stack connectors need RFC,
    which is a different project — they must keep saying so."""
    for key in ("sap_s4", "sap_bw", "sap_ecc", "sap_bw4", "sap_datasphere",
                "sap_di"):
        caps = livecheck.live_support(key)
        assert caps["live_test"] is False, key
        assert caps["introspect"] is False, key


# ---------------------------------------------------------------------------
# Analyze (introspect) over the SYS catalog. The fake below answers with the
# real SAP shapes â€” MARA/VBAK as they exist in HANA, where DATS columns are
# physically NVARCHAR(8).
# ---------------------------------------------------------------------------

_CATALOG_TABLES = [("SAPABAP1", "MARA"), ("SAPABAP1", "VBAK")]
_CATALOG_COLUMNS = [
    # schema, table, column, DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE
    ("SAPABAP1", "MARA", "MANDT", "NVARCHAR", 3, None, "FALSE"),
    ("SAPABAP1", "MARA", "MATNR", "NVARCHAR", 18, None, "FALSE"),
    ("SAPABAP1", "MARA", "ERSDA", "NVARCHAR", 8, None, "TRUE"),
    ("SAPABAP1", "MARA", "BRGEW", "DECIMAL", 13, 3, "TRUE"),
    ("SAPABAP1", "MARA", "SEQNO", "INTEGER", 10, 0, "TRUE"),
    ("SAPABAP1", "MARA", "FLOATVAL", "DECIMAL", 34, None, "TRUE"),
    ("SAPABAP1", "VBAK", "MANDT", "NVARCHAR", 3, None, "FALSE"),
    ("SAPABAP1", "VBAK", "VBELN", "NVARCHAR", 10, None, "FALSE"),
    ("SAPABAP1", "VBAK", "NETWR", "DECIMAL", 15, 2, "TRUE"),
    ("SAPABAP1", "VBAK", "AEDAT", "NVARCHAR", 8, "", "TRUE"),
]


class _FakeHanaCatalog:
    """Answers the SYS.* statements _hana_introspect issues."""

    # (SCHEMA_NAME, SCHEMA_OWNER) exactly as HANA Cloud 2026.14 reports them.
    # The PAL_* rows are real: SAP's Predictive Analysis Library ships with
    # every instance and is owned by the _SYS_AFL technical user.
    _SCHEMAS = [("SAPABAP1", "DBADMIN"),
                ("PAL_CONTENT", "_SYS_AFL"),
                ("PAL_STEM_TFIDF", "_SYS_AFL"),
                ("PAL_ANNS_CONTENT", "_SYS_AFL"),
                ("PAL_EMBEDDING_VECTOR_PCA", "_SYS_AFL"),
                ("PAL_SCHEDULED_EXECUTION", "_SYS_AFL"),
                ("_SYS_BIC", "_SYS_REPO"),
                ("SYS", "SYS"),
                ("SYSTEM", "SYSTEM")]

    def __init__(self, schemas=None, fail_on=()):
        self._schemas = list(schemas if schemas is not None
                             else self._SCHEMAS)
        self._fail_on = fail_on
        self._row = None
        self._rows = []
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append(sql)
        s = " ".join(sql.split())
        for frag in self._fail_on:
            if frag in s:
                raise RuntimeError("insufficient privilege: %s" % frag)
        self._rows = []
        self._row = None
        if "CURRENT_USER" in s:
            self._row = ("DBADMIN", "DBADMIN")
        elif "DATABASE_NAME, VERSION FROM SYS.M_DATABASE" in s:
            self._row = ("H00", "4.00.000.00.1234567890")
        elif "FROM SYS.SCHEMAS" in s:
            self._rows = list(self._schemas)
        elif "FROM SYS.TABLES" in s:
            self._rows = list(_CATALOG_TABLES)
        elif "FROM SYS.TABLE_COLUMNS" in s:
            self._rows = list(_CATALOG_COLUMNS)
        elif "FROM SYS.M_TABLES" in s:
            self._rows = [("SAPABAP1", "MARA", 8, 4096),
                          ("SAPABAP1", "VBAK", 8, 2048)]
        elif "FROM SYS.CONSTRAINTS" in s:
            self._rows = [("SAPABAP1", "MARA", "MANDT"),
                          ("SAPABAP1", "MARA", "MATNR")]
        elif "FROM SYS.VIEWS" in s:
            self._rows = [("SAPABAP1", "V_SALES",
                           "SELECT VBELN, NETWR FROM SAPABAP1.VBAK")]
        elif "FROM SYS.PROCEDURES" in s:
            self._rows = [("SAPABAP1", "P_REBUILD", "BEGIN SELECT 1; END")]
        elif "FROM SYS.FUNCTIONS" in s:
            self._rows = []
        return self

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


@pytest.fixture()
def fake_hana_catalog(fake_hana):
    fake_hana["cursor"] = _FakeHanaCatalog()
    return fake_hana


def test_introspect_inventory(fake_hana_catalog):
    r = livecheck.introspect("sap_hana", dict(HANA_PARAMS))
    assert r["ok"] is True and r["connector"] == "sap_hana"
    assert r["context"]["database"] == "H00"
    assert [t["name"] for t in r["tables"]] == ["MARA", "VBAK"]
    assert r["readiness"]["tables"] == 2
    assert r["readiness"]["views"] == 1
    assert r["readiness"]["verdict"] == "READY"


def test_introspect_reads_real_row_counts(fake_hana_catalog):
    """HANA publishes RECORD_COUNT for free, so rows are FACT here â€” not the
    unknown that Teradata has to report without collected statistics."""
    r = livecheck.introspect("sap_hana", dict(HANA_PARAMS))
    for t in r["tables"]:
        assert t["rows_known"] is True
        assert t["rows"] == 8
    assert r["readiness"]["total_rows"] == 16
    assert r["readiness"]["tables_without_row_stats"] == 0


@pytest.mark.parametrize("dtype,length,scale,expect", [
    ("NVARCHAR", 18, None, "NVARCHAR(18)"),
    ("VARCHAR", 8, None, "VARCHAR(8)"),
    ("DECIMAL", 15, 2, "DECIMAL(15,2)"),
    # a BARE decimal reports a length but NULL scale and must stay bare â€”
    # DECIMAL(34,0) there would truncate every fractional value
    ("DECIMAL", 34, None, "DECIMAL"),
    ("DECIMAL", 34, "", "DECIMAL"),
    # HANA reports a LENGTH for types that never declared one
    ("INTEGER", 10, 0, "INTEGER"),
    ("TIMESTAMP", 27, 7, "TIMESTAMP"),
    ("SECONDDATE", 20, 0, "SECONDDATE"),
    ("DATE", 10, 0, "DATE"),
    ("BOOLEAN", 1, 0, "BOOLEAN"),
])
def test_native_type_reattaches_only_declared_parameters(
        dtype, length, scale, expect):
    assert livecheck._hana_native_type(dtype, length, scale) == expect


def test_introspect_column_types_survive_into_the_manifest(
        fake_hana_catalog):
    r = livecheck.introspect("sap_hana", dict(HANA_PARAMS))
    mara = next(t for t in r["tables"] if t["name"] == "MARA")
    by_name = {c["name"]: c["type"] for c in mara["columns"]}
    assert by_name["MATNR"] == "NVARCHAR(18)"
    assert by_name["BRGEW"] == "DECIMAL(13,3)"
    assert by_name["SEQNO"] == "INTEGER"
    assert by_name["FLOATVAL"] == "DECIMAL"
    # SAP stores DATS as an 8-char string; the catalog says so and the
    # manifest must carry that fact rather than a flattering guess
    assert by_name["ERSDA"] == "NVARCHAR(8)"
    assert "NVARCHAR(18)" in r["manifest_yaml"]
    assert "MARA" in r["manifest_yaml"]


def test_introspect_declared_primary_key_becomes_unique_key(
        fake_hana_catalog):
    r = livecheck.introspect("sap_hana", dict(HANA_PARAMS))
    assert "unique_key" in r["manifest_yaml"]
    assert "MANDT" in r["manifest_yaml"]


def test_introspect_skips_sap_managed_schemas_by_owner(fake_hana_catalog):
    """The PAL_* library is SAP content, not customer data. No name pattern
    identifies it — PAL_CONTENT looks exactly like a user schema — but its
    OWNER (_SYS_AFL) does. Without this, a 4-table estate analyzed as 163."""
    r = livecheck.introspect("sap_hana", {k: v for k, v in HANA_PARAMS.items()
                                          if k != "schema"})
    assert set(r["databases_skipped"]) == {
        "PAL_CONTENT", "PAL_STEM_TFIDF", "PAL_ANNS_CONTENT",
        "PAL_EMBEDDING_VECTOR_PCA", "PAL_SCHEDULED_EXECUTION",
        "_SYS_BIC", "SYS", "SYSTEM"}
    # and the one real user schema survived
    joined = " ".join(fake_hana_catalog["cursor"].executed)
    assert "'SAPABAP1'" in joined
    assert "'PAL_CONTENT'" not in joined
    assert "owned by SYS/_SYS_*" in r["capabilities"]["schemas"]["detail"]


@pytest.mark.parametrize("name,owner,is_system", [
    # owner is the primary test
    ("SAPABAP1", "DBADMIN", False),
    ("PAL_CONTENT", "_SYS_AFL", True),
    ("_SYS_BIC", "_SYS_REPO", True),
    ("ANYTHING", "SYS", True),
    # name is the fallback for system schemas a normal user owns
    ("SYSTEM", "SYSTEM", True),
    ("PUBLIC", "DBADMIN", True),
    # SYSTEM is NOT a system OWNER: on-prem estates create real customer
    # schemas as SYSTEM, and skipping those would drop the migration's data
    ("SALES_DW", "SYSTEM", False),
    # a customer schema that merely LOOKS like SAP content is kept
    ("PAL_MY_OWN_DATA", "DBADMIN", False),
])
def test_system_schema_decided_by_owner_then_name(name, owner, is_system):
    assert livecheck._hana_is_system_schema(name, owner) is is_system


def test_introspect_explicit_schema_is_taken_verbatim(fake_hana_catalog):
    """An explicit scope wins even if it looks like a system schema â€” the
    caller asked for it."""
    r = livecheck.introspect("sap_hana", dict(HANA_PARAMS, schema="SYS"))
    assert r["schema"] == "SYS"
    assert r["databases_skipped"] == []
    joined = " ".join(fake_hana_catalog["cursor"].executed)
    assert "'SYS'" in joined


def test_introspect_never_queries_information_schema(fake_hana_catalog):
    livecheck.introspect("sap_hana", dict(HANA_PARAMS))
    joined = " ".join(fake_hana_catalog["cursor"].executed).upper()
    assert "INFORMATION_SCHEMA" not in joined


def test_introspect_one_denied_class_does_not_cost_the_inventory(fake_hana):
    """A role without SYS.PROCEDURES must still get its tables."""
    fake_hana["cursor"] = _FakeHanaCatalog(fail_on=("SYS.PROCEDURES",))
    r = livecheck.introspect("sap_hana", dict(HANA_PARAMS))
    assert r["ok"] is True
    assert len(r["tables"]) == 2
    assert r["procedures"] == []
    assert r["capabilities"]["procedures"]["status"] != "available"


def test_introspect_is_honest_that_view_sql_is_only_ansi_checked(
        fake_hana_catalog):
    """sqlglot has no HANA dialect. Saying a view is 'convertible' on an ANSI
    parse would overclaim, so the limitation is reported."""
    r = livecheck.introspect("sap_hana", dict(HANA_PARAMS))
    cap = r["capabilities"]["view_parsing"]
    assert cap["status"] == "partial"
    assert "no SAP HANA dialect" in cap["detail"]


def test_introspect_manifest_omits_database(fake_hana_catalog):
    """HANA addresses objects as SCHEMA.TABLE â€” the tenant DB is the
    connection. A `database:` key would build an invalid three-part name."""
    r = livecheck.introspect("sap_hana", dict(HANA_PARAMS))
    assert "database:" not in r["manifest_yaml"]
    assert "schema: SAPABAP1" in r["manifest_yaml"]


def test_introspect_connection_failure_is_reported_not_raised(fake_hana):
    fake_hana["raise"] = "Connection failed (RTE:[89013] Socket closed)"
    r = livecheck.introspect("sap_hana", dict(HANA_PARAMS))
    assert r["ok"] is False and "Connection failed" in r["error"]


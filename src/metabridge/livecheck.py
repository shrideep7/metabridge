"""Live connection check + real-time validation runner.

Two capabilities the marketplace connection form feeds:

    test_connection(key, params)   open a REAL connection to the target
                                   (Snowflake first), run read-only
                                   probes (version, context, object
                                   counts) and return an evidence-based
                                   report with latency
    run_live_validation(...)       execute the GENERATED validation
                                   tests (validation_tests/tests.json)
                                   against the live warehouse and report
                                   per-test pass/fail — the real-time
                                   proof that a conversion holds

Secrets follow the marketplace contract: values are never stored — the
password is read from the MB_<CONNECTOR>_<FIELD> environment variable
(e.g. MB_SNOWFLAKE_PASSWORD) or passed transiently by the API caller
and used for the session only.

Everything here is READ-ONLY: probes are SELECT/SHOW statements and the
validation runner executes the suite's SELECT COUNT(*)-style checks.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional


# Connectors that have a REAL, live driver in this build. Snowflake and
# Databricks each use their native driver; PostgreSQL and Amazon Redshift
# share the psycopg2 + INFORMATION_SCHEMA path (Redshift speaks the
# PostgreSQL wire protocol). Adding another SQL database is a one-line entry
# here plus its driver in the `connectors` extra — the UI turns on
# Test/Analyze automatically for any connector this reports as live.
# Everything else stays declarative (artifact generation / scaffold from a
# manifest) and a live probe returns an HONEST "unsupported" result, which
# callers must NOT treat as a failed connection.
_SQL_DIALECTS = {"postgres": "postgres", "redshift": "redshift"}


# Types that carry (precision, scale); everything else keeps INFORMATION_
# SCHEMA's numeric_precision to itself. INTEGER reports precision 32 in
# Postgres, and "integer(32,0)" is not a type.
_PARAMETERIZED_NUMERIC = ("numeric", "decimal", "number", "dec")
# A character length equal to the platform's own maximum is what a catalog
# reports for a column declared with NO length (Snowflake VARCHAR is
# VARCHAR(16777216)). Recording that as a real width would claim knowledge we
# do not have, and would ask a target for a width it cannot create.
_CHAR_LEN_MEANS_UNBOUNDED = frozenset({16777216, 16777215, 1073741824,
                                       2147483647})


def _native_type(data_type: object, char_len: object = None,
                 num_precision: object = None, num_scale: object = None,
                 full_type: object = None) -> str:
    """Rebuild the column's DECLARED type from INFORMATION_SCHEMA.

    ``data_type`` alone is lossy: NUMBER(38,0) and NUMBER(12,2) both report
    "NUMBER", VARCHAR(80) reports "VARCHAR". Every downstream artifact then
    has to invent a precision, so a key column ends up DECIMAL(38,6) and a
    short code column ends up 4000 characters wide. Carrying the real
    precision/scale/length here is what removes those fallbacks.
    """
    if full_type:                       # Databricks exposes the whole type
        ft = str(full_type).strip()
        if ft:
            return ft
    base = str(data_type or "").strip()
    if not base:
        return ""
    low = base.lower()
    if char_len is not None:
        try:
            n = int(char_len)
            if n > 0 and n not in _CHAR_LEN_MEANS_UNBOUNDED:
                return "%s(%d)" % (base, n)
        except (TypeError, ValueError):
            pass
    if num_precision is not None and low in _PARAMETERIZED_NUMERIC:
        try:
            prec = int(num_precision)
            scale = int(num_scale or 0)
            if prec > 0:
                return "%s(%d,%d)" % (base, prec, scale)
        except (TypeError, ValueError):
            pass
    return base


def _fetch_columns(run, enriched_sql: str, plain_sql: str,
                   on_retry=None) -> List[tuple]:
    """Column rows as ``(schema, table, column, native_type)``.

    Tries the projection that carries precision/scale/length and degrades to
    bare ``data_type`` if the catalog or the role will not give it — losing
    exact types is a downgrade, losing the whole inventory is an outage.
    """
    rows: List[tuple] = []
    try:
        raw = run(enriched_sql)
        for r in raw:
            rows.append((r[0], r[1], str(r[2]),
                         _native_type(r[3],
                                      r[4] if len(r) > 4 else None,
                                      r[5] if len(r) > 5 else None,
                                      r[6] if len(r) > 6 else None,
                                      r[7] if len(r) > 7 else None)))
        return rows
    except Exception:  # noqa: BLE001 — fall back to base types
        if on_retry is not None:
            try:
                on_retry()
            except Exception:  # noqa: BLE001
                pass
    return [(r[0], r[1], str(r[2]), str(r[3])) for r in run(plain_sql)]


_TEXTUAL_BASES = frozenset({"text", "varchar", "string", "char",
                            "nvarchar", "nchar", "character varying",
                            "character"})


def _measure_column_sizes(run, tables: dict, length_fn: str = "LENGTH",
                          max_rows: int = 10_000_000,
                          max_tables: int = 80) -> int:
    """Measure, live, what the catalog would not declare: the real maximum
    length of unbounded text columns and the real magnitude of >18-digit
    whole-number columns (Snowflake's INT is NUMBER(38,0), so every integer
    reports 38 digits). Column types are rewritten in place, so the
    manifest — and everything generated from it — carries measured fact
    instead of a documented guess, and the probe file becomes unnecessary.

    MAX() is a scan, so this only touches tables whose row count is known
    and small enough, caps the table count, and treats any per-table
    failure as "leave the type alone". Text widths get power-of-two
    headroom (a snapshot is not a ceiling); integrals that fit 18 digits
    become (18,0) — enough for BIGINT downstream, generous against growth.
    """
    import re
    wide_int = re.compile(
        r"^(number|numeric|decimal|dec)\((\d+),\s*0\)$", re.I)
    measured = 0
    probed = 0
    for t in tables.values():
        if "VIEW" in str(t.get("type", "")).upper():
            continue
        try:
            rows = int(t.get("rows") or 0)
        except (TypeError, ValueError):
            rows = 0
        if rows <= 0 or rows > max_rows or probed >= max_tables:
            continue
        plan = []                      # (column dict, kind)
        for c in t.get("columns", []):
            ty = str(c.get("type", ""))
            base = ty.split("(")[0].strip().lower()
            if "(" not in ty and base in _TEXTUAL_BASES:
                plan.append((c, "len"))
            else:
                m = wide_int.match(ty.replace(" ", ""))
                if m and int(m.group(2)) > 18:
                    plan.append((c, "abs"))
        if not plan:
            continue
        probed += 1
        exprs = ["MAX(%s(%s))" % (length_fn, c["name"]) if kind == "len"
                 else "MAX(ABS(%s))" % c["name"] for c, kind in plan]
        qualified = "%s.%s" % (t["schema"], t["name"]) if t.get("schema") \
            else t["name"]
        try:
            row = run("SELECT %s FROM %s" % (", ".join(exprs), qualified))
        except Exception:  # noqa: BLE001 — measuring is best-effort
            continue
        if not row:
            continue
        values = row[0] if isinstance(row[0], (list, tuple)) else row
        for (c, kind), v in zip(plan, values):
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            base = str(c["type"]).split("(")[0].strip()
            if kind == "len" and n > 0:
                width = 16
                while width < n:
                    width *= 2
                c["type"] = "%s(%d)" % (base, width)
                measured += 1
            elif kind == "abs" and 0 <= n < 10 ** 18:
                c["type"] = "%s(18,0)" % base
                measured += 1
        t["sizes_measured"] = True
    return measured


def _has_live_driver(key: str) -> bool:
    return key in ("snowflake", "databricks") or key in _SQL_DIALECTS


# Back-compat: earlier code imported this set directly.
LIVE_CONNECTORS = frozenset({"snowflake", "databricks"}) | frozenset(_SQL_DIALECTS)


def live_support(key: str) -> Dict[str, bool]:
    """What live actions a connector's driver actually implements. The UI
    reads this to offer only working flows instead of showing every
    connector as if it were fully live-integrated.

    Reported honestly per capability: the SQL connectors have a read path
    (Test connection + Analyze/introspect), while live LOAD — writing data
    into the target — is certified for Snowflake and Databricks today; the
    other connectors fall back to a generated load PACKAGE, so live_load
    stays False for them rather than overclaiming."""
    live = _has_live_driver(key)
    return {"live_test": live, "introspect": live,
            "live_load": key in ("snowflake", "databricks")}


def _secret(key: str, field: str, params: Dict[str, str]) -> str:
    return (params.get(field)
            or os.environ.get("MB_%s_%s" % (key.upper(), field.upper()))
            or os.environ.get("%s_%s" % (key.upper(), field.upper()), ""))


def _snowflake_connect(params: Dict[str, str],
                       with_context: bool = True):
    try:
        import snowflake.connector  # driver import deferred: optional dep
    except ModuleNotFoundError as e:
        # Actionable message instead of a bare "No module named 'snowflake'":
        # the deployment was built without the live-connector driver.
        raise RuntimeError(
            "The Snowflake driver is not installed in this deployment. "
            "Rebuild the image with the connectors extra — "
            "pip install 'metabridge[web,dtd,connectors]' "
            "(adds snowflake-connector-python) — then retry Test connection."
        ) from e
    kw = {
        "account": params.get("account", ""),
        "user": params.get("user", ""),
        "password": _secret("snowflake", "password", params),
        "login_timeout": int(params.get("login_timeout", 20)),
        "network_timeout": 30,
    }
    opts = ("role", "warehouse", "database", "schema") if with_context \
        else ("role",)
    for opt in opts:
        if params.get(opt):
            kw[opt] = params[opt]
    if not kw["password"]:
        raise ValueError(
            "No password provided — export MB_SNOWFLAKE_PASSWORD (values "
            "are never stored) or pass it transiently in the request.")
    return snowflake.connector.connect(**kw)


def _databricks_connect(params: Dict[str, str]):
    """Open a READ-ONLY session against a Databricks SQL warehouse. Auth is
    a personal access token (Databricks' standard programmatic credential),
    following the same never-stored contract as every other connector."""
    try:
        import databricks.sql as databricks_sql  # optional dep
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "The Databricks driver is not installed in this deployment. "
            "Rebuild the image with the connectors extra — "
            "pip install 'metabridge[web,dtd,connectors]' "
            "(adds databricks-sql-connector) — then retry Test connection."
        ) from e
    token = _secret("databricks", "token", params)
    if not token:
        raise ValueError(
            "No access token provided — export MB_DATABRICKS_TOKEN (values "
            "are never stored) or pass it transiently in the request.")
    host = (params.get("host") or "").strip()
    if not host:
        raise ValueError("No workspace host provided — the SQL warehouse's "
                         "workspace hostname is required to connect.")
    http_path = (params.get("http_path") or "").strip()
    if not http_path:
        raise ValueError("No SQL warehouse HTTP path provided — copy it "
                         "from the warehouse's Connection Details.")
    kw = {"server_hostname": host, "http_path": http_path,
          "access_token": token}
    if params.get("catalog"):
        kw["catalog"] = params["catalog"]
    if params.get("schema"):
        kw["schema"] = params["schema"]
    return databricks_sql.connect(**kw)


def _databricks_test(params: Dict[str, str]) -> dict:
    """Databricks live probe — the SAME evidence-based report shape as the
    Snowflake/SQL paths. Catalog and schema are fixed at connect time by the
    driver (unlike Snowflake's USE-based ladder), so a bad catalog/schema
    surfaces as a connect-time failure rather than a separate step."""
    started = time.time()
    try:
        conn = _databricks_connect(params)
    except Exception as e:  # noqa: BLE001 — report, never crash the app
        msg = str(e)
        low = msg.lower()
        needs_credential = "no access token provided" in low
        return {"ok": False, "connector": "databricks", "authenticated": False,
                "needs_credential": needs_credential,
                "latency_ms": int((time.time() - started) * 1000),
                "error": msg[:400]}
    report: dict = {"ok": True, "connector": "databricks",
                    "authenticated": True, "probes": [], "steps": []}
    try:
        cur = conn.cursor()

        def probe(label: str, sql: str):
            t0 = time.time()
            cur.execute(sql)
            row = cur.fetchone()
            report["probes"].append({
                "probe": label, "sql": sql,
                "result": str(row[0]) if row and row[0] is not None
                else str(row),
                "ms": int((time.time() - t0) * 1000)})
            return row

        probe("server_version", "SELECT current_version()")
        probe("server_time", "SELECT current_timestamp()")

        row = cur.execute(
            "SELECT current_catalog(), current_database(), "
            "current_user()").fetchone()
        report["context"] = dict(zip(
            ("catalog", "schema", "user"),
            (str(x) if x is not None else None for x in row)))

        row = cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = current_database()").fetchone()
        report["objects"] = {"tables_visible": int(row[0]) if row else 0}

        report["latency_ms"] = int((time.time() - started) * 1000)
    except Exception as e:  # noqa: BLE001
        report.update({"ok": False, "error": str(e)[:400],
                       "latency_ms": int((time.time() - started) * 1000)})
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return report


def _databricks_introspect(params: Dict[str, str],
                           max_tables: int = 500) -> dict:
    """Read-only inventory for Databricks, returning the SAME shape as the
    Snowflake/SQL introspect (tables/columns/views + readiness + manifest)
    so the console and scaffold consume it unchanged."""
    catalog = (params.get("catalog") or "").strip()
    schema = (params.get("schema") or "").strip()
    # Databricks catalogs do not expose row counts, so live size
    # measurement never runs here — types come from full_data_type instead.
    measured = 0
    started = time.time()
    try:
        conn = _databricks_connect(params)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "connector": "databricks", "error": str(e)[:400]}
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT current_catalog(), current_database()").fetchone()
        catalog = catalog or str(row[0] or "")
        schema = schema or str(row[1] or "")
        cur.execute(
            "SELECT table_schema, table_name, table_type "
            "FROM information_schema.tables "
            "WHERE table_schema = COALESCE(?, table_schema) "
            "LIMIT %d" % int(max_tables),
            [schema or None])
        tables = {(r[0], r[1]): {"schema": r[0], "name": r[1],
                                 "type": ("VIEW" if str(r[2]).upper() ==
                                          "VIEW" else "BASE TABLE"),
                                 "rows": 0, "bytes": 0, "columns": []}
                  for r in cur.fetchall()}
        def _run(sql):
            cur.execute(sql, [schema or None])
            return cur.fetchall()

        for r in _fetch_columns(
                _run,
                "SELECT table_schema, table_name, column_name, data_type, "
                "character_maximum_length, numeric_precision, numeric_scale, "
                "full_data_type FROM information_schema.columns "
                "WHERE table_schema = COALESCE(?, table_schema) "
                "ORDER BY table_schema, table_name, ordinal_position",
                "SELECT table_schema, table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = COALESCE(?, table_schema) "
                "ORDER BY table_schema, table_name, ordinal_position"):
            key_ = (r[0], r[1])
            if key_ in tables:
                tables[key_]["columns"].append(
                    {"name": str(r[2]), "type": r[3]})
        # ── Row counts: 3-tier fallback ──────────────────────────────────────
        # Tier 1: information_schema.table_statistics — populated by ANALYZE
        # TABLE or Databricks' automatic stats collection. Fast, but zero for
        # freshly loaded tables that have never been ANALYZEd.
        try:
            cur.execute(
                "SELECT table_schema, table_name, row_count "
                "FROM information_schema.table_statistics "
                "WHERE table_schema = COALESCE(?, table_schema)",
                [schema or None])
            for r in cur.fetchall():
                key_ = (r[0], r[1])
                if key_ in tables and r[2] is not None:
                    tables[key_]["rows"] = max(0, int(r[2]))
        except Exception:  # noqa: BLE001 — stats view may not exist
            pass

        # Tier 2: DESCRIBE DETAIL <schema>.<table> — reads numRows straight
        # from the Delta transaction log. Always current after any write (no
        # ANALYZE needed). Only issued for base tables still showing 0 rows so
        # we skip tables that Tier 1 already populated.
        zero_tables = [(s, n) for (s, n), t in tables.items()
                       if t["rows"] == 0 and "VIEW" not in t["type"].upper()]
        for tbl_schema, tbl_name in zero_tables:
            try:
                cur.execute("DESCRIBE DETAIL `%s`.`%s`"
                            % (tbl_schema.replace("`", ""),
                               tbl_name.replace("`", "")))
                detail = cur.fetchone()
                if detail:
                    # DESCRIBE DETAIL returns a single row; numRows is the
                    # 8th column (index 7) in the standard schema:
                    # format, id, name, description, location, createdAt,
                    # lastModified, partitionColumns, numFiles, sizeInBytes,
                    # properties, minReaderVersion, minWriterVersion, numRows
                    row_cols = [d[0].lower() for d in cur.description]
                    if "numrows" in row_cols:
                        nr = detail[row_cols.index("numrows")]
                        key_ = (tbl_schema, tbl_name)
                        if key_ in tables and nr is not None:
                            tables[key_]["rows"] = max(0, int(nr))
            except Exception:  # noqa: BLE001 — non-Delta tables, skip
                pass

        # Tier 3: exact COUNT(*) — last resort for tables still at 0 after
        # both stats sources (non-Delta tables, or a Delta table whose
        # transaction log hasn't materialized numRows yet, e.g. right after
        # a fresh load). Bounded to the handful of tables still unresolved
        # so this stays cheap even though it scans data.
        still_zero = [(s, n) for (s, n), t in tables.items()
                      if t["rows"] == 0 and "VIEW" not in t["type"].upper()]
        for tbl_schema, tbl_name in still_zero:
            try:
                cur.execute("SELECT COUNT(*) FROM `%s`.`%s`"
                            % (tbl_schema.replace("`", ""),
                               tbl_name.replace("`", "")))
                cnt = cur.fetchone()
                key_ = (tbl_schema, tbl_name)
                if cnt is not None and key_ in tables:
                    tables[key_]["rows"] = max(0, int(cnt[0] or 0))
            except Exception:  # noqa: BLE001 — best-effort, skip on error
                pass
        views = []
        cur.execute(
            "SELECT table_schema, table_name, view_definition "
            "FROM information_schema.views "
            "WHERE table_schema = COALESCE(?, table_schema)",
            [schema or None])
        for r in cur.fetchall():
            views.append({"schema": r[0], "name": r[1],
                          "definition": str(r[2] or "")[:8000]})
    except Exception as e:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "connector": "databricks", "error": str(e)[:400]}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # conversion readiness: every view's SQL parsed with the real dialect
    import sqlglot
    convertible, needs_review = [], []
    for v in views:
        if not v["definition"].strip():
            needs_review.append({"view": v["name"],
                                 "reason": "definition not visible to "
                                           "this user"})
            continue
        try:
            sqlglot.parse_one(v["definition"], read="databricks")
            convertible.append(v["name"])
        except Exception as e:  # noqa: BLE001
            needs_review.append({"view": v["name"], "reason": str(e)[:150]})

    base_tables = [t for t in tables.values()
                   if "VIEW" not in t["type"].upper()]
    manifest = {"tables": [
        {"name": t["name"], "schema": t["schema"],
         **({"database": catalog} if catalog else {}),
         "columns": [{"name": c["name"], "type": c["type"]}
                     for c in t["columns"]]}
        for t in base_tables]}
    import yaml as _yaml
    return {
        "ok": True, "connector": "databricks",
        "database": catalog, "schema": schema or "(all)",
        "elapsed_ms": int((time.time() - started) * 1000),
        "tables": sorted(tables.values(),
                         key=lambda t: (-t["rows"], t["name"])),
        "views": [{"schema": v["schema"], "name": v["name"]}
                  for v in views],
        "view_definitions": {v["name"]: v["definition"] for v in views},
        "readiness": {
            "tables": len(base_tables),
            "views": len(views),
            "total_rows": sum(t["rows"] for t in base_tables),
            "tables_with_columns": sum(1 for t in base_tables
                                       if t["columns"]),
            "column_sizes_measured": measured,
            "views_convertible": len(convertible),
            "views_needing_review": needs_review,
            "verdict": "READY" if base_tables or convertible else
                       "NOTHING_TO_CONVERT",
        },
        "manifest_yaml": _yaml.safe_dump(manifest, sort_keys=False,
                                         width=100),
    }


def _split_endpoint(raw: str):
    """Tolerate a full endpoint pasted into the host field. The AWS Redshift
    console shows an endpoint as ``host:port/database`` and JDBC/URI strings
    are common copy-paste sources, so normalize any of::

        host
        host:5439
        host:5439/dev
        postgres://[user[:pw]@]host:5432/db
        jdbc:redshift://host:5439/dev

    into ``(host, port|None, database|None)``. A bare, already-clean host
    passes through unchanged (idempotent)."""
    s = (raw or "").strip()
    if not s:
        return "", None, None
    if s.lower().startswith("jdbc:"):        # jdbc:redshift://…, jdbc:postgresql://…
        s = s[5:]
    idx = s.find("://")                       # scheme:// prefix
    if idx != -1:
        s = s[idx + 3:]
    if "@" in s:                              # user:pw@ userinfo
        s = s.rsplit("@", 1)[1]
    s = s.split("?", 1)[0]                     # drop ?query params
    database = None
    if "/" in s:
        s, rest = s.split("/", 1)
        database = rest.strip() or None
    host, port = s, None
    if host.startswith("["):                   # [ipv6]:port
        end = host.find("]")
        if end != -1:
            after = host[end + 1:]
            host = host[:end + 1]
            if after.startswith(":") and after[1:].isdigit():
                port = int(after[1:])
    elif host.count(":") == 1:                 # host:port (one colon only;
        host, port_s = host.rsplit(":", 1)     # more => bare IPv6, leave it)
        if port_s.isdigit():
            port = int(port_s)
        else:
            host, port = "%s:%s" % (host, port_s), None
    return host.strip(), port, database


def _normalize_sql_params(params: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of params with a pasted full endpoint split apart: a
    host of the form ``host:port/database`` has its port and database peeled
    off into the dedicated fields WHEN THOSE ARE EMPTY (explicit fields
    always win), so DNS resolves the bare hostname and introspection still
    sees the database."""
    p = dict(params or {})
    host, port, database = _split_endpoint(p.get("host", ""))
    if host:
        p["host"] = host
    if port and not str(p.get("port") or "").strip():
        p["port"] = str(port)
    if database and not str(p.get("database") or "").strip():
        p["database"] = database
    return p


def _psycopg_connect(key: str, params: Dict[str, str]):
    """Open a READ-ONLY PostgreSQL / Amazon Redshift session via psycopg2.
    Both speak the same wire protocol and expose INFORMATION_SCHEMA, so a
    single driver path serves both. The password follows the same
    never-stored contract as every other connector."""
    # Validate user-supplied inputs BEFORE importing the driver: a missing
    # credential is an actionable form problem (surfaced as "needs credential"
    # in the UI) and must be reported the same way whether or not the optional
    # driver happens to be installed on this host.
    password = _secret(key, "password", params)
    if not password:
        raise ValueError(
            "No password provided — export MB_%s_PASSWORD (values are never "
            "stored) or pass it transiently in the request." % key.upper())
    host = (params.get("host") or "").strip()
    if not host:
        raise ValueError("No host provided — a hostname is required to "
                         "connect.")
    try:
        import psycopg2  # driver import deferred: optional dep
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "The PostgreSQL driver is not installed in this deployment. "
            "Rebuild the image with the connectors extra — "
            "pip install 'metabridge[web,dtd,connectors]' "
            "(adds psycopg2-binary) — then retry Test connection."
        ) from e
    default_port = 5439 if key == "redshift" else 5432
    kw = {
        "host": host,
        "port": int(params.get("port") or default_port),
        "user": params.get("user", ""),
        "password": password,
        "connect_timeout": int(params.get("login_timeout", 20)),
    }
    if params.get("database"):
        kw["dbname"] = params["database"]
    schema = (params.get("schema") or "").strip()
    if schema:
        # Pin the default search_path so unqualified probes resolve in the
        # user's schema. Stripping spaces/quotes prevents smuggling a second
        # "-c" startup option through the value.
        safe_schema = schema.replace('"', "").replace(" ", "").replace(
            "'", "")
        kw["options"] = "-c search_path=%s" % safe_schema
    conn = psycopg2.connect(**kw)
    conn.autocommit = True
    try:  # defense in depth — every statement we run is already a SELECT
        cur = conn.cursor()
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cur.close()
    except Exception:  # noqa: BLE001 — Redshift may reject; SELECT-only anyway
        pass
    return conn


def _sqldb_test(key: str, params: Dict[str, str]) -> dict:
    """PostgreSQL / Redshift live probe — the SAME evidence-based report
    shape as the Snowflake path: authenticate first, then verify the
    schema context as a separate diagnostic step (so one wrong value never
    masks that authentication works), then count the visible objects."""
    params = _normalize_sql_params(params)
    started = time.time()
    try:
        conn = _psycopg_connect(key, params)
    except Exception as e:  # noqa: BLE001 — report, never crash the app
        msg = str(e)
        low = msg.lower()
        needs_credential = "no password provided" in low
        if ("translate host name" in low or "could not resolve" in low
                or "name or service not known" in low
                or "nodename nor servname" in low):
            msg += (" — put ONLY the hostname in the Host field; the port "
                    "and database belong in their own fields (an endpoint "
                    "pasted as host:port/database is split automatically, "
                    "so re-check the value if this persists).")
        return {"ok": False, "connector": key, "authenticated": False,
                "needs_credential": needs_credential,
                "latency_ms": int((time.time() - started) * 1000),
                "error": msg[:400]}
    report: dict = {"ok": True, "connector": key, "authenticated": True,
                    "probes": [], "steps": []}
    try:
        cur = conn.cursor()

        def probe(label: str, sql: str):
            t0 = time.time()
            cur.execute(sql)
            row = cur.fetchone()
            report["probes"].append({
                "probe": label, "sql": sql,
                "result": str(row[0]) if row and row[0] is not None
                else str(row),
                "ms": int((time.time() - t0) * 1000)})
            return row

        probe("server_version", "SELECT version()")
        probe("server_time", "SELECT current_timestamp")

        cur.execute("SELECT current_user, current_database(), "
                    "current_schema()")
        row = cur.fetchone()
        report["context"] = dict(zip(
            ("user", "database", "schema"),
            (str(x) if x is not None else None for x in row)))

        schema = (params.get("schema") or "").strip()
        if schema:
            cur.execute("SELECT 1 FROM information_schema.schemata "
                        "WHERE schema_name = %s", (schema,))
            if cur.fetchone():
                report["steps"].append({"step": "schema %s" % schema,
                                        "ok": True})
            else:
                report["steps"].append(
                    {"step": "schema %s" % schema, "ok": False,
                     "error": "schema not found or not visible to this user"})
                report["ok"] = False
                cur.execute("SELECT schema_name FROM "
                            "information_schema.schemata "
                            "ORDER BY schema_name")
                report["schemas_visible"] = [str(r[0]) for r in
                                             cur.fetchall()][:50]

        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "AND table_schema = COALESCE(%s, table_schema)",
            (schema or None,))
        row = cur.fetchone()
        report["objects"] = {"tables_visible": int(row[0]) if row else 0}

        report["latency_ms"] = int((time.time() - started) * 1000)
        if not report["ok"]:
            bad = [s for s in report["steps"] if not s["ok"]]
            report["error"] = ("authenticated, but context failed — %s"
                               % "; ".join("%s: %s" % (s["step"],
                                                       s.get("error", ""))
                                           for s in bad))[:500]
    except Exception as e:  # noqa: BLE001
        report.update({"ok": False, "error": str(e)[:400],
                       "latency_ms": int((time.time() - started) * 1000)})
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return report


def _sqldb_introspect(key: str, params: Dict[str, str],
                      max_tables: int = 500) -> dict:
    """Read-only inventory for PostgreSQL / Redshift, returning the SAME
    shape as the Snowflake introspect (tables/columns/views + readiness +
    manifest) so the console and scaffold consume it unchanged."""
    params = _normalize_sql_params(params)
    database = params.get("database", "")
    schema = (params.get("schema") or "").strip()
    if not database:
        return {"ok": False, "error": "database is required to introspect"}
    started = time.time()
    try:
        conn = _psycopg_connect(key, params)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "connector": key, "error": str(e)[:400]}
    try:
        cur = conn.cursor()
        # scope: a chosen schema, else everything but the system schemas
        if schema:
            where, args = "AND table_schema = %s", (schema,)
        else:
            where = ("AND table_schema NOT IN "
                     "('pg_catalog', 'information_schema')")
            args = ()
        cur.execute(
            "SELECT table_schema, table_name, table_type "
            "FROM information_schema.tables "
            "WHERE table_catalog = current_database() %s "
            "ORDER BY table_schema, table_name LIMIT %d"
            % (where, int(max_tables)), args)
        tables = {(r[0], r[1]): {"schema": r[0], "name": r[1],
                                 "type": ("VIEW" if str(r[2]).upper() ==
                                          "VIEW" else "BASE TABLE"),
                                 "rows": 0, "bytes": 0, "columns": []}
                  for r in cur.fetchall()}
        def _run(sql):
            cur.execute(sql % where, args)
            return cur.fetchall()

        for r in _fetch_columns(
                _run,
                "SELECT table_schema, table_name, column_name, data_type, "
                "character_maximum_length, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_catalog = current_database() %s "
                "ORDER BY table_schema, table_name, ordinal_position",
                "SELECT table_schema, table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_catalog = current_database() %s "
                "ORDER BY table_schema, table_name, ordinal_position",
                on_retry=lambda: conn.rollback()):
            key_ = (r[0], r[1])
            if key_ in tables:
                tables[key_]["columns"].append({"name": str(r[2]),
                                                "type": r[3]})
        # row estimates are best-effort — exact COUNT(*) is too costly at
        # scale, and the catalog estimate is what warehouses expose cheaply.
        try:
            cur.execute(
                "SELECT n.nspname, c.relname, c.reltuples "
                "FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p')")
            for r in cur.fetchall():
                key_ = (r[0], r[1])
                if key_ in tables:
                    tables[key_]["rows"] = max(0, int(r[2] or 0))
        except Exception:  # noqa: BLE001 — estimates are optional
            pass

        def _run_measure(sql):
            try:
                cur.execute(sql)
                return cur.fetchall()
            except Exception:
                conn.rollback()
                raise
        try:
            measured = _measure_column_sizes(_run_measure, tables)
        except Exception:  # noqa: BLE001
            measured = 0
        views = []
        cur.execute(
            "SELECT table_schema, table_name, view_definition "
            "FROM information_schema.views "
            "WHERE table_catalog = current_database() %s"
            % where, args)
        for r in cur.fetchall():
            views.append({"schema": r[0], "name": r[1],
                          "definition": str(r[2] or "")[:8000]})
    except Exception as e:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "connector": key, "error": str(e)[:400]}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # conversion readiness: every view's SQL parsed with the real dialect
    import sqlglot
    dialect = _SQL_DIALECTS.get(key, "postgres")
    convertible, needs_review = [], []
    for v in views:
        if not v["definition"].strip():
            needs_review.append({"view": v["name"],
                                 "reason": "definition not visible to "
                                           "this user"})
            continue
        try:
            sqlglot.parse_one(v["definition"], read=dialect)
            convertible.append(v["name"])
        except Exception as e:  # noqa: BLE001
            needs_review.append({"view": v["name"], "reason": str(e)[:150]})

    base_tables = [t for t in tables.values()
                   if "VIEW" not in t["type"].upper()]
    manifest = {"tables": [
        {"name": t["name"], "schema": t["schema"],
         **({"database": database} if database else {}),
         "columns": [{"name": c["name"], "type": c["type"]}
                     for c in t["columns"]]}
        for t in base_tables]}
    import yaml as _yaml
    return {
        "ok": True, "connector": key,
        "database": database, "schema": schema or "(all)",
        "elapsed_ms": int((time.time() - started) * 1000),
        "tables": sorted(tables.values(),
                         key=lambda t: (-t["rows"], t["name"])),
        "views": [{"schema": v["schema"], "name": v["name"]}
                  for v in views],
        "view_definitions": {v["name"]: v["definition"] for v in views},
        "readiness": {
            "tables": len(base_tables),
            "views": len(views),
            "total_rows": sum(t["rows"] for t in base_tables),
            "tables_with_columns": sum(1 for t in base_tables
                                       if t["columns"]),
            "column_sizes_measured": measured,
            "views_convertible": len(convertible),
            "views_needing_review": needs_review,
            "verdict": "READY" if base_tables or convertible else
                       "NOTHING_TO_CONVERT",
        },
        "manifest_yaml": _yaml.safe_dump(manifest, sort_keys=False,
                                         width=100),
    }


def test_connection(key: str, params: Dict[str, str]) -> dict:
    """Open a real session and probe it as a DIAGNOSTIC LADDER: the
    connection is established with credentials only, then role/warehouse/
    database/schema are verified as separate steps — so one wrong context
    value never masks the fact that authentication works, and a missing
    database comes back with the list of databases the role CAN see."""
    if not _has_live_driver(key):
        return {"ok": False, "connector": key, "unsupported": True,
                "error": "live check not implemented for '%s' yet — "
                         "snowflake is the first certified connector"
                         % key}
    if key in _SQL_DIALECTS:
        return _sqldb_test(key, params)
    if key == "databricks":
        return _databricks_test(params)
    started = time.time()
    try:
        # credentials + role only: context problems must not hide auth
        conn = _snowflake_connect(params, with_context=False)
    except Exception as e:  # noqa: BLE001 — report, never crash the app
        msg = str(e)
        # A missing credential is not a broken connection — it just can't be
        # probed until a password is supplied. Flag it so the UI shows an
        # actionable "needs credential" state instead of a red failure.
        needs_credential = "no password provided" in msg.lower()
        return {"ok": False, "connector": key, "authenticated": False,
                "needs_credential": needs_credential,
                "latency_ms": int((time.time() - started) * 1000),
                "error": msg[:400]}
    report: dict = {"ok": True, "connector": key, "authenticated": True,
                    "probes": [], "steps": []}
    try:
        cur = conn.cursor()

        def probe(label: str, sql: str):
            t0 = time.time()
            cur.execute(sql)
            row = cur.fetchone()
            report["probes"].append({
                "probe": label, "sql": sql,
                "result": str(row[0]) if row and row[0] is not None
                else str(row),
                "ms": int((time.time() - t0) * 1000)})
            return row

        def step(label: str, sql: str) -> bool:
            try:
                cur.execute(sql)
                report["steps"].append({"step": label, "ok": True})
                return True
            except Exception as e:  # noqa: BLE001
                report["steps"].append({"step": label, "ok": False,
                                        "error": str(e)[:300]})
                report["ok"] = False
                return False

        probe("server_version", "SELECT CURRENT_VERSION()")
        probe("server_time", "SELECT CURRENT_TIMESTAMP()")

        wh_ok = not params.get("warehouse") or step(
            "warehouse %s" % params["warehouse"],
            'USE WAREHOUSE "%s"' % params["warehouse"].replace('"', ""))
        db_ok = True
        if params.get("database"):
            db_ok = step("database %s" % params["database"],
                         'USE DATABASE "%s"'
                         % params["database"].replace('"', ""))
            if not db_ok:
                try:  # what CAN this role see? (names only, capped)
                    cur.execute("SHOW DATABASES")
                    report["databases_visible"] = sorted(
                        str(r[1]) for r in cur.fetchall())[:50]
                except Exception:  # noqa: BLE001
                    pass
        if db_ok and params.get("schema"):
            step("schema %s" % params["schema"],
                 'USE SCHEMA "%s"' % params["schema"].replace('"', "")
                 .upper())

        ctx = cur.execute(
            "SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), "
            "CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA()"
        ).fetchone()
        report["context"] = dict(zip(
            ("account", "user", "role", "warehouse", "database", "schema"),
            (str(x) if x is not None else None for x in ctx)))
        if db_ok and wh_ok and params.get("database"):
            row = cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE table_schema = COALESCE(%s, table_schema)",
                ((params.get("schema") or "").upper() or None,)).fetchone()
            report["objects"] = {"tables_visible": int(row[0])}
        report["latency_ms"] = int((time.time() - started) * 1000)
        if not report["ok"]:
            bad = [s for s in report["steps"] if not s["ok"]]
            report["error"] = ("authenticated, but context failed — %s"
                               % "; ".join("%s: %s" % (s["step"],
                                                       s.get("error", ""))
                                           for s in bad))[:500]
    except Exception as e:  # noqa: BLE001
        report.update({"ok": False, "error": str(e)[:400],
                       "latency_ms": int((time.time() - started) * 1000)})
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return report


def run_live_validation(tests_path: str, key: str, params: Dict[str, str],
                        max_tests: int = 50,
                        mappings: Optional[List[str]] = None) -> dict:
    """Execute the generated validation suite's target-side queries
    against the LIVE warehouse. Read-only; per-test evidence returned.

    A test passes when its expectation is met (violations == 0 style) or,
    for comparison tests without a legacy connection, its value is
    recorded for the reconciliation pair."""
    doc = json.loads(Path(tests_path).read_text(encoding="utf-8"))
    if key != "snowflake":
        return {"ok": False, "error": "live validation supports snowflake "
                                      "first; '%s' pending" % key}
    conn = _snowflake_connect(params)
    results: List[dict] = []
    ran = passed = failed = errored = 0
    try:
        cur = conn.cursor()
        for pm in doc["mappings"]:
            if pm.get("skipped"):
                continue
            if mappings and pm["mapping"] not in mappings:
                continue
            for t in pm["tests"]:
                sql = t.get("target_sql")
                if not sql or ran >= max_tests:
                    continue
                ran += 1
                entry = {"mapping": pm["mapping"], "name": t["name"],
                         "test_type": t["test_type"], "sql": sql}
                try:
                    t0 = time.time()
                    row = cur.execute(sql).fetchone()
                    value = row[0] if row else None
                    entry["value"] = str(value)
                    entry["ms"] = int((time.time() - t0) * 1000)
                    if "violations == 0" in str(t.get("expectation", "")):
                        ok = (value == 0)
                        entry["status"] = "pass" if ok else "fail"
                        passed += ok
                        failed += (not ok)
                    else:
                        entry["status"] = "measured"   # reconciliation side
                except Exception as e:  # noqa: BLE001
                    entry["status"] = "error"
                    entry["error"] = str(e)[:300]
                    errored += 1
                results.append(entry)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": errored == 0 and failed == 0,
            "ran": ran, "passed": passed, "failed": failed,
            "errored": errored,
            "measured": sum(1 for r in results
                            if r["status"] == "measured"),
            "results": results}


def introspect(key: str, params: Dict[str, str],
               max_tables: int = 500) -> dict:
    """Read-only inventory of the connected database: tables (rows/bytes),
    columns, views WITH their SQL, a conversion-readiness assessment of
    every view definition, and a ready-to-use table manifest for the
    Pipeline scaffold. Nothing is written, nothing is executed beyond
    INFORMATION_SCHEMA selects."""
    if not _has_live_driver(key):
        return {"ok": False, "connector": key, "unsupported": True,
                "error": "introspection not implemented for '%s' yet" % key}
    if key in _SQL_DIALECTS:
        return _sqldb_introspect(key, params, max_tables=max_tables)
    if key == "databricks":
        return _databricks_introspect(params, max_tables=max_tables)
    database = params.get("database", "")
    schema = (params.get("schema") or "").upper()
    if not database:
        return {"ok": False, "error": "database is required to introspect"}
    started = time.time()
    try:
        conn = _snowflake_connect(params)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "connector": key, "error": str(e)[:400]}
    try:
        cur = conn.cursor()
        schema_pred = "AND table_schema = %s" if schema else \
            "AND table_schema <> 'INFORMATION_SCHEMA'"
        args = (schema,) if schema else ()
        rows = cur.execute(
            "SELECT table_schema, table_name, table_type, row_count, "
            "bytes FROM INFORMATION_SCHEMA.TABLES "
            "WHERE table_catalog = CURRENT_DATABASE() %s "
            "ORDER BY table_schema, table_name LIMIT %d"
            % (schema_pred, max_tables), args).fetchall()
        tables = {(r[0], r[1]): {"schema": r[0], "name": r[1],
                                 "type": str(r[2] or "BASE TABLE"),
                                 "rows": int(r[3] or 0),
                                 "bytes": int(r[4] or 0), "columns": []}
                  for r in rows}
        for r in _fetch_columns(
                lambda sql: cur.execute(sql % schema_pred, args).fetchall(),
                "SELECT table_schema, table_name, column_name, data_type, "
                "character_maximum_length, numeric_precision, numeric_scale "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE table_catalog = CURRENT_DATABASE() %s "
                "ORDER BY table_schema, table_name, ordinal_position",
                "SELECT table_schema, table_name, column_name, data_type "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE table_catalog = CURRENT_DATABASE() %s "
                "ORDER BY table_schema, table_name, ordinal_position"):
            key_ = (r[0], r[1])
            if key_ in tables:
                tables[key_]["columns"].append({"name": str(r[2]),
                                                "type": r[3]})
        try:
            measured = _measure_column_sizes(
                lambda sql: cur.execute(sql).fetchall(), tables)
        except Exception:  # noqa: BLE001
            measured = 0
        views = []
        for r in cur.execute(
                "SELECT table_schema, table_name, view_definition "
                "FROM INFORMATION_SCHEMA.VIEWS "
                "WHERE table_catalog = CURRENT_DATABASE() %s"
                % schema_pred, args).fetchall():
            views.append({"schema": r[0], "name": r[1],
                          "definition": str(r[2] or "")[:8000]})
    except Exception as e:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "connector": key, "error": str(e)[:400]}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # conversion readiness: every view's SQL parsed with the real engine
    import sqlglot
    convertible, needs_review = [], []
    for v in views:
        if not v["definition"].strip():
            needs_review.append({"view": v["name"],
                                 "reason": "definition not visible to "
                                           "this role"})
            continue
        try:
            sqlglot.parse_one(v["definition"], read="snowflake")
            convertible.append(v["name"])
        except Exception as e:  # noqa: BLE001
            needs_review.append({"view": v["name"],
                                 "reason": str(e)[:150]})

    base_tables = [t for t in tables.values()
                   if "VIEW" not in t["type"].upper()]
    manifest = {"tables": [
        {"name": t["name"], "schema": t["schema"],
         **({"database": database} if database else {}),
         "columns": [{"name": c["name"], "type": c["type"]}
                     for c in t["columns"]]}
        for t in base_tables]}
    import yaml as _yaml
    return {
        "ok": True, "connector": key,
        "database": database, "schema": schema or "(all)",
        "elapsed_ms": int((time.time() - started) * 1000),
        "tables": sorted(tables.values(),
                         key=lambda t: (-t["rows"], t["name"])),
        "views": [{"schema": v["schema"], "name": v["name"]}
                  for v in views],
        "view_definitions": {v["name"]: v["definition"] for v in views},
        "readiness": {
            "tables": len(base_tables),
            "views": len(views),
            "total_rows": sum(t["rows"] for t in base_tables),
            "tables_with_columns": sum(1 for t in base_tables
                                       if t["columns"]),
            "column_sizes_measured": measured,
            "views_convertible": len(convertible),
            "views_needing_review": needs_review,
            "verdict": "READY" if base_tables or convertible else
                       "NOTHING_TO_CONVERT",
        },
        "manifest_yaml": _yaml.safe_dump(manifest, sort_keys=False,
                                         width=100),
    }

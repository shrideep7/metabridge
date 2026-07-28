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


# Connectors that have a real, live driver in this build. Everything else is
# reachable only through the declarative flows (artifact generation, scaffold
# from a table manifest) — attempting a live probe returns an HONEST
# "unsupported" result, which callers must NOT treat as a failed connection.
LIVE_CONNECTORS = frozenset({"snowflake"})


def live_support(key: str) -> Dict[str, bool]:
    """What live actions a connector's driver actually implements. The UI
    reads this to offer only working flows instead of showing every
    connector as if it were fully live-integrated."""
    live = key in LIVE_CONNECTORS
    return {"live_test": live, "introspect": live, "live_load": live}


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


def test_connection(key: str, params: Dict[str, str]) -> dict:
    """Open a real session and probe it as a DIAGNOSTIC LADDER: the
    connection is established with credentials only, then role/warehouse/
    database/schema are verified as separate steps — so one wrong context
    value never masks the fact that authentication works, and a missing
    database comes back with the list of databases the role CAN see."""
    if key not in LIVE_CONNECTORS:
        return {"ok": False, "connector": key, "unsupported": True,
                "error": "live check not implemented for '%s' yet — "
                         "snowflake is the first certified connector"
                         % key}
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
    doc = json.loads(Path(tests_path).read_text())
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
    if key not in LIVE_CONNECTORS:
        return {"ok": False, "connector": key, "unsupported": True,
                "error": "introspection not implemented for '%s' yet" % key}
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
        for r in cur.execute(
                "SELECT table_schema, table_name, column_name, data_type "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE table_catalog = CURRENT_DATABASE() %s "
                "ORDER BY table_schema, table_name, ordinal_position"
                % schema_pred, args).fetchall():
            key_ = (r[0], r[1])
            if key_ in tables:
                tables[key_]["columns"].append(
                    {"name": str(r[2]), "type": str(r[3])})
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
            "views_convertible": len(convertible),
            "views_needing_review": needs_review,
            "verdict": "READY" if base_tables or convertible else
                       "NOTHING_TO_CONVERT",
        },
        "manifest_yaml": _yaml.safe_dump(manifest, sort_keys=False,
                                         width=100),
    }

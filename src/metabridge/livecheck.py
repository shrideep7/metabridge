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
import re
import time
from pathlib import Path
from typing import Dict, List, Optional


# Connectors that have a REAL, live driver in this build. Snowflake,
# Databricks and Oracle each use their native driver; PostgreSQL and Amazon
# Redshift share the psycopg2 + INFORMATION_SCHEMA path (Redshift speaks the
# PostgreSQL wire protocol), which is what this map is for. A database that
# speaks that wire protocol is a one-line entry here plus its driver in the
# `connectors` extra; one that does not — Oracle reads ALL_* views, not
# INFORMATION_SCHEMA — gets its own connect/test/introspect trio and a branch
# in `_has_live_driver`. Either way the UI turns on Test/Analyze automatically
# for any connector `live_support` reports as live.
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
                            "character", "varchar2", "nvarchar2", "clob",
                            "nclob"})


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


_MAX_BODY = 8000


def _with_body(obj: dict, raw: object, where: str) -> dict:
    """Attach a fetched object's SQL body to it, REDACTED.

    Any object that carries logic — a function, a procedure, a task, a pipe —
    can have a credential written into its body. The raw text is scanned, the
    secret VALUES are replaced before storage, and the finding (location and
    type only, never the value) rides along so the UI can say a credential was
    found here. The plaintext never enters the response.
    """
    text = str(raw or "")
    findings = []
    if text:
        try:
            from .security.engine import redact_secrets, scan_text_secrets
            findings = scan_text_secrets(text, where)
            text = redact_secrets(text)
        except Exception:  # noqa: BLE001
            # The scanner is unavailable, so this body CANNOT be cleared.
            # Withhold it rather than risk carrying a plaintext credential
            # out of the source system — the object itself still lists.
            text = ""
            findings = [{"location": where, "type": "unscanned",
                         "evidence": "body withheld — secret scan "
                                     "unavailable"}]
    obj["definition"] = text[:_MAX_BODY]
    if findings:
        obj["secret_findings"] = findings
    return obj


# Per-class cap. One object class must not be able to dominate a response
# (a database with PostGIS installed reports thousands of functions), and a
# truncated list is recorded as such — a capped list that reads as complete
# is worse than no list at all.
_MAX_OBJECTS = 1000

# Capability status vocabulary, shared by every connector's introspect:
#
#   available          the probe returned objects
#   empty              the probe ran; this database has none of this class
#   blocked_privilege  the connected role is not allowed to see them
#   not_applicable     this platform/version has no such catalog object
#                      (Redshift has no pg_matviews; a Snowflake edition
#                      does not carry masking policies)
#   error              anything else — `reason` carries the driver's text
#
# "The platform doesn't have it", "your role can't see it" and "it broke"
# are three different facts, and a zero that means all three is a lie.
#
# Privilege is tested FIRST: Snowflake reports a permission problem as
# "does not exist or not authorized", which would otherwise be read as
# "this platform has no such object".
_PRIVILEGE_TOKENS = ("permission denied", "insufficient privilege",
                     "not authorized", "must be owner", "access denied",
                     "does not have privilege", "access control",
                     "not granted")
_ABSENT_TOKENS = ("does not exist", "not supported", "unsupported feature",
                  "undefined table", "unknown table", "no such",
                  "not supported in", "requires enterprise",
                  "only available in the", "not enabled for")


def _classify_probe_error(msg: str) -> dict:
    """Map a driver error to a capability status. The error TEXT is trusted
    first because it is the only thing that distinguishes "you may not read
    this" from "this does not exist here"."""
    low = (msg or "").lower()
    if any(t in low for t in _PRIVILEGE_TOKENS):
        status = "blocked_privilege"
    elif any(t in low for t in _ABSENT_TOKENS):
        status = "not_applicable"
    else:
        status = "error"
    return {"status": status, "reason": str(msg)[:200]}


def _guarded(caps: dict, name: str, fetch, on_error=None,
             cap: Optional[int] = None) -> List[tuple]:
    """Run ONE object-class fetch and record what happened to it.

    Every class is independent: a class the role cannot read, or that this
    platform does not have, yields [] with the reason recorded and the rest
    of the inventory still returns. `on_error` is where psycopg2 callers pass
    conn.rollback() — a failed statement poisons the transaction, so the next
    class cannot run until it is cleared.
    """
    try:
        rows = list(fetch() or [])
    except Exception as e:  # noqa: BLE001 — one class must not cost the run
        caps[name] = _classify_probe_error(str(e))
        if on_error is not None:
            try:
                on_error()
            except Exception:  # noqa: BLE001
                pass
        return []
    entry = {"status": "available" if rows else "empty"}
    if cap and len(rows) >= cap:
        entry["truncated"] = True
    caps[name] = entry
    return rows


# ---------------------------------------------------------------------------
# Table manifest: the handoff from introspection to the Pipeline scaffold.
#
# Columns and types come straight from the catalog. The two settings that
# decide LOAD BEHAVIOUR do not:
#
#   unique_key         a declared PRIMARY KEY, when the catalog has one.
#                      Warehouses like Snowflake do not enforce PKs, so many
#                      tables simply never declare one.
#   incremental_column no catalog records "this is my watermark". It can only
#                      be INFERRED from column names/types, so it is emitted
#                      as a COMMENTED suggestion a human confirms — an
#                      incorrect watermark silently drops rows, which is far
#                      worse than a full reload.
#
# Without both, scaffold falls through to LoadStrategy.FULL: a full reload of
# every table, every run.
# ---------------------------------------------------------------------------

# Ordered by how strongly the name implies a change-tracking timestamp.
_WATERMARK_HINTS = ("updated_at", "update_ts", "last_updated", "last_modified",
                    "modified_at", "modified_date", "changed_on", "change_date",
                    "aedat", "laeda", "erdat", "load_ts", "load_date",
                    "etl_timestamp", "dw_updated_at", "_fivetran_synced",
                    "created_at", "create_date", "erdat_tst")
_DATEISH = ("date", "time", "timestamp", "datetime", "dats", "tims")


def _watermark_candidates(columns: List[dict]) -> List[str]:
    """Date/timestamp columns whose NAME implies change tracking, best first.

    Name AND type must both agree: a VARCHAR called 'updated_at' is not a
    usable watermark, and a DATE called 'birth_date' is not a change marker.
    """
    dated = [c for c in columns
             if any(d in str(c.get("type", "")).lower() for d in _DATEISH)]
    ranked: List[str] = []
    for hint in _WATERMARK_HINTS:
        for c in dated:
            name = str(c.get("name", ""))
            if name.lower() == hint and name not in ranked:
                ranked.append(name)
    for hint in _WATERMARK_HINTS:                    # then substring matches
        for c in dated:
            name = str(c.get("name", ""))
            if hint in name.lower() and name not in ranked:
                ranked.append(name)
    return ranked


def _manifest_entry(table: dict, database: str,
                    pks: Optional[Dict[tuple, List[str]]] = None) -> dict:
    """One manifest table entry, with unique_key when the catalog declared a
    primary key. The watermark is NOT set here — see _manifest_yaml."""
    entry: Dict[str, object] = {"name": table["name"], "schema": table["schema"]}
    if database:
        entry["database"] = database
    key = (table["schema"], table["name"])
    declared = list((pks or {}).get(key, []))
    if declared:
        entry["unique_key"] = declared
    entry["columns"] = [{"name": c["name"], "type": c["type"]}
                        for c in table["columns"]]
    return entry


def _manifest_yaml(base_tables: List[dict], database: str,
                   pks: Optional[Dict[tuple, List[str]]] = None) -> str:
    """Scaffold-ready manifest YAML.

    Inferred watermarks are appended as commented `incremental_column:` lines
    under their table, so the manifest runs as-is (full reload — always
    correct, just expensive) and becomes incremental the moment a human
    uncomments a line they have verified.
    """
    import yaml as _yaml
    doc = {"tables": [_manifest_entry(t, database, pks) for t in base_tables]}
    text = _yaml.safe_dump(doc, sort_keys=False, width=100)

    suggestions: Dict[str, List[str]] = {}
    missing_key: List[str] = []
    for t in base_tables:
        cands = _watermark_candidates(t["columns"])
        if cands:
            suggestions[t["name"]] = cands[:3]
        if not (pks or {}).get((t["schema"], t["name"])):
            missing_key.append(t["name"])
    if not suggestions and not missing_key:
        return text

    # Re-emit line by line. A table's comments are held until the NEXT table
    # starts (or EOF) and inserted BEFORE that line, so they close the block
    # they describe instead of landing inside its `columns:` list.
    out: List[str] = []
    pending: List[str] = []

    for line in text.splitlines():
        mo = re.match(r"^(\s*)-\s+name:\s+(\S+)\s*$", line)
        if mo and mo.group(1) == "":          # top-level table entry only
            out.extend(pending)
            pending = []
            pad, tname = "  ", mo.group(2)
            for cand in suggestions.get(tname, []):
                pending.append(
                    "%s# incremental_column: %s   # inferred from the column "
                    "name - VERIFY before enabling" % (pad, cand))
            if tname in missing_key:
                pending.append(
                    "%s# unique_key: []   # no PRIMARY KEY declared in the "
                    "catalog; set this for MERGE loads" % pad)
        out.append(line)
    out.extend(pending)
    header = ("# Table manifest generated by introspection.\n"
              "# columns/types are catalog fact. Commented lines are "
              "SUGGESTIONS:\n"
              "# uncomment (and verify) incremental_column + unique_key to "
              "get MERGE\n"
              "# loads - until then every table is a FULL reload on every "
              "run.\n")
    return header + "\n".join(out) + "\n"


def _at(row, i: int, default: str = "") -> str:
    """Positional access that tolerates a short row — catalog result shapes
    vary by server version, and a missing trailing column must not raise."""
    try:
        v = row[i]
    except (IndexError, TypeError):
        return default
    return default if v is None else str(v)


# ---------------------------------------------------------------------------
# Snowflake SHOW helpers. Several object classes (streams, tasks, pipes,
# stages, policies, tags) have no INFORMATION_SCHEMA view at all, so SHOW is
# the only catalog source.
# ---------------------------------------------------------------------------

def _snowflake_primary_keys(cur, database: str,
                            schema: str) -> Dict[tuple, List[str]]:
    """{(schema, table): [pk columns in key order]} from SHOW PRIMARY KEYS.

    Snowflake does not ENFORCE primary keys, so this finds only what someone
    declared — an empty result is a normal, expected outcome, never an error.
    Failure here must not cost the inventory, so it degrades to {}.
    """
    scope = ('SCHEMA "%s"."%s"' % (database.replace('"', ""),
                                   schema.replace('"', "")) if schema
             else 'DATABASE "%s"' % database.replace('"', ""))
    try:
        rows = _show(cur, "SHOW PRIMARY KEYS IN " + scope)
    except Exception:  # noqa: BLE001 — no PKs declared, or no privilege
        return {}
    # SHOW PRIMARY KEYS: created_on, database_name, schema_name, table_name,
    # column_name, key_sequence, constraint_name, rely, comment
    ordered: Dict[tuple, List[tuple]] = {}
    for r in rows:
        tbl, col = _at(r, 3), _at(r, 4)
        if not tbl or not col:
            continue
        try:
            seq = int(_at(r, 5, "0") or 0)
        except ValueError:
            seq = 0
        ordered.setdefault((_at(r, 2), tbl), []).append((seq, col))
    return {k: [c for _, c in sorted(v)] for k, v in ordered.items()}


def _show(cur, sql: str):
    """Run a SHOW and return its rows.

    Deliberately does NOT swallow errors: `_guarded` is what records why a
    class came back empty, and an error swallowed here would report a
    privilege-blocked class as merely "this account has none".
    """
    return cur.execute(sql).fetchall()


def _named(cur, r, column: str) -> str:
    """A SHOW row's column looked up by NAME instead of offset.

    SHOW output column ORDER shifts between Snowflake releases; the NAMES do
    not. Definitions are read this way because a wrong offset would not fail
    loudly — it would silently carry the wrong text into a conversion.
    Returns "" when the driver exposes no description, so a missing
    definition stays visibly missing."""
    cols = [str(d[0]).lower()
            for d in (getattr(cur, "description", None) or [])]
    if column.lower() not in cols:
        return ""
    return _at(r, cols.index(column.lower()))


def _scope(database: str, schema: str) -> str:
    """The scoping clause for SHOW. Restricts to the CONNECTED database (and
    schema when given) so a bare SHOW never leaks another database's objects
    into this inventory. Roles, grants and shares are account-level and are
    never scoped this way — Snowflake has no per-database notion of them."""
    db = (database or "").replace('"', "")
    sc = (schema or "").replace('"', "")
    if db and sc:
        return ' IN SCHEMA "%s"."%s"' % (db, sc)
    if db:
        return ' IN DATABASE "%s"' % db
    return ""


def _build_task_dag(tasks: List[dict]) -> List[dict]:
    """Task -> task edges from each task's declared predecessors. Names are
    reduced to the bare task name so a fully-qualified predecessor still
    matches the task it points at."""
    def short(n: str) -> str:
        return str(n).split(".")[-1].strip('"').upper()
    return [{"from": short(p), "to": short(t["name"])}
            for t in tasks for p in t.get("predecessors", [])]


def _parse_predecessors(v) -> List[str]:
    """A task's predecessors arrive as a JSON array, a list, or a bare name
    depending on the Snowflake version."""
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    s = str(v).strip()
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:  # noqa: BLE001
        pass
    return [s] if s and s not in ("[]", "null") else []


def _has_live_driver(key: str) -> bool:
    return key in ("snowflake", "databricks", "oracle") \
        or key in _SQL_DIALECTS


# Back-compat: earlier code imported this set directly.
LIVE_CONNECTORS = frozenset({"snowflake", "databricks", "oracle"}) \
    | frozenset(_SQL_DIALECTS)


def live_support(key: str) -> Dict[str, bool]:
    """What live actions a connector's driver actually implements. The UI
    reads this to offer only working flows instead of showing every
    connector as if it were fully live-integrated.

    Reported honestly per capability: the SQL connectors have a read path
    (Test connection + Analyze/introspect), while live LOAD — writing data
    into the target — is certified for Snowflake and Databricks today; the
    other connectors fall back to a generated load PACKAGE, so live_load
    stays False for them rather than overclaiming. Oracle is a live SOURCE
    for exactly that reason: it is read and inventoried, never written to."""
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


def _databricks_objects(cur, caps: dict, schema: str) -> Dict[str, list]:
    """Unity Catalog object classes beyond tables/views/columns.

    Deliberately does NOT fetch Snowflake-only classes (streams, tasks,
    pipes, stages) — Databricks has no equivalent, and an absent class is
    absent, not blocked. `volumes` runs the other way: it has no Snowflake
    equivalent and simply becomes another pill in the console.
    """
    arg = [schema or None]

    def q(sql, args=None):
        def go():
            cur.execute(sql, args if args is not None else arg)
            return cur.fetchall()
        return go

    out: Dict[str, list] = {}

    def routines(kind):
        rows = _guarded(caps, "functions" if kind == "FUNCTION"
                        else "procedures",
                        q("SELECT routine_schema, routine_name, data_type, "
                          "external_language, routine_definition "
                          "FROM information_schema.routines "
                          "WHERE routine_schema = COALESCE(?, routine_schema) "
                          "AND routine_type = ? "
                          "ORDER BY routine_schema, routine_name LIMIT %d"
                          % _MAX_OBJECTS, [schema or None, kind]),
                        cap=_MAX_OBJECTS)
        objs = []
        for r in rows:
            o = {"schema": _at(r, 0), "name": _at(r, 1),
                 "returns": _at(r, 2), "language": _at(r, 3)}
            objs.append(_with_body(o, _at(r, 4), "%s %s.%s"
                                   % (kind.lower(), o["schema"], o["name"])))
        return objs

    out["functions"] = routines("FUNCTION")
    out["procedures"] = routines("PROCEDURE")

    out["volumes"] = [
        {"schema": _at(r, 0), "name": _at(r, 1), "type": _at(r, 2)}
        for r in _guarded(caps, "volumes",
                          q("SELECT volume_schema, volume_name, volume_type "
                            "FROM information_schema.volumes "
                            "WHERE volume_schema = COALESCE(?, volume_schema) "
                            "ORDER BY volume_schema, volume_name LIMIT %d"
                            % _MAX_OBJECTS), cap=_MAX_OBJECTS)]

    out["tags"] = [
        {"schema": _at(r, 0), "name": _at(r, 2), "object": _at(r, 1),
         "allowed_values": _at(r, 3)}
        for r in _guarded(caps, "tags",
                          q("SELECT schema_name, table_name, tag_name, "
                            "tag_value FROM information_schema.table_tags "
                            "WHERE schema_name = COALESCE(?, schema_name) "
                            "LIMIT %d" % _MAX_OBJECTS), cap=_MAX_OBJECTS)]

    out["grants"] = [
        {"role": _at(r, 2), "privilege": _at(r, 3), "granted_on": "TABLE",
         "object": "%s.%s" % (_at(r, 0), _at(r, 1))}
        for r in _guarded(caps, "grants",
                          q("SELECT table_schema, table_name, grantee, "
                            "privilege_type "
                            "FROM information_schema.table_privileges "
                            "WHERE table_schema = COALESCE(?, table_schema) "
                            "LIMIT %d" % _MAX_OBJECTS), cap=_MAX_OBJECTS)]
    return out


def _databricks_catalogs(cur) -> List[str]:
    """The catalogs this login can see.

    Databricks always resolves a DEFAULT catalog, so unlike Snowflake and
    Postgres it never needs a blocking picker — inventorying the default is
    the right behaviour for the Twin and the assessment. The list rides along
    in the payload instead, so the console can offer a switcher without any
    caller losing its automatic analysis.
    """
    try:
        cur.execute("SHOW CATALOGS")
        return [n for n in (_at(r, 0) for r in cur.fetchall()) if n]
    except Exception:  # noqa: BLE001 — a switcher is a bonus, never required
        return []


def _databricks_introspect(params: Dict[str, str],
                           max_tables: int = 500) -> dict:
    """Read-only inventory for Databricks, returning the SAME shape as the
    Snowflake/SQL introspect (context + capabilities + the object classes +
    readiness + manifest) so the console and scaffold consume it unchanged."""
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
    caps: Dict[str, dict] = {}
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT current_catalog(), current_database(), "
            "current_user()").fetchone()
        catalog = catalog or _at(row, 0)
        schema = schema or _at(row, 1)
        context = {"user": _at(row, 2), "current_role": _at(row, 2),
                   "database": catalog, "schema": schema,
                   "warehouse": "", "account": "", "edition": "n/a"}
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
        caps["tables"] = {"status": "available" if tables else "empty"}
        caps["views"] = {"status": "available" if views else "empty"}
        objects = _databricks_objects(cur, caps, schema)
        available_databases = _databricks_catalogs(cur)
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
    secret_findings = [f for cls in ("functions", "procedures")
                       for o in objects.get(cls, [])
                       for f in o.get("secret_findings", [])]
    readiness = {
        "tables": len(base_tables),
        "views": len(views),
        "total_rows": sum(t["rows"] for t in base_tables),
        "tables_with_columns": sum(1 for t in base_tables if t["columns"]),
        "column_sizes_measured": measured,
        "views_convertible": len(convertible),
        "views_needing_review": needs_review,
        "verdict": "READY" if base_tables or convertible else
                   "NOTHING_TO_CONVERT",
    }
    readiness.update({k: len(v) for k, v in objects.items()})
    return {
        "ok": True, "connector": "databricks",
        "database": catalog, "schema": schema or "(all)",
        "elapsed_ms": int((time.time() - started) * 1000),
        "context": context, "capabilities": caps,
        "available_databases": available_databases,
        "tables": sorted(tables.values(),
                         key=lambda t: (-t["rows"], t["name"])),
        "views": [{"schema": v["schema"], "name": v["name"]}
                  for v in views],
        "view_definitions": {v["name"]: v["definition"] for v in views},
        "secret_findings": secret_findings,
        "readiness": readiness,
        "manifest_yaml": _manifest_yaml(base_tables, catalog),
        **objects,
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


def _sqldb_context(cur) -> dict:
    """Who this session is connected AS, and to what. PostgreSQL and Redshift
    have no edition concept, so `edition` reports "n/a" rather than inventing
    one. Never raises — an unreadable context must not cost the inventory."""
    ctx = {"user": "", "current_role": "", "database": "", "schema": "",
           "version": "", "account": "", "warehouse": "", "edition": "n/a"}
    try:
        cur.execute("SELECT current_user, current_database(), "
                    "current_schema(), version()")
        r = cur.fetchone() or ()
        ctx["user"] = _at(r, 0)
        # Postgres authorises by ROLE and current_user IS that role; the UI
        # banner reads current_role for every connector.
        ctx["current_role"] = _at(r, 0)
        ctx["database"] = _at(r, 1)
        ctx["schema"] = _at(r, 2)
        ctx["version"] = _at(r, 3)[:200]
    except Exception:  # noqa: BLE001
        pass
    return ctx


# The database a picker connects to purely to READ the database list.
# PostgreSQL cannot switch database inside a session, so listing requires
# being connected to something first.
_BOOTSTRAP_DB = {"postgres": "postgres", "redshift": "dev"}


def _list_databases_sqldb(key: str, params: Dict[str, str]) -> dict:
    """The databases this login can see, for the Data Estate picker.

    A connection saved without a database has nothing to introspect, and
    "database is required" is a dead end for someone who does not yet know
    which databases exist. Drilling into one RECONNECTS with that name —
    unlike Snowflake, a Postgres session cannot cross databases.
    """
    started = time.time()
    bootstrap = params.get("database") or _BOOTSTRAP_DB.get(key, "postgres")
    try:
        conn = _psycopg_connect(key, dict(params, database=bootstrap))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "connector": key, "error": str(e)[:400]}
    context, rows = {}, []
    try:
        cur = conn.cursor()
        context = _sqldb_context(cur)
        try:
            cur.execute("SELECT d.datname, pg_get_userbyid(d.datdba) "
                        "FROM pg_database d "
                        "WHERE d.datallowconn AND NOT d.datistemplate "
                        "ORDER BY d.datname")
            rows = [{"name": _at(r, 0), "kind": "", "owner": _at(r, 1)}
                    for r in cur.fetchall()]
        except Exception:  # noqa: BLE001 — owner lookup is the optional half
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            cur.execute("SELECT datname FROM pg_database "
                        "WHERE datallowconn AND NOT datistemplate "
                        "ORDER BY datname")
            rows = [{"name": _at(r, 0), "kind": "", "owner": ""}
                    for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "connector": key, "error": str(e)[:400]}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "connector": key, "mode": "databases",
            "elapsed_ms": int((time.time() - started) * 1000),
            "context": context, "databases": rows}


def _sqldb_introspect(key: str, params: Dict[str, str],
                      max_tables: int = 500) -> dict:
    """Read-only inventory for PostgreSQL / Redshift, returning the SAME
    shape as the Snowflake introspect (context + capabilities + the object
    classes + readiness + manifest) so the console and scaffold consume it
    unchanged. Every class beyond tables/views is fetched independently and
    reports its own capability status."""
    params = _normalize_sql_params(params)
    database = params.get("database", "")
    schema = (params.get("schema") or "").strip()
    if not database:
        # nothing to inventory yet — offer the databases instead of failing
        return _list_databases_sqldb(key, params)
    started = time.time()
    try:
        conn = _psycopg_connect(key, params)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "connector": key, "error": str(e)[:400]}
    caps: Dict[str, dict] = {}
    try:
        cur = conn.cursor()
        context = _sqldb_context(cur)

        # scope: a chosen schema, else everything but the system schemas.
        # Each catalog view names its schema column differently, so the
        # predicate is built per column rather than assumed.
        def _scope_pred(col: str):
            if schema:
                return "AND %s = %%s" % col, (schema,)
            return ("AND %s NOT IN ('pg_catalog', 'information_schema')"
                    % col, ())

        where, args = _scope_pred("table_schema")
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
        caps["tables"] = {"status": "available" if tables else "empty"}
        caps["views"] = {"status": "available" if views else "empty"}

        # ── object classes beyond tables and views ──────────────────────
        # Each is fetched independently and records its own status. Redshift
        # has no pg_matviews and no PROCEDURE routines, and a restricted role
        # may read tables but not routine bodies — none of that may cost us
        # the inventory already in hand.
        def _rollback():
            # a no-op while the session is in autocommit (which
            # _psycopg_connect sets), and the thing that clears a poisoned
            # transaction if it ever is not — a failed statement otherwise
            # blocks every class after it
            conn.rollback()

        def _q(sql, sql_args):
            def go():
                cur.execute(sql, sql_args)
                return cur.fetchall()
            return go

        mv_where, mv_args = _scope_pred("schemaname")
        materialized_views = [
            {"schema": _at(r, 0), "name": _at(r, 1)}
            for r in _guarded(
                caps, "materialized_views",
                _q("SELECT schemaname, matviewname FROM pg_matviews "
                   "WHERE true %s ORDER BY schemaname, matviewname "
                   "LIMIT %d" % (mv_where, _MAX_OBJECTS), mv_args),
                on_error=_rollback, cap=_MAX_OBJECTS)]

        seq_where, seq_args = _scope_pred("sequence_schema")
        sequences = [
            {"schema": _at(r, 0), "name": _at(r, 1),
             "start": _at(r, 2), "increment": _at(r, 3)}
            for r in _guarded(
                caps, "sequences",
                _q("SELECT sequence_schema, sequence_name, start_value, "
                   "increment FROM information_schema.sequences "
                   "WHERE sequence_catalog = current_database() %s "
                   "ORDER BY sequence_schema, sequence_name LIMIT %d"
                   % (seq_where, _MAX_OBJECTS), seq_args),
                on_error=_rollback, cap=_MAX_OBJECTS)]

        rt_where, rt_args = _scope_pred("routine_schema")

        def _routines(routine_type):
            return _q(
                "SELECT routine_schema, routine_name, data_type, "
                "external_language, routine_definition "
                "FROM information_schema.routines "
                "WHERE routine_catalog = current_database() "
                "AND routine_type = %%s %s "
                "ORDER BY routine_schema, routine_name LIMIT %d"
                % (rt_where, _MAX_OBJECTS), (routine_type,) + rt_args)

        def _routine_objects(rows):
            out = []
            for r in rows:
                o = {"schema": _at(r, 0), "name": _at(r, 1),
                     "returns": _at(r, 2), "language": _at(r, 3)}
                # the body may carry a credential — redact before storing
                out.append(_with_body(o, _at(r, 4),
                                      "%s.%s" % (o["schema"], o["name"])))
            return out

        functions = _routine_objects(_guarded(
            caps, "functions", _routines("FUNCTION"),
            on_error=_rollback, cap=_MAX_OBJECTS))
        procedures = _routine_objects(_guarded(
            caps, "procedures", _routines("PROCEDURE"),
            on_error=_rollback, cap=_MAX_OBJECTS))
        secret_findings = [f for o in functions + procedures
                           for f in o.get("secret_findings", [])]
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
    return {
        "ok": True, "connector": key,
        "database": database, "schema": schema or "(all)",
        "elapsed_ms": int((time.time() - started) * 1000),
        "context": context, "capabilities": caps,
        "tables": sorted(tables.values(),
                         key=lambda t: (-t["rows"], t["name"])),
        "views": [{"schema": v["schema"], "name": v["name"]}
                  for v in views],
        "view_definitions": {v["name"]: v["definition"] for v in views},
        "materialized_views": materialized_views,
        "sequences": sequences,
        "functions": functions,
        "procedures": procedures,
        "secret_findings": secret_findings,
        "readiness": {
            "tables": len(base_tables),
            "views": len(views),
            "total_rows": sum(t["rows"] for t in base_tables),
            "tables_with_columns": sum(1 for t in base_tables
                                       if t["columns"]),
            "column_sizes_measured": measured,
            "materialized_views": len(materialized_views),
            "sequences": len(sequences),
            "functions": len(functions),
            "procedures": len(procedures),
            "views_convertible": len(convertible),
            "views_needing_review": needs_review,
            "verdict": "READY" if base_tables or convertible else
                       "NOTHING_TO_CONVERT",
        },
        "manifest_yaml": _manifest_yaml(base_tables, database),
    }


# ---------------------------------------------------------------------------
# Oracle Database
#
# Oracle is the one live connector whose database is NOT switchable inside a
# session: the service name resolves the database (or PDB) at connect time.
# There is therefore no database picker here — the Level 1 choice an Oracle
# user actually has is the SCHEMA, and that is a filter over one inventory
# rather than a reconnect, so a missing service name is reported as the
# actionable form problem it is instead of a list to drill into.
#
# It also has no INFORMATION_SCHEMA. Every class below reads an ALL_* catalog
# view, which shows exactly what the connected user has been granted and
# nothing more — the same read-only, evidence-based contract as the others.
# ---------------------------------------------------------------------------

# Schemas Oracle ships and maintains itself. Inventorying them would bury the
# estate the user asked about under thousands of internal objects. 12c+ flags
# them with ALL_USERS.ORACLE_MAINTAINED, but that column does not exist on
# 11g, so the list is explicit and version-independent.
_ORACLE_SYSTEM_SCHEMAS = (
    "SYS", "SYSTEM", "XDB", "OUTLN", "CTXSYS", "MDSYS", "ORDSYS", "ORDDATA",
    "ORDPLUGINS", "DBSNMP", "APPQOSSYS", "GSMADMIN_INTERNAL", "WMSYS",
    "LBACSYS", "OLAPSYS", "AUDSYS", "DVSYS", "DVF", "SI_INFORMTN_SCHEMA",
    "ANONYMOUS", "DIP", "ORACLE_OCM", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC",
    "SYS$UMF", "GGSYS", "REMOTE_SCHEDULER_AGENT", "OJVMSYS", "DBSFWUSER",
    "GSMCATUSER", "GSMUSER", "PDBADMIN", "FLOWS_FILES", "RDSADMIN",
)

# Table NAMES Oracle generates for its own machinery. No catalog flag marks
# these: they are ordinary heap tables owned by the APPLICATION schema, so
# only the name gives them away. Counting them as tables to migrate both
# inflates the estate and invents work — a schema with five Advanced Queuing
# queues presents a dozen extra "tables" that have no business meaning and no
# migration target. `$` is the marker Oracle uses for generated names, so the
# patterns stop at it rather than spelling out the suffix (`_` is a LIKE
# single-character wildcard, and `AQ$_%` would need an ESCAPE clause to mean
# what it looks like it means).
_ORACLE_INTERNAL_TABLES = (
    "AQ$%",             # Advanced Queuing: subscriber, history, index tables
    "MLOG$%",           # materialized view logs
    "RUPD$%",           # updatable materialized view logs
    "DR$%",             # Oracle Text index internals
    "SYS_IOT_OVER%",    # IOT overflow (IOT_TYPE also covers it)
    "SYS_EXPORT%",      # Data Pump master tables
    "SYS_IMPORT%",
    "SYSTP%",           # online-redefinition scratch tables
    "SCHEDULER$%",      # scheduler internals
    "LOGMNR%",          # LogMiner staging
    "BIN$%",            # dropped, still in the recycle bin
)

# ALL_SOURCE stores one row per LINE, so this cap is a line budget rather than
# an object budget — a single large package can run to thousands of lines.
_MAX_SOURCE_LINES = 50_000


def _ora_not_internal(col: str = "table_name") -> str:
    """Exclude Oracle's generated tables by name. Returned ready to
    CONCATENATE, never to %-format — the LIKE patterns contain % themselves."""
    return " ".join("AND %s NOT LIKE '%s'" % (col, p)
                    for p in _ORACLE_INTERNAL_TABLES)


def _ora_owner_pred(col: str, schema: str, allow_public: bool = False):
    """Scope a catalog query to one schema, else to everything Oracle does not
    maintain itself. Returns the SQL fragment AND its binds, because the
    unscoped form takes none and psycopg-style positional args would not
    survive that.

    PUBLIC is excluded by default. It is not a schema anyone owns — it is the
    pseudo-owner Oracle files public synonyms under, and a stock 23ai database
    ships THOUSANDS of them. Including it does not just add noise: it floods
    past `_MAX_OBJECTS`, so the class comes back flagged `truncated` and the
    console correctly reports a partial list — for an estate that may have had
    a dozen real synonyms. `allow_public` re-admits it for the two classes
    where a PUBLIC object is genuinely the user's (see the callers).
    """
    if schema:
        return "AND %s = :owner" % col, {"owner": schema}
    excluded = _ORACLE_SYSTEM_SCHEMAS if allow_public \
        else _ORACLE_SYSTEM_SCHEMAS + ("PUBLIC",)
    return ("AND %s NOT IN (%s) AND %s NOT LIKE 'APEX%%'"
            % (col, ", ".join("'%s'" % s for s in excluded), col)), {}


def _ora_top(sql: str, limit: int) -> str:
    """Oracle's portable top-N. FETCH FIRST is 12c-only, and a bare ROWNUM
    predicate is evaluated BEFORE the sort — which would cap an arbitrary set
    rather than the first N — so the ranked query goes in an inline view and
    the cap is applied outside it.

    NOT usable on a query that selects a LONG column (ALL_VIEWS.TEXT,
    ALL_MVIEWS.QUERY, ALL_TRIGGERS.TRIGGER_BODY): a LONG may not appear in an
    inline view at all (ORA-00997). Those queries cap inline instead and sort
    in Python.
    """
    return "SELECT * FROM (%s) WHERE ROWNUM <= %d" % (sql, int(limit))


def _oracle_connect(params: Dict[str, str]):
    """Open a READ-ONLY Oracle session via python-oracledb in THIN mode — no
    Oracle Instant Client, so the deployment needs nothing beyond the wheel.

    Inputs are validated BEFORE the driver import so a missing credential is
    reported as the same actionable form problem whether or not the optional
    driver happens to be installed on this host.
    """
    password = _secret("oracle", "password", params)
    if not password:
        raise ValueError(
            "No password provided — export MB_ORACLE_PASSWORD (values are "
            "never stored) or pass it transiently in the request.")
    host = (params.get("host") or "").strip()
    if not host:
        raise ValueError("No host provided — a hostname is required to "
                         "connect.")
    service = (params.get("database") or "").strip()
    if not service:
        raise ValueError(
            "No service name provided — Oracle fixes the database at connect "
            "time, so the Database field must carry the service name "
            "(Oracle 23ai Free uses FREE or FREEPDB1) or the SID.")
    try:
        import oracledb  # driver import deferred: optional dep
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "The Oracle driver is not installed in this deployment. "
            "Rebuild the image with the connectors extra — "
            "pip install 'metabridge[web,dtd,connectors]' "
            "(adds oracledb) — then retry Test connection."
        ) from e
    conn = oracledb.connect(
        user=params.get("user", ""), password=password,
        dsn="%s:%d/%s" % (host, int(params.get("port") or 1521), service),
        tcp_connect_timeout=int(params.get("login_timeout", 20)))
    schema = (params.get("schema") or "").strip().upper()
    if schema:
        try:
            # Unqualified probes resolve in the chosen schema. Quoted so a
            # case-sensitively created schema still resolves, with any quote
            # stripped so nothing can be smuggled into the statement. Every
            # catalog query below is scoped explicitly anyway, so failing
            # here (the schema does not exist) must not cost the session —
            # _oracle_test reports that as its own diagnostic step.
            cur = conn.cursor()
            cur.execute('ALTER SESSION SET CURRENT_SCHEMA = "%s"'
                        % schema.replace('"', ""))
            cur.close()
        except Exception:  # noqa: BLE001
            pass
    try:  # defense in depth — every statement we run is already a SELECT
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        cur.close()
    except Exception:  # noqa: BLE001
        pass
    return conn


def _oracle_context(cur) -> dict:
    """Who this session is connected AS, and to what. Oracle has no edition
    concept the catalog will name, so `edition` reports "n/a" rather than
    inventing one. Never raises — an unreadable context must not cost the
    inventory."""
    ctx = {"user": "", "current_role": "", "database": "", "schema": "",
           "version": "", "account": "", "warehouse": "", "edition": "n/a",
           "available_roles": []}
    try:
        cur.execute("SELECT SYS_CONTEXT('USERENV', 'SESSION_USER'), "
                    "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'), "
                    "SYS_CONTEXT('USERENV', 'DB_NAME'), "
                    "SYS_CONTEXT('USERENV', 'CON_NAME') FROM DUAL")
        r = cur.fetchone() or ()
        ctx["user"] = _at(r, 0)
        # Oracle authorises by USER; roles are additive grants on top of it,
        # and the console banner reads current_role for every connector.
        ctx["current_role"] = _at(r, 0)
        ctx["schema"] = _at(r, 1)
        # CON_NAME is the pluggable database and is what the user connected
        # to; it is empty on a non-CDB, where DB_NAME is the whole story.
        ctx["database"] = _at(r, 3) or _at(r, 2)
    except Exception:  # noqa: BLE001
        pass
    try:
        cur.execute("SELECT version FROM product_component_version "
                    "WHERE ROWNUM = 1")
        ctx["version"] = _at(cur.fetchone() or (), 0)[:200]
    except Exception:  # noqa: BLE001
        pass
    try:
        # the roles this user may exercise — what makes a "reconnect with more
        # privilege" recommendation actionable rather than a guess
        cur.execute("SELECT granted_role FROM user_role_privs "
                    "ORDER BY granted_role")
        ctx["available_roles"] = [_at(r, 0) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        ctx["available_roles"] = []
    return ctx


def _oracle_hint(msg: str) -> str:
    """Turn the ORA- code the listener returns into the fix for it. These
    three are what a first connection actually fails on, and the raw code
    alone tells a non-DBA nothing."""
    low = msg.lower()
    if "ora-12514" in low or "ora-12505" in low:
        return (" — the listener does not know that service. The Database "
                "field carries the SERVICE NAME (Oracle 23ai Free uses FREE "
                "or FREEPDB1), not the host or the container name.")
    if "ora-12541" in low or "ora-12170" in low:
        return (" — nothing answered on that host and port. Check the "
                "listener is running and reachable (the default port is "
                "1521).")
    if "ora-01017" in low:
        return " — the username or password was rejected by the database."
    if "ora-28000" in low:
        return " — the account is locked; a DBA must unlock it."
    return ""


def _oracle_test(params: Dict[str, str]) -> dict:
    """Oracle live probe — the SAME evidence-based report shape as the
    Snowflake/SQL paths: authenticate first, then verify the schema as a
    separate diagnostic step (so one wrong value never masks that
    authentication works), then count the visible tables."""
    params = _normalize_sql_params(params)
    started = time.time()
    try:
        conn = _oracle_connect(params)
    except Exception as e:  # noqa: BLE001 — report, never crash the app
        msg = str(e)
        needs_credential = "no password provided" in msg.lower()
        return {"ok": False, "connector": "oracle", "authenticated": False,
                "needs_credential": needs_credential,
                "latency_ms": int((time.time() - started) * 1000),
                "error": (msg + _oracle_hint(msg))[:400]}
    report: dict = {"ok": True, "connector": "oracle", "authenticated": True,
                    "probes": [], "steps": []}
    try:
        cur = conn.cursor()

        def probe(label: str, sql: str, fallback: str = ""):
            t0 = time.time()
            try:
                cur.execute(sql)
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                if not fallback:
                    raise
                # V$VERSION needs a grant PRODUCT_COMPONENT_VERSION does not,
                # and vice versa on some hardened builds — either one answers
                # the question, so losing one is not a failed probe.
                sql = fallback
                cur.execute(sql)
                row = cur.fetchone()
            report["probes"].append({
                "probe": label, "sql": sql,
                "result": str(row[0]) if row and row[0] is not None
                else str(row),
                "ms": int((time.time() - t0) * 1000)})
            return row

        probe("server_version",
              "SELECT version FROM product_component_version "
              "WHERE ROWNUM = 1",
              "SELECT banner FROM v$version WHERE ROWNUM = 1")
        probe("server_time", "SELECT CURRENT_TIMESTAMP FROM DUAL")

        ctx = _oracle_context(cur)
        report["context"] = {"user": ctx["user"], "database": ctx["database"],
                             "schema": ctx["schema"],
                             "version": ctx["version"]}

        schema = (params.get("schema") or "").strip().upper()
        if schema:
            cur.execute("SELECT 1 FROM all_users WHERE username = :owner",
                        {"owner": schema})
            if cur.fetchone():
                report["steps"].append({"step": "schema %s" % schema,
                                        "ok": True})
            else:
                report["steps"].append(
                    {"step": "schema %s" % schema, "ok": False,
                     "error": "schema not found or not visible to this user"})
                report["ok"] = False
                cur.execute("SELECT username FROM all_users "
                            "ORDER BY username")
                report["schemas_visible"] = [_at(r, 0)
                                             for r in cur.fetchall()][:50]

        where, binds = _ora_owner_pred("owner", schema)
        cur.execute("SELECT COUNT(*) FROM all_tables WHERE 1 = 1 " + where,
                    binds)
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


def _oracle_primary_keys(cur, schema: str) -> Dict[tuple, List[str]]:
    """{(schema, table): [pk columns in key order]} from ALL_CONSTRAINTS.

    Unlike Snowflake, Oracle ENFORCES primary keys, so this is a declared key
    that can be trusted as a MERGE key — which is what lets the generated
    manifest emit `unique_key` instead of falling through to a full reload.
    Degrades to {} rather than costing the inventory."""
    where, binds = _ora_owner_pred("c.owner", schema)
    try:
        cur.execute(
            "SELECT c.owner, c.table_name, cc.column_name, cc.position "
            "FROM all_constraints c JOIN all_cons_columns cc "
            "ON c.owner = cc.owner "
            "AND c.constraint_name = cc.constraint_name "
            "WHERE c.constraint_type = 'P' AND c.status = 'ENABLED' %s "
            "ORDER BY c.owner, c.table_name, cc.position" % where, binds)
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001 — no privilege on the constraint views
        return {}
    ordered: Dict[tuple, List[tuple]] = {}
    for r in rows:
        owner, table, col = _at(r, 0), _at(r, 1), _at(r, 2)
        if not table or not col:
            continue
        try:
            pos = int(_at(r, 3, "0") or 0)
        except ValueError:
            pos = 0
        ordered.setdefault((owner, table), []).append((pos, col))
    return {k: [c for _, c in sorted(v)] for k, v in ordered.items()}


def _oracle_sources(cur, schema: str, types) -> Dict[tuple, str]:
    """PL/SQL bodies keyed by (owner, name, type), assembled from
    ALL_SOURCE's one-row-per-LINE shape. Never raises: a body this user may
    not read leaves the object listed WITHOUT its definition, which is a
    downgrade — losing the whole object list would be an outage."""
    where, binds = _ora_owner_pred("owner", schema)
    lines: Dict[tuple, List[str]] = {}
    try:
        cur.execute(_ora_top(
            "SELECT owner, name, type, text FROM all_source "
            "WHERE type IN (%s) %s ORDER BY owner, name, type, line"
            % (", ".join("'%s'" % t for t in types), where),
            _MAX_SOURCE_LINES), binds)
        for r in cur.fetchall():
            lines.setdefault((_at(r, 0), _at(r, 1), _at(r, 2)), []).append(
                _at(r, 3))
    except Exception:  # noqa: BLE001
        return {}
    return {k: "".join(v) for k, v in lines.items()}


def _oracle_long_rows(cur, sql: str, where: str, binds: dict,
                      limit: int = _MAX_OBJECTS) -> List[tuple]:
    """Run a catalog query that selects a LONG column.

    A LONG may not appear in an inline view (ORA-00997), so `_ora_top` cannot
    wrap these — the cap goes in the predicate instead and the caller sorts
    the result in Python. Deliberately does NOT swallow errors: `_guarded` is
    what records why a class came back empty.
    """
    cur.execute("%s %s AND ROWNUM <= %d" % (sql, where, int(limit)), binds)
    return cur.fetchall()


def _oracle_objects(cur, caps: dict, schema: str) -> Dict[str, list]:
    """Every Oracle object class beyond tables/views/columns.

    Each class is fetched independently through `_guarded`, so a class this
    user may not read comes back empty WITH the reason and never costs the
    classes around it. Packages, triggers, synonyms, database links and
    scheduler jobs have no Snowflake equivalent — they are simply more types
    in the estate, the same way Databricks contributes volumes.
    """
    where, binds = _ora_owner_pred("owner", schema)
    seq_where, seq_binds = _ora_owner_pred("sequence_owner", schema)

    def q(head: str, tail: str = ""):
        def go():
            cur.execute(_ora_top("%s %s %s" % (head, where, tail),
                                 _MAX_OBJECTS), binds)
            return cur.fetchall()
        return go

    def g(name, fn):
        return _guarded(caps, name, fn, cap=_MAX_OBJECTS)

    out: Dict[str, list] = {}
    bodies = _oracle_sources(cur, schema, ("FUNCTION", "PROCEDURE", "PACKAGE",
                                           "PACKAGE BODY"))

    def plsql(object_type: str, cls: str, label: str, source_types):
        objs = []
        for r in g(cls, q("SELECT owner, object_name FROM all_objects "
                          "WHERE object_type = '%s'" % object_type,
                          "ORDER BY owner, object_name")):
            o = {"schema": _at(r, 0), "name": _at(r, 1), "language": "PL/SQL"}
            body = "".join(bodies.get((o["schema"], o["name"], t), "")
                           for t in source_types)
            objs.append(_with_body(o, body, "%s %s.%s"
                                   % (label, o["schema"], o["name"])))
        return objs

    out["functions"] = plsql("FUNCTION", "functions", "function",
                             ("FUNCTION",))
    out["procedures"] = plsql("PROCEDURE", "procedures", "procedure",
                              ("PROCEDURE",))
    # A package's logic lives in its BODY, but the spec is what declares the
    # callable surface — a conversion needs both, so both are carried.
    out["packages"] = plsql("PACKAGE", "packages", "package",
                            ("PACKAGE", "PACKAGE BODY"))

    # ALL_SEQUENCES is the one catalog view here that does not call its owner
    # column `owner`, so it needs its own scoping predicate.
    def _sequences():
        cur.execute(_ora_top(
            "SELECT sequence_owner, sequence_name, min_value, increment_by "
            "FROM all_sequences WHERE 1 = 1 %s "
            "ORDER BY sequence_owner, sequence_name" % seq_where,
            _MAX_OBJECTS), seq_binds)
        return cur.fetchall()

    out["sequences"] = [
        {"schema": _at(r, 0), "name": _at(r, 1), "start": _at(r, 2),
         "increment": _at(r, 3)}
        for r in g("sequences", _sequences)]

    out["materialized_views"] = sorted(
        (_with_body({"schema": _at(r, 0), "name": _at(r, 1)}, _at(r, 2),
                    "materialized view %s.%s" % (_at(r, 0), _at(r, 1)))
         for r in g("materialized_views", lambda: _oracle_long_rows(
             cur, "SELECT owner, mview_name, query FROM all_mviews "
                  "WHERE 1 = 1", where, binds))),
        key=lambda o: (o["schema"], o["name"]))

    # Oracle's answer to a Snowflake stream + task: row-level logic that fires
    # on DML. It carries a body, so it carries the same credential risk.
    out["triggers"] = sorted(
        (_with_body({"schema": _at(r, 0), "name": _at(r, 1),
                     "table": _at(r, 2), "type": _at(r, 3),
                     "event": _at(r, 4)}, _at(r, 5),
                    "trigger %s.%s" % (_at(r, 0), _at(r, 1)))
         for r in g("triggers", lambda: _oracle_long_rows(
             cur, "SELECT owner, trigger_name, table_name, trigger_type, "
                  "triggering_event, trigger_body FROM all_triggers "
                  "WHERE 1 = 1", where, binds))),
        key=lambda o: (o["schema"], o["name"]))

    # A synonym is how an Oracle estate hides its real object names; a
    # conversion that ignores them rewrites references that do not resolve.
    #
    # PUBLIC is re-admitted here because a public synonym is the classic way a
    # legacy estate exposes one schema to another — dropping them all would
    # hide real migration work. What IS dropped is the several thousand public
    # synonyms Oracle ships pointing at its own dictionary: those are
    # identified by their TARGET owner, not by being public.
    syn_where, syn_binds = _ora_owner_pred("owner", schema, allow_public=True)
    out["synonyms"] = [
        {"schema": _at(r, 0), "name": _at(r, 1),
         "target": "%s.%s" % (_at(r, 2), _at(r, 3)) if _at(r, 2)
         else _at(r, 3), "db_link": _at(r, 4)}
        for r in g("synonyms", lambda: (
            cur.execute(_ora_top(
                "SELECT owner, synonym_name, table_owner, table_name, db_link "
                "FROM all_synonyms "
                # a synonym resolved through a db_link has no local
                # table_owner, and `NULL NOT IN (...)` is NULL — which would
                # drop precisely the remote ones worth knowing about
                "WHERE (table_owner IS NULL OR table_owner NOT IN (%s)) %s "
                "ORDER BY owner, synonym_name"
                % (", ".join("'%s'" % s for s in _ORACLE_SYSTEM_SCHEMAS),
                   syn_where), _MAX_OBJECTS), syn_binds),
            cur.fetchall())[1])]

    # Database links are the estate's outbound edges — every one is a system
    # this migration also has to account for. The password is not exposed by
    # the catalog and is never sought here. PUBLIC is re-admitted for the same
    # reason as synonyms, and needs no target filter: Oracle ships no public
    # database links, so every one of them is the user's own.
    dbl_where, dbl_binds = _ora_owner_pred("owner", schema, allow_public=True)
    out["db_links"] = [
        {"schema": _at(r, 0), "name": _at(r, 1), "host": _at(r, 2),
         "user": _at(r, 3)}
        for r in g("db_links", lambda: (
            cur.execute(_ora_top(
                "SELECT owner, db_link, host, username FROM all_db_links "
                "WHERE 1 = 1 %s ORDER BY owner, db_link" % dbl_where,
                _MAX_OBJECTS), dbl_binds),
            cur.fetchall())[1])]

    # Advanced Queuing, reported as the QUEUE it is rather than as the dozen
    # AQ$ tables Oracle builds underneath it (those are filtered out of the
    # table list). A queue is real migration work — it becomes a stream+task
    # pair or an external broker — and it is invisible if only tables are
    # listed. AQ$_-prefixed queues are Oracle's own exception queues.
    out["queues"] = [
        {"schema": _at(r, 0), "name": _at(r, 1), "table": _at(r, 2),
         "type": _at(r, 3)}
        for r in g("queues", q(
            "SELECT owner, name, queue_table, queue_type FROM all_queues "
            "WHERE name NOT LIKE 'AQ$%'", "ORDER BY owner, name"))]

    out["scheduler_jobs"] = [
        _with_body({"schema": _at(r, 0), "name": _at(r, 1),
                    "schedule": _at(r, 3) or _at(r, 2), "state": _at(r, 4)},
                   _at(r, 5), "scheduler job %s.%s" % (_at(r, 0), _at(r, 1)))
        for r in g("scheduler_jobs", q(
            "SELECT owner, job_name, schedule_type, repeat_interval, state, "
            "job_action FROM all_scheduler_jobs WHERE 1 = 1",
            "ORDER BY owner, job_name"))]

    out["grants"] = [
        {"role": _at(r, 0), "privilege": _at(r, 3), "granted_on": "TABLE",
         "object": "%s.%s" % (_at(r, 1), _at(r, 2))}
        for r in g("grants", q(
            "SELECT grantee, owner, table_name, privilege FROM all_tab_privs "
            "WHERE 1 = 1", "ORDER BY grantee, owner, table_name"))]
    return out


def _oracle_introspect(params: Dict[str, str],
                       max_tables: int = 500) -> dict:
    """Read-only inventory for Oracle, returning the SAME shape as the
    Snowflake/SQL introspect (context + capabilities + the object classes +
    readiness + manifest) so the console and scaffold consume it unchanged."""
    params = _normalize_sql_params(params)
    database = (params.get("database") or "").strip()
    # Oracle folds unquoted identifiers to upper case and the ALL_* views
    # store them that way, so a schema typed in any case still matches.
    schema = (params.get("schema") or "").strip().upper()
    started = time.time()
    try:
        conn = _oracle_connect(params)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        return {"ok": False, "connector": "oracle",
                "error": (msg + _oracle_hint(msg))[:400]}
    caps: Dict[str, dict] = {}
    measured = 0
    try:
        cur = conn.cursor()
        context = _oracle_context(cur)
        where, binds = _ora_owner_pred("owner", schema)

        # NESTED and IOT-overflow tables are storage for another table rather
        # than estate objects, and `_ora_not_internal` drops the ones only
        # their NAME identifies (AQ plumbing, MV logs, recycle-bin entries).
        # Both are concatenated, not %-formatted: the LIKE patterns carry %.
        not_internal = _ora_not_internal("table_name")
        cur.execute(_ora_top(
            "SELECT owner, table_name, num_rows FROM all_tables "
            "WHERE nested = 'NO' "
            "AND (iot_type IS NULL OR iot_type <> 'IOT_OVERFLOW') "
            + not_internal + " " + where +
            " ORDER BY owner, table_name", max_tables), binds)
        tables = {(r[0], r[1]): {"schema": r[0], "name": r[1],
                                 "type": "BASE TABLE",
                                 "rows": max(0, int(r[2] or 0)),
                                 "bytes": 0, "columns": []}
                  for r in cur.fetchall()}

        # Columns are filtered the same way. A row for a table we did not list
        # would be dropped on lookup anyway, but AQ tables are wide and there
        # is no reason to drag their columns across the wire to discard them.
        col_where = where + " " + not_internal

        def _run(sql):
            cur.execute(sql % col_where, binds)
            return cur.fetchall()

        for r in _fetch_columns(
                _run,
                "SELECT owner, table_name, column_name, data_type, "
                "char_length, data_precision, data_scale "
                "FROM all_tab_columns WHERE 1 = 1 %s "
                "ORDER BY owner, table_name, column_id",
                "SELECT owner, table_name, column_name, data_type "
                "FROM all_tab_columns WHERE 1 = 1 %s "
                "ORDER BY owner, table_name, column_id"):
            key_ = (r[0], r[1])
            if key_ in tables:
                tables[key_]["columns"].append({"name": str(r[2]),
                                                "type": r[3]})

        # Size on disk comes from the segment, not the table — and a role that
        # may read ALL_TABLES is often not granted ALL_SEGMENTS, so a missing
        # size leaves bytes at 0 rather than costing the inventory.
        try:
            seg_where, seg_binds = _ora_owner_pred("owner", schema)
            cur.execute("SELECT owner, segment_name, bytes FROM all_segments "
                        "WHERE segment_type = 'TABLE' " + seg_where,
                        seg_binds)
            for r in cur.fetchall():
                key_ = (r[0], r[1])
                if key_ in tables and r[2] is not None:
                    tables[key_]["bytes"] = max(0, int(r[2]))
        except Exception:  # noqa: BLE001 — sizes are optional
            pass

        def _run_measure(sql):
            cur.execute(sql)
            return cur.fetchall()
        try:
            measured = _measure_column_sizes(_run_measure, tables)
        except Exception:  # noqa: BLE001
            measured = 0

        # ALL_VIEWS.TEXT is a LONG. TEXT_VC (12.2+) is the same SQL as a
        # VARCHAR2, which is both cheaper to fetch and legal in an inline
        # view — so try it first and fall back on older releases.
        views = []
        try:
            rows = _oracle_long_rows(
                cur, "SELECT owner, view_name, text_vc FROM all_views "
                     "WHERE 1 = 1", where, binds)
        except Exception:  # noqa: BLE001 — pre-12.2 has no TEXT_VC
            rows = _oracle_long_rows(
                cur, "SELECT owner, view_name, text FROM all_views "
                     "WHERE 1 = 1", where, binds)
        for r in sorted(rows, key=lambda x: (str(x[0]), str(x[1]))):
            views.append({"schema": r[0], "name": r[1],
                          "definition": str(r[2] or "")[:8000]})

        caps["tables"] = {"status": "available" if tables else "empty"}
        caps["views"] = {"status": "available" if views else "empty"}
        primary_keys = _oracle_primary_keys(cur, schema)
        objects = _oracle_objects(cur, caps, schema)
    except Exception as e:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "connector": "oracle", "error": str(e)[:400]}
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
            sqlglot.parse_one(v["definition"], read="oracle")
            convertible.append(v["name"])
        except Exception as e:  # noqa: BLE001
            needs_review.append({"view": v["name"], "reason": str(e)[:150]})

    base_tables = list(tables.values())
    secret_findings = [f for cls in ("functions", "procedures", "packages",
                                     "triggers", "materialized_views",
                                     "scheduler_jobs")
                       for o in objects.get(cls, [])
                       for f in o.get("secret_findings", [])]
    readiness = {
        "tables": len(base_tables),
        "views": len(views),
        "total_rows": sum(t["rows"] for t in base_tables),
        "tables_with_columns": sum(1 for t in base_tables if t["columns"]),
        "column_sizes_measured": measured,
        # Oracle enforces primary keys, so a table WITH one can be loaded by
        # MERGE; one without still falls through to a full reload.
        "tables_with_primary_key": sum(
            1 for t in base_tables
            if primary_keys.get((t["schema"], t["name"]))),
        "views_convertible": len(convertible),
        "views_needing_review": needs_review,
        "verdict": "READY" if base_tables or convertible else
                   "NOTHING_TO_CONVERT",
    }
    readiness.update({k: len(v) for k, v in objects.items()})
    return {
        "ok": True, "connector": "oracle",
        "database": context.get("database") or database,
        "schema": schema or "(all)",
        "elapsed_ms": int((time.time() - started) * 1000),
        "context": context, "capabilities": caps,
        "recommendations": _context_recommendations(caps, context, "oracle"),
        "tables": sorted(tables.values(),
                         key=lambda t: (-t["rows"], t["name"])),
        "views": [{"schema": v["schema"], "name": v["name"]}
                  for v in views],
        "view_definitions": {v["name"]: v["definition"] for v in views},
        "secret_findings": secret_findings,
        "readiness": readiness,
        "manifest_yaml": _manifest_yaml(base_tables, database, primary_keys),
        **objects,
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
    if key == "oracle":
        return _oracle_test(params)
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


# Object classes the account EDITION gates — used to infer the edition.
_EDITION_GATED = ("masking_policies", "row_access_policies", "tags")


def _snowflake_context(cur) -> dict:
    """CURRENT_* session context plus the roles this user may assume. The
    assumable roles are what makes a "re-connect with a higher role"
    recommendation actionable rather than a guess."""
    ctx = {"user": "", "current_role": "", "warehouse": "", "database": "",
           "schema": "", "region": "", "account": "", "available_roles": [],
           "edition": "unknown"}
    try:
        row = cur.execute(
            "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), "
            "CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_REGION(), "
            "CURRENT_ACCOUNT()").fetchone() or ()
        for i, k in enumerate(("user", "current_role", "warehouse",
                               "database", "schema", "region", "account")):
            ctx[k] = _at(row, i)
    except Exception:  # noqa: BLE001
        pass
    try:
        row = cur.execute("SELECT CURRENT_AVAILABLE_ROLES()").fetchone()
        ctx["available_roles"] = sorted(json.loads(row[0])) \
            if row and row[0] else []
    except Exception:  # noqa: BLE001
        ctx["available_roles"] = []
    return ctx


def _edition_from_caps(caps: dict) -> str:
    """An Enterprise-only class that RESOLVES — even to zero rows — means the
    account has that edition; one that is edition-gated means it does not."""
    statuses = [caps.get(k, {}).get("status") for k in _EDITION_GATED]
    if any(s in ("available", "empty") for s in statuses):
        return "Enterprise (or higher)"
    if any(s == "not_applicable" for s in statuses):
        return "Standard"
    return "unknown"


# The role that unblocks a catalog read, per platform: the roles a session
# may already be able to ASSUME, and the one to ask a DBA for otherwise.
# Naming Snowflake's SECURITYADMIN to an Oracle DBA would be advice they
# cannot act on, which is worse than saying nothing.
_ELEVATED_ROLES = {
    "snowflake": (("ACCOUNTADMIN", "SECURITYADMIN"), "SECURITYADMIN"),
    "oracle": (("DBA", "SELECT_CATALOG_ROLE"), "SELECT_CATALOG_ROLE"),
}


def _context_recommendations(caps: dict, ctx: dict,
                             key: str = "snowflake") -> List[str]:
    """Turn blocked classes into the action that would unblock them."""
    recs: List[str] = []
    priv = sorted(k for k, v in caps.items()
                  if v.get("status") == "blocked_privilege")
    if priv:
        assumable, ask_for = _ELEVATED_ROLES.get(
            key, _ELEVATED_ROLES["snowflake"])
        elevated = sorted(set(assumable)
                          & set(ctx.get("available_roles") or []))
        if elevated:
            recs.append("Re-connect with a higher role (%s) to inventory "
                        "%s — set the role before connecting."
                        % (", ".join(elevated), ", ".join(priv)))
        else:
            recs.append("Grant the connection role %s (or the "
                        "specific privileges) to inventory %s."
                        % (ask_for, ", ".join(priv)))
    gated = sorted(k for k, v in caps.items()
                   if v.get("status") == "not_applicable"
                   and k in _EDITION_GATED)
    if gated:
        recs.append("%s require Enterprise Edition and are not available on "
                    "this account." % ", ".join(gated))
    return recs


def _list_databases_snowflake(params: Dict[str, str]) -> dict:
    """The databases this role can see, for the Data Estate picker. Unlike
    Postgres, one Snowflake session can read across databases, so drilling in
    only re-scopes the queries."""
    started = time.time()
    try:
        conn = _snowflake_connect(params)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "connector": "snowflake", "error": str(e)[:400]}
    context, dbs = {}, []
    try:
        cur = conn.cursor()
        context = _snowflake_context(cur)
        for r in _show(cur, "SHOW DATABASES"):
            dbs.append({"name": _named(cur, r, "name") or _at(r, 1),
                        "kind": _named(cur, r, "kind") or _at(r, 9),
                        "owner": _named(cur, r, "owner") or _at(r, 5)})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "connector": "snowflake", "error": str(e)[:400]}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "connector": "snowflake", "mode": "databases",
            "elapsed_ms": int((time.time() - started) * 1000),
            "context": context, "databases": dbs}


def _snowflake_objects(cur, caps: dict, database: str,
                       schema: str) -> Dict[str, list]:
    """Every object class beyond tables/views/columns.

    Each class is fetched independently through `_guarded`, so a class the
    role may not read or the edition does not carry comes back empty WITH the
    reason, and never costs the classes around it. A platform that lacks a
    class simply contributes no rows — the console renders whatever arrives.
    """
    sc = _scope(database, schema)
    inf_pred = (lambda col: ("AND %s = %%s" % col) if schema
                else ("AND %s <> 'INFORMATION_SCHEMA'" % col))
    args = (schema,) if schema else ()

    def info(sql_cols, table, col_prefix, order):
        def go():
            return cur.execute(
                "SELECT %s FROM INFORMATION_SCHEMA.%s "
                "WHERE %s_catalog = CURRENT_DATABASE() %s ORDER BY %s "
                "LIMIT %d" % (sql_cols, table, col_prefix,
                              inf_pred("%s_schema" % col_prefix), order,
                              _MAX_OBJECTS), args).fetchall()
        return go

    def shown(sql):
        return lambda: _show(cur, sql + sc)

    out: Dict[str, list] = {}
    g = lambda name, fn: _guarded(caps, name, fn,      # noqa: E731
                                  cap=_MAX_OBJECTS)

    out["sequences"] = [
        {"schema": _at(r, 0), "name": _at(r, 1), "start": _at(r, 2),
         "increment": _at(r, 3)}
        for r in g("sequences", info(
            "sequence_schema, sequence_name, start_value, increment",
            "SEQUENCES", "sequence", "sequence_schema, sequence_name"))]

    out["file_formats"] = [
        {"schema": _at(r, 0), "name": _at(r, 1), "type": _at(r, 2)}
        for r in g("file_formats", info(
            "file_format_schema, file_format_name, file_format_type",
            "FILE_FORMATS", "file_format",
            "file_format_schema, file_format_name"))]

    def logic(rows, kind):
        out_ = []
        for r in rows:
            o = {"schema": _at(r, 0), "name": _at(r, 1),
                 "signature": _at(r, 2), "returns": _at(r, 3),
                 "language": _at(r, 4)}
            out_.append(_with_body(o, _at(r, 5),
                                   "%s %s.%s" % (kind, o["schema"],
                                                 o["name"])))
        return out_

    out["functions"] = logic(g("functions", info(
        "function_schema, function_name, argument_signature, data_type, "
        "function_language, function_definition", "FUNCTIONS", "function",
        "function_schema, function_name")), "function")
    out["procedures"] = logic(g("procedures", info(
        "procedure_schema, procedure_name, argument_signature, data_type, "
        "procedure_language, procedure_definition", "PROCEDURES",
        "procedure", "procedure_schema, procedure_name")), "procedure")

    out["materialized_views"] = [
        _with_body({"schema": _at(r, 4), "name": _at(r, 1)},
                   _named(cur, r, "text"),
                   "materialized view %s" % _at(r, 1))
        for r in g("materialized_views", shown("SHOW MATERIALIZED VIEWS"))]

    out["dynamic_tables"] = [
        _with_body({"schema": _at(r, 4), "name": _at(r, 1),
                    "target_lag": _named(cur, r, "target_lag") or _at(r, 9),
                    "warehouse": _named(cur, r, "warehouse"),
                    "refresh_mode": _named(cur, r, "refresh_mode")},
                   _named(cur, r, "text"), "dynamic table %s" % _at(r, 1))
        for r in g("dynamic_tables", shown("SHOW DYNAMIC TABLES"))]

    out["streams"] = [
        {"schema": _at(r, 3), "name": _at(r, 1), "source": _at(r, 6),
         "type": _at(r, 9), "stale": _at(r, 10)}
        for r in g("streams", shown("SHOW STREAMS"))]

    out["tasks"] = [
        _with_body({"schema": _at(r, 4), "name": _at(r, 1),
                    "warehouse": _at(r, 7), "schedule": _at(r, 8),
                    "state": _at(r, 10),
                    "predecessors": _parse_predecessors(
                        r[9] if len(r) > 9 else None)},
                   _at(r, 11), "task %s" % _at(r, 1))
        for r in g("tasks", shown("SHOW TASKS"))]

    out["pipes"] = [
        _with_body({"schema": _at(r, 3), "name": _at(r, 1),
                    "notification_channel": _at(r, 6)},
                   _at(r, 4), "pipe %s" % _at(r, 1))
        for r in g("pipes", shown("SHOW PIPES"))]

    out["stages"] = [
        {"schema": _at(r, 3), "name": _at(r, 1), "url": _at(r, 4),
         "type": _at(r, 10)}
        for r in g("stages", shown("SHOW STAGES"))]

    for name, sql in (("masking_policies", "SHOW MASKING POLICIES"),
                      ("row_access_policies", "SHOW ROW ACCESS POLICIES")):
        out[name] = [{"schema": _at(r, 3), "name": _at(r, 1),
                      "kind": _at(r, 4)}
                     for r in g(name, shown(sql))]

    out["tags"] = [{"schema": _at(r, 3), "name": _at(r, 1),
                    "allowed_values": _at(r, 6)}
                   for r in g("tags", shown("SHOW TAGS"))]

    # account-level classes: never schema-scoped, and privilege-gated
    out["roles"] = [{"name": _at(r, 1), "assigned_to_users": _at(r, 5),
                     "granted_to_roles": _at(r, 6), "comment": _at(r, 9)}
                    for r in g("roles", lambda: _show(cur, "SHOW ROLES"))]
    out["shares"] = [{"kind": _at(r, 1), "name": _at(r, 2),
                      "database": _at(r, 3)}
                     for r in g("shares", lambda: _show(cur, "SHOW SHARES"))]

    # Grants ON this database's containers — "who can reach what in here",
    # which is the access control we re-create on the target. Container-level
    # today; per-table grants would need a SHOW GRANTS per object.
    def _grants():
        rows = []
        if database:
            db = database.replace('"', "")
            targets = ['DATABASE "%s"' % db]
            if schema:
                targets.append('SCHEMA "%s"."%s"'
                               % (db, schema.replace('"', "")))
            for tgt in targets:
                rows.extend(_show(cur, "SHOW GRANTS ON " + tgt))
        return rows

    out["grants"] = [{"role": _at(r, 5), "privilege": _at(r, 1),
                      "granted_on": _at(r, 2), "object": _at(r, 3)}
                     for r in g("grants", _grants)]
    return out


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
    if key == "oracle":
        return _oracle_introspect(params, max_tables=max_tables)
    database = params.get("database", "")
    schema = (params.get("schema") or "").upper()
    if not database:
        # nothing to inventory yet — offer the databases instead of failing
        return _list_databases_snowflake(params)
    started = time.time()
    try:
        conn = _snowflake_connect(params)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "connector": key, "error": str(e)[:400]}
    caps: Dict[str, dict] = {}
    try:
        cur = conn.cursor()
        context = _snowflake_context(cur)
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
        caps["tables"] = {"status": "available" if tables else "empty"}
        caps["views"] = {"status": "available" if views else "empty"}
        primary_keys = _snowflake_primary_keys(cur, database, schema)
        objects = _snowflake_objects(cur, caps, database, schema)
        context["edition"] = _edition_from_caps(caps)
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
    # every object that carries a body may have carried a credential with it
    secret_findings = [f for cls in ("functions", "procedures", "tasks",
                                     "pipes", "materialized_views",
                                     "dynamic_tables")
                       for o in objects.get(cls, [])
                       for f in o.get("secret_findings", [])]
    readiness = {
        "tables": len(base_tables),
        "views": len(views),
        "total_rows": sum(t["rows"] for t in base_tables),
        "tables_with_columns": sum(1 for t in base_tables if t["columns"]),
        "column_sizes_measured": measured,
        # load behaviour: tables with no declared PK fall back to FULL reload
        "tables_with_primary_key": sum(
            1 for t in base_tables
            if primary_keys.get((t["schema"], t["name"]))),
        "views_convertible": len(convertible),
        "views_needing_review": needs_review,
        "verdict": "READY" if base_tables or convertible else
                   "NOTHING_TO_CONVERT",
    }
    readiness.update({k: len(v) for k, v in objects.items()})
    return {
        "ok": True, "connector": key,
        "database": database, "schema": schema or "(all)",
        "elapsed_ms": int((time.time() - started) * 1000),
        "context": context, "capabilities": caps,
        "recommendations": _context_recommendations(caps, context),
        "tables": sorted(tables.values(),
                         key=lambda t: (-t["rows"], t["name"])),
        "views": [{"schema": v["schema"], "name": v["name"]}
                  for v in views],
        "view_definitions": {v["name"]: v["definition"] for v in views},
        "task_dag": _build_task_dag(objects.get("tasks", [])),
        "secret_findings": secret_findings,
        "readiness": readiness,
        "manifest_yaml": _manifest_yaml(base_tables, database, primary_keys),
        **objects,
    }

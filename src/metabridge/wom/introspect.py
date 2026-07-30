"""Live object enumeration — read-only, best-effort per category.

Design rule: a category the connected role cannot see is RECORDED as
unreadable with the driver's reason, and the sweep continues. Losing one
category is a gap in the report; losing the whole inventory because SHOW
TAGS needs a privilege the role lacks would be an outage.

Everything here is SELECTs against catalogs plus SHOW commands. Nothing is
written, nothing user-defined is executed.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from .model import DbObject, ObjectInventory

# stop a pathological account from turning the report into a memory problem
_MAX_PER_CATEGORY = 2000
_MAX_GRANTS = 5000


class _Sweep:
    """Shared bookkeeping for one enumeration pass."""

    def __init__(self, inv: ObjectInventory) -> None:
        self.inv = inv

    def add(self, obj: DbObject) -> None:
        self.inv.objects.append(obj)

    def category(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — degrade per category
            self.inv.unreadable.append(
                {"category": name, "reason": str(e)[:220]})


def _dictrows(cur) -> List[dict]:
    """Rows as name->value dicts. SHOW commands return position-unstable
    columns across versions, so mapping by cursor description is the only
    safe way to read them."""
    cols = [str(d[0]).lower() for d in (cur.description or [])]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _s(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v is not None and str(v) != "":
            return str(v)
    return ""


# ===========================================================================
# Snowflake
# ===========================================================================

def _snowflake_objects(params: Dict[str, str], inv: ObjectInventory) -> None:
    from ..livecheck import _snowflake_connect
    conn = _snowflake_connect(params)
    sw = _Sweep(inv)
    try:
        cur = conn.cursor()
        db = params.get("database", "")
        schema = (params.get("schema") or "").upper()
        pred = "AND table_schema = '%s'" % schema if schema \
            else "AND table_schema <> 'INFORMATION_SCHEMA'"

        def q(sql: str) -> List[dict]:
            cur.execute(sql)
            return _dictrows(cur)[:_MAX_PER_CATEGORY]

        def add_info(kind: str, sql: str, schema_col: str, name_col: str,
                     defn_col: str = "", lang_col: str = "",
                     props: Optional[Dict[str, str]] = None) -> Callable:
            def run() -> None:
                for r in q(sql):
                    o = DbObject(kind=kind, name=_s(r, name_col),
                                 schema=_s(r, schema_col), database=db,
                                 definition=_s(r, defn_col) if defn_col
                                 else "",
                                 language=_s(r, lang_col).lower()
                                 if lang_col else "")
                    for pk, col in (props or {}).items():
                        v = _s(r, col)
                        if v:
                            o.properties[pk] = v
                    sw.add(o)
            return run

        sw.category("tables", add_info(
            "table",
            "SELECT table_schema, table_name, row_count "
            "FROM INFORMATION_SCHEMA.TABLES "
            "WHERE table_catalog = CURRENT_DATABASE() "
            "AND table_type = 'BASE TABLE' %s" % pred,
            "table_schema", "table_name", props={"rows": "row_count"}))
        sw.category("views", add_info(
            "view",
            "SELECT table_schema, table_name, view_definition, is_secure "
            "FROM INFORMATION_SCHEMA.VIEWS "
            "WHERE table_catalog = CURRENT_DATABASE() %s" % pred,
            "table_schema", "table_name", "view_definition",
            props={"secure": "is_secure"}))
        sw.category("procedures", add_info(
            "procedure",
            "SELECT procedure_schema, procedure_name, procedure_language, "
            "procedure_definition, argument_signature "
            "FROM INFORMATION_SCHEMA.PROCEDURES "
            "WHERE procedure_catalog = CURRENT_DATABASE()"
            + (" AND procedure_schema = '%s'" % schema if schema else ""),
            "procedure_schema", "procedure_name", "procedure_definition",
            "procedure_language", props={"signature": "argument_signature"}))
        sw.category("functions", add_info(
            "function",
            "SELECT function_schema, function_name, function_language, "
            "function_definition, argument_signature, is_external "
            "FROM INFORMATION_SCHEMA.FUNCTIONS "
            "WHERE function_catalog = CURRENT_DATABASE()"
            + (" AND function_schema = '%s'" % schema if schema else ""),
            "function_schema", "function_name", "function_definition",
            "function_language",
            props={"signature": "argument_signature",
                   "external": "is_external"}))
        sw.category("sequences", add_info(
            "sequence",
            "SELECT sequence_schema, sequence_name, start_value, "
            "\"INCREMENT\" FROM INFORMATION_SCHEMA.SEQUENCES "
            "WHERE sequence_catalog = CURRENT_DATABASE()"
            + (" AND sequence_schema = '%s'" % schema if schema else ""),
            "sequence_schema", "sequence_name",
            props={"start": "start_value", "increment": "increment"}))
        sw.category("external tables", add_info(
            "external_table",
            "SELECT table_schema, table_name, location, file_format_type "
            "FROM INFORMATION_SCHEMA.EXTERNAL_TABLES "
            "WHERE table_catalog = CURRENT_DATABASE() %s" % pred,
            "table_schema", "table_name",
            props={"location": "location", "format": "file_format_type"}))
        sw.category("stages", add_info(
            "stage",
            "SELECT stage_schema, stage_name, stage_url, stage_type "
            "FROM INFORMATION_SCHEMA.STAGES "
            "WHERE stage_catalog = CURRENT_DATABASE()"
            + (" AND stage_schema = '%s'" % schema if schema else ""),
            "stage_schema", "stage_name",
            props={"url": "stage_url", "type": "stage_type"}))
        sw.category("file formats", add_info(
            "file_format",
            "SELECT file_format_schema, file_format_name, file_format_type "
            "FROM INFORMATION_SCHEMA.FILE_FORMATS "
            "WHERE file_format_catalog = CURRENT_DATABASE()"
            + (" AND file_format_schema = '%s'" % schema if schema else ""),
            "file_format_schema", "file_format_name",
            props={"type": "file_format_type"}))
        sw.category("pipes", add_info(
            "pipe",
            "SELECT pipe_schema, pipe_name, definition "
            "FROM INFORMATION_SCHEMA.PIPES "
            "WHERE pipe_catalog = CURRENT_DATABASE()"
            + (" AND pipe_schema = '%s'" % schema if schema else ""),
            "pipe_schema", "pipe_name", "definition"))

        # -- SHOW-based categories (no INFORMATION_SCHEMA view exists) ------
        scope = "IN SCHEMA %s" % schema if schema else "IN DATABASE"

        def add_show(kind: str, show_sql: str,
                     defn_cols: tuple = (),
                     props: Optional[Dict[str, tuple]] = None) -> Callable:
            def run() -> None:
                for r in q(show_sql):
                    o = DbObject(kind=kind, name=_s(r, "name"),
                                 schema=_s(r, "schema_name", "schema"),
                                 database=db)
                    if defn_cols:
                        o.definition = _s(r, *defn_cols)
                    for pk, cols in (props or {}).items():
                        v = _s(r, *cols)
                        if v:
                            o.properties[pk] = v
                    if o.name:
                        sw.add(o)
            return run

        sw.category("materialized views", add_show(
            "materialized_view", "SHOW MATERIALIZED VIEWS %s" % scope,
            defn_cols=("text",)))
        sw.category("dynamic tables", add_show(
            "dynamic_table", "SHOW DYNAMIC TABLES %s" % scope,
            defn_cols=("text",),
            props={"target_lag": ("target_lag",),
                   "warehouse": ("warehouse",)}))
        sw.category("tasks", add_show(
            "task", "SHOW TASKS %s" % scope, defn_cols=("definition",),
            props={"schedule": ("schedule",), "state": ("state",),
                   "predecessors": ("predecessors",),
                   "warehouse": ("warehouse",)}))
        sw.category("streams", add_show(
            "stream", "SHOW STREAMS %s" % scope,
            props={"source": ("table_name",), "mode": ("mode",),
                   "stale": ("stale",)}))
        sw.category("masking policies", add_show(
            "masking_policy", "SHOW MASKING POLICIES %s" % scope))
        sw.category("row access policies", add_show(
            "row_access_policy", "SHOW ROW ACCESS POLICIES %s" % scope))
        sw.category("tags", add_show("tag", "SHOW TAGS %s" % scope))

        def keys(kind_label: str, show: str, ctype: str) -> None:
            rows = q(show)
            by_constraint: Dict[tuple, dict] = {}
            for r in rows:
                tbl = _s(r, "table_name", "fk_table_name")
                sch = _s(r, "schema_name", "fk_schema_name")
                cname = _s(r, "constraint_name", "fk_name") or \
                    "%s_%s" % (ctype.lower(), tbl)
                key = (sch, tbl, cname)
                e = by_constraint.setdefault(
                    key, {"columns": [], "ref_table": _s(r, "pk_table_name"),
                          "ref_columns": []})
                col = _s(r, "column_name", "fk_column_name")
                if col:
                    e["columns"].append(col)
                ref = _s(r, "pk_column_name")
                if ref:
                    e["ref_columns"].append(ref)
            for (sch, tbl, cname), e in by_constraint.items():
                sw.add(DbObject(
                    kind="constraint", name=cname, schema=sch, database=db,
                    properties={"constraint_type": ctype, "table": tbl,
                                "columns": e["columns"],
                                **({"ref_table": e["ref_table"],
                                    "ref_columns": e["ref_columns"]}
                                   if ctype == "FOREIGN KEY" else {})}))

        sw.category("primary keys",
                    lambda: keys("pk", "SHOW PRIMARY KEYS %s" % scope,
                                 "PRIMARY KEY"))
        sw.category("unique keys",
                    lambda: keys("uk", "SHOW UNIQUE KEYS %s" % scope,
                                 "UNIQUE"))
        sw.category("foreign keys",
                    lambda: keys("fk", "SHOW IMPORTED KEYS %s" % scope,
                                 "FOREIGN KEY"))

        def comments() -> None:
            for r in q("SELECT table_schema, table_name, comment "
                       "FROM INFORMATION_SCHEMA.TABLES "
                       "WHERE table_catalog = CURRENT_DATABASE() %s "
                       "AND comment IS NOT NULL AND comment <> ''" % pred):
                sw.add(DbObject(kind="comment", name=_s(r, "table_name"),
                                schema=_s(r, "table_schema"), database=db,
                                definition=_s(r, "comment"),
                                properties={"on": "table"}))
            for r in q("SELECT table_schema, table_name, column_name, "
                       "comment FROM INFORMATION_SCHEMA.COLUMNS "
                       "WHERE table_catalog = CURRENT_DATABASE() %s "
                       "AND comment IS NOT NULL AND comment <> ''" % pred):
                sw.add(DbObject(
                    kind="comment",
                    name="%s.%s" % (_s(r, "table_name"),
                                    _s(r, "column_name")),
                    schema=_s(r, "table_schema"), database=db,
                    definition=_s(r, "comment"),
                    properties={"on": "column"}))
        sw.category("comments", comments)

        def grants() -> None:
            cur.execute(
                "SELECT grantee, privilege_type, object_type, "
                "object_schema, object_name "
                "FROM INFORMATION_SCHEMA.OBJECT_PRIVILEGES "
                "WHERE object_catalog = CURRENT_DATABASE()"
                + (" AND object_schema = '%s'" % schema if schema else "")
                + " LIMIT %d" % _MAX_GRANTS)
            for r in _dictrows(cur):
                sw.add(DbObject(
                    kind="grant", name=_s(r, "object_name"),
                    schema=_s(r, "object_schema"), database=db,
                    properties={"grantee": _s(r, "grantee"),
                                "privilege": _s(r, "privilege_type"),
                                "on_type": _s(r, "object_type")}))
        sw.category("grants", grants)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# PostgreSQL / Amazon Redshift (one wire protocol, one path)
# ===========================================================================

_PG_SYSTEM = "('pg_catalog', 'information_schema', 'pg_internal')"


def _pg_objects(key: str, params: Dict[str, str],
                inv: ObjectInventory) -> None:
    from ..livecheck import _psycopg_connect
    conn = _psycopg_connect(key, params)
    sw = _Sweep(inv)
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_database()")
        db = str(cur.fetchone()[0])
        inv.database = inv.database or db
        schema = (params.get("schema") or "").strip()

        def q(sql: str, args: tuple = ()) -> List[dict]:
            try:
                cur.execute(sql, args)
                return _dictrows(cur)[:_MAX_PER_CATEGORY]
            except Exception:
                conn.rollback()          # do not poison later categories
                raise

        if schema:
            vpred, vargs = "AND table_schema = %s", (schema,)
            npred, nargs = "AND n.nspname = %s", (schema,)
        else:
            vpred, vargs = "AND table_schema NOT IN %s" % _PG_SYSTEM, ()
            npred, nargs = "AND n.nspname NOT IN %s" % _PG_SYSTEM, ()

        def tables() -> None:
            for r in q("SELECT table_schema, table_name "
                       "FROM information_schema.tables "
                       "WHERE table_catalog = current_database() "
                       "AND table_type = 'BASE TABLE' %s" % vpred, vargs):
                sw.add(DbObject(kind="table", name=_s(r, "table_name"),
                                schema=_s(r, "table_schema"), database=db))
        sw.category("tables", tables)

        def views() -> None:
            for r in q("SELECT table_schema, table_name, view_definition "
                       "FROM information_schema.views "
                       "WHERE table_catalog = current_database() %s"
                       % vpred, vargs):
                sw.add(DbObject(kind="view", name=_s(r, "table_name"),
                                schema=_s(r, "table_schema"), database=db,
                                definition=_s(r, "view_definition")))
        sw.category("views", views)

        def matviews() -> None:
            # Postgres catalogs materialized views separately; Redshift has
            # STV_MV_INFO. Try both shapes, keep whichever answers.
            try:
                rows = q("SELECT schemaname, matviewname, definition "
                         "FROM pg_matviews"
                         + (" WHERE schemaname = %s" if schema else ""),
                         (schema,) if schema else ())
                for r in rows:
                    sw.add(DbObject(kind="materialized_view",
                                    name=_s(r, "matviewname"),
                                    schema=_s(r, "schemaname"), database=db,
                                    definition=_s(r, "definition")))
                return
            except Exception:  # noqa: BLE001 — not Postgres, try Redshift
                pass
            for r in q("SELECT schema AS schemaname, name FROM stv_mv_info"):
                sw.add(DbObject(kind="materialized_view",
                                name=_s(r, "name"),
                                schema=_s(r, "schemaname"), database=db))
        sw.category("materialized views", matviews)

        def routines() -> None:
            # pg_proc serves both engines; prokind is Postgres 11+ only, so
            # fall back to the pre-11 shape that Redshift still speaks.
            base = ("SELECT n.nspname AS schema, p.proname AS name, "
                    "l.lanname AS language, p.prosrc AS body%s "
                    "FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "JOIN pg_language l ON l.oid = p.prolang "
                    "WHERE l.lanname <> 'internal' AND l.lanname <> 'c' %s")
            try:
                rows = q(base % (", p.prokind AS prokind", npred), nargs)
            except Exception:  # noqa: BLE001
                rows = q(base % ("", npred), nargs)
            for r in rows:
                is_proc = str(r.get("prokind", "")) == "p" or \
                    str(r.get("language", "")) == "plpgsql"
                sw.add(DbObject(
                    kind="procedure" if is_proc else "function",
                    name=_s(r, "name"), schema=_s(r, "schema"), database=db,
                    definition=_s(r, "body"),
                    language=_s(r, "language").lower()))
        sw.category("procedures/functions", routines)

        def sequences() -> None:
            for r in q("SELECT sequence_schema, sequence_name, start_value, "
                       "increment FROM information_schema.sequences "
                       "WHERE sequence_catalog = current_database()"
                       + (" AND sequence_schema = %s" if schema else ""),
                       (schema,) if schema else ()):
                sw.add(DbObject(kind="sequence",
                                name=_s(r, "sequence_name"),
                                schema=_s(r, "sequence_schema"), database=db,
                                properties={"start": _s(r, "start_value"),
                                            "increment": _s(r, "increment")}))
        sw.category("sequences", sequences)

        def constraints() -> None:
            rows = q("SELECT tc.constraint_name, tc.table_schema, "
                     "tc.table_name, tc.constraint_type, kcu.column_name "
                     "FROM information_schema.table_constraints tc "
                     "LEFT JOIN information_schema.key_column_usage kcu "
                     "ON kcu.constraint_name = tc.constraint_name "
                     "AND kcu.table_schema = tc.table_schema "
                     "WHERE tc.constraint_type IN "
                     "('PRIMARY KEY','UNIQUE','FOREIGN KEY') "
                     "AND tc.table_schema NOT IN %s" % _PG_SYSTEM
                     + (" AND tc.table_schema = %s" if schema else ""),
                     (schema,) if schema else ())
            grouped: Dict[tuple, dict] = {}
            for r in rows:
                key_ = (_s(r, "table_schema"), _s(r, "table_name"),
                        _s(r, "constraint_name"))
                e = grouped.setdefault(
                    key_, {"type": _s(r, "constraint_type"), "columns": []})
                col = _s(r, "column_name")
                if col and col not in e["columns"]:
                    e["columns"].append(col)
            for (sch, tbl, cname), e in grouped.items():
                sw.add(DbObject(kind="constraint", name=cname, schema=sch,
                                database=db,
                                properties={"constraint_type": e["type"],
                                            "table": tbl,
                                            "columns": e["columns"]}))
        sw.category("constraints", constraints)

        def grants() -> None:
            for r in q("SELECT grantee, privilege_type, table_schema, "
                       "table_name FROM information_schema.table_privileges "
                       "WHERE table_schema NOT IN %s LIMIT %d"
                       % (_PG_SYSTEM, _MAX_GRANTS)):
                sw.add(DbObject(kind="grant", name=_s(r, "table_name"),
                                schema=_s(r, "table_schema"), database=db,
                                properties={"grantee": _s(r, "grantee"),
                                            "privilege":
                                                _s(r, "privilege_type"),
                                            "on_type": "TABLE"}))
        sw.category("grants", grants)

        def comments() -> None:
            for r in q("SELECT n.nspname AS schema, c.relname AS name, "
                       "d.description FROM pg_description d "
                       "JOIN pg_class c ON c.oid = d.objoid "
                       "JOIN pg_namespace n ON n.oid = c.relnamespace "
                       "WHERE d.objsubid = 0 AND n.nspname NOT IN %s"
                       % _PG_SYSTEM):
                sw.add(DbObject(kind="comment", name=_s(r, "name"),
                                schema=_s(r, "schema"), database=db,
                                definition=_s(r, "description"),
                                properties={"on": "table"}))
        sw.category("comments", comments)

        if key == "redshift":
            def external() -> None:
                for r in q("SELECT schemaname, tablename, location "
                           "FROM svv_external_tables"):
                    sw.add(DbObject(kind="external_table",
                                    name=_s(r, "tablename"),
                                    schema=_s(r, "schemaname"), database=db,
                                    properties={"location":
                                                _s(r, "location")}))
            sw.category("external tables", external)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# Databricks (Unity Catalog information_schema)
# ===========================================================================

def _databricks_objects(params: Dict[str, str],
                        inv: ObjectInventory) -> None:
    from ..livecheck import _databricks_connect
    conn = _databricks_connect(params)
    sw = _Sweep(inv)
    try:
        cur = conn.cursor()
        schema = params.get("schema") or None

        def q(sql: str, args: list) -> List[dict]:
            cur.execute(sql, args)
            return _dictrows(cur)[:_MAX_PER_CATEGORY]

        def tables() -> None:
            for r in q("SELECT table_schema, table_name "
                       "FROM information_schema.tables "
                       "WHERE table_schema = COALESCE(?, table_schema) "
                       "AND table_type <> 'VIEW'", [schema]):
                sw.add(DbObject(kind="table", name=_s(r, "table_name"),
                                schema=_s(r, "table_schema")))
        sw.category("tables", tables)

        def views() -> None:
            for r in q("SELECT table_schema, table_name, view_definition "
                       "FROM information_schema.views "
                       "WHERE table_schema = COALESCE(?, table_schema)",
                       [schema]):
                sw.add(DbObject(kind="view", name=_s(r, "table_name"),
                                schema=_s(r, "table_schema"),
                                definition=_s(r, "view_definition")))
        sw.category("views", views)

        def routines() -> None:
            for r in q("SELECT routine_schema, routine_name, "
                       "routine_definition, external_language "
                       "FROM information_schema.routines "
                       "WHERE routine_schema = COALESCE(?, routine_schema)",
                       [schema]):
                lang = (_s(r, "external_language") or "sql").lower()
                sw.add(DbObject(kind="function", name=_s(r, "routine_name"),
                                schema=_s(r, "routine_schema"),
                                definition=_s(r, "routine_definition"),
                                language=lang))
        sw.category("functions", routines)

        def constraints() -> None:
            for r in q("SELECT constraint_name, table_schema, table_name, "
                       "constraint_type "
                       "FROM information_schema.table_constraints "
                       "WHERE table_schema = COALESCE(?, table_schema)",
                       [schema]):
                sw.add(DbObject(kind="constraint",
                                name=_s(r, "constraint_name"),
                                schema=_s(r, "table_schema"),
                                properties={"constraint_type":
                                            _s(r, "constraint_type"),
                                            "table": _s(r, "table_name"),
                                            "columns": []}))
        sw.category("constraints", constraints)

        def grants() -> None:
            for r in q("SELECT grantee, privilege_type, table_schema, "
                       "table_name "
                       "FROM information_schema.table_privileges "
                       "WHERE table_schema = COALESCE(?, table_schema) "
                       "LIMIT %d" % _MAX_GRANTS, [schema]):
                sw.add(DbObject(kind="grant", name=_s(r, "table_name"),
                                schema=_s(r, "table_schema"),
                                properties={"grantee": _s(r, "grantee"),
                                            "privilege":
                                                _s(r, "privilege_type"),
                                            "on_type": "TABLE"}))
        sw.category("grants", grants)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# entry point
# ===========================================================================

def inventory_objects(key: str, params: Dict[str, str]) -> dict:
    """Enumerate every schema-level object the connected role can see.
    -> {"ok": bool, "inventory": {...}} — read-only throughout."""
    from ..livecheck import _SQL_DIALECTS, _has_live_driver
    if not _has_live_driver(key):
        return {"ok": False, "connector": key, "unsupported": True,
                "error": "Live object inventory is not implemented for "
                         "'%s' yet — it needs a live driver. Supported "
                         "today: Snowflake, Amazon Redshift, PostgreSQL, "
                         "Databricks." % key}
    inv = ObjectInventory(connector=key,
                          database=params.get("database", ""),
                          schema=params.get("schema", ""))
    started = time.time()
    try:
        if key == "snowflake":
            _snowflake_objects(params, inv)
        elif key in _SQL_DIALECTS:
            _pg_objects(key, params, inv)
        elif key == "databricks":
            _databricks_objects(params, inv)
    except Exception as e:  # noqa: BLE001 — connection-level failure
        return {"ok": False, "connector": key, "error": str(e)[:400]}
    inv.elapsed_ms = int((time.time() - started) * 1000)
    return {"ok": True, "inventory": inv.to_dict()}

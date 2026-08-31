"""Landing-layer DDL + bulk-load scripts for the target warehouse.

The gap this closes: a scaffold hands you a dbt project whose models read
``{{ source(...) }}``, and a dbt profile pointing at the TARGET warehouse.
dbt transforms inside one warehouse — it never moves data between two. So
until the source tables physically exist in the target, ``dbt run`` fails on
the first model with "relation does not exist".

This generator emits the missing extract-and-load layer:

    00_probe_string_widths.sql  (only when widths are still unmeasured)
    01_create_landing.sql    schemas + one typed CREATE TABLE per source
                             table, warehouse-native, with physical design
                             (Redshift DIST/SORT keys) inferred from the
                             merge key and watermark column
    02_unload_from_<src>.sql source-side bulk export — sets its own
                             USE DATABASE/WAREHOUSE context and fully
                             qualifies every table, so it is
                             session-independent
    03_load_into_<tgt>.sql   target-side bulk import
    README.md                run order and anything left to substitute

Schema names deliberately match the generated ``sources.yml`` so the dbt
models resolve with no edits — the two artifacts agree by construction.

Workspace Data movement settings (stage URI / named stage / IAM role) are
substituted into every statement, so a configured workspace gets
ready-to-run scripts with zero placeholders; nothing here ever embeds a
credential.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

from ..connectors.base import ConnectorSpec
from ..ir.model import (
    ConversionIssue, IssueSeverity, LoadStrategy, Pipeline, Port, SourceTable,
    TransformationType,
)

# Canonical -> warehouse type. Only where a warehouse genuinely differs from
# ANSI; everything else falls through to _ANSI.
_ANSI = {"string": "VARCHAR", "integer": "INTEGER", "bigint": "BIGINT",
         "decimal": "DECIMAL", "double": "DOUBLE PRECISION", "date": "DATE",
         "timestamp": "TIMESTAMP", "boolean": "BOOLEAN", "binary": "VARBINARY"}
_BY_DIALECT: Dict[str, Dict[str, str]] = {
    "redshift": {"double": "DOUBLE PRECISION", "binary": "VARBYTE",
                 "timestamp": "TIMESTAMP"},
    "snowflake": {"double": "FLOAT", "binary": "BINARY",
                  "timestamp": "TIMESTAMP_NTZ"},
    "bigquery": {"string": "STRING", "integer": "INT64", "bigint": "INT64",
                 "decimal": "NUMERIC", "double": "FLOAT64",
                 "timestamp": "TIMESTAMP", "boolean": "BOOL",
                 "binary": "BYTES"},
    "databricks": {"integer": "INT", "double": "DOUBLE", "binary": "BINARY",
                   "string": "STRING"},
    "postgres": {"binary": "BYTEA", "double": "DOUBLE PRECISION"},
    "tsql": {"string": "NVARCHAR", "double": "FLOAT", "binary": "VARBINARY",
             "timestamp": "DATETIME2", "boolean": "BIT"},
    "oracle": {"string": "VARCHAR2", "integer": "NUMBER(10)",
               "bigint": "NUMBER(19)", "decimal": "NUMBER",
               "double": "BINARY_DOUBLE", "timestamp": "TIMESTAMP",
               "boolean": "NUMBER(1)", "binary": "BLOB"},
    "teradata": {"string": "VARCHAR", "double": "FLOAT", "binary": "VARBYTE"},
}
# Widths used when the source metadata declares none.
#
# Not the maximum. A column declared VARCHAR(65535) costs nothing to store on
# Redshift but everything to query: sorts, joins and aggregates reserve memory
# from the DECLARED width, so max-width columns push queries to disk. The
# opposite risk — too narrow — is not silent: Redshift COPY *errors* on an
# over-long value unless TRUNCATECOLUMNS is set. So the default is moderate,
# the failure mode is loud, and 00_probe_string_widths.sql measures the real
# widths in one query.
from ..sqlx.type_engine import DECIMAL_FALLBACK as _DECIMAL_FALLBACK
_VARCHAR_FALLBACK = {"redshift": 4000, "tsql": 4000, "oracle": 4000,
                     "teradata": 4000}
# Dialects where an unbounded VARCHAR is legal, so no width is emitted at all
_UNBOUNDED_STRING = ("bigquery", "databricks", "postgres", "snowflake")
# Dialects whose string type takes NO length at all: Spark/Databricks STRING
# rejects STRING(20), so a declared width has to be dropped, not passed on.
_NO_STRING_LENGTH = ("databricks",)

# Scale-0 numerics -> the narrowest EXACT integer type, per dialect
# (32-bit name, 64-bit name). Narrowing is loss-free in one direction only:
# INTEGER spans +/-2.1e9 and so contains every DECIMAL(9,0); BIGINT spans
# +/-9.2e18 and contains every DECIMAL(18,0). Above 18 digits the decimal
# stays, because nothing narrower holds it.
#
# Omitted on purpose: Snowflake, where INT/BIGINT are aliases of NUMBER(38,0)
# so narrowing gains nothing and discards the declared precision; and Oracle,
# where NUMBER(p) already IS the exact-integer type.
_INT_NARROWING = {
    "redshift": ("INTEGER", "BIGINT"), "postgres": ("INTEGER", "BIGINT"),
    "tsql": ("INT", "BIGINT"), "databricks": ("INT", "BIGINT"),
    "bigquery": ("INT64", "INT64"), "teradata": ("INTEGER", "BIGINT"),
}
_INT32_DIGITS, _INT64_DIGITS = 9, 18

# Largest width each target will accept on a character column, and what to
# use past it. A manifest can legitimately declare more than a target allows
# (Snowflake VARCHAR goes to 16 MB, Redshift stops at 64 KB), so the DDL has
# to resolve that rather than emit a statement the warehouse rejects.
_MAX_STRING = {"redshift": 65535, "tsql": 4000, "oracle": 4000,
               "teradata": 64000}
_OVER_MAX_STRING = {"tsql": "NVARCHAR(MAX)", "oracle": "CLOB",
                    "teradata": "CLOB"}


# Source platforms sqlx.type_engine can parse natively. A connector key
# outside this set still works — the engine's own cross-platform fallback
# resolves the type — but naming the source makes the parse exact.
_ENGINE_PLATFORMS = frozenset({
    "oracle", "snowflake", "databricks", "bigquery", "redshift", "synapse",
    "sqlserver", "postgres", "teradata", "informatica",
})


def _engine_type(port: Port, dialect: str, source_platform: str) -> str:
    """Target type resolved from the source's DECLARED type.

    Returns "" when the native type is unknown or the engine cannot place
    it, leaving the caller on the canonical path. The engine understands 18
    canonical types against the IR's 9, so it is the only path that can
    tell TIME from VARCHAR or keep a time-zone offset — reading the coarse
    canonical instead is what emitted VARCHAR(6) for TIME(6).
    """
    if not port.native_type or dialect not in _ENGINE_PLATFORMS:
        return ""
    try:
        from ..sqlx.type_engine import get_type_engine
        eng = get_type_engine()
        src = source_platform if source_platform in _ENGINE_PLATFORMS else "ansi"
        ct, parse_warn = eng.parse_type(port.native_type, src)
        # An unparsed type lands on STRING, which is exactly the guess the
        # canonical path already makes — defer to it rather than dressing
        # a default up as an engine answer.
        if any(w.code == "unknown_type" for w in parse_warn):
            return ""
        # A DECIMAL with no declared precision renders as NUMBER(38,0) here,
        # and scale 0 truncates every fractional value — an FX rate of
        # 83.4125 lands as 83, silently. The canonical path's documented
        # (38,6) fallback keeps the fraction AND raises
        # NUMERIC_PRECISION_FALLBACK, so it owns this case.
        if ct.name == "DECIMAL" and ct.precision is None:
            return ""
        rendered, _ = eng.render_type(ct, dialect)
        return rendered or ""
    except (ImportError, KeyError, OSError, ValueError):
        return ""


def _type_of(port: Port, dialect: str, source_platform: str = "") -> str:
    engine = _engine_type(port, dialect, source_platform)
    if engine:
        return engine
    base = dict(_ANSI)
    base.update(_BY_DIALECT.get(dialect, {}))
    canon = port.datatype or "string"
    sqlt = base.get(canon, base["string"])
    if canon == "decimal":
        p, sc = (port.precision, port.scale or 0) if port.precision \
            else _DECIMAL_FALLBACK
        narrow = _INT_NARROWING.get(dialect)
        if narrow and sc == 0 and port.precision:
            if p <= _INT32_DIGITS:
                return narrow[0]
            if p <= _INT64_DIGITS:
                return narrow[1]
        return "%s(%d,%d)" % (base["decimal"], p, sc)
    if canon == "string":
        if port.precision and dialect not in _NO_STRING_LENGTH:
            cap = _MAX_STRING.get(dialect, 0)
            if cap and port.precision > cap:
                over = _OVER_MAX_STRING.get(dialect)
                return over if over else "%s(%d)" % (sqlt, cap)
            return "%s(%d)" % (sqlt, port.precision)
        if dialect in _UNBOUNDED_STRING:
            return sqlt
        width = _VARCHAR_FALLBACK.get(dialect, 0)
        return "%s(%d)" % (sqlt, width) if width else sqlt
    return sqlt


def _quote(dialect: str, ident: str) -> str:
    """Quote an identifier for `dialect`, only where it has to be.

    Delegated to sqlx.identifiers so the LANDING DDL and the models that read
    it apply exactly the same rule. They must: an unquoted identifier folds to
    upper case on Snowflake and Oracle and a quoted one does not, so a table
    created with a bare `ORDER` column and a model selecting `"order"` would
    disagree about a column that exists.

    This used to cover only characters illegal in a bare identifier, which
    left every RESERVED WORD — `ORDER`, `GROUP`, `USER`, `DATE` — emitted bare
    on both sides: consistent, and consistently invalid SQL.
    """
    from ..sqlx.identifiers import quote_identifier
    return quote_identifier(dialect, ident)


def _qualified(dialect: str, schema: str, name: str) -> str:
    return "%s.%s" % (_quote(dialect, schema), _quote(dialect, name)) \
        if schema else _quote(dialect, name)


def _create_schemas(dialect: str, schemas: List[str]) -> str:
    """``CREATE SCHEMA IF NOT EXISTS`` is not universal: T-SQL has no IF NOT
    EXISTS on CREATE SCHEMA, and an Oracle schema IS a user."""
    if dialect == "tsql":
        return "".join(
            "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '%s')\n"
            "    EXEC('CREATE SCHEMA [%s]');\n" % (s, s) for s in schemas)
    if dialect == "oracle":
        return "".join(
            "-- Oracle: a schema is a user. Run as a DBA and grant a quota.\n"
            "-- CREATE USER %s IDENTIFIED BY \"<password>\" "
            "QUOTA UNLIMITED ON USERS;\n" % _quote(dialect, s) for s in schemas)
    return "".join("CREATE SCHEMA IF NOT EXISTS %s;\n" % _quote(dialect, s) for s in schemas)


def _load_hints(pipeline: Pipeline) -> Dict[str, dict]:
    """source table -> {unique_key, watermark} taken from the mapping that
    ingests it. This is what a Redshift DIST/SORT key should follow: the
    merge key drives colocation, the watermark drives range scans."""
    hints: Dict[str, dict] = {}
    for m in pipeline.mappings:
        table = ""
        for t in m.by_type(TransformationType.SOURCE):
            table = str(t.properties.get("table", "") or "")
            if table:
                break
        if not table:
            continue
        watermark = ""
        for t in m.by_type(TransformationType.FILTER):
            cond = str(t.properties.get("condition", "") or "")
            if "$$LAST_RUN_TS" in cond:
                watermark = cond.split(">")[0].strip()
                break
        h = hints.setdefault(table, {"unique_key": [], "watermark": "",
                                     "strategy": m.load_strategy.value})
        if m.unique_key and not h["unique_key"]:
            h["unique_key"] = list(m.unique_key)
        if watermark and not h["watermark"]:
            h["watermark"] = watermark
    return hints


def _redshift_physical(src: SourceTable, hint: dict) -> List[str]:
    """DIST/SORT keys — the single largest performance lever on Redshift, and
    the one thing a lift-and-shift always forgets."""
    cols = {c.name.lower() for c in src.columns}
    out: List[str] = []
    uk = [k for k in hint.get("unique_key", []) if k.lower() in cols]
    wm = hint.get("watermark", "")
    if uk:
        out.append("DISTSTYLE KEY")
        out.append("DISTKEY(%s)" % uk[0])
    else:
        # AUTO lets Redshift start as ALL and promote to EVEN as the table
        # grows — the right default when row counts are unknown.
        out.append("DISTSTYLE AUTO")
    sort = wm if wm and wm.lower() in cols else (uk[0] if uk else "")
    if sort:
        out.append("COMPOUND SORTKEY(%s)" % sort)
    return out


def _probe_widths(spec: Optional[ConnectorSpec], dialect: str,
                  tables: List[SourceTable]) -> str:
    """Measure the real width of every text column whose length the source
    metadata does not declare. Run on the SOURCE; feed the answers back into
    the manifest so the landing DDL stops guessing."""
    parts: List[str] = []
    fn = "LEN" if dialect == "tsql" else "LENGTH"
    for t in tables:
        measures = ["       MAX(%s(%s)) AS %s_len" % (fn, _quote(dialect, c.name), re.sub(r"[^A-Za-z0-9_]", "_", c.name)[:110])
                    for c in t.columns
                    if c.datatype == "string" and not c.precision]
        # an integral column too wide to narrow: measure it so a 128-bit
        # DECIMAL(38,0) key can become a BIGINT on evidence
        measures += ["       MAX(ABS(%s)) AS %s_max" % (_quote(dialect, c.name), re.sub(r"[^A-Za-z0-9_]", "_", c.name)[:110])
                     for c in t.columns
                     if c.datatype == "decimal" and not (c.scale or 0)
                     and c.precision > _INT64_DIGITS]
        # A decimal that declares NO precision at all — Oracle's bare NUMBER.
        # This is the column the landing DDL had to guess at, so it is the
        # one worth measuring: the guess is what makes the two sides of the
        # migration declare different types, and a reconciliation checksum
        # compares the RENDERING of a number, so a column stored as
        # NUMBER(38,6) on one side and NUMBER on the other never matches
        # however equal the values are.
        #
        # `= FLOOR(x)` rather than a scale function because it is true on
        # every engine and correct for negatives (FLOOR(-1.5) is -2, so a
        # fractional negative still reports 1).
        for c in t.columns:
            if c.datatype != "decimal" or c.precision:
                continue
            col, safe = (_quote(dialect, c.name),
                         re.sub(r"[^A-Za-z0-9_]", "_", c.name)[:105])
            measures.append("       MAX(ABS(%s)) AS %s_max" % (col, safe))
            measures.append(
                "       MAX(CASE WHEN %s = FLOOR(%s) THEN 0 ELSE 1 END) "
                "AS %s_frac" % (col, col, safe))
        if not measures:
            continue
        parts.append("SELECT '%s' AS table_name,\n%s\nFROM %s;"
                     % (t.name, ",\n".join(measures),
                        _qualified(dialect, t.schema, t.name)))
    if not parts:
        return ""
    return ("-- Step 0 (optional but recommended): measure real column sizes.\n"
            "-- Run on the SOURCE, then put the measured sizes in the manifest\n"
            "-- and regenerate.\n"
            "--\n"
            "--   *_len   text columns whose length the source does not\n"
            "--           declare; 01_create_landing.sql used a documented\n"
            "--           default. Narrow columns keep Redshift/Synapse\n"
            "--           queries in memory instead of spilling to disk.\n"
            "--   *_max   how large the column actually gets. For an\n"
            "--           integral column wider than 18 digits: if the real\n"
            "--           maximum fits in 9.2e18, declare NUMBER(18,0) and it\n"
            "--           becomes BIGINT — much cheaper to join and\n"
            "--           distribute on. For a column with no declared\n"
            "--           precision, this is the precision to declare.\n"
            "--   *_frac  0 means every value in that column is a whole\n"
            "--           number, so declare it NUMBER(<*_max digits>,0).\n"
            "--           1 means it really does carry decimals — declare\n"
            "--           the scale the business uses.\n"
            "--\n"
            "--           Worth doing even when the default looks harmless:\n"
            "--           an undeclared decimal lands as %s and the source\n"
            "--           keeps its own type, so the reconciliation checksum\n"
            "--           hashes '1001.000000' against '1001' and reports a\n"
            "--           mismatch on data that is identical.\n\n"
            % ("decimal(%d,%d)" % _DECIMAL_FALLBACK)
            + "\n\n".join(parts) + "\n")


def _target_context(dialect: str,
                    target_params: Optional[Dict[str, str]]) -> List[str]:
    """Statements that put the session somewhere before anything is created.

    01_create_landing.sql issues CREATE SCHEMA and CREATE TABLE with no
    database qualifier, so it lands wherever the session happens to point —
    or fails outright if it points nowhere. The unload script has always set
    its own context for exactly this reason; the landing script needs it just
    as much, and not having it was a step every operator had to know about
    and perform by hand.

    Same convention as the unload: a known value becomes a real statement, an
    unknown one a commented line naming what to set. Dialects that resolve
    the database at CONNECT time (PostgreSQL, Redshift) or carry it in every
    identifier (BigQuery) get a note rather than a statement they cannot run.

    A placeholder also says WHY it is one. These values come from the target
    CONNECTION, so a bundle generated with a connection selected has real
    names here and one generated without has `<database>` — and with nothing
    saying so, the second looks like a defect in the generator rather than a
    consequence of how the run was set up.
    """
    p = target_params or {}
    unfilled: List[str] = []

    def val(name: str) -> str:
        return str(p.get(name, "") or "").strip()

    def missing(token: str) -> str:
        unfilled.append(token)
        return token

    lines: List[str] = []
    if dialect == "snowflake":
        db, wh = val("database"), val("warehouse")
        lines.append("USE DATABASE %s;" % db if db
                     else "-- USE DATABASE %s;    -- uncomment and set"
                     % missing("<database>"))
        # CREATE SCHEMA/TABLE are metadata-only on Snowflake and need no
        # running warehouse, so this is emitted only when one is known —
        # a commented placeholder would imply a requirement that is not real.
        if wh:
            lines.append("USE WAREHOUSE %s;" % wh)
    elif dialect == "databricks":
        cat = val("catalog")
        lines.append("USE CATALOG %s;" % cat if cat
                     else "-- USE CATALOG %s;      -- uncomment and set"
                     % missing("<catalog>"))
    elif dialect == "tsql":
        db = val("database")
        lines.append("USE %s;" % db if db
                     else "-- USE %s;             -- uncomment and set"
                     % missing("<database>"))
    elif dialect == "teradata":
        db = val("database")
        lines.append("DATABASE %s;" % db if db
                     else "-- DATABASE %s;        -- uncomment and set"
                     % missing("<database>"))
    elif dialect in ("postgres", "redshift"):
        db = val("database")
        lines.append("-- Connect to database %s before running — %s selects "
                     "the database at" % (db, dialect) if db
                     else "-- Connect to the target database before running "
                          "— %s selects it at" % dialect)
        lines.append("-- connect time, so there is no in-session statement "
                     "to switch it.")
    if not lines:
        return []
    why: List[str] = []
    if unfilled:
        many = len(unfilled) > 1
        why = ["-- %s below %s a placeholder, and %s from the TARGET "
               "CONNECTION." % (", ".join(unfilled),
                                "are" if many else "is",
                                "they come" if many else "it comes"),
               "-- This bundle was generated without one selected. Choose a "
               "target connection",
               "-- and regenerate to have %s filled in, or substitute %s "
               "here." % (("them", "them") if many else ("it", "it"))]
    return ["-- Session context, so this runs the same from any worksheet:"] \
        + why + lines + [""]


def _create_table(src: SourceTable, dialect: str, hint: dict,
                  source_platform: str = "") -> str:
    # NOT NULL travels with the column. It is not decoration: a landing
    # table that accepts nulls where the source rejected them turns a load
    # that should have failed loudly into rows that quietly break every
    # downstream join, and the reconciliation null_comparison would then be
    # the first thing to notice — after the data was already in.
    cols = ",\n".join(
        "    %-32s %s%s" % (_quote(dialect, c.name),
                            _type_of(c, dialect, source_platform),
                            "" if getattr(c, "nullable", True) else " NOT NULL")
        for c in src.columns)
    stmt = "CREATE TABLE IF NOT EXISTS %s (\n%s\n)" % (
        _qualified(dialect, src.schema, src.name), cols)
    if dialect == "redshift":
        phys = _redshift_physical(src, hint)
        if phys:
            stmt += "\n" + "\n".join(phys)
    elif dialect == "databricks":
        stmt += "\nUSING DELTA"
    elif dialect == "bigquery" and hint.get("watermark"):
        stmt += "\nPARTITION BY DATE(%s)" % hint["watermark"]
    return stmt + ";"


# ---------------------------------------------------------------------------
# bulk export / import — the actual data movement
# ---------------------------------------------------------------------------
#
# Movement settings (workspace-level, set once in Pipeline Studio) fill the
# values engineers previously had to paste into every statement:
#
#     stage_uri     object-storage prefix both warehouses can reach
#     iam_role      role ARN the target assumes for its bulk load
#     source_stage  a NAMED stage that already exists on the source —
#                   the zero-credential path: the stage holds the storage
#                   credential, so nothing sensitive appears anywhere here
#
# Anything not configured stays an explicit <angle-bracket> token and is
# listed in the README. Credentials are NEVER written into these files.

# Where a PostgreSQL \copy lands when the stage is object storage. Relative
# on purpose: it resolves against wherever psql was launched, needs no
# privileges, and is obvious to clean up.
_PG_LOCAL_EXPORT_DIR = "./mb_export"
# Platforms whose object names really do carry three parts.
_THREE_PART_DIALECTS = ("snowflake", "bigquery", "databricks", "tsql")
# Where a delimited Teradata export lands when NOS is unavailable.
_TD_LOCAL_EXPORT_DIR = "./mb_export"
# SQLcl spools to a FILE, never to a bucket, so an Oracle export always lands
# locally first and is uploaded afterwards.
_ORACLE_LOCAL_EXPORT_DIR = "./mb_export"


def _hana_object_uri(uri: str, region: str = "") -> str:
    """Rewrite an object-storage URI into the form SAP HANA accepts.

    HANA puts the REGION in the scheme — `s3-ap-south-1://bucket/path` —
    where Snowflake, Databricks and Redshift all take a plain `s3://`. The
    same workspace stage URI therefore has to render differently on the two
    halves of the package, so this converts only for HANA and leaves the
    load side untouched. That is also why the region is its own setting
    rather than something the user bakes into the stage URI: written there,
    HANA would be right and every other target would break.

    Verified against SAP HANA Cloud 2026.14: `s3-ap-south-1://` exported
    successfully. The region cannot be inferred from a bucket name, so
    without one a plain `s3://` becomes an explicit `<region>` placeholder
    rather than a guess. A URI that already names its region is left alone.
    """
    if uri.startswith("s3://"):
        return "s3-%s://%s" % (region or "<region>", uri[len("s3://"):])
    return uri


def _td_nos_location(uri: str) -> str:
    """Rewrite an object-storage URI into the form Teradata NOS accepts.

    NOS addresses a bucket as a PATH with the endpoint host inside it —
    `/s3/bucket.s3.amazonaws.com/prefix/` — where every other platform here
    takes `s3://bucket/prefix`. One workspace setting, rendered differently
    on the two halves of the package, exactly as with SAP HANA's
    region-in-the-scheme.
    """
    if uri.startswith("s3://"):
        rest = uri[len("s3://"):].strip("/")
        bucket, _, path = rest.partition("/")
        return "/s3/%s.s3.amazonaws.com/%s" % (
            bucket, path + "/" if path else "")
    for scheme, nos in (("abfss://", "/az/"), ("az://", "/az/"),
                        ("gs://", "/gs/")):
        if uri.startswith(scheme):
            return nos + uri.split("://", 1)[1].strip("/") + "/"
    return uri


def _mv(movement: Optional[dict]) -> dict:
    m = movement or {}
    return {"stage_uri": str(m.get("stage_uri", "") or "").rstrip("/"),
            "iam_role": str(m.get("iam_role", "") or "").strip(),
            "source_stage":
                str(m.get("source_stage", "") or "").strip().lstrip("@"),
            # Consumed only by the SAP HANA unload — see _hana_object_uri.
            "region": str(m.get("region", "") or "").strip().lower(),
            # A NAMED credential, created once on the source. Like
            # source_stage it is an identifier, not a secret, so storing it
            # keeps the generated script complete without holding a key.
            "source_credential":
                str(m.get("source_credential", "") or "").strip(),
            # The target-side mirror of source_stage: a Snowflake stage
            # built on a storage integration, so the load needs no
            # CREDENTIALS clause and no key reaches a generated file.
            "target_stage":
                str(m.get("target_stage", "") or "").strip().lstrip("@")}


# One unambiguous textual shape for every temporal value that travels
# through CSV. The load side reads these too — the two ends of the move
# cannot be allowed to disagree about what a date looks like, so they read
# the same constants rather than each spelling out a format.
#
# Oracle DATE carries seconds but no fraction, so it cannot use the FF form;
# that is why there are two, and why the load stays on AUTO rather than
# pinning one of them and rejecting the other. AUTO is only a guess when the
# input is ambiguous, and ISO-8601 with a four-digit year is not.
_ISO_DATE_ORACLE = "YYYY-MM-DD HH24:MI:SS"
_ISO_TIMESTAMP_ORACLE = "YYYY-MM-DD HH24:MI:SS.FF9"
_ISO_TIMESTAMP_TZ_ORACLE = "YYYY-MM-DD HH24:MI:SS.FF9 TZH:TZM"


def _unload(spec: Optional[ConnectorSpec], dialect: str,
            tables: List[SourceTable],
            source_params: Optional[Dict[str, str]] = None,
            movement: Optional[dict] = None) -> str:
    """Source-side export to object storage, in Parquet (typed, so the
    import does not have to re-guess every column). Session-independent:
    the script sets its own database/warehouse context and fully qualifies
    every table, so it runs the same from any worksheet."""
    sp = source_params or {}
    mv = _mv(movement)
    uri = mv["stage_uri"] or "<stage-uri>"
    head = ["-- Step 2: export each source table to object storage.",
            "-- Run on the SOURCE.", ""]

    def db_of(t: SourceTable) -> str:
        return t.database or str(sp.get("database", "") or "")

    def fq(t: SourceTable) -> str:
        q = _qualified(dialect, t.schema, t.name)
        d = db_of(t)
        # Only these platforms address an object as
        # DATABASE.SCHEMA.TABLE. On Oracle the database is the
        # CONNECTION (a PDB); on Teradata and SAP HANA a "database" IS
        # the schema. Prefixing one there builds a three-part name that
        # addresses nothing — and since the connection database and the
        # schema are the same word on Teradata, it read back as the
        # absurdity it was: BANKING_DB.BANKING_DB.RAW_ACCOUNTS.
        if d and dialect in _THREE_PART_DIALECTS:
            return "%s.%s" % (d, q)
        return q

    # SAP HANA dispatches on the CONNECTOR, not the dialect. It declares no
    # sqlglot dialect because none exists, so the dialect chain below would
    # never reach it and it would fall to the "no bulk-export form" default.
    # Objects are addressed SCHEMA.TABLE — the tenant database is the
    # connection, so db_of()/fq() are deliberately NOT used here: prefixing
    # the tenant would build an invalid three-part name.
    if spec is not None and spec.key == "sap_hana":
        hana_uri = _hana_object_uri(uri, mv["region"])
        cred = mv["source_credential"] or "<credentials>"
        head += [
            "-- EXPORT INTO writes CSV; SAP HANA has no Parquet export form.",
            "-- The matching load on the target must therefore read CSV.",
            "--",
            # NB: no literal <region> token in this comment — the README's
            # placeholder scan is a substring match over this file, and an
            # illustration containing it would report a substitution that is
            # already filled in.
            "-- The REGION belongs in the scheme (e.g. s3-ap-south-1://),",
            "-- which is HANA-specific: the load side keeps a plain s3://.",
            "--",
            "-- The credential is a NAME, not a secret: create it ONCE on "
            "HANA and",
            "-- no key material ever appears in this file.",
            "--   CREATE CREDENTIAL FOR COMPONENT 'SAPHANAIMPORTEXPORT'",
            "--     PURPOSE '%s' TYPE 'PASSWORD'" % cred,
            "--     USING 'user=<access-key>;password=<secret-key>';",
            ""]
        body = ["EXPORT INTO '%s/%s/'\n  FROM %s\n"
                "  WITH CREDENTIAL '%s'\n"
                "       COLUMN LIST IN FIRST ROW;"
                % (hana_uri, t.name.lower(),
                   _qualified(dialect, t.schema, t.name), cred)
                for t in tables]
    elif dialect == "snowflake":
        db = next((db_of(t) for t in tables if db_of(t)), "")
        wh = str(sp.get("warehouse", "") or "")
        head += ["-- Context is set explicitly so this runs from any "
                 "worksheet/session:"]
        if not db or not wh:
            head += ["-- The commented value(s) below come from the SOURCE "
                     "CONNECTION, and this",
                     "-- bundle was generated without one supplying them. "
                     "Connect the source",
                     "-- and regenerate to have them filled in, or "
                     "substitute them here."]
        head += ["USE DATABASE %s;" % db if db
                 else "-- USE DATABASE <database>;   -- uncomment and set",
                 "USE WAREHOUSE %s;" % wh if wh
                 else "-- USE WAREHOUSE <warehouse>;  -- uncomment and set "
                      "(an unload needs compute)",
                 ""]
        if mv["source_stage"]:
            stage_ref = "@%s" % mv["source_stage"]
            head += ["-- Using the named stage from your Data movement "
                     "settings — its storage",
                     "-- credential lives in Snowflake, so nothing "
                     "sensitive appears here.", ""]
        else:
            stage_ref = "@mb_unload"
            head += [
                "CREATE OR REPLACE FILE FORMAT mb_parquet TYPE = PARQUET;",
                "CREATE OR REPLACE STAGE mb_unload",
                "  URL = '%s/'" % uri,
                "  CREDENTIALS = (<credentials>)   "
                "-- or create a named stage once and set it in Data "
                "movement settings",
                "  FILE_FORMAT = mb_parquet;", ""]
        body = ["COPY INTO %s/%s/\n  FROM %s\n  "
                "HEADER = TRUE OVERWRITE = TRUE;"
                % (stage_ref, t.name.lower(), fq(t)) for t in tables]
    elif dialect == "oracle":
        # Oracle has no single bulk-export statement, and the two paths it
        # does have are not interchangeable:
        #
        #   DBMS_CLOUD.EXPORT_DATA  writes Parquet straight to object storage,
        #                           but ships only on Autonomous Database
        #   SQLcl SET SQLFORMAT csv works on EVERY edition including Free/XE,
        #                           and produces CSV
        #
        # Data Pump (expdp) is the usual Oracle answer and is useless here: a
        # .dmp is a proprietary format no cloud warehouse can COPY from.
        #
        # SQLcl is generated because it runs everywhere; the Autonomous form
        # rides along as a comment for estates that have it. The load side
        # follows this choice — see _source_writes_csv.
        head += [
            "-- Oracle has no single bulk-export statement. This is the SQLcl",
            "-- form, which works on every edition (including Free/XE).",
            "-- Run with `sql` (SQLcl), NOT sqlplus — sqlplus has no",
            "-- SET SQLFORMAT csv.",
            "--",
            "-- Data Pump (expdp) is NOT used: a .dmp file is proprietary and",
            "-- no cloud warehouse can load one.",
            "--",
            "-- On Autonomous Database, prefer Parquet straight to object",
            "-- storage (and set the load side to PARQUET to match):",
            "--   BEGIN DBMS_CLOUD.EXPORT_DATA(",
            "--     credential_name => '<credential>',",
            "--     file_uri_list   => '%s/<table>/'," % uri,
            "--     format          => JSON_OBJECT('type' VALUE 'parquet'),",
            "--     query           => 'SELECT * FROM <schema>.<table>');",
            "--   END;",
            "--   /",
            "",
            "SET SQLFORMAT csv",
            "SET FEEDBACK OFF",
            "SET HEADING ON",
            "SET TERMOUT OFF",
            "",
            "-- Pin the session's text formats before a single row is",
            "-- written. CSV carries no types, so whatever these say IS the",
            "-- data — and they default to the SERVER's locale, not to",
            "-- anything this script controls.",
            "--",
            "-- NLS_DATE_FORMAT defaults to DD-MON-RR on most installs, and",
            "-- RR is a two-digit year: 1959 and 2059 both spool as '59',",
            "-- and the load resolves them by rule, not by fact. A date of",
            "-- birth in that window arrives off by a century with no error",
            "-- on either side. ISO-8601 has no such window.",
            "--",
            "-- NLS_NUMERIC_CHARACTERS is the same trap for numbers: a",
            "-- comma-decimal locale spools 1234,56, which a CSV reader",
            "-- splits into two fields.",
            "ALTER SESSION SET NLS_DATE_FORMAT = "
            "'%s';" % _ISO_DATE_ORACLE,
            "ALTER SESSION SET NLS_TIMESTAMP_FORMAT = "
            "'%s';" % _ISO_TIMESTAMP_ORACLE,
            "ALTER SESSION SET NLS_TIMESTAMP_TZ_FORMAT = "
            "'%s';" % _ISO_TIMESTAMP_TZ_ORACLE,
            "ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '.,';",
            "",
        ]
        # One file per table, in its own folder: step 3 loads FROM
        # '<uri>/<table>/' as a PREFIX, so a file written beside that prefix
        # rather than inside it is invisible to the load.
        local = _ORACLE_LOCAL_EXPORT_DIR
        body = []
        for t in tables:
            low = t.name.lower()
            # SCHEMA.TABLE, never fq(): on Oracle the PDB is the CONNECTION,
            # not a name part. `FREEPDB1.RAW_SCHEMA.ORDERS` does not address
            # the table — Oracle reads it as schema FREEPDB1, table
            # RAW_SCHEMA, column ORDERS, and every line fails ORA-00942.
            body.append("SPOOL %s/%s/%s.csv\nSELECT * FROM %s;\nSPOOL OFF"
                        % (local, low, low,
                           _qualified("oracle", t.schema, t.name)))
        # SPOOL is a CLIENT-side file write with no object-storage driver, so
        # an Oracle export always lands locally first. Shelling out to the AWS
        # CLI from the same script keeps it a single run — the same trade the
        # Postgres branch makes with `\copy ... TO PROGRAM`.
        upload = ("aws s3 cp %s %s/ --recursive --exclude '*' --include '*.csv'"
                  % (local, uri))
        head += ["-- Written locally first: SQLcl SPOOL writes a FILE, never to",
                 "-- a bucket. The upload runs from this script at the end.",
                 "--",
                 "-- Create the per-table folders first — SPOOL does not make",
                 "-- them, and a missing one fails that table silently:",
                 "--   POSIX:   mkdir -p %s/{%s}" % (
                     local, ",".join(t.name.lower() for t in tables)),
                 "--   Windows: for %%d in (%s) do mkdir %s\\%%d" % (
                     " ".join(t.name.lower() for t in tables),
                     local.replace("./", ".\\").replace("/", "\\")),
                 ""]
        # A placeholder URI would upload to a path that does not exist, so the
        # step is only wired up once a real stage URI is known.
        if "<" not in uri:
            tail_upload = ["", "-- Upload to object storage. Needs the AWS CLI",
                           "-- on this machine; without it, delete this line and",
                           "-- run the same command by hand afterwards.",
                           "HOST %s" % upload]
        else:
            tail_upload = ["", "-- Set a stage URI in Data movement settings and",
                           "-- this becomes a runnable upload step:",
                           "--   HOST %s" % upload]
        body = body + ["\n".join(tail_upload)]
    elif dialect == "teradata":
        # WRITE_NOS writes PARQUET straight to object storage, so Teradata
        # needs no local staging and no upload step — the same shape as
        # Snowflake's COPY INTO @stage and Oracle's DBMS_CLOUD.EXPORT_DATA.
        # It needs Vantage 17.00+ with NOS enabled; the delimited fallback for
        # older systems rides along as a comment, because neither BTEQ nor TPT
        # can write Parquet and moving to them means changing the LOAD too.
        #
        # The credential is a NAME, not a secret: the AUTHORIZATION object is
        # created once on Teradata, so no key material appears in this file.
        auth = mv["source_credential"] or "MB_OBJECT_STORE_AUTH"
        loc = _td_nos_location(uri)
        head += [
            "-- WRITE_NOS exports Parquet directly to object storage.",
            "-- Requires Vantage 17.00+ with Native Object Store enabled.",
            "--",
            "-- Create the AUTHORIZATION object ONCE, outside this file, so no",
            "-- key material is ever written here:",
            "--   CREATE AUTHORIZATION %s" % auth,
            "--     USER '<access-id>' PASSWORD '<secret-key>';",
            "--",
            "-- No NOS on this system? Export delimited text instead, with BTEQ",
            "-- (run `bteq < file`; .SET SEPARATOR needs TTU 16.10+):",
            "--   .LOGON <host>/<user>,<password>",
            "--   .SET WIDTH 65531",
            "--   .SET TITLEDASHES OFF",
            "--   .SET SEPARATOR ','",
            "--   .EXPORT REPORT FILE = %s/<table>/<table>.csv"
            % _TD_LOCAL_EXPORT_DIR,
            "--   SELECT * FROM <database>.<table>;",
            "--   .EXPORT RESET",
            "-- or with TPT for volume: tbuild -f export.tpt, a DATACONNECTOR",
            "-- CONSUMER operator with Format = 'Delimited'.",
            "--",
            "-- Those write to DISK, so upload afterwards (one folder per",
            "-- table) AND switch 03_load_into_* to CSV, or the load reads",
            "-- Parquet that was never written:",
            "--   aws s3 cp %s %s/ --recursive --include '*.csv'"
            % (_TD_LOCAL_EXPORT_DIR, uri),
            "",
        ]
        # SCHEMA.TABLE, never fq(): a Teradata DATABASE *is* the schema.
        body = ["SELECT * FROM WRITE_NOS (\n"
                "  ON (SELECT * FROM %s)\n"
                "  USING\n"
                "    LOCATION('%s%s/')\n"
                "    AUTHORIZATION(%s)\n"
                "    STOREDAS('PARQUET')\n"
                ") AS export_%s;"
                % (_qualified("teradata", t.schema, t.name), loc,
                   t.name.lower(), auth, t.name.lower())
                for t in tables]
    elif dialect == "bigquery":
        body = ["EXPORT DATA OPTIONS(uri='%s/%s/*.parquet',\n"
                "  format='PARQUET', overwrite=true) AS\n"
                "SELECT * FROM %s;"
                % (uri, t.name.lower(),
                   _qualified("bigquery", t.schema, t.name))
                for t in tables]
    elif dialect == "redshift":
        role = mv["iam_role"] or "<iam-role-arn>"
        body = ["UNLOAD ('SELECT * FROM %s')\n  TO '%s/%s/'\n"
                "  IAM_ROLE '%s' FORMAT AS PARQUET ALLOWOVERWRITE;"
                % (_qualified("redshift", t.schema, t.name), uri,
                   t.name.lower(), role)
                for t in tables]
    elif dialect == "databricks":
        body = ["CREATE OR REPLACE TABLE delta.`%s/%s` AS "
                "SELECT * FROM %s;"
                % (uri, t.name.lower(),
                   _qualified("databricks", t.schema, t.name))
                for t in tables]
    elif dialect == "postgres":
        db = next((db_of(t) for t in tables if db_of(t)), "")
        head += ["-- psql meta-command: run with psql, not a SQL client."
                 + (" Connect to database '%s'." % db if db else ""), ""]
        # \copy cannot name a bucket as a FILE, but it can pipe to a
        # PROGRAM, and that program runs on the client where the AWS CLI
        # already is. `aws s3 cp - <uri>` reads stdin, so the rows stream
        # straight to object storage with nothing staged on disk and no
        # server-side privilege needed.
        #
        # The path is <uri>/<table>/<table>.csv, NOT <uri>/<table>.csv:
        # step 3 loads FROM '<uri>/<table>/', treating it as a prefix, and a
        # file written beside that prefix rather than inside it is invisible
        # to the load. The two halves have to agree on layout or the COPY
        # succeeds having read nothing.
        remote = "://" in uri
        if remote:
            head += [
                "-- \\copy streams straight to object storage by piping to "
                "the AWS CLI,",
                "-- which runs on the machine running psql — no local "
                "staging, and no",
                "-- server-side privilege. The CLI must be installed and "
                "authenticated.",
                "--",
                "-- No CLI? Write locally instead and upload afterwards, "
                "keeping the",
                "-- per-table folders so step 3 still finds the files:",
                "--   TO '%s/<table>/<table>.csv'" % _PG_LOCAL_EXPORT_DIR,
                "--   aws s3 cp %s %s/ --recursive"
                % (_PG_LOCAL_EXPORT_DIR, uri),
                ""]
            body = ["\\copy (SELECT * FROM %s) TO PROGRAM "
                    "'aws s3 cp - %s/%s/%s.csv' "
                    "WITH (FORMAT csv, HEADER true);"
                    % (_qualified("postgres", t.schema, t.name), uri,
                       t.name.lower(), t.name.lower())
                    for t in tables]
        else:
            body = ["\\copy (SELECT * FROM %s) TO '%s/%s.csv' "
                    "WITH (FORMAT csv, HEADER true);"
                    % (_qualified("postgres", t.schema, t.name), uri,
                       t.name.lower())
                    for t in tables]
    else:
        name = spec.name if spec is not None else "the source"
        head += ["-- %s has no generated bulk-export form in MetaBridge yet."
                 % name,
                 "-- Export each table below to Parquet with the platform's",
                 "-- own unload/extract utility, one folder per table.", ""]
        body = ["-- %s -> %s/%s/" % (fq(t), uri, t.name.lower())
                for t in tables]
    return "\n".join(head) + "\n" + "\n\n".join(body) + "\n"


def _source_writes_csv(source: Optional[ConnectorSpec]) -> bool:
    """Whether step 2 produced CSV rather than Parquet.

    Every entry is a fact about what _unload() actually emits, not a
    preference: SAP HANA's EXPORT INTO has no Parquet form, PostgreSQL's
    `\\copy ... WITH (FORMAT csv)` is CSV by construction, and Oracle's only
    export path available on every edition is SQLcl's `SET SQLFORMAT csv`
    (DBMS_CLOUD.EXPORT_DATA writes Parquet but ships only on Autonomous, so
    it cannot be the generated default). A load that reads Parquet from any
    of them fails on the first row.

    Everything else here unloads Parquet, so the default stays Parquet —
    which is also the better format when a source can produce it, since it
    carries its own types instead of re-inferring them at load time.
    """
    return source is not None and source.key in ("sap_hana", "postgres",
                                                 "oracle")


def snowflake_stage_setup(stage: str, uri: str, configured: bool) -> List[str]:
    """README section: the one-time CREATE STAGE the Snowflake load needs.

    It lives in the README rather than in `03_load_into_snowflake.sql`
    because that file must stay free of both a bucket URL and the word
    CREDENTIALS — the named-stage form is the only load that writes neither,
    and putting the stage's own DDL beside it would hand back exactly what
    the setting removed. Creating a stage is also a one-time act; a statement
    sitting in a per-load script gets re-run and silently repoints it."""
    name = stage or "MB_LANDING_STAGE"
    url = "%s/" % uri.rstrip("/")
    out = ["## Create the load stage (one-time, on Snowflake)", "",
           "`%s` is read by `03_load_into_snowflake.sql`. Naming a stage "
           "does not create it — run this once per target:" % name, "",
           "```sql",
           "-- preferred: the credential lives inside Snowflake, so no key",
           "-- is written into any file here",
           "CREATE STAGE %s" % name,
           "  URL = '%s'" % url,
           "  STORAGE_INTEGRATION = <integration>;",
           "",
           "-- without an integration (key stored in Snowflake, still not "
           "in these files)",
           "CREATE STAGE %s" % name,
           "  URL = '%s'" % url,
           "  CREDENTIALS = (AWS_KEY_ID='<key>' AWS_SECRET_KEY='<secret>');",
           "```", ""]
    if not configured:
        out += ["Then set **Named target stage** to `%s` in Data movement "
                "settings and regenerate: every `CREDENTIALS` clause in the "
                "load disappears and the file runs as generated." % name, ""]
    return out


def _load(spec: Optional[ConnectorSpec], dialect: str,
          tables: List[SourceTable],
          movement: Optional[dict] = None,
          source: Optional[ConnectorSpec] = None) -> str:
    """Target-side bulk import from the same object-storage layout.

    ``source`` is consulted only for the FILE FORMAT: a load that reads
    Parquet from a CSV export fails at the first row, so the two halves must
    agree about what step 2 actually wrote."""
    mv = _mv(movement)
    csv_src = _source_writes_csv(source)
    uri = mv["stage_uri"] or "<stage-uri>"
    role = mv["iam_role"] or "<iam-role-arn>"
    head = ["-- Step 3: load each table from object storage into the "
            "target.",
            "-- Run AFTER 01_create_landing.sql. Run on the TARGET.", ""]
    if dialect == "redshift":
        body = ["COPY %s\n  FROM '%s/%s/'\n"
                "  IAM_ROLE '%s'\n  FORMAT AS PARQUET;"
                % (_qualified("redshift", t.schema, t.name), uri,
                   t.name.lower(), role)
                for t in tables]
    elif dialect == "snowflake":
        if csv_src:
            # MATCH_BY_COLUMN_NAME is a semi-structured feature; CSV loads
            # positionally instead. 01_create_landing.sql emits columns in
            # source order precisely so that works. SKIP_HEADER pairs with
            # the exporter's header row.
            # DATE_FORMAT/TIMESTAMP_FORMAT are stated rather than left to
            # their defaults — which are these values, but silently. AUTO
            # infers a format per file, so it is a guess exactly as far as
            # the input is ambiguous: DD-MON-RR would be read by rule, and
            # a 1959 date of birth would land in 2059 with no error raised
            # anywhere. Step 2 pins its exporter to ISO-8601 with a
            # four-digit year, which leaves AUTO nothing to guess at.
            # ON_ERROR is stated for the same reason: it is the
            # default, but a load that stops on a bad row and one
            # that skips it are different operations, and the file
            # should not leave the reader to know which.
            fmt = ("  FILE_FORMAT = (TYPE = CSV "
                   "FIELD_OPTIONALLY_ENCLOSED_BY = '\"' SKIP_HEADER = 1\n"
                   "                 DATE_FORMAT = AUTO "
                   "TIMESTAMP_FORMAT = AUTO)\n"
                   "  ON_ERROR = ABORT_STATEMENT;")
        else:
            fmt = ("  FILE_FORMAT = (TYPE = PARQUET)\n"
                   "  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;")
        if mv["target_stage"]:
            # A stage built on a STORAGE INTEGRATION holds its own
            # credential inside Snowflake, so the statement carries none —
            # this is the only form here that never puts a key in a file.
            head += ["-- Loading through the named stage from your Data "
                     "movement settings:",
                     "-- its storage credential lives in Snowflake, so no "
                     "key appears below.", ""]
            src_ref = ["@%s/%s/" % (mv["target_stage"], t.name.lower())
                       for t in tables]
            cred_line = ""
        else:
            src_ref = ["'%s/%s/'" % (uri, t.name.lower()) for t in tables]
            # Spell the clause out rather than hiding it behind one opaque
            # token. `(<credentials>)` is not valid SQL and says nothing about
            # what replaces it, so pasting the file gives a syntax error with
            # no clue — Redshift's `IAM_ROLE '<iam-role-arn>'` right above has
            # always been self-describing; this now matches.
            cred_line = ("  CREDENTIALS = (AWS_KEY_ID='<aws-key-id>' "
                         "AWS_SECRET_KEY='<aws-secret-key>')\n")
        body = ["COPY INTO %s\n  FROM %s\n%s%s"
                % (_qualified("snowflake", t.schema, t.name), ref,
                   cred_line, fmt)
                for t, ref in zip(tables, src_ref)]
    elif dialect == "databricks":
        body = ["COPY INTO %s\n  FROM '%s/%s/'\n"
                "  FILEFORMAT = %s\n"
                "  %s;"
                % (_qualified("databricks", t.schema, t.name), uri,
                   t.name.lower(), "CSV" if csv_src else "PARQUET",
                   "FORMAT_OPTIONS ('header' = 'true')" if csv_src
                   else "COPY_OPTIONS ('mergeSchema' = 'true')")
                for t in tables]
    elif dialect == "bigquery":
        body = ["LOAD DATA INTO %s\n  FROM FILES(format = 'PARQUET',\n"
                "    uris = ['%s/%s/*.parquet']);"
                % (_qualified("bigquery", t.schema, t.name), uri,
                   t.name.lower())
                for t in tables]
    elif dialect == "postgres":
        head += ["-- psql meta-command: run with psql, not a SQL client.",
                 ""]
        body = ["\\copy %s FROM '%s/%s.csv' "
                "WITH (FORMAT csv, HEADER true);"
                % (_qualified("postgres", t.schema, t.name), uri,
                   t.name.lower())
                for t in tables]
    elif dialect == "tsql":
        body = ["COPY INTO %s\n  FROM '%s/%s/'\n"
                "  WITH (FILE_TYPE = 'PARQUET', "
                "CREDENTIAL = (<credentials>));"
                % (_qualified("tsql", t.schema, t.name), uri,
                   t.name.lower())
                for t in tables]
    else:
        name = spec.name if spec is not None else "the target"
        head += ["-- %s has no generated bulk-load form in MetaBridge yet."
                 % name,
                 "-- Load each table below with the platform's own utility.",
                 ""]
        body = ["-- %s/%s/ -> %s"
                % (uri, t.name.lower(),
                   _qualified(dialect, t.schema, t.name))
                for t in tables]
    return "\n".join(head) + "\n" + "\n\n".join(body) + "\n"


def _readme(pipeline: Pipeline, source: Optional[ConnectorSpec],
            target: Optional[ConnectorSpec], dialect: str,
            tables: List[SourceTable], files: List[str],
            placeholders: List[str], prefilled: List[str],
            movement: Optional[dict] = None) -> str:
    s_name = source.name if source is not None else "the source"
    t_name = target.name if target is not None else "the target"
    lines = [
        "# Landing layer — %s to %s" % (s_name, t_name), "",
        "The dbt project in `../dbt` transforms data **inside %s**. It does "
        "not read %s. These scripts create the landing tables and move the "
        "data, so the dbt models have something to select from."
        % (t_name, s_name), "",
        "Run in order:", "",
    ]
    why = {
        "00_probe_string_widths.sql":
            "*(optional, run on %s)* measure real column sizes, then pin "
            "them in the manifest" % s_name,
        "01_create_landing.sql":
            "on %s — schemas + %d typed landing table(s) in one file"
            % (t_name, len(tables)),
    }
    for i, f in enumerate(files, 1):
        if f == "README.md":
            continue
        label = why.get(f, ("export from %s" % s_name) if "unload" in f
                        else ("load into %s" % t_name))
        lines.append("%d. `%s` — %s" % (i, f, label))
    lines += [
        "", "Then:", "",
        "```bash",
        "cd ../dbt",
        "dbt debug   --profiles-dir .   # connection",
        "dbt compile --profiles-dir .   # check the resolved FROM clauses",
        "dbt run     --profiles-dir .",
        "```", "",
    ]
    if prefilled:
        lines += ["## Pre-filled from your Data movement settings", "",
                  "Set once in Pipeline Studio, substituted everywhere: "
                  + ", ".join("`%s`" % x for x in prefilled) + ".", ""]
    if placeholders:
        token_note = {
            "<stage-uri>": "object-storage prefix both sides can reach "
                           "(e.g. `s3://my-bucket/mb`) — set it once in "
                           "Pipeline Studio's Data movement settings",
            "<iam-role-arn>": "role the target warehouse assumes for the "
                              "bulk load — set it once in Data movement "
                              "settings",
            "<credentials>": "platform credential clause. For Snowflake, "
                             "create a NAMED STAGE once and set it in Data "
                             "movement settings — then no credential "
                             "appears here at all",
            "<aws-key-id>": "AWS access key ID with read access to the "
                            "bucket. Setting a named target stage removes "
                            "this clause entirely",
            "<aws-secret-key>": "the matching AWS secret access key — "
                                "substitute it in the file you run, never "
                                "in the one you keep",
            "<database>": "the database to work in. In "
                          "`02_unload_*` it is the SOURCE database to "
                          "unload from; in `01_create_landing.sql` it is "
                          "the TARGET database to create into. Both come "
                          "from the connection you select — generate with "
                          "one connected and this token does not appear",
            "<catalog>": "the target Unity Catalog to create into — comes "
                         "from the target connection",
            "<warehouse>": "source warehouse (an unload needs compute) — "
                           "comes from the source connection",
            "<region>": "the bucket's AWS region, e.g. `ap-south-1`. SAP "
                        "HANA puts it in the URI scheme "
                        "(`s3-ap-south-1://`) where the target takes a "
                        "plain `s3://`, and it cannot be inferred from the "
                        "bucket name",
        }
        lines += ["## Substitute before running", "",
                  "| token | meaning |", "|---|---|"]
        for t in placeholders:
            lines.append("| `%s` | %s |" % (t, token_note.get(t, "")))
        lines += ["", "No credential is written into these files.", ""]
    else:
        lines += ["## Nothing to substitute", "",
                  "Every location and role was filled from your Data "
                  "movement settings. No credential is written into these "
                  "files.", ""]
    # The load reads from a stage the reader has to create first; without
    # this the first run of any bundle stops at "Stage does not exist" with
    # no statement anywhere in the package to fix it.
    if target is not None and target.dialect == "snowflake":
        mv_doc = _mv(movement)
        lines += snowflake_stage_setup(mv_doc["target_stage"],
                                       mv_doc["stage_uri"] or "<stage-uri>",
                                       bool(mv_doc["target_stage"]))
    lines += [
        "## Types here vs. types in `sources.yml`", "",
        "`sources.yml` documents each column's logical type exactly as the "
        "source declares it (`decimal(9,0)`). The CREATE TABLE may spell the "
        "same type differently. dbt neither creates nor validates source "
        "columns from `data_type`, so nothing depends on the two strings "
        "being identical.", "",
    ]
    if dialect in _INT_NARROWING:
        i32, i64 = _INT_NARROWING[dialect]
        lines += [
            "A scale-0 numeric becomes the narrowest exact integer that "
            "holds it — `decimal(9,0)` becomes `%s`, `decimal(18,0)` "
            "becomes `%s` — because a native integer is materially cheaper "
            "to join and distribute on than a decimal, and the narrowing is "
            "loss-free in that direction. Above 18 digits the decimal "
            "stays." % (i32, i64), "",
        ]
    else:
        lines += [
            "Integers are NOT narrowed on %s: `INT` and `BIGINT` are "
            "aliases here, so narrowing would gain nothing and discard the "
            "declared precision." % dialect, "",
        ]
    lines += [
        "A numeric the source declares with NO precision at all (Oracle's "
        "bare `NUMBER`) has no precision to preserve, so it lands on the "
        "documented widest-exact fallback rather than a guess. Every column "
        "that took it is raised as `NUMERIC_PRECISION_FALLBACK` — narrow "
        "them by hand where the real range is known, because the fallback "
        "is wider and more expensive than any real column needs.", "",
        "## Nullability", "",
        "`NOT NULL` is carried through from the manifest, so a load that "
        "would put a null in a column the source rejects fails here rather "
        "than downstream. Columns the manifest does not mark are created "
        "nullable — absence of a constraint in the manifest is not evidence "
        "of one in the source.", "",
        "## Schemas", "",
        "The landing tables keep the source schema names, because that is "
        "what the generated `sources.yml` references — so the dbt models "
        "resolve unchanged. To land somewhere else, change both together.",
        "",
        "This file also creates the schemas the dbt MODELS build into, "
        "which are the target schemas the source estate declared. dbt does "
        "not create a schema it was not pointed at, and it derives nothing "
        "from a folder name — `dbt_project.yml` carries a `+schema:` per "
        "folder and `macros/generate_schema_name.sql` makes dbt take those "
        "names literally instead of concatenating them onto the profile's "
        "schema.", "",
    ]
    if dialect == "redshift":
        lines += [
            "## Redshift physical design", "",
            "`DISTKEY`/`SORTKEY` are inferred from the manifest: the "
            "`unique_key` becomes the distribution key (co-locating the "
            "rows a merge has to match) and the `incremental_column` "
            "becomes the sort key (so watermark range scans skip blocks). "
            "Tables with neither get `DISTSTYLE AUTO`.", "",
            "This is inference from declared intent, not from measured data "
            "volumes or query patterns — review it against real workloads "
            "before it matters.", "",
        ]
    return "\n".join(lines)


def generate_target_ddl(pipeline: Pipeline, out_dir: str,
                        source: Optional[ConnectorSpec] = None,
                        target: Optional[ConnectorSpec] = None,
                        dialect: str = "",
                        source_params: Optional[Dict[str, str]] = None,
                        movement: Optional[dict] = None,
                        target_params: Optional[Dict[str, str]] = None) -> dict:
    """Write the landing-layer DDL + movement scripts. -> file manifest.

    ``movement`` carries the workspace Data movement settings; whatever it
    provides is substituted into every statement so the scripts come out
    ready-to-run, not ready-to-edit."""
    dialect = (dialect or str(pipeline.metadata.get("dialect", "")
                              or "")).lower()
    tables = [s for s in pipeline.sources
              if s.columns and any(c.name != "ROW_DATA" for c in s.columns)]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    skipped = [s.name for s in pipeline.sources if s not in tables]
    if not tables:
        return {"files": [], "tables": 0, "skipped": skipped}

    hints = _load_hints(pipeline)
    mv = _mv(movement)
    files: List[str] = []

    def src_of(spec: Optional[ConnectorSpec], fallback: str) -> str:
        """The SOURCE's dialect, never the target's.

        A connector that declares no dialect is not SQL-addressable (every
        SAP connector, and anything reached by upload). Inheriting the
        TARGET's dialect there produced an 02_unload_from_<src>.sql written
        in Snowflake/Databricks syntax but addressed to a SAP system, which
        cannot run it — and told the type engine the source platform was
        Snowflake, so SAP native types were parsed against Snowflake's
        vocabulary. Returning "" keeps both honest: the unload falls to the
        documented "use the platform's own utility" form, and the type
        engine parses against ANSI. The fallback still applies when NO
        source spec was passed at all, where the caller's dialect is the
        only thing known."""
        if spec is not None:
            return spec.dialect
        return fallback

    src_dialect = src_of(source, dialect)
    probe = _probe_widths(source, src_dialect, tables)
    if probe:
        (out / "00_probe_string_widths.sql").write_text(probe,
                                                        encoding="utf-8")
        files.append("00_probe_string_widths.sql")

    # schemas + tables in ONE file: one connection, one run, in order
    schemas = sorted({s.schema for s in tables if s.schema})
    # The schemas dbt BUILDS into are not the schemas we land into, and until
    # they exist the first `dbt run` fails on the first model. They are the
    # target schemas the estate itself declared, so an estate that separated
    # RAW / SILVER / GOLD gets that separation back rather than one flat
    # schema. (dbt does not create a schema it was not pointed at, and it
    # derives nothing from a folder name.)
    from .dbt_naming import target_schema as _model_schema
    built = sorted({_model_schema(m) for m in pipeline.mappings
                    if _model_schema(m)} - set(schemas))
    body = ["-- Step 1: landing schemas + tables, typed from the source "
            "metadata.",
            "-- Column names and order match the source, so a bulk load",
            "-- maps positionally as well as by name.", ""]
    body += _target_context(dialect, target_params)
    if schemas:
        body += ["-- schemas the dbt sources.yml expects "
                 "(needs CREATE SCHEMA privilege):",
                 _create_schemas(dialect, schemas)]
    if built:
        body += ["-- schemas the dbt MODELS build into, from the target "
                 "schemas the source estate declared:",
                 _create_schemas(dialect, built)]
    for s_ in tables:
        body.append("-- %s (%s)" % (
            _qualified(dialect, s_.schema, s_.name),
            hints.get(s_.name, {}).get("strategy",
                                       LoadStrategy.FULL.value)))
        body.append(_create_table(s_, dialect, hints.get(s_.name, {}),
                                  src_dialect))
        body.append("")
    (out / "01_create_landing.sql").write_text("\n".join(body),
                                               encoding="utf-8")
    files.append("01_create_landing.sql")

    un = "02_unload_from_%s.sql" % (source.key if source is not None
                                    else "source")
    un_text = _unload(source, src_dialect, tables, source_params, movement)
    (out / un).write_text(un_text, encoding="utf-8")
    files.append(un)
    ld = "03_load_into_%s.sql" % (target.key if target is not None
                                  else "target")
    ld_text = _load(target, dialect, tables, movement, source=source)
    (out / ld).write_text(ld_text, encoding="utf-8")
    files.append(ld)

    # honest bookkeeping for the README and the UI: what came pre-filled,
    # what still needs a hand. The LANDING text is scanned too — it now
    # carries session context, and a <database> left unfilled there is just
    # as much a substitution as one in the unload.
    both = "\n".join(body) + un_text + ld_text
    placeholders = [t for t in ("<stage-uri>", "<iam-role-arn>",
                                "<credentials>", "<aws-key-id>",
                                "<aws-secret-key>", "<database>",
                                "<catalog>", "<warehouse>",
                                "<region>") if t in both]
    prefilled = []
    if mv["stage_uri"]:
        prefilled.append("stage URI")
    if mv["iam_role"] and "<iam-role-arn>" not in both:
        prefilled.append("IAM role")
    if mv["source_stage"]:
        prefilled.append("named source stage")
    if mv["region"] and "<region>" not in both:
        prefilled.append("bucket region")
    # Scoped to the UNLOAD text on purpose: the source credential is a
    # source-side fact, and the target's own <credentials> lives in the load
    # file. Checking both would report the source one as unfilled whenever
    # the target still needs one.
    if mv["source_credential"] and "<credentials>" not in un_text:
        prefilled.append("named source credential")
    if mv["target_stage"] and "<credentials>" not in ld_text:
        prefilled.append("named target stage")

    (out / "README.md").write_text(
        _readme(pipeline, source, target, dialect, tables, files,
                placeholders, prefilled, movement),
        encoding="utf-8")
    files.append("README.md")

    # Declared, never silent: every place a width had to be invented.
    wide = [s_.name for s_ in tables
            if any(c.datatype == "string" and not c.precision
                   for c in s_.columns)]
    if wide and dialect not in _UNBOUNDED_STRING:
        width = _VARCHAR_FALLBACK.get(dialect, 0)
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.WARNING, code="STRING_WIDTH_FALLBACK",
            message="%d landing table(s) have text column(s) with no "
                    "declared length — DDL uses the documented default "
                    "%s(%d)"
                    % (len(wide), _BY_DIALECT.get(dialect, {}).get(
                        "string", _ANSI["string"]), width),
            obj=", ".join(wide[:8]),
            suggestion="Re-introspect the live source (widths are measured "
                       "automatically when the table is small enough), or "
                       "run ddl/00_probe_string_widths.sql and put the real "
                       "lengths in the manifest. A value longer than the "
                       "default makes the bulk load fail loudly rather "
                       "than truncate."))
    cap = 0 if dialect in _NO_STRING_LENGTH else _MAX_STRING.get(dialect, 0)
    clamped = [s_.name for s_ in tables
               if cap and any(c.datatype == "string" and c.precision > cap
                              for c in s_.columns)]
    if clamped:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.WARNING, code="STRING_WIDTH_CLAMPED",
            message="%d table(s) declare text column(s) wider than %s "
                    "allows (%d) — the DDL uses %s"
                    % (len(clamped), dialect, cap,
                       _OVER_MAX_STRING.get(dialect, "the maximum width")),
            obj=", ".join(clamped[:8]),
            suggestion="Check no value actually exceeds %d characters, or "
                       "the bulk load will reject those rows." % cap))

    wide_int: List[str] = []
    for t in tables:
        if any(c.datatype == "decimal" and not (c.scale or 0)
               and c.precision > _INT64_DIGITS for c in t.columns):
            wide_int.append(t.name)
    if wide_int and dialect in _INT_NARROWING:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.INFO, code="WIDE_INTEGER_KEPT_DECIMAL",
            message="%d table(s) have whole-number column(s) declared wider "
                    "than %d digits, so the DDL keeps them DECIMAL rather "
                    "than %s" % (len(wide_int), _INT64_DIGITS,
                                 _INT_NARROWING[dialect][1]),
            obj=", ".join(wide_int[:8]),
            suggestion="Snowflake's INT/BIGINT are aliases of NUMBER(38,0), "
                       "so its integers always report 38 digits. Re-"
                       "introspect the live source (ranges are measured "
                       "automatically when the table is small enough): if "
                       "the real maximum fits 18 digits the column becomes "
                       "a native integer — materially cheaper as a join or "
                       "distribution key."))
    if skipped:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.WARNING, code="DDL_NO_COLUMN_METADATA",
            message="%d table(s) have no column metadata, so no landing "
                    "DDL could be generated for them" % len(skipped),
            obj=", ".join(skipped[:8]),
            suggestion="Add columns to the manifest, or introspect the "
                       "live system, then regenerate."))
    return {"files": files, "tables": len(tables), "skipped": skipped,
            "dialect": dialect, "placeholders": placeholders,
            "movement_prefilled": prefilled}

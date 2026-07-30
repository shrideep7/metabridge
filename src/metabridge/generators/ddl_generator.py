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
_DECIMAL_FALLBACK = (38, 6)
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


def _type_of(port: Port, dialect: str) -> str:
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
    """Leave identifiers bare. Redshift/Postgres fold unquoted names to
    lower case and dbt resolves them the same way, so quoting here (and not
    there) is how a migration ends up with UPPER and lower twins of every
    table."""
    return ident


def _qualified(dialect: str, schema: str, name: str) -> str:
    return "%s.%s" % (_quote(dialect, schema), _quote(dialect, name)) \
        if schema else _quote(dialect, name)


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


def _create_schemas(dialect: str, schemas: List[str]) -> str:
    """``CREATE SCHEMA IF NOT EXISTS`` is not universal: T-SQL has no IF NOT
    EXISTS on CREATE SCHEMA, and an Oracle schema IS a user."""
    if dialect == "tsql":
        return "".join(
            "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '%s')\n"
            "    EXEC('CREATE SCHEMA %s');\n" % (s, s) for s in schemas)
    if dialect == "oracle":
        return "".join(
            "-- Oracle: a schema is a user. Run as a DBA and grant a quota.\n"
            "-- CREATE USER %s IDENTIFIED BY \"<password>\" "
            "QUOTA UNLIMITED ON USERS;\n" % s for s in schemas)
    return "".join("CREATE SCHEMA IF NOT EXISTS %s;\n" % s for s in schemas)


def _probe_widths(spec: Optional[ConnectorSpec], dialect: str,
                  tables: List[SourceTable]) -> str:
    """Measure the real width of every text column whose length the source
    metadata does not declare. Run on the SOURCE; feed the answers back into
    the manifest so the landing DDL stops guessing."""
    parts: List[str] = []
    fn = "LEN" if dialect == "tsql" else "LENGTH"
    for t in tables:
        measures = ["       MAX(%s(%s)) AS %s_len" % (fn, c.name, c.name[:110])
                    for c in t.columns
                    if c.datatype == "string" and not c.precision]
        # an integral column too wide to narrow: measure it so a 128-bit
        # DECIMAL(38,0) key can become a BIGINT on evidence
        measures += ["       MAX(ABS(%s)) AS %s_max" % (c.name, c.name[:110])
                     for c in t.columns
                     if c.datatype == "decimal" and not (c.scale or 0)
                     and c.precision > _INT64_DIGITS]
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
            "--   *_max   integral columns wider than 18 digits, so they had\n"
            "--           to stay DECIMAL. If the real maximum fits in\n"
            "--           9.2e18, declare them NUMBER(18,0) and they become\n"
            "--           BIGINT — much cheaper to join and distribute on.\n\n"
            + "\n\n".join(parts) + "\n")


def _create_table(src: SourceTable, dialect: str, hint: dict) -> str:
    cols = ",\n".join("    %-32s %s" % (_quote(dialect, c.name),
                                        _type_of(c, dialect))
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

def _mv(movement: Optional[dict]) -> dict:
    m = movement or {}
    return {"stage_uri": str(m.get("stage_uri", "") or "").rstrip("/"),
            "iam_role": str(m.get("iam_role", "") or "").strip(),
            "source_stage":
                str(m.get("source_stage", "") or "").strip().lstrip("@")}


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
        return "%s.%s" % (d, q) if d else q

    if dialect == "snowflake":
        db = next((db_of(t) for t in tables if db_of(t)), "")
        wh = str(sp.get("warehouse", "") or "")
        head += ["-- Context is set explicitly so this runs from any "
                 "worksheet/session:",
                 "USE DATABASE %s;" % db if db
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


def _load(spec: Optional[ConnectorSpec], dialect: str,
          tables: List[SourceTable],
          movement: Optional[dict] = None) -> str:
    """Target-side bulk import from the same object-storage layout."""
    mv = _mv(movement)
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
        body = ["COPY INTO %s\n  FROM '%s/%s/'\n"
                "  CREDENTIALS = (<credentials>)\n"
                "  FILE_FORMAT = (TYPE = PARQUET)\n"
                "  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;"
                % (_qualified("snowflake", t.schema, t.name), uri,
                   t.name.lower())
                for t in tables]
    elif dialect == "databricks":
        body = ["COPY INTO %s\n  FROM '%s/%s/'\n"
                "  FILEFORMAT = PARQUET\n"
                "  COPY_OPTIONS ('mergeSchema' = 'true');"
                % (_qualified("databricks", t.schema, t.name), uri,
                   t.name.lower())
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
            placeholders: List[str], prefilled: List[str]) -> str:
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
            "<database>": "source database to unload from",
            "<warehouse>": "source warehouse (an unload needs compute)",
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
    lines += [
        "## Types here vs. types in `sources.yml`", "",
        "`sources.yml` documents each column's logical type exactly as the "
        "source declares it (`decimal(9,0)`). `01_create_landing.sql` "
        "creates the narrowest exact type that holds it (`INTEGER`) — same "
        "values, different notation, chosen because a native integer is "
        "materially cheaper to join and distribute on than a decimal. dbt "
        "neither creates nor validates source columns from `data_type`, so "
        "nothing depends on the two strings being identical.", "",
        "## Why the schema names match the source", "",
        "The landing tables keep the source schema names, because that is "
        "what the generated `sources.yml` references — so the dbt models "
        "resolve unchanged. To land somewhere else, change both together.",
        "",
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
                        movement: Optional[dict] = None) -> dict:
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
        return (spec.dialect if spec is not None else "") or fallback

    src_dialect = src_of(source, dialect)
    probe = _probe_widths(source, src_dialect, tables)
    if probe:
        (out / "00_probe_string_widths.sql").write_text(probe,
                                                        encoding="utf-8")
        files.append("00_probe_string_widths.sql")

    # schemas + tables in ONE file: one connection, one run, in order
    schemas = sorted({s.schema for s in tables if s.schema})
    body = ["-- Step 1: landing schemas + tables, typed from the source "
            "metadata.",
            "-- Column names and order match the source, so a bulk load",
            "-- maps positionally as well as by name.", ""]
    if schemas:
        body += ["-- schemas the dbt sources.yml expects "
                 "(needs CREATE SCHEMA privilege):",
                 _create_schemas(dialect, schemas)]
    for s_ in tables:
        body.append("-- %s (%s)" % (
            _qualified(dialect, s_.schema, s_.name),
            hints.get(s_.name, {}).get("strategy",
                                       LoadStrategy.FULL.value)))
        body.append(_create_table(s_, dialect, hints.get(s_.name, {})))
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
    ld_text = _load(target, dialect, tables, movement)
    (out / ld).write_text(ld_text, encoding="utf-8")
    files.append(ld)

    # honest bookkeeping for the README and the UI: what came pre-filled,
    # what still needs a hand
    both = un_text + ld_text
    placeholders = [t for t in ("<stage-uri>", "<iam-role-arn>",
                                "<credentials>", "<database>",
                                "<warehouse>") if t in both]
    prefilled = []
    if mv["stage_uri"]:
        prefilled.append("stage URI")
    if mv["iam_role"] and "<iam-role-arn>" not in both:
        prefilled.append("IAM role")
    if mv["source_stage"]:
        prefilled.append("named source stage")

    (out / "README.md").write_text(
        _readme(pipeline, source, target, dialect, tables, files,
                placeholders, prefilled),
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

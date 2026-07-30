"""Landing-layer DDL + bulk-load scripts for the target warehouse.

The gap this closes: a scaffold hands you a dbt project whose models read
``{{ source(...) }}``, and a dbt profile pointing at the TARGET warehouse.
dbt transforms inside one warehouse — it never moves data between two. So
until the source tables physically exist in the target, ``dbt run`` fails on
the first model with "relation does not exist".

This generator emits the missing extract-and-load layer:

    01_create_schemas.sql    the schemas the dbt sources.yml declares
    02_create_tables.sql     one typed CREATE TABLE per source table,
                             warehouse-native, with physical design
                             (Redshift DIST/SORT keys) inferred from the
                             merge key and watermark column
    03_unload_from_<src>.sql source-side bulk export to object storage
    04_load_into_<tgt>.sql   target-side bulk import
    README.md                run order and what to substitute

Schema names deliberately match the generated ``sources.yml`` so the dbt
models resolve with no edits — the two artifacts agree by construction.

Placeholders you must substitute are written as ``<angle-bracket>`` tokens
and listed in the README; nothing here embeds a credential.
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
               "bigint": "NUMBER(19)", "double": "BINARY_DOUBLE",
               "timestamp": "TIMESTAMP", "boolean": "NUMBER(1)",
               "binary": "BLOB"},
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


def _type_of(port: Port, dialect: str) -> str:
    base = dict(_ANSI)
    base.update(_BY_DIALECT.get(dialect, {}))
    canon = port.datatype or "string"
    sqlt = base.get(canon, base["string"])
    if canon == "decimal":
        p, sc = (port.precision, port.scale or 0) if port.precision \
            else _DECIMAL_FALLBACK
        return "%s(%d,%d)" % (base["decimal"], p, sc)
    if canon == "string":
        if port.precision:
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
    for t in tables:
        undeclared = [c.name for c in t.columns
                      if c.datatype == "string" and not c.precision]
        if not undeclared:
            continue
        fn = "LEN" if dialect == "tsql" else "LENGTH"
        cols = ",\n".join(
            "       MAX(%s(%s)) AS %s_len" % (fn, c, c[:110])
            for c in undeclared)
        parts.append("SELECT '%s' AS table_name,\n%s\nFROM %s;"
                     % (t.name, cols,
                        _qualified(dialect, t.schema, t.name)))
    if not parts:
        return ""
    return ("-- Step 0 (optional but recommended): measure real text widths.\n"
            "-- The source metadata declares no length for these columns, so\n"
            "-- 02_create_tables.sql used a documented default. Run this on\n"
            "-- the SOURCE, then put the real lengths in the manifest and\n"
            "-- regenerate — narrow columns keep Redshift/Synapse queries in\n"
            "-- memory instead of spilling to disk.\n\n"
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

def _unload(spec: Optional[ConnectorSpec], dialect: str,
            tables: List[SourceTable]) -> str:
    """Source-side export to object storage, in Parquet (typed, so the
    import does not have to re-guess every column)."""
    head = ["-- Step 3: export each source table to object storage.",
            "-- Substitute: <stage-uri>, <credentials>. Run on the SOURCE.",
            ""]
    if dialect == "snowflake":
        head += ["CREATE OR REPLACE FILE FORMAT mb_parquet TYPE = PARQUET;",
                 "CREATE OR REPLACE STAGE mb_unload",
                 "  URL = '<stage-uri>'                 "
                 "-- s3://bucket/prefix/",
                 "  CREDENTIALS = (<credentials>)",
                 "  FILE_FORMAT = mb_parquet;", ""]
        body = ["COPY INTO @mb_unload/%s/\n  FROM %s\n  "
                "HEADER = TRUE OVERWRITE = TRUE;"
                % (t.name.lower(),
                   _qualified("snowflake", t.schema, t.name))
                for t in tables]
    elif dialect == "bigquery":
        body = ["EXPORT DATA OPTIONS(uri='<stage-uri>/%s/*.parquet',\n"
                "  format='PARQUET', overwrite=true) AS\n"
                "SELECT * FROM %s;"
                % (t.name.lower(), _qualified("bigquery", t.schema, t.name))
                for t in tables]
    elif dialect == "redshift":
        body = ["UNLOAD ('SELECT * FROM %s')\n  TO '<stage-uri>/%s/'\n"
                "  IAM_ROLE '<iam-role-arn>' FORMAT AS PARQUET ALLOWOVERWRITE;"
                % (_qualified("redshift", t.schema, t.name), t.name.lower())
                for t in tables]
    elif dialect == "databricks":
        body = ["CREATE OR REPLACE TABLE delta.`<stage-uri>/%s` AS "
                "SELECT * FROM %s;"
                % (t.name.lower(), _qualified("databricks", t.schema, t.name))
                for t in tables]
    elif dialect == "postgres":
        head += ["-- psql meta-command: run with psql, not a SQL client.", ""]
        body = ["\\copy (SELECT * FROM %s) TO '<stage-uri>/%s.csv' "
                "WITH (FORMAT csv, HEADER true);"
                % (_qualified("postgres", t.schema, t.name), t.name.lower())
                for t in tables]
    else:
        name = spec.name if spec is not None else "the source"
        head += ["-- %s has no generated bulk-export form in MetaBridge yet."
                 % name,
                 "-- Export each table below to Parquet with the platform's",
                 "-- own unload/extract utility, one folder per table.", ""]
        body = ["-- %s -> <stage-uri>/%s/"
                % (_qualified(dialect, t.schema, t.name), t.name.lower())
                for t in tables]
    return "\n".join(head) + "\n" + "\n\n".join(body) + "\n"


def _load(spec: Optional[ConnectorSpec], dialect: str,
          tables: List[SourceTable]) -> str:
    """Target-side bulk import from the same object-storage layout."""
    head = ["-- Step 4: load each table from object storage into the target.",
            "-- Substitute: <stage-uri> and the credential token below.",
            "-- Run AFTER 02_create_tables.sql. Run on the TARGET.",
            ""]
    if dialect == "redshift":
        body = ["COPY %s\n  FROM '<stage-uri>/%s/'\n"
                "  IAM_ROLE '<iam-role-arn>'\n  FORMAT AS PARQUET;"
                % (_qualified("redshift", t.schema, t.name), t.name.lower())
                for t in tables]
    elif dialect == "snowflake":
        body = ["COPY INTO %s\n  FROM '<stage-uri>/%s/'\n"
                "  CREDENTIALS = (<credentials>)\n"
                "  FILE_FORMAT = (TYPE = PARQUET)\n"
                "  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;"
                % (_qualified("snowflake", t.schema, t.name), t.name.lower())
                for t in tables]
    elif dialect == "databricks":
        body = ["COPY INTO %s\n  FROM '<stage-uri>/%s/'\n"
                "  FILEFORMAT = PARQUET\n"
                "  COPY_OPTIONS ('mergeSchema' = 'true');"
                % (_qualified("databricks", t.schema, t.name), t.name.lower())
                for t in tables]
    elif dialect == "bigquery":
        body = ["LOAD DATA INTO %s\n  FROM FILES(format = 'PARQUET',\n"
                "    uris = ['<stage-uri>/%s/*.parquet']);"
                % (_qualified("bigquery", t.schema, t.name), t.name.lower())
                for t in tables]
    elif dialect == "postgres":
        head += ["-- psql meta-command: run with psql, not a SQL client.", ""]
        body = ["\\copy %s FROM '<stage-uri>/%s.csv' "
                "WITH (FORMAT csv, HEADER true);"
                % (_qualified("postgres", t.schema, t.name), t.name.lower())
                for t in tables]
    elif dialect == "tsql":
        body = ["COPY INTO %s\n  FROM '<stage-uri>/%s/'\n"
                "  WITH (FILE_TYPE = 'PARQUET', "
                "CREDENTIAL = (<credentials>));"
                % (_qualified("tsql", t.schema, t.name), t.name.lower())
                for t in tables]
    else:
        name = spec.name if spec is not None else "the target"
        head += ["-- %s has no generated bulk-load form in MetaBridge yet."
                 % name,
                 "-- Load each table below with the platform's own utility.",
                 ""]
        body = ["-- <stage-uri>/%s/ -> %s"
                % (t.name.lower(), _qualified(dialect, t.schema, t.name))
                for t in tables]
    return "\n".join(head) + "\n" + "\n\n".join(body) + "\n"


def _readme(pipeline: Pipeline, source: Optional[ConnectorSpec],
            target: Optional[ConnectorSpec], dialect: str,
            tables: List[SourceTable], files: List[str]) -> str:
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
            "*(optional, run on %s)* measure real text widths, then pin them "
            "in the manifest" % s_name,
        "01_create_schemas.sql":
            "on %s — create the schemas `sources.yml` declares" % t_name,
        "02_create_tables.sql":
            "on %s — create %d typed landing table(s)" % (t_name, len(tables)),
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
        "## Substitute before running", "",
        "| token | meaning |",
        "|---|---|",
        "| `<stage-uri>` | object-storage prefix both sides can reach "
        "(e.g. `s3://my-bucket/mb`) |",
        "| `<iam-role-arn>` | role the warehouse assumes to read/write it |",
        "| `<credentials>` | platform credential clause, if not using a role |",
        "", "No credential is written into these files.", "",
        "## Why the schema names match the source", "",
        "The landing tables keep the source schema names, because that is "
        "what the generated `sources.yml` references — so the dbt models "
        "resolve unchanged. To land somewhere else, change both together.", "",
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
                        dialect: str = "") -> dict:
    """Write the landing-layer DDL + movement scripts. -> file manifest."""
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
    files: List[str] = []

    def src_of(spec: Optional[ConnectorSpec], fallback: str) -> str:
        return (spec.dialect if spec is not None else "") or fallback

    schemas = sorted({s.schema for s in tables if s.schema})
    if schemas:
        (out / "01_create_schemas.sql").write_text(
            "-- Step 1: schemas the dbt sources.yml expects.\n"
            + _create_schemas(dialect, schemas), encoding="utf-8")
        files.append("01_create_schemas.sql")

    probe = _probe_widths(source, src_of(source, dialect), tables)
    if probe:
        (out / "00_probe_string_widths.sql").write_text(probe,
                                                        encoding="utf-8")
        files.insert(0, "00_probe_string_widths.sql")

    body = ["-- Step 2: landing tables, typed from the source metadata.",
            "-- Column names and order match the source, so a bulk load",
            "-- maps positionally as well as by name.", ""]
    for s in tables:
        body.append("-- %s (%s)" % (
            _qualified(dialect, s.schema, s.name),
            hints.get(s.name, {}).get("strategy", LoadStrategy.FULL.value)))
        body.append(_create_table(s, dialect, hints.get(s.name, {})))
        body.append("")
    (out / "02_create_tables.sql").write_text("\n".join(body),
                                              encoding="utf-8")
    files.append("02_create_tables.sql")

    src_dialect = src_of(source, dialect)
    un = "03_unload_from_%s.sql" % (source.key if source is not None
                                    else "source")
    (out / un).write_text(_unload(source, src_dialect, tables),
                          encoding="utf-8")
    files.append(un)
    ld = "04_load_into_%s.sql" % (target.key if target is not None
                                  else "target")
    (out / ld).write_text(_load(target, dialect, tables), encoding="utf-8")
    files.append(ld)

    (out / "README.md").write_text(
        _readme(pipeline, source, target, dialect, tables, files),
        encoding="utf-8")
    files.append("README.md")

    # Declared, never silent: every place a width had to be invented.
    wide = [s.name for s in tables
            if any(c.datatype == "string" and not c.precision
                   for c in s.columns)]
    if wide and dialect not in _UNBOUNDED_STRING:
        width = _VARCHAR_FALLBACK.get(dialect, 0)
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.WARNING, code="STRING_WIDTH_FALLBACK",
            message="%d landing table(s) have text column(s) with no declared "
                    "length — DDL uses the documented default %s(%d)"
                    % (len(wide), _BY_DIALECT.get(dialect, {}).get(
                        "string", _ANSI["string"]), width),
            obj=", ".join(wide[:8]),
            suggestion="Run ddl/00_probe_string_widths.sql on the source, put "
                       "the real lengths in the manifest and regenerate. A "
                       "value longer than the default makes the bulk load "
                       "fail loudly rather than truncate."))
    if skipped:
        pipeline.issues.append(ConversionIssue(
            severity=IssueSeverity.WARNING, code="DDL_NO_COLUMN_METADATA",
            message="%d table(s) have no column metadata, so no landing DDL "
                    "could be generated for them" % len(skipped),
            obj=", ".join(skipped[:8]),
            suggestion="Add columns to the manifest, or introspect the live "
                       "system, then regenerate."))
    return {"files": files, "tables": len(tables), "skipped": skipped,
            "dialect": dialect}

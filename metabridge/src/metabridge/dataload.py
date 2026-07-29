"""Data loading: any tabular file -> analyzed tables on any warehouse.

Three capabilities, all schema-inferring (Excel .xlsx, CSV/TSV, JSON
records):

    load_into_snowflake(...)      REAL load over a live connection using
                                  Snowflake's STANDARD path — CREATE
                                  TABLE IF NOT EXISTS, PUT to the table
                                  stage (@%table), COPY INTO with a CSV
                                  file format, then a verification
                                  SELECT COUNT(*). Evidence per step.
    load_into_databricks(...)     REAL load over a live SQL warehouse
                                  connection — CREATE TABLE IF NOT EXISTS,
                                  batched parameterized INSERTs (no
                                  client-reachable stage/COPY INTO source
                                  without a Unity Catalog volume), then a
                                  verification SELECT COUNT(*).
    generate_load_package(...)    every other warehouse gets its OWN
                                  standard load artifacts generated
                                  (never generic SQL renamed): BigQuery
                                  bq load, Redshift COPY ... IAM_ROLE,
                                  Synapse COPY INTO from storage, Postgres
                                  \\copy — DDL + load script + notes.

Type inference is conservative: integers, decimals, dates, timestamps,
booleans by full-column agreement over a sample; anything mixed stays
STRING. The inferred schema plugs straight into the scaffold manifest,
so loaded tables convert onward like any analyzed table.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_INT_RE = re.compile(r"^[+-]?\d{1,18}$")
_DEC_RE = re.compile(r"^[+-]?\d+\.\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
_BOOL = {"true", "false", "y", "n", "yes", "no", "0", "1"}


def _safe_ident(name: str) -> str:
    out = re.sub(r"\W+", "_", str(name).strip()).strip("_")
    if not out:
        out = "col"
    if out[0].isdigit():
        out = "c_" + out
    return out.upper()


def infer_type(values: List[str]) -> str:
    vals = [str(v).strip() for v in values
            if v is not None and str(v).strip() != ""]
    if not vals:
        return "STRING"
    if all(_INT_RE.match(v) for v in vals):
        return "INTEGER"
    if all(_DEC_RE.match(v) or _INT_RE.match(v) for v in vals):
        return "DECIMAL"
    if all(_TS_RE.match(v) for v in vals):
        return "TIMESTAMP"
    if all(_DATE_RE.match(v) for v in vals):
        return "DATE"
    if all(v.lower() in ("true", "false") for v in vals):
        return "BOOLEAN"
    return "STRING"


def read_tabular(data: bytes, filename: str,
                 sample: int = 500) -> Tuple[List[dict], List[list], List[str]]:
    """-> (columns [{name,type}], rows [list], notes). Supported:
    .xlsx (first sheet), .csv/.tsv/.txt, .json (list of records)."""
    suffix = Path(filename).suffix.lower()
    notes: List[str] = []
    header: List[str] = []
    rows: List[list] = []

    if suffix == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                    data_only=True)
        ws = wb.worksheets[0]
        if len(wb.worksheets) > 1:
            notes.append("workbook has %d sheets — loaded '%s' (first)"
                         % (len(wb.worksheets), ws.title))
        it = ws.iter_rows(values_only=True)
        for i, row in enumerate(it):
            cells = ["" if c is None else
                     (c.isoformat() if hasattr(c, "isoformat") else str(c))
                     for c in row]
            if i == 0:
                header = cells
            else:
                rows.append(cells)
        wb.close()
    elif suffix == ".json":
        doc = json.loads(data.decode("utf-8", errors="replace"))
        if isinstance(doc, dict):
            doc = doc.get("data") or doc.get("rows") or [doc]
        if not isinstance(doc, list) or not doc:
            raise ValueError("JSON must be a non-empty list of records")
        header = sorted({k for rec in doc if isinstance(rec, dict)
                         for k in rec})
        for rec in doc:
            rows.append(["" if rec.get(k) is None else str(rec.get(k))
                         for k in header])
        notes.append("JSON records flattened to %d columns" % len(header))
    else:                                   # csv / tsv / txt
        text = data.decode("utf-8-sig", errors="replace")
        delim = "\t" if suffix == ".tsv" or \
            ("\t" in text.splitlines()[0] if text.splitlines() else False) \
            else ","
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        table = [r for r in reader if any(c.strip() for c in r)]
        if not table:
            raise ValueError("%s contains no rows" % filename)
        header, rows = table[0], table[1:]

    if not header:
        raise ValueError("%s has no header row" % filename)
    if not rows:
        raise ValueError("%s has a header but no data rows" % filename)

    names, seen = [], set()
    for h in header:
        n = _safe_ident(h)
        base, i = n, 2
        while n in seen:
            n = "%s_%d" % (base, i)
            i += 1
        seen.add(n)
        names.append(n)
    width = len(names)
    rows = [r[:width] + [""] * (width - len(r)) for r in rows]
    cols = [{"name": names[i],
             "type": infer_type([r[i] for r in rows[:sample]])}
            for i in range(width)]
    return cols, rows, notes


_TYPES = {
    "snowflake": {"INTEGER": "NUMBER(38,0)", "DECIMAL": "NUMBER(38,6)",
                  "TIMESTAMP": "TIMESTAMP_NTZ", "DATE": "DATE",
                  "BOOLEAN": "BOOLEAN", "STRING": "VARCHAR"},
    "databricks": {"INTEGER": "BIGINT", "DECIMAL": "DECIMAL(38,6)",
                   "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
                   "BOOLEAN": "BOOLEAN", "STRING": "STRING"},
    "bigquery": {"INTEGER": "INT64", "DECIMAL": "NUMERIC",
                 "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
                 "BOOLEAN": "BOOL", "STRING": "STRING"},
    "redshift": {"INTEGER": "BIGINT", "DECIMAL": "DECIMAL(38,6)",
                 "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
                 "BOOLEAN": "BOOLEAN", "STRING": "VARCHAR(65535)"},
    "synapse": {"INTEGER": "BIGINT", "DECIMAL": "DECIMAL(38,6)",
                "TIMESTAMP": "DATETIME2", "DATE": "DATE",
                "BOOLEAN": "BIT", "STRING": "NVARCHAR(4000)"},
    "postgres": {"INTEGER": "BIGINT", "DECIMAL": "NUMERIC(38,6)",
                 "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
                 "BOOLEAN": "BOOLEAN", "STRING": "TEXT"},
}


def _ddl(table: str, cols: List[dict], target: str,
         extra: str = "") -> str:
    tmap = _TYPES[target]
    body = ",\n  ".join("%s %s" % (c["name"], tmap[c["type"]])
                        for c in cols)
    return "CREATE TABLE IF NOT EXISTS %s (\n  %s\n)%s;" % (
        table, body, extra)


def generate_load_package(table: str, cols: List[dict], filename: str,
                          target: str) -> dict:
    """Target-STANDARD load artifacts for a tabular file (section: 'as
    per respective standard' — each platform's own bulk path)."""
    t = _safe_ident(table)
    f = Path(filename).name
    if target == "snowflake":
        load = ("PUT file://%s @%%%s AUTO_COMPRESS=TRUE;\n"
                "COPY INTO %s FROM @%%%s\n"
                "  FILE_FORMAT = (TYPE = CSV SKIP_HEADER = 1 "
                "FIELD_OPTIONALLY_ENCLOSED_BY = '\"')\n"
                "  ON_ERROR = ABORT_STATEMENT;" % (f, t, t, t))
        note = "Snowflake standard: PUT to the table stage, COPY INTO."
    elif target == "databricks":
        load = ("-- upload %s to a Unity Catalog volume first\n"
                "COPY INTO %s\n"
                "FROM '/Volumes/<catalog>/<schema>/<volume>/%s'\n"
                "FILEFORMAT = CSV\n"
                "FORMAT_OPTIONS ('header' = 'true', 'inferSchema' = "
                "'false');" % (f, t, f))
        note = "Databricks standard: COPY INTO a Delta table from a " \
               "volume/cloud path."
    elif target == "bigquery":
        load = ("bq load --source_format=CSV --skip_leading_rows=1 \\\n"
                "  <dataset>.%s ./%s \\\n  %s"
                % (t, f, ",".join("%s:%s" % (c["name"],
                                             _TYPES["bigquery"][c["type"]])
                                  for c in cols)))
        note = "BigQuery standard: bq load (or LOAD DATA from GCS)."
    elif target == "redshift":
        load = ("COPY %s FROM 's3://<bucket>/%s'\n"
                "IAM_ROLE '<iam-role-arn>'\n"
                "CSV IGNOREHEADER 1 TIMEFORMAT 'auto';" % (t, f))
        note = "Redshift standard: COPY from S3 with an IAM role."
    elif target == "synapse":
        load = ("COPY INTO %s\n"
                "FROM 'https://<account>.blob.core.windows.net/"
                "<container>/%s'\n"
                "WITH (FILE_TYPE = 'CSV', FIRSTROW = 2);" % (t, f))
        note = "Synapse/Fabric standard: COPY INTO from Azure storage."
    elif target == "postgres":
        load = "\\copy %s FROM '%s' WITH (FORMAT csv, HEADER true);" % (t, f)
        note = "PostgreSQL standard: \\copy (client-side COPY)."
    else:
        raise ValueError("Unsupported load target: %s" % target)
    ddl = _ddl(t, cols, target,
               " USING DELTA" if target == "databricks" else "")
    return {"table": t, "target": target, "ddl": ddl, "load": load,
            "note": note,
            "columns": cols,
            "manifest_yaml": _manifest_yaml(t, cols)}


def _manifest_yaml(table: str, cols: List[dict]) -> str:
    import yaml
    return yaml.safe_dump(
        {"tables": [{"name": table,
                     "columns": [{"name": c["name"],
                                  "type": c["type"].lower()}
                                 for c in cols]}]}, sort_keys=False)


def load_into_snowflake(params: Dict[str, str], table: str,
                        data: bytes, filename: str,
                        create: bool = True) -> dict:
    """REAL load over a live connection, the standard way: CREATE TABLE
    IF NOT EXISTS -> PUT to @%table -> COPY INTO -> verify count."""
    from .livecheck import _snowflake_connect
    cols, rows, notes = read_tabular(data, filename)
    t = _safe_ident(table or Path(filename).stem)

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / (t.lower() + ".csv")
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([c["name"] for c in cols])
            w.writerows(rows)

        started = time.time()
        conn = _snowflake_connect(params)
        steps: List[dict] = []
        try:
            cur = conn.cursor()

            def step(label: str, sql: str):
                t0 = time.time()
                cur.execute(sql)
                steps.append({"step": label,
                              "ms": int((time.time() - t0) * 1000)})

            if create:
    # Set the schema context from connection params
                schema = params.get("schema", "PUBLIC")
                cur.execute(f"USE SCHEMA {schema}")
            step("create table", _ddl(t, cols, "snowflake"))
            step("put to table stage",
                 "PUT 'file://%s' @%%%s AUTO_COMPRESS=TRUE OVERWRITE=TRUE"
                 % (str(csv_path).replace("\\", "/"), t))
            step("copy into",
                 "COPY INTO %s FROM @%%%s FILE_FORMAT = (TYPE = CSV "
                 "SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"') "
                 "ON_ERROR = ABORT_STATEMENT PURGE = TRUE" % (t, t))
            verified = cur.execute(
                "SELECT COUNT(*) FROM %s" % t).fetchone()
            return {"ok": True, "table": t,
                    "columns": cols, "rows_in_file": len(rows),
                    "rows_in_table": int(verified[0]),
                    "steps": steps, "notes": notes,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "manifest_yaml": _manifest_yaml(t, cols)}
        except Exception as e:  # noqa: BLE001 — report, never crash
            return {"ok": False, "table": t, "error": str(e)[:400],
                    "steps": steps, "notes": notes}
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def load_into_databricks(params: Dict[str, str], table: str,
                         data: bytes, filename: str,
                         create: bool = True,
                         batch_size: int = 1000) -> dict:
    """REAL load over a live Databricks SQL warehouse connection: CREATE
    TABLE IF NOT EXISTS -> batched parameterized INSERTs -> verify count.
    Databricks SQL warehouses have no client-side file-stage primitive
    reachable from a plain Python driver (COPY INTO needs the file already
    sitting in a Unity Catalog volume/cloud path), so row-by-row INSERT is
    the connector's own live path rather than a re-implementation of
    COPY INTO."""
    from .livecheck import _databricks_connect
    cols, rows, notes = read_tabular(data, filename)
    t = _safe_ident(table or Path(filename).stem)

    started = time.time()
    conn = _databricks_connect(params)
    steps: List[dict] = []
    try:
        cur = conn.cursor()

        def step(label: str, sql: str, params_: Optional[list] = None):
            t0 = time.time()
            cur.execute(sql, params_) if params_ is not None else cur.execute(sql)
            steps.append({"step": label,
                          "ms": int((time.time() - t0) * 1000)})

        if create:
            step("create table", _ddl(t, cols, "databricks", " USING DELTA"))
        col_list = ", ".join(c["name"] for c in cols)
        placeholders = ", ".join(["?"] * len(cols))
        insert_sql = "INSERT INTO %s (%s) VALUES (%s)" % (t, col_list,
                                                           placeholders)
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            t0 = time.time()
            for r in batch:
                cur.execute(insert_sql, [v if v != "" else None for v in r])
            steps.append({"step": "insert rows %d-%d" % (i, i + len(batch)),
                         "ms": int((time.time() - t0) * 1000)})
        verified = cur.execute("SELECT COUNT(*) FROM %s" % t).fetchone()
        return {"ok": True, "table": t,
                "columns": cols, "rows_in_file": len(rows),
                "rows_in_table": int(verified[0]),
                "steps": steps, "notes": notes,
                "elapsed_ms": int((time.time() - started) * 1000),
                "manifest_yaml": _manifest_yaml(t, cols)}
    except Exception as e:  # noqa: BLE001 — report, never crash
        return {"ok": False, "table": t, "error": str(e)[:400],
                "steps": steps, "notes": notes}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

"""Data loading: schema-inferring file readers, per-target standard load
packages, and the live Snowflake PUT+COPY path."""
import io
import json
import os
import tempfile

os.environ.setdefault("METABRIDGE_DATA_DIR",
                      tempfile.mkdtemp(prefix="mb_load_"))

import pytest

from metabridge.dataload import (generate_load_package, infer_type,
                                 load_into_snowflake, read_tabular)

CSV = (b"order id,amount,order_dt,active\n"
       b"1,10.50,2026-01-02,true\n"
       b"2,7,2026-01-03,false\n")


def test_csv_reading_and_type_inference():
    cols, rows, _ = read_tabular(CSV, "orders.csv")
    assert cols == [{"name": "ORDER_ID", "type": "INTEGER"},
                    {"name": "AMOUNT", "type": "DECIMAL"},
                    {"name": "ORDER_DT", "type": "DATE"},
                    {"name": "ACTIVE", "type": "BOOLEAN"}]
    assert len(rows) == 2


def test_excel_reading():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Cust ID", "Name", "Joined"])
    ws.append([1, "Ada", "2026-05-01"])
    ws.append([2, "Linus", "2026-06-01"])
    buf = io.BytesIO()
    wb.save(buf)
    cols, rows, _ = read_tabular(buf.getvalue(), "customers.xlsx")
    assert [c["name"] for c in cols] == ["CUST_ID", "NAME", "JOINED"]
    assert cols[0]["type"] == "INTEGER"
    assert len(rows) == 2


def test_json_records():
    data = json.dumps([{"id": 1, "city": "Pune"},
                       {"id": 2, "city": "Mumbai"}]).encode()
    cols, rows, notes = read_tabular(data, "cities.json")
    assert {c["name"] for c in cols} == {"ID", "CITY"}
    assert len(rows) == 2 and notes


def test_mixed_column_stays_string():
    assert infer_type(["1", "2", "x"]) == "STRING"
    assert infer_type(["1", "", "3"]) == "INTEGER"   # blanks are NULLs


@pytest.mark.parametrize("target,must_have", [
    ("snowflake", ["PUT file://", "COPY INTO", "@%"]),
    ("databricks", ["COPY INTO", "USING DELTA", "Volumes"]),
    ("bigquery", ["bq load", "INT64"]),
    ("redshift", ["COPY", "IAM_ROLE", "IGNOREHEADER"]),
    ("synapse", ["COPY INTO", "blob.core.windows.net"]),
    ("postgres", ["\\copy", "FORMAT csv"]),
])
def test_standard_load_package_per_target(target, must_have):
    cols, _, _ = read_tabular(CSV, "orders.csv")
    pkg = generate_load_package("orders", cols, "orders.csv", target)
    text = pkg["ddl"] + "\n" + pkg["load"]
    for token in must_have:
        assert token in text, (target, token)
    assert "CREATE TABLE IF NOT EXISTS ORDERS" in pkg["ddl"]
    # inferred schema flows onward: manifest feeds the scaffold
    from metabridge.scaffold import load_table_manifest
    import pathlib
    f = pathlib.Path(tempfile.mkdtemp()) / "m.yml"
    f.write_text(pkg["manifest_yaml"])
    tables, _ = load_table_manifest(str(f))
    assert tables[0]["name"] == "ORDERS"
    assert len(tables[0]["columns"]) == 4


def test_live_snowflake_load_put_copy_verify(monkeypatch):
    import sys
    import types

    executed = []

    class _Cur:
        def execute(self, sql, *a):
            executed.append(sql)
            self._row = (2,) if sql.upper().startswith("SELECT COUNT") \
                else ("ok",)
            return self

        def fetchone(self):
            return self._row

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    mod = types.ModuleType("snowflake.connector")
    mod.connect = lambda **kw: _Conn()
    pkg = types.ModuleType("snowflake")
    pkg.connector = mod
    monkeypatch.setitem(sys.modules, "snowflake", pkg)
    monkeypatch.setitem(sys.modules, "snowflake.connector", mod)
    monkeypatch.setenv("MB_SNOWFLAKE_PASSWORD", "x")

    r = load_into_snowflake({"account": "a", "user": "u",
                             "database": "d", "schema": "s"},
                            "orders", CSV, "orders.csv")
    assert r["ok"] is True
    assert r["rows_in_file"] == 2 and r["rows_in_table"] == 2
    joined = "\n".join(executed)
    assert "CREATE TABLE IF NOT EXISTS ORDERS" in joined
    assert "PUT 'file://" in joined and "@%ORDERS" in joined
    assert "COPY INTO ORDERS" in joined and "SKIP_HEADER = 1" in joined
    assert [s["step"] for s in r["steps"]] == \
        ["create table", "put to table stage", "copy into"]
    assert "ORDERS" in r["manifest_yaml"]

"""Scaffold table-manifest tolerance: any reasonable YAML shape works;
unrecognizable files get an error that says what they are and shows a
valid example — never a bare 'has no tables'."""
import pytest

from metabridge.scaffold import load_table_manifest, scaffold


def _load(tmp_path, text, name="tables.yml"):
    f = tmp_path / name
    f.write_text(text)
    return load_table_manifest(str(f))


def test_canonical_shape_and_string_entries(tmp_path):
    tables, _ = _load(tmp_path, """
tables:
  - name: CUSTOMERS
    schema: SALES
    columns: [{name: ID, type: integer}, NAME]
  - ORDERS
""")
    assert [t["name"] for t in tables] == ["CUSTOMERS", "ORDERS"]
    assert tables[0]["columns"] == [{"name": "ID", "type": "integer"},
                                    {"name": "NAME"}]
    assert tables[1]["columns"] == []


def test_dbt_sources_yml(tmp_path):
    tables, notes = _load(tmp_path, """
version: 2
sources:
  - name: raw
    schema: RAW
    tables:
      - name: raw_customers
        columns: [{name: id, data_type: integer}]
      - name: raw_orders
""")
    assert {t["name"] for t in tables} == {"raw_customers", "raw_orders"}
    assert tables[0]["schema"] == "RAW"
    assert tables[0]["columns"][0]["type"] == "integer"
    assert any("sources.yml" in n for n in notes)


def test_dbt_schema_models(tmp_path):
    tables, notes = _load(tmp_path, """
version: 2
models:
  - name: dim_customer
    columns: [{name: cust_id}]
""")
    assert tables[0]["name"] == "dim_customer"
    assert any("models" in n for n in notes)


def test_plain_list_and_mapping_shapes(tmp_path):
    tables, _ = _load(tmp_path, "- CUSTOMERS\n- ORDERS\n")
    assert [t["name"] for t in tables] == ["CUSTOMERS", "ORDERS"]
    tables, notes = _load(tmp_path, """
CUSTOMERS: [ID, NAME]
ORDERS:
""", name="map.yml")
    assert {t["name"] for t in tables} == {"CUSTOMERS", "ORDERS"}
    assert any("mapping" in n for n in notes)


def test_connection_profile_gets_a_helpful_error(tmp_path):
    # exactly the marketplace-generated snowflake.yml the console emits
    with pytest.raises(ValueError) as e:
        _load(tmp_path, """
conn_snowflake:
  target: prod
  outputs:
    prod:
      type: snowflake
      account: GZWSSAV-RH89300
      user: DhimanMukul5911
      password: "{{ env_var('MB_SNOWFLAKE_PASSWORD') }}"
""", name="snowflake (1).yml")
    msg = str(e.value)
    assert "CONNECTION PROFILE" in msg
    assert "tables:" in msg          # shows a working example
    assert "has no tables" not in msg


def test_empty_file_error_lists_accepted_shapes(tmp_path):
    with pytest.raises(ValueError) as e:
        _load(tmp_path, "# just a comment\n")
    msg = str(e.value)
    assert "Accepted shapes" in msg and "sources.yml" in msg


def test_scaffold_runs_from_dbt_sources_yml(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "sources.yml"
    f.write_text("""
version: 2
sources:
  - name: raw
    schema: RAW
    tables:
      - name: raw_customers
        columns: [{name: id, data_type: integer},
                  {name: email, data_type: nvarchar}]
""")
    report = scaffold("snowflake", "databricks", str(f),
                      str(tmp_path / "out"),
                      source_region="on_prem", target_region="eu")
    assert report["summary"]["status_counts"]
    assert (tmp_path / "out" / "dbt" / "dbt_project.yml").exists()
    assert any("sources.yml" in n
               for n in report.get("manifest_notes", []))


# ---------------------------------------------------------------------------
# The unload script must speak the SOURCE's dialect, never the target's.
# A connector with no dialect is not SQL-addressable (every SAP connector),
# and inheriting the target's dialect emitted e.g. Snowflake COPY INTO
# addressed to a SAP system — syntactically fine, impossible to run.
# ---------------------------------------------------------------------------

def _unload_text(tmp_path, source_key, target_key):
    f = tmp_path / "tables.yml"
    f.write_text("""
tables:
  - name: MARA
    schema: SAPSR3
    columns: [{name: MATNR, type: nvarchar(18)}, {name: ERSDA, type: dats}]
""")
    out = tmp_path / ("out_%s_%s" % (source_key, target_key))
    report = scaffold(source_key, target_key, str(f), str(out))
    name = next(n for n in report["ddl"]["files"] if n.startswith("02_"))
    return (out / "ddl" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("source_key,target_key,forbidden", [
    # sap_hana is absent on purpose: it HAS a generated unload now, so it is
    # covered by the SAP HANA tests below rather than by this fallback.
    ("sap_s4", "snowflake", "COPY INTO"),
    ("sap_bw", "databricks", "CREATE OR REPLACE TABLE delta."),
    ("sap_ecc", "bigquery", "EXPORT DATA"),
])
def test_dialectless_source_never_inherits_target_syntax(
        tmp_path, monkeypatch, source_key, target_key, forbidden):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    text = _unload_text(tmp_path, source_key, target_key)
    assert forbidden not in text, \
        "%s unload inherited %s syntax it cannot run" % (source_key,
                                                         target_key)
    assert "no generated bulk-export form" in text
    # the operator still gets the table list and its destination folder
    assert "SAPSR3.MARA" in text and "mara/" in text


# ---------------------------------------------------------------------------
# SAP HANA writes a REAL unload. It dispatches on the connector key, because
# HANA declares no sqlglot dialect and would otherwise never be reached.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target_key", ["snowflake", "databricks",
                                        "bigquery", "redshift"])
def test_sap_hana_generates_export_into_whatever_the_target_is(
        tmp_path, monkeypatch, target_key):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    text = _unload_text(tmp_path, "sap_hana", target_key)
    assert "EXPORT INTO '<stage-uri>/mara/'" in text
    assert "FROM SAPSR3.MARA" in text
    assert "COLUMN LIST IN FIRST ROW" in text
    assert "no generated bulk-export form" not in text
    # never the target's syntax, which was the original defect
    for alien in ("COPY INTO", "EXPORT DATA", "UNLOAD (", "delta.`"):
        assert alien not in text


def test_sap_hana_unload_uses_two_part_names_not_the_tenant(
        tmp_path, monkeypatch):
    """HANA addresses objects SCHEMA.TABLE — the tenant database is the
    connection. A three-part name would not resolve."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "t.yml"
    f.write_text("""
tables:
  - name: MARA
    schema: SAPABAP1
    columns: [{name: MATNR, type: nvarchar(18)}]
""")
    out = tmp_path / "out"
    report = scaffold("sap_hana", "snowflake", str(f), str(out),
                      source_params={"database": "H00"})
    name = next(n for n in report["ddl"]["files"] if n.startswith("02_"))
    text = (out / "ddl" / name).read_text(encoding="utf-8")
    assert "FROM SAPABAP1.MARA" in text
    assert "H00.SAPABAP1.MARA" not in text


def test_sap_hana_unload_writes_no_key_material(tmp_path, monkeypatch):
    """The credential is NAMED — created once on HANA — so no access key
    ever lands in a generated file."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    text = _unload_text(tmp_path, "sap_hana", "snowflake")
    assert "WITH CREDENTIAL '<credentials>'" in text
    assert "SAPHANAIMPORTEXPORT" in text          # how to create it
    assert "password=" not in text.replace("password=<secret-key>", "")


@pytest.mark.parametrize("target_key,expect,forbid", [
    ("snowflake", "TYPE = CSV", "TYPE = PARQUET"),
    ("databricks", "FILEFORMAT = CSV", "FILEFORMAT = PARQUET"),
])
def test_sap_hana_load_reads_the_format_the_unload_wrote(
        tmp_path, monkeypatch, target_key, expect, forbid):
    """EXPORT INTO writes CSV. A load that reads Parquet fails on row one, so
    the two halves of the package must agree."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: nvarchar(3)}]\n")
    out = tmp_path / ("out_" + target_key)
    report = scaffold("sap_hana", target_key, str(f), str(out))
    name = next(n for n in report["ddl"]["files"] if n.startswith("03_"))
    text = (out / "ddl" / name).read_text(encoding="utf-8")
    assert expect in text and forbid not in text


def test_non_hana_sources_keep_their_parquet_load(tmp_path, monkeypatch):
    """The CSV switch is scoped to SAP HANA on purpose. PostgreSQL has the
    same defect (\\copy writes CSV, its load says PARQUET) but widening the
    fix changes an already-shipped connector, so it stays a separate call."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: varchar(3)}]\n")
    out = tmp_path / "out_pg"
    report = scaffold("postgres", "snowflake", str(f), str(out))
    name = next(n for n in report["ddl"]["files"] if n.startswith("03_"))
    text = (out / "ddl" / name).read_text(encoding="utf-8")
    assert "TYPE = PARQUET" in text


def test_source_with_a_real_dialect_still_generates_its_own_unload(
        tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    text = _unload_text(tmp_path, "postgres", "snowflake")
    # postgres declares a dialect, so it keeps its psql \copy form and must
    # NOT fall into the "no bulk-export form" branch
    assert r"\copy (SELECT * FROM SAPSR3.MARA)" in text
    assert "no generated bulk-export form" not in text
    assert "CREATE OR REPLACE STAGE" not in text

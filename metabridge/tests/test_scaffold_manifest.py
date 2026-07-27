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

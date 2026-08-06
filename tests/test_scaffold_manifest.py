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


@pytest.mark.parametrize("stage_uri,expect_unload", [
    # HANA puts the region IN the scheme. Verified against SAP HANA Cloud
    # 2026.14: s3-ap-south-1:// exported successfully, plain s3:// is not
    # the accepted form.
    ("s3://bucket/mb", "s3-<region>://bucket/mb/mara/"),
    # already regioned — pass through, do not double-rewrite
    ("s3-ap-south-1://bucket/mb", "s3-ap-south-1://bucket/mb/mara/"),
    # other providers are left alone
    ("azure://acct/mb", "azure://acct/mb/mara/"),
    ("gs://bucket/mb", "gs://bucket/mb/mara/"),
])
def test_sap_hana_unload_puts_the_region_in_the_scheme(
        tmp_path, monkeypatch, stage_uri, expect_unload):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: nvarchar(3)}]\n")
    out = tmp_path / "out"
    report = scaffold("sap_hana", "snowflake", str(f), str(out),
                      movement={"stage_uri": stage_uri})
    un = next(n for n in report["ddl"]["files"] if n.startswith("02_"))
    assert expect_unload in (out / "ddl" / un).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 01_create_landing.sql must set its own session context. It issues CREATE
# SCHEMA / CREATE TABLE with no database qualifier, so without one it lands
# wherever the session happens to point — or fails outright. The unload
# script has always done this; the landing script did not, and every
# operator had to know to run USE DATABASE first.
# ---------------------------------------------------------------------------

def _landing(tmp_path, target_key, target_params):
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: nvarchar(3)}]\n")
    out = tmp_path / ("land_%s_%s" % (target_key,
                                      abs(hash(str(target_params)))))
    report = scaffold("sap_hana", target_key, str(f), str(out),
                      target_params=target_params)
    return report, (out / "ddl" / "01_create_landing.sql").read_text(
        encoding="utf-8")


@pytest.mark.parametrize("target_key,params,expect", [
    ("snowflake", {"database": "SAPHANA", "warehouse": "WH"},
     ["USE DATABASE SAPHANA;", "USE WAREHOUSE WH;"]),
    ("databricks", {"catalog": "main"}, ["USE CATALOG main;"]),
    ("synapse", {"database": "DW"}, ["USE DW;"]),
    ("teradata", {"database": "DW"}, ["DATABASE DW;"]),
])
def test_landing_ddl_sets_its_own_session_context(
        tmp_path, monkeypatch, target_key, params, expect):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    _, text = _landing(tmp_path, target_key, params)
    for line in expect:
        assert line in text
    # context must come BEFORE anything is created, or it is decoration
    assert text.index(expect[0]) < text.index("CREATE ")


@pytest.mark.parametrize("target_key", ["postgres", "redshift"])
def test_connect_time_databases_get_a_note_not_a_statement(
        tmp_path, monkeypatch, target_key):
    """PostgreSQL and Redshift pick the database at CONNECT time. Emitting a
    USE they cannot run would be worse than saying so."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    _, text = _landing(tmp_path, target_key, {"database": "analytics"})
    assert "-- Connect to database analytics before running" in text
    assert "USE DATABASE" not in text


def test_bigquery_gets_no_context_block(tmp_path, monkeypatch):
    """Every BigQuery identifier already carries its dataset, so there is no
    session to set — an empty 'context' header would just be noise."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    _, text = _landing(tmp_path, "bigquery", {"project": "p",
                                              "dataset": "d"})
    assert "Session context" not in text


def test_missing_target_database_is_declared_as_a_substitution(
        tmp_path, monkeypatch):
    """A commented USE is a placeholder like any other, and the README's
    scan reads the landing file now — previously it read only the unload and
    load, so this one would have gone unreported."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    report, text = _landing(tmp_path, "snowflake", {})
    assert "-- USE DATABASE <database>;" in text
    assert "<database>" in report["ddl"]["placeholders"]


def test_snowflake_landing_does_not_demand_a_warehouse(tmp_path,
                                                       monkeypatch):
    """CREATE SCHEMA/TABLE are metadata-only on Snowflake and need no
    running warehouse. A commented USE WAREHOUSE would imply otherwise."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    report, text = _landing(tmp_path, "snowflake", {"database": "DB"})
    assert "USE DATABASE DB;" in text
    assert "WAREHOUSE" not in text
    assert "<warehouse>" not in report["ddl"]["placeholders"]


def _hana_pkg(tmp_path, movement):
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: nvarchar(3)}]\n")
    out = tmp_path / ("out_%d" % abs(hash(str(movement))))
    report = scaffold("sap_hana", "snowflake", str(f), str(out),
                      movement=movement)
    ddl = out / "ddl"
    un = next(n for n in report["ddl"]["files"] if n.startswith("02_"))
    ld = next(n for n in report["ddl"]["files"] if n.startswith("03_"))
    return (report, (ddl / un).read_text(encoding="utf-8"),
            (ddl / ld).read_text(encoding="utf-8"))


def test_region_setting_fills_the_hana_scheme(tmp_path, monkeypatch):
    """The workspace region turns the last HANA placeholder into a real
    value, while the target's own URI stays plain."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    report, unload, load = _hana_pkg(
        tmp_path, {"stage_uri": "s3://b/mb", "region": "ap-south-1"})
    assert "s3-ap-south-1://b/mb/mara/" in unload
    assert "'s3://b/mb/mara/'" in load          # target unaffected
    assert "<region>" not in report["ddl"]["placeholders"]
    assert "bucket region" in report["ddl"]["movement_prefilled"]


def test_region_left_blank_still_declares_the_substitution(tmp_path,
                                                           monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    report, unload, _ = _hana_pkg(tmp_path, {"stage_uri": "s3://b/mb"})
    assert "s3-<region>://b/mb/mara/" in unload
    assert "<region>" in report["ddl"]["placeholders"]
    assert "bucket region" not in report["ddl"]["movement_prefilled"]


def test_a_filled_region_is_never_reported_as_outstanding(tmp_path,
                                                          monkeypatch):
    """Regression: the placeholder scan is a substring match over the whole
    file, so an explanatory COMMENT that spelled out the literal token made
    a substituted region look unfilled. Illustrations must use a concrete
    example, never the placeholder itself."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    report, unload, _ = _hana_pkg(
        tmp_path, {"stage_uri": "s3://b/mb", "region": "eu-central-1"})
    assert "<region>" not in unload, \
        "no <region> may survive anywhere in the file, comments included"
    readme_says = "<region>" in report["ddl"]["placeholders"]
    assert not readme_says


def test_named_source_credential_fills_the_hana_unload(tmp_path,
                                                       monkeypatch):
    """The credential is an identifier, not a secret — the key stays inside
    HANA. Storing the NAME completes the script without holding anything
    sensitive, exactly as source_stage already does for Snowflake."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    report, unload, load = _hana_pkg(
        tmp_path, {"stage_uri": "s3://b/mb", "region": "ap-south-1",
                   "source_credential": "MB_S3"})
    assert "WITH CREDENTIAL 'MB_S3'" in unload
    assert "<credentials>" not in unload
    # the how-to comment names the same credential, so it stays copy-pasteable
    assert "PURPOSE 'MB_S3' TYPE 'PASSWORD'" in unload
    # ...and no key material is ever written
    assert "password=<secret-key>" in unload      # the placeholder, not a key
    assert "named source credential" in report["ddl"]["movement_prefilled"]


def test_target_credential_is_not_confused_with_the_source_one(tmp_path,
                                                               monkeypatch):
    """Regression: 'prefilled' scanned all three files, so a target that
    still needed <credentials> made a FILLED source credential look
    outstanding. The two are separate facts about separate systems."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    report, unload, load = _hana_pkg(
        tmp_path, {"stage_uri": "s3://b/mb", "source_credential": "MB_S3"})
    assert "<credentials>" not in unload            # source: filled
    assert "<credentials>" in load                  # target: still needed
    assert "named source credential" in report["ddl"]["movement_prefilled"]
    assert "<credentials>" in report["ddl"]["placeholders"]


def test_named_target_stage_removes_the_credentials_clause(tmp_path,
                                                           monkeypatch):
    """A Snowflake stage on a STORAGE INTEGRATION holds its credential
    inside Snowflake. This is the only load form that never writes a key
    into a file — the inline AWS_KEY_ID/AWS_SECRET_KEY alternative does."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    report, _, load = _hana_pkg(
        tmp_path, {"stage_uri": "s3://b/mb",
                   "target_stage": "MB_LANDING_STAGE"})
    assert "FROM @MB_LANDING_STAGE/mara/" in load
    assert "CREDENTIALS" not in load
    assert "<credentials>" not in load
    assert "s3://" not in load          # the stage already knows the bucket
    assert "named target stage" in report["ddl"]["movement_prefilled"]


def test_without_a_target_stage_the_credential_is_still_declared(
        tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    report, _, load = _hana_pkg(tmp_path, {"stage_uri": "s3://b/mb"})
    assert "CREDENTIALS = (<credentials>)" in load
    assert "FROM 's3://b/mb/mara/'" in load
    assert "<credentials>" in report["ddl"]["placeholders"]


def test_a_fully_configured_workspace_leaves_nothing_to_substitute(
        tmp_path, monkeypatch):
    """The point of Data movement settings: set them once and the ddl/
    bundle is runnable as generated, with no secret in any file."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: nvarchar(3)}]\n")
    out = tmp_path / "full"
    report = scaffold(
        "sap_hana", "snowflake", str(f), str(out),
        movement={"stage_uri": "s3://b/mb", "region": "ap-south-1",
                  "source_credential": "MB_S3",
                  "target_stage": "MB_LANDING_STAGE"},
        target_params={"database": "SAPHANA", "warehouse": "WH"})
    assert report["ddl"]["placeholders"] == []
    # Only EXECUTABLE lines matter here. Comments legitimately carry
    # illustrative tokens — the credential how-to shows
    # `user=<access-key>;password=<secret-key>` to document a one-time step
    # already performed on the source — and those are documentation, not
    # something anyone has to substitute before running.
    runnable = "\n".join(
        line
        for n in report["ddl"]["files"] if n.endswith(".sql")
        for line in (out / "ddl" / n).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("--"))
    assert "<" not in runnable, runnable
    for leaky in ("AWS_KEY_ID", "AWS_SECRET_KEY", "password="):
        assert leaky not in runnable


def test_the_same_stage_uri_renders_per_platform(tmp_path, monkeypatch):
    """One workspace setting, two spellings: the HANA export needs the
    region in the scheme and the Snowflake load must NOT have it. Emitting
    one form on both halves breaks whichever half it is wrong for."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: nvarchar(3)}]\n")
    out = tmp_path / "out"
    report = scaffold("sap_hana", "snowflake", str(f), str(out),
                      movement={"stage_uri": "s3://bucket/mb"})
    ddl = out / "ddl"
    un = next(n for n in report["ddl"]["files"] if n.startswith("02_"))
    ld = next(n for n in report["ddl"]["files"] if n.startswith("03_"))
    unload = (ddl / un).read_text(encoding="utf-8")
    load = (ddl / ld).read_text(encoding="utf-8")
    assert "s3-<region>://bucket/mb/mara/" in unload
    assert "'s3://bucket/mb/mara/'" in load
    assert "s3-<region>" not in load
    # and the substitution is declared rather than left to be discovered
    assert "<region>" in report["ddl"]["placeholders"]
    assert "<region>" in (ddl / "README.md").read_text(encoding="utf-8")


def _pg_unload(tmp_path, stage_uri):
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: varchar(3)}]\n")
    out = tmp_path / ("pg_%s" % abs(hash(stage_uri)))
    report = scaffold("postgres", "snowflake", str(f), str(out),
                      movement={"stage_uri": stage_uri})
    un = next(n for n in report["ddl"]["files"] if n.startswith("02_"))
    return (out / "ddl" / un).read_text(encoding="utf-8")


def test_postgres_streams_to_object_storage_via_program(tmp_path,
                                                        monkeypatch):
    """`\\copy` cannot name a bucket as a FILE — that produced a statement
    psql rejects — but it CAN pipe to a PROGRAM, and the AWS CLI runs on the
    same client. One step, nothing staged on disk, no server privilege."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    text = _pg_unload(tmp_path, "s3://bucket/mb")
    assert ("\\copy (SELECT * FROM S.MARA) TO PROGRAM "
            "'aws s3 cp - s3://bucket/mb/mara/mara.csv'") in text
    # the broken form: a bucket URI as a plain file target
    assert "TO 's3://" not in text
    # and the no-CLI route is still offered
    assert "No CLI?" in text


def test_postgres_unload_path_matches_what_the_load_reads(tmp_path,
                                                          monkeypatch):
    """Step 2 wrote <uri>/<table>.csv while step 3 read <uri>/<table>/ as a
    prefix — a file BESIDE the prefix, not inside it. COPY INTO then
    succeeded having read nothing, which is the worst way to fail."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: varchar(3)}]\n")
    out = tmp_path / "pg_paths"
    report = scaffold("postgres", "snowflake", str(f), str(out),
                      movement={"stage_uri": "s3://bucket/mb"})
    ddl = out / "ddl"
    un = next(n for n in report["ddl"]["files"] if n.startswith("02_"))
    ld = next(n for n in report["ddl"]["files"] if n.startswith("03_"))
    unload = (ddl / un).read_text(encoding="utf-8")
    load = (ddl / ld).read_text(encoding="utf-8")
    # the written object must sit INSIDE the prefix the load scans
    assert "s3://bucket/mb/mara/mara.csv" in unload
    assert "FROM 's3://bucket/mb/mara/'" in load
    assert "s3://bucket/mb/mara.csv" not in unload


def test_postgres_local_stage_is_used_verbatim(tmp_path, monkeypatch):
    """A local path is exactly what \\copy wants — no redirection, and no
    upload note for an upload that is not happening."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    text = _pg_unload(tmp_path, "/data/export")
    assert "TO '/data/export/mara.csv'" in text
    assert "mb_export" not in text
    assert "aws s3 cp" not in text


@pytest.mark.parametrize("source_key", ["sap_hana", "postgres"])
def test_csv_sources_get_a_csv_load(tmp_path, monkeypatch, source_key):
    """Both of these unload CSV — HANA because EXPORT INTO has no Parquet
    form, PostgreSQL because `\\copy` is CSV by construction. A Parquet load
    against either fails on row one."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: varchar(3)}]\n")
    out = tmp_path / ("out_" + source_key)
    report = scaffold(source_key, "snowflake", str(f), str(out))
    name = next(n for n in report["ddl"]["files"] if n.startswith("03_"))
    text = (out / "ddl" / name).read_text(encoding="utf-8")
    assert "TYPE = CSV" in text and "TYPE = PARQUET" not in text


@pytest.mark.parametrize("source_key", ["snowflake", "redshift",
                                        "databricks", "teradata"])
def test_parquet_sources_keep_a_parquet_load(tmp_path, monkeypatch,
                                             source_key):
    """Parquet stays the default: a source that can emit it should, because
    it carries its own types instead of re-inferring them at load time."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "t.yml"
    f.write_text("tables:\n  - name: MARA\n    schema: S\n"
                 "    columns: [{name: A, type: varchar(3)}]\n")
    out = tmp_path / ("outp_" + source_key)
    report = scaffold(source_key, "snowflake", str(f), str(out))
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

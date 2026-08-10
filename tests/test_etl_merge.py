"""Merging an ETL project's logic into a scaffold, in one run.

A table manifest describes the RAW layer, so scaffolding it alone yields
pass-through models. Supplying the ETL that builds the curated tables converts
that logic into the SAME project — and, because both facts are then in one
process, lets the landing layer skip tables the logic already produces. Two
separate runs cannot see that, which is how the same table ends up both copied
and rebuilt.
"""
import io
import json
import pathlib
import tempfile
import zipfile

import pytest
import yaml

from metabridge.etl_logic import (
    _target_tables, merge_etl_logic, unland_built_tables)
from metabridge.scaffold import scaffold

# reuse the native-export fixture builders so these tests exercise the real
# decode path rather than a hand-made IR
from test_idmc_export import _asset_zip, _dim_customer, _obj, _package, _template

MANIFEST = {"tables": [
    {"name": "CUSTOMERS", "schema": "RAW_SCHEMA",
     "columns": [{"name": "CUSTOMER_ID", "type": "varchar"},
                 {"name": "EMAIL", "type": "varchar"},
                 {"name": "CREATED_DATE", "type": "date"}]},
    {"name": "DIM_CUSTOMER", "schema": "SILVER_SCHEMA",
     "columns": [{"name": "CUSTOMER_SK", "type": "varchar"}]},
]}


def _manifest(tmp_path, doc=None):
    p = tmp_path / "tables.yml"
    p.write_text(yaml.safe_dump(doc or MANIFEST), encoding="utf-8")
    return str(p)


def _bundle(tmp_path, template=None):
    return str(_package(tmp_path / "bundle",
                        {"m": _asset_zip(template or _dim_customer())}))


def _run(tmp_path, **kw):
    out = tmp_path / "out"
    report = scaffold("oracle", "snowflake", _manifest(tmp_path), str(out),
                      "combined", **kw)
    models = sorted(f.parent.name + "/" + f.name
                    for f in (out / "dbt" / "models").rglob("*.sql"))
    ddl = (out / "ddl" / "01_create_landing.sql").read_text(encoding="utf-8")
    return report, models, ddl


# --- the three modes -------------------------------------------------------

def test_manifest_only_is_unchanged(tmp_path):
    """The regression gate: with no bundle every generator must see exactly
    the pipeline it saw before this argument existed."""
    report, models, ddl = _run(tmp_path)
    assert "staging/stg_dim_customer.sql" in models
    assert "DIM_CUSTOMER" in ddl
    assert "etl" not in report


def test_bundle_adds_the_curated_layer(tmp_path):
    report, models, _ = _run(tmp_path, etl_bundle=_bundle(tmp_path))
    assert "marts/dim_customer.sql" in models
    assert "staging/stg_customers.sql" in models
    assert report["etl"]["converted"] == 1


def test_produced_table_is_not_landed(tmp_path):
    """DIM_CUSTOMER is in the manifest AND built by the ETL. Landing it too
    yields the same relation twice, diverging the moment either changes."""
    report, models, ddl = _run(tmp_path, etl_bundle=_bundle(tmp_path))
    assert "staging/stg_dim_customer.sql" not in models
    assert "DIM_CUSTOMER" not in ddl
    assert [t["table"] for t in report["etl"]["not_landed"]] == ["DIM_CUSTOMER"]


def test_land_etl_targets_keeps_it(tmp_path):
    """Option (b): landing both copies is only useful while running the two
    sides in parallel to compare them, so it stays available."""
    report, models, ddl = _run(tmp_path, etl_bundle=_bundle(tmp_path),
                               land_etl_targets=True)
    assert "staging/stg_dim_customer.sql" in models
    assert "DIM_CUSTOMER" in ddl
    assert report["etl"]["not_landed"] == []


# --- reconciliation --------------------------------------------------------

def test_source_missing_from_manifest_is_reported(tmp_path):
    """A model reading a relation the raw layer does not carry will not
    compile; that has to surface at generate time, not at dbt run time."""
    doc = {"tables": [t for t in MANIFEST["tables"]
                      if t["name"] != "CUSTOMERS"]}
    out = tmp_path / "out"
    report = scaffold("oracle", "snowflake", _manifest(tmp_path, doc),
                      str(out), "combined", etl_bundle=_bundle(tmp_path))
    assert "RAW_SCHEMA.CUSTOMERS" in report["etl"]["unresolved_sources"]
    assert any(i["code"] == "ETL_SOURCE_NOT_IN_MANIFEST"
               for i in report["project_issues"])


def test_qualified_source_is_not_ambiguous(tmp_path):
    """The ETL names its schema, so a same-named table in another schema is
    not a conflict — the qualified match settles it without a warning."""
    doc = {"tables": MANIFEST["tables"] + [
        {"name": "CUSTOMERS", "schema": "ARCHIVE_SCHEMA",
         "columns": [{"name": "CUSTOMER_ID", "type": "varchar"}]}]}
    out = tmp_path / "out"
    report = scaffold("oracle", "snowflake", _manifest(tmp_path, doc),
                      str(out), "combined", etl_bundle=_bundle(tmp_path))
    assert report["etl"]["ambiguous_sources"] == []
    assert report["etl"]["unresolved_sources"] == []


def test_unqualified_source_in_two_schemas_is_not_guessed():
    """With no schema on the ETL side, a bare name living in two schemas is
    how a RAW and an ARCHIVE copy are told apart — picking one silently would
    wire the logic to the wrong layer.

    Driven directly: `load_table_manifest` de-duplicates tables by bare name,
    so a manifest cannot currently carry both and the guard is unreachable
    through a scaffold run. It is kept because the merge must not start
    guessing if that ever changes."""
    from metabridge.etl_logic import _rebase_sources
    from metabridge.ir.model import (
        Mapping, Pipeline, SourceTable, Transformation, TransformationType)

    pipeline = Pipeline(name="p", source_format="scaffold")
    pipeline.sources += [SourceTable(name="CUSTOMERS", schema="RAW_SCHEMA"),
                         SourceTable(name="CUSTOMERS", schema="ARCHIVE_SCHEMA")]
    logic = Pipeline(name="l", source_format="idmc")
    m = Mapping(name="DIM_CUSTOMER")
    m.transformations.append(Transformation(
        name="src", type=TransformationType.SOURCE,
        properties={"table": "CUSTOMERS", "schema": ""}))
    logic.mappings.append(m)

    summary = {}
    _rebase_sources(logic, pipeline, summary)
    assert summary["ambiguous_sources"]
    assert summary["unresolved_sources"] == []
    assert any(i.code == "ETL_SOURCE_AMBIGUOUS" for i in pipeline.issues)


def test_empty_bundle_is_reported_not_ignored(tmp_path):
    """A connection-only export converts to nothing. Carrying on silently
    would present a raw-only project as if the logic had been included."""
    root = tmp_path / "empty"
    (root / "Explore" / "Default").mkdir(parents=True)
    (root / "exportMetadata.v2.json").write_text(json.dumps({"packageName": "P"}))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fileRecord.json", json.dumps(
            [{"@type": "fileRecord", "id": "@2", "type": "IMAGE"}]))
        z.writestr("bin/@2.bin", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01")
    (root / "Explore" / "Default" / "x.DTEMPLATE.zip").write_bytes(buf.getvalue())
    with pytest.raises(FileNotFoundError):
        _run(tmp_path, etl_bundle=str(root))


# --- procedures get the same treatment -------------------------------------

PROC_MANIFEST = {
    "tables": [
        {"name": "ORDERS", "schema": "RAW_SCHEMA",
         "columns": [{"name": "ORDER_ID", "type": "NUMBER"},
                     {"name": "AMOUNT", "type": "NUMBER"}]},
        {"name": "SLV_ORDERS", "schema": "SILVER_SCHEMA",
         "columns": [{"name": "ORDER_ID", "type": "NUMBER"},
                     {"name": "AMOUNT", "type": "NUMBER"}]},
    ],
    "procedures": [
        {"name": "prc_load_slv_orders", "schema": "SILVER_SCHEMA",
         "kind": "procedure",
         "body": "PROCEDURE prc_load_slv_orders IS\nBEGIN\n"
                 "  INSERT INTO SILVER_SCHEMA.SLV_ORDERS (ORDER_ID, AMOUNT)\n"
                 "  SELECT ORDER_ID, AMOUNT FROM RAW_SCHEMA.ORDERS;\n"
                 "END;"},
    ],
}


def test_procedure_built_table_is_not_landed_either(tmp_path):
    """A table rebuilt by a PROCEDURE is duplicated exactly as one rebuilt by
    an ETL mapping. Procedures only warned about it, leaving the landing layer
    still copying a table the generated models rebuild."""
    out = tmp_path / "out"
    report = scaffold("oracle", "snowflake",
                      _manifest(tmp_path, PROC_MANIFEST), str(out), "procs")
    models = sorted(f.parent.name + "/" + f.name
                    for f in (out / "dbt" / "models").rglob("*.sql"))
    ddl = (out / "ddl" / "01_create_landing.sql").read_text(encoding="utf-8")
    unload = (out / "ddl" / "02_unload_from_oracle.sql").read_text(
        encoding="utf-8")

    assert "staging/stg_orders.sql" in models          # raw still landed
    assert "ORDERS" in ddl
    assert "staging/stg_slv_orders.sql" not in models  # rebuilt, so not landed
    assert "SLV_ORDERS" not in ddl
    assert "SLV_ORDERS" not in unload
    assert [t["table"] for t in report["procedures"]["not_landed"]] == \
        ["SLV_ORDERS"]


def test_land_flag_keeps_procedure_targets_too(tmp_path):
    out = tmp_path / "out"
    scaffold("oracle", "snowflake", _manifest(tmp_path, PROC_MANIFEST),
             str(out), "procs", land_etl_targets=True)
    ddl = (out / "ddl" / "01_create_landing.sql").read_text(encoding="utf-8")
    assert "SLV_ORDERS" in ddl


# --- oracle unload uploads itself ------------------------------------------

def test_oracle_unload_uploads_when_a_stage_uri_is_known(tmp_path):
    """SPOOL is a client-side file write with no object-storage driver, so an
    Oracle export always lands locally. Shelling out to the AWS CLI from the
    same script keeps it one run — otherwise the CSVs sit on disk and the
    load step finds an empty prefix."""
    out = tmp_path / "out"
    scaffold("oracle", "snowflake", _manifest(tmp_path, PROC_MANIFEST),
             str(out), "up",
             movement={"stage_uri": "s3://bucket/prefix"})
    sql = (out / "ddl" / "02_unload_from_oracle.sql").read_text(encoding="utf-8")
    assert "HOST aws s3 cp ./mb_export s3://bucket/prefix/ --recursive" in sql
    # the upload can only run after every SPOOL has closed
    assert sql.index("HOST aws s3 cp") > sql.rindex("SPOOL OFF")


def test_oracle_upload_stays_commented_without_a_stage_uri(tmp_path):
    """A placeholder URI would upload into a path that does not exist, so the
    step is shown rather than run."""
    out = tmp_path / "out"
    scaffold("oracle", "snowflake", _manifest(tmp_path, PROC_MANIFEST),
             str(out), "noup")
    sql = (out / "ddl" / "02_unload_from_oracle.sql").read_text(encoding="utf-8")
    assert "\nHOST aws s3 cp" not in sql
    assert "--   HOST aws s3 cp" in sql


# --- the load carries its own stage setup ----------------------------------

def test_bundle_ships_the_stage_ddl_it_needs(tmp_path):
    """Naming a stage does not create one. Without this DDL somewhere in the
    package, the first run stops at 'Stage does not exist' with no statement
    anywhere to fix it."""
    out = tmp_path / "out"
    scaffold("oracle", "snowflake", _manifest(tmp_path, PROC_MANIFEST),
             str(out), "stage",
             movement={"stage_uri": "s3://bucket/prefix",
                       "target_stage": "MB_LANDING_STAGE"})
    readme = (out / "ddl" / "README.md").read_text(encoding="utf-8")
    assert "CREATE STAGE MB_LANDING_STAGE" in readme
    assert "URL = 's3://bucket/prefix/'" in readme
    assert "STORAGE_INTEGRATION" in readme


def test_the_load_itself_stays_free_of_bucket_and_credentials(tmp_path):
    """The named-stage load is the only form that writes neither a key nor a
    bucket. Putting the stage's own DDL beside it would hand back exactly
    what the setting removed — so that belongs in the README, not here."""
    out = tmp_path / "out"
    scaffold("oracle", "snowflake", _manifest(tmp_path, PROC_MANIFEST),
             str(out), "stage2",
             movement={"stage_uri": "s3://bucket/prefix",
                       "target_stage": "MB_LANDING_STAGE"})
    sql = (out / "ddl" / "03_load_into_snowflake.sql").read_text(encoding="utf-8")
    assert "FROM @MB_LANDING_STAGE/" in sql
    assert "CREDENTIALS" not in sql
    assert "s3://" not in sql


def test_readme_says_how_to_get_a_stage_when_none_is_set(tmp_path):
    out = tmp_path / "out"
    scaffold("oracle", "snowflake", _manifest(tmp_path, PROC_MANIFEST),
             str(out), "nostage", movement={"stage_uri": "s3://bucket/prefix"})
    readme = (out / "ddl" / "README.md").read_text(encoding="utf-8")
    sql = (out / "ddl" / "03_load_into_snowflake.sql").read_text(encoding="utf-8")
    assert "CREATE STAGE MB_LANDING_STAGE" in readme
    assert "Named target stage" in readme
    # still the un-configured form, but spelled out so it can be acted on
    assert "AWS_KEY_ID='<aws-key-id>'" in sql


# --- unit ------------------------------------------------------------------

def test_unland_removes_source_and_its_staging_model():
    """Two removals are needed: the source drives the landing DDL, but the
    scaffold also gives every manifest table a pass-through staging mapping —
    dropping only the source leaves a model selecting from nothing."""
    from metabridge.ir.model import (
        Mapping, Pipeline, SourceTable, Transformation, TransformationType)

    pipeline = Pipeline(name="p", source_format="scaffold")
    pipeline.sources.append(SourceTable(name="DIM_CUSTOMER", schema="SILVER"))
    stg = Mapping(name="stg_dim_customer")
    stg.transformations.append(Transformation(
        name="src", type=TransformationType.SOURCE,
        properties={"table": "DIM_CUSTOMER"}))
    pipeline.mappings.append(stg)

    logic = Pipeline(name="l", source_format="idmc")
    built = Mapping(name="DIM_CUSTOMER")
    built.transformations.append(Transformation(
        name="tgt", type=TransformationType.TARGET,
        properties={"table": "DIM_CUSTOMER"}))
    logic.mappings.append(built)

    dropped = unland_built_tables(pipeline, {"dim_customer": "DIM_CUSTOMER"},
                                  {"dim_customer": "stg_dim_customer"})
    assert [d["table"] for d in dropped] == ["DIM_CUSTOMER"]
    assert pipeline.sources == []
    assert [m.name for m in pipeline.mappings] == []


def test_target_scan_ignores_the_scaffolds_own_staging_mappings():
    """Every scaffold staging mapping declares a TARGET of its own
    (`TGT_stg_accounts`), so an unrestricted scan finds every table 'built'
    and unlands the entire landing layer."""
    from metabridge.ir.model import (
        Mapping, Pipeline, Transformation, TransformationType)

    pipeline = Pipeline(name="p", source_format="scaffold")
    stg = Mapping(name="stg_accounts")
    stg.transformations.append(Transformation(
        name="TGT_stg_accounts", type=TransformationType.TARGET,
        properties={"table": "ACCOUNTS"}))
    built = Mapping(name="DIM_CUSTOMER")
    built.transformations.append(Transformation(
        name="tgt", type=TransformationType.TARGET,
        properties={"table": "DIM_CUSTOMER"}))
    pipeline.mappings += [stg, built]

    assert _target_tables(pipeline) == {"accounts": "stg_accounts",
                                        "dim_customer": "DIM_CUSTOMER"}
    assert _target_tables(pipeline, only={"DIM_CUSTOMER"}) == \
        {"dim_customer": "DIM_CUSTOMER"}


def test_conversion_report_lists_what_the_project_builds():
    """Modernize-only runs cannot detect the double-build, so the report names
    the tables in front of the person who can act on it."""
    from metabridge.parsers.idmc_parser import parse_idmc
    from metabridge.report.reporter import build_report

    tmp = pathlib.Path(tempfile.mkdtemp())
    pl = parse_idmc(_bundle(tmp))
    report = build_report(pl, "snowflake")
    assert [t["table"] for t in report["produced_tables"]] == ["DIM_CUSTOMER"]
    assert "exclude them from the landing layer" in report["produced_tables_note"]

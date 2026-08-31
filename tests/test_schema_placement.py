"""A migrated estate keeps its schemas.

dbt derives nothing from a directory name. A project can have a perfect
staging/ intermediate/ marts/ tree and still build every model into one
schema, because the only thing that places a model is `+schema:` — and dbt's
own `generate_schema_name` then CONCATENATES that onto the profile's schema,
so `+schema: SILVER_SCHEMA` against a profile pointed at ANALYTICS builds
ANALYTICS_SILVER_SCHEMA and nothing downstream finds it.

So three things have to line up, and each is tested here:

  * `dbt_project.yml` says which schema each folder builds into
  * a `generate_schema_name` override makes dbt take that name literally
  * the landing DDL creates those schemas, since dbt creates none it was not
    pointed at

The failure this prevents is silent: an Oracle estate with RAW_SCHEMA,
SILVER_SCHEMA and GOLD_SCHEMA converts, runs, and arrives as one flat schema.
"""
import os
import pathlib
import tempfile

import yaml

from metabridge.generators.dbt_generator import generate_dbt_project
from metabridge.ir.model import (Link, LoadStrategy, Mapping, Pipeline, Port,
                                 SourceTable, Transformation,
                                 TransformationType)

COLS = ["CUSTOMER_ID", "EMAIL"]


def _tx(name, ttype, **props):
    return Transformation(name=name, type=ttype,
                          ports=[Port(name=c) for c in COLS],
                          properties=props)


def _mapping(name, src_table, src_schema, tgt_table, tgt_schema):
    m = Mapping(name=name, load_strategy=LoadStrategy.FULL)
    m.transformations = [
        _tx("SRC", TransformationType.SOURCE,
            table=src_table, schema=src_schema),
        _tx("EXP", TransformationType.EXPRESSION),
        _tx("TGT", TransformationType.TARGET,
            table=tgt_table, schema=tgt_schema),
    ]
    m.links = [Link("SRC", "EXP"), Link("EXP", "TGT")]
    return m


def _estate():
    """RAW -> SILVER -> GOLD, the shape a real three-tier Oracle estate has."""
    p = Pipeline(name="oracle_to_snowflake", source_format="idmc")
    p.metadata["dialect"] = "snowflake"
    p.sources = [SourceTable(name="CUSTOMER", schema="RAW_SCHEMA",
                             system="RAW_SCHEMA",
                             columns=[Port(name=c) for c in COLS])]
    p.mappings = [
        _mapping("load_final", "CUSTOMER", "RAW_SCHEMA",
                 "FINAL_CUSTOMER", "SILVER_SCHEMA"),
        _mapping("load_analysis", "FINAL_CUSTOMER", "SILVER_SCHEMA",
                 "ANALYSIS", "GOLD_SCHEMA"),
    ]
    return p


def _generate(pipeline):
    out = pathlib.Path(tempfile.mkdtemp()) / "dbt"
    generate_dbt_project(pipeline, str(out))
    return out


def _project(out):
    return yaml.safe_load((out / "dbt_project.yml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------

def test_each_folder_declares_the_schema_the_estate_declared():
    out = _generate(_estate())
    marts = _project(out)["models"]["oracle_to_snowflake"]["marts"]
    assert marts["silver_schema"]["+schema"] == "SILVER_SCHEMA"
    assert marts["gold_schema"]["+schema"] == "GOLD_SCHEMA"


def test_the_schema_name_is_taken_literally():
    """dbt's default macro concatenates. Left alone, `+schema: SILVER_SCHEMA`
    builds <target.schema>_SILVER_SCHEMA — a schema the landing DDL never
    created and nothing references."""
    out = _generate(_estate())
    macro = (out / "macros" / "generate_schema_name.sql").read_text(
        encoding="utf-8")
    assert "macro generate_schema_name" in macro
    assert "custom_schema_name | trim" in macro
    # the concatenating form must NOT survive anywhere in it
    assert "_{{ custom_schema_name" not in macro
    assert "default_schema }}_" not in macro


def test_a_project_with_no_declared_schemas_gets_no_macro():
    """The override is a real behaviour change — dev isolation stops coming
    from the schema suffix. It is only justified when there are schemas to
    reproduce, so a project with none must not carry it."""
    p = _estate()
    for m in p.mappings:
        for t in m.by_type(TransformationType.TARGET):
            t.properties["schema"] = ""
    out = _generate(p)
    assert not (out / "macros" / "generate_schema_name.sql").exists()
    cfg = _project(out)["models"]["oracle_to_snowflake"]
    assert "+schema" not in yaml.safe_dump(cfg)


def test_a_model_that_disagrees_with_its_folder_says_so_itself():
    """Two schemas in one folder cannot be settled by one folder-level line,
    so the models carry it — silently dropping one would put it in the other
    model's schema."""
    p = _estate()
    # force both marts into the same folder by giving them the same origin
    for m in p.mappings:
        m.properties["folder"] = "core"
    out = _generate(p)
    marts = _project(out)["models"]["oracle_to_snowflake"]["marts"]
    assert "+schema" not in yaml.safe_dump(marts)
    bodies = {f.name: f.read_text(encoding="utf-8")
              for f in (out / "models").rglob("*.sql")}
    assert "schema='SILVER_SCHEMA'" in bodies["fct_final_customer.sql"]
    assert "schema='GOLD_SCHEMA'" in bodies["fct_analysis.sql"]


def test_empty_layers_are_not_configured():
    """dbt warns on every run about configuration paths that apply to no
    resources, and a project that greets its owner with a warning teaches
    them to ignore warnings."""
    cfg = _project(_generate(_estate()))["models"]["oracle_to_snowflake"]
    assert "intermediate" not in cfg          # nothing was filed there
    assert "marts" in cfg


# --------------------------------------------------------------------------
# the schemas have to exist
# --------------------------------------------------------------------------

def test_the_landing_ddl_creates_the_schemas_the_models_build_into():
    """dbt creates no schema it was not pointed at, so without this the first
    `dbt run` fails on the first model."""
    from metabridge.generators.ddl_generator import generate_target_ddl
    tmp = pathlib.Path(tempfile.mkdtemp())
    os.environ["METABRIDGE_DATA_DIR"] = str(tmp / "iso")
    p = _estate()
    p.sources[0].columns = [Port(name=c, datatype="string") for c in COLS]
    generate_target_ddl(p, str(tmp / "ddl"), dialect="snowflake")
    ddl = (tmp / "ddl" / "01_create_landing.sql").read_text(encoding="utf-8")
    for schema in ("RAW_SCHEMA", "SILVER_SCHEMA", "GOLD_SCHEMA"):
        assert "CREATE SCHEMA IF NOT EXISTS %s;" % schema in ddl, schema


def test_not_null_survives_the_manifest():
    """Nullability is catalog fact. A landing table that accepts nulls where
    the source rejects them turns a load that should fail loudly into rows
    that quietly break every downstream join."""
    from metabridge.scaffold import scaffold
    tmp = pathlib.Path(tempfile.mkdtemp())
    os.environ["METABRIDGE_DATA_DIR"] = str(tmp / "iso")
    manifest = tmp / "tables.yml"
    manifest.write_text(yaml.safe_dump({"tables": [{
        "name": "CUSTOMER", "schema": "RAW_SCHEMA", "database": "FREEPDB1",
        "columns": [{"name": "CUSTOMER_ID", "type": "NUMBER",
                     "nullable": False},
                    {"name": "EMAIL", "type": "VARCHAR2(120)"}]}]}),
        encoding="utf-8")
    out = tmp / "out"
    scaffold("oracle", "snowflake", str(manifest), str(out), governance=False)
    ddl = (out / "ddl" / "01_create_landing.sql").read_text(encoding="utf-8")
    assert "CUSTOMER_ID" in ddl and "NOT NULL" in ddl
    # a column the manifest does not mark stays nullable: absence of a
    # constraint in the manifest is not evidence of one in the source
    email = next(l for l in ddl.splitlines() if "EMAIL" in l)
    assert "NOT NULL" not in email


# --------------------------------------------------------------------------
# the relation keeps the estate's name
# --------------------------------------------------------------------------

def test_the_relation_keeps_the_legacy_name():
    """A model called fct_analysis builds a relation called FCT_ANALYSIS, and
    everything still reading GOLD_SCHEMA.ANALYSIS stops finding it. `alias`
    decouples the two so consumers migrate on their own schedule."""
    out = _generate(_estate())
    bodies = {f.name: f.read_text(encoding="utf-8")
              for f in (out / "models").rglob("*.sql")}
    assert "alias='ANALYSIS'" in bodies["fct_analysis.sql"]
    assert "alias='FINAL_CUSTOMER'" in bodies["fct_final_customer.sql"]


def test_an_invented_target_gets_no_alias():
    """The scaffold names a landing model stg_customer. No legacy relation is
    called that, so there is nothing to stay compatible with — and aliasing
    it to the table it lands would collide with the landed table itself."""
    p = _estate()
    tgt = p.mappings[0].by_type(TransformationType.TARGET)[0]
    tgt.properties["landed_from"] = "CUSTOMER"
    out = _generate(p)
    body = next((out / "models").rglob("*final_customer*.sql")).read_text(
        encoding="utf-8")
    assert "alias=" not in body


def test_a_target_two_mappings_build_gets_no_alias():
    """One relation cannot take the name for both, and handing it to
    whichever sorted first would be a silent coin flip."""
    p = _estate()
    p.mappings[1].by_type(TransformationType.TARGET)[0].properties.update(
        {"table": "FINAL_CUSTOMER", "schema": "SILVER_SCHEMA"})
    out = _generate(p)
    bodies = [f.read_text(encoding="utf-8")
              for f in (out / "models").rglob("*.sql")]
    assert not any("alias='FINAL_CUSTOMER'" in b for b in bodies)


def test_a_name_needing_quotes_is_not_aliased():
    """dbt writes an alias as a bare identifier, so an alias of ORDER is a
    syntax error rather than a compatible relation."""
    p = _estate()
    p.mappings[1].by_type(TransformationType.TARGET)[0].properties["table"] = \
        "ORDER"
    out = _generate(p)
    bodies = [f.read_text(encoding="utf-8")
              for f in (out / "models").rglob("*.sql")]
    assert not any("alias='ORDER'" in b for b in bodies)


def test_an_alias_never_takes_a_name_a_landed_table_occupies():
    """Landing a table the project also builds IS the parallel-run setup:
    the legacy copy and the rebuilt one, side by side, so they can be diffed.
    Under the alias they are one relation, and the model is a table — so the
    first `dbt run` would CREATE OR REPLACE the copy the comparison exists to
    use. The model keeps its own name until the landed copy goes away."""
    p = _estate()
    # what "land those tables anyway" leaves behind: the built table is still
    # a source, so the landing DDL creates it
    p.sources.append(SourceTable(name="FINAL_CUSTOMER", schema="SILVER_SCHEMA",
                                 system="SILVER_SCHEMA",
                                 columns=[Port(name=c) for c in COLS]))
    out = _generate(p)
    bodies = {f.name: f.read_text(encoding="utf-8")
              for f in (out / "models").rglob("*.sql")}
    assert "alias='FINAL_CUSTOMER'" not in bodies["fct_final_customer.sql"]
    # the one that is NOT landed keeps parity — the guard is per relation,
    # not a switch that turns the feature off
    assert "alias='ANALYSIS'" in bodies["fct_analysis.sql"]


def test_a_landed_table_in_a_different_schema_does_not_block_the_alias():
    """Two relations only collide when they are the SAME relation."""
    p = _estate()
    p.sources.append(SourceTable(name="FINAL_CUSTOMER", schema="ARCHIVE",
                                 system="ARCHIVE",
                                 columns=[Port(name=c) for c in COLS]))
    out = _generate(p)
    body = (out / "models" / "marts" / "silver_schema"
            / "fct_final_customer.sql").read_text(encoding="utf-8")
    assert "alias='FINAL_CUSTOMER'" in body


def test_a_deferred_alias_is_declared_not_silent():
    """Parity quietly not applying looks like a bug. It is a decision, so it
    says so and says what turns it back on."""
    p = _estate()
    p.sources.append(SourceTable(name="FINAL_CUSTOMER", schema="SILVER_SCHEMA",
                                 system="SILVER_SCHEMA",
                                 columns=[Port(name=c) for c in COLS]))
    _generate(p)
    codes = {i.code for i in p.issues}
    assert "RELATION_ALIAS_DEFERRED" in codes
    issue = next(i for i in p.issues if i.code == "RELATION_ALIAS_DEFERRED")
    assert "fct_final_customer" in issue.message
    assert "cutover" in (issue.suggestion or "")

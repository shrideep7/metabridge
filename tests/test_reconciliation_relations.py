"""Reconciliation compares two environments, so it needs two names.

It used one — the legacy target table — on both sides, which holds only while
the migrated object keeps the legacy name. It does not. A dbt project builds
ANALYSIS as `fct_analysis` and lands CUSTOMER as `stg_raw_schema__customer`,
so `SELECT COUNT(*) FROM ANALYSIS` run against the migrated warehouse either
fails to resolve or counts some unrelated relation that happens to be in the
session's schema. Either way the pair diffs clean or errors, and nothing is
actually compared.

The same argument applies to qualification: an unqualified name resolves
against whatever schema the session points at, which on the migrated side is
rarely the one holding the model.
"""
import pathlib
import tempfile

from metabridge.ir.model import (Link, LoadStrategy, Mapping, Pipeline, Port,
                                 SourceTable, Transformation,
                                 TransformationType)
from metabridge.report.testgen import generate_tests

COLS = ["CUSTOMER_ID", "EMAIL"]


def _tx(name, ttype, **props):
    return Transformation(name=name, type=ttype,
                          ports=[Port(name=c) for c in COLS],
                          properties=props)


def _pipeline():
    p = Pipeline(name="oracle_to_snowflake", source_format="idmc")
    p.metadata["dialect"] = "snowflake"
    p.sources = [SourceTable(name="CUSTOMER", schema="RAW_SCHEMA",
                             system="RAW_SCHEMA",
                             columns=[Port(name=c, datatype="string")
                                      for c in COLS])]
    m = Mapping(name="load_analysis", load_strategy=LoadStrategy.FULL)
    m.unique_key = ["CUSTOMER_ID"]
    m.transformations = [
        _tx("SRC", TransformationType.SOURCE,
            table="CUSTOMER", schema="RAW_SCHEMA"),
        _tx("EXP", TransformationType.EXPRESSION),
        _tx("TGT", TransformationType.TARGET,
            table="ANALYSIS", schema="GOLD_SCHEMA"),
    ]
    m.links = [Link("SRC", "EXP"), Link("EXP", "TGT")]
    p.mappings = [m]
    return p


def _sql(doc, test_type):
    t = next(t for pm in doc["mappings"] for t in pm["tests"]
             if t["test_type"] == test_type)
    return t.get("source_sql", ""), t.get("target_sql", "")


def test_the_two_sides_name_different_relations():
    """The model has no legacy counterpart, so the names genuinely differ and
    one cannot stand in for both."""
    doc = generate_tests(_landing_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    source_sql, target_sql = _sql(doc, "row_count")
    assert "RAW_SCHEMA.CUSTOMER" in source_sql
    assert "stg_raw_schema__customer" in target_sql
    assert "stg_raw_schema__customer" not in source_sql


def test_both_sides_are_schema_qualified():
    doc = generate_tests(_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    for test_type in ("row_count", "null_comparison", "duplicate_comparison",
                      "checksum_comparison"):
        for sql in _sql(doc, test_type):
            if sql:
                assert "GOLD_SCHEMA." in sql, (test_type, sql)


def test_target_only_checks_name_the_migrated_relation():
    """pk_uniqueness and the business rules run on the migrated warehouse
    only, so they get the model — naming the legacy table would run the check
    against nothing."""
    doc = generate_tests(_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    _, target_sql = _sql(doc, "pk_uniqueness")
    assert "GOLD_SCHEMA.ANALYSIS" in target_sql


def test_the_schema_comparison_queries_each_side_by_its_own_name():
    """information_schema is looked up by table name, so one name cannot
    serve both sides when they differ."""
    doc = generate_tests(_landing_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    source_sql, target_sql = _sql(doc, "schema_comparison")
    assert "'customer'" in source_sql
    assert "'stg_raw_schema__customer'" in target_sql


def test_without_dbt_the_target_keeps_the_table_name():
    """A warehouse-SQL conversion builds ANALYSIS as ANALYSIS. Renaming it to
    a dbt model that was never generated would be the same bug mirrored."""
    doc = generate_tests(_pipeline(), "oracle", "snowflake")
    source_sql, target_sql = _sql(doc, "row_count")
    assert "GOLD_SCHEMA.ANALYSIS" in source_sql
    assert "GOLD_SCHEMA.ANALYSIS" in target_sql


def test_a_landing_mapping_reconciles_against_its_source_table():
    """The scaffold invents `stg_customer` to name a landing model. Nothing
    in the legacy estate is called that, so the legacy side has to be the
    table the model lands — which is also the only thing it reproduces 1:1,
    so the counts and checksums genuinely compare."""
    import os
    import yaml
    from metabridge.scaffold import scaffold
    tmp = pathlib.Path(tempfile.mkdtemp())
    os.environ["METABRIDGE_DATA_DIR"] = str(tmp / "iso")
    manifest = tmp / "tables.yml"
    manifest.write_text(yaml.safe_dump({"tables": [{
        "name": "CUSTOMER", "schema": "RAW_SCHEMA", "database": "FREEPDB1",
        "unique_key": ["CUSTOMER_ID"],
        "columns": [{"name": "CUSTOMER_ID", "type": "NUMBER"},
                    {"name": "EMAIL", "type": "VARCHAR2(120)"}]}]}),
        encoding="utf-8")
    out = tmp / "out"
    scaffold("oracle", "snowflake", str(manifest), str(out), governance=False)
    recon = out / "validation_tests" / "reconciliation"
    legacy = next(recon.glob("*.legacy_*.sql")).read_text(encoding="utf-8")
    migrated = next(recon.glob("*.migrated_*.sql")).read_text(encoding="utf-8")
    assert "FROM RAW_SCHEMA.CUSTOMER" in legacy
    assert "FROM stg_customer" not in legacy      # a table that exists nowhere
    assert "stg_raw_schema__customer" in migrated


def test_a_schema_the_generator_cannot_know_is_named_not_omitted():
    """A staging model builds into the schema the dbt PROFILE names, and the
    generated profile reads that from an environment variable — so it is not
    knowable here. Unqualified, the query still runs: against whatever the
    session's schema happens to be, reconciling the wrong relation or none
    and reporting neither. A placeholder fails loudly instead."""
    doc = generate_tests(_landing_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    _, target_sql = _sql(doc, "row_count")
    assert "{{TARGET_SCHEMA}}.stg_raw_schema__customer" in target_sql


def test_a_known_schema_is_used_rather_than_the_placeholder():
    doc = generate_tests(_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    _, target_sql = _sql(doc, "row_count")
    assert "GOLD_SCHEMA.ANALYSIS" in target_sql
    assert "TARGET_SCHEMA" not in target_sql


def test_the_schema_comparison_pins_the_schema_too():
    """information_schema is per-database. Filtering on table name alone was
    safe only while one schema existed — with RAW/SILVER/GOLD in one
    database, or a parallel-run copy of the same table, the lookup returns
    several tables' columns and the comparison reads whichever comes first."""
    doc = generate_tests(_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    source_sql, target_sql = _sql(doc, "schema_comparison")
    assert "LOWER(table_schema) = 'gold_schema'" in target_sql
    # Oracle's catalog spells it differently, and the wrong column name is a
    # query that errors rather than one that over-matches
    assert "LOWER(owner) = 'gold_schema'" in source_sql


def test_no_schema_predicate_is_better_than_a_guessed_one():
    """A predicate against a schema we invented returns zero rows, which
    reads as 'this table has no columns' rather than as 'I cannot tell'."""
    doc = generate_tests(_landing_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    _, target_sql = _sql(doc, "schema_comparison")
    assert "table_schema" not in target_sql


def _landing_pipeline():
    """The scaffold's synthesised landing mapping: a target it invented, with
    no schema of its own."""
    p = _pipeline()
    m = p.mappings[0]
    tgt = m.by_type(TransformationType.TARGET)[0]
    tgt.properties.update({"table": "stg_customer", "schema": "",
                           "landed_from": "CUSTOMER",
                           "landed_from_schema": "RAW_SCHEMA"})
    m.name = "stg_customer"
    return p


def test_reconciliation_queries_the_aliased_relation():
    """The migrated model is called fct_analysis but builds ANALYSIS, so a
    query naming the model finds nothing. Both sides converge on the same
    name here — but because the relation really is called that on both, not
    because the generator assumed it."""
    doc = generate_tests(_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    source_sql, target_sql = _sql(doc, "row_count")
    assert "GOLD_SCHEMA.ANALYSIS" in target_sql
    assert "fct_analysis" not in target_sql
    assert "GOLD_SCHEMA.ANALYSIS" in source_sql


def test_the_schema_comparison_looks_up_the_relation_not_the_model():
    """information_schema holds relations. fct_analysis is not one."""
    doc = generate_tests(_pipeline(), "oracle", "snowflake",
                         target_format="dbt")
    _, target_sql = _sql(doc, "schema_comparison")
    assert "'analysis'" in target_sql
    assert "fct_analysis" not in target_sql

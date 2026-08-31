"""Identifier quoting: only where it is needed, and the same rule everywhere.

Quoting indiscriminately is not the safe option it looks like. An unquoted
identifier folds to UPPER case on Snowflake and Oracle and a quoted one does
not, so `"customer_id"` stops matching a column stored as CUSTOMER_ID —
quoting everything breaks far more columns than it rescues.

And whatever the rule is, the landing DDL and the models that read it have to
share it: a table created with a bare `ORDER` column and a model selecting
`"order"` disagree about a column that exists.
"""
import os
import pathlib
import tempfile

import yaml

from metabridge.generators.ddl_generator import _quote
from metabridge.sqlx.identifiers import needs_quoting, quote_identifier


def test_ordinary_identifiers_are_left_bare():
    """The case-folding trap: these must not acquire quotes."""
    for name in ("CUSTOMER_ID", "first_name", "amount", "col1", "A$B", "X#Y"):
        assert not needs_quoting(name), name
        assert quote_identifier("snowflake", name) == name


def test_reserved_words_are_quoted():
    for name in ("ORDER", "order", "Group", "SELECT", "USER", "DATE", "TABLE"):
        assert needs_quoting(name), name
    assert quote_identifier("snowflake", "ORDER") == '"ORDER"'


def test_characters_illegal_unquoted_are_quoted():
    for name in ("USER-ID", "my col", "2fast", "a.b"):
        assert needs_quoting(name), name


def test_the_quote_character_follows_the_dialect():
    assert quote_identifier("snowflake", "ORDER") == '"ORDER"'
    assert quote_identifier("postgres", "ORDER") == '"ORDER"'
    assert quote_identifier("bigquery", "ORDER") == "`ORDER`"
    assert quote_identifier("databricks", "ORDER") == "`ORDER`"
    assert quote_identifier("tsql", "ORDER") == "[ORDER]"


def test_an_embedded_quote_is_escaped():
    assert quote_identifier("snowflake", 'a"b') == '"a""b"'
    assert quote_identifier("bigquery", "a`b") == "`a``b`"
    assert quote_identifier("tsql", "a]b") == "[a]]b]"


def test_declared_spelling_is_preserved():
    """The name came from the source catalog, so it IS what the physical
    column is called — re-casing it would break the very lookup quoting is
    meant to protect."""
    assert quote_identifier("snowflake", "Order") == '"Order"'


def test_the_ddl_generator_uses_the_same_rule():
    """Not a coincidence to be maintained by hand — one function."""
    for name in ("ORDER", "CUSTOMER_ID", "USER-ID"):
        assert _quote("snowflake", name) == quote_identifier("snowflake", name)


def test_models_and_landing_ddl_agree_end_to_end():
    """The property that actually matters: whatever a column is called, the
    table the DDL creates and the model that selects from it spell it the
    same way."""
    from metabridge.scaffold import scaffold
    tmp = pathlib.Path(tempfile.mkdtemp())
    os.environ["METABRIDGE_DATA_DIR"] = str(tmp / "iso")
    manifest = tmp / "tables.yml"
    manifest.write_text(yaml.safe_dump({"tables": [{
        "name": "ORDERS", "schema": "RAW", "database": "DB",
        "columns": [{"name": "ORDER", "type": "NUMBER"},
                    {"name": "GROUP", "type": "VARCHAR2(10)"},
                    {"name": "USER-ID", "type": "VARCHAR2(10)"},
                    {"name": "AMOUNT", "type": "NUMBER(12,2)"}]}]}),
        encoding="utf-8")
    out = tmp / "out"
    scaffold("oracle", "snowflake", str(manifest), str(out), governance=False)

    model = next((out / "dbt").rglob("models/**/*.sql")).read_text(
        encoding="utf-8")
    ddl = (out / "ddl" / "01_create_landing.sql").read_text(encoding="utf-8")
    for name in ('"ORDER"', '"GROUP"', '"USER-ID"'):
        assert name in model, (name, model)
        assert name in ddl, (name, ddl)
    # ...and the ordinary one is bare on BOTH sides, or it would stop
    # resolving once the engine folded one of them
    assert "AMOUNT" in model and '"AMOUNT"' not in model
    assert '"AMOUNT"' not in ddl


def test_a_column_needing_quotes_is_flagged_for_dbt_tests():
    """dbt injects a generic test's column into SQL RAW —
    `select {{ column_name }} as unique_field` — and quotes it only when the
    column carries `quote: true`. Without the flag a unique/not_null test on a
    column called ORDER compiles to `select ORDER as unique_field`, which is a
    syntax error on every engine.
    """
    import yaml
    from metabridge.scaffold import scaffold
    tmp = pathlib.Path(tempfile.mkdtemp())
    os.environ["METABRIDGE_DATA_DIR"] = str(tmp / "iso")
    manifest = tmp / "tables.yml"
    manifest.write_text(yaml.safe_dump({"tables": [{
        "name": "ORDERS", "schema": "RAW", "database": "DB",
        "unique_key": ["ORDER"],
        "columns": [{"name": "ORDER", "type": "NUMBER"},
                    {"name": "AMOUNT", "type": "NUMBER(12,2)"}]}]}),
        encoding="utf-8")
    out = tmp / "out"
    scaffold("oracle", "snowflake", str(manifest), str(out), governance=False)
    doc = yaml.safe_load(
        next((out / "dbt").rglob("_*__models.yml")).read_text(encoding="utf-8"))
    cols = {c["name"]: c for m in doc["models"] for c in m["columns"]}
    assert cols["ORDER"].get("quote") is True
    assert cols["ORDER"].get("tests")          # the flag has to reach a test
    # an ordinary column must NOT be flagged: quoting it would make it
    # case-sensitive and stop it matching the folded column in the warehouse
    assert "quote" not in cols["AMOUNT"]

"""Semantic function registry: YAML-driven, spec examples, hygiene, guards."""
import pytest

from metabridge.sqlx.registry import (
    CATEGORIES, PLATFORMS, get_function_registry,
)

reg = get_function_registry()


# ---------------------------------------------------------------------------
# Spec examples — exactly as requested
# ---------------------------------------------------------------------------

def test_null_coalesce_spec_example():
    f = reg.lookup("NULL_COALESCE")
    assert f.mapping("oracle").template == "NVL"
    for p in ("snowflake", "databricks", "bigquery", "redshift", "synapse",
              "sqlserver", "postgres", "teradata", "ansi"):
        assert f.mapping(p).template == "COALESCE", p
    assert f.render("oracle", ["customer_name", "'UNKNOWN'"]) == \
        "NVL(customer_name, 'UNKNOWN')"
    assert f.render("snowflake", ["customer_name", "'UNKNOWN'"]) == \
        "COALESCE(customer_name, 'UNKNOWN')"
    assert f.render("informatica", ["customer_name", "'UNKNOWN'"]) == \
        "IIF(ISNULL(customer_name), 'UNKNOWN', customer_name)"


def test_current_datetime_spec_example():
    f = reg.lookup("CURRENT_DATETIME")
    assert f.mapping("oracle").template == "SYSDATE"
    assert f.mapping("snowflake").template == "CURRENT_TIMESTAMP()"
    assert f.mapping("redshift").template == "GETDATE()"
    assert f.mapping("sqlserver").template == "GETDATE()"
    assert f.mapping("postgres").template == "CURRENT_TIMESTAMP"
    assert f.mapping("teradata").template == "CURRENT_TIMESTAMP"


def test_string_length_spec_example():
    f = reg.lookup("STRING_LENGTH")
    assert f.mapping("oracle").template == "LENGTH"
    assert f.mapping("redshift").template == "LEN"
    assert f.mapping("sqlserver").template == "LEN"
    assert f.mapping("teradata").template == "CHAR_LENGTH"
    assert f.render("redshift", ["name"]) == "LEN(name)"
    assert f.render("teradata", ["name"]) == "CHAR_LENGTH(name)"


# ---------------------------------------------------------------------------
# Coverage requirements
# ---------------------------------------------------------------------------

def test_all_required_categories_present():
    cats = set(reg.categories())
    assert {"null", "date", "timestamp", "string", "numeric", "conversion",
            "json", "array", "regex", "window", "hash",
            "conditional"} <= cats


def test_registry_size_and_platform_coverage():
    assert len(reg.all()) >= 45
    matrix = reg.coverage_matrix()
    for p in PLATFORMS:
        covered = matrix["platforms"][p]["supported"] + \
            matrix["platforms"][p]["workaround"]
        assert covered >= 40, "%s only covers %d functions" % (p, covered)


def test_registry_is_valid():
    assert reg.validate() == []


# ---------------------------------------------------------------------------
# Rendering behavior
# ---------------------------------------------------------------------------

def test_template_rendering():
    assert reg.render("STRING_POSITION", "sqlserver", ["name", "'x'"]) == \
        "CHARINDEX('x', name)"          # argument reorder via template
    assert reg.render("DATE_ADD_INTERVAL", "informatica",
                      ["order_date", "DD", "7"]) == \
        "ADD_TO_DATE(order_date, 'DD', 7)"
    assert reg.render("HASH_SHA256", "bigquery", ["email"]) == \
        "TO_HEX(SHA256(email))"


def test_unsupported_raises_with_workaround():
    with pytest.raises(ValueError, match="workaround"):
        reg.render("REGEX_MATCH", "sqlserver", ["a", "'x'"])
    m = reg.lookup("REGEX_MATCH").mapping("sqlserver")
    assert m.supported is False and m.workaround


def test_unknown_lookups():
    with pytest.raises(KeyError):
        reg.lookup("NOT_A_FUNCTION")
    assert reg.has("null_coalesce")     # case-insensitive
    assert not reg.has("nope")


# ---------------------------------------------------------------------------
# No-hardcoding guards: code paths must agree with the registry
# ---------------------------------------------------------------------------

def test_expression_transpiler_agrees_with_registry():
    """The Informatica transpiler and the registry must express the same
    mapping for NULL_COALESCE — the registry is the declared truth."""
    from metabridge.sqlx.expressions import sql_to_infa
    assert sql_to_infa("COALESCE(a, b)") == \
        reg.render("NULL_COALESCE", "informatica", ["a", "b"])
    assert sql_to_infa("CURRENT_TIMESTAMP") == \
        reg.lookup("CURRENT_DATETIME").mapping("informatica").template


def test_semantic_layer_agrees_with_registry():
    """Module 3's semantic parser classifies into names the registry knows."""
    from metabridge.cir.semantic import parse_expression
    sem = parse_expression("NVL(a, b)", dialect="oracle")
    assert reg.has(sem.function_type.value)  # NULL_COALESCE is registered


def test_full_catalog_serializes():
    import json
    doc = [f.to_dict() for f in reg.all()]
    blob = json.dumps(doc)
    assert len(blob) > 10000

"""Data type mapping engine: parse/render/convert + warning detection."""
import pytest

from metabridge.sqlx.type_engine import (
    CANONICAL_TYPES, TYPE_PLATFORMS, CanonicalType, get_type_engine,
)

eng = get_type_engine()


# ---------------------------------------------------------------------------
# The spec example, verbatim
# ---------------------------------------------------------------------------

def test_oracle_number_to_databricks_decimal_spec_example():
    r = eng.convert_type("NUMBER(38,10)", "oracle", "databricks")
    assert r["canonical"] == {"name": "DECIMAL", "precision": 38, "scale": 10}
    assert r["target_type"] == "DECIMAL(38,10)"
    assert r["warnings"] == []          # perfect fidelity, no noise


# ---------------------------------------------------------------------------
# Parsing (native -> canonical)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("native,platform,expect", [
    ("VARCHAR2(100)", "oracle", ("STRING", None, None, 100)),
    ("NVARCHAR(50)", "sqlserver", ("STRING", None, None, 50)),
    ("TEXT", "postgres", ("STRING", None, None, None)),
    ("CHAR(3)", "teradata", ("FIXED_STRING", None, None, 3)),
    ("NUMBER(18,2)", "oracle", ("DECIMAL", 18, 2, None)),
    ("NUMERIC(10,0)", "postgres", ("DECIMAL", 10, 0, None)),
    ("BIGNUMERIC(76,38)", "bigquery", ("DECIMAL", 76, 38, None)),
    ("BIGINT", "snowflake", ("BIG_INTEGER", None, None, None)),
    ("INT64", "bigquery", ("BIG_INTEGER", None, None, None)),
    ("DOUBLE PRECISION", "redshift", ("FLOAT", None, None, None)),
    ("BINARY_DOUBLE", "oracle", ("FLOAT", None, None, None)),
    ("BIT", "sqlserver", ("BOOLEAN", None, None, None)),
    ("DATE", "snowflake", ("DATE", None, None, None)),
    ("TIME", "postgres", ("TIME", None, None, None)),
    ("DATETIME2", "sqlserver", ("TIMESTAMP", None, None, None)),
    ("TIMESTAMP_NTZ", "snowflake", ("TIMESTAMP", None, None, None)),
    ("TIMESTAMP WITH TIME ZONE", "oracle", ("TIMESTAMP_TZ", None, None, None)),
    ("DATETIMEOFFSET", "sqlserver", ("TIMESTAMP_TZ", None, None, None)),
    ("TIMESTAMPTZ", "postgres", ("TIMESTAMP_TZ", None, None, None)),
    ("RAW(2000)", "oracle", ("BINARY", None, None, 2000)),
    ("JSONB", "postgres", ("JSON", None, None, None)),
    ("VARIANT", "snowflake", ("VARIANT", None, None, None)),
    ("SUPER", "redshift", ("VARIANT", None, None, None)),
    ("GEOGRAPHY", "bigquery", ("GEOGRAPHY", None, None, None)),
])
def test_parse_type(native, platform, expect):
    ct, warnings = eng.parse_type(native, platform)
    assert (ct.name, ct.precision, ct.scale, ct.length) == expect
    assert warnings == []


def test_parse_unknown_type_warns_and_defaults():
    ct, warnings = eng.parse_type("FROBNICATOR(9)", "oracle")
    assert ct.name == "STRING"
    assert warnings[0].code == "unknown_type"
    assert warnings[0].severity == "MANUAL"


def test_parse_cross_platform_fallback():
    # T-SQL type pasted into an "oracle" file still resolves
    ct, _ = eng.parse_type("NVARCHAR(50)", "oracle")
    assert ct.name == "STRING" and ct.length == 50


# ---------------------------------------------------------------------------
# Warning detection
# ---------------------------------------------------------------------------

def test_precision_loss():
    r = eng.convert_type("BIGNUMERIC(76,38)", "bigquery", "snowflake")
    codes = {w["code"] for w in r["warnings"]}
    assert "precision_loss" in codes
    assert r["target_type"] == "NUMBER(38,37)"  # clamped, and it says so


def test_scale_loss():
    r = eng.convert_type("NUMBER(38,50)", "oracle", "sqlserver")
    codes = {w["code"] for w in r["warnings"]}
    assert "scale_loss" in codes


def test_timezone_change_to_informatica():
    r = eng.convert_type("TIMESTAMP WITH TIME ZONE", "oracle", "informatica")
    codes = {w["code"] for w in r["warnings"]}
    assert "timezone_change" in codes
    assert r["target_type"] == "date/time"


def test_timezone_preserved_where_supported():
    r = eng.convert_type("TIMESTAMPTZ", "postgres", "sqlserver")
    assert r["target_type"] == "DATETIMEOFFSET"
    assert not [w for w in r["warnings"] if w["code"] == "timezone_change"]


def test_unsupported_type_with_fallback():
    r = eng.convert_type("TIME", "postgres", "databricks")
    w = next(w for w in r["warnings"] if w["code"] == "unsupported_type")
    assert "STRING" in r["target_type"]
    assert "HH:MM:SS" in w["message"]   # the workaround travels with the warning


def test_implicit_conversion():
    r = eng.convert_type("BOOLEAN", "postgres", "oracle")
    assert r["target_type"] == "NUMBER(1)"
    assert any(w["code"] == "implicit_conversion" for w in r["warnings"])


def test_length_overflow():
    r = eng.convert_type("VARCHAR(100000)", "snowflake", "redshift")
    w = next(w for w in r["warnings"] if w["code"] == "length_overflow")
    assert "65535" in w["message"]
    assert r["target_type"] == "VARCHAR(65535)"  # clamped to the platform cap


# ---------------------------------------------------------------------------
# Registry hygiene + coverage
# ---------------------------------------------------------------------------

def test_all_18_canonical_types_and_platforms():
    assert len(CANONICAL_TYPES) == 18
    assert eng.validate() == []          # every type x every platform declared
    matrix = eng.matrix()
    assert set(matrix) == set(CANONICAL_TYPES)
    for cname, row in matrix.items():
        assert set(row) == set(TYPE_PLATFORMS), cname


def test_render_bare_types():
    t, _ = eng.render_type(CanonicalType("STRING"), "snowflake")
    assert t == "VARCHAR"
    t, _ = eng.render_type(CanonicalType("DECIMAL"), "databricks")
    assert t == "DECIMAL(38,6)"


def test_ir_canonical_map_agrees_with_engine():
    """No-hardcode guard: ir.model.canonical_type must agree with the engine
    for the types both understand."""
    from metabridge.ir.model import canonical_type
    pairs = [("varchar(100)", "STRING"), ("number", "DECIMAL"),
             ("bigint", "BIG_INTEGER"), ("timestamp", "TIMESTAMP"),
             ("date", "DATE"), ("boolean", "BOOLEAN")]
    ir_to_engine = {"string": "STRING", "decimal": "DECIMAL",
                    "bigint": "BIG_INTEGER", "timestamp": "TIMESTAMP",
                    "date": "DATE", "boolean": "BOOLEAN"}
    for native, expected in pairs:
        assert ir_to_engine[canonical_type(native)] == expected
        ct, _ = eng.parse_type(native, "ansi")
        assert ct.name == expected


def test_convert_serializable():
    import json
    json.dumps(eng.convert_type("NUMBER(38,10)", "oracle", "databricks"))

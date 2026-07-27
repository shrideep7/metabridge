"""Phase 3 §10/§11: legacy function + datatype registries, pinned to the
live engine so they cannot drift."""
import pytest
import sqlglot

from metabridge.sqlx.legacy_normalize import normalize_legacy_statement
from metabridge.sqlx.legacy_registry import (TARGETS,
                                             get_legacy_datatype_registry,
                                             get_legacy_function_registry)

FN = get_legacy_function_registry()
DT = get_legacy_datatype_registry()
_READ = {"oracle": "oracle", "teradata": "teradata", "sqlserver": "tsql"}


def test_registries_validate_clean_and_cover_spec_minimums():
    assert FN.validate() == []
    assert DT.validate() == []
    assert len(FN.all()) >= 36
    for d, fns in {
        "oracle": ["NVL", "NVL2", "DECODE", "TO_DATE", "TO_CHAR",
                   "TO_NUMBER", "TRUNC", "ADD_MONTHS", "MONTHS_BETWEEN",
                   "LAST_DAY", "LISTAGG", "REGEXP_REPLACE"],
        "teradata": ["ZEROIFNULL", "NULLIFZERO", "OREPLACE", "OTRANSLATE",
                     "INDEX", "SUBSTR", "CAST", "RANDOM", "CURRENT_DATE",
                     "CURRENT_TIMESTAMP"],
        "sqlserver": ["ISNULL", "IIF", "CHOOSE", "GETDATE", "DATEADD",
                      "DATEDIFF", "EOMONTH", "FORMAT", "CONVERT",
                      "TRY_CONVERT", "TRY_CAST", "STRING_AGG",
                      "CHARINDEX", "PATINDEX"],
    }.items():
        for f in fns:
            assert FN.get(d, f) is not None, "%s.%s" % (d, f)


@pytest.mark.parametrize("row", FN.all(),
                         ids=lambda r: "%s.%s" % (r["source_dialect"],
                                                  r["source_function"]))
def test_every_automatic_mapping_pinned_to_engine(row):
    """No drift: what the registry documents per target must be exactly
    what normalize+transpile produces today."""
    if row["automation"] == "manual":
        return
    read = _READ[row["source_dialect"]]
    stmt = sqlglot.parse_one(row["example"], read=read)
    stmt, _f, _fl = normalize_legacy_statement(stmt, read)
    for tgt_label, expected in row["target_implementations"].items():
        if expected == "MANUAL_REVIEW":
            continue
        write = "tsql" if tgt_label == "synapse_fabric" else tgt_label
        out = sqlglot.transpile(stmt.sql(), write=write)[0]
        expr = out
        frm = out.upper().rfind(" FROM ")
        if out.upper().startswith("SELECT ") and frm > 0:
            expr = out[7:frm]
        assert expr == expected, "%s -> %s" % (row["source_function"],
                                               tgt_label)


def test_semantic_conversions_not_blind_replacement():
    nvl = FN.get("oracle", "NVL")
    assert nvl["target_implementations"]["snowflake"] == "COALESCE(a, 0)"
    zin = FN.get("teradata", "ZEROIFNULL")
    assert zin["target_implementations"]["bigquery"] == "COALESCE(x, 0)"
    isnull = FN.get("sqlserver", "ISNULL")
    assert "COALESCE" in isnull["target_implementations"]["postgres"]
    assert "DATATYPE" in isnull["null_semantics_warning"].upper()
    dd = FN.get("sqlserver", "DATEDIFF")
    assert "boundary" in dd["notes"].lower()


def test_datatype_mappings_and_warnings():
    n = DT.map_type("oracle", "NUMBER(10,2)")
    assert n["canonical"] == "DECIMAL"
    v2 = DT.map_type("oracle", "VARCHAR2(100)")
    assert v2["canonical"] == "STRING"
    assert any("'' as NULL" in w for w in v2["warnings"])
    od = DT.map_type("oracle", "DATE")
    assert od["canonical"] == "TIMESTAMP"      # Oracle DATE carries time
    assert any("time" in w.lower() for w in od["warnings"])
    money = DT.map_type("sqlserver", "MONEY")
    assert money["canonical"] == "DECIMAL"
    assert any("rounding" in w.lower() for w in money["warnings"])
    nv = DT.map_type("sqlserver", "NVARCHAR(50)")
    assert any("UTF" in w for w in nv["warnings"])
    xml = DT.map_type("sqlserver", "XML")
    assert xml["canonical"] == "XML"
    assert any("manual" in w.lower() for w in xml["warnings"])
    assert DT.map_type("teradata", "VARBYTE(64)")["canonical"] == "BINARY"
    assert DT.map_type("teradata", "PERIOD")["canonical"] == "STRUCT"
    assert DT.map_type("oracle", "NO_SUCH_TYPE") is None


def test_all_targets_present_per_function():
    for row in FN.all():
        assert set(row["target_implementations"]) == set(TARGETS)

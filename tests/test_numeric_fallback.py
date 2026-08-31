"""One bundle, one answer about what an undeclared decimal is.

Oracle's bare `NUMBER` declares no precision and no scale, so every generator
has to put something in the type. They each invented their own: the warehouse
DDL said `decimal(38,6)`, the PowerCenter XML said `decimal(28,0)`, the IDMC
JSON said `decimal(10,0)`. One column, three contradictory types in a single
delivered bundle — and the narrowest of them overflows any key wider than ten
digits, while the two that said scale 0 assert the column is integral and
would truncate every fractional value on import.

The fallback is still a guess. What it must not be is three different
guesses, and it must not be a guess nobody can replace — which is why the
probe measures the columns that took it.
"""
import json
import os
import pathlib
import re
import tempfile

import yaml

from metabridge.sqlx.type_engine import DECIMAL_FALLBACK, decimal_fallback

MANIFEST = {"tables": [{
    "name": "CUSTOMER", "schema": "RAW_SCHEMA", "database": "FREEPDB1",
    "unique_key": ["CUSTOMER_ID"],
    "columns": [
        # bare NUMBER: no precision, no scale — the case every generator had
        # to invent an answer for
        {"name": "CUSTOMER_ID", "type": "NUMBER", "nullable": False},
        {"name": "AMOUNT", "type": "NUMBER"},
        # and one that DOES declare them, which must be carried untouched
        {"name": "RATE", "type": "NUMBER(9,4)"},
        {"name": "EMAIL", "type": "VARCHAR2(120)"},
    ]}]}


def _bundle():
    tmp = pathlib.Path(tempfile.mkdtemp())
    os.environ["METABRIDGE_DATA_DIR"] = str(tmp / "iso")
    manifest = tmp / "tables.yml"
    manifest.write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    out = tmp / "out"
    from metabridge.scaffold import scaffold
    scaffold("oracle", "snowflake", str(manifest), str(out), "acme",
             governance=False)
    return out


def test_the_scale_is_the_same_in_every_artifact():
    """Scale is the half that changes meaning. 0 says integral, 6 says
    fractional, and a bundle cannot say both about one column."""
    out = _bundle()
    _, scale = DECIMAL_FALLBACK

    ddl = (out / "ddl" / "01_create_landing.sql").read_text(encoding="utf-8")
    assert "DECIMAL(%d,%d)" % DECIMAL_FALLBACK in ddl

    xml = next(out.glob("wf_*.xml")).read_text(encoding="utf-8")
    pc = re.search(r'NAME="CUSTOMER_ID" DATATYPE="decimal" '
                   r'PRECISION="(\d+)" SCALE="(\d+)"', xml)
    assert pc, xml[:400]
    assert int(pc.group(2)) == scale

    doc = json.loads(next((out / "idmc" / "mappings").glob("*.json")).read_text(
        encoding="utf-8"))
    fields = [f for f in re.findall(r'\{[^{}]*?"name": "CUSTOMER_ID".*?\}',
                                    json.dumps(doc), re.S)]
    assert fields
    entry = json.loads(fields[0])
    assert entry["scale"] == scale
    assert entry["precision"] == DECIMAL_FALLBACK[0]


def test_powercenter_clamps_precision_but_not_scale():
    """PowerCenter's decimal stops at 28 without high precision, so it cannot
    take 38 — but taking a different SCALE is what made the bundle contradict
    itself, and that part does not get to differ."""
    assert decimal_fallback(28) == (28, DECIMAL_FALLBACK[1])
    assert decimal_fallback() == DECIMAL_FALLBACK
    assert decimal_fallback(999) == DECIMAL_FALLBACK      # never widened


def test_a_declared_precision_is_never_replaced_by_the_fallback():
    """The fallback exists for the absence of a declaration. Applying it to a
    column that HAS one would throw away catalog fact."""
    out = _bundle()
    ddl = (out / "ddl" / "01_create_landing.sql").read_text(encoding="utf-8")
    rate = next(l for l in ddl.splitlines() if "RATE" in l)
    assert "(9,4)" in rate, rate


def test_the_probe_measures_the_columns_that_took_the_fallback():
    """A guess nobody can replace is just a wrong answer with a comment on
    it. One query on the source turns it into a fact."""
    out = _bundle()
    probe = (out / "ddl" / "00_probe_string_widths.sql").read_text(
        encoding="utf-8")
    for col in ("CUSTOMER_ID", "AMOUNT"):
        assert "MAX(ABS(%s)) AS %s_max" % (col, col) in probe, col
        # whether ANY value is fractional decides scale 0 vs the real scale,
        # and FLOOR is the form that is true on every engine and right for
        # negatives
        assert "MAX(CASE WHEN %s = FLOOR(%s) THEN 0 ELSE 1 END)" % (col, col) \
            in probe, col
    # a column that declares its precision has nothing to measure
    assert "RATE_frac" not in probe

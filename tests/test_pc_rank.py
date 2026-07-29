"""Rank handler: ties semantics, RANKINDEX, QUALIFY vs portable CTE."""
from pathlib import Path

import pytest

from metabridge.engine import parse_input


def _rank_xml(rank_index: bool = True) -> str:
    idx = ('<TRANSFORMFIELD NAME="RANKINDEX" DATATYPE="integer" '
           'PRECISION="10" SCALE="0" PORTTYPE="OUTPUT"/>') if rank_index \
        else ""
    idx_conn = ('<CONNECTOR FROMFIELD="RANKINDEX" FROMINSTANCE="RNK_1" '
                'TOFIELD="pos" TOINSTANCE="TGT_top"/>') if rank_index \
        else ""
    return """<?xml version="1.0"?>
<POWERMART CREATION_DATE="01/01/2026" REPOSITORY_VERSION="188.97">
 <REPOSITORY NAME="R" VERSION="188" CODEPAGE="UTF-8" DATABASETYPE="Oracle">
  <FOLDER NAME="f" OWNER="x" SHARED="NOTSHARED" DESCRIPTION="" PERMISSIONS="rwx---r--" GROUP="">
   <SOURCE NAME="sales" DATABASETYPE="Oracle" DBDNAME="S" OWNERNAME="" OBJECTVERSION="1" VERSIONNUMBER="1">
    <SOURCEFIELD NAME="region" DATATYPE="string" PRECISION="32" SCALE="0" FIELDNUMBER="1" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
    <SOURCEFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" FIELDNUMBER="2" KEYTYPE="NOT A KEY" NULLABLE="NULL"/>
   </SOURCE>
   <TARGET NAME="top_sales" DATABASETYPE="Oracle" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TARGETFIELD NAME="region" DATATYPE="string" PRECISION="32" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="1"/>
    <TARGETFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="2"/>
    <TARGETFIELD NAME="pos" DATATYPE="integer" PRECISION="10" SCALE="0" KEYTYPE="NOT A KEY" NULLABLE="NULL" FIELDNUMBER="3"/>
   </TARGET>
   <MAPPING NAME="m_top" ISVALID="YES" OBJECTVERSION="1" VERSIONNUMBER="1">
    <TRANSFORMATION NAME="SQ_s" TYPE="Source Qualifier">
     <TRANSFORMFIELD NAME="region" DATATYPE="string" PRECISION="32" SCALE="0" PORTTYPE="INPUT/OUTPUT"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT"/>
    </TRANSFORMATION>
    <TRANSFORMATION NAME="RNK_1" TYPE="Rank">
     <TRANSFORMFIELD NAME="region" DATATYPE="string" PRECISION="32" SCALE="0" PORTTYPE="INPUT/OUTPUT" EXPRESSIONTYPE="GROUPBY"/>
     <TRANSFORMFIELD NAME="amount" DATATYPE="decimal" PRECISION="18" SCALE="2" PORTTYPE="INPUT/OUTPUT/RANK"/>
     %(idx)s
     <TABLEATTRIBUTE NAME="Number of Ranks" VALUE="3"/>
     <TABLEATTRIBUTE NAME="Top/Bottom" VALUE="Top"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_s" TYPE="SOURCE" TRANSFORMATION_NAME="sales" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="SQ_s" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="SQ_s" TRANSFORMATION_TYPE="Source Qualifier"/>
    <INSTANCE NAME="RNK_1" TYPE="TRANSFORMATION" TRANSFORMATION_NAME="RNK_1" TRANSFORMATION_TYPE="Rank"/>
    <INSTANCE NAME="TGT_top" TYPE="TARGET" TRANSFORMATION_NAME="top_sales" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMFIELD="region" FROMINSTANCE="SRC_s" TOFIELD="region" TOINSTANCE="SQ_s"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SRC_s" TOFIELD="amount" TOINSTANCE="SQ_s"/>
    <CONNECTOR FROMFIELD="region" FROMINSTANCE="SQ_s" TOFIELD="region" TOINSTANCE="RNK_1"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="SQ_s" TOFIELD="amount" TOINSTANCE="RNK_1"/>
    <CONNECTOR FROMFIELD="region" FROMINSTANCE="RNK_1" TOFIELD="region" TOINSTANCE="TGT_top"/>
    <CONNECTOR FROMFIELD="amount" FROMINSTANCE="RNK_1" TOFIELD="amount" TOINSTANCE="TGT_top"/>
    %(idx_conn)s
   </MAPPING>
  </FOLDER>
 </REPOSITORY>
</POWERMART>""" % {"idx": idx, "idx_conn": idx_conn}


@pytest.fixture()
def ranked(tmp_path):
    f = tmp_path / "r.xml"
    f.write_text(_rank_xml())
    return parse_input(str(f), "powercenter").mapping("top")


# ---------------------------------------------------------------------------
# parsing + CIR
# ---------------------------------------------------------------------------

def test_rank_cir_contract(ranked):
    rnk = ranked.transformation("RNK_1")
    cir = rnk.properties["rank_cir"]
    assert cir == {"top": True, "number_of_ranks": 3,
                   "group_by": ["region"], "rank_port": "amount",
                   "rank_index_port": "RANKINDEX", "function": "RANK"}


def test_ties_semantics_flagged(ranked):
    issue = next(i for i in ranked.issues
                 if i.code == "RANK_TIES_INCLUDED")
    assert "more than 3 rows" in issue.message
    assert "ROW_NUMBER" in issue.suggestion
    assert "DENSE_RANK" in issue.suggestion


# ---------------------------------------------------------------------------
# generation strategies
# ---------------------------------------------------------------------------

def _convert(tmp_path, target, monkeypatch, **kw):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    f = tmp_path / "r.xml"
    f.write_text(_rank_xml(**kw))
    from metabridge.engine import convert
    rep = convert(str(f), str(tmp_path / "out"),
                  source_format="powercenter", target_format=target)
    sub = "dbt" if target == "dbt" else "sql"
    pat = "int_*top*.sql" if target == "dbt" else "*top*.sql"
    hit = next((tmp_path / "out" / sub).rglob(pat))
    return hit.read_text(), rep


def test_dbt_portable_cte_with_rank_and_rankindex(tmp_path, monkeypatch):
    sql, _ = _convert(tmp_path, "dbt", monkeypatch)
    # portable, adapter-agnostic: CTE + window + filter, RANK() for ties
    assert "rank() over (partition by region order by amount desc)" in sql
    assert "as RANKINDEX" in sql               # PC rank position exposed
    assert "where RANKINDEX <= 3" in sql
    assert "qualify" not in sql.lower()


def test_snowflake_uses_qualify(tmp_path, monkeypatch):
    sql, rep = _convert(tmp_path, "snowflake", monkeypatch)
    assert "QUALIFY" in sql.upper()
    assert "RANK() OVER (PARTITION BY REGION ORDER BY AMOUNT DESC" \
        in sql.upper()
    assert rep["conversion_output"]["errors"]["count"] == 0


def test_databricks_uses_cte_filter_not_qualify(tmp_path, monkeypatch):
    sql, rep = _convert(tmp_path, "databricks", monkeypatch)
    assert "QUALIFY" not in sql.upper()
    assert "RANK() OVER" in sql.upper()
    assert "<= 3" in sql
    assert rep["conversion_output"]["errors"]["count"] == 0


def test_without_rankindex_uses_internal_alias(tmp_path, monkeypatch):
    sql, _ = _convert(tmp_path, "databricks", monkeypatch,
                      rank_index=False)
    assert "_mb_rank" in sql
    assert "RANKINDEX" not in sql


def test_legacy_ir_ranks_keep_row_number(tmp_path, monkeypatch):
    """dbt-parsed ranks (no rank_function property) stay ROW_NUMBER —
    exactly-N semantics preserved for non-PC sources."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
    convert(str(EXAMPLES / "dbt_retail"), str(tmp_path / "out"),
            source_format="dbt", target_format="powercenter")
    # round-trip regression is covered by the existing suite; this test
    # pins that the default function without rank_function is ROW_NUMBER
    from metabridge.ir.model import Mapping, Port, Transformation
    from metabridge.ir.model import TransformationType as TT
    from metabridge.generators.dbt_generator import render_plain_select
    m = Mapping(name="x")
    m.transformations = [
        Transformation(name="SRC_a", type=TT.SOURCE,
                       properties={"table": "a"},
                       ports=[Port(name="v")]),
        Transformation(name="SQ_a", type=TT.SOURCE_QUALIFIER,
                       properties={"source": "SRC_a"},
                       ports=[Port(name="v")]),
        Transformation(name="RNK", type=TT.RANK,
                       ports=[Port(name="v")],
                       properties={"number_of_ranks": 2,
                                   "order_port": "v"}),
    ]
    from metabridge.ir.model import Link, Pipeline
    m.links = [Link("SRC_a", "SQ_a"), Link("SQ_a", "RNK")]
    sql = render_plain_select(m, Pipeline(name="p", mappings=[m]), {"x"})
    assert "row_number() over" in sql

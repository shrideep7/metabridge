"""Transformation mapping engine: every spec arrow pinned to real behavior."""
from pathlib import Path

import pytest

from metabridge.cir.builder import build_cir
from metabridge.cir.model import CirTransformationType
from metabridge.cir.transform_map import get_transformation_map
from metabridge.generators.dbt_generator import render_model_sql
from metabridge.ir.model import (
    Link, LoadStrategy, Mapping, Pipeline, Port, Transformation,
    TransformationType,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
tm = get_transformation_map()


# ---------------------------------------------------------------------------
# Registry rows — the spec table, verbatim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("obj,cir", [
    ("Expression", "EXPRESSION"), ("Filter", "FILTER"), ("Joiner", "JOIN"),
    ("Lookup Procedure", "LOOKUP"), ("Aggregator", "AGGREGATOR"),
    ("Router", "ROUTER"), ("Rank", "WINDOW"),
    ("Sequence Generator", "SEQUENCE"), ("Update Strategy", "MERGE"),
    ("SCD", "SCD_TYPE_2"),
])
def test_powercenter_rows(obj, cir):
    row = tm.lookup("powercenter", obj)
    assert row["cir"] == cir
    assert row["targets"].get("dbt")


@pytest.mark.parametrize("obj,cir", [
    ("ref", "DEPENDENCY"), ("source", "SOURCE"),
    ("incremental", "INCREMENTAL"), ("snapshot", "SCD_TYPE_2"),
    ("test", "DATA_QUALITY_RULE"),
])
def test_dbt_rows(obj, cir):
    assert tm.lookup("dbt", obj)["cir"] == cir


def test_registry_valid():
    assert tm.validate() == []
    assert len(tm.rows()) >= 15


# ---------------------------------------------------------------------------
# Native behavior behind the rows
# ---------------------------------------------------------------------------

def _one_node_mapping(t: Transformation, cols=("id", "amount")) -> Mapping:
    ports = [Port(name=c) for c in cols]
    src = Transformation(name="SRC_t", type=TransformationType.SOURCE,
                         ports=list(ports), properties={"table": "t"})
    sq = Transformation(name="SQ_t", type=TransformationType.SOURCE_QUALIFIER,
                        ports=list(ports), properties={"source": "SRC_t"})
    if not t.ports:
        t.ports = list(ports)
    out = Transformation(name="__OUTPUT__", type=TransformationType.EXPRESSION,
                         ports=list(t.ports),
                         properties={"virtual": True, "upstream": t.name})
    tgt = Transformation(name="TGT_m", type=TransformationType.TARGET,
                         ports=list(t.ports), properties={"table": "m"})
    m = Mapping(name="m", transformations=[src, sq, t, out, tgt],
                links=[Link("SRC_t", "SQ_t"), Link("SQ_t", t.name),
                       Link(t.name, "__OUTPUT__"), Link("__OUTPUT__", "TGT_m")])
    return m


def test_rank_renders_as_window():
    rank = Transformation(name="RNK_1", type=TransformationType.RANK,
                          properties={"group_by": ["region"],
                                      "order_port": "amount",
                                      "top": True, "number_of_ranks": 5})
    m = _one_node_mapping(rank, cols=("id", "amount", "region"))
    sql = render_model_sql(m, Pipeline(name="p", mappings=[m]), {"m"})
    assert "row_number() over (partition by region order by amount desc)" in sql
    assert "_mb_rank <= 5" in sql
    assert any(i.code == "RANK_AS_WINDOW" for i in m.issues)


def test_router_renders_route_column():
    router = Transformation(name="RTR_1", type=TransformationType.ROUTER,
                            properties={"groups": [
                                {"name": "US", "condition": "country = 'US'"},
                                {"name": "EU", "condition": "country = 'EU'"}]})
    m = _one_node_mapping(router, cols=("id", "country"))
    sql = render_model_sql(m, Pipeline(name="p", mappings=[m]), {"m"})
    assert "when country = 'US' then 'US'" in sql
    assert "_mb_route_group" in sql
    assert any(i.code == "ROUTER_AS_ROUTE_COLUMN" for i in m.issues)


def test_sequence_renders_surrogate():
    seq = Transformation(name="SEQ_1", type=TransformationType.SEQUENCE,
                         ports=[Port(name="NEXTVAL")])
    m = _one_node_mapping(seq)
    sql = render_model_sql(m, Pipeline(name="p", mappings=[m]), {"m"})
    assert "row_number() over (order by 1) as NEXTVAL" in sql
    assert any(i.code == "SEQUENCE_AS_ROW_NUMBER" for i in m.issues)


def test_rank_maps_to_cir_window():
    rank = Transformation(name="RNK_1", type=TransformationType.RANK,
                          properties={"number_of_ranks": 3})
    m = _one_node_mapping(rank)
    project = build_cir(Pipeline(name="p", mappings=[m],
                                 source_format="powercenter"))
    tx = next(t for t in project.pipelines[0].transformations
              if t.name == "RNK_1")
    assert tx.transformation_type == CirTransformationType.WINDOW


# ---------------------------------------------------------------------------
# dbt snapshot -> SCD_TYPE_2 end-to-end
# ---------------------------------------------------------------------------

@pytest.fixture()
def snapshot_project(tmp_path):
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "dbt_project.yml").write_text(
        "name: snap_demo\nversion: '1'\nconfig-version: 2\nprofile: x\n")
    (tmp_path / "snapshots" / "customers_snapshot.sql").write_text("""
{% snapshot customers_snapshot %}
{{ config(target_schema='history', unique_key='customer_id',
          strategy='timestamp', updated_at='updated_at') }}
select customer_id, name, email, updated_at from {{ source('raw', 'customers') }}
{% endsnapshot %}
""")
    return tmp_path


def test_snapshot_parses_as_scd2(snapshot_project):
    from metabridge.parsers.base import get_parser
    p = get_parser("dbt").parse_project(str(snapshot_project))
    snap = p.mapping("customers_snapshot")
    assert snap is not None
    assert snap.load_strategy == LoadStrategy.SCD2
    assert snap.unique_key == ["customer_id"]
    assert snap.properties["scd"]["updated_at"] == "updated_at"
    # CIR: target node is SCD_TYPE_2
    project = build_cir(p)
    tgt = next(t for t in project.pipeline("customers_snapshot").transformations
               if t.name.startswith("TGT_"))
    assert tgt.transformation_type == CirTransformationType.SCD_TYPE_2


def test_snapshot_regenerates_as_dbt_snapshot(snapshot_project, tmp_path):
    from metabridge.generators.dbt_generator import generate_dbt_project
    from metabridge.parsers.base import get_parser
    p = get_parser("dbt").parse_project(str(snapshot_project))
    out = tmp_path / "out"
    generate_dbt_project(p, str(out))
    snap = (out / "snapshots" / "customers_snapshot.sql").read_text()
    assert "{% snapshot customers_snapshot %}" in snap
    assert "strategy='timestamp'" in snap
    assert "unique_key='customer_id'" in snap
    assert "{% endsnapshot %}" in snap


def test_snapshot_to_warehouse_sql_scd2(snapshot_project, tmp_path):
    from metabridge.engine import convert
    report = convert(str(snapshot_project), str(tmp_path / "o"),
                     source_format="dbt", target_format="snowflake")
    stmt = next((tmp_path / "o" / "sql").glob("*customers_snapshot.sql")).read_text()
    assert "mb_valid_to" in stmt and "mb_is_current" in stmt
    assert "-- Statement 1: close current versions" in stmt
    assert "-- Statement 2: insert new current versions" in stmt
    snap_row = next(m for m in report["mappings"]
                    if m["name"] == "customers_snapshot")
    assert snap_row["load_strategy"] == "SCD2"

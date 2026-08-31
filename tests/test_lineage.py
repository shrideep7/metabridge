"""Automatic data lineage: table/column/transformation levels + Mermaid."""
import json
from pathlib import Path

import pytest

from metabridge.parsers.base import get_parser
from metabridge.report.lineage import (
    build_lineage, column_lineage, mermaid_column, mermaid_table,
    mermaid_transformations, table_lineage, transformation_lineage,
    write_lineage,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def retail():
    return get_parser("dbt").parse_project(str(EXAMPLES / "dbt_retail"))


# ---------------------------------------------------------------------------
# Table-level
# ---------------------------------------------------------------------------

def test_table_lineage_graph(retail):
    tl = table_lineage(retail)
    ids = {n["id"] for n in tl["nodes"]}
    assert {"raw_customers", "raw_orders", "stg_customers", "stg_orders",
            "customer_orders", "customer_ranking"} <= ids
    kinds = {n["id"]: n["kind"] for n in tl["nodes"]}
    assert kinds["raw_customers"] == "source"
    assert kinds["customer_orders"] == "target"
    edges = {(e["from"], e["to"]) for e in tl["edges"]}
    assert ("raw_customers", "stg_customers") in edges
    assert ("stg_orders", "customer_orders") in edges
    assert ("customer_orders", "customer_ranking") in edges
    via = next(e["via"] for e in tl["edges"]
               if (e["from"], e["to"]) == ("raw_orders", "stg_orders"))
    assert via == "stg_orders"


# ---------------------------------------------------------------------------
# Column-level — the spec chain shape
# ---------------------------------------------------------------------------

def test_column_chain_spec_shape(retail):
    """source.table.col -> SQ.col -> ... -> target.col, in order."""
    cl = column_lineage(retail.mapping("stg_customers"))
    entry = next(c for c in cl if c["target_column"].endswith(".email"))
    assert entry["derivation"] == "expression"       # lower(email)
    path = entry["paths"][0]
    assert path[0] == "RAW.raw_customers.email"      # qualified source
    assert path[1].startswith("SQ_raw_customers")    # source qualifier hop
    assert any(step.startswith("EXP_") for step in path)  # derivation hop
    assert path[-1] == "TGT_stg_customers.email"     # target


def test_renamed_column_traces_to_origin(retail):
    """customer_id is derived from id — the path must reach raw id."""
    cl = column_lineage(retail.mapping("stg_customers"))
    entry = next(c for c in cl
                 if c["target_column"].endswith(".customer_id"))
    path = entry["paths"][0]
    assert path[0] == "RAW.raw_customers.id"         # original column name
    assert path[-1].endswith(".customer_id")         # renamed at the end


def test_aggregate_column_traces_through_join(retail):
    cl = column_lineage(retail.mapping("customer_orders"))
    entry = next(c for c in cl
                 if c["target_column"].endswith(".lifetime_value"))
    assert entry["derivation"] == "expression"       # SUM(amount)
    flat = [step for path in entry["paths"] for step in path]
    assert any(s == "RAW.raw_orders.amount" or s.endswith("stg_orders.amount")
               for s in flat)


def test_override_pipeline_has_visible_coarse_or_ast_lineage(retail):
    cl = column_lineage(retail.mapping("customer_ranking"))
    entry = next(c for c in cl
                 if c["target_column"].endswith(".lifetime_value"))
    assert entry["paths"]                            # never empty
    # from the override's AST projections when resolvable
    assert entry["derivation"] in ("passthrough", "expression", "coarse")


# ---------------------------------------------------------------------------
# Transformation-level
# ---------------------------------------------------------------------------

def test_transformation_lineage(retail):
    tl = transformation_lineage(retail.mapping("customer_orders"))
    names = {n["name"] for n in tl["nodes"]}
    assert "__OUTPUT__" not in names
    assert any(n["type"] == "JOINER" for n in tl["nodes"])
    edges = {(e["from"], e["to"]) for e in tl["edges"]}
    assert ("SRC_stg_customers", "SQ_stg_customers_1") in edges
    # target is wired to the real upstream, not the virtual node
    assert any(to == "TGT_customer_orders" for _, to in edges)


# ---------------------------------------------------------------------------
# Mermaid + document contract
# ---------------------------------------------------------------------------

def test_mermaid_outputs(retail):
    tl = table_lineage(retail)
    mm = mermaid_table(tl)
    assert mm.startswith("graph LR")
    assert '[("RAW.raw_customers")]' in mm           # cylinder for sources
    assert "-->|stg_customers|" in mm
    mt = mermaid_transformations(retail.mapping("daily_revenue"))
    assert "graph LR" in mt and "AGG_1" in mt
    cl = column_lineage(retail.mapping("stg_customers"))
    mc = mermaid_column(cl[0])
    assert "graph LR" in mc and "-->" in mc


def test_build_and_write(retail, tmp_path):
    doc = build_lineage(retail)
    json.dumps(doc)                                   # serializable
    assert len(doc["pipelines"]) == 5
    assert doc["mermaid"]["table_lineage"].startswith("graph LR")
    path = write_lineage(doc, str(tmp_path))
    assert Path(path).exists()
    md = (tmp_path / "lineage.md").read_text(encoding="utf-8")
    assert "```mermaid" in md
    assert "## customer_orders — transformation lineage" in md
    assert "**TGT_stg_customers.email**" in md or "TGT_stg_customers.email" in md

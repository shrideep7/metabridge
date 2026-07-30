"""Port-level lineage engine: all port kinds, confidence, JSON + Mermaid."""
import json
from pathlib import Path

import pytest

from metabridge.parsers.pc_lineage import (
    build_port_lineage, build_repository_lineage, lineage_to_mermaid,
    write_port_lineage,
)
from metabridge.parsers.pc_model import (
    PCConnector, PCInstance, PCMapping, build_pc_model,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_XML = EXAMPLES / "powercenter_repo" / "repo_export.xml"


@pytest.fixture(scope="module")
def model():
    return build_pc_model(str(REPO_XML))


@pytest.fixture(scope="module")
def enrich(model):
    folder = model.folder("sales")
    m = next(x for x in folder.mappings if x.name == "m_customer_enrich")
    return build_port_lineage(m, folder)


def _field(doc, column):
    return next(f for f in doc["target_fields"]
                if f["target_column"] == column)


# ---------------------------------------------------------------------------
# the spec's example chain
# ---------------------------------------------------------------------------

def test_spec_example_chain(enrich):
    """SRC -> SQ -> EXP -> LKP -> RTR -> TGT with a rename at the end."""
    f = _field(enrich, "customer_id")
    (path,) = f["paths"]
    assert path == ("SRC_customer.cust_id -> SQ_customer.cust_id -> "
                    "EXP_CLEAN.cust_id -> LKP_ACCOUNT.cust_id -> "
                    "RTR_CUSTOMER.cust_id1 -> TGT_customer.customer_id")
    assert f["target_table"] == "tgt_customer"
    assert f["source_tables"] == ["raw_customer"]
    assert f["source_columns"] == ["raw_customer.cust_id"]
    assert f["lineage_confidence"] == 100
    types = [t["type"] for t in f["transformations_applied"]]
    assert types == ["Source Qualifier", "Expression", "Lookup Procedure",
                     "Router"]


def test_contract_fields_present(enrich):
    f = _field(enrich, "customer_id")
    for key in ("target_table", "target_column", "source_tables",
                "source_columns", "transformations_applied",
                "expressions_applied", "lookup_dependencies",
                "business_rules", "lineage_confidence"):
        assert key in f, key


# ---------------------------------------------------------------------------
# port kinds
# ---------------------------------------------------------------------------

def test_variable_port_expressions_surfaced(enrich):
    """name_clean = UPPER(v_name_trim); the variable's own expression is
    part of the applied logic, and lineage resolves through it."""
    f = _field(enrich, "customer_name")
    assert f["source_columns"] == ["raw_customer.name"]
    assert "EXP_CLEAN.name_clean = UPPER(v_name_trim)" in \
        f["expressions_applied"]
    assert "EXP_CLEAN.v_name_trim = LTRIM(RTRIM(name))" in \
        f["expressions_applied"]


def test_lookup_output_origin_is_the_lookup_table(enrich):
    f = _field(enrich, "account_id")
    assert f["origin_types"] == ["lookup"]
    assert f["source_tables"] == ["ACCOUNTS"]
    assert f["source_columns"] == ["ACCOUNTS.acct_id (lookup)"]
    (dep,) = f["lookup_dependencies"]
    assert dep["table"] == "ACCOUNTS"
    assert dep["condition"] == "CUST_ID = cust_id"


def test_sequence_generated_value(enrich):
    f = _field(enrich, "surrogate_key")
    assert f["sequence_generated"] is True
    assert f["origin_types"] == ["sequence"]
    assert f["source_tables"] == []      # no source table pretended
    assert f["lineage_confidence"] == 100
    assert f["paths"][0].startswith("SEQ_SK.NEXTVAL")


def test_router_group_business_rule(enrich):
    for column in ("customer_id", "customer_name", "account_id"):
        f = _field(enrich, column)
        rule = next(r for r in f["business_rules"]
                    if r["type"] == "router_group")
        assert rule["group"] == "ACTIVE"
        assert rule["condition"] == "status = 'A'"


def test_aggregator_output_and_grain(model):
    folder = model.folder("sales")
    m = next(x for x in folder.mappings if x.name == "m_agg_sales")
    doc = build_port_lineage(m, folder)
    f = _field(doc, "total_amount")
    assert f["source_columns"] == ["fct_sales.amount"]
    assert any("SUM(amount)" in e for e in f["expressions_applied"])
    grain = next(r for r in f["business_rules"]
                 if r["type"] == "aggregation_grain")
    assert grain["condition"] == "region_up"


def test_mapplet_boundary_expressions(model):
    folder = model.folder("sales")
    m = next(x for x in folder.mappings if x.name == "m_load_sales")
    doc = build_port_lineage(m, folder)
    f = _field(doc, "region_up")
    assert f["source_columns"] == ["raw_sales.region"]
    assert any("LTRIM(RTRIM(region))" in e and "mapplet" in e
               for e in f["expressions_applied"])
    assert f["lineage_confidence"] == 100


def test_union_inputs_all_branches_traced():
    m = PCMapping(name="m_union")
    m.instances = [
        PCInstance(name="S1", instance_type="SOURCE",
                   transformation_name="t1"),
        PCInstance(name="S2", instance_type="SOURCE",
                   transformation_name="t2"),
        PCInstance(name="UN", instance_type="TRANSFORMATION",
                   transformation_name="UN",
                   transformation_type="Union Transformation"),
        PCInstance(name="T", instance_type="TARGET",
                   transformation_name="tgt"),
    ]
    m.connectors = [
        PCConnector(from_instance="S1", from_field="id",
                    to_instance="UN", to_field="id"),
        PCConnector(from_instance="S2", from_field="id",
                    to_instance="UN", to_field="id"),
        PCConnector(from_instance="UN", from_field="id",
                    to_instance="T", to_field="id"),
    ]
    from metabridge.parsers.pc_graph import build_mapping_graph
    from metabridge.parsers.pc_lineage import trace_target_field
    g = build_mapping_graph(m, None)
    f = trace_target_field(g, m, None, "T", "id")
    assert len(f["paths"]) == 2                    # both branches
    assert {p.split(" -> ")[0] for p in f["paths"]} == {"S1.id", "S2.id"}


# ---------------------------------------------------------------------------
# confidence honesty
# ---------------------------------------------------------------------------

def test_unresolved_path_lowers_confidence():
    m = PCMapping(name="m_broken")
    m.instances = [
        PCInstance(name="X", instance_type="TRANSFORMATION",
                   transformation_name="X",
                   transformation_type="Expression"),
        PCInstance(name="T", instance_type="TARGET",
                   transformation_name="tgt"),
    ]
    m.connectors = [PCConnector(from_instance="X", from_field="a",
                                to_instance="T", to_field="a")]
    from metabridge.parsers.pc_graph import build_mapping_graph
    from metabridge.parsers.pc_lineage import trace_target_field
    g = build_mapping_graph(m, None)
    f = trace_target_field(g, m, None, "T", "a")
    assert f["lineage_confidence"] < 100
    assert "unresolved" in f["origin_types"]
    assert any("without reaching a source" in b
               for b in f["confidence_basis"])


def test_no_path_is_flagged(enrich, model):
    folder = model.folder("sales")
    m = next(x for x in folder.mappings if x.name == "m_customer_enrich")
    from metabridge.parsers.pc_graph import build_mapping_graph
    from metabridge.parsers.pc_lineage import trace_target_field
    g = build_mapping_graph(m, folder)
    f = trace_target_field(g, m, folder, "TGT_customer", "ghost_column")
    assert f["lineage_confidence"] < 100
    assert any("no connector path" in b for b in f["confidence_basis"])


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------

def test_mermaid_definition(enrich):
    mm = enrich["mermaid"]["TGT_customer.customer_id"]
    assert mm.startswith("graph TD")
    assert 'SRC_customer_cust_id["SRC_customer.cust_id"]' in mm
    assert "--> TGT_customer_customer_id" in mm
    assert "style SRC_customer_cust_id" in mm      # source styled


def test_repository_rollup_and_writer(model, tmp_path):
    doc = build_repository_lineage(model)
    assert doc["repository"] == "PROD_REPO"
    assert doc["summary"]["mappings"] == 5
    assert doc["summary"]["fields_traced"] >= 13
    assert doc["summary"]["average_confidence"] >= 90
    json.dumps(doc)
    path = write_port_lineage(doc, str(tmp_path))
    assert Path(path).exists()
    md = (tmp_path / "port_lineage.md").read_text()
    assert "```mermaid" in md
    assert "tgt_customer.customer_id" in md

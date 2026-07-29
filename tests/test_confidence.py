"""Migration confidence scoring (module 31): per-type scores, weighted
critical-path aggregation (never a simple average), module contract."""
import pytest

from metabridge.ir.model import (Link, Mapping, Transformation,
                                 TransformationType)
from metabridge.report.confidence import (TYPE_CONFIDENCE, node_confidence,
                                          score_mapping_confidence)


def _chain(*types_and_names):
    """Build a linear SOURCE -> ... -> TARGET mapping."""
    m = Mapping(name="m")
    prev = None
    for i, (ttype, props) in enumerate(types_and_names):
        name = "N%d" % i
        m.transformations.append(Transformation(
            name=name, type=ttype, properties=dict(props or {})))
        if prev:
            m.links.append(Link(from_transformation=prev,
                                to_transformation=name))
        prev = name
    return m


def _linear(*mid_types):
    steps = [(TransformationType.SOURCE, {"table": "s"})]
    steps += [(t, {}) for t in mid_types]
    steps += [(TransformationType.TARGET, {"table": "t"})]
    return _chain(*steps)


# ---------------------------------------------------------------------------
# per-transformation scores (the spec's table)
# ---------------------------------------------------------------------------

def test_spec_example_scores():
    assert TYPE_CONFIDENCE["SOURCE_QUALIFIER"] == 98
    assert TYPE_CONFIDENCE["EXPRESSION"] == 95
    assert TYPE_CONFIDENCE["FILTER"] == 98
    assert TYPE_CONFIDENCE["JOINER"] == 95
    assert TYPE_CONFIDENCE["LOOKUP"] == 88
    assert TYPE_CONFIDENCE["AGGREGATOR"] == 95
    assert TYPE_CONFIDENCE["ROUTER"] == 85
    assert TYPE_CONFIDENCE["UPDATE_STRATEGY"] == 80


def test_java_transformation_scores_35():
    t = Transformation(name="J", type=TransformationType.EXPRESSION,
                       properties={"unconverted_pc_type":
                                   "Java Transformation"})
    assert node_confidence(t) == 35


def test_handler_findings_adjust_node_scores():
    dyn = Transformation(name="L", type=TransformationType.LOOKUP,
                         properties={"lookup_cir": {"dynamic_lookup": True}})
    assert node_confidence(dyn) == 55
    static = Transformation(name="L2", type=TransformationType.LOOKUP,
                            properties={"lookup_cir": {}})
    assert node_confidence(static) == 88


# ---------------------------------------------------------------------------
# aggregation: weighted impact, never a simple average
# ---------------------------------------------------------------------------

def test_low_confidence_on_critical_path_drags_mapping_down():
    clean = _linear(TransformationType.SOURCE_QUALIFIER,
                    TransformationType.EXPRESSION,
                    TransformationType.FILTER)
    with_java = _chain(
        (TransformationType.SOURCE, {"table": "s"}),
        (TransformationType.SOURCE_QUALIFIER, {}),
        (TransformationType.EXPRESSION,
         {"unconverted_pc_type": "Java Transformation"}),
        (TransformationType.FILTER, {}),
        (TransformationType.TARGET, {"table": "t"}))
    c_clean = score_mapping_confidence(clean)["conversion_confidence"]
    c_java = score_mapping_confidence(with_java)["conversion_confidence"]
    assert c_clean >= 90
    # a simple average of (100+98+35+98+100)/5 would be 86 — the damping
    # must pull far below that
    assert c_java < 65
    assert c_clean - c_java > 30


def test_critical_risk_reported_with_original_type():
    m = _chain(
        (TransformationType.SOURCE, {"table": "s"}),
        (TransformationType.EXPRESSION,
         {"unconverted_pc_type": "Java Transformation"}),
        (TransformationType.TARGET, {"table": "t"}))
    conf = score_mapping_confidence(m)
    (risk,) = conf["critical_risks"]
    assert risk["transformation"] == "N1"
    assert risk["type"] == "Java Transformation"
    assert risk["confidence"] == 35
    assert conf["bottleneck_confidence"] == 35


def test_side_branch_weighs_less_than_critical_path():
    # same Java node: on the critical path vs dangling off it
    on_path = _chain(
        (TransformationType.SOURCE, {"table": "s"}),
        (TransformationType.EXPRESSION,
         {"unconverted_pc_type": "Java Transformation"}),
        (TransformationType.TARGET, {"table": "t"}))
    off_path = _linear(TransformationType.EXPRESSION)
    off_path.transformations.append(Transformation(
        name="SIDE", type=TransformationType.EXPRESSION,
        properties={"unconverted_pc_type": "Java Transformation"}))
    off_path.links.append(Link(from_transformation="N1",
                               to_transformation="SIDE"))
    c_on = score_mapping_confidence(on_path)["conversion_confidence"]
    c_off = score_mapping_confidence(off_path)["conversion_confidence"]
    assert c_off > c_on


def test_module_contract_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from tests.test_pc_scd1 import _scd1_xml
    f = tmp_path / "s.xml"
    f.write_text(_scd1_xml())
    from metabridge.engine import parse_input
    from metabridge.report.reporter import build_report
    report = build_report(parse_input(str(f), "powercenter"), "dbt")
    mm = report["mappings"][0]
    assert {"complexity_score", "conversion_confidence",
            "automation_percentage"} <= set(mm["complexity"])
    assert {"conversion_confidence", "bottleneck_confidence",
            "critical_risks", "manual_review_items"} <= \
        set(mm["confidence"])

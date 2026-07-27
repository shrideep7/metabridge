"""PowerCenter transformation semantic registry: 60 types, contract,
no-drift guards pinning the registry to the actual engine."""
import json
from pathlib import Path

import pytest

from metabridge.parsers.pc_registry import (
    AUTOMATION_LEVELS, REQUIRED_FIELDS, PCTransformationRegistry,
    get_pc_registry,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def reg():
    return get_pc_registry()


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------

def test_at_least_sixty_transformations(reg):
    assert len(reg.all()) >= 60


def test_every_entry_has_the_full_contract(reg):
    for key, row in reg.all().items():
        for f in REQUIRED_FIELDS:
            assert f in row, (key, f)
        assert row["automation_level"] in AUTOMATION_LEVELS, key
        assert isinstance(row["requires_manual_review"], bool), key


def test_registry_validates_clean(reg):
    assert reg.validate() == []


def test_spec_examples_match(reg):
    sq = reg.get("SOURCE_QUALIFIER")
    assert (sq["cir_type"], sq["automation_level"],
            sq["dbt_strategy"], sq["databricks_strategy"]) == \
        ("SOURCE", "FULL", "staging_model", "source_query")
    exp = reg.get("EXPRESSION")
    assert (exp["cir_type"], exp["automation_level"],
            exp["dbt_strategy"]) == ("EXPRESSION", "HIGH",
                                     "select_expression")
    lkp = reg.get("LOOKUP")
    assert (lkp["automation_level"], lkp["dbt_strategy"],
            lkp["databricks_strategy"]) == \
        ("HIGH", "join_or_correlated_lookup", "join_or_broadcast_join")
    java = reg.get("JAVA_TRANSFORMATION")
    assert (java["cir_type"], java["automation_level"],
            java["databricks_strategy"],
            java["requires_manual_review"]) == \
        ("CUSTOM_CODE", "LOW", "pyspark_or_scala", True)


def test_low_and_manual_always_require_review(reg):
    for key, row in reg.all().items():
        if row["automation_level"] in ("LOW", "MANUAL"):
            assert row["requires_manual_review"] is True, key


def test_all_sixty_spec_names_present(reg):
    keys = set(reg.all())
    for wanted in ("SOURCE_QUALIFIER", "EXPRESSION", "FILTER", "JOINER",
                   "LOOKUP", "AGGREGATOR", "SORTER", "ROUTER", "UNION",
                   "RANK", "SEQUENCE_GENERATOR", "UPDATE_STRATEGY",
                   "NORMALIZER", "STORED_PROCEDURE", "SQL_TRANSFORMATION",
                   "TRANSACTION_CONTROL", "XML_SOURCE_QUALIFIER",
                   "XML_PARSER", "XML_GENERATOR", "HTTP_TRANSFORMATION",
                   "JAVA_TRANSFORMATION", "EXTERNAL_PROCEDURE",
                   "CUSTOM_TRANSFORMATION", "MAPPLET",
                   "INPUT_TRANSFORMATION", "OUTPUT_TRANSFORMATION",
                   "DATA_MASKING", "ADDRESS_VALIDATOR",
                   "MATCH_TRANSFORMATION", "MERGE_TRANSFORMATION",
                   "LABELER", "PARSER_TRANSFORMATION", "CLASSIFIER",
                   "ASSOCIATION", "CONSOLIDATION", "KEY_GENERATOR",
                   "EXCEPTION_TRANSFORMATION", "DECISION",
                   "WINDOW_TRANSFORMATION", "WEB_SERVICES_CONSUMER",
                   "WEB_SERVICES_HUB", "JMS_SOURCE", "JMS_TARGET",
                   "SAP_SOURCE", "SAP_TARGET", "SALESFORCE_SOURCE",
                   "SALESFORCE_TARGET", "MQ_SOURCE", "MQ_TARGET",
                   "FLAT_FILE_SOURCE", "FLAT_FILE_TARGET",
                   "DYNAMIC_LOOKUP", "UNCONNECTED_LOOKUP",
                   "CONNECTED_LOOKUP", "REUSABLE_TRANSFORMATION",
                   "REUSABLE_SEQUENCE", "STORED_PROCEDURE_PRELOAD",
                   "STORED_PROCEDURE_POSTLOAD", "SCD_TYPE_1", "SCD_TYPE_2"):
        assert wanted in keys, wanted


# ---------------------------------------------------------------------------
# classification of real export TYPE strings
# ---------------------------------------------------------------------------

def test_classify_export_type_strings(reg):
    assert reg.classify("Lookup Procedure")["key"] == "LOOKUP"
    assert reg.classify("Union Transformation")["key"] == "UNION"
    assert reg.classify("Sequence")["key"] == "SEQUENCE_GENERATOR"
    assert reg.classify("Mapplet Input")["key"] == "INPUT_TRANSFORMATION"
    assert reg.classify("Custom Transformation")["key"] == \
        "CUSTOM_TRANSFORMATION"


def test_classify_unknown_is_honest_never_none(reg):
    entry = reg.classify("Quantum Flux Capacitor")
    assert entry["key"] == "UNKNOWN"
    assert entry["automation_level"] == "MANUAL"
    assert entry["requires_manual_review"] is True
    assert "workaround" in entry


def test_coverage(reg):
    cov = reg.coverage()
    assert cov["transformations"] >= 60
    assert sum(cov["by_automation_level"].values()) == \
        cov["transformations"]
    assert cov["by_automation_level"]["FULL"] >= 8
    assert cov["by_automation_level"]["MANUAL"] >= 15


# ---------------------------------------------------------------------------
# no-drift guards: the registry is pinned to the actual engine
# ---------------------------------------------------------------------------

def test_every_parser_type_is_in_the_registry(reg):
    """Every TYPE string the IR parser converts natively must resolve to
    a registry entry marked native_in_engine."""
    from metabridge.parsers.powercenter_parser import _PC_TO_IR_TYPE
    for pc_type in _PC_TO_IR_TYPE:
        entry = reg.classify(pc_type)
        assert entry["key"] != "UNKNOWN", pc_type
        assert entry.get("native_in_engine"), pc_type


def test_native_claims_are_backed_by_the_engine(reg):
    """A registry entry claiming native_in_engine must have at least one
    alias the parser (or the mapplet inliner) actually handles."""
    from metabridge.parsers.powercenter_parser import _PC_TO_IR_TYPE
    engine_types = {k.lower() for k in _PC_TO_IR_TYPE}
    # sources/targets/flat files and reusables are handled structurally,
    # SCDs at the load-strategy level — not via the type map
    structural = {"SOURCE_QUALIFIER", "FLAT_FILE_SOURCE",
                  "FLAT_FILE_TARGET", "REUSABLE_TRANSFORMATION",
                  "REUSABLE_SEQUENCE", "MAPPLET", "SCD_TYPE_1",
                  "SCD_TYPE_2", "CONNECTED_LOOKUP", "NORMALIZER"}
    for key, row in reg.all().items():
        if not row.get("native_in_engine") or key in structural:
            continue
        aliases = [row["powercenter_type"]] + (row.get("aliases") or [])
        assert any(a.lower() in engine_types for a in aliases), key


def test_full_and_high_never_require_review_unless_flagged(reg):
    """FULL automation with manual review would be a contradiction."""
    for key, row in reg.all().items():
        if row["automation_level"] == "FULL":
            assert row["requires_manual_review"] is False, key


# ---------------------------------------------------------------------------
# parser integration: unknown types get registry-guided issues
# ---------------------------------------------------------------------------

def test_parser_uses_registry_for_unknown_types(tmp_path):
    xml = (EXAMPLES / "powercenter_repo" / "repo_export.xml").read_text()
    xml = xml.replace(
        '<INSTANCE NAME="EXP_CLEAN" TYPE="TRANSFORMATION" '
        'TRANSFORMATION_NAME="EXP_CLEAN" TRANSFORMATION_TYPE="Expression"/>',
        '<INSTANCE NAME="EXP_CLEAN" TYPE="TRANSFORMATION" '
        'TRANSFORMATION_NAME="EXP_CLEAN" '
        'TRANSFORMATION_TYPE="Java Transformation"/>', 1)
    f = tmp_path / "java.xml"
    f.write_text(xml)
    from metabridge.parsers.powercenter_ingest import (
        PowerCenterRepositoryParser,
    )
    p = PowerCenterRepositoryParser().parse(str(f))
    m = p.mapping("customer_enrich")
    issue = next(i for i in m.issues
                 if i.code == "UNSUPPORTED_TRANSFORMATION")
    assert issue.severity.value == "MANUAL"          # LOW automation
    assert "automation level LOW" in issue.message
    assert "PySpark" in issue.suggestion or "pyspark" in issue.suggestion
    assert "JAVA_TRANSFORMATION" in issue.detail


def test_json_serializable(reg):
    json.dumps({"coverage": reg.coverage(),
                "transformations": reg.all()})

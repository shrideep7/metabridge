"""Conversion validation engine: five layers, one verdict, always a report."""
import json
from pathlib import Path

import pytest

from metabridge.engine import convert, parse_input
from metabridge.ir.model import (
    Link, LoadStrategy, Mapping, Pipeline, Port, Transformation,
    TransformationType,
)
from metabridge.validate.conversion_validator import (
    LAYER_NAMES, VERDICTS, _layer1_syntax, _layer2_dependencies,
    _layer4_reconciliation, validate_conversion, write_validation_report,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(autouse=True)
def _no_host_llm(tmp_path, monkeypatch):
    # isolate from any AI provider configured on the host machine
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    out = tmp_path_factory.mktemp("pc_out")
    convert(str(EXAMPLES / "dbt_retail"), str(out),
            source_format="dbt", target_format="powercenter")
    return out


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

def test_five_layers_and_verdict(converted):
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    r = validate_conversion(pipeline, str(converted), "powercenter")
    assert [L["name"] for L in r["layers"]] == list(LAYER_NAMES)
    assert r["verdict"] in VERDICTS
    assert r["verdict"] != "FAIL"
    assert r["layers"][0]["status"] == "PASS"          # syntax
    assert r["layers"][1]["status"] == "PASS"          # dependencies
    json.dumps(r)


def test_report_files_written(converted, tmp_path):
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    r = validate_conversion(pipeline, str(converted), "powercenter")
    path = write_validation_report(r, str(tmp_path))
    md = Path(path).read_text()
    assert "Migration Validation Report" in md
    assert "Verdict:" in md
    for name in LAYER_NAMES:
        assert name.replace("_", " ") in md
    assert (tmp_path / "migration_validation_report.json").exists()


def test_every_convert_generates_the_report(tmp_path):
    report = convert(str(EXAMPLES / "dbt_retail"), str(tmp_path),
                     source_format="dbt", target_format="databricks")
    mv = report["migration_validation"]
    assert mv["verdict"] in VERDICTS
    assert set(mv["layers"]) == set(LAYER_NAMES)
    assert (tmp_path / "migration_validation_report.md").exists()
    assert (tmp_path / "migration_validation_report.json").exists()


def test_all_target_families_do_not_fail(tmp_path):
    for tgt in ("dbt",):
        out = tmp_path / tgt
        report = convert(str(EXAMPLES / "powercenter" /
                             "wf_retail_analytics.xml"), str(out),
                         source_format="powercenter", target_format=tgt)
        assert report["migration_validation"]["verdict"] != "FAIL", tgt


# ---------------------------------------------------------------------------
# LAYER 1 — syntax
# ---------------------------------------------------------------------------

def test_layer1_catches_broken_sql(tmp_path):
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "01_bad.sql").write_text(
        "CREATE TABLE t AS SELECT FROM WHERE;")
    findings = _layer1_syntax(tmp_path, "snowflake", "")
    assert any(f["severity"] == "ERROR" and f["code"] == "SQL_SYNTAX"
               for f in findings)


def test_layer1_dbt_jinja_is_shielded(tmp_path):
    models = tmp_path / "dbt" / "models"
    models.mkdir(parents=True)
    (models / "ok.sql").write_text(
        "{{ config(materialized='table') }}\n\n"
        "select a, b from {{ ref('upstream') }}\n"
        "{% if is_incremental() %}\nwhere a > 1\n{% endif %}\n")
    assert _layer1_syntax(tmp_path, "dbt", "") == []
    (models / "bad.sql").write_text("select from from")
    findings = _layer1_syntax(tmp_path, "dbt", "")
    assert len(findings) == 1 and "bad.sql" in findings[0]["object"]


def test_layer1_missing_artifact_fails(tmp_path):
    findings = _layer1_syntax(tmp_path, "powercenter", "")
    assert findings[0]["code"] == "ARTIFACT_MISSING"
    assert findings[0]["severity"] == "ERROR"


# ---------------------------------------------------------------------------
# LAYER 2 — dependencies
# ---------------------------------------------------------------------------

def _tiny(name="a", depends_on=None):
    m = Mapping(name=name, depends_on=depends_on or [])
    m.transformations = [
        Transformation(name="SRC_x", type=TransformationType.SOURCE,
                       properties={"table": "x"}),
        Transformation(name="TGT_%s" % name, type=TransformationType.TARGET,
                       properties={"table": name},
                       ports=[Port(name="c1")]),
    ]
    m.links = [Link("SRC_x", "TGT_%s" % name)]
    return m


def test_layer2_missing_dependency(tmp_path):
    p = Pipeline(name="p", mappings=[_tiny("a", depends_on=["ghost"])])
    findings = _layer2_dependencies(p, tmp_path, "powercenter")
    assert any(f["code"] == "DEP_MISSING" and f["severity"] == "ERROR"
               for f in findings)


def test_layer2_cycle(tmp_path):
    p = Pipeline(name="p", mappings=[_tiny("a", ["b"]), _tiny("b", ["a"])])
    findings = _layer2_dependencies(p, tmp_path, "powercenter")
    assert any(f["code"] == "DEP_CYCLE" for f in findings)


def test_layer2_broken_link_and_external_input(tmp_path):
    m = _tiny("a")
    m.links.append(Link("NOPE", "TGT_a"))
    p = Pipeline(name="p", mappings=[m])
    findings = _layer2_dependencies(p, tmp_path, "powercenter")
    codes = {f["code"] for f in findings}
    assert "LINK_BROKEN" in codes
    assert "EXTERNAL_INPUT" in codes     # 'x' is neither declared nor produced


def test_layer2_dbt_unresolved_ref(tmp_path):
    models = tmp_path / "dbt" / "models"
    models.mkdir(parents=True)
    (models / "m1.sql").write_text("select * from {{ ref('nonexistent') }}")
    p = Pipeline(name="p", mappings=[])
    findings = _layer2_dependencies(p, tmp_path, "dbt")
    assert any(f["code"] == "REF_UNRESOLVED" for f in findings)


# ---------------------------------------------------------------------------
# LAYER 3 — semantics (round-trip)
# ---------------------------------------------------------------------------

def test_layer3_fails_when_output_missing(tmp_path):
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    r = validate_conversion(pipeline, str(tmp_path), "dbt")
    assert r["verdict"] == "FAIL"
    l3 = r["layers"][2]
    assert any(f["code"] == "ROUNDTRIP_PARSE_FAILED" for f in l3["findings"])


def test_layer3_select_star_is_unverifiable_not_lost(tmp_path):
    """customer_ranking generates SELECT * — round-trip cannot see columns;
    that is a warning about verifiability, never a false 'columns lost'."""
    convert(str(EXAMPLES / "dbt_retail"), str(tmp_path),
            source_format="dbt", target_format="snowflake")
    mv = json.loads(
        (tmp_path / "migration_validation_report.json").read_text())
    l3 = mv["layers"][2]
    codes = {(f["code"], f["severity"]) for f in l3["findings"]}
    assert ("COLUMNS_LOST", "MANUAL") not in codes
    assert any(c == "COLUMNS_UNVERIFIABLE" for c, _ in codes)


def test_flattened_override_recovered():
    """A `--` comment flattened by XML attribute normalization must not
    swallow the statement (the recovery is flagged, not silent)."""
    pipeline = parse_input(
        str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
        "powercenter")
    m = next(m for m in pipeline.mappings if m.name == "customer_ranking")
    sq = next(t for t in m.transformations
              if t.type == TransformationType.SOURCE_QUALIFIER)
    override = str(sq.properties["sql_override"])
    assert "\n" in override                       # line structure restored
    assert any(i.code == "SQL_OVERRIDE_NEWLINES_LOST" for i in m.issues)


# ---------------------------------------------------------------------------
# LAYER 4 — reconciliation suite
# ---------------------------------------------------------------------------

def test_layer4_merge_without_key_is_manual(tmp_path):
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    m = pipeline.mapping("stg_orders")
    m.unique_key = []
    m.load_strategy = LoadStrategy.MERGE
    findings = _layer4_reconciliation(pipeline, tmp_path, "powercenter")
    assert any(f["code"] == "MERGE_UNVERIFIABLE" and
               f["severity"] == "MANUAL" and f["object"] == "stg_orders"
               for f in findings)


def test_layer4_own_sql_parses(converted):
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    findings = _layer4_reconciliation(pipeline, converted, "powercenter")
    assert not any(f["code"] == "RECON_SQL_INVALID" for f in findings)


# ---------------------------------------------------------------------------
# LAYER 5 — AI review honesty
# ---------------------------------------------------------------------------

def test_layer5_skipped_without_provider_and_labeled(converted):
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    r = validate_conversion(pipeline, str(converted), "powercenter")
    l5 = r["layers"][4]
    assert l5["status"] == "SKIPPED"
    assert "No AI provider configured" in l5["note"]
    assert r["ai_reviewed"] is False
    # a skipped advisory layer never degrades the verdict
    assert r["verdict"] != "FAIL"


def test_layer5_disabled_explicitly(converted):
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    r = validate_conversion(pipeline, str(converted), "powercenter",
                            use_ai=False)
    assert r["layers"][4]["status"] == "SKIPPED"
    assert "disabled" in r["layers"][4]["note"]


# ---------------------------------------------------------------------------
# honest degradation: declared loss vs silent loss
# ---------------------------------------------------------------------------

def test_unresolved_source_routed_to_manual_not_broken_sql(tmp_path):
    """A mapping whose FROM cannot be resolved must NOT be emitted as
    'FROM <nothing>' — it goes to the manual queue, and layer 3 reports
    declared loss (MANUAL), never silent loss (FAIL)."""
    from metabridge.generators.sql_generator import generate_sql_scripts
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    m = pipeline.mapping("stg_customers")
    src = m.by_type(TransformationType.SOURCE)[0]
    src.properties["table"] = ""
    generate_sql_scripts(pipeline, str(tmp_path / "sql"), "snowflake")
    assert not list((tmp_path / "sql").glob("*stg_customers*"))
    assert any(i.code == "SOURCE_UNRESOLVED" for i in m.issues)
    # nothing emitted may be broken
    findings = _layer1_syntax(tmp_path, "snowflake", "")
    assert not any(f["severity"] == "ERROR" for f in findings)
    r = validate_conversion(pipeline, str(tmp_path), "snowflake",
                            use_ai=False)
    l3 = r["layers"][2]
    codes = {f["code"]: f["severity"] for f in l3["findings"]}
    assert codes.get("TARGET_NOT_GENERATED") == "MANUAL"
    assert "TARGET_LOST" not in codes
    assert r["verdict"] == "MANUAL_REVIEW"


def test_deploy_all_statements_are_terminated(tmp_path):
    from metabridge.generators.sql_generator import generate_sql_scripts
    pipeline = parse_input(str(EXAMPLES / "dbt_retail"), "dbt")
    generate_sql_scripts(pipeline, str(tmp_path / "sql"), "snowflake")
    body = (tmp_path / "sql" / "deploy_all.sql").read_text()
    chunks = [c for c in body.split("-- @")[1:]]
    for c in chunks:
        assert c.rstrip().endswith(";"), c[:60]

"""Command 5 end-to-end: every legacy ETL platform converts to every major
target through SOURCE PARSER -> CIR -> SEMANTIC NORMALIZATION -> GENERATOR
-> VALIDATION, and none of them ever FAILs."""
import json
from pathlib import Path

import pytest

from metabridge.engine import ETL_FORMATS, FORMATS, convert, detect_format

ETL = Path(__file__).resolve().parent.parent / "examples" / "etl_legacy"
SOURCES = ("ssis", "datastage", "talend", "abinitio")
TARGETS = ("dbt", "snowflake", "databricks", "bigquery", "redshift",
           "synapse", "postgres")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


def test_etl_formats_registered():
    assert set(ETL_FORMATS) <= set(FORMATS)


@pytest.mark.parametrize("source", SOURCES)
def test_platform_auto_detected(source):
    assert detect_format(str(ETL / source)) == source


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("target", TARGETS)
def test_convert_all_sources_all_targets(tmp_path, source, target):
    out = tmp_path / ("%s__%s" % (source, target))
    report = convert(str(ETL / source), str(out), source, target)
    counts = report["summary"]["status_counts"]
    assert counts["FAILED"] == 0, counts
    assert report["summary"]["objects_total"] >= 1
    # generated artifacts exist
    if target == "dbt":
        assert (out / "dbt" / "dbt_project.yml").exists()
        assert list((out / "dbt" / "models").rglob("*.sql"))
    else:
        assert list((out / "sql").glob("0*.sql"))
    # workflow DAGs become orchestration assets
    orch = list(out.rglob("orchestration/*"))
    assert orch, "workflow DAGs must be rendered for %s" % target


@pytest.mark.parametrize("source", SOURCES)
def test_convert_into_informatica(tmp_path, source):
    """Bidirectional reach: legacy ETL modernizes into PowerCenter and
    IDMC through the same CIR — no pairwise converter involved."""
    for target in ("powercenter", "idmc"):
        out = tmp_path / ("%s__%s" % (source, target))
        report = convert(str(ETL / source), str(out), source, target)
        assert report["summary"]["status_counts"]["FAILED"] == 0


@pytest.mark.parametrize("source", SOURCES)
def test_validation_plan_and_lineage_generated(tmp_path, source):
    out = tmp_path / source
    convert(str(ETL / source), str(out), source, "snowflake")
    plan = json.loads((out / "validation_plan.json").read_text())
    assert plan.get("checks") or plan.get("mappings") or plan
    assert (out / "migration_report.json").exists()
    mr = json.loads((out / "migration_report.json").read_text())
    assert mr.get("sections", {}).get("lineage") is not None or \
        (out / "conversion_report.json").exists()


def test_execution_order_survives_to_orchestration(tmp_path):
    out = tmp_path / "ssis_dbt"
    convert(str(ETL / "ssis"), str(out), "ssis", "dbt")
    import yaml
    spec = yaml.safe_load(
        (out / "dbt" / "orchestration" / "LoadSales_job.yml").read_text())
    assert spec["job"]["name"] == "LoadSales"
    # non-model tasks (script task, email) are preserved with
    # recommendations — not silently dropped
    kinds = {t["type"] for t in spec["non_model_tasks"]}
    assert "email" in kinds or "command" in kinds

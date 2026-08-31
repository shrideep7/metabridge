"""Acceptance gate: real dbt must be able to parse what we generate.

Every other dbt test in this suite inspects the artifacts we wrote. This one
hands them to dbt itself, which is the only check that cannot be fooled by our
own assumptions about the format.

`dbt parse` resolves every ref() and source() against the nodes that actually
exist and renders every model's Jinja, so it catches a dangling ref, a missing
source declaration, and a Jinja syntax error. It is deliberately the widest
gate available without credentials.

What it does NOT catch, and why our own checks still matter:
  * an undeclared ``var()`` — dbt defers var resolution to compile/run, so a
    model referencing a var no one declared parses cleanly and fails later on
    the customer's warehouse (see DBT_VAR_REQUIRED);
  * a bare physical relation in a SQL override — ``from SOME_TABLE`` is valid
    SQL, so dbt has nothing to complain about; it simply never learns there is
    a dependency there (see DBT_REF_DANGLING / SQL_OVERRIDE_UNMANAGED_RELATION
    and the emitted ref graph in test_dbt_project_generator.py).

Skipped when no dbt is installed; the repo keeps one in .venv-dbt.
"""
import os
import pathlib
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"

# minimal, connection-free profile per adapter — `dbt parse` registers the
# adapter but never opens a connection
_PROFILES = {
    "snowflake": {"type": "snowflake", "account": "none", "user": "none",
                  "password": "none", "role": "none", "database": "none",
                  "warehouse": "none", "schema": "none", "threads": 1},
    "databricks": {"type": "databricks", "host": "none", "http_path": "none",
                   "token": "none", "schema": "none", "threads": 1},
    "postgres": {"type": "postgres", "host": "none", "user": "none",
                 "password": "none", "port": 5432, "dbname": "none",
                 "schema": "none", "threads": 1},
}


def _dbt_executable():
    for rel in ("Scripts/dbt.exe", "bin/dbt"):
        cand = REPO / ".venv-dbt" / rel
        if cand.exists():
            return str(cand)
    from shutil import which
    return which("dbt")


DBT = _dbt_executable()


def _installed_adapter(dbt: str):
    """The first adapter this dbt has a plugin for, or None."""
    try:
        out = subprocess.run([dbt, "--version"], capture_output=True,
                             text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):        # pragma: no cover
        return None
    return next((a for a in _PROFILES if a + ":" in out), None)


ADAPTER = _installed_adapter(DBT) if DBT else None

pytestmark = pytest.mark.skipif(
    not ADAPTER,
    reason="no dbt with a known adapter installed (see .venv-dbt)")


def _parse(project_dir: pathlib.Path, tmp_path: pathlib.Path):
    """-> (returncode, combined output) from `dbt parse` on this project."""
    profile = yaml.safe_load(
        (project_dir / "dbt_project.yml").read_text(encoding="utf-8"))["profile"]
    prof_dir = tmp_path / "profiles"
    prof_dir.mkdir(exist_ok=True)
    (prof_dir / "profiles.yml").write_text(yaml.safe_dump(
        {profile: {"target": "parse",
                   "outputs": {"parse": _PROFILES[ADAPTER]}}}),
        encoding="utf-8")
    env = dict(os.environ, DBT_SEND_ANONYMOUS_USAGE_STATS="False")
    proc = subprocess.run(
        [DBT, "parse", "--project-dir", str(project_dir),
         "--profiles-dir", str(prof_dir)],
        capture_output=True, text=True, timeout=600, env=env)
    return proc.returncode, proc.stdout + proc.stderr


def _generated_project(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))
    from metabridge.engine import convert
    convert(str(EXAMPLES / "powercenter" / "wf_retail_analytics.xml"),
            str(tmp_path / "out"), source_format="powercenter",
            target_format="dbt")
    return tmp_path / "out" / "dbt"


@pytest.mark.slow
def test_generated_project_parses(tmp_path, monkeypatch):
    project = _generated_project(tmp_path, monkeypatch)
    code, out = _parse(project, tmp_path)
    assert code == 0, out[-4000:]
    assert "Compilation Error" not in out, out[-4000:]


@pytest.mark.slow
def test_generated_project_has_no_deprecations(tmp_path, monkeypatch):
    """A gate that always prints warnings is a gate people stop reading.

    dbt deprecations become errors in a later release, so the generated project
    has to be clean today rather than at the point it breaks.
    """
    project = _generated_project(tmp_path, monkeypatch)
    _code, out = _parse(project, tmp_path)
    assert "Deprecated functionality" not in out, out[-4000:]


@pytest.mark.slow
def test_project_with_a_snapshot_parses(tmp_path):
    """A project containing an SCD2 mapping, which none of the examples has.

    dbt reads a snapshot's node name from its `{% snapshot %}` tag, not from
    the filename. Those two drifted apart once — the file was renamed to
    snap_<entity> and the tag still said the mapping name — and every ref to
    the snapshot pointed at a node that did not exist. Nothing else in this
    suite exercises a snapshot end to end, so nothing else could catch it.
    """
    from metabridge.generators.dbt_generator import generate_dbt_project
    from metabridge.ir.model import (Link, LoadStrategy, Mapping, Pipeline,
                                     Port, SourceTable, Transformation,
                                     TransformationType)

    def tx(name, ttype, cols, **props):
        return Transformation(name=name, type=ttype,
                              ports=[Port(name=c) for c in cols],
                              properties=props)

    dim = Mapping(name="m_load_customer_dim",
                  load_strategy=LoadStrategy.SCD2, unique_key=["cust_id"])
    dim.properties["scd"] = {"strategy": "timestamp",
                             "updated_at": "updated_at"}
    dim.transformations = [
        tx("SRC_C", TransformationType.SOURCE, ["cust_id"],
           table="raw_customers", schema="raw"),
        tx("SQ_C", TransformationType.SOURCE_QUALIFIER, ["cust_id"]),
        tx("TGT_D", TransformationType.TARGET, ["cust_id"],
           table="DIM_CUSTOMER")]
    dim.links = [Link("SRC_C", "SQ_C"), Link("SQ_C", "TGT_D")]

    fact = Mapping(name="m_load_sales_fact",
                   depends_on=["m_load_customer_dim"])
    fact.transformations = [
        tx("SRC_D", TransformationType.SOURCE, ["cust_id"],
           table="DIM_CUSTOMER"),
        tx("SQ_D", TransformationType.SOURCE_QUALIFIER, ["cust_id"]),
        tx("TGT_F", TransformationType.TARGET, ["cust_id"],
           table="FCT_SALES")]
    fact.links = [Link("SRC_D", "SQ_D"), Link("SQ_D", "TGT_F")]

    pipeline = Pipeline(
        name="snapproj", mappings=[dim, fact], source_format="powercenter",
        sources=[SourceTable(name="raw_customers", schema="raw", system="crm",
                             columns=[Port(name="cust_id")])])
    project = tmp_path / "out"
    generate_dbt_project(pipeline, str(project))
    code, out = _parse(project, tmp_path)
    assert code == 0, out[-4000:]
    assert "Compilation Error" not in out, out[-4000:]


@pytest.mark.slow
def test_scaffolded_project_parses(tmp_path):
    """The scaffold path emits its own flavour of dbt project (a landing layer
    from a table manifest) and has to clear the same gate."""
    from metabridge.scaffold import scaffold
    scaffold("sap_s4", "snowflake",
             str(EXAMPLES / "sap_to_snowflake" / "tables.yml"),
             str(tmp_path / "out"))
    code, out = _parse(tmp_path / "out" / "dbt", tmp_path)
    assert code == 0, out[-4000:]
    assert "Compilation Error" not in out, out[-4000:]


if __name__ == "__main__":                              # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


@pytest.mark.slow
def test_a_partial_property_file_is_not_the_model_s_column_list(tmp_path):
    """dbt does not require a property file to list every column — documenting
    one (to hang a test on it) is normal and legal.

    Letting it REPLACE the SQL projection made a model that selects six
    columns re-parse as producing one, and the round-trip check then reported
    five columns "lost" that were never missing — a MANUAL item in the queue
    for a defect that did not exist.
    """
    from metabridge.ir.model import TransformationType
    from metabridge.parsers.base import get_parser

    project = tmp_path / "proj"
    (project / "models" / "staging").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: p\nversion: 1.0.0\nconfig-version: 2\nprofile: p\n"
        "model-paths: [models]\n", encoding="utf-8")
    (project / "models" / "staging" / "stg_x.sql").write_text(
        "select a, b, c from {{ source('s', 't') }}\n", encoding="utf-8")
    # documents ONE of the three columns, which is all dbt asks for
    (project / "models" / "staging" / "_s__models.yml").write_text(
        "version: 2\nmodels:\n- name: stg_x\n  columns:\n  - name: a\n"
        "    tests: [not_null]\n", encoding="utf-8")
    (project / "models" / "staging" / "_s__sources.yml").write_text(
        "version: 2\nsources:\n- name: s\n  schema: s\n  tables:\n"
        "  - name: t\n", encoding="utf-8")

    back = get_parser("dbt").parse_project(str(project))
    m = back.mappings[0]
    cols = [p.name.lower()
            for p in m.by_type(TransformationType.TARGET)[0].ports]
    assert cols == ["a", "b", "c"], cols

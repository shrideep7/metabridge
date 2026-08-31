"""The generated-artifact templates: packaging, overrides, and pass-through.

The dbt generator renders every TEXT artifact (model SQL, snapshot blocks, doc
blocks, .gitignore, packages.yml) from a Jinja template. The structured YAML
files are deliberately not templated — they are built as dicts and serialised
with yaml.safe_dump, which cannot emit invalid YAML.
"""
import pathlib
import re

import pytest
import tomllib

from metabridge.generators.dbt_generator import (TEMPLATE_OVERRIDE_ENV,
                                                 render_template)

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "src" / "metabridge" / "generators" / "templates" / "dbt"


def test_every_template_is_reachable():
    """A template that exists on disk but cannot be loaded is a latent
    TemplateNotFound in a customer's install."""
    names = sorted(p.name for p in TEMPLATES.glob("*.j2"))
    assert names, "no templates found"
    for name in names:
        assert render_template(name, **_dummy_context(name)) is not None


def _dummy_context(name):
    """Enough context to render each template once."""
    return {
        "model.sql.j2": {"header": "", "ctes": "a as (select 1)",
                         "terminal": "a"},
        "model_empty.sql.j2": {"header": ""},
        "model_passthrough.sql.j2": {"header": "", "upstream": "up"},
        "staging_model.sql.j2": {"projection": "select\n    a",
                                 "source": "s", "table": "t"},
        "snapshot.sql.j2": {"name": "snap_x", "config": "unique_key='id'",
                            "select": "select 1"},
        "source_docs.md.j2": {"group": "g", "project": "p", "platform": "x",
                              "databases": "d", "schemas": "s",
                              "table_count": 1, "column_count": 2},
        "incremental_where.sql.j2": {"predicate": "a > b"},
        "macros.yml.j2": {"macros": [
            {"name": "mapplet_x", "description": "d",
             "arguments": [{"name": "relation", "type": "string",
                            "description": "the upstream relation"}]}]},
        "generate_schema_name.sql.j2": {},
        "packages.yml.j2": {},
        "seeds.yml.j2": {},
        "gitignore.j2": {},
    }[name]


def test_templates_are_declared_as_package_data():
    """Templates live outside the .py files, so setuptools only ships them if
    package-data says so. Without it a wheel install has no templates at all
    and every generation fails — invisible when developing from a checkout."""
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = cfg["tool"]["setuptools"]["package-data"]["metabridge"]
    assert any(p.endswith("*.j2") for p in patterns), patterns


def test_templates_are_lf_only():
    """These render into SQL. A template checked out with CRLF put carriage
    returns inside generated models, which is invisible until someone diffs
    the output on another platform (.gitattributes pins the checkout; this
    pins the committed bytes)."""
    for p in sorted(TEMPLATES.glob("*.j2")):
        assert b"\r" not in p.read_bytes(), p.name


def test_dbt_jinja_passes_through_untouched():
    """We generate Jinja WITH Jinja. Our delimiters are << >> / <% %> so dbt's
    own {{ ref() }} and {% snapshot %} survive rendering verbatim; if they were
    ever evaluated as ours, the artifact would come out empty or raise."""
    out = render_template("model_passthrough.sql.j2", header="",
                          upstream="stg_x")
    assert out == "select * from {{ ref('stg_x') }}\n"
    snap = render_template("snapshot.sql.j2", name="snap_x", config="a=1",
                           select="select 1")
    assert snap.startswith("{% snapshot snap_x %}\n{{ config(a=1) }}")
    assert snap.rstrip().endswith("{% endsnapshot %}")


def test_a_missing_variable_fails_loudly():
    """StrictUndefined: a typo in a template must not render an empty string
    into a customer's SQL."""
    from jinja2 import UndefinedError
    with pytest.raises(UndefinedError):
        render_template("model.sql.j2", header="", ctes="x")


def test_project_templates_override_the_packaged_ones(tmp_path, monkeypatch):
    """A delivery team can change the shape of a generated artifact — a house
    header, a standard config block — without patching the generator."""
    override = tmp_path / "templates"
    override.mkdir()
    (override / "gitignore.j2").write_text("# house style\ntarget/\n",
                                           encoding="utf-8")
    monkeypatch.setenv(TEMPLATE_OVERRIDE_ENV, str(override))
    assert render_template("gitignore.j2") == "# house style\ntarget/\n"
    # partial override: anything absent still falls back to the packaged set
    assert "{% snapshot" in render_template(
        "snapshot.sql.j2", name="s", config="a=1", select="select 1")


def test_override_reaches_a_generated_project(tmp_path, monkeypatch):
    from metabridge.engine import parse_input
    from metabridge.generators.dbt_generator import generate_dbt_project
    override = tmp_path / "templates"
    override.mkdir()
    (override / "model.sql.j2").write_text(
        "-- house header\n<< header >>with << ctes >>\n\n"
        "select * from << terminal >>\n", encoding="utf-8")
    monkeypatch.setenv(TEMPLATE_OVERRIDE_ENV, str(override))
    pipeline = parse_input(
        str(REPO / "examples" / "powercenter" / "wf_retail_analytics.xml"),
        "powercenter")
    out = tmp_path / "out"
    generate_dbt_project(pipeline, str(out))
    models = list(out.rglob("models/marts/**/*.sql"))
    assert models
    for m in models:
        assert m.read_text().startswith("-- house header\n"), m.name


def test_no_percent_escaped_jinja_left_in_the_generator():
    """The templates exist so SQL-containing-Jinja is not written as
    %%-escaped format strings. Leaving one behind means an artifact whose
    shape is still hidden in Python."""
    src = (REPO / "src" / "metabridge" / "generators"
           / "dbt_generator.py").read_text(encoding="utf-8")
    # comments are allowed to quote the old form while explaining why it went
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    offenders = re.findall(r'"[^"\n]*\{%%[^"\n]*"', code)
    assert not offenders, offenders

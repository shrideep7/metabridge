"""Shared test helpers.

The dbt generator derives a model's LAYER — and therefore its `stg_`/`int_`/
`dim_`/`fct_` prefix and its subfolder — from what the mapping actually does.
That means a test which hardcodes `models/intermediate/int_sales.sql` is
asserting the placement rules a second time, in a file that is really about
something else, and it breaks whenever those rules are refined.

`model_sql` resolves a model through `migration_manifest.json` instead — the
artifact whose whole job is to trace source object -> dbt object(s). A test
that cares about rendered SQL says which mapping it means and gets the SQL;
tests that genuinely own the placement rules (test_dbt_project_generator.py)
keep asserting paths and names directly.
"""
import json
from pathlib import Path


def _names_match(wanted, obj, key):
    """Whether `wanted` names this manifest object under `key`.

    Exact, with a leading `m_` tolerated: a PowerCenter mapping is exported as
    `m_load_sales` and referred to everywhere else as `load_sales`.
    Deliberately not a substring match — `sales` would otherwise also select
    `agg_sales` and `rpt_sales`, and a test that silently asserts against the
    wrong model is worse than one that fails.
    """
    name = str(obj.get(key, "")).lower()
    return wanted.lower() in (
        name, name[2:] if name.startswith("m_") else name)


def dbt_objects(project, source_type="mapping", source_object=None, role=None):
    """Manifest entries for the generated project at `project`.

    project        the dbt project root (the directory holding dbt_project.yml)
    source_type    "mapping" | "source_definition"
    source_object  the mapping / source name, when the fixture has several
    role          "staging" | "transformation_logic" | "mart" | "snapshot"
    """
    project = Path(project)
    doc = json.loads(
        (project / "migration_manifest.json").read_text(encoding="utf-8"))
    objects = [o for o in doc["objects"]
               if not source_type or o.get("source_type") == source_type]
    if source_object:
        # CIR name first. Two mappings in different folders can share one
        # PowerCenter name (`m_load_sales` in both), and the CIR is where they
        # are told apart (`load_sales` vs `load_sales__finance`) — so matching
        # the source name first would make both of them ambiguous.
        matched = [o for o in objects
                   if _names_match(source_object, o, "cir_object")]
        objects = matched or [o for o in objects
                              if _names_match(source_object, o,
                                              "source_object")]
    out = []
    for obj in objects:
        for dbt_obj in obj.get("dbt_objects", []):
            if role and dbt_obj.get("role") != role:
                continue
            out.append(dbt_obj)
    return out


def model_sql(project, source_object=None, role=None):
    """The SQL of the model generated for a mapping.

    With no `source_object`, the fixture is expected to have produced exactly
    one mapping model — which is the case for the single-mapping fixtures that
    exercise one transformation type at a time. Ambiguity raises rather than
    silently picking one, so a test can never quietly assert against the wrong
    model.
    """
    project = Path(project)
    hits = dbt_objects(project, "mapping", source_object, role)
    if not hits:
        raise AssertionError(
            "no dbt model in the manifest for source_object=%r role=%r; "
            "generated: %s" % (source_object, role,
                              sorted(p.name for p in
                                     project.rglob("models/**/*.sql"))))
    if len(hits) > 1 and not role:
        # prefer the model that carries the logic over a thin wrapper (the
        # legacy layered layout emits both for one mapping)
        logic = [h for h in hits
                 if h["role"] in ("transformation_logic", "staging")]
        hits = logic or hits
    if len(hits) > 1:
        raise AssertionError(
            "ambiguous: %d models match — pass source_object= or role= (%s)"
            % (len(hits), ", ".join(h["name"] for h in hits)))
    return (project / hits[0]["path"]).read_text(encoding="utf-8")


def staging_sql(project, source_object=None):
    """The SQL of the staging view generated over a source table."""
    project = Path(project)
    hits = dbt_objects(project, "source_definition", source_object)
    if len(hits) != 1:
        raise AssertionError(
            "expected one staging model for source_object=%r, got %d"
            % (source_object, len(hits)))
    return (project / hits[0]["path"]).read_text(encoding="utf-8")

"""Merge an ETL project's transformation logic into a scaffold pipeline.

A table manifest describes the RAW layer and nothing else, so scaffolding it
alone produces a project of pass-through models — the curated layer is left
behind in whatever tool built it. `merge_procedure_logic` already solves that
for logic held in stored procedures; this module does the same for logic held
in an ETL tool (Informatica PowerCenter/IDMC, DataStage, SSIS, Talend).

The two halves used to require two separate runs, and neither could see the
other. That is not only inconvenient: a table the ETL PRODUCES is also a table
the manifest happily lands, so the same relation gets built twice — once
copied from the source, once rebuilt by the converted logic — and they diverge
the moment either side changes. Nothing detects it, because the detection
needs both facts in one process. Merging here is what makes the landing layer
scopeable at all.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .ir.model import IssueSeverity, Pipeline, SourceTable, TransformationType
from .procedures import _relink, _staging_map


def _safe(s: str) -> str:
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in str(s))


def _table_key(schema: str, table: str) -> str:
    return ("%s.%s" % (schema, table)).strip(".").lower()


def _manifest_index(pipeline: Pipeline):
    """-> (qualified index, bare-name index, ambiguous bare names).

    Matching on the bare table name alone is right almost always and wrong in
    exactly the case that matters — the same name in two schemas, which is how
    a RAW and a SILVER copy of one table are usually told apart."""
    qualified: Dict[str, SourceTable] = {}
    bare: Dict[str, List[SourceTable]] = {}
    for s in pipeline.sources:
        qualified[_table_key(str(s.schema or ""), s.name)] = s
        bare.setdefault(s.name.lower(), []).append(s)
    ambiguous = {k for k, v in bare.items() if len(v) > 1}
    return qualified, bare, ambiguous


def _rebase_sources(logic: Pipeline, pipeline: Pipeline, summary: dict) -> None:
    """Reconcile the ETL project's source tables against the manifest.

    The manifest's definitions win: its columns come from the catalog, the ETL
    tool's are that tool's own approximation of them. A source the manifest
    does not carry is reported rather than invented — it means the raw layer is
    incomplete for this logic, and a model reading it would not compile."""
    qualified, bare, ambiguous = _manifest_index(pipeline)
    missing: List[str] = []
    unclear: List[str] = []

    for m in logic.mappings:
        for t in m.by_type(TransformationType.SOURCE):
            table = str(t.properties.get("table", "") or "")
            if not table:
                continue
            schema = str(t.properties.get("schema", "") or "")
            key, low = _table_key(schema, table), table.lower()
            if key in qualified:
                continue
            if low in ambiguous:
                unclear.append("%s (in %s)" % (
                    table, ", ".join(sorted(
                        str(s.schema or "?") for s in bare[low]))))
                continue
            if low not in bare:
                # report the source's own spelling — the match key is lowered
                # for comparison, and a user told to add `raw_schema.customers`
                # goes looking for something their catalog does not call that
                missing.append("%s.%s" % (schema, table) if schema else table)

    for name in sorted(set(unclear)):
        pipeline.issues.append(
            _issue(IssueSeverity.MANUAL, "ETL_SOURCE_AMBIGUOUS",
                   "The ETL reads %s and the manifest carries that table in "
                   "more than one schema — which one feeds this logic cannot "
                   "be inferred" % name,
                   suggestion="Re-analyze with a schema scope that keeps only "
                              "the layer this ETL reads."))
    for name in sorted(set(missing)):
        pipeline.issues.append(
            _issue(IssueSeverity.MANUAL, "ETL_SOURCE_NOT_IN_MANIFEST",
                   "The ETL reads %s, which the table manifest does not carry "
                   "— the generated model references it by bare name and will "
                   "not compile until that relation exists on the target"
                   % name,
                   suggestion="Add the table to the manifest (re-analyze with "
                              "a schema scope that includes it) so it is "
                              "landed and staged first."))
    summary["unresolved_sources"] = sorted(set(missing))
    summary["ambiguous_sources"] = sorted(set(unclear))


def _issue(severity, code, message, suggestion=""):
    from .ir.model import ConversionIssue
    return ConversionIssue(severity=severity, code=code, message=message,
                           suggestion=suggestion)


def _claim_names(pipeline: Pipeline, logic: Pipeline, label: str) -> Dict[str, str]:
    """Rename incoming mappings that collide with ones already in the project.

    Two models cannot share a file name. Renaming has to happen before the
    dependency lists are rewritten, or a downstream mapping keeps pointing at
    the name that lost."""
    taken = {m.name.lower() for m in pipeline.mappings}
    renamed: Dict[str, str] = {}
    for m in logic.mappings:
        if m.name.lower() in taken:
            base = "%s__%s" % (m.name, _safe(label).lower())
            new, n = base, 2
            while new.lower() in taken:
                new = "%s_%d" % (base, n)
                n += 1
            renamed[m.name.lower()] = new
            m.add_issue(IssueSeverity.WARNING, "ETL_MODEL_NAME_TAKEN",
                        "A mapping named '%s' already exists — the model from "
                        "%s is named '%s' instead." % (m.name, label, new))
            m.name = new
        taken.add(m.name.lower())
    for m in logic.mappings:
        m.depends_on = [renamed.get(d.lower(), d) for d in m.depends_on]
    return renamed


def _target_tables(logic: Pipeline,
                   only: Optional[set] = None) -> Dict[str, str]:
    """{table (lower): the mapping that builds it}.

    ``only`` restricts the scan to named mappings. That restriction is not
    optional in practice: a scaffold gives every manifest table a staging
    mapping which declares a TARGET of its own (`TGT_stg_accounts`), so an
    unrestricted scan finds every table "built" and unlands the entire
    landing layer."""
    out: Dict[str, str] = {}
    for m in logic.mappings:
        if only is not None and m.name not in only:
            continue
        for t in m.by_type(TransformationType.TARGET):
            table = str(t.properties.get("table", "") or "").lower()
            if table:
                out.setdefault(table, m.name)
    return out


def unland_built_tables(pipeline: Pipeline, built: Dict[str, str],
                        stage_of: Optional[Dict[str, str]] = None,
                        origin: str = "the converted logic") -> List[dict]:
    """Drop tables that MERGED LOGIC rebuilds from the landing layer.

    A curated table the logic rebuilds is not a table to copy across: landing
    it too yields the same relation twice, diverging silently. This applies
    equally to logic lifted out of stored procedures and logic converted from
    an ETL tool — the source of the logic changes nothing about the
    duplication.

    Two removals are needed, not one. `pipeline.sources` drives the landing
    DDL and the unload/load pair, but a scaffold ALSO gives every manifest
    table a pass-through staging mapping — so dropping the source alone still
    leaves a `stg_` model selecting from a relation that is no longer created.
    `stage_of` must be the staging map taken BEFORE any logic joined, or it
    reports the incoming mappings' own sources as staged too."""
    stage_of = stage_of or {}
    dropped: List[dict] = []
    keep: List[SourceTable] = []
    drop_models: set = set()

    for s in pipeline.sources:
        producer = built.get(s.name.lower())
        if producer is None:
            keep.append(s)
            continue
        dropped.append({"table": s.name, "schema": str(s.schema or ""),
                        "built_by": producer})
        staged = stage_of.get(s.name.lower())
        if staged:
            drop_models.add(staged)
        pipeline.issues.append(
            _issue(IssueSeverity.INFO, "LOGIC_TARGET_NOT_LANDED",
                   "%s.%s is not landed from the source: %s builds it (%s), "
                   "so copying it as raw data too would produce the same "
                   "table twice"
                   % (s.schema or "?", s.name, origin, producer),
                   suggestion="Tick 'land those tables anyway' to keep it — "
                              "useful only while running both sides in "
                              "parallel to compare them."))
    if not dropped:
        return dropped

    pipeline.sources[:] = keep

    # The merge warns "landed AND rebuilt" while deciding; unlanding then
    # settles it. Left in place that warning describes a duplication that no
    # longer exists and tells the reader to fix something already fixed, so
    # it is withdrawn for exactly the tables that were dropped.
    # Withdrawn from EVERY mapping, not just the one recorded as the producer:
    # one procedure decomposes into several statements that all write the same
    # table, so each raises its own copy and matching the producer alone left
    # the rest behind.
    stale = {"PROCEDURE_TARGET_IS_LANDED_TABLE", "LOGIC_TARGET_IS_LANDED_TABLE"}
    qualified = {"%s.%s" % (d["schema"] or "?", d["table"]) for d in dropped}
    for m in pipeline.mappings:
        m.issues[:] = [i for i in m.issues
                       if not (i.code in stale
                               and any(q in i.message for q in qualified))]

    if drop_models:
        pipeline.mappings[:] = [m for m in pipeline.mappings
                                if m.name not in drop_models]
        # nothing may depend on a model that no longer exists
        for m in pipeline.mappings:
            m.depends_on = [d for d in m.depends_on if d not in drop_models]
    return dropped


def merge_etl_logic(pipeline: Pipeline, bundle_path: str, fmt: str = "",
                    land_targets: bool = False,
                    stage_of: Optional[Dict[str, str]] = None) -> dict:
    """Parse an ETL project and merge its mappings into `pipeline` in place.

    -> a summary the caller reports on."""
    from .parsers.base import get_parser, parser_for_path

    parser = get_parser(fmt) if fmt else parser_for_path(bundle_path)
    label = getattr(parser, "display_name", "") or getattr(
        parser, "format_name", "ETL")
    summary: dict = {"format": getattr(parser, "format_name", fmt or ""),
                     "label": label, "mappings": [], "converted": 0,
                     "not_landed": [], "unresolved_sources": [],
                     "ambiguous_sources": []}

    logic = parser.parse_project(bundle_path)
    if not logic.mappings:
        pipeline.issues.append(
            _issue(IssueSeverity.MANUAL, "ETL_BUNDLE_EMPTY",
                   "No transformation logic was found in the supplied %s "
                   "project, so the generated project carries the raw layer "
                   "only" % label))
        return summary

    _rebase_sources(logic, pipeline, summary)
    _claim_names(pipeline, logic, label)

    # Wire each incoming mapping onto the staging models rather than back onto
    # the raw relation, the same way procedure-derived logic is wired. The
    # caller may hand in the map taken before ANY logic merged; computing it
    # here would already include a procedure layer's own sources.
    stage_of = stage_of if stage_of is not None else _staging_map(pipeline)
    known = {s.name.lower(): s for s in pipeline.sources}
    produced = {n for n in _target_tables(logic)}
    for m in logic.mappings:
        _relink(m, stage_of, known, produced)
        if not m.description:
            m.description = "Transformation logic from %s" % label
        m.properties.setdefault("source_etl", label)
        pipeline.mappings.append(m)
        summary["mappings"].append({
            "model": m.name, "origin": m.origin or m.name,
            "load_strategy": m.load_strategy.value,
            "depends_on": list(m.depends_on)})
    summary["converted"] = len(logic.mappings)

    pipeline.issues.extend(logic.issues)
    for key in ("workflow_dags", "etl_units"):
        extra = logic.metadata.get(key)
        if extra:
            pipeline.metadata.setdefault(key, []).extend(extra)

    if not land_targets:
        built = _target_tables(pipeline,
                              only={m["model"] for m in summary["mappings"]})
        summary["not_landed"] = unland_built_tables(
            pipeline, built, stage_of, origin="the converted %s logic" % label)
    return summary

"""Deterministic dbt object naming and placement.

Every name and path is a pure function of the mapping / source table — same
input, same result, every run — so a diff between two generated projects shows
real change, not churn.

Two layouts:

``standard`` (the default)
    dbt Labs' own convention, from *How we structure our dbt projects*:
    staging subfoldered by SOURCE SYSTEM, intermediate and marts by domain,
    one model per mapping, per-folder property files::

        models/staging/<system>/stg_<system>__<entity>.sql
        models/intermediate/<domain>/int_<entity>_<verb>.sql
        models/marts/<area>/dim_<entity>.sql | fct_<process>.sql
        snapshots/snap_<entity>.sql

``layered``
    What MetaBridge emitted before: flat folders, ``stg_<base>`` /
    ``int_<base>``, and a leaf mapping decomposed into an ``int_`` view plus a
    thin mart that selects * from it. Kept so a customer with a generated
    project already deployed keeps resolving to the same relations while they
    migrate.

Two decisions changed with ``standard``, and both were conceptual rather than
cosmetic:

*Layer comes from evidence, not from what the legacy tool called the mapping.*
``layered`` read the layer off the mapping's own prefix, so a PowerCenter
mapping named ``m_stg_customers`` was filed as staging however much business
logic it carried — one real case was an incremental-merge model with a CASE
decode sitting in ``staging/``. It also demoted every non-leaf target table to
intermediate, which stranded real fact tables out of ``marts/``. Marts are
allowed to ref other marts.

*One mapping is one model.* The ``int_`` + thin-mart split materialised an
extra relation for nothing, left the mart a ``select *`` shell, and forced
downstream consumers (the round-trip validator especially) to guess which of
the two carried the real column list.
"""
from __future__ import annotations

import re
from typing import Optional, Dict, List, Tuple

from ..ir.model import (LoadStrategy, Mapping, Pipeline, SourceTable,
                        TransformationType)

LAYOUTS = ("standard", "layered")
DEFAULT_LAYOUT = "standard"

_LEAD = re.compile(r"^(m|ld|load|map|mapping|wf|s|sq)_", re.IGNORECASE)
_PHYS = re.compile(r"^(src|raw|stg|tgt|t|tbl)_", re.IGNORECASE)
_TAIL = re.compile(r"_(dim|dimension|fact|fct|tbl|table|tgt|target)$",
                   re.IGNORECASE)

# A target whose NAME says it is scratch: a model that exists to break up
# logic, not one anybody consumes. These belong in intermediate however the
# graph is shaped.
_WORK_TABLE = re.compile(
    r"(^(tmp|temp|wrk|work|scratch)_)|((_tmp|_temp|_wrk|_work|_stage|_staging)$)",
    re.IGNORECASE)

# A target whose NAME declares it a curated table. `is_dimension` covers the
# dim side (including the SCD CIR signals and the `d_` prefix); this adds the
# fact side, the suffix forms, and the `tgt_` convention — a table the estate
# calls the TARGET is the curated output, not a landing table. `base_name` and
# `_TAIL` already treat tgt_/_target as physical decoration, so the convention
# is recognised consistently across this module.
_MART_TARGET = re.compile(
    r"(^(dim|dimension|fct|fact|tgt|target)_)"
    r"|((_dim|_dimension|_fact|_fct|_tgt|_target)$)",
    re.IGNORECASE)

# ...and one whose name declares it a landing table.
_STAGING_TARGET = re.compile(
    r"(^(stg|staging|src|raw|land|landing|lnd)_)|((_stg|_staging|_raw)$)",
    re.IGNORECASE)

# Nodes that RESHAPE rows. A staging model renames, casts and cleans; the
# moment it joins, aggregates, ranks, unions, routes or looks up, it is doing
# business logic and is not staging any more.
_RESHAPING = frozenset({
    TransformationType.JOINER, TransformationType.AGGREGATOR,
    TransformationType.LOOKUP, TransformationType.RANK,
    TransformationType.UNION, TransformationType.ROUTER,
})

# Verb for an intermediate model, in precedence order. dbt's convention names
# an intermediate model after what it DOES (int_payments_pivoted_to_orders);
# `int_<base>` alone said nothing, so two intermediates over the same entity
# were indistinguishable.
_VERBS = (
    (TransformationType.JOINER, "joined"),
    (TransformationType.AGGREGATOR, "aggregated"),
    (TransformationType.RANK, "ranked"),
    (TransformationType.UNION, "unioned"),
    (TransformationType.ROUTER, "routed"),
    (TransformationType.LOOKUP, "enriched"),
    (TransformationType.FILTER, "filtered"),
)
_MAX_VERBS = 2

_CONVENTION = {"stg_": "staging", "int_": "intermediate",
               "dim_": "marts", "fct_": "marts"}


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def _slug(name: str) -> str:
    return _safe(str(name or "")).strip("_").lower()


def base_name(raw: str) -> str:
    """Deterministic entity base from a mapping or table name."""
    n = _safe(str(raw)).lower().strip("_")
    for rx in (_LEAD, _LEAD):           # strip up to two lead prefixes
        n = rx.sub("", n)
    n = _PHYS.sub("", n)
    n = _TAIL.sub("", n)
    return n or _safe(str(raw)).lower()


def source_system(s: SourceTable, layout: str = DEFAULT_LAYOUT) -> str:
    """The dbt source name for a SourceTable.

    ``standard`` groups by source SYSTEM, which is also what keeps two
    same-named schemas in different databases apart: RETAIL_DB.RAW and
    SRC_DB.raw both reduced to "RAW"/"raw" and collided on case alone, giving
    one project two sources whose names differed only in capitalisation.

    ``system`` is set by the parsers that actually know it; the database is the
    next best proxy, and the schema the last.
    """
    if layout == "layered":
        return s.schema or "raw"
    return _slug(getattr(s, "system", "") or s.database or s.schema or "raw")


# ---------------------------------------------------------------------------
# Per-mapping classification
# ---------------------------------------------------------------------------

def _target_table(m: Mapping) -> str:
    tgts = m.by_type(TransformationType.TARGET)
    return str(tgts[0].properties.get("table", "")) if tgts else ""


def _source_tables(m: Mapping) -> List[str]:
    return [str(t.properties.get("table", "") or "").lower()
            for t in m.by_type(TransformationType.SOURCE)
            if str(t.properties.get("table", "") or "").strip()]


def built_tables(pipeline: Pipeline) -> Dict[str, str]:
    """Lowercased target table -> the mapping that builds it.

    A table this project builds is not a raw source, whatever the IR also
    happens to record it as.
    """
    out: Dict[str, str] = {}
    for m in pipeline.mappings:
        table = _target_table(m).lower()
        if table:
            out.setdefault(table, m.name)
    return out


def _schema_of(t) -> str:
    return _slug(t.properties.get("schema", "")) if t is not None else ""


def relation_alias(m: Mapping, shared: Optional[Dict[str, int]] = None,
                   model_name: str = "",
                   landed: Optional[Dict[str, str]] = None) -> str:
    """The name the RELATION keeps when it differs from the model's.

    A dbt model called fct_analysis builds a relation called FCT_ANALYSIS,
    and everything downstream that reads the legacy GOLD_SCHEMA.ANALYSIS
    stops finding it. `alias` decouples the two: the project keeps dbt naming
    conventions in its own files, and the warehouse keeps the name the estate
    already uses, so consumers migrate on their own schedule instead of all
    at once on cutover day.

    Three cases get no alias, each because the legacy name is not actually
    available to take:

      * a target this generator INVENTED (the scaffold's stg_customer) —
        there is no legacy relation of that name to be compatible with, and
        aliasing to the table it lands would collide with the landed table
      * a target several mappings build — one relation cannot take the name
        for all of them, and silently giving it to whichever sorted first is
        worse than not aliasing
      * a name that would need quoting, since dbt emits an alias as a bare
        identifier on the platforms that fold case
      * a name a LANDED table already occupies. Landing a table the project
        also builds is the parallel-run setup — you copy the legacy table
        across so you can diff it against the rebuilt one. Under the alias
        those two are the same relation, and the model is materialized as a
        table, so the first `dbt run` issues CREATE OR REPLACE and destroys
        the copy the comparison exists to use. The model keeps its own name
        until the landed copy goes away, which is the cutover.

    ``landed`` is lowercased table name -> the schema it lands in.
    """
    target = _target_table(m)
    if not target:
        return ""
    tgts = m.by_type(TransformationType.TARGET)
    if tgts and tgts[0].properties.get("landed_from"):
        return ""
    if (shared or {}).get(target.lower(), 1) > 1:
        return ""
    if target == model_name:
        return ""
    from ..sqlx.identifiers import needs_quoting
    if needs_quoting(target):
        return ""
    here = (landed or {}).get(target.lower())
    if here is not None and here.lower() == (target_schema(m) or "").lower():
        return ""
    return target


def target_schema(m: Mapping) -> str:
    """The schema the estate declares this mapping's target lives in.

    Un-slugged, because this one is not a folder name: it is the physical
    schema the model has to be BUILT into, and SILVER_SCHEMA is not
    silver_schema on a case-folding warehouse.
    """
    tgts = m.by_type(TransformationType.TARGET)
    return str(tgts[0].properties.get("schema", "") or "").strip() if tgts else ""


def _crosses_schema(m: Mapping) -> bool:
    """Whether this mapping writes into a different schema than it reads.

    The estate's schemas ARE its layering — RAW_SCHEMA / SILVER_SCHEMA /
    GOLD_SCHEMA say what each table is for. A mapping that crosses one is
    promoting data between layers, which is business logic by definition.

    Only decided when both ends declare a schema; a landing model whose target
    carries no schema is not "crossing" anything.
    """
    tgts = m.by_type(TransformationType.TARGET)
    target = _schema_of(tgts[0] if tgts else None)
    if not target:
        return False
    sources = {_schema_of(t) for t in m.by_type(TransformationType.SOURCE)}
    sources.discard("")
    return bool(sources) and target not in sources


def shared_targets(pipeline: Pipeline) -> Dict[str, int]:
    """Lowercased target table -> how many mappings build it.

    More than one is normal in a real estate: a stored procedure decomposed
    into statements, or an ETL mapping and a procedure that both maintain the
    same table.
    """
    counts: Dict[str, int] = {}
    for m in pipeline.mappings:
        table = _target_table(m).lower()
        if table:
            counts[table] = counts.get(table, 0) + 1
    return counts


def entity_of(m: Mapping, shared: Optional[Dict[str, int]] = None) -> str:
    """What this model is named after.

    Normally the TARGET table: it is the artifact the mapping produces and it
    reads far better than an ETL job name.

    But a target only IDENTIFIES a mapping while one mapping builds it. When
    several do, every one of them claims the same name and they end up told
    apart by a meaningless numeric suffix — and worse, the conversion report
    quotes the CIR name the parser gave them ("the model from
    prc_load_slv_orders is named 'slv_orders__prc_load_slv_orders' instead"),
    so the artifact and the report disagree about what exists. The parser
    already made those names unique for exactly this reason; use them.
    """
    target = _target_table(m)
    if target and (shared or {}).get(target.lower(), 1) < 2:
        return target
    return m.name


def is_dimension(m: Mapping) -> bool:
    if "scd1_cir" in m.properties or "scd2_cir" in m.properties:
        return True
    t = _target_table(m).lower()
    return t.startswith(("dim_", "d_")) or t.endswith("_dim")


def is_snapshot(m: Mapping) -> bool:
    """A mapping emitted as a {% snapshot %} block rather than a model."""
    if m.load_strategy != LoadStrategy.SCD2:
        return False
    scd2 = m.properties.get("scd2_cir") or {}
    return scd2.get("dbt_strategy") != "incremental_scd2"


def _verbs(m: Mapping) -> str:
    kinds = {t.type for t in m.transformations}
    found = [word for ttype, word in _VERBS if ttype in kinds]
    if TransformationType.AGGREGATOR in kinds and not any(
            p.expression for t in m.by_type(TransformationType.AGGREGATOR)
            for p in t.ports):
        # group-by with no aggregate function is Informatica's keep-one-row
        # idiom, which the generator converts to a dedup — not an aggregation
        found = ["deduplicated" if w == "aggregated" else w for w in found]
    return "_".join(found[:_MAX_VERBS]) or "prepared"


def classify(m: Mapping, pipeline: Pipeline,
             built: Dict[str, str]) -> str:
    """The dbt layer this mapping belongs in, from what it actually does.

    The TARGET table's name is consulted first, and only the target's. That is
    the physical artifact the business named, so when it says `dim_` or `_fact`
    it is the estate declaring this a curated table, and that outranks how
    simple the logic happens to be — a dimension loaded by one cleansing
    projection is still a dimension, not a staging view.

    What is deliberately NOT consulted is the MAPPING's name. `m_stg_customers`
    was named by Informatica, and reading the dbt layer off that prefix is what
    filed an incremental-merge model with a CASE decode under staging/.
    """
    target = _target_table(m)
    if target and (_MART_TARGET.search(target) or is_dimension(m)):
        return "marts"
    srcs = _source_tables(m)
    known = {s.name.lower() for s in pipeline.sources}
    reads_only_raw = bool(srcs) and all(
        s in known and built.get(s, m.name) == m.name for s in srcs)
    if target and _STAGING_TARGET.search(target):
        # The estate named the target a landing table, and that is trusted the
        # same way `dim_`/`fct_` is trusted above. An incremental landing table
        # is still a landing table, so this also wins over the load strategy.
        #
        # Deliberately NOT also requiring that it reads only raw sources: when
        # a table is landed AND rebuilt on purpose — to run the two sides in
        # parallel and compare them — the landed copy's source is a table
        # another mapping builds, and demanding raw-only filed that copy as a
        # mart competing with the real one.
        return "staging"
    # A mapping whose target sits in a DIFFERENT schema from its sources is
    # moving data between layers of the estate — RAW_SCHEMA.CUSTOMER into
    # SILVER_SCHEMA.FINAL_CUSTOMER is a silver build, not a landing copy, no
    # matter how simple the logic. Without this, a one-source full-load
    # mapping was filed as staging and the estate's own layering — the thing
    # the schemas exist to express — was thrown away.
    if _crosses_schema(m):
        return "marts" if target and not _WORK_TABLE.search(target) \
            else "intermediate"
    reshapes = any(t.type in _RESHAPING for t in m.transformations)
    routes = any(t.type == TransformationType.UPDATE_STRATEGY
                 for t in m.transformations)
    # The DD_REJECT exception dataset is a sibling mapping we derived from a
    # curated load. Structurally it is "read one source, filter, write" and so
    # looks exactly like staging, but it belongs beside the table it protects.
    derived = any(t.properties.get("synthesized_from") == "dd_reject"
                  for t in m.transformations)
    simple_load = m.load_strategy in (LoadStrategy.FULL, LoadStrategy.VIEW)
    if len(srcs) == 1 and reads_only_raw and not reshapes and not routes \
            and not derived and simple_load:
        # one raw source in, rename/cast/clean, nothing joined or aggregated.
        # An update strategy or an incremental/upsert load means this mapping
        # maintains a curated table rather than mirroring a source — a MERGE
        # driven by DD_UPDATE/DD_DELETE flags is not a staging view.
        return "staging"
    if not target or _WORK_TABLE.search(target) \
            or m.load_strategy == LoadStrategy.EPHEMERAL:
        # nothing exposed, or a name that says scratch
        return "intermediate"
    return "marts"


def group_of(m: Mapping, pipeline: Pipeline, layer: str) -> str:
    """The subfolder: source system for staging, business domain otherwise."""
    if layer == "staging":
        srcs = _source_tables(m)
        for s in pipeline.sources:
            if s.name.lower() in srcs:
                return source_system(s)
        return "raw"
    # PowerCenter folder / IDMC project / ETL project is the estate's own
    # statement of which business area this belongs to. Falling back to the
    # target's schema catches curated-layer conventions (SILVER, FINANCE).
    folder = _slug(m.properties.get("folder", ""))
    if folder:
        return folder
    tgts = m.by_type(TransformationType.TARGET)
    schema = _slug(tgts[0].properties.get("schema", "")) if tgts else ""
    return schema or "core"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def stg_name(table: str, system: str = "",
             layout: str = DEFAULT_LAYOUT) -> str:
    if layout == "layered" or not system:
        return "stg_%s" % base_name(table)
    return "stg_%s__%s" % (system, base_name(table))


def int_name(m: Mapping, layout: str = DEFAULT_LAYOUT,
             entity: str = "") -> str:
    if layout == "layered":
        return "int_%s" % base_name(m.name)
    entity = entity or _target_table(m) or m.name
    # An intermediate model's target is often a work table (revenue_tmp), and
    # carrying that marker into the name is noise: `int_` already says this is
    # a helper, so `int_revenue_tmp_joined` says it twice.
    base = _WORK_TABLE.sub("", base_name(entity)).strip("_")
    return "int_%s_%s" % (base or "model", _verbs(m))


def mart_name(m: Mapping, entity: str = "") -> str:
    """dim_/fct_ mart name; a target that already follows the convention is
    kept verbatim."""
    entity = entity or _target_table(m) or m.name
    t = _slug(entity)
    if t.startswith(("dim_", "fct_")):
        return t
    return ("dim_%s" if is_dimension(m) else "fct_%s") % base_name(entity)


def snapshot_name(m: Mapping, layout: str = DEFAULT_LAYOUT,
                  entity: str = "") -> str:
    if layout == "layered":
        return _safe(m.name)
    entity = entity or _target_table(m) or m.name
    # a snapshot OF a dimension is `snap_customer`, not `snap_dim_customer` —
    # the snap_ prefix already says what kind of object this is
    base = re.sub(r"^(dim|dimension|fct|fact)_", "",
                  base_name(entity), flags=re.IGNORECASE)
    return "snap_%s" % (base or "entity")


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def plan_names(pipeline: Pipeline, layout: str = DEFAULT_LAYOUT
               ) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """-> (per-mapping plan, per-source-table staging plan).

    Every mapping entry carries::

        layer     staging | intermediate | marts
        group     subfolder under that layer ("" for the layered layout)
        kind      "model" | "snapshot"
        ref       the node name a DOWNSTREAM model must ref()
        schema    the schema the estate builds this target in ("" if none)
        alias     the relation name to keep, when it differs from the model
        targets   what to write, in order; the last one is `ref`
        int/mart  kept for callers that predate `targets`

    Each ``targets`` entry is ``{"name", "role", "dir", "body", "columns"}``
    where ``body`` is "logic" (the rendered graph) or "passthrough" (select *
    from the previous target). ``standard`` always emits exactly one.

    Model names are unique across the WHOLE project, not per folder: dbt
    requires globally unique node names regardless of where the file sits.
    """
    layout = layout if layout in LAYOUTS else DEFAULT_LAYOUT
    built = built_tables(pipeline)
    shared = shared_targets(pipeline)
    # what the landing layer will physically create, so an alias cannot be
    # handed a name that is already taken
    landed = {s.name.lower(): str(s.schema or "") for s in pipeline.sources}
    trust_prefix = (pipeline.source_format or "").lower() == "dbt"
    taken: set = set()

    def claim(name: str) -> str:
        out, i = name, 2
        while out in taken:
            out = "%s_%d" % (name, i)
            i += 1
        taken.add(out)
        return out

    def sub(layer: str, group: str) -> str:
        return "%s/%s" % (layer, group) if group else layer

    # `columns` says whether a model's property entry documents its column
    # list. Only marts did, which left a staging model's columns undocumented
    # and — once tests are installed against one of them — a `columns:` entry
    # naming a column with no type beside marts that carry types for all of
    # them. The legacy layered layout keeps its old, narrower behaviour.
    plan: Dict[str, dict] = {}
    for m in sorted(pipeline.mappings, key=lambda x: x.name.lower()):
        n = _slug(m.name)
        # Only a dbt project's OWN naming is authoritative about the layer.
        # A PowerCenter mapping called m_stg_customers was named by Informatica,
        # and reading the dbt layer off that prefix is what put business logic
        # in staging/.
        kept = next((lyr for pfx, lyr in _CONVENTION.items()
                     if n.startswith(pfx)), None) if trust_prefix else None
        if kept:
            layer, group = kept, ""
        else:
            layer = classify(m, pipeline, built)
            group = "" if layout == "layered" else group_of(m, pipeline, layer)

        entity = entity_of(m, shared)
        schema = target_schema(m)
        if is_snapshot(m):
            # A dbt project's own snapshot keeps its name, for the same reason
            # a conventionally-named model does: a round trip has to be
            # idempotent, and renaming customers_snapshot to snap_customers
            # would point every existing ref at a new relation.
            name = claim(_safe(m.name) if trust_prefix
                         else snapshot_name(m, layout, entity))
            plan[m.name] = {"layer": layer, "group": group, "kind": "snapshot",
                            "ref": name, "int": name, "mart": "",
                            "schema": schema, "alias": "",
                            "targets": [{"name": name, "role": "snapshot",
                                         "dir": "snapshots",
                                         "body": "logic", "columns": False}]}
            continue

        if kept:
            name = claim(n)
            targets = [{"name": name, "role": "transformation_logic",
                        "dir": sub(layer, group), "body": "logic",
                        "columns": layout != "layered" or layer == "marts"}]
        elif layout == "layered" and layer == "marts":
            # the historical decomposition: logic in an int_ view, plus a thin
            # mart carrying the real config
            logic = claim(int_name(m, layout, entity))
            mart = claim(mart_name(m, entity))
            targets = [{"name": logic, "role": "transformation_logic",
                        "dir": "intermediate", "body": "logic",
                        "columns": False},
                       {"name": mart, "role": "mart", "dir": "marts",
                        "body": "passthrough", "columns": True}]
        else:
            if layer == "marts":
                name = claim(mart_name(m, entity))
                role = "mart"
            elif layer == "staging":
                # named for what it LANDS (see entity_of), falling back to the
                # source table only when the mapping declares no target at all
                srcs = _source_tables(m)
                table = entity or (srcs[0] if srcs else m.name)
                name = claim(stg_name(table, group, layout))
                role = "staging"
            else:
                name = claim(int_name(m, layout, entity))
                role = "transformation_logic"
            targets = [{"name": name, "role": role, "dir": sub(layer, group),
                        "body": "logic", "columns": layout != "layered" or layer == "marts"}]

        plan[m.name] = {
            "layer": layer, "group": group, "kind": "model",
            "ref": targets[-1]["name"], "targets": targets,
            "schema": schema,
            "alias": relation_alias(m, shared, targets[-1]["name"], landed),
            # the name the landing layer is holding, so whoever reads the
            # issue is told which relation is in the way
            "alias_blocked_by_landing":
                relation_alias(m, shared, targets[-1]["name"])
                if not relation_alias(m, shared, targets[-1]["name"], landed)
                else "",
            "int": targets[0]["name"],
            "mart": targets[-1]["name"] if len(targets) > 1 else "",
        }

    # ------------------------------------------------------------------
    # Staging models for RAW source tables. Two exclusions, both about not
    # emitting a model that duplicates or contradicts one we already have:
    #   * a table this project BUILDS is not a raw source. Staging it produced
    #     an orphan model reading a relation the project overwrites.
    #   * a table a staging-layer MAPPING already reads is already staged by
    #     that mapping; matching on the source table rather than on the
    #     generated name is what settles it, since a mapping may keep its own
    #     spelling.
    # ------------------------------------------------------------------
    staged: set = set()
    for m in pipeline.mappings:
        if plan[m.name]["layer"] == "staging":
            staged.update(_source_tables(m))

    stg: Dict[str, dict] = {}
    for s in sorted(pipeline.sources, key=lambda x: x.name.lower()):
        key = s.name.lower()
        if key in staged or key in built:
            continue
        system = source_system(s, layout)
        group = "" if layout == "layered" else system
        name = stg_name(s.name, group, layout)
        if name in taken:
            continue
        stg[key] = {"name": claim(name), "dir": sub("staging", group),
                    "system": system, "group": group}
    return plan, stg

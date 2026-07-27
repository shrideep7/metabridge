"""CIR -> IR reverse converter.

Target generators receive the CIR (module 8 contract). Internally the proven
generation machinery runs on the working graph IR, so this module rebuilds an
IR pipeline from a CIR project. Fidelity notes:

  * transformation ``metadata`` carries the original IR properties verbatim,
    so conditions, group-bys, overrides, rank/router settings all survive;
  * expressions come back from ``Expression.canonical_sql``;
  * graph edges come from ``inputs``/``outputs`` id lists;
  * load semantics come back from pipeline ``load_strategy``.

Round-trip (IR -> CIR -> IR -> generate) is pinned by tests to produce the
same generated artifacts as direct generation.
"""
from __future__ import annotations

from typing import Dict

from ..ir.model import (
    Link, LoadStrategy, Mapping, Pipeline as IrPipeline, Port, SourceTable,
    Transformation, TransformationType as IrT,
)
from . import model as cir

_REVERSE_TYPE = {
    cir.CirTransformationType.SOURCE: IrT.SOURCE,
    cir.CirTransformationType.EXPRESSION: IrT.EXPRESSION,
    cir.CirTransformationType.FILTER: IrT.FILTER,
    cir.CirTransformationType.JOIN: IrT.JOINER,
    cir.CirTransformationType.AGGREGATOR: IrT.AGGREGATOR,
    cir.CirTransformationType.SORTER: IrT.SORTER,
    cir.CirTransformationType.UNION: IrT.UNION,
    cir.CirTransformationType.LOOKUP: IrT.LOOKUP,
    cir.CirTransformationType.ROUTER: IrT.ROUTER,
    cir.CirTransformationType.WINDOW: IrT.RANK,       # rank props in metadata
    cir.CirTransformationType.RANK: IrT.RANK,
    cir.CirTransformationType.SEQUENCE: IrT.SEQUENCE,
    cir.CirTransformationType.SQL: IrT.SOURCE_QUALIFIER,
    cir.CirTransformationType.MERGE: IrT.TARGET,
    cir.CirTransformationType.INCREMENTAL: IrT.TARGET,
    cir.CirTransformationType.SCD_TYPE_2: IrT.TARGET,
    cir.CirTransformationType.SCD_TYPE_1: IrT.TARGET,
    cir.CirTransformationType.TARGET: IrT.TARGET,
}


def cir_to_ir(project: cir.Project) -> IrPipeline:
    pipeline = IrPipeline(name=project.name,
                          source_format=project.source_platform)
    dialect = ""
    if project.runtime is not None:
        dialect = project.runtime.dialect
    pipeline.metadata["dialect"] = dialect or \
        str((project.metadata or {}).get("dialect", ""))

    dataset_by_name = {d.name.lower(): d for d in project.datasets}
    for d in project.datasets:
        if isinstance(d, cir.Table) or d.kind == "table":
            pipeline.sources.append(SourceTable(
                name=d.name, schema=d.schema, database=d.database,
                columns=[Port(name=c.name, datatype=c.data_type,
                              precision=c.precision, scale=c.scale)
                         for c in d.columns]))

    target_names = {p.target_dataset for p in project.pipelines}
    pipeline.sources = [s for s in pipeline.sources
                        if s.name not in target_names]

    for pl in project.pipelines:
        pipeline.mappings.append(_mapping(pl, dataset_by_name))
    return pipeline


def _mapping(pl: cir.Pipeline, datasets: Dict[str, cir.Dataset]) -> Mapping:
    m = Mapping(name=pl.name, depends_on=list(pl.depends_on),
                unique_key=list(pl.unique_key))
    try:
        m.load_strategy = LoadStrategy(pl.load_strategy)
    except ValueError:
        m.load_strategy = LoadStrategy.FULL
    scd = next((n for n in pl.conversion_notes if "scd" in n.lower()), "")
    if m.load_strategy == LoadStrategy.SCD2 or scd:
        m.properties.setdefault("scd", {})

    id_to_name = {t.id: t.name for t in pl.transformations}
    target_tx = None
    for t in pl.transformations:
        ir_type = _REVERSE_TYPE.get(t.transformation_type, IrT.EXPRESSION)
        props = dict(t.metadata or {})
        # The builder folds SOURCE and SOURCE_QUALIFIER into CIR SOURCE;
        # metadata disambiguates: physical sources carry 'table', qualifiers
        # carry 'source' (and SQL overrides became CIR SQL nodes already).
        if t.transformation_type == cir.CirTransformationType.SOURCE \
                and "table" not in props:
            ir_type = IrT.SOURCE_QUALIFIER
        # SCD config rides on the pipeline in CIR
        if ir_type == IrT.TARGET and "scd" in (props or {}):
            m.properties["scd"] = props["scd"]
        tx = Transformation(
            name=t.name, type=ir_type,
            ports=[Port(name=c.name, datatype=c.data_type,
                        precision=c.precision, scale=c.scale)
                   for c in t.output_columns],
            properties=props)
        # restore port expressions from the semantic layer
        for e in t.expressions:
            if e.output_column == "(sql_override)":
                tx.properties["sql_override"] = e.canonical_sql
                continue
            port = tx.port(e.output_column)
            if port is not None:
                port.expression = e.canonical_sql
        m.transformations.append(tx)
        if ir_type == IrT.TARGET:
            target_tx = tx
        for up_id in t.inputs:
            up = id_to_name.get(up_id)
            if up:
                m.links.append(Link(up, t.name))

    # rebuild the virtual __OUTPUT__ marker in front of the target
    if target_tx is not None:
        ups = m.upstream_of(target_tx.name)
        if ups:
            up = ups[0]
            m.transformations.append(Transformation(
                name="__OUTPUT__", type=IrT.EXPRESSION,
                ports=[Port(name=p.name, datatype=p.datatype)
                       for p in up.ports],
                properties={"virtual": True, "upstream": up.name}))
            m.links = [l for l in m.links
                       if not (l.from_transformation == up.name and
                               l.to_transformation == target_tx.name)]
            m.links.append(Link(up.name, "__OUTPUT__"))
            m.links.append(Link("__OUTPUT__", target_tx.name))
    return m

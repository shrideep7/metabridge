"""Elevate the working graph IR into the Canonical Intermediate Representation.

``build_cir(ir_pipeline)`` works for anything any of the 13 parsers produced —
dbt, PowerCenter, IDMC, or warehouse SQL — because it reads only the canonical
IR. It adds what integrations need and the IR doesn't carry:

  * stable deterministic IDs (diffable across runs)
  * semantic expressions (function_type + arguments, renderable per target)
  * typed business rules, column-level inputs/outputs, dataset lineage
  * per-transformation and per-pipeline confidence scores
  * load semantics expressed as transformation types (MERGE / INCREMENTAL)
  * data-quality rules, parameters, connections, stored procedures, workflow
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..ir.model import (
    IssueSeverity, LoadStrategy, Mapping, Pipeline as IrPipeline,
    TransformationType as IrT,
)
from . import model as cir
from .semantic import SemanticFunction, parse_expression

_TYPE_MAP = {
    IrT.SOURCE: cir.CirTransformationType.SOURCE,
    IrT.SOURCE_QUALIFIER: cir.CirTransformationType.SOURCE,
    IrT.EXPRESSION: cir.CirTransformationType.EXPRESSION,
    IrT.FILTER: cir.CirTransformationType.FILTER,
    IrT.JOINER: cir.CirTransformationType.JOIN,
    IrT.AGGREGATOR: cir.CirTransformationType.AGGREGATOR,
    IrT.SORTER: cir.CirTransformationType.SORTER,
    IrT.UNION: cir.CirTransformationType.UNION,
    IrT.LOOKUP: cir.CirTransformationType.LOOKUP,
    IrT.ROUTER: cir.CirTransformationType.ROUTER,
    IrT.RANK: cir.CirTransformationType.WINDOW,  # spec: Rank -> WINDOW
    IrT.SEQUENCE: cir.CirTransformationType.SEQUENCE,
    IrT.UPDATE_STRATEGY: cir.CirTransformationType.MERGE,
    IrT.TARGET: cir.CirTransformationType.TARGET,
}

_LOAD_TO_TYPE = {
    LoadStrategy.MERGE: cir.CirTransformationType.MERGE,
    LoadStrategy.DELETE_INSERT: cir.CirTransformationType.MERGE,
    LoadStrategy.APPEND: cir.CirTransformationType.INCREMENTAL,
    LoadStrategy.SCD2: cir.CirTransformationType.SCD_TYPE_2,
}

_PARAM_RE = re.compile(r"\$\$(\w+)")

_PROCEDURE_HINT = re.compile(r"\b(PROCEDURE|FUNCTION|CALL|BEGIN|TASK)\b",
                             re.IGNORECASE)


def build_cir(pipeline: IrPipeline,
              source_platform: str = "") -> cir.Project:
    platform = source_platform or pipeline.source_format or "unknown"
    dialect = str(pipeline.metadata.get("dialect", ""))
    proj_id = cir.make_id("prj", platform, pipeline.name)

    project = cir.Project(
        id=proj_id, name=pipeline.name, source_platform=platform,
        metadata={"inventory": pipeline.metadata.get("inventory"),
                  "dialect": dialect})

    params: Dict[str, cir.Parameter] = {}
    connections: Dict[str, cir.Connection] = {}
    dataset_ids: Dict[str, str] = {}

    # ---- datasets from project-level sources -------------------------------
    for s in pipeline.sources:
        did = cir.make_id("ds", platform, s.schema, s.name)
        dataset_ids[s.name.lower()] = did
        project.datasets.append(cir.Table(
            id=did, name=s.name, schema=s.schema, database=s.database,
            system=s.system,
            columns=[_column(did, c.name, c.datatype, c.precision, c.scale)
                     for c in s.columns]))

    # ---- pipelines ----------------------------------------------------------
    for m in pipeline.mappings:
        project.pipelines.append(
            _build_pipeline(m, platform, dialect, project, params,
                            connections, dataset_ids))

    # ---- workflow from the DAG ---------------------------------------------
    waves = pipeline.execution_order()
    tasks = []
    name_to_task = {}
    for m in pipeline.mappings:
        tid = cir.make_id("tsk", platform, pipeline.name, m.name)
        name_to_task[m.name] = tid
        tasks.append(cir.Task(id=tid, name=m.name, task_type="pipeline",
                              pipeline_id=cir.make_id("pl", platform, m.name)))
    for t, m in zip(tasks, pipeline.mappings):
        t.depends_on = [name_to_task[d] for d in m.depends_on
                        if d in name_to_task]
    project.workflows.append(cir.Workflow(
        id=cir.make_id("wf", platform, pipeline.name),
        name="wf_%s" % pipeline.name, tasks=tasks, execution_waves=waves))

    # ---- project-level issues: procedures + tests + notes ------------------
    for i in pipeline.issues:
        if i.code in ("STATEMENT_UNSUPPORTED", "STATEMENT_PARSE_FAILED") \
                and i.detail:
            if _PROCEDURE_HINT.search(i.detail[:200]):
                project.stored_procedures.append(cir.StoredProcedure(
                    id=cir.make_id("sp", platform, i.detail[:80]),
                    name=(i.obj or "procedure"), source_platform=platform,
                    code=i.detail,
                    conversion_notes=[i.message, i.suggestion or ""]))
            else:
                project.assets.append(cir.Asset(
                    id=cir.make_id("as", platform, i.detail[:80]),
                    name=i.obj or "statement", asset_type="script",
                    source_platform=platform, content=i.detail,
                    conversion_notes=[i.message]))

    _extract_dq_rules(pipeline, platform, project)

    project.connections = list(connections.values())
    project.runtime = cir.RuntimeConfiguration(
        id=cir.make_id("rt", platform, pipeline.name),
        dialect=dialect, parameters=list(params.values()))

    # ---- cross-pipeline data dependencies ----------------------------------
    for m in pipeline.mappings:
        for dep in m.depends_on:
            project.dependencies.append(cir.Dependency(
                id=cir.make_id("dep", m.name, dep),
                from_id=cir.make_id("pl", platform, dep),
                to_id=cir.make_id("pl", platform, m.name),
                dependency_type="data"))
    return project


# ---------------------------------------------------------------------------

def _column(parent_id: str, name: str, dtype: str = "string",
            precision: int = 0, scale: int = 0) -> cir.Column:
    return cir.Column(id=cir.make_id("col", parent_id, name), name=name,
                      data_type=dtype, precision=precision, scale=scale)


def _confidence_for(m: Mapping, tx_name: str, base: float) -> float:
    """Down-weight transformations with unresolved conversion findings."""
    conf = base
    for i in m.issues:
        target = (i.detail or "") + (i.message or "")
        if i.severity == IssueSeverity.MANUAL and tx_name in target:
            conf -= 0.25
        elif i.code == "SQL_OVERRIDE_FALLBACK" and tx_name.startswith("SQ_"):
            conf -= 0.35
    return round(max(0.1, min(1.0, conf)), 2)


def _build_pipeline(m: Mapping, platform: str, dialect: str,
                    project: cir.Project, params: Dict[str, cir.Parameter],
                    connections: Dict[str, cir.Connection],
                    dataset_ids: Dict[str, str]) -> cir.Pipeline:
    pl_id = cir.make_id("pl", platform, m.name)
    upstream_of_output = ""
    out_tx = m.transformation("__OUTPUT__")
    if out_tx is not None:
        upstream_of_output = str(out_tx.properties.get("upstream", ""))

    tx_ids: Dict[str, str] = {}
    real_txs = [t for t in m.transformations if t.name != "__OUTPUT__"]
    for t in real_txs:
        tx_ids[t.name] = cir.make_id("tx", platform, m.name, t.name)

    # links with the virtual __OUTPUT__ node resolved away
    edges: List[tuple] = []
    for l in m.links:
        frm = upstream_of_output if l.from_transformation == "__OUTPUT__" \
            else l.from_transformation
        to = l.to_transformation
        if to == "__OUTPUT__" or frm not in tx_ids or to not in tx_ids:
            continue
        edges.append((frm, to))

    source_tables = [str(t.properties.get("table", t.name))
                     for t in m.by_type(IrT.SOURCE)]

    notes = ["[%s] %s: %s" % (i.severity.value, i.code, i.message)
             for i in m.issues if i.severity != IssueSeverity.INFO]
    is_fallback = any(i.code == "SQL_OVERRIDE_FALLBACK" for i in m.issues)
    has_manual = any(i.severity == IssueSeverity.MANUAL for i in m.issues)
    pl_conf = 0.95 if not is_fallback else 0.6
    if has_manual:
        pl_conf = min(pl_conf, 0.5)

    pipeline = cir.Pipeline(
        id=pl_id, name=m.name, source_platform=platform,
        load_strategy=m.load_strategy.value, unique_key=list(m.unique_key),
        target_dataset=next((str(t.properties.get("table", t.name))
                             for t in m.by_type(IrT.TARGET)), ""),
        depends_on=list(m.depends_on), conversion_notes=notes,
        confidence_score=round(pl_conf, 2))

    for t in real_txs:
        cir_type = _TYPE_MAP.get(t.type, cir.CirTransformationType.EXPRESSION)
        override = str(t.properties.get("sql_override", "") or "")
        if t.type == IrT.SOURCE_QUALIFIER and override:
            cir_type = cir.CirTransformationType.SQL
        if t.type == IrT.TARGET:
            cir_type = _LOAD_TO_TYPE.get(m.load_strategy,
                                         cir.CirTransformationType.TARGET)

        tx = cir.Transformation(
            id=tx_ids[t.name], name=t.name, source_platform=platform,
            transformation_type=cir_type,
            inputs=[tx_ids[f] for f, to in edges if to == t.name],
            outputs=[tx_ids[to] for f, to in edges if f == t.name],
            output_columns=[_column(tx_ids[t.name], p.name, p.datatype,
                                    p.precision, p.scale) for p in t.ports],
            metadata={**{k: v for k, v in t.properties.items()
                         if k not in ("virtual",)},
                      **({"scd": m.properties.get("scd")}
                         if t.type == IrT.TARGET and m.properties.get("scd")
                         else {})},
            source_location=m.origin.splitlines()[0][:120] if m.origin else "",
            confidence_score=_confidence_for(m, t.name,
                                             0.6 if cir_type ==
                                             cir.CirTransformationType.SQL
                                             else 0.95),
        )
        # input columns = union of upstream output ports
        seen = set()
        for f, to in edges:
            if to != t.name:
                continue
            up = m.transformation(f)
            if up is not None:
                for p in up.ports:
                    if p.name.lower() not in seen:
                        seen.add(p.name.lower())
                        tx.input_columns.append(p.name)
        # dataset lineage: which physical sources feed this node
        tx.source_lineage = source_tables if t.type in (
            IrT.SOURCE, IrT.SOURCE_QUALIFIER) else \
            [d for d in source_tables]

        # semantic expressions
        for p in t.ports:
            if p.expression:
                sem = parse_expression(p.expression, dialect)
                conf = 0.5 if sem.function_type == SemanticFunction.UNKNOWN else 1.0
                tx.expressions.append(cir.Expression(
                    id=cir.make_id("ex", tx.id, p.name),
                    output_column=p.name, semantic=sem.to_dict(),
                    canonical_sql=sem.raw_sql, source_sql=p.expression,
                    confidence_score=conf))
        if override:
            sem = parse_expression(override, dialect)
            tx.expressions.append(cir.Expression(
                id=cir.make_id("ex", tx.id, "sql_override"),
                output_column="(sql_override)", semantic=sem.to_dict(),
                canonical_sql=override, source_sql=override,
                confidence_score=0.6))
            tx.conversion_notes.append(
                "Converted as an opaque SQL block — native graph was not "
                "provable for this shape.")

        tx.business_rules = _rules_for(t, m, tx.id)
        pipeline.transformations.append(tx)

        # sources register connections + datasets
        if t.type == IrT.SOURCE:
            db = str(t.properties.get("database", "") or "")
            schema = str(t.properties.get("schema", "") or "")
            ckey = (db or "default") + "." + (schema or "default")
            if ckey not in connections:
                connections[ckey] = cir.Connection(
                    id=cir.make_id("cn", platform, ckey), name=ckey,
                    platform=platform, database=db, schema=schema)
            table = str(t.properties.get("table", t.name))
            if table.lower() not in dataset_ids:
                did = cir.make_id("ds", platform, schema, table)
                dataset_ids[table.lower()] = did
                project.datasets.append(cir.Table(
                    id=did, name=table, schema=schema, database=db,
                    columns=[_column(did, p.name, p.datatype) for p in t.ports]))

        if t.type == IrT.TARGET:
            table = str(t.properties.get("table", t.name))
            kind = cir.View if m.load_strategy == LoadStrategy.VIEW else cir.Table
            if table.lower() not in dataset_ids:
                did = cir.make_id("ds", platform, "", table)
                dataset_ids[table.lower()] = did
                project.datasets.append(kind(
                    id=did, name=table,
                    columns=[_column(did, p.name, p.datatype) for p in t.ports]))

    # parameters ($$X) anywhere in the mapping
    for t in real_txs:
        blob = " ".join([str(v) for v in t.properties.values()] +
                        [p.expression for p in t.ports if p.expression])
        for name in _PARAM_RE.findall(blob):
            if name not in params:
                params[name] = cir.Parameter(
                    id=cir.make_id("pr", name), name=name,
                    source_syntax="$$%s" % name)
    return pipeline


def _rules_for(t, m: Mapping, tx_id: str) -> List[object]:
    rules: List[object] = []
    if t.type == IrT.FILTER:
        cond = str(t.properties.get("condition", ""))
        rules.append(cir.Filter(
            id=cir.make_id("ru", tx_id, "filter"), condition=cond,
            is_incremental_watermark="$$" in cond or
            t.name == "FIL_INCREMENTAL"))
    elif t.type == IrT.JOINER:
        rules.append(cir.Join(
            id=cir.make_id("ru", tx_id, "join"),
            join_type=str(t.properties.get("join_type", "INNER")),
            condition=str(t.properties.get("condition", "")),
            left_input=str(t.properties.get("left", "")),
            right_input=str(t.properties.get("right", ""))))
    elif t.type == IrT.AGGREGATOR:
        rules.append(cir.Aggregation(
            id=cir.make_id("ru", tx_id, "agg"),
            group_by=[str(g) for g in t.properties.get("group_by", [])],
            aggregates=[{"column": p.name, "expression": p.expression}
                        for p in t.ports if p.expression]))
    elif t.type == IrT.LOOKUP:
        rules.append(cir.Lookup(
            id=cir.make_id("ru", tx_id, "lookup"),
            table=str(t.properties.get("table", "")),
            condition=str(t.properties.get("condition", ""))))
    elif t.type == IrT.UNION:
        rules.append(cir.Union(
            id=cir.make_id("ru", tx_id, "union"),
            inputs=[str(i) for i in t.properties.get("inputs", [])]))
    elif t.type == IrT.SORTER and t.properties.get("distinct"):
        rules.append(cir.Union(
            id=cir.make_id("ru", tx_id, "distinct"), inputs=[], distinct=True))
    # window functions living inside SQL overrides get surfaced explicitly
    override = str(t.properties.get("sql_override", "") or "")
    if override and re.search(r"\bOVER\s*\(", override, re.IGNORECASE):
        rules.append(cir.WindowFunction(
            id=cir.make_id("ru", tx_id, "window"),
            expression=override[:400]))
    return rules


def _extract_dq_rules(pipeline: IrPipeline, platform: str,
                      project: cir.Project) -> None:
    test_re = re.compile(r"dbt test '([^']+)'")
    col_re = re.compile(r"^(\w+)\((\w+)\)$")
    for m in pipeline.mappings:
        for i in m.issues:
            if i.code != "DBT_TEST":
                continue
            match = test_re.search(i.message)
            spec = match.group(1) if match else i.message
            cmatch = col_re.match(spec)
            rule, column = (cmatch.group(1), cmatch.group(2)) if cmatch \
                else (spec, "")
            project.data_quality_rules.append(cir.DataQualityRule(
                id=cir.make_id("dq", m.name, spec), name=spec,
                dataset=m.name, column=column, rule=rule))
            project.tests.append(cir.Test(
                id=cir.make_id("ts", m.name, spec), name=spec,
                dataset=m.name, definition=spec, origin="dbt test"))

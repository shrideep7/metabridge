"""Generate IDMC (Informatica Intelligent Data Management Cloud) assets from the IR.

Emits one mapping JSON per IR mapping plus a taskflow JSON encoding the DAG,
in a structure aligned with the IDMC REST API v3 object model (mapping
specifications with typed transformation specs and field links). Assets are
designed to be pushed through the IDMC REST API; a manifest.json indexes the
bundle. Expression fields use the Informatica expression language (shared with
PowerCenter), translated from the IR's canonical SQL.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..ir.model import (
    IssueSeverity, LoadStrategy, Mapping, Pipeline, Port, Transformation,
    TransformationType,
)
from ..sqlx.expressions import ExpressionError, sql_to_infa

AssistFn = Optional[Callable[[str, str], Optional[str]]]

_IDMC_TYPE = {
    TransformationType.SOURCE_QUALIFIER: "Source",
    TransformationType.EXPRESSION: "Expression",
    TransformationType.FILTER: "Filter",
    TransformationType.JOINER: "Joiner",
    TransformationType.AGGREGATOR: "Aggregator",
    TransformationType.SORTER: "Sorter",
    TransformationType.UNION: "Union",
    TransformationType.LOOKUP: "Lookup",
    TransformationType.ROUTER: "Router",
    TransformationType.RANK: "Rank",
    TransformationType.SEQUENCE: "Sequence",
    TransformationType.UPDATE_STRATEGY: "UpdateStrategy",
    TransformationType.TARGET: "Target",
}

_IDMC_DATATYPE = {
    "string": "string", "integer": "integer", "bigint": "bigint",
    "decimal": "decimal", "double": "double", "date": "date",
    "timestamp": "datetime", "boolean": "integer", "binary": "binary",
}


def generate_idmc(pipeline: Pipeline, out_dir: str, assist: AssistFn = None) -> None:
    root = Path(out_dir)
    (root / "mappings").mkdir(parents=True, exist_ok=True)
    dialect = str(pipeline.metadata.get("dialect", ""))

    index = []
    for m in pipeline.mappings:
        doc = _mapping_doc(m, dialect, assist)
        fname = "mappings/m_%s.json" % m.name
        (root / fname).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        index.append({"name": "m_" + m.name, "path": fname, "type": "mapping"})

    taskflow = _taskflow_doc(pipeline)
    (root / ("taskflow_%s.json" % _safe(pipeline.name))).write_text(
        json.dumps(taskflow, indent=2), encoding="utf-8")
    index.append({"name": "tf_" + pipeline.name,
                  "path": "taskflow_%s.json" % _safe(pipeline.name), "type": "taskflow"})

    manifest = {
        "bundleType": "metabridge.idmc.bundle", "version": "1.0",
        "project": pipeline.name, "sourceFormat": pipeline.source_format,
        "deployment": {
            "api": "POST /public/core/v3/import (package as zip) or per-object "
                   "POST /disnext/api/v1/mappings",
            "note": "Assets follow the IDMC v3 object model; validate against "
                    "your org's POD before bulk import.",
        },
        "objects": index,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def _field(p: Port) -> dict:
    return {
        "name": p.name,
        "type": _IDMC_DATATYPE.get(p.datatype, "string"),
        "precision": p.precision or (255 if p.datatype == "string" else 10),
        "scale": p.scale,
        "nullable": p.nullable,
    }


def _to_infa(expr_sql: str, m: Mapping, context: str, dialect: str,
             assist: AssistFn) -> Optional[str]:
    try:
        return sql_to_infa(expr_sql, dialect)
    except ExpressionError as e:
        if assist is not None:
            result = assist(expr_sql, context)
            if result:
                m.add_issue(IssueSeverity.WARNING, "LLM_CONVERTED_EXPRESSION",
                            "Expression converted by LLM assist — verify before deploy",
                            detail="%s  =>  %s" % (expr_sql, result))
                m.issues[-1].resolved_by_llm = True
                return result
        m.add_issue(IssueSeverity.MANUAL, "EXPRESSION_UNCONVERTED",
                    "Expression could not be converted (%s)" % context,
                    detail="%s | %s" % (expr_sql, e))
        return None


def _mapping_doc(m: Mapping, dialect: str, assist: AssistFn) -> dict:
    transformations = []
    for t in m.transformations:
        if t.name == "__OUTPUT__":
            continue
        if t.type == TransformationType.SOURCE:
            transformations.append({
                "name": t.name, "type": "Source",
                "connection": {"connectionType": "relational",
                               "database": t.properties.get("database", ""),
                               "schema": t.properties.get("schema", "")},
                "object": t.properties.get("table", t.name),
                "fields": [_field(p) for p in t.ports],
            })
            continue
        spec: Dict[str, object] = {
            "name": t.name, "type": _IDMC_TYPE.get(t.type, "Expression"),
            "fields": [],
        }
        for p in t.ports:
            f = _field(p)
            if p.expression:
                infa = _to_infa(p.expression, m, "%s.%s" % (t.name, p.name),
                                dialect, assist)
                f["fieldType"] = "expression"
                f["expression"] = infa if infa is not None else p.name
                if infa is None:
                    f["review"] = "Original SQL: " + p.expression
            spec["fields"].append(f)

        props = dict(t.properties)
        if t.type == TransformationType.FILTER and props.get("condition"):
            infa = _to_infa(str(props["condition"]), m, "%s filter" % t.name,
                            dialect, assist)
            props["condition"] = infa if infa is not None else \
                "TRUE"  # flagged via issue above
        if t.type == TransformationType.JOINER and props.get("condition"):
            infa = _to_infa(str(props["condition"]), m, "%s join" % t.name,
                            dialect, assist)
            if infa is not None:
                props["condition"] = infa
        if t.type == TransformationType.SOURCE_QUALIFIER and props.get("sql_override"):
            spec["type"] = "Source"
            props["queryMode"] = "customQuery"
        spec["properties"] = props

        if t.type == TransformationType.TARGET:
            spec["operation"] = {
                LoadStrategy.FULL: "truncateAndInsert",
                LoadStrategy.VIEW: "insert",
                LoadStrategy.APPEND: "insert",
                LoadStrategy.MERGE: "upsert",
                LoadStrategy.DELETE_INSERT: "deleteAndInsert",
                LoadStrategy.EPHEMERAL: "insert",
                LoadStrategy.SCD2: "upsert",
            }.get(m.load_strategy, "insert")
            if m.unique_key:
                spec["updateColumns"] = m.unique_key
        transformations.append(spec)

    upstream = ""
    out = m.transformation("__OUTPUT__")
    if out is not None:
        upstream = str(out.properties.get("upstream", ""))
    links = []
    for l in m.links:
        frm = upstream if l.from_transformation == "__OUTPUT__" else l.from_transformation
        to = l.to_transformation
        if to == "__OUTPUT__" or not frm:
            continue
        links.append({"from": frm, "to": to})

    return {
        "@type": "mapping", "name": "m_" + m.name,
        "description": m.description or
        "Converted by MetaBridge AI from %s" % (m.origin.splitlines()[0][:80]
                                             if m.origin else m.name),
        "transformations": transformations,
        "links": links,
        "runtime": {"loadStrategy": m.load_strategy.value,
                    "uniqueKey": m.unique_key},
    }


def _taskflow_doc(pipeline: Pipeline) -> dict:
    steps = []
    for i, wave in enumerate(pipeline.execution_order(), 1):
        steps.append({
            "step": i, "type": "parallelPaths" if len(wave) > 1 else "task",
            "tasks": [{"type": "mappingTask", "mapping": "m_" + n} for n in wave],
        })
    return {"@type": "taskflow", "name": "tf_" + pipeline.name,
            "description": "DAG-equivalent orchestration generated by MetaBridge AI",
            "steps": steps}

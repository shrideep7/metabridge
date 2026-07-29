"""Parse IDMC mapping bundles (MetaBridge AI IDMC JSON structure) into the IR.

Accepts a directory containing manifest.json + mappings/*.json as produced by
the IDMC generator, or any directory of mapping JSON documents that follow the
same object model. Expressions arrive in the Informatica expression language
and are converted to canonical SQL on the way in.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ..ir.model import (
    ConversionIssue, IssueSeverity, Link, LoadStrategy, Mapping, Pipeline,
    Port, SourceTable, Transformation, TransformationType,
)
from ..sqlx.expressions import ExpressionError, infa_to_sql

_IDMC_TO_IR = {
    "Source": TransformationType.SOURCE_QUALIFIER,  # refined below
    "Expression": TransformationType.EXPRESSION,
    "Filter": TransformationType.FILTER,
    "Joiner": TransformationType.JOINER,
    "Aggregator": TransformationType.AGGREGATOR,
    "Sorter": TransformationType.SORTER,
    "Union": TransformationType.UNION,
    "Lookup": TransformationType.LOOKUP,
    "Router": TransformationType.ROUTER,
    "Rank": TransformationType.RANK,
    "Sequence": TransformationType.SEQUENCE,
    "UpdateStrategy": TransformationType.UPDATE_STRATEGY,
    "Target": TransformationType.TARGET,
}

_IDMC_DT_TO_CANONICAL = {
    "string": "string", "text": "string", "integer": "integer", "bigint": "bigint",
    "decimal": "decimal", "double": "double", "date": "date",
    "datetime": "timestamp", "timestamp": "timestamp", "binary": "binary",
}


def parse_idmc(path: str) -> Pipeline:
    p = Path(path)
    docs: List[dict] = []
    name = p.stem
    taskflows: List[dict] = []
    for f in sorted(p.rglob("*.json")) if p.is_dir() else [p]:
        try:
            doc = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        if doc.get("@type") == "mapping" or "transformations" in doc:
            docs.append(doc)
        elif doc.get("@type") == "taskflow":
            taskflows.append(doc)
        elif doc.get("bundleType", "").startswith("metabridge"):
            name = doc.get("project", name)
    if not docs:
        raise FileNotFoundError("No IDMC mapping JSON documents found under %s" % path)

    pipeline = Pipeline(name=name, source_format="idmc")
    for doc in docs:
        pipeline.mappings.append(_parse_mapping(doc, pipeline))
    _apply_taskflow_order(taskflows, pipeline)
    return pipeline


def _port(f: dict) -> Port:
    return Port(name=f.get("name", ""),
                datatype=_IDMC_DT_TO_CANONICAL.get(str(f.get("type", "string")).lower(),
                                                   "string"),
                precision=int(f.get("precision") or 0), scale=int(f.get("scale") or 0),
                nullable=bool(f.get("nullable", True)))


def _expr_to_sql(expr: str, m: Mapping, context: str) -> Optional[str]:
    if not expr:
        return None
    try:
        return infa_to_sql(expr)
    except ExpressionError as e:
        m.add_issue(IssueSeverity.MANUAL, "EXPRESSION_UNCONVERTED",
                    "IDMC expression could not be converted to SQL (%s)" % context,
                    detail="%s | %s" % (expr, e))
        return None


def _parse_mapping(doc: dict, pipeline: Pipeline) -> Mapping:
    raw = doc.get("name", "mapping")
    name = raw[2:] if raw.startswith("m_") else raw
    m = Mapping(name=name, origin=raw, description=doc.get("description", ""))

    runtime = doc.get("runtime", {}) or {}
    try:
        m.load_strategy = LoadStrategy(runtime.get("loadStrategy", "FULL"))
    except ValueError:
        m.load_strategy = LoadStrategy.FULL
    m.unique_key = list(runtime.get("uniqueKey", []) or [])

    for spec in doc.get("transformations", []) or []:
        ttype = _IDMC_TO_IR.get(spec.get("type", "Expression"),
                                TransformationType.EXPRESSION)
        props = dict(spec.get("properties", {}) or {})
        tname = spec.get("name", "t")

        if spec.get("type") == "Source":
            if props.get("sql_override") or props.get("queryMode") == "customQuery":
                ttype = TransformationType.SOURCE_QUALIFIER
            elif "object" in spec:
                ttype = TransformationType.SOURCE
                props.setdefault("table", spec.get("object"))
                conn = spec.get("connection", {}) or {}
                props.setdefault("schema", conn.get("schema", ""))
                props.setdefault("database", conn.get("database", ""))

        ports = []
        for f in spec.get("fields", []) or []:
            port = _port(f)
            if f.get("expression") and f["expression"] != port.name:
                sql = _expr_to_sql(str(f["expression"]), m,
                                   "%s.%s" % (tname, port.name))
                if sql is not None:
                    port.expression = sql
            ports.append(port)

        for key in ("condition",):
            if props.get(key):
                sql = _expr_to_sql(str(props[key]), m, "%s %s" % (tname, key))
                props[key] = sql if sql is not None else "TRUE"

        if ttype == TransformationType.TARGET:
            props.setdefault("table", spec.get("object", name))
            if spec.get("updateColumns"):
                m.unique_key = m.unique_key or list(spec["updateColumns"])

        t = Transformation(name=tname, type=ttype, ports=ports, properties=props)
        m.transformations.append(t)

        if ttype == TransformationType.SOURCE and t.properties.get("table"):
            table = str(t.properties["table"])
            if all(s.name.lower() != table.lower() for s in pipeline.sources):
                pipeline.sources.append(SourceTable(
                    name=table, schema=str(t.properties.get("schema", "")),
                    database=str(t.properties.get("database", "")),
                    columns=[Port(name=pp.name, datatype=pp.datatype)
                             for pp in ports]))

    for l in doc.get("links", []) or []:
        m.links.append(Link(str(l.get("from", "")), str(l.get("to", ""))))

    # Rebuild the virtual __OUTPUT__ marker in front of the target
    tgts = m.by_type(TransformationType.TARGET)
    if tgts:
        ups = m.upstream_of(tgts[0].name)
        if ups:
            up = ups[0]
            m.transformations.append(Transformation(
                name="__OUTPUT__", type=TransformationType.EXPRESSION,
                ports=[Port(name=p.name, datatype=p.datatype) for p in up.ports],
                properties={"virtual": True, "upstream": up.name}))
            m.links = [l for l in m.links
                       if not (l.from_transformation == up.name and
                               l.to_transformation == tgts[0].name)]
            m.links.append(Link(up.name, "__OUTPUT__"))
            m.links.append(Link("__OUTPUT__", tgts[0].name))
    return m


def _apply_taskflow_order(taskflows: List[dict], pipeline: Pipeline) -> None:
    for tf in taskflows:
        prev_wave: List[str] = []
        for step in tf.get("steps", []) or []:
            wave = [str(t.get("mapping", ""))[2:] for t in step.get("tasks", []) or []]
            for w in wave:
                m = pipeline.mapping(w)
                if m is not None:
                    for pw in prev_wave:
                        if pw not in m.depends_on:
                            m.depends_on.append(pw)
            prev_wave = wave

"""Talend Open Studio / Data Integration source adapter (Command 5).

Parses Talend job exports — ``.item`` process XML (``talendfile:ProcessType``)
plus optional ``.properties`` companions — into the canonical IR:

    one job (.item)          -> one Mapping per data-flow subjob, plus a
                                WORKFLOW DAG over subjob triggers
                                (OnSubjobOk/OnSubjobError/OnComponentOk)
    components (by componentName):
        t*Input / tFileInput*                 SOURCE (+ SOURCE_QUALIFIER;
                                              QUERY carried as SQL override)
        t*Output / tFileOutput*               TARGET
        tMap                                  JOINER(s) for lookup inputs +
                                              EXPRESSION for output columns
        tJoin                                 JOINER
        tFilterRow                            FILTER
        tAggregateRow                         AGGREGATOR
        tSortRow                              SORTER
        tUnite                                UNION
        tUniqRow                              RANK (dedup)
        tNormalize / tDenormalize             declared MANUAL (row-shape
                                              change) + EXPRESSION placeholder
        tJava / tJavaRow / tJavaFlex          declared MANUAL (code preserved)
        tRunJob                               workflow dependency node
    contexts / context params -> metadata["parameters"]
    repository metadata refs  -> metadata["connections"]

Java expressions inside tMap/tFilterRow go through the Talend translator;
untranslatable code becomes a MANUAL issue with the original preserved.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from ..ir.model import (
    ConversionIssue, IssueSeverity, Link, Mapping, Pipeline, Port,
    SourceTable, Transformation, TransformationType,
)
from .etl_expressions import talend_expression_to_sql


def _clean(name: str) -> str:
    return re.sub(r"\W+", "_", name).strip("_")


def _pipe_issue(pipeline: Pipeline, severity: IssueSeverity, code: str,
                message: str, obj: str = "", detail: str = "",
                suggestion: str = "") -> None:
    pipeline.issues.append(ConversionIssue(
        severity=severity, code=code, message=message, obj=obj,
        detail=detail, suggestion=suggestion))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


_TYPE = {"id_integer": "integer", "id_long": "bigint", "id_int": "integer",
         "id_short": "integer", "id_float": "double", "id_double": "double",
         "id_bigdecimal": "decimal", "id_date": "timestamp",
         "id_boolean": "boolean", "id_string": "string"}


def _column_ports(node: ET.Element) -> List[Port]:
    ports = []
    for md in node:
        if _local(md.tag) != "metadata":
            continue
        for col in md:
            if _local(col.tag) == "column":
                ports.append(Port(
                    name=col.get("name", ""),
                    datatype=_TYPE.get((col.get("type") or "").lower(),
                                       "string")))
        break                      # first FLOW metadata describes the schema
    return ports


def _params(node: ET.Element) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for ep in node:
        if _local(ep.tag) == "elementParameter":
            out[ep.get("name", "")] = ep.get("value", "") or ""
    return out


def _unquote(java: str) -> str:
    s = (java or "").strip()
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return s[1:-1]
    return s


_KIND = [
    (re.compile(r"^t\w*Input", re.I), "source"),
    (re.compile(r"^tFileInput", re.I), "source"),
    (re.compile(r"^t\w*Output", re.I), "target"),
    (re.compile(r"^tFileOutput", re.I), "target"),
    (re.compile(r"^tMap$", re.I), "tmap"),
    (re.compile(r"^tJoin$", re.I), "join"),
    (re.compile(r"^tFilterRow$", re.I), "filter"),
    (re.compile(r"^tAggregateRow$", re.I), "aggregate"),
    (re.compile(r"^tSortRow$", re.I), "sort"),
    (re.compile(r"^tUnite$", re.I), "union"),
    (re.compile(r"^tUniqRow$", re.I), "dedup"),
    (re.compile(r"^t(Normalize|Denormalize)$", re.I), "reshape"),
    (re.compile(r"^tJava", re.I), "java"),
    (re.compile(r"^tRunJob$", re.I), "runjob"),
    (re.compile(r"^t(LogRow|Buffer\w*|FlowMeter|Die|Warn)$", re.I),
     "passthrough"),
]


def _kind(component: str) -> str:
    for rx, kind in _KIND:
        if rx.match(component or ""):
            return kind
    return "unknown"


class _TalendProject:
    def parse(self, path: str, dialect: str = "") -> Pipeline:
        p = Path(path)
        items = [p] if p.is_file() else sorted(
            f for f in p.rglob("*.item")
            if b"ProcessType" in f.read_bytes()[:4000])
        if not items:
            raise FileNotFoundError("No Talend .item jobs found under %s"
                                    % path)
        pipeline = Pipeline(name=_clean(p.stem), source_format="talend")
        params: List[dict] = []
        connections: List[dict] = []
        dags: List[dict] = []
        job_names = {_clean(re.sub(r"_\d+\.\d+$", "", f.stem)).lower()
                     for f in items}

        for f in items:
            try:
                root = ET.parse(str(f)).getroot()
            except ET.ParseError as e:
                _pipe_issue(pipeline, IssueSeverity.ERROR,
                            "TALEND_ITEM_PARSE",
                            "%s is not valid XML" % f.name, detail=str(e))
                continue
            self._parse_job(f, root, pipeline, params, connections, dags,
                            job_names)

        pipeline.metadata["workflow_dags"] = dags
        pipeline.metadata["parameters"] = params
        pipeline.metadata["connections"] = connections
        pipeline.metadata["inventory"] = {
            "jobs": len(items), "mappings": len(pipeline.mappings),
            "parameters": len(params)}
        return pipeline

    def _parse_job(self, f: Path, root: ET.Element, pipeline: Pipeline,
                   params: List[dict], connections: List[dict],
                   dags: List[dict], job_names: set) -> None:
        job = _clean(re.sub(r"_\d+\.\d+$", "", f.stem)).lower()

        for ctx in root:
            if _local(ctx.tag) != "context":
                continue
            for cp in ctx:
                if _local(cp.tag) == "contextParameter":
                    params.append({"name": cp.get("name", ""),
                                   "scope": "%s/%s" % (job,
                                                       ctx.get("name", "")),
                                   "class": "runtime_parameter",
                                   "default": cp.get("value", "")})

        nodes: Dict[str, dict] = {}
        for node in root:
            if _local(node.tag) != "node":
                continue
            prm = _params(node)
            uname = prm.get("UNIQUE_NAME") or node.get("componentName", "")
            nodes[uname] = {"el": node,
                            "component": node.get("componentName", ""),
                            "kind": _kind(node.get("componentName", "")),
                            "params": prm,
                            "ports": _column_ports(node)}
            if prm.get("PROPERTY:REPOSITORY_PROPERTY_TYPE"):
                connections.append({
                    "name": prm.get("PROPERTY:REPOSITORY_PROPERTY_TYPE"),
                    "used_by": uname, "scope": job})

        flows: List[dict] = []
        triggers: List[dict] = []
        for conn in root:
            if _local(conn.tag) != "connection":
                continue
            kind = conn.get("connectorName", "")
            entry = {"from": conn.get("source", ""),
                     "to": conn.get("target", ""),
                     "label": conn.get("label", ""), "connector": kind}
            if kind in ("FLOW", "LOOKUP", "MERGE", "FILTER", "REJECT",
                        "UNIQUE", "OUTPUT"):
                flows.append(entry)
            elif kind.startswith("On"):
                triggers.append(entry)

        mapping = Mapping(name=job, origin=str(f))
        transforms: Dict[str, str] = {}     # component unique name -> IR name

        for uname, info in nodes.items():
            kind, prm, ports = info["kind"], info["params"], info["ports"]
            cname = _clean(uname)
            if kind == "source":
                table = _unquote(prm.get("TABLE", "")) or \
                    _clean(Path(_unquote(prm.get("FILENAME", ""))).stem) or \
                    cname
                src = Transformation(
                    name="SRC_" + cname, type=TransformationType.SOURCE,
                    ports=ports, properties={"table": _clean(table),
                                             "component":
                                             info["component"]})
                sq = Transformation(
                    name="SQ_" + cname,
                    type=TransformationType.SOURCE_QUALIFIER,
                    ports=[Port(p.name, p.datatype) for p in ports])
                query = _unquote(prm.get("QUERY", ""))
                if query and query.lower().split() and \
                        "select *" not in query.lower():
                    sq.properties["sql_override"] = query
                    mapping.add_issue(
                        IssueSeverity.INFO, "TALEND_SOURCE_QUERY",
                        "%s reads via component query — carried as SQL "
                        "override" % uname, detail=query[:300])
                mapping.transformations += [src, sq]
                mapping.links.append(Link(src.name, sq.name))
                transforms[uname] = sq.name
                if not any(s.name == src.properties["table"]
                           for s in pipeline.sources):
                    pipeline.sources.append(SourceTable(
                        name=str(src.properties["table"]),
                        columns=[Port(p.name, p.datatype) for p in ports]))
                continue
            if kind == "target":
                table = _unquote(prm.get("TABLE", "")) or \
                    _clean(Path(_unquote(prm.get("FILENAME", ""))).stem) or \
                    cname
                t = Transformation(
                    name="TGT_" + cname, type=TransformationType.TARGET,
                    ports=ports, properties={"table": _clean(table),
                                             "component":
                                             info["component"]})
                action = prm.get("DATA_ACTION", "")
                if action.upper() in ("INSERT_OR_UPDATE", "UPDATE_OR_INSERT",
                                      "UPSERT"):
                    from ..ir.model import LoadStrategy
                    mapping.load_strategy = LoadStrategy.MERGE
                mapping.transformations.append(t)
                transforms[uname] = t.name
                continue
            if kind == "tmap":
                self._parse_tmap(uname, info, mapping, flows, transforms)
                continue
            if kind == "filter":
                conds = []
                for tbl in info["el"].iter():
                    if _local(tbl.tag) == "elementValue" and \
                            tbl.get("elementRef") in ("OPERATOR", "FUNCTION",
                                                      "INPUT_COLUMN",
                                                      "RVALUE"):
                        conds.append(tbl.get("value", ""))
                cond_java = _unquote(prm.get("LOGICAL_OP", "&&")).join(
                    [])  # element-table conditions assembled below
                # assemble triples input OP value from the element table
                triples, cur = [], []
                for tbl in info["el"].iter():
                    if _local(tbl.tag) == "elementValue":
                        ref = tbl.get("elementRef")
                        if ref == "INPUT_COLUMN":
                            cur = [tbl.get("value", "")]
                        elif ref == "OPERATOR" and cur:
                            cur.append(tbl.get("value", ""))
                        elif ref == "RVALUE" and len(cur) == 2:
                            cur.append(tbl.get("value", ""))
                            triples.append(tuple(cur))
                            cur = []
                parts = []
                for colname, op, val in triples:
                    sql, _n = talend_expression_to_sql(
                        "%s %s %s" % (colname, op, val))
                    parts.append(sql or "%s %s %s" % (colname, op, val))
                condition = " AND ".join(parts) if parts else cond_java
                t = Transformation(name=cname,
                                   type=TransformationType.FILTER,
                                   ports=ports,
                                   properties={"condition": condition})
                if not parts:
                    mapping.add_issue(
                        IssueSeverity.MANUAL, "TALEND_FILTER_MANUAL",
                        "tFilterRow %s condition could not be extracted"
                        % uname)
            elif kind == "join":
                t = Transformation(name=cname,
                                   type=TransformationType.JOINER,
                                   ports=ports,
                                   properties={"join_type":
                                               "INNER" if prm.get(
                                                   "INNER_JOIN") == "true"
                                               else "LEFT"})
            elif kind == "aggregate":
                group, aggs = [], []
                for tbl in info["el"].iter():
                    if _local(tbl.tag) == "elementValue":
                        ref = tbl.get("elementRef")
                        if ref == "INPUT_COLUMN":
                            aggs.append(tbl.get("value", ""))
                        elif ref == "FUNCTION":
                            aggs.append("FN:" + tbl.get("value", ""))
                        elif ref == "OUTPUT_COLUMN":
                            aggs.append("OUT:" + tbl.get("value", ""))
                # GROUPBYS come as OUTPUT_COLUMN/INPUT_COLUMN pairs before
                # any FN entries; simplest faithful read: group = columns
                # in the GROUPBYS table
                group = self._element_table(info["el"], "GROUPBYS",
                                            "INPUT_COLUMN")
                ops = self._agg_operations(info["el"])
                t = Transformation(name=cname,
                                   type=TransformationType.AGGREGATOR,
                                   properties={"group_by": group})
                _FN = {"sum": "SUM", "count": "COUNT", "min": "MIN",
                       "max": "MAX", "avg": "AVG", "average": "AVG",
                       "first": "MIN", "last": "MAX",
                       "count_distinct": "COUNT(DISTINCT %s)"}
                for g in group:
                    t.ports.append(Port(g, "string"))
                for out_col, fn, in_col in ops:
                    fn_sql = _FN.get(fn.lower())
                    if fn_sql is None:
                        mapping.add_issue(
                            IssueSeverity.MANUAL, "TALEND_AGG_MANUAL",
                            "tAggregateRow %s: function '%s' has no direct "
                            "SQL equivalent" % (uname, fn), obj=uname)
                        expr = "NULL"
                    elif "%s" in fn_sql:
                        expr = fn_sql % in_col
                    else:
                        expr = "%s(%s)" % (fn_sql, in_col)
                    t.ports.append(Port(out_col, "string", expression=expr,
                                        direction="OUTPUT"))
            elif kind == "sort":
                keys = [{"port": c, "order": "ASC"} for c in
                        self._element_table(info["el"], "CRITERIA",
                                            "COLNAME")]
                t = Transformation(name=cname,
                                   type=TransformationType.SORTER,
                                   ports=ports,
                                   properties={"sort_keys": keys})
            elif kind == "union":
                t = Transformation(name=cname, type=TransformationType.UNION,
                                   ports=ports, properties={"inputs": []})
            elif kind == "dedup":
                t = Transformation(name=cname, type=TransformationType.RANK,
                                   ports=ports, properties={"dedup": True})
            elif kind == "reshape":
                t = Transformation(name=cname,
                                   type=TransformationType.EXPRESSION,
                                   ports=ports,
                                   properties={"unconverted_pc_type":
                                               info["component"]})
                mapping.add_issue(
                    IssueSeverity.MANUAL, "TALEND_RESHAPE_MANUAL",
                    "%s (%s) changes row shape — port as UNNEST/PIVOT in "
                    "the target" % (uname, info["component"]), obj=uname)
            elif kind == "java":
                in_flow = any(fl["from"] == uname or fl["to"] == uname
                              for fl in flows)
                if not in_flow:
                    # trigger-only tJava: an orchestration step, not a
                    # data-flow transformation
                    _pipe_issue(pipeline, IssueSeverity.MANUAL,
                                "TALEND_JAVA_MANUAL",
                                "%s contains Java code — port manually"
                                % uname, obj=job,
                                detail=_unquote(prm.get("CODE", ""))[:400])
                    continue
                t = Transformation(name=cname,
                                   type=TransformationType.EXPRESSION,
                                   ports=ports,
                                   properties={"unconverted_pc_type":
                                               info["component"]})
                mapping.add_issue(
                    IssueSeverity.MANUAL, "TALEND_JAVA_MANUAL",
                    "%s contains Java code — port manually" % uname, detail=_unquote(prm.get("CODE", ""))[:400])
            elif kind == "passthrough":
                t = Transformation(name=cname,
                                   type=TransformationType.EXPRESSION,
                                   ports=ports)
            elif kind == "runjob":
                continue          # workflow-level; handled via triggers
            else:
                t = Transformation(name=cname,
                                   type=TransformationType.EXPRESSION,
                                   ports=ports,
                                   properties={"unconverted_pc_type":
                                               info["component"]})
                mapping.add_issue(
                    IssueSeverity.MANUAL, "TALEND_COMPONENT_UNSUPPORTED",
                    "Component %s (%s) is not supported — preserved for "
                    "manual porting" % (uname, info["component"]))
            mapping.transformations.append(t)
            transforms[uname] = t.name

        for fl in flows:
            f_t, t_t = transforms.get(fl["from"]), transforms.get(fl["to"])
            if not f_t or not t_t:
                continue
            mapping.links.append(Link(f_t, t_t))
            to_t = mapping.transformation(t_t)
            if to_t is not None and to_t.type == TransformationType.UNION:
                to_t.properties.setdefault("inputs", []).append(f_t)
            if to_t is not None and to_t.type == TransformationType.JOINER:
                slot = "right" if fl["connector"] == "LOOKUP" or \
                    "left" in to_t.properties else "left"
                to_t.properties.setdefault(slot, f_t)

        if mapping.transformations:
            pipeline.mappings.append(mapping)

        # subjob / trigger DAG
        nodes_dag = [{"task_key": "Start__" + job, "task": "Start",
                      "type": "start"}]
        edges = []
        seen = set()

        def dag_node(uname: str) -> str:
            key = _clean(uname)
            if key in seen:
                return key
            seen.add(key)
            info = nodes.get(uname, {})
            if info.get("kind") == "java":
                nodes_dag.append({"task_key": key, "task": uname,
                                  "type": "command",
                                  "config": {"language": "java"}})
                return key
            if info.get("kind") == "runjob":
                child = _clean(_unquote(info["params"].get(
                    "PROCESS", ""))).lower()
                nodes_dag.append({
                    "task_key": key, "task": uname,
                    "type": "worklet" if child in job_names else "command",
                    "config": {"job": child}})
            else:
                nodes_dag.append({"task_key": key, "task": uname,
                                  "type": "session", "mapping": job})
            return key

        for trg in triggers:
            f_k, t_k = dag_node(trg["from"]), dag_node(trg["to"])
            kind = {"OnSubjobOk": "success", "OnComponentOk": "success",
                    "OnSubjobError": "failure",
                    "OnComponentError": "failure"}.get(trg["connector"],
                                                       "conditional")
            edges.append({"from": f_k, "to": t_k, "kind": kind,
                          "condition": ""})
        if len(nodes_dag) > 1:
            entry = {e["to"] for e in edges}
            for nd in nodes_dag[1:]:
                if nd["task_key"] not in entry:
                    edges.append({"from": "Start__" + job,
                                  "to": nd["task_key"], "kind": "success",
                                  "condition": ""})
            dags.append({
                "workflow": job, "nodes": nodes_dag, "edges": edges,
                "failure_paths": [(e["from"], e["to"]) for e in edges
                                  if e["kind"] == "failure"],
                "execution_order": [n["task_key"] for n in nodes_dag[1:]]})

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _element_table(node: ET.Element, table: str, ref: str) -> List[str]:
        out = []
        for ep in node:
            if _local(ep.tag) == "elementParameter" and \
                    ep.get("name") == table:
                for ev in ep:
                    if _local(ev.tag) == "elementValue" and \
                            ev.get("elementRef") == ref:
                        v = ev.get("value", "")
                        out.append(_unquote(v).split(".")[-1])
        return out

    @staticmethod
    def _agg_operations(node: ET.Element) -> List[tuple]:
        """OPERATIONS table -> [(output_column, function, input_column)]."""
        ops = []
        for ep in node:
            if _local(ep.tag) == "elementParameter" and \
                    ep.get("name") == "OPERATIONS":
                cur: Dict[str, str] = {}
                for ev in ep:
                    if _local(ev.tag) != "elementValue":
                        continue
                    ref, val = ev.get("elementRef", ""), ev.get("value", "")
                    cur[ref] = _unquote(val)
                    if ref == "INPUT_COLUMN":
                        ops.append((cur.get("OUTPUT_COLUMN", ""),
                                    cur.get("FUNCTION", ""),
                                    val.split(".")[-1]))
                        cur = {}
        return ops

    def _parse_tmap(self, uname: str, info: dict, mapping: Mapping,
                    flows: List[dict], transforms: Dict[str, str]) -> None:
        """tMap -> JOINER (per lookup input) + EXPRESSION (output exprs)."""
        cname = _clean(uname)
        el = info["el"]
        lookups = [fl for fl in flows
                   if fl["to"] == uname and fl["connector"] == "LOOKUP"]
        join = None
        if lookups:
            join = Transformation(name=cname + "_join",
                                  type=TransformationType.JOINER,
                                  properties={"join_type": "LEFT"})
            mapping.transformations.append(join)
        expr_t = Transformation(name=cname,
                                type=TransformationType.EXPRESSION)

        # mapper data: outputTables entries name/expression
        found_output = False
        for md in el.iter():
            tag = _local(md.tag)
            if tag not in ("outputTables",):
                continue
            found_output = True
            for entry in md.iter():
                if _local(entry.tag) != "mapperTableEntries":
                    continue
                colname = entry.get("name", "")
                java = entry.get("expression", "")
                if not colname:
                    continue
                if not java or re.fullmatch(r"row\d+\.%s"
                                            % re.escape(colname), java):
                    expr_t.ports.append(Port(colname, "string"))
                    continue
                sql, notes = talend_expression_to_sql(java)
                if not sql:
                    sql = "NULL"
                    mapping.add_issue(
                        IssueSeverity.MANUAL, "TALEND_TMAP_MANUAL",
                        "tMap %s.%s expression not translatable — NULL "
                        "placeholder emitted" % (uname, colname),
                        detail=java,
                        suggestion="Port the Java expression manually.")
                for n in notes:
                    mapping.add_issue(IssueSeverity.WARNING,
                                      "TALEND_TMAP_NOTE",
                                      "%s.%s: %s" % (uname, colname, n),
                                      detail=java)
                expr_t.ports.append(Port(colname, "string", expression=sql,
                                         direction="OUTPUT"))
            # join keys from lookup input tables
        for md in el.iter():
            if _local(md.tag) == "inputTables" and join is not None:
                conds = []
                for entry in md.iter():
                    if _local(entry.tag) == "mapperTableEntries" and \
                            entry.get("expression"):
                        key_java = entry.get("expression", "")
                        sql, _n = talend_expression_to_sql(key_java)
                        if sql:
                            conds.append("%s = %s" % (sql,
                                                      entry.get("name", "")))
                if conds:
                    join.properties["condition"] = " AND ".join(conds)
        if not found_output:
            mapping.add_issue(
                IssueSeverity.MANUAL, "TALEND_TMAP_NO_MAPPER_DATA",
                "tMap %s has no embedded mapper data — outputs unknown"
                % uname)
        mapping.transformations.append(expr_t)
        if join is not None:
            mapping.links.append(Link(join.name, expr_t.name))
            transforms[uname + "__join"] = join.name
            transforms[uname] = expr_t.name
            # re-point incoming flows: main flow + lookups enter the join
            for fl in flows:
                if fl["to"] == uname:
                    fl["to"] = uname + "__join"
            # entry for downstream stays the expression
            transforms[uname + "__join"] = join.name
        else:
            transforms[uname] = expr_t.name


def parse_talend(path: str, dialect: str = "") -> Pipeline:
    return _TalendProject().parse(path, dialect)

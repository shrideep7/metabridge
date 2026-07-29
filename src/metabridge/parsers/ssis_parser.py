"""Microsoft SSIS source adapter (Command 5).

Parses SSIS projects — ``.dtsx`` packages plus project-level ``.conmgr``
connection managers, ``.params`` parameter files and the ``.dtproj``
manifest — into the canonical IR:

    Data Flow Task            -> one Mapping (components -> transformations)
    Control flow              -> WORKFLOW DAG in metadata["workflow_dags"]
    Execute SQL Task          -> Mapping when the statement is set-based DML
                                 (INSERT..SELECT / MERGE / CTAS via the SQL
                                 decomposer); otherwise a command node with
                                 the statement preserved
    Variables / parameters    -> metadata["parameters"] (classified) with
                                 declared issues where behaviour cannot carry
    Connection managers       -> metadata["connections"] (never credentials)
    Event handlers            -> metadata["error_handlers"] + WARNING

Data-flow component coverage (componentClassID, name form):
    OLEDBSource/ADONETSource/ExcelSource/FlatFileSource   SOURCE (+ SQ)
    OLEDBDestination/ADONETDestination/FlatFileDestination TARGET
    DerivedColumn      EXPRESSION (per-column SSIS expressions translated)
    Lookup             LOOKUP
    ConditionalSplit   ROUTER
    MergeJoin          JOINER        Merge/UnionAll  UNION
    Aggregate          AGGREGATOR    Sort            SORTER
    SCD wizard         UPDATE_STRATEGY + SCD2 review flag
    Multicast/RowCount pass-through EXPRESSION
    ScriptComponent    EXPRESSION placeholder + MANUAL (code preserved)

Anything unrecognised is preserved as a MANUAL issue with the original
payload — never dropped silently. No regex-only parsing: packages are
walked as XML documents; only expressions use the translation layer.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from ..ir.model import (
    ConversionIssue, IssueSeverity, Link, LoadStrategy, Mapping, Pipeline,
    Port, SourceTable, Transformation, TransformationType,
)
from .etl_expressions import ssis_expression_to_sql
from .etl_graph import propagate_passthrough_ports

_DTS = "www.microsoft.com/SqlServer/Dts"


def _dts(tag: str) -> str:
    return "{%s}%s" % (_DTS, tag)


def _attr(el: ET.Element, name: str) -> str:
    return el.get(_dts(name)) or el.get(name) or ""


def _clean(name: str) -> str:
    return re.sub(r"\W+", "_", name).strip("_")


def _sql_type(ssis_type: str) -> str:
    t = (ssis_type or "").lower()
    if t in ("i4", "ui4", "i2", "ui2"):
        return "integer"
    if t in ("i8", "ui8"):
        return "bigint"
    if t in ("numeric", "decimal", "cy"):
        return "decimal"
    if t in ("r4", "r8"):
        return "double"
    if t in ("date", "dbdate"):
        return "date"
    if t.startswith("dbtimestamp") or t == "filetime":
        return "timestamp"
    if t == "bool":
        return "boolean"
    return "string"



def _pipe_issue(pipeline: Pipeline, severity: IssueSeverity, code: str,
                message: str, obj: str = "", detail: str = "",
                suggestion: str = "") -> None:
    pipeline.issues.append(ConversionIssue(
        severity=severity, code=code, message=message, obj=obj,
        detail=detail, suggestion=suggestion))

def _table_from_rowset(rowset: str) -> str:
    return rowset.replace("[", "").replace("]", "").strip()


# --------------------------------------------------------------------------
# data-flow parsing
# --------------------------------------------------------------------------

_COMPONENT_KIND = {
    "oledbsource": "source", "adonetsource": "source",
    "excelsource": "source", "flatfilesource": "source",
    "odbcsource": "source",
    "oledbdestination": "target", "adonetdestination": "target",
    "flatfiledestination": "target", "odbcdestination": "target",
    "derivedcolumn": "derived", "lookup": "lookup",
    "conditionalsplit": "split", "mergejoin": "mergejoin",
    "merge": "union", "unionall": "union", "aggregate": "aggregate",
    "sort": "sort", "scd": "scd", "multicast": "passthrough",
    "rowcount": "passthrough", "scriptcomponent": "script",
    "managedcomponenthost": "script",
}


def _component_kind(class_id: str) -> str:
    c = (class_id or "").lower()
    for key, kind in _COMPONENT_KIND.items():
        if key in c:
            return kind
    return "unknown"


def _props(component: ET.Element) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for prop in component.iter("property"):
        out[prop.get("name", "")] = (prop.text or "").strip()
    return out


def _output_columns(component: ET.Element) -> List[dict]:
    cols = []
    for out in component.iter("output"):
        if "Error" in (out.get("name") or ""):
            continue
        for col in out.iter("outputColumn"):
            cols.append({
                "name": col.get("name", ""),
                "type": _sql_type(col.get("dataType", "")),
                "output": out.get("name", ""),
                "props": {p.get("name", ""): (p.text or "").strip()
                          for p in col.iter("property")},
                "sort_pos": col.get("sortKeyPosition", ""),
            })
    return cols


def _parse_dataflow(task_name: str, obj: ET.Element, mapping: Mapping,
                    connections: Dict[str, dict],
                    pipeline: Pipeline) -> None:
    components: Dict[str, dict] = {}       # refId -> info
    for comp in obj.iter("component"):
        ref = comp.get("refId", comp.get("name", ""))
        components[ref] = {
            "el": comp, "name": _clean(comp.get("name", ref)),
            "kind": _component_kind(comp.get("componentClassID", "")),
            "props": _props(comp), "cols": _output_columns(comp),
        }

    # refId prefix of a path endpoint identifies its component
    def owner(path_end: str) -> Optional[str]:
        best = ""
        for ref in components:
            if path_end.startswith(ref + ".") and len(ref) > len(best):
                best = ref
        return best or None

    edges: List[tuple] = []
    for path in obj.iter("path"):
        f, t = owner(path.get("startId", "")), owner(path.get("endId", ""))
        if f and t:
            edges.append((f, t))

    sq_of: Dict[str, str] = {}

    for ref, c in components.items():
        name, kind, props = c["name"], c["kind"], c["props"]
        ports = [Port(name=col["name"], datatype=col["type"])
                 for col in c["cols"]]

        if kind == "source":
            table = _table_from_rowset(props.get("OpenRowset", "")) or name
            conn = ""
            cm = c["el"].find(".//connection")
            if cm is not None:
                conn = cm.get("connectionManagerID", "")
            src = Transformation(name="SRC_" + name,
                                 type=TransformationType.SOURCE,
                                 ports=ports,
                                 properties={"table": table.split(".")[-1],
                                             "schema": table.split(".")[0]
                                             if "." in table else "",
                                             "connection": conn})
            sq = Transformation(name="SQ_" + name,
                                type=TransformationType.SOURCE_QUALIFIER,
                                ports=[Port(p.name, p.datatype)
                                       for p in ports])
            if props.get("SqlCommand"):
                sq.properties["sql_override"] = props["SqlCommand"]
                mapping.add_issue(
                    IssueSeverity.INFO, "SSIS_SOURCE_SQL",
                    "Source '%s' reads via SqlCommand — carried as SQL "
                    "override" % name, detail=props["SqlCommand"][:300])
            mapping.transformations += [src, sq]
            mapping.links.append(Link("SRC_" + name, "SQ_" + name))
            sq_of[ref] = "SQ_" + name
            if not any(s.name == src.properties["table"]
                       for s in pipeline.sources):
                pipeline.sources.append(SourceTable(
                    name=str(src.properties["table"]),
                    schema=str(src.properties.get("schema", "")),
                    columns=[Port(p.name, p.datatype) for p in ports]))
            continue

        if kind == "target":
            table = _table_from_rowset(props.get("OpenRowset", "")) or name
            t = Transformation(
                name="TGT_" + name, type=TransformationType.TARGET,
                ports=[Port(col.get("name", ""), "string") for col in
                       (ic for ic in c["el"].iter("inputColumn"))]
                or ports,
                properties={"table": table.split(".")[-1],
                            "schema": table.split(".")[0]
                            if "." in table else ""})
            mapping.transformations.append(t)
            sq_of[ref] = t.name
            continue

        if kind == "derived":
            t = Transformation(name=name, type=TransformationType.EXPRESSION)
            for col in c["cols"]:
                expr = (col["props"].get("FriendlyExpression")
                        or col["props"].get("Expression") or "")
                sql, notes = ssis_expression_to_sql(expr) if expr else ("", [])
                if expr and not sql:
                    mapping.add_issue(
                        IssueSeverity.MANUAL, "SSIS_EXPRESSION_MANUAL",
                        "Derived column '%s.%s' expression could not be "
                        "translated" % (name, col["name"]),
                        detail=expr, suggestion="Port the expression to the "
                        "target SQL dialect manually.")
                for n in notes:
                    mapping.add_issue(IssueSeverity.WARNING,
                                      "SSIS_EXPRESSION_NOTE",
                                      "%s.%s: %s" % (name, col["name"], n),
                                      detail=expr)
                t.ports.append(Port(name=col["name"], datatype=col["type"],
                                    expression=sql, direction="OUTPUT"))
        elif kind == "lookup":
            ref_sql = props.get("SqlCommand", "")
            ref_table = ""
            m = re.search(r"from\s+([\w.\[\]]+)", ref_sql, re.I)
            if m:
                ref_table = _table_from_rowset(m.group(1))
            joins = []
            for ic in c["el"].iter("inputColumn"):
                for p in ic.iter("property"):
                    if p.get("name") == "JoinToReferenceColumn" and p.text:
                        joins.append("%s = %s" % (ic.get("cachedName",
                                                         ic.get("name", "")),
                                                  p.text))
            t = Transformation(
                name=name, type=TransformationType.LOOKUP, ports=ports,
                properties={"table": ref_table,
                            "condition": " AND ".join(joins)})
        elif kind == "split":
            groups = []
            for col in c["cols"]:
                pass
            for out in c["el"].iter("output"):
                for p in out.iter("property"):
                    if p.get("name") in ("FriendlyExpression", "Expression") \
                            and p.text:
                        sql, notes = ssis_expression_to_sql(p.text)
                        groups.append({"name": _clean(out.get("name", "")),
                                       "condition": sql or p.text})
                        if not sql:
                            mapping.add_issue(
                                IssueSeverity.MANUAL, "SSIS_SPLIT_MANUAL",
                                "Conditional Split output '%s' condition "
                                "not translatable" % out.get("name", ""),
                                detail=p.text)
            t = Transformation(name=name, type=TransformationType.ROUTER,
                               ports=ports, properties={"groups": groups})
        elif kind == "mergejoin":
            jt = {"0": "FULL", "1": "LEFT", "2": "INNER"}.get(
                props.get("JoinType", "2"), "INNER")
            t = Transformation(name=name, type=TransformationType.JOINER,
                               ports=ports,
                               properties={"join_type": jt})
        elif kind == "union":
            t = Transformation(name=name, type=TransformationType.UNION,
                               ports=ports, properties={"inputs": []})
        elif kind == "aggregate":
            group_by, aggs = [], {}
            _AGG = {"1": "COUNT", "2": "COUNT", "3": "SUM", "4": "AVG",
                    "5": "MIN", "6": "MAX"}
            t = Transformation(name=name, type=TransformationType.AGGREGATOR)
            for col in c["cols"]:
                a = col["props"].get("AggregationType", "0")
                if a == "0":
                    group_by.append(col["name"])
                    t.ports.append(Port(col["name"], col["type"]))
                else:
                    fn = _AGG.get(a, "SUM")
                    base = col["props"].get("AggregationColumnId", "") \
                        or col["name"]
                    t.ports.append(Port(col["name"], col["type"],
                                        expression="%s(%s)" % (fn, col["name"]
                                                               if not base
                                                               else col["name"]),
                                        direction="OUTPUT"))
            t.properties["group_by"] = group_by
        elif kind == "sort":
            keys = [{"port": col["name"], "order": "ASC"}
                    for col in c["cols"]
                    if col.get("sort_pos") not in ("", "0", None)]
            t = Transformation(name=name, type=TransformationType.SORTER,
                               ports=ports,
                               properties={"sort_keys": keys})
        elif kind == "scd":
            t = Transformation(name=name,
                               type=TransformationType.UPDATE_STRATEGY,
                               ports=ports, properties={"scd": "wizard"})
            mapping.load_strategy = LoadStrategy.SCD2
            mapping.add_issue(
                IssueSeverity.WARNING, "SSIS_SCD_WIZARD",
                "SCD wizard component converted as SCD Type 2 strategy — "
                "verify historical/changing attribute lists")
        elif kind == "script":
            t = Transformation(name=name, type=TransformationType.EXPRESSION,
                               ports=ports,
                               properties={"unconverted_pc_type": "Script"})
            mapping.add_issue(
                IssueSeverity.MANUAL, "SSIS_SCRIPT_COMPONENT",
                "Script component '%s' contains .NET code — port manually"
                % name,
                detail=props.get("SourceCode", "")[:400],
                suggestion="Re-implement as SQL or a target-native function.")
        elif kind == "passthrough":
            t = Transformation(name=name, type=TransformationType.EXPRESSION,
                               ports=ports)
        else:
            t = Transformation(name=name, type=TransformationType.EXPRESSION,
                               ports=ports,
                               properties={"unconverted_pc_type":
                                           c["el"].get("componentClassID",
                                                       "unknown")})
            mapping.add_issue(
                IssueSeverity.MANUAL, "SSIS_COMPONENT_UNSUPPORTED",
                "Data-flow component '%s' (%s) is not supported — preserved "
                "for manual porting" % (name,
                                        c["el"].get("componentClassID", "?")),
                obj=name)
        mapping.transformations.append(t)
        sq_of[ref] = t.name

    for f, t in edges:
        if f in sq_of and t in sq_of:
            mapping.links.append(Link(sq_of[f], sq_of[t]))
            tt = mapping.transformation(sq_of[t])
            if tt is not None and tt.type == TransformationType.UNION:
                tt.properties.setdefault("inputs", []).append(sq_of[f])
            if tt is not None and tt.type == TransformationType.JOINER:
                if "left" not in tt.properties:
                    tt.properties["left"] = sq_of[f]
                else:
                    tt.properties.setdefault("right", sq_of[f])

    propagate_passthrough_ports(mapping)


# --------------------------------------------------------------------------
# control-flow parsing
# --------------------------------------------------------------------------

def _exec_kind(exec_type: str) -> str:
    e = (exec_type or "").lower()
    if "pipeline" in e:
        return "dataflow"
    if "executesqltask" in e or "sqltask" in e:
        return "sql"
    if "scripttask" in e:
        return "script"
    if "executepackage" in e:
        return "package"
    if "sendmail" in e:
        return "email"
    if "foreachloop" in e or "forloop" in e:
        return "loop"
    if "sequence" in e:
        return "sequence"
    return "task"


class SSISPackage:
    """One parsed .dtsx file."""

    def __init__(self, path: Path):
        self.path = path
        self.name = _clean(path.stem)
        self.root = ET.parse(str(path)).getroot()

    def variables(self) -> List[dict]:
        out = []
        for v in self.root.iter(_dts("Variable")):
            out.append({
                "name": _attr(v, "ObjectName"),
                "namespace": _attr(v, "Namespace") or "User",
                "expression": _attr(v, "Expression"),
                "evaluate_as_expression":
                    _attr(v, "EvaluateAsExpression") == "True",
                "value": (v.findtext(_dts("VariableValue")) or "").strip(),
            })
        return out

    def package_parameters(self) -> List[dict]:
        return [{"name": _attr(pp, "ObjectName"),
                 "value": (pp.findtext(_dts("Property")) or "").strip()}
                for pp in self.root.iter(_dts("PackageParameter"))]

    def connection_managers(self) -> List[dict]:
        out = []
        for cm in self.root.iter(_dts("ConnectionManager")):
            cs = ""
            for inner in cm.iter():
                if inner.tag.endswith("ConnectionManager") and \
                        _attr(inner, "ConnectionString"):
                    cs = _attr(inner, "ConnectionString")
            info = {"name": _attr(cm, "ObjectName"),
                    "type": _attr(cm, "CreationName")}
            m = re.search(r"Data Source=([^;]+)", cs)
            if m:
                info["server"] = m.group(1)
            m = re.search(r"Initial Catalog=([^;]+)", cs)
            if m:
                info["database"] = m.group(1)
            if info["name"]:
                out.append(info)          # never the raw string (credentials)
        return out

    def event_handlers(self) -> List[dict]:
        return [{"event": _attr(eh, "EventName") or "OnError",
                 "package": self.name}
                for eh in self.root.iter(_dts("EventHandler"))]

    def executables(self) -> List[dict]:
        """Flattened control-flow tasks (containers flattened, recorded)."""
        tasks: List[dict] = []

        def walk(parent: ET.Element, container: str):
            execs = parent.find(_dts("Executables"))
            if execs is None:
                return
            for ex in execs.findall(_dts("Executable")):
                kind = _exec_kind(_attr(ex, "ExecutableType")
                                  or _attr(ex, "CreationName"))
                info = {"name": _clean(_attr(ex, "ObjectName")),
                        "refId": _attr(ex, "refId") or _attr(ex, "DTSID"),
                        "kind": kind, "container": container, "el": ex}
                tasks.append(info)
                if kind in ("loop", "sequence"):
                    walk(ex, info["name"])
        walk(self.root, "")
        return tasks

    def constraints(self) -> List[dict]:
        out = []
        for pc in self.root.iter(_dts("PrecedenceConstraint")):
            value = _attr(pc, "Value")
            kind = {"1": "failure", "2": "always"}.get(value, "success")
            cond = _attr(pc, "Expression")
            if _attr(pc, "EvalOp") in ("2", "3") and cond:
                kind = "conditional"
            out.append({"from": _attr(pc, "From"), "to": _attr(pc, "To"),
                        "kind": kind, "condition": cond})
        return out


class _SSISProjectParser:
    def parse(self, path: str, dialect: str = "") -> Pipeline:
        p = Path(path)
        dtsx = [p] if p.is_file() and p.suffix.lower() == ".dtsx" else \
            sorted(p.rglob("*.dtsx"))
        if not dtsx:
            raise FileNotFoundError("No .dtsx packages found under %s" % path)
        pipeline = Pipeline(name=_clean(p.stem), source_format="ssis")
        pipeline.metadata["dialect"] = "tsql"

        params: List[dict] = []
        connections: List[dict] = []
        for f in ([] if p.is_file() else sorted(p.rglob("*.params"))):
            try:
                root = ET.parse(str(f)).getroot()
                for prm in root.iter():
                    if prm.tag.endswith("Parameter"):
                        name = _attr(prm, "ObjectName") or prm.get(
                            "{www.microsoft.com/SqlServer/SSIS}Name",
                            prm.get("Name", ""))
                        if name:
                            params.append({"name": name, "scope": "project",
                                           "class": "runtime_parameter"})
            except ET.ParseError:
                _pipe_issue(pipeline, IssueSeverity.WARNING, "SSIS_PARAMS_PARSE",
                                   "Could not parse %s" % f.name)
        for f in ([] if p.is_file() else sorted(p.rglob("*.conmgr"))):
            try:
                root = ET.parse(str(f)).getroot()
                cs = ""
                for el in root.iter():
                    if _attr(el, "ConnectionString"):
                        cs = _attr(el, "ConnectionString")
                info = {"name": f.stem, "scope": "project"}
                m = re.search(r"Data Source=([^;]+)", cs)
                if m:
                    info["server"] = m.group(1)
                m = re.search(r"Initial Catalog=([^;]+)", cs)
                if m:
                    info["database"] = m.group(1)
                connections.append(info)
            except ET.ParseError:
                _pipe_issue(pipeline, IssueSeverity.WARNING, "SSIS_CONMGR_PARSE",
                                   "Could not parse %s" % f.name)

        dags: List[dict] = []
        package_names = {_clean(f.stem) for f in dtsx}
        error_handlers: List[dict] = []

        for f in dtsx:
            try:
                pkg = SSISPackage(f)
            except ET.ParseError as e:
                _pipe_issue(pipeline, IssueSeverity.ERROR, "SSIS_PACKAGE_PARSE",
                                   "Package %s is not valid XML" % f.name,
                                   detail=str(e))
                continue
            self._parse_package(pkg, pipeline, dags, params, connections,
                                error_handlers, package_names, dialect)

        pipeline.metadata["workflow_dags"] = dags
        pipeline.metadata["parameters"] = params
        pipeline.metadata["connections"] = connections
        if error_handlers:
            pipeline.metadata["error_handlers"] = error_handlers
        pipeline.metadata["inventory"] = {
            "packages": len(dtsx), "data_flows": len(pipeline.mappings),
            "parameters": len(params), "connections": len(connections)}
        return pipeline

    # -- one package -------------------------------------------------------
    def _parse_package(self, pkg: SSISPackage, pipeline: Pipeline,
                       dags: List[dict], params: List[dict],
                       connections: List[dict],
                       error_handlers: List[dict],
                       package_names: set, dialect: str) -> None:
        connections.extend(c for c in pkg.connection_managers()
                           if c["name"] not in {x["name"]
                                                for x in connections})
        for v in pkg.variables():
            cls = "derived_variable" if v["evaluate_as_expression"] \
                else "static_variable"
            params.append({"name": "%s::%s" % (v["namespace"], v["name"]),
                           "scope": pkg.name, "class": cls,
                           "expression": v["expression"]})
            if v["evaluate_as_expression"]:
                _pipe_issue(pipeline, 
                    IssueSeverity.WARNING, "SSIS_VARIABLE_EXPRESSION",
                    "Variable %s::%s is expression-evaluated — supply an "
                    "equivalent orchestrator parameter"
                    % (v["namespace"], v["name"]), detail=v["expression"])
        for prm in pkg.package_parameters():
            params.append({"name": prm["name"], "scope": pkg.name,
                           "class": "runtime_parameter"})
        for eh in pkg.event_handlers():
            error_handlers.append(eh)
            _pipe_issue(pipeline, 
                IssueSeverity.WARNING, "SSIS_EVENT_HANDLER",
                "Package %s has an %s event handler — recreate as "
                "orchestrator failure handling" % (pkg.name, eh["event"]))

        tasks = pkg.executables()
        nodes: List[dict] = [{"task_key": "Start__" + pkg.name,
                              "task": "Start", "type": "start"}]
        key_of: Dict[str, str] = {}

        for t in tasks:
            key = t["name"]
            key_of[t["refId"]] = key
            node = {"task_key": key, "task": t["name"]}
            if t["kind"] == "dataflow":
                mname = _clean("%s_%s" % (pkg.name, t["name"])).lower()
                mapping = Mapping(name=mname, origin=str(pkg.path))
                _parse_dataflow(t["name"], t["el"], mapping,
                                {}, pipeline)
                if not any(x.type == TransformationType.TARGET
                           for x in mapping.transformations):
                    mapping.add_issue(
                        IssueSeverity.MANUAL, "SSIS_NO_DESTINATION",
                        "Data flow '%s' has no supported destination "
                        "component" % t["name"])
                pipeline.mappings.append(mapping)
                node.update({"type": "session", "mapping": mname})
            elif t["kind"] == "sql":
                sql = ""
                for prop in t["el"].iter():
                    if prop.tag.endswith("SqlTaskData"):
                        sql = prop.get(
                            "{www.microsoft.com/sqlserver/dts/tasks/"
                            "sqltask}SqlStatementSource", "") or \
                            prop.get("SQLTask:SqlStatementSource", "")
                mapped = self._sql_task_mapping(t["name"], pkg, sql,
                                                pipeline)
                if mapped:
                    node.update({"type": "session", "mapping": mapped})
                else:
                    node.update({"type": "command",
                                 "config": {"sql": sql[:2000]}})
            elif t["kind"] == "script":
                node.update({"type": "command",
                             "config": {"language": ".NET script task"}})
                _pipe_issue(pipeline, 
                    IssueSeverity.MANUAL, "SSIS_SCRIPT_TASK",
                    "Script Task '%s' contains .NET code — port manually"
                    % t["name"], obj=t["name"])
            elif t["kind"] == "package":
                child = ""
                for el in t["el"].iter():
                    pn = el.get("PackageName", "") or _attr(el, "PackageName")
                    if not pn and el.tag.endswith("PackageName"):
                        pn = (el.text or "").strip()
                    if pn:
                        child = _clean(Path(pn).stem)
                node.update({"type": "worklet" if child in package_names
                             else "command", "config": {"package": child}})
            elif t["kind"] == "email":
                node.update({"type": "email", "config": {}})
            elif t["kind"] in ("loop", "sequence"):
                node.update({"type": "control",
                             "config": {"container": t["kind"]}})
                if t["kind"] == "loop":
                    _pipe_issue(pipeline, 
                        IssueSeverity.WARNING, "SSIS_LOOP_CONTAINER",
                        "ForEach/For loop '%s' — iteration must be "
                        "recreated in the orchestrator" % t["name"])
            else:
                node.update({"type": "command", "config": {}})
            nodes.append(node)

        edges = []
        for c in pkg.constraints():
            f, t = key_of.get(c["from"]), key_of.get(c["to"])
            if f and t:
                edges.append({"from": f, "to": t, "kind": c["kind"],
                              "condition": c["condition"]})
        # container children run inside their container
        for t in tasks:
            if t["container"]:
                edges.append({"from": t["container"], "to": t["name"],
                              "kind": "success", "condition": ""})
        entry = {e["to"] for e in edges}
        for t in tasks:
            if t["name"] not in entry:
                edges.append({"from": "Start__" + pkg.name, "to": t["name"],
                              "kind": "success", "condition": ""})

        order, incoming = [], {n["task_key"]: 0 for n in nodes}
        for e in edges:
            incoming[e["to"]] = incoming.get(e["to"], 0) + 1
        ready = [k for k, n in incoming.items() if n == 0]
        adj: Dict[str, List[str]] = {}
        for e in edges:
            adj.setdefault(e["from"], []).append(e["to"])
        while ready:
            k = ready.pop(0)
            order.append(k)
            for nxt in adj.get(k, []):
                incoming[nxt] -= 1
                if incoming[nxt] == 0:
                    ready.append(nxt)

        dags.append({
            "workflow": pkg.name, "nodes": nodes, "edges": edges,
            "failure_paths": [(e["from"], e["to"]) for e in edges
                              if e["kind"] == "failure"],
            "execution_order": [k for k in order
                                if not k.startswith("Start__")],
        })

    def _sql_task_mapping(self, task: str, pkg: SSISPackage, sql: str,
                          pipeline: Pipeline) -> str:
        """Execute SQL Task with set-based DML -> Mapping via the SQL
        decomposer; anything else stays a command node."""
        s = (sql or "").strip()
        if not s or not re.match(r"(insert|merge|create\s+(or\s+replace\s+)?"
                                 r"(table|view))", s, re.I):
            return ""
        from ..sqlx.decompose import decompose_model
        import sqlglot
        from sqlglot import exp
        try:
            stmt = sqlglot.parse_one(s, read="tsql")
        except Exception:  # noqa: BLE001
            _pipe_issue(pipeline, IssueSeverity.MANUAL, "SSIS_SQL_TASK_MANUAL",
                               "Execute SQL Task '%s' statement could not "
                               "be parsed" % task, detail=s[:300])
            return ""
        target, select = "", None

        def _table_name(node) -> str:
            if isinstance(node, exp.Schema):     # "t (col, col)" column list
                node = node.this
            return node.sql() if node is not None else task

        if isinstance(stmt, exp.Insert):
            target = _table_name(stmt.this)
            select = stmt.expression
        elif isinstance(stmt, exp.Create) and stmt.expression is not None:
            target = _table_name(stmt.this)
            select = stmt.expression
        if select is None:
            return ""
        name = _clean("%s_%s" % (pkg.name, task)).lower()
        local = {st.name: st for st in pipeline.sources}
        mapping = decompose_model(name, select.sql(dialect="tsql"), "tsql",
                                  local)
        mapping.origin = str(pkg.path)
        tgt_table = target.replace("[", "").replace("]", "").split(".")[-1]
        mapping.transformations.append(Transformation(
            name="TGT_" + _clean(tgt_table),
            type=TransformationType.TARGET,
            properties={"table": tgt_table}))
        tails = [t.name for t in mapping.transformations
                 if t.type not in (TransformationType.SOURCE,
                                   TransformationType.TARGET)
                 and not any(l.from_transformation == t.name
                             for l in mapping.links)]
        for tail in tails:
            mapping.links.append(Link(tail, "TGT_" + _clean(tgt_table)))
        pipeline.mappings.append(mapping)
        return name


def parse_ssis(path: str, dialect: str = "") -> Pipeline:
    return _SSISProjectParser().parse(path, dialect)

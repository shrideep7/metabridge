"""IBM DataStage source adapter (Command 5).

Parses DSX exports (``BEGIN DSJOB`` / ``BEGIN DSRECORD`` block format used
by DataStage export, both parallel and server jobs) into the canonical IR:

    parallel / server job    -> one Mapping
    stages                   -> transformations (by StageType):
        PxSequentialFile / CSeqFileStage / PxDataSet / CHashedFileStage /
        Px*Connector / ODBC / DB2 / Oracle stages     SOURCE or TARGET
                                                      (by pin direction)
        CTransformerStage / PxTransformer             EXPRESSION (+FILTER
                                                      when constraints exist)
        PxLookup / CLookup                            LOOKUP
        PxJoin                                        JOINER
        PxAggregator / CAggregatorStage               AGGREGATOR
        PxSort                                        SORTER
        PxFunnel                                      UNION
        PxCopy / PxPeek                               pass-through EXPRESSION
        PxRemDup                                      RANK (dedup semantics)
    links (pins + Partner)   -> Link edges
    transformer derivations  -> port expressions via the BASIC translator
    job sequences (CJS*)     -> WORKFLOW DAG in metadata["workflow_dags"]
    shared containers        -> inventoried; jobs referencing them get a
                               declared MANUAL issue (container internals
                               ship separately in the DSX)
    job parameters           -> metadata["parameters"]

The DSX block structure is parsed with a real tokenizer (BEGIN/END nesting,
quoted values, ``=+=+=+=`` multiline markers) — not regex-over-the-file.
Unknown stage types are preserved as MANUAL issues, never dropped.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from ..ir.model import (
    ConversionIssue, IssueSeverity, Link, Mapping, Pipeline, Port,
    SourceTable, Transformation, TransformationType,
)
from .etl_expressions import datastage_expression_to_sql


def _clean(name: str) -> str:
    return re.sub(r"\W+", "_", name).strip("_")


# --------------------------------------------------------------------------
# DSX block reader
# --------------------------------------------------------------------------

class DSRecord:
    def __init__(self) -> None:
        self.props: Dict[str, str] = {}
        self.subrecords: List[Dict[str, str]] = []

    def get(self, key: str, default: str = "") -> str:
        return self.props.get(key, default)


def _parse_value(line: str) -> Optional[tuple]:
    m = re.match(r'\s*(\w+)\s+"((?:[^"\\]|\\.)*)"\s*$', line)
    if m:
        return m.group(1), m.group(2).replace('\\"', '"')
    m = re.match(r"\s*(\w+)\s+(\S+)\s*$", line)
    if m:
        return m.group(1), m.group(2)
    return None


def parse_dsx(text: str) -> List[dict]:
    """DSX text -> list of jobs: {name, records:[DSRecord], props}."""
    jobs: List[dict] = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if line.startswith("BEGIN DSJOB"):
            job = {"name": "", "records": [], "props": {}}
            i += 1
            while i < n and not lines[i].strip().startswith("END DSJOB"):
                s = lines[i].strip()
                if s.startswith("BEGIN DSRECORD"):
                    rec = DSRecord()
                    i += 1
                    while i < n and not lines[i].strip().startswith(
                            "END DSRECORD"):
                        s2 = lines[i].strip()
                        if s2.startswith("BEGIN DSSUBRECORD"):
                            sub: Dict[str, str] = {}
                            i += 1
                            while i < n and not lines[i].strip().startswith(
                                    "END DSSUBRECORD"):
                                kv = _parse_value(lines[i])
                                if kv:
                                    sub[kv[0]] = kv[1]
                                i += 1
                            rec.subrecords.append(sub)
                        else:
                            kv = _parse_value(lines[i])
                            if kv:
                                rec.props[kv[0]] = kv[1]
                        i += 1
                    job["records"].append(rec)
                else:
                    kv = _parse_value(lines[i])
                    if kv:
                        job["props"][kv[0]] = kv[1]
                        if kv[0] == "Identifier":
                            job["name"] = kv[1]
                i += 1
            jobs.append(job)
        i += 1
    return jobs


# --------------------------------------------------------------------------
# stage classification
# --------------------------------------------------------------------------

_STAGE_KIND = [
    (("pxsequentialfile", "cseqfilestage", "pxdataset", "chashedfilestage",
      "pxfileset", "odbc", "db2", "oracle", "teradata", "netezza",
      "drsstage", "cudtstage", "pxodbc", "pxdb2", "pxoracle"), "io"),
    (("ctransformerstage", "pxtransformer", "transformer"), "transformer"),
    (("pxlookup", "clookup", "lookup"), "lookup"),
    (("pxjoin", "cjoin", "join"), "join"),
    (("pxaggregator", "caggregatorstage", "aggregator"), "aggregate"),
    (("pxsort", "csort", "sort"), "sort"),
    (("pxfunnel", "funnel"), "union"),
    (("pxremdup", "remdup"), "dedup"),
    (("pxcopy", "pxpeek", "copy", "peek"), "passthrough"),
]


def _stage_kind(stage_type: str) -> str:
    s = (stage_type or "").lower()
    for keys, kind in _STAGE_KIND:
        for k in keys:
            if k in s:
                return kind
    return "unknown"


def _record_columns(rec: DSRecord) -> List[Port]:
    ports = []
    for sub in rec.subrecords:
        if "SqlType" in sub or ("Name" in sub and "Derivation" in sub):
            dt = {"1": "string", "2": "decimal", "3": "decimal",
                  "4": "integer", "5": "integer", "6": "double",
                  "8": "double", "9": "date", "10": "date", "11": "timestamp",
                  "12": "string", "-5": "bigint"}.get(
                      sub.get("SqlType", "12"), "string")
            ports.append(Port(name=sub.get("Name", ""), datatype=dt,
                              expression=""))
    return ports


# --------------------------------------------------------------------------
# job -> Mapping
# --------------------------------------------------------------------------

def _pipe_issue(pipeline: Pipeline, severity: IssueSeverity, code: str,
                message: str, obj: str = "", detail: str = "",
                suggestion: str = "") -> None:
    pipeline.issues.append(ConversionIssue(
        severity=severity, code=code, message=message, obj=obj,
        detail=detail, suggestion=suggestion))


class _DataStageProject:
    def __init__(self) -> None:
        self.shared_containers: Dict[str, dict] = {}

    def parse(self, path: str, dialect: str = "") -> Pipeline:
        p = Path(path)
        files = [p] if p.is_file() else sorted(p.rglob("*.dsx"))
        if not files:
            raise FileNotFoundError("No .dsx exports found under %s" % path)
        pipeline = Pipeline(name=_clean(p.stem), source_format="datastage")
        params: List[dict] = []
        dags: List[dict] = []

        all_jobs: List[dict] = []
        for f in files:
            try:
                all_jobs += [dict(j, file=str(f)) for j in
                             parse_dsx(f.read_text(errors="replace", encoding="utf-8"))]
            except OSError as e:
                _pipe_issue(pipeline, IssueSeverity.ERROR, "DSX_READ_ERROR",
                            "Could not read %s" % f.name, detail=str(e))

        job_names = set()
        for job in all_jobs:
            kinds = {r.get("OLEType") for r in job["records"]}
            if any(str(k).startswith("CJS") for k in kinds):
                self._parse_sequence(job, pipeline, dags)
            elif any(str(k) == "CContainerView" for k in kinds) and \
                    "shared" in job.get("props", {}).get(
                        "Category", "").lower():
                self.shared_containers[job["name"]] = job
            else:
                name = self._parse_job(job, pipeline, params)
                if name:
                    job_names.add(name)

        pipeline.metadata["workflow_dags"] = dags
        pipeline.metadata["parameters"] = params
        pipeline.metadata["inventory"] = {
            "jobs": len(all_jobs), "mappings": len(pipeline.mappings),
            "sequences": len(dags),
            "shared_containers": len(self.shared_containers),
            "parameters": len(params)}
        return pipeline

    # -- one job -----------------------------------------------------------
    def _parse_job(self, job: dict, pipeline: Pipeline,
                   params: List[dict]) -> str:
        name = _clean(job["name"]).lower()
        if not name:
            return ""
        mapping = Mapping(name=name, origin=job.get("file", ""))
        records = job["records"]

        # job parameters live on the ROOT record's subrecords
        for rec in records:
            if rec.get("OLEType") in ("CJobDefn",):
                mapping.description = rec.get("Description", "")
                for sub in rec.subrecords:
                    if "ParamName" in sub or ("Name" in sub and
                                              "Prompt" in sub):
                        params.append({
                            "name": sub.get("ParamName", sub.get("Name", "")),
                            "scope": job["name"],
                            "class": "runtime_parameter",
                            "default": sub.get("Default", "")})

        stages: Dict[str, dict] = {}     # record Identifier -> stage info
        pins: Dict[str, dict] = {}       # pin Identifier -> {stage, dir, rec}
        for rec in records:
            ole = rec.get("OLEType", "")
            if ole in ("CCustomStage", "CTransformerStage",
                       "CAggregatorStage", "CHashedFileStage",
                       "CSeqFileStage", "CODBCStage"):
                stages[rec.get("Identifier")] = {
                    "rec": rec, "name": _clean(rec.get("Name", "stage")),
                    "type": rec.get("StageType", ole),
                    "in": (rec.get("InputPins", "") or "").split("|"),
                    "out": (rec.get("OutputPins", "") or "").split("|"),
                }
            elif ole in ("CCustomInput", "CCustomOutput", "CTrxInput",
                         "CTrxOutput", "CODBCInput", "CODBCOutput",
                         "CHashedInput", "CHashedOutput", "CSeqInput",
                         "CSeqOutput", "CAggregatorInput",
                         "CAggregatorOutput"):
                pins[rec.get("Identifier")] = {
                    "rec": rec, "dir": "in" if "Input" in ole else "out",
                    "partner": rec.get("Partner", ""),
                    "name": rec.get("Name", "")}

        if not stages:
            return ""

        # pin -> owning stage
        stage_of_pin: Dict[str, str] = {}
        for sid, st in stages.items():
            for pin in st["in"] + st["out"]:
                if pin:
                    stage_of_pin[pin] = sid

        transforms: Dict[str, Transformation] = {}
        for sid, st in stages.items():
            kind = _stage_kind(st["type"])
            rec = st["rec"]
            ports: List[Port] = []
            for pin in st["out"]:
                if pin in pins:
                    ports += _record_columns(pins[pin]["rec"])
            if kind == "io":
                is_source = bool(st["out"]) and not any(
                    p for p in st["in"] if p)
                table = ""
                for sub in rec.subrecords:
                    table = sub.get("FileName", sub.get("TableName", table)) \
                        or table
                table = table or rec.get("FileName",
                                         rec.get("TableName", st["name"]))
                if "/" in table or table.lower().endswith(
                        (".csv", ".txt", ".dat", ".ds")):
                    schema, table = "", Path(table).stem
                elif "." in table:
                    schema, _, table = table.rpartition(".")
                else:
                    schema = ""
                if is_source:
                    t = Transformation(
                        name="SRC_" + st["name"],
                        type=TransformationType.SOURCE, ports=ports,
                        properties={"table": _clean(table) or st["name"],
                                    "schema": schema,
                                    "stage_type": st["type"]})
                    sq = Transformation(
                        name="SQ_" + st["name"],
                        type=TransformationType.SOURCE_QUALIFIER,
                        ports=[Port(p.name, p.datatype) for p in ports])
                    transforms[sid] = sq
                    mapping.transformations += [t, sq]
                    mapping.links.append(Link(t.name, sq.name))
                    if not any(s.name == t.properties["table"]
                               for s in pipeline.sources):
                        pipeline.sources.append(SourceTable(
                            name=str(t.properties["table"]),
                            columns=[Port(p.name, p.datatype)
                                     for p in ports]))
                else:
                    in_ports: List[Port] = []
                    for pin in st["in"]:
                        if pin in pins:
                            in_ports += _record_columns(pins[pin]["rec"])
                    t = Transformation(
                        name="TGT_" + st["name"],
                        type=TransformationType.TARGET, ports=in_ports,
                        properties={"table": _clean(table) or st["name"],
                                    "schema": schema,
                                    "stage_type": st["type"]})
                    transforms[sid] = t
                    mapping.transformations.append(t)
                continue

            if kind == "transformer":
                t = Transformation(name=st["name"],
                                   type=TransformationType.EXPRESSION)
                constraint = ""
                for pin in st["out"]:
                    prec = pins.get(pin, {}).get("rec")
                    if prec is None:
                        continue
                    constraint = prec.get("Constraint", "") or constraint
                    for sub in prec.subrecords:
                        col, deriv = sub.get("Name", ""), \
                            sub.get("Derivation", "")
                        if not col:
                            continue
                        if not deriv or deriv.split(".")[-1] == col:
                            t.ports.append(Port(col, "string"))
                            continue
                        sql, notes = datastage_expression_to_sql(deriv)
                        if not sql:
                            sql = "NULL"     # placeholder — declared below
                            mapping.add_issue(
                                IssueSeverity.MANUAL,
                                "DS_DERIVATION_MANUAL",
                                "Transformer %s.%s derivation not "
                                "translatable — emitted as NULL placeholder"
                                % (st["name"], col),
                                detail=deriv,
                                suggestion="Port the BASIC derivation "
                                "manually.")
                        for n2 in notes:
                            mapping.add_issue(
                                IssueSeverity.WARNING, "DS_DERIVATION_NOTE",
                                "%s.%s: %s" % (st["name"], col, n2),
                                detail=deriv)
                        t.ports.append(Port(col, "string", expression=sql,
                                            direction="OUTPUT"))
                # stage variables are stateful — declare them
                for sub in st["rec"].subrecords:
                    if sub.get("Name", "").startswith("StageVar"):
                        mapping.add_issue(
                            IssueSeverity.MANUAL, "DS_STAGE_VARIABLE",
                            "Transformer %s uses stage variable %s — "
                            "row-order state needs manual porting"
                            % (st["name"], sub.get("Name")),
                            detail=sub.get("Derivation", ""))
                mapping.transformations.append(t)
                transforms[sid] = t
                if constraint:
                    # a link constraint filters INPUT rows — place the
                    # FILTER before the transformer so its columns resolve
                    sql, _n = datastage_expression_to_sql(constraint)
                    f = Transformation(
                        name=st["name"] + "_constraint",
                        type=TransformationType.FILTER,
                        properties={"condition": sql or constraint})
                    if not sql:
                        mapping.add_issue(
                            IssueSeverity.MANUAL, "DS_CONSTRAINT_MANUAL",
                            "Transformer %s link constraint not "
                            "translatable" % st["name"], detail=constraint)
                    mapping.transformations.append(f)
                    mapping.links.append(Link(f.name, t.name))
                    transforms["%s__entry" % sid] = f   # upstream enters here
                continue

            if kind == "lookup":
                table = rec.get("TableName", "")
                t = Transformation(name=st["name"],
                                   type=TransformationType.LOOKUP,
                                   ports=ports,
                                   properties={"table": table,
                                               "condition": rec.get(
                                                   "KeyExpression", "")})
            elif kind == "join":
                t = Transformation(
                    name=st["name"], type=TransformationType.JOINER,
                    ports=ports,
                    properties={"join_type": rec.get("JoinType",
                                                     "INNER").upper()})
            elif kind == "aggregate":
                group = [s.get("Name", "") for s in rec.subrecords
                         if s.get("Grouping") == "1"]
                # output-pin derivations carry the aggregate expressions
                agg_ports: List[Port] = []
                for pin in st["out"]:
                    prec = pins.get(pin, {}).get("rec")
                    if prec is None:
                        continue
                    for sub in prec.subrecords:
                        col = sub.get("Name", "")
                        deriv = sub.get("Derivation", "")
                        if not col:
                            continue
                        if col in group or not deriv:
                            agg_ports.append(Port(col, "string"))
                            continue
                        sql, _n = datastage_expression_to_sql(deriv)
                        if sql and re.match(r"COUNT\s*\(", sql, re.I):
                            sql = "COUNT(*)" if sql.upper() in (
                                "COUNT()", "COUNT( )") else sql
                        if not sql:
                            sql = "NULL"
                            mapping.add_issue(
                                IssueSeverity.MANUAL, "DS_AGG_MANUAL",
                                "Aggregator %s.%s derivation not "
                                "translatable" % (st["name"], col),
                                detail=deriv)
                        agg_ports.append(Port(col, "string", expression=sql,
                                              direction="OUTPUT"))
                t = Transformation(name=st["name"],
                                   type=TransformationType.AGGREGATOR,
                                   ports=agg_ports or ports,
                                   properties={"group_by": group})
            elif kind == "sort":
                keys = [{"port": s.get("Name", ""), "order":
                         "DESC" if s.get("SortOrder") == "1" else "ASC"}
                        for s in rec.subrecords if s.get("SortKey") == "1"]
                t = Transformation(name=st["name"],
                                   type=TransformationType.SORTER,
                                   ports=ports,
                                   properties={"sort_keys": keys})
            elif kind == "union":
                t = Transformation(name=st["name"],
                                   type=TransformationType.UNION,
                                   ports=ports, properties={"inputs": []})
            elif kind == "dedup":
                t = Transformation(name=st["name"],
                                   type=TransformationType.RANK,
                                   ports=ports,
                                   properties={"dedup": True})
            elif kind == "passthrough":
                t = Transformation(name=st["name"],
                                   type=TransformationType.EXPRESSION,
                                   ports=ports)
            else:
                t = Transformation(name=st["name"],
                                   type=TransformationType.EXPRESSION,
                                   ports=ports,
                                   properties={"unconverted_pc_type":
                                               st["type"]})
                mapping.add_issue(
                    IssueSeverity.MANUAL, "DS_STAGE_UNSUPPORTED",
                    "Stage '%s' (%s) is not supported — preserved for "
                    "manual porting" % (st["name"], st["type"]))
            mapping.transformations.append(t)
            transforms[sid] = t

        # links via pin partners: out-pin.partner == "inStageId|inPinId"
        for pid, pin in pins.items():
            if pin["dir"] != "out" or not pin["partner"]:
                continue
            to_stage = pin["partner"].split("|")[0]
            f_sid = stage_of_pin.get(pid)
            if f_sid in transforms and to_stage in transforms:
                to_t = transforms[to_stage]
                entry = transforms.get("%s__entry" % to_stage, to_t)
                mapping.links.append(
                    Link(transforms[f_sid].name, entry.name))
                if to_t.type == TransformationType.UNION:
                    to_t.properties.setdefault("inputs", []).append(
                        transforms[f_sid].name)
                if to_t.type == TransformationType.JOINER:
                    if "left" not in to_t.properties:
                        to_t.properties["left"] = transforms[f_sid].name
                    else:
                        to_t.properties.setdefault(
                            "right", transforms[f_sid].name)

        # shared container references
        for rec in records:
            if rec.get("OLEType") == "CSharedContainerRef" or \
                    "SharedContainer" in rec.get("StageType", ""):
                mapping.add_issue(
                    IssueSeverity.MANUAL, "DS_SHARED_CONTAINER",
                    "Job uses shared container '%s' — convert the container "
                    "export and inline it" % rec.get("Name", "?"))

        pipeline.mappings.append(mapping)
        return mapping.name

    # -- job sequence -> workflow DAG ---------------------------------------
    def _parse_sequence(self, job: dict, pipeline: Pipeline,
                        dags: List[dict]) -> None:
        name = job["name"]
        nodes = [{"task_key": "Start__" + name, "task": "Start",
                  "type": "start"}]
        edges: List[dict] = []
        id_to_key: Dict[str, str] = {}
        for rec in job["records"]:
            ole = rec.get("OLEType", "")
            if ole == "CJSJobActivity":
                key = _clean(rec.get("Name", rec.get("Identifier", "")))
                id_to_key[rec.get("Identifier")] = key
                nodes.append({"task_key": key, "task": key,
                              "type": "session",
                              "mapping": _clean(rec.get("JobName",
                                                        "")).lower()})
            elif ole == "CJSExecCommandActivity":
                key = _clean(rec.get("Name", ""))
                id_to_key[rec.get("Identifier")] = key
                nodes.append({"task_key": key, "task": key,
                              "type": "command",
                              "config": {"command":
                                         rec.get("Command", "")}})
            elif ole == "CJSNotificationActivity":
                key = _clean(rec.get("Name", ""))
                id_to_key[rec.get("Identifier")] = key
                nodes.append({"task_key": key, "task": key, "type": "email"})
            elif ole and ole.startswith("CJS") and rec.get("Name") and \
                    ole not in ("CJSSequence",):
                key = _clean(rec.get("Name", ""))
                id_to_key[rec.get("Identifier")] = key
                nodes.append({"task_key": key, "task": key,
                              "type": "command", "config": {"ole": ole}})
        for rec in job["records"]:
            for sub in rec.subrecords:
                if "TriggerType" in sub and "Target" in sub:
                    src = id_to_key.get(rec.get("Identifier"))
                    dst = id_to_key.get(sub["Target"], sub["Target"])
                    if not src or not dst:
                        continue
                    kind = {"0": "success", "1": "failure",
                            "2": "always"}.get(sub.get("TriggerType", "0"),
                                               "conditional")
                    edges.append({"from": src, "to": dst, "kind": kind,
                                  "condition":
                                  sub.get("Expression", "")})
        entry = {e["to"] for e in edges}
        for nd in nodes[1:]:
            if nd["task_key"] not in entry:
                edges.append({"from": "Start__" + name,
                              "to": nd["task_key"], "kind": "success",
                              "condition": ""})
        # topological order
        incoming = {nd["task_key"]: 0 for nd in nodes}
        adj: Dict[str, List[str]] = {}
        for e in edges:
            incoming[e["to"]] = incoming.get(e["to"], 0) + 1
            adj.setdefault(e["from"], []).append(e["to"])
        ready = [k for k, v in incoming.items() if v == 0]
        order = []
        while ready:
            k = ready.pop(0)
            order.append(k)
            for nxt in adj.get(k, []):
                incoming[nxt] -= 1
                if incoming[nxt] == 0:
                    ready.append(nxt)
        dags.append({"workflow": name, "nodes": nodes, "edges": edges,
                     "failure_paths": [(e["from"], e["to"]) for e in edges
                                       if e["kind"] == "failure"],
                     "execution_order": [k for k in order
                                         if not k.startswith("Start__")]})


def parse_datastage(path: str, dialect: str = "") -> Pipeline:
    return _DataStageProject().parse(path, dialect)

"""Orchestration source adapters (Command 6) — every platform -> COR.

    airflow          DAG .py files, parsed with Python's ``ast`` module
                     (DAGs, operators, sensors, TaskGroups, >>/<< chains,
                     set_upstream/downstream, chain(), pools, retries,
                     SLAs, schedules; XCom use surfaced as variables)
    adf / synapse_pipelines / fabric
                     pipeline JSON (activities, dependsOn conditions,
                     policy retry/timeout, IfCondition/ForEach/Until
                     flattened, Wait, Validation sensors, ExecutePipeline)
                     + trigger JSON (ScheduleTrigger/TumblingWindow/event)
                     + linked services inventoried as connections
    stepfunctions    Amazon States Language (Task/Choice/Parallel/Map/
                     Wait/Pass/Succeed/Fail, Retry, Catch)
    glue_workflow    ``aws glue get-workflow --include-graph`` JSON
    controlm         Control-M Automation API JSON (folders, jobs,
                     When/calendars, events wait/add, notifications,
                     rerun limits). Legacy XML DEFTABLE exports are
                     declared unsupported with conversion guidance.
    autosys          JIL text (insert_job blocks — JIL is a line-based
                     attribute format; parsed as key:value blocks, with
                     condition expressions parsed structurally)
    idmc_taskflow    IDMC taskflow JSON export (steps, decision/parallel
                     paths, schedules)
    dbtcloud         dbt Cloud job JSON (execute_steps, schedule cron)
    cron             crontab text (entries -> scheduled command workflows)
    powercenter / ssis / datastage / talend / abinitio
                     bridged from the existing project parsers' WORKFLOW
                     DAG CIR (pipeline.metadata["workflow_dags"]) — one
                     upgrade path, no re-parsing

Anything unknown is preserved as a declared issue with the original
payload — never dropped.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .cor import (
    COR, Dependency, Notification, RetryPolicy, Schedule, Task, Workflow,
    normalize_cron,
)

ORCH_PLATFORMS = ("airflow", "adf", "synapse_pipelines", "fabric",
                  "stepfunctions", "glue_workflow", "controlm", "autosys",
                  "idmc_taskflow", "dbtcloud", "cron",
                  "powercenter", "ssis", "datastage", "talend", "abinitio",
                  "sap")


def _clean(name: str) -> str:
    return re.sub(r"\W+", "_", str(name)).strip("_")


# ===========================================================================
# platform detection
# ===========================================================================

def detect_orchestration_platform(path: str) -> dict:
    """Score-based platform detection over a file or directory."""
    p = Path(path)
    files = [p] if p.is_file() else sorted(
        f for f in p.rglob("*") if f.is_file())[:400]
    scores: Dict[str, int] = {}
    reasons: Dict[str, List[str]] = {}

    def add(fmt: str, pts: int, why: str) -> None:
        scores[fmt] = scores.get(fmt, 0) + pts
        reasons.setdefault(fmt, []).append(why)

    for f in files:
        try:
            head = f.read_text(errors="replace")[:65536]
        except OSError:
            continue
        suf = f.suffix.lower()
        if suf == ".py" and ("from airflow" in head or
                             "import airflow" in head):
            add("airflow", 30, "airflow import in %s" % f.name)
        elif suf == ".json":
            try:
                doc = json.loads(head if len(head) < 65536
                                 else f.read_text(errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(doc, dict):
                if "States" in doc and "StartAt" in doc:
                    add("stepfunctions", 30, "ASL StartAt/States in %s"
                        % f.name)
                elif "Workflow" in doc and "Graph" in str(
                        doc.get("Workflow", {})):
                    add("glue_workflow", 30, "Glue workflow graph in %s"
                        % f.name)
                elif "execute_steps" in doc:
                    add("dbtcloud", 30, "dbt Cloud job in %s" % f.name)
                elif "taskflow" in doc or doc.get("type") == "taskflow":
                    add("idmc_taskflow", 30, "IDMC taskflow in %s" % f.name)
                elif any(isinstance(v, dict) and str(v.get("Type", ""))
                         .startswith(("Job:", "Folder", "SimpleFolder"))
                         for v in doc.values()):
                    add("controlm", 30, "Control-M job objects in %s"
                        % f.name)
                elif "properties" in doc and "activities" in str(
                        doc.get("properties", {}))[:2000]:
                    marker = json.dumps(doc)[:4000].lower()
                    fmt = "fabric" if "fabric" in marker or \
                        "workspaceid" in marker else "adf"
                    add(fmt, 28, "pipeline activities in %s" % f.name)
                elif str(doc.get("properties", {}).get("type", "")) \
                        .endswith("Trigger"):
                    add("adf", 10, "trigger definition in %s" % f.name)
        elif suf == ".jil" or "insert_job:" in head:
            add("autosys", 30, "JIL insert_job in %s" % f.name)
        elif suf == ".xml" and "<WORKFLOW " in head:
            add("powercenter", 25, "PowerCenter WORKFLOW in %s" % f.name)
        elif suf == ".dsx" and "BEGIN DSJOB" in head:
            add("datastage", 22, "DataStage DSX in %s" % f.name)
        elif suf == ".item" and "ProcessType" in head:
            add("talend", 22, "Talend job in %s" % f.name)
        elif suf == ".dtsx":
            add("ssis", 22, "SSIS package %s" % f.name)
        elif f.name in ("crontab", "crontab.txt") or suf == ".cron" or \
                re.search(r"^\s*[\d*@]\S*\s+\S+\s+\S+\s+\S+\s+\S+\s+\S",
                          head, re.M):
            if not head.lstrip().startswith("<"):
                add("cron", 8 if suf not in (".cron",) else 20,
                    "cron entries in %s" % f.name)
    if not scores:
        return {"detected_platform": "", "confidence": 0, "reasons": [],
                "alternatives": []}
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    top, pts = ranked[0]
    conf = min(99, 40 + pts)
    return {"detected_platform": top, "confidence": conf,
            "reasons": reasons[top][:8],
            "alternatives": [{"platform": k, "score": v}
                             for k, v in ranked[1:4]]}


# ===========================================================================
# Airflow (ast — never regex-only)
# ===========================================================================

_AF_TYPE = [
    ("branch", "choice"), ("sensor", "sensor"),
    ("triggerdagrun", "subworkflow"), ("emptyoperator", "dummy"),
    ("dummyoperator", "dummy"), ("emailoperator", "email"),
    ("bashoperator", "command"), ("pythonoperator", "command"),
    ("sparksubmit", "notebook"), ("databricks", "notebook"),
    ("notebook", "notebook"), ("sql", "sql"), ("bigquery", "sql"),
    ("snowflake", "sql"), ("redshift", "sql"), ("postgres", "sql"),
    ("copy", "copy"), ("s3", "copy"), ("gcstos3", "copy"),
    ("dbtcloudrunjob", "subworkflow"), ("kubernetespod", "command"),
]


def _af_task_type(cls: str) -> str:
    c = cls.lower()
    for key, t in _AF_TYPE:
        if key in c:
            return t
    return "unknown"


def _const(node) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        return {_const(k): _const(v) for k, v in
                zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_const(e) for e in node.elts]
    if isinstance(node, ast.Call):
        fn = _call_name(node)
        if fn == "timedelta":
            kw = {k.arg: _const(k.value) for k in node.keywords}
            return int(kw.get("days", 0) or 0) * 86400 + \
                int(kw.get("hours", 0) or 0) * 3600 + \
                int(kw.get("minutes", 0) or 0) * 60 + \
                int(kw.get("seconds", 0) or 0)
    if isinstance(node, ast.Name):
        return "$" + node.id
    return None


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def parse_airflow(path: str) -> COR:
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.rglob("*.py"))
    cor = COR(name=_clean(p.stem), source_platform="airflow")
    for f in files:
        try:
            tree = ast.parse(f.read_text(errors="replace"))
        except (SyntaxError, OSError) as e:
            cor.issues.append({"severity": "ERROR", "code": "AF_PARSE",
                               "message": "%s is not parseable Python: %s"
                               % (f.name, e)})
            continue
        if not any(isinstance(n, (ast.Import, ast.ImportFrom)) and
                   "airflow" in ast.dump(n) for n in ast.walk(tree)):
            continue
        _parse_airflow_module(f, tree, cor)
    if not cor.workflows and not cor.issues:
        raise FileNotFoundError("No Airflow DAG files under %s" % path)
    return cor


def _parse_airflow_module(f: Path, tree: ast.Module, cor: COR) -> None:
    wf = Workflow(name=_clean(f.stem), platform="airflow",
                  metadata={"file": f.name})
    default_args: Dict[str, object] = {}
    module_dicts: Dict[str, dict] = {}     # module-level dict literals
    var_task: Dict[str, str] = {}          # python var -> task key
    group_of_ctx: List[str] = []           # TaskGroup nesting

    def dag_call(call: ast.Call) -> None:
        args = {k.arg: k.value for k in call.keywords if k.arg}
        if call.args and isinstance(call.args[0], ast.Constant):
            wf.name = _clean(str(call.args[0].value))
        if "dag_id" in args:
            wf.name = _clean(str(_const(args["dag_id"])))
        sched = args.get("schedule") or args.get("schedule_interval")
        if sched is not None:
            v = _const(sched)
            if isinstance(v, str) and v:
                wf.schedules.append(Schedule(
                    kind="cron", cron=normalize_cron(v), raw=v))
            elif isinstance(v, int):
                wf.schedules.append(Schedule(kind="interval",
                                             interval_seconds=v,
                                             raw=str(v)))
            elif v is None:
                wf.schedules.append(Schedule(kind="manual", raw="None"))
        if "default_args" in args:
            v = _const(args["default_args"])
            if isinstance(v, str) and v.startswith("$"):
                v = module_dicts.get(v[1:])       # default_args=<var>
            if isinstance(v, dict):
                default_args.update(v)
        for key in ("description",):
            if key in args:
                wf.description = str(_const(args[key]) or "")

    def task_from_call(call: ast.Call, varname: str = "") -> Optional[str]:
        cls = _call_name(call)
        if not (cls.endswith("Operator") or cls.endswith("Sensor") or
                cls in ("TaskGroup",)):
            return None
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        if cls == "TaskGroup":
            return None
        if "task_id" not in kw:
            return None
        key = _clean(str(_const(kw["task_id"])))
        ttype = _af_task_type(cls)
        action: Dict[str, object] = {"operator": cls}
        for src, tgt in (("bash_command", "command"), ("sql", "sql"),
                         ("python_callable", "callable"),
                         ("notebook_path", "notebook"),
                         ("trigger_dag_id", "workflow"),
                         ("job_id", "job"), ("to", "to"),
                         ("external_dag_id", "workflow")):
            if src in kw:
                v = _const(kw[src])
                action[tgt] = str(v)[:500] if v is not None else ""
        retries = _const(kw["retries"]) if "retries" in kw \
            else default_args.get("retries", 0)
        delay = _const(kw["retry_delay"]) if "retry_delay" in kw \
            else default_args.get("retry_delay", 0)
        timeout = _const(kw["execution_timeout"]) \
            if "execution_timeout" in kw else 0
        sla = _const(kw["sla"]) if "sla" in kw else default_args.get("sla", 0)
        conn = ""
        for k in kw:
            if k.endswith("conn_id"):
                conn = str(_const(kw[k]) or "")
        t = Task(key=key, name=key, type=ttype, action=action,
                 retry=RetryPolicy(int(retries or 0), int(delay or 0)),
                 timeout_seconds=int(timeout or 0),
                 sla_seconds=int(sla or 0), connection=conn,
                 parallel_group="/".join(group_of_ctx),
                 original={"operator": cls})
        if "pool" in kw:
            t.resources["pool"] = str(_const(kw["pool"]) or "")
        src_txt = ast.dump(call)
        if "xcom_pull" in src_txt or "xcom_push" in src_txt:
            t.variables_used.append("xcom")
        if ttype == "unknown":
            wf.add_issue("MANUAL", "AF_OPERATOR_UNSUPPORTED",
                         "Operator %s (task %s) has no direct target "
                         "equivalent — preserved for manual porting"
                         % (cls, key))
        wf.tasks.append(t)
        if varname:
            var_task[varname] = key
        return key

    def resolve(node) -> List[str]:
        """>>-chain operand -> task keys (vars, lists, direct calls)."""
        if isinstance(node, ast.Name):
            return [var_task[node.id]] if node.id in var_task else []
        if isinstance(node, (ast.List, ast.Tuple)):
            return [k for e in node.elts for k in resolve(e)]
        if isinstance(node, ast.Call):
            key = task_from_call(node)
            return [key] if key else []
        return []

    def dep(f_keys: List[str], t_keys: List[str]) -> None:
        for a in f_keys:
            for b in t_keys:
                wf.dependencies.append(Dependency(a, b))

    def walk(body) -> None:
        for node in body:
            if isinstance(node, ast.With):
                entered = False
                for item in node.items:
                    ce = item.context_expr
                    if isinstance(ce, ast.Call):
                        nm = _call_name(ce)
                        if nm == "DAG":
                            dag_call(ce)
                        elif nm == "TaskGroup":
                            gname = ""
                            if ce.args and isinstance(ce.args[0],
                                                      ast.Constant):
                                gname = str(ce.args[0].value)
                            for k in ce.keywords:
                                if k.arg == "group_id":
                                    gname = str(_const(k.value))
                            group_of_ctx.append(_clean(gname))
                            entered = True
                walk(node.body)
                if entered:
                    group_of_ctx.pop()
            elif isinstance(node, ast.Assign) and \
                    isinstance(node.value, ast.Dict) and \
                    len(node.targets) == 1 and \
                    isinstance(node.targets[0], ast.Name):
                v = _const(node.value)
                if isinstance(v, dict):
                    module_dicts[node.targets[0].id] = v
            elif isinstance(node, ast.Assign) and \
                    isinstance(node.value, ast.Call):
                nm = _call_name(node.value)
                if nm == "DAG":
                    dag_call(node.value)
                elif len(node.targets) == 1 and \
                        isinstance(node.targets[0], ast.Name):
                    task_from_call(node.value, node.targets[0].id)
            elif isinstance(node, ast.Expr):
                v = node.value
                if isinstance(v, ast.BinOp):
                    _chain_binop(v)
                elif isinstance(v, ast.Call):
                    nm = _call_name(v)
                    if nm == "chain":
                        ops = [resolve(a) for a in v.args]
                        for a, b in zip(ops, ops[1:]):
                            dep(a, b)
                    elif nm in ("set_downstream", "set_upstream") and \
                            isinstance(v.func, ast.Attribute):
                        left = resolve(v.func.value)
                        right = [k for a in v.args for k in resolve(a)]
                        if nm == "set_downstream":
                            dep(left, right)
                        else:
                            dep(right, left)
                    else:
                        task_from_call(v)
            elif isinstance(node, (ast.FunctionDef,
                                   ast.AsyncFunctionDef)):
                walk(node.body)
            elif isinstance(node, (ast.If, ast.For, ast.Try)):
                walk(getattr(node, "body", []))
                walk(getattr(node, "orelse", []))

    def _chain_binop(b: ast.BinOp) -> None:
        """a >> b >> [c, d] >> e  — flatten same-operator chains into
        ordered operand groups, then link successive groups."""
        if not isinstance(b.op, (ast.RShift, ast.LShift)):
            return
        groups: List[List[str]] = []

        def collect(n) -> None:
            if isinstance(n, ast.BinOp) and isinstance(n.op, type(b.op)):
                collect(n.left)
                collect(n.right)
            else:
                groups.append(resolve(n))

        collect(b)
        if isinstance(b.op, ast.LShift):          # a << b == b >> a
            groups.reverse()
        for g1, g2 in zip(groups, groups[1:]):
            dep(g1, g2)

    walk(tree.body)
    # de-duplicate dependencies from chain recursion
    seen = set()
    uniq = []
    for d in wf.dependencies:
        sig = (d.from_task, d.to_task, d.kind)
        if sig not in seen:
            seen.add(sig)
            uniq.append(d)
    wf.dependencies = uniq
    if default_args.get("email") and (
            default_args.get("email_on_failure", True)):
        tgt = default_args["email"]
        wf.notifications.append(Notification(
            on="failure", channel="email",
            target=", ".join(tgt) if isinstance(tgt, list) else str(tgt)))
    if wf.tasks:
        cor.workflows.append(wf)


# ===========================================================================
# ADF / Synapse Pipelines / Fabric (one JSON family, one parser)
# ===========================================================================

_ADF_TYPE = {"copy": "copy", "executepipeline": "subworkflow",
             "lookup": "sql", "script": "sql",
             "sqlserverstoredprocedure": "sql", "storedprocedure": "sql",
             "databricksnotebook": "notebook", "synapsenotebook": "notebook",
             "notebook": "notebook", "wait": "wait",
             "ifcondition": "choice", "switch": "choice",
             "foreach": "loop", "until": "loop",
             "executedataflow": "mapping", "dataflow": "mapping",
             "web": "command", "webhook": "approval", "custom": "command",
             "azurefunctionactivity": "command", "setvariable": "dummy",
             "appendvariable": "dummy", "validation": "sensor",
             "getmetadata": "sensor", "delete": "command",
             "trydbtjob": "subworkflow"}


def _adf_timeout(v: str) -> int:
    m = re.match(r"(?:(\d+)\.)?(\d+):(\d+):(\d+)", str(v or ""))
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_adf(path: str, platform: str = "adf") -> COR:
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.rglob("*.json"))
    cor = COR(name=_clean(p.stem), source_platform=platform)
    triggers: List[dict] = []
    linked: List[dict] = []
    for f in files:
        try:
            doc = json.loads(f.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(doc, dict):
            continue
        props = doc.get("properties", {})
        if isinstance(props, dict) and "activities" in props:
            wf = _parse_adf_pipeline(doc, platform)
            cor.workflows.append(wf)
        elif str(props.get("type", "")).endswith("Trigger"):
            triggers.append(doc)
        elif str(props.get("type", "")) in ("AzureSqlDatabase", "AzureBlobFS",
                                            "AzureBlobStorage", "Snowflake",
                                            "AzureDatabricks", "Oracle",
                                            "SqlServer") or \
                "typeProperties" in props and "connectVia" in str(props):
            linked.append({"name": doc.get("name", f.stem),
                           "type": props.get("type", "")})
    if not cor.workflows:
        raise FileNotFoundError("No %s pipeline JSON under %s"
                                % (platform, path))
    # bind triggers to their pipelines
    for trg in triggers:
        props = trg.get("properties", {})
        rec = props.get("typeProperties", {}).get("recurrence", {})
        pipes = [str(pl.get("pipelineReference", {}).get("referenceName",
                                                         ""))
                 for pl in props.get("pipelines", [])]
        for wf in cor.workflows:
            if wf.name in [_clean(x) for x in pipes] or not pipes:
                if props.get("type") == "ScheduleTrigger" and rec:
                    freq = str(rec.get("frequency", "")).lower()
                    iv = int(rec.get("interval", 1) or 1)
                    secs = {"minute": 60, "hour": 3600, "day": 86400,
                            "week": 604800}.get(freq, 0) * iv
                    wf.schedules.append(Schedule(
                        kind="interval", interval_seconds=secs,
                        start_date=str(rec.get("startTime", "")),
                        timezone=str(rec.get("timeZone", "")),
                        raw=json.dumps(rec)[:200]))
                elif "Event" in str(props.get("type", "")):
                    wf.schedules.append(Schedule(
                        kind="event",
                        event=str(props.get("typeProperties", {}))[:150],
                        raw=str(props.get("type"))))
                elif props.get("type") == "TumblingWindowTrigger":
                    tp = props.get("typeProperties", {})
                    secs = {"Minute": 60, "Hour": 3600}.get(
                        str(tp.get("frequency", "")), 3600) * \
                        int(tp.get("interval", 1) or 1)
                    wf.schedules.append(Schedule(
                        kind="interval", interval_seconds=secs,
                        raw="TumblingWindow"))
    if linked:
        cor.metadata["linked_services"] = linked
        for wf in cor.workflows:
            wf.connections = linked
    return cor


def _parse_adf_pipeline(doc: dict, platform: str) -> Workflow:
    props = doc.get("properties", {})
    wf = Workflow(name=_clean(doc.get("name", "pipeline")),
                  platform=platform,
                  description=str(props.get("description", "")))
    for pname, pdef in (props.get("parameters") or {}).items():
        wf.variables.append({"name": pname, "scope": "parameter",
                             "value": str((pdef or {}).get(
                                 "defaultValue", "")),
                             "secret": False})
    for vname, vdef in (props.get("variables") or {}).items():
        wf.variables.append({"name": vname, "scope": "variable",
                             "value": str((vdef or {}).get(
                                 "defaultValue", "")), "secret": False})

    def add_activity(act: dict, group: str = "",
                     parent_key: str = "", parent_kind: str = "success",
                     condition: str = "") -> str:
        name = _clean(act.get("name", "activity"))
        atype = _ADF_TYPE.get(str(act.get("type", "")).lower(), "unknown")
        pol = act.get("policy", {}) or {}
        tp = act.get("typeProperties", {}) or {}
        action: Dict[str, object] = {"activity_type": act.get("type", "")}
        if atype == "subworkflow":
            action["workflow"] = _clean(str(
                tp.get("pipeline", {}).get("referenceName", "")))
        if atype == "sql":
            action["sql"] = str(tp.get("scripts", "")
                                or tp.get("storedProcedureName", "")
                                or tp.get("query", ""))[:400]
        if atype == "notebook":
            action["notebook"] = str(tp.get("notebookPath", ""))[:200]
        if atype == "wait":
            action["seconds"] = tp.get("waitTimeInSeconds", 0)
        secrets = [m.group(1) for m in re.finditer(
            r'"secretName"\s*:\s*"([^"]+)"', json.dumps(act))]
        t = Task(key=name, name=act.get("name", name), type=atype,
                 action=action,
                 retry=RetryPolicy(int(pol.get("retry", 0) or 0),
                                   int(pol.get("retryIntervalInSeconds",
                                               0) or 0)),
                 timeout_seconds=_adf_timeout(pol.get("timeout", "")),
                 parallel_group=group, secrets=secrets,
                 condition=condition,
                 original={"type": act.get("type", "")})
        if atype == "unknown":
            wf.add_issue("MANUAL", "ADF_ACTIVITY_UNSUPPORTED",
                         "Activity %s (%s) has no direct equivalent — "
                         "preserved for manual porting"
                         % (name, act.get("type")))
        wf.tasks.append(t)
        for dep in act.get("dependsOn", []) or []:
            conds = dep.get("dependencyConditions", ["Succeeded"])
            kind = ("failure" if "Failed" in conds else
                    "always" if "Completed" in conds or "Skipped" in conds
                    else "success")
            wf.dependencies.append(Dependency(
                _clean(dep.get("activity", "")), name, kind))
        if parent_key:
            wf.dependencies.append(Dependency(parent_key, name,
                                              parent_kind, condition))
        # containers: IfCondition / ForEach / Until nest activities
        if atype == "choice":
            expr = str(tp.get("expression", {}).get("value", ""))
            t.condition = expr
            for sub in tp.get("ifTrueActivities", []) or []:
                add_activity(sub, group, name, "conditional",
                             "true: %s" % expr)
            for sub in tp.get("ifFalseActivities", []) or []:
                add_activity(sub, group, name, "conditional",
                             "false: %s" % expr)
        if atype == "loop":
            t.loop = {"items": str(tp.get("items", {}).get("value", ""))
                      [:200],
                      "parallel": not tp.get("isSequential", False),
                      "batch_count": tp.get("batchCount", 0)}
            for sub in tp.get("activities", []) or []:
                add_activity(sub, group or name, name)
        return name

    for act in props.get("activities", []) or []:
        add_activity(act)
    return wf


# ===========================================================================
# AWS Step Functions (ASL)
# ===========================================================================

def parse_stepfunctions(path: str) -> COR:
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.rglob("*.json"))
    cor = COR(name=_clean(p.stem), source_platform="stepfunctions")
    for f in files:
        try:
            doc = json.loads(f.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(doc, dict) and "States" in doc and "StartAt" in doc:
            wf = Workflow(name=_clean(f.stem), platform="stepfunctions",
                          description=str(doc.get("Comment", "")))
            _asl_states(doc, wf, "")
            cor.workflows.append(wf)
    if not cor.workflows:
        raise FileNotFoundError("No Step Functions ASL under %s" % path)
    return cor


def _asl_states(machine: dict, wf: Workflow, group: str) -> None:
    states = machine.get("States", {})
    for name, st in states.items():
        key = _clean(name)
        stype = str(st.get("Type", ""))
        ttype = {"Task": "command", "Choice": "choice",
                 "Parallel": "parallel", "Map": "loop", "Wait": "wait",
                 "Pass": "dummy", "Succeed": "dummy",
                 "Fail": "dummy"}.get(stype, "unknown")
        action: Dict[str, object] = {"asl_type": stype}
        if st.get("Resource"):
            action["resource"] = str(st["Resource"])
            if ":glue:" in action["resource"] or "glue" in str(
                    st.get("Parameters", {})).lower():
                ttype = "mapping"
            if ":sns:" in action["resource"]:
                ttype = "email"
        retry = RetryPolicy()
        for r in st.get("Retry", []) or []:
            retry = RetryPolicy(int(r.get("MaxAttempts", 3)),
                                int(r.get("IntervalSeconds", 1)),
                                float(r.get("BackoffRate", 2.0)))
        t = Task(key=key, name=name, type=ttype, action=action,
                 retry=retry,
                 timeout_seconds=int(st.get("TimeoutSeconds", 0) or 0),
                 parallel_group=group, original={"state": stype})
        if stype == "Wait":
            t.action["seconds"] = st.get("Seconds", 0)
        wf.tasks.append(t)
        if st.get("Next"):
            wf.dependencies.append(Dependency(key, _clean(st["Next"])))
        for c in st.get("Catch", []) or []:
            wf.dependencies.append(Dependency(
                key, _clean(c.get("Next", "")), "failure",
                ",".join(c.get("ErrorEquals", []))))
        if stype == "Choice":
            for ch in st.get("Choices", []) or []:
                cond = {k: v for k, v in ch.items() if k != "Next"}
                wf.dependencies.append(Dependency(
                    key, _clean(ch.get("Next", "")), "conditional",
                    json.dumps(cond)[:200]))
            if st.get("Default"):
                wf.dependencies.append(Dependency(
                    key, _clean(st["Default"]), "conditional", "default"))
        if stype == "Parallel":
            for i, br in enumerate(st.get("Branches", []) or []):
                _asl_states(br, wf, group="%s_branch%d" % (key, i))
                if br.get("StartAt"):
                    wf.dependencies.append(Dependency(
                        key, _clean(br["StartAt"])))
        if stype == "Map":
            it = st.get("Iterator") or st.get("ItemProcessor") or {}
            t.loop = {"items": str(st.get("ItemsPath", ""))[:100],
                      "parallel": True,
                      "max_concurrency": st.get("MaxConcurrency", 0)}
            if it:
                _asl_states(it, wf, group=key + "_map")
                if it.get("StartAt"):
                    wf.dependencies.append(Dependency(
                        key, _clean(it["StartAt"])))


# ===========================================================================
# AWS Glue Workflows
# ===========================================================================

def parse_glue_workflow(path: str) -> COR:
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.rglob("*.json"))
    cor = COR(name=_clean(p.stem), source_platform="glue_workflow")
    for f in files:
        try:
            doc = json.loads(f.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        w = doc.get("Workflow") if isinstance(doc, dict) else None
        if not isinstance(w, dict) or "Graph" not in w:
            continue
        wf = Workflow(name=_clean(w.get("Name", f.stem)),
                      platform="glue_workflow")
        graph = w.get("Graph", {})
        node_by_id: Dict[str, dict] = {}
        for n in graph.get("Nodes", []) or []:
            node_by_id[str(n.get("UniqueId", n.get("Name")))] = n
            key = _clean(n.get("Name", ""))
            ntype = str(n.get("Type", ""))
            if ntype == "TRIGGER":
                trg = (n.get("TriggerDetails", {}) or {}).get("Trigger", {})
                if str(trg.get("Type")) == "SCHEDULED" or \
                        trg.get("Schedule"):
                    cron = re.sub(r"^cron\((.+)\)$", r"\1",
                                  str(trg.get("Schedule", "")))
                    wf.schedules.append(Schedule(kind="cron", cron=cron,
                                                 raw=str(trg.get(
                                                     "Schedule", ""))))
                elif str(trg.get("Type")) == "ON_DEMAND":
                    wf.schedules.append(Schedule(kind="manual",
                                                 raw="ON_DEMAND"))
                continue
            ttype = "mapping" if ntype == "JOB" else \
                "sensor" if ntype == "CRAWLER" else "unknown"
            wf.tasks.append(Task(key=key, name=n.get("Name", key),
                                 type=ttype,
                                 action={"glue_type": ntype},
                                 original={"type": ntype}))
        for e in graph.get("Edges", []) or []:
            src = node_by_id.get(str(e.get("SourceId")))
            dst = node_by_id.get(str(e.get("DestinationId")))
            if not src or not dst:
                continue
            if str(src.get("Type")) == "TRIGGER":
                trg = (src.get("TriggerDetails", {}) or {}).get(
                    "Trigger", {})
                for cond in (trg.get("Predicate", {}) or {}).get(
                        "Conditions", []) or []:
                    kind = "failure" if str(cond.get("State")) == "FAILED" \
                        else "success"
                    wf.dependencies.append(Dependency(
                        _clean(cond.get("JobName",
                                        cond.get("CrawlerName", ""))),
                        _clean(dst.get("Name", "")), kind))
            else:
                wf.dependencies.append(Dependency(
                    _clean(src.get("Name", "")),
                    _clean(dst.get("Name", ""))))
        if wf.tasks:
            cor.workflows.append(wf)
    if not cor.workflows:
        raise FileNotFoundError("No Glue workflow JSON under %s" % path)
    return cor


# ===========================================================================
# Control-M (Automation API JSON)
# ===========================================================================

def parse_controlm(path: str) -> COR:
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.rglob("*.json"))
    cor = COR(name=_clean(p.stem), source_platform="controlm")
    for f in files:
        if f.suffix.lower() == ".xml":
            cor.issues.append({
                "severity": "ERROR", "code": "CTM_XML_UNSUPPORTED",
                "message": "%s looks like a legacy DEFTABLE XML export"
                           % f.name,
                "suggestion": "Export with the Control-M Automation API "
                              "(deploy jobs::get) as JSON and re-run."})
            continue
        try:
            doc = json.loads(f.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(doc, dict):
            continue
        for folder, fdef in doc.items():
            if not isinstance(fdef, dict) or not str(
                    fdef.get("Type", "")).startswith(
                        ("Folder", "SimpleFolder")):
                continue
            wf = Workflow(name=_clean(folder), platform="controlm")
            events_added: Dict[str, str] = {}   # event -> producing job
            waits: List[Tuple[str, str]] = []   # (job, event)
            for jname, jdef in fdef.items():
                if not isinstance(jdef, dict) or not str(
                        jdef.get("Type", "")).startswith("Job"):
                    continue
                key = _clean(jname)
                jtype = str(jdef.get("Type", ""))
                ttype = {"Job:Command": "command", "Job:Script": "command",
                         "Job:EmbeddedScript": "command",
                         "Job:Database:SQLScript": "sql",
                         "Job:Database": "sql", "Job:FileTransfer": "copy",
                         "Job:Dummy": "dummy",
                         "Job:SLAManagement": "dummy"}.get(jtype, "unknown")
                action: Dict[str, object] = {"controlm_type": jtype}
                for k in ("Command", "Script", "FilePath"):
                    if k in jdef:
                        action[k.lower()] = str(jdef[k])[:300]
                t = Task(key=key, name=jname, type=ttype, action=action,
                         original={"type": jtype})
                if "RunAs" in jdef:
                    t.resources["run_as"] = str(jdef["RunAs"])
                if "Host" in jdef:
                    t.resources["machine"] = str(jdef["Host"])
                rl = jdef.get("RerunLimit", {})
                if isinstance(rl, dict) and rl.get("Times"):
                    t.retry = RetryPolicy(int(rl["Times"]))
                when = jdef.get("When", {}) or {}
                if when.get("Schedule") or when.get("Months") or \
                        when.get("WeekDays"):
                    wf.schedules.append(Schedule(
                        kind="calendar",
                        calendar=str(when.get("Schedule", "") or
                                     when.get("Calendar", "")),
                        raw=json.dumps(when)[:200]))
                for k, v in jdef.items():
                    if not isinstance(v, dict):
                        continue
                    if v.get("Type") == "WaitForEvents":
                        for ev in v.get("Events", []) or []:
                            waits.append((key, str(ev.get("Event", ""))))
                    if v.get("Type") == "AddEvents":
                        for ev in v.get("Events", []) or []:
                            events_added[str(ev.get("Event", ""))] = key
                    if v.get("Type") == "Notification" or \
                            k.startswith("Notify"):
                        wf.notifications.append(Notification(
                            on="failure",
                            channel="email" if "Mail" in json.dumps(v)
                            else "alert",
                            target=str(v.get("To",
                                             v.get("Destination", "")))))
                if ttype == "unknown":
                    wf.add_issue("MANUAL", "CTM_JOB_UNSUPPORTED",
                                 "Control-M job %s (%s) needs manual "
                                 "porting" % (jname, jtype))
                wf.tasks.append(t)
            for job, event in waits:
                producer = events_added.get(event)
                if producer:
                    wf.dependencies.append(Dependency(producer, job,
                                                      "event", event))
                else:
                    wf.add_issue("WARNING", "CTM_EVENT_EXTERNAL",
                                 "Job %s waits for event '%s' produced "
                                 "outside this folder" % (job, event))
            if wf.tasks:
                cor.workflows.append(wf)
    if not cor.workflows and not cor.issues:
        raise FileNotFoundError("No Control-M JSON under %s" % path)
    return cor


# ===========================================================================
# AutoSys (JIL)
# ===========================================================================

_JIL_COND_RE = re.compile(r"([sfdtn])\s*\(\s*([^)]+?)\s*\)", re.I)


def parse_autosys(path: str) -> COR:
    p = Path(path)
    files = [p] if p.is_file() else sorted(
        list(p.rglob("*.jil")) + list(p.rglob("*.txt")))
    cor = COR(name=_clean(p.stem), source_platform="autosys")
    jobs: List[dict] = []
    for f in files:
        text = f.read_text(errors="replace")
        if "insert_job" not in text:
            continue
        cur: Dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("/*") or line.startswith("#"):
                continue
            m = re.match(r"insert_job\s*:\s*(\S+)", line)
            if m:
                if cur:
                    jobs.append(cur)
                cur = {"name": m.group(1)}
                rest = line[m.end():]
                m2 = re.search(r"job_type\s*:\s*(\w+)", rest)
                if m2:
                    cur["job_type"] = m2.group(1)
                continue
            m = re.match(r"(\w+)\s*:\s*(.*)$", line)
            if m and cur:
                cur[m.group(1)] = m.group(2).strip().strip('"')
        if cur:
            jobs.append(cur)
    if not jobs:
        raise FileNotFoundError("No AutoSys JIL jobs under %s" % path)

    wf = Workflow(name=cor.name or "autosys_jobs", platform="autosys")
    boxes = {j["name"] for j in jobs if j.get("job_type", "c") == "b"}
    for j in jobs:
        key = _clean(j["name"])
        jtype = j.get("job_type", "c")
        ttype = "parallel" if jtype == "b" else \
            "copy" if jtype == "f" else "command"
        t = Task(key=key, name=j["name"], type=ttype,
                 action={"command": j.get("command", "")[:300]}
                 if j.get("command") else {},
                 parallel_group=_clean(j.get("box_name", "")),
                 original={"job_type": jtype})
        if j.get("machine"):
            t.resources["machine"] = j["machine"]
        if j.get("n_retrys"):
            t.retry = RetryPolicy(int(j["n_retrys"] or 0))
        if j.get("term_run_time"):
            t.timeout_seconds = int(j["term_run_time"] or 0) * 60
        if j.get("max_run_alarm"):
            t.sla_seconds = int(j["max_run_alarm"] or 0) * 60
        if j.get("alarm_if_fail") in ("1", "y", "yes"):
            wf.notifications.append(Notification(
                on="failure", channel="alert", target=j["name"]))
        if j.get("start_times") or j.get("date_conditions"):
            wf.schedules.append(Schedule(
                kind="calendar",
                calendar=j.get("run_calendar", ""),
                raw="start_times=%s days=%s" % (
                    j.get("start_times", ""),
                    j.get("days_of_week", j.get("run_calendar", "")))))
        for m in _JIL_COND_RE.finditer(j.get("condition", "")):
            kind = {"s": "success", "f": "failure", "d": "always",
                    "t": "always", "n": "always"}[m.group(1).lower()]
            dep_job = _clean(m.group(2).split(",")[0])
            wf.dependencies.append(Dependency(dep_job, key, kind))
        if j.get("box_name"):
            wf.dependencies.append(Dependency(_clean(j["box_name"]), key))
        wf.tasks.append(t)
    for b in boxes:
        pass    # boxes already tasks of type parallel
    cor.workflows.append(wf)
    return cor


# ===========================================================================
# IDMC taskflows / dbt Cloud jobs / cron
# ===========================================================================

def parse_idmc_taskflow(path: str) -> COR:
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.rglob("*.json"))
    cor = COR(name=_clean(p.stem), source_platform="idmc_taskflow")
    for f in files:
        try:
            doc = json.loads(f.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        body = doc.get("taskflow", doc) if isinstance(doc, dict) else None
        if not isinstance(body, dict):
            continue
        steps = body.get("steps") or body.get("tasks") or []
        if not steps:
            continue
        wf = Workflow(name=_clean(doc.get("name", f.stem)),
                      platform="idmc_taskflow",
                      description=str(doc.get("description", "")))
        sched = doc.get("schedule") or body.get("schedule")
        if isinstance(sched, dict) and sched.get("cron"):
            wf.schedules.append(Schedule(kind="cron",
                                         cron=str(sched["cron"]),
                                         raw=json.dumps(sched)[:150]))
        prev = ""
        for st in steps:
            key = _clean(st.get("name", "step"))
            stype = str(st.get("taskType", st.get("type", ""))).upper()
            ttype = {"MTT": "mapping", "MAPPING_TASK": "mapping",
                     "DTT": "mapping", "CMD": "command",
                     "COMMAND": "command", "DECISION": "choice",
                     "PARALLEL_PATHS": "parallel", "WAIT": "wait",
                     "NOTIFICATION": "email",
                     "SUBTASKFLOW": "subworkflow"}.get(ttype_key(stype),
                                                       "unknown")
            t = Task(key=key, name=st.get("name", key), type=ttype,
                     action={"idmc_type": stype,
                             **({"mapping": st.get("mappingTask", "")}
                                if st.get("mappingTask") else {})},
                     original={"type": stype})
            if st.get("maxRetries"):
                t.retry = RetryPolicy(int(st["maxRetries"]))
            if ttype == "unknown":
                wf.add_issue("MANUAL", "IDMC_STEP_UNSUPPORTED",
                             "Taskflow step %s (%s) needs manual porting"
                             % (key, stype))
            wf.tasks.append(t)
            nxt = st.get("onSuccess") or st.get("next")
            if nxt:
                wf.dependencies.append(Dependency(key, _clean(str(nxt))))
            elif prev:
                wf.dependencies.append(Dependency(prev, key))
            if st.get("onFailure"):
                wf.dependencies.append(Dependency(
                    key, _clean(str(st["onFailure"])), "failure"))
            prev = key
        cor.workflows.append(wf)
    if not cor.workflows:
        raise FileNotFoundError("No IDMC taskflow JSON under %s" % path)
    return cor


def ttype_key(s: str) -> str:
    return s.strip().upper().replace(" ", "_")


def parse_dbtcloud(path: str) -> COR:
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.rglob("*.json"))
    cor = COR(name=_clean(p.stem), source_platform="dbtcloud")
    for f in files:
        try:
            doc = json.loads(f.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        docs = doc if isinstance(doc, list) else [doc]
        for job in docs:
            if not isinstance(job, dict) or "execute_steps" not in job:
                continue
            wf = Workflow(name=_clean(job.get("name", f.stem)),
                          platform="dbtcloud",
                          description=str(job.get("description") or ""))
            trig = job.get("triggers", {}) or {}
            if trig.get("schedule") and job.get("schedule"):
                cron = job["schedule"].get("cron", "") if isinstance(
                    job["schedule"], dict) else ""
                wf.schedules.append(Schedule(kind="cron", cron=cron,
                                             raw=cron))
            if trig.get("github_webhook") or trig.get("git_provider_webhook"):
                wf.schedules.append(Schedule(kind="event",
                                             event="git_webhook",
                                             raw="webhook"))
            prev = ""
            for i, step in enumerate(job.get("execute_steps", []) or []):
                key = "step_%d" % (i + 1)
                wf.tasks.append(Task(key=key, name=str(step)[:60],
                                     type="sql" if "dbt" in str(step)
                                     else "command",
                                     action={"command": str(step)}))
                if prev:
                    wf.dependencies.append(Dependency(prev, key))
                prev = key
            cor.workflows.append(wf)
    if not cor.workflows:
        raise FileNotFoundError("No dbt Cloud job JSON under %s" % path)
    return cor


_CRON_LINE_RE = re.compile(
    r"^\s*(@\w+|(?:\S+\s+){4}\S+)\s+(.+?)\s*$")


def parse_cron(path: str) -> COR:
    p = Path(path)
    files = [p] if p.is_file() else sorted(
        f for f in p.rglob("*") if f.is_file()
        and f.suffix.lower() in ("", ".cron", ".txt")
        or f.name.startswith("crontab"))
    cor = COR(name=_clean(p.stem) or "crontab", source_platform="cron")
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        comment = ""
        for i, line in enumerate(text.splitlines()):
            s = line.strip()
            if s.startswith("#"):
                comment = s.lstrip("# ")
                continue
            if not s or "=" in s.split()[0]:
                continue
            m = _CRON_LINE_RE.match(s)
            if not m:
                continue
            expr, cmd = m.group(1), m.group(2)
            name = _clean(comment) or _clean(cmd.split()[0].rsplit(
                "/", 1)[-1]) or "cron_entry_%d" % (i + 1)
            wf = Workflow(name=name, platform="cron",
                          description=comment)
            wf.schedules.append(Schedule(kind="cron",
                                         cron=normalize_cron(expr),
                                         raw=expr))
            wf.tasks.append(Task(key=name, name=name, type="command",
                                 action={"command": cmd[:300]}))
            cor.workflows.append(wf)
            comment = ""
    if not cor.workflows:
        raise FileNotFoundError("No crontab entries under %s" % path)
    return cor


# ===========================================================================
# bridge from the project parsers' WORKFLOW DAG CIR
# ===========================================================================

_LEGACY_TYPE = {"session": "mapping", "command": "command",
                "email": "email", "decision": "choice", "timer": "wait",
                "event_wait": "sensor", "event_raise": "command",
                "assignment": "dummy", "control": "command",
                "worklet": "subworkflow", "task": "command"}


def cor_from_legacy_dags(dags: List[dict], platform: str,
                         name: str) -> COR:
    """workflow_dags CIR (PowerCenter/SSIS/DataStage/Talend/AbInitio)
    -> COR. One bridge, five platforms."""
    cor = COR(name=name, source_platform=platform)

    def build(dag: dict) -> Workflow:
        wf = Workflow(name=_clean(dag.get("workflow", "workflow")),
                      platform=platform)
        for n in dag.get("nodes", []):
            if n.get("type") == "start":
                continue
            ttype = _LEGACY_TYPE.get(str(n.get("type", "")), "unknown")
            action: Dict[str, object] = {}
            if n.get("mapping"):
                action["mapping"] = n["mapping"]
            if n.get("config"):
                action.update({k: str(v)[:300]
                               for k, v in dict(n["config"]).items()})
            wf.tasks.append(Task(key=n["task_key"],
                                 name=n.get("task", n["task_key"]),
                                 type=ttype, action=action,
                                 original={"type": n.get("type", "")}))
            if n.get("worklet_dag"):
                child = build(n["worklet_dag"])
                if not cor.workflow(child.name):
                    cor.workflows.append(child)
                wf.tasks[-1].action["workflow"] = child.name
        start_keys = {n["task_key"] for n in dag.get("nodes", [])
                      if n.get("type") == "start"}
        for e in dag.get("edges", []):
            if e.get("from") in start_keys:
                continue
            wf.dependencies.append(Dependency(
                e["from"], e["to"], e.get("kind", "success"),
                e.get("condition", "")))
        return wf

    for dag in dags or []:
        wf = build(dag)
        if not cor.workflow(wf.name):
            cor.workflows.append(wf)
    return cor


def parse_legacy_platform(path: str, platform: str) -> COR:
    from ..engine import parse_input
    pipeline = parse_input(path, platform)
    dags = pipeline.metadata.get("workflow_dags", [])
    if not dags:
        raise ValueError("%s project at %s contains no workflows"
                         % (platform, path))
    cor = cor_from_legacy_dags(dags, platform, _clean(Path(path).stem))
    for i in pipeline.all_issues():
        if i.severity.value in ("MANUAL", "WARNING"):
            cor.issues.append({"severity": i.severity.value,
                               "code": i.code, "message": i.message})
    return cor


# ===========================================================================
# dispatch
# ===========================================================================

_PARSERS = {
    "airflow": parse_airflow,
    "adf": lambda p: parse_adf(p, "adf"),
    "synapse_pipelines": lambda p: parse_adf(p, "synapse_pipelines"),
    "fabric": lambda p: parse_adf(p, "fabric"),
    "stepfunctions": parse_stepfunctions,
    "glue_workflow": parse_glue_workflow,
    "controlm": parse_controlm,
    "autosys": parse_autosys,
    "idmc_taskflow": parse_idmc_taskflow,
    "dbtcloud": parse_dbtcloud,
    "cron": parse_cron,
}


def parse_orchestration(path: str, platform: str = "") -> COR:
    """Any supported orchestration export -> COR (auto-detected unless
    the platform is given)."""
    if not platform:
        det = detect_orchestration_platform(path)
        platform = det["detected_platform"]
        if not platform:
            raise ValueError("Could not detect an orchestration platform "
                             "at %s" % path)
    platform = platform.lower()
    if platform in _PARSERS:
        cor = _PARSERS[platform](path)
    elif platform in ("powercenter", "ssis", "datastage", "talend",
                      "abinitio", "idmc", "sap"):
        cor = parse_legacy_platform(path, platform)
    else:
        raise ValueError("Unsupported orchestration platform: %s (one of "
                         "%s)" % (platform, ", ".join(ORCH_PLATFORMS)))
    cor.metadata.setdefault("inventory", {
        "workflows": len(cor.workflows),
        "tasks": sum(len(w.tasks) for w in cor.workflows),
        "dependencies": sum(len(w.dependencies) for w in cor.workflows),
        "schedules": sum(len(w.schedules) for w in cor.workflows),
    })
    return cor

"""Canonical Orchestration Representation (Command 6).

Every orchestration platform normalizes into COR; every target generates
from COR. Nothing converts pairwise.

    Legacy Orchestration -> Parser -> COR -> Semantic Analysis ->
    Target Generator -> Validation + AI Review

Entities: Workflow, Task (typed: mapping/pipeline/command/sql/notebook/
sensor/wait/choice/parallel/loop/email/approval/subworkflow/dummy),
Dependency (success/failure/always/conditional/event), Schedule (cron/
interval/event/manual/calendar), RetryPolicy, TimeoutPolicy (on Task),
Notification, Variable, SecretReference, Connection, Resource pools,
SLA, ErrorHandler, and the derived ExecutionGraph.

Semantics preserved end-to-end: execution order (topological waves),
dependency kinds, scheduling, failure paths, retry behaviour, business
metadata. The original platform payload always rides along in
``Task.original`` — declared loss, never silent loss.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

TASK_TYPES = ("mapping", "pipeline", "command", "sql", "notebook", "copy",
              "sensor", "wait", "choice", "parallel", "loop", "email",
              "approval", "subworkflow", "dummy", "unknown")
DEP_KINDS = ("success", "failure", "always", "conditional", "event")
SCHEDULE_KINDS = ("cron", "interval", "event", "manual", "calendar")


@dataclass
class RetryPolicy:
    max_attempts: int = 0            # 0 = no retries configured
    interval_seconds: int = 0
    backoff_factor: float = 1.0

    def to_dict(self) -> dict:
        return {"max_attempts": self.max_attempts,
                "interval_seconds": self.interval_seconds,
                "backoff_factor": self.backoff_factor}


@dataclass
class Schedule:
    kind: str = "manual"             # SCHEDULE_KINDS
    cron: str = ""
    interval_seconds: int = 0
    timezone: str = ""
    calendar: str = ""               # named calendar (Control-M/AutoSys)
    event: str = ""                  # event/file/dataset trigger name
    start_date: str = ""
    raw: str = ""                    # original expression, always kept

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class Notification:
    on: str = "failure"              # failure | success | sla | start
    channel: str = "email"
    target: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Task:
    key: str
    name: str = ""
    type: str = "unknown"            # TASK_TYPES
    # what the task runs: {"mapping": ..} / {"command": ..} / {"sql": ..}
    # / {"pipeline": ..} / {"notebook": ..} — payload by type
    action: Dict[str, object] = field(default_factory=dict)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_seconds: int = 0
    condition: str = ""              # choice/branch expression
    loop: Dict[str, object] = field(default_factory=dict)
    parallel_group: str = ""
    connection: str = ""
    resources: Dict[str, str] = field(default_factory=dict)   # pool/machine
    sla_seconds: int = 0
    secrets: List[str] = field(default_factory=list)          # names only
    variables_used: List[str] = field(default_factory=list)
    error_handler: Dict[str, object] = field(default_factory=dict)
    description: str = ""
    original: Dict[str, object] = field(default_factory=dict)  # preserved

    def to_dict(self) -> dict:
        d = {"key": self.key, "name": self.name or self.key,
             "type": self.type, "action": self.action,
             "retry": self.retry.to_dict(),
             "timeout_seconds": self.timeout_seconds}
        for k in ("condition", "loop", "parallel_group", "connection",
                  "resources", "sla_seconds", "secrets", "variables_used",
                  "error_handler", "description"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.original:
            d["original"] = self.original
        return d


@dataclass
class Dependency:
    from_task: str
    to_task: str
    kind: str = "success"            # DEP_KINDS
    condition: str = ""

    def to_dict(self) -> dict:
        d = {"from": self.from_task, "to": self.to_task, "kind": self.kind}
        if self.condition:
            d["condition"] = self.condition
        return d


@dataclass
class Workflow:
    name: str
    platform: str = ""
    description: str = ""
    schedules: List[Schedule] = field(default_factory=list)
    tasks: List[Task] = field(default_factory=list)
    dependencies: List[Dependency] = field(default_factory=list)
    variables: List[dict] = field(default_factory=list)   # {name, value,
    #                                    scope, secret: bool}
    connections: List[dict] = field(default_factory=list)
    notifications: List[Notification] = field(default_factory=list)
    sla_seconds: int = 0
    metadata: Dict[str, object] = field(default_factory=dict)
    issues: List[dict] = field(default_factory=list)   # {severity, code,
    #                                    message, detail, suggestion}

    # -- helpers -------------------------------------------------------------
    def task(self, key: str) -> Optional[Task]:
        for t in self.tasks:
            if t.key == key:
                return t
        return None

    def add_issue(self, severity: str, code: str, message: str,
                  detail: str = "", suggestion: str = "") -> None:
        self.issues.append({"severity": severity, "code": code,
                            "message": message, "detail": detail,
                            "suggestion": suggestion})

    def execution_waves(self) -> List[List[str]]:
        """Topological execution order as parallel waves (Kahn levels).
        Tasks stranded by cycles are appended as a final declared wave."""
        keys = [t.key for t in self.tasks]
        incoming: Dict[str, set] = {k: set() for k in keys}
        outgoing: Dict[str, List[str]] = {k: [] for k in keys}
        for d in self.dependencies:
            if d.from_task in incoming and d.to_task in incoming:
                incoming[d.to_task].add(d.from_task)
                outgoing[d.from_task].append(d.to_task)
        waves, done = [], set()
        ready = sorted(k for k in keys if not incoming[k])
        while ready:
            waves.append(ready)
            done.update(ready)
            nxt = set()
            for k in ready:
                for o in outgoing[k]:
                    if o not in done and incoming[o] <= done:
                        nxt.add(o)
            ready = sorted(nxt)
        stranded = [k for k in keys if k not in done]
        if stranded:
            waves.append(sorted(stranded))
        return waves

    def execution_order(self) -> List[str]:
        return [k for wave in self.execution_waves() for k in wave]

    def cycles(self) -> List[List[str]]:
        """Dependency cycles (each reported once)."""
        adj: Dict[str, List[str]] = {}
        for d in self.dependencies:
            adj.setdefault(d.from_task, []).append(d.to_task)
        seen, stack, cycles = set(), [], []

        def visit(node, path):
            if node in path:
                cyc = path[path.index(node):] + [node]
                if sorted(cyc[:-1]) not in [sorted(c[:-1]) for c in cycles]:
                    cycles.append(cyc)
                return
            if node in seen:
                return
            seen.add(node)
            for nxt in adj.get(node, []):
                visit(nxt, path + [node])

        for t in self.tasks:
            visit(t.key, [])
        return cycles

    def to_dict(self) -> dict:
        return {
            "name": self.name, "platform": self.platform,
            "description": self.description,
            "schedules": [s.to_dict() for s in self.schedules],
            "tasks": [t.to_dict() for t in self.tasks],
            "dependencies": [d.to_dict() for d in self.dependencies],
            "variables": self.variables,
            "connections": self.connections,
            "notifications": [n.to_dict() for n in self.notifications],
            "sla_seconds": self.sla_seconds,
            "metadata": self.metadata,
            "issues": self.issues,
            "execution_order": self.execution_order(),
            "execution_waves": self.execution_waves(),
        }


@dataclass
class COR:
    """Top-level: one imported orchestration estate."""
    name: str
    source_platform: str = ""
    workflows: List[Workflow] = field(default_factory=list)
    issues: List[dict] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)

    def workflow(self, name: str) -> Optional[Workflow]:
        for w in self.workflows:
            if w.name == name:
                return w
        return None

    def all_issues(self) -> List[dict]:
        out = list(self.issues)
        for w in self.workflows:
            out += [dict(i, workflow=w.name) for i in w.issues]
        return out

    def to_dict(self) -> dict:
        return {"name": self.name, "source_platform": self.source_platform,
                "workflows": [w.to_dict() for w in self.workflows],
                "issues": self.all_issues(), "metadata": self.metadata}


def cor_from_dict(doc: dict) -> "COR":
    """Round-trip loader for persisted COR JSON (job store)."""
    cor = COR(name=doc.get("name", ""),
              source_platform=doc.get("source_platform", ""),
              metadata=doc.get("metadata", {}))
    cor.issues = [i for i in doc.get("issues", [])
                  if not i.get("workflow")]
    for wd in doc.get("workflows", []):
        wf = Workflow(name=wd["name"], platform=wd.get("platform", ""),
                      description=wd.get("description", ""),
                      variables=wd.get("variables", []),
                      connections=wd.get("connections", []),
                      sla_seconds=wd.get("sla_seconds", 0),
                      metadata=wd.get("metadata", {}),
                      issues=wd.get("issues", []))
        for sd in wd.get("schedules", []):
            wf.schedules.append(Schedule(**{k: v for k, v in sd.items()
                                            if k in Schedule().__dict__}))
        for nd in wd.get("notifications", []):
            wf.notifications.append(Notification(**{
                k: v for k, v in nd.items()
                if k in Notification().__dict__}))
        for td in wd.get("tasks", []):
            r = td.get("retry", {})
            wf.tasks.append(Task(
                key=td["key"], name=td.get("name", td["key"]),
                type=td.get("type", "unknown"),
                action=td.get("action", {}),
                retry=RetryPolicy(r.get("max_attempts", 0),
                                  r.get("interval_seconds", 0),
                                  r.get("backoff_factor", 1.0)),
                timeout_seconds=td.get("timeout_seconds", 0),
                condition=td.get("condition", ""),
                loop=td.get("loop", {}),
                parallel_group=td.get("parallel_group", ""),
                connection=td.get("connection", ""),
                resources=td.get("resources", {}),
                sla_seconds=td.get("sla_seconds", 0),
                secrets=td.get("secrets", []),
                variables_used=td.get("variables_used", []),
                error_handler=td.get("error_handler", {}),
                description=td.get("description", ""),
                original=td.get("original", {})))
        for dd in wd.get("dependencies", []):
            wf.dependencies.append(Dependency(
                dd["from"], dd["to"], dd.get("kind", "success"),
                dd.get("condition", "")))
        cor.workflows.append(wf)
    return cor


# ---------------------------------------------------------------------------
# cron helpers (shared by parsers + validation)
# ---------------------------------------------------------------------------

_CRON_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *",
                 "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
                 "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
                 "@annually": "0 0 1 1 *"}


def normalize_cron(expr: str) -> str:
    return _CRON_ALIASES.get((expr or "").strip().lower(),
                             (expr or "").strip())


import re as _re

_CRON_FIELD_RE = _re.compile(
    r"^(\*|\?|L|W|[\d*/,\-LW#?]+|[A-Z]{3}(?:-[A-Z]{3})?(?:,[A-Z]{3})*)$")


# A year field is '*', a 4-digit year/range, or a step. NOT '?' — that is
# only legal in day-of-month/day-of-week, so a trailing '?' means this is
# the seconds-first Quartz form and the LAST field is day-of-week. Letting
# '?' match here trimmed the wrong end: '0 0 2 * * ?' (02:00 daily) became
# '0 0 2 * *' (midnight on the 2nd).
_YEAR_FIELD_RE = _re.compile(r"^(\*|\d{4}(?:-\d{4})?(?:/\d+)?|\*/\d+)$")


def cron_to_posix(expr: str) -> str:
    """Quartz / AWS EventBridge cron -> 5-field POSIX cron.

    AWS writes ``cron(0 2 * * ? *)``: six fields ending in a YEAR, with
    ``?`` meaning "no specific value" in whichever of day-of-month or
    day-of-week is not being used. POSIX crontab — and therefore Airflow
    and dbt Cloud — takes five fields and rejects ``?``. Emitting the
    source dialect unchanged produced a schedule the target cannot read.

    Only the AWS/Quartz *year-last* six-field form is converted. The
    seconds-first Quartz variant is left untouched rather than guessed
    at, because dropping the wrong end silently shifts every run.
    """
    parts = normalize_cron(expr).split()
    if len(parts) == 6 and _YEAR_FIELD_RE.match(parts[5]):
        parts = parts[:5]
    if len(parts) != 5:
        return normalize_cron(expr)
    return " ".join("*" if p == "?" else p for p in parts)


def cron_is_valid(expr: str) -> bool:
    expr = normalize_cron(expr)
    parts = expr.split()
    if len(parts) not in (5, 6):
        return False
    return all(_CRON_FIELD_RE.match(p.upper()) for p in parts)

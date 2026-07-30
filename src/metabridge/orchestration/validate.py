"""COR validation (Command 6, §8) + migration intelligence (§9).

Deterministic checks over the canonical representation — the same eight
checks regardless of the source platform:

    dependency integrity      edges naming unknown tasks
    circular dependencies     Tarjan-free DFS cycle report
    schedule consistency      invalid cron, duplicate/overlapping triggers
    retry configuration       negative/absurd retry settings
    missing connections       tasks referencing no connection where one
                              is clearly required (sql/copy/mapping)
    broken triggers           event schedules naming no event
    orphan workflows          no schedule, no trigger, not referenced by
                              any other workflow
    dead-end tasks            non-terminal task types with no outgoing
                              edge while siblings continue

``resilience_audit`` adds the operational read on top: critical path,
serialization, failure blast radius, retry/timeout coverage, sensor mode,
catchup-on-cutover and run-overlap risk, plus a cutover checklist.
"""
from __future__ import annotations

from typing import Dict, List

from .cor import COR, Workflow, cron_is_valid, normalize_cron

VERDICTS = ("PASS", "PASS_WITH_WARNINGS", "MANUAL_REVIEW", "FAIL")


def _f(severity: str, code: str, message: str, workflow: str = "",
       obj: str = "", suggestion: str = "") -> dict:
    return {"severity": severity, "code": code, "message": message,
            "workflow": workflow, "object": obj, "suggestion": suggestion}


def validate_cor(cor: COR) -> dict:
    findings: List[dict] = []
    referenced: set = set()
    for w in cor.workflows:
        for t in w.tasks:
            if t.type == "subworkflow":
                referenced.add(str(t.action.get("workflow", "")))

    for w in cor.workflows:
        keys = {t.key for t in w.tasks}

        # dependency integrity
        for d in w.dependencies:
            for end, label in ((d.from_task, "from"), (d.to_task, "to")):
                if end not in keys:
                    findings.append(_f(
                        "ERROR", "DEP_UNKNOWN_TASK",
                        "Dependency %s->%s names unknown task '%s'"
                        % (d.from_task, d.to_task, end), w.name, end,
                        "Fix the reference in the source export."))

        # circular dependencies
        for cyc in w.cycles():
            findings.append(_f(
                "ERROR", "DEP_CYCLE",
                "Circular dependency: %s" % " -> ".join(cyc), w.name,
                cyc[0], "Break the cycle before modernizing."))

        # schedule consistency
        crons = []
        for s in w.schedules:
            if s.kind == "cron":
                if not cron_is_valid(s.cron):
                    findings.append(_f(
                        "WARNING", "SCHEDULE_INVALID_CRON",
                        "Schedule '%s' is not a valid cron expression"
                        % (s.raw or s.cron), w.name, s.cron,
                        "Correct the expression or convert to a "
                        "target-native trigger."))
                else:
                    crons.append(normalize_cron(s.cron))
            if s.kind == "event" and not s.event:
                findings.append(_f(
                    "WARNING", "TRIGGER_BROKEN",
                    "Event trigger has no event reference", w.name,
                    "", "Name the file/dataset/event that fires it."))
        dupes = {c for c in crons if crons.count(c) > 1}
        for c in dupes:
            findings.append(_f(
                "WARNING", "SCHEDULE_DUPLICATE",
                "Duplicate cron schedule '%s' — the workflow triggers "
                "twice" % c, w.name, c))

        # retry configuration
        for t in w.tasks:
            if t.retry.max_attempts < 0 or t.retry.interval_seconds < 0:
                findings.append(_f(
                    "WARNING", "RETRY_INVALID",
                    "Task '%s' has a negative retry setting" % t.key,
                    w.name, t.key))
            if t.retry.max_attempts > 20:
                findings.append(_f(
                    "WARNING", "RETRY_EXCESSIVE",
                    "Task '%s' retries %d times — review before porting"
                    % (t.key, t.retry.max_attempts), w.name, t.key))
            if t.timeout_seconds < 0:
                findings.append(_f(
                    "WARNING", "TIMEOUT_INVALID",
                    "Task '%s' has a negative timeout" % t.key,
                    w.name, t.key))

        # missing connections
        for t in w.tasks:
            if t.type in ("sql", "copy", "mapping") and not t.connection \
                    and not t.action.get("connection"):
                findings.append(_f(
                    "WARNING", "CONNECTION_MISSING",
                    "Task '%s' (%s) has no connection reference"
                    % (t.key, t.type), w.name, t.key,
                    "Bind a connection in the target platform."))

        # orphan workflow
        has_trigger = bool(w.schedules) and any(
            s.kind != "manual" for s in w.schedules)
        if not has_trigger and w.name not in referenced:
            findings.append(_f(
                "INFO", "WORKFLOW_ORPHAN",
                "Workflow '%s' has no schedule/trigger and is not "
                "invoked by another workflow — manual-run only" % w.name,
                w.name))

        # dead-end tasks: a branch/choice with an outgoing edge set that
        # silently strands one branch, or a mid-graph task nothing follows
        outgoing = {t.key: 0 for t in w.tasks}
        incoming = {t.key: 0 for t in w.tasks}
        for d in w.dependencies:
            if d.from_task in outgoing:
                outgoing[d.from_task] += 1
            if d.to_task in incoming:
                incoming[d.to_task] += 1
        terminals = [k for k, n in outgoing.items() if n == 0]
        if len(terminals) > 1:
            for k in terminals:
                t = w.task(k)
                if t is not None and t.type in ("choice", "parallel",
                                                "sensor", "wait"):
                    findings.append(_f(
                        "WARNING", "TASK_DEAD_END",
                        "Task '%s' (%s) has no downstream task — a "
                        "control-flow node should route somewhere"
                        % (k, t.type), w.name, k))
        for k, n in incoming.items():
            t = w.task(k)
            if n == 0 and outgoing[k] == 0 and len(w.tasks) > 1 \
                    and t is not None:
                findings.append(_f(
                    "WARNING", "TASK_DISCONNECTED",
                    "Task '%s' is disconnected from the execution graph"
                    % k, w.name, k))

    worst = "PASS"
    sev = {f["severity"] for f in findings}
    if "ERROR" in sev:
        worst = "FAIL"
    elif "MANUAL" in sev or any(
            i.get("severity") == "MANUAL" for i in cor.all_issues()):
        worst = "MANUAL_REVIEW"
    elif "WARNING" in sev or any(
            i.get("severity") in ("WARNING", "MANUAL")
            for i in cor.all_issues()):
        worst = "PASS_WITH_WARNINGS" if "WARNING" in sev else "MANUAL_REVIEW"
    return {"verdict": worst, "findings": findings,
            "checks_run": ["dependency_integrity", "circular_dependencies",
                           "schedule_consistency", "retry_configuration",
                           "missing_connections", "broken_triggers",
                           "orphan_workflows", "dead_end_tasks"],
            "workflows_validated": [w.name for w in cor.workflows]}


# ---------------------------------------------------------------------------
# migration intelligence (§9)
# ---------------------------------------------------------------------------

_TYPE_WEIGHT = {"mapping": 1, "pipeline": 1, "sql": 1, "copy": 1,
                "command": 2, "notebook": 2, "email": 1, "dummy": 0,
                "sensor": 3, "wait": 2, "choice": 3, "parallel": 2,
                "loop": 4, "approval": 4, "subworkflow": 2, "unknown": 5}
_AUTOMATABLE = {"mapping", "pipeline", "sql", "copy", "email", "dummy",
                "command", "parallel", "wait", "subworkflow", "notebook"}


def migration_intelligence(cor: COR, validation: dict) -> dict:
    tasks = [(w, t) for w in cor.workflows for t in w.tasks]
    total = len(tasks) or 1
    auto = sum(1 for _w, t in tasks if t.type in _AUTOMATABLE)
    manual_issues = [i for i in cor.all_issues()
                     if i.get("severity") == "MANUAL"]
    unsupported = sorted({t.type for _w, t in tasks
                          if t.type in ("unknown", "approval", "loop")})
    complexity = min(100, sum(_TYPE_WEIGHT.get(t.type, 3)
                              for _w, t in tasks)
                     + 4 * len(manual_issues)
                     + 3 * sum(len(w.cycles()) for w in cor.workflows))
    sla_tasks = [t.key for _w, t in tasks if t.sla_seconds] + \
        [w.name for w in cor.workflows if w.sla_seconds]
    risks = []
    for f in validation["findings"]:
        if f["severity"] in ("ERROR", "WARNING"):
            risks.append("%s: %s" % (f["code"], f["message"]))
    for w in cor.workflows:
        waves = w.execution_waves()
        widest = max((len(x) for x in waves), default=0)
        if widest > 8:
            risks.append("PARALLELISM: workflow '%s' fans out to %d "
                         "parallel tasks — verify target pool limits"
                         % (w.name, widest))
    # effort: 0.5h per simple task, 2h per control-flow node, 4h per
    # manual item — floor 1h. Deterministic and explainable, not a promise.
    hours = round(sum(0.5 if t.type in _AUTOMATABLE else 2.0
                      for _w, t in tasks) + 4.0 * len(manual_issues), 1)
    return {
        "automation_score": round(100.0 * auto / total, 1),
        "migration_complexity": complexity,
        "complexity_level": ("LOW" if complexity < 20 else
                             "MEDIUM" if complexity < 45 else
                             "HIGH" if complexity < 75 else "VERY_HIGH"),
        "tasks_total": len(tasks),
        "workflows_total": len(cor.workflows),
        "unsupported_features": unsupported,
        "manual_review_items": [
            {k: i.get(k, "") for k in ("workflow", "code", "message")}
            for i in manual_issues],
        "execution_risks": risks[:25],
        "sla_impact": {"sla_bearing_objects": sla_tasks,
                       "note": "Re-validate SLAs after cutover — engine "
                               "timings differ between platforms."}
        if sla_tasks else {"sla_bearing_objects": []},
        "estimated_effort_hours": max(hours, 1.0),
    }


# ---------------------------------------------------------------------------
# runtime resilience + critical path
# ---------------------------------------------------------------------------
#
# validate_cor answers "is this COR internally consistent"; migration
# intelligence answers "how much work is the port". Neither answers the
# questions a migration lead actually gets asked in the go/no-go meeting:
#
#     how long is the critical path, and where does the DAG serialize?
#     which single task failure stops the most downstream work?
#     what will behave differently the first night after cutover?
#
# Those are properties of the execution graph, not of the source syntax, so
# they are computed once here from COR and apply to every platform.

# tasks that cross a network or a process boundary — a transient failure is
# expected behaviour for these, so no retry policy is a real defect
_IO_BOUND = ("sql", "copy", "mapping", "notebook", "subworkflow", "sensor",
             "command")
# task types that can hang indefinitely and hold an executor slot
_CAN_HANG = ("sql", "copy", "mapping", "notebook", "subworkflow", "sensor",
             "command")


def _dur(seconds: float) -> str:
    s = int(seconds or 0)
    if not s:
        return "unbounded"
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm" % round(s / 60.0)
    return "%.1fh" % (s / 3600.0)


def _reachable(succ: Dict[str, List[str]], start: str) -> set:
    """Everything downstream of a task — its failure blast radius."""
    seen: set = set()
    stack = list(succ.get(start, ()))
    while stack:
        k = stack.pop()
        if k in seen:
            continue
        seen.add(k)
        stack.extend(succ.get(k, ()))
    return seen


def _longest_chain(order: List[str], succ: Dict[str, List[str]],
                   cost: Dict[str, float]):
    """Longest dependency chain over a topological order.

    Iterative reverse DP — no recursion (deep estates blow the stack) and
    cycle-safe, because any edge that points backwards in the topological
    order is a cycle edge and is skipped rather than followed."""
    rank = {k: i for i, k in enumerate(order)}
    length: Dict[str, int] = {}
    spend: Dict[str, float] = {}
    nxt: Dict[str, str] = {}
    for k in reversed(order):
        best_len, best_cost, best_next = 0, 0.0, ""
        for n in succ.get(k, ()):
            if rank.get(n, -1) <= rank.get(k, 0):
                continue                              # back edge (cycle)
            cand = (length.get(n, 0), spend.get(n, 0.0))
            if cand > (best_len, best_cost):
                best_len, best_cost, best_next = cand[0], cand[1], n
        length[k] = best_len + 1
        spend[k] = best_cost + cost.get(k, 0.0)
        nxt[k] = best_next
    if not order:
        return [], 0.0
    head = max(order, key=lambda k: (length[k], spend[k]))
    path, cur = [], head
    while cur:
        path.append(cur)
        cur = nxt.get(cur, "")
    return path, spend.get(head, 0.0)


def resilience_audit(cor: COR) -> dict:
    """Critical path, serialization, blast radius and cutover risk per
    workflow — the operational read on an orchestration estate."""
    findings: List[dict] = []
    checklist: List[str] = []
    per_wf: List[dict] = []

    for w in cor.workflows:
        keys = [t.key for t in w.tasks]
        by = {t.key: t for t in w.tasks}
        succ: Dict[str, List[str]] = {k: [] for k in keys}
        for d in w.dependencies:
            if d.from_task in succ and d.to_task in succ:
                succ[d.from_task].append(d.to_task)
        waves = w.execution_waves()
        order = [k for wave in waves for k in wave]
        cost = {t.key: float(t.timeout_seconds or 0) for t in w.tasks}
        path, budget = _longest_chain(order, succ, cost)
        widest = max((len(x) for x in waves), default=0)
        interval = next((s.interval_seconds for s in w.schedules
                         if s.interval_seconds), 0)
        blast = sorted(
            [{"task": k, "type": by[k].type,
              "blocks_downstream": len(_reachable(succ, k)),
              "retries": by[k].retry.max_attempts,
              "has_timeout": bool(by[k].timeout_seconds)} for k in keys],
            key=lambda r: (-r["blocks_downstream"], r["task"]))
        spof = [r for r in blast if r["blocks_downstream"] >= 2][:6]

        per_wf.append({
            "workflow": w.name,
            "tasks": len(keys),
            "critical_path": path,
            "critical_path_length": len(path),
            "critical_path_declared_timeout_seconds": int(budget),
            "critical_path_declared_timeout": _dur(budget),
            "critical_path_unbounded_tasks": sum(
                1 for k in path if not by[k].timeout_seconds),
            "serial_stages": len(waves),
            "max_parallel_width": widest,
            "parallelism_ratio": round(len(keys) / float(len(waves)), 2)
            if waves else 0.0,
            "schedule_interval_seconds": interval,
            "single_points_of_failure": spof,
        })

        def flag(severity: str, code: str, message: str, obj: str = "",
                 suggestion: str = "") -> None:
            findings.append(_f(severity, code, message, w.name, obj,
                               suggestion))

        no_retry = [t.key for t in w.tasks
                    if t.type in _IO_BOUND and t.retry.max_attempts < 1]
        if no_retry:
            flag("WARNING", "NO_RETRY_ON_IO_TASK",
                 "%d task(s) have no retry policy: %s"
                 % (len(no_retry), ", ".join(no_retry[:8])), no_retry[0],
                 "One transient fault — a warehouse restart, a throttled "
                 "API, a dropped connection — fails the whole run. Two or "
                 "three attempts with a delay is the norm.")
            checklist.append("Set retries on %d task(s) in %s (%s)"
                             % (len(no_retry), w.name,
                                ", ".join(no_retry[:5])))
        no_timeout = [t.key for t in w.tasks
                      if t.type in _CAN_HANG and not t.timeout_seconds]
        if no_timeout:
            flag("WARNING", "NO_EXECUTION_TIMEOUT",
                 "%d task(s) can run unbounded — no execution timeout: %s"
                 % (len(no_timeout), ", ".join(no_timeout[:8])),
                 no_timeout[0],
                 "A hung task holds its executor slot until someone clears "
                 "it by hand. Cap each one below the schedule interval.")
            checklist.append("Add an execution timeout to %d task(s) in %s"
                             % (len(no_timeout), w.name))
        poking = [t.key for t in w.tasks if t.type == "sensor"
                  and str(t.resources.get("mode", "poke")).lower()
                  != "reschedule"]
        if poking:
            flag("WARNING", "SENSOR_HOLDS_SLOT",
                 "%d sensor(s) wait in poke mode: %s"
                 % (len(poking), ", ".join(poking[:8])), poking[0],
                 "A poking sensor occupies a worker for its whole wait. "
                 "Long waits belong in reschedule/deferrable mode, or the "
                 "estate deadlocks itself under load.")
            checklist.append("Move %d sensor(s) in %s to reschedule or "
                             "deferrable mode" % (len(poking), w.name))
        sla_only = [t.key for t in w.tasks
                    if t.sla_seconds and not t.timeout_seconds
                    and t.type in _CAN_HANG]
        if sla_only:
            flag("WARNING", "SLA_WITHOUT_TIMEOUT",
                 "%d task(s) carry an SLA but no timeout: %s"
                 % (len(sla_only), ", ".join(sla_only[:8])), sla_only[0],
                 "An SLA raises an alert; it does not stop the task. "
                 "Without a timeout the breach keeps running.")
        if w.metadata.get("catchup") is True:
            flag("WARNING", "CATCHUP_BACKFILL_ON_CUTOVER",
                 "Workflow '%s' has catchup enabled — the first run after "
                 "cutover backfills every interval missed since its start "
                 "date" % w.name, w.name,
                 "Cut over with catchup disabled, backfill deliberately, "
                 "then re-enable.")
            checklist.append("Disable catchup for the first post-cutover "
                             "run of %s, then backfill deliberately"
                             % w.name)
        if w.metadata.get("depends_on_past") or \
                w.metadata.get("wait_for_downstream"):
            flag("WARNING", "SERIALIZED_BY_HISTORY",
                 "Workflow '%s' depends on the previous run — runs cannot "
                 "overlap and a backfill cannot be parallelized" % w.name,
                 w.name,
                 "One stuck historical run blocks every later one. Confirm "
                 "the target honours this, and plan the backfill serially.")
        if not w.notifications:
            flag("WARNING", "NO_FAILURE_ALERT",
                 "Workflow '%s' declares no failure notification — a "
                 "failure is silent until someone opens the UI" % w.name,
                 w.name, "Wire a failure channel before cutover.")
            checklist.append("Wire a failure notification for %s" % w.name)
        if widest > 8 and not any(t.resources.get("pool") for t in w.tasks):
            flag("WARNING", "PARALLEL_FANOUT_UNBOUNDED",
                 "Workflow '%s' fans out to %d concurrent tasks with no "
                 "pool or concurrency limit declared" % (w.name, widest),
                 w.name,
                 "The source engine's global slot limit was doing the "
                 "throttling. Declare it explicitly, or the first run on "
                 "the target overwhelms the shared warehouse.")
            checklist.append("Declare a pool/concurrency cap for the %d-wide "
                             "fan-out in %s" % (widest, w.name))
        if interval and budget and budget > interval:
            flag("WARNING", "RUN_OVERLAP_RISK",
                 "Declared timeouts along the critical path of '%s' total "
                 "%s, longer than its own %s schedule interval — runs can "
                 "overlap" % (w.name, _dur(budget), _dur(interval)), w.name,
                 "Cap max active runs at 1, or shorten the path.")
        if len(keys) >= 6 and len(waves) == len(keys):
            flag("INFO", "FULLY_SERIAL",
                 "Workflow '%s' runs %d tasks in %d sequential stages — "
                 "nothing runs in parallel" % (w.name, len(keys), len(waves)),
                 w.name,
                 "Modernization is the moment to parallelize: independent "
                 "extracts and loads rarely need to be chained.")

    sev = [x["severity"] for x in findings]
    score = max(0, 100 - 8 * sev.count("ERROR") - 5 * sev.count("WARNING")
                - sev.count("INFO"))
    return {
        "resilience_score": score,
        "grade": ("A" if score >= 90 else "B" if score >= 75 else
                  "C" if score >= 60 else "D"),
        "workflows": per_wf,
        "findings": findings,
        "cutover_checklist": list(dict.fromkeys(checklist))[:12],
        "checks_run": ["critical_path", "serialization", "blast_radius",
                       "retry_coverage", "timeout_coverage", "sensor_mode",
                       "sla_enforcement", "catchup_on_cutover",
                       "run_overlap", "failure_alerting",
                       "parallel_fanout_limits"],
    }

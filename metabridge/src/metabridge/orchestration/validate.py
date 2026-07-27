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

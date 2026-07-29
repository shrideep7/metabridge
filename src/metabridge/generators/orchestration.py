"""Orchestration generators over the WORKFLOW DAG CIR (Phase 2, module 25).

The DAG (parsers/pc_workflow.py) renders three ways:

    dbt         model DEPENDENCIES are already in the models (refs /
                depends_on drive dbt's own DAG); what dbt cannot express
                — shell commands, emails, timers, failure paths — goes
                into a JOB SPECIFICATION (orchestration/<wf>_job.yml)
                with an explicit recommendation per non-model task
    databricks  a Databricks Workflows job spec (Jobs API shape):
                task_key / depends_on / run_if per task, failure
                handling via AT_LEAST_ONE_FAILED branches and
                email_notifications.on_failure
    other       the same task graph as an engine-neutral spec plus the
                native-scheduler recommendation (Snowflake tasks, Azure
                Data Factory, Airflow, ...) — every warehouse, not one

Honesty rule: tasks that need a human decision (decision expressions,
event semantics, control aborts) are emitted as explicit placeholders
with the original payload preserved — never silently dropped.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

_RUN_IF = {"success": "ALL_SUCCESS", "failure": "AT_LEAST_ONE_FAILED",
           "always": "ALL_DONE", "conditional": "ALL_SUCCESS"}

_NON_MODEL_RECOMMENDATION = {
    "command": "shell step in the orchestrator (Databricks Jobs task, "
               "Airflow BashOperator, ADF custom activity)",
    "email": "orchestrator notification (job email_notifications / "
             "Airflow EmailOperator / dbt Cloud job notifications)",
    "decision": "orchestrator branch (Databricks condition_task / "
                "Airflow BranchPythonOperator); expression preserved",
    "timer": "schedule or sensor in the orchestrator, not a task",
    "event_wait": "sensor / file-arrival trigger in the orchestrator",
    "event_raise": "signal emitted by the orchestrator (dataset/event)",
    "assignment": "job/run parameters set by the orchestrator",
    "control": "fail/abort policy on the job (stop-on-failure settings)",
}


def _deps_for(dag: dict, key: str) -> List[dict]:
    return [e for e in dag["edges"] if e["to"] == key]


def _run_if(deps: List[dict]) -> Optional[str]:
    if not deps:
        return None
    kinds = {d["kind"] for d in deps}
    if kinds == {"failure"}:
        return "AT_LEAST_ONE_FAILED"
    if "always" in kinds or len(kinds) > 1:
        return "ALL_DONE"
    return _RUN_IF[deps[0]["kind"]]


def databricks_job_spec(dag: dict,
                        sql_files: Optional[Dict[str, str]] = None) -> dict:
    """WORKFLOW DAG -> Databricks Workflows job specification (Jobs API
    shape): task_key, depends_on, run_if, failure handling."""
    sql_files = sql_files or {}
    tasks: List[dict] = []
    failure_emails: List[str] = []
    start_keys = {n["task_key"] for n in dag["nodes"]
                  if n["type"] == "start"}
    failure_targets = {to for _, to in dag["failure_paths"]}

    for n in dag["nodes"]:
        if n["type"] == "start":
            continue
        deps = [d for d in _deps_for(dag, n["task_key"])
                if d["from"] not in start_keys]
        t: dict = {"task_key": n["task_key"]}
        if deps:
            t["depends_on"] = [{"task_key": d["from"]} for d in deps]
            run_if = _run_if(deps)
            if run_if:
                t["run_if"] = run_if
        conds = [d["condition"] for d in deps
                 if d["kind"] == "conditional" and d["condition"]]
        if conds:
            t["_metabridge_note"] = ("conditional link(s) preserved from "
                                     "PowerCenter: %s — implement as a "
                                     "condition_task" % "; ".join(conds))

        if n["type"] == "session":
            mapping = n.get("mapping", "")
            t["sql_task"] = {
                "file": {"path": "sql/%s" % sql_files.get(mapping,
                                                          mapping + ".sql")},
                "warehouse_id": "<warehouse_id>",
            }
        elif n["type"] == "worklet":
            t["run_job_task"] = {"job_id": "<job:%s>" % n["task"]}
            t["_metabridge_note"] = ("worklet '%s' converted as its own "
                                     "job spec — deploy it and set job_id"
                                     % n["task"])
        elif n["type"] == "email":
            cfg = n.get("config") or {}
            addr = cfg.get("Email User Name", "")
            if addr and n["task_key"] in failure_targets:
                failure_emails.append(addr)
            t["_metabridge_task_type"] = "email"
            t["_metabridge_note"] = ("email task '%s' (to: %s subject: %s)"
                                     " — use job email_notifications; kept"
                                     " as a placeholder for the DAG shape"
                                     % (n["task"], addr or "?",
                                        cfg.get("Email Subject", "")))
        else:
            t["_metabridge_task_type"] = n["type"]
            payload = n.get("config") or {}
            if payload:
                t["_metabridge_payload"] = payload
            t["_metabridge_note"] = "%s task — port manually: %s" % (
                n["type"],
                _NON_MODEL_RECOMMENDATION.get(n["type"],
                                              "orchestrator step"))
        tasks.append(t)

    spec: dict = {"name": dag["workflow"], "tasks": tasks,
                  "max_concurrent_runs": 1}
    if failure_emails:
        spec["email_notifications"] = {
            "on_failure": sorted(set(failure_emails))}
    return spec


def generic_workflow_spec(dag: dict, format_name: str) -> dict:
    """Engine-neutral orchestration spec for non-Databricks warehouses."""
    native = {
        "snowflake": "Snowflake TASK graph (AFTER dependencies) or an "
                     "external orchestrator",
        "bigquery": "Cloud Composer (Airflow) or scheduled queries with "
                    "an orchestrator for dependencies",
        "redshift": "Step Functions / MWAA (Airflow)",
        "synapse": "Azure Data Factory / Synapse pipelines",
        "sqlserver": "SQL Server Agent job steps",
        "oracle": "DBMS_SCHEDULER chains",
        "teradata": "Teradata TASM / an external orchestrator",
        "postgres": "pg_cron plus an orchestrator for dependencies",
    }.get(format_name, "an external orchestrator (Airflow, Dagster)")
    return {
        "workflow": dag["workflow"],
        "recommendation": "Recreate this DAG on %s; task list preserves "
                          "dependencies, conditions and failure paths."
                          % native,
        "execution_order": dag["execution_order"],
        "tasks": [
            {"task_key": n["task_key"], "type": n["type"],
             **({"mapping": n["mapping"]} if n.get("mapping") else {}),
             **({"config": n["config"]} if n.get("config") else {}),
             "depends_on": [{"task_key": e["from"], "when": e["kind"],
                             **({"condition": e["condition"]}
                                if e["condition"] else {})}
                            for e in _deps_for(dag, n["task_key"])]}
            for n in dag["nodes"] if n["type"] != "start"],
    }


def dbt_job_spec(dag: dict, model_names: Dict[str, str]) -> dict:
    """WORKFLOW DAG -> dbt job specification. Model ordering is dbt's own
    DAG (refs/depends_on); this spec adds the run steps and everything
    dbt cannot express."""
    models = [model_names[n["mapping"]]
              for k in dag["execution_order"]
              for n in dag["nodes"]
              if n["task_key"] == k and n["type"] == "session"
              and n.get("mapping") in model_names]
    non_model = []
    failure_handlers = {to for _, to in dag["failure_paths"]}
    for n in dag["nodes"]:
        if n["type"] in ("session", "start"):
            continue
        entry = {"task": n["task"], "type": n["type"],
                 "recommendation": _NON_MODEL_RECOMMENDATION.get(
                     n["type"], "orchestrator step")}
        if n.get("config"):
            entry["payload"] = n["config"]
        if n["task_key"] in failure_handlers:
            entry["on"] = "failure"
        if n["type"] == "worklet":
            entry["recommendation"] = ("nested worklet — its sessions are "
                                       "models in this project; run via "
                                       "the same job")
        non_model.append(entry)
    return {
        "job": {
            "name": dag["workflow"],
            "description": "Converted from PowerCenter workflow '%s' by "
                           "MetaBridge AI" % dag["workflow"],
            "steps": (["dbt run --select %s" % " ".join(models)]
                      if models else []) + ["dbt test"],
            "note": "Model-to-model ordering is enforced by dbt's own "
                    "DAG; the workflow's success paths are the default "
                    "behavior (a failed model skips its children).",
        },
        "non_model_tasks": non_model,
    }


def write_orchestration(out_dir, dags: List[dict], format_name: str,
                        sql_files: Optional[Dict[str, str]] = None,
                        model_names: Optional[Dict[str, str]] = None,
                        ) -> List[str]:
    """Write per-workflow orchestration specs; returns file names."""
    from pathlib import Path
    out = Path(out_dir) / "orchestration"
    written: List[str] = []

    def _wl_dags(dag):
        for n in dag["nodes"]:
            if n.get("worklet_dag"):
                yield n["worklet_dag"]
                for sub in _wl_dags(n["worklet_dag"]):
                    yield sub

    for dag in dags or []:
        out.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c == "_" else "_"
                       for c in dag["workflow"])
        if format_name == "dbt":
            import yaml
            spec = dbt_job_spec(dag, model_names or {})
            fname = "%s_job.yml" % safe
            (out / fname).write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        elif format_name == "databricks":
            fname = "%s_job.json" % safe
            (out / fname).write_text(json.dumps(
                databricks_job_spec(dag, sql_files), indent=2) + "\n", encoding="utf-8")
            for wl in _wl_dags(dag):        # worklets become their own jobs
                wf = "%s_job.json" % "".join(
                    c if c.isalnum() or c == "_" else "_"
                    for c in wl["workflow"])
                (out / wf).write_text(json.dumps(
                    databricks_job_spec(wl, sql_files), indent=2) + "\n", encoding="utf-8")
                written.append("orchestration/" + wf)
        else:
            fname = "%s_workflow.json" % safe
            (out / fname).write_text(json.dumps(
                generic_workflow_spec(dag, format_name), indent=2) + "\n", encoding="utf-8")
        written.append("orchestration/" + fname)
    return written

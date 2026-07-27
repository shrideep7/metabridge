"""Parameter and variable engine (Phase 2, module 26).

Everything PowerCenter parameterizes with is collected into ONE registry
(pipeline.metadata['parameter_registry']) and CLASSIFIED:

    runtime_parameter          $$REGION_FILTER (ISPARAM=YES), workflow
                               variables without persistence — a value
                               supplied per run
    environment_configuration  $PMSourceFileDir, $PMRootDir, connection
                               -ish paths — deployment config, not data
    stateful_variable          $$LAST_RUN_DATE (ISPARAM=NO, repository-
                               persisted, often AGGFUNCTION MAX/MIN),
                               persistent workflow variables — the value
                               SURVIVES across runs
    system_variable            $PMWorkflowRunId, $PMWorkflowName, ... —
                               run identity from the engine

Each entry carries its target strategy:

    dbt         vars / env_var / run_started_at / invocation_id
    databricks  job parameters / widgets / task values / environment
                variables
    warehouses  bind variables / deployment configuration / watermark
                state tables (every warehouse, not one)

THE RULE: stateful variables are never silently converted into static
dbt vars. Every stateful variable raises a MANUAL review item with the
watermark pattern spelled out; the substituted var() is marked STATEFUL
inline so the review cannot be missed.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from ..ir.model import ConversionIssue, IssueSeverity, Mapping, Pipeline

_PM_RE = re.compile(r"\$(PM\w+)")

# run-identity system variables and their target equivalents
SYSTEM_VARIABLES: Dict[str, Dict[str, str]] = {
    "PMWorkflowRunId": {"dbt": "{{ invocation_id }}",
                        "databricks": "{{job.run_id}} (job parameter)",
                        "warehouses": "bind :PMWorkflowRunId from the "
                                      "scheduler's run id"},
    "PMWorkflowName": {"dbt": "the job name (orchestrator context) or a "
                              "project var",
                       "databricks": "{{job.name}}",
                       "warehouses": "bind :PMWorkflowName"},
    "PMWorkflowRunInstanceName": {"dbt": "{{ invocation_id }}",
                                  "databricks": "{{job.run_id}}",
                                  "warehouses": "bind variable"},
    "PMSessionName": {"dbt": "the model name (literal)",
                      "databricks": "{{task.name}}",
                      "warehouses": "the script name (literal)"},
    "PMSessionRunMode": {"dbt": "not applicable (always batch)",
                         "databricks": "not applicable",
                         "warehouses": "not applicable"},
    "PMMappingName": {"dbt": "the model name (literal)",
                      "databricks": "{{task.name}}",
                      "warehouses": "the script name (literal)"},
    "PMIntegrationServiceName": {"dbt": "{{ target.name }}",
                                 "databricks": "workspace context",
                                 "warehouses": "deployment config"},
    "PMRepositoryServiceName": {"dbt": "{{ target.name }}",
                                "databricks": "workspace context",
                                "warehouses": "deployment config"},
}


def classify(name: str, *, is_param: Optional[bool] = None,
             persistent: Optional[bool] = None,
             aggregation: str = "") -> str:
    """One canonical classification for any PowerCenter parameter-ish
    name."""
    bare = name.lstrip("$")
    if name.startswith("$PM") or bare.startswith("PM"):
        if "Dir" in bare or bare in ("PMRootDir",):
            return "environment_configuration"
        return "system_variable"
    if persistent:
        return "stateful_variable"
    if is_param is False or aggregation:
        return "stateful_variable"          # repository-persisted $$var
    return "runtime_parameter"


def _strategies(classification: str, name: str, default: str) -> dict:
    bare = name.lstrip("$")
    if classification == "runtime_parameter":
        return {
            "dbt": "{{ var('%s'%s) }}" % (
                bare, ", '%s'" % default if default else ""),
            "databricks": "job parameter / widget: "
                          "dbutils.widgets.get('%s')" % bare,
            "warehouses": "bind variable :%s supplied by the scheduler"
                          % bare,
        }
    if classification == "environment_configuration":
        return {
            "dbt": "{{ env_var('%s'%s) }}" % (
                bare, ", '%s'" % default if default else ""),
            "databricks": "environment variable / cluster spark env",
            "warehouses": "deployment configuration (never in the SQL)",
        }
    if classification == "system_variable":
        return dict(SYSTEM_VARIABLES.get(bare, {
            "dbt": "{{ invocation_id }} / {{ run_started_at }} depending "
                   "on use",
            "databricks": "job context value",
            "warehouses": "bind variable from the scheduler run context",
        }))
    return {                                # stateful_variable
        "dbt": "NOT a static var — persist the value: watermark query "
               "(SELECT COALESCE(MAX(<col>), '<default>') FROM {{ this }})"
               " or a state table; see the STATEFUL_VARIABLE review item",
        "databricks": "task values (dbutils.jobs.taskValues) or a Delta "
                      "state table read at job start",
        "warehouses": "a watermark/state table read by the load and "
                      "updated at the end of the run",
    }


def _entry(name: str, scope: str, classification: str, **kw) -> dict:
    e = {"name": name, "scope": scope, "classification": classification,
         "datatype": kw.get("datatype", ""),
         "default": kw.get("default", ""),
         "used_by": kw.get("used_by", []),
         **_strategies(classification, name, kw.get("default", ""))}
    if kw.get("aggregation"):
        e["aggregation"] = kw["aggregation"]
    return e


def _mapping_text(m: Mapping) -> str:
    parts = [p.expression or "" for t in m.transformations for p in t.ports]
    for t in m.transformations:
        parts.append(json.dumps(t.properties, default=str))
    parts.append(json.dumps(
        {k: v for k, v in m.properties.items() if k != "variables"},
        default=str))
    text = " ".join(parts)
    # references inside SQL comments ('-- REVIEW: ...' / '/* ... */') are
    # notes, not active logic — strip before reference checks
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"--[^\n\\\"]*", " ", text)


def build_parameter_registry(pipeline: Pipeline,
                             workflows: Optional[List[dict]] = None,
                             ) -> dict:
    """Collect + classify every parameter/variable; merge-safe across
    folders (keyed by name+scope)."""
    reg = pipeline.metadata.setdefault(
        "parameter_registry", {"parameters": [], "parameter_files": [],
                               "summary": {}})
    byname = {(e["name"], e["scope"]): e for e in reg["parameters"]}

    def put(entry: dict, used_by: str = "") -> dict:
        key = (entry["name"], entry["scope"])
        cur = byname.get(key)
        if cur is None:
            byname[key] = entry
            reg["parameters"].append(entry)
            cur = entry
        if used_by and used_by not in cur["used_by"]:
            cur["used_by"].append(used_by)
        return cur

    # mapping parameters + variables
    for m in pipeline.mappings:
        text = _mapping_text(m)
        for v in m.properties.get("variables") or []:
            name = v["name"] if str(v["name"]).startswith("$$") \
                else "$$" + str(v["name"])
            cls = classify(name, is_param=v.get("is_param"),
                           aggregation=v.get("aggregation", ""))
            put(_entry(name, "mapping", cls, datatype=v.get("datatype", ""),
                       default=v.get("default", ""),
                       aggregation=v.get("aggregation", ""),
                       used_by=[]), used_by=m.name)
            if cls != "stateful_variable" or any(
                    i.code.startswith("STATEFUL_VARIABLE")
                    and name in i.message for i in m.issues):
                continue
            if name not in text:
                # declared but never referenced in active logic — no
                # behavior to lose, so no review demanded
                m.add_issue(
                    IssueSeverity.INFO, "STATEFUL_VARIABLE_UNREFERENCED",
                    "Stateful mapping variable %s is declared but not "
                    "referenced in the mapping's active logic — nothing "
                    "to convert" % name)
                continue
            m.add_issue(
                IssueSeverity.MANUAL, "STATEFUL_VARIABLE",
                "Mapping variable %s is STATEFUL (ISPARAM=NO%s) — "
                "PowerCenter persisted its value across runs in the "
                "repository. It was NOT silently converted to a "
                "static var" % (name,
                                ", aggregation %s" % v["aggregation"]
                                if v.get("aggregation") else ""),
                suggestion="Reproduce the state: dbt — incremental "
                           "watermark (SELECT COALESCE(MAX(<col>), "
                           "'%s') FROM {{ this }}) or a state table; "
                           "Databricks — task values or a Delta "
                           "state table; warehouses — a watermark "
                           "table updated at end of load."
                           % (v.get("default", "")))
        # system / environment $PM references used inside the mapping
        for pm in sorted(set(_PM_RE.findall(_mapping_text(m)))):
            cls = classify("$" + pm)
            put(_entry("$" + pm, "system", cls), used_by=m.name)

    # workflow variables
    for wf in workflows or []:
        for name, v in (wf.get("variables") or {}).items():
            canonical = name if str(name).startswith("$$") \
                else "$$" + str(name)
            cls = classify(canonical, persistent=v.get("persistent"),
                           is_param=True)
            e = put(_entry(canonical, "workflow", cls,
                           datatype=v.get("datatype", ""),
                           default=v.get("default", "")),
                    used_by=wf.get("name", ""))
            if cls == "stateful_variable" and not any(
                    i.code == "STATEFUL_VARIABLE" and canonical in i.message
                    for i in pipeline.issues):
                pipeline.issues.append(ConversionIssue(
                    severity=IssueSeverity.MANUAL,
                    code="STATEFUL_VARIABLE",
                    message="Workflow variable %s is persistent — its "
                            "value survives across runs and was NOT "
                            "silently converted" % canonical,
                    suggestion=e["warehouses"]))

    # session parameter files (module 24 already warned per session)
    for m in pipeline.mappings:
        pf = (m.properties.get("session_cir") or {}).get("parameter_file")
        if pf and pf not in reg["parameter_files"]:
            reg["parameter_files"].append(pf)

    summary: Dict[str, int] = {}
    for e in reg["parameters"]:
        summary[e["classification"]] = \
            summary.get(e["classification"], 0) + 1
    reg["summary"] = summary
    return reg

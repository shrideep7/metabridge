"""Session parser (Phase 2, module 24).

A PowerCenter session is RUNTIME configuration for a mapping — engine
tuning, connections, error policy, operational commands. It is never
transformation logic, so nothing here touches the mapping graph or the
generated SQL. The session becomes:

    props: session_cir = {mapping reference, source/target connections,
                          parameter_file, commit_interval,
                          error_threshold, dtm_buffer_size, partitioning,
                          pushdown_optimization, tracing_level,
                          pre/post_session_commands, target_load_type}

plus MIGRATION RECOMMENDATIONS — each runtime setting mapped to what
replaces it on a modern target (all warehouses; Databricks is one
example):

    pushdown optimization   already the architecture: every converted
                            transformation executes natively on the
                            target engine (Spark SQL/Delta, Snowflake,
                            BigQuery, ...) — pushdown is the default,
                            not an option
    partitioning            replaced by the target's native parallelism;
                            carry the keys into physical table layout
    commit interval         set-based statements are atomic; row-batch
                            commits do not carry over
    error threshold         row-level error tolerance does not exist in
                            set-based SQL — validation tests + __rejects
                            quarantine replace it
    DTM buffer              obsolete; targets size memory automatically
    tracing level           target-native query history / logging
    pre/post-session cmds   shell commands cannot become SQL — they move
                            to the orchestrator (declared MANUAL, text
                            preserved)
    bulk load type          native bulk ingestion (COPY INTO, LOAD DATA)
    parameter file          values are NOT in the export — map $$params
                            to dbt vars / session variables at deploy
"""
from __future__ import annotations

from typing import List, Optional

from ..ir.model import IssueSeverity, Mapping

_PUSHDOWN_NORM = {
    "to source": "to_source", "to target": "to_target", "full": "full",
}


def _to_int(v: Optional[str]) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def build_session_cir(session: dict) -> dict:
    """Extract the runtime contract from a parsed SESSION dict."""
    attrs = session.get("attributes") or {}
    a = {k.lower(): v for k, v in attrs.items()}

    sources, targets = [], []
    partition_count = 0
    partition_types: List[str] = []
    for ext in session.get("extensions") or []:
        conns = [{"instance": ext.get("instance", ""),
                  "name": c.get("name", ""), "type": c.get("type", "")}
                 for c in ext.get("connections") or []]
        et = (ext.get("type") or "").upper()
        if et == "READER":
            sources.extend(conns)
        elif et == "WRITER":
            targets.extend(conns)
        partition_count = max(partition_count,
                              len(ext.get("partitions") or []))
        ptype = (ext.get("attributes") or {}).get("Partition Type", "")
        if ptype and ptype not in partition_types:
            partition_types.append(ptype)

    pre_cmds, post_cmds = [], []
    for comp in session.get("components") or []:
        ctype = (comp.get("type") or "").lower()
        cmds = list(comp.get("commands") or []) or \
            ([comp["task"]] if comp.get("task") else [])
        if "pre-session" in ctype:
            pre_cmds.extend(cmds)
        elif "post-session" in ctype:
            post_cmds.extend(cmds)

    pushdown_raw = (a.get("pushdown optimization") or "").strip().lower()
    pushdown = _PUSHDOWN_NORM.get(pushdown_raw) \
        if pushdown_raw and pushdown_raw != "none" else None
    tracing = (a.get("override tracing") or a.get("tracing level")
               or "").strip()

    return {
        "session": session.get("name", ""),
        "mapping": session.get("mapping", ""),
        "source_connections": sources,
        "target_connections": targets,
        "parameter_file": (a.get("parameter filename") or "").strip(),
        "commit_interval": _to_int(a.get("commit interval")),
        "error_threshold": _to_int(a.get("stop on errors")),
        "dtm_buffer_size": (a.get("dtm buffer size") or "").strip(),
        "partitioning": {"partition_count": partition_count,
                         "types": partition_types}
        if partition_count > 1 or partition_types else None,
        "pushdown_optimization": pushdown,
        "tracing_level": tracing or None,
        "pre_session_commands": pre_cmds,
        "post_session_commands": post_cmds,
        "target_load_type": (a.get("target load type") or "").strip()
        or None,
    }


def apply_session_cir(mapping: Mapping, cir: dict) -> None:
    """Attach the runtime contract and its migration recommendations.
    NEVER restructures the graph — session configuration is not
    transformation logic."""
    if "session_cir" in mapping.properties:
        return                       # first session wins (rare: multiple)
    mapping.properties["session_cir"] = cir

    def rec(severity, code, message, suggestion=None):
        mapping.add_issue(severity, code, message, suggestion=suggestion)

    if cir["pushdown_optimization"]:
        rec(IssueSeverity.INFO, "SESSION_PUSHDOWN",
            "Session used Pushdown Optimization (%s) — the converted "
            "pipeline already executes EVERY transformation natively on "
            "the target engine, so pushdown is the architecture, not an "
            "option" % cir["pushdown_optimization"],
            suggestion="No action: Databricks runs the logic as Spark "
                       "SQL over Delta; Snowflake/BigQuery/Redshift/"
                       "Synapse/Teradata run it as native SQL. The "
                       "PowerCenter engine hop is gone.")
    if cir["partitioning"]:
        p = cir["partitioning"]
        rec(IssueSeverity.INFO, "SESSION_PARTITIONING",
            "Session defined %d pipeline partition(s)%s — session-level "
            "partitioning is replaced by the target's native parallelism"
            % (p["partition_count"],
               " (%s)" % ", ".join(p["types"]) if p["types"] else ""),
            suggestion="MPP warehouses and Spark parallelize "
                       "automatically. If the partition keys were chosen "
                       "for data layout, carry them into the physical "
                       "design instead (Databricks: partition/ZORDER "
                       "columns; Snowflake: clustering keys; BigQuery: "
                       "partition + cluster columns).")
    if cir["commit_interval"]:
        rec(IssueSeverity.INFO, "SESSION_COMMIT_INTERVAL",
            "Commit interval %d does not carry over — converted loads "
            "are set-based and atomic per statement/model, not row-batch "
            "committed" % cir["commit_interval"])
    thr = cir["error_threshold"]
    if thr is not None:
        rec(IssueSeverity.WARNING, "SESSION_ERROR_THRESHOLD",
            "'Stop on errors' = %d: row-level error tolerance does not "
            "exist in set-based SQL — a statement succeeds or fails "
            "atomically%s"
            % (thr, "; the session tolerated unlimited row errors "
               "SILENTLY, review what used to be dropped" if thr == 0
               else ""),
            suggestion="Use the generated validation tests for the "
                       "checks, and the __rejects quarantine pattern "
                       "(Update Strategy handler) for row-level "
                       "isolation.")
    if cir["dtm_buffer_size"]:
        rec(IssueSeverity.INFO, "SESSION_DTM_BUFFER",
            "DTM buffer size '%s' is obsolete — target engines manage "
            "memory automatically; no equivalent setting is needed"
            % cir["dtm_buffer_size"])
    if cir["tracing_level"] and \
            cir["tracing_level"].lower() not in ("none", "normal"):
        rec(IssueSeverity.INFO, "SESSION_TRACING",
            "Tracing level '%s' maps to target-native observability, "
            "not a pipeline setting" % cir["tracing_level"],
            suggestion="Databricks: cluster logs + query profile; "
                       "Snowflake: QUERY_HISTORY; BigQuery: "
                       "INFORMATION_SCHEMA.JOBS; dbt: --log-level debug "
                       "and artifacts.")
    for stage, cmds in (("pre", cir["pre_session_commands"]),
                        ("post", cir["post_session_commands"])):
        if cmds:
            rec(IssueSeverity.MANUAL, "SESSION_SHELL_COMMAND",
                "%s-session shell command(s) cannot be converted to SQL: "
                "%s — move them to the orchestrator"
                % (stage.capitalize(), "; ".join(cmds)),
                suggestion="Run as an orchestrator step %s the load "
                           "(Databricks Jobs task, Airflow operator, or "
                           "the scheduler of your warehouse). dbt hooks "
                           "run SQL only — shell steps belong outside "
                           "the model." % ("before" if stage == "pre"
                                           else "after"))
    if (cir["target_load_type"] or "").lower() == "bulk":
        rec(IssueSeverity.INFO, "SESSION_BULK_LOAD",
            "Bulk target load type maps to the target's native bulk "
            "ingestion — set-based INSERT/MERGE and COPY INTO/LOAD are "
            "already bulk paths; no row-mode fallback exists to avoid")
    if cir["parameter_file"]:
        rec(IssueSeverity.WARNING, "SESSION_PARAMETER_FILE",
            "Session reads parameter file '%s' — parameter VALUES are "
            "not part of the repository export" % cir["parameter_file"],
            suggestion="Map $$parameters to dbt vars / warehouse session "
                       "variables and supply the values from the file at "
                       "deploy time (the mapping's variables are listed "
                       "in its CIR).")
    if cir["source_connections"] or cir["target_connections"]:
        rec(IssueSeverity.INFO, "SESSION_CONNECTIONS",
            "Connections — sources: %s; targets: %s"
            % (", ".join(sorted({c["name"] for c in
                                 cir["source_connections"] if c["name"]}))
               or "-",
               ", ".join(sorted({c["name"] for c in
                                 cir["target_connections"] if c["name"]}))
               or "-"),
            suggestion="Recreate as target-side configuration: dbt "
                       "profiles.yml / sources, warehouse external "
                       "locations, or catalog connections — never inside "
                       "the transformation SQL.")

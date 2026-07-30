"""Enterprise FinOps Engine.

Models the run-cost of a data estate and the economics of optimizing /
migrating it. MetaBridge holds METADATA, not billing telemetry, so every
figure here is a MODELED estimate from explicitly labelled planning
assumptions — declared as such — unless the caller supplies real
telemetry (credits, storage GB, query volume), which replaces the
matching modeled figure and is marked "measured". NOTHING calls an LLM.

Analyzed (spec order):

    warehouse_utilization   active vs idle warehouse hours
    cloud_storage           GB and $/mo by platform
    streaming_cost          topics x partitions + streaming jobs
    compute_cost            pipeline runs x runtime
    data_movement           cross-platform / CDC egress
    idle_resources          unused assets (from the debt reachability)
    query_history           modeled or measured query spend
    etl_runtime             total pipeline runtime hours
    pipeline_efficiency     incremental adoption, dead/duplicate waste

Generated: current cost, future (optimized) cost, migration cost, ROI,
payback period, reserved-capacity recommendations, warehouse sizing,
cluster recommendations, and Snowflake / Databricks / BigQuery / Fabric
optimization playbooks.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..twin.model import DigitalTwin, twin_from_dict

_HOURS_PER_MONTH = 730
_PLATFORM_TOKENS = ("snowflake", "databricks", "bigquery", "fabric",
                    "synapse", "redshift", "postgres")
_FULL_REFRESH = {"FULL"}
_INCREMENTAL = {"MERGE", "DELETE_INSERT", "APPEND", "SCD2"}

# --- labelled planning assumptions (list-price planning figures) -----------
FINOPS_ASSUMPTIONS = {
    "avg_table_size_gb": 25.0,
    "storage_usd_per_gb_month": {
        "snowflake": 0.023, "databricks": 0.021, "bigquery": 0.020,
        "fabric": 0.023, "synapse": 0.024, "redshift": 0.024,
        "postgres": 0.10, "default": 0.023},
    "pipeline_runs_per_month": 30,
    "avg_pipeline_runtime_min": 8.0,
    "compute_usd_per_min": {           # blended warehouse/cluster $/min
        "snowflake": 0.067, "databricks": 0.058, "bigquery": 0.060,
        "fabric": 0.063, "synapse": 0.070, "redshift": 0.065,
        "postgres": 0.030, "default": 0.060},
    "partition_usd_month": 1.2,
    "streaming_job_usd_month": 180.0,
    "queries_per_endpoint_month": 3000,
    "avg_tb_scanned_per_query": 0.05,
    "query_usd_per_tb": 5.0,
    "egress_usd_per_gb": 0.09,
    "cross_boundary_gb_per_edge_month": 40.0,
    "reserved_discount_pct": 30,       # committed-use vs on-demand
    "reserved_steady_share": 0.6,      # portion of compute that is steady
    "rightsizing_saving_pct": 20,      # on the burst/dev portion
    "incremental_saving_pct": 40,      # full-refresh -> incremental
    "storage_optimization_pct": 12,    # compression / partitioning
    "migration_hours_per_pipeline": 6.0,
    "migration_hours_per_table": 0.5,
    "blended_rate_usd_per_hour": 95.0,
    "note": "MODELED from estate metadata, NOT measured bills — supply "
            "telemetry (credits, storage GB, query volume) to replace "
            "each modeled figure; rates are list-price planning figures, "
            "replace with negotiated / committed pricing before "
            "budgeting",
}


def _rate(table: str, platform: str, a=FINOPS_ASSUMPTIONS):
    d = a[table]
    return d.get(platform, d["default"])


def _platforms(twin: DigitalTwin) -> List[str]:
    found = []
    for n in twin.nodes.values():
        tech = str(n.technology or "").lower()
        for tok in _PLATFORM_TOKENS:
            if tok in tech and tok not in found:
                found.append(tok)
    return found


def _primary_platform(twin: DigitalTwin, pipelines: list) -> str:
    plats = _platforms(twin)
    if plats:
        # warehouse platforms win over operational DBs
        for p in ("snowflake", "databricks", "bigquery", "fabric",
                  "synapse", "redshift"):
            if p in plats:
                return p
        return plats[0]
    for p in pipelines or []:
        fmt = str(getattr(p, "source_format", "")).lower()
        if fmt in _PLATFORM_TOKENS:
            return fmt
    return "default"


# ---------------------------------------------------------------------------
# analyses
# ---------------------------------------------------------------------------

def _inventory(twin: DigitalTwin) -> dict:
    kinds: Dict[str, list] = {}
    for n in twin.nodes.values():
        kinds.setdefault(n.kind, []).append(n)
    topics = kinds.get("topic", [])
    partitions = 0
    for t in topics:
        try:
            partitions += int(t.metadata.get("partitions", 1) or 1)
        except (TypeError, ValueError):
            partitions += 1
    return {
        "warehouses": kinds.get("warehouse", []),
        "tables": kinds.get("table", []),
        # all workloads (for migration/sizing); batch-only pipelines are
        # what draw warehouse COMPUTE — streaming jobs bill under
        # streaming, never compute
        "pipelines": kinds.get("pipeline", []) + kinds.get("streaming_job",
                                                           []),
        "pipelines_only": kinds.get("pipeline", []),
        "streaming_jobs": kinds.get("streaming_job", []),
        "topics": topics, "partitions": partitions,
        "endpoints": (kinds.get("dashboard", []) + kinds.get("api", [])
                      + kinds.get("data_product", [])),
        "databases": kinds.get("database", []),
    }


def analyze_finops(twin, pipelines: Optional[list] = None,
                   telemetry: Optional[dict] = None) -> dict:
    """Deterministic FinOps model. twin: DigitalTwin or dict; pipelines:
    parsed IR; telemetry: optional measured figures that override the
    matching modeled component."""
    if isinstance(twin, dict):
        twin = twin_from_dict(twin)
    if pipelines is None:
        pipelines = []
    elif not isinstance(pipelines, (list, tuple)):
        pipelines = [pipelines]              # tolerate a single Pipeline
    tele = telemetry or {}
    a = FINOPS_ASSUMPTIONS
    inv = _inventory(twin)
    platform = _primary_platform(twin, pipelines)
    measured, modeled = [], []

    # ---- cloud storage ----
    n_tables = len(inv["tables"])
    if "storage_gb" in tele:
        storage_gb = float(tele["storage_gb"])
        measured.append("cloud_storage")
    else:
        storage_gb = n_tables * a["avg_table_size_gb"]
        modeled.append("cloud_storage")
    storage_month = storage_gb * _rate("storage_usd_per_gb_month",
                                       platform)

    # ---- compute cost / etl runtime ----
    n_pipe = len(inv["pipelines"])            # batch + streaming workloads
    n_pipe_only = len(inv["pipelines_only"])  # batch — draws compute
    runtime_min_total = 0.0
    for p in pipelines:
        for m in p.mappings:
            xf = max(1, len([t for t in m.transformations
                             if t.name != "__OUTPUT__"]))
            runtime_min_total += a["avg_pipeline_runtime_min"] * \
                (1 + 0.15 * (xf - 1))       # heavier graphs run longer
    if not pipelines:
        # streaming jobs bill under streaming, NOT compute — model
        # compute from batch pipeline nodes only to avoid double-billing
        runtime_min_total = n_pipe_only * a["avg_pipeline_runtime_min"]
    runs = a["pipeline_runs_per_month"]
    if "monthly_compute_credits" in tele and "credit_price_usd" in tele:
        compute_month = float(tele["monthly_compute_credits"]) * \
            float(tele["credit_price_usd"])
        measured.append("compute_cost")
    else:
        compute_month = runtime_min_total * runs * \
            _rate("compute_usd_per_min", platform)
        modeled.append("compute_cost")
    etl_runtime_hours_month = round(runtime_min_total * runs / 60.0, 1)

    # ---- streaming ----
    streaming_month = (inv["partitions"] * a["partition_usd_month"]
                       + len(inv["streaming_jobs"])
                       * a["streaming_job_usd_month"])
    if inv["topics"] or inv["streaming_jobs"]:
        modeled.append("streaming_cost")

    # ---- query history ----
    if "monthly_query_usd" in tele:
        query_month = float(tele["monthly_query_usd"])
        measured.append("query_history")
    else:
        query_month = (len(inv["endpoints"])
                       * a["queries_per_endpoint_month"]
                       * a["avg_tb_scanned_per_query"]
                       * a["query_usd_per_tb"])
        modeled.append("query_history")

    # ---- data movement ----
    # count each cross-system hop ONCE. A CDC edge (database --feeds-->
    # topic) is both a cross-technology hop AND a CDC source; count it in
    # the CDC bucket only so it is not billed twice.
    cross = 0
    cdc_edges = 0
    for e in twin.edges.values():
        if e.kind not in ("feeds", "writes", "reads", "produces",
                           "consumes"):
            continue
        na, nb = twin.nodes[e.from_id], twin.nodes[e.to_id]
        is_cdc = (e.kind == "feeds" and na.kind == "database"
                  and nb.kind == "topic")
        if is_cdc:
            cdc_edges += 1
            continue
        ta = str(na.technology or "").lower()
        tb = str(nb.technology or "").lower()
        if ta and tb and ta != tb:
            cross += 1
    cdc = cdc_edges
    movement_gb = (cross + cdc) * a["cross_boundary_gb_per_edge_month"]
    movement_month = movement_gb * a["egress_usd_per_gb"]

    current_month = (storage_month + compute_month + streaming_month
                     + query_month + movement_month)

    # ---- idle resources (from the debt reachability graph) ----
    # Attribute idle cost as a SHARE of the actual (possibly
    # telemetry-measured) cost slice each waste type sits in, capped at
    # that slice — never a standalone modeled figure that could exceed
    # the measured bill. Dead batch pipelines hit compute; dead
    # streaming jobs and unconsumed topics hit streaming.
    from ..debt.engine import _reachability_debt
    reach = _reachability_debt(twin)
    dead_list = reach.get("dead_etl", [])
    n_unused_tables = len(reach.get("unused_tables", []))
    n_dead_etl = len(dead_list)
    dead_pipelines = sum(1 for f in dead_list if f.get("kind") == "pipeline")
    dead_stream = sum(1 for f in dead_list
                      if f.get("kind") == "streaming_job")
    n_unused_topics = len(reach.get("unused_kafka_topics", []))

    idle_storage = min(storage_month,
                       storage_month * (n_unused_tables / n_tables)
                       if n_tables else 0.0)
    idle_compute = min(compute_month,
                       compute_month * (dead_pipelines / n_pipe_only)
                       if n_pipe_only else 0.0)
    partition_cost = inv["partitions"] * a["partition_usd_month"]
    idle_streaming = dead_stream * a["streaming_job_usd_month"]
    if inv["topics"]:
        idle_streaming += partition_cost * \
            (n_unused_topics / len(inv["topics"]))
    idle_streaming = min(streaming_month, idle_streaming)
    idle_month = idle_storage + idle_compute + idle_streaming

    # ---- warehouse utilization ----
    scheduled = sum(1 for p in inv["pipelines"])
    if "warehouse_utilization_pct" in tele:
        util = float(tele["warehouse_utilization_pct"])
        measured.append("warehouse_utilization")
    else:
        active_hours = min(_HOURS_PER_MONTH,
                           scheduled * runs *
                           a["avg_pipeline_runtime_min"] / 60.0)
        util = round(100 * active_hours / _HOURS_PER_MONTH, 1)
        modeled.append("warehouse_utilization")

    # ---- pipeline efficiency ----
    full = incr = 0
    for p in pipelines:
        for m in p.mappings:
            if m.load_strategy.value in _FULL_REFRESH:
                full += 1
            elif m.load_strategy.value in _INCREMENTAL:
                incr += 1
    total_loads = full + incr
    incr_pct = round(100 * incr / total_loads, 1) if total_loads else 0.0
    waste_ratio = round((n_dead_etl + n_unused_tables)
                        / max(1, len(inv["tables"]) + n_pipe), 3)
    efficiency = max(0, min(100, round(
        0.5 * incr_pct + 0.5 * (100 - 100 * waste_ratio))))

    analyses = {
        "warehouse_utilization": {
            "utilization_pct": util,
            "assessment": ("over-provisioned — consolidate/auto-suspend"
                           if util < 25 else "healthy" if util < 75
                           else "hot — consider scaling out"),
            "active_pipelines": scheduled},
        "cloud_storage": {"storage_gb": round(storage_gb, 1),
                          "monthly_usd": round(storage_month, 0),
                          "tables": n_tables, "platform": platform},
        "streaming_cost": {"monthly_usd": round(streaming_month, 0),
                           "topics": len(inv["topics"]),
                           "partitions": inv["partitions"],
                           "streaming_jobs": len(inv["streaming_jobs"])},
        "compute_cost": {"monthly_usd": round(compute_month, 0),
                         "pipelines": n_pipe_only,
                         "runs_per_month": runs},
        "data_movement": {"monthly_usd": round(movement_month, 0),
                          "cross_platform_hops": cross,
                          "cdc_sources": cdc,
                          "estimated_gb_month": round(movement_gb, 0)},
        "idle_resources": {
            "monthly_usd": round(idle_month, 0),
            "unused_tables": n_unused_tables, "dead_etl": n_dead_etl,
            "unused_topics": n_unused_topics,
            "pct_of_current": round(100 * idle_month / current_month, 1)
            if current_month else 0.0},
        "query_history": {"monthly_usd": round(query_month, 0),
                          "endpoints": len(inv["endpoints"]),
                          "modeled_queries_month":
                          len(inv["endpoints"])
                          * a["queries_per_endpoint_month"]},
        "etl_runtime": {"runtime_hours_month": etl_runtime_hours_month,
                        "pipelines": n_pipe,
                        "avg_min_per_run": a["avg_pipeline_runtime_min"]},
        "pipeline_efficiency": {
            "efficiency_score": efficiency,
            "incremental_adoption_pct": incr_pct,
            "full_refresh_loads": full, "incremental_loads": incr,
            "waste_ratio": waste_ratio},
    }

    outputs = _outputs(a, platform, twin, pipelines, inv,
                       {"storage": storage_month, "compute": compute_month,
                        "streaming": streaming_month, "query": query_month,
                        "movement": movement_month,
                        "current": current_month},
                       {"idle_storage": idle_storage,
                        "idle_compute": idle_compute,
                        "idle_streaming": idle_streaming,
                        "idle_month": idle_month},
                       full, total_loads, n_tables, n_pipe)

    return {
        "tool": "MetaBridge AI — Enterprise FinOps",
        "estate": twin.name,
        "primary_platform": platform,
        "platforms_detected": _platforms(twin),
        "analyses": analyses,
        **outputs,
        "data_basis": {"measured_from_telemetry": sorted(set(measured)),
                       "modeled_from_metadata": sorted(set(modeled))},
        "determinism_note": "deterministic model from estate metadata; "
                            "components not backed by supplied telemetry "
                            "are modeled estimates, not bills",
        "assumptions": FINOPS_ASSUMPTIONS,
    }


# ---------------------------------------------------------------------------
# generated outputs (costs, ROI, recommendations, platform playbooks)
# ---------------------------------------------------------------------------

def _outputs(a, platform, twin, pipelines, inv, cost, idle,
             full_loads, total_loads, n_tables, n_pipe) -> dict:
    # future cost — sequential savings levers (no double counting):
    # 1) remove idle  2) incremental conversion  3) reserved+rightsizing
    # 4) storage optimization
    comp = cost["compute"] - idle["idle_compute"]
    stor = cost["storage"] - idle["idle_storage"]
    strm = cost["streaming"] - idle["idle_streaming"]

    full_share = (full_loads / total_loads) if total_loads else 0.0
    incr_saving = comp * full_share * a["incremental_saving_pct"] / 100.0
    comp -= incr_saving

    reserved_saving = comp * a["reserved_steady_share"] * \
        a["reserved_discount_pct"] / 100.0
    rightsize_saving = comp * (1 - a["reserved_steady_share"]) * \
        a["rightsizing_saving_pct"] / 100.0
    comp -= (reserved_saving + rightsize_saving)

    storage_saving = stor * a["storage_optimization_pct"] / 100.0
    stor -= storage_saving

    # round each component first, then derive the totals from the
    # rounded parts, so every displayed figure reconciles: the current
    # components sum to the current total, monthly x 12 == annual, and
    # current - future == savings, all to the dollar
    cur_parts = {"storage": round(cost["storage"]),
                 "compute": round(cost["compute"]),
                 "streaming": round(cost["streaming"]),
                 "query": round(cost["query"]),
                 "data_movement": round(cost["movement"])}
    cur_m = sum(cur_parts.values())
    fut_parts = {"storage": round(stor), "compute": round(comp),
                 "streaming": round(strm), "query": round(cost["query"]),
                 "data_movement": round(cost["movement"])}
    fut_m = sum(fut_parts.values())
    monthly_savings = max(0, cur_m - fut_m)
    annual_savings = monthly_savings * 12

    savings_levers = [
        {"lever": "Decommission idle resources",
         "monthly_usd": round(idle["idle_month"], 0)},
        {"lever": "Convert full-refresh to incremental",
         "monthly_usd": round(incr_saving, 0)},
        {"lever": "Reserved / committed-use capacity",
         "monthly_usd": round(reserved_saving, 0)},
        {"lever": "Right-size compute",
         "monthly_usd": round(rightsize_saving, 0)},
        {"lever": "Storage compression / partitioning",
         "monthly_usd": round(storage_saving, 0)},
    ]

    # migration / optimization one-time cost
    mig_hours = (n_pipe * a["migration_hours_per_pipeline"]
                 + n_tables * a["migration_hours_per_table"])
    migration_cost = round(mig_hours * a["blended_rate_usd_per_hour"], 0)

    if monthly_savings <= 0:
        payback_months, roi_y1, roi_3y = None, None, None
        payback_text = "no modeled savings — payback not applicable"
        roi_note = "not applicable — no savings modeled"
    elif migration_cost <= 0:
        # savings with no modeled migration investment -> immediate
        # payback; ROI is undefined (division by zero), NOT zero
        payback_months, roi_y1, roi_3y = 0.0, None, None
        payback_text = "immediate — no migration investment modeled"
        roi_note = ("not applicable — no migration investment modeled "
                    "(the estate has no pipelines/tables to migrate)")
    else:
        payback_months = round(migration_cost / monthly_savings, 1)
        roi_y1 = round(100 * (annual_savings - migration_cost)
                       / migration_cost, 0)
        roi_3y = round(100 * (3 * annual_savings - migration_cost)
                       / migration_cost, 0)
        payback_text = "%.1f months" % payback_months
        roi_note = ("net return: (savings - one-time cost) / one-time "
                    "cost")

    return {
        "current_cost": {
            "monthly_usd": cur_m,
            "annual_usd": cur_m * 12,
            "by_component_monthly_usd": cur_parts},
        "future_cost": {
            "monthly_usd": fut_m,
            "annual_usd": fut_m * 12,
            "monthly_savings_usd": monthly_savings,
            "annual_savings_usd": annual_savings,
            "reduction_pct": round(100 * monthly_savings / cur_m, 1)
            if cur_m else 0.0,
            "savings_levers": savings_levers},
        "migration_cost": {
            "one_time_usd": migration_cost,
            "engineer_hours": round(mig_hours, 1),
            "basis": "%.1f hr/pipeline x %d + %.1f hr/table x %d at $%d/hr"
                     % (a["migration_hours_per_pipeline"], n_pipe,
                        a["migration_hours_per_table"], n_tables,
                        a["blended_rate_usd_per_hour"])},
        "roi": {"first_year_pct": roi_y1, "three_year_pct": roi_3y,
                "annual_savings_usd": round(annual_savings, 0),
                "note": roi_note},
        "payback_period": {"months": payback_months, "text": payback_text},
        "reserved_capacity_recommendations":
            _reserved(a, platform, comp_baseline=cost["compute"]
                      - idle["idle_compute"]),
        "warehouse_sizing": _sizing(pipelines, n_pipe, inv),
        "cluster_recommendations": _clusters(inv, n_pipe),
        "snowflake_optimization": _snowflake(platform, inv, full_loads),
        "databricks_optimization": _databricks(platform, inv),
        "bigquery_optimization": _bigquery(platform, inv),
        "fabric_optimization": _fabric(platform, inv),
    }


def _reserved(a, platform, comp_baseline) -> dict:
    steady = comp_baseline * a["reserved_steady_share"]
    saving = steady * a["reserved_discount_pct"] / 100.0
    mech = {"snowflake": "Snowflake capacity / pre-purchased credits",
            "databricks": "Databricks committed-use DBUs (DBCU)",
            "bigquery": "BigQuery Editions slot commitments (1yr/3yr)",
            "fabric": "Microsoft Fabric reserved capacity (F-SKU)",
            "synapse": "Synapse reserved capacity",
            "redshift": "Redshift reserved nodes",
            "default": "committed-use / reserved capacity"}
    return {
        "mechanism": mech.get(platform, mech["default"]),
        "steady_baseline_usd_month": round(steady, 0),
        "recommended_commit_pct": int(a["reserved_steady_share"] * 100),
        "estimated_saving_usd_month": round(saving, 0),
        "estimated_saving_pct": a["reserved_discount_pct"],
        "note": "commit only the steady baseline; keep burst/dev on "
                "on-demand — confirm the baseline against real usage "
                "before signing a commitment"}


_SIZE_LADDER = ["X-Small", "Small", "Medium", "Large", "X-Large"]


def _sizing(pipelines, n_pipe, inv) -> dict:
    max_xf = 0
    for p in pipelines or []:
        for m in p.mappings:
            max_xf = max(max_xf, len([t for t in m.transformations
                                      if t.name != "__OUTPUT__"]))
    # size from concurrency (pipeline count) and heaviest graph
    idx = 0
    if n_pipe > 3 or max_xf > 4:
        idx = 1
    if n_pipe > 10 or max_xf > 8:
        idx = 2
    if n_pipe > 30 or max_xf > 14:
        idx = 3
    if n_pipe > 80:
        idx = 4
    multi = n_pipe > 20
    return {
        "recommended_size": _SIZE_LADDER[idx],
        "multi_cluster": multi,
        "basis": "%d pipeline(s), heaviest graph %d transformation(s)"
                 % (n_pipe, max_xf),
        "note": "start one size below for dev/test; enable auto-suspend "
                "(60s) and auto-resume; split heavy batch and interactive "
                "BI onto separate warehouses to stop them competing"}


def _clusters(inv, n_pipe) -> dict:
    streaming = len(inv["streaming_jobs"])
    return {
        "min_workers": 1,
        "max_workers": max(2, min(16, n_pipe // 2 or 2)),
        "autoscaling": True,
        "spot_instances": "use for non-SLA batch (60-90% cheaper); "
                          "keep on-demand for streaming/critical",
        "auto_termination_min": 10,
        "recommendations": [
            "enable Photon for SQL/ETL (2-3x price-performance)",
            "job clusters for scheduled pipelines, not all-purpose",
            "serverless SQL warehouses for BI concurrency",
            ("dedicated always-on cluster for %d streaming job(s)"
             % streaming) if streaming else
            "no streaming jobs — no always-on cluster needed"],
        "basis": "%d pipeline(s), %d streaming job(s)" % (n_pipe,
                                                          streaming)}


def _play(platform, key, items) -> dict:
    return {"in_estate": platform == key or (key == "fabric"
            and platform in ("fabric", "synapse")),
            "recommendations": items}


def _snowflake(platform, inv, full_loads) -> dict:
    items = [
        "set auto-suspend to 60s and auto-resume on every warehouse",
        "separate warehouses per workload (ETL / BI / data science) so "
        "they scale and bill independently",
        "multi-cluster warehouses (auto) for high-concurrency BI instead "
        "of one oversized warehouse",
        "lean on result cache + materialized views for repeated BI",
        "add clustering keys on large, filtered fact tables",
        "run resource monitors with credit quotas + alerts",
        "push heavy transforms to Snowpark; avoid full-table rebuilds"
        + (" (%d full-refresh load(s) detected)" % full_loads
           if full_loads else ""),
        "adopt pre-purchased capacity for the steady baseline",
    ]
    return _play(platform, "snowflake", items)


def _databricks(platform, inv) -> dict:
    items = [
        "enable Photon on all SQL/ETL clusters",
        "autoscaling + spot workers + 10-min auto-termination",
        "job clusters for scheduled work, serverless SQL for BI",
        "OPTIMIZE + Z-ORDER (or liquid clustering) on hot Delta tables",
        "VACUUM to reclaim storage; tune file sizes to avoid small-file "
        "overhead",
        "Delta Live Tables for declarative, auto-managed pipelines",
        "Unity Catalog for governance + cross-workspace cost attribution",
        "committed-use DBUs for the steady baseline",
    ]
    return _play(platform, "databricks", items)


def _bigquery(platform, inv) -> dict:
    items = [
        "choose Editions (Standard/Enterprise) with autoscaling slot "
        "reservations over pure on-demand for steady workloads",
        "partition + cluster large tables to cut bytes scanned",
        "materialized views + BI Engine for dashboards",
        "set per-query and per-project bytes-billed limits",
        "prefer physical-bytes storage billing for compressed data",
        "expire staging tables/partitions automatically",
        "avoid SELECT * — select only needed columns to reduce scan cost",
        "1yr/3yr slot commitments for the steady baseline",
    ]
    return _play(platform, "bigquery", items)


def _fabric(platform, inv) -> dict:
    items = [
        "right-size the capacity SKU (F2..F2048) to actual peak, not "
        "headroom",
        "pause capacity outside business hours; schedule resume",
        "use Direct Lake mode in Power BI to avoid import refresh cost",
        "consolidate workloads onto shared capacity via pools",
        "store in OneLake / Delta once; avoid duplicate copies per "
        "engine",
        "enable autoscale only for genuine spikes, with a ceiling",
        "reserved (1yr) capacity for the steady baseline vs pay-as-you-go",
    ]
    return _play(platform, "fabric", items)


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------

def analyze_from_paths(paths: Optional[List[str]] = None,
                       estate_docs: Optional[List[dict]] = None,
                       telemetry: Optional[dict] = None,
                       include_connections: bool = False,
                       jobs_dir: Optional[str] = None) -> dict:
    from ..twin.discover import build_twin
    from ..debt.engine import _parse_pipelines
    twin = build_twin(paths=paths, estate_docs=estate_docs,
                      include_connections=include_connections,
                      jobs_dir=jobs_dir)
    return analyze_finops(twin, _parse_pipelines(paths), telemetry)

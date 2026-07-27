"""Observability engine — deterministic operational monitoring of
MetaBridge's own run history plus modeled estate resource/cloud figures.

Provenance is explicit on every monitor via ``basis``:
  measured  — derived from real run/job/connection facts (status,
              timestamps, durations, test results);
  modeled   — derived from estate topology (resource, cloud) — not live
              metering;
  no_data   — nothing observed yet.

Nothing is invented; an empty history yields ``no_data``, not a
flattering default.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

MONITORS = ("pipeline_health", "migration_progress", "validation_status",
            "agent_health", "connector_health", "performance", "latency",
            "failures", "resource_utilization", "cloud_consumption")

# service-level objectives (targets); actuals are computed from history
DEFAULT_SLA = {
    "availability_pct": 99.0,       # successful runs / total
    "latency_p95_ms": 60000,        # p95 action/run latency
    "max_failure_rate_pct": 5.0,    # failed / total
}

# below this many latency samples, a p95 is not a meaningful percentile —
# don't let it drive the health score, an SLO or an alert
MIN_LATENCY_SAMPLES = 5

# alert rules (documentation of what fires; evaluated in _build_alerts)
ALERT_RULES = (
    {"id": "failure_rate", "monitor": "failures",
     "warning": 20.0, "critical": 50.0, "unit": "% failed"},
    {"id": "latency_p95", "monitor": "latency",
     "warning": "sla.latency_p95_ms", "unit": "ms p95"},
    {"id": "health_score", "monitor": "health_score",
     "warning": 60, "critical": 40, "unit": "score"},
    {"id": "connector_down", "monitor": "connector_health",
     "warning": 1, "unit": "connectors failing/stopped"},
    {"id": "availability", "monitor": "pipeline_health",
     "warning": "sla.availability_pct", "unit": "% available"},
)

# health-score weights over the available sub-scores
WEIGHTS = {"pipeline": 2.0, "reliability": 2.0, "agents": 2.0,
           "validation": 1.5, "connectors": 1.0, "latency": 1.0}

_BUCKET_CONVERSION = ("convert", "modern", "etl", "sap", "legacy", "sql",
                      "scaffold", "deploy", "event", "orchestr", "migrat")


# --------------------------------------------------------------------------
# small numeric helpers
# --------------------------------------------------------------------------
def _int(v) -> int:
    """Safe int coercion — a wrong-typed value (str/None/list) becomes 0
    instead of crashing (these fields come from files other components
    wrote, which we do not control)."""
    return int(v) if isinstance(v, (int, float)) else 0


def _mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def _median(xs: List[float]):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _percentile(xs: List[float], p: float):
    """Nearest-rank percentile (p in [0,100])."""
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    import math
    rank = max(1, math.ceil(p / 100.0 * len(xs)))
    return xs[min(rank, len(xs)) - 1]


def _band(score: Optional[float]) -> str:
    if score is None:
        return "no_data"
    if score >= 90:
        return "healthy"
    if score >= 75:
        return "ok"
    if score >= 50:
        return "degraded"
    return "critical"


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _parse_ts(s: str) -> Optional[datetime.datetime]:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s)[:19])
    except (ValueError, TypeError):
        return None


def _job_bucket(kind: str) -> str:
    k = (kind or "").lower()
    if k == "upload":
        return "upload"
    if k == "agents":
        return "agent"
    if any(t in k for t in _BUCKET_CONVERSION):
        return "conversion"
    return "analysis"


# --------------------------------------------------------------------------
# signal readers
# --------------------------------------------------------------------------
def _data_dir(data_dir: str = "") -> Path:
    return Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR", "."))


def _read_jobs(base: Path) -> List[dict]:
    jobs_dir = base / "jobs"
    out = []
    if not jobs_dir.exists():
        return out
    for meta in sorted(jobs_dir.glob("*/meta.json")):
        try:
            m = json.loads(meta.read_text())
        except (ValueError, OSError):
            continue
        if not isinstance(m, dict):
            continue
        m["_bucket"] = _job_bucket(m.get("kind", ""))
        c = _parse_ts(m.get("created", ""))
        f = _parse_ts(m.get("finished", ""))
        m["_duration_ms"] = (int((f - c).total_seconds() * 1000)
                             if c and f and f >= c else None)
        out.append(m)
    return out


def _read_agent_runs(base: Path) -> List[dict]:
    runs_dir = base / "agents" / "runs"
    out = []
    if not runs_dir.exists():
        return out
    for f in sorted(runs_dir.glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        # results MUST be a list — a dict/str would be iterated as keys/chars
        # by every downstream consumer and crash them
        if isinstance(r, dict) and isinstance(r.get("results"), list) \
                and r["results"]:
            out.append(r)
    return out


def _read_connections(base: Path) -> List[dict]:
    """Read connections from the SAME resolved data dir as jobs/twin.
    (connections_store reads os.environ independently, which would make
    observe(data_dir=X) mix X's jobs with the env tenant's connections —
    so read base/connections.json directly and drop any secrets.)"""
    f = base / "connections.json"
    if not f.exists():
        return []
    try:
        rows = json.loads(f.read_text()) or []
    except (ValueError, OSError):
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict):
            out.append({k: v for k, v in r.items() if k != "secrets"})
    return out


def _read_twin(base: Path):
    f = base / "twin.json"
    if not f.exists():
        return None
    try:
        from ..twin.model import twin_from_dict
        return twin_from_dict(json.loads(f.read_text()))
    except Exception:                            # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# monitors
# --------------------------------------------------------------------------
def _mon_pipeline_health(jobs: List[dict]) -> dict:
    runs = [j for j in jobs if j["_bucket"] in ("conversion", "analysis")]
    if not runs:
        return {"basis": "no_data", "status": "no_data",
                "headline": "No pipeline runs observed yet.", "total": 0}
    done = sum(1 for j in runs if j.get("status") == "done")
    failed = sum(1 for j in runs if j.get("status") == "failed")
    running = sum(1 for j in runs if j.get("status") == "running")
    completed = done + failed
    success_rate = _pct(done, completed) if completed else 0.0
    score = success_rate if completed else None
    return {"basis": "measured", "status": _band(score),
            "score": score, "total": len(runs), "done": done,
            "failed": failed, "running": running,
            "success_rate_pct": success_rate,
            "headline": "%d/%d runs succeeded (%.0f%%), %d in flight, "
                        "%d failed." % (done, completed, success_rate,
                                        running, failed)}


def _mon_migration_progress(jobs: List[dict], runs: List[dict]) -> dict:
    conv = [j for j in jobs if j["_bucket"] == "conversion"]
    proposals = 0
    for r in runs:
        for row in r.get("results", []):
            out = row.get("outputs")
            if row.get("agent_id") == "migration" and \
                    isinstance(out, dict) and out.get("migration"):
                proposals += 1
    if not conv and not proposals:
        return {"basis": "no_data", "status": "no_data",
                "headline": "No migrations run yet.", "total": 0,
                "proposals": 0}
    done = sum(1 for j in conv if j.get("status") == "done")
    failed = sum(1 for j in conv if j.get("status") == "failed")
    running = sum(1 for j in conv if j.get("status") == "running")
    pct = _pct(done, len(conv)) if conv else 0.0
    return {"basis": "measured", "status": "in_progress" if running else
            ("complete" if conv and done == len(conv) else "partial"),
            "total": len(conv), "completed": done, "failed": failed,
            "running": running, "percent_complete": pct,
            "agent_proposals": proposals,
            "headline": "%d/%d migration runs complete (%.0f%%); "
                        "%d agent proposal(s)." % (done, len(conv), pct,
                                                   proposals)}


def _mon_validation_status(jobs: List[dict], runs: List[dict]) -> dict:
    confs = []
    cyclic = 0
    manual = 0
    samples = 0
    for r in runs:
        for row in r.get("results", []):
            out = row.get("outputs")
            v = out.get("validation") if isinstance(out, dict) else None
            if isinstance(v, dict):
                samples += 1
                ac = v.get("avg_confidence")
                if isinstance(ac, (int, float)):      # ignore label-typed
                    confs.append(float(ac))
                cyclic += _int(v.get("cyclic_pipelines"))
                manual += _int(v.get("manual_review_items"))
    if not samples:
        return {"basis": "no_data", "status": "no_data",
                "headline": "No validation runs observed yet."}
    avg = _mean(confs) or 0.0
    status = ("pass" if avg >= 80 and cyclic == 0
              else "warn" if avg >= 60 else "fail")
    return {"basis": "measured", "status": status, "score": round(avg),
            "runs": samples, "avg_conversion_confidence": round(avg),
            "cyclic_pipelines": cyclic, "manual_review_items": manual,
            "headline": "Validation %s: avg conversion confidence %.0f%% "
                        "over %d run(s); %d cyclic, %d manual item(s)."
                        % (status, avg, samples, cyclic, manual)}


def _mon_agent_health(runs: List[dict]) -> dict:
    if not runs:
        return {"basis": "no_data", "status": "no_data",
                "headline": "No agent runs observed yet.", "runs": 0}
    total = failed = denied = needs_appr = 0
    confs = []
    per_agent: Dict[str, dict] = {}
    for r in runs:
        for row in r.get("results", []):
            total += 1
            st = row.get("status")
            dec = row.get("decision")
            if st == "failed":
                failed += 1
            if dec == "deny":
                denied += 1
            if st == "needs_approval":
                needs_appr += 1
            if isinstance(row.get("confidence"), (int, float)):
                confs.append(row["confidence"])
            a = per_agent.setdefault(row.get("agent_id", "?"),
                                     {"runs": 0, "failed": 0, "conf": []})
            a["runs"] += 1
            if st == "failed":
                a["failed"] += 1
            if isinstance(row.get("confidence"), (int, float)):
                a["conf"].append(row["confidence"])
    mean_conf = _mean(confs) or 0.0
    fail_rate = _pct(failed + denied, total)
    score = round(max(0.0, mean_conf * 100 - fail_rate))
    agents = sorted(({"agent": k, "runs": v["runs"], "failed": v["failed"],
                      "mean_confidence": round((_mean(v["conf"]) or 0) * 100)}
                     for k, v in per_agent.items()),
                    key=lambda x: x["agent"])
    return {"basis": "measured", "status": _band(score), "score": score,
            "runs": len(runs), "actions": total, "failed": failed,
            "denied": denied, "needs_approval": needs_appr,
            "mean_confidence_pct": round(mean_conf * 100), "agents": agents,
            "headline": "%d agent run(s), %d action(s); mean confidence "
                        "%.0f%%, %d failed, %d awaiting approval."
                        % (len(runs), total, mean_conf * 100, failed,
                           needs_appr)}


def _mon_connector_health(conns: List[dict]) -> dict:
    if not conns:
        return {"basis": "no_data", "status": "no_data",
                "headline": "No connections configured.", "total": 0}
    active = sum(1 for c in conns if c.get("status") == "active")
    stopped = sum(1 for c in conns if c.get("status") == "stopped")
    tested = [c for c in conns if isinstance(c.get("last_test"), dict)]
    # strict-bool: an arbitrary truthy 'ok' (e.g. "no") is NOT a pass
    ok = sum(1 for c in tested if c["last_test"].get("ok") is True)
    failing = sum(1 for c in tested if c["last_test"].get("ok") is not True)
    latencies = [c["last_test"].get("latency_ms") for c in tested
                 if isinstance(c["last_test"].get("latency_ms"), (int, float))]
    # a connection is healthy iff it is active AND (untested OR its last
    # test passed) — counted per-connection on ONE population so a stopped
    # connection's failing test can't be subtracted from actives
    healthy = sum(1 for c in conns if c.get("status") == "active" and not (
        isinstance(c.get("last_test"), dict) and
        c["last_test"].get("ok") is not True))
    score = _pct(healthy, len(conns))
    return {"basis": "measured", "status": _band(score), "score": score,
            "total": len(conns), "active": active, "stopped": stopped,
            "tested": len(tested), "test_ok": ok, "test_failing": failing,
            "avg_test_latency_ms": round(_mean(latencies)) if latencies
            else None,
            "headline": "%d connection(s): %d active, %d stopped; "
                        "%d tested OK, %d failing." % (len(conns), active,
                                                       stopped, ok, failing)}


def _all_durations(jobs: List[dict], runs: List[dict],
                   conns: List[dict]) -> Dict[str, List[float]]:
    action = []                                  # per-agent action durations
    run_total = []                               # per-run summed duration
    job = []                                     # per-job wall clock
    conn = []                                    # connection test latency
    for r in runs:
        s = 0
        for row in r.get("results", []):
            d = row.get("duration_ms")
            if isinstance(d, (int, float)):
                action.append(d)
                s += d
        if s:
            run_total.append(s)
    for j in jobs:
        if isinstance(j.get("_duration_ms"), (int, float)):
            job.append(j["_duration_ms"])
    for c in conns:
        lt = c.get("last_test")
        if isinstance(lt, dict) and isinstance(lt.get("latency_ms"),
                                               (int, float)):
            conn.append(lt["latency_ms"])
    return {"action_ms": action, "run_ms": run_total, "job_ms": job,
            "connection_ms": conn}


def _mon_performance(jobs: List[dict], runs: List[dict],
                     durs: Dict[str, List[float]]) -> dict:
    completed = [j for j in jobs if j.get("status") in ("done", "failed")
                 and j["_bucket"] != "upload"]
    total_ops = len(completed) + len(runs)
    if not total_ops:
        return {"basis": "no_data", "status": "no_data",
                "headline": "No completed operations to measure."}
    # throughput over the observed span
    dates = [_parse_ts(j.get("created", "")) for j in jobs] + \
            [_parse_ts(r.get("created_at", "")) for r in runs]
    dates = [d for d in dates if d]
    span_days = max(1, ((max(dates) - min(dates)).days + 1)) if dates else 1
    mean_run = _mean(durs["run_ms"])
    mean_job = _mean(durs["job_ms"])
    have_durations = bool(durs["run_ms"] or durs["job_ms"])
    # honest: only claim MEASURED performance when at least one real
    # duration was captured; otherwise this is a throughput count only
    return {"basis": "measured" if have_durations else "count_only",
            "status": "ok" if have_durations else "count_only",
            "operations": total_ops, "span_days": span_days,
            "throughput_per_day": round(total_ops / span_days, 2),
            "mean_run_ms": round(mean_run) if mean_run is not None else None,
            "mean_job_ms": round(mean_job) if mean_job is not None else None,
            "headline": "%d operation(s) over %d day(s) — %.2f/day; %s"
                        % (total_ops, span_days, total_ops / span_days,
                           ("mean agent run %s" % _ms(mean_run)) if
                           have_durations else "no duration data captured")}


def _mon_latency(durs: Dict[str, List[float]]) -> dict:
    samples = durs["action_ms"] + durs["connection_ms"]
    if not samples:
        return {"basis": "no_data", "status": "no_data",
                "headline": "No latency samples measured yet."}
    p50 = _percentile(samples, 50)
    p95 = _percentile(samples, 95)
    thin = len(samples) < MIN_LATENCY_SAMPLES
    return {"basis": "measured", "status": "ok",
            "samples": len(samples), "p50_ms": round(p50),
            "p95_ms": round(p95), "max_ms": round(max(samples)),
            "action_samples": len(durs["action_ms"]),
            "connection_samples": len(durs["connection_ms"]),
            "sufficient_for_percentile": not thin,
            "headline": "p50 %s, p95 %s over %d sample(s)%s."
                        % (_ms(p50), _ms(p95), len(samples),
                           " — too few for a reliable percentile" if thin
                           else "")}


def _mon_failures(jobs: List[dict], runs: List[dict]) -> dict:
    # derive failed_jobs FROM completed so the numerator is always a subset
    # of the denominator (failed uploads must not inflate the rate > 100%)
    completed = [j for j in jobs if j.get("status") in ("done", "failed")
                 and j["_bucket"] != "upload"]
    failed_jobs = [j for j in completed if j.get("status") == "failed"]
    agent_failed = agent_denied = agent_actions = 0
    recent = []
    for r in runs:
        for row in r.get("results", []):
            agent_actions += 1
            if row.get("status") == "failed":
                agent_failed += 1
                recent.append({"kind": "agent", "id": row.get("agent_id"),
                               "run": r.get("run_id"),
                               "detail": (str(row.get("error") or ""))[:160]})
            if row.get("decision") == "deny":
                agent_denied += 1
    for j in failed_jobs:
        recent.append({"kind": "job", "id": j.get("id"),
                       "job_kind": j.get("kind"),
                       "detail": (str(j.get("error") or ""))[:160]})
    total = len(completed) + agent_actions
    if total == 0:
        return {"basis": "no_data", "status": "no_data",
                "failure_rate_pct": 0.0, "failures": 0, "failed_jobs": 0,
                "agent_failed": 0, "agent_denied": 0, "recent": [],
                "headline": "No completed operations to measure failures."}
    failures = len(failed_jobs) + agent_failed
    rate = _pct(failures, total)
    return {"basis": "measured",
            "status": "critical" if rate >= 50 else "degraded" if rate >= 20
            else "ok",
            "failure_rate_pct": rate, "failures": failures,
            "failed_jobs": len(failed_jobs), "agent_failed": agent_failed,
            "agent_denied": agent_denied, "recent": recent[:10],
            "headline": "%.1f%% failure rate — %d failed job(s), "
                        "%d failed agent action(s), %d denied."
                        % (rate, len(failed_jobs), agent_failed,
                           agent_denied)}


def _mon_resource_utilization(twin) -> dict:
    if twin is None or not getattr(twin, "nodes", None):
        return {"basis": "no_data", "status": "no_data",
                "headline": "No estate twin built — resource view "
                            "unavailable."}
    counts = twin.counts()
    warehouses = counts.get("warehouse", 0) + counts.get("database", 0)
    tables = counts.get("table", 0)
    pipelines = counts.get("pipeline", 0)
    streaming = counts.get("streaming_job", 0)
    workloads = pipelines + streaming
    # a MODELED load index: how much work rides on each compute target
    per_wh = round(workloads / warehouses, 1) if warehouses else workloads
    idx = min(100, round(per_wh * 8))            # bounded modeled index
    return {"basis": "modeled", "status": _band(100 - idx),
            "modeled_utilization_index": idx,
            "compute_targets": warehouses, "tables": tables,
            "pipelines": pipelines, "streaming_jobs": streaming,
            "workloads_per_target": per_wh,
            "headline": "Modeled from topology: %d workload(s) across %d "
                        "compute target(s) (%.1f/target). Not live metering."
                        % (workloads, warehouses, per_wh)}


def _mon_cloud_consumption(twin) -> dict:
    if twin is None or not getattr(twin, "nodes", None):
        return {"basis": "no_data", "status": "no_data",
                "headline": "No estate twin built — cost model unavailable."}
    try:
        from ..finops.engine import analyze_finops
        fin = analyze_finops(twin, pipelines=None)
    except Exception:                            # noqa: BLE001
        return {"basis": "no_data", "status": "no_data",
                "headline": "Cost model unavailable."}
    cur = fin.get("current_cost", {}) or {}
    monthly = cur.get("monthly_usd")
    return {"basis": "modeled", "status": "ok",
            "monthly_usd": monthly, "annual_usd": cur.get("annual_usd"),
            "by_component_monthly_usd": cur.get("by_component_monthly_usd", {}),
            "primary_platform": fin.get("primary_platform"),
            "headline": "Modeled cloud spend ~$%s/mo (topology-based; feed "
                        "telemetry for billed figures)."
                        % (monthly if monthly is not None else "?")}


def _ms(v) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    return "%.0f ms" % v if v < 1000 else "%.1f s" % (v / 1000.0)


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------
# minimum real operations before a composite band is trustworthy — below
# this a perfect score off n=1 must NOT read as a healthy track record
MIN_OPS_FOR_BAND = 5


def health_score(monitors: dict) -> dict:
    """Composite 0-100 over the available monitor sub-scores."""
    subs = {
        "pipeline": monitors["pipeline_health"].get("score"),
        "reliability": (100.0 - monitors["failures"].get("failure_rate_pct")
                        if monitors["failures"].get("basis") == "measured"
                        else None),
        "agents": monitors["agent_health"].get("score"),
        "validation": monitors["validation_status"].get("score"),
        "connectors": monitors["connector_health"].get("score"),
        "latency": None,
    }
    lat = monitors["latency"]
    if lat.get("basis") == "measured" and lat.get("p95_ms") is not None \
            and lat.get("samples", 0) >= MIN_LATENCY_SAMPLES:
        subs["latency"] = 100.0 if lat["p95_ms"] <= \
            DEFAULT_SLA["latency_p95_ms"] else round(max(
                0.0, 100.0 * DEFAULT_SLA["latency_p95_ms"] / lat["p95_ms"]))
    num = den = 0.0
    contributors = {}
    for k, v in subs.items():
        if v is None:
            continue
        w = WEIGHTS[k]
        num += float(v) * w
        den += w
        contributors[k] = round(float(v))
    if den == 0:
        return {"score": None, "band": "no_data",
                "headline": "Not enough operational history to score.",
                "contributors": {}, "sample": "insufficient"}
    score = round(num / den)
    detractors = sorted(contributors.items(), key=lambda kv: kv[1])[:3]
    # how many REAL operations back this score (not the weight sum) — a
    # perfect score off one job must not present as a healthy history
    ph = monitors["pipeline_health"]
    ah = monitors["agent_health"]
    ops = (_int(ph.get("done")) + _int(ph.get("failed"))
           + (_int(ah.get("actions")) if ah.get("basis") == "measured" else 0))
    thin = ops < MIN_OPS_FOR_BAND
    band = "insufficient" if thin else _band(score)
    return {"score": score, "band": band,
            "contributors": contributors, "operations": ops,
            "sample": "thin" if thin else "adequate",
            "top_detractors": [{"area": k, "score": v} for k, v in detractors],
            "headline": ("Operational health %d/100 from %d signal(s) over "
                         "only %d operation(s) — treat as provisional."
                         % (score, len(contributors), ops)) if thin else
                        ("Operational health %d/100 (%s), from %d signal(s) "
                         "over %d operation(s)."
                         % (score, band, len(contributors), ops))}


def _build_sla(monitors: dict, sla: dict) -> dict:
    ph = monitors["pipeline_health"]
    lat = monitors["latency"]
    fail = monitors["failures"]
    objectives = []

    # availability
    avail_actual = ph.get("success_rate_pct")
    n_avail = _int(ph.get("done")) + _int(ph.get("failed"))
    if ph.get("basis") == "measured" and n_avail > 0:
        target = sla["availability_pct"]
        budget = max(0.0, 100.0 - target)
        consumed = (min(100.0, (100.0 - avail_actual) / budget * 100.0)
                    if budget > 0 else (0.0 if avail_actual >= 100 else 100.0))
        objectives.append({
            "name": "Availability", "unit": "%", "target": target,
            "actual": avail_actual, "met": avail_actual >= target,
            "error_budget_consumed_pct": round(consumed, 1),
            "sample": n_avail,
            "low_confidence": n_avail < MIN_OPS_FOR_BAND})

    # latency — only a real SLO once there are enough samples for a p95
    if lat.get("basis") == "measured" and lat.get("p95_ms") is not None \
            and lat.get("samples", 0) >= MIN_LATENCY_SAMPLES:
        target = sla["latency_p95_ms"]
        objectives.append({
            "name": "Latency (p95)", "unit": "ms", "target": target,
            "actual": lat["p95_ms"], "met": lat["p95_ms"] <= target,
            "samples": lat.get("samples")})

    # failure rate
    if fail.get("basis") == "measured":
        target = sla["max_failure_rate_pct"]
        objectives.append({
            "name": "Failure rate", "unit": "%", "target": target,
            "actual": fail["failure_rate_pct"],
            "met": fail["failure_rate_pct"] <= target})

    if not objectives:
        return {"status": "no_data", "objectives": [],
                "headline": "No SLA history yet."}
    met = sum(1 for o in objectives if o["met"])
    return {"status": "met" if met == len(objectives) else "breached",
            "objectives": objectives, "met": met, "total": len(objectives),
            "headline": "%d/%d SLOs met." % (met, len(objectives))}


def _build_alerts(monitors: dict, health: dict, sla: dict) -> dict:
    alerts = []

    def add(sev, monitor, rule, message, value=None, threshold=None):
        alerts.append({"id": "%s:%s" % (monitor, rule), "severity": sev,
                       "monitor": monitor, "rule": rule, "message": message,
                       "value": value, "threshold": threshold})

    fail = monitors["failures"]
    if fail.get("basis") == "measured":
        fr = fail["failure_rate_pct"]
        if fr >= 50:
            add("critical", "failures", "failure_rate",
                "Failure rate %.1f%% (>=50%%)" % fr, fr, 50)
        elif fr >= 20:
            add("warning", "failures", "failure_rate",
                "Failure rate %.1f%% (>=20%%)" % fr, fr, 20)

    lat = monitors["latency"]
    if lat.get("basis") == "measured" and lat.get("p95_ms") is not None \
            and lat.get("samples", 0) >= MIN_LATENCY_SAMPLES \
            and lat["p95_ms"] > sla["latency_p95_ms"]:
        add("warning", "latency", "latency_p95",
            "p95 latency %d ms exceeds SLA %d ms"
            % (lat["p95_ms"], sla["latency_p95_ms"]), lat["p95_ms"],
            sla["latency_p95_ms"])

    if health.get("score") is not None:
        if health["score"] < 40:
            add("critical", "health_score", "health_score",
                "Health score %d (<40)" % health["score"], health["score"], 40)
        elif health["score"] < 60:
            add("warning", "health_score", "health_score",
                "Health score %d (<60)" % health["score"], health["score"], 60)

    conn = monitors["connector_health"]
    if conn.get("basis") == "measured":
        down = conn.get("test_failing", 0) + conn.get("stopped", 0)
        if down >= 1:
            add("warning", "connector_health", "connector_down",
                "%d connector(s) stopped or failing" % down, down, 1)

    ph = monitors["pipeline_health"]
    if ph.get("basis") == "measured" and ph.get("done", 0) + \
            ph.get("failed", 0) > 0 and \
            ph["success_rate_pct"] < sla["availability_pct"]:
        add("warning", "pipeline_health", "availability",
            "Availability %.1f%% below SLA %.1f%%"
            % (ph["success_rate_pct"], sla["availability_pct"]),
            ph["success_rate_pct"], sla["availability_pct"])

    sev_rank = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: sev_rank.get(a["severity"], 9))
    counts = {"critical": 0, "warning": 0, "info": 0}
    for a in alerts:
        counts[a["severity"]] = counts.get(a["severity"], 0) + 1
    return {"alerts": alerts, "counts": counts, "total": len(alerts),
            "headline": "%d alert(s): %d critical, %d warning."
                        % (len(alerts), counts["critical"], counts["warning"])}


def _build_trends(jobs: List[dict], runs: List[dict]) -> dict:
    by_day: Dict[str, dict] = {}

    def bump(day, ok, dur):
        d = by_day.setdefault(day, {"date": day, "ops": 0, "ok": 0,
                                    "failed": 0, "_dur": []})
        d["ops"] += 1
        if ok is True:
            d["ok"] += 1
        elif ok is False:
            d["failed"] += 1
        if dur:
            d["_dur"].append(dur)

    for j in jobs:
        if j["_bucket"] == "upload":
            continue
        c = _parse_ts(j.get("created", ""))
        if not c:
            continue
        st = j.get("status")
        bump(c.date().isoformat(),
             True if st == "done" else False if st == "failed" else None,
             j.get("_duration_ms"))
    for r in runs:
        c = _parse_ts(r.get("created_at", ""))
        if not c:
            continue
        failed = r.get("summary", {}).get("failed", 0)
        dur = sum(row.get("duration_ms", 0) or 0
                  for row in r.get("results", []))
        bump(c.date().isoformat(), failed == 0, dur or None)

    series = []
    for day in sorted(by_day):
        d = by_day[day]
        md = _mean(d["_dur"])
        series.append({"date": day, "operations": d["ops"], "ok": d["ok"],
                       "failed": d["failed"],
                       "success_rate_pct": _pct(d["ok"], d["ok"] + d["failed"]),
                       "mean_duration_ms": round(md) if md is not None
                       else None})
    direction = "stable"
    if len(series) >= 2:
        half = len(series) // 2
        first = _mean([s["success_rate_pct"] for s in series[:half]]) or 0
        last = _mean([s["success_rate_pct"] for s in series[half:]]) or 0
        direction = ("improving" if last > first + 5 else
                     "declining" if last < first - 5 else "stable")
    return {"series": series, "days": len(series), "direction": direction,
            "headline": "%d day(s) of history; trend %s."
                        % (len(series), direction)}


def _build_historical(jobs: List[dict], runs: List[dict],
                      durs: Dict[str, List[float]]) -> dict:
    by_kind: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    for j in jobs:
        if j["_bucket"] == "upload":
            continue
        by_kind[j.get("kind", "?")] = by_kind.get(j.get("kind", "?"), 0) + 1
        by_status[j.get("status", "?")] = by_status.get(
            j.get("status", "?"), 0) + 1
    completed = by_status.get("done", 0) + by_status.get("failed", 0)
    dates = [_parse_ts(j.get("created", "")) for j in jobs] + \
            [_parse_ts(r.get("created_at", "")) for r in runs]
    dates = [d for d in dates if d]
    span = None
    if dates:
        span = {"from": min(dates).date().isoformat(),
                "to": max(dates).date().isoformat()}
    mjob = _mean(durs["job_ms"])
    mrun = _mean(durs["run_ms"])
    return {"jobs_total": sum(by_kind.values()), "agent_runs_total": len(runs),
            "by_kind": by_kind, "by_status": by_status,
            "overall_success_rate_pct": _pct(by_status.get("done", 0),
                                             completed) if completed else 0.0,
            "date_span": span,
            "mean_job_ms": round(mjob) if mjob is not None else None,
            "mean_run_ms": round(mrun) if mrun is not None else None,
            "median_action_ms": round(_median(durs["action_ms"]))
            if durs["action_ms"] else None,
            "headline": "%d job(s) + %d agent run(s) analyzed."
                        % (sum(by_kind.values()), len(runs))}


# --------------------------------------------------------------------------
# top-level
# --------------------------------------------------------------------------
def observe(data_dir: str = "", sla: Optional[dict] = None,
            as_of: str = "") -> dict:
    base = _data_dir(data_dir)
    jobs = _read_jobs(base)
    runs = _read_agent_runs(base)
    conns = _read_connections(base)
    twin = _read_twin(base)
    durs = _all_durations(jobs, runs, conns)
    sla_cfg = dict(DEFAULT_SLA)
    if sla:
        sla_cfg.update(sla)

    def _safe(fn, *args):
        """One malformed input another component wrote must degrade a single
        monitor to no_data, never abort (500) the whole report."""
        try:
            return fn(*args)
        except Exception as exc:                 # noqa: BLE001
            return {"basis": "no_data", "status": "no_data",
                    "headline": "monitor unavailable (%s)"
                                % type(exc).__name__}

    monitors = {
        "pipeline_health": _safe(_mon_pipeline_health, jobs),
        "migration_progress": _safe(_mon_migration_progress, jobs, runs),
        "validation_status": _safe(_mon_validation_status, jobs, runs),
        "agent_health": _safe(_mon_agent_health, runs),
        "connector_health": _safe(_mon_connector_health, conns),
        "performance": _safe(_mon_performance, jobs, runs, durs),
        "latency": _safe(_mon_latency, durs),
        "failures": _safe(_mon_failures, jobs, runs),
        "resource_utilization": _safe(_mon_resource_utilization, twin),
        "cloud_consumption": _safe(_mon_cloud_consumption, twin),
    }
    health = health_score(monitors)
    sla_dash = _build_sla(monitors, sla_cfg)
    alerts = _build_alerts(monitors, health, sla_cfg)
    trends = _build_trends(jobs, runs)
    historical = _build_historical(jobs, runs, durs)

    operational = {
        "health_score": health,
        "monitors": {k: {"status": v.get("status"), "basis": v.get("basis"),
                         "headline": v.get("headline"),
                         "score": v.get("score")}
                     for k, v in monitors.items()},
        "active": {"running_jobs": sum(1 for j in jobs
                                       if j.get("status") == "running"),
                   "pending_approvals": monitors["agent_health"].get(
                       "needs_approval", 0)},
        "alerts": alerts["counts"],
    }

    return {
        "tool": "MetaBridge Observability Engine",
        "as_of": as_of,
        "determinism_note": ("Deterministic. Success/failure, agent-action "
                             "durations and connection tests are MEASURED "
                             "from run history; per-job wall-clock is "
                             "measured only where a finish time was recorded. "
                             "Resource utilization and cloud consumption are "
                             "MODELED from estate topology, not live "
                             "metering."),
        "sla_config": sla_cfg,
        "monitors": monitors,
        "operational_dashboard": operational,
        "sla_dashboard": sla_dash,
        "alerting": alerts,
        "health_score": health,
        "performance_trends": trends,
        "historical_analytics": historical,
    }

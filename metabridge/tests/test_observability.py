"""Observability engine — signal collection, the ten monitors and the six
outputs (operational, SLA, alerting, health score, trends, historical)."""
import json
from pathlib import Path

import pytest

from metabridge.observability import observe, health_score, DEFAULT_SLA


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "wk"))
    (tmp_path / "wk").mkdir(parents=True, exist_ok=True)


def _job(base, jid, kind, status, created, finished=None):
    d = base / "jobs" / jid
    d.mkdir(parents=True, exist_ok=True)
    meta = {"id": jid, "kind": kind, "status": status, "created": created}
    if finished:
        meta["finished"] = finished
    if status == "failed":
        meta["error"] = "boom"
    (d / "meta.json").write_text(json.dumps(meta))


def _run(base, rid, created_at, results, failed=0):
    d = base / "agents" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    rep = {"run_id": rid, "created_at": created_at,
           "summary": {"failed": failed}, "results": results,
           "audit": {"verification": {"intact": True}, "events": []}}
    (d / ("%s.json" % rid)).write_text(json.dumps(rep))


def _result(agent_id, status="ok", decision="allow", confidence=0.9,
            duration_ms=100, outputs=None, action_class="read_only"):
    return {"agent_id": agent_id, "task_type": agent_id,
            "action_class": action_class, "status": status,
            "decision": decision, "confidence": confidence,
            "confidence_level": "high", "duration_ms": duration_ms,
            "summary": "s", "outputs": outputs or {}}


@pytest.fixture()
def workspace(tmp_path):
    base = tmp_path / "wk"
    # jobs: conversions + analyses, mix of done/failed/running
    _job(base, "j1", "convert", "done", "2026-07-10T09:00:00",
         "2026-07-10T09:00:20")
    _job(base, "j2", "modernize", "failed", "2026-07-10T10:00:00",
         "2026-07-10T10:00:05")
    _job(base, "j3", "convert", "running", "2026-07-11T10:00:00")
    _job(base, "j4", "ai_readiness", "done", "2026-07-11T11:00:00",
         "2026-07-11T11:00:10")
    _job(base, "j5", "assessment", "done", "2026-07-12T11:00:00",
         "2026-07-12T11:00:08")
    _job(base, "up", "upload", "done", "2026-07-12T08:00:00")   # ignored
    # an agent run with validation output + a failed action + a migration
    _run(base, "r1", "2026-07-12T12:00:00", [
        _result("discovery", duration_ms=50),
        _result("parse", duration_ms=800),
        _result("validation", confidence=0.95, duration_ms=120,
                outputs={"validation": {"avg_confidence": 95,
                                        "cyclic_pipelines": 0,
                                        "manual_review_items": 2}}),
        _result("security", status="failed", confidence=0.0,
                duration_ms=30),
        _result("migration", action_class="generate",
                decision="needs_approval", status="needs_approval",
                confidence=0.8, duration_ms=200,
                outputs={"migration": {"avg_confidence": 80}}),
    ], failed=1)
    # connections
    (base / "connections.json").write_text(json.dumps([
        {"id": "c1", "connector": "snowflake", "name": "Prod",
         "status": "active",
         "last_test": {"ok": True, "at": "2026-07-12T12:00:00",
                       "latency_ms": 240}},
        {"id": "c2", "connector": "oracle", "name": "Legacy",
         "status": "stopped",
         "last_test": {"ok": False, "at": "2026-07-12T12:00:00",
                       "latency_ms": None, "error": "timeout"}},
    ]))
    return base


# --- shape ----------------------------------------------------------------

def test_observe_shape(workspace):
    r = observe()
    for k in ("monitors", "operational_dashboard", "sla_dashboard",
              "alerting", "health_score", "performance_trends",
              "historical_analytics", "determinism_note"):
        assert k in r
    assert set(r["monitors"]) == {
        "pipeline_health", "migration_progress", "validation_status",
        "agent_health", "connector_health", "performance", "latency",
        "failures", "resource_utilization", "cloud_consumption"}


# --- monitors -------------------------------------------------------------

def test_pipeline_health_measured(workspace):
    ph = observe()["monitors"]["pipeline_health"]
    assert ph["basis"] == "measured"
    # 4 completed (j1,j2 done/failed conversions + j4,j5 done analyses),
    # 3 succeeded of 4 completed (j1,j4,j5), j2 failed, j3 running
    assert ph["done"] == 3 and ph["failed"] == 1 and ph["running"] == 1
    assert ph["success_rate_pct"] == 75.0


def test_migration_progress(workspace):
    mp = observe()["monitors"]["migration_progress"]
    assert mp["basis"] == "measured"
    assert mp["total"] == 3          # convert/modernize conversions
    assert mp["completed"] == 1 and mp["failed"] == 1 and mp["running"] == 1
    assert mp["agent_proposals"] == 1


def test_validation_status(workspace):
    v = observe()["monitors"]["validation_status"]
    assert v["basis"] == "measured" and v["status"] == "pass"
    assert v["avg_conversion_confidence"] == 95 and v["manual_review_items"] == 2


def test_agent_health(workspace):
    a = observe()["monitors"]["agent_health"]
    assert a["basis"] == "measured"
    assert a["actions"] == 5 and a["failed"] == 1 and a["needs_approval"] == 1
    assert 0 <= a["score"] <= 100


def test_connector_health(workspace):
    c = observe()["monitors"]["connector_health"]
    assert c["basis"] == "measured"
    assert c["total"] == 2 and c["active"] == 1 and c["stopped"] == 1
    assert c["test_ok"] == 1 and c["test_failing"] == 1
    assert c["avg_test_latency_ms"] == 240


def test_latency_measured(workspace):
    lat = observe()["monitors"]["latency"]
    assert lat["basis"] == "measured"
    # action durations 50,800,120,30,200 + connection 240
    assert lat["p50_ms"] is not None and lat["p95_ms"] is not None
    assert lat["max_ms"] == 800


def test_failures(workspace):
    f = observe()["monitors"]["failures"]
    assert f["basis"] == "measured"
    assert f["failed_jobs"] == 1 and f["agent_failed"] == 1
    assert f["failure_rate_pct"] > 0
    assert any(x["kind"] == "job" for x in f["recent"])
    assert any(x["kind"] == "agent" for x in f["recent"])


def test_resource_and_cloud_no_data_without_twin(workspace):
    m = observe()["monitors"]
    assert m["resource_utilization"]["basis"] == "no_data"
    assert m["cloud_consumption"]["basis"] == "no_data"


def test_resource_and_cloud_modeled_with_twin(workspace):
    from metabridge.twin.model import DigitalTwin
    t = DigitalTwin("est")
    t.add_node("warehouse", "wh1", "x", technology="snowflake")
    t.add_node("table", "t1", "x")
    t.add_node("pipeline", "p1", "x")
    t.add_node("pipeline", "p2", "x")
    (workspace / "twin.json").write_text(json.dumps(t.to_dict()))
    m = observe()["monitors"]
    assert m["resource_utilization"]["basis"] == "modeled"
    assert m["resource_utilization"]["pipelines"] == 2
    assert m["cloud_consumption"]["basis"] == "modeled"
    # honesty: the headline must disclose it is modeled, not metered
    assert "modeled" in m["cloud_consumption"]["headline"].lower()


# --- outputs --------------------------------------------------------------

def test_health_score_composite(workspace):
    h = observe()["health_score"]
    assert h["score"] is not None and 0 <= h["score"] <= 100
    assert h["band"] in ("healthy", "ok", "degraded", "critical")
    assert h["contributors"]                     # at least one sub-score
    assert h["top_detractors"]


def test_sla_dashboard_breach(workspace):
    sla = observe()["sla_dashboard"]
    names = {o["name"]: o for o in sla["objectives"]}
    # availability 75% < 99% target -> breached
    assert names["Availability"]["met"] is False
    assert 0 <= names["Availability"]["error_budget_consumed_pct"] <= 100
    assert names["Failure rate"]["met"] is False   # >5%


def test_alerts_fire_on_failures(workspace):
    al = observe()["alerting"]
    ids = {a["id"] for a in al["alerts"]}
    # 25% failure rate (2 of 8) -> warning; a stopped/failing connector too
    assert any(a["monitor"] == "failures" for a in al["alerts"])
    assert any(a["monitor"] == "connector_health" for a in al["alerts"])
    assert al["counts"]["warning"] >= 1


def test_trends_and_historical(workspace):
    r = observe()
    tr = r["performance_trends"]
    assert tr["days"] >= 3 and tr["direction"] in ("improving", "declining",
                                                   "stable")
    assert all("success_rate_pct" in s for s in tr["series"])
    h = r["historical_analytics"]
    assert h["jobs_total"] == 5           # upload excluded
    assert h["agent_runs_total"] == 1
    assert "convert" in h["by_kind"]
    assert 0 <= h["overall_success_rate_pct"] <= 100


# --- honest empty state ---------------------------------------------------

def test_empty_workspace_is_honest():
    r = observe()
    assert r["health_score"]["band"] == "no_data"
    assert r["health_score"]["score"] is None
    assert r["monitors"]["pipeline_health"]["basis"] == "no_data"
    assert r["alerting"]["total"] == 0
    assert r["historical_analytics"]["jobs_total"] == 0


def test_sla_config_override(workspace):
    r = observe(sla={"availability_pct": 50.0})
    names = {o["name"]: o for o in r["sla_dashboard"]["objectives"]}
    assert names["Availability"]["met"] is True   # 75% >= 50% now
    assert r["sla_config"]["availability_pct"] == 50.0


# --- API ------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None)
             for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    c = TestClient(webapp.app)
    c.post("/auth/signup", json={"email": "o@x.com", "password": "Pw123456!",
                                 "name": "Owner"})
    yield c
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def test_api_observability(client, tmp_path):
    # a fresh instance has no run history -> honest no_data
    r = client.get("/api/observability")
    assert r.status_code == 200
    d = r.json()
    assert set(d["monitors"]) and "health_score" in d
    assert d["health_score"]["band"] == "no_data"

    # seed a couple of jobs and re-observe -> measured
    for jid, status in (("a1", "done"), ("a2", "failed")):
        jd = tmp_path / "jobs" / jid
        jd.mkdir(parents=True, exist_ok=True)
        (jd / "meta.json").write_text(json.dumps(
            {"id": jid, "kind": "convert", "status": status,
             "created": "2026-07-10T09:00:00",
             "finished": "2026-07-10T09:00:10"}))
    d2 = client.get("/api/observability").json()
    assert d2["monitors"]["pipeline_health"]["basis"] == "measured"
    assert d2["monitors"]["pipeline_health"]["done"] == 1

    # export sets a download header
    ex = client.get("/api/observability/export")
    assert ex.status_code == 200
    assert "attachment" in ex.headers.get("content-disposition", "")


# --- adversarial-review regressions --------------------------------------

def test_near_empty_history_is_not_healthy(tmp_path):
    base = tmp_path / "wk"
    _job(base, "j1", "convert", "done", "2026-07-10T09:00:00",
         "2026-07-10T09:00:05")
    h = observe()["health_score"]
    assert h["band"] == "insufficient"        # 1 op must not read "healthy"
    assert h["operations"] == 1 and h["sample"] == "thin"


def test_performance_count_only_without_durations(tmp_path):
    base = tmp_path / "wk"
    for i in range(6):        # jobs with NO finish time -> no durations
        _job(base, "p%d" % i, "convert", "done", "2026-07-10T09:00:00")
    m = observe()["monitors"]["performance"]
    assert m["basis"] == "count_only" and m["mean_job_ms"] is None


def test_failed_uploads_do_not_inflate_failure_rate(tmp_path):
    base = tmp_path / "wk"
    _job(base, "u1", "upload", "failed", "2026-07-10T09:00:00")
    _job(base, "u2", "upload", "failed", "2026-07-10T09:00:00")
    _job(base, "c1", "convert", "done", "2026-07-10T09:00:00")
    f = observe()["monitors"]["failures"]
    assert 0 <= f["failure_rate_pct"] <= 100 and f["failed_jobs"] == 0


def test_connector_stopped_failure_does_not_drop_actives(tmp_path):
    base = tmp_path / "wk"
    (base / "connections.json").write_text(json.dumps([
        {"id": "a", "connector": "x", "status": "active",
         "last_test": {"ok": True, "latency_ms": 10}},
        {"id": "b", "connector": "x", "status": "active",
         "last_test": {"ok": True, "latency_ms": 10}},
        {"id": "c", "connector": "y", "status": "stopped",
         "last_test": {"ok": False}}]))
    c = observe()["monitors"]["connector_health"]
    assert c["score"] >= 66.0                 # 2 healthy actives / 3, not 1/3


def test_connector_non_bool_ok_is_not_a_pass(tmp_path):
    base = tmp_path / "wk"
    (base / "connections.json").write_text(json.dumps([
        {"id": "a", "connector": "x", "status": "active",
         "last_test": {"ok": "no", "latency_ms": 5}}]))
    c = observe()["monitors"]["connector_health"]
    assert c["test_ok"] == 0 and c["test_failing"] == 1


def test_single_latency_sample_has_no_slo(tmp_path):
    base = tmp_path / "wk"
    (base / "connections.json").write_text(json.dumps([
        {"id": "a", "connector": "x", "status": "active",
         "last_test": {"ok": True, "latency_ms": 240}}]))
    d = observe()
    assert d["monitors"]["latency"]["sufficient_for_percentile"] is False
    assert "Latency (p95)" not in [o["name"]
                                   for o in d["sla_dashboard"]["objectives"]]


def test_observe_reads_connections_from_the_given_dir(tmp_path):
    argdir = tmp_path / "argtenant"
    argdir.mkdir()
    (argdir / "connections.json").write_text(json.dumps([
        {"id": "a", "connector": "x", "status": "active",
         "last_test": {"ok": True, "latency_ms": 5}}]))
    # env (_iso) points at wk with no connections; the arg dir must win
    d = observe(data_dir=str(argdir))
    assert d["monitors"]["connector_health"]["total"] == 1


def test_malformed_files_degrade_not_crash(tmp_path):
    base = tmp_path / "wk"
    # a job meta that is a list, and a job with a non-string error
    (base / "jobs" / "bad").mkdir(parents=True)
    (base / "jobs" / "bad" / "meta.json").write_text("[1,2,3]")
    _job(base, "c1", "convert", "failed", "2026-07-10T09:00:00")
    (base / "jobs" / "c1" / "meta.json").write_text(json.dumps(
        {"id": "c1", "kind": "convert", "status": "failed",
         "created": "2026-07-10T09:00:00", "error": 12345}))
    (base / "agents" / "runs").mkdir(parents=True)
    # results as a dict (must be skipped), and a valid run with a null
    # error + non-dict outputs + non-numeric validation confidence
    (base / "agents" / "runs" / "r1.json").write_text(json.dumps(
        {"run_id": "r1", "results": {"agent_id": "x"}}))
    (base / "agents" / "runs" / "r2.json").write_text(json.dumps(
        {"run_id": "r2", "created_at": "2026-07-10T09:00:00",
         "summary": {"failed": 1}, "results": [
             {"agent_id": "security", "status": "failed", "error": None,
              "duration_ms": 30, "outputs": "not-a-dict"},
             {"agent_id": "validation", "status": "ok", "duration_ms": 10,
              "outputs": {"validation": {"avg_confidence": "high"}}}]}))
    r = observe()                              # must not raise
    assert r["monitors"]["failures"]["failed_jobs"] == 1
    assert r["monitors"]["agent_health"]["basis"] == "measured"
    assert r["monitors"]["agent_health"]["actions"] == 2   # r1 skipped

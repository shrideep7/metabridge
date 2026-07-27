"""Agentic AI Architecture — confidence, memory, audit chain, governance
gate, approval workflow, orchestrator DAG and the twelve agents."""
import sys
from pathlib import Path

import pytest

from metabridge.agents import (ActionClass, AgentGovernance, AgentResult,
                               ApprovalError, ApprovalQueue, AuditTrail,
                               Decision, OrchestratorError, SharedContext,
                               SharedMemory, Status, TaskOrchestrator,
                               TaskType, confidence_level, default_agents,
                               score_confidence)
from metabridge.agents.base import Agent, ConfidenceError

REPO = Path(__file__).resolve().parents[1]
FIXTURE = str(REPO / "examples" / "dbt_retail")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


# --- confidence -----------------------------------------------------------

def test_score_confidence_weighted_mean():
    assert score_confidence({"a": (0.8, 2.0), "b": (0.4, 1.0)}) == \
        round((0.8 * 2 + 0.4) / 3, 4)


def test_score_confidence_clamps_and_empty():
    assert score_confidence({"a": (1.5, 1.0)}) == 1.0     # clamped high
    assert score_confidence({"a": (-2.0, 1.0)}) == 0.0    # clamped low
    assert score_confidence({}) == 0.0                    # no evidence -> 0


def test_score_confidence_rejects_bad_signals():
    with pytest.raises(ConfidenceError):
        score_confidence({"a": (float("nan"), 1.0)})
    with pytest.raises(ConfidenceError):
        score_confidence({"a": (0.5, -1.0)})              # negative weight


def test_confidence_levels():
    assert confidence_level(0.9) == "high"
    assert confidence_level(0.6) == "medium"
    assert confidence_level(0.3) == "low"


# --- shared memory --------------------------------------------------------

def test_memory_provenance_and_snapshot():
    m = SharedMemory()
    r1 = m.put("twin", {"n": 1}, "discovery")
    r2 = m.put("pipelines", [1, 2], "parser")
    assert r2 > r1
    assert m.provenance("twin")["agent_id"] == "discovery"
    snap = m.snapshot()
    # snapshot is a deep copy — mutating the source must not rewrite it
    m.get("twin")["n"] = 99
    assert snap["twin"]["value"]["n"] == 1


# --- audit trail (tamper-evident) ----------------------------------------

def test_audit_chain_intact_and_tamper_detected():
    a = AuditTrail()
    for i in range(3):
        a.record(agent_id="ag%d" % i, task_type="t", action_class="read_only",
                 decision="allow", status="ok", confidence=0.9,
                 confidence_level="high", summary="s%d" % i)
    assert a.verify()["intact"]
    head = a.head()
    a._events[1].summary = "TAMPERED"
    v = a.verify()
    assert not v["intact"] and v["broken_at"] == 2
    assert a.head() != head or not a.verify()["intact"]


# --- governance gate ------------------------------------------------------

def _res(action_class, confidence, status=Status.OK, task_type="x",
         sensitivity=None):
    return AgentResult(agent_id=task_type, task_type=task_type,
                       action_class=action_class, status=status,
                       confidence=confidence, sensitivity=sensitivity or {})


def test_governance_readonly_allows_and_flags():
    g = AgentGovernance()
    hi = g.decide(_res(ActionClass.READ_ONLY, 0.9))
    assert hi.decision == Decision.ALLOW and hi.commits
    lo = g.decide(_res(ActionClass.ADVISORY, 0.3))
    assert lo.decision == Decision.FLAG_REVIEW and lo.commits   # still commits


def test_governance_generate_needs_approval_and_deny():
    g = AgentGovernance()
    gen = g.decide(_res(ActionClass.GENERATE, 0.9, task_type="migration"))
    assert gen.decision == Decision.NEEDS_APPROVAL and not gen.commits
    denied = g.decide(_res(ActionClass.GENERATE, 0.1, task_type="migration"))
    assert denied.decision == Decision.DENY and not denied.commits


def test_governance_preapproval_and_failed():
    g = AgentGovernance()
    ok = g.decide(_res(ActionClass.GENERATE, 0.9, task_type="migration"),
                  preapproved={"migration"}, approver="sam")
    assert ok.decision == Decision.APPROVED and ok.commits
    assert ok.approver == "sam"
    fail = g.decide(_res(ActionClass.READ_ONLY, 0.0, status=Status.FAILED))
    assert fail.decision == Decision.FAILED and not fail.commits


# --- approval queue -------------------------------------------------------

def test_approval_queue_lifecycle(tmp_path):
    q = ApprovalQueue(data_dir=str(tmp_path / "q"))
    r = _res(ActionClass.GENERATE, 0.9, task_type="migration")
    aid = q.open_request("run1", r, ["needs approval"])
    assert len(q.pending()) == 1
    q.approve(aid, approver="sam", note="ok")
    assert q.get(aid)["status"] == "approved" and q.get(aid)["approver"] == "sam"
    assert q.pending() == []
    with pytest.raises(ApprovalError):
        q.approve(aid, approver="sam")            # already decided
    with pytest.raises(ApprovalError):
        q.reject(q.open_request("run1", r, []), approver="")  # approver req'd


# --- orchestrator plan / cycle / skip -------------------------------------

def test_plan_is_topological():
    plan = [p["id"] for p in TaskOrchestrator().plan()]
    pos = {a: i for i, a in enumerate(plan)}
    for p in TaskOrchestrator().plan():
        for dep in p["depends_on"]:
            assert pos[dep] < pos[p["id"]], "%s before its dep %s" % (
                p["id"], dep)


def test_cycle_detected():
    class A(Agent):
        id = "a"; task_type = "a"; depends_on = ("b",)
    class B(Agent):
        id = "b"; task_type = "b"; depends_on = ("a",)
    with pytest.raises(OrchestratorError):
        TaskOrchestrator(agents=[A(), B()]).plan()


def test_unmet_dependency_is_skipped():
    ctx = SharedContext(paths=[FIXTURE])
    # run semantic alone: its dependency 'parse' is not selected -> skipped
    rep = TaskOrchestrator().run(ctx, task_types=["semantic"])
    row = rep["results"][0]
    assert row["agent_id"] == "semantic" and row["status"] == Status.SKIPPED
    assert rep["summary"]["skipped"] == 1


# --- full run over a real fixture -----------------------------------------

@pytest.fixture()
def full_run(tmp_path):
    ctx = SharedContext(paths=[FIXTURE], project="retail", target_region="us")
    orch = TaskOrchestrator()
    return ctx, orch, orch.run(ctx, created_at="2026-07-14")


def test_full_run_all_agents_scored_and_audited(full_run):
    ctx, orch, rep = full_run
    ids = [r["agent_id"] for r in rep["results"]]
    assert set(ids) == set(TaskType.__dict__[k] for k in dir(TaskType)
                           if k.isupper())
    # every action carries a confidence and a decision, and is audited
    assert all("confidence" in r and "decision" in r for r in rep["results"])
    assert rep["audit"]["verification"]["intact"]
    assert len(rep["audit"]["events"]) == 12


def test_full_run_generate_agents_need_approval(full_run):
    ctx, orch, rep = full_run
    by = {r["agent_id"]: r for r in rep["results"]}
    for gen in ("migration", "testing", "documentation"):
        assert by[gen]["decision"] == Decision.NEEDS_APPROVAL
        assert by[gen]["action_class"] == ActionClass.GENERATE
    # the three GENERATE proposals are queued
    assert len(rep["approvals"]) == 3
    # read-only/advisory analysis committed to the shared CIR
    assert ctx.memory.has("twin") and ctx.memory.has("pipelines")
    assert by["discovery"]["decision"] == Decision.ALLOW


def test_full_run_executive_synthesizes(full_run):
    ctx, orch, rep = full_run
    er = ctx.memory.get("executive_report")
    assert er and "overall_confidence" in er and "recommendation" in er
    assert by_key(rep, "governance")["outputs"]["governance"]["classified_columns"] >= 0


def test_preapproval_commits_generate(tmp_path):
    ctx = SharedContext(paths=[FIXTURE], project="retail")
    orch = TaskOrchestrator()
    rep = orch.run(ctx, preapproved={"migration"}, approver="sam")
    mig = by_key(rep, "migration")
    assert mig["decision"] == Decision.APPROVED and mig["approver"] == "sam"
    assert ctx.memory.has("migration")


def test_run_persisted_and_listable(full_run):
    ctx, orch, rep = full_run
    got = orch.get_run(rep["run_id"])
    assert got and got["run_id"] == rep["run_id"]
    assert any(r["run_id"] == rep["run_id"] for r in orch.list_runs())


def by_key(rep, agent_id):
    return next(r for r in rep["results"] if r["agent_id"] == agent_id)


# --- API ------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None)
             for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    # first signup becomes owner; subsequent requests carry the cookie
    c = TestClient(webapp.app)
    c.post("/auth/signup", json={"email": "o@x.com", "password": "Pw123456!",
                                 "name": "Owner"})
    yield c
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def _project_files(root):
    base = Path(root)
    files = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            try:
                files.append({"name": base.name + "/" +
                              str(p.relative_to(base)),
                              "content": p.read_text()})
            except (UnicodeDecodeError, OSError):
                continue
    return files


def test_api_roster(client):
    d = client.get("/api/agents").json()
    assert len(d["task_types"]) == 12
    assert len(d["agents"]) == 12
    assert len(d["plan"]) == 12
    assert "approval_threshold" in d["governance"]


def test_api_run_and_segregation_of_duties(client):
    from fastapi.testclient import TestClient
    files = _project_files(FIXTURE)
    # even if a caller sends `preapproved`, the API ignores it — GENERATE
    # proposals are still held (the self-service bypass is closed)
    r = client.post("/api/agents/run",
                    json={"files": files, "target_region": "us",
                          "preapproved": ["migration", "testing",
                                          "documentation"]})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["audit"]["verification"]["intact"]
    assert rep["summary"]["failed"] == 0
    run_id = rep["run_id"]
    gens = [x for x in rep["results"] if x["action_class"] == "generate"]
    assert gens and all(x["decision"] == "needs_approval" for x in gens)

    assert client.get("/api/agents/runs/nope").status_code == 404
    pend = client.get("/api/agents/approvals").json()["approvals"]
    assert pend
    aid = pend[0]["id"]

    # the owner ran it -> cannot self-approve their own action
    self_ap = client.post("/api/agents/approvals/approve",
                          json={"approval_id": aid})
    assert self_ap.status_code == 422

    # an engineer (run-capable) lacks the distinct agents:approve permission
    client.post("/api/users", json={"email": "e@x.com",
                                    "password": "Pw123456!", "name": "Eng",
                                    "role": "engineer"})
    eng = TestClient(client.app)
    eng.post("/auth/login", json={"email": "e@x.com", "password": "Pw123456!"})
    assert eng.post("/api/agents/approvals/approve",
                    json={"approval_id": aid}).status_code == 403

    # a distinct admin CAN approve
    client.post("/api/users", json={"email": "a@x.com",
                                    "password": "Pw123456!", "name": "Adm",
                                    "role": "admin"})
    admin = TestClient(client.app)
    admin.post("/auth/login", json={"email": "a@x.com", "password": "Pw123456!"})
    ok = admin.post("/api/agents/approvals/approve", json={"approval_id": aid})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    # the decision is recorded on the run's audit chain, still intact
    live = client.get("/api/agents/runs/%s" % run_id).json()
    assert live["audit"]["verification"]["intact"]
    assert any(e["task_type"] == "approval" and e["decision"] == "approved"
               for e in live["audit"]["events"])
    approved = [a for a in live["approvals"] if a["id"] == aid]
    assert approved and approved[0]["status"] == "approved"


def test_api_bad_input(client):
    assert client.post("/api/agents/run", json={"files": []}
                       ).status_code == 422
    assert client.post("/api/agents/approvals/approve", json={}
                       ).status_code == 422
    assert client.post("/api/agents/run", content=b"{bad",
                       headers={"Content-Type": "application/json"}
                       ).status_code == 422


# --- adversarial-review regressions --------------------------------------

def test_confidence_never_hand_set_without_evidence():
    class Sneaky(Agent):
        id = "sneaky"; task_type = "discovery"
        action_class = ActionClass.READ_ONLY
        def _execute(self, ctx):
            return self._result(confidence=0.99, summary="no evidence")
    r = Sneaky().run(SharedContext())
    assert r.confidence == 0.0            # empty evidence -> no confidence


def test_confidence_level_boundary_aligns_with_gate():
    from metabridge.agents.base import confidence_level
    # 0.55 is below the 0.6 approval threshold -> must NOT read 'medium'
    assert confidence_level(0.55) == "low"
    assert confidence_level(0.6) == "medium"
    assert confidence_level(0.8) == "high"


def test_governance_unknown_action_class_fails_closed():
    g = AgentGovernance()
    r = AgentResult(agent_id="x", task_type="x", action_class="weird_custom",
                    status=Status.OK, confidence=0.95)
    d = g.decide(r)
    assert d.decision == Decision.NEEDS_APPROVAL and not d.commits


def test_governance_sensitivity_overrides_preapproval():
    g = AgentGovernance()
    r = AgentResult(agent_id="migration", task_type="migration",
                    action_class=ActionClass.GENERATE, status=Status.OK,
                    confidence=0.9, sensitivity={"violations": 3, "phi": True})
    d = g.decide(r, preapproved={"migration"}, approver="sam")
    assert d.decision == Decision.NEEDS_APPROVAL and not d.commits


def test_approval_open_request_does_not_clobber_decision(tmp_path):
    q = ApprovalQueue(data_dir=str(tmp_path / "q"))
    r = _res(ActionClass.GENERATE, 0.9, task_type="migration")
    aid = q.open_request("run1", r, [])
    q.approve(aid, approver="sam")
    q.open_request("run1", r, [])              # re-open must NOT reset it
    assert q.get(aid)["status"] == "approved" and q.get(aid)["approver"] == "sam"


def test_approval_self_approval_rejected(tmp_path):
    q = ApprovalQueue(data_dir=str(tmp_path / "q"))
    r = _res(ActionClass.GENERATE, 0.9, task_type="migration")
    aid = q.open_request("run1", r, [], requested_by="sam@x.com")
    with pytest.raises(ApprovalError):
        q.approve(aid, approver="sam@x.com")   # requester cannot self-approve
    assert q.approve(aid, approver="boss@x.com")["status"] == "approved"


def test_get_run_reverifies_and_detects_tampering():
    import json
    ctx = SharedContext(paths=[FIXTURE])
    orch = TaskOrchestrator()
    run_id = orch.run(ctx)["run_id"]
    assert orch.get_run(run_id)["audit"]["verification"]["intact"]
    f = orch._runs_dir / (run_id + ".json")
    doc = json.loads(f.read_text())
    doc["audit"]["events"][2]["summary"] = "TAMPERED"    # edit persisted event
    f.write_text(json.dumps(doc))
    v = orch.get_run(run_id)["audit"]["verification"]    # re-verified on read
    assert v["intact"] is False and v["broken_at"] == 3


def test_record_decision_is_audited():
    ctx = SharedContext(paths=[FIXTURE])
    orch = TaskOrchestrator()
    run_id = orch.run(ctx)["run_id"]
    n0 = len(orch.get_run(run_id)["audit"]["events"])
    ev = orch.record_decision(run_id, "migration", "approved", "boss@x.com",
                              summary="ok")
    assert ev and ev["decision"] == "approved"
    r = orch.get_run(run_id)
    assert len(r["audit"]["events"]) == n0 + 1
    assert r["audit"]["verification"]["intact"]
    assert r["audit"]["events"][-1]["detail"].get("approver") == "boss@x.com"


def test_parser_coverage_penalizes_failed_sources():
    from metabridge.agents.agents import ParserAgent
    good = ParserAgent().run(SharedContext(paths=[FIXTURE]))
    mixed = ParserAgent().run(
        SharedContext(paths=[FIXTURE, str(REPO / "no_such_dir_xyz")]))
    assert mixed.warnings                        # the bad source is disclosed
    assert dict(mixed.evidence)["coverage"][0] == 0.5
    assert mixed.confidence < good.confidence    # unparsed source depresses it


def test_executive_coverage_scoped_to_declared_deps():
    ctx = SharedContext(paths=[FIXTURE])
    TaskOrchestrator().run(ctx)
    er = ctx.memory.get("executive_report")
    # ExecutiveReporting declares 7 deps; coverage counts only those, not
    # every 'ok' agent (semantic etc. commit too but are not deps)
    assert er["agents_contributing"] == 7

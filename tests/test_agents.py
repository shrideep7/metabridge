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
    aid = q.open_request("run1", r, ["needs approval"], requested_by="alex@x.com")
    assert len(q.pending()) == 1
    q.approve(aid, approver="sam", note="ok")
    assert q.get(aid)["status"] == "approved" and q.get(aid)["approver"] == "sam"
    assert q.pending() == []
    with pytest.raises(ApprovalError):
        q.approve(aid, approver="sam")            # already decided
    with pytest.raises(ApprovalError):
        q.reject(q.open_request("run1", r, [], requested_by="alex@x.com"),
                approver="")  # approver req'd
    with pytest.raises(ApprovalError):
        q.open_request("run2", r, [])              # requested_by mandatory


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
    return ctx, orch, orch.run(ctx, created_at="2026-07-14",
                               requested_by="requester@x.com")


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
    rep = orch.run(ctx, preapproved={"migration"}, approver="sam",
                   requested_by="requester@x.com")
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


def _saved_snowflake_full_ddl():
    """Snowflake returns the WHOLE `CREATE OR REPLACE VIEW ... AS SELECT ...`
    statement in INFORMATION_SCHEMA.VIEWS.view_definition — not a bare SELECT
    like Postgres/MySQL. This is the real shape."""
    from metabridge import connections_store as cs
    row = cs.save_connection("snowflake", {
        "account": "acme-x1", "user": "svc", "warehouse": "WH",
        "database": "BANK", "schema": "ANALYTICS"}, name="Bank Data")
    cs.record_inventory(row["id"], {
        "connector": "snowflake", "database": "BANK", "schema": "ANALYTICS",
        "tables": [{"schema": "ANALYTICS", "name": "CUSTOMER_HOLDINGS",
                    "type": "BASE TABLE", "rows": 900, "bytes": 2048,
                    "columns": [{"name": "CUSTOMER_ID"},
                                {"name": "TOTAL_BALANCE"}]}],
        "views": [{"schema": "ANALYTICS", "name": "V_CUSTOMER_360"}],
        "view_definitions": {
            "V_CUSTOMER_360":
                "CREATE OR REPLACE VIEW ANALYTICS.V_CUSTOMER_360 AS\n"
                "SELECT customer_id, email, total_balance\n"
                "FROM ANALYTICS.CUSTOMER_HOLDINGS"}})
    return row


def test_connection_view_ddl_reaches_the_column_level_agents(client):
    """Regression: the CREATE VIEW header was added unconditionally, yielding
    `CREATE VIEW x AS CREATE OR REPLACE VIEW x AS SELECT ...` — invalid SQL
    that parsed to nothing, so the run classified 0 columns and every
    downstream document came out empty. Asserted on behaviour, not on the
    file layout: the view's columns must actually reach governance."""
    row = _saved_snowflake_full_ddl()
    rep = client.post("/api/agents/run",
                      json={"connection_ids": [row["id"]]}).json()
    parse = next(r for r in rep["results"] if r["agent_id"] == "parse")
    assert parse["confidence"] > 0, parse["summary"]
    gov = next(r for r in rep["results"] if r["agent_id"] == "governance")
    classified = gov["outputs"]["governance"]["classified_columns"]
    assert classified > 0, gov["summary"]
    # `email` in the view's SELECT list is PII by name heuristics
    assert gov["outputs"]["governance"]["special_category_columns"] >= 0
    assert "Classified 0 column(s)" not in gov["summary"]


def test_connection_only_run_leaves_a_documentable_input_tree(client):
    """Regression: connection SQL was written to a sibling connections/ dir, so
    `from_job` resolved to the empty input/ that _new_job pre-creates and every
    generated document reported 0 file(s) / 0 pipeline(s)."""
    row = _saved_snowflake_full_ddl()
    rep = client.post("/api/agents/run",
                      json={"connection_ids": [row["id"]]}).json()
    job_id = next(j["id"] for j in
                  client.get("/api/jobs?kind=agents&limit=1000").json()["jobs"]
                  if j.get("run_id") == rep["run_id"])
    # documentation regenerated from the run must see real source
    d = client.post("/api/docs",
                    json={"from_job": job_id,
                          "documents": ["governance_report", "data_dictionary"],
                          "formats": ["md"]})
    assert d.status_code == 200, d.text
    got = d.json()
    md = client.get("/api/docs/%s/download?doc=governance_report&format=md"
                    % got["docs_id"]).text
    # the exact string the empty PDFs carried in their source snapshot
    assert "0 file(s), 0 pipeline(s)" not in md, md[:800]
    assert "CUSTOMER" in md.upper(), md[:800]


def _saved_snowflake(with_inventory=True, name="Prod Snowflake", prefix="ORD",
                     database="RETAIL"):
    """A saved, ACTIVE Snowflake connection, optionally already analyzed so an
    object inventory (tables + view SQL) exists without touching a network.

    Pass a distinct `database` for a SECOND connection: save_connection upserts
    on connector+params and ignores `name`, so two calls with identical params
    return the same row — and the twin keys nodes by name, so identical object
    names would collapse onto the same nodes even if they didn't.
    """
    from metabridge import connections_store as cs
    row = cs.save_connection("snowflake", {
        "account": "acme-x1", "user": "svc", "warehouse": "WH",
        "database": database, "schema": "PUBLIC"}, name=name)
    if with_inventory:
        tbl, view = "%s_ORDERS" % prefix, "V_%s_TOTALS" % prefix
        cs.record_inventory(row["id"], {
            "connector": "snowflake", "database": database,
            "schema": "PUBLIC",
            "tables": [{"schema": "PUBLIC", "name": tbl,
                        "type": "BASE TABLE", "rows": 1200, "bytes": 4096,
                        "columns": [{"name": "ID"}, {"name": "TOTAL"}]}],
            "views": [{"schema": "PUBLIC", "name": view}],
            "view_definitions": {
                view: "SELECT ID, SUM(TOTAL) AS TOTAL FROM PUBLIC.%s "
                      "GROUP BY ID" % tbl}})
    return row


def test_api_run_on_connection_only_no_files(client):
    """A connected warehouse is a valid source on its own: no ETL folder to
    upload, but its view SQL is real, parseable evidence."""
    row = _saved_snowflake()
    r = client.post("/api/agents/run",
                    json={"connection_ids": [row["id"]],
                          "project": "warehouse", "target_region": "us"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["params"]["connection_ids"] == [row["id"]]
    assert rep["audit"]["verification"]["intact"]
    assert rep["summary"]["failed"] == 0
    disc = next(x for x in rep["results"] if x["agent_id"] == "discovery")
    assert disc["decision"] in ("allow", "flag_review")
    assert "live connection" in disc["summary"]
    # the view SQL reached the PARSE-dependent agents
    parse = next(x for x in rep["results"] if x["agent_id"] == "parse")
    assert parse["confidence"] > 0, parse["summary"]
    # ...and it survives a reopen (params are persisted with the run)
    again = client.get("/api/agents/runs/%s" % rep["run_id"]).json()
    assert again["params"]["connection_ids"] == [row["id"]]


def test_api_run_accepts_folder_and_connection_together(client):
    """Both sources at once: the folder supplies the pipelines, the connection
    supplies the live estate they land in."""
    row = _saved_snowflake()
    r = client.post("/api/agents/run",
                    json={"files": _project_files(FIXTURE),
                          "connection_ids": [row["id"]], "project": "both"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["params"]["connection_ids"] == [row["id"]]
    # ONE root: with both sources present, input/ itself is the root, so the
    # uploaded tree and the materialized view SQL are each walked exactly once
    # (two separate paths would have double-counted the connection SQL).
    # Two disjoint sibling paths under input/, so nothing is walked twice and
    # each still gets its own format detection (collapsing both to input/ made
    # the twin report one "path:input (unrecognized)" blob).
    paths = rep["params"]["paths"]
    assert len(paths) == 2, paths
    assert any(p.endswith("_connections") for p in paths), paths
    disc = next(x for x in rep["results"] if x["agent_id"] == "discovery")
    assert "live connection" in disc["summary"]
    srcs = disc["outputs"]["discovery"]["sources"]
    assert "connections" in srcs, srcs
    assert not any("unrecognized" in s for s in srcs), srcs
    parse = next(x for x in rep["results"] if x["agent_id"] == "parse")
    assert parse["confidence"] > 0, parse["summary"]


def test_api_run_connection_without_inventory_still_discovers_system(client):
    """No inventory and no reachable driver -> the system is still a node in
    the twin, and the run says so instead of pretending it read the tables."""
    row = _saved_snowflake(with_inventory=False)
    r = client.post("/api/agents/run",
                    json={"connection_ids": [row["id"]], "project": "warehouse"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["params"]["connection_notes"], "expected a capture note"
    assert any("Prod Snowflake" in n for n in rep["params"]["connection_notes"])


def test_api_run_rejects_unknown_and_stopped_connections(client):
    r = client.post("/api/agents/run", json={"connection_ids": ["nope"]})
    assert r.status_code == 422 and "Unknown connection" in r.text

    from metabridge import connections_store as cs
    row = _saved_snowflake()
    cs.set_status(row["id"], "stopped")
    r = client.post("/api/agents/run", json={"connection_ids": [row["id"]]})
    assert r.status_code == 422 and "stopped" in r.text


def test_api_run_target_region_is_optional(client):
    """Target region is optional: omitted entirely, or sent empty by the
    console's "Not specified" default. Residency then falls back to the policy
    and, failing that, is reported as not declared — never a hard failure."""
    row = _saved_snowflake()
    for body in ({"connection_ids": [row["id"]]},
                 {"connection_ids": [row["id"]], "target_region": ""}):
        r = client.post("/api/agents/run", json=body)
        assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["params"]["target_region"] == ""
        assert rep["summary"]["failed"] == 0
        gov = next(x for x in rep["results"]
                   if x["agent_id"] == "governance")
        assert gov["decision"] in ("allow", "flag_review"), gov
    # an explicit region still rides through
    rep = client.post("/api/agents/run",
                      json={"connection_ids": [row["id"]],
                            "target_region": "eu"}).json()
    assert rep["params"]["target_region"] == "eu"


def test_api_run_requires_at_least_one_source(client):
    r = client.post("/api/agents/run", json={})
    assert r.status_code == 422
    assert "connected system" in r.text


def test_agent_run_does_not_pull_in_unselected_connections(client):
    """A run is scoped to the systems its requester picked — it must not
    silently sweep in every saved connection in the workspace.

    Asserted on the twin's NODE COUNT, not on built_from: build_twin records
    the whole connection walk as the single literal source "connections", so a
    "connection:<id> not in sources" check would pass no matter what.
    """
    picked = _saved_snowflake()
    other = _saved_snowflake(name="Other Snowflake", prefix="INV",
                             database="WAREHOUSE")
    assert picked["id"] != other["id"], "fixture must create two connections"

    def nodes(ids):
        rep = client.post("/api/agents/run",
                          json={"connection_ids": ids}).json()
        d = next(x for x in rep["results"] if x["agent_id"] == "discovery")
        return d["outputs"]["discovery"]["nodes"]

    one, both = nodes([picked["id"]]), nodes([picked["id"], other["id"]])
    assert one > 0, "the selected connection must produce nodes"
    assert both > one, (
        "selecting a second connection must add nodes — equal counts mean the "
        "walk ignored the selection and took every saved connection")


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


def test_approved_documentation_is_downloadable_from_the_run(client):
    """The documentation agent discards its rendered bodies by design (that is
    what makes the GENERATE gate real), so the run view regenerates them on
    demand from the job that already holds the uploaded tree — no re-upload
    into a second tab."""
    from fastapi.testclient import TestClient
    rep = client.post("/api/agents/run",
                      json={"files": _project_files(FIXTURE)}).json()
    run_id = rep["run_id"]

    # the run reports which documents it produced, with slugs
    doc_row = next(r for r in rep["results"] if r["agent_id"] == "documentation")
    assert doc_row["decision"] == "needs_approval"
    slugs = [d["slug"] for d in doc_row["outputs"]["documents"]["documents"]]
    assert slugs, "documentation slugs must survive into the report"

    # a distinct admin approves it (the requester never can)
    client.post("/api/users", json={"email": "a2@x.com",
                                    "password": "Pw123456!", "name": "Adm",
                                    "role": "admin"})
    admin = TestClient(client.app)
    admin.post("/auth/login", json={"email": "a2@x.com",
                                    "password": "Pw123456!"})
    aid = next(a["id"] for a in client.get("/api/agents/approvals").json()
               ["approvals"] if a["agent_id"] == "documentation")
    assert admin.post("/api/agents/approvals/approve",
                      json={"approval_id": aid}).status_code == 200

    # the run's own job carries the source tree, so from_job needs no upload
    jobs = client.get("/api/jobs?kind=agents&limit=1000").json()["jobs"]
    job_id = next(j["id"] for j in jobs if j.get("run_id") == run_id)

    d = client.post("/api/docs", json={"from_job": job_id, "documents": slugs,
                                       "formats": ["md"]})
    assert d.status_code == 200, d.text
    got = d.json()
    assert {x["slug"] for x in got["documents"]} == set(slugs)

    # and each one actually downloads
    first = got["documents"][0]
    r = client.get("/api/docs/%s/download?doc=%s&format=md"
                   % (got["docs_id"], first["slug"]))
    assert r.status_code == 200 and r.content


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
    aid = q.open_request("run1", r, [], requested_by="alex@x.com")
    q.approve(aid, approver="sam")
    q.open_request("run1", r, [], requested_by="alex@x.com")  # re-open must NOT reset it
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
    run_id = orch.run(ctx, requested_by="requester@x.com")["run_id"]
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
    run_id = orch.run(ctx, requested_by="requester@x.com")["run_id"]
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
    TaskOrchestrator().run(ctx, requested_by="requester@x.com")
    er = ctx.memory.get("executive_report")
    # ExecutiveReporting declares 7 deps; coverage counts only those, not
    # every 'ok' agent (semantic etc. commit too but are not deps)
    assert er["agents_contributing"] == 7

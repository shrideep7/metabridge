"""Approval workflow (regression): segregation of duties is preserved while
alternate approvers can actually find and decide pending requests, requesters
can withdraw their own, and a workspace with no eligible approver surfaces a
clear escalation state instead of deadlocking silently."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "examples" / "dbt_retail"


def _files():
    return [{"name": str(p.relative_to(FIXTURE)), "content": p.read_text()}
            for p in FIXTURE.rglob("*") if p.is_file()]


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """Workspace with owner + admin + engineer + viewer and one agent run
    (requested by the engineer) that produced pending approvals."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None) for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    owner = TestClient(webapp.app)
    assert owner.post("/auth/signup", json={
        "email": "owner@x.com", "password": "password123",
        "name": "Owner"}).status_code == 200
    clients = {"owner": owner}
    for email, role in [("adm@x.com", "admin"), ("eng@x.com", "engineer"),
                        ("view@x.com", "viewer")]:
        assert owner.post("/api/users", json={
            "email": email, "password": "password123",
            "role": role}).status_code == 200
        cl = TestClient(webapp.app)
        assert cl.post("/auth/login", json={
            "email": email, "password": "password123"}).status_code == 200
        clients[role] = cl
    run = clients["engineer"].post("/api/agents/run", json={
        "files": _files(), "source_format": "dbt", "target_region": "us"})
    assert run.status_code == 200, run.text
    rep = run.json()
    pending = [a for a in rep["approvals"] if a["status"] == "pending"]
    assert pending, "fixture run should hold GENERATE actions for approval"
    yield type("WS", (), {"clients": clients, "report": rep,
                          "pending": pending, "webapp": webapp})
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


@pytest.fixture()
def solo(tmp_path_factory, monkeypatch):
    """A workspace whose only member is the owner — the requester is the
    only apparent approver."""
    data = tmp_path_factory.mktemp("mb_solo")
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(data))
    saved = {m: sys.modules.pop(m, None) for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    c = TestClient(webapp.app)
    assert c.post("/auth/signup", json={
        "email": "solo@x.com", "password": "password123",
        "name": "Solo"}).status_code == 200
    run = c.post("/api/agents/run", json={
        "files": _files(), "source_format": "dbt", "target_region": "us"})
    assert run.status_code == 200
    yield type("Solo", (), {"client": c, "report": run.json()})
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


# -- segregation of duties stays intact ---------------------------------------

def test_requester_cannot_approve_own_request_even_with_permission(solo):
    aid = solo.report["approvals"][0]["id"]
    r = solo.client.post("/api/agents/approvals/approve",
                         json={"approval_id": aid})
    assert r.status_code == 422
    assert "cannot approve their own run's action" in r.json()["detail"]


def test_engineer_still_lacks_approve_permission(ws):
    r = ws.clients["engineer"].post("/api/agents/approvals/approve",
                                    json={"approval_id": ws.pending[0]["id"]})
    assert r.status_code == 403


def test_viewer_can_see_but_not_decide(ws):
    v = ws.clients["viewer"]
    q = v.get("/api/agents/approvals").json()
    assert q["pending_count"] == len(ws.pending)
    assert v.post("/api/agents/approvals/approve",
                  json={"approval_id": ws.pending[0]["id"]}).status_code == 403
    assert v.post("/api/agents/approvals/reject",
                  json={"approval_id": ws.pending[0]["id"]}).status_code == 403


# -- alternate approver flow ---------------------------------------------------

def test_eligible_approver_sees_actionable_queue(ws):
    q = ws.clients["admin"].get("/api/agents/approvals").json()
    assert q["pending_count"] == len(ws.pending)
    for rec in q["approvals"]:
        assert rec["can_approve"] and rec["can_claim"] and rec["can_reject"]
        assert rec["requested_by"] == "eng@x.com"
        assert not rec["no_eligible_approver"]
    assert q["escalated"] == []


def test_claim_then_approve_by_alternate_approver(ws):
    adm = ws.clients["admin"]
    aid = ws.pending[0]["id"]
    rec = adm.post("/api/agents/approvals/claim",
                   json={"approval_id": aid}).json()
    assert rec["claimed_by"] == "adm@x.com"
    rec = adm.post("/api/agents/approvals/approve",
                   json={"approval_id": aid}).json()
    assert rec["status"] == "approved" and rec["approver"] == "adm@x.com"
    # decisions are final
    assert adm.post("/api/agents/approvals/approve",
                    json={"approval_id": aid}).status_code == 422
    # audit trail carries the human decision
    run = adm.get("/api/agents/runs/%s" % ws.report["run_id"]).json()
    kinds = [(e.get("task_type"), e.get("decision"))
             for e in run["audit"]["events"]]
    assert ("approval", "approved") in kinds


def test_owner_can_also_approve(ws):
    aid = ws.pending[1]["id"]
    rec = ws.clients["owner"].post("/api/agents/approvals/approve",
                                   json={"approval_id": aid}).json()
    assert rec["status"] == "approved" and rec["approver"] == "owner@x.com"


def test_alternate_approver_can_reject(ws):
    aid = ws.pending[1]["id"]
    rec = ws.clients["admin"].post("/api/agents/approvals/reject",
                                   json={"approval_id": aid}).json()
    assert rec["status"] == "rejected" and rec["approver"] == "adm@x.com"


def test_requester_can_withdraw_own_request_without_approve_permission(ws):
    eng = ws.clients["engineer"]
    aid = ws.pending[2]["id"]
    rec = eng.post("/api/agents/approvals/reject",
                   json={"approval_id": aid}).json()
    assert rec["status"] == "rejected" and rec["approver"] == "eng@x.com"
    # but they may only withdraw their OWN requests: a second run by the
    # owner produces requests the engineer cannot touch
    run2 = ws.clients["owner"].post("/api/agents/run", json={
        "files": _files(), "source_format": "dbt", "target_region": "us"})
    other = [a for a in run2.json()["approvals"]
             if a["status"] == "pending"][0]
    r = eng.post("/api/agents/approvals/reject",
                 json={"approval_id": other["id"]})
    assert r.status_code == 403


def test_requester_view_flags_shape_the_ui(ws):
    q = ws.clients["engineer"].get("/api/agents/approvals").json()
    for rec in q["approvals"]:
        assert rec["is_requester"]
        assert not rec["can_approve"]
        assert rec["can_reject"]            # withdraw


def test_pending_approvals_are_announced_to_the_workspace(ws):
    notes = ws.clients["owner"].get(
        "/api/system/notifications").json()["notifications"]
    appr = [n for n in notes if n["topic"] == "approvals"]
    assert any("await approval" in n["title"] for n in appr)
    assert any("eng@x.com" in n["body"] for n in appr)


def test_run_report_overlays_viewer_capabilities(ws):
    run = ws.clients["admin"].get(
        "/api/agents/runs/%s" % ws.report["run_id"]).json()
    pend = [a for a in run["approvals"] if a["status"] == "pending"]
    assert pend and all(a["can_approve"] for a in pend)


# -- no eligible approver: escalation, not silent deadlock --------------------

def test_solo_workspace_flags_escalation(solo):
    q = solo.client.get("/api/agents/approvals").json()
    assert q["pending_count"] > 0
    assert len(q["escalated"]) == q["pending_count"]
    for rec in q["approvals"]:
        assert rec["no_eligible_approver"]
        assert not rec["can_approve"]        # own request
        assert rec["can_reject"]             # withdraw stays possible

    notes = solo.client.get(
        "/api/system/notifications").json()["notifications"]
    crit = [n for n in notes
            if n["topic"] == "approvals" and n["severity"] == "critical"]
    assert crit and "no eligible approver" in crit[0]["title"].lower()


def test_solo_owner_can_withdraw_and_unblock(solo):
    aid = solo.report["approvals"][0]["id"]
    rec = solo.client.post("/api/agents/approvals/reject",
                           json={"approval_id": aid}).json()
    assert rec["status"] == "rejected"


def test_escalation_clears_once_second_approver_exists(solo):
    c = solo.client
    assert c.post("/api/users", json={
        "email": "helper@x.com", "password": "password123",
        "role": "admin"}).status_code == 200
    q = c.get("/api/agents/approvals").json()
    assert q["escalated"] == []
    assert all(not rec["no_eligible_approver"] for rec in q["approvals"])
    # and the new admin can actually decide
    from fastapi.testclient import TestClient
    import web.app as webapp
    helper = TestClient(webapp.app)
    assert helper.post("/auth/login", json={
        "email": "helper@x.com", "password": "password123"}).status_code == 200
    pend = [a for a in q["approvals"] if a["status"] == "pending"]
    rec = helper.post("/api/agents/approvals/approve",
                      json={"approval_id": pend[0]["id"]}).json()
    assert rec["status"] == "approved" and rec["approver"] == "helper@x.com"


# -- claim guards --------------------------------------------------------------

def test_requester_cannot_claim_own_request(solo):
    pend = [a for a in solo.report["approvals"]
            if a["status"] == "pending"]
    r = solo.client.post("/api/agents/approvals/claim",
                         json={"approval_id": pend[0]["id"]})
    assert r.status_code == 422
    assert "cannot claim their own" in r.json()["detail"]


def test_console_has_a_shared_approval_queue_ui():
    console = ((REPO / "web" / "templates" / "console.html").read_text(encoding="utf-8")
               + (REPO / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8"))
    assert 'id="agentApprovalsPanel"' in console
    assert "loadApprovalQueue" in console
    assert "no_eligible_approver" in console or "approvalEscalationBanner" in console
    assert "can_approve" in console
    assert "navbadge" in console

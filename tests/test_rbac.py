"""RBAC: roles, permission matrix, user management, endpoint mapping."""
from pathlib import Path

import pytest

from web.auth import (
    API_KEY_PERMISSIONS, ROLES, AuthStore, has_permission, normalize_role,
    permissions_for,
)


@pytest.fixture()
def store(tmp_path):
    return AuthStore(tmp_path)


# ---------------------------------------------------------------------------
# Permission matrix
# ---------------------------------------------------------------------------

def test_role_matrix():
    assert has_permission({"role": "owner"}, "users:manage")
    assert has_permission({"role": "owner"}, "anything:at-all")  # wildcard
    assert has_permission({"role": "admin"}, "users:manage")
    assert has_permission({"role": "admin"}, "settings:manage")
    assert has_permission({"role": "engineer"}, "jobs:run")
    assert not has_permission({"role": "engineer"}, "settings:manage")
    assert not has_permission({"role": "engineer"}, "users:manage")
    assert has_permission({"role": "viewer"}, "jobs:read")
    assert not has_permission({"role": "viewer"}, "jobs:run")
    assert not has_permission(None, "jobs:read")


def test_legacy_member_maps_to_engineer():
    assert normalize_role("member") == "engineer"
    assert permissions_for("member") == permissions_for("engineer")


def test_unknown_role_is_viewer():
    assert normalize_role("superuser") == "viewer"


def test_api_key_cannot_manage_people_or_settings():
    assert "jobs:run" in API_KEY_PERMISSIONS
    assert "users:manage" not in API_KEY_PERMISSIONS
    assert "settings:manage" not in API_KEY_PERMISSIONS


# ---------------------------------------------------------------------------
# User management on the store
# ---------------------------------------------------------------------------

def test_first_user_is_owner_then_role_assignment(store):
    u1 = store.create_user("a@x.com", "password1", "A")
    assert u1["role"] == "owner"
    u2 = store.create_user("b@x.com", "password2", "B", role="viewer")
    assert u2["role"] == "viewer"
    u3 = store.create_user("c@x.com", "password3", "C")  # default
    assert u3["role"] == "engineer"


def test_set_role_and_last_owner_protection(store):
    store.create_user("a@x.com", "password1")
    store.create_user("b@x.com", "password2", role="engineer")
    with pytest.raises(ValueError, match="last owner"):
        store.set_role("a@x.com", "viewer")
    store.set_role("b@x.com", "owner")          # promote a second owner
    assert store.set_role("a@x.com", "viewer")["role"] == "viewer"  # now allowed


def test_remove_user_kills_sessions_and_protects_last_owner(store):
    store.create_user("a@x.com", "password1")
    store.create_user("b@x.com", "password2")
    token = store.create_session("b@x.com")
    assert store.session_user(token) is not None
    store.remove_user("b@x.com")
    assert store.session_user(token) is None    # session revoked immediately
    with pytest.raises(ValueError, match="last owner"):
        store.remove_user("a@x.com")


def test_list_users_owner_first_no_secrets(store):
    store.create_user("z@x.com", "password1")
    store.create_user("a@x.com", "password2", role="viewer")
    users = store.list_users()
    assert users[0]["role"] == "owner"
    assert all("hash" not in u and "salt" not in u for u in users)


# ---------------------------------------------------------------------------
# Endpoint -> permission mapping
# ---------------------------------------------------------------------------

def test_required_permission_mapping():
    from web.app import _required_permission as rp
    assert rp("/api/users", "GET") == "users:manage"
    assert rp("/api/users/a@x.com", "DELETE") == "users:manage"
    assert rp("/api/settings/ai", "GET") == "jobs:read"
    assert rp("/api/settings/ai", "PUT") == "settings:manage"
    assert rp("/api/convert", "POST") == "jobs:run"
    assert rp("/api/jobs/abc/autofix", "POST") == "jobs:run"
    assert rp("/api/jobs/abc", "DELETE") == "jobs:delete"
    assert rp("/api/jobs", "GET") == "jobs:read"
    assert rp("/api/jobs/abc/report.json", "GET") == "jobs:read"


def test_required_permission_new_mappings():
    from web.app import _required_permission as rp
    # approvals: approve/claim need the distinct permission; reject passes
    # the gate at jobs:run (handler then requires approver-or-requester);
    # listing the queue is read-side so every role can FIND pending work
    assert rp("/api/agents/approvals/approve", "POST") == "agents:approve"
    assert rp("/api/agents/approvals/claim", "POST") == "agents:approve"
    assert rp("/api/agents/approvals/reject", "POST") == "jobs:run"
    assert rp("/api/agents/approvals", "GET") == "jobs:read"
    # plugin code loading / marketplace installs reconfigure the platform
    assert rp("/api/plugins/load", "POST") == "settings:manage"
    assert rp("/api/plugins/some-plugin", "DELETE") == "settings:manage"
    assert rp("/api/marketplace/install", "POST") == "settings:manage"
    assert rp("/api/marketplace/uninstall", "POST") == "settings:manage"
    assert rp("/api/marketplace/update", "POST") == "settings:manage"
    assert rp("/api/marketplace/keypair", "POST") == "settings:manage"
    assert rp("/api/marketplace", "GET") == "jobs:read"
    # ordinary runs are unchanged
    assert rp("/api/plugins/scaffold", "POST") == "jobs:run"
    # password-reset minting lives under the users namespace
    assert rp("/api/users/a@x.com/reset-link", "POST") == "users:manage"


def test_required_permission_prefix_boundaries():
    """A rule for /api/foo must not leak onto /api/foobar siblings."""
    from web.app import _required_permission as rp
    assert rp("/api/v1/metrics", "POST") == "jobs:run"       # not /api/v1/me
    assert rp("/api/settingsx", "PUT") == "jobs:run"         # not /api/settings
    assert rp("/api/usersx", "GET") == "jobs:read"           # not /api/users
    assert rp("/api/marketplacex", "POST") == "jobs:run"


# ---------------------------------------------------------------------------
# API-level enforcement: the owner role can only be granted/revoked by owners
# ---------------------------------------------------------------------------

import sys


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
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
    yield clients
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def test_admin_cannot_mint_or_touch_owner_accounts(workspace):
    adm = workspace["admin"]
    assert adm.post("/api/users", json={
        "email": "evil@x.com", "password": "password123",
        "role": "owner"}).status_code == 403
    assert adm.patch("/api/users/eng@x.com",
                     json={"role": "owner"}).status_code == 403
    assert adm.patch("/api/users/owner@x.com",
                     json={"role": "viewer"}).status_code == 403
    assert adm.delete("/api/users/owner@x.com").status_code == 403
    # normal member management still works for admins
    assert adm.post("/api/users", json={
        "email": "new@x.com", "password": "password123",
        "role": "engineer"}).status_code == 200
    assert adm.patch("/api/users/new@x.com",
                     json={"role": "viewer"}).status_code == 200
    assert adm.delete("/api/users/new@x.com").status_code == 200


def test_owner_can_manage_owner_role(workspace):
    owner = workspace["owner"]
    assert owner.post("/api/users", json={
        "email": "own2@x.com", "password": "password123",
        "role": "owner"}).status_code == 200
    assert owner.patch("/api/users/own2@x.com",
                       json={"role": "admin"}).status_code == 200
    assert owner.patch("/api/users/own2@x.com",
                       json={"role": "owner"}).status_code == 200
    assert owner.delete("/api/users/own2@x.com").status_code == 200


def test_engineer_and_viewer_cannot_manage_users_at_all(workspace):
    for role in ("engineer", "viewer"):
        cl = workspace[role]
        assert cl.get("/api/users").status_code == 403
        assert cl.post("/api/users", json={
            "email": "x@x.com", "password": "password123"}).status_code == 403
        assert cl.patch("/api/users/adm@x.com",
                        json={"role": "viewer"}).status_code == 403
        assert cl.delete("/api/users/adm@x.com").status_code == 403


def test_viewer_gets_403_on_runs_and_401_when_signed_out(workspace):
    view = workspace["viewer"]
    r = view.post("/api/agents/run", json={"files": []})
    assert r.status_code == 403
    assert "jobs:run" in r.json()["detail"]
    from fastapi.testclient import TestClient
    import web.app as webapp
    anon = TestClient(webapp.app)
    assert anon.get("/api/jobs").status_code == 401
    assert anon.post("/api/agents/approvals/approve",
                     json={"approval_id": "x"}).status_code == 401


def test_engineer_cannot_load_plugins_or_install_packages(workspace):
    eng = workspace["engineer"]
    assert eng.post("/api/plugins/load", json={"id": "x"}).status_code == 403
    assert eng.post("/api/marketplace/install",
                    json={"id": "x"}).status_code == 403
    assert eng.delete("/api/plugins/some-plugin").status_code == 403


def test_self_role_change_still_blocked_for_admins(workspace):
    adm = workspace["admin"]
    r = adm.patch("/api/users/adm@x.com", json={"role": "engineer"})
    assert r.status_code == 422
    assert "own role" in r.json()["detail"]


def test_self_guard_not_bypassable_via_whitespace_or_case(workspace):
    # the self-modification guard normalizes the {email} path segment, so a
    # trailing space or different case can't slip past it
    adm = workspace["admin"]
    r = adm.patch("/api/users/%20ADM@x.com%20", json={"role": "engineer"})
    assert r.status_code == 422 and "own role" in r.json()["detail"]
    r = adm.delete("/api/users/%20adm@x.com")
    assert r.status_code == 422 and "yourself" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Console UI mirrors the server rules
# ---------------------------------------------------------------------------

def test_console_gates_actions_by_permission():
    from pathlib import Path
    _web = Path(__file__).resolve().parents[1] / "web"
    console = ((_web / "templates" / "console.html").read_text(encoding="utf-8")
               + (_web / "static" / "js" / "console.js").read_text(encoding="utf-8"))
    assert "function can(p)" in console
    assert "applyRbacUi" in console and "RBAC_UI" in console
    # run-class and settings-class controls are covered by the static map
    for sel in ("#agRun", "#analyzeBtn", "#convertSelected", "#aiTest",
                "#twinBuild", "#mktKeypair"):
        assert sel in console
    # only owners may hand out the owner role in the member dialogs
    assert "roleOptions(" in console
    assert "myPerms._role === 'owner'" in console

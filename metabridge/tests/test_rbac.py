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

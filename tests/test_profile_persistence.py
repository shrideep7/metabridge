"""Profile save persistence (regression): what the user saves is exactly
what the API, the storage file, a fresh process, and the console form all
show afterwards — no silent display-side rewriting."""
import json
import sys
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[1] / "web"
CONSOLE = ((_WEB / "templates" / "console.html").read_text(encoding="utf-8")
           + (_WEB / "static" / "js" / "console.js").read_text(encoding="utf-8"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None) for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    c = TestClient(webapp.app)
    assert c.post("/auth/signup", json={
        "email": "user@example.com", "password": "password123",
        "name": "Original Name"}).status_code == 200
    c._data_dir = tmp_path
    c._webapp = webapp
    yield c
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


# names that the old display normalizer used to rewrite (all-caps tokens,
# repeated tokens) — saving them must round-trip EXACTLY
EXACT_NAMES = [
    "New Name",
    "SHRIDHAR VINCHURKAR",
    "Sachin Sachin Mane",
    "McDonald van der Berg",
    "MB",
]


@pytest.mark.parametrize("name", EXACT_NAMES)
def test_patch_me_round_trips_exact_name(client, name):
    r = client.patch("/api/v1/me", json={"name": name})
    assert r.status_code == 200
    assert r.json()["user"]["name"] == name
    assert client.get("/api/v1/me").json()["user"]["name"] == name
    # storage-level truth
    users = json.loads((client._data_dir / "users.json").read_text())
    assert users["user@example.com"]["name"] == name


def test_name_survives_restart_and_relogin(client):
    assert client.patch("/api/v1/me",
                        json={"name": "Renamed Person"}).status_code == 200
    # fresh store instance = process restart reading the same data dir
    from web.auth import AuthStore
    fresh = AuthStore(client._data_dir)
    u = fresh.verify_user("user@example.com", "password123")
    assert u is not None and u["name"] == "Renamed Person"
    # fresh login session sees the persisted name
    from fastapi.testclient import TestClient
    c2 = TestClient(client._webapp.app)
    assert c2.post("/auth/login", json={
        "email": "user@example.com",
        "password": "password123"}).status_code == 200
    assert c2.get("/api/v1/me").json()["user"]["name"] == "Renamed Person"


def test_whitespace_is_collapsed_but_nothing_else_changes(client):
    r = client.patch("/api/v1/me", json={"name": "  Ada   Lovelace  "})
    assert r.status_code == 200
    assert r.json()["user"]["name"] == "Ada Lovelace"


def test_validation_messages_are_actionable(client):
    r = client.patch("/api/v1/me", json={"name": "   "})
    assert r.status_code == 422
    assert "cannot be empty" in r.json()["detail"]
    r = client.patch("/api/v1/me", json={"name": "x" * 81})
    assert r.status_code == 422
    assert "80 characters" in r.json()["detail"]
    # failed saves change nothing
    assert client.get("/api/v1/me").json()["user"]["name"] == "Original Name"


def test_avatar_choice_persists_alongside_name(client):
    assert client.put("/api/v1/me/avatar",
                      json={"type": "PRESET",
                            "preset": "mb-3"}).status_code == 200
    assert client.patch("/api/v1/me",
                        json={"name": "Both Fields"}).status_code == 200
    me = client.get("/api/v1/me").json()["user"]
    assert me["name"] == "Both Fields"
    assert me["avatar"] == {"type": "PRESET", "preset": "mb-3"}


def test_anonymous_cannot_update_profile(client):
    from fastapi.testclient import TestClient
    anon = TestClient(client._webapp.app)
    assert anon.patch("/api/v1/me",
                      json={"name": "Hacker"}).status_code == 401


# -- console template: the form edits the RAW stored value -------------------

def test_console_profile_form_binds_raw_stored_name():
    assert "myUser.name || getUserDisplayName(myUser)" in CONSOLE


def test_console_display_name_no_longer_rewrites_saved_names():
    # the old normalizer un-shouted ALL-CAPS tokens and collapsed duplicate
    # adjacent tokens, which made saved edits look lost
    assert "t.slice(1).toLowerCase()" not in CONSOLE
    assert "toks[toks.length - 1].toLowerCase()" not in CONSOLE


def test_console_profile_save_has_success_feedback():
    assert 'id="pnOk"' in CONSOLE
    assert "#pnOk" in CONSOLE

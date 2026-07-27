"""Workspace settings + self-service profile name (Settings redesign)."""
import sys

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None) for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    c = TestClient(webapp.app)
    r = c.post("/auth/signup", json={
        "email": "owner@example.com", "password": "password123",
        "name": "Sachin Shrinivas Mane", "company": "Metafor Data"})
    assert r.status_code == 200
    yield c
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def test_workspace_defaults_and_owner(client):
    d = client.get("/api/settings/workspace").json()
    assert d["deployment"] == "self-hosted"
    assert d["workspace_id"] == ""            # not assigned until first save
    assert d["owner"]["email"] == "owner@example.com"


def test_workspace_save_generates_immutable_id(client):
    d = client.put("/api/settings/workspace",
                   json={"name": "MFD Workspace",
                         "timezone": "Asia/Kolkata"}).json()
    assert d["name"] == "MFD Workspace"
    assert d["workspace_id"] == "mfd-workspace"
    assert d["timezone"] == "Asia/Kolkata"
    # renaming keeps the generated id
    d2 = client.put("/api/settings/workspace",
                    json={"name": "Metafor Modernization"}).json()
    assert d2["workspace_id"] == "mfd-workspace"
    assert d2["name"] == "Metafor Modernization"


def test_workspace_validation(client):
    assert client.put("/api/settings/workspace",
                      json={"name": "   "}).status_code == 422
    assert client.put("/api/settings/workspace",
                      json={"name": "X", "timezone": "nonsense"}).status_code == 422


def test_workspace_save_requires_settings_manage(client):
    from fastapi.testclient import TestClient
    import web.app as webapp
    client.post("/api/users", json={"email": "eng@example.com",
                                    "password": "password123",
                                    "name": "Eng", "role": "engineer"})
    e = TestClient(webapp.app)
    e.post("/auth/login", json={"email": "eng@example.com",
                                "password": "password123"})
    assert e.get("/api/settings/workspace").status_code == 200   # readable
    assert e.put("/api/settings/workspace",
                 json={"name": "Nope"}).status_code == 403       # not writable


def test_profile_name_update_and_validation(client):
    r = client.patch("/api/v1/me", json={"name": "Sachin S. Mane"})
    assert r.status_code == 200
    assert r.json()["user"]["name"] == "Sachin S. Mane"
    me = client.get("/api/v1/me").json()["user"]
    assert me["name"] == "Sachin S. Mane"
    assert client.patch("/api/v1/me", json={"name": "   "}).status_code == 422


def test_clear_bedrock_token_on_iam_switch(client, tmp_path):
    client.put("/api/settings/ai", json={
        "provider": "bedrock", "region": "ap-south-1",
        "bedrock_token": "bearer-abc"})
    assert client.get("/api/settings/ai").json()["bedrock_token_set"] is True
    # switching auth back to the server IAM role detaches the stored key
    d = client.put("/api/settings/ai", json={
        "provider": "bedrock", "region": "ap-south-1",
        "clear_bedrock_token": True}).json()
    assert d["bedrock_token_set"] is False
    # ... and the raw settings file no longer holds it
    assert "bearer-abc" not in (tmp_path / "settings.json").read_text()

"""Forgot/Reset password: one-time expiring tokens, no-enumeration forgot
flow, admin-minted links (no-SMTP delivery), SMTP delivery, RBAC on minting,
session revocation and token invalidation after use."""
import json
import sys
import time

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None) for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    c = TestClient(webapp.app)
    r = c.post("/auth/signup", json={
        "email": "owner@example.com", "password": "ownerpass123",
        "name": "Owner One", "company": "Metafor"})
    assert r.status_code == 200
    c._data_dir = tmp_path
    c._webapp = webapp
    yield c
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def _login(webapp, email, password):
    from fastapi.testclient import TestClient
    cl = TestClient(webapp.app)
    r = cl.post("/auth/login", json={"email": email, "password": password})
    return cl, r


def _add_member(client, email, role, password="memberpass123"):
    r = client.post("/api/users", json={"email": email, "password": password,
                                        "name": email.split("@")[0],
                                        "role": role})
    assert r.status_code == 200, r.text
    return password


# -- store-level ------------------------------------------------------------

def test_store_token_lifecycle(tmp_path):
    from web.auth import AuthStore
    s = AuthStore(tmp_path)
    s.create_user("a@x.com", "password123")
    tok = s.create_reset_token("a@x.com")
    assert tok and s.peek_reset_token(tok) == "a@x.com"
    # only the digest is persisted — the secret never touches disk
    raw = (tmp_path / "reset_tokens.json").read_text()
    assert tok not in raw
    session = s.create_session("a@x.com")
    s.reset_password(tok, "newpassword1")
    assert s.peek_reset_token(tok) is None          # single use
    assert s.session_user(session) is None          # sessions revoked
    assert s.verify_user("a@x.com", "password123") is None
    assert s.verify_user("a@x.com", "newpassword1") is not None
    with pytest.raises(ValueError, match="invalid, expired, or already used"):
        s.reset_password(tok, "anotherpass1")


def test_store_one_live_token_per_account_and_expiry(tmp_path):
    from web.auth import AuthStore
    s = AuthStore(tmp_path)
    s.create_user("a@x.com", "password123")
    t1 = s.create_reset_token("a@x.com")
    t2 = s.create_reset_token("a@x.com")            # replaces t1
    assert s.peek_reset_token(t1) is None
    assert s.peek_reset_token(t2) == "a@x.com"
    # force expiry
    doc = json.loads((tmp_path / "reset_tokens.json").read_text())
    for rec in doc.values():
        rec["expires"] = time.time() - 1
    (tmp_path / "reset_tokens.json").write_text(json.dumps(doc))
    assert s.peek_reset_token(t2) is None
    with pytest.raises(ValueError):
        s.reset_password(t2, "newpassword1")


def test_store_no_token_for_unknown_account_and_weak_password(tmp_path):
    from web.auth import AuthStore
    s = AuthStore(tmp_path)
    s.create_user("a@x.com", "password123")
    assert s.create_reset_token("ghost@x.com") is None
    tok = s.create_reset_token("a@x.com")
    with pytest.raises(ValueError, match="at least 8"):
        s.reset_password(tok, "short")
    assert s.peek_reset_token(tok) == "a@x.com"     # failure does not burn


# -- pages ------------------------------------------------------------------

def test_pages_are_public(client):
    from fastapi.testclient import TestClient
    anon = TestClient(client._webapp.app)
    assert anon.get("/forgot-password").status_code == 200
    assert anon.get("/reset-password").status_code == 200
    assert "Forgot password?" in anon.get("/login").text


# -- forgot (no SMTP -> admin delivery) --------------------------------------

def test_forgot_answers_identically_for_unknown_accounts(client):
    r1 = client.post("/auth/forgot", json={"email": "owner@example.com"})
    r2 = client.post("/auth/forgot", json={"email": "ghost@example.com"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["detail"] == r2.json()["detail"]
    assert "token" not in r1.text.lower()


def test_forgot_notifies_admins_only_for_real_accounts(client):
    client.post("/auth/forgot", json={"email": "owner@example.com"})
    client.post("/auth/forgot", json={"email": "ghost@example.com"})
    notes = client.get("/api/system/notifications").json()["notifications"]
    auth_notes = [n for n in notes if n["topic"] == "auth"]
    assert len(auth_notes) == 1
    assert "owner@example.com" in auth_notes[0]["title"]
    assert "ghost" not in json.dumps(auth_notes)
    # deduped while unseen
    client.post("/auth/forgot", json={"email": "owner@example.com"})
    notes = client.get("/api/system/notifications").json()["notifications"]
    assert len([n for n in notes if n["topic"] == "auth"]) == 1


def test_forgot_rejects_garbage_email(client):
    assert client.post("/auth/forgot", json={"email": ""}).status_code == 422
    assert client.post("/auth/forgot", json={"email": "nope"}).status_code == 422


# -- admin-minted reset link --------------------------------------------------

def test_admin_mints_link_and_member_resets(client):
    webapp = client._webapp
    _add_member(client, "dev@example.com", "engineer")
    r = client.post("/api/users/dev@example.com/reset-link")
    assert r.status_code == 200
    body = r.json()
    assert body["one_time"] and body["expires_in_minutes"] == 60
    token = body["reset_link"].split("token=")[1]

    v = client.post("/auth/reset/validate", json={"token": token}).json()
    assert v["valid"] and "@example.com" in v["account"]
    assert "dev@example.com" not in v["account"]     # masked

    # the member had a live session; reset must revoke it
    dev, r = _login(webapp, "dev@example.com", "memberpass123")
    assert r.status_code == 200
    assert dev.post("/auth/reset",
                    json={"token": token,
                          "password": "freshpass456"}).status_code == 200
    assert dev.get("/api/v1/me").status_code == 401       # session gone
    _, old = _login(webapp, "dev@example.com", "memberpass123")
    assert old.status_code == 401
    _, new = _login(webapp, "dev@example.com", "freshpass456")
    assert new.status_code == 200
    # burned
    assert client.post("/auth/reset/validate",
                       json={"token": token}).json() == {"valid": False}
    assert client.post("/auth/reset",
                       json={"token": token,
                             "password": "yetanother7"}).status_code == 422


def test_mint_rbac(client):
    webapp = client._webapp
    _add_member(client, "adm@example.com", "admin")
    _add_member(client, "dev@example.com", "engineer")
    _add_member(client, "view@example.com", "viewer")
    adm, _ = _login(webapp, "adm@example.com", "memberpass123")
    dev, _ = _login(webapp, "dev@example.com", "memberpass123")
    view, _ = _login(webapp, "view@example.com", "memberpass123")
    from fastapi.testclient import TestClient
    anon = TestClient(webapp.app)

    assert adm.post("/api/users/dev@example.com/reset-link").status_code == 200
    assert dev.post("/api/users/adm@example.com/reset-link").status_code == 403
    assert view.post("/api/users/dev@example.com/reset-link").status_code == 403
    assert anon.post("/api/users/dev@example.com/reset-link").status_code == 401
    assert adm.post("/api/users/ghost@example.com/reset-link").status_code == 404
    # an admin must not be able to take over the OWNER account via reset
    assert adm.post(
        "/api/users/owner@example.com/reset-link").status_code == 403
    # the owner may mint for anyone, including another owner
    assert client.post(
        "/api/users/owner@example.com/reset-link").status_code == 200


def test_reset_weak_password_and_bad_token(client):
    _add_member(client, "dev@example.com", "engineer")
    token = client.post("/api/users/dev@example.com/reset-link") \
        .json()["reset_link"].split("token=")[1]
    r = client.post("/auth/reset", json={"token": token, "password": "short"})
    assert r.status_code == 422 and "at least 8" in r.json()["detail"]
    r = client.post("/auth/reset", json={"token": "bogus",
                                         "password": "longenough1"})
    assert r.status_code == 422
    # the real token survives the failed attempts
    assert client.post("/auth/reset/validate",
                       json={"token": token}).json()["valid"]


# -- SMTP delivery ------------------------------------------------------------

class _FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=0):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


def test_forgot_with_smtp_emails_a_working_link(client, monkeypatch):
    import smtplib
    monkeypatch.setenv("METABRIDGE_SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("METABRIDGE_PUBLIC_URL", "https://mb.example.com")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent = []
    _add_member(client, "dev@example.com", "engineer")

    r = client.post("/auth/forgot", json={"email": "dev@example.com"})
    assert r.status_code == 200 and r.json()["delivery"] == "email"
    # unknown account: same outward answer, no email
    r2 = client.post("/auth/forgot", json={"email": "ghost@example.com"})
    assert r2.json()["detail"] == r.json()["detail"]
    # delivery happens off-request (so timing can't enumerate accounts) —
    # wait for the background send
    deadline = time.time() + 5
    while not _FakeSMTP.sent and time.time() < deadline:
        time.sleep(0.05)
    assert len(_FakeSMTP.sent) == 1
    msg = _FakeSMTP.sent[0]
    assert msg["To"] == "dev@example.com"
    link = [ln for ln in msg.get_content().splitlines()
            if "reset-password#token=" in ln][0].strip()
    assert link.startswith("https://mb.example.com/reset-password#token=")
    token = link.split("token=")[1]
    assert client.post("/auth/reset",
                       json={"token": token,
                             "password": "mailedpass1"}).status_code == 200


def test_reset_endpoints_reject_malformed_json(client):
    # public endpoints must 422 on garbage, never 500
    for path in ("/auth/forgot", "/auth/reset", "/auth/reset/validate"):
        r = client.post(path, content=b"{not json",
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 422, (path, r.status_code)
        r = client.post(path, json=["not", "an", "object"])
        assert r.status_code == 422, (path, r.status_code)


def test_validate_is_post_so_token_never_hits_a_query_string(client):
    _add_member(client, "dev@example.com", "engineer")
    link = client.post("/api/users/dev@example.com/reset-link") \
        .json()["reset_link"]
    # the reset link carries the token in the FRAGMENT, not the query
    assert "/reset-password#token=" in link and "?token=" not in link
    token = link.split("token=")[1]
    # validation happens over POST with the token in the body
    assert client.post("/auth/reset/validate",
                       json={"token": token}).json()["valid"]
    # the old GET-with-query form is gone (would have logged the secret)
    assert client.get("/auth/reset/validate",
                      params={"token": token}).status_code == 405


class _NoSendSMTP(_FakeSMTP):
    pass


def test_forgot_never_builds_email_link_from_host_header(client, monkeypatch):
    """Host-header poisoning guard: an unauthenticated /auth/forgot must not
    email a link whose host comes from the (spoofable) Host header."""
    import smtplib
    monkeypatch.setenv("METABRIDGE_SMTP_HOST", "mail.example.com")
    monkeypatch.delenv("METABRIDGE_PUBLIC_URL", raising=False)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent = []
    _add_member(client, "dev@example.com", "engineer")

    # attacker spoofs the Host header
    r = client.post("/auth/forgot", json={"email": "dev@example.com"},
                    headers={"Host": "evil.attacker.example"})
    assert r.status_code == 200
    # with no configured public URL we refuse to email a Host-derived link
    # and fall back to admin-notification delivery instead
    assert r.json()["delivery"] == "admin"
    time.sleep(0.3)
    assert _FakeSMTP.sent == []          # nothing emailed to a spoofed host


def test_forgot_email_link_uses_configured_public_url_not_host(client,
                                                               monkeypatch):
    import smtplib
    monkeypatch.setenv("METABRIDGE_SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("METABRIDGE_PUBLIC_URL", "https://mb.example.com")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent = []
    _add_member(client, "dev@example.com", "engineer")

    r = client.post("/auth/forgot", json={"email": "dev@example.com"},
                    headers={"Host": "evil.attacker.example"})
    assert r.status_code == 200 and r.json()["delivery"] == "email"
    deadline = time.time() + 5
    while not _FakeSMTP.sent and time.time() < deadline:
        time.sleep(0.05)
    body = _FakeSMTP.sent[0].get_content()
    assert "https://mb.example.com/reset-password" in body
    assert "evil.attacker.example" not in body


def test_cli_reset_link_mints_usable_token(client):
    from metabridge import cli
    from typer.testing import CliRunner
    res = CliRunner().invoke(cli.app, ["reset-link", "owner@example.com",
                                       "--data-dir", str(client._data_dir),
                                       "--base-url", "http://mb.local"])
    assert res.exit_code == 0
    link = [ln for ln in res.output.splitlines()
            if "reset-password#token=" in ln][0].strip()
    token = link.split("token=")[1]
    assert client.post("/auth/reset/validate",
                       json={"token": token}).json()["valid"]
    res = CliRunner().invoke(cli.app, ["reset-link", "ghost@example.com",
                                       "--data-dir", str(client._data_dir)])
    assert res.exit_code == 1

"""Outbound email: transport config, persistence and the settings API.

Covers the notify package (previously untested) and the console's
Settings -> Notifications endpoints. A tiny in-process SMTP sink stands in for
SES so the send path is exercised for real without touching the network.
"""
import json
import os
import socket
import threading
import time

import pytest


# --------------------------------------------------------------------------
# a minimal SMTP sink (stdlib only -- `smtpd` was removed in Python 3.12)
# --------------------------------------------------------------------------

class SMTPSink:
    """Accepts mail on an ephemeral port and records the raw messages."""

    def __init__(self):
        self.messages = []
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self._srv.settimeout(10)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        f = conn.makefile("rwb")

        def snd(s):
            f.write((s + "\r\n").encode())
            f.flush()

        snd("220 sink ESMTP")
        body, in_data = [], False
        while True:
            line = f.readline()
            if not line:
                break
            text = line.decode("utf8", "replace").rstrip("\r\n")
            if in_data:
                if text == ".":
                    in_data = False
                    self.messages.append("\n".join(body))
                    body = []
                    snd("250 OK queued")
                else:
                    body.append(text)
                continue
            up = text.upper()
            if up.startswith(("EHLO", "HELO")):
                snd("250-sink")
                snd("250 AUTH LOGIN PLAIN")
            elif up.startswith("AUTH"):
                snd("235 authenticated")
            elif up.startswith(("MAIL", "RCPT")):
                snd("250 OK")
            elif up.startswith("DATA"):
                snd("354 send it")
                in_data = True
            elif up.startswith("QUIT"):
                snd("221 bye")
                break
            else:
                snd("250 OK")
        conn.close()

    def wait(self, n=1, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end and len(self.messages) < n:
            time.sleep(0.05)
        return self.messages

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass


_ENV_PREFIXES = ("METABRIDGE_SMTP", "METABRIDGE_NOTIFY", "METABRIDGE_EMAIL")


@pytest.fixture
def mailer(tmp_path, monkeypatch):
    """The mailer with an isolated settings file and a clean environment."""
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    for key in list(os.environ):
        if key.startswith(_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    from metabridge.notify import mailer as m
    return m


@pytest.fixture
def sink():
    s = SMTPSink()
    yield s
    s.close()


# --- status ---------------------------------------------------------------

def test_status_unconfigured_explains_what_to_set(mailer):
    st = mailer.email_status()
    assert st["ready"] is False and st["configured"] is False
    assert st["provider"] == "none"
    assert any("METABRIDGE_SMTP_HOST" in n for n in st["notes"])


def test_status_flags_missing_from_address(mailer):
    mailer.save_settings(host="smtp.example.com")
    st = mailer.email_status()
    assert st["configured"] is True and st["ready"] is False
    assert any("METABRIDGE_SMTP_FROM" in n for n in st["notes"])


def test_ses_host_is_labelled_as_ses(mailer):
    mailer.save_settings(host="email-smtp.ap-south-1.amazonaws.com",
                         sender="no-reply@example.com")
    assert mailer.email_status()["provider"] == "Amazon SES (SMTP)"


def test_status_never_returns_the_password(mailer):
    mailer.save_settings(host="smtp.example.com", sender="a@b.com",
                         password="hunter2")
    st = mailer.email_status()
    assert "password" not in st
    assert st["password_set"] is True
    assert "hunter2" not in json.dumps(st)


# --- persistence ----------------------------------------------------------

def test_saved_settings_make_transport_ready(mailer):
    st = mailer.save_settings(host="smtp.example.com", port="587", user="u",
                              password="p", sender="no-reply@example.com")
    assert st["ready"] is True
    assert mailer.email_enabled() is True


def test_empty_password_keeps_the_stored_one(mailer):
    mailer.save_settings(host="smtp.example.com", sender="a@b.com",
                         password="original")
    mailer.save_settings(host="smtp.example.com", sender="a@b.com",
                         password="")
    assert mailer._cfg()["password"] == "original"


def test_clear_password_removes_it(mailer):
    mailer.save_settings(host="smtp.example.com", sender="a@b.com",
                         password="original")
    mailer.save_settings(host="smtp.example.com", sender="a@b.com",
                         password="", clear_password=True)
    assert mailer._cfg()["password"] == ""


def test_clearing_the_host_drops_the_credential(mailer):
    mailer.save_settings(host="smtp.example.com", sender="a@b.com",
                         password="secret")
    mailer.save_settings(host="", sender="")
    assert mailer._cfg()["password"] == ""
    assert mailer.email_status()["ready"] is False


def test_invalid_port_falls_back_to_587(mailer):
    assert mailer.save_settings(host="h", sender="a@b.com",
                                port="not-a-number")["port"] == 587
    assert mailer.save_settings(host="h", sender="a@b.com",
                                port="99999")["port"] == 587


def test_corrupt_settings_file_does_not_break_status(mailer, tmp_path):
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    assert mailer.email_status()["ready"] is False


def test_saving_email_preserves_other_settings_sections(mailer, tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"ai": {"provider": "anthropic"}}),
                 encoding="utf-8")
    mailer.save_settings(host="smtp.example.com", sender="a@b.com")
    doc = json.loads(f.read_text(encoding="utf-8"))
    assert doc["ai"]["provider"] == "anthropic"      # untouched
    assert doc["email"]["host"] == "smtp.example.com"


# --- environment precedence ----------------------------------------------

def test_environment_wins_over_saved_settings(mailer, monkeypatch):
    mailer.save_settings(host="saved.example.com", sender="a@b.com")
    monkeypatch.setenv("METABRIDGE_SMTP_HOST", "env.example.com")
    st = mailer.email_status()
    assert st["host"] == "env.example.com"
    assert st["env_locked"]["host"] is True


def test_unset_environment_leaves_saved_values_in_place(mailer):
    mailer.save_settings(host="saved.example.com", sender="a@b.com")
    st = mailer.email_status()
    assert st["host"] == "saved.example.com"
    assert st["env_locked"]["host"] is False


def test_master_switch_off_in_panel_blocks_sending(mailer):
    mailer.save_settings(host="smtp.example.com", sender="a@b.com",
                         enabled=False)
    st = mailer.email_status()
    assert st["ready"] is False and mailer.email_enabled() is False
    assert any("switched off in this panel" in n for n in st["notes"])


def test_env_master_switch_overrides_a_saved_on(mailer, monkeypatch):
    mailer.save_settings(host="smtp.example.com", sender="a@b.com",
                         enabled=True)
    monkeypatch.setenv("METABRIDGE_NOTIFY_EMAIL", "0")
    st = mailer.email_status()
    assert st["ready"] is False
    assert any("METABRIDGE_NOTIFY_EMAIL" in n for n in st["notes"])


# --- sending --------------------------------------------------------------

def test_send_is_skipped_when_unconfigured(mailer):
    res = mailer.send_email("ops@example.com", "s", "t")
    assert res["ok"] is False and res["skipped"] is True


def test_send_delivers_over_smtp(mailer, sink):
    mailer.save_settings(host="127.0.0.1", port=str(sink.port),
                         sender="no-reply@localhost", starttls=False)
    res = mailer.send_email("ops@example.com", "Hello", "body text")
    assert res["ok"] is True
    body = sink.wait(1)[0]
    assert "Subject: Hello" in body
    assert "To: ops@example.com" in body


def test_send_rejects_a_recipientless_call(mailer, sink):
    mailer.save_settings(host="127.0.0.1", port=str(sink.port),
                         sender="no-reply@localhost", starttls=False)
    res = mailer.send_email(["not-an-address"], "s", "t")
    assert res["ok"] is False and res["skipped"] is True


def test_send_never_raises_on_a_dead_relay(mailer):
    # port 9 (discard) refuses SMTP -- the failure must be reported, not raised
    mailer.save_settings(host="127.0.0.1", port="9",
                         sender="no-reply@localhost", starttls=False)
    res = mailer.send_email("ops@example.com", "s", "t")
    assert res["ok"] is False and "error" in res


def test_service_event_sends_a_built_message(mailer, sink):
    mailer.save_settings(host="127.0.0.1", port=str(sink.port),
                         sender="no-reply@localhost", starttls=False)
    from metabridge.notify import service
    res = service.member_invited("new@example.com", "New User", "Acme",
                                 "analyst", "Om", sync=True)
    assert res["ok"] is True
    assert "To: new@example.com" in sink.wait(1)[0]


# --- settings API ---------------------------------------------------------

@pytest.fixture
def client(mailer, monkeypatch):
    """TestClient authorised as an owner.

    Both the access middleware (which maps these routes to ``settings:manage``)
    and the per-handler owner check must be satisfied; RBAC itself is covered
    in the platform tests."""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from web import app as webapp
    user = {"email": "ops@example.com", "name": "Om", "role": "owner"}
    monkeypatch.setattr(webapp, "_require_owner", lambda request: None)
    monkeypatch.setattr(webapp, "_request_user", lambda request: user)
    # the middleware runs before the handler: give the request full perms
    # `perms` was added as a third parameter; the real function defaults it,
    # but a stub with a fixed arity does not — every request through the auth
    # middleware raised TypeError rather than reaching the route.
    monkeypatch.setattr(webapp, "_required_permission",
                        lambda path, method, perms=None: "jobs:read")
    monkeypatch.setattr(webapp.AUTH, "has_users", lambda: False)
    monkeypatch.setattr(webapp, "API_KEY", "")
    return TestClient(webapp.app)


def test_get_reports_unconfigured(client):
    body = client.get("/api/settings/notifications/email").json()
    assert body["ready"] is False and body["provider"] == "none"


def test_put_saves_and_reports_ready(client):
    body = client.put("/api/settings/notifications/email",
                      json={"host": "smtp.example.com", "port": "587",
                            "user": "u", "password": "p",
                            "from": "no-reply@example.com"}).json()
    assert body["ready"] is True
    assert body["password_set"] is True
    assert "password" not in body


def test_put_rejects_a_host_without_a_sender(client):
    r = client.put("/api/settings/notifications/email",
                   json={"host": "smtp.example.com", "from": ""})
    assert r.status_code == 422


def test_put_rejects_a_malformed_sender(client):
    r = client.put("/api/settings/notifications/email",
                   json={"host": "h", "from": "not-an-address"})
    assert r.status_code == 422


@pytest.mark.parametrize("port", ["99999", "0", "abc"])
def test_put_rejects_an_out_of_range_port(client, port):
    r = client.put("/api/settings/notifications/email",
                   json={"host": "h", "from": "a@b.com", "port": port})
    assert r.status_code == 422


def test_test_send_refuses_until_configured(client):
    r = client.post("/api/settings/notifications/email/test")
    assert r.status_code == 422


def test_test_send_delivers_to_the_signed_in_operator(client, sink):
    client.put("/api/settings/notifications/email",
               json={"host": "127.0.0.1", "port": str(sink.port),
                     "from": "no-reply@localhost", "starttls": False})
    r = client.post("/api/settings/notifications/email/test")
    assert r.status_code == 200
    assert r.json()["sent_to"] == "ops@example.com"
    assert "To: ops@example.com" in sink.wait(1)[0]

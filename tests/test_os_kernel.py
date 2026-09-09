"""MetaBridge OS kernel — canonical registry, engine/service registry, the
self-describing manifest, and the three new platform services (feature
flags, notifications, version management)."""
import sys

import pytest

from metabridge.platform import (CANONICAL_MODELS, ENGINES, CATEGORY_ORDER,
                                 FeatureFlags, MetaBridgeOS,
                                 NotificationCenter, PlatformRegistry,
                                 VersionRegistry, canonical_registry,
                                 system_manifest)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "os"))


# --- canonical models -----------------------------------------------------

def test_canonical_models_resolve():
    reg = canonical_registry()
    assert len(reg) == 7
    assert all(m["available"] for m in reg), \
        [m for m in reg if not m["available"]]


def test_canonical_producers_consumers_are_real_engines():
    engine_ids = {e.id for e in ENGINES}
    for m in CANONICAL_MODELS:
        for ref in tuple(m.produced_by) + tuple(m.consumed_by):
            assert ref in engine_ids, "%s references unknown engine %r" % (
                m.id, ref)


# --- engine registry ------------------------------------------------------

def test_sixteen_engines_all_available():
    reg = PlatformRegistry()
    engines = [e.to_dict() for e in reg.engines()]
    assert len(engines) == 16
    unavailable = [e["id"] for e in engines if e["health"] != "available"]
    assert not unavailable, unavailable
    assert not reg.validate_canonical_refs()      # every consumes/produces real
    assert all(e["category"] in CATEGORY_ORDER for e in engines)


def test_engine_set_covers_the_required_engines():
    names = {e.id for e in ENGINES}
    required = {"data_estate", "semantic", "migration", "pipeline_studio",
                "validation", "governance", "digital_twin", "ai_readiness",
                "security", "technical_debt", "finops", "documentation",
                "marketplace", "plugin_sdk", "agent_orchestration",
                "observability"}
    assert required <= names


def test_nine_services_registered_and_resolvable():
    svc = [s.to_dict() for s in PlatformRegistry().services()]
    assert len(svc) == 9
    assert {s["id"] for s in svc} == {
        "authentication", "rbac", "audit", "reporting", "notifications",
        "secrets", "version_management", "feature_flags", "plugin_registry"}
    for s in svc:
        if s["layer"] == "platform":
            assert s["health"] == "available", s
        else:
            assert s["health"] in ("available", "external"), s


# --- kernel ---------------------------------------------------------------

def test_kernel_manifest():
    m = MetaBridgeOS().manifest()
    assert m["os"]["engine_count"] == 16
    assert m["os"]["service_count"] == 9
    assert m["os"]["canonical_model_count"] == 7
    assert m["health"]["operational"] is True
    assert m["integrity"]["canonical_ref_problems"] == []
    assert {"engines_by_category", "services", "versions", "feature_flags",
            "notifications", "canonical_models"} <= set(m)
    assert system_manifest()["os"]["engine_count"] == 16


# --- feature flags --------------------------------------------------------

def test_flags_defaults_and_determinism():
    f = FeatureFlags()
    assert f.evaluate("agent_orchestration") is True
    assert f.evaluate("ai_llm_assist") is False   # off by default (honest)
    f.set("ai_llm_assist", enabled=True, rollout_pct=50)
    a = f.evaluate("ai_llm_assist", subject="user-42")
    assert a == f.evaluate("ai_llm_assist", subject="user-42")   # stable
    f.set("ai_llm_assist", rollout_pct=0)
    assert f.evaluate("ai_llm_assist", subject="anyone") is False
    f.set("ai_llm_assist", rollout_pct=100)
    assert f.evaluate("ai_llm_assist", subject="anyone") is True


def test_flags_role_gating():
    f = FeatureFlags()
    f.set("marketplace_publishing", roles=["admin", "owner"])
    assert f.evaluate("marketplace_publishing", role="admin") is True
    assert f.evaluate("marketplace_publishing", role="viewer") is False
    assert f.evaluate("nonexistent_flag") is False


# --- notifications --------------------------------------------------------

def test_notifications_lifecycle(tmp_path):
    # its own store: the center persists now, and a bare one loads whatever
    # the running machine already had, which made the count machine-specific
    n = NotificationCenter(data_dir=str(tmp_path / "nc"))
    n.notify("migration", "Done", "ok", "success")
    n.notify("observability", "SLA breach", "avail 80%", "warning")
    c = n.counts()
    assert c["total"] == 2 and c["unseen"] == 2
    assert c["by_severity"]["warning"] == 1
    assert n.recent(1)[0]["title"] == "SLA breach"   # newest first
    assert n.mark_seen() == 2 and n.counts()["unseen"] == 0
    assert n.notify("x", "t", severity="bogus")["severity"] == "info"


def test_notifications_capped(tmp_path):
    n = NotificationCenter(data_dir=str(tmp_path / "nc"))
    for i in range(520):
        n.notify("t", "m%d" % i)
    assert n.counts()["total"] == 500


# --- versions -------------------------------------------------------------

def test_versions_compatibility():
    v = VersionRegistry()
    assert v.get("plugin_api") == "1.0"
    assert v.compatible("plugin_api", ">=1.0,<2.0") is True
    assert v.compatible("metabridge_os", ">=99") is False
    assert v.compatible("unknown_component", "*") is False
    assert v.check({"plugin_api": ">=1.0",
                    "metabridge_os": ">=99"})["compatible"] is False
    v.register("my_ext", "2.3.0")
    assert v.get("my_ext") == "2.3.0"


# --- API ------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
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


def test_api_system_manifest(client):
    d = client.get("/api/system").json()
    assert d["os"]["engine_count"] == 16 and d["os"]["service_count"] == 9
    assert len(d["canonical_models"]) == 7
    assert d["health"]["operational"] is True
    assert client.get("/api/system/health").json()["engines_total"] == 16


def test_api_flag_set_and_rbac(client):
    from fastapi.testclient import TestClient
    r = client.post("/api/system/flags",
                    json={"key": "ai_llm_assist", "enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True

    # a viewer may read the manifest but NOT change flags
    client.post("/api/users", json={"email": "v@x.com",
                                    "password": "Pw123456!", "name": "V",
                                    "role": "viewer"})
    viewer = TestClient(client.app)
    viewer.post("/auth/login", json={"email": "v@x.com",
                                     "password": "Pw123456!"})
    assert viewer.get("/api/system").status_code == 200
    assert viewer.post("/api/system/flags",
                       json={"key": "observability", "enabled": False}
                       ).status_code == 403


def test_api_notifications_and_bad_input(client):
    d = client.get("/api/system/notifications").json()
    assert "counts" in d and "notifications" in d
    assert client.post("/api/system/notifications/seen",
                       json={}).status_code == 200
    assert client.post("/api/system/flags", json={}).status_code == 422
    assert client.post("/api/system/flags",
                       json={"key": "x", "rollout_pct": "lots"}
                       ).status_code == 422


# --- adversarial-review regressions --------------------------------------

import json as _json


def _write_flags(base, obj):
    p = base / "platform"
    p.mkdir(parents=True, exist_ok=True)
    (p / "flags.json").write_text(_json.dumps(obj))


def test_flags_stored_string_enabled_fails_closed(tmp_path):
    # a hand-edited/externally-written 'enabled':'false' must NOT read ON
    _write_flags(tmp_path / "ff", {"ai_llm_assist": {"enabled": "false"}})
    f = FeatureFlags(data_dir=str(tmp_path / "ff"))
    assert f.evaluate("ai_llm_assist") is False
    # every non-bool spelling is treated as off (only real JSON true enables)
    _write_flags(tmp_path / "ff2", {"ai_llm_assist": {"enabled": "true"}})
    assert FeatureFlags(data_dir=str(tmp_path / "ff2")
                        ).evaluate("ai_llm_assist") is False


def test_flags_roles_as_string_no_substring_gating(tmp_path):
    # a malformed roles string must never grant via substring membership
    _write_flags(tmp_path / "r", {"marketplace_publishing":
                                  {"enabled": True, "roles": "admin"}})
    f = FeatureFlags(data_dir=str(tmp_path / "r"))
    # 'd' is a char of 'admin' but must NOT be treated as a matching role;
    # a malformed roles value is normalized away (no substring behaviour)
    assert f.evaluate("marketplace_publishing", role="d") == \
        f.evaluate("marketplace_publishing", role="zzz")
    # a PROPER list still gates correctly
    f.set("marketplace_publishing", roles=["admin"])
    assert f.evaluate("marketplace_publishing", role="admin") is True
    assert f.evaluate("marketplace_publishing", role="d") is False


def test_flags_non_numeric_rollout_does_not_crash(tmp_path):
    _write_flags(tmp_path / "rp", {"observability":
                                   {"enabled": True, "rollout_pct": "lots"}})
    f = FeatureFlags(data_dir=str(tmp_path / "rp"))
    assert f.evaluate("observability", subject="u") is True   # coerced to 100


def test_versions_malformed_spec_fails_closed():
    v = VersionRegistry()
    assert v.compatible("plugin_api", "@@@bad") is False       # no raise
    chk = v.check({"plugin_api": "@@@bad", "metabridge_os": ">=0.1"})
    assert chk["results"][0]["satisfied"] is False
    assert chk["results"][1]["satisfied"] is True              # kept going


def test_notifications_resilient_to_malformed_file(tmp_path):
    p = tmp_path / "nc" / "platform"
    p.mkdir(parents=True, exist_ok=True)
    # inner types wrong + a partial record lacking id/seen
    (p / "notifications.json").write_text(_json.dumps(
        {"log": [{"title": "legacy"}, "junk"], "subscriptions": "nope"}))
    n = NotificationCenter(data_dir=str(tmp_path / "nc"))
    assert n.counts()["total"] >= 0            # no crash
    assert n.mark_seen() >= 0                  # no KeyError
    assert isinstance(n.recent(5), list)


def test_health_operational_spans_services_and_models():
    h = PlatformRegistry().health()
    assert {"engines_operational", "services_operational",
            "canonical_resolvable", "operational"} <= set(h)
    assert h["operational"] == (h["engines_operational"]
                                and h["services_operational"]
                                and h["canonical_resolvable"])


def test_viewer_can_ack_own_notifications(client):
    from fastapi.testclient import TestClient
    client.post("/api/users", json={"email": "vw@x.com",
                                    "password": "Pw123456!", "name": "VW",
                                    "role": "viewer"})
    vw = TestClient(client.app)
    vw.post("/auth/login", json={"email": "vw@x.com", "password": "Pw123456!"})
    assert vw.post("/api/system/notifications/seen",
                   json={}).status_code == 200      # read-side action allowed

"""Enterprise Marketplace: Ed25519 signing, catalog, install lifecycle."""
import copy
import sys
from pathlib import Path

import pytest

from metabridge.marketplace.package import (ITEM_TYPES, MarketplaceItem,
                                            MarketplaceError, verify_item,
                                            sign_item, generate_keypair,
                                            FIRST_PARTY_PRIVATE_HEX)
from metabridge.marketplace.catalog import MarketplaceCatalog
from metabridge.marketplace.install import InstallManager
from metabridge.plugins.registry import PluginRegistry
from metabridge.plugins.builtins import register_builtins


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "iso"))


@pytest.fixture()
def cat():
    return MarketplaceCatalog()


@pytest.fixture()
def mgr(cat, tmp_path):
    reg = PluginRegistry()
    register_builtins(reg)
    return InstallManager(catalog=cat, data_dir=str(tmp_path / "mkt"),
                          plugin_registry=reg)


# --- catalog -------------------------------------------------------------

def test_catalog_covers_all_eight_types(cat):
    types = {i.type for i in cat.all()}
    assert types == set(ITEM_TYPES)
    assert len(cat.all()) >= 8


# --- signing (the supply-chain-critical core) ----------------------------

def test_first_party_items_all_verify(cat):
    assert all(verify_item(i, cat.trust)["verified"] for i in cat.all())


def test_tampered_payload_is_rejected(cat):
    it = copy.deepcopy(cat.get("mkt.transform-lib.scd2-macros"))
    it.payload["files"]["scd2.sql"] = "DROP TABLE users;"   # tamper
    v = verify_item(it, cat.trust)
    assert not v["verified"]
    assert v["status"] == "checksum_mismatch"


def test_forged_signature_is_rejected(cat):
    it = copy.deepcopy(cat.get("mkt.transform-lib.scd2-macros"))
    atk = generate_keypair()                    # attacker key
    sign_item(it, atk["private_key"])           # publisher still 'metabridge'
    assert verify_item(it, cat.trust)["status"] == "signature_invalid"


def test_untrusted_publisher_is_rejected(cat):
    atk = generate_keypair()
    it = MarketplaceItem(id="x.evil", type="connector", name="Evil",
                         version="1.0", publisher="evilcorp")
    sign_item(it, atk["private_key"])
    assert verify_item(it, cat.trust)["status"] == "untrusted_publisher"


def test_unsigned_is_flagged(cat):
    it = MarketplaceItem(id="x.un", type="connector", name="U",
                         version="1.0")
    assert verify_item(it, cat.trust)["status"] == "unsigned"


def test_trusting_a_publisher_key_lets_it_verify(cat):
    kp = generate_keypair()
    it = MarketplaceItem(id="x.acme", type="connector", name="Acme",
                         version="1.0", publisher="acme")
    sign_item(it, kp["private_key"])
    assert not verify_item(it, cat.trust)["verified"]      # untrusted
    cat.trust.trust("acme", kp["public_key"])
    assert verify_item(it, cat.trust)["verified"]          # now trusted


# --- install refuses unverified / incompatible / unlicensed --------------

def test_install_refuses_unsigned(cat, mgr):
    kp = generate_keypair()
    it = MarketplaceItem(id="mkt.bad.unsigned", type="business_rules",
                         name="Bad", version="1.0",
                         publisher="acme", payload={"files": {"x": "y"}})
    cat.publish(it)                              # published but unsigned
    with pytest.raises(MarketplaceError):
        mgr.install("mkt.bad.unsigned")
    # explicit override installs it
    rep = mgr.install("mkt.bad.unsigned", allow_unverified=True)
    assert "mkt.bad.unsigned" in rep["installed"]


def test_install_refuses_incompatible(cat, mgr):
    it = MarketplaceItem(
        id="mkt.future.thing", type="business_rules", name="Future",
        version="1.0", publisher="metabridge",
        compatibility={"plugin_api": ">=2.0"},
        payload={"files": {"x": "y"}})
    sign_item(it, FIRST_PARTY_PRIVATE_HEX)
    cat.publish(it)
    with pytest.raises(MarketplaceError):
        mgr.install("mkt.future.thing")


def test_license_acceptance_gate(mgr):
    # sql-explainer is Commercial requires_acceptance
    with pytest.raises(MarketplaceError):
        mgr.install("mkt.ai-skill.sql-explainer")
    rep = mgr.install("mkt.ai-skill.sql-explainer", accept_license=True)
    assert "mkt.ai-skill.sql-explainer" in rep["installed"]


# --- dependency resolution -----------------------------------------------

def test_dependency_resolution_order(mgr):
    order = [i.id for i in mgr.resolve("mkt.accelerator.healthcare-hipaa")]
    # dependencies must precede the dependent
    assert order[-1] == "mkt.accelerator.healthcare-hipaa"
    assert "mkt.business-rules.phi-masking" in order[:-1]
    assert "mkt.template.medallion-lakehouse" in order[:-1]


def test_install_pulls_dependencies(mgr):
    rep = mgr.install("mkt.accelerator.healthcare-hipaa",
                      accept_license=True)
    assert set(rep["installed"]) >= {
        "mkt.business-rules.phi-masking",
        "mkt.template.medallion-lakehouse",
        "mkt.accelerator.healthcare-hipaa"}
    assert mgr.is_installed("mkt.business-rules.phi-masking")


def test_dependency_cycle_detected(cat, mgr):
    def item(iid, dep):
        it = MarketplaceItem(id=iid, type="business_rules", name=iid,
                             version="1.0", publisher="metabridge",
                             dependencies=[{"id": dep, "version": ">=1.0"}],
                             payload={"files": {"r": iid}})
        sign_item(it, FIRST_PARTY_PRIVATE_HEX)
        return it
    cat.publish(item("mkt.cyc.a", "mkt.cyc.b"))
    cat.publish(item("mkt.cyc.b", "mkt.cyc.a"))
    with pytest.raises(MarketplaceError):
        mgr.resolve("mkt.cyc.a")


def test_dependency_version_conflict_detected(cat, mgr):
    # a dep pinned to two incompatible ranges cannot resolve
    def rules(iid, ver):
        it = MarketplaceItem(id=iid, type="business_rules", name=iid,
                             version=ver, publisher="metabridge",
                             versions=[ver], payload={"files": {"r": ver}})
        sign_item(it, FIRST_PARTY_PRIVATE_HEX)
        return it
    cat.publish(rules("mkt.dep.leaf", "1.0.0"))
    cat.publish(rules("mkt.dep.leaf", "2.0.0"))

    def dependent(iid, spec):
        it = MarketplaceItem(id=iid, type="pipeline_template", name=iid,
                             version="1.0", publisher="metabridge",
                             dependencies=[{"id": "mkt.dep.leaf",
                                            "version": spec}],
                             payload={"files": {"t": iid}})
        sign_item(it, FIRST_PARTY_PRIVATE_HEX)
        return it
    cat.publish(dependent("mkt.dep.top", "<1.0"))    # no version <1.0
    with pytest.raises(MarketplaceError):
        mgr.resolve("mkt.dep.top")


# --- plugin bridging + health + versioning + updates ---------------------

def test_plugin_backed_item_registers_plugin(cat, mgr):
    rep = mgr.install("mkt.connector.acme-warehouse")
    assert rep["plugin_ids"]
    pid = rep["plugin_ids"][0]
    assert mgr._plugins().get(pid) is not None
    assert mgr._plugins().get(pid).invoke("describe")["key"] == \
        "acme_warehouse"
    # uninstall removes the plugin
    mgr.uninstall("mkt.connector.acme-warehouse")
    assert mgr._plugins().get(pid) is None


def test_health_reports_content_and_plugins(mgr):
    mgr.install("mkt.template.medallion-lakehouse")
    mgr.install("mkt.connector.acme-warehouse")
    h = mgr.health()
    assert h["summary"].get("error", 0) == 0
    assert h["items"]["mkt.template.medallion-lakehouse"]["status"] == "ok"


def test_version_selection_and_updates(cat, mgr):
    # install an OLD version, then an update should be available
    mgr.install("mkt.connector.acme-warehouse", version="1.0.0")
    ups = {u["id"]: u for u in mgr.check_updates()}
    u = ups["mkt.connector.acme-warehouse"]
    assert u["installed"] == "1.0.0"
    assert u["latest"] == "1.2.0"
    assert u["update_available"] is True
    mgr.update("mkt.connector.acme-warehouse")
    assert mgr.is_installed("mkt.connector.acme-warehouse", "1.2.0")


def test_install_path_traversal_blocked(cat, mgr):
    it = MarketplaceItem(
        id="mkt.evil.traversal", type="business_rules", name="Evil",
        version="1.0", publisher="metabridge",
        payload={"files": {"../../escape.txt": "pwned"}})
    sign_item(it, FIRST_PARTY_PRIVATE_HEX)
    cat.publish(it)
    mgr.install("mkt.evil.traversal")        # sanitized, not an escape
    # the file must NOT exist outside the install root
    assert not (Path(mgr._root).parent.parent / "escape.txt").exists()


# --- API -----------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None)
             for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    yield TestClient(webapp.app)
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def test_marketplace_api(client):
    lst = client.get("/api/marketplace").json()
    assert len(lst["item_types"]) == 8
    assert len(lst["items"]) >= 8
    assert all(i["signed"] and i["signature_status"] == "verified"
               for i in lst["items"])

    # detail + versions
    g = client.get("/api/marketplace/mkt.connector.acme-warehouse").json()
    assert g["versions"] == ["1.0.0", "1.1.0", "1.2.0"]
    assert g["verification"]["verified"] is True

    # license gate via API
    r = client.post("/api/marketplace/install",
                    json={"item_id": "mkt.ai-skill.sql-explainer"})
    assert r.status_code == 422
    r2 = client.post("/api/marketplace/install",
                     json={"item_id": "mkt.ai-skill.sql-explainer",
                           "accept_license": True})
    assert r2.status_code == 200

    # dependency resolution via API
    r3 = client.post("/api/marketplace/install",
                     json={"item_id": "mkt.accelerator.healthcare-hipaa",
                           "accept_license": True})
    assert r3.status_code == 200
    assert len(r3.json()["installed"]) >= 3

    inst = client.get("/api/marketplace/installed").json()
    assert "mkt.accelerator.healthcare-hipaa" in inst["installed"]
    assert inst["health"]["summary"].get("error", 0) == 0

    # uninstall + keypair
    assert client.post("/api/marketplace/uninstall",
                       json={"item_id": "mkt.ai-skill.sql-explainer"}
                       ).status_code == 200
    kp = client.post("/api/marketplace/keypair").json()
    assert "private_key" in kp and "public_key" in kp


def test_marketplace_api_bad_input(client):
    assert client.post("/api/marketplace/install",
                       content=b"{bad",
                       headers={"Content-Type": "application/json"}
                       ).status_code == 422
    assert client.post("/api/marketplace/install",
                       json={}).status_code == 422
    assert client.get("/api/marketplace/nope.nope").status_code == 404
    assert client.get("/api/marketplace",
                      params={"type": "bogus"}).status_code == 422


# --- adversarial-review regressions --------------------------------------

def test_signature_binds_identity_and_metadata(cat):
    """The critical fix: the signature must cover id/type/version/publisher
    and the gate-driving metadata (license/compatibility/dependencies), not
    just the payload — else a signed payload can be relabelled onto any
    item and still verify (impersonation / downgrade / license bypass)."""
    base = cat.get("mkt.transform-lib.scd2-macros")
    assert verify_item(base, cat.trust)["verified"]
    for field, value in [("id", "mkt.connector.acme-warehouse"),
                         ("type", "connector"),
                         ("version", "99.0.0"),
                         ("publisher", "evilcorp"),
                         ("name", "Totally Different"),
                         ("license", {"type": "MIT",
                                      "requires_acceptance": False}),
                         ("compatibility", {"plugin_api": ">=0.0"}),
                         ("dependencies", [{"id": "mkt.x", "version": ">=1"}])]:
        it = copy.deepcopy(base)
        setattr(it, field, value)
        assert not verify_item(it, cat.trust)["verified"], \
            "mutating %s must break verification" % field


def test_tampered_license_cannot_bypass_gate(cat, mgr):
    """Stripping requires_acceptance off a signed commercial item must not
    let it install without acceptance — the tamper breaks the signature."""
    it = copy.deepcopy(cat.get("mkt.ai-skill.sql-explainer"))
    it.license = {"type": "MIT", "requires_acceptance": False}
    cat.publish(it)                                  # signature now stale
    assert not verify_item(it, cat.trust)["verified"]
    with pytest.raises(MarketplaceError):
        mgr.install("mkt.ai-skill.sql-explainer")    # refused: bad signature


def test_allow_unverified_does_not_bypass_forged_dependency(cat, mgr):
    """allow_unverified is scoped to the explicitly requested root — a
    forged/untrusted transitive dependency must still be refused."""
    atk = generate_keypair()
    dep = MarketplaceItem(id="mkt.e.evil", type="business_rules",
                          name="Evil", version="1.0", publisher="evilcorp",
                          payload={"files": {"r": "x"}})
    sign_item(dep, atk["private_key"])               # untrusted publisher
    cat.publish(dep)
    root = MarketplaceItem(id="mkt.e.root", type="pipeline_template",
                           name="Root", version="1.0", publisher="metabridge",
                           dependencies=[{"id": "mkt.e.evil",
                                          "version": ">=1.0"}],
                           payload={"files": {"t": "y"}})
    sign_item(root, FIRST_PARTY_PRIVATE_HEX)
    cat.publish(root)
    with pytest.raises(MarketplaceError):
        mgr.install("mkt.e.root", allow_unverified=True)
    assert not mgr.is_installed("mkt.e.evil")


def test_metabridge_version_gate_is_enforced(cat, mgr):
    """A declared MetaBridge-version requirement is a real gate now, not a
    dead one that installs regardless."""
    it = MarketplaceItem(id="mkt.mb.future", type="business_rules",
                         name="Future", version="1.0", publisher="metabridge",
                         compatibility={"metabridge": ">=999"},
                         payload={"files": {"x": "y"}})
    sign_item(it, FIRST_PARTY_PRIVATE_HEX)
    cat.publish(it)
    assert it.platform_compatible()["compatible"] is False
    with pytest.raises(MarketplaceError):
        mgr.install("mkt.mb.future")


def test_dotdot_item_id_cannot_escape_or_nuke(cat, mgr):
    """An item id of '..' must neither escape the install sandbox on install
    nor rmtree the parent tree on uninstall."""
    it = MarketplaceItem(id="..", type="business_rules", name="Dots",
                         version="1.0", publisher="metabridge",
                         payload={"files": {"x": "pwned"}})
    sign_item(it, FIRST_PARTY_PRIVATE_HEX)
    cat.publish(it)
    mgr.install("mkt.template.medallion-lakehouse")  # legit neighbour
    root = mgr._root
    with pytest.raises(MarketplaceError):
        mgr.install("..")                            # refused, no escape
    assert not (root.parent / "x").exists()          # nothing hit the parent
    assert mgr.uninstall("..") is False              # nothing to rmtree
    assert root.exists()
    assert mgr.is_installed("mkt.template.medallion-lakehouse")


def test_update_clears_stale_files(cat, mgr):
    """Re-materialize on update must clear the dir so a stale file from a
    previous version cannot persist (and, for plugins, be hot-loaded)."""
    def content(ver, files):
        it = MarketplaceItem(id="mkt.stale.lib", type="transformation_library",
                             name="Stale", version=ver, publisher="metabridge",
                             versions=[ver],
                             payload={"plugin": False, "files": files})
        sign_item(it, FIRST_PARTY_PRIVATE_HEX)
        return it
    cat.publish(content("1.0.0", {"a.txt": "A", "stale.txt": "B"}))
    cat.publish(content("2.0.0", {"a.txt": "A2"}))   # stale.txt dropped
    mgr.install("mkt.stale.lib", version="1.0.0")
    d = mgr._install_dir("mkt.stale.lib")
    assert (d / "stale.txt").exists()
    mgr.update("mkt.stale.lib")
    assert (d / "a.txt").exists()
    assert not (d / "stale.txt").exists()            # gone after re-materialize


def test_no_spurious_update_when_at_latest(mgr):
    mgr.install("mkt.template.medallion-lakehouse")
    ups = {u["id"]: u for u in mgr.check_updates()}
    assert ups["mkt.template.medallion-lakehouse"]["update_available"] is False


def test_ver_key_zero_pads():
    from metabridge.marketplace.catalog import _ver_key
    assert _ver_key("1.2") == _ver_key("1.2.0")
    assert _ver_key("1.2.0") > _ver_key("1.1.9")


def test_install_manager_rebinds_on_data_dir_change(tmp_path, monkeypatch):
    """get_install_manager must rebind by path equality, not substring — no
    cross-tenant state leak when one data dir is a substring of another."""
    import metabridge.marketplace.install as inst
    inst._MANAGER = None
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "tenant_alpha"))
    a = inst.get_install_manager()
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path / "tenant"))
    b = inst.get_install_manager()
    assert a is not b
    assert a._root != b._root

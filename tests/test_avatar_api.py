"""Profile photo / avatar management: upload validation, normalization,
storage lifecycle, preset selection, fallback, and access control."""
import io
import os
import sys

import pytest

_DATA = None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    global _DATA
    monkeypatch.setenv("METABRIDGE_DATA_DIR", str(tmp_path))
    saved = {m: sys.modules.pop(m, None) for m in ("web.app", "web.auth", "web")}
    from fastapi.testclient import TestClient
    import web.app as webapp
    _DATA = tmp_path
    c = TestClient(webapp.app)
    r = c.post("/auth/signup", json={
        "email": "owner@example.com", "password": "password123",
        "name": "Sachin Shrinivas Mane", "company": "Metafor"})
    assert r.status_code == 200
    yield c
    # restore whatever other test modules had imported — the tmp_path-bound
    # module instance must not leak past this test
    for m, orig in saved.items():
        if orig is not None:
            sys.modules[m] = orig
        else:
            sys.modules.pop(m, None)


def _png(size=(64, 64), color=(200, 30, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _upload(client, data, mime="image/png", name="photo.png"):
    return client.post("/api/v1/me/avatar",
                       files={"file": (name, io.BytesIO(data), mime)})


# -- fallback / model -------------------------------------------------------

def test_me_defaults_to_initials_avatar(client):
    me = client.get("/api/v1/me").json()["user"]
    assert me["avatar"] == {"type": "INITIALS"}


# -- upload ------------------------------------------------------------------

def test_upload_photo_normalizes_and_serves(client):
    from PIL import Image
    r = _upload(client, _png(size=(900, 640)))
    assert r.status_code == 200
    av = r.json()["user"]["avatar"]
    assert av["type"] == "PHOTO"
    assert av["url"].startswith("/api/v1/users/owner%40example.com/avatar?v=")

    got = client.get(av["url"])
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/webp"
    img = Image.open(io.BytesIO(got.content))
    assert img.format == "WEBP"
    assert img.size == (512, 512)          # square, capped at 512
    assert not img.info.get("exif")        # metadata not carried over

    # small images stay their own size but still square
    r2 = _upload(client, _png(size=(100, 80)))
    img2 = Image.open(io.BytesIO(client.get(
        r2.json()["user"]["avatar"]["url"]).content))
    assert img2.size == (80, 80)


def test_upload_replaces_in_place_single_file(client):
    _upload(client, _png(color=(10, 10, 200)))
    _upload(client, _png(color=(10, 200, 10)))
    files = list((_DATA / "avatars").iterdir())
    assert len(files) == 1                  # replaced, not accumulated
    assert files[0].suffix == ".webp"       # server-generated name


def test_upload_rejects_oversize(client):
    r = _upload(client, b"\x89PNG" + b"0" * (5 * 1024 * 1024 + 1))
    assert r.status_code == 413


def test_upload_rejects_non_image_content(client):
    # correct MIME + extension, garbage bytes: decode gate must catch it
    r = _upload(client, b"not an image at all")
    assert r.status_code == 415


def test_upload_rejects_svg_and_bad_mime(client):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    assert _upload(client, svg, mime="image/svg+xml",
                   name="a.svg").status_code == 415
    # SVG smuggled with a permitted MIME still fails the decode gate
    assert _upload(client, svg, mime="image/png",
                   name="a.png").status_code == 415


def test_upload_ignores_original_filename(client):
    r = _upload(client, _png(), name="../../../evil.png")
    assert r.status_code == 200
    files = [p.name for p in (_DATA / "avatars").iterdir()]
    assert files and "evil" not in files[0] and ".." not in files[0]


# -- presets / initials ------------------------------------------------------

def test_preset_selection_and_validation(client):
    r = client.put("/api/v1/me/avatar", json={"type": "PRESET",
                                              "preset": "mb-3"})
    assert r.status_code == 200
    assert r.json()["user"]["avatar"] == {"type": "PRESET", "preset": "mb-3"}
    assert client.put("/api/v1/me/avatar",
                      json={"type": "PRESET",
                            "preset": "../etc"}).status_code == 422
    assert client.put("/api/v1/me/avatar",
                      json={"type": "WHATEVER"}).status_code == 422


def test_presets_catalog_and_assets_exist(client):
    from pathlib import Path
    presets = client.get("/api/v1/avatars/presets").json()["presets"]
    assert len(presets) == 6
    static = Path(__file__).resolve().parents[1] / "web" / "static"
    for p in presets:
        assert (static / "avatars" / (p["id"] + ".svg")).exists()


def test_switching_to_preset_deletes_photo_file(client):
    _upload(client, _png())
    assert list((_DATA / "avatars").iterdir())
    client.put("/api/v1/me/avatar", json={"type": "PRESET", "preset": "mb-1"})
    assert not list((_DATA / "avatars").iterdir())


# -- removal -----------------------------------------------------------------

def test_remove_photo_falls_back_to_initials(client):
    _upload(client, _png())
    r = client.delete("/api/v1/me/avatar")
    assert r.status_code == 200
    assert r.json()["user"]["avatar"] == {"type": "INITIALS"}
    assert not list((_DATA / "avatars").iterdir())
    me = client.get("/api/v1/me").json()["user"]
    assert me["avatar"]["type"] == "INITIALS"
    assert client.get(
        "/api/v1/users/owner%40example.com/avatar").status_code == 404


# -- access control -----------------------------------------------------------

def test_avatar_requires_authentication(client):
    _upload(client, _png())
    from fastapi.testclient import TestClient
    import web.app as webapp
    anon = TestClient(webapp.app)
    assert anon.get(
        "/api/v1/users/owner%40example.com/avatar").status_code == 401
    assert anon.post("/api/v1/me/avatar",
                     files={"file": ("p.png", io.BytesIO(_png()),
                                     "image/png")}).status_code == 401
    assert anon.delete("/api/v1/me/avatar").status_code == 401


def test_viewer_can_manage_own_photo_and_see_others(client):
    from fastapi.testclient import TestClient
    import web.app as webapp
    _upload(client, _png())               # owner sets a photo
    client.post("/api/users", json={"email": "viewer@example.com",
                                    "password": "password123",
                                    "name": "Audit Viewer",
                                    "role": "viewer"})
    v = TestClient(webapp.app)
    assert v.post("/auth/login", json={"email": "viewer@example.com",
                                       "password": "password123"}).status_code == 200
    # viewers (jobs:read only) may still manage their own profile photo
    r = v.post("/api/v1/me/avatar",
               files={"file": ("p.png", io.BytesIO(_png()), "image/png")})
    assert r.status_code == 200
    # ... and view a teammate's photo
    assert v.get(
        "/api/v1/users/owner%40example.com/avatar").status_code == 200

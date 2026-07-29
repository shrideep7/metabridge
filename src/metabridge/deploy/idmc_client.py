"""IDMC (Informatica Cloud) REST deployment client.

Implements the IDMC v3 REST flow with no third-party dependencies:

  1. POST {login_url}/saas/public/core/v3/login          -> session + base API URL
  2. POST {base}/public/core/v3/import/package           -> upload bundle zip
  3. POST {base}/public/core/v3/import                   -> start import job
  4. GET  {base}/public/core/v3/import/{jobId}           -> poll until terminal

`dry_run=True` performs everything local (packages the bundle, prints the plan)
without any network calls — the default in the CLI so nobody deploys to a
customer org by accident.

The client is deliberately transport-thin: `_request` is a single seam, so
tests (and air-gapped environments) can stub it.
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_LOGIN_URL = "https://dm-us.informaticacloud.com"


class IDMCError(Exception):
    pass


@dataclass
class DeployResult:
    ok: bool
    dry_run: bool
    package: str = ""
    objects: List[str] = field(default_factory=list)
    job_id: str = ""
    job_state: str = ""
    messages: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "dry_run": self.dry_run, "package": self.package,
                "objects": self.objects, "job_id": self.job_id,
                "job_state": self.job_state, "messages": self.messages}


def package_bundle(bundle_dir: str, out_zip: str = "") -> str:
    """Zip a MetaBridge AI IDMC bundle directory for import; returns the zip path."""
    src = Path(bundle_dir)
    manifest = src / "manifest.json"
    if not manifest.exists():
        raise IDMCError("Not a MetaBridge AI IDMC bundle (missing manifest.json): %s"
                        % bundle_dir)
    dest = Path(out_zip) if out_zip else src.with_suffix(".zip")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src))
    return str(dest)


class IDMCClient:
    def __init__(self, username: str = "", password: str = "",
                 login_url: str = DEFAULT_LOGIN_URL, timeout: int = 60):
        self.username = username
        self.password = password
        self.login_url = login_url.rstrip("/")
        self.timeout = timeout
        self.session_id: str = ""
        self.base_url: str = ""

    # -- transport seam (stubbed in tests) ---------------------------------
    def _request(self, method: str, url: str, headers: Dict[str, str],
                 body: Optional[bytes]) -> dict:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise IDMCError("HTTP %d from %s: %s" % (e.code, url, detail))
        except urllib.error.URLError as e:
            raise IDMCError("Cannot reach %s: %s" % (url, e.reason))

    # -- API steps ----------------------------------------------------------
    def login(self) -> None:
        if not self.username or not self.password:
            raise IDMCError("IDMC username/password required (or use --dry-run)")
        doc = self._request(
            "POST", self.login_url + "/saas/public/core/v3/login",
            {"Content-Type": "application/json"},
            json.dumps({"username": self.username,
                        "password": self.password}).encode())
        self.session_id = str(doc.get("userInfo", {}).get("sessionId", ""))
        products = doc.get("products", []) or []
        for prod in products:
            if prod.get("name") == "Integration Cloud":
                self.base_url = str(prod.get("baseApiUrl", "")).rstrip("/")
        if not self.session_id or not self.base_url:
            raise IDMCError("Login succeeded but session/base URL missing — "
                            "check org entitlements")

    def _auth_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"INFA-SESSION-ID": self.session_id}
        h.update(extra or {})
        return h

    def upload_package(self, zip_path: str) -> str:
        data = Path(zip_path).read_bytes()
        boundary = "----metabridge" + uuid.uuid4().hex
        buf = io.BytesIO()
        buf.write(("--%s\r\nContent-Disposition: form-data; name=\"package\"; "
                   "filename=\"%s\"\r\nContent-Type: application/zip\r\n\r\n"
                   % (boundary, Path(zip_path).name)).encode())
        buf.write(data)
        buf.write(("\r\n--%s--\r\n" % boundary).encode())
        doc = self._request(
            "POST", self.base_url + "/public/core/v3/import/package",
            self._auth_headers({"Content-Type":
                                "multipart/form-data; boundary=%s" % boundary}),
            buf.getvalue())
        job_id = str(doc.get("jobId", "") or doc.get("id", ""))
        if not job_id:
            raise IDMCError("Package upload returned no job id: %s" % doc)
        return job_id

    def start_import(self, job_id: str, name: str) -> None:
        self._request(
            "POST", self.base_url + "/public/core/v3/import/%s" % job_id,
            self._auth_headers({"Content-Type": "application/json"}),
            json.dumps({"name": name,
                        "importSpecification": {
                            "defaultConflictResolution": "OVERWRITE"}}).encode())

    def poll_import(self, job_id: str, interval: float = 3.0,
                    max_wait: float = 600.0) -> str:
        waited = 0.0
        while True:
            doc = self._request(
                "GET", self.base_url + "/public/core/v3/import/%s" % job_id,
                self._auth_headers(), None)
            state = str(doc.get("status", {}).get("state", "")
                        or doc.get("state", "UNKNOWN"))
            if state in ("SUCCESSFUL", "FAILED", "WARNING"):
                return state
            if waited >= max_wait:
                raise IDMCError("Import job %s still %s after %.0fs"
                                % (job_id, state, max_wait))
            time.sleep(interval)
            waited += interval

    def logout(self) -> None:
        if self.session_id:
            try:
                self._request("POST",
                              self.login_url + "/saas/public/core/v3/logout",
                              self._auth_headers(), None)
            except IDMCError:
                pass  # best effort


def deploy_bundle(bundle_dir: str, username: str = "", password: str = "",
                  login_url: str = DEFAULT_LOGIN_URL,
                  dry_run: bool = True) -> DeployResult:
    manifest = json.loads((Path(bundle_dir) / "manifest.json").read_text())
    objects = ["%s (%s)" % (o["name"], o["type"]) for o in manifest.get("objects", [])]
    zip_path = package_bundle(bundle_dir)
    result = DeployResult(ok=True, dry_run=dry_run, package=zip_path, objects=objects)

    if dry_run:
        result.messages.append(
            "DRY RUN — packaged %d objects; would login to %s, upload the "
            "package, start an OVERWRITE import, and poll to completion."
            % (len(objects), login_url))
        return result

    client = IDMCClient(username, password, login_url)
    client.login()
    result.messages.append("Logged in; org API base: %s" % client.base_url)
    try:
        job_id = client.upload_package(zip_path)
        result.job_id = job_id
        result.messages.append("Package uploaded; import job %s" % job_id)
        client.start_import(job_id, "metabridge_" + Path(bundle_dir).name)
        state = client.poll_import(job_id)
        result.job_state = state
        result.ok = state in ("SUCCESSFUL", "WARNING")
        result.messages.append("Import finished: %s" % state)
    finally:
        client.logout()
    return result

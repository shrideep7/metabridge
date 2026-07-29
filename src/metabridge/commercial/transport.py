"""Connected-mode transport (EB-501 connected half + EB-504 sink).

The instance-side HTTP client for the control plane's instance API. It:

- **enrolls** with a one-time token and persists the returned credential
  (0600) under the data dir, so the instance authenticates itself thereafter;
- **polls entitlements** with a local TTL cache + offline grace — a brief
  control-plane outage serves the last-good snapshot; beyond the grace window
  it fails closed (``ControlPlaneUnavailable``), and the bridge then denies
  *expansion* only;
- **drains the usage spool** to ``/instance/usage/batch`` as a
  ``UsageReporter`` sink (raising on non-2xx so the reporter retries; the
  server dedups on idempotency key, so re-sends are exactly-once).

The HTTP mechanism is injectable (``http=``) so this is testable against the
real control-plane app via a TestClient adapter, and uses only ``urllib`` by
default — no new dependency.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

from ..platform._util import atomic_write_json
from .bridge import CommercialBridge
from .entitlements import EntitlementSet
from .usage import UsageReporter, UsageSpool


class TransportError(Exception):
    """A control-plane call returned a non-success status."""

    def __init__(self, status: int, body) -> None:
        super().__init__(f"control plane returned HTTP {status}: {body}")
        self.status = status
        self.body = body


class ControlPlaneUnavailable(Exception):
    """The control plane is unreachable and no in-grace cached data exists."""


class _UrllibHttp:
    """Default JSON-over-HTTP adapter."""

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    def request(self, method: str, path: str, headers: dict,
                body: Optional[dict] = None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self._base + path, data=data,
                                     headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode() or "{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as e:              # 4xx/5xx with a body
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError) as e:    # connection failure
            raise ControlPlaneUnavailable(str(e))


class ControlPlaneClient:
    def __init__(self, base_url: str = "", *, http=None,
                 data_dir: Optional[str] = None) -> None:
        self._http = http or _UrllibHttp(base_url)
        self._dir = Path(data_dir or os.environ.get("METABRIDGE_DATA_DIR", ".")) \
            / "commercial"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cfg_path = self._dir / "enrollment.json"
        self._ent_cache = self._dir / "entitlements_cache.json"

    # -- config ---------------------------------------------------------------
    def _config(self) -> dict:
        if self._cfg_path.exists():
            try:
                return json.loads(self._cfg_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return {}
        return {}

    @property
    def enrolled(self) -> bool:
        return bool(self._config().get("credential"))

    def _auth(self) -> dict:
        cred = self._config().get("credential")
        if not cred:
            raise ControlPlaneUnavailable("instance is not enrolled")
        return {"X-Instance-Key": cred}

    # -- enrollment -----------------------------------------------------------
    def enroll(self, token: str, *, public_key: Optional[str] = None,
               name: str = "", fingerprint: Optional[str] = None) -> dict:
        status, body = self._http.request(
            "POST", "/instances/enroll", {},
            {"token": token, "public_key": public_key, "name": name,
             "fingerprint": fingerprint})
        if status != 200:
            raise TransportError(status, body)
        atomic_write_json(self._cfg_path, {
            "instance_id": body["instance_id"], "credential": body["credential"],
            "tenant_id": body.get("tenant_id"),
            "subscription_id": body.get("subscription_id")})
        try:
            os.chmod(self._cfg_path, 0o600)
        except OSError:                                   # pragma: no cover
            pass
        return {k: v for k, v in body.items() if k != "credential"}

    # -- entitlements (TTL cache + offline grace) -----------------------------
    def fetch_entitlements(self, *, max_age_s: float = 300.0,
                           grace_s: float = 86400.0,
                           now: Optional[float] = None) -> list:
        """Return current entitlements. Serves a fresh cache within ``max_age_s``
        without a call; on a control-plane outage serves a cached snapshot up to
        ``grace_s`` old; beyond that, fails closed (ControlPlaneUnavailable)."""
        now = time.time() if now is None else now
        cached = self._read_ent_cache()
        if cached and (now - cached["fetched_at"]) <= max_age_s:
            return cached["entitlements"]
        try:
            status, body = self._http.request(
                "GET", "/instance/entitlements", self._auth())
        except ControlPlaneUnavailable:
            if cached and (now - cached["fetched_at"]) <= grace_s:
                return cached["entitlements"]
            raise
        if status != 200:
            if cached and (now - cached["fetched_at"]) <= grace_s:
                return cached["entitlements"]
            raise TransportError(status, body)
        ents = body.get("entitlements", [])
        atomic_write_json(self._ent_cache,
                          {"fetched_at": now, "entitlements": ents})
        return ents

    def _read_ent_cache(self) -> Optional[dict]:
        if self._ent_cache.exists():
            try:
                doc = json.loads(self._ent_cache.read_text(encoding="utf-8"))
                if isinstance(doc, dict) and "fetched_at" in doc:
                    return doc
            except (ValueError, OSError):
                return None
        return None

    # -- usage sink -----------------------------------------------------------
    def push_usage(self, events: List[dict]) -> dict:
        """Sink for ``UsageReporter``: POST a batch; raise on non-2xx so the
        reporter keeps the events spooled and retries."""
        status, body = self._http.request(
            "POST", "/instance/usage/batch", self._auth(), {"events": events})
        if not (200 <= status < 300):
            raise TransportError(status, body)
        return body

    def heartbeat(self) -> dict:
        status, body = self._http.request("GET", "/instance/heartbeat",
                                          self._auth())
        if status != 200:
            raise TransportError(status, body)
        return body


def connected_bridge(client: ControlPlaneClient, flags=None, **kw) -> CommercialBridge:
    """Build a bridge whose entitlements come from the control plane (cached)."""
    cfg = client._config()
    ents = EntitlementSet(client.fetch_entitlements(**kw))
    return CommercialBridge(ents, flags, subject=cfg.get("instance_id", ""))


def drain_usage(client: ControlPlaneClient, spool: UsageSpool, **kw) -> dict:
    """Drain a usage spool to the control plane, with retry/backoff."""
    return UsageReporter(spool, client.push_usage).flush(**kw)

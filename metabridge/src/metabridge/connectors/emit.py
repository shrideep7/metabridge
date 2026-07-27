"""Emit platform-specific connection artifacts from a connector spec + params.

One set of connection parameters, three deployment targets:
  * dbt profiles.yml entry
  * IDMC connection JSON (v3 object shape)
  * PowerCenter relational connection stub (for pmrep createconnection)

Secrets are never written to artifacts — they are emitted as environment-variable
references ({{ env_var('...') }} / $SECRET$ placeholders), which is also what
SOC2/ISO reviewers expect to see.
"""
from __future__ import annotations

from typing import Dict, Tuple

import yaml

from .base import ConnectorSpec


def _envvar(spec: ConnectorSpec, fname: str) -> str:
    return "MB_%s_%s" % (spec.key.upper(), fname.upper())


def split_secrets(spec: ConnectorSpec, params: Dict[str, str]) -> Tuple[dict, dict]:
    """Returns (safe_params, secret_envvars). Secret values never leave here."""
    safe, secrets = {}, {}
    secret_fields = {f.name for f in spec.fields if f.secret}
    for f in spec.fields:
        val = params.get(f.name, f.default)
        if f.name in secret_fields:
            secrets[_envvar(spec, f.name)] = val or ""
        else:
            safe[f.name] = val or ""
    return safe, secrets


def dbt_profile(spec: ConnectorSpec, params: Dict[str, str],
                profile_name: str) -> str:
    """profiles.yml snippet with env_var() references for secrets."""
    if not spec.dbt_adapter:
        raise ValueError("Connector %s has no dbt adapter — use it as a source "
                         "system, not a dbt target." % spec.key)
    safe, secrets = split_secrets(spec, params)
    output = {"type": spec.dbt_adapter, **{k: v for k, v in safe.items() if v}}
    for env in secrets:
        fname = env.split("_")[-1].lower()
        output[fname] = "{{ env_var('%s') }}" % env
    doc = {profile_name: {"target": "prod", "outputs": {"prod": output}}}
    return yaml.safe_dump(doc, sort_keys=False)


def idmc_connection(spec: ConnectorSpec, params: Dict[str, str],
                    conn_name: str) -> dict:
    safe, secrets = split_secrets(spec, params)
    props = [{"name": k, "value": v} for k, v in safe.items() if v]
    props += [{"name": env.split("_")[-1].lower(),
               "value": "$" + env + "$", "secret": True} for env in secrets]
    return {"@type": "connection", "name": conn_name,
            "connectionType": spec.idmc_type or spec.name,
            "deployment": spec.deployment,
            "properties": props}


def powercenter_connection(spec: ConnectorSpec, params: Dict[str, str],
                           conn_name: str) -> str:
    """pmrep command stub — the scripted way PC admins actually create these."""
    safe, _ = split_secrets(spec, params)
    host = safe.get("host", safe.get("account", safe.get("ashost", "HOST")))
    db = safe.get("database", safe.get("dataset", safe.get("client", "DB")))
    user = safe.get("user", "USER")
    return ("pmrep createconnection -s relational -t \"%s\" -n \"%s\" "
            "-u \"%s\" -p \"$%s$\" -c \"%s\" -d \"%s\""
            % (spec.powercenter_dbtype or "ODBC", conn_name, user,
               _envvar(spec, "password"), host, db))

"""Sample source-connector plugin implementation.

`build(manifest)` is what the registry calls after reading plugin.yml;
it returns a metabridge.plugins.Plugin whose capabilities are the two
declared in the manifest. This file is a copy-paste starting point for a
real connector.
"""
from metabridge.plugins import Plugin


def _describe():
    """Static metadata a marketplace would list for this connector."""
    return {
        "key": "acme_warehouse",
        "name": "ACME Warehouse",
        "category": "cloud_warehouse",
        "auth": ["host", "user", "password", "warehouse"],
        "regions": ["us", "eu"],
    }


def _introspect(params):
    """Given connection params, return a table/column manifest.

    A real connector would open a live connection; the sample returns a
    fixed, deterministic manifest so the contract is demonstrable
    without network access.
    """
    return {
        "ok": True,
        "database": params.get("database", "ACME_DB"),
        "tables": [
            {"name": "orders", "schema": "PUBLIC",
             "columns": [{"name": "order_id", "type": "integer"},
                         {"name": "amount", "type": "decimal"}]},
            {"name": "customers", "schema": "PUBLIC",
             "columns": [{"name": "customer_id", "type": "integer"},
                         {"name": "email", "type": "varchar"}]},
        ],
    }


def build(manifest):
    return Plugin(manifest, {"describe": _describe,
                             "introspect": _introspect},
                  health_fn=lambda: {"status": "ok",
                                     "detail": "sample connector ready"})

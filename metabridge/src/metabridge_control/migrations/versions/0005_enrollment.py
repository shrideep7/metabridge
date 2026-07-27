"""0005 — Phase 5 instance enrollment.

Creates the ``instances`` and ``instance_enrollment_tokens`` tables — the trust
anchor between a data-plane deployment and the control plane
(docs/commercialization/03-domain-model.md §3.1). An instance's credential is
how its tenant is derived on every instance-authenticated request; its public
key is the trust anchor for verifying its air-gapped signed usage statements.
"""
from __future__ import annotations

from ... import schema

_TABLES = [
    schema.instances,
    schema.instance_enrollment_tokens,
]


def up(conn) -> None:
    schema.metadata.create_all(conn, tables=_TABLES)


def down(conn) -> None:
    schema.metadata.drop_all(conn, tables=list(reversed(_TABLES)))

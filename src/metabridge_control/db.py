"""Control-plane database access.

SQLite for development and tests (zero-install), PostgreSQL (Amazon RDS) in
production via ``CONTROLPLANE_DATABASE_URL``. All commercial operations run
inside explicit transactions (``engine.begin()``); SQLAlchemy Core only — no
ORM session state.

The control plane deliberately does NOT reuse the product's file-backed store:
commercial and billing-critical records need real transactions
(Phase 0: docs/commercialization/02-target-architecture.md).
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

_DEF_ENV = "CONTROLPLANE_DATABASE_URL"


def default_database_url() -> str:
    """SQLite file under the instance data dir (dev fallback only)."""
    base = Path(os.environ.get("METABRIDGE_DATA_DIR", str(Path.home() / ".metabridge")))
    base.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + str(base / "controlplane.db")


def get_engine(url: str | None = None) -> Engine:
    """Create an engine for the control-plane database.

    ``sqlite://`` (in-memory) engines use a StaticPool so every connection in
    a test shares one database.
    """
    url = url or os.environ.get(_DEF_ENV) or default_database_url()
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        # pysqlite autocommits DDL and mismanages transaction scope by
        # default, which would make migrations non-atomic and SAVEPOINTs
        # unreliable. Apply the documented SQLAlchemy recipe: disable
        # pysqlite's implicit BEGIN and emit BEGIN ourselves, so
        # engine.begin()/begin_nested() actually control the transaction.
        @event.listens_for(engine, "connect")
        def _sqlite_setup(dbapi_conn, _record):  # pragma: no cover - driver glue
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")   # wait, don't fail, on lock
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # pragma: no cover - driver glue
            # IMMEDIATE takes the write lock up front, so read-modify-write
            # transactions (reservations, quota) serialize correctly on the
            # single-writer engine — no oversell. PostgreSQL uses FOR UPDATE.
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    return engine

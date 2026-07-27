"""Minimal, explicit migration runner for the control-plane schema.

Numbered migration modules live in ``versions/`` and each exposes
``up(conn)`` and ``down(conn)``. Applied versions are recorded in
``schema_migrations``; each migration runs inside one transaction, so a
failed migration leaves the database untouched.

Usage:
    from metabridge_control import db
    from metabridge_control.migrations import runner
    engine = db.get_engine()
    runner.migrate(engine)            # apply all pending
    runner.rollback(engine, to=0)     # walk back down to version 0
    runner.status(engine)             # [(version, name, applied?)]
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from typing import List, Tuple

from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table,
                        select)
from sqlalchemy.engine import Engine

from .. import schema as cp_schema
from ..errors import MigrationError

_meta = MetaData()
schema_migrations = Table(
    "schema_migrations", _meta,
    Column("version", Integer, primary_key=True),
    Column("name", String(128), nullable=False),
    Column("applied_at", DateTime, nullable=False, default=cp_schema.utcnow),
)

_VERSION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)$")


def _discover() -> List[Tuple[int, str, object]]:
    from . import versions as versions_pkg
    found = []
    for mod_info in pkgutil.iter_modules(versions_pkg.__path__):
        m = _VERSION_RE.match(mod_info.name)
        if not m:
            continue
        module = importlib.import_module(
            f"{versions_pkg.__name__}.{mod_info.name}")
        if not callable(getattr(module, "up", None)) or \
                not callable(getattr(module, "down", None)):
            raise MigrationError(f"migration {mod_info.name} must define "
                                 "up(conn) and down(conn)")
        found.append((int(m.group(1)), mod_info.name, module))
    found.sort(key=lambda x: x[0])
    return found


def _applied(engine: Engine) -> dict:
    _meta.create_all(engine, tables=[schema_migrations])
    with engine.connect() as conn:
        rows = conn.execute(select(schema_migrations)).mappings().all()
    return {r["version"]: r["name"] for r in rows}


def status(engine: Engine) -> List[Tuple[int, str, bool]]:
    applied = _applied(engine)
    return [(v, name, v in applied) for v, name, _m in _discover()]


def migrate(engine: Engine, to: int | None = None) -> List[int]:
    """Apply all pending migrations (optionally up to ``to``); returns the
    versions applied."""
    applied = _applied(engine)
    done = []
    for version, name, module in _discover():
        if version in applied or (to is not None and version > to):
            continue
        with engine.begin() as conn:
            module.up(conn)
            conn.execute(schema_migrations.insert().values(
                version=version, name=name, applied_at=cp_schema.utcnow()))
        done.append(version)
    return done


def rollback(engine: Engine, to: int) -> List[int]:
    """Roll back applied migrations with version > ``to`` (highest first);
    returns the versions rolled back."""
    applied = _applied(engine)
    undone = []
    for version, name, module in sorted(_discover(), reverse=True):
        if version not in applied or version <= to:
            continue
        with engine.begin() as conn:
            module.down(conn)
            conn.execute(schema_migrations.delete().where(
                schema_migrations.c.version == version))
        undone.append(version)
    return undone

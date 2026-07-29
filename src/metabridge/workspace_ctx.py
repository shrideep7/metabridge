"""Active-workspace data directory — the isolation primitive.

MetaBridge is multi-workspace: identity (accounts, sessions) is account-level
and global, but a workspace's RESOURCES — connections, jobs, the digital
twin, settings and its notification feed — are isolated on disk under that
workspace's own directory.

The active workspace is request-scoped: the web layer resolves it from the
session and sets it here via a ``contextvars.ContextVar`` (which propagates to
the request's async task and any threads it spawns for that request). Stores
that hold workspace state call :func:`active_data_dir` instead of reading
``METABRIDGE_DATA_DIR`` directly, so a single choke point scopes them all.

The FIRST/legacy workspace maps to the deployment's ROOT data dir (so an
existing single-workspace install keeps its data exactly where it is, with no
migration/move); every additional workspace lives under ``<root>/ws/<id>``.
When no workspace is active (API-key automation, CLI, tests) the root dir is
used — identical to the pre-multi-workspace behavior.
"""
from __future__ import annotations

import contextvars
import os
from pathlib import Path
from typing import Optional

_active_dir: contextvars.ContextVar = contextvars.ContextVar(
    "mb_active_ws_dir", default=None)


def root_data_dir() -> Path:
    return Path(os.environ.get("METABRIDGE_DATA_DIR",
                               str(Path.home() / ".metabridge"))).expanduser()


def set_active_data_dir(path: Optional[str]) -> contextvars.Token:
    """Point workspace-scoped stores at ``path`` for the current context.
    Returns a token; pass it to :func:`reset_active_data_dir` to restore."""
    return _active_dir.set(str(path) if path else None)


def reset_active_data_dir(token) -> None:
    try:
        _active_dir.reset(token)
    except (ValueError, LookupError):        # token from another context
        pass


def active_data_dir() -> Path:
    """The data dir for the active workspace, or the root when none is set."""
    d = _active_dir.get()
    return Path(d) if d else root_data_dir()

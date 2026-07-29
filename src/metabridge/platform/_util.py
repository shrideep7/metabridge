"""Shared IO helpers for the file-backed platform services: a best-effort
inter-process lock and an atomic JSON write that never shares a temp file
name between concurrent writers.
"""
from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path


@contextlib.contextmanager
def file_lock(lock_path: Path):
    """Serialize a read-modify-write across processes (best-effort; a no-op
    on platforms without flock)."""
    fh = open(lock_path, "w")
    try:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        fh.close()


def atomic_write_json(path: Path, obj) -> None:
    """Write JSON atomically via a PER-WRITER temp file + os.replace, so two
    concurrent writers can never consume each other's staging file."""
    tmp = path.with_name("%s.%d.%s.tmp" % (path.name, os.getpid(),
                                           uuid.uuid4().hex))
    try:
        tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

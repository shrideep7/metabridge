#!/usr/bin/env python
"""Syntax-check the JavaScript inlined in web/templates/console.html.

    python tools/check_console_js.py

console.html is one ~9,800-line document with ~400 KB of JS inlined in a
single <script>. A syntax error anywhere in it does not break one screen — it
kills the whole console, every page, because the browser discards the entire
block. The repo has no JS test runner and no build step, so nothing else
catches that before a human loads the page.

Requires node on PATH (used only for `node --check`). Exits non-zero on a
syntax error, so it can gate a commit.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CONSOLE = Path(__file__).resolve().parent.parent / "web" / "templates" / \
    "console.html"


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("node not found on PATH — cannot syntax-check", file=sys.stderr)
        return 2
    html = CONSOLE.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
    if not blocks:
        print("no inline <script> found in %s" % CONSOLE, file=sys.stderr)
        return 2
    # Joined with a separator so a stray trailing expression in one block
    # cannot silently swallow the next one.
    source = "\n;\n".join(blocks)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(source)
        tmp = fh.name
    try:
        res = subprocess.run([node, "--check", tmp], capture_output=True,
                             text=True)
    finally:
        Path(tmp).unlink(missing_ok=True)
    if res.returncode:
        print(res.stderr.strip() or res.stdout.strip(), file=sys.stderr)
        print("\nSYNTAX ERROR in console.html inline script — the whole "
              "console would fail to load.", file=sys.stderr)
        return 1
    print("console.html: %d script block(s), %d bytes — syntax OK"
          % (len(blocks), len(source)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

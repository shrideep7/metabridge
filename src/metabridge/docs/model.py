"""Canonical document model.

ONE structured representation every generated document is built as, and
that every renderer (Markdown / HTML / PDF / Word) consumes. Generators
never touch a specific output format, and renderers never know which
document they are rendering — so N documents x M formats stays N + M
code, not N x M.

A Doc is a title + metadata + an ordered list of typed blocks:

    heading   {level 1-3, text}
    para      {text}
    bullets   {items: [str]}
    table     {headers: [str], rows: [[cell]]}   (rows padded to width)
    kv        {pairs: [(key, value)]}             definition list
    code      {text, lang}
    note      {text}                              callout / disclaimer
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Doc:
    title: str
    subtitle: str = ""
    meta: dict = field(default_factory=dict)
    blocks: List[dict] = field(default_factory=list)

    # -- fluent builders (each returns self) --------------------------------
    def h(self, text: str, level: int = 1) -> "Doc":
        self.blocks.append({"type": "heading",
                            "level": max(1, min(3, int(level))),
                            "text": str(text)})
        return self

    def p(self, text: str) -> "Doc":
        self.blocks.append({"type": "para", "text": str(text)})
        return self

    def bullets(self, items) -> "Doc":
        items = [str(i) for i in items if str(i).strip()]
        if items:
            self.blocks.append({"type": "bullets", "items": items})
        return self

    def table(self, headers, rows) -> "Doc":
        headers = [str(h) for h in headers]
        w = len(headers)
        norm = []
        for r in rows:
            cells = [str(c) for c in r][:w]
            cells += [""] * (w - len(cells))     # pad ragged rows
            norm.append(cells)
        self.blocks.append({"type": "table", "headers": headers,
                            "rows": norm})
        return self

    def kv(self, pairs) -> "Doc":
        pairs = [(str(k), str(v)) for k, v in pairs]
        if pairs:
            self.blocks.append({"type": "kv", "pairs": pairs})
        return self

    def code(self, text: str, lang: str = "") -> "Doc":
        self.blocks.append({"type": "code", "text": str(text),
                            "lang": str(lang)})
        return self

    def note(self, text: str) -> "Doc":
        self.blocks.append({"type": "note", "text": str(text)})
        return self

    def to_dict(self) -> dict:
        return {"title": self.title, "subtitle": self.subtitle,
                "meta": self.meta, "blocks": self.blocks}

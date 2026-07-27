"""Doc renderers — Markdown, HTML, PDF, Word.

Each renderer walks a Doc's blocks and knows nothing about which
document it is. All user/estate-derived text is escaped for the target
format (HTML/Markdown), so a table cell or heading carrying a stray
``|``, ``<`` or ``&`` can never break the output or inject markup.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from .model import Doc

MEDIA = {
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
}
EXTENSIONS = tuple(MEDIA)


def render(doc: Doc, fmt: str, path: str = "") -> object:
    fmt = fmt.lower()
    if fmt in ("md", "markdown"):
        text = to_markdown(doc)
        return _write_text(text, path) if path else text
    if fmt == "html":
        text = to_html(doc)
        return _write_text(text, path) if path else text
    if fmt == "pdf":
        return to_pdf(doc, path)          # bytes when path is empty
    if fmt == "docx":
        return to_docx(doc, path)         # bytes when path is empty
    raise ValueError("unknown format: %s (expected %s)"
                     % (fmt, ", ".join(EXTENSIONS)))


def _write_text(text: str, path: str) -> str:
    Path(path).write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _md_cell(v: str) -> str:
    # a literal pipe or newline would break a Markdown table row
    return str(v).replace("\\", "\\\\").replace("|", "\\|") \
        .replace("\n", " ").strip()


def to_markdown(doc: Doc) -> str:
    out = ["# %s" % doc.title]
    if doc.subtitle:
        out.append("_%s_" % doc.subtitle)
    if doc.meta:
        out.append("")
        out.append(" · ".join("**%s:** %s" % (k, v)
                              for k, v in doc.meta.items()))
    out.append("")
    for b in doc.blocks:
        t = b["type"]
        if t == "heading":
            out.append("")
            out.append("#" * (b["level"] + 1) + " " + b["text"])
        elif t == "para":
            out.append("")
            out.append(b["text"])
        elif t == "bullets":
            out.append("")
            out += ["- " + i for i in b["items"]]
        elif t == "kv":
            out.append("")
            out += ["- **%s:** %s" % (k, v) for k, v in b["pairs"]]
        elif t == "table":
            out.append("")
            out.append("| " + " | ".join(_md_cell(h)
                                         for h in b["headers"]) + " |")
            out.append("| " + " | ".join(["---"] * len(b["headers"]))
                       + " |")
            for r in b["rows"]:
                out.append("| " + " | ".join(_md_cell(c)
                                             for c in r) + " |")
        elif t == "code":
            out.append("")
            out.append("```" + b.get("lang", ""))
            out.append(b["text"])
            out.append("```")
        elif t == "note":
            out.append("")
            out.append("> **Note:** " + b["text"].replace("\n", "\n> "))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# HTML (self-contained, escaped)
# ---------------------------------------------------------------------------

_HTML_STYLE = """
body{font-family:-apple-system,'Segoe UI',Roboto,Helvetica,sans-serif;
 max-width:900px;margin:0 auto;padding:40px 28px;color:#1a2230;line-height:1.55}
h1{font-size:26px;color:#101d33;margin:0 0 2px}
.sub{color:#68738a;font-size:14px;margin-bottom:6px}
.meta{color:#68738a;font-size:12.5px;border-bottom:1px solid #e3e7ee;
 padding-bottom:14px;margin-bottom:22px}
h2{font-size:19px;color:#16233c;margin-top:30px;border-bottom:1px solid #eef1f6;padding-bottom:4px}
h3{font-size:15.5px;color:#33415c;margin-top:20px}
h4{font-size:13.5px;color:#45526b;margin-top:16px}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}
th{background:#101d33;color:#fff;text-align:left;padding:7px 10px;font-weight:600}
td{border:1px solid #dfe4ec;padding:6px 10px;vertical-align:top}
tr:nth-child(even) td{background:#f6f8fb}
ul{margin:8px 0}li{margin:2px 0}
code,pre{font-family:'SF Mono',Menlo,Consolas,monospace}
pre{background:#0f1728;color:#e8edf6;padding:12px 14px;border-radius:8px;
 overflow-x:auto;font-size:12.5px}
dl{margin:8px 0}dt{font-weight:600;color:#33415c}dd{margin:0 0 6px 14px;color:#33415c}
.note{background:#fff8e6;border-left:3px solid #d9a520;padding:10px 14px;
 border-radius:4px;margin:12px 0;font-size:13px;color:#5c4a10}
"""


def to_html(doc: Doc) -> str:
    e = html.escape
    parts = ['<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,'
             'initial-scale=1">',
             "<title>%s</title><style>%s</style></head><body>"
             % (e(doc.title), _HTML_STYLE),
             "<h1>%s</h1>" % e(doc.title)]
    if doc.subtitle:
        parts.append('<div class="sub">%s</div>' % e(doc.subtitle))
    if doc.meta:
        parts.append('<div class="meta">%s</div>'
                     % " &nbsp;·&nbsp; ".join(
                         "<b>%s:</b> %s" % (e(str(k)), e(str(v)))
                         for k, v in doc.meta.items()))
    for b in doc.blocks:
        t = b["type"]
        if t == "heading":
            parts.append("<h%d>%s</h%d>" % (b["level"] + 1,
                                            e(b["text"]), b["level"] + 1))
        elif t == "para":
            parts.append("<p>%s</p>" % e(b["text"]).replace("\n", "<br>"))
        elif t == "bullets":
            parts.append("<ul>%s</ul>"
                         % "".join("<li>%s</li>" % e(i)
                                   for i in b["items"]))
        elif t == "kv":
            parts.append("<dl>%s</dl>"
                         % "".join("<dt>%s</dt><dd>%s</dd>"
                                   % (e(k), e(v)) for k, v in b["pairs"]))
        elif t == "table":
            head = "".join("<th>%s</th>" % e(h) for h in b["headers"])
            body = "".join("<tr>%s</tr>"
                           % "".join("<td>%s</td>" % e(c) for c in r)
                           for r in b["rows"])
            parts.append("<table><tr>%s</tr>%s</table>" % (head, body))
        elif t == "code":
            parts.append("<pre><code>%s</code></pre>" % e(b["text"]))
        elif t == "note":
            parts.append('<div class="note">%s</div>'
                         % e(b["text"]).replace("\n", "<br>"))
    parts.append("</body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# PDF (reportlab)
# ---------------------------------------------------------------------------

def to_pdf(doc: Doc, path: str = ""):
    import io
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (ListFlowable, ListItem, Paragraph,
                                    SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    e = html.escape
    styles = getSampleStyleSheet()
    navy = colors.HexColor("#101d33")
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=20,
                        textColor=navy, alignment=TA_LEFT)
    hs = {1: ParagraphStyle("H1", parent=styles["Heading1"], fontSize=15,
                            textColor=navy),
          2: ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13,
                            textColor=colors.HexColor("#33415c")),
          3: ParagraphStyle("H3", parent=styles["Heading3"], fontSize=11,
                            textColor=colors.HexColor("#45526b"))}
    body = styles["BodyText"]
    small = ParagraphStyle("small", parent=body, fontSize=8,
                           textColor=colors.grey)
    mono = ParagraphStyle("mono", parent=body, fontName="Courier",
                          fontSize=8, backColor=colors.HexColor("#f2f4f8"))

    buf = io.BytesIO() if not path else None
    d = SimpleDocTemplate(buf or path, pagesize=letter,
                          topMargin=0.7 * inch, bottomMargin=0.7 * inch,
                          leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                          title=doc.title)
    el = [Paragraph(e(doc.title), h1)]
    if doc.subtitle:
        el.append(Paragraph(e(doc.subtitle), small))
    if doc.meta:
        el.append(Paragraph(" | ".join("%s: %s" % (e(str(k)), e(str(v)))
                                       for k, v in doc.meta.items()),
                            small))
    el.append(Spacer(1, 12))
    avail = letter[0] - 1.6 * inch
    for b in doc.blocks:
        t = b["type"]
        if t == "heading":
            el.append(Spacer(1, 6))
            el.append(Paragraph(e(b["text"]), hs[b["level"]]))
        elif t == "para":
            el.append(Paragraph(e(b["text"]).replace("\n", "<br/>"), body))
        elif t == "bullets":
            el.append(ListFlowable(
                [ListItem(Paragraph(e(i), body)) for i in b["items"]],
                bulletType="bullet", start="•"))
        elif t == "kv":
            for k, v in b["pairs"]:
                el.append(Paragraph("<b>%s:</b> %s" % (e(k), e(v)), body))
        elif t == "table":
            ncol = len(b["headers"]) or 1
            cw = avail / ncol
            data = [[Paragraph(e(str(h)), ParagraphStyle(
                "th", parent=body, textColor=colors.white, fontSize=8,
                fontName="Helvetica-Bold")) for h in b["headers"]]]
            for r in b["rows"]:
                data.append([Paragraph(e(str(c)), ParagraphStyle(
                    "td", parent=body, fontSize=8)) for c in r])
            tbl = Table(data, colWidths=[cw] * ncol, repeatRows=1)
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), navy),
                ("GRID", (0, 0), (-1, -1), 0.4,
                 colors.HexColor("#c9d2df")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#f4f6fa")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            el.append(tbl)
        elif t == "code":
            el.append(Paragraph(e(b["text"]).replace("\n", "<br/>")
                                .replace(" ", "&nbsp;"), mono))
        elif t == "note":
            el.append(Paragraph("<i>Note: %s</i>" % e(b["text"]), small))
        el.append(Spacer(1, 4))
    d.build(el)
    return buf.getvalue() if buf is not None else path


# ---------------------------------------------------------------------------
# Word (python-docx)
# ---------------------------------------------------------------------------

def to_docx(doc: Doc, path: str = ""):
    import io
    from docx import Document
    from docx.shared import Pt, RGBColor
    d = Document()
    d.add_heading(doc.title, level=0)
    if doc.subtitle:
        d.add_paragraph(doc.subtitle).italic = True
    if doc.meta:
        m = d.add_paragraph()
        run = m.add_run(" | ".join("%s: %s" % (k, v)
                                   for k, v in doc.meta.items()))
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0x68, 0x73, 0x8a)
    for b in doc.blocks:
        t = b["type"]
        if t == "heading":
            d.add_heading(b["text"], level=b["level"])
        elif t == "para":
            d.add_paragraph(b["text"])
        elif t == "bullets":
            for i in b["items"]:
                d.add_paragraph(i, style="List Bullet")
        elif t == "kv":
            for k, v in b["pairs"]:
                para = d.add_paragraph()
                para.add_run("%s: " % k).bold = True
                para.add_run(v)
        elif t == "table":
            ncol = len(b["headers"]) or 1
            tbl = d.add_table(rows=1, cols=ncol)
            try:
                tbl.style = "Light Grid Accent 1"
            except Exception:  # noqa: BLE001 — style optional
                pass
            for i, h in enumerate(b["headers"]):
                cell = tbl.rows[0].cells[i]
                cell.text = str(h)
                for p in cell.paragraphs:
                    for run in p.runs:
                        run.bold = True
            for r in b["rows"]:
                cells = tbl.add_row().cells
                for i, c in enumerate(r):
                    cells[i].text = str(c)
        elif t == "code":
            para = d.add_paragraph()
            run = para.add_run(b["text"])
            run.font.name = "Courier New"
            run.font.size = Pt(8)
        elif t == "note":
            para = d.add_paragraph()
            run = para.add_run("Note: " + b["text"])
            run.italic = True
    if not path:
        buf = io.BytesIO()
        d.save(buf)
        return buf.getvalue()
    d.save(path)
    return path

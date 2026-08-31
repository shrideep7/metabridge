"""Lineage as a PDF: the diagram drawn, not the code for one.

`lineage.md` and `lineage.mmd` carry Mermaid, which only becomes a picture in
a viewer that renders it. This draws the graph directly with reportlab — the
same library every other PDF in the platform uses — so the diagram is IN the
document and needs no renderer, no browser and no Node toolchain. That last
point is not incidental: mermaid-cli would put a Node dependency on the
air-gapped install path for the sake of one image.

The page is landscape because a lineage graph is wide; layers run left to
right, which is the direction the data moves.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Layers in flow order, with the label shown above each column.
_LAYERS = (("sources", "Sources"), ("staging", "Staging"),
           ("intermediate", "Intermediate"), ("marts", "Marts"),
           ("snapshots", "Snapshots"), ("unmanaged", "Unmanaged"))

_FILL = {"source": "#e8eef7", "model": "#eef4ec", "snapshot": "#f4efe6",
         "unmanaged": "#f7ecec"}
_STROKE = {"source": "#5b7ca6", "model": "#4a7c59", "snapshot": "#9a7b4f",
           "unmanaged": "#b05555"}

# Above this the picture stops being readable and the table below it is the
# better artifact, so the diagram says so instead of drawing spaghetti.
MAX_NODES = 48


def _wrap(label: str, width: int = 22) -> List[str]:
    """Break a model name for a narrow box, preferring dbt's own separators."""
    out: List[str] = []
    for chunk in label.replace("__", "__\x00").split("\x00"):
        while len(chunk) > width:
            cut = max(chunk.rfind("_", 0, width), chunk.rfind(".", 0, width))
            cut = cut + 1 if cut > width // 2 else width
            out.append(chunk[:cut])
            chunk = chunk[cut:]
        if chunk:
            out.append(chunk)
    return out or [label]


def _build_flowable(graph: dict, avail_width: float):
    """A reportlab Flowable that paints the DAG, or None when it is too big."""
    from reportlab.lib import colors
    from reportlab.platypus import Flowable

    nodes = [n for n in graph.get("nodes", [])]
    if not nodes or len(nodes) > MAX_NODES:
        return None

    by_layer: Dict[str, List[dict]] = {}
    for n in nodes:
        by_layer.setdefault(n.get("layer") or "marts", []).append(n)
    columns = [(key, title, by_layer[key])
               for key, title in _LAYERS if by_layer.get(key)]
    if not columns:
        return None

    rows = max(len(c[2]) for c in columns)
    box_h, gap_y, head_h = 30.0, 14.0, 18.0
    col_w = avail_width / len(columns)
    box_w = min(col_w - 16.0, 150.0)
    height = head_h + rows * box_h + (rows - 1) * gap_y + 10.0

    # centre of every box, so the edges can be drawn between them
    pos: Dict[str, tuple] = {}
    for ci, (_key, _title, members) in enumerate(columns):
        cx = ci * col_w + col_w / 2.0
        span = len(members) * box_h + (len(members) - 1) * gap_y
        top = height - head_h - (height - head_h - span) / 2.0
        for ri, node in enumerate(members):
            cy = top - ri * (box_h + gap_y) - box_h / 2.0
            pos[node["name"]] = (cx, cy)

    class _Graph(Flowable):
        def wrap(self, *_args):
            return avail_width, height

        def draw(self):
            c = self.canv
            for ci, (_key, title, _members) in enumerate(columns):
                c.setFont("Helvetica-Bold", 7)
                c.setFillColor(colors.HexColor("#6b7280"))
                c.drawCentredString(ci * col_w + col_w / 2.0,
                                    height - 10.0, title.upper())

            for e in graph.get("edges", []):
                a, b = pos.get(e["from"]), pos.get(e["to"])
                if not a or not b:
                    continue
                dashed = e.get("kind") == "unmanaged"
                c.setStrokeColor(colors.HexColor(
                    "#b05555" if dashed else "#9aa4b2"))
                c.setLineWidth(0.8)
                c.setDash(3, 2) if dashed else c.setDash()
                x1, x2 = a[0] + box_w / 2.0, b[0] - box_w / 2.0
                c.line(x1, a[1], x2, b[1])
                # arrowhead at the downstream end
                c.setFillColor(colors.HexColor(
                    "#b05555" if dashed else "#9aa4b2"))
                c.setDash()
                p = c.beginPath()
                p.moveTo(x2, b[1])
                p.lineTo(x2 - 5, b[1] + 2.4)
                p.lineTo(x2 - 5, b[1] - 2.4)
                p.close()
                c.drawPath(p, fill=1, stroke=0)

            for node in nodes:
                cx, cy = pos[node["name"]]
                kind = node.get("kind", "model")
                c.setFillColor(colors.HexColor(_FILL.get(kind, "#eeeeee")))
                c.setStrokeColor(colors.HexColor(_STROKE.get(kind, "#666666")))
                c.setLineWidth(1.6 if node.get("terminal") else 0.9)
                c.setDash(3, 2) if kind == "unmanaged" else c.setDash()
                c.roundRect(cx - box_w / 2.0, cy - box_h / 2.0, box_w, box_h,
                            3, stroke=1, fill=1)
                c.setDash()
                lines = _wrap(node.get("label") or node["name"],
                              max(10, int(box_w / 3.4)))[:3]
                c.setFillColor(colors.HexColor("#1f2937"))
                c.setFont("Helvetica", 5.6)
                start = cy + (len(lines) - 1) * 3.1
                for i, ln in enumerate(lines):
                    c.drawCentredString(cx, start - i * 6.2 - 2.0, ln)

    return _Graph()


def write_lineage_pdf(doc: dict, path: str) -> Optional[str]:
    """Write the lineage PDF. -> path, or None when it cannot be produced.

    Returns None rather than raising when reportlab is absent: PDF export
    lives in the `web` extra, and a core install should still get the JSON,
    Markdown and Mermaid without a hard failure.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (KeepTogether, Paragraph,
                                        SimpleDocTemplate, Spacer, Table,
                                        TableStyle)
    except ImportError:                                  # pragma: no cover
        return None

    dbt = doc.get("dbt_project") or {}
    graph = dbt if dbt.get("nodes") else {}
    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=9,
                          leading=13, alignment=TA_LEFT)
    small = ParagraphStyle("s", parent=body, fontSize=7.5,
                           textColor=colors.HexColor("#6b7280"))
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16,
                        textColor=colors.HexColor("#1f2937"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11,
                        spaceBefore=10,
                        textColor=colors.HexColor("#33415c"))

    page = landscape(letter)
    avail = page[0] - 1.2 * inch
    out = SimpleDocTemplate(path, pagesize=page, topMargin=0.5 * inch,
                            bottomMargin=0.5 * inch, leftMargin=0.6 * inch,
                            rightMargin=0.6 * inch,
                            title="Data lineage — %s" % doc.get("project", ""))

    el: List = [Paragraph("Data lineage — %s" % doc.get("project", ""), h1)]
    counts = graph.get("counts", {})
    el.append(Paragraph(
        "Source platform: <b>%s</b> &nbsp;·&nbsp; %d source(s), %d model(s), "
        "%d snapshot(s), %d edge(s)."
        % (doc.get("source_platform") or "unknown", counts.get("source", 0),
           counts.get("model", 0), counts.get("snapshot", 0),
           len(graph.get("edges", []))), small))
    el.append(Spacer(1, 8))
    el.append(Paragraph(
        "Every arrow below is a <b>ref()</b> or <b>source()</b> call the "
        "generator actually emitted — the graph is read back off the written "
        "artifacts, not re-derived, so it cannot show an edge the project "
        "does not have.", body))
    el.append(Spacer(1, 10))

    if graph:
        flow = _build_flowable(graph, avail)
        if flow is not None:
            el.append(flow)
            el.append(Spacer(1, 6))
            el.append(Paragraph(
                "Heavier border = terminal model (nothing downstream in this "
                "project reads it). Dashed = a relation the project reads but "
                "neither builds nor declares as a source.", small))
        else:
            el.append(Paragraph(
                "This project has %d nodes — too many to draw legibly, so the "
                "table below is the lineage. lineage.mmd renders the full "
                "graph at any size." % len(graph.get("nodes", [])), body))
        el.append(Spacer(1, 12))

        el.append(Paragraph("Models", h2))
        rows = [["Model", "Layer", "Built from", "Path"]]
        for n in graph.get("nodes", []):
            if n.get("kind") not in ("model", "snapshot"):
                continue
            rows.append([n["name"], n.get("layer", ""),
                         n.get("source_object", ""), n.get("path", "")])
        if len(rows) > 1:
            t = Table(rows, colWidths=[avail * 0.26, avail * 0.12,
                                       avail * 0.22, avail * 0.40],
                      repeatRows=1)
            t.setStyle(TableStyle([
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 7),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#1f2937")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d7dde5")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
            el.append(t)

        if dbt.get("terminal_models"):
            el.append(Paragraph("Consumption boundary", h2))
            el.append(Paragraph(
                "Nothing downstream in this project reads %s — this is where "
                "the estate's own consumers attach. This is <b>topology, not "
                "telemetry</b>: it says nothing about who actually reads them, "
                "which is why no dbt exposure is generated for it."
                % ", ".join("<b>%s</b>" % m
                            for m in dbt["terminal_models"]), body))
        if dbt.get("unmanaged_relations"):
            el.append(Paragraph("Unmanaged relations", h2))
            el.append(Paragraph(
                "Read by a model but neither built here nor declared as a "
                "source, so dbt has no node for them and draws no edge — the "
                "holes in the graph above: %s. Resolve each by adding it to "
                "the source manifest, converting the object that builds it, "
                "or checking it in as a seed."
                % ", ".join("<b>%s</b>" % r
                            for r in dbt["unmanaged_relations"]), body))
    else:
        el.append(Paragraph(
            "No dbt project was generated for this run, so there is no model "
            "graph to draw. The table-level lineage of the source estate is "
            "in lineage.md.", body))

    # Column lineage is the part dbt's own docs site does not have, so it is
    # worth carrying into the PDF — capped, because a wide estate would other-
    # wise bury the diagram under hundreds of rows.
    chains = [(p["name"], c) for p in doc.get("pipelines", [])
              for c in p.get("column_lineage", [])]
    if chains:
        el.append(Paragraph("Column lineage", h2))
        el.append(Paragraph(
            "Each target column traced back through the transformation graph. "
            "A chain marked <i>coarse</i> could not be resolved to individual "
            "source columns and is labelled rather than guessed.", small))
        rows = [["Mapping", "Target column", "Derivation", "Chain"]]
        for name, c in chains[:40]:
            path = c["paths"][0] if c.get("paths") else []
            rows.append([name, c["target_column"], c.get("derivation", ""),
                         " → ".join(path[-4:])])
        t = Table(rows, colWidths=[avail * 0.16, avail * 0.24, avail * 0.10,
                                   avail * 0.50], repeatRows=1)
        t.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
            ("FONT", (0, 1), (-1, -1), "Helvetica", 6.5),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d7dde5")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        el.append(t)
        if len(chains) > 40:
            el.append(Spacer(1, 4))
            el.append(Paragraph(
                "%d further column chains are in lineage.json."
                % (len(chains) - 40), small))

    out.build(el)
    return path

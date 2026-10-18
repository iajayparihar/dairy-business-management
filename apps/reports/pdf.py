"""
PDF rendering of the report tables (same table the JSON and CSV use).

reportlab cannot shape Devanagari (vowel signs and conjuncts come out in the
wrong order), so any cell containing Hindi is drawn by Pillow with the Raqm /
HarfBuzz engine into a PNG and placed as an image. Latin text stays real,
selectable PDF text. Rows are capped (MAX_ROWS); beyond that use CSV.
"""
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils import timezone
from PIL import Image as PILImage, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

from apps.reports import exports

MAX_ROWS = 5000
FONT_DIR = Path(__file__).parent / "fonts"
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
SCALE = 4  # image pixels per PDF point


def has_hindi(text):
    return bool(DEVANAGARI.search(text or ""))


def _font(path, size_px):
    return ImageFont.truetype(str(path), size_px, layout_engine=ImageFont.Layout.RAQM)


def _paths(bold):
    """(Devanagari font, Latin font). Latin = Vera, shipped inside reportlab."""
    import reportlab

    vera = Path(reportlab.__file__).parent / "fonts"
    return (
        FONT_DIR / ("NotoSansDevanagari-Bold.ttf" if bold else "NotoSansDevanagari-Regular.ttf"),
        vera / ("VeraBd.ttf" if bold else "Vera.ttf"),
    )


# Devanagari, joiners and the rupee sign use the Devanagari font; everything
# else (Latin letters, digits, punctuation) uses the Latin font.
_DEVA_RUN = re.compile(r"[\u0900-\u097F\u200c\u200d\u20b9]+")


def _runs(text):
    """[(chunk, is_devanagari)] in order."""
    out, pos = [], 0
    for m in _DEVA_RUN.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


def shaped_image(text, size, max_width, bold=False):
    """Hindi/mixed text as a correctly shaped image flowable (at most max_width pt)."""
    px = int(size * SCALE)
    deva_path, latin_path = _paths(bold)
    fonts = {True: _font(deva_path, px), False: _font(latin_path, px)}
    runs = _runs(text)
    ascent = max(f.getmetrics()[0] for f in fonts.values())
    descent = max(f.getmetrics()[1] for f in fonts.values())
    width = sum(fonts[d].getlength(chunk) for chunk, d in runs)
    pad = SCALE
    img = PILImage.new("L", (int(width) + 2 * pad, ascent + descent + 2 * pad), 255)
    draw, x = ImageDraw.Draw(img), pad
    for chunk, is_deva in runs:
        draw.text((x, pad + ascent), chunk, font=fonts[is_deva], fill=0, anchor="ls")
        x += fonts[is_deva].getlength(chunk)
    buf = BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    w, h = img.width / SCALE, img.height / SCALE
    if w > max_width:
        h, w = h * max_width / w, max_width
    flow = Image(buf, width=w, height=h)
    flow.hAlign = "LEFT"
    return flow


def cell(text, size, max_width, bold=False, align="LEFT"):
    if has_hindi(text):
        return shaped_image(text, size, max_width, bold)
    style = ParagraphStyle(
        "c", fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size, leading=size + 2, alignment={"LEFT": 0, "RIGHT": 2}[align],
    )
    return Paragraph(escape(text or ""), style)


class _Numbered(canvas.Canvas):
    """Two-pass canvas so every page can say "Page x of y"."""

    footer = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont("Helvetica", 8)
            width = self._pagesize[0]
            self.drawString(15 * mm, 8 * mm, self.footer)
            self.drawRightString(width - 15 * mm, 8 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _widths(table, rows, usable):
    weights = []
    for i, (_, label, _) in enumerate(table["columns"]):
        longest = max([len(label)] + [len(r[i]) for r in rows[:200]])
        weights.append(min(max(longest, 4), 28) + 2)
    total = sum(weights)
    return [usable * w / total for w in weights]


def build_pdf(table, business, params):
    tz = ZoneInfo(business.timezone)
    cells = exports.table_cells(table, tz)
    if len(cells) > MAX_ROWS:
        raise ValidationError(
            {"export": f"Too many rows for a PDF ({len(cells)}; max {MAX_ROWS}). "
                       "Narrow the filters or export CSV."}
        )
    cols = table["columns"]
    pagesize = landscape(A4) if len(cols) > 6 else A4
    margin = 15 * mm
    usable = pagesize[0] - 2 * margin
    widths = _widths(table, cells, usable)
    size = 7 if len(cols) > 9 else 8

    out = BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=pagesize, leftMargin=margin, rightMargin=margin,
        topMargin=14 * mm, bottomMargin=16 * mm, title=table["title"],
        author=business.name,
    )
    story = [
        cell(business.name, 14, usable, bold=True),
        cell(table["title"], 11, usable, bold=True),
    ]
    period = ", ".join(f"{k}: {v}" for k, v in params.items() if v)
    stamp = timezone.now().astimezone(tz).strftime("%Y-%m-%d %H:%M")
    story.append(cell(f"{period + ' | ' if period else ''}Generated {stamp} ({business.timezone})", 8, usable))
    story.append(Spacer(1, 4 * mm))

    right = {i for i, (_, _, kind) in enumerate(cols) if kind in ("money", "qty")}

    def row_cells(values, bold=False):
        return [
            cell(v, size, widths[i] - 4, bold=bold, align="RIGHT" if i in right else "LEFT")
            for i, v in enumerate(values)
        ]

    data = [row_cells([label for _, label, _ in cols], bold=True)]
    data += [row_cells(r) for r in cells]
    totals = exports.totals_cells(table, tz)
    if totals:
        data.append(row_cells(totals, bold=True))
    if not cells:
        story.append(cell("No data for the selected filters.", 9, usable))
    tbl = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#b5bcc6")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]
    if totals:
        style.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f3f5f8")))
    tbl.setStyle(TableStyle(style))
    story.append(tbl)
    for w in table["warnings"]:
        story += [Spacer(1, 2 * mm), cell(w, 7, usable)]

    title = table["title"] if table["title"].isascii() else table["key"]
    canvas_cls = type("C", (_Numbered,), {"footer": title[:90]})
    doc.build(story, canvasmaker=canvas_cls)
    return out.getvalue()


def pdf_response(table, business, params):
    data = build_pdf(table, business, params)
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="{exports.safe_filename(table, business, params, "pdf")}"'
    )
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response

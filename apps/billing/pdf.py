"""
Printable bill, rendered from the stored snapshot only (lines, balance_before,
header_snapshot), never from live data, so reprinting an old bill gives the
same document. English, amounts as "Rs." (the standard PDF fonts have no
rupee sign); a Hindi bill needs a Devanagari font and text shaping.
"""
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.billing.documents import bill_document
from apps.billing.models import CustomerBill


def _rs(value):
    return f"Rs. {Decimal(value):,.2f}"


def _balance_line(balance):
    balance = Decimal(balance)
    if balance > 0:
        return "Previous outstanding", _rs(balance)
    if balance < 0:
        return "Previous advance", _rs(-balance)
    return "Previous balance", _rs(0)


def render_bill_pdf(bill):
    """PDF bytes for a FINALIZED or VOID bill. Byte-identical on every call."""
    if bill.status == CustomerBill.Status.DRAFT:
        raise ValueError("Only a finalized or void bill has a PDF.")

    doc_data = bill_document(bill)
    header = doc_data["header"]
    business, customer = header["business"], header["customer"]
    tz = ZoneInfo(business["timezone"])
    styles = getSampleStyleSheet()
    p = lambda text, style="Normal": Paragraph(escape(str(text)), styles[style])
    total = Decimal(doc_data["totals"]["total"])
    before = Decimal(doc_data["account"]["balance_before"])
    after = before + total

    story = [
        p(business["name"], "Title"),
        p(" | ".join(x for x in (business["address"], business["phone"]) if x)),
        Spacer(1, 6 * mm),
        p(f"Bill {bill.bill_number}", "Heading2"),
        p(f"Customer: {customer['name']} ({customer['code']})"),
    ]
    if customer["address"]:
        story.append(p(customer["address"]))
    story.append(p(
        "Opening balance brought forward"
        if bill.bill_type == CustomerBill.BillType.OPENING
        else f"Period: {bill.period_start:%d %b %Y} to {bill.period_end:%d %b %Y}"
    ))
    story.append(Spacer(1, 5 * mm))

    rows = [["Item", "Quantity", "Rate", "Amount"]]
    for item in doc_data["summary"]:
        rows.append([
            item["product"], f"{item['quantity']} {item['unit']}",
            _rs(item["rate"]), _rs(item["amount"]),
        ])
    if bill.bill_type == CustomerBill.BillType.OPENING:
        rows.append(["Balance brought forward", "", "", _rs(total)])
    rows.append(["This bill", "", "", _rs(total)])

    table = Table(rows, colWidths=[75 * mm, 35 * mm, 30 * mm, 35 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story += [table, Spacer(1, 6 * mm)]

    label, amount = _balance_line(before)
    account = [[label, amount], ["This bill", _rs(total)]]
    account.append(
        ["Amount payable", _rs(after)] if after >= 0
        else ["Advance remaining", _rs(-after)]
    )
    box = Table(account, colWidths=[60 * mm, 40 * mm])
    box.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black),
    ]))
    story += [box, Spacer(1, 6 * mm)]
    if bill.finalized_at:
        story.append(p(
            f"Issued {bill.finalized_at.astimezone(tz):%d %b %Y, %H:%M} "
            f"({business['timezone']})"
        ))
    if bill.status == CustomerBill.Status.VOID:
        story.append(p(f"VOID: {bill.void_reason}", "Heading3"))

    def stamp(canvas, _doc):
        if bill.status == CustomerBill.Status.VOID:
            canvas.saveState()
            canvas.setFont("Helvetica-Bold", 90)
            canvas.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.25))
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, "VOID")
            canvas.restoreState()

    out = BytesIO()
    SimpleDocTemplate(
        out, pagesize=A4, title=f"Bill {bill.bill_number}",
        author=business["name"], invariant=1,
    ).build(story, onFirstPage=stamp, onLaterPages=stamp)
    return out.getvalue()

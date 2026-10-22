"""
Printable bill, rendered from the stored snapshot only (lines, balance_before,
header_snapshot), never from live data, so reprinting an old bill gives the
same document. Language is English ("Rs.") or Hindi ("₹"); the default is
Business.bill_language. Any text with Devanagari or ₹ is shaped by Pillow/Raqm
(apps.reports.pdf), since reportlab cannot shape Hindi.
"""
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle

from apps.billing.documents import bill_document
from apps.billing.models import CustomerBill
from apps.businesses.models import Business
from apps.reports.pdf import SCALE, cell, shaped_pil

LANGUAGES = tuple(Business.BillLanguage.values)

# ponytail: wording drafted by us; the client may correct the Hindi here.
LABELS = {
    "en": {
        "money": "Rs. {}", "date": "%d %b %Y",
        "bill": "Bill {}", "customer": "Customer: {} ({})",
        "period": "Period: {} to {}", "opening": "Opening balance brought forward",
        "item": "Item", "quantity": "Quantity", "rate": "Rate", "amount": "Amount",
        "brought_forward": "Balance brought forward", "this_bill": "This bill",
        "prev_due": "Previous outstanding", "prev_advance": "Previous advance",
        "prev_zero": "Previous balance", "payable": "Amount payable",
        "advance_left": "Advance remaining", "issued": "Issued {} ({})",
        "void": "VOID: {}", "stamp": "VOID",
        "cancelled_title": "Cancelled / corrected entries (not charged, shown for transparency)",
        "cancelled": "Cancelled", "reason": "Reason",
    },
    "hi": {
        "money": "₹ {}", "date": "%d-%m-%Y",
        "bill": "बिल {}", "customer": "ग्राहक: {} ({})",
        "period": "अवधि: {} से {}", "opening": "पिछला बकाया आगे लाया गया",
        "item": "सामान", "quantity": "मात्रा", "rate": "दर", "amount": "राशि",
        "brought_forward": "आगे लाया गया बकाया", "this_bill": "इस बिल की राशि",
        "prev_due": "पिछला बकाया", "prev_advance": "पिछला अग्रिम",
        "prev_zero": "पिछला शेष", "payable": "कुल देय राशि",
        "advance_left": "शेष अग्रिम", "issued": "जारी {} ({})",
        "void": "रद्द: {}", "stamp": "रद्द / VOID",
        "cancelled_title": "रद्द / सुधारी गई प्रविष्टियाँ (शुल्क नहीं, पारदर्शिता के लिए)",
        "cancelled": "रद्द", "reason": "कारण",
    },
}


def render_bill_pdf(bill, lang=None):
    """PDF bytes for a FINALIZED or VOID bill. Byte-identical on every call."""
    if bill.status == CustomerBill.Status.DRAFT:
        raise ValueError("Only a finalized or void bill has a PDF.")
    lang = lang or bill.business.bill_language
    if lang not in LANGUAGES:
        raise ValueError(f"Unknown bill language {lang!r}.")
    t = LABELS[lang]
    rs = lambda value: t["money"].format(f"{Decimal(value):,.2f}")
    day = lambda d: d.strftime(t["date"])

    doc_data = bill_document(bill)
    header = doc_data["header"]
    business, customer = header["business"], header["customer"]
    tz = ZoneInfo(business["timezone"])
    total = Decimal(doc_data["totals"]["total"])
    before = Decimal(doc_data["account"]["balance_before"])
    after = before + total
    width = A4[0] - 2 * 20 * mm  # SimpleDocTemplate default margins
    line = lambda text, size=10, bold=False: cell(str(text), size, width, bold=bold)

    story = [
        line(business["name"], 18, bold=True),
        line(" | ".join(x for x in (business["address"], business["phone"]) if x)),
        Spacer(1, 6 * mm),
        line(t["bill"].format(bill.bill_number), 13, bold=True),
        line(t["customer"].format(customer["name"], customer["code"])),
    ]
    if customer["address"]:
        story.append(line(customer["address"]))
    story.append(line(
        t["opening"] if bill.bill_type == CustomerBill.BillType.OPENING
        else t["period"].format(day(bill.period_start), day(bill.period_end))
    ))
    story.append(Spacer(1, 5 * mm))

    widths = [75 * mm, 35 * mm, 30 * mm, 35 * mm]

    def row(values, bold=False):
        return [
            cell(v, 10, widths[i] - 12, bold=bold, align="RIGHT" if i else "LEFT")
            for i, v in enumerate(values)
        ]

    rows = [row([t["item"], t["quantity"], t["rate"], t["amount"]], bold=True)]
    for item in doc_data["summary"]:
        rows.append(row([
            item["product"], f"{item['quantity']} {item['unit']}",
            rs(item["rate"]), rs(item["amount"]),
        ]))
    if bill.bill_type == CustomerBill.BillType.OPENING:
        rows.append(row([t["brought_forward"], "", "", rs(total)]))
    rows.append(row([t["this_bill"], "", "", rs(total)], bold=True))

    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story += [table, Spacer(1, 6 * mm)]

    cancelled = [l for l in doc_data["lines"] if l["cancelled"]]
    if cancelled:
        story.append(line(t["cancelled_title"], 10, bold=True))
        crow = [row([t["item"], t["quantity"], t["rate"], t["cancelled"]], bold=True)]
        for c in cancelled:
            when = c["delivered_at"].astimezone(tz)
            reason = (c["cancel_reason"] or "").strip()
            label = f"{day(when)} {c['product']}"
            if reason:
                label += f" ({t['reason']}: {reason[:80]})"
            crow.append(row([
                label, f"{c['quantity']} {c['unit']}", rs(c["rate"]), rs(c["amount"]),
            ]))
        ctable = Table(crow, colWidths=widths, repeatRows=1)
        ctable.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("TEXTCOLOR", (0, 1), (-1, -1), colors.grey),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story += [ctable, Spacer(1, 6 * mm)]

    if before > 0:
        first = [t["prev_due"], rs(before)]
    elif before < 0:
        first = [t["prev_advance"], rs(-before)]
    else:
        first = [t["prev_zero"], rs(0)]
    last = [t["payable"], rs(after)] if after >= 0 else [t["advance_left"], rs(-after)]
    box_widths = [60 * mm, 40 * mm]
    account = [first, [t["this_bill"], rs(total)], last]
    box = Table(
        [
            [cell(v, 10, box_widths[i] - 12, bold=r == 2, align="RIGHT" if i else "LEFT")
             for i, v in enumerate(values)]
            for r, values in enumerate(account)
        ],
        colWidths=box_widths, hAlign="LEFT",
    )
    box.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black),
    ]))
    story += [box, Spacer(1, 6 * mm)]
    if bill.finalized_at:
        issued = bill.finalized_at.astimezone(tz)
        story.append(line(t["issued"].format(
            f"{day(issued)}, {issued:%H:%M}", business["timezone"],
        )))
    if bill.status == CustomerBill.Status.VOID:
        story.append(line(t["void"].format(bill.void_reason), 12, bold=True))

    watermark = None
    if bill.status == CustomerBill.Status.VOID:
        # Transparent RGBA so the stamp does not hide the bill under it.
        watermark = shaped_pil(
            t["stamp"], 70, bold=True, mode="RGBA",
            fill=(204, 26, 26, 64), background=(255, 255, 255, 0),
        )

    def stamp(canvas, _doc):
        if watermark is not None:
            w, h = watermark.width / SCALE, watermark.height / SCALE
            canvas.saveState()
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(35)
            canvas.drawImage(ImageReader(watermark), -w / 2, -h / 2, w, h, mask="auto")
            canvas.restoreState()

    out = BytesIO()
    SimpleDocTemplate(
        out, pagesize=A4, title=f"Bill {bill.bill_number}",
        author=business["name"], invariant=1,
    ).build(story, onFirstPage=stamp, onLaterPages=stamp)
    return out.getvalue()

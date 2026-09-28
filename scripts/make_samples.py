"""Generate the fictional sample documents in ``sample_data/``.

All companies, people, addresses and tax IDs are invented. Several samples are
deliberately "tricky" to exercise validation and routing:

    01  clean EUR invoice                               -> approved
    02  vendor spelled differently + GBP currency       -> approved (fuzzy vendor, FX)
    03  line items do not add up to the subtotal        -> needs review
    04  supplier email, EU number format, no due date   -> approved with a warning
    05  receipt with an unlabeled reference number      -> needs review (low confidence)
    06  re-sent copy of invoice 01                      -> needs review (duplicate)

Usage:  uv run python scripts/make_samples.py [output_dir]
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
BUYER = ["Bill to:", "Acme Analytics GmbH", "Accounts Payable", "Torstrasse 45, 10119 Berlin"]


@dataclass
class PdfInvoice:
    filename: str
    vendor_lines: list[str]
    meta: list[tuple[str, str]]
    items: list[tuple[str, str, str, str]]
    totals: list[tuple[str, str]]
    title: str = "INVOICE"
    footer: list[str] = field(default_factory=list)
    header_columns: tuple[str, str, str, str] = ("Description", "Qty", "Unit price", "Amount")


def render_pdf(doc: PdfInvoice, out_dir: Path) -> Path:
    path = out_dir / doc.filename
    pdf = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    pdf.setTitle(f"{doc.title} {doc.meta[0][1]}")
    pdf.setAuthor(doc.vendor_lines[0])
    width, height = A4
    left, right = 56, width - 56
    y = height - 64

    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(left, y, doc.vendor_lines[0])
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawRightString(right, y, doc.title)
    pdf.setFont("Helvetica", 9.5)
    for line in doc.vendor_lines[1:]:
        y -= 14
        pdf.drawString(left, y, line)

    y -= 36
    meta_y = y
    for label, value in doc.meta:
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(left, y, f"{label}:")
        pdf.setFont("Helvetica", 10)
        pdf.drawString(left + 90, y, value)
        y -= 15
    buyer_y = meta_y
    for i, line in enumerate(BUYER):
        pdf.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 10)
        pdf.drawString(width / 2 + 40, buyer_y, line)
        buyer_y -= 15
    y = min(y, buyer_y) - 24

    columns = (left, right - 190, right - 95, right)
    pdf.setFont("Helvetica-Bold", 10)
    _row(pdf, y, columns, doc.header_columns)
    y -= 6
    pdf.line(left, y, right, y)
    pdf.setFont("Helvetica", 10)
    for item in doc.items:
        y -= 16
        _row(pdf, y, columns, item)
    y -= 10
    pdf.line(left, y, right, y)

    for i, (label, value) in enumerate(doc.totals):
        y -= 17
        bold = i == len(doc.totals) - 1
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        pdf.drawString(right - 230, y, label)
        pdf.drawRightString(right, y, value)

    pdf.setFont("Helvetica", 8.5)
    y = 90
    for line in doc.footer:
        pdf.drawString(left, y, line)
        y -= 12
    pdf.showPage()
    pdf.save()
    return path


def _row(pdf: canvas.Canvas, y: float, columns: tuple[float, ...], cells: tuple[str, ...]) -> None:
    pdf.drawString(columns[0], y, cells[0])
    for x, cell in zip(columns[1:], cells[1:], strict=True):
        pdf.drawRightString(x, y, cell)


BRIGHTLINE = PdfInvoice(
    filename="01_brightline_software_invoice.pdf",
    vendor_lines=[
        "Brightline Software GmbH",
        "Friedrichstrasse 120, 10117 Berlin, Germany",
        "VAT ID: DE811234567",
        "billing@brightline-software.example",
    ],
    meta=[
        ("Invoice No", "BLS-2026-0142"),
        ("Invoice Date", "2026-03-02"),
        ("Due Date", "2026-04-01"),
    ],
    items=[
        ("Team plan subscription (March)", "12", "49.00", "588.00"),
        ("Priority support add-on", "1", "150.00", "150.00"),
        ("Onboarding workshop (half day)", "1", "600.00", "600.00"),
    ],
    totals=[("Subtotal", "1,338.00"), ("VAT 19%", "254.22"), ("Total due (EUR)", "1,592.22")],
    footer=[
        "Payment by bank transfer within 30 days. IBAN DE00 1234 5678 9012 3456 78",
        "Brightline Software GmbH - Amtsgericht Charlottenburg HRB 000000 (fictional)",
    ],
)

NORTHWIND = PdfInvoice(
    filename="02_northwind_office_supplies.pdf",
    vendor_lines=[
        "Northwind Traders Ltd.",
        "14 Wharf Road, London N1 7GR, United Kingdom",
        "VAT Reg No: GB 123 4567 89",
    ],
    meta=[
        ("Invoice No", "NWT-58311"),
        ("Invoice date", "14 March 2026"),
        ("Payment due", "13 April 2026"),
    ],
    items=[
        ("A4 copy paper, 80gsm (box of 5 reams)", "10", "21.50", "215.00"),
        ("Toner cartridge TN-2420", "4", "68.90", "275.60"),
        ("Whiteboard markers (pack of 12)", "6", "9.40", "56.40"),
    ],
    totals=[("Subtotal", "547.00"), ("VAT 20%", "109.40"), ("Total (GBP)", "656.40")],
    footer=["Thank you for your business. Registered in England & Wales No. 00000000."],
)

BLUEPEAK = PdfInvoice(
    filename="03_bluepeak_consulting_invoice.pdf",
    vendor_lines=[
        "Bluepeak Consulting LLC",
        "221 Harbor Street, Suite 400, Boston, MA 02110, USA",
        "EIN: 84-2917365",
    ],
    meta=[
        ("Invoice #", "BPC-1007"),
        ("Invoice Date", "March 5, 2026"),
        ("Due Date", "April 4, 2026"),
    ],
    items=[
        ("Strategy workshop (2 days on site)", "2", "1,800.00", "3,600.00"),
        ("Follow-up report and recommendations", "1", "600.00", "600.00"),
    ],
    # Deliberate error: line items sum to 4,200.00 but the subtotal says 4,500.00.
    totals=[("Subtotal", "4,500.00"), ("Tax", "0.00"), ("Amount due USD", "4,500.00")],
    footer=["Wire transfer only. Late payments incur 1.5% monthly interest."],
)

BRIGHTLINE_COPY = PdfInvoice(
    filename="06_brightline_software_invoice_copy.pdf",
    vendor_lines=[
        "Brightline Software GmbH",
        "Friedrichstrasse 120, 10117 Berlin, Germany",
        "VAT ID: DE811234567",
        "billing@brightline-software.example",
    ],
    title="INVOICE (COPY)",
    meta=BRIGHTLINE.meta,
    items=BRIGHTLINE.items,
    totals=BRIGHTLINE.totals,
    footer=["This is a copy of an invoice sent on 2026-03-02, re-sent at your request."],
)

KESTREL_BODY = """\
Dear Accounts Payable team,

please find below our invoice for the March consolidation shipments.

Invoice number: KFL-88412
Invoice date: 12.03.2026
Supplier: Kestrel Freight Logistics B.V.
VAT number: NL853746291B01

Pallet transport Rotterdam - Berlin     4     210,00     840,00
Customs documentation                   1      75,00      75,00
Fuel surcharge                          1      42,50      42,50

Net amount:    957,50 EUR
VAT 21%:       201,08 EUR
Total amount:  1.158,58 EUR

Kind regards,
Marieke de Vries
Billing Department, Kestrel Freight Logistics B.V.
Waalhaven Oostzijde 88, 3087 BM Rotterdam
"""

PINECREST_RECEIPT = """\
PINECREST HARDWARE
1450 Alder Street, Portland, OR 97205
Tel (503) 555-0142

SALES RECEIPT
Date: 2026-03-18
Ref PH-88213
Cashier: 04

USB-C docking station       2      129.99      259.98
27in monitor arm            2       64.50      129.00
HDMI cable 2m               4        9.75       39.00

Subtotal                                        427.98
Sales tax (OR 0%)                                 0.00
TOTAL USD                                       427.98

Paid: VISA ****4417
Thank you for shopping local!
"""


def write_email(out_dir: Path) -> Path:
    message = EmailMessage()
    message["From"] = "Kestrel Freight Logistics <billing@kestrel-freight.example>"
    message["To"] = "ap@acme-analytics.example"
    message["Subject"] = "Invoice KFL-88412 - March consolidation shipments"
    sent = datetime(2026, 3, 12, 9, 14, tzinfo=timezone(timedelta(hours=1)))
    message["Date"] = format_datetime(sent)
    message["Message-ID"] = "<kfl-88412@kestrel-freight.example>"
    message.set_content(KESTREL_BODY)
    path = out_dir / "04_kestrel_freight_email.eml"
    path.write_bytes(bytes(message))
    return path


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = [
        render_pdf(BRIGHTLINE, out_dir),
        render_pdf(NORTHWIND, out_dir),
        render_pdf(BLUEPEAK, out_dir),
        write_email(out_dir),
    ]
    receipt = out_dir / "05_pinecrest_hardware_receipt.txt"
    receipt.write_text(PINECREST_RECEIPT, encoding="utf-8")
    written += [receipt, render_pdf(BRIGHTLINE_COPY, out_dir)]
    for path in written:
        print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sample_data")

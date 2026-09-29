"""Generate the fictional sample documents in ``sample_data/``.

All companies, people, addresses and tax IDs are invented. Several samples are
deliberately "tricky" to exercise validation and routing:

    01  clean EUR invoice                               -> approved
    02  vendor spelled differently + GBP currency       -> approved (fuzzy vendor, FX)
    03  line items do not add up to the subtotal        -> needs review
    04  supplier email, EU number format, no due date   -> approved with a warning
    05  receipt with an unlabeled reference number      -> needs review (low confidence)
    06  re-sent copy of invoice 01                      -> needs review (duplicate)
    07  scanned invoice: image-only PDF, skewed, noisy   -> OCR
    08  phone photo of a faded till receipt (JPEG)      -> OCR

``feedback_loop/`` holds two invoices from one vendor whose labels ("Document no.",
"Tax point") the offline fake extractor does not know; ``manage.py
demo_feedback_loop`` uses them to show reviewer corrections turning into few-shot
examples.

Scans are rendered from the same vector layout, then degraded deterministically
(fixed random seed): rotation, blur and speckle noise for the scan; a desk
background, uneven lighting, blur and JPEG compression for the photo.

Usage:  uv run python scripts/make_samples.py [output_dir]
"""

from __future__ import annotations

import io
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
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


LANTERN = PdfInvoice(
    filename="07_lantern_print_scan.pdf",
    vendor_lines=[
        "Lantern Print Studio GmbH",
        "Hafenstrasse 8, 20359 Hamburg, Germany",
        "VAT ID: DE274839165",
    ],
    meta=[
        ("Invoice No", "LPS-24-0918"),
        ("Invoice Date", "2026-03-09"),
        ("Due Date", "2026-04-08"),
    ],
    items=[
        ("Business cards, 400gsm (500 pcs)", "3", "45.00", "135.00"),
        ("Roll-up banner 85 x 200 cm", "2", "119.00", "238.00"),
        ("Flyers A5, 135gsm (2000 pcs)", "1", "164.00", "164.00"),
    ],
    totals=[("Subtotal", "537.00"), ("VAT 19%", "102.03"), ("Total due (EUR)", "639.03")],
    footer=["Payment by bank transfer within 30 days. Thank you for your order."],
)

HAVERFORD_ADDRESS = [
    "Haverford Office Interiors Ltd",
    "12 Canal Street, Manchester M1 3HE, United Kingdom",
    "VAT Reg No: GB 318 4476 25",
]

HAVERFORD_MARCH = PdfInvoice(
    filename="haverford_invoice_march.pdf",
    vendor_lines=HAVERFORD_ADDRESS,
    meta=[
        ("Document no.", "HOI-7731"),
        ("Tax point", "06/03/2026"),
        ("Payment due", "05/04/2026"),
    ],
    items=[
        ("Height-adjustable desk frame", "4", "310.00", "1,240.00"),
        ("Desk installation (per desk)", "4", "45.00", "180.00"),
    ],
    totals=[("Subtotal", "1,420.00"), ("VAT 20%", "284.00"), ("Total (GBP)", "1,704.00")],
    footer=["Goods remain our property until paid in full."],
)

HAVERFORD_APRIL = PdfInvoice(
    filename="haverford_invoice_april.pdf",
    vendor_lines=HAVERFORD_ADDRESS,
    meta=[
        ("Document no.", "HOI-7802"),
        ("Tax point", "03/04/2026"),
        ("Payment due", "03/05/2026"),
    ],
    items=[
        ("Acoustic desk screen 140 cm", "6", "89.00", "534.00"),
        ("Cable tray, steel", "6", "24.50", "147.00"),
    ],
    totals=[("Subtotal", "681.00"), ("VAT 20%", "136.20"), ("Total (GBP)", "817.20")],
    footer=["Goods remain our property until paid in full."],
)

COPPERLEAF_RECEIPT = [
    "COPPERLEAF CATERING LTD",
    "42 Mill Lane, Bristol BS1 5TY",
    "VAT Reg No: GB 284 1937 62",
    "",
    "TAX INVOICE",
    "Invoice No: CC-20931",
    "Date: 20/03/2026",
    "",
    "Lunch platter 10p  2   48.00   96.00",
    "Fruit bowl         3   12.50   37.50",
    "Coffee service     1   35.00   35.00",
    "",
    "Subtotal                   168.50",
    "VAT 20%                     33.70",
    "TOTAL GBP                  202.20",
    "",
    "Paid by card **** 8812",
    "Thank you!",
]


@dataclass(frozen=True)
class Degradation:
    angle: float  # degrees, counter-clockwise
    blur: float  # Gaussian blur radius in pixels
    speckle: float  # share of pixels replaced by black or white noise
    seed: int


def rasterize(pdf_path: Path, dpi: int) -> Image.Image:
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return pdf[0].render(scale=dpi / 72).to_pil().convert("RGB")
    finally:
        pdf.close()


def _speckle(size: tuple[int, int], share: float, rnd: random.Random) -> Image.Image:
    """Mask image: 0/255 where noise hits, 128 (neutral in 'overlay') elsewhere."""
    cut = int(share * 128)
    noise = Image.frombytes("L", size, rnd.randbytes(size[0] * size[1]))
    return noise.point(lambda v: 0 if v < cut else 255 if v > 255 - cut else 128)


def scan(page: Image.Image, fx: Degradation) -> Image.Image:
    """Flatbed-scanner look: grayscale, slightly skewed, soft, with dust speckles."""
    rnd = random.Random(fx.seed)
    image = page.convert("L").rotate(fx.angle, Image.Resampling.BICUBIC, fillcolor=255)
    image = image.filter(ImageFilter.GaussianBlur(fx.blur))
    return ImageChops.overlay(image, _speckle(image.size, fx.speckle, rnd))


def photograph(page: Image.Image, fx: Degradation) -> Image.Image:
    """Phone-photo look: paper on a desk, tilted, light falling off, soft focus."""
    rnd = random.Random(fx.seed)
    width, height = page.size
    desk = Image.new("RGB", (int(width * 1.5), int(height * 1.12)), (84, 68, 54))
    desk.paste(page, ((desk.width - width) // 2, (desk.height - height) // 2))
    image = desk.rotate(fx.angle, Image.Resampling.BICUBIC, fillcolor=(84, 68, 54))
    # Vignette plus light falling off towards the bottom of the frame.
    vignette = Image.radial_gradient("L").resize(image.size).point(lambda v: 255 - v // 3)
    falloff = Image.linear_gradient("L").resize(image.size).point(lambda v: 255 - v // 4)
    light = ImageChops.multiply(vignette, falloff)
    image = ImageChops.multiply(image, Image.merge("RGB", [light] * 3))
    image = image.filter(ImageFilter.GaussianBlur(fx.blur))
    noise = _speckle(image.size, fx.speckle, rnd).convert("RGB")
    return ImageChops.overlay(image, noise)


def render_receipt(lines: list[str]) -> Path:
    """Narrow thermal-printer receipt (faded monospace print) as a temporary PDF."""
    buffer = io.BytesIO()
    width, height = 80 * mm, (len(lines) * 4.4 + 16) * mm
    pdf = canvas.Canvas(buffer, pagesize=(width, height), invariant=True)
    pdf.setFillColorRGB(0.33, 0.33, 0.33)  # thermal print fades to grey
    y = height - 9 * mm
    for line in lines:
        pdf.setFont("Courier-Bold" if line.isupper() else "Courier", 8.2)
        pdf.drawString(5 * mm, y, line)
        y -= 4.4 * mm
    pdf.showPage()
    pdf.save()
    return _temp_pdf(buffer.getvalue())


def _temp_pdf(data: bytes) -> Path:
    import tempfile

    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)  # noqa: SIM115
    handle.write(data)
    handle.close()
    return Path(handle.name)


def write_scanned_pdf(doc: PdfInvoice, fx: Degradation, out_dir: Path, dpi: int = 200) -> Path:
    """Image-only PDF (no text layer), like the output of an office scanner."""
    vector = render_pdf(doc, _temp_dir())
    image = scan(rasterize(vector, dpi), fx)
    path = out_dir / doc.filename
    image.save(path, "PDF", resolution=dpi, creationDate=None, modDate=None)
    return path


def write_photo(lines: list[str], fx: Degradation, path: Path, dpi: int = 220) -> Path:
    receipt = rasterize(render_receipt(lines), dpi)
    photograph(receipt, fx).save(path, "JPEG", quality=72)
    return path


def _temp_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp())


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
    written.append(
        write_scanned_pdf(LANTERN, Degradation(angle=0.9, blur=0.7, speckle=0.004, seed=7), out_dir)
    )
    written.append(
        write_photo(
            COPPERLEAF_RECEIPT,
            Degradation(angle=-3.2, blur=1.1, speckle=0.002, seed=8),
            out_dir / "08_copperleaf_catering_photo.jpg",
        )
    )
    loop_dir = out_dir / "feedback_loop"
    loop_dir.mkdir(exist_ok=True)
    written += [render_pdf(HAVERFORD_MARCH, loop_dir), render_pdf(HAVERFORD_APRIL, loop_dir)]
    for path in written:
        print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sample_data")

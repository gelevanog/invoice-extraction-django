# DocExtract

**Turn invoices, receipts and supplier emails into validated, structured records - with an LLM doing the reading, deterministic code doing the checking, and a human reviewing only the uncertain cases.**

[![CI](https://github.com/gelevanog/invoice-extraction-django/actions/workflows/ci.yml/badge.svg)](https://github.com/gelevanog/invoice-extraction-django/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-5.2-092E20?logo=django&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-5-37814A?logo=celery&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

https://github.com/user-attachments/assets/79b0f0ab-bdfa-4164-bb81-1e5f76594710

<sub>40-second walkthrough with voiceover. Can't play it? [Download the MP4](docs/demo.mp4).</sub>

![Review page: extracted fields next to the source text, evidence highlighted](docs/review.png)

## What problem it solves

Finance and operations teams still type data from PDFs and emails into their accounting system by hand. It is slow, it does not scale with volume, and typos, duplicates and invoices that "don't add up" slip through.

DocExtract automates the typing without giving up control:

- An LLM reads each document - PDF, email, or a scan / phone photo via OCR - and fills in a fixed form (vendor, invoice number, dates, amounts, line items).
- Plain business rules then check the result: do the lines add up, is the total right, is this a duplicate, is the tax ID plausible.
- Clean records are approved automatically. Anything with an error or a low-confidence field lands in a review queue, where a person sees the extracted values **next to the highlighted text they came from**, fixes what is wrong and approves or rejects.
- Approved data can be exported (CSV/JSON) or pulled by other systems through a REST API.
- Every correction a reviewer makes is remembered: it becomes a worked example for the next invoice from that vendor, and an accuracy dashboard shows which fields and vendors still need help.

## Features

- **Multi-format intake** - text-layer PDFs (page-aware), scanned PDFs, photos and scans (PNG, JPEG, multi-page TIFF), plain text and `.eml` emails including PDF / image / text attachments.
- **OCR behind an interface** - `tesseract` (local, word boxes and per-word confidence) or `vision` (a vision LLM transcribes the page). Only PDF pages without a usable text layer are OCRed. A value is never more confident than the words it was read from, and poorly recognised words are underlined in the review UI.
- **LLM structured extraction** - Pydantic schema with a value, confidence and verbatim evidence quote per field; invalid output is sent back to the model with the exact validation errors (up to `LLM_MAX_ATTEMPTS`).
- **Provider abstraction** - `anthropic` (Claude, structured outputs), `openai` (JSON-schema response format), `openrouter` (any OpenAI-compatible model incl. free ones, with fallback models, retries and throttling) or `fake`, a deterministic offline extractor so the demo and the tests need **zero API keys**.
- **Learns from reviewer corrections** - approvals record every field as accepted or corrected; verified documents of the same vendor are added to the extraction prompt as few-shot examples (bounded, corrected first, then most recent).
- **Accuracy tracking** - share of fields accepted unchanged, per field, per vendor and over time: dashboard, REST endpoint and `manage.py accuracy_report`.
- **Deterministic validation** - line-item sums, subtotal + tax = total, date sanity, ISO 4217 currency, tax ID formats, duplicate detection, "quote not found in document", scan quality - each a small, pluggable function producing a structured issue with a severity.
- **Enrichment** - fuzzy vendor matching (rapidfuzz) against vendor master data, currency conversion to a base currency through a pluggable FX provider (bundled static table or live ECB rates), expense categories per line item (keyword rules or LLM).
- **Confidence-based routing** - errors, poor scans or any field below `REVIEW_CONFIDENCE_THRESHOLD` go to review; everything else is auto-approved.
- **Review UI** (Django templates + htmx + Pico.css) - filterable queue, side-by-side review page with evidence highlighting (also on OCR text, next to the original image), edit-in-place that re-runs validation instantly, approve/reject with an audit note, "approve and go to next", accuracy dashboard.
- **REST API** (Django REST Framework + OpenAPI/Swagger) - upload, poll status, list/filter, approve/reject, CSV/JSON export, accuracy and corrections.
- **Audit trail** - every LLM run is stored with raw output, attempts, per-attempt errors, tokens, latency and the few-shot examples it was given; reviewer, time and note are stored on decisions.
- **Background processing** - Celery + Redis in Docker; eager (inline) mode for local runs and tests. Batch import with `manage.py process_folder`.
- **Framework-agnostic core** - the `extraction/` package is plain, typed Python (`mypy --strict` clean) with no Django imports.

## Architecture

```mermaid
flowchart LR
    A[PDF / TXT / EML] --> P[Parse<br/>pypdf, email]
    S[Scan / photo /<br/>PDF without text layer] --> O[OCR<br/>Tesseract or vision LLM]
    O -- "text + word confidence" --> P
    P --> X[Extract<br/>LLM + Pydantic schema]
    FS[(Verified examples<br/>same vendor)] -. "few-shot" .-> X
    X -- "invalid JSON / schema errors<br/>(fed back, up to N attempts)" --> X
    X -- "confidence capped<br/>by OCR confidence" --> V[Validate<br/>deterministic rules]
    V --> E[Enrich<br/>vendor match, FX, categories]
    E --> R{Route}
    R -- "no errors and<br/>confidence >= threshold" --> OK[Approved]
    R -- "error, poor scan or<br/>low confidence" --> Q[Review queue]
    Q -- "edit fields<br/>(re-validates)" --> Q
    Q --> OK
    Q --> NO[Rejected]
    Q -- "approve: field reviews<br/>(accepted / corrected)" --> FS
    FS --> ACC[Accuracy dashboard]
    OK --> API[REST API / CSV / JSON export]
```

Document status machine (enforced in `extraction/status.py`, persisted after every stage):

```mermaid
stateDiagram-v2
    [*] --> uploaded
    uploaded --> parsed
    parsed --> extracted
    extracted --> validated
    validated --> enriched
    enriched --> approved: auto-approve
    enriched --> needs_review
    needs_review --> approved: reviewer
    needs_review --> rejected: reviewer
    uploaded --> failed
    parsed --> failed
    extracted --> failed
    validated --> failed
    enriched --> failed
    needs_review --> uploaded: reprocess
    rejected --> uploaded: reprocess
    failed --> uploaded: reprocess
    approved --> [*]
```

## Example

Input - a supplier email (`sample_data/04_kestrel_freight_email.eml`, excerpt). Note the European number format and the missing due date:

```text
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
```

Extracted record (real output of the pipeline with `LLM_PROVIDER=fake`; line items shortened to one):

```json
{
  "vendor_name":    {"value": "Kestrel Freight Logistics B.V.", "confidence": 0.92,
                     "evidence": {"quote": "Supplier: Kestrel Freight Logistics B.V.", "page": 1}},
  "vendor_tax_id":  {"value": "NL853746291B01", "confidence": 0.95,
                     "evidence": {"quote": "VAT number: NL853746291B01", "page": 1}},
  "invoice_number": {"value": "KFL-88412", "confidence": 0.95,
                     "evidence": {"quote": "Invoice number: KFL-88412", "page": 1}},
  "issue_date":     {"value": "2026-03-12", "confidence": 0.95,
                     "evidence": {"quote": "Invoice date: 12.03.2026", "page": 1}},
  "due_date":       {"value": null, "confidence": 0.9, "evidence": null},
  "currency":       {"value": "EUR", "confidence": 0.95,
                     "evidence": {"quote": "Total amount: 1.158,58 EUR", "page": 1}},
  "subtotal":       {"value": "957.50", "confidence": 0.95,
                     "evidence": {"quote": "Net amount: 957,50 EUR", "page": 1}},
  "tax":            {"value": "201.08", "confidence": 0.95,
                     "evidence": {"quote": "VAT 21%: 201,08 EUR", "page": 1}},
  "total":          {"value": "1158.58", "confidence": 0.95,
                     "evidence": {"quote": "Total amount: 1.158,58 EUR", "page": 1}},
  "line_items": [
    {"description": "Pallet transport Rotterdam - Berlin", "quantity": "4",
     "unit_price": "210.00", "amount": "840.00", "confidence": 0.9,
     "evidence": {"quote": "Pallet transport Rotterdam - Berlin 4 210,00 840,00", "page": 1}}
  ]
}
```

Result: one `missing_due_date` **warning**, vendor matched to the master record by tax ID, **auto-approved**.

A tricky one - `03_bluepeak_consulting_invoice.pdf`, whose line items add up to 4,200.00 while the printed subtotal is 4,500.00:

```json
[
  {"code": "line_items_sum_mismatch", "severity": "error", "field": "subtotal",
   "message": "Line items add up to 4200.00 but the subtotal is 4500.00 (difference -300.00)."},
  {"code": "new_vendor", "severity": "info", "field": "vendor_name",
   "message": "'Bluepeak Consulting LLC' does not match any known vendor; a vendor record is created when the invoice is approved."}
]
```

Routing decision: `needs_review` - *"1 error-level issue(s): line_items_sum_mismatch"*. The screenshot at the top is this document in the review UI.

## Scanned documents and photos (OCR)

Images, and PDF pages without a usable text layer (fewer than 20 letters/digits), go through an OCR engine before extraction. The engine returns page text laid out like the PDF parser's (one line per printed line, table columns separated by wide gaps) **plus every word's offsets, box and confidence**, so evidence matching, highlighting and validation work unchanged on OCR text.

![Review page for a phone photo: original image, OCR text with evidence highlighted and uncertain words underlined](docs/review_scan.png)

`sample_data/07_lantern_print_scan.pdf` is an image-only PDF (no text layer; rendered at 200 dpi, rotated 0.9°, blurred, with dust speckles) and `08_copperleaf_catering_photo.jpg` a phone-style photo of a faded till receipt (desk background, tilt, uneven light, JPEG). Both are generated by `scripts/make_samples.py` with a fixed seed.

What Tesseract 5.5 reads from the scan (real output, `OCR_ENGINE=tesseract`; the stray `Ne` is the table rule, recognised with confidence 0.00):

```text
Lantern Print Studio GmbH    INVOICE
Hafenstrasse 8, 20359 Hamburg, Germany
VAT ID: DE274839165
Invoice No:    LPS-24-0918    Bill to:
Invoice Date:    2026-03-09    Acme Analytics GmbH
Due Date:    2026-04-08    Accounts Payable
Torstrasse 45, 10119 Berlin
Description    Qty    Unit price    Amount
Business cards, 400gsm (500 pcs)    3    45.00    135.00
Roll-up banner 85 x 200 cm    2    119.00    238.00
Flyers A5, 135gsm (2000 pcs)    1    164.00    164.00
Ne
Subtotal    537.00
VAT 19%    102.03
Total due (EUR)    639.03
Payment by bank transfer within 30 days. Thank you for your order.
```

Extracted by the pipeline from that text (`LLM_PROVIDER=fake`; `ocr` = lowest OCR confidence of the words the evidence covers, which caps the field confidence):

| Field | Value | Confidence | Evidence (found in OCR text) | OCR |
|---|---|---|---|---|
| vendor_name | Lantern Print Studio GmbH | 0.90 | `Lantern Print Studio GmbH` | 0.96 |
| vendor_tax_id | DE274839165 | **0.91** | `VAT ID: DE274839165` | 0.91 |
| invoice_number | LPS-24-0918 | **0.92** | `Invoice No: LPS-24-0918` | 0.92 |
| issue_date / due_date | 2026-03-09 / 2026-04-08 | 0.95 | `Invoice Date: 2026-03-09` / `Due Date: 2026-04-08` | 0.96 |
| subtotal / tax / total | 537.00 / 102.03 / 639.03 EUR | 0.95 | `Subtotal 537.00` ... `Total due (EUR) 639.03` | 0.96 |

Result: 3 line items, sums check out, an `ocr_text` info issue ("Page(s) 1 read by OCR (tesseract, mean confidence 0.95)"), **auto-approved**.

The photo is harder, and that is visible rather than hidden: Tesseract reads the quantity `1` of "Coffee service" as `ai` (confidence 0.35) and `10p` with 0.42. The line item quoting `10p` is capped to 0.42, the misread line is not recognised as a line item, so the sums no longer match - **needs review** with `line_items_sum_mismatch` and `confidence below 0.75: line_items.0=0.42`. The reviewer sees the photo next to the OCR text with exactly those words underlined (screenshot above).

**Vision path.** `OCR_ENGINE=vision` sends each page image to the configured LLM and asks for a line-by-line **transcription** with a confidence per line - not for the invoice fields directly. The transcript becomes the source text, so every extracted value still has to be quoted from text the reviewer can see and hallucinations are still caught by `evidence_not_found`. Real results are in [Real models via OpenRouter](#real-models-via-openrouter-free-tier).

## Learning from reviewer corrections

When a reviewer approves an invoice, every field is stored as a `FieldReview`: the value the model extracted (from the audit trail), the value approved, whether it was corrected and where the approved value is printed. Two things are built on that:

1. **Few-shot examples.** Before extraction, the vendor is recognised in the raw text (printed tax ID, else whole-word name/alias). Up to `FEWSHOT_MAX_EXAMPLES` verified documents of that vendor - corrected ones first, then the most recent - are added to the prompt within `FEWSHOT_MAX_CHARS`, each as the verified values, the line they are printed on, and `reviewer_corrected_from` for fields the model got wrong. Every extraction run records which examples it used.
2. **Accuracy tracking** - share of fields accepted unchanged, per field, per vendor and per reviewed invoice over time, at `/review/accuracy/`, `GET /api/accuracy/` and `manage.py accuracy_report [--rebuild]`.

The loop, end to end (`manage.py demo_feedback_loop`; real output with `LLM_PROVIDER=fake`). The Haverford invoices label their number "Document no." and their date "Tax point", which the fake extractor does not know; worse, it confidently reads the tax as `2026.00` from "Tax point: 06/03/2026". The reviewer fixes three fields on the March invoice and approves; the April invoice from the same vendor is then extracted with March as a verified example:

```text
haverford_invoice_march.pdf: reviewer corrected invoice_number, issue_date, tax; approved
haverford_invoice_april.pdf: approved (few-shot examples used: document #9)
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Field          ┃ haverford_invoice_march.pdf (no examples) ┃ haverford_invoice_april.pdf (with examples) ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ vendor_name    │ Haverford Office Interiors Ltd  0.90  ok  │ Haverford Office Interiors Ltd  0.90  ok    │
│ vendor_tax_id  │ GB318447625  0.95  ok                     │ GB318447625  0.95  ok                       │
│ invoice_number │ (not found)  0.90  wrong                  │ HOI-7802  0.90  ok                          │
│ issue_date     │ (not found)  0.90  wrong                  │ 2026-04-03  0.90  ok                        │
│ due_date       │ 2026-04-05  0.95  ok                      │ 2026-05-03  0.95  ok                        │
│ currency       │ GBP  0.95  ok                             │ GBP  0.95  ok                               │
│ subtotal       │ 1420.00  0.95  ok                         │ 681.00  0.95  ok                            │
│ tax            │ 2026.00  0.95  wrong                      │ 136.20  0.90  ok                            │
│ total          │ 1704.00  0.95  ok                         │ 817.20  0.95  ok                            │
└────────────────┴───────────────────────────────────────────┴─────────────────────────────────────────────┘
```

March: 6/9 fields right, needs review (missing invoice number and date, totals mismatch). April: 9/9, auto-approved. The fake provider honours examples deterministically - it learns the label printed before each verified value ("Document no.", "Tax point", "VAT 20%") and reads the same label in the new document; corrected fields override its own confident reading. A real model gets the same prompt block.

![Accuracy dashboard: headline accuracy, accuracy per reviewed invoice with the cumulative line, per-field and per-vendor tables, recent corrections](docs/accuracy.png)

The dashboard above is the Docker demo after `seed_demo` (which runs the loop) plus three decisions in the UI: Pinecrest approved after correcting the vendor name, the Copperleaf photo approved unchanged, Bluepeak and the duplicate rejected (rejections are not counted, nor are the five auto-approved invoices).

## Real models via OpenRouter (free tier)

Everything above uses the offline `fake` provider. To see what a real model does with the same documents, the pipeline was run through [OpenRouter](https://openrouter.ai) on **2026-09-29** with `LLM_PROVIDER=openrouter`, model **`dots-studio/dots-3-note-preview:free`** (fallbacks `google/gemma-4-31b-it:free`, `qwen/qwen3.8-27b:free` configured; they were never needed - every response came from dots), `LLM_MIN_INTERVAL_SECONDS=3`, `LLM_MAX_RETRIES=4`, Tesseract OCR for the scans:

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┓
┃ File                                    ┃ Status       ┃ Vendor                         ┃ Invoice #     ┃        Total ┃ Total EUR ┃ Issues (E/W) ┃ Min conf. ┃     Time ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━┩
│ 01_brightline_software_invoice.pdf      │ approved     │ Brightline Software GmbH       │ BLS-2026-0142 │ 1,592.22 EUR │  1,592.22 │     0/0      │      1.00 │ 64227 ms │
│ 02_northwind_office_supplies.pdf        │ approved     │ Northwind Traders Limited      │ NWT-58311     │   656.40 GBP │    768.62 │     0/0      │      1.00 │ 38411 ms │
│ 03_bluepeak_consulting_invoice.pdf      │ needs_review │ Bluepeak Consulting LLC        │ BPC-1007      │ 4,500.00 USD │  4,147.47 │     1/0      │      1.00 │ 30416 ms │
│ 04_kestrel_freight_email.eml            │ approved     │ Kestrel Freight Logistics B.V. │ KFL-88412     │ 1,158.58 EUR │  1,158.58 │     0/1      │      0.99 │ 31494 ms │
│ 05_pinecrest_hardware_receipt.txt       │ approved     │ PINECREST HARDWARE             │ PH-88213      │   427.98 USD │    394.45 │     0/1      │      1.00 │ 42656 ms │
│ 06_brightline_software_invoice_copy.pdf │ needs_review │ Brightline Software GmbH       │ BLS-2026-0142 │ 1,592.22 EUR │  1,592.22 │     1/0      │      1.00 │ 38346 ms │
│ 07_lantern_print_scan.pdf               │ needs_review │ Lantern Print Studio GmbH      │ LPS-24-0918   │       639.03 │         - │     1/0      │      0.89 │ 31408 ms │
│ 08_copperleaf_catering_photo.jpg        │ needs_review │ COPPERLEAF CATERING LTD        │ CC-20931      │   202.20 GBP │    236.77 │     0/1      │      0.35 │ 41665 ms │
└─────────────────────────────────────────┴──────────────┴────────────────────────────────┴───────────────┴──────────────┴───────────┴──────────────┴───────────┴──────────┘
Processed 8 document(s) in 318.63s: 4 approved, 4 needs_review
```

8 documents, 8 LLM calls, all valid on the first attempt, 30-64 s each on the free tier. Evidence for every field was found in the source text. Where the real model differs from the fake one:

| Sample | Real model | Pipeline outcome |
|---|---|---|
| 01, 02, 04 | all fields correct | approved (as with the fake) |
| 03 | correct; the document itself does not add up | review: `line_items_sum_mismatch` (as with the fake) |
| 06 | correct | review: `duplicate_invoice` |
| 05 till receipt | correct values, but **confidence 1.00** for the unlabeled "Ref PH-88213" and an all-caps shop name | **auto-approved** - the fake sends it to review. Self-reported confidence is not calibrated; `REVIEW_NEW_VENDORS=true` is the safety net for first-time vendors. |
| 07 scan | quoted `Total due (EUR)` as evidence for the currency but **left out the `value` key** | review: `missing_required_field: currency` |
| 08 photo | recovered the "Coffee service" line despite the OCR misreading its quantity (returned quantity `null`, amount 35.00) - better than the fake | review: OCR-capped confidence `line_items.0=0.42, line_items.2=0.35` |

The 07 miss led to a fix: `Extracted.value` is now required (but nullable), so an omitted key is a validation error that goes back to the model instead of silently meaning "not on the document". Re-running 07 afterwards: approved, currency EUR - on the first attempt, so that rerun did not exercise the new check (a unit test does).

Extracted record for the supplier email (raw model output, real, abridged to one line item; compare with the fake's in [Example](#example) - the only difference is the model quoting the net-amount line for the currency):

```json
{
  "vendor_name":    {"value": "Kestrel Freight Logistics B.V.", "confidence": 0.99,
                     "evidence": {"quote": "Supplier: Kestrel Freight Logistics B.V.", "page": 1}},
  "vendor_tax_id":  {"value": "NL853746291B01", "confidence": 0.99,
                     "evidence": {"quote": "VAT number: NL853746291B01", "page": 1}},
  "invoice_number": {"value": "KFL-88412", "confidence": 0.99,
                     "evidence": {"quote": "Invoice number: KFL-88412", "page": 1}},
  "issue_date":     {"value": "2026-03-12", "confidence": 0.99,
                     "evidence": {"quote": "Invoice date: 12.03.2026", "page": 1}},
  "due_date":       {"value": null, "confidence": 0.99, "evidence": null},
  "currency":       {"value": "EUR", "confidence": 0.99,
                     "evidence": {"quote": "Net amount:    957,50 EUR", "page": 1}},
  "subtotal":       {"value": 957.5, "confidence": 0.99,
                     "evidence": {"quote": "Net amount:    957,50 EUR", "page": 1}},
  "tax":            {"value": 201.08, "confidence": 0.99,
                     "evidence": {"quote": "VAT 21%:       201,08 EUR", "page": 1}},
  "total":          {"value": 1158.58, "confidence": 0.99,
                     "evidence": {"quote": "Total amount:  1.158,58 EUR", "page": 1}},
  "line_items": [
    {"description": "Pallet transport Rotterdam - Berlin", "quantity": 4, "unit_price": 210.0,
     "amount": 840.0, "confidence": 0.99,
     "evidence": {"quote": "Pallet transport Rotterdam - Berlin     4     210,00     840,00", "page": 1}}
  ]
}
```

**Vision OCR on the scans** (`OCR_ENGINE=vision`, same model, one transcription call per page plus one extraction call). Transcript of the scanned PDF as returned (column spacing is the model's):

```text
Lantern Print Studio GmbH                             INVOICE
Hafenstrasse 8, 20359 Hamburg, Germany
VAT ID: DE274839165

Invoice No:    LPS-24-0918                  Bill to:
Invoice Date:  2026-03-09                  Acme Analytics GmbH
Due Date:      2026-04-08                  Accounts Payable
                                              Torstrasse 45, 10119 Berlin

Description    Qty    Unit price    Amount
Business cards, 400gsm (500 pcs)    3    45.00    135.00
Roll-up banner 85 x 200 cm    2    119.00    238.00
Flyers A5, 135gsm (2000 pcs)    1    164.00    164.00

Subtotal                                             537.00
VAT 19%                                              102.03
Total due (EUR)                                      639.03

Payment by bank transfer within 30 days. Thank you for your order.
```

Both scans were transcribed character-perfect - including the photo's `Coffee service    1    35.00    35.00` and `**** 8812` that Tesseract misread - and both were **approved with all fields correct** (07: 1,418 input / 3,307 output tokens for the transcription; ~75 s per document in total). The trade-off: the model reported 0.99 for every line, so unlike Tesseract it gives the router no signal when it is unsure, and there are no word boxes. Tesseract stays the default (local, free, fast, calibrated per word); `vision` is the better reader for bad photos.

**Feedback loop with the real model.** The model read the March Haverford invoice correctly without help ("Document no." and "Tax point" are no obstacle for an LLM), so it was auto-approved and there was nothing to learn. With `REVIEW_NEW_VENDORS=true` the first Haverford invoice goes to a reviewer, is approved unchanged and becomes an example; April was then extracted with it in the prompt (`few-shot examples used: document #1`), 9/9 fields correct. The measurable before/after effect of a correction is shown with the fake provider above; with this model and these samples there was no error to correct.

24 real requests were used in total (including 7 request-shape probes). One finding from them is now in the code: the provider behind the dots model rejects Pydantic's JSON Schema (`$ref`, `pattern`, `format`...) with a bare `400 bad request`, so OpenAI-compatible providers get a portable schema (references inlined, value constraints dropped - Pydantic still validates the full schema).

## Quick start

### Docker (zero keys)

```bash
git clone <this repo> && cd invoice-extraction-django
docker compose up --build
```

This starts `web` (Django + gunicorn), `worker` (Celery), `db` (PostgreSQL 16) and `redis`; the image includes Tesseract. On start the web container runs migrations and `seed_demo --with-samples`, which creates three demo vendors, a `demo` superuser, processes everything in `sample_data/` (including the two scans) and runs the reviewer-correction demo, so the accuracy dashboard has data.

- Review UI: <http://localhost:8000> - log in as **demo / demo** (set `DEMO_USER_PASSWORD` to change it)
- API docs (Swagger): <http://localhost:8000/api/docs/>
- Accuracy dashboard: <http://localhost:8000/review/accuracy/>
- Django admin: <http://localhost:8000/admin/>

### Local with uv

```bash
sudo apt install tesseract-ocr   # or brew install tesseract; only needed for scans and photos
uv sync
make dev          # migrate, seed demo data + samples, runserver on :8000 (Celery runs inline)
make test         # 268 tests, offline (OCR tests skip without the tesseract binary)
make demo         # batch-process sample_data/ and print the summary table
make feedback     # the reviewer-correction loop, then the accuracy report
```

SQLite and eager Celery are the defaults, so no services are needed. Without Tesseract, scans fail with a clear message (or set `OCR_ENGINE=vision` / `none`). To use a real worker locally: start Redis, set `CELERY_TASK_ALWAYS_EAGER=false` and run `make worker`.

### Using real models

```bash
# Claude (default model claude-sonnet-5, via structured outputs)
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... docker compose up

# OpenAI (model configurable)
LLM_PROVIDER=openai OPENAI_API_KEY=sk-... OPENAI_MODEL=gpt-5.4-mini docker compose up

# Vision OCR instead of Tesseract (any of the above providers)
OCR_ENGINE=vision LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... docker compose up
```

Or put the variables in `.env` (see `.env.example`). With a real model, `LINE_ITEM_CATEGORIZER=llm` also categorizes line items with one extra call per invoice (keyword rules remain the fallback).

### Run with free models via OpenRouter

[OpenRouter](https://openrouter.ai) exposes many models behind one OpenAI-compatible API, including rate-limited `:free` ones - enough to try the pipeline on real models at no cost:

```bash
export OPENROUTER_API_KEY=sk-or-...           # from openrouter.ai/keys
LLM_PROVIDER=openrouter \
OPENROUTER_MODEL=dots-studio/dots-3-note-preview:free \
OPENROUTER_FALLBACK_MODELS=google/gemma-4-31b-it:free,qwen/qwen3.8-27b:free \
LLM_MIN_INTERVAL_SECONDS=3 LLM_MAX_RETRIES=4 \
docker compose up
```

`OPENROUTER_FALLBACK_MODELS` is sent as OpenRouter's `models` list (tried in order when the primary is rate-limited or down; the model that answered is stored with each run), `LLM_MAX_RETRIES` makes the SDK retry 429/5xx with exponential backoff, and `LLM_MIN_INTERVAL_SECONDS` spaces requests out for free-tier limits (~20 requests/minute). `OPENROUTER_SITE_URL` / `OPENROUTER_APP_NAME` set the optional attribution headers. Free models come and go and are often rate-limited upstream; the three above supported structured outputs and image input on 2026-09-29.

Note: no Anthropic or OpenAI keys were available while building this; the Claude and OpenAI adapters - including image input for vision OCR - are unit-tested against stubbed SDK clients (request shape, refusal/truncation handling), not against the live APIs. The real-model results above come from OpenRouter free models. All other numbers and outputs in this README come from the offline `fake` provider, which is a regex/layout heuristic tuned for the sample documents - a test double, not a production extractor.

## Validation rules

| Check | Severity | Description |
|---|---|---|
| `missing_required_field` | error | vendor name, invoice number, issue date, currency or total not found |
| `line_items_sum_mismatch` | error | sum of line amounts differs from the subtotal (or total - tax) by more than `AMOUNT_TOLERANCE` |
| `totals_mismatch` | error | subtotal + tax differs from the total |
| `due_before_issue` | error | due date earlier than issue date |
| `invalid_currency` | error | currency is not an ISO 4217 code |
| `duplicate_invoice` | error | an earlier, non-rejected invoice has the same number from the same vendor (tax ID, else fuzzy name) |
| `line_item_amount_mismatch` | warning | quantity x unit price differs from the line amount |
| `missing_due_date` | warning | no due date on the document |
| `implausible_issue_date` | warning | issue date before 2000 or more than 30 days in the future |
| `invalid_tax_id_format` | warning | tax ID does not match the format of its country (DE, GB, FR, NL, ES, IT VAT; US EIN) |
| `evidence_not_found` | warning | the quote the model gave as evidence does not occur in the document (possible hallucination) |
| `no_line_items` | warning | no line items extracted |
| `fx_rate_unavailable` | warning | total could not be converted to the base currency |
| `low_ocr_confidence` | warning (routes to review) | mean OCR word confidence of a page below `OCR_MIN_CONFIDENCE` - a poor scan |
| `ocr_text` | info | the text came from OCR; field confidences are capped by the confidence of the words they were read from |
| `missing_tax_id` / `unrecognized_tax_id` | info | no tax ID, or a format the validator does not know |
| `new_vendor` | info | vendor not in master data (created on approval) |

Date parsing itself is enforced by the schema: an unparseable date fails Pydantic validation and triggers the retry-with-feedback loop.

### Adding your own checks

Checks are plain functions `(invoice, context) -> Iterable[Issue]`:

```python
from extraction.issues import Issue, Severity
from extraction.pipeline import PipelineConfig
from extraction.validate import DEFAULT_CHECKS


def check_round_totals(invoice, ctx):
    if invoice.total.value and invoice.total.value % 1000 == 0:
        yield Issue("round_total", Severity.WARNING, "Suspiciously round total", "total")


config = PipelineConfig(checks=(*DEFAULT_CHECKS, check_round_totals))
```

### Adding your own schema (purchase orders, resumes, contracts, ...)

The extraction stage is generic over any Pydantic model; wrap fields in `Extracted[...]` to get confidence and evidence for free:

```python
from datetime import date
from decimal import Decimal
from pathlib import Path
from pydantic import BaseModel
from extraction.extract import extract_structured
from extraction.llm import build_provider
from extraction.parse import parse_document
from extraction.schemas import Extracted


class PurchaseOrder(BaseModel):
    buyer: Extracted[str]
    po_number: Extracted[str]
    delivery_date: Extracted[date]
    total: Extracted[Decimal]


document = parse_document("po.pdf", Path("po.pdf").read_bytes())
result = extract_structured(document, PurchaseOrder, build_provider("anthropic"), max_attempts=3)
print(result.data, result.attempts)
```

Retry-with-feedback, evidence resolution (`extraction.evidence.locate`) and token/latency accounting work unchanged. The `fake` provider only understands the invoice schema, and the Django models, review UI and API are invoice-shaped - a new document type needs its own model/serializer/template in the Django app.

## Configuration

All settings come from environment variables (`.env` is read if present). Full, commented list in [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `fake` | `fake`, `anthropic`, `openai` or `openrouter` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | - / `claude-sonnet-5` | Claude credentials and model |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | - / `gpt-5.4-mini` | OpenAI credentials and model |
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | - / `dots-studio/dots-3-note-preview:free` | OpenRouter credentials and model |
| `OPENROUTER_FALLBACK_MODELS` | - | comma-separated models OpenRouter tries when the primary is rate-limited or down |
| `OPENROUTER_SITE_URL` / `OPENROUTER_APP_NAME` | - / `DocExtract` | optional attribution headers (`HTTP-Referer`, `X-Title`) |
| `LLM_MAX_ATTEMPTS` | `3` | attempts per document when output fails validation |
| `LLM_MAX_RETRIES` | `2` | SDK retries on 429 / 5xx / connection errors (exponential backoff) |
| `LLM_MIN_INTERVAL_SECONDS` | `0` | minimum gap between LLM requests per process (~3 for free tiers) |
| `LLM_TIMEOUT_SECONDS` | `120` | request timeout |
| `OCR_ENGINE` | `tesseract` | `tesseract`, `vision` (the LLM transcribes page images; needs `anthropic`, `openai` or `openrouter`) or `none` |
| `OCR_LANGUAGES` | `eng` | Tesseract languages, e.g. `eng+deu` (install `tesseract-ocr-deu`; Docker: `--build-arg OCR_LANGUAGE_PACKS="eng deu"`) |
| `OCR_DPI` | `300` | resolution PDF pages without a text layer are rendered at |
| `OCR_MIN_CONFIDENCE` | `0.65` | pages with a lower mean word confidence are flagged and sent to review |
| `FEWSHOT_MAX_EXAMPLES` | `3` | verified documents of the same vendor added to the prompt (`0` = off) |
| `FEWSHOT_MAX_CHARS` | `4000` | character budget for those examples |
| `REVIEW_CONFIDENCE_THRESHOLD` | `0.75` | fields/line items below this go to review |
| `REVIEW_ON_WARNINGS` | `false` | also route warnings to review |
| `REVIEW_NEW_VENDORS` | `false` | always review the first invoice from an unknown vendor |
| `AMOUNT_TOLERANCE` | `0.02` | absolute tolerance for sum checks |
| `VENDOR_MATCH_THRESHOLD` | `85` | rapidfuzz score (0-100) to link to an existing vendor |
| `BASE_CURRENCY` | `EUR` | currency totals are converted to |
| `FX_PROVIDER` / `FX_RATES_FILE` / `FX_API_URL` | `static` / bundled / Frankfurter | `static` JSON table or `frankfurter` (live ECB rates, no key) |
| `LINE_ITEM_CATEGORIZER` | `keyword` | `keyword` or `llm` |
| `DATABASE_URL` | SQLite | e.g. `postgres://user:pass@host:5432/db` |
| `CELERY_BROKER_URL` / `CELERY_TASK_ALWAYS_EAGER` | Redis / `true` | background processing |
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | dev values | standard Django settings |
| `MEDIA_ROOT`, `MAX_UPLOAD_MB`, `API_PAGE_SIZE`, `TIME_ZONE` | | storage and API limits |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `console` | structlog output, `json` for log shippers |
| `DEMO_USER_PASSWORD` | - | `seed_demo` creates a `demo` superuser when set |

## API

Authentication: token (`Authorization: Token <key>`) or session. Interactive docs at `/api/docs/`, schema at `/api/schema/`.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/documents/` | upload a document (multipart field `file`: PDF, image, `.txt`, `.eml`); returns `202` and queues processing |
| `GET` | `/api/documents/?status=` | list documents |
| `GET` | `/api/documents/{id}/` | status, routing reasons, invoice record, issues, extraction runs |
| `POST` | `/api/documents/{id}/reprocess/` | re-run the pipeline for failed / rejected / in-review documents |
| `GET` | `/api/invoices/?status=&vendor=&currency=&issued_from=&issued_to=` | list and filter invoices |
| `GET` | `/api/invoices/{id}/` | one invoice with line items, confidence and evidence |
| `POST` | `/api/invoices/{id}/approve/` / `reject/` | review decision (optional `note`); `409` if not awaiting review |
| `GET` | `/api/invoices/export.csv` / `export.json` | download; `status=approved` by default, `status=all` for everything |
| `GET` | `/api/accuracy/?vendor=` | fields accepted unchanged: overall, per field, per vendor, per reviewed invoice over time |
| `GET` | `/api/corrections/?vendor=&field=` | reviewer corrections (extracted vs. approved value, where it is printed) |

```bash
TOKEN=$(docker compose exec web python manage.py drf_create_token demo | awk '{print $3}')

curl -H "Authorization: Token $TOKEN" \
     -F "file=@sample_data/02_northwind_office_supplies.pdf" \
     http://localhost:8000/api/documents/
# {"id": 7, "status": "uploaded", "url": "http://localhost:8000/api/documents/7/", ...}

curl -H "Authorization: Token $TOKEN" http://localhost:8000/api/documents/7/
curl -H "Authorization: Token $TOKEN" -o invoices.csv http://localhost:8000/api/invoices/export.csv
```

## Project structure

```text
extraction/                 framework-agnostic pipeline core (no Django imports)
  schemas.py                Pydantic Invoice schema, Extracted[T] = value + confidence + evidence
  parse.py                  PDF (pypdf, layout mode) / image / text / email -> page-aware text
  ocr/                      OcrEngine interface, tesseract.py, vision.py, layout.py, images.py
  extract.py                LLM call + validation-error feedback loop, few-shot examples
  evidence.py               evidence quotes -> spans, pages and OCR confidence
  validate.py               deterministic business-rule checks
  enrich/                   vendors.py (rapidfuzz), fx.py (static / Frankfurter), categorize.py
  route.py                  approve vs review decision
  status.py                 document status machine
  pipeline.py               InvoicePipeline: stages one by one, or run() end to end
  llm/                      provider interface + anthropic / openai / openrouter / fake
documents/                  Django app wiring the core to the database
  models.py                 Document, ExtractionRun, Invoice, LineItem, Vendor, ValidationIssue,
                            FieldReview
  services.py               per-stage persistence, review actions, field edits + revalidation
  feedback.py               record reviewer decisions, pick per-vendor few-shot examples
  accuracy.py, charts.py    accuracy aggregates and the dashboard's inline SVG
  tasks.py                  Celery task, enqueued on transaction commit
  views.py, templates/      review UI (htmx + Pico.css)
  api/                      DRF serializers, viewsets, export
  management/commands/      process_folder, seed_demo, demo_feedback_loop, accuracy_report
config/                     settings (django-environ, structlog), urls, celery
sample_data/                eight fictional documents incl. a scan and a photo, feedback_loop/
                            (all generated by scripts/make_samples.py)
tests/                      unit tests for the core, integration tests for pipeline, API and UI
docker/, Dockerfile, docker-compose.yml, .github/workflows/ci.yml, Makefile
```

## Key design decisions

- **Deterministic validation after LLM extraction.** The model is good at reading messy layouts; arithmetic, date logic and duplicate checks are better done by code that is exact, explainable and unit-tested. Each rule's result is visible to the reviewer.
- **Evidence spans for trust.** The model returns a verbatim quote, not character offsets (which LLMs get wrong); the pipeline locates the quote itself. Reviewers see every value highlighted in the source, and a quote that cannot be found is flagged as possible hallucination.
- **Confidence-based routing.** Humans only look at records with an error or an uncertain field. The threshold, warning handling and new-vendor policy are configuration, not code.
- **Retry with error feedback.** When output does not match the schema, the model gets its own answer back plus the exact Pydantic errors and tries again. Transport errors stop immediately instead of burning retries. Every attempt is logged with tokens and latency.
- **Framework-agnostic core.** `extraction/` runs in a script, notebook, Lambda or another web framework; Django adds persistence, the queue UI and the API. The core is typed and checked with `mypy --strict`.
- **Master data changes only on approval.** Unknown vendors, new aliases and learned tax IDs are written to the vendor table only when an invoice is approved (automatically or by a person), so a bad extraction cannot pollute reference data.
- **Offline fake provider.** A deterministic stand-in LLM makes the demo, CI and 260+ tests reproducible and free.
- **OCR behind an interface.** An engine only has to turn a page image into text plus word offsets and confidences; everything after that - extraction, evidence, validation, the review UI - is the same code for born-digital and scanned documents. Tesseract (local, free, per-word confidence) and a vision LLM are interchangeable by configuration, and adding a cloud OCR service is one class. Only pages without a usable text layer are OCRed, and the word confidences flow into routing: a field's confidence is capped at the lowest confidence of the words it was read from, and a poor page sends the document to review.
- **Vision models transcribe, they do not extract.** Letting the vision model fill the invoice fields directly would save a call, but there would be no text to check its quotes against. As a transcriber, its output is shown to the reviewer and every extracted value must still be found in it.
- **Corrections are used carefully.** Only invoices a person approved become examples - auto-approved ones were never checked, rejected ones are wrong. Examples come from the same vendor only (recognised by printed tax ID or name before extraction), are capped in count and characters, sit in their own delimited block after the stable instructions, and are labelled as other documents whose values must not be copied. Nothing is trusted because it came from an example: a value copied from one would not be found in the current document (`evidence_not_found`), and a repeated invoice number hits the duplicate check. Every run stores which examples it saw, and accuracy is measured only on human-reviewed fields.
- **Portable JSON Schema for OpenAI-compatible gateways.** Pydantic's schema (`$ref`, `pattern`, `format`) is rejected by some providers behind OpenRouter; they get a flattened schema without value constraints, and the full Pydantic model still validates every response.

## Batch processing results

`python manage.py process_folder sample_data/` on a fresh database seeded with the three demo vendors (`LLM_PROVIDER=fake`, Tesseract 5.5 for the scans):

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┓
┃ File                                    ┃ Status       ┃ Vendor                         ┃ Invoice #     ┃        Total ┃ Total EUR ┃ Issues (E/W) ┃ Min conf. ┃   Time ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━┩
│ 01_brightline_software_invoice.pdf      │ approved     │ Brightline Software GmbH       │ BLS-2026-0142 │ 1,592.22 EUR │  1,592.22 │     0/0      │      0.90 │  28 ms │
│ 02_northwind_office_supplies.pdf        │ approved     │ Northwind Traders Limited      │ NWT-58311     │   656.40 GBP │    768.62 │     0/0      │      0.90 │  14 ms │
│ 03_bluepeak_consulting_invoice.pdf      │ needs_review │ Bluepeak Consulting LLC        │ BPC-1007      │ 4,500.00 USD │  4,147.47 │     1/0      │      0.90 │  12 ms │
│ 04_kestrel_freight_email.eml            │ approved     │ Kestrel Freight Logistics B.V. │ KFL-88412     │ 1,158.58 EUR │  1,158.58 │     0/1      │      0.90 │  12 ms │
│ 05_pinecrest_hardware_receipt.txt       │ needs_review │ PINECREST HARDWARE             │ PH-88213      │   427.98 USD │    394.45 │     0/1      │      0.55 │  10 ms │
│ 06_brightline_software_invoice_copy.pdf │ needs_review │ Brightline Software GmbH       │ BLS-2026-0142 │ 1,592.22 EUR │  1,592.22 │     1/0      │      0.90 │  12 ms │
│ 07_lantern_print_scan.pdf               │ approved     │ Lantern Print Studio GmbH      │ LPS-24-0918   │   639.03 EUR │    639.03 │     0/0      │      0.89 │ 635 ms │
│ 08_copperleaf_catering_photo.jpg        │ needs_review │ COPPERLEAF CATERING LTD        │ CC-20931      │   202.20 GBP │    236.77 │     1/1      │      0.42 │ 210 ms │
└─────────────────────────────────────────┴──────────────┴────────────────────────────────┴───────────────┴──────────────┴───────────┴──────────────┴───────────┴────────┘
Processed 8 document(s) in 0.94s: 4 approved, 4 needs_review
```

| Sample | What is tricky | Outcome |
|---|---|---|
| 01 | nothing - clean EUR invoice | approved |
| 02 | vendor printed as "Northwind Traders Ltd.", master record is "Northwind Traders Limited"; GBP | fuzzy-matched, converted to EUR, approved |
| 03 | line items sum to 4,200 but subtotal says 4,500 | review: `line_items_sum_mismatch` |
| 04 | supplier email, `1.158,58` number format, no due date | approved with a warning |
| 05 | till receipt, no legal vendor name, unlabeled "Ref" number | review: confidence 0.55 / 0.60 |
| 06 | re-sent copy of invoice 01 | review: `duplicate_invoice` |
| 07 | scanned invoice: no text layer, skewed, noisy | OCR, confidences capped by word confidence (0.89-0.92), approved |
| 08 | phone photo of a faded receipt; OCR misreads a quantity | review: `line_items_sum_mismatch`, `line_items.0=0.42` |

The same run with a real model is in [Real models via OpenRouter](#real-models-via-openrouter-free-tier).

The queue after this run:

![Review queue](docs/queue.png)

## Testing

```bash
make test    # or: uv run pytest
make lint    # ruff check, ruff format --check, mypy --strict on extraction/
```

268 tests, no API keys or services required (fake provider, SQLite, eager Celery):

- **Unit (216)** - every validator with parametrized edge cases, amount/date/company-name normalization, PDF/email/text parsing, evidence location, vendor fuzzy matching, FX conversion (static table and Frankfurter via `httpx.MockTransport`), keyword and LLM categorization with fallback, the retry loop (scripted fake LLM that fails first and succeeds on the next attempt, error feedback content, max attempts, transport errors), routing thresholds, status transitions; OCR layout assembly, Tesseract output parsing and error mapping (binary stubbed), vision OCR (LLM stubbed), scanned/mixed PDFs, multi-page TIFF, image attachments, OCR confidence capping; few-shot rendering and budget, vendor detection in raw text, the fake provider learning labels; the Anthropic/OpenAI/OpenRouter adapters against stubbed clients (structured output, image blocks, fallback models, headers, throttling, portable schema).
- **Integration (52)** - all sample documents end to end with asserted routing, fuzzy vendor linking, duplicate detection, field edits that resolve issues, approval side effects, reprocessing, `process_folder`/`seed_demo`; real Tesseract on the scan and the photo (pipeline, persisted word boxes, review page); the correction loop (field reviews, examples in the next extraction, accuracy report, dashboard, API, commands); REST API (auth, upload, filters, approve/reject, CSV/JSON export, OpenAPI); review UI (queue filters, htmx partials, inline editing, decisions, uncertain-word highlighting).

Tests that need the `tesseract` binary are marked `requires_tesseract` and skip without it; CI installs Tesseract and sets `REQUIRE_TESSERACT=true`, which turns a missing binary into a failure instead of silent skips.

GitHub Actions (`.github/workflows/ci.yml`) runs `lint`, `test` (with Tesseract, plus a missing-migrations check) and `build` (Docker image).

## Roadmap

- More schemas out of the box: purchase orders, credit notes, delivery notes, receipts with per-line tax.
- Integrations: push approved invoices to QuickBooks, Xero or an ERP; ingest from a mailbox (IMAP) or cloud storage.
- Live VAT number verification (EU VIES, HMRC) instead of format checks.
- PDF viewer with bounding-box highlights in addition to text highlights (OCR word boxes are already stored).

## License

[MIT](LICENSE) - Copyright (c) 2026 Ivan Savchenko. All companies, people and tax IDs in `sample_data/` are fictional.

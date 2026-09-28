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

- An LLM reads each document and fills in a fixed form (vendor, invoice number, dates, amounts, line items).
- Plain business rules then check the result: do the lines add up, is the total right, is this a duplicate, is the tax ID plausible.
- Clean records are approved automatically. Anything with an error or a low-confidence field lands in a review queue, where a person sees the extracted values **next to the highlighted text they came from**, fixes what is wrong and approves or rejects.
- Approved data can be exported (CSV/JSON) or pulled by other systems through a REST API.

## Features

- **Multi-format intake** - text-layer PDFs (page-aware), plain text and `.eml` emails including PDF/text attachments.
- **LLM structured extraction** - Pydantic schema with a value, confidence and verbatim evidence quote per field; invalid output is sent back to the model with the exact validation errors (up to `LLM_MAX_ATTEMPTS`).
- **Provider abstraction** - `anthropic` (Claude, structured outputs), `openai` (JSON-schema response format) or `fake`, a deterministic offline extractor so the demo and the tests need **zero API keys**.
- **Deterministic validation** - line-item sums, subtotal + tax = total, date sanity, ISO 4217 currency, tax ID formats, duplicate detection, "quote not found in document" - each a small, pluggable function producing a structured issue with a severity.
- **Enrichment** - fuzzy vendor matching (rapidfuzz) against vendor master data, currency conversion to a base currency through a pluggable FX provider (bundled static table or live ECB rates), expense categories per line item (keyword rules or LLM).
- **Confidence-based routing** - errors or any field below `REVIEW_CONFIDENCE_THRESHOLD` go to review; everything else is auto-approved.
- **Review UI** (Django templates + htmx + Pico.css) - filterable queue, side-by-side review page with evidence highlighting, edit-in-place that re-runs validation instantly, approve/reject with an audit note, "approve and go to next".
- **REST API** (Django REST Framework + OpenAPI/Swagger) - upload, poll status, list/filter, approve/reject, CSV/JSON export.
- **Audit trail** - every LLM run is stored with raw output, attempts, per-attempt errors, tokens and latency; reviewer, time and note are stored on decisions.
- **Background processing** - Celery + Redis in Docker; eager (inline) mode for local runs and tests. Batch import with `manage.py process_folder`.
- **Framework-agnostic core** - the `extraction/` package is plain, typed Python (`mypy --strict` clean) with no Django imports.

## Architecture

```mermaid
flowchart LR
    A[PDF / TXT / EML] --> P[Parse<br/>pypdf, email]
    P --> X[Extract<br/>LLM + Pydantic schema]
    X -- "invalid JSON / schema errors<br/>(fed back, up to N attempts)" --> X
    X --> V[Validate<br/>deterministic rules]
    V --> E[Enrich<br/>vendor match, FX, categories]
    E --> R{Route}
    R -- "no errors and<br/>confidence >= threshold" --> OK[Approved]
    R -- "error issue or<br/>low confidence" --> Q[Review queue]
    Q -- "edit fields<br/>(re-validates)" --> Q
    Q --> OK
    Q --> NO[Rejected]
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

## Quick start

### Docker (zero keys)

```bash
git clone <this repo> && cd invoice-extraction-django
docker compose up --build
```

This starts `web` (Django + gunicorn), `worker` (Celery), `db` (PostgreSQL 16) and `redis`. On start the web container runs migrations and `seed_demo --with-samples`, which creates three demo vendors, a `demo` superuser and processes everything in `sample_data/`.

- Review UI: <http://localhost:8000> - log in as **demo / demo** (set `DEMO_USER_PASSWORD` to change it)
- API docs (Swagger): <http://localhost:8000/api/docs/>
- Django admin: <http://localhost:8000/admin/>

### Local with uv

```bash
uv sync
make dev          # migrate, seed demo data + samples, runserver on :8000 (Celery runs inline)
make test         # 209 tests, offline
make demo         # batch-process sample_data/ and print the summary table
```

SQLite and eager Celery are the defaults, so no services are needed. To use a real worker locally: start Redis, set `CELERY_TASK_ALWAYS_EAGER=false` and run `make worker`.

### Using real models

```bash
# Claude (default model claude-sonnet-5, via structured outputs)
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... docker compose up

# OpenAI (model configurable)
LLM_PROVIDER=openai OPENAI_API_KEY=sk-... OPENAI_MODEL=gpt-5.4-mini docker compose up
```

Or put the variables in `.env` (see `.env.example`). With a real model, `LINE_ITEM_CATEGORIZER=llm` also categorizes line items with one extra call per invoice (keyword rules remain the fallback).

Note: the Anthropic and OpenAI adapters are unit-tested against stubbed SDK clients (request shape, refusal/truncation handling). All numbers and outputs shown in this README come from the offline `fake` provider, which is a regex/layout heuristic tuned for the sample documents - it is a test double, not a production extractor.

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
| `LLM_PROVIDER` | `fake` | `fake`, `anthropic` or `openai` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | - / `claude-sonnet-5` | Claude credentials and model |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | - / `gpt-5.4-mini` | OpenAI credentials and model |
| `LLM_MAX_ATTEMPTS` | `3` | attempts per document when output fails validation |
| `LLM_TIMEOUT_SECONDS` | `120` | request timeout |
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
| `POST` | `/api/documents/` | upload a document (multipart field `file`); returns `202` and queues processing |
| `GET` | `/api/documents/?status=` | list documents |
| `GET` | `/api/documents/{id}/` | status, routing reasons, invoice record, issues, extraction runs |
| `POST` | `/api/documents/{id}/reprocess/` | re-run the pipeline for failed / rejected / in-review documents |
| `GET` | `/api/invoices/?status=&vendor=&currency=&issued_from=&issued_to=` | list and filter invoices |
| `GET` | `/api/invoices/{id}/` | one invoice with line items, confidence and evidence |
| `POST` | `/api/invoices/{id}/approve/` / `reject/` | review decision (optional `note`); `409` if not awaiting review |
| `GET` | `/api/invoices/export.csv` / `export.json` | download; `status=approved` by default, `status=all` for everything |

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
  parse.py                  PDF (pypdf, layout mode) / text / email -> page-aware text
  extract.py                LLM call + validation-error feedback loop, attempt accounting
  evidence.py               resolve evidence quotes to character spans and pages
  validate.py               deterministic business-rule checks
  enrich/                   vendors.py (rapidfuzz), fx.py (static / Frankfurter), categorize.py
  route.py                  approve vs review decision
  status.py                 document status machine
  pipeline.py               InvoicePipeline: stages one by one, or run() end to end
  llm/                      provider interface + anthropic / openai / fake implementations
documents/                  Django app wiring the core to the database
  models.py                 Document, ExtractionRun, Invoice, LineItem, Vendor, ValidationIssue
  services.py               per-stage persistence, review actions, field edits + revalidation
  tasks.py                  Celery task, enqueued on transaction commit
  views.py, templates/      review UI (htmx + Pico.css)
  api/                      DRF serializers, viewsets, export
  management/commands/      process_folder, seed_demo
config/                     settings (django-environ, structlog), urls, celery
sample_data/                six fictional documents (generated by scripts/make_samples.py)
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
- **Offline fake provider.** A deterministic stand-in LLM makes the demo, CI and 200+ tests reproducible and free.

## Batch processing results

`python manage.py process_folder sample_data/` on a fresh database seeded with the three demo vendors (`LLM_PROVIDER=fake`):

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┓
┃ File                                    ┃ Status       ┃ Vendor                         ┃ Invoice #     ┃        Total ┃ Total EUR ┃ Issues (E/W) ┃ Min conf. ┃  Time ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━┩
│ 01_brightline_software_invoice.pdf      │ approved     │ Brightline Software GmbH       │ BLS-2026-0142 │ 1,592.22 EUR │  1,592.22 │     0/0      │      0.90 │ 21 ms │
│ 02_northwind_office_supplies.pdf        │ approved     │ Northwind Traders Limited      │ NWT-58311     │   656.40 GBP │    768.62 │     0/0      │      0.90 │ 12 ms │
│ 03_bluepeak_consulting_invoice.pdf      │ needs_review │ Bluepeak Consulting LLC        │ BPC-1007      │ 4,500.00 USD │  4,147.47 │     1/0      │      0.90 │ 12 ms │
│ 04_kestrel_freight_email.eml            │ approved     │ Kestrel Freight Logistics B.V. │ KFL-88412     │ 1,158.58 EUR │  1,158.58 │     0/1      │      0.90 │ 10 ms │
│ 05_pinecrest_hardware_receipt.txt       │ needs_review │ PINECREST HARDWARE             │ PH-88213      │   427.98 USD │    394.45 │     0/1      │      0.55 │  9 ms │
│ 06_brightline_software_invoice_copy.pdf │ needs_review │ Brightline Software GmbH       │ BLS-2026-0142 │ 1,592.22 EUR │  1,592.22 │     1/0      │      0.90 │ 12 ms │
└─────────────────────────────────────────┴──────────────┴────────────────────────────────┴───────────────┴──────────────┴───────────┴──────────────┴───────────┴───────┘
Processed 6 document(s) in 0.08s: 3 approved, 3 needs_review
```

| Sample | What is tricky | Outcome |
|---|---|---|
| 01 | nothing - clean EUR invoice | approved |
| 02 | vendor printed as "Northwind Traders Ltd.", master record is "Northwind Traders Limited"; GBP | fuzzy-matched, converted to EUR, approved |
| 03 | line items sum to 4,200 but subtotal says 4,500 | review: `line_items_sum_mismatch` |
| 04 | supplier email, `1.158,58` number format, no due date | approved with a warning |
| 05 | till receipt, no legal vendor name, unlabeled "Ref" number | review: confidence 0.55 / 0.60 |
| 06 | re-sent copy of invoice 01 | review: `duplicate_invoice` |

The queue after this run:

![Review queue](docs/queue.png)

## Testing

```bash
make test    # or: uv run pytest
make lint    # ruff check, ruff format --check, mypy --strict on extraction/
```

209 tests, no API keys or services required (fake provider, SQLite, eager Celery):

- **Unit (173)** - every validator with parametrized edge cases, amount/date/company-name normalization, PDF/email/text parsing, evidence location, vendor fuzzy matching, FX conversion (static table and Frankfurter via `httpx.MockTransport`), keyword and LLM categorization with fallback, the retry loop (scripted fake LLM that fails first and succeeds on the next attempt, error feedback content, max attempts, transport errors), routing thresholds, status transitions, and the Anthropic/OpenAI adapters against stubbed clients.
- **Integration (36)** - all sample documents end to end with asserted routing, fuzzy vendor linking, duplicate detection, field edits that resolve issues, approval side effects, reprocessing, `process_folder`/`seed_demo`; REST API (auth, upload, filters, approve/reject, CSV/JSON export, OpenAPI); review UI (queue filters, htmx partials, inline editing, decisions).

GitHub Actions (`.github/workflows/ci.yml`) runs `lint`, `test` (plus a missing-migrations check) and `build` (Docker image).

## Roadmap

- OCR for scanned PDFs and photos (e.g. Tesseract or a vision model) - images are currently rejected with a clear message.
- More schemas out of the box: purchase orders, credit notes, delivery notes, receipts with per-line tax.
- Integrations: push approved invoices to QuickBooks, Xero or an ERP; ingest from a mailbox (IMAP) or cloud storage.
- Live VAT number verification (EU VIES, HMRC) instead of format checks.
- PDF viewer with bounding-box highlights in addition to text highlights.
- Use reviewer corrections as few-shot examples per vendor, and track extraction accuracy over time.

## License

[MIT](LICENSE) - Copyright (c) 2026 Ivan Savchenko. All companies, people and tax IDs in `sample_data/` are fictional.

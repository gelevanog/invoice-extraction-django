# Paid models vs the free baseline: invoice extraction

Run on 2026-10-12 on branch `eval/paid-models`. The harness is `manage.py eval_extraction`, added on this
branch. It sends every sample document through the normal pipeline (parse, OCR, extraction with
retry-with-feedback, validation, enrichment, routing) and scores the model's validated record against
`sample_data/ground_truth.json`, which holds the values printed on the fictional documents
(see `scripts/make_samples.py`).

- **Eval set:** the 10 sample documents with ground truth. These are the 8 files in `sample_data/` plus the
  two Haverford invoices in `sample_data/feedback_loop/`, and they are the same documents the README reports on.
  - Header fields: 9 per document (vendor name, tax ID, invoice number, issue/due date, currency, subtotal,
    tax, total), so 90 per run.
  - Line items: 27 lines × 4 attributes (description, qty, unit price, amount), so 108 per run.
- **Models:** all were reached through an OpenAI-compatible gateway with `LLM_PROVIDER=openrouter`,
  `OPENROUTER_BASE_URL=<gateway>`, `ALLOW_PAID_MODELS=true` and `OCR_ENGINE=vision`. With `OCR_ENGINE=vision`,
  each model also transcribes the two scans (07, 08) itself.
- **Raw results:** `haiku-run{1,2,3}.json`, `opus-run{1,2}.json` and `qwen-run{1,2,3}.json` in this folder. Each
  file has per-field expected/got/confidence, line items, attempts, tokens, latency, routing and issues.

## Results

| | Free baseline: `dots-3-note-preview:free` (README, 2026-09-29) | `claude-haiku-5-5` | `claude-opus-5-5` | `qwen3.7-plus` |
|---|---|---|---|---|
| Runs × documents | 1 × 8 (Tesseract) + reruns of 07/08 (vision) + 2 Haverford | 3 × 10 | 2 × 10 | 3 × 10 |
| Header-field accuracy | 71/72 (98.6 %) with Tesseract; 07 omitted the `currency` value. With vision OCR and the reruns, every field was reported correct. | **269/270 (99.6 %)** | **180/180 (100 %)** | 252/270 (93.3 %)¹. When the document was processed: 252/252 (100 %) |
| Line-item attribute accuracy | not scored in the README. The 08 "Coffee service" quantity was `null` with Tesseract and correct with vision | **324/324 (100 %)** | **216/216 (100 %)** | 300/324 (92.6 %)¹. When processed: 300/300 |
| Schema-valid on 1st attempt (extraction) | 8/8 | 30/30 | 20/20 | 28/28 extraction calls. 2 of 30 documents failed earlier, in vision OCR¹ |
| Documents completed | 8/8 | 30/30 | 20/20 | 28/30 |
| `evidence_not_found` (possible hallucination) | 0 | 0 | 0 | 0 |
| Routing per run (approved / needs_review) | 4 / 4 (Tesseract run) | 5 / 5 every run | 8 / 2 every run | 7 / 2 + 1 failed, 7 / 2 + 1 failed, 8 / 2 |
| LLM latency per extraction call (median) | 30–64 s per document (median ≈ 38 s, pipeline time) | 5.9 s | 7.0 s | 18.5 s |
| Wall time per document (median, incl. OCR) | ≈ 38 s | 6.0 s | 7.3 s | 18.5 s |
| Tokens per run (extraction, in / out)² | n/a | 64.9k / 12.8–13.2k | 64.9k / 7.8–7.9k | 43.3–48.0k / 8.3–9.3k |
| Cost per run of 10 docs (gateway meter)³ | $0 | **$0.015** (≈ $0.0015/doc) | **$0.50** (≈ $0.05/doc) | ≈ 58k Qwen tokens (55.9k, 56.1k, 61.5k) |

¹ In 2 of 3 Qwen runs the photo (08) failed in the **vision OCR** stage. The model wrapped its transcription
in a top-level JSON list (`[{"lines": [...]}]`) instead of returning the object. Vision OCR has no
retry-with-feedback loop, so the document fails, and all 9 + 12 of its fields count as wrong. Every Qwen
**extraction** call was valid on the first attempt and fully correct.

² The Claude runs use `LLM_RESPONSE_FORMAT=none`, because this gateway only accepts `strict: true` schemas
and cannot compile the invoice schema in strict mode ("32 parameters with union types, limit 16"). With
`none`, the schema goes into the system prompt, adding about 2.2k input tokens per document. Qwen accepted
the default non-strict `json_schema`. Token counts exclude the vision-OCR transcription calls, which the
cost row includes.

³ This is the gateway's budget meter, delta per run. It bills with a 10 % safety margin, so it is about 10 % above
usage-based cost. Example: Haiku extraction usage is 64.9k × $0.10/M + 13k × $0.50/M ≈ $0.013, plus the
two OCR calls.

**Total spend for this project**, including probes, as metered by the gateway (with its 10 % margin):

| Model | Spend |
|---|---|
| Haiku | $0.046 (3 runs) |
| Opus | $1.009 (2 runs) |
| Anthropic total | $1.058 (≈ $0.003 of it went to request-shape probes) |
| Qwen | 182,109 tokens: 173.5k for the 3 runs, the rest for probes |

### Where routing differs (all runs, all extractions correct unless noted)

| Document | Haiku | Opus | Qwen |
|---|---|---|---|
| 03 (sum mismatch, by design) | review | review | review |
| 06 (duplicate, by design) | review | review | review |
| 08 photo | review: its own OCR gave the faded lines 0.65–0.70, which caps the field confidences | approved (all correct) | approved in the run where OCR worked |
| Haverford March/April ("Tax point" date label) | review: `issue_date` confidence 0.60 | approved | approved |
| Haiku run 2, 08 photo | OCR misread the tax ID as `GB2841937762`, and extraction copied it faithfully. Caught by `invalid_tax_id_format` plus the low OCR confidence, so it went to review | – | – |

## Observations

- **The free baseline was already close to the ceiling on these documents.** dots-3 (free) missed one
  field out of 72, the omitted `currency` value on the Tesseract-read scan. That miss has since been fixed
  in code (a required `value` key, enforced by the retry loop). Haiku, Opus and Qwen all score 100 % on every
  document they processed. With 10 synthetic documents and 1–3 runs per model, the set **cannot rank the
  models on accuracy**; it is too easy. Treat the 99.6 % / 100 % / 93.3 % figures as "no regressions", not
  as a ranking.
- **The real differences are latency, cost and confidence calibration.** Haiku is about 6× faster than the
  free tier (6 s vs about 38 s per document) at about $0.0015 per document. Opus costs about 33× more than
  Haiku and is no more accurate here. Opus and Qwen report high confidence and auto-approve 8 of 10
  documents, which matches the ideal routing on this set (only 03 and 06 need a human). Haiku is more
  conservative. It marks the "Tax point" date (0.60) and its own photo transcription (0.65–0.70) as
  uncertain, so 3 correct documents per run go to review. In run 2 that caution caught a real OCR misread
  of the tax ID on the photo, which the validators also flagged.
- **Vision OCR is where the models differ, not extraction.** All three Claude/Qwen models transcribed the
  scan (07) correctly; the free model managed this too in the README's vision rerun. The photo (08) is
  the stress case:
  - Haiku misread one digit once in 3 runs; the error was flagged and routed to review.
  - Opus was correct 2/2.
  - Qwen returned a malformed response shape 2/3 times.
  A small hardening would remove the Qwen failure: retry-with-feedback in `extraction/ocr/vision.py`, or
  accepting a single-element list. *Update:* both are now implemented (list-wrapped transcriptions are
  unwrapped, other invalid responses get one retry with feedback), covered by unit tests; Qwen was not re-run.
- **Request-shape compatibility was the main integration cost.** The Claude-behind-OpenAI gateway rejects
  `strict: false` and cannot compile the invoice schema in strict mode. The new `LLM_RESPONSE_FORMAT=none`
  mode handles it by keeping the schema in the prompt; Pydantic validation and the retry loop are unchanged,
  and Claude produced valid JSON on the first attempt 50/50 times. `temperature` is never sent.
- **Caveats.**
  - The baseline is taken from the README's narrative and tables; it was not re-run or re-scored with this
    harness, and the README does not score line items.
  - The paid runs use vision OCR because Tesseract is not installed on the eval machine. They are comparable
    to the README's vision reruns of 07/08, not to its Tesseract table.
  - Opus has only 2 runs. No transient gateway errors (403 / connection) occurred, so no retries were needed.

## Reproduce

```bash
# throwaway database and media so earlier documents don't trigger duplicate checks
export DATABASE_URL=sqlite:////tmp/eval.sqlite3 MEDIA_ROOT=/tmp/eval-media
export LLM_PROVIDER=openrouter OPENROUTER_BASE_URL=<openai-compatible endpoint> OPENROUTER_API_KEY=<key> \
       ALLOW_PAID_MODELS=true OPENROUTER_MODEL=claude-haiku-5-5 LLM_RESPONSE_FORMAT=none OCR_ENGINE=vision
uv run python manage.py migrate && uv run python manage.py seed_demo --password ""
uv run python manage.py eval_extraction sample_data --label haiku-run1 --out results/paid-models/haiku-run1.json
# Qwen: OPENROUTER_MODEL=qwen3.7-plus LLM_RESPONSE_FORMAT=json_schema
```

Without `ALLOW_PAID_MODELS=true` the OpenRouter provider rejects any model that is not `:free`, which is the default.

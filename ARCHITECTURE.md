# Architecture

Rufus Generator is a FastAPI web application with a Celery task queue for asynchronous AI processing. It generates Amazon Rufus/COSMOS-optimized product listings from spreadsheet data and product images using the Claude API.

## System Diagram

```
                          ┌─────────────────────────────────────────┐
                          │              Browser                     │
                          │  HTTP Basic Auth on every request        │
                          └────────────────┬────────────────────────┘
                                           │
                          ┌────────────────▼────────────────────────┐
                          │           FastAPI (uvicorn)              │
                          │                                          │
                          │  app/main.py                             │
                          │  ├── GET  /              (home)          │
                          │  ├── GET  /health        (healthcheck)   │
                          │  └── GET  /uploads/*     (file serving)  │
                          │                                          │
                          │  Routers:                                │
                          │  ├── /upload      upload.py              │
                          │  ├── /processing  processing.py          │
                          │  ├── /review      review.py              │
                          │  ├── /export      export.py              │
                          │  └── /reference   reference.py           │
                          └──┬──────────┬──────────┬────────────────┘
                             │          │          │
                    ┌────────▼──┐  ┌────▼────┐  ┌──▼──────────────┐
                    │ PostgreSQL │  │  Redis   │  │  Google Sheets  │
                    │            │  │          │  │  API            │
                    │ jobs       │  │ broker   │  └─────────────────┘
                    │ asin_recs  │  │ results  │
                    │ ref_profs  │  └────┬─────┘
                    └────────────┘       │
                                   ┌─────▼──────────────────────────┐
                                   │      Celery Worker              │
                                   │                                 │
                                   │  interactive queue:             │
                                   │  └── enqueue_job (fan-out)      │
                                   │                                 │
                                   │  bulk queue:                    │
                                   │  └── process_single_asin        │
                                   │      ├── Step 1: Vision AI      │
                                   │      ├── Step 2: Generation     │
                                   │      └── Step 3: Gap Detection  │
                                   └────────────┬───────────────────┘
                                                │
                                   ┌────────────▼───────────────────┐
                                   │       Claude API                │
                                   │  (anthropic Python SDK)         │
                                   │                                 │
                                   │  - Vision analysis (images)     │
                                   │  - Content generation (text)    │
                                   │  - PDF analysis (reference)     │
                                   └─────────────────────────────────┘
```

## Component Descriptions

### Web Layer (`app/main.py`)

The FastAPI application. Configures lifespan (logging, upload directory creation), mounts static files, serves uploaded files behind auth with path traversal protection, and includes all routers.

### Routers (`app/routers/`)

| Router | Prefix | Responsibility |
|---|---|---|
| `upload.py` | `/upload` | File upload form, spreadsheet parsing, ZIP image extraction, ASIN detection, column mapping confirmation, job creation |
| `processing.py` | `/processing` | Progress dashboard (SSE real-time updates), pause/resume, retry-failed, job stats HTML partial for home page |
| `review.py` | `/review` | COSMO-style report card review with filtering (status, confidence), sorting, pagination (20/page), single approve, batch approve, inline edit+save |
| `export.py` | `/export` | Export approved/completed ASINs to Google Sheets |
| `reference.py` | `/reference` | Upload competitor listing PDFs, analyze with Claude vision to extract optimization patterns, store as ReferenceProfile |

### Services (`app/services/`)

| Service | Purpose |
|---|---|
| `spreadsheet_parser.py` | Parse `.xlsx` (openpyxl) and `.csv` files. Auto-detect ASIN column by header name or content pattern (80%+ match on first 20 rows). Cell sanitization to prevent prompt injection. |
| `image_extractor.py` | Extract images from ZIP archives. Match to ASINs via folder name, filename pattern, or manifest CSV. ZIP bomb protection (10 GB decompressed limit, 50K file limit). Path traversal blocking. |
| `column_mapper.py` | Load JSON mapping configs. Fuzzy-match spreadsheet headers to Amazon template fields. Identify which fields need AI generation. |
| `vision_ai.py` | Send up to 5 product images per ASIN to Claude vision API. Extract features, selling points, use cases, packaging text, quality notes, alt text. Rate-limited. |
| `content_generator.py` | Generate RUFUS/COSMOS-optimized listing content. Builds prompt from source data + vision results + reference profile. Outputs title, 5 bullets, description, search terms, image alt text, 10 backend attributes. |
| `gap_detector.py` | Analyze data completeness across all required fields. Weighted confidence scoring: source data (1.0) > vision-informed AI (0.8) > text-only AI (0.7). Field length validation and search term overlap warnings. |
| `pdf_analyzer.py` | Convert PDF pages to images (pdfplumber, max 10 pages), send to Claude vision for pattern extraction: tone, bullet style, keyword density, title pattern, formatting rules, example content. |
| `sheets_exporter.py` | Create a Google Sheets spreadsheet via API. Color-coded cells (green=source, blue=AI, red=missing). Formula injection protection (strips leading `=+\-@\t\r\n`). Batched writes (50 rows per request). |
| `report_generator.py` | Render COSMO-style HTML report cards for the review UI. Inline-editable fields, confidence badge, source/AI/missing indicators, image thumbnails. |
| `rate_limiter.py` | Thread-safe adaptive rate limiter for Claude API calls. Tracks RPM and TPM in sliding 60-second windows with concurrency slots. |

### Tasks (`app/tasks/process_asin.py`)

The Celery task module containing the core processing pipeline:

- **`enqueue_job`** — Fan-out task on the `interactive` queue. Loads all queued ASINRecords for a job and dispatches `process_single_asin` for each to the `bulk` queue.
- **`process_single_asin`** — Per-ASIN pipeline on the `bulk` queue. Three steps: vision AI, content generation, gap detection. Updates ASINRecord status and job counters on completion.

### Configuration (`app/config.py`)

Pydantic Settings with `.env` file support. Includes a model validator that rewrites Heroku's `postgres://` to `postgresql://` and defaults Celery URLs to `REDIS_URL`.

### Auth (`app/auth.py`)

HTTP Basic authentication using FastAPI's `HTTPBasic` security scheme. Timing-safe comparison via `secrets.compare_digest`. Injected as a dependency on every endpoint.

## Data Flow

### Upload -> Process -> Review -> Export

```
1. UPLOAD
   User uploads spreadsheets (.xlsx/.csv) + optional images ZIP
   ├── Spreadsheets parsed, ASINs detected and deduplicated
   ├── Images extracted from ZIP, matched to ASINs
   ├── Column mapping auto-detected
   └── Job created in "confirming" status

2. CONFIRM
   User reviews column mapping on confirmation page
   ├── Job status set to "pending"
   └── enqueue_job task dispatched to Celery

3. PROCESS (async, via Celery)
   For each ASIN:
   ├── Step 1: Vision AI (if images available)
   │   └── Falls back to text-only on vision failure
   ├── Step 2: Content generation via Claude
   │   └── Incorporates source data + vision + reference profile
   └── Step 3: Gap detection + confidence scoring
       ├── score >= 0.85 → "completed"
       ├── missing fields  → "missing_data"
       ├── score < 0.85    → "needs_review"
       └── empty required  → retry once, then "needs_review"

4. REVIEW
   User reviews COSMO-style report cards
   ├── Filter by status, min confidence
   ├── Sort by confidence, status, ASIN
   ├── Inline-edit any generated field
   ├── Approve individual ASINs
   └── Batch-approve all above threshold

5. EXPORT
   User exports approved + completed ASINs to Google Sheets
   └── Color-coded spreadsheet with all listing fields
```

## Database Schema

Three tables managed via SQLAlchemy ORM + Alembic migrations.

### `jobs`

| Column | Type | Description |
|---|---|---|
| `id` | `UUID` (PK) | Job identifier |
| `status` | `Enum(JobStatus)` | `pending`, `confirming`, `processing`, `completed`, `failed`, `paused` |
| `created_at` | `DateTime` | Creation timestamp |
| `updated_at` | `DateTime` | Last update timestamp |
| `source_files` | `JSON` | List of uploaded file paths |
| `template_type` | `String(100)` | Amazon flat file category |
| `mapping_config` | `String(200)` | Path to column mapping JSON |
| `total_asins` | `Integer` | Total ASINs in this job |
| `completed_asins` | `Integer` | Successfully completed + approved |
| `failed_asins` | `Integer` | Failed ASINs |
| `missing_data_asins` | `Integer` | ASINs with missing data |
| `total_input_tokens` | `Integer` | Aggregate input tokens used |
| `total_output_tokens` | `Integer` | Aggregate output tokens used |
| `estimated_cost_usd` | `Float` | Estimated cost ($3/MTok in, $15/MTok out) |

### `asin_records`

| Column | Type | Description |
|---|---|---|
| `id` | `UUID` (PK) | Record identifier |
| `job_id` | `UUID` (FK -> jobs) | Parent job |
| `asin` | `String(20)` | Amazon ASIN (unique per job) |
| `parent_asin` | `String(20)` | Parent ASIN for variations |
| `variation_theme` | `String(100)` | Variation theme (color, size, etc.) |
| `status` | `Enum(ASINStatus)` | `queued`, `processing`, `completed`, `needs_review`, `missing_data`, `failed`, `approved` |
| `generation_mode` | `Enum(GenerationMode)` | `vision` or `text_only` |
| `source_data` | `JSON` | Raw fields from spreadsheet |
| `vision_data` | `JSON` | Features extracted from product images |
| `generated_data` | `JSON` | AI-generated listing fields |
| `image_paths` | `JSON` | Paths to product images |
| `confidence_score` | `Float` | Weighted quality score (0.0 - 1.0) |
| `missing_fields` | `JSON` | List of missing required fields |
| `error_log` | `Text` | Error details (truncated to 1000 chars) |
| `retry_count` | `Integer` | Number of retries |
| `input_tokens` | `Integer` | Input tokens for this ASIN |
| `output_tokens` | `Integer` | Output tokens for this ASIN |
| `created_at` | `DateTime` | Creation timestamp |
| `updated_at` | `DateTime` | Last update timestamp |

**Unique constraint:** `(job_id, asin)`

### `reference_profiles`

| Column | Type | Description |
|---|---|---|
| `id` | `UUID` (PK) | Profile identifier |
| `name` | `String(200)` | Display name (derived from filename) |
| `source_filename` | `String(500)` | Path to uploaded PDF |
| `created_at` | `DateTime` | Creation timestamp |
| `tone` | `String(100)` | Writing tone (e.g., "professional yet conversational") |
| `bullet_style` | `String(100)` | Bullet structure (e.g., "question-answer format") |
| `keyword_density` | `String(50)` | Keyword approach (e.g., "moderate") |
| `title_pattern` | `Text` | Title structure pattern |
| `formatting_rules` | `JSON` | List of extracted formatting rules |
| `example_content` | `JSON` | Sample optimized content from the PDF |

## Celery Task Pipeline

### Queues

| Queue | Purpose | Tasks |
|---|---|---|
| `interactive` | Low-latency user-initiated actions | `enqueue_job` |
| `bulk` | High-throughput ASIN processing | `process_single_asin` |

### Worker Configuration

```python
task_acks_late = True          # acknowledge after completion (at-least-once)
worker_prefetch_multiplier = 1 # fetch one task at a time
worker_max_tasks_per_child = 50 # restart worker after 50 tasks (memory leak prevention)
task_serializer = "json"
```

### Retry Strategy

- **Max retries:** 3 (configurable via `RETRY_MAX_ATTEMPTS`)
- **Backoff:** Exponential — `base_delay * 2^attempt` (default base: 2 seconds)
- **Rate limit 429s:** Automatic Celery retry
- **Partial AI response:** Retry once with modified prompt, then mark `needs_review`
- **Max retries exceeded:** Mark ASIN as `failed`
- **Task ID format:** `{job_id}:{asin}:{retry_count}` (prevents collision on re-enqueue)

### Job Completion Logic

After each ASIN completes, `_update_job_counters` recalculates aggregate stats. A job is marked `completed` when all ASINs reach a terminal state (`completed`, `approved`, `failed`, `missing_data`, `needs_review`). If every ASIN failed, the job itself is marked `failed`.

## Rate Limiting Strategy

The rate limiter (`app/services/rate_limiter.py`) is a per-process, thread-safe, adaptive limiter:

### Limits Tracked

- **RPM** (requests per minute) — sliding 60-second window, default 50
- **TPM** (tokens per minute) — sliding 60-second window, default 200,000
- **Concurrency** — max simultaneous API calls, default 10

### Adaptive Behavior

1. **Normal mode:** Requests proceed if all three limits have capacity
2. **On 429 (rate limit):** 60-second cooldown activated; concurrency halved
3. **Recovery:** After cooldown expires and a successful request completes, full concurrency is restored

### Slot Lifecycle

```
acquire() → [API call] → release(tokens_used)
             on success → on_success()
             on 429     → on_rate_limit()
```

`wait_for_slot(timeout=120)` blocks until a slot opens, polling every 500ms. Returns `False` on timeout.

### Limitation

This is a per-process limiter. With multiple Celery workers, each gets its own instance, so effective limits scale with worker count. For multi-worker production, a Redis-backed sliding window (INCR + EXPIRE) is recommended.

## Auth Model

- **Mechanism:** HTTP Basic authentication via FastAPI's `HTTPBasic` security scheme
- **Credential storage:** Environment variables (`AUTH_USERNAME`, `AUTH_PASSWORD`)
- **Comparison:** Timing-safe via `secrets.compare_digest` to prevent timing attacks
- **Scope:** Every endpoint (including file uploads and SSE streams) requires authentication
- **Upload protection:** Files served through `/uploads/*` require auth and validate against path traversal

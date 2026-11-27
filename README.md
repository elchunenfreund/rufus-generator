# Rufus Generator

Turns a vendor's product spreadsheet and photos into Amazon listing content: titles, bullets, descriptions, search terms and backend attributes. A person reviews the drafts and exports them to Google Sheets.

I built it for Amazon catalog work, where a new line of products can mean hundreds of listings written by hand.

**Stack:** Python, FastAPI, Celery, PostgreSQL, Redis, Claude API, HTMX

## How it works

1. **Upload.** You upload an `.xlsx` or `.csv` and a ZIP of images. The app finds the ASIN column, maps the other columns, and matches each image to a product by folder, filename or manifest.
2. **Process.** One Celery task runs per product. Claude vision reads the photos (features, packaging text), then Claude writes the listing. Rate limits are enforced in Redis, and failed products retry with backoff. Jobs can be paused and resumed.
3. **Score.** Each field is scored by where it came from (the spreadsheet beats vision, vision beats a guess), and missing fields are flagged.
4. **Review.** Report cards show every product. You can edit inline, or batch-approve everything above a confidence threshold.
5. **Export.** Approved listings go to Google Sheets, colored by source, with formula-injection protection.

You can also upload competitor listing PDFs. The app pulls out their tone and bullet style and uses that to guide generation.

## Engineering notes

- Two Celery queues: one for fanning out a job, one for the per-product work. This keeps the UI responsive during big batches.
- Progress streams to the browser over server-sent events.
- Uploads are hardened against path traversal and ZIP bombs. Logins use timing-safe comparison. The app refuses to start in production with a default password.
- Around 200 pytest tests cover the services, routers and tasks.

## Quickstart

### Docker (recommended)

```bash
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY at minimum

docker compose up --build
```

The app starts at `http://localhost:8000`. Locally the login defaults to `admin` / `changeme`. In production, `AUTH_PASSWORD` must be set or the app will not start.

### Local development

Prerequisites: Python 3.12+, PostgreSQL 16, Redis 7.

```bash
# Create database
createdb rufus_generator

# Install dependencies
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env — set DATABASE_URL, REDIS_URL, ANTHROPIC_API_KEY

# Run migrations
alembic upgrade head

# Start the web server
uvicorn app.main:app --reload --port 8000

# Start the Celery worker (separate terminal)
celery -A app.celery_app worker --loglevel=info --concurrency=1 -Q bulk,interactive
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | `development` or `production` |
| `DEBUG` | `false` | Enable debug logging (console renderer) |
| `AUTH_USERNAME` | `admin` | HTTP Basic auth username |
| `AUTH_PASSWORD` | `changeme` | HTTP Basic auth password |
| `DATABASE_URL` | `postgresql://localhost:5432/rufus_generator` | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis URL (also used as default for Celery) |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery broker (defaults to `REDIS_URL` if unset) |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/1` | Celery result backend (defaults to `REDIS_URL` if unset) |
| `ANTHROPIC_API_KEY` | — | Claude API key (**required** for generation) |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6-20250514` | Claude model to use |
| `ANTHROPIC_RPM_LIMIT` | `50` | Requests per minute limit |
| `ANTHROPIC_TPM_LIMIT` | `200000` | Tokens per minute limit |
| `ANTHROPIC_MAX_CONCURRENT` | `10` | Max concurrent API calls |
| `GOOGLE_SERVICE_ACCOUNT_PATH` | — | Path to Google service account JSON (for Sheets export) |
| `UPLOAD_DIR` | `./uploads` | Directory for uploaded files |
| `MAX_UPLOAD_SIZE_BYTES` | `10737418240` | Max ZIP upload size (10 GB) |

## Deployment (Heroku)

The app includes a `Procfile` with two process types:

```
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: celery -A app.celery_app worker --loglevel=info --concurrency=10 -Q bulk,interactive
```

```bash
heroku create rufus-generator
heroku addons:create heroku-postgresql:essential-0
heroku addons:create heroku-redis:mini

heroku config:set AUTH_USERNAME=<username>
heroku config:set AUTH_PASSWORD=<password>
heroku config:set ANTHROPIC_API_KEY=sk-ant-xxxxx
heroku config:set ENVIRONMENT=production

git push heroku main

heroku ps:scale web=1 worker=1
heroku run alembic upgrade head
```

The `postgres://` scheme Heroku provides is automatically rewritten to `postgresql://` for SQLAlchemy. Celery broker/backend URLs default to `REDIS_URL` when not explicitly set.

## Architecture Overview

```
Browser (HTTP Basic)
  |
FastAPI (uvicorn)
  ├── /upload     — spreadsheet + ZIP upload, ASIN detection, column mapping
  ├── /processing — SSE progress dashboard, pause/resume, retry-failed
  ├── /review     — COSMO report cards, approve/batch-approve, inline edit
  ├── /export     — Google Sheets export
  └── /reference  — competitor PDF upload and pattern extraction
  |
Celery (Redis broker)
  ├── interactive queue → enqueue_job (fan-out)
  └── bulk queue        → process_single_asin (per-ASIN pipeline)
        Step 1: Vision AI (if images)
        Step 2: Content Generation (Claude)
        Step 3: Gap Detection + Confidence Scoring
  |
PostgreSQL ← jobs, asin_records, reference_profiles
Redis      ← Celery broker + result backend
Claude API ← vision analysis + content generation + PDF analysis
```

## API Routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Home page with recent jobs table |
| `GET` | `/health` | Health check endpoint |
| `GET` | `/uploads/{path}` | Serve uploaded files (auth required) |
| `GET` | `/upload/` | Upload form |
| `POST` | `/upload/process` | Upload spreadsheets + images ZIP |
| `POST` | `/upload/confirm/{job_id}` | Confirm column mapping, start processing |
| `GET` | `/processing/` | Redirect to latest job |
| `GET` | `/processing/stats` | HTML partial with job stats (HTMX) |
| `GET` | `/processing/{job_id}` | Job progress dashboard |
| `GET` | `/processing/{job_id}/stream` | SSE progress stream |
| `POST` | `/processing/{job_id}/pause` | Pause a running job |
| `POST` | `/processing/{job_id}/resume` | Resume a paused job |
| `POST` | `/processing/{job_id}/retry-failed` | Re-queue all failed ASINs |
| `GET` | `/review/` | Redirect to latest job |
| `GET` | `/review/{job_id}` | Review dashboard with report cards |
| `POST` | `/review/{job_id}/approve/{asin_id}` | Approve a single ASIN |
| `POST` | `/review/{job_id}/batch-approve` | Batch-approve above confidence threshold |
| `POST` | `/review/{job_id}/save/{asin_id}` | Save inline edits |
| `GET` | `/export/` | Redirect to latest job |
| `GET` | `/export/{job_id}` | Export page |
| `POST` | `/export/{job_id}/sheets` | Export to Google Sheets |
| `GET` | `/reference/` | Reference profiles list |
| `POST` | `/reference/upload` | Upload and analyze competitor PDF |
| `GET` | `/reference/{profile_id}` | View reference profile detail |

## Development

```bash
# Lint
ruff check app/

# Format
ruff format app/

# Tests
pytest
```

## License

Proprietary.

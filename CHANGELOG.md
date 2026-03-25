# Changelog

All notable changes to the Rufus Generator will be documented in this file.

## [0.1.0.0] - 2026-03-25

### Added
- Full Amazon Rufus Listing Generator web application
- Upload pipeline: spreadsheet parsing (.xlsx, .csv), ASIN detection, ZIP image extraction with folder/filename matching
- AI processing pipeline: Claude vision analysis, RUFUS/COSMOS-optimized content generation, gap detection, confidence scoring
- Celery task queue with adaptive rate limiting, per-ASIN retry with exponential backoff, pause/resume
- COSMO-style report card review UI with batch-approve by confidence threshold, inline editing, filter/sort
- Google Sheets export with conditional formatting and formula injection protection
- Reference PDF analysis for competitor listing optimization pattern extraction
- SSE progress dashboard with real-time counters and ETA
- Column mapping confirmation step before processing
- HTTP Basic auth on all endpoints including file uploads
- Path traversal protection on uploaded file serving
- Docker Compose + Dockerfile for local development

### Fixed
- 41 bugs found and fixed across 8 rounds of adversarial code review (3 Claude, 4 Codex, 1 full /review pipeline)
- Formula injection whitespace bypass in Google Sheets export
- All-failed job incorrectly marked as "completed" instead of "failed"
- Nav links to Processing/Review/Export returning 404 without job context
- Raw JSON displayed on home page instead of formatted job table
- TemplateResponse compatibility with newer Starlette versions

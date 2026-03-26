from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import verify_credentials
from app.config import get_logger, settings, setup_logging
from app.services.storage import S3Storage, storage
from app.templating import templates

log = get_logger(__name__)

# --- Sentry ---
if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=0.2,
        send_default_pii=False,
    )
    log.info("sentry_initialized", environment=settings.environment)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # Ensure upload directory exists (only matters for local storage)
    if not settings.aws_s3_bucket:
        Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    log.info("app_started", environment=settings.environment)
    yield
    log.info("app_stopped")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

# Static files
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Uploads served via authenticated endpoint (NOT public StaticFiles)
if not settings.aws_s3_bucket:
    uploads_dir = Path(settings.upload_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)


@app.get("/uploads/{file_path:path}")
async def serve_upload(file_path: str, username: str = Depends(verify_credentials)):
    """Serve uploaded files (images, spreadsheets) — requires auth.

    For S3 storage, redirects to a pre-signed URL.
    For local storage, serves the file directly.
    """
    if isinstance(storage, S3Storage):
        # Redirect to a pre-signed S3 URL
        if not storage.exists(file_path):
            raise HTTPException(status_code=404, detail="File not found")
        return RedirectResponse(url=storage.get_url(file_path))

    # Local storage — serve file directly
    uploads_dir = Path(settings.upload_dir)
    full_path = (uploads_dir / file_path).resolve()
    # Prevent path traversal
    if not str(full_path).startswith(str(uploads_dir.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(full_path)


@app.get("/health")
async def health_check():
    """Enhanced health check with DB, Redis, Celery, and job stats."""
    checks: dict = {"app": settings.app_name, "version": "0.1.0"}
    overall = "healthy"

    # --- Database ---
    try:
        from sqlalchemy import text
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            checks["database"] = "ok"
        finally:
            db.close()
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        overall = "degraded"

    # --- Redis ---
    try:
        import redis as redis_lib
        r = redis_lib.from_url(settings.redis_url, socket_connect_timeout=2)
        r.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"
        overall = "degraded"

    # --- Celery workers ---
    try:
        from app.celery_app import celery as celery_app
        inspector = celery_app.control.inspect(timeout=2.0)
        active = inspector.active()
        if active is None:
            checks["celery_workers"] = "no workers responding"
            overall = "degraded"
        else:
            checks["celery_workers"] = {name: len(tasks) for name, tasks in active.items()}
    except Exception as exc:
        checks["celery_workers"] = f"error: {exc}"
        overall = "degraded"

    # --- Active jobs ---
    try:
        from app.database import SessionLocal
        from app.models import Job, JobStatus
        db = SessionLocal()
        try:
            active_count = (
                db.query(Job)
                .filter(Job.status.in_([JobStatus.pending, JobStatus.processing]))
                .count()
            )
            checks["active_jobs"] = active_count
        finally:
            db.close()
    except Exception as exc:
        checks["active_jobs"] = f"error: {exc}"

    checks["status"] = overall
    status_code = 200 if overall == "healthy" else 503
    from fastapi.responses import JSONResponse
    return JSONResponse(content=checks, status_code=status_code)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, username: str = Depends(verify_credentials)):
    return templates.TemplateResponse(request=request, name="home.html")


# Import and include routers
from app.routers import upload, processing, review, export, reference  # noqa: E402

app.include_router(upload.router, prefix="/upload", tags=["upload"])
app.include_router(processing.router, prefix="/processing", tags=["processing"])
app.include_router(review.router, prefix="/review", tags=["review"])
app.include_router(export.router, prefix="/export", tags=["export"])
app.include_router(reference.router, prefix="/reference", tags=["reference"])

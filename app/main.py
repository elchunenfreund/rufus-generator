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


# Google OAuth callback — must be at /auth/google/callback (not under /export prefix)
@app.get("/auth/google/callback")
async def google_oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """Handle Google OAuth2 callback, exchange code for tokens."""
    import httpx

    if error:
        log.error("google_oauth_error", error=error)
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    parts = state.split(":", 1)
    username = parts[0] if parts else ""
    job_id = parts[1] if len(parts) > 1 else ""

    redirect_uri = f"{request.url.scheme}://{request.url.netloc}/auth/google/callback"

    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )

    if token_response.status_code != 200:
        log.error("google_token_exchange_failed", status=token_response.status_code, body=token_response.text)
        raise HTTPException(status_code=500, detail="Failed to exchange Google authorization code")

    tokens = token_response.json()

    from app.routers.export import _user_google_tokens
    _user_google_tokens[username] = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
    }

    log.info("google_oauth_connected", username=username)

    if job_id:
        return RedirectResponse(url=f"/export/{job_id}", status_code=302)
    return RedirectResponse(url="/export/", status_code=302)

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.auth import verify_credentials
from app.config import get_logger, settings, setup_logging
from app.templating import templates

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # Ensure upload directory exists
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
uploads_dir = Path(settings.upload_dir)
uploads_dir.mkdir(parents=True, exist_ok=True)


@app.get("/uploads/{file_path:path}")
async def serve_upload(file_path: str, username: str = Depends(verify_credentials)):
    """Serve uploaded files (images, spreadsheets) — requires auth."""
    full_path = (uploads_dir / file_path).resolve()
    # Prevent path traversal
    if not str(full_path).startswith(str(uploads_dir.resolve())):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Access denied")
    if not full_path.is_file():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(full_path)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "app": settings.app_name, "version": "0.1.0"}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, username: str = Depends(verify_credentials)):
    return templates.TemplateResponse("home.html", {"request": request})


# Import and include routers
from app.routers import upload, processing, review, export, reference  # noqa: E402

app.include_router(upload.router, prefix="/upload", tags=["upload"])
app.include_router(processing.router, prefix="/processing", tags=["processing"])
app.include_router(review.router, prefix="/review", tags=["review"])
app.include_router(export.router, prefix="/export", tags=["export"])
app.include_router(reference.router, prefix="/reference", tags=["reference"])

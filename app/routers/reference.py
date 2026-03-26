"""Reference router: upload competitor PDFs, analyze optimization patterns."""

import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import verify_credentials
from app.config import get_logger, settings
from app.database import get_db
from app.templating import templates
from app.models import ReferenceProfile
from app.services.pdf_analyzer import analyze_pdf_with_vision
from app.services.storage import storage

log = get_logger(__name__)
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def reference_page(
    request: Request,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    profiles = db.query(ReferenceProfile).order_by(ReferenceProfile.created_at.desc()).all()
    return templates.TemplateResponse("reference.html", request, profiles=profiles)


@router.post("/upload")
async def upload_reference(
    request: Request,
    pdf_file: UploadFile = File(...),
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Upload and analyze a competitor listing PDF."""
    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        return templates.TemplateResponse(
            "reference.html", request,
            profiles=db.query(ReferenceProfile).all(),
            error="Please upload a PDF file.",
        )

    # Read PDF with size limit (50MB)
    file_id = uuid.uuid4()
    safe_name = Path(pdf_file.filename or "reference.pdf").name
    max_pdf_size = 50 * 1024 * 1024  # 50MB
    chunks = []
    written = 0
    oversized = False
    while chunk := pdf_file.file.read(8192):
        written += len(chunk)
        if written > max_pdf_size:
            oversized = True
            break
        chunks.append(chunk)

    if oversized:
        return templates.TemplateResponse(
            "reference.html", request,
            profiles=db.query(ReferenceProfile).all(),
            error="PDF file exceeds 50MB limit.",
        )

    pdf_data = b"".join(chunks)
    storage_key = f"references/{file_id}_{safe_name}"
    storage.save(storage_key, pdf_data)

    # Analyze with Claude vision — needs a local temp file
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_data)
        tmp_path = Path(tmp.name)

    try:
        result = analyze_pdf_with_vision(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.error:
        return templates.TemplateResponse(
            "reference.html", request,
            profiles=db.query(ReferenceProfile).all(),
            error=f"Analysis failed: {result.error}",
        )

    # Save to DB
    profile = ReferenceProfile(
        name=pdf_file.filename.replace(".pdf", "").replace("_", " ").title(),
        source_filename=storage_key,
        tone=result.tone,
        bullet_style=result.bullet_style,
        keyword_density=result.keyword_density,
        title_pattern=result.title_pattern,
        formatting_rules=result.formatting_rules or [],
        example_content=result.example_content or {},
    )
    db.add(profile)
    db.commit()

    log.info(
        "reference_profile_created",
        profile_id=str(profile.id),
        name=profile.name,
        tone=profile.tone,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )

    profiles = db.query(ReferenceProfile).order_by(ReferenceProfile.created_at.desc()).all()
    return templates.TemplateResponse(
        "reference.html", request,
        profiles=profiles,
        success=f"Reference profile '{profile.name}' created successfully.",
    )


@router.get("/{profile_id}", response_class=HTMLResponse)
async def view_profile(
    profile_id: str,
    request: Request,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    profile = db.query(ReferenceProfile).filter(ReferenceProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Reference profile not found")
    return templates.TemplateResponse("reference_detail.html", request, profile=profile)

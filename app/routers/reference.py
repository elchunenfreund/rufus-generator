"""Reference router: upload competitor PDFs, analyze optimization patterns."""

import shutil
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

log = get_logger(__name__)
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def reference_page(
    request: Request,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    profiles = db.query(ReferenceProfile).order_by(ReferenceProfile.created_at.desc()).all()
    return templates.TemplateResponse(
        "reference.html", {"request": request, "profiles": profiles}
    )


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
            "reference.html",
            {
                "request": request,
                "profiles": db.query(ReferenceProfile).all(),
                "error": "Please upload a PDF file.",
            },
            status_code=400,
        )

    # Save PDF
    ref_dir = Path(settings.upload_dir) / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4()
    safe_name = Path(pdf_file.filename or "reference.pdf").name
    pdf_path = ref_dir / f"{file_id}_{safe_name}"
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(pdf_file.file, f)

    # Analyze with Claude vision
    result = analyze_pdf_with_vision(pdf_path)

    if result.error:
        return templates.TemplateResponse(
            "reference.html",
            {
                "request": request,
                "profiles": db.query(ReferenceProfile).all(),
                "error": f"Analysis failed: {result.error}",
            },
            status_code=500,
        )

    # Save to DB
    profile = ReferenceProfile(
        name=pdf_file.filename.replace(".pdf", "").replace("_", " ").title(),
        source_filename=str(pdf_path),
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
        "reference.html",
        {
            "request": request,
            "profiles": profiles,
            "success": f"Reference profile '{profile.name}' created successfully.",
        },
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
    return templates.TemplateResponse(
        "reference_detail.html", {"request": request, "profile": profile}
    )

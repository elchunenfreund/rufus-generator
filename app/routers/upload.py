"""Upload router: spreadsheet + image upload, ASIN detection, mapping confirmation."""

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import verify_credentials
from app.config import get_logger, settings
from app.database import get_db
from app.templating import templates
from app.models import ASINRecord, ASINStatus, GenerationMode, Job, JobStatus
from app.services.column_mapper import auto_detect_columns, load_mapping_config
from app.services.image_extractor import extract_images
from app.services.spreadsheet_parser import parse_file

log = get_logger(__name__)
router = APIRouter()

ALLOWED_SPREADSHEET_TYPES = {".xlsx", ".csv"}
ALLOWED_ARCHIVE_TYPES = {".zip"}


@router.get("/", response_class=HTMLResponse)
async def upload_page(request: Request, username: str = Depends(verify_credentials)):
    return templates.TemplateResponse("upload.html", {"request": request})


@router.post("/process")
async def process_upload(
    request: Request,
    spreadsheets: list[UploadFile] = File(...),
    images_zip: UploadFile | None = File(None),
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Handle file uploads, parse spreadsheets, extract images, and create a job
    in 'confirming' status for the user to review before processing."""
    job_id = uuid.uuid4()
    job_dir = Path(settings.upload_dir) / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    # Save and parse spreadsheets
    all_asins: dict[str, dict] = {}  # asin -> merged row data
    all_headers: list[str] = []
    source_files = []
    parse_errors = []
    duplicate_asins = []

    for spreadsheet in spreadsheets:
        suffix = Path(spreadsheet.filename or "").suffix.lower()
        if suffix not in ALLOWED_SPREADSHEET_TYPES:
            parse_errors.append(f"Invalid file type: {spreadsheet.filename}. Accepted: .xlsx, .csv")
            continue

        # Save to disk with size enforcement
        safe_name = Path(spreadsheet.filename or "upload").name
        file_path = job_dir / f"source_{safe_name}"
        max_spreadsheet_size = 100 * 1024 * 1024  # 100MB per spreadsheet
        written = 0
        oversized = False
        with open(file_path, "wb") as f:
            while chunk := spreadsheet.file.read(8192):
                written += len(chunk)
                if written > max_spreadsheet_size:
                    oversized = True
                    break
                f.write(chunk)

        if oversized:
            file_path.unlink(missing_ok=True)
            parse_errors.append(f"{spreadsheet.filename} exceeds 100MB limit — skipped")
            continue

        source_files.append(str(file_path))

        # Parse
        result = parse_file(file_path)
        parse_errors.extend(result.errors)
        all_headers.extend(h for h in result.headers if h not in all_headers)

        # Collect ASINs — reject duplicates instead of silent last-seen-wins
        for row in result.rows:
            if result.asin_column and result.asin_column in row:
                asin = row[result.asin_column].strip().upper()
                if len(asin) == 10 and asin.isalnum():
                    if asin in all_asins:
                        duplicate_asins.append(asin)
                    else:
                        all_asins[asin] = row

    # Reject if duplicates found
    unique_duplicates = sorted(set(duplicate_asins))
    if unique_duplicates:
        parse_errors.append(
            f"Duplicate ASINs found across spreadsheets ({len(unique_duplicates)}): "
            f"{', '.join(unique_duplicates[:10])}{'...' if len(unique_duplicates) > 10 else ''}. "
            f"Please fix source data — each ASIN must appear only once."
        )

    if not all_asins and not parse_errors:
        parse_errors.append("No valid ASINs found in uploaded spreadsheets")

    # Extract images if provided
    image_result = None
    if images_zip and images_zip.filename:
        suffix = Path(images_zip.filename).suffix.lower()
        if suffix not in ALLOWED_ARCHIVE_TYPES:
            parse_errors.append(f"Invalid archive type: {images_zip.filename}. Accepted: .zip")
        else:
            safe_zip_name = Path(images_zip.filename or "images.zip").name
            zip_path = job_dir / f"images_{safe_zip_name}"
            written = 0
            oversized = False
            with open(zip_path, "wb") as f:
                while chunk := images_zip.file.read(65536):
                    written += len(chunk)
                    if written > settings.max_upload_size_bytes:
                        oversized = True
                        break
                    f.write(chunk)
            if oversized:
                zip_path.unlink(missing_ok=True)
                parse_errors.append(
                    f"ZIP file exceeds {settings.max_upload_size_bytes // (1024**3)}GB limit"
                )
            else:
                source_files.append(str(zip_path))

            images_dir = job_dir / "images"
            images_dir.mkdir(exist_ok=True)
            image_result = extract_images(zip_path, images_dir, set(all_asins.keys()))
            parse_errors.extend(image_result.errors)

    # If critical errors, return to upload page with errors
    if not all_asins:
        return templates.TemplateResponse(
            "upload.html",
            {"request": request, "errors": parse_errors},
            status_code=400,
        )

    # Load mapping config
    config_path = Path(__file__).parent.parent.parent / "config" / "mappings" / "default.json"
    mapping_config = load_mapping_config(config_path)
    detected_columns = auto_detect_columns(all_headers, mapping_config)

    # Create job in confirming status
    job = Job(
        id=job_id,
        status=JobStatus.confirming,
        source_files=source_files,
        mapping_config=str(config_path),
        total_asins=len(all_asins),
    )
    db.add(job)

    # Create ASINRecords
    for asin, row_data in all_asins.items():
        has_images = image_result and asin in image_result.matched_asins
        record = ASINRecord(
            job_id=job_id,
            asin=asin,
            source_data=row_data,
            image_paths=image_result.matched_asins.get(asin, []) if image_result else [],
            generation_mode=GenerationMode.vision if has_images else GenerationMode.text_only,
        )
        db.add(record)

    db.commit()

    # Show confirmation page
    asins_with_images = len(image_result.matched_asins) if image_result else 0
    asins_without_images = len(all_asins) - asins_with_images

    return templates.TemplateResponse(
        "upload_confirm.html",
        {
            "request": request,
            "job_id": str(job_id),
            "total_asins": len(all_asins),
            "asins_with_images": asins_with_images,
            "asins_without_images": asins_without_images,
            "orphaned_images": image_result.orphaned_images[:20] if image_result else [],
            "orphaned_count": len(image_result.orphaned_images) if image_result else 0,
            "detected_columns": detected_columns,
            "all_headers": all_headers,
            "parse_errors": [e for e in parse_errors if "Duplicate" not in e],  # dupes already rejected
            "source_file_count": len(spreadsheets),
            "total_images": image_result.total_images if image_result else 0,
        },
    )


@router.post("/confirm/{job_id}")
async def confirm_job(
    job_id: str,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """User confirmed the mapping — start processing."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.confirming:
        raise HTTPException(status_code=400, detail=f"Job is {job.status}, not confirming")

    job.status = JobStatus.pending
    db.commit()

    # Enqueue Celery tasks
    from app.tasks.process_asin import enqueue_job

    enqueue_job.delay(str(job_id))

    log.info("job_confirmed", job_id=str(job_id), total_asins=job.total_asins)
    return RedirectResponse(url=f"/processing/{job_id}", status_code=303)

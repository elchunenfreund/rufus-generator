"""Upload router: spreadsheet + image upload, ASIN detection, mapping confirmation."""

import tempfile
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
from app.services.storage import storage

log = get_logger(__name__)
router = APIRouter()

ALLOWED_SPREADSHEET_TYPES = {".xlsx", ".csv"}
ALLOWED_ARCHIVE_TYPES = {".zip"}
ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}


@router.get("/", response_class=HTMLResponse)
async def upload_page(request: Request, username: str = Depends(verify_credentials)):
    return templates.TemplateResponse(request=request, name="upload.html")


@router.post("/process")
async def process_upload(
    request: Request,
    spreadsheets: list[UploadFile] = File(...),
    images: list[UploadFile] = File([]),
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Handle file uploads, parse spreadsheets, extract images, and create a job
    in 'confirming' status for the user to review before processing."""
    job_id = uuid.uuid4()
    job_prefix = str(job_id)  # storage key prefix for this job

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

        # Read upload into memory with size enforcement
        safe_name = Path(spreadsheet.filename or "upload").name
        max_spreadsheet_size = 100 * 1024 * 1024  # 100MB per spreadsheet
        chunks = []
        written = 0
        oversized = False
        while chunk := spreadsheet.file.read(8192):
            written += len(chunk)
            if written > max_spreadsheet_size:
                oversized = True
                break
            chunks.append(chunk)

        if oversized:
            parse_errors.append(f"{spreadsheet.filename} exceeds 100MB limit — skipped")
            continue

        file_data = b"".join(chunks)
        storage_key = f"{job_prefix}/source_{safe_name}"
        storage.save(storage_key, file_data)
        source_files.append(storage_key)

        # Parse needs a local file — write to a temp file
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_data)
            tmp_path = Path(tmp.name)

        try:
            result = parse_file(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

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

    # Reject if duplicates found — hard stop, not just a warning
    unique_duplicates = sorted(set(duplicate_asins))
    if unique_duplicates:
        return templates.TemplateResponse(
            "upload.html",
            {
                "request": request,
                "errors": [
                    f"Duplicate ASINs found ({len(unique_duplicates)}): "
                    f"{', '.join(unique_duplicates[:10])}{'...' if len(unique_duplicates) > 10 else ''}. "
                    f"Please fix source data — each ASIN must appear only once."
                ] + parse_errors,
            },
            status_code=400,
        )

    if not all_asins and not parse_errors:
        parse_errors.append("No valid ASINs found in uploaded spreadsheets")

    # Extract images if provided — supports individual files and/or ZIP archives
    image_result = None
    uploaded_images = [img for img in images if img.filename]
    if uploaded_images:
        from app.services.image_extractor import ImageExtractionResult

        # Separate ZIPs from individual images
        zip_files = [f for f in uploaded_images if Path(f.filename).suffix.lower() in ALLOWED_ARCHIVE_TYPES]
        loose_images = [f for f in uploaded_images if Path(f.filename).suffix.lower() in ALLOWED_IMAGE_TYPES]
        rejected = [f for f in uploaded_images
                    if Path(f.filename).suffix.lower() not in ALLOWED_ARCHIVE_TYPES
                    and Path(f.filename).suffix.lower() not in ALLOWED_IMAGE_TYPES]

        for f in rejected:
            parse_errors.append(f"Unsupported image file: {f.filename}. Accepted: .jpg, .jpeg, .png, .webp, .zip")

        # Handle loose images — assign to first ASIN if only one, otherwise try filename matching
        if loose_images:
            image_result = image_result or ImageExtractionResult()
            asins_list = list(all_asins.keys())

            for img_file in loose_images:
                img_data = await img_file.read()
                if len(img_data) > settings.max_image_size_bytes:
                    parse_errors.append(
                        f"Image too large: {img_file.filename} "
                        f"({len(img_data) / 1e6:.1f}MB > {settings.max_image_size_bytes / 1e6:.0f}MB)"
                    )
                    continue

                image_result.total_images += 1
                safe_name = Path(img_file.filename).name

                # Try to match image to an ASIN by filename
                from app.services.image_extractor import _extract_asin_from_path
                matched_asin = _extract_asin_from_path(safe_name, set(asins_list))

                # If only one ASIN in the job, assign all images to it
                if not matched_asin and len(asins_list) == 1:
                    matched_asin = asins_list[0]

                if matched_asin:
                    storage_key = f"{job_prefix}/images/{matched_asin}/{safe_name}"
                    storage.save(storage_key, img_data)
                    if matched_asin not in image_result.matched_asins:
                        image_result.matched_asins[matched_asin] = []
                    image_result.matched_asins[matched_asin].append(storage_key)

                    # Enforce max images per ASIN
                    if len(image_result.matched_asins[matched_asin]) > settings.max_images_per_asin:
                        image_result.matched_asins[matched_asin] = image_result.matched_asins[matched_asin][
                            : settings.max_images_per_asin
                        ]
                else:
                    image_result.orphaned_images.append(safe_name)

        # Handle ZIP archives (existing logic)
        for zip_upload in zip_files:
            chunks = []
            written = 0
            oversized = False
            while chunk := zip_upload.file.read(65536):
                written += len(chunk)
                if written > settings.max_upload_size_bytes:
                    oversized = True
                    break
                chunks.append(chunk)

            if oversized:
                parse_errors.append(
                    f"ZIP file exceeds {settings.max_upload_size_bytes // (1024**3)}GB limit"
                )
                continue

            zip_data = b"".join(chunks)

            # Save ZIP to storage
            safe_zip_name = Path(zip_upload.filename or "images.zip").name
            zip_storage_key = f"{job_prefix}/images_{safe_zip_name}"
            storage.save(zip_storage_key, zip_data)
            source_files.append(zip_storage_key)

            # Extract images needs local files — use a temp directory
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_zip = Path(tmp_dir) / "images.zip"
                tmp_zip.write_bytes(zip_data)
                tmp_images_dir = Path(tmp_dir) / "images"
                tmp_images_dir.mkdir()

                zip_result = extract_images(
                    tmp_zip, tmp_images_dir, set(all_asins.keys())
                )
                parse_errors.extend(zip_result.errors)

                # Merge ZIP results into image_result
                image_result = image_result or ImageExtractionResult()
                image_result.total_images += zip_result.total_images
                image_result.orphaned_images.extend(zip_result.orphaned_images)

                # Upload extracted images to storage and merge matched ASINs
                for asin, local_paths in zip_result.matched_asins.items():
                    if asin not in image_result.matched_asins:
                        image_result.matched_asins[asin] = []
                    for local_path in local_paths:
                        p = Path(local_path)
                        storage_key = f"{job_prefix}/images/{asin}/{p.name}"
                        storage.save(storage_key, p.read_bytes())
                        image_result.matched_asins[asin].append(storage_key)

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

    # Create ASINRecords — image_paths are now storage keys, not filesystem paths
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

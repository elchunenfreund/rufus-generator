"""Review router: COSMO-style report card review with batch-approve and filtering."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sqlalchemy import func

from app.auth import verify_credentials
from app.config import get_logger
from app.database import get_db
from app.templating import templates
from app.models import ASINRecord, ASINStatus, Job, JobStatus
from app.services.report_generator import render_report_card_html, report_data_from_asin_record

log = get_logger(__name__)
router = APIRouter()


def _refresh_job_counters(db: Session, job_id: str):
    """Update job counters after approval actions."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return
    counts = dict(
        db.query(ASINRecord.status, func.count(ASINRecord.id))
        .filter(ASINRecord.job_id == job_id)
        .group_by(ASINRecord.status)
        .all()
    )
    job.completed_asins = counts.get(ASINStatus.completed, 0) + counts.get(ASINStatus.approved, 0)
    job.failed_asins = counts.get(ASINStatus.failed, 0)
    job.missing_data_asins = counts.get(ASINStatus.missing_data, 0)

    # Mark job complete if all ASINs are in terminal states
    total_done = (
        job.completed_asins + job.failed_asins + job.missing_data_asins
        + counts.get(ASINStatus.needs_review, 0)
    )
    if total_done >= job.total_asins and job.total_asins > 0:
        job.status = JobStatus.completed


@router.get("/", response_class=HTMLResponse)
async def review_index(
    request: Request,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Redirect to most recent job's review page."""
    job = db.query(Job).order_by(Job.created_at.desc()).first()
    if not job:
        return templates.TemplateResponse("review.html", {
            "request": request, "job": None, "records": [],
            "username": username, "pagination": {}, "filters": {},
        })
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/review/{job.id}", status_code=302)


@router.get("/{job_id}", response_class=HTMLResponse)
async def review_page(
    job_id: str,
    request: Request,
    status_filter: str = Query(None),
    min_confidence: float = Query(None),
    sort_by: str = Query("confidence"),
    page: int = Query(1),
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Review dashboard with COSMO-style report cards, filtering, and batch actions."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Build query with filters
    query = db.query(ASINRecord).filter(ASINRecord.job_id == job_id)

    if status_filter:
        try:
            query = query.filter(ASINRecord.status == ASINStatus(status_filter))
        except ValueError:
            pass

    if min_confidence is not None:
        query = query.filter(ASINRecord.confidence_score >= min_confidence)

    # Sort
    if sort_by == "confidence":
        query = query.order_by(ASINRecord.confidence_score.asc().nullsfirst())
    elif sort_by == "confidence_desc":
        query = query.order_by(ASINRecord.confidence_score.desc().nullslast())
    elif sort_by == "status":
        query = query.order_by(ASINRecord.status)
    else:
        query = query.order_by(ASINRecord.asin)

    # Pagination (20 per page)
    per_page = 20
    total_count = query.count()
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    records = query.offset((page - 1) * per_page).limit(per_page).all()

    # Render report cards
    report_cards = []
    for record in records:
        data = report_data_from_asin_record(record)
        html = render_report_card_html(data)
        report_cards.append({"record": record, "html": html})

    # Summary stats
    from sqlalchemy import func

    stats = (
        db.query(ASINRecord.status, func.count(ASINRecord.id))
        .filter(ASINRecord.job_id == job_id)
        .group_by(ASINRecord.status)
        .all()
    )
    status_counts = dict(stats)

    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "job_id": job_id,
            "job": job,
            "report_cards": report_cards,
            "status_counts": status_counts,
            "total_count": total_count,
            "page": page,
            "total_pages": total_pages,
            "status_filter": status_filter,
            "min_confidence": min_confidence,
            "sort_by": sort_by,
        },
    )


@router.post("/{job_id}/approve/{asin_id}")
async def approve_asin(
    job_id: str,
    asin_id: str,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Approve a single ASIN."""
    record = db.query(ASINRecord).filter(
        ASINRecord.id == asin_id, ASINRecord.job_id == job_id
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="ASIN not found")

    record.status = ASINStatus.approved
    db.flush()
    _refresh_job_counters(db, job_id)
    db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/review/{job_id}", status_code=303)


@router.post("/{job_id}/batch-approve")
async def batch_approve(
    job_id: str,
    min_confidence: float = Query(0.85),
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Batch-approve all ASINs above a confidence threshold."""
    records = (
        db.query(ASINRecord)
        .filter(
            ASINRecord.job_id == job_id,
            ASINRecord.status.in_([ASINStatus.completed, ASINStatus.needs_review]),
            ASINRecord.confidence_score >= min_confidence,
        )
        .all()
    )

    for record in records:
        record.status = ASINStatus.approved

    db.flush()
    _refresh_job_counters(db, job_id)
    db.commit()
    log.info("batch_approved", job_id=job_id, count=len(records), threshold=min_confidence)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/review/{job_id}", status_code=303)


@router.post("/{job_id}/save/{asin_id}")
async def save_edits(
    job_id: str,
    asin_id: str,
    request: Request,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Save inline edits to an ASIN's generated content."""
    record = db.query(ASINRecord).filter(
        ASINRecord.id == asin_id, ASINRecord.job_id == job_id
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="ASIN not found")

    EDITABLE_FIELDS = {
        "title", "bullet_point_1", "bullet_point_2", "bullet_point_3",
        "bullet_point_4", "bullet_point_5", "description", "search_terms",
        "image_alt_text", "color", "size", "material", "intended_use",
        "age_range_description", "target_audience", "item_form",
        "special_features", "included_components", "country_of_origin",
    }

    body = await request.json()
    if record.generated_data is None:
        record.generated_data = {}

    # Merge edits into generated data (allowlisted fields only)
    updated = dict(record.generated_data)
    for key, value in body.items():
        if key in EDITABLE_FIELDS and isinstance(value, str):
            updated[key] = value.strip()
    record.generated_data = updated

    db.commit()
    return {"status": "saved", "asin": record.asin}

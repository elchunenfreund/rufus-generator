"""Export router: Google Sheets export and PDF report generation."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import verify_credentials
from app.config import get_logger
from app.database import get_db
from app.templating import templates
from app.models import ASINRecord, ASINStatus, Job

log = get_logger(__name__)
router = APIRouter()


@router.get("/{job_id}", response_class=HTMLResponse)
async def export_page(
    job_id: str,
    request: Request,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    approved = db.query(ASINRecord).filter(
        ASINRecord.job_id == job_id, ASINRecord.status == ASINStatus.approved
    ).count()

    completed = db.query(ASINRecord).filter(
        ASINRecord.job_id == job_id, ASINRecord.status == ASINStatus.completed
    ).count()

    total = db.query(ASINRecord).filter(ASINRecord.job_id == job_id).count()

    return templates.TemplateResponse(
        "export.html",
        {
            "request": request,
            "job_id": job_id,
            "job": job,
            "approved_count": approved,
            "completed_count": completed,
            "total_count": total,
            "exportable": approved + completed,
        },
    )


@router.post("/{job_id}/sheets")
async def export_to_sheets(
    job_id: str,
    include_completed: bool = Query(True),
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Export approved (and optionally completed) ASINs to Google Sheets."""
    statuses = [ASINStatus.approved]
    if include_completed:
        statuses.append(ASINStatus.completed)

    records = (
        db.query(ASINRecord)
        .filter(ASINRecord.job_id == job_id, ASINRecord.status.in_(statuses))
        .order_by(ASINRecord.asin)
        .all()
    )

    if not records:
        raise HTTPException(status_code=400, detail="No exportable ASINs")

    try:
        from app.services.sheets_exporter import export_to_google_sheets
        result = export_to_google_sheets(records, job_id)
        log.info("sheets_export", job_id=job_id, count=len(records), sheet_url=result.get("url"))
        return result
    except Exception as e:
        log.error("sheets_export_error", job_id=job_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

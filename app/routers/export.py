"""Export router: CSV download, Google Sheets export, and PDF report generation."""

import csv
import io

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

import urllib.parse

from app.auth import verify_credentials
from app.config import get_logger, settings
from app.database import get_db
from app.templating import templates
from app.models import ASINRecord, ASINStatus, Job

log = get_logger(__name__)
router = APIRouter()

# In-memory token store: {username: {"access_token": ..., "refresh_token": ..., ...}}
_user_google_tokens: dict[str, dict] = {}

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_SCOPES = "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.file"


@router.get("/auth/google")
async def google_oauth_redirect(
    request: Request,
    job_id: str = Query(""),
    username: str = Depends(verify_credentials),
):
    """Redirect user to Google OAuth2 consent screen."""
    if not settings.google_oauth_client_id:
        raise HTTPException(status_code=500, detail="Google OAuth not configured")

    # Build redirect URI from request host
    redirect_uri = f"{request.url.scheme}://{request.url.netloc}/auth/google/callback"
    state = f"{username}:{job_id}"
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=url, status_code=302)


@router.get("/", response_class=HTMLResponse)
async def export_index(
    request: Request,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Redirect to most recent job's export page."""
    from fastapi.responses import RedirectResponse
    job = db.query(Job).order_by(Job.created_at.desc()).first()
    if not job:
        return templates.TemplateResponse("export.html", {
            "request": request, "job": None,
            "approved_count": 0, "completed_count": 0, "username": username,
            "google_connected": username in _user_google_tokens,
            "google_oauth_configured": bool(settings.google_oauth_client_id),
        })
    return RedirectResponse(url=f"/export/{job.id}", status_code=302)


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
            "google_connected": username in _user_google_tokens,
            "google_oauth_configured": bool(settings.google_oauth_client_id),
        },
    )


@router.post("/{job_id}/sheets")
async def export_to_sheets(
    job_id: str,
    request: Request,
    share_emails: str = Form(""),
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

    # Parse comma/space-separated email list
    emails = [e.strip() for e in share_emails.replace(",", " ").split() if "@" in e]

    try:
        user_tokens = _user_google_tokens.get(username)
        if user_tokens:
            from app.services.sheets_exporter import export_with_user_credentials
            result = export_with_user_credentials(
                records, job_id,
                access_token=user_tokens["access_token"],
                share_with_emails=emails or None,
            )
        else:
            from app.services.sheets_exporter import export_to_google_sheets
            result = export_to_google_sheets(records, job_id, share_with_emails=emails or None)

        log.info("sheets_export", job_id=job_id, count=len(records), sheet_url=result.get("url"), shared_with=emails)
        return RedirectResponse(url=result["url"], status_code=303)
    except Exception as e:
        log.error("sheets_export_error", job_id=job_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")


@router.get("/{job_id}/csv")
async def export_csv(
    job_id: str,
    include_completed: bool = Query(True),
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Download approved (and optionally completed) ASINs as a CSV file."""
    from app.services.sheets_exporter import EXPORT_COLUMNS

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

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([col.replace("_", " ").title() for col in EXPORT_COLUMNS])

    for record in records:
        source = record.source_data or {}
        generated = record.generated_data or {}
        row = []
        for col in EXPORT_COLUMNS:
            if col == "asin":
                row.append(record.asin)
            elif col == "confidence_score":
                row.append(f"{record.confidence_score:.0%}" if record.confidence_score else "")
            elif col == "generation_mode":
                row.append(record.generation_mode.value if record.generation_mode else "")
            elif col == "missing_fields":
                row.append(", ".join(record.missing_fields or []))
            else:
                row.append(source.get(col, "") or generated.get(col, ""))
        writer.writerow(row)

    output.seek(0)
    log.info("csv_export", job_id=job_id, count=len(records))
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=rufus-export-{job_id[:8]}.csv"},
    )

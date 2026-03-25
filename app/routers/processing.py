"""Processing router: job progress dashboard with SSE real-time updates."""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.auth import verify_credentials
from app.config import get_logger
from app.database import get_db, SessionLocal
from app.templating import templates
from app.models import ASINRecord, ASINStatus, Job, JobStatus

log = get_logger(__name__)
router = APIRouter()


def _job_stats(db: Session, job_id: str) -> dict:
    """Compute job progress stats."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return {}

    counts = (
        db.query(ASINRecord.status, func.count(ASINRecord.id))
        .filter(ASINRecord.job_id == job_id)
        .group_by(ASINRecord.status)
        .all()
    )
    status_map = dict(counts)

    completed = status_map.get(ASINStatus.completed, 0) + status_map.get(ASINStatus.approved, 0)
    failed = status_map.get(ASINStatus.failed, 0)
    needs_review = status_map.get(ASINStatus.needs_review, 0)
    missing = status_map.get(ASINStatus.missing_data, 0)
    processing = status_map.get(ASINStatus.processing, 0)
    queued = status_map.get(ASINStatus.queued, 0)

    total = job.total_asins or 0
    done = completed + failed + needs_review + missing
    progress_pct = int((done / total) * 100) if total > 0 else 100

    # ETA: rough estimate based on average processing time
    if done > 0 and processing + queued > 0:
        # Get average processing time from completed records
        avg_result = (
            db.query(func.avg(
                func.extract("epoch", ASINRecord.updated_at) - func.extract("epoch", ASINRecord.created_at)
            ))
            .filter(ASINRecord.job_id == job_id, ASINRecord.status.in_([ASINStatus.completed, ASINStatus.approved]))
            .scalar()
        )
        avg_seconds = float(avg_result) if avg_result else 20.0
        remaining = queued + processing
        eta_seconds = int(remaining * avg_seconds / max(1, 10))  # assume ~10 concurrent
        eta_min = max(1, eta_seconds // 60)
        eta_str = f"~{eta_min} min" if eta_min < 60 else f"~{eta_min // 60}h {eta_min % 60}m"
    else:
        eta_str = "Calculating..."

    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "total": total,
        "completed": completed,
        "failed": failed,
        "needs_review": needs_review,
        "missing_data": missing,
        "processing": processing,
        "queued": queued,
        "done": done,
        "progress_pct": progress_pct,
        "eta": eta_str,
        "cost_usd": f"${job.estimated_cost_usd:.2f}" if job.estimated_cost_usd else "$0.00",
        "is_complete": job.status == JobStatus.completed,
        "is_paused": job.status == JobStatus.paused,
    }


@router.get("/", response_class=HTMLResponse)
async def processing_index(
    request: Request,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Show most recent job's processing page, or redirect to upload."""
    job = db.query(Job).order_by(Job.created_at.desc()).first()
    if not job:
        return templates.TemplateResponse("processing.html", {
            "request": request, "job": None, "username": username,
            "message": "No jobs yet. Upload a batch to get started.",
        })
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/processing/{job.id}", status_code=302)


@router.get("/stats")
async def all_jobs_stats(
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Return summary stats for all jobs (used by home page)."""
    jobs = db.query(Job).order_by(Job.created_at.desc()).limit(10).all()
    return [
        {
            "id": str(j.id),
            "status": j.status.value,
            "total_asins": j.total_asins,
            "completed": j.completed_asins,
            "failed": j.failed_asins,
            "created_at": j.created_at.isoformat() if j.created_at else "",
            "cost_usd": f"${j.estimated_cost_usd:.2f}" if j.estimated_cost_usd else "$0.00",
        }
        for j in jobs
    ]


@router.get("/{job_id}", response_class=HTMLResponse)
async def processing_page(
    job_id: str,
    request: Request,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    stats = _job_stats(db, job_id)

    # Get recently completed ASINs for the live feed
    recent = (
        db.query(ASINRecord)
        .filter(
            ASINRecord.job_id == job_id,
            ASINRecord.status.in_([ASINStatus.completed, ASINStatus.needs_review]),
        )
        .order_by(ASINRecord.updated_at.desc())
        .limit(10)
        .all()
    )

    return templates.TemplateResponse(
        "processing.html",
        {"request": request, "stats": stats, "recent": recent, "job_id": job_id},
    )


@router.get("/{job_id}/stream")
async def progress_stream(
    job_id: str,
    username: str = Depends(verify_credentials),
):
    """SSE stream for real-time progress updates."""

    async def event_generator():
        while True:
            db = None
            try:
                db = SessionLocal()
                stats = _job_stats(db, job_id)
                if not stats:
                    yield {"event": "error", "data": json.dumps({"error": "Job not found"})}
                    return

                yield {"event": "progress", "data": json.dumps(stats)}

                if stats.get("is_complete"):
                    yield {"event": "complete", "data": json.dumps(stats)}
                    return
            finally:
                if db:
                    db.close()

            await asyncio.sleep(5)  # update every 5 seconds

    return EventSourceResponse(event_generator())


@router.post("/{job_id}/pause")
async def pause_job(
    job_id: str,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Pause a running job — in-flight tasks complete but no new ones start."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == JobStatus.processing:
        job.status = JobStatus.paused
        db.commit()
        log.info("job_paused", job_id=job_id)
    return {"status": "paused"}


@router.post("/{job_id}/resume")
async def resume_job(
    job_id: str,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Resume a paused job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == JobStatus.paused:
        job.status = JobStatus.processing
        db.commit()

        # Re-enqueue any remaining queued ASINs
        from app.tasks.process_asin import enqueue_job
        enqueue_job.delay(str(job_id))

        log.info("job_resumed", job_id=job_id)
    return {"status": "resumed"}


@router.post("/{job_id}/retry-failed")
async def retry_failed(
    job_id: str,
    username: str = Depends(verify_credentials),
    db: Session = Depends(get_db),
):
    """Retry all failed ASINs in a job."""
    failed_records = (
        db.query(ASINRecord)
        .filter(ASINRecord.job_id == job_id, ASINRecord.status == ASINStatus.failed)
        .all()
    )

    job = db.query(Job).filter(Job.id == job_id).first()

    for record in failed_records:
        record.status = ASINStatus.queued
        record.retry_count += 1  # increment so task ID is unique on re-enqueue
        record.error_log = None

    # Reset job status so dashboard doesn't show "complete" during retry
    if job and failed_records:
        job.status = JobStatus.processing

    db.commit()

    from app.tasks.process_asin import enqueue_job
    enqueue_job.delay(str(job_id))

    log.info("retry_failed", job_id=job_id, count=len(failed_records))
    return {"retried": len(failed_records)}

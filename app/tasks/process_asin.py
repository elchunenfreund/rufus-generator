"""Celery tasks for processing ASINs — the core pipeline.

Task chain per ASIN:
  Step 1: Vision AI (if images) + Data Transform (parallel)
  Step 2: Content Generation (uses Step 1 output)
  Step 3: Gap Detection + Validation + Confidence Scoring
"""

import time

from celery import shared_task
from celery.exceptions import Retry as CeleryRetry
from sqlalchemy.orm import Session

from app.celery_app import celery
from app.config import get_logger, settings
from app.database import SessionLocal
from app.models import ASINRecord, ASINStatus, GenerationMode, Job, JobStatus, ReferenceProfile
from app.services.content_generator import generate_listing
from app.services.gap_detector import detect_gaps_and_score
from app.services.vision_ai import analyze_images

log = get_logger(__name__)


def _get_db() -> Session:
    return SessionLocal()


@celery.task(name="app.tasks.process_asin.enqueue_job", queue="interactive")
def enqueue_job(job_id: str):
    """Enqueue all ASINs in a job for processing."""
    db = _get_db()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            log.error("job_not_found", job_id=job_id)
            return

        records = (
            db.query(ASINRecord)
            .filter(ASINRecord.job_id == job_id, ASINRecord.status == ASINStatus.queued)
            .all()
        )

        if not records:
            log.info("no_queued_asins", job_id=job_id)
            # Re-evaluate job completion in case all ASINs are done
            _update_job_counters(db, job_id)
            db.commit()
            return

        job.status = JobStatus.processing
        db.commit()

        for record in records:
            # Unique task ID per attempt (retry_count makes retries non-colliding)
            task_id = f"{job_id}:{record.asin}:{record.retry_count}"
            process_single_asin.apply_async(
                args=[str(record.id), job_id],
                task_id=task_id,
                queue="bulk",
            )

        log.info("job_enqueued", job_id=job_id, asin_count=len(records))
    finally:
        db.close()


@celery.task(
    name="app.tasks.process_asin.process_single_asin",
    bind=True,
    max_retries=settings.retry_max_attempts,
    default_retry_delay=settings.retry_base_delay,
    queue="bulk",
)
def process_single_asin(self, record_id: str, job_id: str):
    """Process a single ASIN through the full pipeline."""
    db = _get_db()
    try:
        record = db.query(ASINRecord).filter(ASINRecord.id == record_id).first()
        if not record:
            log.error("asin_record_not_found", record_id=record_id)
            return

        # Check if job is paused
        job = db.query(Job).filter(Job.id == job_id).first()
        if job and job.status == JobStatus.paused:
            log.info("job_paused_skip", job_id=job_id, asin=record.asin)
            return

        record.status = ASINStatus.processing
        db.commit()

        start_time = time.time()
        total_input_tokens = 0
        total_output_tokens = 0

        # Step 1: Vision AI (if images available)
        vision_result = None
        if record.generation_mode == GenerationMode.vision and record.image_paths:
            vision_result = analyze_images(record.image_paths, record.asin)

            if vision_result.error:
                # Retry on rate limit, otherwise fall back to text-only
                if "Rate limited" in (vision_result.error or ""):
                    raise self.retry(exc=Exception(vision_result.error))
                log.warning(
                    "vision_fallback_text_only",
                    asin=record.asin,
                    error=vision_result.error,
                )
                record.generation_mode = GenerationMode.text_only
            else:
                record.vision_data = {
                    "features": vision_result.features,
                    "selling_points": vision_result.selling_points,
                    "use_cases": vision_result.use_cases,
                    "packaging_text": vision_result.packaging_text,
                    "quality_notes": vision_result.quality_notes,
                    "suggested_alt_text": vision_result.suggested_alt_text,
                }
                total_input_tokens += vision_result.input_tokens
                total_output_tokens += vision_result.output_tokens

        # Step 2: Content Generation
        # Get the most recent reference profile (if any)
        reference = (
            db.query(ReferenceProfile)
            .order_by(ReferenceProfile.created_at.desc())
            .first()
        )

        gen_result = generate_listing(
            asin=record.asin,
            source_data=record.source_data or {},
            vision_result=vision_result,
            reference_profile=reference,
        )

        if gen_result.error:
            if "Rate limited" in (gen_result.error or ""):
                raise self.retry(exc=Exception(gen_result.error))
            # After retries exhausted, mark as failed
            record.status = ASINStatus.failed
            record.error_log = gen_result.error
            db.flush()
            _update_job_counters(db, job_id)
            db.commit()
            return

        total_input_tokens += gen_result.input_tokens
        total_output_tokens += gen_result.output_tokens

        # Store generated data
        generated = {
            "title": gen_result.title,
            "bullet_point_1": gen_result.bullet_points[0] if len(gen_result.bullet_points) > 0 else "",
            "bullet_point_2": gen_result.bullet_points[1] if len(gen_result.bullet_points) > 1 else "",
            "bullet_point_3": gen_result.bullet_points[2] if len(gen_result.bullet_points) > 2 else "",
            "bullet_point_4": gen_result.bullet_points[3] if len(gen_result.bullet_points) > 3 else "",
            "bullet_point_5": gen_result.bullet_points[4] if len(gen_result.bullet_points) > 4 else "",
            "description": gen_result.description,
            "search_terms": gen_result.search_terms,
            "image_alt_text": gen_result.image_alt_text,
            **gen_result.backend_attributes,
        }
        record.generated_data = generated

        # Step 3: Gap Detection + Confidence Scoring
        gap_report = detect_gaps_and_score(
            source_data=record.source_data or {},
            generated_data=generated,
            vision_data=record.vision_data,
            has_images=record.generation_mode == GenerationMode.vision,
        )

        record.confidence_score = gap_report.confidence_score
        record.missing_fields = gap_report.missing_fields

        # Per-field validation: check all required fields are non-empty
        empty_required = []
        for field_name in ["title", "bullet_point_1", "description"]:
            if not generated.get(field_name):
                empty_required.append(field_name)

        if empty_required:
            # Partial response — retry once with modified prompt
            if self.request.retries < 1:
                log.warning("partial_response_retry", asin=record.asin, empty=empty_required)
                raise self.retry(exc=Exception(f"Empty required fields: {empty_required}"))
            record.status = ASINStatus.needs_review
            record.error_log = f"Partial AI response — empty fields: {', '.join(empty_required)}"
        elif gap_report.missing_fields:
            record.status = ASINStatus.missing_data
        elif gap_report.confidence_score >= 0.85:
            record.status = ASINStatus.completed
        else:
            record.status = ASINStatus.needs_review

        # Token tracking
        record.input_tokens = total_input_tokens
        record.output_tokens = total_output_tokens

        elapsed = time.time() - start_time
        log.info(
            "asin_processed",
            asin=record.asin,
            status=record.status.value,
            confidence=f"{record.confidence_score:.2f}",
            tokens=total_input_tokens + total_output_tokens,
            elapsed_s=f"{elapsed:.1f}",
        )

        db.flush()
        _update_job_counters(db, job_id)
        db.commit()

    except self.MaxRetriesExceededError:
        record = db.query(ASINRecord).filter(ASINRecord.id == record_id).first()
        if record:
            record.status = ASINStatus.failed
            record.error_log = "Max retries exceeded"
            db.flush()
            _update_job_counters(db, job_id)
            db.commit()
        log.error("max_retries_exceeded", record_id=record_id,
                  asin=record.asin if record else "unknown")
    except CeleryRetry:
        raise  # let Celery handle its own Retry exception
    except Exception as exc:
        db.rollback()
        record = db.query(ASINRecord).filter(ASINRecord.id == record_id).first()
        # Use only Celery's retry mechanism (remove manual retry_count to avoid double-counting)
        if record and self.request.retries < settings.retry_max_attempts:
            db.commit()
            raise self.retry(exc=exc, countdown=settings.retry_base_delay * (2 ** self.request.retries))
        elif record:
            record.status = ASINStatus.failed
            record.error_log = str(exc)[:1000]
            db.flush()
            _update_job_counters(db, job_id)
            db.commit()
        log.error("asin_processing_error", record_id=record_id, error=str(exc))
    finally:
        db.close()


def _update_job_counters(db: Session, job_id: str):
    """Update job aggregate counters from ASINRecord statuses."""
    from sqlalchemy import func

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return

    counts = (
        db.query(ASINRecord.status, func.count(ASINRecord.id))
        .filter(ASINRecord.job_id == job_id)
        .group_by(ASINRecord.status)
        .all()
    )
    status_counts = dict(counts)

    job.completed_asins = status_counts.get(ASINStatus.completed, 0) + status_counts.get(ASINStatus.approved, 0)
    job.failed_asins = status_counts.get(ASINStatus.failed, 0)
    job.missing_data_asins = status_counts.get(ASINStatus.missing_data, 0)

    # Token totals
    totals = (
        db.query(func.sum(ASINRecord.input_tokens), func.sum(ASINRecord.output_tokens))
        .filter(ASINRecord.job_id == job_id)
        .first()
    )
    job.total_input_tokens = totals[0] or 0
    job.total_output_tokens = totals[1] or 0

    # Rough cost estimate: $3/MTok input, $15/MTok output (Sonnet pricing)
    job.estimated_cost_usd = (job.total_input_tokens * 3 + job.total_output_tokens * 15) / 1_000_000

    # Check if job is complete
    total_done = (
        job.completed_asins + job.failed_asins + job.missing_data_asins
        + status_counts.get(ASINStatus.needs_review, 0)
    )
    if total_done >= job.total_asins:
        job.status = JobStatus.completed
        log.info(
            "job_completed",
            job_id=str(job.id),
            completed=job.completed_asins,
            failed=job.failed_asins,
            missing=job.missing_data_asins,
            cost_usd=f"${job.estimated_cost_usd:.2f}",
        )

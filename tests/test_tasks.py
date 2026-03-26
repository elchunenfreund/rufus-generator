"""Tests for Celery tasks (process_asin) — uses mocks for external services."""

import uuid
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from app.models import ASINRecord, ASINStatus, GenerationMode, Job, JobStatus
from app.tasks.process_asin import _update_job_counters


class TestUpdateJobCounters:
    """Test the _update_job_counters helper independently."""

    def test_counters_reflect_asin_statuses(self, db_session, sample_job):
        """Job counters should aggregate from ASINRecord statuses."""
        records = [
            ASINRecord(id=uuid.uuid4(), job_id=sample_job.id, asin="B0COMPLET1",
                       status=ASINStatus.completed, input_tokens=100, output_tokens=50),
            ASINRecord(id=uuid.uuid4(), job_id=sample_job.id, asin="B0COMPLET2",
                       status=ASINStatus.completed, input_tokens=200, output_tokens=100),
            ASINRecord(id=uuid.uuid4(), job_id=sample_job.id, asin="B0FAILED01",
                       status=ASINStatus.failed, input_tokens=50, output_tokens=0),
        ]
        db_session.add_all(records)
        db_session.commit()

        _update_job_counters(db_session, str(sample_job.id))
        db_session.commit()
        db_session.refresh(sample_job)

        assert sample_job.completed_asins == 2
        assert sample_job.failed_asins == 1
        assert sample_job.total_input_tokens == 350
        assert sample_job.total_output_tokens == 150

    def test_job_marked_completed_when_all_done(self, db_session):
        """Job should transition to completed when all ASINs are in terminal states."""
        job = Job(id=uuid.uuid4(), status=JobStatus.processing, total_asins=2)
        db_session.add(job)
        db_session.flush()

        r1 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0DONE0001",
                        status=ASINStatus.completed)
        r2 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0DONE0002",
                        status=ASINStatus.completed)
        db_session.add_all([r1, r2])
        db_session.commit()

        _update_job_counters(db_session, str(job.id))
        db_session.commit()
        db_session.refresh(job)

        assert job.status == JobStatus.completed

    def test_job_marked_failed_when_all_failed(self, db_session):
        """If ALL ASINs failed, job should be marked failed (not completed)."""
        job = Job(id=uuid.uuid4(), status=JobStatus.processing, total_asins=2)
        db_session.add(job)
        db_session.flush()

        r1 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0FAIL0001",
                        status=ASINStatus.failed)
        r2 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0FAIL0002",
                        status=ASINStatus.failed)
        db_session.add_all([r1, r2])
        db_session.commit()

        _update_job_counters(db_session, str(job.id))
        db_session.commit()
        db_session.refresh(job)

        assert job.status == JobStatus.failed

    def test_cost_calculation(self, db_session, sample_job):
        """Cost estimate should be based on Sonnet pricing: $3/MTok in, $15/MTok out."""
        r = ASINRecord(
            id=uuid.uuid4(), job_id=sample_job.id, asin="B0COST0001",
            status=ASINStatus.completed,
            input_tokens=1_000_000, output_tokens=100_000,
        )
        db_session.add(r)
        db_session.commit()

        _update_job_counters(db_session, str(sample_job.id))
        db_session.commit()
        db_session.refresh(sample_job)

        # 1M * $3/M + 100K * $15/M = $3.00 + $1.50 = $4.50
        assert abs(sample_job.estimated_cost_usd - 4.50) < 0.01

    def test_approved_asins_count_as_completed(self, db_session):
        """Approved ASINs should be counted in completed_asins."""
        job = Job(id=uuid.uuid4(), status=JobStatus.processing, total_asins=2)
        db_session.add(job)
        db_session.flush()

        r1 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0APPROV01",
                        status=ASINStatus.approved)
        r2 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0COMPLT01",
                        status=ASINStatus.completed)
        db_session.add_all([r1, r2])
        db_session.commit()

        _update_job_counters(db_session, str(job.id))
        db_session.commit()
        db_session.refresh(job)

        assert job.completed_asins == 2

    def test_nonexistent_job_is_noop(self, db_session):
        """Calling with a fake job_id should not raise."""
        _update_job_counters(db_session, str(uuid.uuid4()))


class TestEnqueueJob:
    @patch("app.tasks.process_asin.process_single_asin")
    def test_enqueue_dispatches_queued_asins(self, mock_process, db_session):
        """enqueue_job should dispatch process_single_asin for each queued ASIN."""
        from app.tasks.process_asin import enqueue_job

        job = Job(id=uuid.uuid4(), status=JobStatus.pending, total_asins=2)
        db_session.add(job)
        db_session.flush()

        r1 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0QUEUE001",
                        status=ASINStatus.queued)
        r2 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0QUEUE002",
                        status=ASINStatus.queued)
        db_session.add_all([r1, r2])
        db_session.commit()

        # Mock _get_db to return our test session, but don't let it close
        mock_db = MagicMock(wraps=db_session)
        mock_db.close = MagicMock()  # prevent real close
        with patch("app.tasks.process_asin._get_db", return_value=mock_db):
            mock_process.apply_async = MagicMock()
            enqueue_job(str(job.id))

        assert mock_process.apply_async.call_count == 2

        # Re-query to check status since we used a mock wrapper
        db_session.refresh(job)
        assert job.status == JobStatus.processing


class TestProcessSingleASIN:
    @patch("app.tasks.process_asin.generate_listing")
    @patch("app.tasks.process_asin.analyze_images")
    def test_text_only_pipeline(self, mock_vision, mock_gen, db_session, sample_asin_record):
        """Text-only ASIN should skip vision and go straight to content generation."""
        from app.services.content_generator import GenerationResult
        from app.tasks.process_asin import process_single_asin

        mock_gen.return_value = GenerationResult(
            title="Generated Title",
            bullet_points=["B1", "B2", "B3", "B4", "B5"],
            description="Great product description",
            search_terms="search terms here",
            backend_attributes={
                "color": "Red", "size": "M", "material": "Steel",
                "intended_use": "General", "age_range_description": "Adult",
                "target_audience": "Everyone", "item_form": "Solid",
                "special_features": "Durable", "included_components": "Widget",
                "country_of_origin": "USA",
            },
            input_tokens=1000,
            output_tokens=500,
        )

        job_id = str(sample_asin_record.job_id)
        record_id = str(sample_asin_record.id)

        # Mock _get_db and the task's self/request attributes
        mock_db = MagicMock(wraps=db_session)
        mock_db.close = MagicMock()

        with patch("app.tasks.process_asin._get_db", return_value=mock_db):
            # Call the underlying run method directly, bypassing Celery dispatch.
            # process_single_asin is bind=True, so self is injected by Celery.
            # We create a mock request context.
            task = process_single_asin
            task.request.retries = 0
            try:
                task.run(record_id, job_id)
            except Exception:
                # MaxRetriesExceededError mock might not be set up
                pass

        mock_vision.assert_not_called()
        mock_gen.assert_called_once()

        db_session.refresh(sample_asin_record)
        assert sample_asin_record.generated_data["title"] == "Generated Title"
        assert sample_asin_record.status in (ASINStatus.completed, ASINStatus.needs_review)
        assert sample_asin_record.input_tokens == 1000

    @patch("app.tasks.process_asin.generate_listing")
    @patch("app.tasks.process_asin.analyze_images")
    def test_vision_pipeline(self, mock_vision, mock_gen, db_session, sample_job):
        """Vision ASIN should call analyze_images before content generation."""
        from app.services.content_generator import GenerationResult
        from app.services.vision_ai import VisionResult
        from app.tasks.process_asin import process_single_asin

        record = ASINRecord(
            id=uuid.uuid4(), job_id=sample_job.id, asin="B0VISION01",
            status=ASINStatus.queued, generation_mode=GenerationMode.vision,
            source_data={"brand": "Acme"}, image_paths=["/img/photo.jpg"],
        )
        db_session.add(record)
        db_session.commit()

        mock_vision.return_value = VisionResult(
            features=["metal"], selling_points=["sleek"],
            input_tokens=800, output_tokens=200,
        )
        mock_gen.return_value = GenerationResult(
            title="Vision Generated", bullet_points=["B1"] * 5,
            description="Desc", search_terms="terms",
            backend_attributes={k: "val" for k in [
                "color", "size", "material", "intended_use",
                "age_range_description", "target_audience", "item_form",
                "special_features", "included_components", "country_of_origin",
            ]},
            input_tokens=1000, output_tokens=500,
        )

        mock_db = MagicMock(wraps=db_session)
        mock_db.close = MagicMock()

        with patch("app.tasks.process_asin._get_db", return_value=mock_db):
            task = process_single_asin
            task.request.retries = 0
            try:
                task.run(str(record.id), str(sample_job.id))
            except Exception:
                pass

        mock_vision.assert_called_once()
        mock_gen.assert_called_once()

        db_session.refresh(record)
        assert record.vision_data["features"] == ["metal"]
        assert record.input_tokens == 1800  # 800 + 1000

    @patch("app.tasks.process_asin.generate_listing")
    def test_generation_failure_marks_failed(self, mock_gen, db_session, sample_asin_record):
        """If content generation fails (after retries), ASIN should be marked failed."""
        from app.services.content_generator import GenerationResult
        from app.tasks.process_asin import process_single_asin

        mock_gen.return_value = GenerationResult(error="API is down")

        mock_db = MagicMock(wraps=db_session)
        mock_db.close = MagicMock()

        with patch("app.tasks.process_asin._get_db", return_value=mock_db):
            task = process_single_asin
            task.request.retries = 3
            try:
                task.run(str(sample_asin_record.id), str(sample_asin_record.job_id))
            except Exception:
                pass

        db_session.refresh(sample_asin_record)
        assert sample_asin_record.status == ASINStatus.failed
        assert "API is down" in sample_asin_record.error_log

    @patch("app.tasks.process_asin.generate_listing")
    def test_paused_job_skips_processing(self, mock_gen, db_session, sample_job, sample_asin_record):
        """If the job is paused, the task should skip without processing."""
        from app.tasks.process_asin import process_single_asin

        sample_job.status = JobStatus.paused
        db_session.commit()

        mock_db = MagicMock(wraps=db_session)
        mock_db.close = MagicMock()

        with patch("app.tasks.process_asin._get_db", return_value=mock_db):
            task = process_single_asin
            task.request.retries = 0
            try:
                task.run(str(sample_asin_record.id), str(sample_job.id))
            except Exception:
                pass

        mock_gen.assert_not_called()

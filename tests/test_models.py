"""Tests for SQLAlchemy models — creation, defaults, relationships."""

import uuid
from datetime import datetime

import pytest

from app.models import (
    ASINRecord,
    ASINStatus,
    GenerationMode,
    Job,
    JobStatus,
    ReferenceProfile,
)


class TestJobModel:
    def test_create_job_defaults(self, db_session):
        """Job should have sensible defaults for counters and status."""
        job = Job(id=uuid.uuid4())
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        assert job.status == JobStatus.pending
        assert job.total_asins == 0
        assert job.completed_asins == 0
        assert job.failed_asins == 0
        assert job.estimated_cost_usd == 0.0

    def test_job_status_values(self):
        """All expected status values should exist."""
        assert JobStatus.pending.value == "pending"
        assert JobStatus.confirming.value == "confirming"
        assert JobStatus.processing.value == "processing"
        assert JobStatus.completed.value == "completed"
        assert JobStatus.failed.value == "failed"
        assert JobStatus.paused.value == "paused"

    def test_job_relationship_to_asin_records(self, db_session):
        """Job.asin_records should reflect child ASINRecord rows."""
        job = Job(id=uuid.uuid4(), total_asins=2)
        db_session.add(job)
        db_session.flush()

        r1 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0AAABBBCC")
        r2 = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B0DDDEEEFF")
        db_session.add_all([r1, r2])
        db_session.commit()
        db_session.refresh(job)

        assert len(job.asin_records) == 2
        asins = {r.asin for r in job.asin_records}
        assert asins == {"B0AAABBBCC", "B0DDDEEEFF"}

    def test_job_cascade_delete(self, db_session):
        """Deleting a Job should cascade-delete its ASINRecords."""
        job = Job(id=uuid.uuid4())
        db_session.add(job)
        db_session.flush()

        record = ASINRecord(id=uuid.uuid4(), job_id=job.id, asin="B012345678")
        db_session.add(record)
        db_session.commit()

        db_session.delete(job)
        db_session.commit()

        remaining = db_session.query(ASINRecord).filter(
            ASINRecord.asin == "B012345678"
        ).first()
        assert remaining is None


class TestASINRecordModel:
    def test_create_asin_record_defaults(self, db_session, sample_job):
        """ASINRecord should default to queued status and text_only mode."""
        record = ASINRecord(
            id=uuid.uuid4(),
            job_id=sample_job.id,
            asin="B0TESTTEST",
        )
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.status == ASINStatus.queued
        assert record.generation_mode == GenerationMode.text_only
        assert record.retry_count == 0
        assert record.input_tokens == 0

    def test_asin_status_values(self):
        """All ASIN status values should be present."""
        expected = {"queued", "processing", "completed", "needs_review",
                    "missing_data", "failed", "approved"}
        actual = {s.value for s in ASINStatus}
        assert actual == expected

    def test_generation_mode_values(self):
        assert GenerationMode.vision.value == "vision"
        assert GenerationMode.text_only.value == "text_only"

    def test_json_fields_store_data(self, db_session, sample_job):
        """JSON columns (source_data, generated_data, etc.) should round-trip."""
        record = ASINRecord(
            id=uuid.uuid4(),
            job_id=sample_job.id,
            asin="B0JSONTEST",
            source_data={"brand": "Acme", "price": "19.99"},
            generated_data={"title": "Acme Widget"},
            image_paths=["/img/a.jpg", "/img/b.jpg"],
            missing_fields=["color", "size"],
        )
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.source_data["brand"] == "Acme"
        assert record.generated_data["title"] == "Acme Widget"
        assert len(record.image_paths) == 2
        assert "color" in record.missing_fields


class TestReferenceProfileModel:
    def test_create_reference_profile(self, db_session):
        """ReferenceProfile should store optimization pattern data."""
        profile = ReferenceProfile(
            id=uuid.uuid4(),
            name="Competitor A",
            tone="conversational",
            bullet_style="question-answer",
            formatting_rules=["Rule 1", "Rule 2"],
            example_content={"title": "Example Title"},
        )
        db_session.add(profile)
        db_session.commit()
        db_session.refresh(profile)

        assert profile.name == "Competitor A"
        assert profile.tone == "conversational"
        assert len(profile.formatting_rules) == 2
        assert profile.example_content["title"] == "Example Title"

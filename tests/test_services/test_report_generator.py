"""Tests for report_generator service."""

import uuid

import pytest

from app.models import ASINRecord, ASINStatus, GenerationMode
from app.services.report_generator import (
    ReportData,
    _esc,
    _missing_fields_html,
    render_report_card_html,
    report_data_from_asin_record,
)


class TestEsc:
    def test_escapes_html(self):
        assert _esc("<script>alert('xss')</script>") == "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"

    def test_none_returns_empty(self):
        assert _esc(None) == ""

    def test_normal_string_unchanged(self):
        assert _esc("Hello World") == "Hello World"


class TestReportDataFromASINRecord:
    def test_basic_conversion(self, db_session, completed_asin_record):
        data = report_data_from_asin_record(completed_asin_record)
        assert data.asin == "B0COMPLETE"
        assert data.title == "TestBrand Widget - Premium Quality"
        assert len(data.bullet_points) == 5
        assert data.confidence_score == 0.92
        assert data.generation_mode == "text_only"

    def test_empty_record(self, db_session, sample_job):
        """A fresh record with no generated data should still convert."""
        record = ASINRecord(
            id=uuid.uuid4(),
            job_id=sample_job.id,
            asin="B0EMPTYYYY",
            source_data={},
            generated_data={},
            generation_mode=GenerationMode.text_only,
        )
        db_session.add(record)
        db_session.commit()

        data = report_data_from_asin_record(record)
        assert data.asin == "B0EMPTYYYY"
        assert data.title == ""
        assert data.bullet_points == []
        assert data.confidence_score == 0.0

    def test_field_sources_populated(self, db_session, sample_job):
        record = ASINRecord(
            id=uuid.uuid4(),
            job_id=sample_job.id,
            asin="B0FIELDSRC",
            source_data={"title": "From Source"},
            generated_data={"description": "From AI"},
            generation_mode=GenerationMode.text_only,
        )
        db_session.add(record)
        db_session.commit()

        data = report_data_from_asin_record(record)
        assert data.field_sources["title"] == "source"
        assert data.field_sources["description"] == "ai"
        assert data.field_sources["search_terms"] == "missing"


class TestRenderReportCardHTML:
    def test_basic_render(self):
        data = ReportData(
            asin="B08N5WRWNW",
            title="Test Widget",
            bullet_points=["Feature 1", "Feature 2"],
            description="A great widget.",
            search_terms="widget premium",
            confidence_score=0.90,
            generation_mode="text_only",
            field_sources={"title": "ai", "description": "ai"},
        )
        html = render_report_card_html(data)
        assert "B08N5WRWNW" in html
        assert "Test Widget" in html
        assert "Feature 1" in html
        assert "90%" in html

    def test_missing_fields_shown(self):
        data = ReportData(
            asin="B08N5WRWNW",
            missing_fields=["color", "size"],
        )
        html = render_report_card_html(data)
        assert "color" in html
        assert "size" in html
        assert "Missing Required Fields" in html

    def test_xss_prevention(self):
        """User-controlled data should be HTML-escaped."""
        data = ReportData(
            asin="<script>alert(1)</script>",
            title="<img onerror=alert(1)>",
        )
        html = render_report_card_html(data)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestMissingFieldsHTML:
    def test_empty_missing_returns_empty(self):
        assert _missing_fields_html([]) == ""
        assert _missing_fields_html(None) == ""

    def test_renders_missing_fields(self):
        html = _missing_fields_html(["color", "size"])
        assert "color" in html
        assert "size" in html
        assert "Missing Required Fields" in html

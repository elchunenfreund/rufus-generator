"""Tests for sheets_exporter service."""

import pytest

from app.services.sheets_exporter import EXPORT_COLUMNS, _sanitize_cell


class TestSanitizeCell:
    def test_normal_string_unchanged(self):
        assert _sanitize_cell("Hello World") == "Hello World"

    def test_formula_injection_equals(self):
        """Cells starting with = should be neutralized."""
        assert _sanitize_cell("=SUM(A1:A10)").startswith("'")

    def test_formula_injection_plus(self):
        assert _sanitize_cell("+1234567890").startswith("'")

    def test_formula_injection_minus(self):
        assert _sanitize_cell("-1+1").startswith("'")

    def test_formula_injection_at(self):
        assert _sanitize_cell("@SUM(A1)").startswith("'")

    def test_tab_before_formula(self):
        """Tab followed by = should be caught (lstrip reveals =)."""
        assert _sanitize_cell("\t=SUM(A1)").startswith("'")

    def test_newline_before_formula(self):
        """Newline followed by = should be caught."""
        assert _sanitize_cell("\n=SUM(A1)").startswith("'")

    def test_empty_string_unchanged(self):
        assert _sanitize_cell("") == ""

    def test_none_returns_none(self):
        assert _sanitize_cell(None) is None

    def test_leading_whitespace_then_formula(self):
        """Leading whitespace before formula char should still be caught."""
        result = _sanitize_cell("  =SUM(A1)")
        assert result.startswith("'")


class TestExportColumns:
    def test_essential_columns_present(self):
        """All essential Amazon flat file columns should be in the export."""
        assert "asin" in EXPORT_COLUMNS
        assert "title" in EXPORT_COLUMNS
        assert "bullet_point_1" in EXPORT_COLUMNS
        assert "description" in EXPORT_COLUMNS
        assert "search_terms" in EXPORT_COLUMNS
        assert "confidence_score" in EXPORT_COLUMNS

    def test_backend_attributes_present(self):
        for attr in ["color", "size", "material", "intended_use",
                      "country_of_origin"]:
            assert attr in EXPORT_COLUMNS

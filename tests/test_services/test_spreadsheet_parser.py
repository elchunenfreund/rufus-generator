"""Tests for spreadsheet_parser service."""

import csv
import tempfile
from pathlib import Path

import pytest

from app.services.spreadsheet_parser import (
    ParsedSpreadsheet,
    _detect_asin_column,
    _detect_asin_column_by_content,
    _is_asin,
    _sanitize_cell,
    parse_csv,
    parse_file,
)


class TestSanitizeCell:
    def test_normal_string_unchanged(self):
        assert _sanitize_cell("Hello World") == "Hello World"

    def test_none_becomes_empty(self):
        assert _sanitize_cell(None) == ""

    def test_integer_converted(self):
        assert _sanitize_cell(42) == "42"

    def test_truncates_long_strings(self):
        long = "x" * 3000
        result = _sanitize_cell(long, max_length=2000)
        assert len(result) == 2000

    def test_strips_control_characters(self):
        # \x01 is a control character that should be removed
        result = _sanitize_cell("hello\x01world")
        assert result == "helloworld"

    def test_preserves_newlines_and_tabs(self):
        result = _sanitize_cell("line1\nline2\tcol")
        assert "\n" in result
        assert "\t" in result


class TestIsASIN:
    def test_valid_asin(self):
        assert _is_asin("B08N5WRWNW") is True

    def test_valid_asin_lowercase(self):
        assert _is_asin("b08n5wrwnw") is True

    def test_too_short(self):
        assert _is_asin("B08N5") is False

    def test_too_long(self):
        assert _is_asin("B08N5WRWNWX") is False

    def test_special_characters(self):
        assert _is_asin("B08N5-RWNW") is False

    def test_empty_string(self):
        assert _is_asin("") is False


class TestDetectASINColumn:
    def test_exact_match_asin(self):
        assert _detect_asin_column(["asin", "brand", "title"]) == "asin"

    def test_exact_match_sku(self):
        assert _detect_asin_column(["sku", "name"]) == "sku"

    def test_case_insensitive(self):
        assert _detect_asin_column(["ASIN", "Brand"]) == "ASIN"

    def test_no_match(self):
        assert _detect_asin_column(["product", "name", "price"]) is None

    def test_amazon_asin_variant(self):
        assert _detect_asin_column(["amazon_asin", "title"]) == "amazon_asin"


class TestDetectASINColumnByContent:
    def test_detects_by_data_pattern(self):
        headers = ["id", "code"]
        rows = [
            {"id": "123", "code": "B08N5WRWNW"},
            {"id": "456", "code": "B09XYZ1234"},
            {"id": "789", "code": "B07ABCDEFG"},
        ]
        result = _detect_asin_column_by_content(headers, rows)
        assert result == "code"

    def test_returns_none_for_no_asins(self):
        headers = ["id", "name"]
        rows = [{"id": "1", "name": "Widget"}]
        result = _detect_asin_column_by_content(headers, rows)
        assert result is None

    def test_empty_rows(self):
        assert _detect_asin_column_by_content(["a"], []) is None


class TestParseCSV:
    def test_parse_valid_csv(self, tmp_path):
        """Parse a CSV with ASIN column."""
        csv_file = tmp_path / "test.csv"
        csv_file.write_text("asin,brand,title\nB08N5WRWNW,Acme,Widget\nB09XYZ1234,Beta,Gadget\n")

        result = parse_csv(csv_file)

        assert result.asin_column == "asin"
        assert len(result.rows) == 2
        assert len(result.detected_asins) == 2
        assert "B08N5WRWNW" in result.detected_asins
        assert not result.errors

    def test_parse_csv_no_asin_column(self, tmp_path):
        """CSV without ASIN column should report an error."""
        csv_file = tmp_path / "no_asin.csv"
        csv_file.write_text("name,price\nWidget,9.99\n")

        result = parse_csv(csv_file)
        assert result.asin_column is None
        assert any("No ASIN column" in e for e in result.errors)

    def test_parse_csv_skips_empty_rows(self, tmp_path):
        csv_file = tmp_path / "empty_rows.csv"
        csv_file.write_text("asin,brand\nB08N5WRWNW,Acme\n,,\nB09XYZ1234,Beta\n")

        result = parse_csv(csv_file)
        assert len(result.rows) == 2

    def test_parse_csv_detects_duplicates(self, tmp_path):
        csv_file = tmp_path / "dupes.csv"
        csv_file.write_text("asin,brand\nB08N5WRWNW,Acme\nB08N5WRWNW,Acme2\n")

        result = parse_csv(csv_file)
        assert any("Duplicate" in e for e in result.errors)

    def test_parse_csv_semicolon_delimited(self, tmp_path):
        csv_file = tmp_path / "semi.csv"
        csv_file.write_text("asin;brand;title\nB08N5WRWNW;Acme;Widget\n")

        result = parse_csv(csv_file)
        assert len(result.rows) == 1
        assert result.asin_column == "asin"


class TestParseFile:
    def test_unsupported_extension(self, tmp_path):
        bad_file = tmp_path / "data.txt"
        bad_file.write_text("some data")

        result = parse_file(bad_file)
        assert any("Unsupported" in e for e in result.errors)

    def test_routes_csv(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_text("asin,brand\nB08N5WRWNW,Acme\n")

        result = parse_file(csv_file)
        assert result.asin_column == "asin"
        assert len(result.detected_asins) == 1

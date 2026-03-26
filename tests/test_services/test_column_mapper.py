"""Tests for column_mapper service."""

import json
from pathlib import Path

import pytest

from app.services.column_mapper import (
    MappingConfig,
    auto_detect_columns,
    load_mapping_config,
    map_row,
    validate_template_headers,
)


@pytest.fixture()
def mapping_config(tmp_path) -> MappingConfig:
    """Create a test mapping config."""
    config_data = {
        "template": "test_template",
        "template_version": "1.0",
        "source_mappings": {
            "catalog": {
                "asin": None,
                "brand": None,
                "color": None,
                "size": None,
            }
        },
        "ai_generated_fields": ["title", "bullet_point_1", "description"],
        "required_backend_attributes": ["color", "size"],
        "required_fields": ["asin", "brand", "title"],
        "validation_rules": {"title_max_chars": 200},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_data))
    return load_mapping_config(path)


class TestLoadMappingConfig:
    def test_load_default_config(self):
        """The real default.json should load without errors."""
        config_path = (
            Path(__file__).parent.parent.parent / "config" / "mappings" / "default.json"
        )
        config = load_mapping_config(config_path)
        assert config.template == "flat_file_generic"
        assert len(config.ai_generated_fields) > 0
        assert len(config.required_fields) > 0

    def test_config_fields_populated(self, mapping_config):
        assert mapping_config.template == "test_template"
        assert "title" in mapping_config.ai_generated_fields
        assert "asin" in mapping_config.required_fields


class TestAutoDetectColumns:
    def test_exact_header_match(self, mapping_config):
        headers = ["asin", "brand", "color", "size", "price"]
        detected = auto_detect_columns(headers, mapping_config)
        assert detected["asin"] == "asin"
        assert detected["brand"] == "brand"

    def test_case_insensitive_match(self, mapping_config):
        headers = ["ASIN", "Brand", "Color"]
        detected = auto_detect_columns(headers, mapping_config)
        assert "asin" in detected
        assert "brand" in detected

    def test_partial_match(self, mapping_config):
        headers = ["product_asin", "brand_name"]
        detected = auto_detect_columns(headers, mapping_config)
        # "asin" is substring of "product_asin"
        assert "asin" in detected

    def test_no_match_returns_empty(self, mapping_config):
        headers = ["foo", "bar", "baz"]
        detected = auto_detect_columns(headers, mapping_config)
        assert len(detected) == 0


class TestMapRow:
    def test_map_row_with_data(self, mapping_config):
        row = {"asin": "B08N5WRWNW", "brand": "Acme", "color": "Red"}
        column_mapping = {"asin": "asin", "brand": "brand", "color": "color", "size": "size"}

        result = map_row(row, column_mapping, mapping_config)

        assert result.mapped_data["asin"] == "B08N5WRWNW"
        assert result.mapped_data["brand"] == "Acme"
        assert result.mapped_data["color"] == "Red"
        assert "size" in result.unmapped_fields  # not in row

    def test_map_row_empty_values_are_unmapped(self, mapping_config):
        row = {"asin": "B08N5WRWNW", "brand": ""}
        column_mapping = {"asin": "asin", "brand": "brand"}

        result = map_row(row, column_mapping, mapping_config)
        assert "brand" in result.unmapped_fields

    def test_ai_fields_always_needed(self, mapping_config):
        row = {"asin": "B08N5WRWNW"}
        column_mapping = {"asin": "asin"}

        result = map_row(row, column_mapping, mapping_config)
        assert "title" in result.ai_fields_needed
        assert "description" in result.ai_fields_needed

    def test_missing_required_generates_warning(self, mapping_config):
        row = {"asin": "B08N5WRWNW"}
        column_mapping = {"asin": "asin", "brand": None}

        result = map_row(row, column_mapping, mapping_config)
        assert any("Required fields missing" in w for w in result.warnings)


class TestValidateTemplateHeaders:
    def test_all_headers_present(self, mapping_config):
        headers = ["asin", "brand", "color", "size"]
        warnings = validate_template_headers(headers, mapping_config)
        assert len(warnings) == 0

    def test_missing_headers_warn(self, mapping_config):
        headers = ["asin"]
        warnings = validate_template_headers(headers, mapping_config)
        assert len(warnings) > 0
        assert any("not found" in w for w in warnings)

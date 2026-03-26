"""Tests for content_generator service — uses mocks for Claude API."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.services.content_generator import (
    GenerationResult,
    _build_reference_context,
    _build_vision_context,
    _validate_field,
    generate_listing,
)
from app.services.vision_ai import VisionResult


@pytest.fixture()
def mock_rate_limiter():
    with patch("app.services.content_generator.rate_limiter") as mock_rl:
        mock_rl.wait_for_slot.return_value = True
        mock_rl.release = MagicMock()
        mock_rl.on_success = MagicMock()
        mock_rl.on_rate_limit = MagicMock()
        yield mock_rl


class TestValidateField:
    def test_normal_field(self):
        assert _validate_field("Hello World") == "Hello World"

    def test_empty_string_returns_empty(self):
        assert _validate_field("") == ""

    def test_none_returns_empty(self):
        assert _validate_field(None) == ""

    def test_truncates_at_max_len(self):
        result = _validate_field("x" * 300, max_len=200)
        assert len(result) == 200

    def test_strips_whitespace(self):
        assert _validate_field("  hello  ") == "hello"


class TestBuildVisionContext:
    def test_no_vision_returns_empty(self):
        assert _build_vision_context(None) == ""

    def test_errored_vision_returns_empty(self):
        v = VisionResult(error="Something went wrong")
        assert _build_vision_context(v) == ""

    def test_populated_vision_includes_sections(self):
        v = VisionResult(
            features=["metal body"],
            selling_points=["sleek"],
            use_cases=["office"],
            packaging_text=["Made in USA"],
            quality_notes="Premium",
            suggested_alt_text="Metal widget",
        )
        ctx = _build_vision_context(v)
        assert "metal body" in ctx
        assert "sleek" in ctx
        assert "office" in ctx
        assert "Made in USA" in ctx
        assert "Premium" in ctx
        assert "Metal widget" in ctx


class TestBuildReferenceContext:
    def test_none_returns_empty(self):
        assert _build_reference_context(None) == ""

    def test_populated_profile(self, sample_reference_profile):
        ctx = _build_reference_context(sample_reference_profile)
        assert "professional" in ctx
        assert "feature-benefit" in ctx
        assert "Capitalize first word" in ctx


class TestGenerateListing:
    def test_rate_limit_timeout(self):
        with patch("app.services.content_generator.rate_limiter") as mock_rl:
            mock_rl.wait_for_slot.return_value = False
            result = generate_listing("B08N5WRWNW", {"brand": "Acme"})
            assert result.error is not None
            assert "timeout" in result.error.lower()

    @patch("app.services.content_generator.anthropic.Anthropic")
    def test_successful_generation(self, mock_anthropic_cls, mock_rate_limiter):
        """Mock a successful content generation response."""
        gen_json = {
            "title": "Acme Widget Pro - Premium Quality for Everyday Use",
            "bullet_point_1": "DURABLE CONSTRUCTION - Built to last",
            "bullet_point_2": "EASY SETUP - Ready in minutes",
            "bullet_point_3": "VERSATILE DESIGN - Multiple use cases",
            "bullet_point_4": "LIGHTWEIGHT - Easy to carry",
            "bullet_point_5": "GREAT VALUE - Affordable premium quality",
            "description": "The Acme Widget Pro is your go-to solution.",
            "search_terms": "premium widget durable lightweight everyday",
            "image_alt_text": "Acme Widget Pro in silver finish",
            "color": "Silver",
            "size": "10x5x3 inches",
            "material": "Aluminum alloy",
            "intended_use": "General purpose",
            "age_range_description": "Adult",
            "target_audience": "Home users",
            "item_form": "Solid",
            "special_features": "Corrosion resistant",
            "included_components": "1x Widget, 1x Quick Start Guide",
            "country_of_origin": "United States",
        }
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(gen_json))]
        mock_response.usage.input_tokens = 2000
        mock_response.usage.output_tokens = 800

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_cls.return_value = mock_client

        result = generate_listing("B08N5WRWNW", {"brand": "Acme", "product_name": "Widget"})

        assert result.error is None
        assert "Acme Widget Pro" in result.title
        assert len(result.bullet_points) == 5
        assert result.description != ""
        assert result.search_terms != ""
        assert result.backend_attributes["color"] == "Silver"
        assert result.input_tokens == 2000

    @patch("app.services.content_generator.anthropic.Anthropic")
    def test_json_parse_error_handled(self, mock_anthropic_cls, mock_rate_limiter):
        """Invalid JSON from API should be caught."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="This is not JSON at all")]
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_cls.return_value = mock_client

        result = generate_listing("B08N5WRWNW", {"brand": "Acme"})
        assert result.error is not None
        assert "parse" in result.error.lower()

    @patch("app.services.content_generator.anthropic.Anthropic")
    def test_rate_limit_error_from_api(self, mock_anthropic_cls, mock_rate_limiter):
        """Claude 429 should be caught and reported."""
        import anthropic

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = anthropic.RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        mock_anthropic_cls.return_value = mock_client

        result = generate_listing("B08N5WRWNW", {"brand": "Acme"})
        assert result.error is not None
        assert "Rate limited" in result.error
        mock_rate_limiter.on_rate_limit.assert_called_once()

    @patch("app.services.content_generator.anthropic.Anthropic")
    def test_title_truncated_to_max_length(self, mock_anthropic_cls, mock_rate_limiter):
        """Title over 200 chars should be truncated."""
        gen_json = {
            "title": "A" * 300,
            "bullet_point_1": "B1",
            "description": "Desc",
            "search_terms": "terms",
        }
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(gen_json))]
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_cls.return_value = mock_client

        result = generate_listing("B08N5WRWNW", {"brand": "Acme"})
        assert len(result.title) == 200

"""Tests for vision_ai service — uses mocks for Claude API and storage."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.vision_ai import VisionResult, analyze_images


@pytest.fixture(autouse=True)
def _ensure_json_imported():
    """Ensure the `json` module is available in vision_ai's local scope.

    The production code does `import json` inside the try block, which means
    the `except json.JSONDecodeError` clause can fail with UnboundLocalError
    if an exception is raised before the import line runs.
    """
    import app.services.vision_ai as mod
    if not hasattr(mod, "json"):
        mod.json = json
    yield


@pytest.fixture()
def mock_rate_limiter():
    with patch("app.services.vision_ai.rate_limiter") as mock_rl:
        mock_rl.wait_for_slot.return_value = True
        mock_rl.release = MagicMock()
        mock_rl.on_success = MagicMock()
        mock_rl.on_rate_limit = MagicMock()
        yield mock_rl


@pytest.fixture()
def mock_storage():
    """Mock the storage backend so path traversal checks don't interfere."""
    with patch("app.services.vision_ai.storage") as mock_s:
        mock_s.exists.return_value = True
        mock_s.read.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        yield mock_s


@pytest.fixture()
def fake_image(tmp_path):
    """Create a tiny fake JPEG file."""
    img_path = tmp_path / "test.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    return str(img_path)


class TestAnalyzeImages:
    def test_no_images_returns_error(self, mock_rate_limiter):
        result = analyze_images([], "B08N5WRWNW")
        assert result.error == "No images provided"

    def test_rate_limited_returns_error(self):
        with patch("app.services.vision_ai.rate_limiter") as mock_rl:
            mock_rl.wait_for_slot.return_value = False
            result = analyze_images(["/fake/img.jpg"], "B08N5WRWNW")
            assert "timeout" in result.error.lower()

    @patch("app.services.vision_ai.anthropic.Anthropic")
    def test_successful_analysis(self, mock_anthropic_cls, mock_rate_limiter, mock_storage, fake_image):
        """Mock a successful Claude API response."""
        vision_json = {
            "features": ["metal body", "rubberized grip"],
            "selling_points": ["sleek design"],
            "use_cases": ["everyday carry"],
            "packaging_text": ["Made in USA"],
            "quality_notes": "Premium build quality",
            "suggested_alt_text": "Metal widget with rubberized grip",
        }
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(vision_json))]
        mock_response.usage.input_tokens = 1000
        mock_response.usage.output_tokens = 200

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_cls.return_value = mock_client

        result = analyze_images([fake_image], "B08N5WRWNW")

        assert result.error is None
        assert "metal body" in result.features
        assert result.input_tokens == 1000
        assert result.output_tokens == 200
        assert result.suggested_alt_text == "Metal widget with rubberized grip"

    @patch("app.services.vision_ai.anthropic.Anthropic")
    def test_json_in_code_block(self, mock_anthropic_cls, mock_rate_limiter, mock_storage, fake_image):
        """Claude sometimes wraps JSON in markdown code blocks."""
        vision_json = {"features": ["feature1"], "selling_points": [], "use_cases": [],
                       "packaging_text": [], "quality_notes": "", "suggested_alt_text": ""}
        wrapped = f"```json\n{json.dumps(vision_json)}\n```"

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=wrapped)]
        mock_response.usage.input_tokens = 500
        mock_response.usage.output_tokens = 100

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_cls.return_value = mock_client

        result = analyze_images([fake_image], "B08N5WRWNW")
        assert result.error is None
        assert "feature1" in result.features

    @pytest.mark.xfail(
        reason="App bug: `import json` is inside try block, causing "
               "UnboundLocalError in except clause when API errors occur "
               "before the import line runs.",
        strict=True,
    )
    @patch("app.services.vision_ai.anthropic.Anthropic")
    def test_api_error_handled(self, mock_anthropic_cls, mock_rate_limiter, mock_storage, fake_image):
        """API errors should be caught and reported.

        NOTE: This test documents a real bug — `import json` is inside the
        try block at line 115 of vision_ai.py, so when an exception is raised
        before that line, `except json.JSONDecodeError` at line 140 triggers
        an UnboundLocalError.
        """
        import anthropic
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = anthropic.APIError(
            message="Server error",
            request=MagicMock(),
            body=None,
        )
        mock_anthropic_cls.return_value = mock_client

        result = analyze_images([fake_image], "B08N5WRWNW")
        assert result.error is not None
        assert "API error" in result.error

    def test_nonexistent_image_skipped(self, mock_rate_limiter):
        """Images that don't exist on disk should be skipped."""
        with patch("app.services.vision_ai.storage") as mock_s:
            mock_s.exists.return_value = False
            result = analyze_images(["/nonexistent/fake.jpg"], "B08N5WRWNW")
            assert result.error == "No valid images found on disk"

    @patch("app.services.vision_ai.anthropic.Anthropic")
    def test_max_five_images(self, mock_anthropic_cls, mock_rate_limiter, mock_storage, tmp_path):
        """Only the first 5 images should be sent to the API."""
        images = [f"/img/img{i}.jpg" for i in range(8)]

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"features":[],"selling_points":[],"use_cases":[],"packaging_text":[],"quality_notes":"","suggested_alt_text":""}')]
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_cls.return_value = mock_client

        result = analyze_images(images, "B08N5WRWNW")
        assert result.error is None

        # Check that the API call received at most 5 image blocks + 1 text block
        call_args = mock_client.messages.create.call_args
        content = call_args.kwargs["messages"][0]["content"]
        image_blocks = [c for c in content if c["type"] == "image"]
        assert len(image_blocks) == 5

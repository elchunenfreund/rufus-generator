"""Tests for pdf_analyzer service — uses mocks for Claude API and pdfplumber."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.pdf_analyzer import PDFAnalysisResult, analyze_pdf_with_vision


@pytest.fixture()
def mock_rate_limiter():
    """Patch the rate_limiter singleton."""
    mock_rl = MagicMock()
    mock_rl.wait_for_slot.return_value = True
    mock_rl.release = MagicMock()
    with patch("app.services.rate_limiter.rate_limiter", mock_rl):
        yield mock_rl


@pytest.fixture()
def mock_pdfplumber():
    """Mock pdfplumber since it's imported inline inside analyze_pdf_with_vision."""
    mock_module = MagicMock()
    with patch.dict(sys.modules, {"pdfplumber": mock_module}):
        yield mock_module


class TestAnalyzePDFWithVision:
    def test_rate_limit_timeout(self):
        mock_rl = MagicMock()
        mock_rl.wait_for_slot.return_value = False
        with patch("app.services.rate_limiter.rate_limiter", mock_rl):
            result = analyze_pdf_with_vision(Path("/fake/file.pdf"))
        assert result.error is not None
        assert "Rate limited" in result.error

    @patch("app.services.pdf_analyzer.anthropic.Anthropic")
    def test_successful_analysis(self, mock_anthropic_cls, mock_rate_limiter, mock_pdfplumber):
        """Mock a full successful PDF analysis flow."""
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="white")

        mock_page = MagicMock()
        mock_page_image = MagicMock()
        mock_page_image.original = img
        mock_page.to_image.return_value = mock_page_image

        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdfplumber.open.return_value = mock_pdf

        analysis_json = {
            "tone": "professional",
            "bullet_style": "feature-benefit",
            "keyword_density": "moderate",
            "title_pattern": "Brand + Product + Feature",
            "formatting_rules": ["Capitalize key features"],
            "example_content": {"title_example": "BrandX Widget Pro"},
        }
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(analysis_json))]
        mock_response.usage.input_tokens = 5000
        mock_response.usage.output_tokens = 500

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_cls.return_value = mock_client

        result = analyze_pdf_with_vision(Path("/fake/file.pdf"))

        assert result.error is None
        assert result.tone == "professional"
        assert result.bullet_style == "feature-benefit"
        assert result.input_tokens == 5000
        assert "Capitalize key features" in result.formatting_rules

    def test_empty_pdf(self, mock_rate_limiter, mock_pdfplumber):
        """PDF with no extractable pages should return error."""
        mock_pdf = MagicMock()
        mock_pdf.pages = []
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdfplumber.open.return_value = mock_pdf

        result = analyze_pdf_with_vision(Path("/fake/empty.pdf"))
        assert result.error is not None
        assert "No pages" in result.error

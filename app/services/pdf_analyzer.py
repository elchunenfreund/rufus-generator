"""Analyze competitor listing PDFs to extract optimization patterns using Claude vision."""

from dataclasses import dataclass
from pathlib import Path

import anthropic

from app.config import get_logger, settings

log = get_logger(__name__)

ANALYSIS_PROMPT = """You are an Amazon listing optimization expert. Analyze this PDF report
of optimized Amazon product listings. Extract the specific patterns and strategies used.

Return your analysis as a JSON object with these exact fields:

{
  "tone": "the overall writing tone (e.g., 'professional yet conversational', 'technical and precise')",
  "bullet_style": "how bullet points are structured (e.g., 'question-answer format', 'feature-benefit pairs', 'use-case focused')",
  "keyword_density": "how keywords are incorporated (e.g., 'moderate - naturally woven in', 'high - keyword-rich but readable')",
  "title_pattern": "the pattern used for titles (e.g., 'Brand + Product Type + Key Feature + Size/Color + Use Case')",
  "formatting_rules": [
    "list of specific formatting rules observed",
    "e.g., 'Each bullet point starts with a capitalized key feature'",
    "e.g., 'Description uses short paragraphs with scenario-based language'",
    "e.g., 'Search terms avoid brand name and use natural language phrases'"
  ],
  "example_content": {
    "title_example": "an example optimized title from the report",
    "bullet_example": "an example optimized bullet point",
    "description_example": "a snippet of optimized description text",
    "search_terms_example": "example search terms format",
    "backend_attributes_example": "example of how backend attributes are populated"
  }
}

Be specific. Extract actual patterns from what you see, not generic advice."""


@dataclass
class PDFAnalysisResult:
    tone: str = ""
    bullet_style: str = ""
    keyword_density: str = ""
    title_pattern: str = ""
    formatting_rules: list[str] | None = None
    example_content: dict | None = None
    raw_response: str = ""
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def analyze_pdf_with_vision(pdf_path: Path) -> PDFAnalysisResult:
    """Send PDF pages as images to Claude vision API for pattern extraction."""
    import base64

    import pdfplumber

    from app.services.rate_limiter import rate_limiter

    result = PDFAnalysisResult()

    # Rate limit: PDF analysis is expensive (multi-image vision call)
    if not rate_limiter.wait_for_slot(timeout=60):
        result.error = "Rate limited — too many concurrent requests. Try again shortly."
        return result

    released = False
    try:
        # Extract pages as images
        images = []
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages[:10]):  # max 10 pages
                img = page.to_image(resolution=200)
                img_bytes = img.original
                import io

                buf = io.BytesIO()
                img_bytes.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                images.append(b64)

        if not images:
            result.error = "No pages could be extracted from PDF"
            rate_limiter.release()
            return result

        # Build message with images
        content = []
        for i, b64_img in enumerate(images):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64_img,
                    },
                }
            )
        content.append({"type": "text", "text": ANALYSIS_PROMPT})

        # Call Claude API
        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=120.0,
        )
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": content}],
        )

        result.input_tokens = response.usage.input_tokens
        result.output_tokens = response.usage.output_tokens
        rate_limiter.release(tokens_used=result.input_tokens + result.output_tokens)
        released = True
        result.raw_response = response.content[0].text

        # Parse JSON response
        import json

        # Find JSON in the response (may be wrapped in markdown code blocks)
        text = result.raw_response
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        data = json.loads(text.strip())

        result.tone = data.get("tone", "")
        result.bullet_style = data.get("bullet_style", "")
        result.keyword_density = data.get("keyword_density", "")
        result.title_pattern = data.get("title_pattern", "")
        result.formatting_rules = data.get("formatting_rules", [])
        result.example_content = data.get("example_content", {})

        log.info(
            "pdf_analyzed",
            pages=len(images),
            tone=result.tone,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    except json.JSONDecodeError as e:
        if not released:
            rate_limiter.release()
        result.error = f"Failed to parse AI response as JSON: {e}"
        log.warning("pdf_analysis_json_error", error=str(e), raw=result.raw_response[:500])
    except anthropic.APIError as e:
        if not released:
            rate_limiter.release()
        result.error = f"Claude API error: {e}"
        log.error("pdf_analysis_api_error", error=str(e))
    except Exception as e:
        if not released:
            rate_limiter.release()
        result.error = f"PDF analysis failed: {e}"
        log.error("pdf_analysis_error", error=str(e))

    return result


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract raw text from PDF as fallback."""
    import pdfplumber

    texts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:20]:
            text = page.extract_text()
            if text:
                texts.append(text)
    return "\n\n".join(texts)

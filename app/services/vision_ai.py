"""Vision AI service — analyze product images using Claude vision API."""

import base64
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from app.config import get_logger, settings
from app.services.rate_limiter import rate_limiter

log = get_logger(__name__)

VISION_PROMPT = """Analyze these product images for an Amazon listing. Extract:

1. **Physical features**: materials, colors, dimensions, weight indicators, texture
2. **Key selling points**: what stands out visually about this product
3. **Use cases**: how the product appears to be used based on lifestyle/context images
4. **Text on packaging**: any text, labels, certifications visible (OCR)
5. **Quality indicators**: build quality, finish, premium vs. budget appearance

Return a JSON object:
{
  "features": ["list of physical features observed"],
  "selling_points": ["list of key visual selling points"],
  "use_cases": ["list of apparent use cases"],
  "packaging_text": ["any text visible on product/packaging"],
  "quality_notes": "overall quality assessment",
  "suggested_alt_text": "A descriptive alt text for the primary product image that references at least 2 physical features"
}

Be specific and factual — describe only what you actually see."""


@dataclass
class VisionResult:
    features: list[str] = field(default_factory=list)
    selling_points: list[str] = field(default_factory=list)
    use_cases: list[str] = field(default_factory=list)
    packaging_text: list[str] = field(default_factory=list)
    quality_notes: str = ""
    suggested_alt_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


def analyze_images(image_paths: list[str], asin: str) -> VisionResult:
    """Send product images to Claude vision API for feature extraction."""
    result = VisionResult()

    if not image_paths:
        result.error = "No images provided"
        return result

    # Wait for rate limiter
    if not rate_limiter.wait_for_slot(timeout=120):
        result.error = "Rate limited — timeout waiting for API slot"
        return result

    released = False
    try:
        # Build content with images
        content = []
        for img_path in image_paths[:5]:  # max 5 images per ASIN
            path = Path(img_path)
            if not path.exists():
                log.warning("image_not_found", path=img_path, asin=asin)
                continue

            suffix = path.suffix.lower()
            media_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
            }.get(suffix, "image/jpeg")

            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")

            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })

        if not content:
            result.error = "No valid images found on disk"
            rate_limiter.release()
            released = True
            return result

        content.append({"type": "text", "text": VISION_PROMPT})

        # Call Claude API
        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=120.0,
        )
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=2048,
            messages=[{"role": "user", "content": content}],
        )

        result.input_tokens = response.usage.input_tokens
        result.output_tokens = response.usage.output_tokens
        total_tokens = result.input_tokens + result.output_tokens

        rate_limiter.release(tokens_used=total_tokens)
        released = True
        rate_limiter.on_success()

        # Parse response
        import json

        text = response.content[0].text
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        data = json.loads(text.strip())
        result.features = data.get("features", [])
        result.selling_points = data.get("selling_points", [])
        result.use_cases = data.get("use_cases", [])
        result.packaging_text = data.get("packaging_text", [])
        result.quality_notes = data.get("quality_notes", "")
        result.suggested_alt_text = data.get("suggested_alt_text", "")

        log.info("vision_analyzed", asin=asin, features=len(result.features),
                 tokens=total_tokens)

    except anthropic.RateLimitError:
        if not released:
            rate_limiter.release()
            released = True
        rate_limiter.on_rate_limit()
        result.error = "Rate limited by API — will retry"
    except json.JSONDecodeError as e:
        if not released:
            rate_limiter.release()
            released = True
        result.error = f"Failed to parse vision response: {e}"
        log.warning("vision_json_error", asin=asin, error=str(e))
    except anthropic.APIError as e:
        if not released:
            rate_limiter.release()
            released = True
        result.error = f"Claude API error: {e}"
        log.error("vision_api_error", asin=asin, error=str(e))
    except Exception as e:
        if not released:
            rate_limiter.release()
            released = True
        result.error = f"Vision analysis failed: {e}"
        log.error("vision_error", asin=asin, error=str(e))

    return result

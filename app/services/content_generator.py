"""Content generator — produce RUFUS/COSMOS-optimized Amazon listing content using Claude API.

Pulls optimization patterns from ReferenceProfile (if available) to inform generation style.
"""

import json
import re
from dataclasses import dataclass, field

import anthropic

from app.config import get_logger, settings
from app.models import ReferenceProfile
from app.services.rate_limiter import rate_limiter
from app.services.vision_ai import VisionResult

log = get_logger(__name__)


@dataclass
class GenerationResult:
    title: str = ""
    bullet_points: list[str] = field(default_factory=list)
    description: str = ""
    search_terms: str = ""
    backend_attributes: dict[str, str] = field(default_factory=dict)
    image_alt_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


def _build_reference_context(profile: ReferenceProfile | None) -> str:
    """Build prompt context from a reference profile."""
    if not profile:
        return ""

    parts = ["\n## Reference Style Guide (from competitor analysis)"]
    if profile.tone:
        parts.append(f"- Writing tone: {profile.tone}")
    if profile.bullet_style:
        parts.append(f"- Bullet point style: {profile.bullet_style}")
    if profile.keyword_density:
        parts.append(f"- Keyword approach: {profile.keyword_density}")
    if profile.title_pattern:
        parts.append(f"- Title pattern: {profile.title_pattern}")
    if profile.formatting_rules:
        parts.append("- Formatting rules:")
        for rule in profile.formatting_rules:
            parts.append(f"  - {rule}")
    if profile.example_content:
        parts.append("- Example content for reference:")
        for key, val in profile.example_content.items():
            parts.append(f"  - {key}: {val[:200]}")

    return "\n".join(parts)


def _build_vision_context(vision: VisionResult | None) -> str:
    """Build prompt context from vision AI analysis."""
    if not vision or vision.error:
        return ""

    parts = ["\n## Product Image Analysis"]
    if vision.features:
        parts.append(f"- Physical features: {', '.join(vision.features[:10])}")
    if vision.selling_points:
        parts.append(f"- Key selling points: {', '.join(vision.selling_points[:5])}")
    if vision.use_cases:
        parts.append(f"- Use cases observed: {', '.join(vision.use_cases[:5])}")
    if vision.packaging_text:
        parts.append(f"- Text on packaging: {', '.join(vision.packaging_text[:5])}")
    if vision.quality_notes:
        parts.append(f"- Quality assessment: {vision.quality_notes}")
    if vision.suggested_alt_text:
        parts.append(f"- Suggested image alt text: {vision.suggested_alt_text}")

    return "\n".join(parts)


SYSTEM_PROMPT = """You are an Amazon listing optimization expert specializing in RUFUS
(Amazon's AI shopping assistant) and COSMOS (Amazon's product understanding system).

Your job is to generate listing content that:
1. RUFUS: Uses intent-based phrasing so RUFUS can match customer questions to this product
2. COSMOS: Includes rich attributes and metadata for COSMOS product graph comprehension
3. Converts well: Persuades shoppers who find this listing via RUFUS queries

Key RUFUS optimization rules:
- Titles: Primary keyword + key differentiator + use case (max 200 chars)
- Bullet points: Each answers a specific customer question RUFUS might field
  (e.g., "Is this BPA-free?", "Does it keep drinks cold?"). Write as answers, not features.
- Search terms: Natural language phrases matching how customers ask RUFUS questions.
  NOT keyword-stuffed lists. Think "best insulated water bottle for hiking" not "water bottle insulated hiking gym"
- Description: Scenario-based, lifestyle context. Help COSMOS understand subjective qualities
  (gift-worthy, premium feel, professional grade).

Key COSMOS optimization rules:
- All 10 required backend attributes MUST be populated
- Use specific, measurable values (not "various" or "multiple")
- Include material composition, certifications, and compatibility info"""


def generate_listing(
    asin: str,
    source_data: dict[str, str],
    vision_result: VisionResult | None = None,
    reference_profile: ReferenceProfile | None = None,
) -> GenerationResult:
    """Generate RUFUS/COSMOS-optimized listing content for a single ASIN."""
    result = GenerationResult()
    released = False

    # Wait for rate limiter
    if not rate_limiter.wait_for_slot(timeout=120):
        result.error = "Rate limited — timeout waiting for API slot"
        return result

    try:
        # Build user prompt with all available context
        is_new_product = asin.startswith(("SKU-", "NEW-", "ROW-"))
        if is_new_product:
            user_parts = [f"Generate an optimized Amazon listing for a NEW product (ID: {asin}).\n"
                          "This product does not have an ASIN yet — we are creating the listing from scratch.\n"]
        else:
            user_parts = [f"Generate an optimized Amazon listing for ASIN: {asin}\n"]
        user_parts.append("## Source Product Data")
        for key, val in source_data.items():
            if val and len(str(val).strip()) > 0:
                # Sanitize: truncate long values, strip control chars
                clean_val = str(val).strip()[:500]
                user_parts.append(f"- {key}: {clean_val}")

        # Add vision context if available
        vision_ctx = _build_vision_context(vision_result)
        if vision_ctx:
            user_parts.append(vision_ctx)

        # Add reference profile context
        ref_ctx = _build_reference_context(reference_profile)
        if ref_ctx:
            user_parts.append(ref_ctx)

        user_parts.append("""
## Required Output (JSON)
Return ONLY a JSON object with these exact fields:
{
  "title": "optimized product title (max 200 chars)",
  "bullet_point_1": "first bullet point (max 500 chars) — answer a customer question",
  "bullet_point_2": "second bullet point",
  "bullet_point_3": "third bullet point",
  "bullet_point_4": "fourth bullet point",
  "bullet_point_5": "fifth bullet point",
  "description": "product description optimized for COSMOS (max 2000 chars)",
  "search_terms": "natural language search terms (max 250 chars, no brand name)",
  "image_alt_text": "descriptive alt text referencing 2+ physical features",
  "color": "specific color",
  "size": "specific size/dimensions",
  "material": "material composition",
  "intended_use": "primary use case",
  "age_range_description": "target age range",
  "target_audience": "target demographic",
  "item_form": "form factor",
  "special_features": "key special features",
  "included_components": "what's in the box",
  "country_of_origin": "manufacturing country"
}

IMPORTANT: You MUST fill in EVERY field above. Do not leave any field as an empty string.
If you don't know the exact value, make your best inference from the product data provided.""")

        user_prompt = "\n".join(user_parts)

        # Call Claude API
        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=120.0,
        )
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=3000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        result.input_tokens = response.usage.input_tokens
        result.output_tokens = response.usage.output_tokens
        total_tokens = result.input_tokens + result.output_tokens

        rate_limiter.release(tokens_used=total_tokens)
        released = True
        rate_limiter.on_success()

        # Parse response — extract JSON robustly
        text = response.content[0].text
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError:
            # Try to find the outermost JSON object with regex
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                # Fix common LLM JSON issues: trailing commas before }
                cleaned = re.sub(r',\s*([}\]])', r'\1', match.group())
                data = json.loads(cleaned)
            else:
                raise

        # Extract and validate each field
        result.title = _validate_field(data.get("title", ""), max_len=200)
        result.bullet_points = [
            _validate_field(data.get(f"bullet_point_{i}", ""), max_len=500)
            for i in range(1, 6)
        ]
        result.description = _validate_field(data.get("description", ""), max_len=2000)
        result.search_terms = _validate_field(data.get("search_terms", ""), max_len=250)
        result.image_alt_text = data.get("image_alt_text", "")

        # Backend attributes
        attr_keys = [
            "color", "size", "material", "intended_use", "age_range_description",
            "target_audience", "item_form", "special_features",
            "included_components", "country_of_origin",
        ]
        result.backend_attributes = {k: data.get(k, "") for k in attr_keys}

        log.info("content_generated", asin=asin, tokens=total_tokens,
                 has_vision=vision_result is not None and not vision_result.error,
                 has_reference=reference_profile is not None)

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
        result.error = f"Failed to parse generation response: {e}"
        log.warning("generation_json_error", asin=asin, error=str(e))
    except anthropic.APIError as e:
        if not released:
            rate_limiter.release()
            released = True
        result.error = f"Claude API error: {e}"
        log.error("generation_api_error", asin=asin, error=str(e))
    except Exception as e:
        if not released:
            rate_limiter.release()
            released = True
        result.error = f"Content generation failed: {e}"
        log.error("generation_error", asin=asin, error=str(e))

    return result


def _validate_field(value: str, max_len: int = 0, min_len: int = 1) -> str:
    """Validate a generated field — ensure non-empty and within length limits."""
    if not value or not isinstance(value, str):
        return ""
    value = value.strip()
    if len(value) < min_len:
        return ""
    if max_len and len(value) > max_len:
        value = value[:max_len]
    return value

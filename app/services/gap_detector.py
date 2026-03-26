"""Gap detector + confidence scorer — identify missing fields and score listing quality."""

from dataclasses import dataclass, field

from app.config import get_logger

log = get_logger(__name__)

REQUIRED_BACKEND_ATTRIBUTES = [
    "color", "size", "material", "intended_use", "age_range_description",
    "target_audience", "item_form", "special_features",
    "included_components", "country_of_origin",
]

REQUIRED_LISTING_FIELDS = ["title", "bullet_point_1", "description", "search_terms"]

# Confidence weights by data source
WEIGHT_SOURCE = 1.0      # data from client spreadsheet
WEIGHT_VISION = 0.95     # data from vision AI analysis
WEIGHT_AI_GENERATED = 0.85  # data from content generation


@dataclass
class GapReport:
    missing_fields: list[str] = field(default_factory=list)
    populated_fields: list[str] = field(default_factory=list)
    confidence_score: float = 0.0
    field_scores: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def detect_gaps_and_score(
    source_data: dict,
    generated_data: dict,
    vision_data: dict | None = None,
    has_images: bool = False,
) -> GapReport:
    """Analyze an ASIN's data completeness and compute confidence score."""
    report = GapReport()

    all_required = REQUIRED_LISTING_FIELDS + REQUIRED_BACKEND_ATTRIBUTES
    total_weight = 0.0
    weighted_sum = 0.0

    # Build case-insensitive lookup for source data (CSV headers may be Title Case)
    source_lower = {k.lower().replace(" ", "_"): v for k, v in source_data.items()}

    for field_name in all_required:
        # Check each data source in priority order
        if field_name in source_lower and _is_populated(source_lower[field_name]):
            report.populated_fields.append(field_name)
            report.field_scores[field_name] = WEIGHT_SOURCE
            weighted_sum += WEIGHT_SOURCE
        elif field_name in generated_data and _is_populated(generated_data[field_name]):
            report.populated_fields.append(field_name)
            # Vision-informed AI content gets higher weight
            if has_images and vision_data:
                report.field_scores[field_name] = WEIGHT_VISION
                weighted_sum += WEIGHT_VISION
            else:
                report.field_scores[field_name] = WEIGHT_AI_GENERATED
                weighted_sum += WEIGHT_AI_GENERATED
        else:
            report.missing_fields.append(field_name)
            report.field_scores[field_name] = 0.0

        total_weight += 1.0

    # Also check bullet points 2-5 (not required but affect score)
    for i in range(2, 6):
        bp = f"bullet_point_{i}"
        if bp in generated_data and _is_populated(generated_data[bp]):
            weighted_sum += WEIGHT_AI_GENERATED * 0.5  # half weight for optional bullets
            total_weight += 0.5

    report.confidence_score = weighted_sum / total_weight if total_weight > 0 else 0.0

    # Validate field lengths
    validation_rules = {
        "title": 200,
        "bullet_point_1": 500, "bullet_point_2": 500, "bullet_point_3": 500,
        "bullet_point_4": 500, "bullet_point_5": 500,
        "description": 2000,
        "search_terms": 250,
    }
    for field_name, max_len in validation_rules.items():
        value = generated_data.get(field_name, "")
        if value and len(str(value)) > max_len:
            report.warnings.append(
                f"{field_name} exceeds max length ({len(str(value))}/{max_len} chars)"
            )

    # Check image alt text references physical features
    alt_text = generated_data.get("image_alt_text", "")
    if has_images and not alt_text:
        report.warnings.append("Image alt text missing despite images being available")

    # Check search terms don't duplicate title
    title = generated_data.get("title", "").lower()
    search = generated_data.get("search_terms", "").lower()
    if title and search:
        title_words = set(title.split())
        search_words = set(search.split())
        overlap = title_words & search_words
        if len(overlap) > len(search_words) * 0.5:
            report.warnings.append("Search terms have >50% overlap with title — consider diversifying")

    log.info(
        "gap_report",
        populated=len(report.populated_fields),
        missing=len(report.missing_fields),
        confidence=f"{report.confidence_score:.2f}",
        warnings=len(report.warnings),
    )
    return report


def _is_populated(value) -> bool:
    """Check if a field value is meaningfully populated (not empty, not placeholder)."""
    if not value:
        return False
    s = str(value).strip().lower()
    return len(s) > 0 and s not in ("n/a", "na", "none", "unknown", "-", "tbd")

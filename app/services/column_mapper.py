"""Map source spreadsheet columns to Amazon template fields using JSON config."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_logger

log = get_logger(__name__)


@dataclass
class MappingConfig:
    template: str
    template_version: str
    source_mappings: dict[str, dict[str, str | None]]
    ai_generated_fields: list[str]
    required_backend_attributes: list[str]
    required_fields: list[str]
    validation_rules: dict[str, int]


@dataclass
class MappingResult:
    mapped_data: dict[str, str]  # amazon_field -> value
    unmapped_fields: list[str]  # required fields with no source data
    ai_fields_needed: list[str]  # fields that need AI generation
    warnings: list[str] = field(default_factory=list)


def load_mapping_config(config_path: Path) -> MappingConfig:
    """Load a column mapping configuration from JSON."""
    with open(config_path) as f:
        data = json.load(f)
    return MappingConfig(
        template=data["template"],
        template_version=data.get("template_version", "unknown"),
        source_mappings=data["source_mappings"],
        ai_generated_fields=data["ai_generated_fields"],
        required_backend_attributes=data.get("required_backend_attributes", []),
        required_fields=data["required_fields"],
        validation_rules=data.get("validation_rules", {}),
    )


def auto_detect_columns(
    headers: list[str], config: MappingConfig
) -> dict[str, str | None]:
    """Fuzzy-match spreadsheet headers to known Amazon field names.

    Returns a mapping of amazon_field -> detected_header.
    """
    # Build a lookup of all known fields from all source mappings
    all_fields = set()
    for source_fields in config.source_mappings.values():
        all_fields.update(source_fields.keys())

    # Normalize headers for matching
    normalized_headers = {h.strip().lower().replace(" ", "_"): h for h in headers}

    detected = {}
    for amazon_field in all_fields:
        normalized_field = amazon_field.strip().lower().replace(" ", "_")
        # Exact match
        if normalized_field in normalized_headers:
            detected[amazon_field] = normalized_headers[normalized_field]
            continue
        # Partial match
        for norm_h, original_h in normalized_headers.items():
            if normalized_field in norm_h or norm_h in normalized_field:
                detected[amazon_field] = original_h
                break

    return detected


def map_row(
    row: dict[str, str],
    column_mapping: dict[str, str | None],
    config: MappingConfig,
) -> MappingResult:
    """Map a single spreadsheet row to Amazon template fields.

    Args:
        row: The source spreadsheet row (header -> value)
        column_mapping: Mapping of amazon_field -> source_column_header
        config: The mapping configuration
    """
    mapped = {}
    unmapped = []

    # Map source data fields
    for amazon_field, source_header in column_mapping.items():
        if source_header and source_header in row:
            value = row[source_header].strip()
            if value:
                mapped[amazon_field] = value
            else:
                unmapped.append(amazon_field)
        else:
            unmapped.append(amazon_field)

    # Determine which fields need AI generation
    ai_needed = list(config.ai_generated_fields)  # all AI fields always need generation
    # Also add any required fields that weren't in source data
    for req_field in config.required_fields:
        if req_field not in mapped and req_field not in ai_needed:
            ai_needed.append(req_field)

    # Warnings
    warnings = []
    missing_required = [f for f in config.required_fields if f not in mapped and f not in config.ai_generated_fields]
    if missing_required:
        warnings.append(f"Required fields missing from source: {', '.join(missing_required)}")

    return MappingResult(
        mapped_data=mapped,
        unmapped_fields=unmapped,
        ai_fields_needed=ai_needed,
        warnings=warnings,
    )


def validate_template_headers(
    headers: list[str], config: MappingConfig
) -> list[str]:
    """Check if uploaded spreadsheet headers match expected template columns.

    Returns list of warnings about mismatches.
    """
    warnings = []
    expected = set()
    for source_fields in config.source_mappings.values():
        expected.update(source_fields.keys())

    header_set = {h.strip().lower().replace(" ", "_") for h in headers}
    expected_set = {f.strip().lower().replace(" ", "_") for f in expected}

    missing = expected_set - header_set
    if missing:
        warnings.append(f"Expected columns not found: {', '.join(sorted(missing))}")

    return warnings

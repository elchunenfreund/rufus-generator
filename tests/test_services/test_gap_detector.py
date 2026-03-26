"""Tests for gap_detector service."""

import pytest

from app.services.gap_detector import (
    GapReport,
    WEIGHT_AI_GENERATED,
    WEIGHT_SOURCE,
    WEIGHT_VISION,
    _is_populated,
    detect_gaps_and_score,
)


class TestIsPopulated:
    def test_normal_string(self):
        assert _is_populated("Hello") is True

    def test_empty_string(self):
        assert _is_populated("") is False

    def test_none(self):
        assert _is_populated(None) is False

    def test_na_values(self):
        assert _is_populated("n/a") is False
        assert _is_populated("NA") is False
        assert _is_populated("none") is False
        assert _is_populated("unknown") is False
        assert _is_populated("-") is False
        assert _is_populated("tbd") is False

    def test_whitespace_only(self):
        assert _is_populated("   ") is False

    def test_number_as_string(self):
        assert _is_populated("42") is True


class TestDetectGapsAndScore:
    def test_fully_populated_high_confidence(self):
        """All fields present from source + AI should yield high confidence."""
        source = {
            "title": "Widget",
            "bullet_point_1": "Durable",
            "description": "Great widget",
            "search_terms": "widget durable",
            "color": "Red",
            "size": "Medium",
            "material": "Steel",
            "intended_use": "General",
            "age_range_description": "Adult",
            "target_audience": "Everyone",
            "item_form": "Solid",
            "special_features": "Rust resistant",
            "included_components": "1x Widget",
            "country_of_origin": "USA",
        }
        generated = {
            "bullet_point_2": "Easy to use",
            "bullet_point_3": "Lightweight",
            "bullet_point_4": "Versatile",
            "bullet_point_5": "Great value",
        }
        report = detect_gaps_and_score(source, generated)

        assert report.confidence_score > 0.85
        assert len(report.missing_fields) == 0
        assert len(report.populated_fields) == 14

    def test_all_missing_zero_confidence(self):
        """Empty data should yield zero confidence with all fields missing."""
        report = detect_gaps_and_score({}, {})

        assert report.confidence_score == 0.0
        assert len(report.missing_fields) == 14  # 4 listing + 10 backend

    def test_ai_generated_lower_weight_than_source(self):
        """AI-generated fields should get WEIGHT_AI_GENERATED, not WEIGHT_SOURCE."""
        source_only = {"title": "Widget"}
        ai_only = {"title": "Widget"}

        report_source = detect_gaps_and_score(source_only, {})
        report_ai = detect_gaps_and_score({}, ai_only)

        assert report_source.field_scores["title"] == WEIGHT_SOURCE
        assert report_ai.field_scores["title"] == WEIGHT_AI_GENERATED

    def test_vision_boost(self):
        """With images + vision data, AI fields should get WEIGHT_VISION."""
        generated = {"title": "Widget", "bullet_point_1": "Feature"}
        vision_data = {"features": ["metal body"]}

        report = detect_gaps_and_score(
            {}, generated, vision_data=vision_data, has_images=True
        )
        assert report.field_scores["title"] == WEIGHT_VISION

    def test_source_takes_priority_over_ai(self):
        """If a field is in both source and generated, source weight should apply."""
        source = {"title": "Source Title"}
        generated = {"title": "AI Title"}

        report = detect_gaps_and_score(source, generated)
        assert report.field_scores["title"] == WEIGHT_SOURCE

    def test_length_validation_warnings(self):
        """Fields exceeding max length should produce warnings."""
        generated = {
            "title": "x" * 250,  # over 200
            "search_terms": "y" * 300,  # over 250
        }
        report = detect_gaps_and_score({}, generated)
        length_warnings = [w for w in report.warnings if "exceeds max length" in w]
        assert len(length_warnings) == 2

    def test_missing_alt_text_warning(self):
        """Should warn if images exist but alt text is missing."""
        generated = {"title": "Widget"}
        report = detect_gaps_and_score({}, generated, has_images=True)
        assert any("alt text" in w.lower() for w in report.warnings)

    def test_search_terms_overlap_warning(self):
        """Should warn if search terms overlap too much with title."""
        generated = {
            "title": "premium durable widget for home use",
            "search_terms": "premium durable widget for home",
        }
        report = detect_gaps_and_score({}, generated)
        assert any("overlap" in w.lower() for w in report.warnings)

    def test_optional_bullets_affect_score(self):
        """Bullet points 2-5 should affect score calculation (change total_weight).

        When all required fields come from source (weight=1.0) and optional
        AI-generated bullets are added (weight=0.7*0.5=0.35 each), the total
        weight increases by 0.5 per bullet while weighted sum increases by 0.35.
        This means score WITH bullets is slightly lower than 1.0 but the
        bullets are still factored in.
        """
        base = {
            "title": "W", "bullet_point_1": "B1", "description": "D",
            "search_terms": "S", "color": "R", "size": "M", "material": "S",
            "intended_use": "G", "age_range_description": "A",
            "target_audience": "E", "item_form": "S",
            "special_features": "F", "included_components": "C",
            "country_of_origin": "US",
        }
        report_no_bullets = detect_gaps_and_score(base, {})
        report_with_bullets = detect_gaps_and_score(base, {
            "bullet_point_2": "B2", "bullet_point_3": "B3",
            "bullet_point_4": "B4", "bullet_point_5": "B5",
        })
        # Both should have high confidence; bullets change the denominator
        assert report_no_bullets.confidence_score == 1.0
        assert report_with_bullets.confidence_score > 0.9
        assert report_with_bullets.confidence_score != report_no_bullets.confidence_score

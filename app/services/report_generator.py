"""Generate COSMO-style per-ASIN optimization report cards as HTML and PDF."""

import html
from dataclasses import dataclass
from pathlib import Path

from app.config import get_logger, settings
from app.models import ASINRecord, GenerationMode

def _esc(value: str) -> str:
    """HTML-escape a string to prevent XSS."""
    return html.escape(str(value)) if value else ""

log = get_logger(__name__)


@dataclass
class ReportData:
    """Standardized data contract consumed by the report generator.
    Both the reference analyzer and the content generator emit this format.
    """

    asin: str
    product_name: str = ""
    brand: str = ""
    image_paths: list[str] | None = None

    # Source data (from spreadsheet)
    source_fields: dict[str, str] | None = None

    # AI-generated fields
    title: str = ""
    bullet_points: list[str] | None = None
    description: str = ""
    search_terms: str = ""
    backend_attributes: dict[str, str] | None = None
    image_alt_text: str = ""

    # Metadata
    generation_mode: str = "text_only"
    confidence_score: float = 0.0
    missing_fields: list[str] | None = None
    field_sources: dict[str, str] | None = None  # field_name -> "source" | "ai" | "missing"


def report_data_from_asin_record(record: ASINRecord) -> ReportData:
    """Convert an ASINRecord ORM object to a ReportData for rendering."""
    source = record.source_data or {}
    generated = record.generated_data or {}

    # Build field source map
    field_sources = {}
    all_fields = ["title", "bullet_point_1", "bullet_point_2", "bullet_point_3",
                  "bullet_point_4", "bullet_point_5", "description", "search_terms"]
    for f in all_fields:
        if f in source and source[f]:
            field_sources[f] = "source"
        elif f in generated and generated[f]:
            field_sources[f] = "ai"
        else:
            field_sources[f] = "missing"

    bullets = [generated.get(f"bullet_point_{i}", "") for i in range(1, 6)]
    bullets = [b for b in bullets if b]

    return ReportData(
        asin=record.asin,
        product_name=source.get("product_name", source.get("title", record.asin)),
        brand=source.get("brand", ""),
        image_paths=record.image_paths or [],
        source_fields=source,
        title=generated.get("title", source.get("title", "")),
        bullet_points=bullets,
        description=generated.get("description", ""),
        search_terms=generated.get("search_terms", ""),
        backend_attributes={
            k: generated.get(k, source.get(k, ""))
            for k in [
                "color", "size", "material", "intended_use", "age_range_description",
                "target_audience", "item_form", "special_features",
                "included_components", "country_of_origin",
            ]
        },
        image_alt_text=generated.get("image_alt_text", ""),
        generation_mode=record.generation_mode.value if record.generation_mode else "text_only",
        confidence_score=record.confidence_score or 0.0,
        missing_fields=record.missing_fields or [],
        field_sources=field_sources,
    )


def render_report_card_html(data: ReportData) -> str:
    """Render a single ASIN report card as HTML (COSMO-style layout)."""

    def source_indicator(field_name: str) -> str:
        source = (data.field_sources or {}).get(field_name, "missing")
        if source == "source":
            return '<span class="indicator indicator-source">&#10003; Source</span>'
        elif source == "ai":
            return '<span class="indicator indicator-ai">&#10024; AI Generated</span>'
        else:
            return '<span class="indicator indicator-missing">&#9888; Missing</span>'

    # Images row (convert absolute paths to relative URLs)
    images_html = ""
    if data.image_paths:
        def _img_url(p: str) -> str:
            try:
                return "/uploads/" + str(Path(p).relative_to(settings.upload_dir))
            except ValueError:
                return "/uploads/" + Path(p).name

        imgs = "".join(
            f'<img src="{_esc(_img_url(p))}" style="max-height:120px;border-radius:4px;margin-right:8px;" alt="Product">'
            for p in data.image_paths[:5]
        )
        images_html = f"""
        <div class="report-section">
            <div class="report-section-header">Product Images</div>
            <div class="report-section-content" style="display:flex;gap:8px;overflow-x:auto;padding:12px;">
                {imgs}
            </div>
        </div>"""

    # Bullet points
    bullets_html = ""
    if data.bullet_points:
        items = "".join(
            f'<li contenteditable="true" style="margin-bottom:8px;">{_esc(b)}</li>'
            for b in data.bullet_points
        )
        bullets_html = f"""
        <div class="report-section">
            <div class="report-section-header">
                Bullet Points {source_indicator('bullet_point_1')}
            </div>
            <div class="report-section-content">
                <ul style="padding-left:20px;">{items}</ul>
            </div>
        </div>"""

    # Backend attributes
    attrs_html = ""
    if data.backend_attributes:
        missing_em = '<em style="color:var(--color-missing)">Missing</em>'
        rows = "".join(
            f'<tr><td style="padding:6px 12px;font-weight:500;">{k.replace("_", " ").title()}</td>'
            f'<td style="padding:6px 12px;" contenteditable="true">{_esc(v) if v else missing_em}</td></tr>'
            for k, v in data.backend_attributes.items()
        )
        attrs_html = f"""
        <div class="report-section">
            <div class="report-section-header">Backend Attributes (COSMOS)</div>
            <div class="report-section-content">
                <table style="width:100%;border-collapse:collapse;">
                    <tbody>{rows}</tbody>
                </table>
            </div>
        </div>"""

    # Confidence badge
    score = data.confidence_score
    score_color = "var(--color-source)" if score >= 0.85 else "#ff9800" if score >= 0.7 else "var(--color-missing)"
    mode_label = "Vision AI" if data.generation_mode == "vision" else "Text Only"

    return f"""
    <div class="report-card card" data-asin="{_esc(data.asin)}">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
            <div>
                <h3 style="margin-bottom:4px;">{_esc(data.asin)}</h3>
                <span style="color:var(--color-text-secondary);font-size:13px;">
                    {_esc(data.brand)} &middot; {mode_label}
                </span>
            </div>
            <div style="text-align:right;">
                <div style="font-size:24px;font-weight:700;color:{score_color};">{score:.0%}</div>
                <div style="font-size:12px;color:var(--color-text-secondary);">confidence</div>
            </div>
        </div>

        {images_html}

        <div class="report-section">
            <div class="report-section-header">
                Title {source_indicator('title')}
            </div>
            <div class="report-section-content" contenteditable="true">
                {_esc(data.title) or '<em style="color:var(--color-missing);">Not generated yet</em>'}
            </div>
        </div>

        {bullets_html}

        <div class="report-section">
            <div class="report-section-header">
                Description {source_indicator('description')}
            </div>
            <div class="report-section-content" contenteditable="true">
                {_esc(data.description) or '<em style="color:var(--color-missing);">Not generated yet</em>'}
            </div>
        </div>

        <div class="report-section">
            <div class="report-section-header">
                Search Terms {source_indicator('search_terms')}
            </div>
            <div class="report-section-content" contenteditable="true">
                {_esc(data.search_terms) or '<em style="color:var(--color-missing);">Not generated yet</em>'}
            </div>
        </div>

        {attrs_html}

        {"<div class='report-section'><div class='report-section-header'>Image Alt Text</div><div class='report-section-content' contenteditable='true'>" + _esc(data.image_alt_text) + "</div></div>" if data.image_alt_text else ""}

        {_missing_fields_html(data.missing_fields)}
    </div>
    """


def _missing_fields_html(missing: list[str] | None) -> str:
    if not missing:
        return ""
    items = "".join(f"<li>{_esc(f)}</li>" for f in missing)
    return f"""
    <div style="background:#ffebee;border-radius:var(--radius);padding:12px;margin-top:12px;">
        <strong style="color:var(--color-missing);">Missing Required Fields:</strong>
        <ul style="margin:4px 0 0 16px;font-size:13px;">{items}</ul>
    </div>"""

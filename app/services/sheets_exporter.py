"""Export ASINRecords to Google Sheets with conditional formatting."""

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.config import get_logger, settings
from app.models import ASINRecord

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Amazon flat file columns in order
EXPORT_COLUMNS = [
    "asin", "brand", "title",
    "bullet_point_1", "bullet_point_2", "bullet_point_3", "bullet_point_4", "bullet_point_5",
    "description", "search_terms", "image_alt_text",
    "color", "size", "material", "intended_use", "age_range_description",
    "target_audience", "item_form", "special_features", "included_components", "country_of_origin",
    "confidence_score", "generation_mode", "missing_fields",
]


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def _sanitize_cell(value: str) -> str:
    """Prevent CSV formula injection in Google Sheets."""
    if not isinstance(value, str) or not value:
        return value
    # Strip leading whitespace before checking for formula characters
    stripped = value.lstrip()
    if stripped and stripped[0] in FORMULA_PREFIXES:
        return "'" + value  # prepend single quote to neutralize formula
    return value


def _get_sheets_service():
    import json
    import os

    # Try JSON from env var first (for Heroku), then file path
    json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if json_str:
        info = json.loads(json_str)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    elif settings.google_service_account_path:
        creds = service_account.Credentials.from_service_account_file(
            settings.google_service_account_path, scopes=SCOPES
        )
    else:
        raise ValueError(
            "Google Sheets export is not configured. "
            "Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_PATH."
        )
    return build("sheets", "v4", credentials=creds)


def export_to_google_sheets(records: list[ASINRecord], job_id: str) -> dict:
    """Export records to a new Google Sheets spreadsheet with conditional formatting."""
    service = _get_sheets_service()

    # Create new spreadsheet
    spreadsheet = service.spreadsheets().create(
        body={
            "properties": {"title": f"Rufus Listings - {job_id[:8]}"},
            "sheets": [{"properties": {"title": "Listings"}}],
        }
    ).execute()

    sheet_id = spreadsheet["sheets"][0]["properties"]["sheetId"]
    spreadsheet_id = spreadsheet["spreadsheetId"]

    # Build rows
    header_row = [col.replace("_", " ").title() for col in EXPORT_COLUMNS]
    data_rows = []
    source_cells = []  # (row, col) pairs for source data cells
    ai_cells = []      # (row, col) pairs for AI-generated cells
    missing_cells = []  # (row, col) pairs for missing data cells

    for row_idx, record in enumerate(records):
        source = record.source_data or {}
        generated = record.generated_data or {}
        row = []

        for col_idx, col in enumerate(EXPORT_COLUMNS):
            if col == "asin":
                row.append(record.asin)
                source_cells.append((row_idx + 1, col_idx))
            elif col == "confidence_score":
                row.append(f"{record.confidence_score:.0%}" if record.confidence_score else "")
            elif col == "generation_mode":
                row.append(record.generation_mode.value if record.generation_mode else "")
            elif col == "missing_fields":
                row.append(", ".join(record.missing_fields) if record.missing_fields else "")
            elif col in source and source[col]:
                row.append(_sanitize_cell(str(source[col])))
                source_cells.append((row_idx + 1, col_idx))
            elif col in generated and generated[col]:
                row.append(_sanitize_cell(str(generated[col])))
                ai_cells.append((row_idx + 1, col_idx))
            else:
                row.append("")
                missing_cells.append((row_idx + 1, col_idx))

        data_rows.append(row)

    # Write data (batch 50 rows per request)
    all_rows = [header_row] + data_rows
    batch_size = 50
    for i in range(0, len(all_rows), batch_size):
        batch = all_rows[i : i + batch_size]
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"Listings!A{i + 1}",
            valueInputOption="RAW",
            body={"values": batch},
        ).execute()

    # Apply conditional formatting
    requests = [
        # Header row: bold + teal background
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.17, "green": 0.54, "blue": 0.54},
                        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
        # Freeze header row
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
    ]

    # Color-code: green for source, blue for AI, red for missing
    # Using conditional formatting rules based on helper columns would be complex,
    # so we use cell-level formatting for the first batch
    def _color_cells(cells, bg_color):
        for row, col in cells[:500]:  # limit to prevent API overload
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row, "endRowIndex": row + 1,
                        "startColumnIndex": col, "endColumnIndex": col + 1,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": bg_color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

    _color_cells(source_cells, {"red": 0.91, "green": 0.96, "blue": 0.91})  # light green
    _color_cells(ai_cells, {"red": 0.89, "green": 0.95, "blue": 0.99})      # light blue
    _color_cells(missing_cells, {"red": 1.0, "green": 0.92, "blue": 0.93})   # light red

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    log.info("sheets_created", spreadsheet_id=spreadsheet_id, rows=len(data_rows))

    return {"url": url, "spreadsheet_id": spreadsheet_id, "rows_exported": len(data_rows)}

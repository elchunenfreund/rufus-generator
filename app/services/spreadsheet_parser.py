"""Parse uploaded spreadsheets (.xlsx, .csv) and detect ASINs."""

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from app.config import get_logger

log = get_logger(__name__)

ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")

# Common column names that contain ASINs
ASIN_COLUMN_NAMES = {"asin", "item_sku", "sku", "product_id", "amazon_asin", "asin1"}


@dataclass
class ParsedSpreadsheet:
    filename: str
    headers: list[str]
    rows: list[dict[str, str]]
    asin_column: str | None = None
    detected_asins: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _sanitize_cell(value: str, max_length: int = 2000) -> str:
    """Sanitize cell content to prevent prompt injection and overflow."""
    if not isinstance(value, str):
        value = str(value) if value is not None else ""
    # Strip control characters (keep newlines and tabs)
    value = "".join(ch for ch in value if ch == "\n" or ch == "\t" or (ord(ch) >= 32))
    # Truncate to max length
    return value[:max_length]


def _detect_asin_column(headers: list[str]) -> str | None:
    """Find which column contains ASINs by matching header names."""
    for header in headers:
        if header.strip().lower().replace(" ", "_") in ASIN_COLUMN_NAMES:
            return header
    return None


def _is_asin(value: str) -> bool:
    """Check if a string looks like an Amazon ASIN."""
    return bool(ASIN_PATTERN.match(value.strip().upper()))


def _detect_asin_column_by_content(headers: list[str], rows: list[dict]) -> str | None:
    """Fallback: detect ASIN column by scanning cell content patterns."""
    if not rows:
        return None
    sample = rows[:20]  # check first 20 rows
    for header in headers:
        asin_count = sum(1 for row in sample if _is_asin(str(row.get(header, ""))))
        if asin_count >= len(sample) * 0.8:  # 80%+ match
            return header
    return None


def parse_xlsx(file_path: Path) -> ParsedSpreadsheet:
    """Parse an Excel .xlsx file."""
    result = ParsedSpreadsheet(filename=file_path.name, headers=[], rows=[])

    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active
        if ws is None:
            result.errors.append("No active worksheet found")
            return result

        rows_iter = ws.iter_rows(values_only=True)

        # First row is headers
        header_row = next(rows_iter, None)
        if header_row is None:
            result.errors.append("Empty spreadsheet")
            return result

        result.headers = [_sanitize_cell(str(h)) if h else f"col_{i}" for i, h in enumerate(header_row)]

        # Parse data rows
        for row in rows_iter:
            row_dict = {}
            for i, cell in enumerate(row):
                if i < len(result.headers):
                    row_dict[result.headers[i]] = _sanitize_cell(cell)
            if any(v.strip() for v in row_dict.values()):  # skip empty rows
                result.rows.append(row_dict)

        wb.close()
    except Exception as e:
        result.errors.append(f"Failed to parse xlsx: {e}")
        return result

    # Detect ASIN column
    result.asin_column = _detect_asin_column(result.headers)
    if not result.asin_column:
        result.asin_column = _detect_asin_column_by_content(result.headers, result.rows)

    if result.asin_column:
        result.detected_asins = list(
            {
                row[result.asin_column].strip().upper()
                for row in result.rows
                if _is_asin(str(row.get(result.asin_column, "")))
            }
        )
    else:
        result.errors.append("No ASIN column detected")

    # Check for duplicate ASINs
    all_asins = [
        row[result.asin_column].strip().upper()
        for row in result.rows
        if result.asin_column and _is_asin(str(row.get(result.asin_column, "")))
    ]
    duplicates = [a for a in set(all_asins) if all_asins.count(a) > 1]
    if duplicates:
        result.errors.append(
            f"Duplicate ASINs found (will be rejected): {', '.join(sorted(duplicates)[:10])}"
        )

    log.info(
        "spreadsheet_parsed",
        filename=file_path.name,
        headers=len(result.headers),
        rows=len(result.rows),
        asins=len(result.detected_asins),
        asin_column=result.asin_column,
    )
    return result


def parse_csv(file_path: Path) -> ParsedSpreadsheet:
    """Parse a CSV file."""
    result = ParsedSpreadsheet(filename=file_path.name, headers=[], rows=[])

    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            # Sniff delimiter
            sample = f.read(8192)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel

            reader = csv.DictReader(f, dialect=dialect)
            if reader.fieldnames:
                result.headers = [_sanitize_cell(h) for h in reader.fieldnames]

            for row in reader:
                sanitized = {k: _sanitize_cell(v) for k, v in row.items() if k}
                if any(v.strip() for v in sanitized.values()):
                    result.rows.append(sanitized)

    except Exception as e:
        result.errors.append(f"Failed to parse csv: {e}")
        return result

    # Same ASIN detection as xlsx
    result.asin_column = _detect_asin_column(result.headers)
    if not result.asin_column:
        result.asin_column = _detect_asin_column_by_content(result.headers, result.rows)

    if result.asin_column:
        result.detected_asins = list(
            {
                row[result.asin_column].strip().upper()
                for row in result.rows
                if _is_asin(str(row.get(result.asin_column, "")))
            }
        )
    else:
        result.errors.append("No ASIN column detected")

    # Duplicate check
    all_asins = [
        row[result.asin_column].strip().upper()
        for row in result.rows
        if result.asin_column and _is_asin(str(row.get(result.asin_column, "")))
    ]
    duplicates = [a for a in set(all_asins) if all_asins.count(a) > 1]
    if duplicates:
        result.errors.append(
            f"Duplicate ASINs found (will be rejected): {', '.join(sorted(duplicates)[:10])}"
        )

    log.info(
        "csv_parsed",
        filename=file_path.name,
        headers=len(result.headers),
        rows=len(result.rows),
        asins=len(result.detected_asins),
        asin_column=result.asin_column,
    )
    return result


def parse_file(file_path: Path) -> ParsedSpreadsheet:
    """Parse a spreadsheet file based on extension."""
    suffix = file_path.suffix.lower()
    if suffix == ".xlsx":
        return parse_xlsx(file_path)
    elif suffix == ".csv":
        return parse_csv(file_path)
    else:
        return ParsedSpreadsheet(
            filename=file_path.name,
            headers=[],
            rows=[],
            errors=[f"Unsupported file type: {suffix}. Accepted: .xlsx, .csv"],
        )

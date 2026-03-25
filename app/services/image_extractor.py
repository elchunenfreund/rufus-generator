"""Extract product images from ZIP archives and match to ASINs."""

import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_logger, settings

log = get_logger(__name__)

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ASIN_PATTERN = re.compile(r"[A-Z0-9]{10}")

# Max decompressed size to prevent zip bombs (10GB)
MAX_DECOMPRESSED_SIZE = 10 * 1024 * 1024 * 1024
MAX_FILE_COUNT = 50_000


@dataclass
class ImageExtractionResult:
    total_images: int = 0
    matched_asins: dict[str, list[str]] = field(default_factory=dict)  # asin -> [image_paths]
    orphaned_images: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _is_safe_path(entry_name: str) -> bool:
    """Reject path traversal attempts."""
    normalized = os.path.normpath(entry_name)
    return not (
        normalized.startswith("/")
        or normalized.startswith("\\")
        or ".." in normalized.split(os.sep)
    )


def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_IMAGE_EXTENSIONS


def _extract_asin_from_path(path: str, known_asins: set[str]) -> str | None:
    """Try to match a file path to a known ASIN.

    Strategy 1: folder name IS an ASIN (folder-per-ASIN structure)
    Strategy 2: filename contains an ASIN
    Strategy 3: manifest CSV (handled separately)
    """
    parts = Path(path).parts

    # Strategy 1: parent folder is an ASIN
    if len(parts) >= 2:
        folder = parts[-2].strip().upper()
        if folder in known_asins:
            return folder

    # Strategy 2: filename contains ASIN
    filename = parts[-1].upper()
    matches = ASIN_PATTERN.findall(filename)
    for match in matches:
        if match in known_asins:
            return match

    # Strategy 2b: check all path components
    full_path_upper = path.upper()
    for asin in known_asins:
        if asin in full_path_upper:
            return asin

    return None


def _parse_manifest(zip_ref: zipfile.ZipFile, known_asins: set[str]) -> dict[str, str]:
    """Look for a manifest CSV inside the ZIP mapping filenames to ASINs."""
    manifest_map = {}
    for name in zip_ref.namelist():
        basename = Path(name).name.lower()
        if basename in ("manifest.csv", "image_manifest.csv", "mapping.csv"):
            try:
                import csv
                import io

                with zip_ref.open(name) as f:
                    content = f.read().decode("utf-8-sig")
                    reader = csv.DictReader(io.StringIO(content))
                    for row in reader:
                        filename = row.get("filename", row.get("image", "")).strip()
                        asin = row.get("asin", row.get("ASIN", "")).strip().upper()
                        if filename and asin in known_asins:
                            manifest_map[filename] = asin
                log.info("manifest_found", manifest=name, mappings=len(manifest_map))
            except Exception as e:
                log.warning("manifest_parse_error", manifest=name, error=str(e))
    return manifest_map


def extract_images(
    zip_path: Path, output_dir: Path, known_asins: set[str]
) -> ImageExtractionResult:
    """Extract images from a ZIP archive and match to known ASINs."""
    result = ImageExtractionResult()

    if not zipfile.is_zipfile(zip_path):
        result.errors.append("File is not a valid ZIP archive")
        return result

    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            # Safety checks
            total_size = sum(info.file_size for info in zip_ref.infolist())
            if total_size > MAX_DECOMPRESSED_SIZE:
                result.errors.append(
                    f"ZIP decompressed size ({total_size / 1e9:.1f}GB) exceeds limit "
                    f"({MAX_DECOMPRESSED_SIZE / 1e9:.0f}GB)"
                )
                return result

            entries = zip_ref.infolist()
            if len(entries) > MAX_FILE_COUNT:
                result.errors.append(
                    f"ZIP contains {len(entries)} files (max {MAX_FILE_COUNT})"
                )
                return result

            # Check for manifest
            manifest_map = _parse_manifest(zip_ref, known_asins)

            # Extract images
            for info in entries:
                if info.is_dir():
                    continue

                # Path traversal check
                if not _is_safe_path(info.filename):
                    log.warning("path_traversal_blocked", path=info.filename)
                    result.errors.append(f"Blocked unsafe path: {info.filename}")
                    continue

                if not _is_image(info.filename):
                    continue

                # Size check per image
                if info.file_size > settings.max_image_size_bytes:
                    result.errors.append(
                        f"Image too large: {info.filename} "
                        f"({info.file_size / 1e6:.1f}MB > {settings.max_image_size_bytes / 1e6:.0f}MB)"
                    )
                    continue

                result.total_images += 1

                # Match to ASIN
                basename = Path(info.filename).name
                asin = manifest_map.get(basename)
                if not asin:
                    asin = _extract_asin_from_path(info.filename, known_asins)

                if asin:
                    # Extract to ASIN-specific directory
                    asin_dir = output_dir / asin
                    asin_dir.mkdir(parents=True, exist_ok=True)
                    target = asin_dir / basename

                    with zip_ref.open(info) as src, open(target, "wb") as dst:
                        dst.write(src.read())

                    if asin not in result.matched_asins:
                        result.matched_asins[asin] = []
                    result.matched_asins[asin].append(str(target))

                    # Enforce max images per ASIN
                    if len(result.matched_asins[asin]) > settings.max_images_per_asin:
                        result.matched_asins[asin] = result.matched_asins[asin][
                            : settings.max_images_per_asin
                        ]
                else:
                    result.orphaned_images.append(info.filename)

    except zipfile.BadZipFile:
        result.errors.append("Corrupt ZIP file")
    except Exception as e:
        result.errors.append(f"ZIP extraction failed: {e}")

    log.info(
        "images_extracted",
        total=result.total_images,
        matched_asins=len(result.matched_asins),
        orphaned=len(result.orphaned_images),
        errors=len(result.errors),
    )
    return result

"""Tests for image_extractor service."""

import io
import zipfile
from pathlib import Path

import pytest

from app.services.image_extractor import (
    ImageExtractionResult,
    _extract_asin_from_path,
    _is_image,
    _is_safe_path,
    extract_images,
)


class TestIsSafePath:
    def test_normal_path(self):
        assert _is_safe_path("images/B08N5WRWNW/photo.jpg") is True

    def test_path_traversal_dotdot(self):
        assert _is_safe_path("../etc/passwd") is False

    def test_absolute_path(self):
        assert _is_safe_path("/etc/passwd") is False

    def test_simple_filename(self):
        assert _is_safe_path("photo.jpg") is True


class TestIsImage:
    def test_jpg(self):
        assert _is_image("photo.jpg") is True

    def test_jpeg(self):
        assert _is_image("photo.jpeg") is True

    def test_png(self):
        assert _is_image("photo.png") is True

    def test_webp(self):
        assert _is_image("photo.webp") is True

    def test_txt_not_image(self):
        assert _is_image("readme.txt") is False

    def test_case_insensitive(self):
        assert _is_image("PHOTO.JPG") is True


class TestExtractASINFromPath:
    def test_folder_name_match(self):
        known = {"B08N5WRWNW", "B09XYZ1234"}
        result = _extract_asin_from_path("B08N5WRWNW/photo.jpg", known)
        assert result == "B08N5WRWNW"

    def test_filename_contains_asin(self):
        known = {"B08N5WRWNW"}
        result = _extract_asin_from_path("images/B08N5WRWNW_main.jpg", known)
        assert result == "B08N5WRWNW"

    def test_no_match(self):
        known = {"B08N5WRWNW"}
        result = _extract_asin_from_path("images/random_photo.jpg", known)
        assert result is None

    def test_case_insensitive_match(self):
        known = {"B08N5WRWNW"}
        result = _extract_asin_from_path("b08n5wrwnw/photo.jpg", known)
        assert result == "B08N5WRWNW"


class TestExtractImages:
    def _make_zip(self, tmp_path, files: dict[str, bytes]) -> Path:
        """Helper: create a zip file with given name->content mapping."""
        zip_path = tmp_path / "images.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        return zip_path

    def test_extract_matched_images(self, tmp_path):
        """Images in ASIN folders should be matched correctly."""
        files = {
            "B08N5WRWNW/photo1.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 100,
            "B08N5WRWNW/photo2.png": b"\x89PNG" + b"\x00" * 100,
            "B09XYZ1234/main.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 100,
        }
        zip_path = self._make_zip(tmp_path, files)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        known_asins = {"B08N5WRWNW", "B09XYZ1234"}
        result = extract_images(zip_path, output_dir, known_asins)

        assert result.total_images == 3
        assert "B08N5WRWNW" in result.matched_asins
        assert len(result.matched_asins["B08N5WRWNW"]) == 2
        assert "B09XYZ1234" in result.matched_asins
        assert len(result.orphaned_images) == 0
        assert len(result.errors) == 0

    def test_orphaned_images(self, tmp_path):
        """Images not matching any ASIN should be orphaned."""
        files = {"unknown/photo.jpg": b"\xff\xd8" + b"\x00" * 100}
        zip_path = self._make_zip(tmp_path, files)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = extract_images(zip_path, output_dir, {"B08N5WRWNW"})

        assert result.total_images == 1
        assert len(result.orphaned_images) == 1

    def test_non_image_files_ignored(self, tmp_path):
        """Non-image files in ZIP should be skipped."""
        files = {
            "B08N5WRWNW/readme.txt": b"hello",
            "B08N5WRWNW/photo.jpg": b"\xff\xd8" + b"\x00" * 100,
        }
        zip_path = self._make_zip(tmp_path, files)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = extract_images(zip_path, output_dir, {"B08N5WRWNW"})
        assert result.total_images == 1

    def test_invalid_zip(self, tmp_path):
        """Non-ZIP file should produce an error."""
        bad_file = tmp_path / "not_a_zip.zip"
        bad_file.write_bytes(b"this is not a zip")
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = extract_images(bad_file, output_dir, {"B08N5WRWNW"})
        assert any("not a valid ZIP" in e for e in result.errors)

    def test_path_traversal_blocked(self, tmp_path):
        """Entries with path traversal should be blocked."""
        zip_path = tmp_path / "traversal.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../../etc/photo.jpg", b"\xff\xd8" + b"\x00" * 100)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = extract_images(zip_path, output_dir, {"B08N5WRWNW"})
        assert any("unsafe path" in e.lower() or "Blocked" in e for e in result.errors)

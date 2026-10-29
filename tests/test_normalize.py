"""Tests for extension normalisation (content vs filename)."""

from __future__ import annotations

from eqsl_core import normalize_extensions

BASE = "LU1-20240315_1423-W1AW-20m-FT8"


def test_renames_png_content_saved_as_jpg(tmp_path, png_bytes):
    original = tmp_path / (BASE + ".jpg")
    original.write_bytes(png_bytes)

    stats = normalize_extensions(tmp_path)

    assert stats["renamed"] == 1
    assert not original.exists()
    assert (tmp_path / (BASE + ".png")).exists()


def test_dry_run_does_not_rename(tmp_path, png_bytes):
    original = tmp_path / (BASE + ".jpg")
    original.write_bytes(png_bytes)

    stats = normalize_extensions(tmp_path, dry_run=True)

    assert stats["renamed"] == 0
    assert original.exists()
    assert not (tmp_path / (BASE + ".png")).exists()


def test_matching_extension_is_ok(tmp_path, jpeg_bytes):
    (tmp_path / (BASE + ".jpg")).write_bytes(jpeg_bytes)

    stats = normalize_extensions(tmp_path)

    assert stats["ok"] == 1
    assert stats["renamed"] == 0


def test_conflict_does_not_overwrite(tmp_path, png_bytes):
    jpg = tmp_path / (BASE + ".jpg")
    png = tmp_path / (BASE + ".png")
    jpg.write_bytes(png_bytes)
    png.write_bytes(png_bytes)

    stats = normalize_extensions(tmp_path)

    assert stats["conflicts"] == 1
    assert jpg.exists()
    assert png.exists()


def test_ignores_files_that_do_not_match_the_pattern(tmp_path, png_bytes):
    (tmp_path / "random.jpg").write_bytes(png_bytes)

    stats = normalize_extensions(tmp_path)

    assert stats["checked"] == 0


def test_jpeg_extension_is_accepted(tmp_path, jpeg_bytes):
    (tmp_path / (BASE + ".jpeg")).write_bytes(jpeg_bytes)
    stats = normalize_extensions(tmp_path)
    assert stats["ok"] == 1
    assert stats["renamed"] == 0
    assert (tmp_path / (BASE + ".jpeg")).exists()


def test_png_with_jpeg_content_is_renamed_to_jpg(tmp_path, jpeg_bytes):
    (tmp_path / (BASE + ".png")).write_bytes(jpeg_bytes)
    stats = normalize_extensions(tmp_path)
    assert stats["renamed"] == 1
    assert (tmp_path / (BASE + ".jpg")).exists()

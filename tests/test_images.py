"""Structural image validation tests (JPEG/PNG)."""

from __future__ import annotations

from eqsl_core import (
    _is_valid_image_bytes,
    _is_valid_jpeg_bytes,
    _is_valid_png_bytes,
    detect_image_kind,
    is_valid_jpeg,
)


def test_detects_formats(jpeg_bytes, png_bytes):
    assert detect_image_kind(jpeg_bytes) == "jpeg"
    assert detect_image_kind(png_bytes) == "png"
    assert detect_image_kind(b"GIF89a....") == "gif"
    assert detect_image_kind(b"") == "empty"
    assert detect_image_kind(b"\x00\x01\x02\x03") == "unknown"
    assert detect_image_kind(b"<!DOCTYPE html><html>") == "html"


def test_valid_jpeg_and_png(jpeg_bytes, png_bytes):
    assert _is_valid_jpeg_bytes(jpeg_bytes) is True
    assert _is_valid_png_bytes(png_bytes) is True
    assert _is_valid_image_bytes(jpeg_bytes) is True
    assert _is_valid_image_bytes(png_bytes) is True


def test_jpeg_with_trailing_padding_is_accepted(jpeg_bytes):
    assert _is_valid_jpeg_bytes(jpeg_bytes + b"\x00\x00\x00") is True


def test_truncated_and_garbage_are_invalid(jpeg_bytes):
    assert _is_valid_jpeg_bytes(jpeg_bytes[: len(jpeg_bytes) // 2]) is False
    assert _is_valid_jpeg_bytes(b"not an image") is False
    assert _is_valid_jpeg_bytes(b"") is False
    assert _is_valid_png_bytes(b"not a png") is False
    assert _is_valid_image_bytes(b"garbage") is False


def test_html_payload_is_not_a_valid_image():
    assert _is_valid_image_bytes(b"<html>Error</html>") is False


def test_is_valid_jpeg_reads_path(write_file, jpeg_bytes):
    good = write_file("good.jpg", jpeg_bytes)
    bad = write_file("bad.jpg", b"broken")
    assert is_valid_jpeg(good) is True
    assert is_valid_jpeg(bad) is False


def test_is_valid_jpeg_missing_file(tmp_path):
    assert is_valid_jpeg(tmp_path / "does-not-exist.jpg") is False

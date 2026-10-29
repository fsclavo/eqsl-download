"""Structural image validation tests (JPEG/PNG)."""

from __future__ import annotations

from eqsl_core import (
    _is_valid_image_bytes,
    _is_valid_jpeg_bytes,
    _is_valid_png_bytes,
    detect_image_kind,
    image_extension,
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


SOI = b"\xff\xd8"


def test_detects_webp():
    data = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
    assert detect_image_kind(data) == "webp"


def test_png_truncated_and_missing_iend():
    assert _is_valid_png_bytes(b"\x89PNG\r\n\x1a\n") is False
    no_iend = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    assert _is_valid_png_bytes(no_iend) is False


def test_jpeg_rejects_unexpected_soi():
    assert _is_valid_jpeg_bytes(SOI + b"\xff\xd8") is False


def test_jpeg_rejects_non_marker_where_expected():
    assert _is_valid_jpeg_bytes(SOI + b"\x00") is False


def test_jpeg_rejects_fill_bytes_at_end():
    assert _is_valid_jpeg_bytes(SOI + b"\xff") is False


def test_jpeg_rejects_truncated_length():
    assert _is_valid_jpeg_bytes(SOI + b"\xff\xe0\x00") is False


def test_jpeg_rejects_too_small_segment_length():
    assert _is_valid_jpeg_bytes(SOI + b"\xff\xe0\x00\x01\x00") is False


def test_jpeg_rejects_segment_overrun():
    assert _is_valid_jpeg_bytes(SOI + b"\xff\xe0\x00\x10") is False


def test_jpeg_standalone_marker_then_eoi_is_not_enough():
    # Standalone (RST) marker, EOI without SOS -> no image data.
    assert _is_valid_jpeg_bytes(SOI + b"\xff\xd0\xff\xd9") is False


def test_jpeg_skips_fill_bytes_before_eoi():
    assert _is_valid_jpeg_bytes(SOI + b"\xff\xff\xff\xd9") is False


def test_jpeg_valid_sos_with_stuffed_ff_and_eoi():
    data = SOI + b"\xff\xda\x00\x02" + b"\x00\x11\xff\x00\x22\xff\xd9"
    assert _is_valid_jpeg_bytes(data) is True


def test_jpeg_sos_with_ff_at_end_is_invalid():
    data = SOI + b"\xff\xda\x00\x02" + b"\x00\x11\xff"
    assert _is_valid_jpeg_bytes(data) is False


def test_jpeg_sos_without_marker_is_invalid():
    data = SOI + b"\xff\xda\x00\x02" + b"\x00\x11"
    assert _is_valid_jpeg_bytes(data) is False


def test_jpeg_non_sos_segment_is_skipped():
    data = SOI + b"\xff\xe0\x00\x02" + b"\xff\xd9"
    assert _is_valid_jpeg_bytes(data) is False



def test_image_extension_mapping():
    assert image_extension("jpeg") == ".jpg"
    assert image_extension("png") == ".png"
    assert image_extension("gif") is None
    assert image_extension("unknown") is None

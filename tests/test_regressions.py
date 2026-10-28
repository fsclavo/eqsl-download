"""Regression tests for edge cases and previously latent bugs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from eqsl_core import (
    QSO,
    classify_local_file,
    is_valid_jpeg,
    list_local_jpgs,
    scan_local_qsos,
    validate_local_paths,
)


def _qso() -> QSO:
    return QSO("C1", "20240101", "1200", "20m", "FT8")


def test_list_local_jpgs_skips_invalid_calendar_date(tmp_path):
    (tmp_path / "x-99999999_0000-C-20m-FT8.jpg").write_bytes(b"x")
    (tmp_path / "y-20240101_0000-C-20m-FT8.jpg").write_bytes(b"x")
    files = list_local_jpgs(tmp_path, date(2024, 1, 1), date(2024, 12, 31))
    assert [p.name for p in files] == ["y-20240101_0000-C-20m-FT8.jpg"]


def test_is_valid_jpeg_on_directory_returns_false(tmp_path):
    # read_bytes() raises IsADirectoryError -> must be handled as invalid.
    assert is_valid_jpeg(tmp_path) is False


def test_classify_zero_byte_file_is_missing(tmp_path):
    (tmp_path / "zero.jpg").write_bytes(b"")
    status, _, _ = classify_local_file("zero.jpg", _qso(), tmp_path)
    assert status == "missing"


def test_classify_handles_oserror(monkeypatch, tmp_path):
    def boom(self):
        raise OSError("nope")

    monkeypatch.setattr(Path, "is_file", boom)
    status, _, _ = classify_local_file("x.jpg", _qso(), tmp_path)
    assert status == "missing"


def test_validate_local_paths_invokes_progress(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    calls = []
    validate_local_paths([path], 1, progress=lambda d, t, s: calls.append((d, t, s)))
    assert calls
    assert calls[-1][0] == calls[-1][1] == 1


def test_scan_local_qsos_invokes_progress(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    calls = []
    scan_local_qsos(
        {"a.jpg": _qso()}, tmp_path, 1,
        progress=lambda d, t, s: calls.append((d, t, s)),
    )
    assert calls
    assert calls[-1][0] == 1

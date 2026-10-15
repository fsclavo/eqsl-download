"""Command-line interface tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import eqsl_download

ROOT = Path(__file__).resolve().parents[1]


def _parse(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["eqsl_download.py", *argv])
    return eqsl_download.parse_args()


def test_year_shortcut(monkeypatch):
    args = _parse(monkeypatch, "--year", "2024")
    assert args.from_date.isoformat() == "2024-01-01"
    assert args.to_date.isoformat() == "2024-12-31"


def test_explicit_range(monkeypatch):
    args = _parse(monkeypatch, "--from", "2024-01-01", "--to", "2024-06-30")
    assert args.from_date.isoformat() == "2024-01-01"
    assert args.to_date.isoformat() == "2024-06-30"


def test_custom_dest(monkeypatch):
    args = _parse(monkeypatch, "--year", "2024", "--dest", "/tmp/cards")
    assert args.dest == Path("/tmp/cards")


@pytest.mark.parametrize(
    "argv",
    [
        (),  # no dates, not validate-only
        ("--from", "2024-01-01"),  # from without to
        ("--to", "2024-01-01"),  # to without from
        ("--year", "2024", "--from", "2024-01-01"),  # both shortcuts
        ("--year", "2024", "--workers", "0"),  # bad worker count
        ("--from", "2024-13-01", "--to", "2024-12-31"),  # invalid date
        ("--from", "2024-06-30", "--to", "2024-01-01"),  # reversed range
    ],
)
def test_invalid_arguments_exit(monkeypatch, argv):
    with pytest.raises(SystemExit):
        _parse(monkeypatch, *argv)


def test_validate_only_allows_missing_dates(monkeypatch):
    args = _parse(monkeypatch, "--validate-only")
    assert args.validate_only is True
    assert args.from_date is None


def _run_cli(*argv):
    return subprocess.run(
        [sys.executable, "eqsl_download.py", *argv],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_validate_only_subprocess_reports_corrupt(tmp_path, jpeg_bytes):
    (tmp_path / "good-20240101_0000-X-20m-FT8.jpg").write_bytes(jpeg_bytes)
    (tmp_path / "bad-20240102_0000-Y-20m-FT8.jpg").write_bytes(b"broken")

    proc = _run_cli("--validate-only", "--dest", str(tmp_path))

    assert proc.returncode == 1
    assert "corrupt" in proc.stdout.lower()
    assert "bad-20240102_0000-Y-20m-FT8.jpg" in proc.stdout


def test_validate_only_subprocess_clean_exit_zero(tmp_path):
    proc = _run_cli("--validate-only", "--dest", str(tmp_path))
    assert proc.returncode == 0
    assert "total=0" in proc.stdout

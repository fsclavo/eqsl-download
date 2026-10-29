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


def test_version_subprocess():
    proc = _run_cli("--version")
    assert proc.returncode == 0
    assert "eQSL Card Downloader" in proc.stdout
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() in proc.stdout


def test_validate_only_writes_log_file(tmp_path):
    logfile = tmp_path / "validate.log"
    proc = _run_cli(
        "--validate-only", "--dest", str(tmp_path), "--log-file", str(logfile)
    )
    assert proc.returncode == 0
    assert logfile.exists()
    assert "validate-only" in logfile.read_text(encoding="utf-8")
    assert "validate-only" in proc.stdout


def _patch_download_run(monkeypatch, tmp_path, adif_text, *extra_argv):
    monkeypatch.setattr(
        sys, "argv",
        [
            "eqsl_download.py", "--year", "2024", "--dest", str(tmp_path),
            "--delay", "0", *extra_argv,
        ],
    )
    monkeypatch.setattr(
        eqsl_download, "resolve_credentials", lambda args: ("LU1ABC", "pw", "")
    )
    monkeypatch.setattr(
        eqsl_download, "fetch_inbox_adif", lambda *a, **k: adif_text
    )


def test_dry_run_does_not_download(monkeypatch, tmp_path, sample_adif):
    _patch_download_run(monkeypatch, tmp_path, sample_adif, "--dry-run")
    calls = {"n": 0}

    def fake_download(*a, **k):
        calls["n"] += 1
        return True, "ok"

    monkeypatch.setattr(eqsl_download, "download_card", fake_download)
    assert eqsl_download.main() == 0
    assert calls["n"] == 0


def test_failed_downloads_are_exported(monkeypatch, tmp_path, sample_adif):
    _patch_download_run(monkeypatch, tmp_path, sample_adif)
    monkeypatch.setattr(eqsl_download, "download_card", lambda *a, **k: (False, "boom"))

    assert eqsl_download.main() == 1
    failures = list(tmp_path.glob("eqsl_failed_*.txt"))
    assert failures
    text = failures[0].read_text(encoding="utf-8")
    assert "LU1ABC-20240315_1423-W1AW-20m-FT8" in text



def test_new_cli_flags(monkeypatch):
    args = _parse(
        monkeypatch, "--year", "2024", "--callsign", "LU1ABC", "--qth", "Home",
        "--password-stdin", "--retries", "3", "--delay", "0",
    )
    assert args.callsign == "LU1ABC"
    assert args.qth == "Home"
    assert args.password_stdin is True
    assert args.retries == 3
    assert args.delay == 0.0


@pytest.mark.parametrize(
    "argv",
    [
        ("--year", "2024", "--retries", "0"),
        ("--year", "2024", "--delay", "-1"),
    ],
)
def test_bad_retries_or_delay(monkeypatch, argv):
    with pytest.raises(SystemExit):
        _parse(monkeypatch, *argv)


def test_resolve_credentials_stdin(monkeypatch):
    import io
    from types import SimpleNamespace

    monkeypatch.setattr(sys, "stdin", io.StringIO("secret\n"))
    args = SimpleNamespace(password_stdin=True, callsign="LU1ABC", qth="Home")
    assert eqsl_download.resolve_credentials(args) == ("LU1ABC", "secret", "Home")


def test_resolve_credentials_stdin_requires_callsign(monkeypatch):
    import io
    from types import SimpleNamespace

    monkeypatch.setattr(sys, "stdin", io.StringIO("secret\n"))
    args = SimpleNamespace(password_stdin=True, callsign=None, qth=None)
    with pytest.raises(SystemExit):
        eqsl_download.resolve_credentials(args)


def test_resolve_credentials_interactive(monkeypatch):
    from types import SimpleNamespace

    answers = iter(["LU1ABC", "Home"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(eqsl_download.getpass, "getpass", lambda *a: "pw")
    args = SimpleNamespace(password_stdin=False, callsign=None, qth=None)
    assert eqsl_download.resolve_credentials(args) == ("LU1ABC", "pw", "Home")


def test_resolve_credentials_interactive_uses_flags(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(eqsl_download.getpass, "getpass", lambda *a: "pw")

    def no_input(*a):
        raise AssertionError("should not prompt")

    monkeypatch.setattr("builtins.input", no_input)
    args = SimpleNamespace(password_stdin=False, callsign="LU1ABC", qth="Home")
    assert eqsl_download.resolve_credentials(args) == ("LU1ABC", "pw", "Home")


def test_force_and_only_missing_are_exclusive(monkeypatch):
    with pytest.raises(SystemExit):
        _parse(monkeypatch, "--year", "2024", "--force", "--only-missing")


_NAME_A = "LU1ABC-20240315_1423-W1AW-20m-FT8.jpg"
_NAME_B = "LU1ABC-20240316_1423-LU1ABC-40m-JT65.jpg"
_BASE_A = _NAME_A[:-4]
_BASE_B = _NAME_B[:-4]


def test_force_redownloads_even_when_valid(monkeypatch, tmp_path, sample_adif, jpeg_bytes):
    (tmp_path / _NAME_A).write_bytes(jpeg_bytes)
    _patch_download_run(monkeypatch, tmp_path, sample_adif, "--force")
    calls = []

    def fake_download(session, u, p, qso, dest_dir, base, log=None, **kwargs):
        calls.append(base)
        (dest_dir / (base + ".jpg")).write_bytes(jpeg_bytes)
        return True, "ok"

    monkeypatch.setattr(eqsl_download, "download_card", fake_download)
    assert eqsl_download.main() == 0
    assert set(calls) == {_BASE_A, _BASE_B}


def test_only_missing_skips_valid(monkeypatch, tmp_path, sample_adif, jpeg_bytes):
    (tmp_path / _NAME_A).write_bytes(jpeg_bytes)  # valid, must be skipped
    _patch_download_run(monkeypatch, tmp_path, sample_adif, "--only-missing")
    calls = []

    def fake_download(session, u, p, qso, dest_dir, base, log=None, **kwargs):
        calls.append(base)
        (dest_dir / (base + ".jpg")).write_bytes(jpeg_bytes)
        return True, "ok"

    monkeypatch.setattr(eqsl_download, "download_card", fake_download)
    assert eqsl_download.main() == 0
    assert calls == [_BASE_B]


def test_only_missing_does_not_redownload_corrupt(monkeypatch, tmp_path, sample_adif, jpeg_bytes):
    (tmp_path / _NAME_A).write_bytes(jpeg_bytes)  # valid
    (tmp_path / _NAME_B).write_bytes(b"broken")   # corrupt -> left untouched
    _patch_download_run(monkeypatch, tmp_path, sample_adif, "--only-missing")
    calls = []

    def fake_download(session, u, p, qso, dest_dir, base, log=None, **kwargs):
        calls.append(base)
        (dest_dir / (base + ".jpg")).write_bytes(jpeg_bytes)
        return True, "ok"

    monkeypatch.setattr(eqsl_download, "download_card", fake_download)
    assert eqsl_download.main() == 0
    assert calls == []
    assert (tmp_path / _NAME_B).read_bytes() == b"broken"


def test_quiet_flag_parse(monkeypatch):
    args = _parse(monkeypatch, "--year", "2024", "--quiet")
    assert args.quiet is True


def test_quiet_validate_only_prints_summary_only(tmp_path):
    proc = _run_cli("--validate-only", "--dest", str(tmp_path), "--quiet")
    assert proc.returncode == 0
    assert "Done. validate_only" in proc.stdout
    assert "files to check" not in proc.stdout


def test_normalize_extensions_cli(tmp_path, png_bytes):
    (tmp_path / "LU1-20240101_0000-X-20m-FT8.jpg").write_bytes(png_bytes)
    proc = _run_cli("--normalize-extensions", "--dest", str(tmp_path))
    assert proc.returncode == 0
    assert "renamed=1" in proc.stdout
    assert (tmp_path / "LU1-20240101_0000-X-20m-FT8.png").exists()


def test_validate_only_reports_extension_mismatch(tmp_path, jpeg_bytes):
    (tmp_path / "LU1-20240101_0000-X-20m-FT8.png").write_bytes(jpeg_bytes)
    proc = _run_cli("--validate-only", "--dest", str(tmp_path))
    assert proc.returncode == 0
    assert "extension mismatches" in proc.stdout
    assert "-> .jpg" in proc.stdout

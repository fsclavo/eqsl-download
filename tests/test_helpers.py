"""Tests for small helpers: dates, ETA, progress bar, logger, CLI helpers."""

from __future__ import annotations

import os
from datetime import date

import pytest
import requests

import eqsl_download
from eqsl_core import QSO, eqsl_date, format_eta


def test_eqsl_date():
    assert eqsl_date(date(2024, 3, 5)) == "03/05/2024"
    assert eqsl_date(date(2024, 12, 31)) == "12/31/2024"


def test_default_scan_workers_in_range():
    import eqsl_core

    assert 1 <= eqsl_core.DEFAULT_SCAN_WORKERS <= 8


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (0, "0s"),
        (-3, "0s"),
        (1, "11s"),
        (10, "1m 50s"),
        (100, "18m 20s"),
        (1000, "3h 03m"),
    ],
)
def test_format_eta(remaining, expected):
    assert format_eta(remaining) == expected


def test_progress_bar_render_update_note_finish(monkeypatch, capsys):
    monkeypatch.setattr(
        eqsl_download.shutil,
        "get_terminal_size",
        lambda *a, **k: os.terminal_size((30, 20)),
    )
    bar = eqsl_download.ProgressBar(10)
    bar.update(5, "half way through a long suffix that will be truncated")
    bar.note("a note")
    bar.finish("done")
    out = capsys.readouterr().out
    assert "a note" in out


def test_progress_bar_unknown_total(capsys):
    bar = eqsl_download.ProgressBar(0)
    bar.update(3, "spinner")
    bar.finish()
    assert "spinner" in capsys.readouterr().out


def test_logger_prints_and_writes_file(tmp_path, capsys):
    logfile = tmp_path / "run.log"
    log = eqsl_download.Logger(logfile)
    log("hello world")
    log.close()
    assert "hello world" in capsys.readouterr().out
    assert "hello world" in logfile.read_text(encoding="utf-8")


def test_logger_without_file(capsys):
    log = eqsl_download.Logger(None)
    log("only console")
    log.close()
    assert "only console" in capsys.readouterr().out


def test_prompt_credentials(monkeypatch):
    answers = iter(["LU1ABC", "Home"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(eqsl_download.getpass, "getpass", lambda *a: "secret")
    assert eqsl_download.prompt_credentials() == ("LU1ABC", "secret", "Home")


def test_prompt_requires_callsign(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "")
    with pytest.raises(SystemExit):
        eqsl_download.prompt_credentials()


def test_prompt_requires_password(monkeypatch):
    answers = iter(["LU1ABC", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(eqsl_download.getpass, "getpass", lambda *a: "")
    with pytest.raises(SystemExit):
        eqsl_download.prompt_credentials()


def test_write_failures_empty(tmp_path):
    log = eqsl_download.Logger(None)
    assert eqsl_download._write_failures(tmp_path, [], log) is None


def test_write_failures_lists_names(tmp_path):
    log = eqsl_download.Logger(None)
    qso = QSO("W1AW", "20240101", "1200", "20m", "FT8")
    out = eqsl_download._write_failures(tmp_path, [("a.jpg", qso, "err")], log)
    assert out is not None
    assert "a.jpg" in out.read_text(encoding="utf-8")


def test_download_one_flags_invalid_image(monkeypatch, tmp_path):
    def fake_download(session, username, password, qso, path, log=None, **kwargs):
        path.write_bytes(b"broken")
        return True, "ok"

    monkeypatch.setattr(eqsl_download, "download_card", fake_download)
    target = tmp_path / "x.jpg"
    ok, msg = eqsl_download._download_one(
        None, "u", "p", QSO("W1AW", "20240101", "1200", "20m", "FT8"),
        target, eqsl_download.Logger(None),
    )
    assert ok is False
    assert "validation" in msg
    assert not target.exists()


def test_download_one_wraps_request_exception(monkeypatch, tmp_path):
    def fake_download(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(eqsl_download, "download_card", fake_download)
    ok, msg = eqsl_download._download_one(
        None, "u", "p", QSO("W1AW", "20240101", "1200", "20m", "FT8"),
        tmp_path / "x.jpg", eqsl_download.Logger(None),
    )
    assert ok is False
    assert "boom" in msg

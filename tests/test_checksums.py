"""Tests for the release checksum generator."""

from __future__ import annotations

import hashlib

import make_checksums


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_writes_sha256sums(tmp_path, capsys):
    (tmp_path / "app.AppImage").write_bytes(b"hello")
    (tmp_path / "setup.exe").write_bytes(b"world")

    assert make_checksums.main(["make_checksums.py", str(tmp_path)]) == 0

    lines = (tmp_path / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert f"{_sha(b'hello')}  app.AppImage" in lines
    assert f"{_sha(b'world')}  setup.exe" in lines
    assert "app.AppImage" in capsys.readouterr().out


def test_empty_directory_returns_error(tmp_path):
    assert make_checksums.main(["x", str(tmp_path)]) == 1


def test_non_directory_returns_error(tmp_path):
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    assert make_checksums.main(["x", str(target)]) == 2


def test_existing_sha256sums_is_ignored(tmp_path):
    (tmp_path / "a.AppImage").write_bytes(b"a")
    (tmp_path / "SHA256SUMS").write_text("old content", encoding="utf-8")

    assert make_checksums.main(["x", str(tmp_path)]) == 0
    text = (tmp_path / "SHA256SUMS").read_text(encoding="utf-8")
    assert "a.AppImage" in text
    assert "old content" not in text

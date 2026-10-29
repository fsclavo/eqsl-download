"""GUI smoke tests (need tkinter and a display; run under xvfb in CI)."""

from __future__ import annotations

import os

import pytest

tk = pytest.importorskip("tkinter")

import eqsl_gui  # noqa: E402  (import after the tkinter guard)

pytestmark = pytest.mark.gui


def _require_display():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("no display available")


def test_gui_builds_and_toggles_date_mode():
    _require_display()
    root = tk.Tk()
    try:
        app = eqsl_gui.EQSLCardGUI(root)
        root.update()

        assert app.date_mode_var.get() == "year"
        assert str(app.year_entry["state"]) == "normal"
        assert str(app.from_entry["state"]) == "disabled"

        app.date_mode_var.set("range")
        app._update_date_mode()
        root.update()
        assert str(app.from_entry["state"]) == "normal"
        assert str(app.year_entry["state"]) == "disabled"
    finally:
        root.destroy()


def test_gui_resolves_year_and_range():
    _require_display()
    root = tk.Tk()
    try:
        app = eqsl_gui.EQSLCardGUI(root)

        app.date_mode_var.set("year")
        app.year_var.set("2024")
        assert app._resolve_dates() == (
            eqsl_gui.date(2024, 1, 1),
            eqsl_gui.date(2024, 12, 31),
        )

        app.date_mode_var.set("range")
        app.from_var.set("2024-01-01")
        app.to_var.set("2024-06-30")
        assert app._resolve_dates() == (
            eqsl_gui.date(2024, 1, 1),
            eqsl_gui.date(2024, 6, 30),
        )
    finally:
        root.destroy()


def test_prefs_roundtrip_excludes_password(tmp_path):
    path = tmp_path / "config.json"
    prefs = {
        "callsign": "LU1ABC",
        "password": "SECRET",
        "dest": "/tmp/cards",
        "date_mode": "range",
        "year": "2024",
        "from": "2024-01-01",
        "to": "2024-12-31",
        "geometry": "800x600",
        "junk": "ignored",
    }
    assert eqsl_gui.save_prefs(prefs, path) is True
    loaded = eqsl_gui.load_prefs(path)
    assert loaded["callsign"] == "LU1ABC"
    assert "password" not in loaded
    assert "junk" not in loaded


def test_prefs_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert eqsl_gui.load_prefs(path) == {}


def test_prefs_non_object_returns_empty(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert eqsl_gui.load_prefs(path) == {}


def test_validate_worker_uses_date_range(monkeypatch, tmp_path):
    _require_display()
    monkeypatch.setattr(eqsl_gui, "load_prefs", lambda *a, **k: {})
    root = tk.Tk()
    try:
        app = eqsl_gui.EQSLCardGUI(root)
        calls = {}

        def fake_list(dest, d_from, d_to):
            calls["args"] = (dest, d_from, d_to)
            return []

        monkeypatch.setattr(eqsl_gui, "list_local_jpgs", fake_list)
        d_from = eqsl_gui.date(2024, 1, 1)
        d_to = eqsl_gui.date(2024, 12, 31)
        app._validate_worker(tmp_path, (d_from, d_to))

        assert calls["args"] == (tmp_path, d_from, d_to)
        messages = []
        while not app.msg_queue.empty():
            messages.append(app.msg_queue.get_nowait())
        assert messages[-1][0] == "done"
    finally:
        root.destroy()



def test_format_summary_download_and_validate():
    download = eqsl_gui.format_summary(
        {
            "kind": "download",
            "downloaded": 2,
            "skipped": 1,
            "corrupt": 0,
            "failed": 1,
            "total": 4,
            "dest": "/tmp/cards",
        }
    )
    assert "Downloaded: 2" in download
    assert "Failed: 1" in download
    assert "/tmp/cards" in download

    validate = eqsl_gui.format_summary(
        {"kind": "validate", "ok": 3, "corrupt": 1, "total": 4, "dest": "/tmp/x"}
    )
    assert "Valid: 3" in validate
    assert "Corrupt: 1" in validate

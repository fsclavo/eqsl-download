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

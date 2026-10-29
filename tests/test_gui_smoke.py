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

        monkeypatch.setattr(eqsl_gui, "list_local_images", fake_list)
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


# ---------------------------------------------------------------- GUI workers
ADIF = (
    "<EOH>\n"
    "<CALL:5>W1AW <QSO_DATE:8>20240315 <TIME_ON:4>1200 <BAND:3>20m <MODE:3>FT8 <EOR>\n"
    "<CALL:4>K1ZZ<QSO_DATE:8>20240316<TIME_ON:4>0900<BAND:3>40m<MODE:2>CW<EOR>\n"
)


@pytest.fixture
def gui_app(monkeypatch):
    _require_display()
    monkeypatch.setattr(eqsl_gui, "load_prefs", lambda *a, **k: {})
    root = tk.Tk()
    app = eqsl_gui.EQSLCardGUI(root)
    try:
        yield app, root
    finally:
        root.destroy()


def _drain(app):
    messages = []
    while not app.msg_queue.empty():
        messages.append(app.msg_queue.get_nowait())
    return messages


def _fake_scan_all_missing(monkeypatch):
    def fake_scan(by_name, dest, workers, progress=None):
        items = list(by_name.items())
        if progress:
            progress(len(items), len(items), "missing")
        return 0, 0, len(items), items, [], [name for name, _ in items]

    monkeypatch.setattr(eqsl_gui, "scan_local_qsos", fake_scan)


def _fake_session(monkeypatch):
    monkeypatch.setattr(eqsl_gui, "make_session", lambda: object())


def test_toggle_password_and_prefs(gui_app):
    app, _root = gui_app
    assert str(app.pass_entry["show"]) == "*"
    app.show_pass_var.set(True)
    app._toggle_password()
    assert str(app.pass_entry["show"]) == ""
    app.show_pass_var.set(False)
    app._toggle_password()

    app.callsign_var.set("LU1ABC")
    app.qth_var.set("Home")
    app.dest_var.set("/tmp/cards")
    prefs = app._current_prefs()
    assert prefs["callsign"] == "LU1ABC"
    assert "password" not in prefs


def test_open_dest_calls_open_path(monkeypatch, gui_app, tmp_path):
    app, _root = gui_app
    called = {}
    monkeypatch.setattr(eqsl_gui, "open_path", lambda p: called.setdefault("path", p))
    app.dest_var.set(str(tmp_path))
    app._open_dest()
    assert called["path"] == tmp_path


def test_on_cancel_sets_event(gui_app):
    app, _root = gui_app
    app.busy = True
    app._on_cancel()
    assert app.cancel_event.is_set()


def test_interruptible_sleep(gui_app):
    app, _root = gui_app
    assert app._interruptible_sleep(0.01) is True
    app.cancel_event.set()
    assert app._interruptible_sleep(5) is False


def test_download_worker_success(monkeypatch, gui_app, tmp_path, jpeg_bytes):
    app, _root = gui_app
    _fake_session(monkeypatch)
    _fake_scan_all_missing(monkeypatch)
    monkeypatch.setattr(eqsl_gui, "GET_DELAY_SEC", 0)
    monkeypatch.setattr(eqsl_gui, "fetch_inbox_adif", lambda *a, **k: ADIF)

    def fake_download(session, u, p, qso, dest_dir, base, log=None, **kwargs):
        (dest_dir / (base + ".jpg")).write_bytes(jpeg_bytes)
        return True, "ok"

    monkeypatch.setattr(eqsl_gui, "download_card", fake_download)

    app._download_worker(
        "LU1ABC", "pw", "", (eqsl_gui.date(2024, 1, 1), eqsl_gui.date(2024, 12, 31)),
        tmp_path,
    )
    done = [m for m in _drain(app) if m[0] == "done"]
    assert done
    summary = done[-1][2]
    assert summary["kind"] == "download"
    assert summary["downloaded"] == 2
    assert summary["failed"] == 0


def test_download_worker_cancelled(monkeypatch, gui_app, tmp_path):
    app, _root = gui_app
    _fake_session(monkeypatch)
    monkeypatch.setattr(eqsl_gui, "fetch_inbox_adif", lambda *a, **k: ADIF)
    app.cancel_event.set()

    app._download_worker(
        "LU1ABC", "pw", "", (eqsl_gui.date(2024, 1, 1), eqsl_gui.date(2024, 12, 31)),
        tmp_path,
    )
    done = [m for m in _drain(app) if m[0] == "done"]
    assert done and done[-1][1] == "Cancelled."


def test_download_worker_auth_error(monkeypatch, gui_app, tmp_path):
    app, _root = gui_app
    _fake_session(monkeypatch)

    def boom(*a, **k):
        raise eqsl_gui.EQSLError("Login failed: bad credentials")

    monkeypatch.setattr(eqsl_gui, "fetch_inbox_adif", boom)

    app._download_worker(
        "LU1ABC", "pw", "", (eqsl_gui.date(2024, 1, 1), eqsl_gui.date(2024, 12, 31)),
        tmp_path,
    )
    errors = [m for m in _drain(app) if m[0] == "error"]
    assert errors and "Login failed" in errors[0][1]


def test_download_worker_no_qsos(monkeypatch, gui_app, tmp_path):
    app, _root = gui_app
    _fake_session(monkeypatch)
    monkeypatch.setattr(eqsl_gui, "fetch_inbox_adif", lambda *a, **k: "<EOH>\n")

    app._download_worker(
        "LU1ABC", "pw", "", (eqsl_gui.date(2024, 1, 1), eqsl_gui.date(2024, 12, 31)),
        tmp_path,
    )
    done = [m for m in _drain(app) if m[0] == "done"]
    assert done and done[-1][2]["total"] == 0


def test_download_worker_retries_and_writes_failures(monkeypatch, gui_app, tmp_path):
    app, _root = gui_app
    _fake_session(monkeypatch)
    _fake_scan_all_missing(monkeypatch)
    monkeypatch.setattr(eqsl_gui, "GET_DELAY_SEC", 0)
    monkeypatch.setattr(eqsl_gui, "fetch_inbox_adif", lambda *a, **k: ADIF)
    monkeypatch.setattr(
        eqsl_gui, "download_card", lambda *a, **k: (False, "boom")
    )

    app._download_worker(
        "LU1ABC", "pw", "", (eqsl_gui.date(2024, 1, 1), eqsl_gui.date(2024, 12, 31)),
        tmp_path,
    )
    done = [m for m in _drain(app) if m[0] == "done"]
    assert done and done[-1][2]["failed"] == 2
    assert list(tmp_path.glob("eqsl_failed_*.txt"))


def test_validate_worker_counts(monkeypatch, gui_app, tmp_path):
    app, _root = gui_app
    monkeypatch.setattr(
        eqsl_gui, "list_local_images", lambda dest, d_from, d_to: [tmp_path / "a.jpg"]
    )
    monkeypatch.setattr(
        eqsl_gui, "validate_local_paths", lambda paths, workers, progress=None: (1, 1, ["b.jpg"])
    )
    app._validate_worker(tmp_path, (eqsl_gui.date(2024, 1, 1), eqsl_gui.date(2024, 12, 31)))
    done = [m for m in _drain(app) if m[0] == "done"]
    assert done and done[-1][2] == {
        "kind": "validate", "ok": 1, "corrupt": 1, "total": 1, "dest": str(tmp_path)
    }


def test_drain_queue_shows_summary(monkeypatch, gui_app):
    app, root = gui_app
    monkeypatch.setattr(root, "after", lambda *a, **k: None)
    shown = {}
    monkeypatch.setattr(
        eqsl_gui.messagebox, "showinfo", lambda *a, **k: shown.setdefault("args", a)
    )
    app._set_busy(True)
    summary = {
        "kind": "download", "downloaded": 1, "skipped": 0,
        "corrupt": 0, "failed": 0, "total": 1, "dest": "/tmp/x",
    }
    app.msg_queue.put(("done", "Download finished", summary))
    app._drain_queue()
    assert "args" in shown
    assert app.busy is False


def test_log_scrollbars_layout(gui_app):
    app, _root = gui_app
    info_x = app.log_scroll_x.grid_info()
    info_y = app.log_scroll_y.grid_info()
    assert int(info_x["row"]) == 1
    assert int(info_x["column"]) == 0
    assert int(info_y["row"]) == 0
    assert int(info_y["column"]) == 1


def test_validate_worker_logs_extension_mismatch(
    monkeypatch, gui_app, tmp_path, jpeg_bytes
):
    app, _root = gui_app
    path = tmp_path / "LU1-20240101_0000-X-20m-FT8.png"
    path.write_bytes(jpeg_bytes)
    monkeypatch.setattr(
        eqsl_gui, "list_local_images", lambda dest, d_from, d_to: [path]
    )
    monkeypatch.setattr(
        eqsl_gui, "validate_local_paths", lambda paths, workers, progress=None: (1, 0, [])
    )
    app._validate_worker(
        tmp_path, (eqsl_gui.date(2024, 1, 1), eqsl_gui.date(2024, 12, 31))
    )
    messages = _drain(app)
    assert any(
        m[0] == "log" and "EXTENSION" in m[1] and "-> .jpg" in m[1]
        for m in messages
    )

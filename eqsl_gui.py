#!/usr/bin/env python3
"""eQSL Card Downloader — graphical front-end for the eQSL.cc card downloader.

Tkinter GUI sharing all logic with the CLI via :mod:`eqsl_core`. Runs the
network work in a background thread so the window stays responsive, and reports
progress/log lines back to the widget thread through a queue.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import requests

from eqsl_core import (
    DEFAULT_SCAN_WORKERS,
    GET_DELAY_SEC,
    EQSLError,
    QSO,
    build_by_name,
    detect_image_kind,
    download_card,
    fetch_inbox_adif,
    get_version,
    is_valid_jpeg,
    list_local_jpgs,
    make_session,
    parse_adif,
    scan_local_qsos,
    validate_local_paths,
)

APP_NAME = "eQSL Card Downloader"
APP_VERSION = get_version()

SCRIPT_DIR = Path(__file__).resolve().parent

PREFS_KEYS = (
    "callsign",
    "qth",
    "dest",
    "date_mode",
    "year",
    "from",
    "to",
    "geometry",
)


def _resource_path(relative: str) -> Path:
    """Resolve a bundled resource path (PyInstaller _MEIPASS aware)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return SCRIPT_DIR / relative


def _default_dest() -> Path:
    """A writable default folder in the user's home directory."""
    home = Path.home()
    for candidate in (home / "eQSL-Card-Downloader", home):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return home


def _config_path() -> Path:
    """Per-user config file location using only the standard library."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "eQSL-Card-Downloader" / "config.json"


def load_prefs(path: Optional[Path] = None) -> dict:
    """Load saved preferences, returning {} when missing or corrupt."""
    path = path or _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in PREFS_KEYS}


def save_prefs(prefs: dict, path: Optional[Path] = None) -> bool:
    """Persist only allow-listed keys (never the password)."""
    path = path or _config_path()
    clean = {k: prefs[k] for k in PREFS_KEYS if k in prefs}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def _write_failures(dest: Path, failed: list[tuple[str, QSO, str]]) -> Optional[Path]:
    if not failed:
        return None
    failures_file = dest / f"eqsl_failed_{date.today():%Y%m%d}.txt"
    try:
        failures_file.write_text(
            "\n".join(name for name, _qso, _msg in failed) + "\n", encoding="utf-8"
        )
        return failures_file
    except OSError:
        return None


class EQSLCardGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.busy = False
        self.prefs = load_prefs()

        self._build_ui()
        self._apply_prefs()
        self._set_icon()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_queue)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.root.title(APP_NAME)
        self.root.minsize(720, 560)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Header.TLabel", font=("TkDefaultFont", 15, "bold"))
        style.configure("Sub.TLabel", foreground="#555555")

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(anchor="w")
        ttk.Label(header, text=f"v{APP_VERSION}", style="Sub.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Download your eQSL.cc QSL cards as images",
            style="Sub.TLabel",
        ).pack(anchor="w")

        # ---- Credentials ----
        cred = ttk.LabelFrame(outer, text="eQSL credentials", padding=10)
        cred.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        cred.columnconfigure(1, weight=1)

        ttk.Label(cred, text="Callsign:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.callsign_var = tk.StringVar()
        ttk.Entry(cred, textvariable=self.callsign_var).grid(row=0, column=1, sticky="ew")

        ttk.Label(cred, text="Password:").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0)
        )
        self.password_var = tk.StringVar()
        self.show_pass_var = tk.BooleanVar(value=False)
        pass_frame = ttk.Frame(cred)
        pass_frame.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        pass_frame.columnconfigure(0, weight=1)
        self.pass_entry = ttk.Entry(
            pass_frame, textvariable=self.password_var, show="*"
        )
        self.pass_entry.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(
            pass_frame,
            text="Show",
            variable=self.show_pass_var,
            command=self._toggle_password,
        ).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(cred, text="QTH (optional):").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(6, 0)
        )
        self.qth_var = tk.StringVar()
        ttk.Entry(cred, textvariable=self.qth_var).grid(row=2, column=1, sticky="ew", pady=(6, 0))

        # ---- Date range ----
        rng = ttk.LabelFrame(outer, text="QSO date range", padding=10)
        rng.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        rng.columnconfigure(3, weight=1)

        self.date_mode_var = tk.StringVar(value="year")
        ttk.Radiobutton(
            rng, text="Full year", value="year",
            variable=self.date_mode_var, command=self._update_date_mode,
        ).grid(row=0, column=0, sticky="w")
        self.year_var = tk.StringVar(value=str(date.today().year))
        self.year_entry = ttk.Entry(rng, textvariable=self.year_var, width=8)
        self.year_entry.grid(row=0, column=1, sticky="w", padx=(6, 20))

        ttk.Radiobutton(
            rng, text="From:", value="range",
            variable=self.date_mode_var, command=self._update_date_mode,
        ).grid(row=0, column=2, sticky="w")
        self.from_var = tk.StringVar(value=f"{date.today().year}-01-01")
        self.from_entry = ttk.Entry(rng, textvariable=self.from_var, width=12)
        self.from_entry.grid(row=0, column=3, sticky="w", padx=(6, 10))

        ttk.Label(rng, text="To:").grid(row=0, column=4, sticky="w")
        self.to_var = tk.StringVar(value=date.today().strftime("%Y-%m-%d"))
        self.to_entry = ttk.Entry(rng, textvariable=self.to_var, width=12)
        self.to_entry.grid(row=0, column=5, sticky="w", padx=(6, 0))

        ttk.Label(
            rng, text="Date format: YYYY-MM-DD", style="Sub.TLabel"
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        # ---- Destination ----
        dest = ttk.LabelFrame(outer, text="Destination folder", padding=10)
        dest.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        dest.columnconfigure(0, weight=1)
        self.dest_var = tk.StringVar(value=str(_default_dest()))
        ttk.Entry(dest, textvariable=self.dest_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(dest, text="Choose…", command=self._choose_dest).grid(
            row=0, column=1, padx=(8, 0)
        )

        # ---- Actions ----
        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.download_btn = ttk.Button(
            actions, text="Download cards", command=self._on_download
        )
        self.download_btn.pack(side="left")
        self.validate_btn = ttk.Button(
            actions, text="Validate folder", command=self._on_validate
        )
        self.validate_btn.pack(side="left", padx=(8, 0))
        self.cancel_btn = ttk.Button(
            actions, text="Stop", command=self._on_cancel, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Quit", command=self._on_close).pack(side="right")

        # ---- Progress + log ----
        prog = ttk.Frame(outer)
        prog.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        prog.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(prog, textvariable=self.status_var).grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="Log", padding=6)
        log_frame.grid(row=6, column=0, sticky="nsew", pady=(10, 0))
        outer.rowconfigure(6, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame, height=10, wrap="none", state="disabled",
            background="#111111", foreground="#dddddd", insertbackground="#dddddd",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll_y = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        scroll_x.grid(row=0, column=2, sticky="ew")
        self.log_text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

        self._update_date_mode()

    def _apply_prefs(self) -> None:
        p = self.prefs
        if p.get("callsign"):
            self.callsign_var.set(str(p["callsign"]))
        if p.get("qth"):
            self.qth_var.set(str(p["qth"]))
        if p.get("dest"):
            self.dest_var.set(str(p["dest"]))
        if p.get("date_mode") in ("year", "range"):
            self.date_mode_var.set(str(p["date_mode"]))
        if p.get("year"):
            self.year_var.set(str(p["year"]))
        if p.get("from"):
            self.from_var.set(str(p["from"]))
        if p.get("to"):
            self.to_var.set(str(p["to"]))
        geometry = p.get("geometry")
        if isinstance(geometry, str):
            try:
                self.root.geometry(geometry)
            except tk.TclError:
                pass
        self._update_date_mode()

    def _current_prefs(self) -> dict:
        geometry = ""
        try:
            geometry = self.root.geometry()
        except tk.TclError:
            pass
        return {
            "callsign": self.callsign_var.get().strip(),
            "qth": self.qth_var.get().strip(),
            "dest": self.dest_var.get().strip(),
            "date_mode": self.date_mode_var.get(),
            "year": self.year_var.get().strip(),
            "from": self.from_var.get().strip(),
            "to": self.to_var.get().strip(),
            "geometry": geometry,
        }

    def _set_icon(self) -> None:
        for name in ("eqsl-card-downloader.png", "icon.png"):
            icon_path = _resource_path(os.path.join("assets", name))
            if icon_path.is_file():
                try:
                    self._icon_image = tk.PhotoImage(file=str(icon_path))
                    self.root.iconphoto(True, self._icon_image)
                    return
                except tk.TclError:
                    pass

    def _toggle_password(self) -> None:
        self.pass_entry.configure(show="" if self.show_pass_var.get() else "*")

    def _update_date_mode(self) -> None:
        is_year = self.date_mode_var.get() == "year"
        self.year_entry.configure(state="normal" if is_year else "disabled")
        state = "disabled" if is_year else "normal"
        self.from_entry.configure(state=state)
        self.to_entry.configure(state=state)

    def _choose_dest(self) -> None:
        chosen = filedialog.askdirectory(
            title="Choose destination folder",
            initialdir=self.dest_var.get() or str(Path.home()),
        )
        if chosen:
            self.dest_var.set(chosen)

    # -------------------------------------------------------------- helpers
    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.download_btn.configure(state=state)
        self.validate_btn.configure(state=state)
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        for entry in (
            self.pass_entry, self.year_entry, self.from_entry, self.to_entry,
        ):
            if entry is not None:
                entry.configure(state="disabled" if busy else "normal")
        if not busy:
            self._update_date_mode()

    def _resolve_dates(self) -> Optional[tuple[date, date]]:
        if self.date_mode_var.get() == "year":
            raw = self.year_var.get().strip()
            if not raw.isdigit() or len(raw) != 4:
                messagebox.showerror(APP_NAME, "Enter a valid 4-digit year.")
                return None
            year = int(raw)
            return date(year, 1, 1), date(year, 12, 31)
        try:
            d_from = datetime.strptime(self.from_var.get().strip(), "%Y-%m-%d").date()
            d_to = datetime.strptime(self.to_var.get().strip(), "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror(APP_NAME, "Invalid dates. Use the YYYY-MM-DD format.")
            return None
        if d_to < d_from:
            messagebox.showerror(APP_NAME, "'To' must be on or after 'From'.")
            return None
        return d_from, d_to

    # -------------------------------------------------------------- actions
    def _on_download(self) -> None:
        if self.busy:
            return
        callsign = self.callsign_var.get().strip()
        password = self.password_var.get()
        qth = self.qth_var.get().strip()
        if not callsign:
            messagebox.showerror(APP_NAME, "Enter your callsign.")
            return
        if not password:
            messagebox.showerror(APP_NAME, "Enter your password.")
            return
        dates = self._resolve_dates()
        if dates is None:
            return
        dest = Path(self.dest_var.get().strip() or _default_dest()).expanduser()
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not create the folder:\n{exc}")
            return

        self._log("")
        self._log(f"=== Download {dates[0]} .. {dates[1]} -> {dest} ===")
        self._start_worker(self._download_worker, callsign, password, qth, dates, dest)

    def _on_validate(self) -> None:
        if self.busy:
            return
        dest = Path(self.dest_var.get().strip() or _default_dest()).expanduser()
        if not dest.is_dir():
            messagebox.showerror(APP_NAME, f"The folder does not exist:\n{dest}")
            return
        dates = self._resolve_dates()
        if dates is None:
            return
        self._log("")
        self._log(f"=== Local validation {dates[0]} .. {dates[1]} -> {dest} ===")
        self._start_worker(self._validate_worker, dest, dates)

    def _on_cancel(self) -> None:
        if self.busy:
            self.cancel_event.set()
            self.status_var.set("Cancelling…")
            self._log("Cancellation requested (stops after the current step).")

    def _start_worker(self, target, *args) -> None:
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.configure(value=0)
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    # -------------------------------------------------------------- workers
    def _emit(self, kind: str, *payload) -> None:
        self.msg_queue.put((kind, *payload))

    def _progress_cb(self, done: int, total: int, text: str) -> None:
        self._emit("progress", done, total, text)

    def _validate_worker(self, dest: Path, dates: tuple[date, date]) -> None:
        try:
            paths = list_local_jpgs(dest, dates[0], dates[1])
            self._emit("log", f".jpg files in range: {len(paths)}")
            if not paths:
                self._emit("done", "Validation: no files to check in the selected range.")
                return
            ok, corrupt, corrupt_names = validate_local_paths(
                paths,
                min(DEFAULT_SCAN_WORKERS, os.cpu_count() or 4),
                progress=self._progress_cb,
            )
            for name in sorted(corrupt_names):
                self._emit("log", f"  CORRUPT: {name}")
            self._emit(
                "done",
                f"Validation finished — ok={ok} corrupt={corrupt} total={len(paths)}",
            )
        except Exception as exc:  # noqa: BLE001 - surface any error to the user
            self._emit("error", f"Validation error: {exc}")

    def _download_pass(
        self, session, callsign, password, items, dest
    ) -> tuple[int, list[tuple[str, QSO, str]]]:
        downloaded = 0
        failed: list[tuple[str, QSO, str]] = []
        total = len(items)
        for i, (name, qso) in enumerate(items, start=1):
            if self.cancel_event.is_set():
                break
            self._emit("status", f"[{i}/{total}] {name}")
            path = dest / name
            try:
                ok, msg = download_card(
                    session, callsign, password, qso, path,
                    log=lambda m: self._emit("log", m),
                )
            except requests.RequestException as exc:
                ok, msg = False, str(exc)

            if ok and not is_valid_jpeg(path):
                ok = False
                try:
                    raw = path.read_bytes()
                    msg = (
                        "Downloaded file failed image validation "
                        f"(kind={detect_image_kind(raw)}, size={len(raw)})"
                    )
                except OSError:
                    msg = "Downloaded file unreadable"
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

            if ok:
                downloaded += 1
            else:
                failed.append((name, qso, msg))
                self._emit("log", f"  FAILED {name}: {msg}")

            self._emit("progress", i, total, f"ok={downloaded} fail={len(failed)}")
            if i < total and not self._interruptible_sleep(GET_DELAY_SEC):
                break
        return downloaded, failed

    def _download_worker(
        self,
        callsign: str,
        password: str,
        qth: str,
        dates: tuple[date, date],
        dest: Path,
    ) -> None:
        d_from, d_to = dates
        workers = min(DEFAULT_SCAN_WORKERS, os.cpu_count() or 4)
        try:
            session = make_session()

            self._emit("status", "Downloading ADIF from eQSL…")
            adif_text = fetch_inbox_adif(
                session, callsign, password, qth, d_from, d_to,
                progress=self._progress_cb,
            )
            if self.cancel_event.is_set():
                self._emit("done", "Cancelled.")
                return

            qsos = parse_adif(adif_text)
            self._emit("log", f"Records parsed: {len(qsos)}")
            if not qsos:
                self._emit("done", "No QSOs in the selected range.")
                return

            by_name, collisions = build_by_name(qsos, callsign)
            total = len(by_name)
            self._emit("log", f"Unique files: {total}")
            if collisions:
                self._emit(
                    "log",
                    f"WARNING: {len(collisions)} filename collision(s) "
                    "(same call/date/band/mode/minute); keeping one card per name",
                )
                for name in sorted(collisions):
                    self._emit("log", f"  collision: {name}")

            self._emit("status", "Scanning local folder…")
            skipped, corrupt, missing, to_fetch, corrupt_names, _missing = (
                scan_local_qsos(by_name, dest, workers, progress=self._progress_cb)
            )
            for name in sorted(corrupt_names):
                self._emit("log", f"  CORRUPT (will re-download): {name}")
            self._emit(
                "log",
                f"Summary — valid: {skipped} | corrupt: {corrupt} | "
                f"missing: {missing} | to download: {len(to_fetch)}",
            )

            if not to_fetch:
                self._emit("done", "Nothing pending: everything is downloaded and valid.")
                return

            downloaded, failed = self._download_pass(
                session, callsign, password, to_fetch, dest
            )

            if failed and not self.cancel_event.is_set():
                self._emit("log", f"Retrying {len(failed)} failed download(s)…")
                items = [(name, qso) for name, qso, _msg in failed]
                retry_downloaded, still_failed = self._download_pass(
                    session, callsign, password, items, dest
                )
                downloaded += retry_downloaded
                failed = still_failed

            failures_file = _write_failures(dest, failed)
            if failures_file is not None:
                self._emit("log", f"Failures written to {failures_file}")

            self._emit(
                "done",
                f"Download finished — new={downloaded} valid={skipped} "
                f"failed={len(failed)} total={total}",
            )
        except (requests.RequestException, EQSLError) as exc:
            self._emit("error", str(exc))
        except Exception as exc:  # noqa: BLE001 - surface any error to the user
            self._emit("error", f"Unexpected error: {exc}")

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep, returning False early if cancellation was requested."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.cancel_event.is_set():
                return False
            time.sleep(0.2)
        return True

    # ------------------------------------------------------------ queue pump
    def _drain_queue(self) -> None:
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "progress":
                    done, total, text = msg[1], msg[2], msg[3]
                    if total > 0:
                        self.progress.configure(mode="determinate", maximum=total, value=done)
                    if text:
                        self.status_var.set(text)
                elif kind == "done":
                    self._log(msg[1])
                    self.status_var.set(msg[1])
                    self.progress.configure(mode="determinate", value=self.progress["maximum"])
                    self._set_busy(False)
                elif kind == "error":
                    self._log("ERROR: " + msg[1])
                    self.status_var.set("Error.")
                    self._set_busy(False)
                    messagebox.showerror(APP_NAME, msg[1])
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(
                APP_NAME,
                "A task is running. Cancel and quit?",
            ):
                return
            self.cancel_event.set()
        save_prefs(self._current_prefs())
        self.root.destroy()


def main() -> int:
    # Allow the bundled binary to double as the command-line tool, e.g.:
    #   eQSL_Card_Downloader --validate-only --dest ~/cards
    if len(sys.argv) > 1:
        from eqsl_download import main as cli_main

        return cli_main()

    root = tk.Tk(className="eQSL-Card-Downloader")
    EQSLCardGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())

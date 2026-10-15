#!/usr/bin/env python3
"""Download eQSL.cc QSL card images for a date range (command line).

Uses the official DownloadInBox + GeteQSL API. Skips files that already
exist in the destination folder, re-downloads corrupt ones, and rate-limits
GeteQSL to under 6 requests/minute. Accepts JPEG or PNG payloads.

All download/parse/validation logic lives in :mod:`eqsl_core`; this module only
handles the command-line interface.
"""

from __future__ import annotations

import argparse
import getpass
import multiprocessing
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests

from eqsl_core import (
    DEFAULT_SCAN_WORKERS,
    GET_DELAY_SEC,
    EQSLError,
    QSO,
    detect_image_kind,
    download_card,
    fetch_inbox_adif,
    format_eta,
    is_valid_jpeg,
    list_local_jpgs,
    make_session,
    parse_adif,
    scan_local_qsos,
    validate_local_paths,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DEST = SCRIPT_DIR


class ProgressBar:
    """Single-line progress bar (stdlib), similar to pip's style."""

    def __init__(self, total: int, width: int = 36) -> None:
        self.total = max(int(total), 0)
        self.width = width
        self.current = 0
        self._last_len = 0

    def _render(self, current: int, suffix: str = "") -> str:
        total = self.total if self.total > 0 else 1
        cur = min(max(current, 0), total) if self.total > 0 else current
        pct = 100 * cur // total if self.total > 0 else 100
        filled = self.width * cur // total if self.total > 0 else self.width
        bar = "━" * filled + "─" * (self.width - filled)
        if self.total > 0:
            core = f"  {bar} {pct:3d}% {cur}/{self.total}"
        else:
            core = f"  {bar} {cur}"
        if suffix:
            core = f"{core} • {suffix}"
        cols = shutil.get_terminal_size((100, 20)).columns
        max_len = max(cols - 1, 40)
        if len(core) > max_len:
            core = core[: max_len - 1] + "…"
        return core

    def update(self, current: int, suffix: str = "") -> None:
        self.current = current
        line = self._render(current, suffix)
        pad = max(self._last_len - len(line), 0)
        sys.stdout.write("\r" + line + (" " * pad))
        sys.stdout.flush()
        self._last_len = len(line)

    def note(self, message: str) -> None:
        """Print a message on its own line (clears the bar line first)."""
        sys.stdout.write("\r" + (" " * self._last_len) + "\r")
        print(f"  {message}")
        self._last_len = 0
        sys.stdout.flush()

    def finish(self, suffix: str = "") -> None:
        if self.total > 0:
            self.update(self.total, suffix)
        else:
            self.update(self.current, suffix)
        sys.stdout.write("\n")
        sys.stdout.flush()
        self._last_len = 0


def prompt_credentials() -> tuple[str, str, str]:
    callsign = input("Callsign: ").strip()
    if not callsign:
        sys.exit("Callsign is required.")
    password = getpass.getpass("Password: ")
    if not password:
        sys.exit("Password is required.")
    qth = input("QTH Nickname (optional, Enter to skip): ").strip()
    return callsign, password, qth


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download eQSL.cc card images for a QSO date range."
    )
    p.add_argument("--year", type=int, help="Shortcut: that calendar year")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="Start QSO date")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="End QSO date")
    p.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        metavar="DIR",
        help=f"Destination folder for card images (default: {DEFAULT_DEST})",
    )
    p.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate local card images only (no login, no download). Dates optional.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_SCAN_WORKERS,
        metavar="N",
        help=f"Processes for local image validation (default: {DEFAULT_SCAN_WORKERS})",
    )
    args = p.parse_args()

    if args.workers < 1:
        p.error("--workers must be >= 1")

    if args.year is not None:
        if args.date_from or args.date_to:
            p.error("Use either --year or --from/--to, not both.")
        args.date_from = f"{args.year:04d}-01-01"
        args.date_to = f"{args.year:04d}-12-31"

    has_from = bool(args.date_from)
    has_to = bool(args.date_to)
    if has_from ^ has_to:
        p.error("Provide both --from and --to, or neither (with --validate-only).")

    if not args.validate_only and not (has_from and has_to):
        p.error("Provide --year YEAR or both --from and --to (unless --validate-only).")

    args.from_date: Optional[date] = None
    args.to_date: Optional[date] = None
    if has_from and has_to:
        try:
            d_from = datetime.strptime(args.date_from, "%Y-%m-%d").date()
            d_to = datetime.strptime(args.date_to, "%Y-%m-%d").date()
        except ValueError as exc:
            p.error(f"Invalid date: {exc}")
        if d_to < d_from:
            p.error("--to must be on or after --from.")
        args.from_date = d_from
        args.to_date = d_to

    return args


def run_validate_only(args: argparse.Namespace) -> int:
    """Local-only validation: no credentials, no ADIF, no download."""
    dest: Path = args.dest
    print(f"[validate-only] Escaneando JPGs locales en {dest} (sin login / sin descarga)")
    if args.from_date and args.to_date:
        print(f"  filtro fechas QSO: {args.from_date} .. {args.to_date}")
    else:
        print("  sin filtro de fechas (todos los .jpg del directorio)")

    paths = list_local_jpgs(dest, args.from_date, args.to_date)
    print(f"  archivos a chequear: {len(paths)}")

    if not paths:
        print("  no hay archivos .jpg para validar")
        print("Done. validate_only ok=0 corrupt=0 total=0")
        return 0

    print(f"  validando imagen (JPEG/PNG, {args.workers} procesos) ...")
    bar = ProgressBar(len(paths))
    ok, corrupt, corrupt_names = validate_local_paths(
        paths, args.workers, progress=lambda d, t, s: bar.update(d, s)
    )
    bar.finish(f"ok={ok} corruptos={corrupt}")

    if corrupt_names:
        print(f"  corruptos ({len(corrupt_names)}):")
        for name in sorted(corrupt_names):
            print(f"    {name}")

    print(f"Done. validate_only ok={ok} corrupt={corrupt} total={len(paths)}")
    return 1 if corrupt else 0


def main() -> int:
    args = parse_args()
    dest: Path = args.dest

    if args.validate_only:
        return run_validate_only(args)

    assert args.from_date is not None and args.to_date is not None

    username, password, qth = prompt_credentials()

    session = make_session()

    print(f"[1/4] Descargando ADIF ({args.from_date} .. {args.to_date})")
    bar = ProgressBar(3)
    try:
        adif_text = fetch_inbox_adif(
            session,
            username,
            password,
            qth,
            args.from_date,
            args.to_date,
            progress=lambda d, t, s: bar.update(d, s),
        )
    except (requests.RequestException, EQSLError) as exc:
        bar.finish("error")
        print(str(exc), file=sys.stderr)
        return 1
    bar.finish()

    print("[2/4] Procesando ADIF ...")
    qsos = parse_adif(adif_text)
    print(f"  registros parseados: {len(qsos)}")

    if not qsos:
        print("Nothing to download.")
        return 0

    by_name: dict[str, QSO] = {}
    for qso in qsos:
        by_name[qso.filename(username)] = qso
    total = len(by_name)
    print(f"  únicos por archivo: {total}")

    print(f"[3/4] Escaneando directorio local ({dest.name}/) ...")
    print(f"  validando imagen (JPEG/PNG, {args.workers} procesos) ...")
    scan_bar = ProgressBar(total)
    (
        skipped,
        corrupt,
        _missing,
        to_fetch,
        _corrupt_names,
        _missing_names,
    ) = scan_local_qsos(
        by_name,
        dest,
        args.workers,
        progress=lambda d, t, s: scan_bar.update(d, s),
    )
    scan_bar.finish(
        f"ok={skipped} corruptos={corrupt} faltan={_missing}"
    )
    print(
        f"  resumen — válidos: {skipped} | corruptos: {corrupt} | "
        f"faltan: {_missing} | a descargar: {len(to_fetch)}"
    )

    pending = len(to_fetch)
    downloaded = 0
    failed = 0

    if pending == 0:
        print("[4/4] Nada pendiente de descargar.")
        print(
            f"Done. downloaded=0 skipped={skipped} corrupt={corrupt} "
            f"failed=0 total_unique={total}"
        )
        return 0

    print(f"[4/4] Descargando pendientes ({pending}) ...")
    dl_bar = ProgressBar(pending)

    for i, (name, qso) in enumerate(to_fetch, start=1):
        path = dest / name
        left = pending - i
        eta = format_eta(left) if left else "0s"
        dl_bar.update(i - 1, f"ETA {eta} | {name}")
        try:
            ok, msg = download_card(
                session, username, password, qso, path, log=dl_bar.note
            )
        except requests.RequestException as exc:
            ok, msg = False, str(exc)

        if ok and not is_valid_jpeg(path):
            ok = False
            try:
                raw = path.read_bytes()
                kind = detect_image_kind(raw)
                msg = (
                    f"Downloaded file failed image validation "
                    f"(kind={kind}, size={len(raw)}, magic={raw[:8].hex()})"
                )
            except OSError:
                msg = "Downloaded file failed image validation (unreadable)"
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

        if ok:
            downloaded += 1
        else:
            failed += 1
            dl_bar.note(f"FAILED {name}: {msg}")

        dl_bar.update(
            i,
            f"ok={downloaded} fail={failed} ETA {format_eta(pending - i)} | {name}",
        )

        if i < pending:
            import time

            time.sleep(GET_DELAY_SEC)

    dl_bar.finish(f"ok={downloaded} fail={failed}")
    print(
        f"Done. downloaded={downloaded} skipped={skipped} "
        f"corrupt_found={corrupt} failed={failed} total_unique={total}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())

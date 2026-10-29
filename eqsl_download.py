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
import time
from datetime import date, datetime
from pathlib import Path
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
    format_eta,
    get_version,
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


class Logger:
    """Console logger that optionally mirrors every line to a file."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def __call__(self, message: str = "") -> None:
        print(message)
        if self._fh is not None:
            stamp = datetime.now().isoformat(timespec="seconds")
            self._fh.write(f"{stamp} {message}\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def prompt_credentials() -> tuple[str, str, str]:
    callsign = input("Callsign: ").strip()
    if not callsign:
        sys.exit("Callsign is required.")
    password = getpass.getpass("Password: ")
    if not password:
        sys.exit("Password is required.")
    qth = input("QTH Nickname (optional, Enter to skip): ").strip()
    return callsign, password, qth


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str, str]:
    """Get credentials from flags/stdin or fall back to interactive prompts.

    ``--password-stdin`` makes the run fully non-interactive: ``--callsign`` is
    then required and the QTH nickname is optional (no prompt).
    """
    if args.password_stdin:
        callsign = (args.callsign or "").strip()
        if not callsign:
            sys.exit("--callsign is required with --password-stdin.")
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            sys.exit("Password is required (read from stdin).")
        return callsign, password, (args.qth or "").strip()

    callsign = (args.callsign or "").strip() or input("Callsign: ").strip()
    if not callsign:
        sys.exit("Callsign is required.")
    password = getpass.getpass("Password: ")
    if not password:
        sys.exit("Password is required.")
    if args.qth is not None:
        qth = args.qth.strip()
    else:
        qth = input("QTH Nickname (optional, Enter to skip): ").strip()
    return callsign, password, qth


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download eQSL.cc card images for a QSO date range."
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"eQSL Card Downloader {get_version()}",
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
        "--dry-run",
        action="store_true",
        help="Fetch the ADIF and list pending cards without downloading them.",
    )
    select = p.add_mutually_exclusive_group()
    select.add_argument(
        "--force",
        action="store_true",
        help="Re-download every card, ignoring valid files already on disk.",
    )
    select.add_argument(
        "--only-missing",
        action="store_true",
        help="Download only absent cards (leave corrupt files untouched).",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Append a timestamped log of the run to PATH.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_SCAN_WORKERS,
        metavar="N",
        help=f"Processes for local image validation (default: {DEFAULT_SCAN_WORKERS})",
    )
    p.add_argument(
        "--callsign",
        metavar="CALL",
        help="eQSL callsign (default: prompt). Required with --password-stdin.",
    )
    p.add_argument(
        "--qth",
        metavar="NAME",
        help="QTH nickname (optional; no prompt when given).",
    )
    p.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from the first line of stdin (non-interactive).",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=2,
        metavar="N",
        help="Attempts per card when GeteQSL throttles or returns no image (default: 2)",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=float(GET_DELAY_SEC),
        metavar="SECONDS",
        help=f"Delay between GeteQSL calls in seconds (default: {GET_DELAY_SEC})",
    )
    args = p.parse_args()

    if args.workers < 1:
        p.error("--workers must be >= 1")
    if args.retries < 1:
        p.error("--retries must be >= 1")
    if args.delay < 0:
        p.error("--delay must be >= 0")

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


def run_validate_only(args: argparse.Namespace, log: Logger) -> int:
    """Local-only validation: no credentials, no ADIF, no download."""
    dest: Path = args.dest
    log(f"[validate-only] Scanning local JPGs in {dest} (no login / no download)")
    if args.from_date and args.to_date:
        log(f"  QSO date filter: {args.from_date} .. {args.to_date}")
    else:
        log("  no date filter (all .jpg files in the folder)")

    paths = list_local_jpgs(dest, args.from_date, args.to_date)
    log(f"  files to check: {len(paths)}")

    if not paths:
        log("  no .jpg files to validate")
        log("Done. validate_only ok=0 corrupt=0 total=0")
        return 0

    log(f"  validating images (JPEG/PNG, {args.workers} processes) ...")
    bar = ProgressBar(len(paths))
    ok, corrupt, corrupt_names = validate_local_paths(
        paths, args.workers, progress=lambda d, t, s: bar.update(d, s)
    )
    bar.finish(f"ok={ok} corrupt={corrupt}")

    if corrupt_names:
        log(f"  corrupt ({len(corrupt_names)}):")
        for name in sorted(corrupt_names):
            log(f"    {name}")

    log(f"Done. validate_only ok={ok} corrupt={corrupt} total={len(paths)}")
    return 1 if corrupt else 0


def _download_one(
    session: requests.Session,
    username: str,
    password: str,
    qso: QSO,
    path: Path,
    log: Logger,
    max_attempts: int = 2,
) -> tuple[bool, str]:
    try:
        ok, msg = download_card(
            session, username, password, qso, path,
            log=log, max_attempts=max_attempts,
        )
    except requests.RequestException as exc:
        return False, str(exc)

    if ok and not is_valid_jpeg(path):
        ok = False
        try:
            raw = path.read_bytes()
            msg = (
                "Downloaded file failed image validation "
                f"(kind={detect_image_kind(raw)}, size={len(raw)})"
            )
        except OSError:
            msg = "Downloaded file failed image validation (unreadable)"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    return ok, msg


def _write_failures(
    dest: Path, failed: list[tuple[str, QSO, str]], log: Logger
) -> Optional[Path]:
    if not failed:
        return None
    failures_file = dest / f"eqsl_failed_{date.today():%Y%m%d}.txt"
    try:
        failures_file.write_text(
            "\n".join(name for name, _qso, _msg in failed) + "\n", encoding="utf-8"
        )
        log(f"  {len(failed)} card(s) still failing; list written to {failures_file}")
        return failures_file
    except OSError as exc:
        log(f"  could not write failures list: {exc}")
        return None


def main() -> int:
    args = parse_args()
    dest: Path = args.dest
    log = Logger(args.log_file)

    try:
        if args.validate_only:
            return run_validate_only(args, log)

        assert args.from_date is not None and args.to_date is not None

        username, password, qth = resolve_credentials(args)

        session = make_session()

        log(f"[1/4] Downloading ADIF ({args.from_date} .. {args.to_date})")
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

        log("[2/4] Parsing ADIF ...")
        qsos = parse_adif(adif_text)
        log(f"  records parsed: {len(qsos)}")

        if not qsos:
            log("Nothing to download.")
            return 0

        by_name, collisions = build_by_name(qsos, username)
        total = len(by_name)
        log(f"  unique files: {total}")
        if collisions:
            log(
                f"  WARNING: {len(collisions)} filename collision(s) "
                "(same call/date/band/mode/minute); keeping one card per name"
            )
            for name in sorted(collisions):
                log(f"    collision: {name}")

        if args.force:
            log(f"[3/4] Force mode: skipping local scan; re-downloading {total} card(s)")
            skipped = corrupt = missing = 0
            to_fetch = list(by_name.items())
        else:
            log(f"[3/4] Scanning local folder ({dest.name}/) ...")
            log(f"  validating images (JPEG/PNG, {args.workers} processes) ...")
            scan_bar = ProgressBar(total)
            (
                skipped,
                corrupt,
                missing,
                to_fetch,
                _corrupt_names,
                missing_names,
            ) = scan_local_qsos(
                by_name,
                dest,
                args.workers,
                progress=lambda d, t, s: scan_bar.update(d, s),
            )
            scan_bar.finish(f"ok={skipped} corrupt={corrupt} missing={missing}")
            if args.only_missing:
                missing_set = set(missing_names)
                to_fetch = [(n, q) for (n, q) in to_fetch if n in missing_set]
            log(
                f"  summary — valid: {skipped} | corrupt: {corrupt} | "
                f"missing: {missing} | to download: {len(to_fetch)}"
            )

        if args.dry_run:
            log("[dry-run] Nothing will be downloaded.")
            for name, _qso in to_fetch:
                log(f"  pending: {name}")
            log(f"[dry-run] pending={len(to_fetch)} total_unique={total}")
            return 0

        pending = len(to_fetch)
        downloaded = 0
        failed_items: list[tuple[str, QSO, str]] = []

        if pending == 0:
            log("[4/4] Nothing pending to download.")
            log(
                f"Done. downloaded=0 skipped={skipped} corrupt={corrupt} "
                f"failed=0 total_unique={total}"
            )
            return 0

        log(f"[4/4] Downloading pending cards ({pending}) ...")
        dl_bar = ProgressBar(pending)

        for i, (name, qso) in enumerate(to_fetch, start=1):
            left = pending - i
            eta = format_eta(left, args.delay) if left else "0s"
            dl_bar.update(i - 1, f"ETA {eta} | {name}")
            ok, msg = _download_one(
                session, username, password, qso, dest / name, log, args.retries
            )
            if ok:
                downloaded += 1
            else:
                failed_items.append((name, qso, msg))
                dl_bar.note(f"FAILED {name}: {msg}")
            dl_bar.update(
                i,
                f"ok={downloaded} fail={len(failed_items)} "
                f"ETA {format_eta(pending - i, args.delay)} | {name}",
            )
            if i < pending:
                time.sleep(args.delay)
        dl_bar.finish(f"ok={downloaded} fail={len(failed_items)}")

        if failed_items:
            log(f"Retrying {len(failed_items)} failed download(s) ...")
            retry_bar = ProgressBar(len(failed_items))
            still_failed: list[tuple[str, QSO, str]] = []
            for i, (name, qso, _msg) in enumerate(failed_items, start=1):
                retry_bar.update(i - 1, f"retry | {name}")
                ok, msg = _download_one(
                    session, username, password, qso, dest / name, log, args.retries
                )
                if ok:
                    downloaded += 1
                else:
                    still_failed.append((name, qso, msg))
                    retry_bar.note(f"FAILED (retry) {name}: {msg}")
                retry_bar.update(
                    i, f"ok={downloaded} fail={len(still_failed)} | {name}"
                )
                if i < len(failed_items):
                    time.sleep(args.delay)
            retry_bar.finish(f"ok={downloaded} fail={len(still_failed)}")
            failed_items = still_failed

        failed = len(failed_items)
        _write_failures(dest, failed_items, log)
        log(
            f"Done. downloaded={downloaded} skipped={skipped} "
            f"corrupt_found={corrupt} failed={failed} total_unique={total}"
        )
        return 1 if failed else 0
    finally:
        log.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())

#!/usr/bin/env python3
"""Download eQSL.cc QSL card images for a date range.

Uses the official DownloadInBox + GeteQSL API. Skips files that already
exist in the project root. Rate-limits GeteQSL to under 6 requests/minute.
Accepts JPEG or PNG payloads (GeteQSL often serves .PNG).
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests

BASE = "https://www.eqsl.cc"
DOWNLOAD_INBOX = f"{BASE}/qslcard/DownloadInBox.cfm"
GET_EQSL = f"{BASE}/qslcard/GeteQSL.cfm"

# Official limit is slower than 6/min; 11s ≈ 5.4/min.
GET_DELAY_SEC = 11
THROTTLE_WAIT_SEC = 20
DEFAULT_SCAN_WORKERS = min(8, (os.cpu_count() or 4))

OUT_DIR = Path(__file__).resolve().parent

ADIF_TAG_RE = re.compile(
    r"<([A-Z0-9_]+)(?::(\d+)(?::[A-Z])?)?>([^<]*)",
    re.IGNORECASE,
)
IMG_SRC_RE = re.compile(
    r'<img[^>]+src=["\']([^"\']*CFFileServlet/_cf_image/[^"\']+)["\']',
    re.IGNORECASE,
)
HREF_ADI_RE = re.compile(
    r'href=["\']([^"\']+\.(?:adi|txt))["\']',
    re.IGNORECASE,
)
# MYCALL-YYYYMMDD_HHMM-THEIRCALL-band-MODE.jpg
LOCAL_JPG_DATE_RE = re.compile(
    r"^[^-]+-(\d{8})_\d{4}-.+\.jpg$",
    re.IGNORECASE,
)


@dataclass
class QSO:
    call: str
    qso_date: str  # YYYYMMDD
    time_on: str  # HHMM or HHMMSS
    band: str
    mode: str
    submode: str = ""

    @property
    def time_hhmm(self) -> str:
        t = self.time_on.strip()
        if len(t) >= 4:
            return t[:4]
        return t.zfill(4)

    def get_mode(self) -> str:
        """Mode for GeteQSL: major mode for JT65/JT9 submodes."""
        sub = (self.submode or "").upper()
        mode = (self.mode or "").upper()
        if sub.startswith("JT65") or mode.startswith("JT65"):
            return "JT65"
        if sub.startswith("JT9") or mode.startswith("JT9"):
            return "JT9"
        return self.mode or self.submode

    def filename(self, mycall: str) -> str:
        my = sanitize_call(mycall)
        their = sanitize_call(self.call)
        band = self.band.lower()
        mode = self.get_mode()
        name = f"{my}-{self.qso_date}_{self.time_hhmm}-{their}-{band}-{mode}.jpg"
        return name.replace("\\", "_").replace("/", "_")


def sanitize_call(call: str) -> str:
    return call.upper().replace("\\", "_").replace("/", "_")


def _is_valid_jpeg_bytes(data: bytes) -> bool:
    """Structural JPEG check: SOI, parse markers, require EOI (no Pillow).

    Trailing bytes after EOI are allowed — some encoders/servers pad the file.
    """
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return False

    i = 2
    n = len(data)
    saw_sos = False

    while i < n:
        # Skip fill bytes 0xFF
        if data[i] != 0xFF:
            return False
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            return False
        marker = data[i]
        i += 1

        # EOI — accept even if a few trailing bytes follow
        if marker == 0xD9:
            return saw_sos

        # Standalone markers without length
        if marker in (0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7):
            continue
        # Unexpected SOI mid-stream
        if marker == 0xD8:
            return False

        if i + 2 > n:
            return False
        seglen = struct.unpack(">H", data[i : i + 2])[0]
        if seglen < 2 or i + seglen > n:
            return False

        # SOS: entropy-coded data until next marker (FF not followed by 00/RST)
        if marker == 0xDA:
            saw_sos = True
            i += seglen
            while i < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                # Count FFs; stuffed FF00 or RST continue scan
                j = i
                while j < n and data[j] == 0xFF:
                    j += 1
                if j >= n:
                    return False
                nxt = data[j]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    i = j + 1
                    continue
                # Real marker (often EOI / DNL); reprocess from this FF
                i = j - 1
                # rewind to first FF of the run
                while i > 0 and data[i - 1] == 0xFF:
                    i -= 1
                break
            else:
                return False
            continue

        i += seglen

    return False


def _is_valid_png_bytes(data: bytes) -> bool:
    """Lightweight PNG check: signature + IEND chunk present."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    # IEND is the last chunk: length=0, type=IEND, + CRC
    return b"IEND" in data[-16:]


def detect_image_kind(data: bytes) -> str:
    """Return jpeg|png|gif|webp|html|empty|unknown for diagnostics."""
    if not data:
        return "empty"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data.lstrip()[:64].lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or head.startswith(b"<"):
        return "html"
    return "unknown"


def _is_valid_image_bytes(data: bytes) -> bool:
    """eQSL GeteQSL often serves PNG (URL ends in .PNG); also accept JPEG."""
    kind = detect_image_kind(data)
    if kind == "jpeg":
        return _is_valid_jpeg_bytes(data)
    if kind == "png":
        return _is_valid_png_bytes(data)
    return False


def is_valid_jpeg(path: Path) -> bool:
    """Valid eQSL card graphic (JPEG or PNG). Name kept for call-site stability."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if not data:
        return False
    return _is_valid_image_bytes(data)


def classify_local_file(name: str, qso: QSO) -> tuple[str, str, QSO]:
    """Return (status, name, qso) where status is ok|corrupt|missing."""
    path = OUT_DIR / name
    if path.is_file() and path.stat().st_size > 0:
        if is_valid_jpeg(path):
            return "ok", name, qso
        return "corrupt", name, qso
    return "missing", name, qso


def parse_adif(text: str) -> list[QSO]:
    """Minimal ADIF body parser into QSO records."""
    upper = text.upper()
    eoh = upper.find("<EOH>")
    body = text[eoh + 5 :] if eoh >= 0 else text

    records: list[QSO] = []
    current: dict[str, str] = {}

    for match in ADIF_TAG_RE.finditer(body):
        tag = match.group(1).upper()
        length = int(match.group(2) or 0)
        raw = match.group(3)
        value = raw[:length] if length else raw.strip()

        if tag == "EOR":
            if all(k in current for k in ("CALL", "QSO_DATE", "TIME_ON", "BAND", "MODE")):
                records.append(
                    QSO(
                        call=current["CALL"].strip(),
                        qso_date=current["QSO_DATE"].strip(),
                        time_on=current["TIME_ON"].strip(),
                        band=current["BAND"].strip(),
                        mode=current["MODE"].strip(),
                        submode=current.get("SUBMODE", "").strip(),
                    )
                )
            current = {}
        else:
            current[tag] = value

    return records


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
        "--validate-only",
        action="store_true",
        help="Validate local card images only (no login, no download). Dates optional.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_SCAN_WORKERS,
        metavar="N",
        help=f"Processes for local JPEG validation (default: {DEFAULT_SCAN_WORKERS})",
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


def list_local_jpgs(
    d_from: Optional[date] = None,
    d_to: Optional[date] = None,
) -> list[Path]:
    """List *.jpg in OUT_DIR; optional filter by QSO date embedded in filename."""
    files = sorted(OUT_DIR.glob("*.jpg"))
    if d_from is None and d_to is None:
        return files

    selected: list[Path] = []
    for path in files:
        m = LOCAL_JPG_DATE_RE.match(path.name)
        if not m:
            continue
        qdate = datetime.strptime(m.group(1), "%Y%m%d").date()
        if d_from is not None and qdate < d_from:
            continue
        if d_to is not None and qdate > d_to:
            continue
        selected.append(path)
    return selected


def validate_local_paths(
    paths: list[Path],
    workers: int,
) -> tuple[int, int, list[str]]:
    """Threaded JPEG check of existing files. Returns ok, corrupt, corrupt_names."""
    total = len(paths)
    if total == 0:
        print("  no hay archivos .jpg para validar")
        return 0, 0, []

    print(f"  validando imagen (JPEG/PNG, {workers} procesos) ...")
    bar = ProgressBar(total)
    ok = 0
    corrupt = 0
    corrupt_names: list[str] = []
    done = 0

    # ProcessPoolExecutor: real multi-core (avoids the CPython GIL).
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(is_valid_jpeg, path): path for path in paths}
        for fut in as_completed(futures):
            path = futures[fut]
            if fut.result():
                ok += 1
            else:
                corrupt += 1
                corrupt_names.append(path.name)
            done += 1
            if done == total or done % 25 == 0:
                bar.update(done, f"ok={ok} corruptos={corrupt}")

    bar.finish(f"ok={ok} corruptos={corrupt}")
    return ok, corrupt, corrupt_names


def run_validate_only(args: argparse.Namespace) -> int:
    """Local-only validation: no credentials, no ADIF, no download."""
    print("[validate-only] Escaneando JPGs locales (sin login / sin descarga)")
    if args.from_date and args.to_date:
        print(f"  filtro fechas QSO: {args.from_date} .. {args.to_date}")
    else:
        print("  sin filtro de fechas (todos los .jpg del directorio)")

    paths = list_local_jpgs(args.from_date, args.to_date)
    print(f"  archivos a chequear: {len(paths)}")
    ok, corrupt, corrupt_names = validate_local_paths(paths, args.workers)

    if corrupt_names:
        print(f"  corruptos ({len(corrupt_names)}):")
        for name in sorted(corrupt_names):
            print(f"    {name}")

    print(f"Done. validate_only ok={ok} corrupt={corrupt} total={len(paths)}")
    return 1 if corrupt else 0


def eqsl_date(d: date) -> str:
    """MM/DD/YYYY for LimitDateLo / LimitDateHi."""
    return d.strftime("%m/%d/%Y")


def format_eta(remaining: int, delay_sec: int = GET_DELAY_SEC) -> str:
    """Rough ETA for remaining downloads given fixed delay between them."""
    if remaining <= 0:
        return "0s"
    secs = remaining * delay_sec
    if secs < 60:
        return f"{secs}s"
    mins, sec = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {sec:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins:02d}m"


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


def fetch_inbox_adif(
    session: requests.Session,
    username: str,
    password: str,
    qth: str,
    d_from: date,
    d_to: date,
) -> str:
    params: dict[str, str] = {
        "UserName": username,
        "Password": password,
        "LimitDateLo": eqsl_date(d_from),
        "LimitDateHi": eqsl_date(d_to),
        # Omit Archive → Inbox + Archive
    }
    if qth:
        params["QTHNickname"] = qth

    print(f"[1/4] Descargando ADIF ({d_from} .. {d_to})")
    bar = ProgressBar(3)
    bar.update(0, "solicitando lista")
    r = session.get(DOWNLOAD_INBOX, params=params, timeout=120)
    r.raise_for_status()
    html = r.text

    if "Your ADIF log file has been built" not in html:
        bar.finish("error")
        # Show a short snippet to help diagnose bad login / empty / errors
        snippet = re.sub(r"\s+", " ", html)[:400]
        sys.exit(f"DownloadInBox did not succeed. Response snippet:\n{snippet}")

    bar.update(1, "localizando enlace")
    m = HREF_ADI_RE.search(html)
    if not m:
        bar.finish("error")
        sys.exit("Could not find ADIF download link in DownloadInBox response.")

    adif_url = urljoin(r.url, m.group(1))
    bar.update(2, "bajando archivo")
    ar = session.get(adif_url, timeout=120)
    ar.raise_for_status()
    size_kb = max(1, len(ar.content) // 1024)
    bar.finish(f"{size_kb} KB")
    return ar.text

def extract_image_url(html: str, page_url: str) -> Optional[str]:
    if "Error:" in html:
        return None
    m = IMG_SRC_RE.search(html)
    if not m:
        return None
    return urljoin(page_url, m.group(1))


def download_card(
    session: requests.Session,
    username: str,
    password: str,
    qso: QSO,
    dest: Path,
) -> tuple[bool, str]:
    """Fetch one card graphic. Returns (ok, message)."""
    params = {
        "Username": username,
        "Password": password,
        "CallsignFrom": qso.call,
        "QSOYear": qso.qso_date[0:4],
        "QSOMonth": qso.qso_date[4:6],
        "QSODay": qso.qso_date[6:8],
        "QSOHour": qso.time_hhmm[0:2],
        "QSOMinute": qso.time_hhmm[2:4],
        "QSOBand": qso.band,
        "QSOMode": qso.get_mode(),
    }

    for attempt in range(2):
        r = session.get(GET_EQSL, params=params, timeout=60)
        r.raise_for_status()
        html = r.text

        if "Throttling invoked" in html:
            print("  Throttling detected; waiting "
                  f"{THROTTLE_WAIT_SEC}s and retrying...")
            time.sleep(THROTTLE_WAIT_SEC)
            continue

        if "Error:" in html:
            err = re.search(r"Error:.*", html)
            msg = err.group(0).strip() if err else "Error in GeteQSL response"
            return False, msg

        img_url = extract_image_url(html, r.url)
        if not img_url:
            if attempt == 0:
                time.sleep(THROTTLE_WAIT_SEC)
                continue
            return False, "No image URL in GeteQSL response"

        ir = session.get(img_url, timeout=60)
        ir.raise_for_status()
        if not ir.content or len(ir.content) < 100:
            return False, "Image download too small / empty"
        dest.write_bytes(ir.content)
        return True, "ok"

    return False, "Failed after throttle retry"


def scan_local_qsos(
    by_name: dict[str, QSO],
    workers: int,
) -> tuple[int, int, int, list[tuple[str, QSO]], list[str], list[str]]:
    """Threaded local scan. Returns ok, corrupt, missing, to_fetch, corrupt_names, missing_names."""
    total = len(by_name)
    skipped = 0
    corrupt = 0
    missing = 0
    to_fetch: list[tuple[str, QSO]] = []
    corrupt_names: list[str] = []
    missing_names: list[str] = []

    print(f"[3/4] Escaneando directorio local ({OUT_DIR.name}/) ...")
    print(f"  validando imagen (JPEG/PNG, {workers} procesos) ...")
    bar = ProgressBar(total)
    done = 0

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(classify_local_file, name, qso)
            for name, qso in by_name.items()
        ]
        for fut in as_completed(futures):
            status, name, qso = fut.result()
            if status == "ok":
                skipped += 1
            elif status == "corrupt":
                corrupt += 1
                corrupt_names.append(name)
                to_fetch.append((name, qso))
            else:
                missing += 1
                missing_names.append(name)
                to_fetch.append((name, qso))
            done += 1
            if done == total or done % 25 == 0:
                bar.update(
                    done,
                    f"ok={skipped} corruptos={corrupt} faltan={missing}",
                )

    bar.finish(f"ok={skipped} corruptos={corrupt} faltan={missing}")
    print(
        f"  resumen — válidos: {skipped} | corruptos: {corrupt} | "
        f"faltan: {missing} | a descargar: {len(to_fetch)}"
    )
    return skipped, corrupt, missing, to_fetch, corrupt_names, missing_names


def main() -> int:
    args = parse_args()

    if args.validate_only:
        return run_validate_only(args)

    # Download mode requires dates (enforced in parse_args) and credentials.
    assert args.from_date is not None and args.to_date is not None

    username, password, qth = prompt_credentials()

    session = requests.Session()
    session.headers.update(
        {"User-Agent": "fer-eqsl-downloader/1.0 (personal use; rate-limited)"}
    )

    try:
        adif_text = fetch_inbox_adif(
            session, username, password, qth, args.from_date, args.to_date
        )
    except requests.RequestException as exc:
        print(f"Network error fetching ADIF: {exc}", file=sys.stderr)
        return 1

    print("[2/4] Procesando ADIF ...")
    qsos = parse_adif(adif_text)
    print(f"  registros parseados: {len(qsos)}")

    if not qsos:
        print("Nothing to download.")
        return 0

    # Deduplicate by target filename (same QSO can appear once)
    by_name: dict[str, QSO] = {}
    for qso in qsos:
        by_name[qso.filename(username)] = qso
    total = len(by_name)
    print(f"  únicos por archivo: {total}")

    (
        skipped,
        corrupt,
        _missing,
        to_fetch,
        _corrupt_names,
        _missing_names,
    ) = scan_local_qsos(by_name, args.workers)

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
    bar = ProgressBar(pending)

    for i, (name, qso) in enumerate(to_fetch, start=1):
        path = OUT_DIR / name
        left = pending - i
        eta = format_eta(left) if left else "0s"
        bar.update(i - 1, f"ETA {eta} | {name}")
        try:
            ok, msg = download_card(session, username, password, qso, path)
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
            bar.note(f"FAILED {name}: {msg}")

        bar.update(
            i,
            f"ok={downloaded} fail={failed} ETA {format_eta(pending - i)} | {name}",
        )

        # Sleep between real GeteQSL calls only (not after the last one)
        if i < pending:
            time.sleep(GET_DELAY_SEC)

    bar.finish(f"ok={downloaded} fail={failed}")
    print(
        f"Done. downloaded={downloaded} skipped={skipped} "
        f"corrupt_found={corrupt} failed={failed} total_unique={total}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

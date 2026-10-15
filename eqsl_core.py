#!/usr/bin/env python3
"""Reusable core logic for the eQSL.cc card downloader.

Presentation-free: no argparse, no input()/getpass, no sys.exit. All terminal
or GUI output is emitted through optional callbacks so both the CLI
(``eqsl_download.py``) and the GUI (``eqsl_gui.py``) can share this module.
"""

from __future__ import annotations

import multiprocessing
import re
import struct
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

import requests

BASE = "https://www.eqsl.cc"
DOWNLOAD_INBOX = f"{BASE}/qslcard/DownloadInBox.cfm"
GET_EQSL = f"{BASE}/qslcard/GeteQSL.cfm"

# Official limit is slower than 6/min; 11s ≈ 5.4/min.
GET_DELAY_SEC = 11
THROTTLE_WAIT_SEC = 20
DEFAULT_SCAN_WORKERS = 8

USER_AGENT = "fer-eqsl-downloader/1.0 (personal use; rate-limited)"

# ``spawn`` is the portable choice and avoids forking a process that already
# holds threads (the GUI runs its worker in a thread). Required for frozen
# builds on Windows/macOS and safe everywhere.
MP_CONTEXT = multiprocessing.get_context("spawn")

# Callback signatures (all optional).
LogFn = Callable[[str], None]
# progress(done, total, text); total == 0 means "unknown / indeterminate".
ProgressFn = Callable[[int, int, str], None]

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


class EQSLError(RuntimeError):
    """Raised for expected, user-facing failures (bad login, HTML changes...)."""


def _log(log: Optional[LogFn], message: str) -> None:
    if log is not None:
        log(message)


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


def classify_local_file(name: str, qso: QSO, dest_dir: Path) -> tuple[str, str, QSO]:
    """Return (status, name, qso) where status is ok|corrupt|missing."""
    path = dest_dir / name
    try:
        if path.is_file() and path.stat().st_size > 0:
            if is_valid_jpeg(path):
                return "ok", name, qso
            return "corrupt", name, qso
    except OSError:
        return "missing", name, qso
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


def list_local_jpgs(
    dest_dir: Path,
    d_from: Optional[date] = None,
    d_to: Optional[date] = None,
) -> list[Path]:
    """List *.jpg in dest_dir; optional filter by QSO date embedded in filename."""
    files = sorted(dest_dir.glob("*.jpg"))
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
    progress: Optional[ProgressFn] = None,
) -> tuple[int, int, list[str]]:
    """Multiprocess image check of existing files. Returns ok, corrupt, names."""
    total = len(paths)
    if total == 0:
        return 0, 0, []

    ok = 0
    corrupt = 0
    corrupt_names: list[str] = []
    done = 0

    with ProcessPoolExecutor(max_workers=workers, mp_context=MP_CONTEXT) as pool:
        futures = {pool.submit(is_valid_jpeg, path): path for path in paths}
        for fut in as_completed(futures):
            path = futures[fut]
            if fut.result():
                ok += 1
            else:
                corrupt += 1
                corrupt_names.append(path.name)
            done += 1
            if progress and (done == total or done % 25 == 0):
                progress(done, total, f"ok={ok} corruptos={corrupt}")

    if progress:
        progress(total, total, f"ok={ok} corruptos={corrupt}")
    return ok, corrupt, corrupt_names


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


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_inbox_adif(
    session: requests.Session,
    username: str,
    password: str,
    qth: str,
    d_from: date,
    d_to: date,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
) -> str:
    """Download the Inbox+Archive ADIF for the date range. Returns ADIF text."""
    params: dict[str, str] = {
        "UserName": username,
        "Password": password,
        "LimitDateLo": eqsl_date(d_from),
        "LimitDateHi": eqsl_date(d_to),
        # Omit Archive → Inbox + Archive
    }
    if qth:
        params["QTHNickname"] = qth

    _log(log, f"Descargando ADIF ({d_from} .. {d_to})")
    if progress:
        progress(0, 3, "solicitando lista")
    r = session.get(DOWNLOAD_INBOX, params=params, timeout=120)
    r.raise_for_status()
    html = r.text

    if "Your ADIF log file has been built" not in html:
        snippet = re.sub(r"\s+", " ", html)[:400]
        raise EQSLError(
            "DownloadInBox did not succeed. Response snippet:\n" + snippet
        )

    if progress:
        progress(1, 3, "localizando enlace")
    m = HREF_ADI_RE.search(html)
    if not m:
        raise EQSLError("Could not find ADIF download link in DownloadInBox response.")

    adif_url = urljoin(r.url, m.group(1))
    if progress:
        progress(2, 3, "bajando archivo")
    ar = session.get(adif_url, timeout=120)
    ar.raise_for_status()
    size_kb = max(1, len(ar.content) // 1024)
    if progress:
        progress(3, 3, f"{size_kb} KB")
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
    log: Optional[LogFn] = None,
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
            _log(log, f"Throttling detected; waiting {THROTTLE_WAIT_SEC}s and retrying...")
            import time as _time

            _time.sleep(THROTTLE_WAIT_SEC)
            continue

        if "Error:" in html:
            err = re.search(r"Error:.*", html)
            msg = err.group(0).strip() if err else "Error in GeteQSL response"
            return False, msg

        img_url = extract_image_url(html, r.url)
        if not img_url:
            if attempt == 0:
                import time as _time

                _time.sleep(THROTTLE_WAIT_SEC)
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
    dest_dir: Path,
    workers: int,
    progress: Optional[ProgressFn] = None,
) -> tuple[int, int, int, list[tuple[str, QSO]], list[str], list[str]]:
    """Multiprocess local scan. Returns ok, corrupt, missing, to_fetch,

    corrupt_names, missing_names.
    """
    total = len(by_name)
    skipped = 0
    corrupt = 0
    missing = 0
    to_fetch: list[tuple[str, QSO]] = []
    corrupt_names: list[str] = []
    missing_names: list[str] = []
    done = 0

    with ProcessPoolExecutor(max_workers=workers, mp_context=MP_CONTEXT) as pool:
        futures = [
            pool.submit(classify_local_file, name, qso, dest_dir)
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
            if progress and (done == total or done % 25 == 0):
                progress(done, total, f"ok={skipped} corruptos={corrupt} faltan={missing}")

    if progress:
        progress(total, total, f"ok={skipped} corruptos={corrupt} faltan={missing}")
    return skipped, corrupt, missing, to_fetch, corrupt_names, missing_names

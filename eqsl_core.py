#!/usr/bin/env python3
"""Reusable core logic for the eQSL.cc card downloader.

Presentation-free: no argparse, no input()/getpass, no sys.exit. All terminal
or GUI output is emitted through optional callbacks so both the CLI
(``eqsl_download.py``) and the GUI (``eqsl_gui.py``) can share this module.
"""

from __future__ import annotations

import multiprocessing
import os
import re
import struct
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.eqsl.cc"
DOWNLOAD_INBOX = f"{BASE}/qslcard/DownloadInBox.cfm"
GET_EQSL = f"{BASE}/qslcard/GeteQSL.cfm"

# Official limit is slower than 6/min; 11s ≈ 5.4/min.
GET_DELAY_SEC = 11
THROTTLE_WAIT_SEC = 20
DEFAULT_SCAN_WORKERS = max(1, min(8, os.cpu_count() or 4))

# Retry policy for transient network failures (all calls are idempotent GETs).
RETRY_TOTAL = 4
RETRY_BACKOFF_FACTOR = 0.5
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)

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
# MYCALL-YYYYMMDD_HHMM-THEIRCALL-band-MODE.<jpg|png>
IMAGE_EXTENSIONS = (".jpg", ".png")
IMAGE_EXT_BY_KIND = {"jpeg": ".jpg", "png": ".png"}

LOCAL_IMAGE_DATE_RE = re.compile(
    r"^[^-]+-(\d{8})_\d{4}-.+\.(?:jpe?g|png)$",
    re.IGNORECASE,
)
# Backwards-compatible alias for older imports.
LOCAL_JPG_DATE_RE = LOCAL_IMAGE_DATE_RE

# Phrases that indicate eQSL rejected the credentials (login page / error).
AUTH_ERROR_RE = re.compile(
    r"(invalid|incorrect|wrong|bad)\s+(user\s*name|username|password|login|callsign)"
    r"|login\s+(failed|error|invalid)"
    r"|not\s+(a\s+)?valid\s+(user\s*name|username|password|login)"
    r"|must\s+(be\s+)?log(?:ged)?\s*in"
    r"|sorry[, ]",
    re.IGNORECASE,
)

# Phrases that indicate the range simply has no matching QSOs.
NO_QSO_RE = re.compile(
    r"no\s+(?:matching\s+)?(?:qso|qsos|records?|matches|log\s*entries)"
    r"|nothing\s+(?:was\s+)?found"
    r"|no\s+(?:data|results?)\s+(?:found|available)",
    re.IGNORECASE,
)

_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")


class EQSLError(RuntimeError):
    """Raised for expected, user-facing failures (bad login, HTML changes...)."""


class EQSLAuthError(EQSLError):
    """Raised when eQSL rejects the supplied credentials."""


class EQSLNoQSOError(EQSLError):
    """Raised when the selected range contains no matching QSOs."""


def _visible_text(markup: str) -> str:
    """Strip tags/scripts and collapse whitespace for a readable snippet."""
    text = _SCRIPT_STYLE_RE.sub(" ", markup)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _log(log: Optional[LogFn], message: str) -> None:
    if log is not None:
        log(message)


def get_version() -> str:
    """Application version, read from the bundled VERSION file when present."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    try:
        return (base / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


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

    def base_name(self, mycall: str) -> str:
        """Canonical QSO identity: filename without extension."""
        my = sanitize_call(mycall)
        their = sanitize_call(self.call)
        band = self.band.lower()
        mode = self.get_mode()
        name = f"{my}-{self.qso_date}_{self.time_hhmm}-{their}-{band}-{mode}"
        return name.replace("\\", "_").replace("/", "_")

    def filename(self, mycall: str) -> str:
        """Legacy helper; the real extension depends on the downloaded bytes."""
        return self.base_name(mycall) + ".jpg"


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


def is_valid_image(path: Path) -> bool:
    """Valid eQSL card graphic (JPEG or PNG), detected by content."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if not data:
        return False
    return _is_valid_image_bytes(data)


# Backwards-compatible alias (the old name was misleading).
is_valid_jpeg = is_valid_image


def image_extension(kind: str) -> Optional[str]:
    """Map a detected content kind to the canonical file extension."""
    return IMAGE_EXT_BY_KIND.get(kind)


def _strip_image_extension(name: str) -> str:
    """Accept either a base name or a legacy filename and return the base."""
    lowered = name.lower()
    for ext in IMAGE_EXTENSIONS:
        if lowered.endswith(ext):
            return name[: -len(ext)]
    return name


def existing_image_paths(dest_dir: Path, base_name: str) -> list[Path]:
    """Return all existing files for a base name across supported extensions."""
    base = _strip_image_extension(base_name)
    paths: list[Path] = []
    for ext in IMAGE_EXTENSIONS:
        candidate = dest_dir / (base + ext)
        try:
            if candidate.is_file():
                paths.append(candidate)
        except OSError:
            continue
    return paths


def classify_local_file(name: str, qso: QSO, dest_dir: Path) -> tuple[str, str, QSO]:
    """Return (status, base_name, qso) where status is ok|corrupt|missing.

    A QSO is satisfied if any supported extension exists and is a valid image.
    """
    base = _strip_image_extension(name)
    candidates = existing_image_paths(dest_dir, base)
    if not candidates:
        return "missing", base, qso
    for path in candidates:
        try:
            if path.stat().st_size > 0 and is_valid_image(path):
                return "ok", base, qso
        except OSError:
            continue
    return "corrupt", base, qso


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


def build_by_name(qsos: list[QSO], mycall: str) -> tuple[dict[str, QSO], list[str]]:
    """Map canonical base name (no extension) -> QSO, reporting collisions.

    Names carry only minute precision, so two QSOs sharing call, date, band,
    mode and minute map to the same base name. The last record wins (legacy
    behaviour) but every collision is returned so callers can warn the user
    instead of silently dropping cards.
    """
    by_name: dict[str, QSO] = {}
    collisions: list[str] = []
    for qso in qsos:
        name = qso.base_name(mycall)
        if name in by_name:
            collisions.append(name)
        by_name[name] = qso
    return by_name, collisions


def list_local_images(
    dest_dir: Path,
    d_from: Optional[date] = None,
    d_to: Optional[date] = None,
) -> list[Path]:
    """List supported image files; optional filter by QSO date in the name."""
    files = sorted(
        path for ext in IMAGE_EXTENSIONS for path in dest_dir.glob("*" + ext)
    )
    if d_from is None and d_to is None:
        return files

    selected: list[Path] = []
    for path in files:
        m = LOCAL_IMAGE_DATE_RE.match(path.name)
        if not m:
            continue
        try:
            qdate = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            # Skip files whose embedded date is not a real calendar date.
            continue
        if d_from is not None and qdate < d_from:
            continue
        if d_to is not None and qdate > d_to:
            continue
        selected.append(path)
    return selected


# Backwards-compatible alias.
list_local_jpgs = list_local_images


def normalize_extensions(
    dest_dir: Path,
    dry_run: bool = False,
    log: Optional[LogFn] = None,
) -> dict[str, int]:
    """Rename card files whose extension does not match their real content.

    Only files following the card naming pattern are touched, and an existing
    target is never overwritten (reported as a conflict).
    """
    stats = {"checked": 0, "renamed": 0, "ok": 0, "conflicts": 0, "unsupported": 0}
    paths = sorted(
        path for ext in IMAGE_EXTENSIONS for path in dest_dir.glob("*" + ext)
    )
    for path in paths:
        if not LOCAL_IMAGE_DATE_RE.match(path.name):
            continue
        stats["checked"] += 1
        try:
            data = path.read_bytes()
        except OSError:
            continue

        kind = detect_image_kind(data)
        want = image_extension(kind)
        if want is None:
            stats["unsupported"] += 1
            continue
        if path.suffix.lower() == want:
            stats["ok"] += 1
            continue
        if not _is_valid_image_bytes(data):
            continue

        base = path.name[: -len(path.suffix)]
        target = dest_dir / (base + want)
        if target.exists():
            stats["conflicts"] += 1
            _log(log, f"  conflict: {path.name} (target {target.name} exists)")
            continue
        if dry_run:
            _log(log, f"  would rename {path.name} -> {target.name}")
            continue
        os.replace(path, target)
        _log(log, f"  renamed {path.name} -> {target.name}")
        stats["renamed"] += 1
    return stats


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
        futures = {pool.submit(is_valid_image, path): path for path in paths}
        for fut in as_completed(futures):
            path = futures[fut]
            if fut.result():
                ok += 1
            else:
                corrupt += 1
                corrupt_names.append(path.name)
            done += 1
            if progress and (done == total or done % 25 == 0):
                progress(done, total, f"ok={ok} corrupt={corrupt}")

    if progress:
        progress(total, total, f"ok={ok} corrupt={corrupt}")
    return ok, corrupt, corrupt_names


def eqsl_date(d: date) -> str:
    """MM/DD/YYYY for LimitDateLo / LimitDateHi."""
    return d.strftime("%m/%d/%Y")


def format_eta(remaining: int, delay_sec: float = GET_DELAY_SEC) -> str:
    """Rough ETA for remaining downloads given a fixed delay between them."""
    if remaining <= 0:
        return "0s"
    secs = int(remaining * delay_sec)
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

    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        status=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
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

    _log(log, f"Downloading ADIF ({d_from} .. {d_to})")
    if progress:
        progress(0, 3, "requesting list")
    r = session.get(DOWNLOAD_INBOX, params=params, timeout=120)
    r.raise_for_status()
    html = r.text

    if "Your ADIF log file has been built" not in html:
        text = _visible_text(html)
        lowered = text.lower()
        if AUTH_ERROR_RE.search(text) or ("username" in lowered and "password" in lowered):
            raise EQSLAuthError(
                "Login failed: eQSL rejected your callsign or password. "
                "Check your credentials (and the QTH nickname, if set) and try again."
            )
        if NO_QSO_RE.search(text):
            raise EQSLNoQSOError(
                "No QSOs were found for the selected date range (nothing to download)."
            )
        snippet = text[:200] or "(empty response)"
        raise EQSLError(
            "DownloadInBox did not return an ADIF file. "
            f"Server response: {snippet}"
        )

    if progress:
        progress(1, 3, "locating link")
    m = HREF_ADI_RE.search(html)
    if not m:
        raise EQSLError("Could not find ADIF download link in DownloadInBox response.")

    adif_url = urljoin(r.url, m.group(1))
    if progress:
        progress(2, 3, "downloading file")
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
    dest_dir: Path,
    base_name: str,
    log: Optional[LogFn] = None,
    max_attempts: int = 2,
    throttle_wait: Optional[float] = None,
) -> tuple[bool, str]:
    """Fetch one card graphic and save it with its real extension.

    The bytes are written unchanged; the extension is derived from the detected
    content kind (``.jpg`` for JPEG, ``.png`` for PNG).
    """
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

    wait = THROTTLE_WAIT_SEC if throttle_wait is None else throttle_wait
    attempts = max(1, int(max_attempts))
    for attempt in range(attempts):
        r = session.get(GET_EQSL, params=params, timeout=60)
        r.raise_for_status()
        html = r.text

        if "Throttling invoked" in html:
            _log(log, f"Throttling detected; waiting {wait}s and retrying...")
            import time as _time

            _time.sleep(wait)
            continue

        if "Error:" in html:
            err = re.search(r"Error:.*", html)
            msg = err.group(0).strip() if err else "Error in GeteQSL response"
            return False, msg

        img_url = extract_image_url(html, r.url)
        if not img_url:
            if attempt == 0 and attempts > 1:
                import time as _time

                _time.sleep(wait)
                continue
            return False, "No image URL in GeteQSL response"

        ir = session.get(img_url, timeout=60)
        ir.raise_for_status()
        content = ir.content
        if not content or len(content) < 100:
            return False, "Image download too small / empty"

        kind = detect_image_kind(content)
        extension = image_extension(kind)
        if extension is None:
            return False, f"Unsupported image format ({kind})"
        if not _is_valid_image_bytes(content):
            return False, (
                "Downloaded image failed validation "
                f"(kind={kind}, size={len(content)})"
            )

        dest = dest_dir / (_strip_image_extension(base_name) + extension)
        # Atomic write: never leave a half-written card behind.
        tmp = dest.with_name(dest.name + ".part")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, dest)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return True, "ok"

    return False, f"Failed after {attempts} attempt(s)"


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
                progress(done, total, f"ok={skipped} corrupt={corrupt} missing={missing}")

    if progress:
        progress(total, total, f"ok={skipped} corrupt={corrupt} missing={missing}")
    return skipped, corrupt, missing, to_fetch, corrupt_names, missing_names

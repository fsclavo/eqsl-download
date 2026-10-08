# eQSL card downloader

Personal CLI tool that downloads [eQSL.cc](https://www.eqsl.cc) QSL card images for a QSO date range, using the official **DownloadInBox** and **GeteQSL** endpoints. It skips valid local files, re-downloads corrupt ones, and rate-limits GeteQSL to stay under the site’s ~6 requests/minute limit.

## Requirements

- Python 3.9+ (uses `from __future__ import annotations`, dataclasses, `Path`)
- [`requests`](https://pypi.org/project/requests/)

```bash
pip install requests
```

## Quick start

```bash
# Full calendar year
python3 eqsl_download.py --year 2024

# Custom QSO date range
python3 eqsl_download.py --from 2024-01-01 --to 2024-06-30

# Validate local JPGs only (no login, no download)
python3 eqsl_download.py --validate-only
python3 eqsl_download.py --validate-only --year 2024
```

On download runs you will be prompted for:

| Prompt | Required | Notes |
|--------|----------|--------|
| Callsign | yes | Used as eQSL `UserName` and in output filenames |
| Password | yes | Entered via `getpass` (not echoed) |
| QTH Nickname | no | Passed as `QTHNickname` when set |

Images are written next to the script (project root).

## CLI options

| Option | Description |
|--------|-------------|
| `--year YYYY` | Shortcut for `--from YYYY-01-01 --to YYYY-12-31` |
| `--from YYYY-MM-DD` | Start QSO date (must be used with `--to`) |
| `--to YYYY-MM-DD` | End QSO date |
| `--validate-only` | Structural JPEG check of local `*.jpg` only |
| `--workers N` | Processes for local JPEG validation (default: `min(8, CPU count)`) |

Rules:

- Use either `--year` **or** `--from`/`--to`, not both.
- Download mode requires a date range.
- `--validate-only` may omit dates (scans every `*.jpg` in the directory).

Exit codes: `0` on success; `1` if any download failed, or if `--validate-only` found corrupt files.

## Pipeline (download mode)

```
[1/4] DownloadInBox  →  HTML with ADIF link  →  fetch .adi/.txt
[2/4] Parse ADIF     →  QSO list, dedupe by target filename
[3/4] Local scan     →  ok | corrupt | missing  (multiprocess JPEG check)
[4/4] GeteQSL        →  card HTML → CF image URL → save .jpg  (rate-limited)
```

### 1. Inbox ADIF

`GET https://www.eqsl.cc/qslcard/DownloadInBox.cfm` with:

- `UserName`, `Password`
- `LimitDateLo` / `LimitDateHi` as `MM/DD/YYYY`
- optional `QTHNickname`
- `Archive` omitted → Inbox **and** Archive

On success the HTML contains *“Your ADIF log file has been built”*; the script follows the `.adi`/`.txt` href and downloads that file.

### 2. ADIF parsing

A minimal tag parser extracts records with `CALL`, `QSO_DATE`, `TIME_ON`, `BAND`, `MODE` (and optional `SUBMODE`). JT65/JT9 submodes are normalized to major mode for the GeteQSL `QSOMode` parameter.

### 3. Local classification

For each unique QSO filename:

| Status | Action |
|--------|--------|
| **ok** | Valid JPEG on disk → skip |
| **corrupt** | Present but fails structural check → re-download |
| **missing** | Absent or empty → download |

Validation walks JPEG markers (SOI → SOS → EOI) without Pillow.

### 4. Card download

`GET https://www.eqsl.cc/qslcard/GeteQSL.cfm` with callsign, date/time, band, and mode. The HTML is scraped for a ColdFusion image servlet URL (`CFFileServlet/_cf_image/...`), then that image is saved.

- Delay between GeteQSL calls: **11 s** (~5.4/min; under the official 6/min cap).
- On *“Throttling invoked”*: wait **20 s** and retry (up to 2 attempts).
- Post-download JPEG validation; invalid files are deleted and counted as failures.

## Output filenames

```
MYCALL-YYYYMMDD_HHMM-THEIRCALL-band-MODE.jpg
```

Example: `LU1ABC-20240315_1423-W1AW-20m-FT8.jpg`

Callsigns are uppercased; `/` and `\` become `_`.

## Architecture notes

| Piece | Role |
|-------|------|
| `QSO` dataclass | ADIF fields + `filename()` / `get_mode()` |
| `ProgressBar` | Single-line terminal progress (pip-style) |
| `ProcessPoolExecutor` | Parallel local JPEG checks (avoids GIL) |
| `requests.Session` | Shared cookies/headers; UA `fer-eqsl-downloader/1.0` |
| `OUT_DIR` | Directory containing `eqsl_download.py` |

`--validate-only` reuses the same JPEG checker and optional filename date filter (`MYCALL-YYYYMMDD_HHMM-...jpg`) without touching the network.

## Limitations / caveats

- Credentials are interactive only (not CLI flags or env vars) — good for interactive use, awkward for cron.
- ADIF parser is intentionally minimal; unusual ADIF layouts may be skipped.
- Relies on eQSL HTML phrases and img URL patterns; site markup changes can break parsing.
- Downloads are strictly sequential after the local scan because of rate limits; large backlogs take ~11 s per card.
- UI messages mix Spanish (progress steps) and English (errors / final summary).

## License / use

Intended for personal use against your own eQSL account. Respect eQSL’s rate limits and terms of service.

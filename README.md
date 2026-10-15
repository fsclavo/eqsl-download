# eQSL card downloader / eQSL Gráfico

Personal toolkit that downloads [eQSL.cc](https://www.eqsl.cc) QSL card images
for a QSO date range, using the official **DownloadInBox** and **GeteQSL**
endpoints. It skips valid local files, re-downloads corrupt ones, and
rate-limits GeteQSL to stay under the site's ~6 requests/minute limit.

It ships in three shapes, all backed by the same core:

| Entry point | Description |
|-------------|-------------|
| `eqsl_download.py` | Command-line tool |
| `eqsl_gui.py` | Tkinter desktop app (**eQSL Gráfico**) |
| `Releases/*.AppImage` / `*_Setup.exe` | Prebuilt graphical releases |

## Requirements

- Python 3.9+
- One third-party runtime package: [`requests`](https://pypi.org/project/requests/)
  (`requirements.txt`). Everything else is the standard library.
- Tkinter for the GUI (usually packaged with Python; on Debian/Ubuntu:
  `sudo apt install python3-tk`).

```bash
python3 -m pip install -r requirements.txt
```

## Graphical app

```bash
python3 eqsl_gui.py
```

The window asks for your callsign, password, optional QTH nickname, a date
range (whole year or a custom range), and the destination folder. It shows a
progress bar and a live log, and can run a local **validation** pass without
logging in.

The bundled binary can still act as the CLI, e.g.:

```bash
eqsl-grafico --validate-only --dest ~/tarjetas
```

## Command line

```bash
# Full calendar year
python3 eqsl_download.py --year 2024

# Custom QSO date range
python3 eqsl_download.py --from 2024-01-01 --to 2024-06-30

# Custom destination folder
python3 eqsl_download.py --year 2024 --dest ~/tarjetas

# Validate local JPGs only (no login, no download)
python3 eqsl_download.py --validate-only
python3 eqsl_download.py --validate-only --year 2024 --dest ~/tarjetas
```

On download runs you will be prompted for:

| Prompt | Required | Notes |
|--------|----------|-------|
| Callsign | yes | Used as eQSL `UserName` and in output filenames |
| Password | yes | Entered via `getpass` (not echoed) |
| QTH Nickname | no | Passed as `QTHNickname` when set |

### CLI options

| Option | Description |
|--------|-------------|
| `--year YYYY` | Shortcut for `--from YYYY-01-01 --to YYYY-12-31` |
| `--from YYYY-MM-DD` | Start QSO date (must be used with `--to`) |
| `--to YYYY-MM-DD` | End QSO date |
| `--dest DIR` | Destination folder (default: script directory) |
| `--validate-only` | Structural image check of local `*.jpg` only |
| `--workers N` | Processes for local image validation (default: `8`) |

Rules:

- Use either `--year` **or** `--from`/`--to`, not both.
- Download mode requires a date range.
- `--validate-only` may omit dates (scans every `*.jpg` in the folder).

Exit codes: `0` on success; `1` if any download failed, or if `--validate-only`
found corrupt files.

## Pipeline (download mode)

```
[1/4] DownloadInBox  →  HTML with ADIF link  →  fetch .adi/.txt
[2/4] Parse ADIF     →  QSO list, dedupe by target filename
[3/4] Local scan     →  ok | corrupt | missing  (multiprocess image check)
[4/4] GeteQSL        →  card HTML → CF image URL → save .jpg  (rate-limited)
```

- **Inbox ADIF:** `GET DownloadInBox.cfm` with `UserName`, `Password`,
  `LimitDateLo`/`LimitDateHi` (`MM/DD/YYYY`) and optional `QTHNickname`
  (`Archive` omitted → Inbox **and** Archive). Success page contains
  *"Your ADIF log file has been built"*.
- **ADIF parsing:** minimal tag parser extracting `CALL`, `QSO_DATE`, `TIME_ON`,
  `BAND`, `MODE` (+ optional `SUBMODE`); JT65/JT9 submodes normalize to the
  major mode.
- **Local classification:** `ok` (valid → skip), `corrupt` (re-download),
  `missing` (download). Validation accepts **JPEG** (SOI → SOS → EOI) and
  **PNG** (signature + IEND); eQSL often serves PNG behind a `.jpg` name.
- **Card download:** `GET GeteQSL.cfm`, scrape the ColdFusion image servlet URL,
  save it. Delay **11 s** between calls (~5.4/min); on *"Throttling invoked"*
  wait **20 s** and retry. Post-download files failing validation are deleted.

## Output filenames

```
MYCALL-YYYYMMDD_HHMM-THEIRCALL-band-MODE.jpg
```

Example: `LU1ABC-20240315_1423-W1AW-20m-FT8.jpg`.
Callsigns are uppercased; `/` and `\` become `_`.

## Project structure

```
eqsl_core.py            # shared logic (no UI): ADIF, validation, HTTP, scan
eqsl_download.py        # CLI front-end
eqsl_gui.py             # Tkinter GUI front-end (eQSL Gráfico)
VERSION                 # single source of truth for the version
assets/                 # generated app icons (PNG + ICO)
packaging/
  make_icons.py         # regenerates assets/
  linux/                # AppRun, .desktop, build_appimage.sh
  windows/              # eqsl_win.spec (PyInstaller), installer.iss (Inno Setup)
.github/workflows/      # release pipeline (Linux + Windows on tag push)
Releases/               # built artifacts land here (binaries are git-ignored)
```

`eqsl_core.py` has no `argparse`/`input()`/`sys.exit`; all output goes through
optional `log`/`progress` callbacks, which is what lets the CLI and GUI share
it. Local image checks run through a `ProcessPoolExecutor` using the `spawn`
context (required for frozen builds and safe when the GUI forks from a thread).

## Building releases

Both builds are wired for CI (`.github/workflows/release.yml`):

- **Linux AppImage** — `packaging/linux/build_appimage.sh` (works locally too).
  It builds a throwaway virtualenv, runs PyInstaller, assembles an AppDir and
  calls `appimagetool`; the result lands in `Releases/`.
- **Windows installer** — `pyinstaller packaging/windows/eqsl_win.spec`, then
  Inno Setup compiles `packaging/windows/installer.iss` (`iscc`). The installer
  lands in `Releases/`.

To publish a release, bump `VERSION`, tag and push:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

The workflow builds the AppImage and the Windows `Setup.exe`, then attaches
both to the GitHub Release.

Local Linux build:

```bash
bash packaging/linux/build_appimage.sh
```

> AppImages need FUSE (`libfuse2`) on modern distros, or run with
> `APPIMAGE_EXTRACT_AND_RUN=1`.

## Limitations / caveats

- Credentials are interactive only (not CLI flags or env vars) — good for
  interactive use, awkward for cron.
- The ADIF parser is intentionally minimal; unusual ADIF layouts may be skipped.
- Relies on eQSL HTML phrases and img URL patterns; site markup changes can
  break parsing.
- Downloads are strictly sequential after the local scan because of rate
  limits; large backlogs take ~11 s per card.
- The Windows installer must be built on Windows (PyInstaller cannot
  cross-compile); use the CI workflow or a Windows machine.

## License / use

Intended for personal use against your own eQSL account. Respect eQSL's rate
limits and terms of service. See `LICENSE`.

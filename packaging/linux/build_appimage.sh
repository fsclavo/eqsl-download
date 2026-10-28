#!/usr/bin/env bash
#
# Build a Linux AppImage for eQSL Card Downloader.
#
# Dependencies (PyInstaller, Pillow) are installed into a throwaway virtualenv
# under build/, so the system Python is never touched. appimagetool is used
# from PATH if available, otherwise downloaded. Runs on CI without FUSE thanks
# to APPIMAGE_EXTRACT_AND_RUN=1.
#
# Usage:  packaging/linux/build_appimage.sh
# Env:    APPIMAGETOOL=/path/to/appimagetool   (optional override)
#         ARCH=x86_64                          (optional, default x86_64)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"

APP="eqsl-card-downloader"
ARCH="${ARCH:-x86_64}"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || echo 0.0.0)"

BUILD="$ROOT/build/appimage"
DIST="$ROOT/dist"
VENV="$BUILD/venv"
APPDIR="$BUILD/${APP}.AppDir"

if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi

echo ">> Cleaning previous build"
rm -rf "$BUILD" "$DIST/$APP"
mkdir -p "$BUILD"

echo ">> Creating build virtualenv"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt" pyinstaller pillow

echo ">> Generating icons"
"$VENV/bin/python" "$ROOT/packaging/make_icons.py"

echo ">> Running PyInstaller (onedir)"
"$VENV/bin/python" -m PyInstaller \
  --noconfirm --clean --windowed \
  --name "$APP" \
  --distpath "$DIST" \
  --workpath "$BUILD/pyinstaller" \
  --specpath "$BUILD" \
  --icon "$ROOT/assets/$APP.png" \
  --add-data "$ROOT/assets/$APP.png:assets" \
  --add-data "$ROOT/assets/$APP-512.png:assets" \
  --add-data "$ROOT/assets/$APP.ico:assets" \
  --add-data "$ROOT/VERSION:." \
  --hidden-import multiprocessing \
  --hidden-import multiprocessing.spawn \
  --hidden-import eqsl_core \
  --hidden-import eqsl_download \
  --collect-submodules multiprocessing \
  "$ROOT/eqsl_gui.py"

echo ">> Assembling AppDir"
rm -rf "$APPDIR"
mkdir -p \
  "$APPDIR/usr/bin" \
  "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$DIST/$APP/." "$APPDIR/usr/bin/"
cp "$SCRIPT_DIR/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp "$SCRIPT_DIR/$APP.desktop" "$APPDIR/$APP.desktop"
cp "$SCRIPT_DIR/$APP.desktop" "$APPDIR/usr/share/applications/$APP.desktop"
cp "$ROOT/assets/$APP.png" "$APPDIR/$APP.png"
cp "$ROOT/assets/$APP.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP.png"

# --- appimagetool ---
TOOL="${APPIMAGETOOL:-}"
if [ -z "$TOOL" ] && command -v appimagetool >/dev/null 2>&1; then
  TOOL="appimagetool"
fi
if [ -z "$TOOL" ]; then
  echo ">> appimagetool not found; downloading"
  TOOL="$BUILD/appimagetool"
  curl -fsSL -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$TOOL"
fi

mkdir -p "$ROOT/Releases"
OUT="$ROOT/Releases/${APP}-${VERSION}-${ARCH}.AppImage"
echo ">> Building AppImage -> $OUT"
ARCH="$ARCH" VERSION="$VERSION" APPIMAGE_EXTRACT_AND_RUN=1 \
  "$TOOL" "$APPDIR" "$OUT"
chmod +x "$OUT"

echo ">> Done: $OUT"

# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for eQSL Gráfico (Windows, onedir).

Build from the repository root with:
    pyinstaller packaging/windows/eqsl_win.spec --noconfirm --clean
Output goes to  dist/eQSL_Grafico/  next to eQSL_Grafico.exe.
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent.parent  # noqa: F821 (SPECPATH injected)
ASSETS = ROOT / "assets"

VERSION = "0.0.0"
try:
    VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() or VERSION
except OSError:
    pass

_ver = [int(p) if p.isdigit() else 0 for p in VERSION.split(".")]
while len(_ver) < 4:
    _ver.append(0)
_ver_tuple = tuple(_ver[:4])
_ver_str = ".".join(str(p) for p in _ver[:3])

VERSION_INFO = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_ver_tuple},
    prodvers={_ver_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040a04b0', [
        StringStruct('CompanyName', 'fsclavo'),
        StringStruct('FileDescription', 'eQSL Gr\u00e1fico'),
        StringStruct('FileVersion', '{_ver_str}'),
        StringStruct('InternalName', 'eQSL_Grafico'),
        StringStruct('OriginalFilename', 'eQSL_Grafico.exe'),
        StringStruct('ProductName', 'eQSL Gr\u00e1fico'),
        StringStruct('ProductVersion', '{_ver_str}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1034, 1200])])
  ]
)
"""

_vi_path = ROOT / "build" / "windows" / "version_info.txt"
_vi_path.parent.mkdir(parents=True, exist_ok=True)
_vi_path.write_text(VERSION_INFO, encoding="utf-8")

a = Analysis(  # noqa: F821
    [str(ROOT / "eqsl_gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ASSETS / "eqsl-grafico.png"), "assets"),
        (str(ASSETS / "eqsl-grafico-512.png"), "assets"),
        (str(ASSETS / "eqsl-grafico.ico"), "assets"),
        (str(ROOT / "VERSION"), "."),
    ],
    hiddenimports=[
        "multiprocessing",
        "multiprocessing.spawn",
        "multiprocessing.pool",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL", "PyInstaller", "setuptools", "pip"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="eQSL_Grafico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ASSETS / "eqsl-grafico.ico"),
    version=str(_vi_path),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="eQSL_Grafico",
)

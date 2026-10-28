#!/usr/bin/env python3
"""Write a SHA256SUMS file for the release artifacts in a directory.

Usage:  python3 packaging/make_checksums.py <dir>

Every regular file in <dir> except an existing SHA256SUMS is hashed, so the
release job can attach the result next to the built artifacts.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    directory = Path(argv[1]) if len(argv) > 1 else Path(".")
    if not directory.is_dir():
        print(f"Not a directory: {directory}", file=sys.stderr)
        return 2

    files = sorted(
        p for p in directory.iterdir() if p.is_file() and p.name != "SHA256SUMS"
    )
    if not files:
        print(f"No files to checksum in {directory}", file=sys.stderr)
        return 1

    lines = [f"{sha256_file(p)}  {p.name}" for p in files]
    output = directory / "SHA256SUMS"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

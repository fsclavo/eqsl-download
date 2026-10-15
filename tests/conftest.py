"""Shared pytest fixtures and helpers.

Adds the repository root to ``sys.path`` so the test suite can import the
modules directly without installing the project.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _image_bytes(fmt: str, size: tuple[int, int] = (200, 200)) -> bytes:
    """Build a *large-enough* (uncompressible) valid image of the given format."""
    from PIL import Image

    pixels = os.urandom(size[0] * size[1] * 3)
    img = Image.frombytes("RGB", size, pixels)
    buffer = io.BytesIO()
    img.save(buffer, fmt)
    return buffer.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes("JPEG")


@pytest.fixture
def png_bytes() -> bytes:
    return _image_bytes("PNG")


@pytest.fixture
def write_file(tmp_path: Path):
    """Return a helper that writes bytes to a file in tmp_path."""

    def _write(name: str, data: bytes) -> Path:
        path = tmp_path / name
        path.write_bytes(data)
        return path

    return _write


SAMPLE_ADIF = (
    "eQSL.cc downloaded file\n"
    "Some header text before EOH\n"
    "<EOH>\n"
    "<CALL:5>W1AW <QSO_DATE:8>20240315 <TIME_ON:4>1423 <BAND:3>20m <MODE:3>FT8 <EOR>\n"
    "<CALL:6>LU1ABC<QSO_DATE:8>20240316<TIME_ON:6>142300<BAND:3>40m"
    "<MODE:4>JT65<SUBMODE:5>JT65A<EOR>\n"
    "<CALL:4>K1ZZ<QSO_DATE:8>20240317<TIME_ON:4>0900<BAND:3>15m<EOR>\n"
)


@pytest.fixture
def sample_adif() -> str:
    return SAMPLE_ADIF

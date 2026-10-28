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
for extra in (ROOT, ROOT / "packaging"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


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


@pytest.fixture
def http_server():
    """Start a local HTTP server returning a scripted list of status codes.

    Usage: url, state = http_server([503, 200]); state["count"] counts hits.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    servers = []

    def _make(codes):
        state = {"count": 0, "codes": list(codes)}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                state["count"] += 1
                code = state["codes"].pop(0) if state["codes"] else 200
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):  # silence test output
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        return url, state

    yield _make

    for server in servers:
        server.shutdown()
        server.server_close()


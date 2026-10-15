"""HTTP layer tests using mocked responses (no real network)."""

from __future__ import annotations

from datetime import date

import pytest
import requests

import eqsl_core
from eqsl_core import (
    DOWNLOAD_INBOX,
    GET_EQSL,
    EQSLError,
    QSO,
    download_card,
    extract_image_url,
    fetch_inbox_adif,
    make_session,
)

ADIF_URL = "https://www.eqsl.cc/qslcard/download.cfm?file=123.adi"
IMG_URL = "https://www.eqsl.cc/qslcard/CFFileServlet/_cf_image/abc.png"
ADIF_BODY = "<EOH>\n<CALL:5>W1AW <QSO_DATE:8>20240315 <EOR>\n"


def test_make_session_sets_user_agent():
    session = make_session()
    assert "fer-eqsl-downloader" in session.headers["User-Agent"]


def test_fetch_inbox_adif_success(requests_mock):
    requests_mock.get(
        DOWNLOAD_INBOX,
        text=f'<html><a href="{ADIF_URL}">Your ADIF log file has been built</a></html>',
    )
    requests_mock.get(ADIF_URL, text=ADIF_BODY)

    out = fetch_inbox_adif(
        make_session(), "user", "pass", "", date(2024, 1, 1), date(2024, 12, 31)
    )
    assert "W1AW" in out


def test_fetch_inbox_adif_missing_success_phrase(requests_mock):
    requests_mock.get(DOWNLOAD_INBOX, text="<html>Wrong password</html>")
    with pytest.raises(EQSLError):
        fetch_inbox_adif(
            make_session(), "user", "bad", "", date(2024, 1, 1), date(2024, 12, 31)
        )


def test_fetch_inbox_adif_missing_link(requests_mock):
    requests_mock.get(DOWNLOAD_INBOX, text="Your ADIF log file has been built")
    with pytest.raises(EQSLError):
        fetch_inbox_adif(
            make_session(), "user", "pass", "", date(2024, 1, 1), date(2024, 12, 31)
        )


def test_extract_image_url():
    page = "https://www.eqsl.cc/qslcard/GeteQSL.cfm"
    html = f'<html><body><img src="{IMG_URL}"></body></html>'
    assert extract_image_url(html, page) == IMG_URL
    assert extract_image_url("<html>Error: nope</html>", page) is None
    assert extract_image_url("<html>no image here</html>", page) is None


def test_download_card_success(requests_mock, tmp_path, png_bytes):
    requests_mock.get(GET_EQSL, text=f'<html><img src="{IMG_URL}"></html>')
    requests_mock.get(IMG_URL, content=png_bytes)

    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    dest = tmp_path / "card.jpg"
    ok, msg = download_card(make_session(), "user", "pass", qso, dest)

    assert ok is True
    assert msg == "ok"
    assert dest.read_bytes() == png_bytes


def test_download_card_throttling_retries(requests_mock, tmp_path, png_bytes, monkeypatch):
    monkeypatch.setattr(eqsl_core, "THROTTLE_WAIT_SEC", 0)
    requests_mock.get(
        GET_EQSL,
        [
            {"text": "<html>Throttling invoked</html>"},
            {"text": f'<html><img src="{IMG_URL}"></html>'},
        ],
    )
    requests_mock.get(IMG_URL, content=png_bytes)

    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    ok, _ = download_card(make_session(), "user", "pass", qso, tmp_path / "card.jpg")
    assert ok is True
    assert requests_mock.call_count == 3  # throttle + retry + image


def test_download_card_error_response(requests_mock, tmp_path):
    requests_mock.get(GET_EQSL, text="<html>Error: Invalid login</html>")
    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    ok, msg = download_card(make_session(), "user", "pass", qso, tmp_path / "x.jpg")
    assert ok is False
    assert "Error" in msg


def test_download_card_no_image_url(requests_mock, tmp_path, monkeypatch):
    monkeypatch.setattr(eqsl_core, "THROTTLE_WAIT_SEC", 0)
    requests_mock.get(GET_EQSL, text="<html>no image</html>")
    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    ok, msg = download_card(make_session(), "user", "pass", qso, tmp_path / "x.jpg")
    assert ok is False
    assert "No image URL" in msg


def test_download_card_network_error(requests_mock, tmp_path):
    requests_mock.get(GET_EQSL, exc=requests.exceptions.ConnectTimeout)
    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    with pytest.raises(requests.RequestException):
        download_card(make_session(), "user", "pass", qso, tmp_path / "x.jpg")

"""Golden-HTML tests: lock the eQSL scraping behaviour to real-ish markup."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from eqsl_core import (
    DOWNLOAD_INBOX,
    GET_EQSL,
    EQSLAuthError,
    EQSLError,
    QSO,
    download_card,
    extract_image_url,
    fetch_inbox_adif,
    make_session,
    parse_adif,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


ADIF_BODY = (
    "<EOH>\n"
    "<CALL:5>W1AW <QSO_DATE:8>20240315 <TIME_ON:4>1200 "
    "<BAND:3>20m <MODE:3>FT8 <EOR>\n"
)


def test_inbox_ok_fixture(requests_mock):
    requests_mock.get(DOWNLOAD_INBOX, text=_fixture("inbox_ok.html"))
    requests_mock.get(re.compile(r".*/qslcard/download\.cfm"), text=ADIF_BODY)

    adif = fetch_inbox_adif(
        make_session(), "u", "p", "", date(2024, 1, 1), date(2024, 12, 31)
    )
    qsos = parse_adif(adif)
    assert len(qsos) == 1
    assert qsos[0].call == "W1AW"


def test_inbox_error_fixture(requests_mock):
    requests_mock.get(DOWNLOAD_INBOX, text=_fixture("inbox_error.html"))
    with pytest.raises(EQSLError):
        fetch_inbox_adif(
            make_session(), "u", "p", "", date(2024, 1, 1), date(2024, 12, 31)
        )


def test_inbox_bad_login_fixture_is_a_friendly_auth_error(requests_mock):
    requests_mock.get(DOWNLOAD_INBOX, text=_fixture("inbox_bad_login.html"))
    with pytest.raises(EQSLAuthError) as excinfo:
        fetch_inbox_adif(
            make_session(), "u", "wrong", "", date(2024, 1, 1), date(2024, 12, 31)
        )
    message = str(excinfo.value)
    assert "Login failed" in message
    assert "<" not in message  # no raw HTML in the user-facing message


def test_geteqsl_ok_fixture_extracts_image_url():
    url = extract_image_url(
        _fixture("geteqsl_ok.html"), "https://www.eqsl.cc/qslcard/GeteQSL.cfm"
    )
    assert url is not None
    assert url.endswith("CFFileServlet/_cf_image/abc123.PNG")


def test_geteqsl_error_fixture_has_no_image():
    assert extract_image_url(_fixture("geteqsl_error.html"), "https://x/") is None


def test_geteqsl_no_image_fixture_has_no_image():
    assert extract_image_url(_fixture("geteqsl_no_image.html"), "https://x/") is None


def test_geteqsl_throttle_fixture_is_detected():
    assert "Throttling invoked" in _fixture("geteqsl_throttle.html")


def test_download_card_from_fixture(requests_mock, tmp_path, png_bytes):
    requests_mock.get(GET_EQSL, text=_fixture("geteqsl_ok.html"))
    requests_mock.get(re.compile(r".*/CFFileServlet/_cf_image/.*"), content=png_bytes)

    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    ok, _ = download_card(make_session(), "u", "p", qso, tmp_path / "card.jpg")
    assert ok is True
    assert (tmp_path / "card.jpg").read_bytes() == png_bytes

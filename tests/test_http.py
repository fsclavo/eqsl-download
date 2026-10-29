"""HTTP layer tests using mocked responses (no real network)."""

from __future__ import annotations

from datetime import date

import pytest
import requests

import eqsl_core
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


def test_fetch_inbox_adif_bad_credentials_is_friendly(requests_mock):
    requests_mock.get(
        DOWNLOAD_INBOX,
        text="<html><body><h2>Invalid UserName or Password</h2></body></html>",
    )
    with pytest.raises(EQSLAuthError) as excinfo:
        fetch_inbox_adif(
            make_session(), "user", "wrong", "", date(2024, 1, 1), date(2024, 12, 31)
        )
    message = str(excinfo.value)
    assert "Login failed" in message
    assert "<" not in message


def test_fetch_inbox_adif_unknown_failure_hides_html(requests_mock):
    requests_mock.get(
        DOWNLOAD_INBOX,
        text="<html><body><p>An unexpected server condition</p></body></html>",
    )
    with pytest.raises(EQSLError) as excinfo:
        fetch_inbox_adif(
            make_session(), "user", "pass", "", date(2024, 1, 1), date(2024, 12, 31)
        )
    message = str(excinfo.value)
    assert "An unexpected server condition" in message
    assert "<" not in message


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
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_card_atomic_failure_leaves_no_partial(
    requests_mock, tmp_path, png_bytes, monkeypatch
):
    requests_mock.get(GET_EQSL, text=f'<html><img src="{IMG_URL}"></html>')
    requests_mock.get(IMG_URL, content=png_bytes)

    def boom_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(eqsl_core.os, "replace", boom_replace)
    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    dest = tmp_path / "card.jpg"
    with pytest.raises(OSError):
        download_card(make_session(), "user", "pass", qso, dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


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


def test_download_card_too_small_image(requests_mock, tmp_path):
    requests_mock.get(GET_EQSL, text=f'<html><img src="{IMG_URL}"></html>')
    requests_mock.get(IMG_URL, content=b"tiny")
    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    ok, msg = download_card(make_session(), "user", "pass", qso, tmp_path / "x.jpg")
    assert ok is False
    assert "too small" in msg


def test_make_session_configures_retries():
    session = make_session()
    retries = session.get_adapter("https://example.com").max_retries
    assert retries.total == eqsl_core.RETRY_TOTAL
    assert retries.backoff_factor == eqsl_core.RETRY_BACKOFF_FACTOR
    assert 429 in retries.status_forcelist
    assert retries.allowed_methods == frozenset({"GET"})


def test_fetch_inbox_adif_sends_expected_params(requests_mock):
    requests_mock.get(
        DOWNLOAD_INBOX,
        text=f'<a href="{ADIF_URL}">Your ADIF log file has been built</a>',
    )
    requests_mock.get(ADIF_URL, text=ADIF_BODY)

    fetch_inbox_adif(
        make_session(), "user", "pass", "Home", date(2024, 1, 2), date(2024, 3, 4)
    )
    query = {k.lower(): v for k, v in requests_mock.request_history[0].qs.items()}
    assert query["username"] == ["user"]
    assert query["limitdatelo"] == ["01/02/2024"]
    assert query["limitdatehi"] == ["03/04/2024"]
    assert query["qthnickname"][0].lower() == "home"


def test_download_card_sends_expected_params(requests_mock, tmp_path, png_bytes):
    requests_mock.get(GET_EQSL, text=f'<html><img src="{IMG_URL}"></html>')
    requests_mock.get(IMG_URL, content=png_bytes)

    qso = QSO("w1aw/1", "20240315", "142359", "20M", "JT65", "JT65A")
    download_card(make_session(), "user", "pass", qso, tmp_path / "c.jpg")

    query = {k.lower(): v for k, v in requests_mock.request_history[0].qs.items()}
    assert query["callsignfrom"] == ["w1aw/1"]
    assert query["qsoyear"] == ["2024"]
    assert query["qsomonth"] == ["03"]
    assert query["qsoday"] == ["15"]
    assert query["qsohour"] == ["14"]
    assert query["qsominute"] == ["23"]
    assert query["qsoband"][0].lower() == "20m"
    assert query["qsomode"][0].lower() == "jt65"


@pytest.mark.slow
def test_retries_on_503_then_succeeds(http_server):
    url, state = http_server([503, 200])
    response = make_session().get(url, timeout=5)
    assert response.status_code == 200
    assert state["count"] == 2


@pytest.mark.slow
def test_does_not_retry_on_400(http_server):
    url, state = http_server([400, 200])
    response = make_session().get(url, timeout=5)
    assert response.status_code == 400
    assert state["count"] == 1



def test_download_card_max_attempts_one(requests_mock, tmp_path, monkeypatch):
    monkeypatch.setattr(eqsl_core, "THROTTLE_WAIT_SEC", 0)
    requests_mock.get(GET_EQSL, text="<html>no image</html>")
    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    ok, _ = download_card(
        make_session(), "u", "p", qso, tmp_path / "x.jpg", max_attempts=1
    )
    assert ok is False
    assert requests_mock.call_count == 1


def test_download_card_max_attempts_two(requests_mock, tmp_path, monkeypatch):
    monkeypatch.setattr(eqsl_core, "THROTTLE_WAIT_SEC", 0)
    requests_mock.get(GET_EQSL, text="<html>no image</html>")
    qso = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    ok, _ = download_card(
        make_session(), "u", "p", qso, tmp_path / "x.jpg", max_attempts=2
    )
    assert ok is False
    assert requests_mock.call_count == 2

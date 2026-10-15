"""ADIF parsing tests."""

from __future__ import annotations

from eqsl_core import parse_adif


def test_parses_complete_records(sample_adif):
    records = parse_adif(sample_adif)
    assert len(records) == 2

    first = records[0]
    assert first.call == "W1AW"
    assert first.qso_date == "20240315"
    assert first.time_on == "1423"
    assert first.band == "20m"
    assert first.mode == "FT8"

    second = records[1]
    assert second.call == "LU1ABC"
    assert second.submode == "JT65A"
    assert second.get_mode() == "JT65"


def test_skips_incomplete_records(sample_adif):
    # The third record has no MODE and must be dropped.
    calls = [q.call for q in parse_adif(sample_adif)]
    assert "K1ZZ" not in calls


def test_parses_without_eoh_marker():
    text = "<CALL:6>N0CALL<QSO_DATE:8>20240101<TIME_ON:4>0000<BAND:3>40m<MODE:3>CW<EOR>"
    records = parse_adif(text)
    assert len(records) == 1
    assert records[0].call == "N0CALL"


def test_empty_input():
    assert parse_adif("") == []
    assert parse_adif("<EOH>\n") == []


def test_length_field_truncates_value():
    # <MODE:4>FT8X must be read as "FT8X" (length honoured), then normalized.
    text = "<CALL:4>W1AW<QSO_DATE:8>20240101<TIME_ON:4>1200<BAND:3>20m<MODE:4>FT8X<EOR>"
    records = parse_adif(text)
    assert records[0].mode == "FT8X"

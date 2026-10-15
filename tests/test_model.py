"""QSO model and callsign sanitisation tests."""

from __future__ import annotations

from eqsl_core import QSO, sanitize_call


def test_sanitize_call_uppercases_and_replaces_separators():
    assert sanitize_call("lu1abc/p") == "LU1ABC_P"
    assert sanitize_call("w1\\aw") == "W1_AW"


def test_time_hhmm_short_and_long():
    assert QSO("W1AW", "20240315", "1423", "20m", "FT8").time_hhmm == "1423"
    assert QSO("W1AW", "20240315", "142300", "20m", "FT8").time_hhmm == "1423"


def test_time_hhmm_pads_short_values():
    assert QSO("W1AW", "20240315", "9", "20m", "FT8").time_hhmm == "0009"


def test_filename_format_and_sanitisation():
    qso = QSO("w1aw/1", "20240315", "1423", "20M", "FT8")
    assert qso.filename("lu1abc/p") == "LU1ABC_P-20240315_1423-W1AW_1-20m-FT8.jpg"


def test_get_mode_normalizes_jt9_submode():
    qso = QSO("W1AW", "20240315", "1423", "20m", "MFSK", "JT9")
    assert qso.get_mode() == "JT9"


def test_get_mode_passthrough_for_regular_modes():
    qso = QSO("W1AW", "20240315", "1423", "20m", "SSB")
    assert qso.get_mode() == "SSB"

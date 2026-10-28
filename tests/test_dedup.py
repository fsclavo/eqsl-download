"""Deduplication / filename collision tests (H1)."""

from __future__ import annotations

from eqsl_core import QSO, build_by_name


def test_build_by_name_without_collisions():
    qsos = [
        QSO("W1AW", "20240315", "1423", "20m", "FT8"),
        QSO("K1ZZ", "20240315", "1423", "40m", "CW"),
    ]
    by_name, collisions = build_by_name(qsos, "LU1ABC")
    assert len(by_name) == 2
    assert collisions == []


def test_build_by_name_detects_same_minute_collision():
    # Different seconds, same HHMM -> same target filename.
    q1 = QSO("W1AW", "20240315", "1423", "20m", "FT8")
    q2 = QSO("W1AW", "20240315", "142359", "20m", "FT8")
    by_name, collisions = build_by_name([q1, q2], "LU1ABC")

    expected = "LU1ABC-20240315_1423-W1AW-20m-FT8.jpg"
    assert expected in by_name
    assert collisions == [expected]
    # Last record wins (documented legacy behaviour).
    assert by_name[expected].time_on == "142359"


def test_build_by_name_empty():
    assert build_by_name([], "LU1ABC") == ({}, [])

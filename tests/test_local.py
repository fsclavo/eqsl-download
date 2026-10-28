"""Local file listing / classification / scanning tests."""

from __future__ import annotations

from datetime import date

from eqsl_core import (
    QSO,
    build_by_name,
    classify_local_file,
    list_local_jpgs,
    parse_adif,
    scan_local_qsos,
    validate_local_paths,
)

NAME_A = "LU1-20240315_1423-W1AW-20m-FT8.jpg"
NAME_B = "LU1-20240316_0900-K1ZZ-40m-CW.jpg"


def _qso(call: str, qso_date: str) -> QSO:
    return QSO(call, qso_date, "1200", "20m", "FT8")


def test_list_local_jpgs_without_filter(write_file, jpeg_bytes):
    write_file(NAME_A, jpeg_bytes)
    write_file(NAME_B, jpeg_bytes)
    write_file("notes.txt", b"ignore me")
    files = list_local_jpgs(write_file(NAME_A, jpeg_bytes).parent)
    names = sorted(p.name for p in files)
    assert names == [NAME_A, NAME_B]


def test_list_local_jpgs_filters_by_date(write_file, jpeg_bytes):
    dest = write_file(NAME_A, jpeg_bytes).parent
    write_file(NAME_B, jpeg_bytes)
    selected = list_local_jpgs(dest, date(2024, 1, 1), date(2024, 3, 15))
    assert [p.name for p in selected] == [NAME_A]


def test_list_local_jpgs_ignores_non_conforming_names(write_file, jpeg_bytes):
    dest = write_file("random.jpg", jpeg_bytes).parent
    assert list_local_jpgs(dest, date(2024, 1, 1), date(2024, 12, 31)) == []


def test_classify_local_file_states(write_file, jpeg_bytes):
    dest = write_file(NAME_A, jpeg_bytes).parent
    status, name, qso = classify_local_file(NAME_A, _qso("W1AW", "20240315"), dest)
    assert (status, name) == ("ok", NAME_A)
    assert qso.call == "W1AW"

    write_file("broken-20240101_0000-X-20m-FT8.jpg", b"not an image")
    status, _, _ = classify_local_file(
        "broken-20240101_0000-X-20m-FT8.jpg", _qso("X", "20240101"), dest
    )
    assert status == "corrupt"

    status, _, _ = classify_local_file("absent.jpg", _qso("X", "20240101"), dest)
    assert status == "missing"


def test_validate_local_paths_counts(write_file, jpeg_bytes):
    good = write_file(NAME_A, jpeg_bytes)
    bad = write_file(NAME_B, b"broken")
    ok, corrupt, names = validate_local_paths([good, bad], workers=2)
    assert ok == 1
    assert corrupt == 1
    assert names == [NAME_B]


def test_validate_local_paths_empty():
    assert validate_local_paths([], workers=2) == (0, 0, [])


def test_scan_local_qsos_classifies_and_lists_pending(write_file, jpeg_bytes):
    write_file(NAME_A, jpeg_bytes)
    write_file(NAME_B, b"broken")

    by_name = {
        NAME_A: _qso("W1AW", "20240315"),
        NAME_B: _qso("K1ZZ", "20240316"),
        "LU1-20240317_1200-N0CALL-15m-FT8.jpg": _qso("N0CALL", "20240317"),
    }
    dest = write_file(NAME_A, jpeg_bytes).parent

    skipped, corrupt, missing, to_fetch, corrupt_names, missing_names = scan_local_qsos(
        by_name, dest, workers=2
    )
    assert skipped == 1
    assert corrupt == 1
    assert missing == 1
    assert {name for name, _ in to_fetch} == {
        NAME_B,
        "LU1-20240317_1200-N0CALL-15m-FT8.jpg",
    }
    assert corrupt_names == [NAME_B]
    assert missing_names == ["LU1-20240317_1200-N0CALL-15m-FT8.jpg"]


def test_end_to_end_adif_to_scan(tmp_path, jpeg_bytes):
    adif = (
        "<EOH>\n"
        "<CALL:5>W1AW <QSO_DATE:8>20240315 <TIME_ON:4>1200 <BAND:3>20m <MODE:3>FT8 <EOR>\n"
        "<CALL:4>K1ZZ<QSO_DATE:8>20240316<TIME_ON:4>0900<BAND:3>40m<MODE:2>CW<EOR>\n"
    )
    by_name, collisions = build_by_name(parse_adif(adif), "LU1ABC")
    assert collisions == []
    assert len(by_name) == 2

    present = next(iter(by_name))
    (tmp_path / present).write_bytes(jpeg_bytes)

    skipped, corrupt, missing, to_fetch, _, _ = scan_local_qsos(by_name, tmp_path, 1)
    assert skipped == 1
    assert corrupt == 0
    assert missing == 1
    assert [name for name, _qso in to_fetch] != [present]

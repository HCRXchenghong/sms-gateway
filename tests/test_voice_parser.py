"""Parser tests driven by AT lines copied from real A7670 sessions."""
from __future__ import annotations

import pytest

from app.voice.parser import classify_voice_line, is_voice_urc, parse_clcc


@pytest.mark.parametrize(
    "line,kind,number",
    [
        ("RING", "ring", None),
        ('+CLIP: "19359372754",129,,0,,0', "clip", "19359372754"),
        ('+CLIP: "+8613800138000",145,,0,,0', "clip", "+8613800138000"),
        ("NO CARRIER", "no_carrier", None),
        ("BUSY", "busy", None),
        ("NO ANSWER", "no_answer", None),
        ("NO DIALTONE", "no_dialtone", None),
        ("CONNECT", "connect", None),
        ("+CTTS: 0", "ctts", None),
        ("+CTTS: 1", "ctts", None),
    ],
)
def test_classify_known_lines(line, kind, number):
    urc = classify_voice_line(line)
    assert urc is not None, line
    assert urc.kind == kind
    assert urc.number == number


def test_ctts_value_parsed():
    assert classify_voice_line("+CTTS: 0").value == 0
    assert classify_voice_line("+CTTS: 2").value == 2


def test_non_voice_lines_are_ignored():
    for line in ("OK", "ERROR", "+CMS ERROR: 500", "+CMTI: \"SM\",3", "ATD138;"):
        assert classify_voice_line(line) is None, line


def test_is_voice_urc_prefixes():
    assert is_voice_urc("RING")
    assert is_voice_urc("NO CARRIER")
    assert is_voice_urc("+CLIP: \"139\",129")
    assert not is_voice_urc("OK")
    assert not is_voice_urc("")
    assert not is_voice_urc("+CMTI: \"SM\",1")


def test_clcc_parsing():
    row = parse_clcc('+CLCC: 1,0,0,0,0,"13800138000",129')
    assert row == {
        "id": "1",
        "dir": "0",
        "stat": "0",
        "mode": "0",
        "mpty": "0",
        "number": "13800138000",
    }


def test_clcc_incoming_direction():
    row = parse_clcc('+CLCC: 1,1,4,0,0,"+8613800138000",145')
    assert row["dir"] == "1"
    assert row["stat"] == "4"
    assert row["number"] == "+8613800138000"


def test_clcc_rejects_other_lines():
    assert parse_clcc("OK") is None
    assert parse_clcc("+CLCC: 1") is None

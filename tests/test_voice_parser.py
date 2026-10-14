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
        # Captured from an A7670G-LLSE (firmware A7670M7_V1.11.1) placing a real
        # call. This firmware reports progress with SIMCom wording rather than
        # the 3GPP CONNECT line, so it has to be mapped onto the same kinds.
        ("VOICE CALL: BEGIN", "connect", None),
        ('+COLP: "15331986287",129', "colp", "15331986287"),
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


def test_simcom_voice_call_end_carries_talk_time():
    """The trailing field is the talk time in seconds, zero padded."""
    urc = classify_voice_line("VOICE CALL: END: 000015")
    assert urc is not None
    assert urc.kind == "cend"
    assert urc.value == 15


def test_simcom_voice_call_end_zero_duration():
    urc = classify_voice_line("VOICE CALL: END: 000000")
    assert urc is not None and urc.value == 0


@pytest.mark.parametrize(
    "line,stat,number",
    [
        ('+CLCC: 1,0,2,0,0,"15331986287",129,""', "2", "15331986287"),
        ('+CLCC: 1,0,3,0,0,"15331986287",129,""', "3", "15331986287"),
        ('+CLCC: 1,0,0,0,0,"15331986287",129,""', "0", "15331986287"),
        ('+CLCC: 1,0,6,0,0,"15331986287",129,""', "6", "15331986287"),
    ],
)
def test_pushed_clcc_row_is_classified(line, stat, number):
    """A7670 pushes CLCC rows on every state change instead of waiting to be polled."""
    urc = classify_voice_line(line)
    assert urc is not None, line
    assert urc.kind == "clcc"
    assert urc.extra["stat"] == stat
    assert urc.number == number


def test_non_voice_lines_are_ignored():
    for line in ("OK", "ERROR", "+CMS ERROR: 500", "+CMTI: \"SM\",3", "ATD138;"):
        assert classify_voice_line(line) is None, line


def test_bearer_and_data_lines_are_ignored():
    """Packet domain notifications are not voice events and must not end a call."""
    for line in ("+CGEV: NW ACT 8,10", "+CGEV: NW DEACT 8,10"):
        assert classify_voice_line(line) is None, line
        assert not is_voice_urc(line), line


def test_is_voice_urc_prefixes():
    assert is_voice_urc("RING")
    assert is_voice_urc("NO CARRIER")
    assert is_voice_urc("+CLIP: \"139\",129")
    assert is_voice_urc("VOICE CALL: BEGIN")
    assert is_voice_urc("VOICE CALL: END: 000015")
    assert is_voice_urc('+COLP: "15331986287",129')
    assert is_voice_urc('+CLCC: 1,0,0,0,0,"15331986287",129,""')
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

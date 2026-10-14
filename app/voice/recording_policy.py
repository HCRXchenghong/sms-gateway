"""Recording policy: whether a call is captured, and whether the far end is told.

These are two independent decisions. Whether audio is kept is an administrative
policy (forced, optional or disabled). Whether the other party hears a notice
is a separate switch, because a call can legitimately be recorded silently or
announced without being kept.

The wording is deliberate. The notice states that the call is being recorded
and nothing more: it never claims the recording is evidence, admissible, or
legally binding, because playing a sentence cannot make it so. What the system
does keep for that purpose is the raw audio, the announcement timestamp and
result, and the access log.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

__all__ = [
    "POLICIES",
    "POLICY_FORCED",
    "POLICY_OPTIONAL",
    "POLICY_DISABLED",
    "NOTICE_AT_START",
    "NOTICE_MID_CALL",
    "RecordingDecision",
    "decide_recording",
]

POLICY_FORCED = "forced"
POLICY_OPTIONAL = "optional"
POLICY_DISABLED = "disabled"
POLICIES = (POLICY_FORCED, POLICY_OPTIONAL, POLICY_DISABLED)

NOTICE_AT_START = "本次通话已开启录音"
NOTICE_MID_CALL = "录音已开启，接下来的通话将被记录"


@dataclass
class RecordingDecision:
    record: bool
    announce: bool
    can_change: bool
    reason: str
    notice_text: str = ""

    def to_dict(self) -> dict:
        return {
            "record": self.record,
            "announce": self.announce,
            "can_change": self.can_change,
            "reason": self.reason,
            "notice_text": self.notice_text,
        }


def decide_recording(
    *,
    policy: str,
    requested: Optional[bool] = None,
    announce: Optional[bool] = None,
    default_enabled: bool = True,
    announce_default: bool = False,
    mid_call: bool = False,
) -> RecordingDecision:
    """Resolve one call's recording behaviour.

    requested and announce are per-call overrides from the app or the open API.
    A forced or disabled policy ignores them: an operator or a third party must
    not be able to turn mandated recording off, or start recording where the
    administrator has forbidden keeping audio.
    """
    normalized = (policy or "").strip().lower()
    if normalized not in POLICIES:
        log.warning("unknown recording policy %r, treating it as optional", policy)
        normalized = POLICY_OPTIONAL

    if normalized == POLICY_FORCED:
        record = True
        can_change = False
        reason = "policy_forced"
        do_announce = bool(announce_default)
    elif normalized == POLICY_DISABLED:
        record = False
        can_change = False
        reason = "policy_disabled"
        do_announce = False
    else:
        record = bool(default_enabled) if requested is None else bool(requested)
        can_change = True
        if requested is None:
            reason = "policy_default"
        elif record:
            reason = "operator_enabled"
        else:
            reason = "operator_disabled"
        do_announce = bool(announce_default) if announce is None else bool(announce)

    if not record:
        # Nothing is being kept, so there is nothing to tell the far end about.
        do_announce = False

    notice = ""
    if do_announce:
        notice = NOTICE_MID_CALL if mid_call else NOTICE_AT_START

    return RecordingDecision(
        record=record,
        announce=do_announce,
        can_change=can_change,
        reason=reason,
        notice_text=notice,
    )

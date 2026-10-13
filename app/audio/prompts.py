"""Prompt tone registry.

Every prompt has a Chinese sentence plus a fallback telephony tone. The tone
version is always synthesised locally so prompts work with zero configuration.
When an AI TTS provider is configured, a spoken variant is rendered next to it
and takes precedence, which is what makes the "connecting, please wait" style
prompts actually speak instead of just beeping.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

from . import tones

log = logging.getLogger(__name__)

__all__ = [
    "PromptSpec",
    "PROMPTS",
    "PROMPT_BY_ID",
    "STATE_PROMPTS",
    "END_REASON_PROMPTS",
    "resolve_prompt",
    "prompt_path",
    "state_prompt",
    "ensure_prompt_files",
]


@dataclass(frozen=True)
class PromptSpec:
    id: str
    text: str
    tone: str
    loops: int = 1
    category: str = "call"
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


PROMPTS: List[PromptSpec] = [
    PromptSpec("dial_connecting", "正在接通，请稍候", "ringback_cn", 2, "call", "拨号后播放，提示操作者正在建立通话"),
    PromptSpec("ringback", "请稍候，对方正在接听", "ringback_cn", 3, "call", "对端振铃"),
    PromptSpec("incoming_call", "您有来电，请及时接听", "ring", 2, "call", "模块收到 RING 时提醒操作者"),
    PromptSpec("call_busy", "您拨打的电话正忙，请稍后再拨", "busy_cn", 3, "call", "对方占线"),
    PromptSpec("call_no_answer", "您拨打的电话暂时无人接听，请稍后再拨", "ringback_cn", 1, "call", "无应答超时"),
    PromptSpec("call_not_exist", "您拨打的号码是空号，请查证后再拨", "congestion", 3, "call", "号码不存在或无载波"),
    PromptSpec("call_rejected", "对方已拒接来电", "busy_cn", 2, "call", "被叫拒接"),
    PromptSpec("call_ended", "通话已结束", "click", 1, "call", "正常挂断"),
    PromptSpec("call_error", "呼叫失败，请检查设备状态", "error", 1, "call", "指令错误或模块离线"),
    PromptSpec("call_hold", "通话已保持，请稍候", "hold", 2, "call", "通话保持"),
    PromptSpec("recording_notice", "本次通话可能被录音", "beep", 1, "compliance", "开启录音时的合规播报"),
    PromptSpec("ai_thinking", "正在为您处理，请稍候", "double_beep", 1, "ai", "AI 生成回复期间的等待音"),
    PromptSpec("ai_handover", "正在为您转接智能助理", "success", 1, "ai", "通话交给 AI 接管"),
    PromptSpec("sms_sent", "短信发送成功", "success", 1, "sms", "短信发送完成"),
    PromptSpec("sms_failed", "短信发送失败", "error", 1, "sms", "短信发送失败"),
    PromptSpec("dial_tone", "", "dialtone", 1, "system", "拨号盘摘机音"),
    PromptSpec("key_press", "", "click", 1, "system", "拨号盘按键反馈"),
    PromptSpec("welcome", "欢迎使用语音短信网关", "success", 1, "system", "登录或首页欢迎语"),
]

PROMPT_BY_ID: Dict[str, PromptSpec] = {p.id: p for p in PROMPTS}

# Call state transitions that should play something to the operator.
STATE_PROMPTS: Dict[str, str] = {
    "dialing": "dial_connecting",
    "alerting": "ringback",
    "incoming": "incoming_call",
    "held": "call_hold",
}

# How a call ended decides which farewell prompt to play.
END_REASON_PROMPTS: Dict[str, str] = {
    "busy": "call_busy",
    "no_answer": "call_no_answer",
    "no_carrier": "call_not_exist",
    "no_dialtone": "call_not_exist",
    "rejected": "call_rejected",
    "local_hangup": "call_ended",
    "remote_hangup": "call_ended",
    "timeout": "call_no_answer",
    "error": "call_error",
    "unknown": "call_ended",
}


def resolve_prompt(prompt_id: str) -> Optional[PromptSpec]:
    return PROMPT_BY_ID.get(prompt_id)


def state_prompt(state: str, end_reason: Optional[str] = None) -> Optional[str]:
    """Map a call state transition to the prompt that should play."""
    if state == "ended":
        return END_REASON_PROMPTS.get(end_reason or "", "call_ended")
    return STATE_PROMPTS.get(state)


def prompt_path(prompt_id: str, base_dir: Path) -> Optional[Path]:
    """Return the best available WAV for a prompt, preferring spoken audio."""
    base = Path(base_dir)
    spoken = base / f"{prompt_id}.tts.wav"
    if spoken.exists():
        return spoken
    tone_file = base / f"{prompt_id}.wav"
    if tone_file.exists():
        return tone_file
    return None


def ensure_prompt_files(base_dir: Path, *, force: bool = False) -> List[dict]:
    """Render any missing tone-backed prompt WAVs. Returns a report."""
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    report: List[dict] = []
    for spec in PROMPTS:
        target = base / f"{spec.id}.wav"
        if target.exists() and not force:
            report.append({"id": spec.id, "path": str(target), "created": False})
            continue
        try:
            tones.write_tone(spec.tone, target, loops=spec.loops)
            report.append({"id": spec.id, "path": str(target), "created": True})
        except Exception as e:  # pragma: no cover - defensive
            log.warning("failed to render prompt %s: %s", spec.id, e)
            report.append({"id": spec.id, "path": None, "error": str(e)})
    dtmf_dir = base / "dtmf"
    dtmf_dir.mkdir(parents=True, exist_ok=True)
    for digit in "0123456789*#":
        target = dtmf_dir / f"{digit}.wav"
        if target.exists() and not force:
            continue
        try:
            tones.write_dtmf(digit, target)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("failed to render dtmf %s: %s", digit, e)
    return report

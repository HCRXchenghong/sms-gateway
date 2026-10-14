"""Call summary and minutes generation.

The model is asked for JSON so the console and the open API can show the same
structured minutes. Parsing is deliberately forgiving: a model that answers in
prose still produces a usable summary instead of an empty card, and anything it
could not confirm stays in its own list rather than being promoted to a fact.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["build_summary_prompt", "parse_summary", "render_minutes"]

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

DEFAULT_TEMPLATE = (
    "请给出：通话摘要、关键事实、双方约定、待办事项（含负责人和时间）、"
    "任务是否达成及依据、以及尚未确认的信息。"
)


def _speaker_label(turn: dict) -> str:
    role = str(turn.get("role") or turn.get("speaker") or "").strip().lower()
    if role in ("peer", "them", "remote", "b", "in"):
        return "对方"
    if role in ("me", "self", "local", "a", "out", "agent", "assistant"):
        return "我方"
    return role or "未知"


def build_summary_prompt(
    transcript: Sequence[dict],
    *,
    purpose: Optional[str] = None,
    template: Optional[str] = None,
) -> Dict[str, str]:
    """Build the system and user messages for one summary request."""
    system = (
        "你是通话记录整理助手。根据给出的通话转写生成结构化纪要。\n"
        "严格输出一个 JSON 对象，不要输出 JSON 以外的任何文字，字段如下：\n"
        '{"summary": string, "key_facts": [string], "agreements": [string], '
        '"todos": [{"task": string, "owner": string, "due": string}], '
        '"purpose_achieved": true|false|null, "purpose_evidence": string, '
        '"unconfirmed": [string]}\n'
        "规则：\n"
        "1. 只写转写里有依据的内容，不要推测、不要补全没提到的信息。\n"
        "2. 拿不准或双方没有确认的内容放进 unconfirmed，不要写进 key_facts。\n"
        "3. purpose_achieved 必须能在对话里找到依据，依据写进 purpose_evidence；"
        "没有明确结论时给 null，不要替对方做决定。\n"
        "4. todos 里没有提到负责人或时间时，对应字段留空字符串。\n"
        "5. 全部用与通话内容相同的语言书写，默认中文。"
    )
    requirement = (template or "").strip() or DEFAULT_TEMPLATE
    lines: List[str] = []
    if purpose and purpose.strip():
        lines.append(f"本次通话目的：{purpose.strip()}")
    lines.append(f"纪要要求：{requirement}")
    lines.append("")
    lines.append("通话转写：")
    if not transcript:
        lines.append("（无转写内容）")
    for turn in transcript:
        text = str(turn.get("text") or turn.get("content") or "").strip()
        if not text:
            continue
        original = str(turn.get("original") or "").strip()
        label = _speaker_label(turn)
        if original and original != text:
            lines.append(f"{label}（原文 {original}）：{text}")
        else:
            lines.append(f"{label}：{text}")
    return {"system": system, "user": "\n".join(lines)}


def _coerce_str_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        out = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text") or item.get("task") or item.get("content")
                if text and str(text).strip():
                    out.append(str(text).strip())
        return out
    return []


def _coerce_todos(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if isinstance(raw, str):
        for piece in re.split(r"[\n;；]", raw):
            if piece.strip():
                out.append({"task": piece.strip(), "owner": "", "due": ""})
        return out
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                out.append({"task": item.strip(), "owner": "", "due": ""})
        elif isinstance(item, dict):
            task = str(item.get("task") or item.get("todo") or item.get("content") or "").strip()
            if not task:
                continue
            out.append({
                "task": task,
                "owner": str(item.get("owner") or item.get("responsible") or "").strip(),
                "due": str(item.get("due") or item.get("deadline") or item.get("time") or "").strip(),
            })
    return out


def _coerce_achieved(raw: Any) -> Optional[bool]:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in ("true", "yes", "y", "是", "达成", "已完成", "done"):
        return True
    if text in ("false", "no", "n", "否", "未达成", "失败"):
        return False
    if text in ("null", "none", "", "unknown", "未知"):
        return None
    return None


def parse_summary(raw: Any) -> Dict[str, Any]:
    """Normalise a model reply into the minutes shape the API returns."""
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    data: Optional[dict] = None
    stripped = (text or "").strip()
    if stripped:
        # Models wrap JSON in prose or a code fence often enough to be worth
        # handling rather than failing the whole summary.
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
        candidate = fenced.group(1) if fenced else None
        if candidate is None:
            match = _JSON_BLOCK.search(stripped)
            candidate = match.group(0) if match else None
        if candidate:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    data = parsed
            except (ValueError, TypeError):
                data = None
    if data is None:
        return {
            "summary": stripped,
            "key_facts": [],
            "agreements": [],
            "todos": [],
            "purpose_achieved": None,
            "purpose_evidence": "",
            "unconfirmed": [],
            "parsed": False,
        }
    return {
        "summary": str(data.get("summary") or "").strip(),
        "key_facts": _coerce_str_list(data.get("key_facts")),
        "agreements": _coerce_str_list(data.get("agreements")),
        "todos": _coerce_todos(data.get("todos")),
        "purpose_achieved": _coerce_achieved(data.get("purpose_achieved")),
        "purpose_evidence": str(data.get("purpose_evidence") or "").strip(),
        "unconfirmed": _coerce_str_list(data.get("unconfirmed")),
        "parsed": True,
    }


def render_minutes(minutes: Dict[str, Any]) -> str:
    """Plain-text minutes for the recording announcement log and exports."""
    lines: List[str] = []
    if minutes.get("summary"):
        lines.append(f"摘要：{minutes['summary']}")
    for label, key in (("关键事实", "key_facts"), ("约定事项", "agreements"), ("未确认", "unconfirmed")):
        items = minutes.get(key) or []
        if items:
            lines.append(f"{label}：")
            lines.extend(f"- {item}" for item in items)
    todos = minutes.get("todos") or []
    if todos:
        lines.append("待办：")
        for todo in todos:
            bits = [todo.get("task", "")]
            if todo.get("owner"):
                bits.append(f"负责人 {todo['owner']}")
            if todo.get("due"):
                bits.append(f"时间 {todo['due']}")
            lines.append("- " + "，".join(b for b in bits if b))
    achieved = minutes.get("purpose_achieved")
    if achieved is not None:
        lines.append(f"目的达成：{'是' if achieved else '否'}")
        if minutes.get("purpose_evidence"):
            lines.append(f"依据：{minutes['purpose_evidence']}")
    return "\n".join(lines)

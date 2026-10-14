"""Live-call AI: translation, the call agent, minutes and AI call tasks.

Two audiences hit the same behaviour. The console and the app authenticate
with a session and see every call; a third party authenticates with an API key
and drives its own tasks. Both create the same task row, so an AI call placed
from the app and one placed from someone else's system are audited the same
way.

Task creation is idempotent through "reference": a retried POST returns the
existing task instead of placing a second call. That matters because the caller
has no way to tell whether their first request reached us before the connection
dropped.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field

from . import repo, repo_calls, repo_tasks
from .auth import (
    CurrentApiKey,
    CurrentUser,
    get_current_user,
    require_scope_voice_dial,
    require_scope_voice_read,
)
from .voice.callflow import CallFlowError

router = APIRouter(tags=["callflow"])

MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _flow(request: Request):
    flow = getattr(request.app.state, "flow", None)
    if flow is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"error": {"code": "unavailable", "message": "通话 AI 子系统未就绪"}},
        )
    return flow


def _conflict(exc: CallFlowError) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, {"error": {"code": "call_flow", "message": str(exc)}}
    )


class TranslateBody(BaseModel):
    mode: Optional[str] = Field(default=None, pattern="^(off|subtitle|voice)$")
    cadence: Optional[str] = Field(default=None, pattern="^(sentence|stream)$")
    my_language: Optional[str] = Field(default=None, min_length=2, max_length=16)
    peer_language: Optional[str] = Field(default=None, min_length=2, max_length=16)


class SayBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class AgentBody(BaseModel):
    purpose: str = Field(..., min_length=2, max_length=1000)
    context: Optional[str] = Field(default=None, max_length=4000)
    language: Optional[str] = Field(default=None, max_length=16)
    peer_language: Optional[str] = Field(default=None, max_length=16)
    max_seconds: Optional[int] = Field(default=None, ge=10, le=3600)
    opener: Optional[str] = Field(default=None, max_length=500)


class TaskBody(BaseModel):
    """One AI phone call with a stated purpose."""

    purpose: str = Field(..., min_length=2, max_length=1000)
    number: Optional[str] = Field(default=None, max_length=24)
    direction: str = Field(default="outbound", pattern="^(outbound|inbound)$")
    device_id: Optional[int] = None
    context: Optional[str] = Field(default=None, max_length=4000)
    language: Optional[str] = Field(default=None, max_length=16)
    peer_language: Optional[str] = Field(default=None, max_length=16)
    template: Optional[str] = Field(default=None, max_length=1000)
    max_seconds: Optional[int] = Field(default=None, ge=10, le=3600)
    # Idempotency key. Send the same value twice and only one call is placed.
    reference: Optional[str] = Field(default=None, max_length=120)


# ---------------------------------------------------------------------------
# Live call state
# ---------------------------------------------------------------------------


@router.get("/app/voice/flow")
async def flow_state(request: Request, user: CurrentUser = Depends(get_current_user)):
    """What the gateway is doing on every live call, plus capture health."""
    return _flow(request).state()


@router.get("/app/voice/calls/{call_id}/flow")
async def flow_for_call(
    call_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
):
    flow = _flow(request).flow(call_id)
    if flow is None:
        row = await asyncio.to_thread(repo_calls.get_call, call_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到这通电话")
        return {"call_id": call_id, "ended": True, "call": repo_calls.serialize_call(row)}
    return flow


@router.get("/app/voice/calls/{call_id}/transcript")
async def call_transcript(
    call_id: str, request: Request, user: CurrentUser = Depends(get_current_user), limit: int = 400
):
    lines = _flow(request).transcript(call_id, limit)
    if lines:
        return {"call_id": call_id, "items": lines, "count": len(lines), "live": True}
    row = await asyncio.to_thread(repo_calls.get_call, call_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到这通电话")
    stored = repo_calls.serialize_call(row).get("transcript") or []
    return {"call_id": call_id, "items": stored, "count": len(stored), "live": False}


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


@router.post("/app/voice/calls/{call_id}/translate")
async def start_translation(
    call_id: str, body: TranslateBody, request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Turn live translation on or change it mid-call."""
    try:
        result = await _flow(request).set_translation(
            call_id, mode=body.mode, cadence=body.cadence,
            my_language=body.my_language, peer_language=body.peer_language,
        )
    except CallFlowError as e:
        raise _conflict(e)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request),
        "call.translate", f"{call_id}:{body.mode or ''}", 200,
    )
    return result


@router.delete("/app/voice/calls/{call_id}/translate")
async def stop_translation(
    call_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
):
    try:
        return await _flow(request).stop_translation(call_id)
    except CallFlowError as e:
        raise _conflict(e)


@router.post("/app/voice/calls/{call_id}/say")
async def operator_says(
    call_id: str, body: SayBody, request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Say something to the far end, translated when translation is on."""
    try:
        return await _flow(request).operator_text(call_id, body.text)
    except CallFlowError as e:
        raise _conflict(e)


@router.post("/app/voice/calls/{call_id}/say-audio")
async def operator_speaks(
    call_id: str, request: Request, user: CurrentUser = Depends(get_current_user),
    file: UploadFile = File(...), language: Optional[str] = None,
):
    """The operator speaks into the phone; we recognise and relay it."""
    audio = await file.read()
    if not audio:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传的音频为空")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "音频过大")
    try:
        return await _flow(request).operator_audio(
            call_id, audio, language=language, filename=file.filename or "operator.wav"
        )
    except CallFlowError as e:
        raise _conflict(e)


# ---------------------------------------------------------------------------
# Call agent on a live call
# ---------------------------------------------------------------------------


@router.post("/app/voice/calls/{call_id}/agent")
async def attach_agent(
    call_id: str, body: AgentBody, request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Hand a live call to the AI with a stated purpose."""
    try:
        result = await _flow(request).attach_agent(
            call_id, purpose=body.purpose, context=body.context,
            language=body.language, peer_language=body.peer_language,
            max_seconds=body.max_seconds, opener=body.opener,
        )
    except CallFlowError as e:
        raise _conflict(e)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request),
        "call.agent.attach", call_id, 200,
    )
    return result


@router.post("/app/voice/calls/{call_id}/agent/handover")
async def agent_handover(
    call_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
):
    """Take the call back from the AI without hanging up."""
    try:
        return await _flow(request).handover(call_id)
    except CallFlowError as e:
        raise _conflict(e)


# ---------------------------------------------------------------------------
# Minutes
# ---------------------------------------------------------------------------


@router.get("/app/voice/calls/{call_id}/summary")
async def call_summary(
    call_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
):
    row = await asyncio.to_thread(repo_calls.get_call, call_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到这通电话")
    call = repo_calls.serialize_call(row)
    return {
        "call_id": call_id,
        "summary": call.get("summary"),
        "summary_at": call.get("summary_at"),
        "transcript": call.get("transcript") or [],
    }


@router.post("/app/voice/calls/{call_id}/summary")
async def generate_summary(
    call_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
):
    """Build the minutes now instead of waiting for the call to end."""
    try:
        minutes = await _flow(request).summarize(call_id, force=True)
    except CallFlowError as e:
        raise _conflict(e)
    if minutes is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": {"code": "no_transcript", "message": "这通电话没有可整理的内容"}},
        )
    return {"call_id": call_id, "summary": minutes}


# ---------------------------------------------------------------------------
# AI call tasks: the app and the console
# ---------------------------------------------------------------------------


async def _create_task(request: Request, body: TaskBody, *, created_by: str,
                       owner_user_id: Optional[int], allowed_devices=None) -> dict:
    device_id = body.device_id
    if device_id is not None and allowed_devices is not None and device_id not in allowed_devices:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "这个 API key 不允许使用该设备")
    try:
        result = await _flow(request).submit_task(
            purpose=body.purpose, number=body.number, direction=body.direction,
            device_id=device_id, context=body.context, language=body.language,
            peer_language=body.peer_language, template=body.template,
            max_seconds=body.max_seconds, reference=body.reference,
            created_by=created_by, owner_user_id=owner_user_id,
        )
    except CallFlowError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, {"error": {"code": "invalid_task", "message": str(e)}}
        )
    return result


@router.post("/app/ai-call/tasks", status_code=201)
async def create_ai_call_task(
    body: TaskBody, request: Request, user: CurrentUser = Depends(get_current_user)
):
    result = await _create_task(
        request, body, created_by=f"user:{user.username}", owner_user_id=user.id
    )
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request),
        "ai_call.task.create", str(result["task"].get("id")), 200,
    )
    return result


@router.get("/app/ai-call/tasks")
async def list_ai_call_tasks(
    request: Request, user: CurrentUser = Depends(get_current_user),
    limit: int = 50, task_status: Optional[str] = None,
):
    rows = await asyncio.to_thread(repo_tasks.list_tasks, min(max(limit, 1), 200), task_status)
    live = _flow(request).state()
    return {"items": rows, "count": len(rows), "running_tasks": live["running_tasks"]}


@router.get("/app/ai-call/tasks/{task_id}")
async def get_ai_call_task(
    task_id: int, request: Request, user: CurrentUser = Depends(get_current_user)
):
    task = await asyncio.to_thread(repo_tasks.get_task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    flow = _flow(request).flow(task["call_id"]) if task.get("call_id") else None
    return {"task": task, "flow": flow}


@router.post("/app/ai-call/tasks/{task_id}/cancel")
async def cancel_ai_call_task(
    task_id: int, request: Request, user: CurrentUser = Depends(get_current_user)
):
    try:
        result = await _flow(request).cancel_task(task_id)
    except CallFlowError as e:
        raise _conflict(e)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request),
        "ai_call.task.cancel", str(task_id), 200,
    )
    return result


# ---------------------------------------------------------------------------
# AI call tasks: open API
# ---------------------------------------------------------------------------


@router.post("/api/v1/ai-call-tasks", status_code=201)
async def api_create_ai_call_task(
    body: TaskBody, request: Request, key: CurrentApiKey = Depends(require_scope_voice_dial)
):
    """Place an AI call from an outside system.

    Send "reference" with a value unique to your request. A retry with the same
    reference returns the original task instead of dialling again.
    """
    result = await _create_task(
        request, body, created_by=f"api:{key.label}", owner_user_id=None,
        allowed_devices=key.allowed_device_ids,
    )
    await asyncio.to_thread(
        repo.audit, "api_key", key.id, key.prefix, _client_ip(request),
        "ai_call.task.create", str(result["task"].get("id")), 200,
    )
    return result


@router.get("/api/v1/ai-call-tasks")
async def api_list_ai_call_tasks(
    request: Request, limit: int = 50, task_status: Optional[str] = None,
    key: CurrentApiKey = Depends(require_scope_voice_read),
):
    rows = await asyncio.to_thread(repo_tasks.list_tasks, min(max(limit, 1), 200), task_status)
    return {"items": rows, "count": len(rows)}


@router.get("/api/v1/ai-call-tasks/{task_id}")
async def api_get_ai_call_task(
    task_id: int, request: Request, key: CurrentApiKey = Depends(require_scope_voice_read)
):
    task = await asyncio.to_thread(repo_tasks.get_task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return {"task": task}


@router.post("/api/v1/ai-call-tasks/{task_id}/cancel")
async def api_cancel_ai_call_task(
    task_id: int, request: Request, key: CurrentApiKey = Depends(require_scope_voice_dial)
):
    try:
        result = await _flow(request).cancel_task(task_id)
    except CallFlowError as e:
        raise _conflict(e)
    await asyncio.to_thread(
        repo.audit, "api_key", key.id, key.prefix, _client_ip(request),
        "ai_call.task.cancel", str(task_id), 200,
    )
    return result

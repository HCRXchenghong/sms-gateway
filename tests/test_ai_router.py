"""Failover and circuit breaking for the per-capability endpoint pool.

The router is the piece that has to keep a live call alive when a vendor times
out or rate limits, so the tests drive it with providers that fail on purpose.
"""
from __future__ import annotations

import pytest

from app import repo_ai
from app.ai.engine import AIEngine, AIError, build_translation_prompt
from app.ai.providers import AIProviderError, BaseProvider, ChatMessage
from app.ai.registry import CapabilityRouter, Endpoint, NoEndpointConfigured, NoStreamEndpoint
from app.ai.summary import build_summary_prompt, parse_summary, render_minutes


class FakeProvider(BaseProvider):
    """Fails a configured number of times, then answers."""

    def __init__(self, fail_times: int = 0, error: str = "boom", reply: str = "ok"):
        self.fail_times = fail_times
        self.error = error
        self.reply = reply
        self.calls: list[dict] = []
        self.closed = False

    async def chat(self, messages, **kwargs):
        self.calls.append({"channel": "chat", "kwargs": kwargs,
                           "messages": [m.to_dict() for m in messages]})
        if len(self.calls) <= self.fail_times:
            raise AIProviderError(self.error)
        return self.reply

    async def transcribe(self, audio, *, filename="audio.wav", language=None):
        self.calls.append({"channel": "asr", "language": language})
        if len(self.calls) <= self.fail_times:
            raise AIProviderError(self.error)
        return self.reply

    async def synthesize(self, text, **kwargs):
        self.calls.append({"channel": "tts", "text": text, "kwargs": kwargs})
        if len(self.calls) <= self.fail_times:
            raise AIProviderError(self.error)
        return b"RIFFfake"

    async def close(self):
        self.closed = True


@pytest.fixture()
def pool(client):
    """A clean endpoint table plus the rows each test creates."""
    for row in repo_ai.list_endpoints():
        repo_ai.delete_endpoint(row["id"])
    created: list[int] = []

    def add(capability, name, *, priority=100, stream=False, enabled=True, model="m"):
        endpoint_id = repo_ai.create_endpoint(
            capability=capability, name=name, kind="api",
            base_url="https://example.invalid/v1", api_key="k", model=model,
            priority=priority, enabled=enabled, supports_stream=stream,
        )
        created.append(endpoint_id)
        return endpoint_id

    yield add, created

    for endpoint_id in created:
        repo_ai.delete_endpoint(endpoint_id)


def _router_with(providers: dict, **kwargs) -> CapabilityRouter:
    def factory(endpoint: Endpoint) -> BaseProvider:
        provider = providers.get(endpoint.id)
        if provider is None:
            raise AssertionError(f"no fake provider for endpoint {endpoint.id}")
        return provider

    return CapabilityRouter(provider_factory=factory, **kwargs)


@pytest.mark.asyncio
async def test_first_healthy_endpoint_wins(pool):
    add, _ = pool
    first = add("translate", "主")
    second = add("translate", "备")
    a, b = FakeProvider(reply="A"), FakeProvider(reply="B")
    router = _router_with({first: a, second: b})
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.chat([ChatMessage("user", "hi")])

    assert await router.run("translate", work) == "A"
    assert b.calls == []


@pytest.mark.asyncio
async def test_failure_falls_over_to_the_next_endpoint(pool):
    add, _ = pool
    first = add("translate", "主")
    second = add("translate", "备")
    a = FakeProvider(fail_times=99, error="HTTP 429: rate limited")
    b = FakeProvider(reply="备用译文")
    router = _router_with({first: a, second: b})
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.chat([ChatMessage("user", "hi")])

    assert await router.run("translate", work) == "备用译文"
    assert router.last_used("translate") == "备"


@pytest.mark.asyncio
async def test_priority_decides_the_order(pool):
    add, _ = pool
    backup = add("translate", "备用", priority=200)
    primary = add("translate", "主用", priority=10)
    a, b = FakeProvider(reply="主"), FakeProvider(reply="备")
    router = _router_with({primary: a, backup: b})
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.chat([ChatMessage("user", "hi")])

    assert await router.run("translate", work) == "主"


@pytest.mark.asyncio
async def test_circuit_opens_and_skips_a_dead_endpoint(pool):
    add, _ = pool
    first = add("asr", "主")
    second = add("asr", "备")
    a = FakeProvider(fail_times=99)
    b = FakeProvider(reply="备")
    router = _router_with({first: a, second: b}, failure_threshold=2, cooldown_seconds=30.0)
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.chat([ChatMessage("user", "hi")])

    await router.run("asr", work)
    await router.run("asr", work)
    # Two failures opened the circuit, so the dead endpoint is not tried again.
    before = len(a.calls)
    assert await router.run("asr", work) == "备"
    assert len(a.calls) == before
    assert router.status("asr")["capabilities"]["asr"]["endpoints"][0]["circuit"] == "open"


@pytest.mark.asyncio
async def test_circuit_half_opens_and_closes_on_recovery(pool):
    add, _ = pool
    first = add("asr", "主")
    second = add("asr", "备")
    a = FakeProvider(fail_times=1)
    b = FakeProvider(reply="备")
    router = _router_with({first: a, second: b}, failure_threshold=1, cooldown_seconds=0.0)
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.chat([ChatMessage("user", "hi")])

    await router.run("asr", work)
    # Cooldown is zero, so the next request probes the recovered endpoint and
    # the pool goes back to preferring it.
    assert await router.run("asr", work) == "ok"
    assert router.status("asr")["capabilities"]["asr"]["endpoints"][0]["circuit"] == "closed"


@pytest.mark.asyncio
async def test_every_endpoint_down_raises_a_clear_error(pool):
    add, _ = pool
    first = add("tts", "主")
    second = add("tts", "备")
    router = _router_with({first: FakeProvider(fail_times=99, error="超时"),
                           second: FakeProvider(fail_times=99, error="500")})
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.synthesize("你好")

    with pytest.raises(AIProviderError) as info:
        await router.run("tts", work)
    assert "所有服务都不可用" in str(info.value)
    assert "超时" in str(info.value)


@pytest.mark.asyncio
async def test_failover_can_be_turned_off(pool):
    add, _ = pool
    first = add("tts", "主")
    second = add("tts", "备")
    a, b = FakeProvider(fail_times=99), FakeProvider(reply="备")
    router = _router_with({first: a, second: b}, failover_enabled=False)
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.synthesize("你好")

    with pytest.raises(AIProviderError):
        await router.run("tts", work)
    assert b.calls == []


@pytest.mark.asyncio
async def test_stream_requirement_picks_a_capable_endpoint(pool):
    add, _ = pool
    plain = add("translate", "短句", stream=False)
    streaming = add("translate", "流式", stream=True)
    a, b = FakeProvider(reply="短句"), FakeProvider(reply="流式")
    router = _router_with({plain: a, streaming: b})
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.chat([ChatMessage("user", "hi")])

    assert await router.run("translate", work, require_stream=True) == "流式"
    assert a.calls == []


@pytest.mark.asyncio
async def test_stream_requirement_reports_when_nothing_can_stream(pool):
    add, _ = pool
    plain = add("translate", "短句", stream=False)
    router = _router_with({plain: FakeProvider()})
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.chat([ChatMessage("user", "hi")])

    with pytest.raises(NoStreamEndpoint):
        await router.run("translate", work, require_stream=True)


@pytest.mark.asyncio
async def test_disabled_endpoint_is_skipped(pool):
    add, _ = pool
    off = add("translate", "停用", enabled=False)
    on = add("translate", "启用")
    a, b = FakeProvider(reply="停用"), FakeProvider(reply="启用")
    router = _router_with({off: a, on: b})
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.chat([ChatMessage("user", "hi")])

    assert await router.run("translate", work) == "启用"
    assert a.calls == []


@pytest.mark.asyncio
async def test_empty_pool_falls_back_to_the_offline_provider(pool, monkeypatch):
    engine = AIEngine(router=_router_with({}))
    await engine.router.refresh()
    engine._kind = "mock"
    reply = await engine.chat("你好")
    assert "模拟回复" in reply


@pytest.mark.asyncio
async def test_empty_pool_raises_for_a_pool_only_call(pool):
    router = _router_with({})
    await router.refresh()

    async def work(provider, endpoint):
        return "never"

    with pytest.raises(NoEndpointConfigured):
        await router.run("summary", work)


@pytest.mark.asyncio
async def test_call_log_records_outcome_and_latency(pool):
    add, _ = pool
    first = add("translate", "主")
    second = add("translate", "备")
    router = _router_with({first: FakeProvider(fail_times=99, error="429"),
                           second: FakeProvider(reply="好")})
    await router.refresh()

    async def work(provider, endpoint):
        return await provider.chat([ChatMessage("user", "hi")])

    await router.run("translate", work, call_id="call-1")
    rows = repo_ai.recent_calls(20, capability="translate")
    assert any(r["ok"] and r["endpoint_name"] == "备" and r["call_id"] == "call-1" for r in rows)
    assert any(not r["ok"] and r["endpoint_name"] == "主" and "429" in (r["error"] or "") for r in rows)
    stats = repo_ai.endpoint_stats(hours=1)
    assert stats[str(second)]["calls"] >= 1


def test_translation_prompt_states_the_direction_and_bans_commentary():
    prompt = build_translation_prompt(source="en", target="zh", glossary=["gateway=网关"])
    assert "中文" in prompt
    assert "英文" in prompt
    assert "只输出译文本身" in prompt
    assert "gateway=网关" in prompt


def test_translation_prompt_handles_an_unknown_language_code():
    prompt = build_translation_prompt(target="sv")
    assert "sv" in prompt


def test_parse_summary_reads_structured_json():
    raw = (
        '```json\n{"summary": "确认了营业时间", "key_facts": ["门店周一到周五营业"],'
        ' "agreements": ["对方会回电"], "todos": [{"task": "发资料", "owner": "我", "due": "明天"}],'
        ' "purpose_achieved": true, "purpose_evidence": "对方口头确认", "unconfirmed": ["具体价格"]}\n```'
    )
    out = parse_summary(raw)
    assert out["parsed"] is True
    assert out["summary"] == "确认了营业时间"
    assert out["key_facts"] == ["门店周一到周五营业"]
    assert out["todos"] == [{"task": "发资料", "owner": "我", "due": "明天"}]
    assert out["purpose_achieved"] is True
    assert out["unconfirmed"] == ["具体价格"]


def test_parse_summary_keeps_prose_answers_usable():
    out = parse_summary("这通电话对方没有接听。")
    assert out["parsed"] is False
    assert out["summary"] == "这通电话对方没有接听。"
    assert out["purpose_achieved"] is None


def test_parse_summary_tolerates_odd_shapes():
    out = parse_summary('{"summary": "s", "key_facts": "一条", "todos": "打电话; 发邮件",'
                        ' "purpose_achieved": "是"}')
    assert out["key_facts"] == ["一条"]
    assert [t["task"] for t in out["todos"]] == ["打电话", "发邮件"]
    assert out["purpose_achieved"] is True


def test_summary_prompt_separates_unconfirmed_from_facts():
    payload = build_summary_prompt(
        [
            {"role": "me", "text": "请问今天营业吗", "original": "Are you open today"},
            {"role": "peer", "text": "营业到晚上九点"},
        ],
        purpose="确认营业时间",
    )
    assert "unconfirmed" in payload["system"]
    assert "不要推测" in payload["system"]
    assert "本次通话目的：确认营业时间" in payload["user"]
    assert "我方（原文 Are you open today）：请问今天营业吗" in payload["user"]
    assert "对方：营业到晚上九点" in payload["user"]


def test_render_minutes_is_readable():
    text = render_minutes({
        "summary": "确认营业时间",
        "key_facts": ["营业到九点"],
        "agreements": [],
        "todos": [{"task": "发资料", "owner": "我", "due": "明天"}],
        "purpose_achieved": True,
        "purpose_evidence": "对方确认",
        "unconfirmed": ["价格"],
    })
    assert "摘要：确认营业时间" in text
    assert "- 发资料，负责人 我，时间 明天" in text
    assert "目的达成：是" in text
    assert "未确认：" in text

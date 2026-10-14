"""Capability router: an ordered pool of model endpoints with circuit breaking.

Each capability (asr, translate, tts, chat, summary) has its own pool. A
request walks the pool in priority order; an endpoint that times out, rate
limits or returns a server error is marked down for a cooldown and the request
continues on the next one, so a single flaky vendor cannot break a live call.

The router holds no vendor-specific knowledge. Callers pass a coroutine that
does the actual work with whichever provider it is handed, which keeps
failover identical across capabilities and makes the whole thing testable with
a fake provider.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from .. import repo_ai
from ..config import settings
from .providers import AIProviderError, BaseProvider, MockProvider, OpenAICompatibleProvider

log = logging.getLogger(__name__)

__all__ = [
    "Endpoint",
    "NoEndpointConfigured",
    "NoStreamEndpoint",
    "CapabilityRouter",
    "build_endpoint_provider",
    "router",
]


class NoEndpointConfigured(RuntimeError):
    """No enabled endpoint exists for this capability."""


class NoStreamEndpoint(RuntimeError):
    """Every reachable endpoint lacks streaming support.

    Raised instead of silently downgrading, so the caller decides whether to
    fall back to sentence cadence or stop translating.
    """


@dataclass
class Endpoint:
    id: int
    capability: str
    name: str
    kind: str = "api"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    params: dict = field(default_factory=dict)
    priority: int = 100
    enabled: bool = True
    supports_stream: bool = False
    timeout_seconds: float = 30.0
    updated_at: int = 0

    @classmethod
    def from_dict(cls, raw: dict) -> "Endpoint":
        return cls(
            id=int(raw["id"]),
            capability=raw["capability"],
            name=raw["name"],
            kind=raw.get("kind") or "api",
            base_url=raw.get("base_url") or "",
            api_key=raw.get("api_key") or "",
            model=raw.get("model") or "",
            params=dict(raw.get("params") or {}),
            priority=int(raw.get("priority", 100)),
            enabled=bool(raw.get("enabled", True)),
            supports_stream=bool(raw.get("supports_stream", False)),
            timeout_seconds=float(raw.get("timeout_seconds") or settings.ai_request_timeout_seconds),
            updated_at=int(raw.get("updated_at") or 0),
        )


@dataclass
class _Circuit:
    """Per-endpoint health. Deliberately plain data so status() can dump it."""

    consecutive_failures: int = 0
    opened_at: Optional[float] = None
    probing: bool = False
    calls: int = 0
    failures: int = 0
    total_ms: float = 0.0
    last_ok_at: Optional[float] = None
    last_error: Optional[str] = None
    last_latency_ms: Optional[int] = None

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def state(self, cooldown: float) -> str:
        if self.opened_at is None:
            return "closed"
        if (time.monotonic() - self.opened_at) >= cooldown:
            return "half_open"
        return "open"


ProviderFactory = Callable[[Endpoint], BaseProvider]
Work = Callable[[BaseProvider, Endpoint], Awaitable[object]]


def build_endpoint_provider(endpoint: Endpoint) -> BaseProvider:
    """Build the provider client for one endpoint row.

    Exposed so the console's connection test can probe a single endpoint
    without going through the pool or its circuit breaker.
    """
    if not endpoint.base_url:
        # No URL means nothing to dial; the offline mock keeps the feature
        # exercisable instead of failing the call outright.
        return MockProvider()
    return OpenAICompatibleProvider(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        timeout=endpoint.timeout_seconds,
        chat_model=endpoint.model or "gpt-4o-mini",
        asr_model=endpoint.model or "whisper-1",
        tts_model=endpoint.model or "tts-1",
        tts_voice=str(endpoint.params.get("voice") or settings.ai_tts_voice),
        tts_format=str(endpoint.params.get("format") or settings.ai_tts_format),
        extra_headers=endpoint.params.get("headers") or None,
    )


class CapabilityRouter:
    def __init__(
        self,
        *,
        provider_factory: Optional[ProviderFactory] = None,
        failure_threshold: Optional[int] = None,
        cooldown_seconds: Optional[float] = None,
        failover_enabled: Optional[bool] = None,
    ) -> None:
        self._factory = provider_factory or build_endpoint_provider
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._failover = failover_enabled
        self._endpoints: Dict[str, List[Endpoint]] = {}
        self._circuits: Dict[int, _Circuit] = {}
        self._providers: Dict[int, BaseProvider] = {}
        # endpoint id -> updated_at of the row the cached provider was built
        # from, so an edited endpoint cannot keep serving stale credentials.
        self._provider_stamp: Dict[int, int] = {}
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()
        self._last_used: Dict[str, Optional[str]] = {}

    # -- configuration --------------------------------------------------

    @property
    def failure_threshold(self) -> int:
        if self._failure_threshold is not None:
            return max(1, int(self._failure_threshold))
        return max(1, int(settings.ai_breaker_failure_threshold))

    @property
    def cooldown(self) -> float:
        if self._cooldown is not None:
            return max(0.0, float(self._cooldown))
        return max(0.0, float(settings.ai_breaker_cooldown_seconds))

    @property
    def failover_enabled(self) -> bool:
        if self._failover is not None:
            return bool(self._failover)
        return bool(settings.ai_failover_enabled)

    async def refresh(self) -> None:
        """Reload the pool from the database and drop providers that changed."""
        rows = await asyncio.to_thread(repo_ai.list_endpoints, None, with_secret=True)
        by_capability: Dict[str, List[Endpoint]] = {}
        seen: set[int] = set()
        for raw in rows:
            endpoint = Endpoint.from_dict(raw)
            seen.add(endpoint.id)
            by_capability.setdefault(endpoint.capability, []).append(endpoint)
        for capability in by_capability:
            by_capability[capability].sort(key=lambda e: (e.priority, e.id))

        # Close providers whose row disappeared or changed, and forget circuits
        # for deleted endpoints so a reused id cannot inherit a stale state.
        stale = [pid for pid in self._providers if pid not in seen]
        async with self._lock:
            for pid in stale:
                provider = self._providers.pop(pid, None)
                self._circuits.pop(pid, None)
                if provider is not None:
                    try:
                        await provider.close()
                    except Exception:
                        pass
            for endpoint in (e for group in by_capability.values() for e in group):
                cached_at = self._provider_stamp.get(endpoint.id)
                if cached_at is not None and cached_at != endpoint.updated_at:
                    provider = self._providers.pop(endpoint.id, None)
                    self._circuits.pop(endpoint.id, None)
                    if provider is not None:
                        try:
                            await provider.close()
                        except Exception:
                            pass
            self._endpoints = by_capability
            self._loaded_at = time.monotonic()

    def endpoints(self, capability: str) -> List[Endpoint]:
        return list(self._endpoints.get(capability, []))

    def _circuit(self, endpoint_id: int) -> _Circuit:
        circuit = self._circuits.get(endpoint_id)
        if circuit is None:
            circuit = _Circuit()
            self._circuits[endpoint_id] = circuit
        return circuit

    def _provider_for(self, endpoint: Endpoint) -> BaseProvider:
        provider = self._providers.get(endpoint.id)
        if provider is None:
            provider = self._factory(endpoint)
            self._providers[endpoint.id] = provider
            self._provider_stamp[endpoint.id] = endpoint.updated_at
        return provider

    def _candidates(self, capability: str, *, require_stream: bool) -> List[Endpoint]:
        pool = [e for e in self._endpoints.get(capability, []) if e.enabled]
        if require_stream:
            pool = [e for e in pool if e.supports_stream]
        available: List[Endpoint] = []
        for endpoint in pool:
            circuit = self._circuit(endpoint.id)
            state = circuit.state(self.cooldown)
            if state == "closed":
                available.append(endpoint)
            elif state == "half_open" and not circuit.probing:
                # Let exactly one request through to test recovery.
                available.append(endpoint)
        return available

    # -- execution ------------------------------------------------------

    async def run(
        self,
        capability: str,
        work: Work,
        *,
        require_stream: bool = False,
        call_id: Optional[str] = None,
    ) -> object:
        """Run work against the first endpoint that succeeds.

        Raises NoEndpointConfigured when the pool is empty, NoStreamEndpoint
        when nothing reachable can stream, and AIProviderError when every
        candidate failed.
        """
        if not self._endpoints:
            await self.refresh()
        if require_stream and not any(
            e.enabled and e.supports_stream for e in self._endpoints.get(capability, [])
        ):
            raise NoStreamEndpoint(f"capability {capability} has no streaming endpoint")
        if not any(e.enabled for e in self._endpoints.get(capability, [])):
            raise NoEndpointConfigured(f"capability {capability} has no enabled endpoint")

        candidates = self._candidates(capability, require_stream=require_stream)
        if not candidates:
            # Everything is cooling down. Rather than fail the call, retry the
            # endpoint closest to recovery; the alternative is a hard outage
            # for the whole cooldown even though the vendor may already be back.
            pool = [e for e in self._endpoints.get(capability, []) if e.enabled]
            if not pool:
                raise NoEndpointConfigured(f"capability {capability} has no enabled endpoint")
            candidates = pool[:1]
            log.warning("all %s endpoints are open, forcing a probe on %s", capability, candidates[0].name)

        errors: List[str] = []
        last_index = len(candidates) - 1
        for index, endpoint in enumerate(candidates):
            circuit = self._circuit(endpoint.id)
            half_open = circuit.is_open
            if half_open:
                circuit.probing = True
            started = time.monotonic()
            try:
                provider = self._provider_for(endpoint)
                result = await work(provider, endpoint)
            except asyncio.CancelledError:
                circuit.probing = False
                raise
            except Exception as exc:  # noqa: BLE001 - any failure must fail over
                elapsed_ms = int((time.monotonic() - started) * 1000)
                circuit.calls += 1
                circuit.failures += 1
                circuit.consecutive_failures += 1
                circuit.last_error = str(exc)[:300]
                circuit.last_latency_ms = elapsed_ms
                circuit.probing = False
                if circuit.consecutive_failures >= self.failure_threshold:
                    circuit.opened_at = time.monotonic()
                await self._record(
                    capability, endpoint, ok=False, latency_ms=elapsed_ms,
                    error=str(exc), call_id=call_id,
                )
                errors.append(f"{endpoint.name}: {exc}")
                if not self.failover_enabled or index >= last_index:
                    break
                log.warning(
                    "%s endpoint %s failed (%s), falling over",
                    capability, endpoint.name, str(exc)[:160],
                )
                continue
            else:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                circuit.calls += 1
                circuit.total_ms += elapsed_ms
                circuit.consecutive_failures = 0
                circuit.opened_at = None
                circuit.probing = False
                circuit.last_ok_at = time.monotonic()
                circuit.last_latency_ms = elapsed_ms
                circuit.last_error = None
                await self._record(
                    capability, endpoint, ok=True, latency_ms=elapsed_ms, call_id=call_id
                )
                self._last_used[capability] = endpoint.name
                return result

        await self._record(capability, None, ok=False, error="; ".join(errors)[:400], call_id=call_id,
                           endpoint_name="(all)")
        raise AIProviderError("所有服务都不可用: " + "; ".join(errors)[:400])

    async def _record(
        self,
        capability: str,
        endpoint: Optional[Endpoint],
        *,
        ok: bool,
        latency_ms: Optional[int] = None,
        error: Optional[str] = None,
        call_id: Optional[str] = None,
        endpoint_name: Optional[str] = None,
    ) -> None:
        try:
            await asyncio.to_thread(
                repo_ai.log_call,
                capability=capability,
                endpoint_id=endpoint.id if endpoint else None,
                endpoint_name=endpoint_name or (endpoint.name if endpoint else ""),
                ok=ok,
                latency_ms=latency_ms,
                error=error,
                call_id=call_id,
            )
        except Exception as exc:  # logging must never break a call
            log.debug("ai call log write failed: %s", exc)

    # -- introspection --------------------------------------------------

    def last_used(self, capability: str) -> Optional[str]:
        return self._last_used.get(capability)

    def status(self, capability: Optional[str] = None) -> dict:
        capabilities = [capability] if capability else sorted(self._endpoints)
        out: dict = {
            "failover_enabled": self.failover_enabled,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown,
            "capabilities": {},
        }
        for cap in capabilities:
            entries = []
            for endpoint in self._endpoints.get(cap, []):
                circuit = self._circuit(endpoint.id)
                entries.append({
                    "id": endpoint.id,
                    "name": endpoint.name,
                    "kind": endpoint.kind,
                    "model": endpoint.model,
                    "base_url": endpoint.base_url,
                    "priority": endpoint.priority,
                    "enabled": endpoint.enabled,
                    "supports_stream": endpoint.supports_stream,
                    "circuit": circuit.state(self.cooldown),
                    "consecutive_failures": circuit.consecutive_failures,
                    "calls": circuit.calls,
                    "failures": circuit.failures,
                    "error_rate": round(circuit.failures / circuit.calls, 4) if circuit.calls else 0.0,
                    "avg_latency_ms": int(circuit.total_ms / max(1, circuit.calls - circuit.failures))
                    if circuit.calls > circuit.failures else 0,
                    "last_latency_ms": circuit.last_latency_ms,
                    "last_error": circuit.last_error,
                    "retry_in_seconds": max(
                        0.0, round(self.cooldown - (time.monotonic() - (circuit.opened_at or 0.0)), 1)
                    ) if circuit.opened_at else 0.0,
                })
            out["capabilities"][cap] = {
                "endpoints": entries,
                "last_used": self._last_used.get(cap),
                "available": bool(self._candidates(cap, require_stream=False)),
            }
        return out

    def reset_circuit(self, endpoint_id: Optional[int] = None) -> None:
        """Clear breaker state, for the console's manual retry button."""
        if endpoint_id is None:
            for circuit in self._circuits.values():
                circuit.consecutive_failures = 0
                circuit.opened_at = None
                circuit.probing = False
            return
        circuit = self._circuits.get(int(endpoint_id))
        if circuit:
            circuit.consecutive_failures = 0
            circuit.opened_at = None
            circuit.probing = False

    async def close(self) -> None:
        async with self._lock:
            for provider in self._providers.values():
                try:
                    await provider.close()
                except Exception:
                    pass
            self._providers.clear()
            self._provider_stamp.clear()


router = CapabilityRouter()

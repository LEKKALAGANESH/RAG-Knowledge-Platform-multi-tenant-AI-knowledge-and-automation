"""Provider adapters against mocked HTTP (real SDK code paths: streaming, retries, errors),
fallback, registry, citation filter and context packing. No network, no credentials."""

import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from decimal import Decimal

import anthropic
import httpx2
import openai
import pytest

from app.llm import providers, registry
from app.llm.providers import (
    AnthropicProvider,
    ChatMessage,
    Delta,
    LLMError,
    OpenAICompatProvider,
    Usage,
    classify,
    stream_with_fallback,
)
from app.llm.usage import usage_cost
from app.rag.answer import pack_context
from app.rag.citations import CitationFilter
from app.retrieval.search import Hit

MSGS: list[ChatMessage] = [{"role": "user", "content": "hi"}]
Handler = Callable[[httpx2.Request], httpx2.Response]


def _sse(events: list[dict[str, object]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def anthropic_body(
    text: str,
    stop: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    final_usage: dict[str, object] | None = None,
) -> bytes:
    usage = {"input_tokens": 12, "output_tokens": 1}
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": model}
    return _sse(
        [
            {
                "type": "message_start",
                "message": {**message, "content": [], "stop_reason": None, "usage": usage},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop},
                "usage": final_usage or {"output_tokens": 7},
            },
            {"type": "message_stop"},
        ]
    )


def _client(handler: Handler, seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def record(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return handler(req)

    return anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(record))


SSE_HEADERS = {"content-type": "text/event-stream"}
NO_WAIT = {"retry-after-ms": "0"}  # SDK honors this, so retry tests don't sleep


async def collect(it: AsyncIterator[Delta | Usage]) -> list[Delta | Usage]:
    return [e async for e in it]


async def test_anthropic_streams_text_then_usage_with_server_fallback_opt_in() -> None:
    seen: list[httpx2.Request] = []
    http = _client(
        lambda r: httpx2.Response(200, headers=SSE_HEADERS, content=anthropic_body("Hello")), seen
    )
    p = AnthropicProvider("claude-sonnet-5-5", "test-key", http_client=http)
    events = await collect(p.stream(MSGS, "sys", 1000))
    assert events == [Delta("Hello"), Usage("anthropic", "claude-sonnet-5-5", 12, 7)]
    body = json.loads(seen[0].content)
    assert body["fallbacks"] == "default" and body["output_config"] == {"effort": "low"}
    assert body["system"] == "sys" and "temperature" not in body
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]


async def test_anthropic_haiku_gets_no_server_fallback() -> None:
    seen: list[httpx2.Request] = []
    body = anthropic_body("q", model="claude-haiku-5-5")
    http = _client(lambda r: httpx2.Response(200, headers=SSE_HEADERS, content=body), seen)
    await collect(
        AnthropicProvider("claude-haiku-5-5", "k", http_client=http).stream(MSGS, "s", 10)
    )
    assert "fallbacks" not in json.loads(seen[0].content)


async def test_anthropic_retries_5xx_then_succeeds() -> None:
    seen: list[httpx2.Request] = []

    def handler(r: httpx2.Request) -> httpx2.Response:
        if len(seen) == 1:
            return httpx2.Response(529, headers=NO_WAIT, json={"type": "error"})
        return httpx2.Response(200, headers=SSE_HEADERS, content=anthropic_body("ok"))

    p = AnthropicProvider(
        "claude-sonnet-5-5", "k", max_retries=2, http_client=_client(handler, seen)
    )
    assert (await collect(p.stream(MSGS, "s", 10)))[0] == Delta("ok")
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "llm_auth_failed", False),
        (429, "llm_rate_limited", True),
        (404, "llm_model_unavailable", True),
        (500, "llm_unavailable", True),
        (400, "llm_bad_request", False),
    ],
)
async def test_anthropic_errors_map_to_safe_messages(
    status: int, code: str, retryable: bool
) -> None:
    raw = "SECRET-PROVIDER-DETAIL invalid x-api-key"
    seen: list[httpx2.Request] = []
    handler: Handler = lambda r: httpx2.Response(  # noqa: E731
        status, headers=NO_WAIT, json={"type": "error", "error": {"type": "x", "message": raw}}
    )
    p = AnthropicProvider(
        "claude-sonnet-5-5", "k", max_retries=1, http_client=_client(handler, seen)
    )
    with pytest.raises(LLMError) as err:
        await collect(p.stream(MSGS, "s", 10))
    assert (err.value.code, err.value.retryable) == (code, retryable)
    assert raw not in err.value.message
    assert len(seen) == (2 if status in (429, 500) else 1)  # SDK retried only retryable statuses


async def test_anthropic_refusal_is_an_error_not_an_answer() -> None:
    body = anthropic_body("", stop="refusal")
    http = _client(lambda r: httpx2.Response(200, headers=SSE_HEADERS, content=body), [])
    with pytest.raises(LLMError, match="declined") as err:
        await collect(
            AnthropicProvider("claude-sonnet-5-5", "k", http_client=http).stream(MSGS, "s", 10)
        )
    assert err.value.code == "llm_refused"
    assert err.value.usage == Usage("anthropic", "claude-sonnet-5-5", 12, 7)  # billed anyway


async def test_refusal_of_every_fallback_model_carries_all_billed_attempts() -> None:
    hops = [
        _hop("message", "claude-sonnet-5-5", 100, 20),
        _hop("fallback_message", "claude-sonnet-5", 120, 3),
    ]
    body = anthropic_body(
        "",
        stop="refusal",
        model="claude-sonnet-5",
        final_usage={"input_tokens": 120, "output_tokens": 3, "iterations": hops},
    )
    http = _client(lambda r: httpx2.Response(200, headers=SSE_HEADERS, content=body), [])
    with pytest.raises(LLMError) as err:
        await collect(
            AnthropicProvider("claude-sonnet-5-5", "k", http_client=http).stream(MSGS, "s", 10)
        )
    u = err.value.usage
    assert u is not None and (u.model, u.in_tok, u.out_tok) == ("claude-sonnet-5", 220, 23)
    assert [h[0] for h in u.attempts] == ["claude-sonnet-5-5", "claude-sonnet-5"]


def _openai_body() -> bytes:
    def chunk(delta: dict[str, object], usage: dict[str, int] | None = None) -> str:
        choices = [{"index": 0, "delta": delta, "finish_reason": None}] if delta else []
        data = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "llama3"}
        return f"data: {json.dumps({**data, 'choices': choices, 'usage': usage})}\n\n"

    return (
        chunk({"role": "assistant", "content": "Hel"})
        + chunk({"content": "lo"})
        + chunk({}, {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11})
        + "data: [DONE]\n\n"
    ).encode()


async def test_openai_compat_streams_with_system_prompt_and_usage() -> None:
    seen: list[httpx2.Request] = []
    handler: Handler = lambda r: httpx2.Response(200, headers=SSE_HEADERS, content=_openai_body())  # noqa: E731
    p = OpenAICompatProvider(
        "llama3", "unused", "http://localhost:11434/v1", http_client=_client(handler, seen)
    )
    events = await collect(p.stream(MSGS, "sys", 50, temperature=0.1))
    assert events == [Delta("Hel"), Delta("lo"), Usage("openai_compat", "llama3", 9, 2)]
    assert str(seen[0].url) == "http://localhost:11434/v1/chat/completions"
    body = json.loads(seen[0].content)
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["stream_options"] == {"include_usage": True} and body["temperature"] == 0.1


async def test_openai_compat_rate_limit_maps_after_retries() -> None:
    seen: list[httpx2.Request] = []
    handler: Handler = lambda r: httpx2.Response(429, headers=NO_WAIT, json={"error": {}})  # noqa: E731
    p = OpenAICompatProvider("gpt-x", "k", max_retries=1, http_client=_client(handler, seen))
    with pytest.raises(LLMError) as err:
        await collect(p.stream(MSGS, "s", 10))
    assert err.value.code == "llm_rate_limited" and len(seen) == 2


def test_connection_and_timeout_errors_are_retryable() -> None:
    req = httpx2.Request("POST", "http://x")
    assert classify(anthropic.APITimeoutError(req)).code == "llm_timeout"
    assert classify(openai.APIConnectionError(request=req)).code == "llm_unavailable"


class Stub:
    provider = "stub"

    def __init__(self, model: str, chunks: list[str], fail_after: int | None = None) -> None:
        self.model, self.chunks, self.fail_after = model, chunks, fail_after
        self.max_tokens: list[int] = []

    async def stream(
        self,
        messages: list[ChatMessage],
        system: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[Delta | Usage]:
        self.max_tokens.append(max_tokens)
        for i, c in enumerate(self.chunks):
            if i == self.fail_after:
                raise LLMError("llm_unavailable", "down", retryable=True)
            yield Delta(c)
        if self.fail_after is not None and self.fail_after >= len(self.chunks):
            raise LLMError("llm_unavailable", "down", retryable=True)
        yield Usage("stub", self.model, 1, 1)


async def test_fallback_used_when_primary_fails_before_any_text() -> None:
    primary, backup = Stub("a", ["x"], fail_after=0), Stub("b", ["from b"])
    events = await collect(stream_with_fallback([primary, backup], MSGS, "s", 50_000))
    assert events == [Delta("from b"), Usage("stub", "b", 1, 1)]
    assert backup.max_tokens == [registry.UNKNOWN.max_output]  # clamped to model capability


async def test_no_fallback_after_text_started_or_on_permanent_error() -> None:
    with pytest.raises(LLMError):
        await collect(
            stream_with_fallback([Stub("a", ["x", "y"], 1), Stub("b", ["z"])], MSGS, "s", 9)
        )

    class Permanent(Stub):
        async def stream(self, *a: object, **k: object) -> AsyncIterator[Delta | Usage]:
            raise LLMError("llm_auth_failed", "no")
            yield Delta("")  # pragma: no cover

    backup = Stub("b", ["z"])
    with pytest.raises(LLMError, match="no"):
        await collect(stream_with_fallback([Permanent("a", []), backup], MSGS, "s", 9))
    assert backup.max_tokens == []


def test_registry_cost_and_unknown_model_defaults(caplog: pytest.LogCaptureFixture) -> None:
    registry.capability.cache_clear()
    assert registry.cost_usd("anthropic", "claude-sonnet-5-5", 1_000_000, 100_000) == Decimal(3)
    assert registry.cost_usd("anthropic", "claude-haiku-5-5", 2_000, 1_000) == Decimal("0.0007")
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        assert registry.capability("ollama", "mystery") == registry.UNKNOWN
        assert registry.cost_usd("ollama", "mystery", 10, 10) is None
    assert sum("unknown model" in r.message for r in caplog.records) == 1  # warned once


def _hop(kind: str, model: str, in_tok: int, out_tok: int) -> dict[str, object]:
    return {
        "type": kind,
        "model": model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


async def test_server_fallback_usage_sums_every_attempt_at_its_own_rates() -> None:
    """Top-level usage covers only the serving hop; usage.iterations bills every attempt."""
    hops = [
        _hop("message", "claude-sonnet-5-5", 100, 20),  # declined mid-stream: still billed
        _hop("fallback_message", "claude-sonnet-5", 120, 7),
    ]
    body = anthropic_body(
        "Hi",
        model="claude-sonnet-5",
        final_usage={"input_tokens": 120, "output_tokens": 7, "iterations": hops},
    )
    http = _client(lambda r: httpx2.Response(200, headers=SSE_HEADERS, content=body), [])
    p = AnthropicProvider("claude-sonnet-5-5", "test-key", http_client=http)
    *_, usage = await collect(p.stream(MSGS, "sys", 1000))
    assert isinstance(usage, Usage)
    assert (usage.model, usage.in_tok, usage.out_tok) == ("claude-sonnet-5", 220, 27)
    # (100*2 + 20*10) + (120*2 + 7*10) per 1M
    assert usage_cost(usage) == Decimal("0.00071")
    one_hop = anthropic_body("Hi", final_usage={"output_tokens": 7, "iterations": hops[:1]})
    http = _client(lambda r: httpx2.Response(200, headers=SSE_HEADERS, content=one_hop), [])
    p = AnthropicProvider("claude-sonnet-5-5", "test-key", http_client=http)
    *_, plain = await collect(p.stream(MSGS, "sys", 1000))
    assert plain == Usage("anthropic", "claude-sonnet-5-5", 12, 7)  # no fallback: top-level


def test_embedding_costs_local_free_and_unknown_null() -> None:
    registry.capability.cache_clear()
    assert registry.cost_usd("fastembed", "any-local-model", 10_000, 0) == Decimal(0)
    assert registry.cost_usd("openai", "text-embedding-3-small", 1_000_000, 0) == Decimal("0.02")
    assert registry.cost_usd("openai", "some-other-embedder", 10, 0) is None


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    for var in ("LLM_PROVIDER", "LLM_MODEL", "LLM_FALLBACK_MODEL", "LLM_REWRITE_MODEL",
                "LLM_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):  # fmt: skip
        monkeypatch.delenv(var, raising=False)
    providers.get_llm_settings.cache_clear()
    providers._build.cache_clear()
    yield monkeypatch
    providers.get_llm_settings.cache_clear()
    providers._build.cache_clear()


def test_get_llm_builds_anthropic_chain_with_fallback_and_cheap_rewriter(
    llm_env: pytest.MonkeyPatch,
) -> None:
    llm_env.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm_env.setenv("LLM_FALLBACK_MODEL", "claude-sonnet-5")
    llm = providers.get_llm()
    assert [p.model for p in llm.chat] == ["claude-sonnet-5-5", "claude-sonnet-5"]
    assert llm.rewrite.model == "claude-haiku-5-5"


def test_get_llm_openai_compat_local_needs_no_key(llm_env: pytest.MonkeyPatch) -> None:
    llm_env.setenv("LLM_PROVIDER", "openai_compat")
    llm_env.setenv("LLM_MODEL", "llama3.2")
    llm_env.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    llm = providers.get_llm()
    assert isinstance(llm.chat[0], OpenAICompatProvider) and llm.rewrite.model == "llama3.2"


@pytest.mark.parametrize("provider", ["anthropic", "openai_compat"])
def test_get_llm_without_credentials_is_503(llm_env: pytest.MonkeyPatch, provider: str) -> None:
    llm_env.setenv("LLM_PROVIDER", provider)
    with pytest.raises(providers.AppError) as err:
        providers.get_llm()
    assert (err.value.status, err.value.code) == (503, "llm_not_configured")


def test_citation_filter_handles_split_markers_and_strips_unknown() -> None:
    f = CitationFilter(2)
    out = [f.feed(t) for t in ["A [", "c", "2] b [c3", "] c [c1][c2]", " [c"]]
    out.append(f.flush())
    assert "".join(t for t, _ in out) == "A [c2] b  c [c1][c2] [c"
    assert [n for _, new in out for n in new] == [2, 1]
    assert f.cited == [2, 1] and f.dropped == ["[c3]"]
    assert CitationFilter(1).feed("[c0] x")[0] == " x"  # 0 is never a valid source


def _hit(chars: int) -> Hit:
    import uuid

    return Hit(uuid.uuid4(), uuid.uuid4(), "d", uuid.uuid4(), "x" * chars, {}, 1.0)


def test_pack_context_respects_model_window() -> None:
    small = registry.Capability(context_window=3_000, max_output=1_000)
    # (3000 - 1000 - 1000) tokens * 4 chars = 4000 chars, minus 200 overhead per hit
    assert len(pack_context([_hit(1_500)] * 5, small, 0)) == 2
    assert len(pack_context([_hit(1_500)] * 5, small, 2_000)) == 1
    assert (
        len(pack_context([_hit(10)] * 5, registry.MODELS[("anthropic", "claude-sonnet-5-5")], 0))
        == 5
    )


@pytest.mark.live
async def test_live_anthropic_roundtrip() -> None:  # pragma: no cover - needs credentials
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    p = AnthropicProvider("claude-haiku-5-5", os.environ["ANTHROPIC_API_KEY"])
    events = await collect(p.stream(MSGS, "Reply with one word.", 1024))
    assert isinstance(events[-1], Usage) and events[-1].out_tok > 0

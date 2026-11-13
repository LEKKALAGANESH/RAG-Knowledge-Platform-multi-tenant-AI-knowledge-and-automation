"""LLM adapters: one Protocol, an Anthropic adapter and an OpenAI-compatible adapter.

Both SDKs already retry 408/409/429/5xx and connection errors with exponential backoff
(`max_retries`); this module adds timeouts, model fallback and readable error mapping."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast

from pydantic import JsonValue

from app.core.errors import AppError
from app.llm.config import get_llm_settings
from app.llm.registry import capability

# The SDKs are imported where a provider is built or called: `anthropic` alone costs ~6 s and
# `openai` ~2 s at import, which every worker/API start used to pay even with no LLM configured.
if TYPE_CHECKING:
    import httpx2
    from anthropic.types.beta import BetaMessageParam, BetaToolParam, BetaUsage
    from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

log = logging.getLogger("app.llm")

# Server-side refusal fallback (Claude API): a declined request is re-run on Anthropic's
# recommended model instead of returning the refusal. Haiku has no server-side fallback.
SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class Usage:
    provider: str
    model: str
    in_tok: int
    out_tok: int
    # (model, in_tok, out_tok) per billed attempt when a call ran on several models; else empty.
    attempts: tuple[tuple[str, int, int], ...] = ()


Event = Delta | Usage


class LLMError(Exception):
    """A provider failure mapped to a stable code and a message safe to show users.
    `usage` is set when the failed call was still billed (a refusal), so callers can record it."""

    def __init__(
        self, code: str, message: str, retryable: bool = False, usage: Usage | None = None
    ) -> None:
        super().__init__(message)
        self.code, self.message, self.retryable, self.usage = code, message, retryable, usage


class LLMProvider(Protocol):
    provider: str
    model: str

    def stream(
        self,
        messages: list[ChatMessage],
        system: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[Event]:
        """Yields text deltas, then exactly one Usage; raises LLMError on failure."""
        ...


# Agent tool calling. Messages and blocks use the Anthropic shape as the neutral format
# ({"role", "content": [{"type": "text" | "tool_use" | "tool_result" | ...}]}); other adapters
# convert. Blocks a provider returns (e.g. thinking) are stored and replayed unchanged.
Block = dict[str, JsonValue]


class AgentMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: list[Block]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, JsonValue]


@dataclass(frozen=True)
class ToolTurn:
    content: list[Block]
    stop_reason: str  # "tool_use" | "end_turn" | "max_tokens" | ...
    usage: Usage


class ToolProvider(Protocol):
    provider: str
    model: str

    async def tool_turn(
        self, messages: list[AgentMessage], system: str, tools: list[ToolSpec], max_tokens: int
    ) -> ToolTurn:
        """One model turn with tools; raises LLMError on failure (refusal included)."""
        ...


def classify(exc: Exception) -> LLMError:
    """Map SDK exceptions to user-safe errors. Raw provider text is logged, never returned."""
    status = getattr(exc, "status_code", None)
    log.warning("llm call failed", extra={"error_type": type(exc).__name__, "status": status})
    import anthropic  # already loaded: exc came from an SDK call
    import openai

    if isinstance(exc, anthropic.APITimeoutError | openai.APITimeoutError):
        return LLMError("llm_timeout", "The AI model took too long to respond. Retry.", True)
    if isinstance(exc, anthropic.APIConnectionError | openai.APIConnectionError):
        return LLMError(
            "llm_unavailable", "The AI model could not be reached. Retry shortly.", True
        )
    if status in (401, 403):
        return LLMError(
            "llm_auth_failed",
            "The AI provider rejected the configured credentials. Ask an admin to check them.",
        )
    if status == 429:
        return LLMError("llm_rate_limited", "The AI model is busy. Wait a moment and retry.", True)
    if status == 404:
        return LLMError("llm_model_unavailable", "The configured AI model is not available.", True)
    if status is not None and status >= 500:
        return LLMError("llm_unavailable", "The AI model is temporarily unavailable. Retry.", True)
    return LLMError("llm_bad_request", "The AI model could not process this request.")


class AnthropicProvider:
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str,
        timeout: float = 60.0,
        max_retries: int = 2,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        import anthropic

        self.model = model
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries, http_client=http_client
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        system: str,
        max_tokens: int,
        temperature: float | None = None,  # Claude 5.x rejects sampling params; effort steers
    ) -> AsyncIterator[Event]:
        import anthropic

        fallback = self.model in SERVER_FALLBACK_MODELS
        params: list[BetaMessageParam] = [
            {"role": "user", "content": m["content"]}
            if m["role"] == "user"
            else {"role": "assistant", "content": m["content"]}
            for m in messages
        ]
        try:
            async with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=params,
                output_config={"effort": "low"},  # chat latency over depth (AC6)
                betas=[SERVER_FALLBACK_BETA] if fallback else anthropic.omit,
                fallbacks="default" if fallback else anthropic.omit,
            ) as stream:
                async for text in stream.text_stream:
                    yield Delta(text)
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise classify(exc) from exc
        if final.stop_reason == "refusal":  # whole fallback chain declined; attempts still billed
            raise LLMError(
                "llm_refused",
                "The AI model declined to answer this question.",
                usage=self._usage(final.model, final.usage),
            )
        yield self._usage(final.model, final.usage)

    def _usage(self, served_by: str, u: BetaUsage) -> Usage:
        """Top-level usage covers only the serving attempt; after a server-side fallback,
        usage.iterations is the per-attempt billing source, each at its own model's rates."""
        from anthropic.types.beta import (
            BetaFallbackMessageIterationUsage,
            BetaMessageIterationUsage,
        )

        hops = tuple(
            (it.model or self.model, it.input_tokens, it.output_tokens)
            for it in u.iterations or []
            if isinstance(it, BetaMessageIterationUsage | BetaFallbackMessageIterationUsage)
        )
        if len(hops) < 2:
            return Usage(self.provider, served_by, u.input_tokens, u.output_tokens)
        in_tok, out_tok = sum(h[1] for h in hops), sum(h[2] for h in hops)
        return Usage(self.provider, served_by, in_tok, out_tok, hops)

    async def tool_turn(
        self, messages: list[AgentMessage], system: str, tools: list[ToolSpec], max_tokens: int
    ) -> ToolTurn:
        import anthropic

        fallback = self.model in SERVER_FALLBACK_MODELS
        specs: list[BetaToolParam] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": cast(dict[str, object], t.input_schema),
                # Inputs stream as generated; the agent validates every input before running it.
                "eager_input_streaming": True,
            }
            for t in tools
        ]
        try:
            async with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=cast("list[BetaMessageParam]", messages),
                tools=specs,
                output_config={"effort": "medium"},
                betas=[SERVER_FALLBACK_BETA] if fallback else anthropic.omit,
                fallbacks="default" if fallback else anthropic.omit,
            ) as stream:
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise classify(exc) from exc
        usage = self._usage(final.model, final.usage)
        if final.stop_reason == "refusal":
            raise LLMError("llm_refused", "The AI model declined this task.", usage=usage)
        content = [b.model_dump(mode="json", exclude_none=True) for b in final.content]
        return ToolTurn(content, final.stop_reason or "end_turn", usage)


class OpenAICompatProvider:
    """OpenAI Chat Completions API: OpenAI, Azure, Ollama, vLLM, LM Studio via base_url."""

    provider = "openai_compat"

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        import openai

        self.model = model
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        system: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[Event]:
        import openai

        params: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
        params += [
            {"role": "user", "content": m["content"]}
            if m["role"] == "user"
            else {"role": "assistant", "content": m["content"]}
            for m in messages
        ]
        in_tok = out_tok = 0
        try:
            chunks = await self._client.chat.completions.create(
                model=self.model,
                messages=params,
                max_tokens=max_tokens,  # widest support across Ollama/vLLM/LM Studio
                temperature=openai.omit if temperature is None else temperature,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in chunks:
                if chunk.usage:
                    in_tok, out_tok = chunk.usage.prompt_tokens, chunk.usage.completion_tokens
                if chunk.choices and chunk.choices[0].delta.content:
                    yield Delta(chunk.choices[0].delta.content)
        except openai.APIError as exc:
            raise classify(exc) from exc
        yield Usage(self.provider, self.model, in_tok, out_tok)

    async def tool_turn(
        self, messages: list[AgentMessage], system: str, tools: list[ToolSpec], max_tokens: int
    ) -> ToolTurn:
        import openai

        specs: list[ChatCompletionToolParam] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": cast(dict[str, object], t.input_schema),
                },
            }
            for t in tools
        ]
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=to_openai(messages, system),
                tools=specs,
                max_tokens=max_tokens,
            )
        except openai.APIError as exc:
            raise classify(exc) from exc
        msg, finish = resp.choices[0].message, resp.choices[0].finish_reason
        content: list[Block] = [{"type": "text", "text": msg.content}] if msg.content else []
        for call in msg.tool_calls or []:
            if call.type != "function":
                continue
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": call.function.arguments}  # fails validation downstream
            content.append(
                {"type": "tool_use", "id": call.id, "name": call.function.name, "input": args}
            )
        stop = {"tool_calls": "tool_use", "length": "max_tokens"}.get(finish, "end_turn")
        u = resp.usage
        usage = Usage(
            self.provider, self.model, u.prompt_tokens if u else 0, u.completion_tokens if u else 0
        )
        return ToolTurn(content, stop, usage)


def to_openai(messages: list[AgentMessage], system: str) -> list[ChatCompletionMessageParam]:
    """Neutral (Anthropic-shaped) transcript -> Chat Completions messages."""
    out: list[dict[str, object]] = [{"role": "system", "content": system}]
    for m in messages:
        texts = [str(b["text"]) for b in m["content"] if b.get("type") == "text"]
        if m["role"] == "assistant":
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                }
                for b in m["content"]
                if b.get("type") == "tool_use"
            ]
            msg: dict[str, object] = {"role": "assistant", "content": "\n".join(texts) or None}
            out.append(msg | ({"tool_calls": calls} if calls else {}))
            continue
        for b in m["content"]:
            if b.get("type") == "tool_result":
                out.append(
                    {"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]}
                )
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return cast("list[ChatCompletionMessageParam]", out)


async def stream_with_fallback(
    providers: Sequence[LLMProvider],
    messages: list[ChatMessage],
    system: str,
    max_tokens: int,
) -> AsyncIterator[Event]:
    """Try each provider in order; switch only on a retryable error before any text streamed."""
    for i, p in enumerate(providers):
        started = False
        limit = min(max_tokens, capability(p.provider, p.model).max_output)
        try:
            async for ev in p.stream(messages, system, limit):
                started = started or isinstance(ev, Delta)
                yield ev
            return
        except LLMError as err:
            if started or not err.retryable or i == len(providers) - 1:
                raise
            log.warning("llm fallback", extra={"from": p.model, "code": err.code})


@dataclass(frozen=True)
class LLM:
    chat: list[LLMProvider]  # primary, then fallback
    rewrite: LLMProvider


def _not_configured(what: str) -> AppError:
    return AppError(
        503,
        "llm_not_configured",
        f"No AI model is configured: set {what} on the server, then restart it.",
    )


@lru_cache
def _build() -> LLM:
    s = get_llm_settings()
    if s.llm_provider == "anthropic":
        key = s.anthropic_api_key.get_secret_value() if s.anthropic_api_key else ""
        if not key:
            raise _not_configured("ANTHROPIC_API_KEY")

        def make(model: str) -> LLMProvider:
            return AnthropicProvider(model, key, s.llm_timeout_s, s.llm_max_retries)

        rewrite_model = s.llm_rewrite_model or "claude-haiku-5-5"
    else:
        key = s.openai_api_key.get_secret_value() if s.openai_api_key else ""
        if not key and not s.llm_base_url:
            raise _not_configured("OPENAI_API_KEY (or LLM_BASE_URL for a local server)")

        def make(model: str) -> LLMProvider:
            # Local servers (Ollama, vLLM) ignore the key, but the SDK requires a value.
            return OpenAICompatProvider(
                model, key or "unused", s.llm_base_url, s.llm_timeout_s, s.llm_max_retries
            )

        rewrite_model = s.llm_rewrite_model or s.llm_model
    chat = [make(s.llm_model)] + ([make(s.llm_fallback_model)] if s.llm_fallback_model else [])
    return LLM(chat=chat, rewrite=make(rewrite_model))


def get_tool_provider() -> ToolProvider:
    """The primary chat model, for the agent; AppError 503 `llm_not_configured` without creds."""
    p = _build().chat[0]
    if not isinstance(p, AnthropicProvider | OpenAICompatProvider):
        raise TypeError(f"{type(p).__name__} has no tool calling")
    return p


def get_llm() -> LLM:
    """FastAPI dependency; 503 `llm_not_configured` when the provider has no credentials."""
    return _build()

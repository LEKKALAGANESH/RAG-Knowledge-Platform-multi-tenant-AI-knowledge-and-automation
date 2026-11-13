"""What each provider+model can do and what it costs. Unknown models get conservative defaults."""

import logging
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

log = logging.getLogger("app.llm")


@dataclass(frozen=True)
class Capability:
    context_window: int
    max_output: int
    streaming: bool = True
    tools: bool = True
    in_price: Decimal | None = None  # USD per million input tokens; None = unknown
    out_price: Decimal | None = None


def _cap(in_price: str, out_price: str) -> Capability:
    return Capability(1_000_000, 128_000, in_price=Decimal(in_price), out_price=Decimal(out_price))


MODELS: dict[tuple[str, str], Capability] = {
    ("anthropic", "claude-opus-5-5"): _cap("4", "20"),
    ("anthropic", "claude-sonnet-5-5"): _cap("2", "10"),
    ("anthropic", "claude-sonnet-5"): _cap("2", "10"),
    # ponytail: Haiku 5.5 bills 0.50/2.50 above 100K prompt tokens; our RAG prompts stay far below.
    ("anthropic", "claude-haiku-5-5"): _cap("0.10", "0.50"),
    # Embedding models (provider names as in retrieval/embedders.PROVIDERS); only prices matter.
    ("openai", "text-embedding-3-small"): _cap("0.02", "0"),
    ("openai", "text-embedding-3-large"): _cap("0.13", "0"),
}
FREE_PROVIDERS = {"fastembed"}  # runs locally in-process: known zero cost, not "unknown"
UNKNOWN = Capability(context_window=8_192, max_output=1_024, tools=False)


@lru_cache
def capability(provider: str, model: str) -> Capability:
    cap = MODELS.get((provider, model))
    if cap is None:
        log.warning(
            "unknown model, using conservative limits; cost will be recorded as unknown",
            extra={"provider": provider, "model": model},
        )
        return UNKNOWN
    return cap


def cost_usd(provider: str, model: str, in_tok: int, out_tok: int) -> Decimal | None:
    if provider in FREE_PROVIDERS:
        return Decimal(0)
    cap = capability(provider, model)
    if cap.in_price is None or cap.out_price is None:
        return None
    return (in_tok * cap.in_price + out_tok * cap.out_price) / 1_000_000

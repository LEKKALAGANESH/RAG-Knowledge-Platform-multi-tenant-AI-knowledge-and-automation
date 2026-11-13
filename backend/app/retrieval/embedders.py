"""Embedding providers. An index records (provider, model, dim); `embedder_for` rebuilds the exact
embedder for it, so queries and writes always use the index's own model (AC8)."""

import threading
from collections.abc import Callable
from functools import lru_cache
from typing import Protocol

import httpx
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    embedding_provider: str = "fastembed"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = Field(default=384, ge=1, le=2000)
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: SecretStr | None = None
    embedding_batch: int = Field(default=64, ge=1, le=2048)
    embedding_timeout_s: float = Field(default=60.0, gt=0)


@lru_cache
def get_embedding_settings() -> EmbeddingSettings:
    return EmbeddingSettings()


class EmbeddingError(RuntimeError):
    pass


# (vectors in input order, provider-reported input tokens or None when the provider reports none)
Embedded = tuple[list[list[float]], int | None]


class Embedder(Protocol):
    provider: str
    model: str
    dim: int

    def embed(self, texts: list[str]) -> Embedded: ...


class FastEmbedEmbedder:
    """Local ONNX model, no API key. Loaded once per process via `embedder_for`'s cache."""

    provider = "fastembed"

    def __init__(self, model: str, dim: int) -> None:
        from fastembed import TextEmbedding  # heavy import, only when actually used

        self.model, self.dim = model, dim
        self._m = TextEmbedding(model_name=model)

    def embed(self, texts: list[str]) -> Embedded:
        batch = get_embedding_settings().embedding_batch
        return [v.tolist() for v in self._m.embed(texts, batch_size=batch)], None


class _Item(BaseModel):
    index: int
    embedding: list[float]


class _Usage(BaseModel):
    prompt_tokens: int


class _Resp(BaseModel):
    data: list[_Item]
    usage: _Usage | None = None  # OpenAI reports it; some compatible servers omit it


class OpenAICompatEmbedder:
    """POST {base_url}/embeddings: OpenAI, Ollama (/v1), vLLM, LM Studio, gateways."""

    provider = "openai"

    def __init__(self, model: str, dim: int, client: httpx.Client | None = None) -> None:
        s = get_embedding_settings()
        key = s.embedding_api_key.get_secret_value() if s.embedding_api_key else None
        self.model, self.dim, self._batch = model, dim, s.embedding_batch
        self._client = client or httpx.Client(
            base_url=s.embedding_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=s.embedding_timeout_s,
        )

    def embed(self, texts: list[str]) -> Embedded:
        out: list[list[float]] = []
        tokens: int | None = 0
        for i in range(0, len(texts), self._batch):
            part = texts[i : i + self._batch]
            r = self._client.post("embeddings", json={"model": self.model, "input": part})
            if r.status_code != 200:
                raise EmbeddingError(f"embedding provider returned HTTP {r.status_code}")
            resp = _Resp.model_validate_json(r.content)
            items = sorted(resp.data, key=lambda d: d.index)
            if len(items) != len(part) or any(len(d.embedding) != self.dim for d in items):
                raise EmbeddingError(
                    f"embedding provider returned wrong count or dim != {self.dim}"
                )
            out.extend(d.embedding for d in items)
            # One batch without usage makes the total unknown: report none rather than a part.
            used = resp.usage.prompt_tokens if resp.usage else None
            tokens = None if tokens is None or used is None else tokens + used
        return out, tokens


PROVIDERS: dict[str, Callable[[str, int], Embedder]] = {
    "fastembed": FastEmbedEmbedder,
    "openai": OpenAICompatEmbedder,
}


_load_lock = threading.Lock()


@lru_cache
def _load(provider: str, model: str, dim: int) -> Embedder:
    if provider not in PROVIDERS:
        raise EmbeddingError(f"unknown embedding provider {provider!r}")
    return PROVIDERS[provider](model, dim)


def embedder_for(provider: str, model: str, dim: int) -> Embedder:
    # lru_cache alone lets two worker threads load the same model at once (the live worker hung).
    with _load_lock:
        return _load(provider, model, dim)


def configured_embedder() -> Embedder:
    s = get_embedding_settings()
    return embedder_for(s.embedding_provider, s.embedding_model, s.embedding_dim)

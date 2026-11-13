"""Cross-encoder reranking of fused candidates. `RERANK_PROVIDER=none` (default) skips it."""

import threading
from functools import lru_cache
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RerankSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    rerank_provider: Literal["none", "local", "cohere"] = "none"
    # Empty = the provider's default model (LOCAL_MODEL / COHERE_MODEL).
    rerank_model: str = ""
    rerank_top_n: int = Field(default=20, ge=1, le=150)
    rerank_base_url: str = "https://api.cohere.com/v2"
    rerank_api_key: SecretStr | None = None
    rerank_timeout_s: float = Field(default=10.0, gt=0)


@lru_cache
def get_rerank_settings() -> RerankSettings:
    return RerankSettings()


class RerankError(RuntimeError):
    pass


class Reranker(Protocol):
    model: str

    def scores(self, query: str, texts: list[str]) -> list[float]:
        """One relevance score per text, in input order (higher = more relevant)."""
        ...


LOCAL_MODEL = "jinaai/jina-reranker-v2-base-multilingual"
COHERE_MODEL = "rerank-v3.5"


class LocalReranker:
    """fastembed ONNX cross-encoder on CPU, no API key, records no usage."""

    def __init__(self, model: str) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # heavy import

        self.model = model
        self._m = TextCrossEncoder(model_name=model)

    def scores(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self._m.rerank(query, texts, batch_size=len(texts) or 1)]


class _Result(BaseModel):
    index: int
    relevance_score: float


class _Resp(BaseModel):
    results: list[_Result]


class CohereReranker:
    """POST {base_url}/rerank (Cohere v2). Contract-tested only; no live key verified.
    ponytail: writes no usage row (usage_events.kind has no 'rerank'); needs a migration first."""

    def __init__(self, model: str, client: httpx.Client | None = None) -> None:
        s = get_rerank_settings()
        if client is None and s.rerank_api_key is None:
            raise RerankError("RERANK_API_KEY is required for RERANK_PROVIDER=cohere")
        key = s.rerank_api_key.get_secret_value() if s.rerank_api_key else ""
        self.model = model
        self._client = client or httpx.Client(
            base_url=s.rerank_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {key}"},
            timeout=s.rerank_timeout_s,
        )

    def scores(self, query: str, texts: list[str]) -> list[float]:
        body = {"model": self.model, "query": query, "documents": texts, "top_n": len(texts)}
        r = self._client.post("rerank", json=body)
        if r.status_code != 200:
            raise RerankError(f"rerank provider returned HTTP {r.status_code}")
        results = _Resp.model_validate_json(r.content).results
        if sorted(x.index for x in results) != list(range(len(texts))):
            raise RerankError("rerank provider returned a wrong set of indexes")
        out = [0.0] * len(texts)
        for x in results:
            out[x.index] = x.relevance_score
        return out


_load_lock = threading.Lock()


@lru_cache
def _load(provider: str, model: str) -> Reranker:
    if provider == "local":
        return LocalReranker(model or LOCAL_MODEL)
    return CohereReranker(model or COHERE_MODEL)


def configured_reranker() -> Reranker | None:
    s = get_rerank_settings()
    if s.rerank_provider == "none":
        return None
    with _load_lock:  # one model load per process, even with several worker threads
        return _load(s.rerank_provider, s.rerank_model)

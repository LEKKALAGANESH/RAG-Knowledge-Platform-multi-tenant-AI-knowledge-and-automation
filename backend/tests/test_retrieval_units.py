import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from app.core.db import SessionLocal
from app.llm.usage import UsageEvent
from app.rag.answer import below_similarity_gate
from app.retrieval import indexing
from app.retrieval import rerank as rerank_mod
from app.retrieval import search as search_mod
from app.retrieval.embedders import (
    Embedded,
    EmbeddingError,
    FastEmbedEmbedder,
    OpenAICompatEmbedder,
)
from app.retrieval.rerank import CohereReranker, RerankError, RerankSettings
from app.retrieval.search import Hit, fuse, rerank


class HashEmbedder:
    """Test double we own: deterministic bag-of-words hashed into `dim` buckets (no model)."""

    provider = "hash"

    def __init__(self, model: str, dim: int) -> None:
        self.model, self.dim = model, dim

    reported: int | None = None  # provider token count; None = provider reports none

    def embed(self, texts: list[str]) -> Embedded:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for word in re.findall(r"\w+", t.lower()):
                v[
                    int(hashlib.sha256(f"{self.model}:{word}".encode()).hexdigest(), 16) % self.dim
                ] += 1
            out.append(v if any(v) else [1.0] + [0.0] * (self.dim - 1))
        return out, self.reported


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


def test_fuse_scales_each_list_then_weights() -> None:
    a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    vec = [(a, 0.80), (b, 0.60), (c, 0.40)]  # cosine
    kw = [(c, 12.0), (d, 3.0)]  # ts_rank, another scale entirely
    fused = dict(fuse([vec, kw], [1.0, 0.5]))
    assert fused[a] == pytest.approx(1.0) and fused[b] == pytest.approx(0.5)
    assert fused[c] == pytest.approx(0.0 + 0.5) and fused[d] == pytest.approx(0.0)
    assert fuse([vec, kw], [1.0, 0.5])[0][0] == a
    # A weak keyword match in both lists no longer beats a far closer vector hit (rank fusion did).
    assert fused[a] > fused[c]
    assert fuse([[(a, 0.3)], []], [1.0, 0.5]) == [(a, 1.0)]  # lone or all-equal list scales to 1
    assert fuse([], []) == []


def test_hash_embedder_is_deterministic() -> None:
    e = HashEmbedder("m", 16)
    assert e.embed(["refund policy"]) == e.embed(["Refund   POLICY"])
    assert len(e.embed(["x"])[0][0]) == 16 and e.embed([""])[0][0][0] == 1.0


def _openai(handler: httpx.MockTransport, dim: int = 3) -> OpenAICompatEmbedder:
    return OpenAICompatEmbedder(
        "text-embedding-3-small", dim, httpx.Client(transport=handler, base_url="http://x/v1/")
    )


def test_openai_compat_embedder_parses_and_orders() -> None:
    seen: list[dict[str, object]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        data = [{"index": i, "embedding": [float(i), 0.0, 1.0]} for i in range(len(body["input"]))]
        usage = {"prompt_tokens": 5 * len(data), "total_tokens": 5 * len(data)}
        return httpx.Response(
            200, json={"object": "list", "data": list(reversed(data)), "usage": usage}
        )

    vecs, tokens = _openai(httpx.MockTransport(handle)).embed(["a", "b"])
    assert vecs == [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0]]
    assert tokens == 10  # provider-reported usage.prompt_tokens
    assert seen == [{"model": "text-embedding-3-small", "input": ["a", "b"]}]


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(500, json={"error": "down"}),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),  # wrong dim
    ],
)
def test_openai_compat_embedder_errors(resp: httpx.Response) -> None:
    with pytest.raises(EmbeddingError):
        _openai(httpx.MockTransport(lambda _: resp)).embed(["a"])


def test_fastembed_real_multilingual_model() -> None:
    t = time.perf_counter()
    e = FastEmbedEmbedder("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384)
    load_s = time.perf_counter() - t
    (en, de, other), tokens = e.embed(
        ["Refunds are issued within 30 days.", "Rückerstattungen erfolgen innerhalb von 30 Tagen.",
         "The cat sleeps on the warm windowsill."]
    )  # fmt: skip
    print(
        f"fastembed load {load_s:.1f}s; "
        f"cos(en,de)={_cos(en, de):.3f} cos(en,other)={_cos(en, other):.3f}"
    )
    assert len(en) == 384 and tokens is None  # local model reports no token usage
    assert _cos(en, de) > _cos(en, other)


def test_openai_compat_embedder_without_usage_reports_none() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        n = len(json.loads(req.content)["input"])
        data = [{"index": i, "embedding": [0.0, 0.0, 1.0]} for i in range(n)]
        usage = {"prompt_tokens": 4} if n == 2 else None  # second batch omits usage
        return httpx.Response(200, json={"data": data, **({"usage": usage} if usage else {})})

    e = _openai(httpx.MockTransport(handle))
    e._batch = 2
    vecs, tokens = e.embed(["a", "b", "c"])
    assert len(vecs) == 3 and tokens is None  # a partial count would under-bill


@pytest.mark.parametrize(("reported", "expected"), [(42, 42), (None, 4)])
async def test_embed_records_provider_tokens_else_the_estimate(
    reported: int | None, expected: int
) -> None:
    emb = HashEmbedder("m", 4)
    emb.reported = reported
    async with SessionLocal() as s:  # nothing is flushed: the row is only staged
        await indexing.embed(s, emb, uuid.uuid4(), ["refund policy"])  # 13 chars ~ ceil(13 / 4) = 4
        [row] = [o for o in s.new if isinstance(o, UsageEvent)]
    assert (row.kind, row.in_tok, row.out_tok) == ("embed", expected, 0)


# ---------- R1 reranker ----------

MINI = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def _hit(text: str, score: float, cosine: float | None) -> Hit:
    return Hit(
        uuid.uuid4(), uuid.uuid4(), f"{text}.md", uuid.uuid4(), text, {}, score, cosine,
        MINI if cosine is not None else None,
    )  # fmt: skip


class LenReranker:
    """Test double we own: longer text = more relevant; records what it was asked to score."""

    model = "len"

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def scores(self, query: str, texts: list[str]) -> list[float]:
        self.seen.append(texts)
        return [float(len(t)) for t in texts]


@pytest.fixture
def use_reranker(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], None]:
    def use(r: object) -> None:
        monkeypatch.setattr(search_mod, "configured_reranker", lambda: r)

    return use


async def test_rerank_reorders_head_by_score_and_keeps_cosine(
    use_reranker: Callable[[object], None],
) -> None:
    r = LenReranker()
    use_reranker(r)
    hits = [
        _hit("a", 1.5, 0.30),
        _hit("ccc", 1.0, 0.25),
        _hit("bb", 0.8, None),
        _hit("dddd", 0.1, 0.2),
    ]
    out = await rerank("q", hits, top_n=3)
    assert r.seen == [["a", "ccc", "bb"]]  # only the top_n candidates are scored
    assert [h.text for h in out] == ["ccc", "bb", "a", "dddd"]  # tail stays in fused order
    assert [h.score for h in out[:3]] == [3.0, 2.0, 1.0] and out[3] is hits[3]
    assert {h.chunk_id: h.cosine for h in out} == {h.chunk_id: h.cosine for h in hits}
    # The /v1/answer gate reads cosine, so it gives the same verdict before and after reranking.
    assert below_similarity_gate(out) is below_similarity_gate(hits) is True


class Broken:
    model = "broken"

    def __init__(self, scores: list[float] | None = None) -> None:
        self._scores = scores

    def scores(self, query: str, texts: list[str]) -> list[float]:
        if self._scores is None:
            raise RerankError("model exploded")
        return self._scores


@pytest.mark.parametrize("bad", [Broken(), Broken([1.0])])  # raises / wrong score count
async def test_rerank_failure_falls_back_to_fused_order(
    use_reranker: Callable[[object], None], bad: Broken, caplog: pytest.LogCaptureFixture
) -> None:
    use_reranker(bad)
    hits = [_hit("a", 1.0, 0.5), _hit("bb", 0.5, 0.4)]
    assert await rerank("q", hits, 20) == hits
    assert "rerank failed, using fused order" in caplog.text


async def test_rerank_none_returns_hits_unchanged(use_reranker: Callable[[object], None]) -> None:
    use_reranker(None)
    hits = [_hit("a", 1.0, 0.5), _hit("bb", 0.5, 0.4)]
    assert await rerank("q", hits, 20) is hits


def test_rerank_settings_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RERANK_PROVIDER", raising=False)
    s = RerankSettings(_env_file=None)
    assert (s.rerank_provider, s.rerank_top_n) == ("none", 20)


def _cohere(handler: Callable[[httpx.Request], httpx.Response]) -> CohereReranker:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x/v2/")
    return CohereReranker("rerank-v3.5", client)


def test_cohere_contract_scores_in_input_order() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, json.loads(req.content)))
        results = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]
        return httpx.Response(200, json={"id": "x", "results": results, "meta": {}})

    assert _cohere(handle).scores("refunds?", ["ship", "refund"]) == [0.1, 0.9]
    assert seen == [(
        "/v2/rerank",
        {"model": "rerank-v3.5", "query": "refunds?", "documents": ["ship", "refund"], "top_n": 2},
    )]  # fmt: skip


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(429, json={"message": "rate limited"}),
        httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]}),  # 1 of 2
        httpx.Response(200, json={"oops": True}),
    ],
)
def test_cohere_errors(resp: httpx.Response) -> None:
    with pytest.raises((RerankError, ValidationError)):
        _cohere(lambda _: resp).scores("q", ["a", "b"])


def test_cohere_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RERANK_API_KEY", raising=False)
    monkeypatch.setattr(rerank_mod, "get_rerank_settings", lambda: RerankSettings(_env_file=None))
    with pytest.raises(RerankError, match="RERANK_API_KEY"):
        CohereReranker("rerank-v3.5")

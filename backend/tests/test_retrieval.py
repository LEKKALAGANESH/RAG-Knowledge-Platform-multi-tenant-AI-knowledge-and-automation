import asyncio
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from dramatiq import Worker
from dramatiq.brokers.stub import StubBroker
from sqlalchemy import text

from app.core.db import SessionLocal, engine, use_tenant
from app.ingestion import storage
from app.ingestion import tasks as ingest_tasks
from app.ingestion.config import get_ingestion_settings
from app.retrieval import search as search_mod
from app.retrieval import tasks as index_tasks
from app.retrieval.embedders import PROVIDERS, Embedded, embedder_for, get_embedding_settings
from app.retrieval.rerank import get_rerank_settings
from app.retrieval.search import Hit, Mode, search
from tests.conftest import Account, register
from tests.test_retrieval_units import HashEmbedder

REFUND = b"# Refunds\n\nRefunds are issued within 30 days of purchase.\n"
SHIPPING = b"# Shipping\n\nParcels ship worldwide with tracking numbers.\n"


def _use_model(monkeypatch: pytest.MonkeyPatch, provider: str, model: str, dim: int) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", provider)
    monkeypatch.setenv("EMBEDDING_MODEL", model)
    monkeypatch.setenv("EMBEDDING_DIM", str(dim))
    get_embedding_settings.cache_clear()


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> Iterator[StubBroker]:
    monkeypatch.setitem(PROVIDERS, "hash", HashEmbedder)
    _use_model(monkeypatch, "hash", "hash-a", 16)
    monkeypatch.setenv("BLOB_BACKEND", "local")
    monkeypatch.setenv("BLOB_DIR", str(tmp_path))
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()
    stub = StubBroker()  # type: ignore[no-untyped-call]
    for actor in (
        ingest_tasks.ingest_document,
        ingest_tasks.ingest_retries_exhausted,
        index_tasks.reindex_kb,
    ):
        monkeypatch.setattr(actor, "broker", stub)
        monkeypatch.setitem(actor.options, "max_retries", 0)  # fail fast instead of backoff
        stub.declare_actor(actor)
    yield stub
    get_embedding_settings.cache_clear()
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()


def _drain(stub: StubBroker, queue: str) -> None:
    """Runs queued jobs on a real Dramatiq worker thread, like `dramatiq app.worker`."""
    worker = Worker(stub, queues={queue}, worker_timeout=100)
    worker.start()
    try:
        stub.join(queue, fail_fast=True)
        worker.join()
    finally:
        worker.stop()


async def _kb(client: httpx.AsyncClient, a: Account, name: str = "kb") -> str:
    r = await client.post("/v1/kbs", json={"name": name}, headers=a.headers)
    return str(r.json()["id"])


async def _ingest(
    client: httpx.AsyncClient, stub: StubBroker, a: Account, kb: str, name: str, data: bytes
) -> str:
    r = await client.post(
        f"/v1/kbs/{kb}/documents", files={"file": (name, data)}, headers=a.headers
    )
    assert r.status_code == 201, r.text
    _drain(stub, "ingest")
    doc = (await client.get(f"/v1/documents/{r.json()['id']}", headers=a.headers)).json()
    assert doc["status"] == "ready", doc
    return str(doc["id"])


async def _search(
    client: httpx.AsyncClient, a: Account, query: str, **kw: object
) -> httpx.Response:
    return await client.post("/v1/search", json={"query": query, **kw}, headers=a.headers)


async def _sql(a: Account, sql: str, **params: object) -> list[tuple[object, ...]]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        return [tuple(r) for r in await s.execute(text(sql), params)]


async def test_keyword_matches_any_content_word_of_a_question(
    client: httpx.AsyncClient, env: StubBroker
) -> None:
    """A3.2: the old all-words AND returned nothing for natural questions."""
    a = await register(client)
    kb = await _kb(client, a)
    await _ingest(client, env, a, kb, "refund.md", REFUND)
    await _ingest(client, env, a, kb, "ship.md", SHIPPING)

    async def kw(q: str) -> list[str]:
        r = await _search(client, a, q, kb_ids=[kb], mode="keyword")
        assert r.status_code == 200, r.text
        return [h["document_name"] for h in r.json()["results"]]

    assert (await kw("How many days do customers have to request a refund?"))[0] == "refund.md"
    assert (await kw("Wie lange dauert der Versand mit tracking?")) == ["ship.md"]
    assert await kw("what is the how") == []  # only stopwords and short words: no query
    assert await kw('"refunds are issued"') == ["refund.md"]  # quoted phrase stays strict
    assert await kw('"issued refunds"') == []


async def test_upload_ready_then_search_all_modes(
    client: httpx.AsyncClient, env: StubBroker
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    refund = await _ingest(client, env, a, kb, "refund.md", REFUND)
    await _ingest(client, env, a, kb, "ship.md", SHIPPING)
    for mode in ("hybrid", "vector", "keyword"):
        r = await _search(client, a, "refunds within 30 days", kb_ids=[kb], k=5, mode=mode)
        assert r.status_code == 200, r.text
        top = r.json()["results"][0]
        assert top["document_id"] == refund and top["document_name"] == "refund.md", mode
        assert (
            top["kb_id"] == kb and top["loc"] == {"section": "Refunds"} and "30 days" in top["text"]
        )
    hybrid = (await _search(client, a, "refunds within 30 days")).json()["results"]  # all KBs
    assert hybrid[0]["score"] == pytest.approx(1.5)  # top of both lists: 1 + 0.5 (keyword)
    kw = (await _search(client, a, "tracking", mode="keyword")).json()["results"]
    assert [h["document_name"] for h in kw] == ["ship.md"]
    (index_id, dim), *_ = await _sql(
        a, "SELECT id, dim FROM embedding_indexes WHERE kb_id = :k", k=uuid.UUID(kb)
    )
    assert dim == 16
    assert await _sql(
        a,
        "SELECT 1 FROM pg_indexes WHERE indexname = :n",
        n=f"chunk_embeddings_hnsw_{index_id.hex}",
    )  # type: ignore[attr-defined]


async def test_reprocess_does_not_duplicate_vectors(
    client: httpx.AsyncClient, env: StubBroker
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    doc = await _ingest(client, env, a, kb, "refund.md", REFUND)
    await client.post(f"/v1/documents/{doc}/reprocess", headers=a.headers)
    _drain(env, "ingest")
    rows = await _sql(
        a,
        "SELECT count(*), count(DISTINCT chunk_id) FROM chunk_embeddings ce "
        "JOIN chunks c ON c.id = ce.chunk_id WHERE c.document_id = :d",
        d=uuid.UUID(doc),
    )
    assert rows == [(1, 1)]


async def test_ac1_search_is_tenant_scoped(client: httpx.AsyncClient, env: StubBroker) -> None:
    a, b = await register(client), await register(client)
    kb_a = await _kb(client, a)
    await _ingest(client, env, a, kb_a, "refund.md", REFUND)
    kb_b = await _kb(client, b)
    for mode in ("hybrid", "vector", "keyword"):
        assert (await _search(client, b, "refunds within 30 days", mode=mode)).json() == {
            "results": []
        }
    r = await _search(client, b, "refunds", kb_ids=[kb_a, kb_b])
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert (await client.post(f"/v1/kbs/{kb_a}/reindex", headers=b.headers)).status_code == 404
    assert await _sql(b, "SELECT count(*) FROM chunk_embeddings") == [(0,)]


async def test_search_validates_input(client: httpx.AsyncClient) -> None:
    a = await register(client)
    for body in (
        {"query": ""},
        {"query": "x", "k": 51},
        {"query": "x", "mode": "fuzzy"},
        {"query": "x", "kb_ids": []},
    ):
        assert (await client.post("/v1/search", json=body, headers=a.headers)).status_code == 422
    assert (await client.post("/v1/search", json={"query": "x"})).status_code == 401


async def _index_dims(a: Account, kb: str) -> list[tuple[object, ...]]:
    return await _sql(
        a,
        "SELECT ei.version, ei.model, ei.status, array_agg(DISTINCT vector_dims(ce.embedding)) "
        "FROM embedding_indexes ei LEFT JOIN chunk_embeddings ce ON ce.index_id = ei.id "
        "WHERE ei.kb_id = :k GROUP BY 1, 2, 3 ORDER BY 1",
        k=uuid.UUID(kb),
    )


async def test_ac8_model_change_creates_new_version_never_mixes(
    client: httpx.AsyncClient, env: StubBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await register(client)
    kb1 = await _kb(client, a)
    await _ingest(client, env, a, kb1, "refund.md", REFUND)

    _use_model(monkeypatch, "hash", "hash-b", 24)  # operator switches the embedding model
    await _ingest(client, env, a, kb1, "ship.md", SHIPPING)
    assert await _index_dims(a, kb1) == [(1, "hash-a", "active", [16])]  # still the old model
    kb2 = await _kb(client, a)
    await _ingest(client, env, a, kb2, "refund.md", REFUND)
    assert await _index_dims(a, kb2) == [(1, "hash-b", "active", [24])]
    both = (await _search(client, a, "parcels tracking", kb_ids=[kb1, kb2], mode="vector")).json()
    # Each KB is queried with its own model; the two lists are fused, so check per KB.
    per_kb = [(h["kb_id"], h["document_name"]) for h in both["results"]]
    assert per_kb[:2] in (
        [(kb1, "ship.md"), (kb2, "refund.md")],
        [(kb2, "refund.md"), (kb1, "ship.md")],
    )

    r = await client.post(f"/v1/kbs/{kb1}/reindex", headers=a.headers)
    assert r.status_code == 202 and r.json()["status"] == "building" and r.json()["version"] == 2
    await _ingest(
        client, env, a, kb1, "late.md", b"Late arrival about refunds."
    )  # during the build
    assert await _index_dims(a, kb1) == [
        (1, "hash-a", "active", [16]),
        (2, "hash-b", "building", [24]),
    ]
    kb_now = (await client.get(f"/v1/kbs/{kb1}", headers=a.headers)).json()
    assert (
        kb_now["embedding_index_id"] != r.json()["id"]
    )  # KB stays on v1 until the build completes

    _drain(env, "index")
    assert await _index_dims(a, kb1) == [
        (1, "hash-a", "retired", [None]),
        (2, "hash-b", "active", [24]),
    ]
    assert (await client.get(f"/v1/kbs/{kb1}", headers=a.headers)).json()[
        "embedding_index_id"
    ] == r.json()["id"]
    n = await _sql(
        a, "SELECT count(*) FROM chunk_embeddings WHERE index_id = :i", i=uuid.UUID(r.json()["id"])
    )
    assert n == [(3,)]  # refund + ship + late, each embedded once
    hits = (await _search(client, a, "refunds within 30 days", kb_ids=[kb1], mode="vector")).json()
    assert hits["results"][0]["document_name"] == "refund.md"


async def test_reindex_requires_editor_and_visible_kb(client: httpx.AsyncClient) -> None:
    a = await register(client)
    r = await client.post(f"/v1/kbs/{uuid.uuid4()}/reindex", headers=a.headers)
    assert r.status_code == 404


async def test_multilingual_real_model_german_doc_english_query(
    client: httpx.AsyncClient, env: StubBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_model(
        monkeypatch, "fastembed", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384
    )
    a = await register(client)
    kb = await _kb(client, a)
    await _ingest(
        client,
        env,
        a,
        kb,
        "de.txt",
        "Rückerstattungen erfolgen innerhalb von 30 Tagen nach dem Kauf.".encode(),
    )
    await _ingest(
        client, env, a, kb, "cats.txt", b"The cat sleeps on the warm windowsill all afternoon."
    )
    hits = (await _search(client, a, "How long do refunds take?", mode="vector")).json()["results"]
    print("multilingual:", [(h["document_name"], round(h["score"], 3)) for h in hits])
    assert hits[0]["document_name"] == "de.txt"


def test_embedder_cache_loads_once() -> None:
    assert embedder_for("hash", "m", 8) is embedder_for("hash", "m", 8)


async def test_two_worker_threads_finish_concurrent_reindexes(
    client: httpx.AsyncClient, env: StubBroker
) -> None:
    """Regression (A2.3): two simultaneous reindexes hung (shared engine) or deadlocked (switch)."""
    a = await register(client)
    kbs = [await _kb(client, a, f"kb{i}") for i in range(2)]
    for kb in kbs:
        await _ingest(client, env, a, kb, "refund.md", REFUND)
    for kb in kbs:
        assert (await client.post(f"/v1/kbs/{kb}/reindex", headers=a.headers)).status_code == 202
    done = [(1, "hash-a", "retired"), (2, "hash-a", "active")]
    worker = Worker(env, queues={"index"}, worker_threads=2, worker_timeout=50)
    worker.start()
    try:
        for _ in range(300):  # 30 s; the old shared engine never finished
            states = [[v[:3] for v in await _index_dims(a, kb)] for kb in kbs]
            if states == [done, done]:
                break
            await asyncio.sleep(0.1)
    finally:
        await asyncio.to_thread(worker.stop, 5_000)
    assert states == [done, done]


async def test_ac12_every_embedding_call_writes_one_usage_row(
    client: httpx.AsyncClient, env: StubBroker
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    await _ingest(client, env, a, kb, "refund.md", REFUND)  # worker job: no user
    r = await _search(client, a, "refunds", kb_ids=[kb], mode="vector")  # request: the user
    assert r.status_code == 200
    await _search(client, a, "refunds", kb_ids=[kb], mode="keyword")  # no embedding call
    r = await client.post(f"/v1/kbs/{kb}/reindex", headers=a.headers)
    _drain(env, "index")  # one batch -> one row
    rows = await _sql(
        a,
        "SELECT provider, model, in_tok, out_tok, cost_usd, user_id FROM usage_events "
        "WHERE kind = 'embed' AND org_id = :o ORDER BY created_at",
        o=a.org_id,
    )
    assert [(p, m, o, c, u) for p, m, _, o, c, u in rows] == [
        ("hash", "hash-a", 0, None, None),  # unknown model -> cost unknown, worker -> no user
        ("hash", "hash-a", 0, None, a.user_id),
        ("hash", "hash-a", 0, None, None),
    ]
    assert rows[1][2] == 2  # "refunds": 7 chars ~ ceil(7 / 4) tokens
    assert rows[0][2] > 0 and rows[2][2] == rows[0][2]  # reindex embeds the same chunks


async def test_rerank_applies_after_fusion_and_none_is_unchanged(
    client: httpx.AsyncClient, env: StubBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1: provider none = the fused order; a reranker reorders hybrid/vector, never keyword."""
    a = await register(client)
    kb = await _kb(client, a)
    await _ingest(client, env, a, kb, "refund.md", REFUND)
    await _ingest(client, env, a, kb, "ship.md", SHIPPING)
    q, kbs = "refunds within 30 days", [uuid.UUID(kb)]

    async def run(mode: Mode) -> list[Hit]:
        async with SessionLocal() as s:
            await use_tenant(s, a.org_id, a.user_id)
            return await search(s, q, kbs, 5, mode)

    monkeypatch.setenv("RERANK_PROVIDER", "none")
    get_rerank_settings.cache_clear()
    monkeypatch.setattr(search_mod, "configured_reranker", lambda: pytest.fail("not called"))
    base = {m: await run(m) for m in ("hybrid", "vector", "keyword")}
    assert base["hybrid"][0].document_name == "refund.md"

    monkeypatch.setenv("RERANK_PROVIDER", "local")
    get_rerank_settings.cache_clear()
    monkeypatch.setattr(search_mod, "configured_reranker", lambda: ShipFirst())
    try:
        for mode in ("hybrid", "vector"):
            hits = await run(mode)
            assert [h.document_name for h in hits] == ["ship.md", "refund.md"], mode
            assert [h.score for h in hits] == [1.0, 0.0]
            cos = {h.chunk_id: h.cosine for h in base[mode]}
            assert {h.chunk_id: h.cosine for h in hits} == cos  # gate input untouched
        assert await run("keyword") == base["keyword"]
    finally:
        get_rerank_settings.cache_clear()


class ShipFirst:
    model = "ship-first"

    def scores(self, query: str, texts: list[str]) -> list[float]:
        return [float("Parcels" in t) for t in texts]


async def test_query_embedding_and_rerank_hold_no_pooled_db_connection(
    client: httpx.AsyncClient, env: StubBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow embedder or reranker must not pin a pooled connection (C1): budget check, commit,
    then the model call; the usage row is still written."""
    a = await register(client)
    kb = await _kb(client, a)
    await _ingest(client, env, a, kb, "refund.md", REFUND)  # creates the KB's embedding index
    held: list[tuple[str, int]] = []
    plain = HashEmbedder.embed

    def slow_embed(self: HashEmbedder, texts: list[str]) -> Embedded:
        held.append(("embed", engine.pool.checkedout()))  # type: ignore[attr-defined]
        time.sleep(0.05)
        return plain(self, texts)

    class SlowReranker:
        model = "slow"

        def scores(self, query: str, texts: list[str]) -> list[float]:
            held.append(("rerank", engine.pool.checkedout()))  # type: ignore[attr-defined]
            return [0.0] * len(texts)

    monkeypatch.setattr(HashEmbedder, "embed", slow_embed)
    monkeypatch.setenv("RERANK_PROVIDER", "local")
    get_rerank_settings.cache_clear()
    monkeypatch.setattr(search_mod, "configured_reranker", SlowReranker)
    try:
        before = await _sql(a, "SELECT count(*) FROM usage_events WHERE kind = 'embed'")
        r = await _search(client, a, "refunds", kb_ids=[kb], mode="hybrid")
        assert r.status_code == 200, r.text
    finally:
        get_rerank_settings.cache_clear()
    assert held == [("embed", 0), ("rerank", 0)]
    after = await _sql(a, "SELECT count(*) FROM usage_events WHERE kind = 'embed'")
    assert after[0][0] == before[0][0] + 1  # type: ignore[operator]

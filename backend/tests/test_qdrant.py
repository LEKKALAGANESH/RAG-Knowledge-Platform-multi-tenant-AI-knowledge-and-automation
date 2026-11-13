"""Qdrant vector store (Q1). Contract tests run on qdrant-client's in-memory mode; live tests need
a Qdrant at QDRANT_TEST_URL (default the throwaway 127.0.0.1:6334) and skip without one."""

import os
import uuid
from collections.abc import Iterator

import httpx
import pytest
from dramatiq.brokers.stub import StubBroker
from qdrant_client import QdrantClient
from qdrant_client.models import Filter

from app.core.db import SessionLocal, use_tenant
from app.retrieval.models import EmbeddingIndex
from app.retrieval.qdrant_store import (
    QdrantVectorStore,
    _eq,
    collection,
    get_vector_store_settings,
    qdrant_client,
)
from app.retrieval.vectorstore import PgVectorStore, vector_store
from tests.conftest import Account, register
from tests.test_retrieval import (
    REFUND,
    SHIPPING,
    _drain,
    _ingest,
    _kb,
    _search,
    _sql,
    _use_model,
    env,  # noqa: F401  (autouse: hash embedder, local blobs, stub broker)
)
from tests.test_retrieval_units import HashEmbedder

URL = os.environ.get("QDRANT_TEST_URL", "http://127.0.0.1:6334")
Rows = tuple[tuple[uuid.UUID, uuid.UUID], ...] | list[tuple[uuid.UUID, uuid.UUID]]
WARRANTY = b"# Warranty\n\nDevices carry a two year warranty against defects.\n"


def _index(
    org: uuid.UUID, kb: uuid.UUID, dim: int = 4, iid: uuid.UUID | None = None
) -> EmbeddingIndex:
    return EmbeddingIndex(
        id=iid or uuid.uuid4(), org_id=org, kb_id=kb, provider="hash", model="m", dim=dim,
        version=1, status="active",
    )  # fmt: skip


class _Session:
    """Stand-in for a tenant-scoped AsyncSession: `info` as use_tenant sets it, and the chunk ->
    document rows the store reads from Postgres."""

    def __init__(self, org: uuid.UUID | None, rows: Rows = ()) -> None:
        self.info = {} if org is None else {"tenant": (org, None)}
        self.rows = list(rows)

    async def execute(self, _stmt: object) -> list[tuple[uuid.UUID, uuid.UUID]]:
        return self.rows


def _store(c: QdrantClient, org: uuid.UUID | None, rows: Rows = ()) -> QdrantVectorStore:
    return QdrantVectorStore(_Session(org, rows), c)  # type: ignore[arg-type]


@pytest.fixture
def stub(env: StubBroker) -> StubBroker:  # noqa: F811
    return env


@pytest.fixture
def mem() -> QdrantClient:
    return QdrantClient(":memory:")


async def _seed(mem: QdrantClient) -> tuple[EmbeddingIndex, list[uuid.UUID], list[uuid.UUID]]:
    org, kb = uuid.uuid4(), uuid.uuid4()
    index = _index(org, kb)
    chunks, docs = [uuid.uuid4() for _ in range(3)], [uuid.uuid4(), uuid.uuid4()]
    rows = [(chunks[0], docs[0]), (chunks[1], docs[0]), (chunks[2], docs[1])]
    store = _store(mem, org, rows)
    await store.create_index(index)
    await store.upsert(index, chunks, [[1, 0, 0, 0], [0.9, 0.1, 0, 0], [0, 1, 0, 0]])
    return index, chunks, docs


async def test_search_cosine_and_delete_by_document(mem: QdrantClient) -> None:
    index, chunks, docs = await _seed(mem)
    store = _store(mem, index.org_id)
    hits = await store.search(index, [1, 0, 0, 0], 3)
    assert [c for c, _ in hits] == chunks and hits[0][1] == pytest.approx(1.0)
    await store.delete_documents(index, [docs[0]])
    assert [c for c, _ in await store.search(index, [1, 0, 0, 0], 3)] == [chunks[2]]
    assert mem.retrieve(collection(index), [str(chunks[2])])[0].payload == {
        "org_id": str(index.org_id), "kb_id": str(index.kb_id), "document_id": str(docs[1]),
    }  # ids only: chunk text never leaves Postgres  # fmt: skip
    await store.drop_index(index)
    assert not mem.collection_exists(collection(index))


async def test_no_query_without_the_tenant_org_filter(mem: QdrantClient) -> None:
    """Every read and delete carries org_id (from the session's RLS tenant) and kb_id; a session
    without a tenant, or an index row of another org, cannot reach the client at all."""
    index, _, docs = await _seed(mem)
    seen: list[Filter] = []
    query, delete = mem.query_points, mem.delete
    mem.query_points = lambda *a, **kw: (seen.append(kw["query_filter"]), query(*a, **kw))[1]  # type: ignore[method-assign]
    mem.delete = lambda name, sel, **kw: (seen.append(sel.filter), delete(name, sel, **kw))[1]  # type: ignore[method-assign]
    await _store(mem, index.org_id).search(index, [1, 0, 0, 0], 2)
    await _store(mem, index.org_id).delete_documents(index, docs)
    want = [_eq("org_id", index.org_id), _eq("kb_id", index.kb_id)]
    assert len(seen) == 2 and all(f.must[:2] == want for f in seen)  # type: ignore[index]

    for org in (None, uuid.uuid4()):  # no tenant / a different tenant than the index's org
        store = _store(mem, org)
        for call in (
            store.search(index, [1, 0, 0, 0], 2),
            store.delete_documents(index, docs),
            store.drop_index(index),
            store.create_index(index),
            store.upsert(index, [], []),
        ):
            with pytest.raises(PermissionError):
                await call
    assert len(seen) == 2 and mem.collection_exists(collection(index))


async def test_forged_kb_id_in_own_org_finds_nothing(mem: QdrantClient) -> None:
    index, _, _ = await _seed(mem)
    other = uuid.uuid4()  # org B claims org A's index id and KB id, under its own tenant
    forged = _index(other, index.kb_id, iid=index.id)
    assert await _store(mem, other).search(forged, [1, 0, 0, 0], 3) == []


async def test_upsert_refuses_chunks_not_visible_in_the_kb(mem: QdrantClient) -> None:
    index, _, _ = await _seed(mem)
    with pytest.raises(LookupError):
        await _store(mem, index.org_id, []).upsert(index, [uuid.uuid4()], [[1, 0, 0, 0]])


def test_vector_store_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    get_vector_store_settings.cache_clear()
    assert isinstance(vector_store(_Session(None)), PgVectorStore)  # type: ignore[arg-type]
    monkeypatch.setenv("VECTOR_STORE", "qdrant")
    get_vector_store_settings.cache_clear()
    try:
        assert isinstance(vector_store(_Session(None)), QdrantVectorStore)  # type: ignore[arg-type]
    finally:
        get_vector_store_settings.cache_clear()


# --- live: real Qdrant + Postgres through the API -------------------------------------------


def _reachable() -> bool:
    try:
        return httpx.get(f"{URL}/readyz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


live = pytest.mark.skipif(not _reachable(), reason=f"no Qdrant at {URL}")


def _use_store(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("VECTOR_STORE", name)
    monkeypatch.setenv("QDRANT_URL", URL)
    get_vector_store_settings.cache_clear()
    qdrant_client.cache_clear()


@pytest.fixture
def qd(monkeypatch: pytest.MonkeyPatch) -> Iterator[QdrantClient]:
    _use_store(monkeypatch, "qdrant")
    yield qdrant_client()
    get_vector_store_settings.cache_clear()
    qdrant_client.cache_clear()


async def _active(a: Account, kb: str) -> uuid.UUID:
    ((iid,),) = await _sql(
        a, "SELECT id FROM embedding_indexes WHERE kb_id = :k AND status = 'active'",
        k=uuid.UUID(kb),
    )  # fmt: skip
    assert isinstance(iid, uuid.UUID)
    return iid


def _count(qd: QdrantClient, iid: uuid.UUID, doc: str | None = None) -> int:
    flt = None if doc is None else Filter(must=[_eq("document_id", uuid.UUID(doc))])
    return qd.count(f"rag_{iid.hex}", count_filter=flt, exact=True).count


@live
async def test_live_parity_with_pgvector(
    client: httpx.AsyncClient,
    stub: StubBroker,
    qd: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = await register(client)
    kbs: dict[str, str] = {}
    for name in ("pgvector", "qdrant"):
        _use_store(monkeypatch, name)
        kbs[name] = await _kb(client, a, name)
        for fname, data in (("refund.md", REFUND), ("ship.md", SHIPPING), ("w.md", WARRANTY)):
            await _ingest(client, stub, a, kbs[name], fname, data)
    iid = await _active(a, kbs["qdrant"])
    assert _count(qd, iid) == 3
    assert await _sql(a, "SELECT count(*) FROM chunk_embeddings WHERE index_id = :i", i=iid) == [
        (0,)
    ]  # vectors live only in Qdrant
    for q in ("refunds within 30 days", "parcels tracking", "two year warranty defects"):
        got = {}
        for name, kb in kbs.items():
            _use_store(monkeypatch, name)
            for mode in ("vector", "hybrid"):
                r = await _search(client, a, q, kb_ids=[kb], k=3, mode=mode)
                assert r.status_code == 200, r.text
                got[name, mode] = [(h["document_name"], h["score"]) for h in r.json()["results"]]
        for mode in ("vector", "hybrid"):
            pg, qq = got["pgvector", mode], got["qdrant", mode]
            assert [n for n, _ in pg] == [n for n, _ in qq], (q, mode)
            assert [s for _, s in pg] == pytest.approx([s for _, s in qq], abs=1e-4), (q, mode)


@live
async def test_live_reprocess_and_delete_propagate(
    client: httpx.AsyncClient,
    stub: StubBroker,
    qd: QdrantClient,
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    refund = await _ingest(client, stub, a, kb, "refund.md", REFUND)
    ship = await _ingest(client, stub, a, kb, "ship.md", SHIPPING)
    iid = await _active(a, kb)
    await client.post(f"/v1/documents/{refund}/reprocess", headers=a.headers)
    _drain(stub, "ingest")
    assert (_count(qd, iid, refund), _count(qd, iid)) == (1, 2)  # old chunk ids replaced
    assert (await client.delete(f"/v1/documents/{ship}", headers=a.headers)).status_code == 204
    assert (_count(qd, iid, ship), _count(qd, iid)) == (0, 1)
    hits = (await _search(client, a, "parcels tracking", kb_ids=[kb], mode="vector")).json()
    assert [h["document_name"] for h in hits["results"]] == ["refund.md"]


@live
async def test_live_index_version_switch(
    client: httpx.AsyncClient,
    stub: StubBroker,
    qd: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    await _ingest(client, stub, a, kb, "refund.md", REFUND)
    old = await _active(a, kb)
    _use_model(monkeypatch, "hash", "hash-b", 24)
    assert (await client.post(f"/v1/kbs/{kb}/reindex", headers=a.headers)).status_code == 202
    await _ingest(client, stub, a, kb, "ship.md", SHIPPING)  # during the build: both versions
    assert (_count(qd, old), qd.collection_exists(f"rag_{old.hex}")) == (2, True)
    _drain(stub, "index")
    new = await _active(a, kb)
    assert new != old and not qd.collection_exists(f"rag_{old.hex}")
    assert qd.get_collection(f"rag_{new.hex}").config.params.vectors.size == 24  # type: ignore[union-attr]
    assert _count(qd, new) == 2
    hits = (await _search(client, a, "refunds within 30 days", kb_ids=[kb], mode="vector")).json()
    assert hits["results"][0]["document_name"] == "refund.md"


@live
async def test_live_cross_org_isolation(
    client: httpx.AsyncClient,
    stub: StubBroker,
    qd: QdrantClient,
) -> None:
    a, b = await register(client), await register(client)
    kb_a = await _kb(client, a)
    await _ingest(client, stub, a, kb_a, "refund.md", REFUND)
    await _kb(client, b)
    for mode in ("hybrid", "vector"):
        r = await _search(client, b, "refunds within 30 days", mode=mode)
        assert r.json() == {"results": []}
    iid = await _active(a, kb_a)
    (qvec,), _ = HashEmbedder("hash-a", 16).embed(["refunds within 30 days"])
    async with SessionLocal() as s:
        await use_tenant(s, b.org_id, b.user_id)
        store = QdrantVectorStore(s, qd)
        forged = _index(b.org_id, uuid.UUID(kb_a), 16, iid)  # B's org, A's KB and index ids
        assert await store.search(forged, qvec, 5) == []
        with pytest.raises(PermissionError):
            await store.search(_index(a.org_id, uuid.UUID(kb_a), 16, iid), qvec, 5)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        own = _index(a.org_id, uuid.UUID(kb_a), 16, iid)
        assert len(await QdrantVectorStore(s, qd).search(own, qvec, 5)) == 1


@live
async def test_live_kb_delete_drops_collection(
    client: httpx.AsyncClient,
    stub: StubBroker,
    qd: QdrantClient,
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    await _ingest(client, stub, a, kb, "refund.md", REFUND)
    iid = await _active(a, kb)
    assert qd.collection_exists(f"rag_{iid.hex}")
    assert (await client.delete(f"/v1/kbs/{kb}", headers=a.headers)).status_code == 204
    assert not qd.collection_exists(f"rag_{iid.hex}")

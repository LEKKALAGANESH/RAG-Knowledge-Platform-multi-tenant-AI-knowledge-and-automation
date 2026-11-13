import asyncio
import hashlib
import os
import uuid
from collections.abc import Iterator

import dramatiq
import httpx
import pytest
from dramatiq.brokers.stub import StubBroker
from dramatiq.middleware import CurrentMessage
from sqlalchemy import select, text

from app.core.db import SessionLocal, use_tenant
from app.ingestion import mime, storage, tasks
from app.ingestion.config import IngestionSettings, get_ingestion_settings
from app.ingestion.pipeline import process_document
from app.kb.models import Chunk
from tests import fixtures as fx
from tests.conftest import Account, register


@pytest.fixture(autouse=True)
def stub_broker(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> Iterator[StubBroker]:
    monkeypatch.setenv("BLOB_BACKEND", "local")
    monkeypatch.setenv("BLOB_DIR", str(tmp_path))
    monkeypatch.setenv("OCR_ENABLED", "false")  # same expectations with or without tesseract
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()
    stub = StubBroker()  # type: ignore[no-untyped-call]
    for actor in (tasks.ingest_document, tasks.ingest_retries_exhausted):
        monkeypatch.setattr(actor, "broker", stub)
        stub.declare_actor(actor)
    yield stub
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()


async def _kb(client: httpx.AsyncClient, a: Account) -> str:
    r = await client.post("/v1/kbs", json={"name": "kb"}, headers=a.headers)
    return str(r.json()["id"])


async def _upload(
    client: httpx.AsyncClient, a: Account, kb: str, name: str, data: bytes
) -> httpx.Response:
    return await client.post(
        f"/v1/kbs/{kb}/documents", files={"file": (name, data)}, headers=a.headers
    )


async def _ingest(
    client: httpx.AsyncClient, a: Account, name: str, data: bytes
) -> dict[str, object]:
    kb = await _kb(client, a)
    r = await _upload(client, a, kb, name, data)
    assert r.status_code == 201, r.text
    await process_document(uuid.UUID(r.json()["id"]), a.org_id)
    doc = (await client.get(f"/v1/documents/{r.json()['id']}", headers=a.headers)).json()
    assert isinstance(doc, dict)
    return doc


async def _chunks(a: Account, doc_id: str) -> list[Chunk]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        stmt = select(Chunk).where(Chunk.document_id == uuid.UUID(doc_id)).order_by(Chunk.ord)
        return list((await s.scalars(stmt)).all())


FORMATS = [
    ("a.pdf", fx.pdf(["Refund policy is 30 days", None, "Shipping takes 5 days"]),
     [{"page": 1}, {"page": 3}]),
    ("b.docx", fx.docx(), [{"section": "Leave Policy"}, {"section": "Expenses"},
                           {"section": "Expenses", "table": 1}]),
    ("c.xlsx", fx.xlsx(), [{"sheet": "Invoices", "range": "A1:B3"},
                           {"sheet": "Vendors", "range": "A1:B2"}]),
    ("d.csv", fx.CSV, [{"range": "A1:B3"}]),
    ("e.md", fx.MD, [{"section": "Intro"}, {"section": "Security"}]),
    ("f.txt", fx.TXT, [{}]),
    ("g.html", fx.HTML, [{"section": "Onboarding guide"}]),
    ("h.json", fx.JSON, [{"record": 1}, {"record": 2}]),
    ("i.jsonl", fx.JSONL, [{"record": 1}, {"record": 2}]),
]  # fmt: skip


@pytest.mark.parametrize(("name", "data", "locs"), FORMATS, ids=[f[0] for f in FORMATS])
async def test_ac2_each_format_yields_located_chunks(
    client: httpx.AsyncClient, stub_broker: StubBroker, name: str, data: bytes, locs: list[object]
) -> None:
    a = await register(client)
    doc = await _ingest(client, a, name, data)
    assert doc["status"] == "ready" and doc["error"] is None
    chunks = await _chunks(a, str(doc["id"]))
    assert [c.loc for c in chunks] == locs
    assert [c.ord for c in chunks] == list(range(len(chunks))) and all(c.text for c in chunks)
    assert stub_broker.queues["ingest"].qsize() == 1  # upload enqueued exactly one job


async def test_ac2_pdf_page_without_text_is_a_warning(client: httpx.AsyncClient) -> None:
    doc = await _ingest(client, await register(client), "a.pdf", fx.pdf(["Text", None]))
    assert doc["status"] == "ready" and doc["warnings"] == ["page 2: no text, OCR not enabled"]


async def test_ac2_corrupt_file_fails_with_reason(client: httpx.AsyncClient) -> None:
    a = await register(client)
    doc = await _ingest(client, a, "bad.pdf", fx.CORRUPT_PDF)
    assert doc["status"] == "failed"
    assert "could not be read" in str(doc["error"])
    assert await _chunks(a, str(doc["id"])) == []


async def test_ac2_zero_chunks_is_failed_never_ready(client: httpx.AsyncClient) -> None:
    a = await register(client)
    doc = await _ingest(client, a, "scan.pdf", fx.pdf([None, None]))
    assert doc["status"] == "failed"
    assert doc["error"] == (
        "No text could be extracted from this file. "
        "(page 1: no text, OCR not enabled; page 2: no text, OCR not enabled)"
    )


async def test_ac3_duplicate_upload_not_reprocessed(
    client: httpx.AsyncClient, stub_broker: StubBroker
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    first = await _upload(client, a, kb, "a.md", fx.MD)
    await process_document(uuid.UUID(first.json()["id"]), a.org_id)
    again = await _upload(client, a, kb, "renamed.md", fx.MD)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["id"] == first.json()["id"] and again.json()["status"] == "ready"
    assert stub_broker.queues["ingest"].qsize() == 1  # second upload enqueued nothing
    other_kb = await _kb(client, a)  # dedupe is per KB
    assert (await _upload(client, a, other_kb, "a.md", fx.MD)).status_code == 201


async def test_upload_not_swallowed_by_crawled_or_deleted_copy(client: httpx.AsyncClient) -> None:
    """Regression: dedupe matched a crawled doc, or a deleted one, with the same sha256."""
    from app.connectors.models import Source
    from app.kb.models import Document

    a = await register(client)
    kb = await _kb(client, a)
    sha = hashlib.sha256(fx.MD).hexdigest()
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        src = Source(org_id=a.org_id, kb_id=uuid.UUID(kb), type="website", name="w", config={},
                     cursor={})  # fmt: skip
        s.add(src)
        await s.flush()
        s.add(Document(org_id=a.org_id, kb_id=uuid.UUID(kb), source_id=src.id, external_key="u",
                       name="u", mime="text/markdown", sha256=sha, blob_key="x", warnings=[],
                       meta={}, status="ready"))  # fmt: skip
        await s.commit()
    first = await _upload(client, a, kb, "a.md", fx.MD)
    assert first.status_code == 201, first.text  # the crawled copy did not swallow it
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(
            text("UPDATE documents SET status = 'deleted' WHERE id = :d"), {"d": first.json()["id"]}
        )
        await s.commit()
    again = await _upload(client, a, kb, "a.md", fx.MD)
    assert again.status_code == 201, again.text  # the deleted copy did not swallow it
    assert again.json()["id"] != first.json()["id"] and again.json()["version"] == 2


async def test_reprocess_is_idempotent(client: httpx.AsyncClient, stub_broker: StubBroker) -> None:
    a = await register(client)
    doc = await _ingest(client, a, "e.md", fx.MD)
    before = [c.id for c in await _chunks(a, str(doc["id"]))]
    r = await client.post(f"/v1/documents/{doc['id']}/reprocess", headers=a.headers)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert stub_broker.queues["ingest"].qsize() == 2
    await process_document(uuid.UUID(str(doc["id"])), a.org_id)
    after = await _chunks(a, str(doc["id"]))
    assert len(after) == len(before) and {c.id for c in after}.isdisjoint(before)


async def test_tenant_isolation(client: httpx.AsyncClient) -> None:
    a, b = await register(client), await register(client)
    kb = await _kb(client, a)
    doc_id = (await _upload(client, a, kb, "e.md", fx.MD)).json()["id"]
    assert (await _upload(client, b, kb, "e.md", fx.MD)).status_code == 404
    assert (await client.get(f"/v1/documents/{doc_id}", headers=b.headers)).status_code == 404
    reprocess = await client.post(f"/v1/documents/{doc_id}/reprocess", headers=b.headers)
    assert reprocess.status_code == 404
    await process_document(uuid.UUID(doc_id), b.org_id)  # wrong org: RLS hides the doc, no-op
    assert (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).json()["status"] == (
        "queued"
    )


async def test_upload_rejects_bad_input(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    exe = b"MZ\x90\x00\x03\x00" + b"\x00" * 64  # was a PNG until B8 made images supported
    assert (await _upload(client, a, kb, "x.pdf", exe)).json()["error"]["code"] == (
        "unsupported_type"
    )
    assert (await _upload(client, a, kb, "x.txt", b"")).status_code == 422
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_ingestion_settings.cache_clear()
    assert (await _upload(client, a, kb, "x.txt", b"a" * (2**20 + 1))).status_code == 413


async def test_delete_removes_blob(client: httpx.AsyncClient) -> None:
    a = await register(client)
    doc = await _ingest(client, a, "e.md", fx.MD)
    blobs = storage.get_blob_store()
    key = f"{a.org_id}/{doc['kb_id']}/{doc['sha256']}"
    assert blobs.get(key) == fx.MD
    assert (await client.delete(f"/v1/documents/{doc['id']}", headers=a.headers)).status_code == 204
    with pytest.raises(FileNotFoundError):
        blobs.get(key)


async def test_transient_error_raises_then_exhaustion_marks_failed(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    doc_id = (await _upload(client, a, kb, "e.md", fx.MD)).json()["id"]
    storage.get_blob_store().delete(f"{a.org_id}/{kb}/{hashlib.sha256(fx.MD).hexdigest()}")
    with pytest.raises(FileNotFoundError):  # raised -> Dramatiq retries with backoff
        await process_document(uuid.UUID(doc_id), a.org_id)
    message = {"args": [doc_id, str(a.org_id)]}
    await asyncio.to_thread(tasks.ingest_retries_exhausted.fn, message, {})
    doc = (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).json()
    assert doc["status"] == "failed" and doc["error"] == tasks.RETRY_EXHAUSTED


def test_local_blob_store_rejects_traversal(tmp_path: object) -> None:
    store = storage.LocalBlobStore(str(tmp_path))
    with pytest.raises(ValueError, match="escapes"):
        store.put("../evil", b"x")


@pytest.mark.skipif(not os.environ.get("S3_ACCESS_KEY"), reason="S3_ACCESS_KEY not set (MinIO)")
def test_s3_blob_store_round_trip() -> None:
    store = storage.S3BlobStore(IngestionSettings(s3_bucket=f"rag-test-{uuid.uuid4().hex[:8]}"))
    key = f"{uuid.uuid4()}/doc.bin"
    store.put(key, b"hello minio")
    assert store.get(key) == b"hello minio"
    store.delete(key)
    with pytest.raises(store.client.exceptions.NoSuchKey):
        store.get(key)


async def test_unsupported_type_message_lists_every_accepted_type(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    r = await _upload(client, a, await _kb(client, a), "x.bin", b"\x89PNG\x00\x00")
    message = r.json()["error"]["message"]
    assert r.status_code == 415 and "JSONL" in message
    assert all(label in message for label in mime.LABELS.values())
    assert set(mime.LABELS) == mime.SUPPORTED


def _run_once(stub: StubBroker, doc_id: str, org_id: uuid.UUID, **options: int) -> None:
    """Runs one real worker pass of ingest_document (plus any on_retry_exhausted follow-up)."""
    stub.add_middleware(CurrentMessage())
    stub.flush_all()  # drop the job the upload enqueued; we send our own with options
    tasks.ingest_document.send_with_options(args=(doc_id, str(org_id)), **options)
    worker = dramatiq.Worker(stub, worker_timeout=50)
    worker.start()
    try:
        stub.queues["ingest"].join()  # the delayed retry (ingest.DQ) is deliberately not awaited
    finally:
        worker.stop()


async def test_retrying_document_shows_processing_with_reason(
    client: httpx.AsyncClient, stub_broker: StubBroker
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    doc_id = (await _upload(client, a, kb, "e.md", fx.MD)).json()["id"]
    key = f"{a.org_id}/{kb}/{hashlib.sha256(fx.MD).hexdigest()}"
    storage.get_blob_store().delete(key)
    # 1st attempt fails; the retry is parked for 10 min so we observe the in-between state.
    await asyncio.to_thread(_run_once, stub_broker, doc_id, a.org_id, min_backoff=600_000)
    doc = (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).json()
    assert doc["status"] == "processing"
    assert doc["error"] == "Retrying (attempt 1 of 5): the stored file could not be found."
    storage.get_blob_store().put(key, fx.MD)
    await process_document(uuid.UUID(doc_id), a.org_id)  # a later retry succeeds
    doc = (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).json()
    assert doc["status"] == "ready" and doc["error"] is None


async def test_last_retry_failure_keeps_exhausted_message(
    client: httpx.AsyncClient, stub_broker: StubBroker
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    doc_id = (await _upload(client, a, kb, "e.md", fx.MD)).json()["id"]
    storage.get_blob_store().delete(f"{a.org_id}/{kb}/{hashlib.sha256(fx.MD).hexdigest()}")
    # retries=5 = this is the last attempt; its failure hands off to ingest_retries_exhausted.
    await asyncio.to_thread(_run_once, stub_broker, doc_id, a.org_id, retries=5)
    doc = (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).json()
    assert doc["status"] == "failed" and doc["error"] == tasks.RETRY_EXHAUSTED


async def test_chunks_endpoint_pages_in_order_and_is_tenant_scoped(
    client: httpx.AsyncClient,
) -> None:
    a, b = await register(client), await register(client)
    doc = await _ingest(client, a, "b.docx", fx.docx())
    url = f"/v1/documents/{doc['id']}/chunks"
    first = (await client.get(url, params={"limit": 2}, headers=a.headers)).json()
    assert [c["ord"] for c in first["items"]] == [0, 1] and first["next_cursor"]
    assert first["items"][0]["loc"] == {"section": "Leave Policy"} and first["items"][0]["text"]
    rest = await client.get(url, params={"cursor": first["next_cursor"]}, headers=a.headers)
    assert [c["ord"] for c in rest.json()["items"]] == [2] and rest.json()["next_cursor"] is None
    assert set(rest.json()["items"][0]) == {"id", "ord", "text", "loc"}
    assert (await client.get(url, headers=b.headers)).status_code == 404
    bad = await client.get(url, params={"cursor": "!!"}, headers=a.headers)
    assert bad.status_code == 422
    assert (await client.get(url, params={"limit": 201}, headers=a.headers)).status_code == 422


async def test_two_worker_threads_finish_concurrent_jobs(
    client: httpx.AsyncClient, stub_broker: StubBroker
) -> None:
    """Regression (A2.2): with 2 worker threads one of two simultaneous jobs hung forever.
    Two KBs: each KB's first job creates its HNSW index while the other KB's job writes vectors."""
    a = await register(client)
    kbs = [await _kb(client, a), await _kb(client, a)]
    ids = [
        (
            await _upload(client, a, kbs[i % 2], f"{i}.txt", f"doc {i} {uuid.uuid4()}".encode())
        ).json()["id"]
        for i in range(4)
    ]

    def run() -> None:
        worker = dramatiq.Worker(stub_broker, worker_threads=2, worker_timeout=50)
        worker.start()
        try:
            stub_broker.join("ingest", timeout=60_000)
        finally:
            worker.stop(timeout=5_000)

    await asyncio.to_thread(run)
    for doc_id in ids:
        doc = (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).json()
        assert doc["status"] == "ready", doc


async def test_kb_delete_drops_its_hnsw_indexes(client: httpx.AsyncClient) -> None:
    a = await register(client)
    doc = await _ingest(client, a, "e.md", fx.MD)  # first embed creates the KB's index + HNSW
    kb = (await client.get(f"/v1/kbs/{doc['kb_id']}", headers=a.headers)).json()
    name = "chunk_embeddings_hnsw_" + str(kb["embedding_index_id"]).replace("-", "")
    exists_sql = text("SELECT count(*) FROM pg_indexes WHERE indexname = :n")
    async with SessionLocal() as s:
        assert await s.scalar(exists_sql, {"n": name}) == 1
    assert (await client.delete(f"/v1/kbs/{kb['id']}", headers=a.headers)).status_code == 204
    async with SessionLocal() as s:
        assert await s.scalar(exists_sql, {"n": name}) == 0


async def test_bomb_fails_once_fast_and_the_worker_goes_on(
    client: httpx.AsyncClient, stub_broker: StubBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ingestion_units import xlsx_bomb

    a = await register(client)
    kb = await _kb(client, a)
    bomb = (await _upload(client, a, kb, "bomb.xlsx", xlsx_bomb(rows=200_000))).json()["id"]
    normal = (await _upload(client, a, kb, "ok.txt", b"after the bomb")).json()["id"]
    attempts = 0

    async def counted(*args: object) -> None:
        nonlocal attempts
        attempts += 1
        await process_document(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(tasks, "process_document", counted)

    def run() -> None:
        worker = dramatiq.Worker(stub_broker, worker_threads=1, worker_timeout=50)
        worker.start()
        try:
            stub_broker.join("ingest", fail_fast=False, timeout=60_000)
        finally:
            worker.stop(timeout=5_000)

    await asyncio.to_thread(run)
    get = lambda d: client.get(f"/v1/documents/{d}", headers=a.headers)  # noqa: E731
    failed, ready = (await get(bomb)).json(), (await get(normal)).json()
    assert failed["status"] == "failed" and "expands to more content" in failed["error"]
    assert ready["status"] == "ready"
    assert attempts == 2  # one each: a bomb is a permanent failure, never retried


async def test_chunk_cap_fails_the_whole_document(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_CHUNKS_PER_DOC", "2")
    monkeypatch.setenv("CHUNK_TOKENS", "50")
    get_ingestion_settings.cache_clear()
    a = await register(client)
    doc = await _ingest(client, a, "long.txt", ("word " * 49 + "\n").encode() * 10)
    assert doc["status"] == "failed" and "the limit is 2" in str(doc["error"])
    assert await _chunks(a, str(doc["id"])) == []

"""Monthly org budget: checked before every model call; block = readable 402, warn = proceed +
flag. Never a silent switch to a cheaper model."""

import asyncio
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from tests.conftest import Account, register  # isort: skip  (sets the test env first)
from app.agent import tasks as agent_tasks
from app.agent.loop import advance
from app.agent.models import AgentRun
from app.core.db import SessionLocal, use_tenant
from app.ingestion import storage
from app.ingestion import tasks as ingest_tasks
from app.ingestion.config import get_ingestion_settings
from app.ingestion.pipeline import process_document
from app.kb.models import Document
from app.llm import providers
from app.llm.config import LLMSettings
from app.llm.providers import LLM, get_llm
from app.llm.usage import UsageEvent
from app.main import app
from app.usage.budget import BudgetExceeded
from tests.test_agent import ScriptedToolProvider, say
from tests.test_rag import ScriptedProvider, parse_sse, seed_kb


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> Iterator[Any]:
    from dramatiq.brokers.stub import StubBroker

    monkeypatch.setenv("BLOB_BACKEND", "local")
    monkeypatch.setenv("BLOB_DIR", str(tmp_path))
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()
    stub = StubBroker()  # type: ignore[no-untyped-call]
    actors = (
        agent_tasks.run_agent,
        ingest_tasks.ingest_document,
        ingest_tasks.ingest_retries_exhausted,
    )
    for actor in actors:
        monkeypatch.setattr(actor, "broker", stub)
        stub.declare_actor(actor)
    yield stub
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()


async def set_budget(
    client: httpx.AsyncClient, a: Account, usd: str | None, action: str = "block"
) -> httpx.Response:
    r = await client.patch(
        f"/v1/orgs/{a.org_id}/settings",
        json={"monthly_budget_usd": usd, "budget_action": action},
        headers=a.headers,
    )
    assert r.status_code == 200, r.text
    return r


async def spend(a: Account, usd: str) -> None:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        s.add(UsageEvent(
            org_id=a.org_id, user_id=a.user_id, kind="chat", provider="p", model="m",
            in_tok=1, out_tok=1, cost_usd=Decimal(usd),
        ))  # fmt: skip
        await s.commit()


async def test_settings_round_trip_and_spend(client: httpx.AsyncClient) -> None:
    a = await register(client)
    r = await client.get(f"/v1/orgs/{a.org_id}/settings", headers=a.headers)
    assert r.json() == {"monthly_budget_usd": None, "budget_action": "block",
                        "month_spend_usd": "0"}  # fmt: skip
    await spend(a, "1.25")
    body = (await set_budget(client, a, "10.50", "warn")).json()
    assert body["monthly_budget_usd"] == "10.50" and body["budget_action"] == "warn"
    assert Decimal(body["month_spend_usd"]) == Decimal("1.25")
    # partial update keeps the other field; null = unlimited
    r = await client.patch(
        f"/v1/orgs/{a.org_id}/settings", json={"monthly_budget_usd": None}, headers=a.headers
    )
    assert r.json()["monthly_budget_usd"] is None and r.json()["budget_action"] == "warn"
    bad = await client.patch(
        f"/v1/orgs/{a.org_id}/settings", json={"monthly_budget_usd": "-1"}, headers=a.headers
    )
    assert bad.status_code == 422
    other = await register(client)
    r = await client.patch(
        f"/v1/orgs/{a.org_id}/settings", json={"monthly_budget_usd": "1"}, headers=other.headers
    )
    assert r.status_code == 404


async def test_block_refuses_answer_with_402_before_any_provider_call(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    kb, _ = await seed_kb(client, a, ["Refunds are issued within 30 days."])
    await set_budget(client, a, "1.00")
    await spend(a, "1.00")
    p = ScriptedProvider(["should never stream"])
    app.dependency_overrides[get_llm] = lambda: LLM(chat=[p], rewrite=p)
    try:
        r = await client.post("/v1/answer", json={"question": "refunds", "kb_ids": [kb]},
                              headers=a.headers)  # fmt: skip
    finally:
        app.dependency_overrides.pop(get_llm, None)
    assert r.status_code == 402, r.text
    err = r.json()["error"]
    assert err["code"] == "budget_exceeded" and "$1.00 of its $1.00" in err["message"]
    assert p.calls == []


async def test_budget_check_runs_before_the_llm_not_configured_check(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await register(client)
    await set_budget(client, a, "0")
    monkeypatch.setattr(
        providers, "get_llm_settings", lambda: LLMSettings(_env_file=None, anthropic_api_key=None)
    )
    providers._build.cache_clear()
    try:
        r = await client.post("/v1/answer", json={"question": "q"}, headers=a.headers)
    finally:
        providers._build.cache_clear()
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget_exceeded"


async def test_warn_proceeds_and_flags_the_answer(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb, _ = await seed_kb(client, a, ["Refunds are issued within 30 days."])
    await set_budget(client, a, "1.00", "warn")
    await spend(a, "2.00")
    p = ScriptedProvider(["Within 30 days [c1]."])
    app.dependency_overrides[get_llm] = lambda: LLM(chat=[p], rewrite=p)
    try:
        r = await client.post("/v1/answer", json={"question": "refunds", "kb_ids": [kb]},
                              headers=a.headers)  # fmt: skip
    finally:
        app.dependency_overrides.pop(get_llm, None)
    assert r.status_code == 200
    events = parse_sse(r.text)
    assert events[-1][0] == "done" and events[-1][1]["budget_warning"] is True
    assert len(p.calls) == 1  # same model as always: warn never downgrades


async def test_under_budget_answer_has_no_flag(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb, _ = await seed_kb(client, a, ["Refunds are issued within 30 days."])
    await set_budget(client, a, "100")
    p = ScriptedProvider(["Within 30 days [c1]."])
    app.dependency_overrides[get_llm] = lambda: LLM(chat=[p], rewrite=p)
    try:
        r = await client.post("/v1/answer", json={"question": "refunds", "kb_ids": [kb]},
                              headers=a.headers)  # fmt: skip
    finally:
        app.dependency_overrides.pop(get_llm, None)
    assert "budget_warning" not in parse_sse(r.text)[-1][1]


async def test_agent_start_402_and_a_running_agent_fails_readably(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    await set_budget(client, a, "1.00")
    run = await client.post("/v1/agent/runs", json={"goal": "g"}, headers=a.headers)
    assert run.status_code == 202
    await spend(a, "5")  # the budget runs out while the run waits for the worker
    blocked = await client.post("/v1/agent/runs", json={"goal": "g"}, headers=a.headers)
    assert blocked.status_code == 402 and blocked.json()["error"]["code"] == "budget_exceeded"

    p = ScriptedToolProvider([[say("done")]])
    run_id = uuid.UUID(run.json()["id"])
    await advance(run_id, a.org_id, a.user_id, SessionLocal, lambda: p)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        row = await s.get(AgentRun, run_id)
    assert row is not None and row.status == "failed"
    assert row.result == {"error_code": "budget_exceeded"} and "monthly AI budget" in str(row.error)
    assert p.calls == []


async def test_warn_header_on_agent_start(client: httpx.AsyncClient) -> None:
    a = await register(client)
    await set_budget(client, a, "0", "warn")
    r = await client.post("/v1/agent/runs", json={"goal": "g"}, headers=a.headers)
    assert r.status_code == 202 and r.headers["x-budget-warning"] == "over_budget"


async def test_ingestion_fails_the_document_readably_without_embedding(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    kb = (await client.post("/v1/kbs", json={"name": "kb"}, headers=a.headers)).json()["id"]
    up = await client.post(f"/v1/kbs/{kb}/documents", files={"file": ("a.txt", b"hello world")},
                           headers=a.headers)  # fmt: skip
    assert up.status_code == 201, up.text
    await set_budget(client, a, "0")
    with pytest.raises(BudgetExceeded):
        await process_document(uuid.UUID(up.json()["id"]), a.org_id)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        doc = await s.get(Document, uuid.UUID(up.json()["id"]))
        embeds = await s.scalar(
            select(func.count()).select_from(UsageEvent).where(UsageEvent.kind == "embed")
        )
    assert doc is not None and doc.status == "failed"
    assert doc.error is not None and "monthly AI budget" in doc.error
    assert embeds == 0


async def test_ingestion_budget_block_fails_once_without_retries(
    client: httpx.AsyncClient, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dramatiq

    a = await register(client)
    kb = (await client.post("/v1/kbs", json={"name": "kb"}, headers=a.headers)).json()["id"]
    up = await client.post(f"/v1/kbs/{kb}/documents", files={"file": ("a.txt", b"hello world")},
                           headers=a.headers)  # fmt: skip
    doc_id = up.json()["id"]
    await set_budget(client, a, "0")
    attempts = 0

    async def counted(*args: Any) -> None:
        nonlocal attempts
        attempts += 1
        await process_document(*args)

    monkeypatch.setattr(ingest_tasks, "process_document", counted)

    def run() -> None:
        env.flush_all()  # drop the upload's job; send one with near-zero backoff
        ingest_tasks.ingest_document.send_with_options(
            args=(doc_id, str(a.org_id)), min_backoff=1, max_backoff=1
        )
        worker = dramatiq.Worker(env, worker_timeout=50)
        worker.start()
        try:
            env.join("ingest", fail_fast=False, timeout=30_000)
        finally:
            worker.stop()

    await asyncio.to_thread(run)
    doc = (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).json()
    assert attempts == 1  # was 1 + MAX_RETRIES before throws=(BudgetExceeded,)
    assert doc["status"] == "failed" and "monthly AI budget" in doc["error"]


async def test_search_query_embedding_is_blocked_too(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb = (await client.post("/v1/kbs", json={"name": "kb"}, headers=a.headers)).json()["id"]
    up = await client.post(f"/v1/kbs/{kb}/documents", files={"file": ("a.txt", b"hello world")},
                           headers=a.headers)  # fmt: skip
    await process_document(uuid.UUID(up.json()["id"]), a.org_id)  # under budget: embeds
    await set_budget(client, a, "0")
    r = await client.post("/v1/search", json={"query": "hello"}, headers=a.headers)
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget_exceeded"
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        n = await s.scalar(text("SELECT count(*) FROM usage_events WHERE kind = 'embed'"))
    assert n == 1  # only the ingestion call; the blocked query never reached the embedder


class PricedEmbedder:
    """Bills 10M tokens ($0.20 on text-embedding-3-small) per call; can fail on call `fail_on`."""

    provider, model, dim = "openai", "text-embedding-3-small", 8

    def __init__(self, fail_on: int = 0) -> None:
        self.calls: list[int] = []
        self.fail_on = fail_on

    def embed(self, texts: list[str]) -> tuple[list[list[float]], int | None]:
        self.calls.append(len(texts))
        if len(self.calls) == self.fail_on:
            from app.retrieval.embedders import EmbeddingError

            raise EmbeddingError("embedding provider returned HTTP 400")
        return [[1.0] * self.dim for _ in texts], 10_000_000


async def _many_chunk_doc(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, emb: PricedEmbedder
) -> tuple[Account, str]:
    from app.retrieval import embedders, indexing

    monkeypatch.setenv("EMBEDDING_BATCH", "2")
    monkeypatch.setenv("CHUNK_TOKENS", "50")
    embedders.get_embedding_settings.cache_clear()
    get_ingestion_settings.cache_clear()
    monkeypatch.setattr(indexing, "configured_embedder", lambda: emb)
    monkeypatch.setattr(indexing, "embedder_of", lambda _index: emb)
    a = await register(client)
    kb = (await client.post("/v1/kbs", json={"name": "kb"}, headers=a.headers)).json()["id"]
    body = ("word " * 49 + "\n").encode() * 10  # 10 chunks = 5 batches of 2
    up = await client.post(f"/v1/kbs/{kb}/documents", files={"file": ("a.txt", body)},
                           headers=a.headers)  # fmt: skip
    assert up.status_code == 201, up.text
    return a, up.json()["id"]


async def _embed_rows(a: Account) -> list[tuple[int, Decimal]]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        rows = await s.execute(
            select(UsageEvent.in_tok, UsageEvent.cost_usd).where(UsageEvent.kind == "embed")
        )
        return [(r[0], r[1]) for r in rows]


async def test_budget_crossed_mid_document_stops_further_batches(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.retrieval import embedders

    emb = PricedEmbedder()
    a, doc_id = await _many_chunk_doc(client, monkeypatch, emb)
    await set_budget(client, a, "0.10")
    with pytest.raises(BudgetExceeded):
        await process_document(uuid.UUID(doc_id), a.org_id)
    embedders.get_embedding_settings.cache_clear()
    assert emb.calls == [2]  # the first batch crossed the budget; the other 4 never ran
    doc = (await client.get(f"/v1/documents/{doc_id}", headers=a.headers)).json()
    assert doc["status"] == "failed" and "monthly AI budget" in doc["error"]
    assert await _embed_rows(a) == [(10_000_000, Decimal("0.2"))]  # billed batch kept


async def test_failed_batch_keeps_usage_of_batches_already_billed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.retrieval import embedders

    emb = PricedEmbedder(fail_on=3)
    a, doc_id = await _many_chunk_doc(client, monkeypatch, emb)
    with pytest.raises(embedders.EmbeddingError):
        await process_document(uuid.UUID(doc_id), a.org_id)
    embedders.get_embedding_settings.cache_clear()
    assert emb.calls == [2, 2, 2]
    assert len(await _embed_rows(a)) == 2  # the rolled-back job still recorded what it paid for

"""GET /v1/usage + CSV export: org totals for owners/admins, own rows only for other roles,
tenant-scoped, and real embed rows from an upload (AC12)."""

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from tests.conftest import Account, register  # isort: skip
from app.core.db import SessionLocal, use_tenant
from app.ingestion import storage
from app.ingestion.config import get_ingestion_settings
from app.ingestion.pipeline import process_document
from app.llm.usage import UsageEvent

NOW = datetime.now(UTC)


async def _event(
    a: Account, user: uuid.UUID | None, kind: str, model: str, cost: str | None, at: datetime = NOW
) -> None:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        ev = UsageEvent(
            id=uuid.uuid4(), org_id=a.org_id, user_id=user, kind=kind, provider="p", model=model,
            in_tok=100, out_tok=10, cost_usd=None if cost is None else Decimal(cost),
        )  # fmt: skip
        s.add(ev)
        await s.flush()
        await s.execute(update(UsageEvent).where(UsageEvent.id == ev.id).values(created_at=at))
        await s.commit()


async def join_as(client: httpx.AsyncClient, owner: Account, role: str) -> Account:
    m = await register(client)
    r = await client.post(
        f"/v1/orgs/{owner.org_id}/members", json={"email": m.email, "role": role},
        headers=owner.headers,
    )  # fmt: skip
    assert r.status_code == 201, r.text
    ok = await client.post(f"/v1/invites/{r.json()['id']}/accept", headers=m.headers)
    assert ok.status_code == 200, ok.text
    sw = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": m.tokens["refresh_token"], "org_id": str(owner.org_id)},
    )
    assert sw.status_code == 200, sw.text
    return Account(m.email, m.password, m.user_id, owner.org_id, sw.json())


async def _usage(client: httpx.AsyncClient, a: Account, **params: str) -> dict[str, Any]:
    r = await client.get("/v1/usage", params=params, headers=a.headers)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_totals_and_every_grouping(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    editor = await join_as(client, owner, "editor")
    await _event(owner, owner.user_id, "chat", "big", "0.50")
    await _event(owner, editor.user_id, "chat", "big", "0.25")
    await _event(owner, None, "embed", "e", None)  # worker job, unknown price
    await _event(owner, owner.user_id, "chat", "big", "9", NOW - timedelta(days=40))  # out of range

    since = (NOW - timedelta(days=1)).isoformat()
    body = await _usage(client, owner, **{"from": since, "group_by": "kind"})
    assert body["scope"] == "org"
    assert body["totals"] == {"in_tok": 300, "out_tok": 30, "cost_usd": "0.750000", "calls": 3,
                              "unpriced_calls": 1}  # fmt: skip
    assert {s["key"]: s["calls"] for s in body["series"]} == {"chat": 2, "embed": 1}

    by_user = await _usage(client, owner, **{"from": since, "group_by": "user"})
    labels = {s["key"]: s["label"] for s in by_user["series"]}
    assert labels == {str(owner.user_id): owner.email, str(editor.user_id): editor.email,
                      None: None}  # fmt: skip
    by_day = await _usage(client, owner, **{"from": since, "group_by": "day"})
    assert [s["key"] for s in by_day["series"]] == [NOW.strftime("%Y-%m-%d")]
    by_model = await _usage(client, owner, **{"from": since, "group_by": "model"})
    assert {s["key"] for s in by_model["series"]} == {"big", "e"}

    mine = await _usage(client, editor, **{"from": since})
    assert mine["scope"] == "self" and mine["totals"]["calls"] == 1
    assert mine["totals"]["cost_usd"] == "0.250000"


async def test_export_csv_and_tenant_isolation(client: httpx.AsyncClient) -> None:
    a, b = await register(client), await register(client)
    await _event(a, a.user_id, "chat", "m", "0.10")
    r = await client.get("/v1/usage/export", headers=a.headers)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1 and rows[0]["kind"] == "chat" and rows[0]["cost_usd"] == "0.100000"
    other = await _usage(client, b)
    assert other["totals"]["calls"] == 0
    r = await client.get("/v1/usage/export", headers=b.headers)
    assert r.text.strip().count("\n") == 0  # header only


@pytest.mark.parametrize(
    "params",
    [{"from": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
     {"from": "2024-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
     {"from": "2026-01-01T00:00:00"},
     {"group_by": "week"}],
)  # fmt: skip
async def test_bad_ranges_are_422(client: httpx.AsyncClient, params: dict[str, str]) -> None:
    a = await register(client)
    r = await client.get("/v1/usage", params=params, headers=a.headers)
    assert r.status_code == 422


async def test_an_upload_shows_up_as_embed_usage(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setenv("BLOB_BACKEND", "local")
    monkeypatch.setenv("BLOB_DIR", str(tmp_path))
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()
    from dramatiq.brokers.stub import StubBroker

    from app.ingestion import tasks

    stub = StubBroker()  # type: ignore[no-untyped-call]
    monkeypatch.setattr(tasks.ingest_document, "broker", stub)
    stub.declare_actor(tasks.ingest_document)
    try:
        a = await register(client)
        kb = (await client.post("/v1/kbs", json={"name": "kb"}, headers=a.headers)).json()["id"]
        up = await client.post(f"/v1/kbs/{kb}/documents", files={"file": ("a.txt", b"hi there")},
                               headers=a.headers)  # fmt: skip
        await process_document(uuid.UUID(up.json()["id"]), a.org_id)
        body = await _usage(client, a, group_by="kind")
    finally:
        get_ingestion_settings.cache_clear()
        storage.get_blob_store.cache_clear()
    assert [s["key"] for s in body["series"]] == ["embed"]
    assert body["series"][0]["calls"] == 1 and body["series"][0]["in_tok"] > 0


async def test_export_neutralizes_formula_cells(client: httpx.AsyncClient) -> None:
    a = await register(client)
    evil = '=HYPERLINK("http://evil.example","x")'
    await _event(a, a.user_id, "chat", evil, "0.10")
    rows = list(csv.DictReader(io.StringIO((await client.get(
        "/v1/usage/export", headers=a.headers)).text)))  # fmt: skip
    assert rows[0]["model"] == "'" + evil


async def test_agent_usage_is_its_own_kind_in_report_and_csv(client: httpx.AsyncClient) -> None:
    a = await register(client)
    await _event(a, a.user_id, "agent", "m", "0.20")
    await _event(a, a.user_id, "chat", "m", "0.10")
    body = await _usage(client, a, group_by="kind")
    assert {s["key"]: s["cost_usd"] for s in body["series"]} == {
        "agent": "0.200000", "chat": "0.100000"}  # fmt: skip
    r = await client.get("/v1/usage/export", headers=a.headers)
    assert sorted(row["kind"] for row in csv.DictReader(io.StringIO(r.text))) == ["agent", "chat"]

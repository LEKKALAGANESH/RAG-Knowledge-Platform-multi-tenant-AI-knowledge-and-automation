"""Audit log: one row per security-relevant action, append-only for the app role, org-scoped,
owner/admin only, CSV export, and no secrets or PII in rows (AC11)."""

import csv
import io
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from dramatiq.brokers.stub import StubBroker
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.agent import tasks as agent_tasks
from app.connectors import router as sources_router
from app.connectors.sync import sync_source
from app.core.db import SessionLocal, use_tenant
from app.core.security import keyed_hash, split_api_key
from app.ingestion import storage, tasks
from app.ingestion.config import get_ingestion_settings
from tests.conftest import Account, register
from tests.test_auth_keys import bearer, make_key


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> Iterator[None]:
    monkeypatch.setenv("BLOB_BACKEND", "local")
    monkeypatch.setenv("BLOB_DIR", str(tmp_path))
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()
    stub = StubBroker()  # type: ignore[no-untyped-call]
    for actor in (tasks.ingest_document, agent_tasks.run_agent, sync_source):
        monkeypatch.setattr(actor, "broker", stub)
        stub.declare_actor(actor)

    async def no_check(*_: object) -> None:
        return None

    monkeypatch.setattr(sources_router, "_check_before_save", no_check)
    yield
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()


async def _rows(a: Account) -> list[dict[str, Any]]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        res = await s.execute(text("SELECT * FROM audit_log ORDER BY created_at, id"))
        return [dict(r._mapping) for r in res]


async def _seed_approval(a: Account) -> uuid.UUID:
    run, step, appr = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p = {"org": a.org_id, "user": a.user_id, "run": run, "step": step, "id": appr, "key": str(appr)}
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        for sql in (
            "INSERT INTO agent_runs (id, org_id, user_id, goal, max_steps, status) "
            "VALUES (:run, :org, :user, 'g', 1, 'awaiting_approval')",
            "INSERT INTO agent_steps (id, org_id, run_id, n, kind, tool, input) "
            "VALUES (:step, :org, :run, 1, 'tool_call', 'delete_report', '{}')",
            "INSERT INTO approvals (id, org_id, run_id, user_id, step_id, tool, input, "
            "idempotency_key) VALUES (:id, :org, :run, :user, :step, 'delete_report', '{}', "
            ":key)",
        ):
            await s.execute(text(sql), p)
        await s.commit()
    return appr


async def _do_everything(client: httpx.AsyncClient, a: Account) -> dict[str, str]:
    """Every audited action once, through the API. Returns ids of the targets."""
    h = a.headers
    assert (
        await client.post("/v1/auth/login", json={"email": a.email, "password": a.password})
    ).status_code == 200
    other = await register(client)
    r = await client.post(
        f"/v1/orgs/{a.org_id}/members", json={"email": other.email, "role": "editor"}, headers=h
    )
    assert r.status_code == 201
    kb = (await client.post("/v1/kbs", json={"name": "k"}, headers=h)).json()["id"]
    files = {"file": ("a.txt", b"hello audit", "text/plain")}
    doc = (await client.post(f"/v1/kbs/{kb}/documents", files=files, headers=h)).json()["id"]
    assert (await client.delete(f"/v1/documents/{doc}", headers=h)).status_code == 204
    body = {"type": "website", "name": "s", "config": {"start_urls": ["https://example.com/"]}}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=h)
    assert r.status_code == 201, r.text
    src = r.json()["id"]
    assert (await client.patch(f"/v1/sources/{src}", json={"name": "s2"}, headers=h)).is_success
    assert (await client.post(f"/v1/sources/{src}/sync", headers=h)).status_code == 202
    async with SessionLocal() as s:  # let the delete through: no worker runs the sync here
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(text("UPDATE sources SET status = 'idle' WHERE id = :id"), {"id": src})
        await s.commit()
    assert (await client.delete(f"/v1/sources/{src}", headers=h)).status_code == 204
    r = await client.post("/v1/agent/runs", json={"goal": "g", "kb_ids": [kb]}, headers=h)
    assert r.status_code == 202, r.text
    appr = await _seed_approval(a)
    r = await client.post(f"/v1/approvals/{appr}", json={"decision": "reject"}, headers=h)
    assert r.status_code == 200, r.text
    key = await make_key(client, a, ["kb:read"])
    assert (await client.delete(f"/v1/api-keys/{key['id']}", headers=h)).status_code == 204
    assert (await client.delete(f"/v1/kbs/{kb}", headers=h)).status_code == 204
    return {"kb": kb, "doc": doc, "src": src, "key": key["id"], "secret": key["key"]}


async def test_every_audited_action_writes_one_row(client: httpx.AsyncClient) -> None:
    a = await register(client)
    ids = await _do_everything(client, a)
    rows = await _rows(a)
    actions = [r["action"] for r in rows]
    assert actions == [
        "auth.register",
        "auth.login",
        "invite.create",
        "kb.create",
        "document.upload",
        "document.delete",
        "source.create",
        "source.update",
        "source.sync",
        "source.delete",
        "agent_run.start",
        "approval.rejected",
        "api_key.create",
        "api_key.revoke",
        "kb.delete",
    ]
    by = {r["action"]: r for r in rows}
    assert str(by["kb.create"]["target_id"]) == ids["kb"] == str(by["kb.delete"]["target_id"])
    assert by["kb.create"]["target_type"] == "knowledge_bases"
    assert str(by["document.upload"]["target_id"]) == ids["doc"]
    assert str(by["source.delete"]["target_id"]) == ids["src"]
    assert by["source.update"]["meta"] == {"fields": "name"}
    assert str(by["api_key.revoke"]["target_id"]) == ids["key"]
    assert by["api_key.create"]["meta"] == {"scopes": ["kb:read"]}
    for r in rows:
        assert r["org_id"] == a.org_id and r["actor_user_id"] == a.user_id
        assert r["request_id"] and len(r["request_id"]) == 32
        assert r["ip_hash"] == keyed_hash("ip:127.0.0.1")

    # AC11: no secret, password or raw IP anywhere in the tenant's audit rows. The actor's email
    # is snapshotted on purpose (0012, docs/SECURITY.md), in actor_email and nowhere else.
    assert {r["actor_email"] for r in rows} == {a.email}
    dump = json.dumps(
        [{k: v for k, v in r.items() if k != "actor_email"} for r in rows], default=str
    )
    _, secret = split_api_key(ids["secret"]) or ("", "")
    assert secret
    for leak in (ids["secret"], secret, a.password, a.email, "127.0.0.1"):
        assert leak not in dump


async def test_actor_is_the_api_key_when_one_is_used(client: httpx.AsyncClient) -> None:
    a = await register(client)
    key = await make_key(client, a, ["kb:write"])
    r = await client.post("/v1/kbs", json={"name": "via key"}, headers=bearer(key["key"]))
    assert r.status_code == 201
    row = (await _rows(a))[-1]
    assert row["action"] == "kb.create" and str(row["actor_api_key_id"]) == key["id"]
    assert row["actor_user_id"] == a.user_id


async def test_audit_log_is_append_only_for_the_app_role(client: httpx.AsyncClient) -> None:
    a = await register(client)
    for sql in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        async with SessionLocal() as s:
            await use_tenant(s, a.org_id, a.user_id)
            with pytest.raises(DBAPIError, match="permission denied"):
                await s.execute(text(sql))
    assert [r["action"] for r in await _rows(a)] == ["auth.register"]


async def test_failed_login_is_logged_not_stored(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    a = await register(client)
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        r = await client.post("/v1/auth/login", json={"email": a.email, "password": "nope-nope"})
        assert r.status_code == 401
        await client.post("/v1/auth/login", json={"email": "ghost@example.com", "password": "x"})
    fails = [rec for rec in caplog.records if rec.getMessage() == "login failed"]
    assert [getattr(f, "user_id", "?") for f in fails] == [str(a.user_id), None]
    assert a.email not in caplog.text and "nope-nope" not in caplog.text
    assert [r["action"] for r in await _rows(a)] == ["auth.register"]


async def test_list_pages_filters_and_is_admin_only(client: httpx.AsyncClient) -> None:
    a, b = await register(client), await register(client)
    for i in range(3):
        await client.post("/v1/kbs", json={"name": f"k{i}"}, headers=a.headers)
    page = (await client.get("/v1/audit?limit=2", headers=a.headers)).json()
    assert [i["action"] for i in page["items"]] == ["kb.create", "kb.create"]
    rest = (
        await client.get(f"/v1/audit?limit=2&cursor={page['next_cursor']}", headers=a.headers)
    ).json()
    assert [i["action"] for i in rest["items"]] == ["kb.create", "auth.register"]
    assert rest["next_cursor"] is None
    only = (await client.get("/v1/audit?action=auth.register", headers=a.headers)).json()
    assert len(only["items"]) == 1
    assert (await client.get("/v1/audit?cursor=bad", headers=a.headers)).status_code == 422

    # org B sees only its own row; an editor in org A is refused.
    assert [i["action"] for i in (await client.get("/v1/audit", headers=b.headers)).json()[
        "items"]] == ["auth.register"]  # fmt: skip
    inv = await client.post(
        f"/v1/orgs/{a.org_id}/members", json={"email": b.email, "role": "editor"}, headers=a.headers
    )
    await client.post(f"/v1/invites/{inv.json()['id']}/accept", headers=b.headers)
    tok = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": b.tokens["refresh_token"], "org_id": str(a.org_id)},
    )
    eh = bearer(tok.json()["access_token"])
    assert (await client.get("/v1/audit", headers=eh)).status_code == 403
    assert (await client.get("/v1/audit/export", headers=eh)).status_code == 403


async def test_csv_export_streams_every_row(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.audit import router as audit_router

    monkeypatch.setattr(audit_router, "EXPORT_BATCH", 2)  # force several batches
    a = await register(client)
    for i in range(4):
        await client.post("/v1/kbs", json={"name": f"k{i}"}, headers=a.headers)
    r = await client.get("/v1/audit/export", headers=a.headers)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [x["action"] for x in rows] == ["kb.create"] * 4 + ["auth.register"]
    assert len({x["id"] for x in rows}) == 5 and json.loads(rows[0]["meta"]) == {}
    only = await client.get("/v1/audit/export?action=auth.register", headers=a.headers)
    assert len(list(csv.DictReader(io.StringIO(only.text)))) == 1


async def test_csv_export_neutralizes_formula_cells(client: httpx.AsyncClient) -> None:
    from app.audit.service import record

    evil = ['=HYPERLINK("http://evil.example")', "+1", "-1", "@SUM(A1)", "\tx", "\rx"]
    a = await register(client)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        for action in evil:
            record(s, a.org_id, action, user_id=a.user_id, ip="127.0.0.1")
        await s.commit()
    r = await client.get("/v1/audit/export", headers=a.headers)
    actions = {x["action"] for x in csv.DictReader(io.StringIO(r.text, newline=""))}
    assert actions == {"'" + e for e in evil} | {"auth.register"}  # ordinary cells untouched


async def test_actor_email_is_a_snapshot_that_outlives_the_membership(
    client: httpx.AsyncClient,
) -> None:
    a, b = await register(client), await register(client)
    inv = await client.post(
        f"/v1/orgs/{a.org_id}/members", json={"email": b.email, "role": "editor"}, headers=a.headers
    )
    assert (
        await client.post(f"/v1/invites/{inv.json()['id']}/accept", headers=b.headers)
    ).is_success
    r = await client.delete(f"/v1/orgs/{a.org_id}/members/{b.user_id}", headers=a.headers)
    assert r.status_code == 204
    items = (await client.get("/v1/audit", headers=a.headers)).json()["items"]
    by = {i["action"]: i for i in items}
    # b left the org, so users RLS hides b's row from a: only the snapshot says who it was.
    assert by["invite.accept"]["actor_email"] == b.email
    assert by["invite.create"]["actor_email"] == a.email
    rows = list(
        csv.DictReader(io.StringIO((await client.get("/v1/audit/export", headers=a.headers)).text))
    )
    assert {r["action"]: r["actor_email"] for r in rows}["invite.accept"] == b.email

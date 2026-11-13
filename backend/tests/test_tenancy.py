"""AC1: RLS blocks cross-org reads on EVERY tenant table, discovered from the live schema."""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, use_tenant
from tests.conftest import Account, register

# orgs is keyed by id; every other tenant table carries org_id.
TENANT_KEY = {"orgs": "id"}


async def _tenant_tables() -> list[str]:
    async with SessionLocal() as s:
        rows = await s.scalars(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'org_id'"
            )
        )
        return sorted({*rows, "orgs"})


async def _seed_every_table(acct: Account, kb_id: str) -> None:
    async with SessionLocal() as s:
        await use_tenant(s, acct.org_id, acct.user_id)
        doc_id, params = (
            uuid.uuid4(),
            {"org": acct.org_id, "kb": uuid.UUID(kb_id), "user": acct.user_id},
        )
        await s.execute(
            text(
                "INSERT INTO documents (id, org_id, kb_id, name, mime, sha256, blob_key) "
                "VALUES (:doc, :org, :kb, 'n', 'text/plain', 'h', 'b')"
            ),
            {**params, "doc": doc_id},
        )
        await s.execute(
            text(
                "INSERT INTO chunks (id, org_id, kb_id, document_id, ord, text) "
                "VALUES (:id, :org, :kb, :doc, 0, 'bonjour le monde')"
            ),
            {**params, "doc": doc_id, "id": (chunk_id := uuid.uuid4())},
        )
        await s.execute(
            text(
                "INSERT INTO embedding_indexes (id, org_id, kb_id, provider, model, dim, version) "
                "VALUES (:id, :org, :kb, 'p', 'm', 3, 1)"
            ),
            {**params, "id": (index_id := uuid.uuid4())},
        )
        await s.execute(
            text(
                "INSERT INTO chunk_embeddings (chunk_id, index_id, org_id, embedding) "
                "VALUES (:chunk, :index, :org, '[1,0,0]')"
            ),
            {**params, "chunk": chunk_id, "index": index_id},
        )
        await s.execute(
            text(
                "INSERT INTO api_keys (id, org_id, user_id, name, prefix, hash) "
                "VALUES (:id, :org, :user, 'k', :prefix, 'h')"
            ),
            {**params, "id": uuid.uuid4(), "prefix": uuid.uuid4().hex[:12]},
        )
        await s.execute(
            text(
                "INSERT INTO conversations (id, org_id, user_id, title) "
                "VALUES (:id, :org, :user, 't')"
            ),
            {**params, "id": (conv_id := uuid.uuid4())},
        )
        await s.execute(
            text(
                "INSERT INTO messages (id, org_id, conversation_id, role, content) "
                "VALUES (:id, :org, :conv, 'user', 'q')"
            ),
            {**params, "id": uuid.uuid4(), "conv": conv_id},
        )
        await s.execute(
            text(
                "INSERT INTO usage_events (id, org_id, user_id, kind, provider, model, in_tok, "
                "out_tok) VALUES (:id, :org, :user, 'chat', 'p', 'm', 1, 1)"
            ),
            {**params, "id": uuid.uuid4()},
        )
        await s.execute(
            text(
                "INSERT INTO sources (id, org_id, kb_id, type, name) "
                "VALUES (:id, :org, :kb, 'website', 's')"
            ),
            {**params, "id": (source_id := uuid.uuid4())},
        )
        await s.execute(
            text("INSERT INTO sync_runs (id, org_id, source_id) VALUES (:id, :org, :src)"),
            {**params, "id": uuid.uuid4(), "src": source_id},
        )
        run, step = uuid.uuid4(), uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO agent_runs (id, org_id, user_id, goal, max_steps) "
                "VALUES (:run, :org, :user, 'g', 1)"
            ),
            {**params, "run": run},
        )
        await s.execute(
            text(
                "INSERT INTO agent_steps (id, org_id, run_id, n, kind, tool, input) "
                "VALUES (:step, :org, :run, 1, 'tool_call', 'delete_report', '{}')"
            ),
            {**params, "run": run, "step": step},
        )
        await s.execute(
            text(
                "INSERT INTO approvals (id, org_id, run_id, user_id, step_id, tool, input, "
                "idempotency_key) VALUES (:id, :org, :run, :user, :step, 'delete_report', '{}', "
                ":key)"
            ),
            {**params, "id": uuid.uuid4(), "run": run, "step": step, "key": str(uuid.uuid4())},
        )
        await s.execute(
            text(
                "INSERT INTO reports (id, org_id, run_id, name, mime, blob_key, size) "
                "VALUES (:id, :org, :run, 'r.xlsx', 'x', 'b', 0)"
            ),
            {**params, "id": uuid.uuid4(), "run": run},
        )
        await s.execute(
            text(
                "INSERT INTO org_invites (id, org_id, email, role, expires_at) "
                "VALUES (:id, :org, 'i@example.com', 'viewer', now() + interval '1 day')"
            ),
            {**params, "id": uuid.uuid4()},
        )
        await s.commit()


async def _count(
    table: str, org_id: uuid.UUID, as_org: uuid.UUID | None, as_user: uuid.UUID | None
) -> int:
    async with SessionLocal() as s:
        await use_tenant(s, as_org, as_user)
        key = TENANT_KEY.get(table, "org_id")
        n = await s.scalar(text(f"SELECT count(*) FROM {table} WHERE {key} = :o"), {"o": org_id})  # noqa: S608
        return int(n or 0)


async def test_rls_blocks_cross_org_reads_on_every_tenant_table(client: httpx.AsyncClient) -> None:
    a, b = await register(client), await register(client)
    kb = (await client.post("/v1/kbs", json={"name": "A only"}, headers=a.headers)).json()
    await _seed_every_table(a, kb["id"])

    tables = await _tenant_tables()
    assert {"orgs", "memberships", "api_keys", "knowledge_bases", "documents", "chunks"} <= set(
        tables
    )
    for table in tables:
        assert await _count(table, a.org_id, a.org_id, a.user_id) > 0, f"{table}: seed missing"
        assert await _count(table, a.org_id, b.org_id, b.user_id) == 0, f"{table}: org B sees org A"
        assert await _count(table, a.org_id, None, None) == 0, (
            f"{table}: no-tenant session sees rows"
        )


async def test_rls_is_enabled_forced_and_app_role_cannot_bypass() -> None:
    tables = [*await _tenant_tables(), "users"]  # users: per user + current org (S9)
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(:t) AND relkind = 'r'"
                ),
                {"t": tables},
            )
        ).all()
        role = (
            await s.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert {r[0] for r in rows} == set(tables)
    assert all(r[1] and r[2] for r in rows), rows
    assert tuple(role) == (False, False)


async def test_rls_blocks_cross_org_writes(client: httpx.AsyncClient) -> None:
    a, b = await register(client), await register(client)
    async with SessionLocal() as s:
        await use_tenant(s, b.org_id, b.user_id)
        with pytest.raises(DBAPIError, match="row-level security"):
            await s.execute(
                text("INSERT INTO knowledge_bases (id, org_id, name) VALUES (:id, :org, 'x')"),
                {"id": uuid.uuid4(), "org": a.org_id},
            )


async def test_fts_column_is_generated_with_simple_config(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb = (await client.post("/v1/kbs", json={"name": "fts"}, headers=a.headers)).json()
    await _seed_every_table(a, kb["id"])
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        hit = await s.scalar(
            text("SELECT count(*) FROM chunks WHERE tsv @@ to_tsquery('simple', 'monde')")
        )
    assert hit == 1

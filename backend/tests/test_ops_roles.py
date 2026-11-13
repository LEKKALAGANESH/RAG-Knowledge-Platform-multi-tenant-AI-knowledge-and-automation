"""FX-6: no superuser owns anything (S14, ADR 0009) and messages follow their conversation (N6)."""

import os
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import register

GUCS = "SELECT set_config('app.org_id', :org, true), set_config('app.user_id', :user, true)"


async def _rows(url: str, sql: str, **params: object) -> list[tuple[object, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [tuple(r) for r in (await conn.execute(text(sql), params)).all()]
    finally:
        await engine.dispose()


async def test_owners_are_not_superusers_and_definers_bypass_only_rls() -> None:
    url = os.environ["MIGRATION_DATABASE_URL"]
    roles = dict(
        await _rows(
            url,
            """
        SELECT rolname, (rolsuper::int, rolbypassrls::int, rolcanlogin::int)::text
        FROM pg_roles WHERE rolname IN ('rag_owner', 'rag_definer', 'rag_app')""",
        )
    )
    assert roles == {"rag_owner": "(0,0,1)", "rag_definer": "(0,1,0)", "rag_app": "(0,0,1)"}
    rel_owners = await _rows(
        url,
        """
        SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class
        WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'S', 'v', 'm', 'i')
          AND NOT EXISTS (
            SELECT 1 FROM pg_depend d WHERE d.objid = pg_class.oid AND d.deptype = 'e')""",
    )
    assert rel_owners == [("rag_owner",)]
    definers = dict(
        await _rows(
            url,
            """
        SELECT proname, pg_get_userbyid(proowner) FROM pg_proc
        WHERE pronamespace = 'public'::regnamespace AND prosecdef""",
        )
    )
    assert definers == {
        "embedding_hnsw": "rag_owner",  # CREATE INDEX needs the table owner; it runs under RLS
        **{
            fn: "rag_definer"
            for fn in (
                "api_key_lookup",
                "due_sync_orgs",
                "revoke_member_sessions",
                "auth_user_by_email",
                "my_invites",
                "respond_invite",
                "prune_sessions",
            )
        },
    }


async def test_messages_are_private_to_the_conversation_author(client: httpx.AsyncClient) -> None:
    a = await register(client)
    conv, admin = uuid.uuid4(), os.environ["TEST_ADMIN_DATABASE_URL"]
    engine = create_async_engine(admin)  # superuser: seed one conversation of user A
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO conversations (id, org_id, user_id, title) "
                    "VALUES (:c, :o, :u, 't')"
                ),
                {"c": conv, "o": a.org_id, "u": a.user_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO messages (id, org_id, conversation_id, role, content) "
                    "VALUES (:m, :o, :c, 'user', 'secret question')"
                ),
                {"m": uuid.uuid4(), "o": a.org_id, "c": conv},
            )
    finally:
        await engine.dispose()

    async def visible(user: uuid.UUID) -> int:
        engine = create_async_engine(os.environ["DATABASE_URL"])  # rag_app, RLS applies
        try:
            async with engine.begin() as conn:
                await conn.execute(text(GUCS), {"org": str(a.org_id), "user": str(user)})
                q = "SELECT count(*) FROM messages WHERE conversation_id = :c"
                return int(await conn.scalar(text(q), {"c": conv}) or 0)
        finally:
            await engine.dispose()

    assert await visible(a.user_id) == 1
    assert await visible(uuid.uuid4()) == 0  # same org, another user: was 1 before 0011

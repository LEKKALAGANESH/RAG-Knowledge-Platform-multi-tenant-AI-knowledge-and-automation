import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

# Tests always target a rag_test* DB (never the dev DB), with throwaway secrets.
# RAG_TEST_DB=rag_test_a gives each concurrent run its own DB so runs can't collide.
_DB = os.environ.get("RAG_TEST_DB", "rag_test")
_PG = "postgresql+asyncpg://{}@localhost:{}/{}".format
_PORT = os.environ.get("POSTGRES_PORT", "5432")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    _PG("rag_app:" + os.environ.get("APP_DB_PASSWORD", "rag_dev_app_pw"), _PORT, _DB),
)
# Migrations run as the non-superuser table owner (ADR 0009), like production.
os.environ["MIGRATION_DATABASE_URL"] = os.environ.get(
    "TEST_MIGRATION_DATABASE_URL",
    _PG("rag_owner:" + os.environ.get("OWNER_DB_PASSWORD", "rag_dev_table_owner_pw"), _PORT, _DB),
)
# Superuser, bootstrap only: creates the test DB + extension (as docker/initdb does) and the
# throwaway source DBs/roles of test_connectors_pg.
os.environ["TEST_ADMIN_DATABASE_URL"] = os.environ.get(
    "TEST_ADMIN_DATABASE_URL",
    _PG("rag:" + os.environ.get("POSTGRES_PASSWORD", "rag_dev_owner_pw"), _PORT, _DB),
)
for _url in (
    os.environ["DATABASE_URL"],
    os.environ["MIGRATION_DATABASE_URL"],
    os.environ["TEST_ADMIN_DATABASE_URL"],
):
    if not re.fullmatch(r"rag_test[a-z0-9_]*", make_url(_url).database or ""):
        raise RuntimeError(f"test DB must match rag_test[a-z0-9_]*: {make_url(_url).database!r}")


async def _ensure_db() -> bool:
    """Creates the test DB if missing; True if an older superuser-owned DB needs 0011 as admin."""
    owner = make_url(os.environ["MIGRATION_DATABASE_URL"])
    admin = make_url(os.environ["TEST_ADMIN_DATABASE_URL"]).set(database=owner.database)
    engine = create_async_engine(admin.set(database="postgres"), isolation_level="AUTOCOMMIT")
    db_owner = text(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :d"
    ).bindparams(d=owner.database)
    try:
        async with engine.connect() as conn:
            legacy = await conn.scalar(db_owner) not in (None, owner.username)
            if legacy:  # built before 0011 by the superuser owner
                await conn.execute(
                    text(f'ALTER DATABASE "{owner.database}" OWNER TO "{owner.username}"')
                )
            if not await conn.scalar(db_owner):
                try:
                    await conn.execute(
                        text(f'CREATE DATABASE "{owner.database}" OWNER "{owner.username}"')
                    )
                except DBAPIError:
                    if not await conn.scalar(db_owner):  # losing a creation race is fine
                        raise
    finally:
        await engine.dispose()
    engine = create_async_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        await engine.dispose()
    return legacy


_LEGACY_DB = asyncio.run(_ensure_db())
os.environ["JWT_SECRET"] = "test-" + uuid.uuid4().hex + uuid.uuid4().hex
os.environ["FERNET_KEYS"] = Fernet.generate_key().decode()
# Every test registers from 127.0.0.1; test_ratelimit lowers this per test to exercise the limit.
os.environ["RATE_LIMIT_AUTH"] = "100000"
# Own rate-limit buckets per run, so tests never spend a dev server's auth bucket on 127.0.0.1.
os.environ.setdefault("RATE_LIMIT_NAMESPACE", "test-" + uuid.uuid4().hex)

import httpx  # noqa: E402
import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402

from alembic import command  # noqa: E402
from app.core.config import get_settings  # noqa: E402

if _LEGACY_DB:
    # 0011 moves ownership off the superuser, so that one upgrade runs as it (see DEPLOYMENT).
    _owner_url = os.environ["MIGRATION_DATABASE_URL"]
    os.environ["MIGRATION_DATABASE_URL"] = os.environ["TEST_ADMIN_DATABASE_URL"]
    command.upgrade(Config("alembic.ini"), "head")
    os.environ["MIGRATION_DATABASE_URL"] = _owner_url
    get_settings.cache_clear()
command.upgrade(Config("alembic.ini"), "head")

from app.main import app  # noqa: E402


@dataclass
class Account:
    email: str
    password: str
    user_id: uuid.UUID
    org_id: uuid.UUID
    tokens: dict[str, str]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens['access_token']}"}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def register(client: httpx.AsyncClient) -> Account:
    email, password = f"u{uuid.uuid4().hex[:12]}@example.com", "correct horse battery"
    r = await client.post("/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    tokens = r.json()
    me = (
        await client.get("/v1/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    ).json()
    return Account(email, password, uuid.UUID(me["user"]["id"]), uuid.UUID(me["org"]["id"]), tokens)

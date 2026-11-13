"""B5: PostgreSQL connector against the compose Postgres: a throwaway source DB + roles per module.

Covers read-only enforcement, identifier safety, statement timeout, keyset/incremental sync,
deletion on full runs (AC7), cross-org 404 (AC1) and secrets never leaking (AC11).
"""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy.engine import make_url

from app.connectors import postgres as pg_mod
from app.connectors import ssrf
from app.connectors.base import Config
from app.connectors.ssrf import ip_allowed_private
from app.core.config import get_settings
from app.core.crypto import encrypt
from tests.conftest import Account, register
from tests.test_connectors import (  # noqa: F401
    _docs,
    _kb,
    _search,
    _source,
    _sql,
    _sync,
    env,
    lan,
)

pytestmark = pytest.mark.usefixtures("lan")

PORT = int(os.environ.get("POSTGRES_PORT", "5432"))


@dataclass
class SrcDB:
    name: str
    owner_dsn: str
    reader: str
    writer: str
    password: str


def _owner_dsn(database: str) -> str:
    url = make_url(os.environ["TEST_ADMIN_DATABASE_URL"]).set(drivername="postgresql")
    return url.set(database=database).render_as_string(hide_password=False)


@pytest.fixture(scope="module")
async def srcdb() -> AsyncIterator[SrcDB]:
    tag = uuid.uuid4().hex[:8]
    db = SrcDB(
        f"rag_src_{tag}", "", f"rag_src_reader_{tag}", f"rag_src_writer_{tag}", uuid.uuid4().hex
    )
    db.owner_dsn = _owner_dsn(db.name)
    admin = await asyncpg.connect(_owner_dsn("postgres"))
    await admin.execute(f"CREATE DATABASE {db.name}")
    for role in (db.reader, db.writer):
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{db.password}'")
    conn = await asyncpg.connect(db.owner_dsn)
    await conn.execute(
        """
        CREATE TABLE articles (id int PRIMARY KEY, title text, body text, updated_at timestamptz);
        CREATE TABLE x (id int);
        CREATE VIEW slow AS SELECT a.id, a.body FROM articles a, LATERAL (SELECT pg_sleep(2)) s;
        """
    )
    await conn.execute(f"GRANT SELECT ON articles, slow TO {db.reader}")
    await conn.execute(f"GRANT SELECT, INSERT ON articles TO {db.writer}")
    await conn.close()
    yield db
    await admin.execute(f"DROP DATABASE {db.name} WITH (FORCE)")
    for role in (db.reader, db.writer):
        await admin.execute(f"DROP ROLE {role}")
    await admin.close()


@pytest.fixture
async def rows(srcdb: SrcDB) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(srcdb.owner_dsn)
    await conn.execute(
        """
        TRUNCATE articles;
        INSERT INTO articles VALUES
          (1, 'Koala care', 'The koala sleeps most of the day.', now() - interval '3 hours'),
          (2, 'Kangaroo facts', 'A kangaroo carries a joey.', now() - interval '2 hours'),
          (3, 'Platypus', 'The platypus lays eggs.', now() - interval '1 hour'),
          (4, NULL, NULL, now());
        """
    )
    yield conn
    await conn.close()


def pg_cfg(**over: Any) -> Config:
    table = {
        "name": "articles",
        "id_column": "id",
        "title_column": "title",
        "text_columns": ["body"],
        "updated_column": "updated_at",
    }
    return {
        "host": "127.0.0.1",
        "port": PORT,
        "database": "",
        "schema": "public",
        "tables": [table],
        "sslmode": "disable",
        "allow_private_endpoint": True,
        **over,
    }


async def _pg_source(
    client: httpx.AsyncClient, a: Account, kb: str, db: SrcDB, user: str = "", **over: Any
) -> str:
    secret = json.dumps({"user": user or db.reader, "password": db.password})
    cfg = pg_cfg(database=db.name, **over)
    return await _source(client, a, kb, cfg, type="postgres", secret=secret)


async def _pg_create(
    client: httpx.AsyncClient, a: Account, kb: str, db: SrcDB, user: str = "", **over: Any
) -> httpx.Response:
    secret = json.dumps({"user": user or db.reader, "password": db.password})
    body = {"type": "postgres", "name": "p", "config": pg_cfg(database=db.name, **over)}
    return await client.post(
        f"/v1/kbs/{kb}/sources", json={**body, "secret": secret}, headers=a.headers
    )


def _refused(r: httpx.Response, *needles: str) -> str:
    assert r.status_code == 422, r.text
    msg = str(r.json()["error"]["message"])
    assert all(n in msg for n in needles), msg
    return msg


async def _test(client: httpx.AsyncClient, a: Account, sid: str) -> dict[str, Any]:
    r = await client.post(f"/v1/sources/{sid}/test", headers=a.headers)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_pg_sync_rows_incremental_and_full_delete(
    client: httpx.AsyncClient, srcdb: SrcDB, rows: asyncpg.Connection
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    sid = await _pg_source(client, a, kb, srcdb, full_sync_every=3)
    t = await _test(client, a, sid)
    assert t["ok"] is True and "articles (id, title, body, updated_at)" in t["message"], t
    tables = {x["name"]: x["columns"] for x in t["schema"]["tables"]}
    assert tables == {"articles": ["id", "title", "body", "updated_at"], "slow": ["id", "body"]}

    run1 = await _sync(client, a, sid)  # first run is full
    assert run1["status"] == "succeeded" and run1["stats"]["added"] == 3, run1  # row 4 is empty
    docs = await _docs(a, sid)
    assert set(docs) == {"public.articles:1", "public.articles:2", "public.articles:3"}
    d2 = docs["public.articles:2"]
    assert d2.name == "Kangaroo facts" and d2.mime == "text/markdown"
    assert (d2.meta["record"], d2.meta["table"]) == (2, "public.articles")
    assert any("joey" in h for h in await _search(client, a, kb, "kangaroo"))

    await rows.execute(
        "UPDATE articles SET body = 'A wallaby is smaller.', updated_at = now() WHERE id = 2"
    )
    run2 = await _sync(client, a, sid)  # incremental: rows with updated_at >= watermark only
    assert run2["stats"]["updated"] == 1 and run2["stats"]["discovered"] < 4, run2
    assert await _search(client, a, kb, "joey") == []
    assert any("wallaby" in h for h in await _search(client, a, kb, "wallaby"))

    await rows.execute("DELETE FROM articles WHERE id = 1")
    run3 = await _sync(client, a, sid)  # incremental runs never delete
    assert (
        run3["stats"]["deleted"] == 0
        and (await _docs(a, sid))["public.articles:1"].status == "ready"
    )
    run4 = await _sync(client, a, sid)  # 4th run: 3 % 3 == 0 -> full scan
    assert run4["status"] == "succeeded" and run4["stats"]["deleted"] == 1, run4
    assert (await _docs(a, sid))["public.articles:1"].status == "deleted"
    assert await _search(client, a, kb, "koala") == []  # AC7

    b = await register(client)  # AC1
    for method, path in [
        ("GET", f"/v1/sources/{sid}"),
        ("POST", f"/v1/sources/{sid}/test"),
        ("POST", f"/v1/sources/{sid}/sync"),
    ]:
        assert (await client.request(method, path, headers=b.headers)).status_code == 404
    r = await client.post(
        "/v1/search", json={"query": "wallaby", "kb_ids": [kb]}, headers=b.headers
    )
    assert r.status_code == 404


async def test_pg_keyset_pagination_and_row_cap(
    client: httpx.AsyncClient,
    srcdb: SrcDB,
    rows: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pg_mod, "PAGE_ROWS", 2)
    a = await register(client)
    kb = await _kb(client, a)
    sid = await _pg_source(client, a, kb, srcdb, max_rows_per_table=3)
    run = await _sync(client, a, sid)
    assert run["stats"]["added"] == 3 and len(await _docs(a, sid)) == 3  # ids 1..3 over 2 pages


async def test_pg_role_that_can_write_is_refused(
    client: httpx.AsyncClient, srcdb: SrcDB, rows: asyncpg.Connection
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    r = await _pg_create(client, a, kb, srcdb, user=srcdb.writer)
    _refused(r, "can modify", "read-only role")  # refused at save time
    assert r.json()["error"]["code"] == "source_test_failed"

    owner = make_url(os.environ["TEST_ADMIN_DATABASE_URL"])  # superuser
    secret = json.dumps({"user": owner.username, "password": owner.password})
    body = {"type": "postgres", "name": "p", "config": pg_cfg(database=srcdb.name)}
    r = await client.post(
        f"/v1/kbs/{kb}/sources", json={**body, "secret": secret}, headers=a.headers
    )
    _refused(r, "superuser")

    # A role granted write access after saving is still refused at sync time.
    sid = await _pg_source(client, a, kb, srcdb)
    await rows.execute(f"GRANT INSERT ON articles TO {srcdb.reader}")
    try:
        run = await _sync(client, a, sid)
    finally:
        await rows.execute(f"REVOKE INSERT ON articles FROM {srcdb.reader}")
    assert run["status"] == "failed" and "read-only role" in run["error"]
    assert await _docs(a, sid) == {}


@pytest.mark.parametrize(
    "table",
    [
        {"name": "users; DROP TABLE x", "id_column": "id", "text_columns": ["body"]},
        {"name": "articles", "id_column": 'id"; DROP TABLE x; --', "text_columns": ["body"]},
        {"name": "articles", "id_column": "id", "text_columns": ["body) FROM x --"]},
        {"name": "articles", "id_column": "id", "text_columns": []},
        {"name": "a" * 64, "id_column": "id", "text_columns": ["body"]},
    ],
)
async def test_pg_injection_shaped_identifiers_are_422(
    client: httpx.AsyncClient, table: dict[str, Any]
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    body = {"type": "postgres", "name": "p", "config": pg_cfg(database="d", tables=[table])}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    "over",
    [
        {"schema": "public; select 1"},
        {"database": "d b"},
        {"host": "evil.com/x"},
        {"sslmode": "allow"},
    ],
)
async def test_pg_invalid_connection_fields_are_422(
    client: httpx.AsyncClient, over: Config
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    body = {"type": "postgres", "name": "p", "config": {**pg_cfg(database="d"), **over}}
    assert (
        await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    ).status_code == 422


async def test_pg_unknown_table_and_column_rejected_by_catalog(
    client: httpx.AsyncClient, srcdb: SrcDB, rows: asyncpg.Connection
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    nosuch = {"name": "nosuch", "id_column": "id", "text_columns": ["body"]}
    _refused(await _pg_create(client, a, kb, srcdb, tables=[nosuch]), "not found", "articles")
    # `x` exists but the reader has no privilege on it: invisible, same as missing.
    hidden = {"name": "x", "id_column": "id", "text_columns": ["id"]}
    _refused(await _pg_create(client, a, kb, srcdb, tables=[hidden]), "not found")
    badcol = {"name": "articles", "id_column": "id", "text_columns": ["summary"]}
    _refused(await _pg_create(client, a, kb, srcdb, tables=[badcol]), "summary")


async def test_pg_statement_timeout_is_enforced(
    client: httpx.AsyncClient,
    srcdb: SrcDB,
    rows: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pg_mod, "STATEMENT_TIMEOUT_MS", 300)
    a = await register(client)
    kb = await _kb(client, a)
    slow = {"name": "slow", "id_column": "id", "text_columns": ["body"]}
    sid = await _pg_source(client, a, kb, srcdb, tables=[slow])
    run = await _sync(client, a, sid)
    assert run["status"] == "failed" and "took longer than" in run["error"], run


async def test_pg_session_is_read_only(srcdb: SrcDB, rows: asyncpg.Connection) -> None:
    """Even a role with INSERT cannot write through the connector's session settings."""
    cfg = pg_mod.PgConfig.model_validate(pg_cfg(database=srcdb.name))
    conn = await pg_mod._connect(cfg, pg_mod.PgSecret(user=srcdb.writer, password=srcdb.password))
    try:
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            await conn.execute("INSERT INTO articles (id) VALUES (99)")
    finally:
        await conn.close()


async def test_pg_private_host_needs_opt_in(client: httpx.AsyncClient, srcdb: SrcDB) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    r = await _pg_create(client, a, kb, srcdb, allow_private_endpoint=False)
    _refused(r, "allow_private_endpoint")
    r = await _pg_create(client, a, kb, srcdb, host="169.254.169.254")  # opt-in on
    _refused(r, "private or reserved")


async def test_pg_loopback_blocked_even_with_gate_and_opt_in(
    client: httpx.AsyncClient, srcdb: SrcDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssrf, "ip_allowed_private", ip_allowed_private)  # undo `lan`'s loopback
    a = await register(client)
    kb = await _kb(client, a)
    for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        _refused(await _pg_create(client, a, kb, srcdb, host=host), "private or reserved")


async def test_pg_gate_turned_off_later_fails_test_and_sync(
    client: httpx.AsyncClient,
    srcdb: SrcDB,
    rows: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    sid = await _pg_source(client, a, kb, srcdb)  # gate on (lan fixture)
    monkeypatch.setattr(get_settings(), "connectors_allow_private_endpoints", False)
    t = await _test(client, a, sid)
    assert t["ok"] is False and "CONNECTORS_ALLOW_PRIVATE_ENDPOINTS" in t["message"]
    run = await _sync(client, a, sid)
    assert run["status"] == "failed" and "Private endpoints are disabled" in run["error"], run
    assert await _docs(a, sid) == {}
    r = await _pg_create(client, a, kb, srcdb)
    assert r.status_code == 422 and r.json()["error"]["code"] == "private_endpoints_disabled"


async def test_pg_full_sync_on_demand_deletes(
    client: httpx.AsyncClient, srcdb: SrcDB, rows: asyncpg.Connection
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    sid = await _pg_source(client, a, kb, srcdb, full_sync_every=1000)
    assert (await _sync(client, a, sid))["stats"]["added"] == 3  # first run is full
    await rows.execute("DELETE FROM articles WHERE id = 1")
    assert (await _sync(client, a, sid))["stats"]["deleted"] == 0  # incremental
    run = await _sync(client, a, sid, full=True)
    assert run["status"] == "succeeded" and run["stats"]["deleted"] == 1, run
    assert (await _docs(a, sid))["public.articles:1"].status == "deleted"
    assert (await _sync(client, a, sid))["stats"]["deleted"] == 0  # back to incremental


async def test_pg_secrets_never_leak(
    client: httpx.AsyncClient,
    srcdb: SrcDB,
    rows: asyncpg.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    a = await register(client)
    kb = await _kb(client, a)
    wrong = "wrong-" + uuid.uuid4().hex
    secret = json.dumps({"user": srcdb.reader, "password": wrong})
    body = {"type": "postgres", "name": "p", "config": pg_cfg(database=srcdb.name)}
    r = await client.post(
        f"/v1/kbs/{kb}/sources", json={**body, "secret": secret}, headers=a.headers
    )
    _refused(r, "InvalidPasswordError")
    good = await _pg_source(client, a, kb, srcdb)
    patched = await client.patch(f"/v1/sources/{good}", json={"secret": secret}, headers=a.headers)
    _refused(patched, "InvalidPasswordError")
    sid = await _pg_source(client, a, kb, srcdb)  # the password goes wrong after saving
    await _sql(a, "UPDATE sources SET secret_enc = :e WHERE id = :i", e=encrypt(secret), i=sid)
    t = await _test(client, a, sid)
    assert t["ok"] is False and "InvalidPasswordError" in t["message"]
    bodies = [
        r.text,
        patched.text,
        str(t),
        str(await _sync(client, a, sid)),
        str(await _sync(client, a, good)),
        (await client.get(f"/v1/kbs/{kb}/sources", headers=a.headers)).text,
    ]
    for s in (wrong, srcdb.password):
        assert all(s not in b for b in bodies) and s not in caplog.text


async def test_pg_target_change_needs_secret_again(
    client: httpx.AsyncClient, srcdb: SrcDB, rows: asyncpg.Connection
) -> None:
    """S1: moving a source to another server must not test() it with the stored password."""
    got = bytearray()

    async def fake_pg(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        got.extend(await reader.read(4096))  # startup message
        writer.write(b"R" + (8).to_bytes(4, "big") + (3).to_bytes(4, "big"))  # cleartext pwd
        await writer.drain()
        got.extend(await reader.read(4096))
        writer.close()

    server = await asyncio.start_server(fake_pg, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    a = await register(client)
    sid = await _pg_source(client, a, await _kb(client, a), srcdb)
    try:
        moved = pg_cfg(database=srcdb.name, port=port)
        r = await client.patch(f"/v1/sources/{sid}", json={"config": moved}, headers=a.headers)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "secret_required_for_target_change"
        assert srcdb.password.encode() not in got
        # A config edit that keeps the server still reuses the stored secret.
        same = pg_cfg(database=srcdb.name, max_rows_per_table=5)
        r = await client.patch(f"/v1/sources/{sid}", json={"config": same}, headers=a.headers)
        assert r.status_code == 200 and r.json()["config"]["max_rows_per_table"] == 5, r.text
        # With the secret re-entered, the move is tested with what the editor typed.
        mine = json.dumps({"user": "me", "password": "typed-by-editor"})
        r = await client.patch(
            f"/v1/sources/{sid}", json={"config": moved, "secret": mine}, headers=a.headers
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "source_test_failed"
        assert b"typed-by-editor" in got and srcdb.password.encode() not in got
    finally:
        server.close()

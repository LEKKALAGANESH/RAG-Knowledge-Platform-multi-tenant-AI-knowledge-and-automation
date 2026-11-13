"""Refresh sessions: rotation, the parallel-refresh grace window, theft detection, logout."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from sqlalchemy import text

from app.connectors import scheduler
from app.core.config import get_settings
from app.core.db import SessionLocal, use_tenant
from tests.conftest import Account, register


async def _refresh(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/v1/auth/refresh", json={"refresh_token": token})


async def _audit(a: Account, action: str) -> int:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        n = await s.scalar(text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": action})
    return int(n or 0)


async def test_refresh_rotates_and_parallel_refresh_gets_the_same_pair(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    old = a.tokens["refresh_token"]
    first, second = await asyncio.gather(_refresh(client, old), _refresh(client, old))
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["refresh_token"] == second.json()["refresh_token"] != old
    assert first.json()["access_token"] == second.json()["access_token"]
    new = first.json()
    me = await client.get("/v1/me", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert me.status_code == 200
    # the successor rotates normally
    nxt = await _refresh(client, new["refresh_token"])
    assert nxt.status_code == 200 and nxt.json()["refresh_token"] != new["refresh_token"]


async def test_reuse_after_the_grace_window_revokes_the_whole_family(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", 0)
    a = await register(client)
    stolen = a.tokens["refresh_token"]
    legit = (await _refresh(client, stolen)).json()["refresh_token"]
    await asyncio.sleep(0.01)

    replay = await _refresh(client, stolen)
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "session_revoked"
    # the legitimate holder is signed out too: one of the two parties is an attacker
    assert (await _refresh(client, legit)).status_code == 401
    assert await _audit(a, "session.reuse_detected") == 1
    # another login (another family) is unaffected
    login = await client.post("/v1/auth/login", json={"email": a.email, "password": a.password})
    assert (await _refresh(client, login.json()["refresh_token"])).status_code == 200


async def test_logout_ends_this_login_only(client: httpx.AsyncClient) -> None:
    a = await register(client)
    other = await client.post("/v1/auth/login", json={"email": a.email, "password": a.password})
    r = await client.post("/v1/auth/logout", json={"refresh_token": a.tokens["refresh_token"]})
    assert r.status_code == 204
    assert (await _refresh(client, a.tokens["refresh_token"])).status_code == 401
    assert (await _refresh(client, other.json()["refresh_token"])).status_code == 200
    # idempotent; the access token lives out its short TTL (documented)
    r = await client.post("/v1/auth/logout", json={"refresh_token": a.tokens["refresh_token"]})
    assert r.status_code == 204
    assert (await client.get("/v1/me", headers=a.headers)).status_code == 200


async def test_logout_all_revokes_every_session_and_is_audited(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    logins = [
        (await client.post("/v1/auth/login", json={"email": a.email, "password": a.password}))
        for _ in range(2)
    ]
    r = await client.post("/v1/auth/logout-all", headers=a.headers)
    assert r.status_code == 200 and r.json() == {"revoked": 3}
    for token in (a.tokens["refresh_token"], *(x.json()["refresh_token"] for x in logins)):
        assert (await _refresh(client, token)).status_code == 401
    assert await _audit(a, "auth.logout_all") == 1


async def test_api_key_cannot_log_the_user_out(client: httpx.AsyncClient) -> None:
    a = await register(client)
    key = await client.post(
        "/v1/api-keys", json={"name": "k", "scopes": ["admin"]}, headers=a.headers
    )
    r = await client.post(
        "/v1/auth/logout-all", headers={"Authorization": f"Bearer {key.json()['key']}"}
    )
    assert r.status_code == 403
    assert (await _refresh(client, a.tokens["refresh_token"])).status_code == 200


async def test_a_token_without_a_session_row_is_refused(client: httpx.AsyncClient) -> None:
    """Pre-0008 tokens (random jti) and forged-but-signed ids have no row: sign in again."""
    a = await register(client)
    now = datetime.now(UTC)
    claims = {
        "sub": str(a.user_id), "org": str(a.org_id), "type": "refresh", "iat": now,
        "exp": now + timedelta(days=1), "jti": uuid.uuid4().hex,
    }  # fmt: skip
    token = jwt.encode(claims, get_settings().jwt_secret.get_secret_value(), algorithm="HS256")
    r = await _refresh(client, token)
    assert r.status_code == 401 and r.json()["error"]["code"] == "session_revoked"


async def test_sessions_are_private_to_their_user(client: httpx.AsyncClient) -> None:
    a, b = await register(client), await register(client)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, b.user_id)  # even inside a's org, b sees only b's rows
        n = await s.scalar(
            text("SELECT count(*) FROM sessions WHERE user_id = :u"), {"u": a.user_id}
        )
    assert n == 0


async def test_scheduler_tick_prunes_long_dead_sessions_only(client: httpx.AsyncClient) -> None:
    a = await register(client)
    day = timedelta(days=1)
    rows = {  # name: (expires_at, revoked_at) relative to now
        "expired_40d": (-40 * day, None),
        "revoked_40d": (day, -40 * day),
        "expired_10d": (-10 * day, None),
        "revoked_1h": (day, timedelta(hours=-1)),
        "live": (day, None),
    }
    ids = {name: uuid.uuid4() for name in rows}
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        for name, (exp, rev) in rows.items():
            await s.execute(text(
                "INSERT INTO sessions (id, org_id, user_id, family_id, created_at, expires_at, "
                "revoked_at) VALUES (:i, :o, :u, :i, now() - interval '50 days', now() + :exp, "
                "now() + CAST(:rev AS interval))"
            ), {"i": ids[name], "o": a.org_id, "u": a.user_id, "exp": exp, "rev": rev})  # fmt: skip
        await s.commit()
        with pytest.raises(Exception, match="permission denied"):  # still no DELETE for rag_app
            await s.execute(text("DELETE FROM sessions WHERE id = :i"), {"i": ids["live"]})
    await scheduler.tick()
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        left = set(await s.scalars(text("SELECT id FROM sessions WHERE id = ANY(:ids)"),
                                   {"ids": list(ids.values())}))  # fmt: skip
        # keep is floored at 1 day server-side, whatever the caller passes
        await s.execute(text("SELECT prune_sessions(interval '0')"))
        await s.commit()
        after = set(await s.scalars(text("SELECT id FROM sessions WHERE id = ANY(:ids)"),
                                    {"ids": list(ids.values())}))  # fmt: skip
    assert left == {ids["expired_10d"], ids["revoked_1h"], ids["live"]}
    assert after == {ids["revoked_1h"], ids["live"]}

"""Rate limits: 429 + Retry-After + error body, isolated per principal/IP, and the documented
Redis-outage policy (auth fails closed, everything else fails open)."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from redis.asyncio import Redis

from app.core import ratelimit
from app.core.config import get_settings
from app.main import app
from tests.conftest import register
from tests.test_auth_keys import bearer, make_key


@pytest.fixture(autouse=True)
def frozen_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed windows reset on the minute: requests that straddle a boundary land in two buckets
    and a test expecting the Nth one to be limited saw [401, 401, 401]. Freeze the limiter clock
    mid-window so every request of a test counts in one bucket."""
    now = (int(time.time()) // ratelimit.WINDOW_S + 0.5) * ratelimit.WINDOW_S
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(time=lambda: now))


def _client_from(ip: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=(ip, 1234))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def fresh_ip() -> AsyncIterator[httpx.AsyncClient]:
    async with _client_from(f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.7") as c:
        yield c


async def test_login_is_limited_per_ip_with_retry_after(
    client: httpx.AsyncClient, fresh_ip: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_auth", 3)
    body = {"email": "nobody@example.com", "password": "x"}
    codes = [(await fresh_ip.post("/v1/auth/login", json=body)).status_code for _ in range(3)]
    assert codes == [401, 401, 401]
    r = await fresh_ip.post("/v1/auth/login", json=body)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited" and r.json()["error"]["request_id"]
    assert 1 <= int(r.headers["retry-after"]) <= ratelimit.WINDOW_S
    # register shares the auth bucket; another IP is unaffected.
    assert (
        await fresh_ip.post("/v1/auth/register", json={"email": "a@b.co", "password": "x" * 8})
    ).status_code == 429
    async with _client_from("10.255.255.1" + str(uuid.uuid4().int % 9)) as other:
        assert (await other.post("/v1/auth/login", json=body)).status_code == 401


async def test_answer_and_search_limits_are_per_principal(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_search", 2)
    a, b = await register(client), await register(client)
    key = (await make_key(client, a, ["search"]))["key"]
    q = {"query": "anything"}
    for h in (a.headers, a.headers):
        assert (await client.post("/v1/search", json=q, headers=h)).status_code == 200
    r = await client.post("/v1/search", json=q, headers=a.headers)
    assert r.status_code == 429 and "retry-after" in r.headers
    # The user's key and another user each have their own bucket.
    assert (await client.post("/v1/search", json=q, headers=bearer(key))).status_code == 200
    assert (await client.post("/v1/search", json=q, headers=b.headers)).status_code == 200


async def test_unauthenticated_calls_do_not_spend_a_principal_bucket(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_search", 1)
    for _ in range(3):
        r = await client.post("/v1/search", json={"query": "q"})
        assert r.status_code == 401


class _DeadRedis:
    def pipeline(self, transaction: bool = True) -> Redis:
        return Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2).pipeline()


async def test_redis_outage_fails_closed_for_auth_and_open_elsewhere(
    client: httpx.AsyncClient, fresh_ip: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await register(client)
    monkeypatch.setattr(ratelimit, "_redis", _DeadRedis())
    r = await fresh_ip.post("/v1/auth/login", json={"email": a.email, "password": a.password})
    assert r.status_code == 503 and r.json()["error"]["code"] == "rate_limiter_unavailable"
    r = await client.post("/v1/search", json={"query": "q"}, headers=a.headers)
    assert r.status_code == 200


async def test_namespace_isolates_servers_sharing_redis_and_unset_keeps_prod_keys(
    fresh_ip: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_auth", 2)
    body = {"email": "nobody@example.com", "password": "x"}
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_namespace", f"dev-{uuid.uuid4().hex}")
    codes = [(await fresh_ip.post("/v1/auth/login", json=body)).status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    # Another server (namespace) on the same IP and Redis still has its own bucket.
    monkeypatch.setattr(settings, "rate_limit_namespace", f"test-{uuid.uuid4().hex}")
    assert (await fresh_ip.post("/v1/auth/login", json=body)).status_code == 401
    # Unset: the production key format is unchanged.
    monkeypatch.setattr(settings, "rate_limit_namespace", "")
    who = f"10.250.{uuid.uuid4().int % 250}.9"
    await ratelimit.hit("auth", who)
    window = int(ratelimit.time.time()) // ratelimit.WINDOW_S
    keys = [f"rl:auth:{who}:{w}" for w in (window, window - 1)]
    assert any([await ratelimit._client().get(k) for k in keys])


async def test_test_runs_get_their_own_namespace_from_settings() -> None:
    ns = get_settings().rate_limit_namespace  # set by conftest, so no dev server's bucket is hit
    assert ns.startswith("test-")
    who = f"10.251.{uuid.uuid4().int % 250}.9"
    await ratelimit.hit("auth", who)
    window = int(ratelimit.time.time()) // ratelimit.WINDOW_S
    keys = [f"rl:{ns}:auth:{who}:{w}" for w in (window, window - 1)]
    assert any([await ratelimit._client().get(k) for k in keys])


def test_client_is_rebuilt_per_event_loop() -> None:
    # The serve helpers seed on asyncio.run() and then serve on uvicorn's loop; a client cached on
    # the first loop used to crash the first login with a 500 (AttributeError, not RedisError).
    who = f"10.252.{uuid.uuid4().int % 250}.9"
    for _ in range(2):
        asyncio.run(ratelimit.hit("auth", who))


def _rand_ip() -> str:
    return f"198.18.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"


async def test_clients_behind_the_trusted_bff_get_separate_auth_buckets(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # S11: every browser login arrives from the BFF's address (127.0.0.1 here, trusted by default);
    # its X-Forwarded-For names the real client, so one attacker can't lock everyone out.
    monkeypatch.setattr(get_settings(), "rate_limit_auth", 2)
    body = {"email": "nobody@example.com", "password": "x"}
    attacker, victim = {"X-Forwarded-For": _rand_ip()}, {"X-Forwarded-For": _rand_ip()}
    codes = [
        (await client.post("/v1/auth/login", json=body, headers=attacker)).status_code
        for _ in range(3)
    ]
    assert codes == [401, 401, 429]
    assert (await client.post("/v1/auth/login", json=body, headers=victim)).status_code == 401


async def test_spoofed_forwarded_for_from_untrusted_peer_is_ignored(
    fresh_ip: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A direct caller rotating fake XFF values still spends its own (peer address) bucket.
    monkeypatch.setattr(get_settings(), "rate_limit_auth", 2)
    body = {"email": "nobody@example.com", "password": "x"}
    codes = [
        (
            await fresh_ip.post(
                "/v1/auth/login", json=body, headers={"X-Forwarded-For": _rand_ip()}
            )
        ).status_code
        for _ in range(3)
    ]
    assert codes == [401, 401, 429]


async def test_stale_stream_slot_expires_and_release_frees_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot left by a crashed process stops counting after STREAM_TTL_S."""
    monkeypatch.setattr(get_settings(), "max_concurrent_streams", 1)
    who = f"u:{uuid.uuid4()}"
    stale = ratelimit.time.time() - ratelimit.STREAM_TTL_S - 1
    await ratelimit._client().zadd(ratelimit._key("streams", who), {"crashed": stale})
    release = await ratelimit.stream_slot(who)
    with pytest.raises(ratelimit.RateLimited, match="open streams"):
        await ratelimit.stream_slot(who)
    await release()
    await (await ratelimit.stream_slot(who))()


async def test_stream_slots_fail_open_when_redis_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    dead = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    monkeypatch.setattr(ratelimit, "_client", lambda: dead)
    monkeypatch.setattr(get_settings(), "max_concurrent_streams", 1)
    for _ in range(3):
        await ratelimit.stream_slot("u:any")  # no 429, no 503

"""Fixed-window rate limits in Redis, declared per route as dependencies.

Outage policy: `auth` fails CLOSED (503) because the per-IP limit is the only brute-force brake
on login/register; every other group fails OPEN (logged) so a Redis blip doesn't take search and
answers down for signed-in users, whose spend is bounded separately.

Concurrent streams (H2): each open SSE stream (answer, agent events) holds one slot per principal
in a Redis sorted set; the slot is released when the stream ends or the client disconnects, and
an entry older than STREAM_TTL_S is dropped (a crashed process cannot leak slots for longer).
Redis down = fail OPEN (logged), like the other non-auth groups.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Literal

import anyio
from fastapi import Depends, Request, params
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.errors import AppError, request_id_var
from app.core.tenancy import Tenant, get_tenant

Group = Literal["auth", "answer", "upload", "search"]
WINDOW_S = 60
# > agent STREAM_MAX_S (900). ponytail: a single stream open longer than this stops counting.
STREAM_TTL_S, STREAM_RETRY_S = 960, 10
log = logging.getLogger("app.ratelimit")
_redis: Redis | None = None
_redis_loop: asyncio.AbstractEventLoop | None = None


class RateLimited(AppError):
    def __init__(
        self, retry_after: int, code: str = "rate_limited", what: str = "requests"
    ) -> None:
        super().__init__(429, code, f"Too many {what}. Retry in {retry_after} s.")
        self.retry_after = retry_after


async def rate_limited_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RateLimited)  # noqa: S101  # narrows the generic handler signature
    body = {"error": {"code": exc.code, "message": exc.message, "request_id": request_id_var.get()}}
    return JSONResponse(body, 429, headers={"Retry-After": str(exc.retry_after)})


def _client() -> Redis:
    # Pooled connections belong to the loop that opened them; reused on another loop they crash
    # with a non-Redis error (a 500, not the auth 503). One client per running loop.
    global _redis, _redis_loop
    loop = asyncio.get_running_loop()
    if _redis is None or _redis_loop is not loop:
        url = get_settings().redis_url
        _redis = Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        _redis_loop = loop
    return _redis


async def hit(group: Group, who: str) -> None:
    """Counts one request for `who` in `group`; raises RateLimited over the limit."""
    limit: int = getattr(get_settings(), f"rate_limit_{group}")
    now = int(time.time())
    key = _key(group, f"{who}:{now // WINDOW_S}")
    try:
        async with _client().pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, WINDOW_S)
            count, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        log.warning("rate limiter unavailable", extra={"group": group, "error": type(exc).__name__})
        if group == "auth":
            raise AppError(
                503,
                "rate_limiter_unavailable",
                "Sign-in is briefly unavailable. Retry in a minute.",
            ) from exc
        return
    if count > limit:
        raise RateLimited(WINDOW_S - now % WINDOW_S)


def _key(name: str, who: str) -> str:
    ns = get_settings().rate_limit_namespace
    return f"rl:{ns + ':' if ns else ''}{name}:{who}"


async def _noop() -> None:
    return None


async def stream_slot(who: str) -> Callable[[], Awaitable[None]]:
    """Takes one of `who`'s concurrent-stream slots and returns its release; raises
    RateLimited(too_many_streams) when all are taken. Call before the stream starts."""
    key, token, now = _key("streams", who), uuid.uuid4().hex, time.time()
    try:
        async with _client().pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, "-inf", now - STREAM_TTL_S)
            pipe.zadd(key, {token: now})
            pipe.zcard(key)
            pipe.expire(key, STREAM_TTL_S)
            count = (await pipe.execute())[2]
    except (RedisError, OSError) as exc:
        log.warning("stream limiter unavailable", extra={"error": type(exc).__name__})
        return _noop

    async def release() -> None:
        with anyio.CancelScope(shield=True):  # runs while a client disconnect cancels the stream
            try:
                await _client().zrem(key, token)
            except (RedisError, OSError) as exc:
                log.warning("stream slot release failed", extra={"error": type(exc).__name__})

    if count > get_settings().max_concurrent_streams:
        await release()
        raise RateLimited(STREAM_RETRY_S, "too_many_streams", "open streams")
    return release


async def held[T](
    release: Callable[[], Awaitable[None]], gen: AsyncIterator[T]
) -> AsyncIterator[T]:
    """`gen`, then `release()` however the stream ends (done, error, client gone)."""
    try:
        async for item in gen:
            yield item
    finally:
        await release()


def principal(t: Tenant) -> str:
    return f"k:{t.api_key_id}" if t.api_key_id else f"u:{t.user_id}"


def by_ip(group: Group) -> params.Depends:
    async def dep(request: Request) -> None:
        await hit(group, request.client.host if request.client else "unknown")

    return params.Depends(dep)


def by_principal(group: Group) -> params.Depends:
    async def dep(t: Tenant = Depends(get_tenant)) -> None:
        await hit(group, principal(t))

    return params.Depends(dep)

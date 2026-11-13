"""OAuth 2.0 authorization code + PKCE (S256) for connectors, plus token refresh and revoke.

Flow: an editor calls start() through the API; the pending source (org, user, kb, config), the
PKCE verifier and a hash of the browser binding (the BFF's httpOnly `rag_oauth` cookie, sent as
X-OAuth-Binding) wait in Redis under a random single-use `state` (10 min). The provider sends
the browser to the FRONTEND finish page, which POSTs code+state to the authenticated finish
endpoint; the state is consumed atomically (GETDEL), so an unknown, replayed or expired state is
refused, and bound() refuses a state started by another user, org or browser (RFC 9700 4.7:
login CSRF / a victim finishing an attacker's flow). The verifier binds the code to this flow,
so a code minted for someone else's flow cannot be injected. Tokens live only in
sources.secret_enc (Fernet) as JSON: {access_token, refresh_token, expires_at, scope, source_id,
org_id}.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlencode

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update

from app.connectors.base import ConnectorError
from app.connectors.models import Source
from app.core.config import get_settings
from app.core.crypto import decrypt, encrypt
from app.core.db import use_tenant, worker_sessions
from app.core.errors import AppError

log = logging.getLogger("app.connectors.oauth")
STATE_TTL_S = 600
REFRESH_MARGIN_S = 60
CALLBACK_PATH = "/oauth/finish"  # frontend page; the API has no public callback
BINDING_LEN = (32, 128)  # X-OAuth-Binding length bounds (the BFF sends 43 chars)
# Tests swap this for an httpx.MockTransport; production uses the default network transport.
TRANSPORT: httpx.AsyncBaseTransport | None = None
_redis: Redis | None = None


@dataclass(frozen=True)
class Provider:
    title: str  # "Google Drive"; used in user-facing messages
    authorize_url: str
    token_url: str
    revoke_url: str | None
    scopes: tuple[str, ...]
    credentials: Callable[[], tuple[str, str] | None]  # (client_id, client_secret) or unset
    auth_params: dict[str, str] = field(default_factory=dict)
    # Scopes the grant must contain; empty = all of `scopes`. Microsoft never echoes
    # offline_access (the refresh token proves it), so it lists only the data scope.
    required_scopes: tuple[str, ...] = ()
    # Dropbox revokes with the token as bearer (no body); RFC 7009 providers take it in the form.
    revoke_bearer: bool = False

    def reconnect(self) -> str:
        return f"{self.title} access was revoked or has expired. Reconnect {self.title}."


class ReconnectRequired(ConnectorError):
    """The grant is gone (revoked, expired, never stored): only a new sign-in fixes it."""


def http() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=TRANSPORT, timeout=30, follow_redirects=False)


def redirect_uri() -> str:
    return get_settings().oauth_redirect_base.rstrip("/") + CALLBACK_PATH


def _client(p: Provider) -> tuple[str, str]:
    creds = p.credentials()
    if creds is None:
        raise AppError(422, "oauth_not_configured", f"{p.title} OAuth is not configured.")
    return creds


def _redis_client() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, socket_timeout=2)
    return _redis


def _digest(binding: str) -> str:
    return hashlib.sha256(binding.encode()).hexdigest()


async def start(p: Provider, pending: dict[str, Any], binding: str) -> str:
    """Stores `pending` + a PKCE verifier + the browser binding hash under a fresh state;
    returns the authorize URL."""
    client_id, _ = _client(p)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    stored = {**pending, "verifier": verifier, "bind": _digest(binding)}
    try:
        await _redis_client().set(f"oauth:state:{state}", json.dumps(stored), ex=STATE_TTL_S)
    except (RedisError, OSError) as exc:
        raise AppError(503, "oauth_unavailable", "Connecting is briefly unavailable.") from exc
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(p.scopes),
        "state": state,
        "code_challenge": challenge.decode().rstrip("="),
        "code_challenge_method": "S256",
        **p.auth_params,
    }
    return f"{p.authorize_url}?{urlencode(query)}"


async def consume_state(state: str) -> dict[str, Any] | None:
    """The pending flow for `state`, deleted in the same step (single use); None if unknown."""
    if not state or len(state) > 100:
        return None
    try:
        raw = await _redis_client().getdel(f"oauth:state:{state}")
    except (RedisError, OSError):
        log.warning("oauth state store unavailable")
        return None
    return json.loads(raw) if raw else None


def bound(pending: dict[str, Any], user: uuid.UUID, org: uuid.UUID, binding: str) -> bool:
    """Finished by the same signed-in user, in the same org and browser that began the flow."""
    same_browser = hmac.compare_digest(str(pending.get("bind", "")), _digest(binding))
    return pending.get("user") == str(user) and pending.get("org") == str(org) and same_browser


async def _token_call(p: Provider, form: dict[str, str]) -> dict[str, Any]:
    client_id, client_secret = _client(p)
    form = {**form, "client_id": client_id, "client_secret": client_secret}
    try:
        async with http() as c:
            r = await c.post(p.token_url, data=form, headers={"Accept": "application/json"})
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ConnectorError(f"Could not reach {p.title}. Try again.") from exc
    if r.status_code != 200 or not isinstance(body, dict) or "access_token" not in body:
        err = body.get("error") if isinstance(body, dict) else None
        if err == "invalid_grant":
            raise ReconnectRequired(p.reconnect())
        # Only the provider's error code is logged: the body may echo request data.
        log.warning("oauth token call failed", extra={"status": r.status_code, "error": str(err)})
        raise ConnectorError(f"{p.title} refused the sign-in ({str(err)[:40]}). Try again.")
    return body


def _tokens(body: dict[str, Any], old_refresh: str | None = None) -> dict[str, Any]:
    return {
        "access_token": str(body["access_token"]),
        # Google returns a refresh token only on the first exchange; keep the stored one.
        "refresh_token": body.get("refresh_token") or old_refresh,
        "expires_at": time.time() + float(body.get("expires_in", 3600)),
        "scope": str(body.get("scope", "")),
    }


async def exchange(p: Provider, code: str, verifier: str) -> dict[str, Any]:
    body = await _token_call(
        p,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri(),
        },
    )
    return _tokens(body)


def granted(p: Provider, tokens: dict[str, Any]) -> bool:
    """The user granted every required scope and offline access (a refresh token). Granted
    names are compared case-insensitively, also without a resource prefix: Microsoft may answer
    `https://graph.microsoft.com/Files.Read.All` (and URL-encoded) for `Files.Read.All`."""
    got = {g.lower() for g in unquote(str(tokens.get("scope", ""))).split()}
    got |= {g.rsplit("/", 1)[-1] for g in got}
    need = {s.lower() for s in p.required_scopes or p.scopes}
    return bool(tokens.get("refresh_token")) and need <= got


async def revoke(p: Provider, secret: str | None) -> None:
    """Best effort: a failed revoke is logged, never raised (the source is already gone)."""
    if p.revoke_url is None or not secret:
        return
    try:
        tokens = json.loads(secret)
        token = tokens.get("refresh_token") or tokens.get("access_token")
        async with http() as c:  # token in the form body, never the URL (URLs get logged)
            if p.revoke_bearer:  # revoking the access token also disables its refresh token
                bearer = await TokenSession(p, secret).bearer()
                r = await c.post(p.revoke_url, headers={"Authorization": f"Bearer {bearer}"})
            else:
                r = await c.post(p.revoke_url, data={"token": token})
        if r.status_code != 200:
            log.warning("oauth revoke refused", extra={"status": r.status_code})
    except (httpx.HTTPError, ValueError, AttributeError, ConnectorError) as exc:
        log.warning("oauth revoke failed", extra={"error": type(exc).__name__})


class TokenSession:
    """Access token for one sync/test, refreshed when under REFRESH_MARGIN_S is left (or after a
    401). Refreshed tokens are written back to the source so later runs reuse them."""

    def __init__(self, p: Provider, secret: str | None) -> None:
        self.p = p
        try:
            tokens = json.loads(secret or "null")
        except ValueError:
            tokens = None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise ReconnectRequired(f"Not connected. Reconnect {p.title}.")
        self.tokens: dict[str, Any] = tokens

    async def bearer(self, force: bool = False) -> str:
        if force or float(self.tokens.get("expires_at", 0)) - time.time() < REFRESH_MARGIN_S:
            await self.refresh()
        return str(self.tokens["access_token"])

    async def refresh(self) -> None:
        old = self.tokens.get("refresh_token")
        if not old:
            raise ReconnectRequired(self.p.reconnect())
        body = await _token_call(self.p, {"grant_type": "refresh_token", "refresh_token": old})
        self.tokens = {**self.tokens, **_tokens(body, old)}
        await self._persist()

    async def _persist(self) -> None:
        try:
            source_id, org_id = (
                uuid.UUID(self.tokens["source_id"]),
                uuid.UUID(self.tokens["org_id"]),
            )
            async with worker_sessions()() as s:
                await use_tenant(s, org_id, None)
                # Locked read: a reconnect (new grant) landing meanwhile must not be overwritten
                # by this older grant's refresh; the reconnect's own write waits for this commit.
                stored = await s.scalar(
                    select(Source.secret_enc).where(Source.id == source_id).with_for_update()
                )
                current = json.loads(decrypt(stored)) if stored else {}
                if current.get("grant") != self.tokens.get("grant"):
                    log.info("oauth refresh not persisted: the source has a newer grant")
                    return
                stmt = update(Source).where(Source.id == source_id)
                await s.execute(stmt.values(secret_enc=encrypt(json.dumps(self.tokens))))
                await s.commit()
        except Exception as exc:  # the in-memory token still works for this run
            log.warning("oauth token persist failed", extra={"error": type(exc).__name__})

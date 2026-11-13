import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Literal

import jwt
from pwdlib import PasswordHash
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.errors import AppError

TokenType = Literal["access", "refresh"]
_ALGORITHM = "HS256"
_hasher = PasswordHash.recommended()
# Verified against when the email is unknown, so login timing doesn't reveal which emails exist.
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-real-password")


class TokenClaims(BaseModel):
    sub: uuid.UUID
    org: uuid.UUID
    type: TokenType
    jti: str = ""  # refresh: the sessions row id (hex)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  # OAuth2 scheme name, not a secret
    expires_in: int


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, pw_hash: str | None) -> bool:
    return _hasher.verify(password, pw_hash or _DUMMY_HASH) and pw_hash is not None


def _encode(
    user_id: uuid.UUID, org_id: uuid.UUID, kind: TokenType, iat: datetime, exp: datetime, jti: str
) -> str:
    claims = {
        "sub": str(user_id), "org": str(org_id), "type": kind, "iat": iat, "exp": exp, "jti": jti,
    }  # fmt: skip
    return jwt.encode(claims, get_settings().jwt_secret.get_secret_value(), algorithm=_ALGORITHM)


def issue_tokens(
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    issued_at: datetime | None = None,
) -> TokenPair:
    """The pair of one sessions row. Deterministic in its inputs, so re-issuing a row returns the
    identical strings: that is how a refresh retried inside the grace window gets the same pair.
    The refresh jti is the row id; HS256 signing means DB access alone cannot mint a token.
    Access tokens are stateless: revoking a session takes effect at its next refresh (<= TTL).
    Without a session (scripts, evals) the refresh token has no row, so it never refreshes."""
    s = get_settings()
    session_id, issued_at = session_id or uuid.uuid4(), issued_at or datetime.now(UTC)
    access_exp = issued_at + timedelta(minutes=s.access_token_ttl_minutes)
    refresh_exp = issued_at + timedelta(days=s.refresh_token_ttl_days)
    left = int((access_exp - datetime.now(UTC)).total_seconds())
    sid = session_id.hex
    return TokenPair(
        access_token=_encode(user_id, org_id, "access", issued_at, access_exp, f"{sid}a"),
        refresh_token=_encode(user_id, org_id, "refresh", issued_at, refresh_exp, sid),
        expires_in=max(left, 1),
    )


def decode_token(token: str, expected: TokenType) -> TokenClaims:
    try:
        raw = jwt.decode(
            token,
            get_settings().jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
            options={"require": ["exp", "sub", "org", "type"]},
        )
        claims = TokenClaims.model_validate(raw)
    except (jwt.PyJWTError, ValueError) as exc:
        raise AppError(
            401, "invalid_token", "Your session is invalid or expired. Sign in again."
        ) from exc
    if claims.type != expected:
        raise AppError(401, "invalid_token", "Your session is invalid or expired. Sign in again.")
    return claims


API_KEY_PREFIX = "rag_"


def _pepper() -> bytes:
    s = get_settings()
    if s.api_key_pepper is not None:
        return s.api_key_pepper.get_secret_value().encode()
    return hmac.digest(s.jwt_secret.get_secret_value().encode(), b"api-key-pepper-v1", "sha256")


def keyed_hash(value: str) -> str:
    """HMAC-SHA256 under the server pepper: API-key secrets and audit IPs."""
    return hmac.new(_pepper(), value.encode(), hashlib.sha256).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    """(plaintext, prefix, hash). The prefix is the O(1) lookup key; the 256-bit secret is
    never stored, only its peppered HMAC (a slow hash buys nothing against 256 random bits)."""
    prefix, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    return f"{API_KEY_PREFIX}{prefix}_{secret}", prefix, keyed_hash(secret)


def split_api_key(key: str) -> tuple[str, str] | None:
    """(prefix, secret) of a well-formed key, else None."""
    if not key.startswith(API_KEY_PREFIX):
        return None
    prefix, sep, secret = key[len(API_KEY_PREFIX) :].partition("_")
    if not sep or len(prefix) != 12 or not secret:
        return None
    return prefix, secret


def api_key_matches(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(keyed_hash(secret), stored_hash)


class SecurityHeadersMiddleware:
    """S12: API bodies (JSON, SSE, CSV) are never meant to be framed or content-sniffed."""

    _headers: ClassVar = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY")]

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *self._headers]
            await send(message)

        await self.app(scope, receive, send_with_headers)

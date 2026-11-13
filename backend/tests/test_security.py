import importlib
import io
import logging
import subprocess
import sys
from collections.abc import Iterator

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import ValidationError

from app.core import crypto, security
from app.core.config import Settings, get_settings
from app.core.logging import JsonFormatter
from app.llm.config import get_llm_settings
from app.main import app
from tests.conftest import register


def test_crypto_round_trip_and_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "postgres://admin:hunter2@db/prod"
    token = crypto.encrypt(secret)
    assert secret.encode() not in token
    assert crypto.decrypt(token) == secret

    # rotation: new primary key prepended, old ciphertext still decrypts
    old = get_settings().fernet_keys.get_secret_value()
    monkeypatch.setenv("FERNET_KEYS", f"{Fernet.generate_key().decode()},{old}")
    get_settings.cache_clear()
    crypto._fernet.cache_clear()
    try:
        assert crypto.decrypt(token) == secret
        monkeypatch.setenv("FERNET_KEYS", Fernet.generate_key().decode())
        get_settings.cache_clear()
        crypto._fernet.cache_clear()
        with pytest.raises(InvalidToken):
            crypto.decrypt(token)
    finally:
        monkeypatch.setenv("FERNET_KEYS", old)
        get_settings.cache_clear()
        crypto._fernet.cache_clear()


def test_config_rejects_weak_or_malformed_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FERNET_KEYS", "not-a-key")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
    monkeypatch.setenv("FERNET_KEYS", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "short")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_blank_optional_secrets_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # N1/N2: an empty .env line must not count as configured (an empty pepper = unkeyed IP hashes;
    # an empty OAuth secret = a connector that shows as set up and fails at token exchange).
    for name in (
        "API_KEY_PEPPER",
        "EMBEDDING_API_KEY",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "MICROSOFT_OAUTH_CLIENT_SECRET",
        "DROPBOX_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.setenv(name, " ")
    s = Settings()  # type: ignore[call-arg]
    assert s.api_key_pepper is None and s.embedding_api_key is None
    assert s.google_oauth_client_id is None and s.google_oauth_client_secret is None
    assert s.microsoft_oauth_client_secret is None and s.dropbox_oauth_client_secret is None
    monkeypatch.setattr(security, "get_settings", lambda: s)
    assert security._pepper() != b"" and len(security._pepper()) == 32  # derived from jwt_secret
    monkeypatch.setenv("API_KEY_PEPPER", "set-pepper")
    assert Settings().api_key_pepper is not None  # type: ignore[call-arg]


async def test_api_responses_forbid_framing_and_sniffing(client: httpx.AsyncClient) -> None:
    for r in (await client.get("/healthz"), await client.get("/v1/kbs")):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"


PROVIDER_KEYS = {
    "ANTHROPIC_API_KEY": "anthropic-" + "a1" * 20,
    "OPENAI_API_KEY": "openai-" + "b2" * 20,
    "EMBEDDING_API_KEY": "embed-" + "c3" * 20,
    "QDRANT_API_KEY": "qdrant-" + "d4" * 20,
    "RERANK_API_KEY": "rerank-" + "e5" * 20,
}


@pytest.fixture
def provider_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    for name, value in PROVIDER_KEYS.items():
        monkeypatch.setenv(name, value)
    get_llm_settings.cache_clear()
    yield PROVIDER_KEYS
    get_llm_settings.cache_clear()


async def test_secrets_never_reach_logs(
    client: httpx.AsyncClient, provider_keys: dict[str, str]
) -> None:
    """AC11: drive auth + crypto through the real JSON formatter and grep the output."""
    settings = Settings()  # type: ignore[call-arg]  # fresh, so it sees the provider keys
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter(settings.secret_values()))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        acct = await register(client)
        await client.post("/v1/auth/login", json={"email": acct.email, "password": acct.password})
        credential = "sk-live-" + "9" * 32
        crypto.encrypt(credential)
        log = logging.getLogger("app.test")
        # a careless caller logging secrets must still be scrubbed
        log.warning("login password=%s token: %s", acct.password, acct.tokens["access_token"])
        log.warning("api_key=%s", credential)
        log.warning(
            "jwt %s fernet %s",
            settings.jwt_secret.get_secret_value(),
            settings.fernet_keys.get_secret_value(),
        )
        # provider keys in free text (no key=value shape for the pattern scrubber to catch)
        log.warning("llm keys %s", " ".join(provider_keys.values()))
    finally:
        root.removeHandler(handler)

    out = buf.getvalue()
    assert '"path": "/v1/auth/login"' in out  # request logging actually ran
    for secret in [
        acct.password,
        acct.tokens["access_token"],
        acct.tokens["refresh_token"],
        credential,
        *settings.secret_values(),
        *provider_keys.values(),
    ]:
        assert secret not in out
    assert "[REDACTED]" in out


async def test_unhandled_error_hides_internals(client: httpx.AsyncClient) -> None:
    async def boom() -> None:
        raise RuntimeError("db password is hunter2")

    # The app is shared across the session: remove the route so route-table tests never see it.
    app.add_api_route("/__boom", boom)
    try:
        r = await client.get("/__boom")
    finally:
        app.router.routes.pop()
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in r.text and "Traceback" not in r.text
    assert r.json()["error"]["request_id"]


async def test_health_and_cors(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).json() == {"status": "ready"}
    pre = await client.options(
        "/v1/kbs",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert pre.headers["access-control-allow-origin"] == "http://localhost:3000"
    evil = await client.options(
        "/v1/kbs", headers={"Origin": "http://evil.test", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in evil.headers


def test_worker_imports_with_redis_broker() -> None:
    worker = importlib.import_module("app.worker")
    assert type(worker.broker).__name__ == "RedisBroker"


def test_worker_entry_point_imports_in_a_fresh_interpreter() -> None:
    """`dramatiq app.worker` imports this module first; config must not cycle back into it."""
    r = subprocess.run(
        [sys.executable, "-c", "import app.worker"], capture_output=True, text=True, timeout=180
    )
    assert r.returncode == 0, r.stderr[-2000:]

"""H3: a token refresh on an OLD grant never overwrites the tokens of a reconnect that landed while
the refresh was in flight (H2 grant ids; oauth.TokenSession._persist)."""

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.connectors import oauth
from app.connectors.gdrive import GOOGLE
from app.core.crypto import encrypt
from app.core.db import SessionLocal, use_tenant
from tests.conftest import Account, register
from tests.test_connectors_gdrive import FakeGoogle, _connect, _secret, _tree, fake  # noqa: F401


async def _store(a: Account, sid: str, tokens: dict[str, Any]) -> None:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(text("UPDATE sources SET secret_enc = :e WHERE id = :i"),
                        {"e": encrypt(json.dumps(tokens)), "i": sid})  # fmt: skip
        await s.commit()


async def test_refresh_on_a_superseded_grant_keeps_the_reconnected_tokens(
    client: httpx.AsyncClient,
    fake: FakeGoogle,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["DEEP0001"]})
    old = await _secret(a, sid)
    reconnected = {**old, "access_token": "NEW-GRANT-TOKEN", "grant": uuid.uuid4().hex}
    real = oauth._token_call

    async def reconnect_meanwhile(p: oauth.Provider, form: dict[str, str]) -> dict[str, Any]:
        body = await real(p, form)
        await _store(a, sid, reconnected)  # the user reconnects while this refresh is in flight
        return body

    monkeypatch.setattr(oauth, "_token_call", reconnect_meanwhile)
    stale = oauth.TokenSession(GOOGLE, json.dumps(old))
    await stale.refresh()
    assert stale.tokens["access_token"] in fake.access  # this run still gets a working token
    assert await _secret(a, sid) == reconnected

    monkeypatch.setattr(oauth, "_token_call", real)
    current = oauth.TokenSession(GOOGLE, json.dumps(reconnected))
    await current.refresh()  # same grant: the refresh is persisted as before
    stored = await _secret(a, sid)
    assert stored["grant"] == reconnected["grant"]
    assert stored["access_token"] == current.tokens["access_token"] != "NEW-GRANT-TOKEN"

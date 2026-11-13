"""A7a: Google Drive connector + generic OAuth (code + PKCE) against a contract fake of Google.

FakeGoogle answers like the documented endpoints (accounts.google.com authorize, oauth2.googleapis
.com token/revoke, Drive v3 about/files/files.export/changes) and enforces PKCE S256, client
credentials, bearer expiry and refresh-token revocation. Live Google is never called.
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import docx
import httpx
import openpyxl
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.connectors import gdrive, oauth
from app.connectors import sync as sync_mod
from app.connectors.gdrive import FOLDER, SCOPE, GDriveConnector
from app.connectors.registry import REGISTRY
from app.connectors.sync import sync_source
from app.core.config import get_settings
from app.core.crypto import decrypt
from app.core.db import SessionLocal, use_tenant
from app.ingestion.config import get_ingestion_settings
from tests.conftest import Account, register
from tests.test_connectors import _docs, _kb, _search, _sync, env  # noqa: F401

CLIENT_ID, CLIENT_SECRET = "cid-test.apps.googleusercontent.com", "gocspx-test-client-secret"
ROOT = "ROOTID0001"
DOC, SHEET, SLIDES = (f"application/vnd.google-apps.{k}" for k in ("document", "spreadsheet",
                                                                    "presentation"))  # fmt: skip
FRONT = "http://localhost:3000"
REDIRECT_URI = f"{FRONT}/oauth/finish"
BIND = "browser-binding-" + "a" * 27  # what the BFF's rag_oauth cookie carries (43 chars)


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """photo.png is now a supported type (B8); pin OCR off so it fails the same way everywhere."""
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_ingestion_settings.cache_clear()
    yield
    get_ingestion_settings.cache_clear()


class FakeGoogle:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, str] = {}
        self.codes: dict[str, str] = {}  # code -> PKCE challenge it was issued for
        self.access: dict[str, float] = {}  # access token -> expiry
        self.refresh_ok: set[str] = set()
        self.changes: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.revoked: list[str] = []
        self.refreshes = 0
        self.expires_in = 3600
        self.page_size = 2
        self.files[ROOT] = {"id": ROOT, "name": "My Drive", "mimeType": FOLDER, "parents": []}

    # ---- test helpers
    def put(self, fid: str, name: str, mime: str, parent: str, body: str = "", **kw: Any) -> None:
        old = self.files.get(fid, {})
        f = {"id": fid, "name": name, "mimeType": mime, "parents": [parent], "trashed": False,
             "version": str(int(old.get("version", 0)) + 1),
             "modifiedTime": f"2026-11-12T00:00:{len(self.changes):02d}Z", **kw}  # fmt: skip
        if mime != FOLDER and not mime.startswith("application/vnd.google-apps."):
            f["md5Checksum"] = hashlib.md5(body.encode()).hexdigest()  # noqa: S324
            f["size"] = str(len(body.encode()))
        self.files[fid], self.blobs[fid] = f, body
        self.changes.append({"fileId": fid, "removed": False, "file": dict(f)})

    def remove(self, fid: str) -> None:
        del self.files[fid]
        self.changes.append({"fileId": fid, "removed": True})

    def trash(self, fid: str) -> None:
        self.files[fid]["trashed"] = True
        self.changes.append({"fileId": fid, "removed": False, "file": dict(self.files[fid])})

    def authorize(self, query: dict[str, list[str]]) -> str:
        """What the user's consent does at Google: a code bound to the request's challenge."""
        assert query["code_challenge_method"] == ["S256"]
        code = "4/0" + uuid.uuid4().hex
        self.codes[code] = query["code_challenge"][0]
        return code

    def drive_calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/drive/v3/{path}"]

    # ---- endpoints
    def _issue(self, refresh: bool) -> dict[str, Any]:
        tok = "ya29." + uuid.uuid4().hex
        self.access[tok] = time.time() + self.expires_in
        out: dict[str, Any] = {"access_token": tok, "expires_in": self.expires_in,
                               "scope": SCOPE, "token_type": "Bearer"}  # fmt: skip
        if refresh:
            out["refresh_token"] = rt = "1//0" + uuid.uuid4().hex
            self.refresh_ok.add(rt)
        return out

    def _token(self, form: dict[str, str]) -> httpx.Response:
        bad = httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad"})
        if (form.get("client_id"), form.get("client_secret")) != (CLIENT_ID, CLIENT_SECRET):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            challenge = self.codes.pop(form.get("code", ""), None)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if challenge != base64.urlsafe_b64encode(digest).decode().rstrip("="):
                return bad
            if form.get("redirect_uri") != REDIRECT_URI:
                return httpx.Response(400, json={"error": "redirect_uri_mismatch"})
            return httpx.Response(200, json=self._issue(refresh=True))
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") not in self.refresh_ok:
                return bad
            self.refreshes += 1
            return httpx.Response(200, json=self._issue(refresh=False))  # no new refresh token
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _list(self, q: dict[str, str], page: list[dict[str, Any]]) -> dict[str, Any]:
        start = int(q.get("pageToken", "0"))
        out: dict[str, Any] = {"files": page[start : start + self.page_size]}
        if start + self.page_size < len(page):
            out["nextPageToken"] = str(start + self.page_size)
        return out

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        host, path = req.url.host, req.url.path
        if host == "oauth2.googleapis.com":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            if path == "/revoke":
                self.revoked.append(form["token"])
                return httpx.Response(200)
            return self._token(form)
        assert host == "www.googleapis.com" and path.startswith("/drive/v3/")
        auth = req.headers.get("Authorization", "")
        if self.access.get(auth.removeprefix("Bearer "), 0) < time.time():
            return httpx.Response(401, json={"error": {"code": 401, "errors": [
                {"reason": "authError"}]}})  # fmt: skip
        q, path = dict(req.url.params), path.removeprefix("/drive/v3/")
        if path == "about":
            return httpx.Response(200, json={"user": {"emailAddress": "owner@example.com"}})
        if path == "changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": str(len(self.changes))})
        if path == "changes":
            start = int(q["pageToken"])
            out: dict[str, Any] = {"changes": self.changes[start : start + self.page_size]}
            nxt = start + self.page_size
            out.update({"nextPageToken": str(nxt)} if nxt < len(self.changes)
                       else {"newStartPageToken": str(len(self.changes))})  # fmt: skip
            return httpx.Response(200, json=out)
        if path == "files":
            m = re.fullmatch(r"'([\w-]+)' in parents and trashed = false", q["q"])
            assert m, q["q"]
            alldrives = q.get("includeItemsFromAllDrives") == "true"
            kids = [f for f in self.files.values() if m[1] in f["parents"] and not f["trashed"]
                    and (alldrives or "driveId" not in f)]  # fmt: skip
            return httpx.Response(200, json=self._list(q, kids))
        fid, _, export = path.removeprefix("files/").partition("/")
        f = self.files.get(ROOT if fid == "root" else fid)
        if f is None:
            return httpx.Response(404, json={"error": {"code": 404, "errors": [
                {"reason": "notFound"}]}})  # fmt: skip
        if export:
            body = _export(q["mimeType"], self.blobs[f["id"]])
            return httpx.Response(200, stream=httpx.ByteStream(body))  # unread, like the network
        if q.get("alt") == "media":
            return httpx.Response(200, stream=httpx.ByteStream(self.blobs[f["id"]].encode()))
        return httpx.Response(200, json=f)


def _export(mime: str, body: str) -> bytes:
    buf = io.BytesIO()
    if mime == gdrive.DOCX:
        d = docx.Document()
        d.add_paragraph(body)
        d.save(buf)
    elif mime == gdrive.XLSX:
        wb = openpyxl.Workbook()
        wb.active.append(["note", body])  # type: ignore[union-attr]
        wb.save(buf)
    else:
        return body.encode()
    return buf.getvalue()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    g = FakeGoogle()
    s = get_settings()
    monkeypatch.setattr(s, "connector_gdrive_enabled", True)
    monkeypatch.setattr(s, "google_oauth_client_id", CLIENT_ID)
    monkeypatch.setattr(s, "google_oauth_client_secret", SecretStr(CLIENT_SECRET))
    monkeypatch.setattr(s, "oauth_redirect_base", FRONT)
    monkeypatch.setitem(REGISTRY, "gdrive", GDriveConnector())
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(g.handler))
    monkeypatch.setattr(gdrive, "BACKOFF_S", 0)
    yield g


def _tree(g: FakeGoogle) -> None:
    g.put("SUB00001", "sub", FOLDER, ROOT)
    g.put("DEEP0001", "deep", FOLDER, "SUB00001")
    g.put("FILEA001", "a.md", "text/markdown", ROOT, "# Alpha\n\nalpha zebra quokka\n")
    g.put("GDOC0001", "Design doc", DOC, ROOT, "beta design narwhal")
    g.put("PNG00001", "photo.png", "image/png", ROOT, "not text")
    g.put("FORM0001", "Survey", "application/vnd.google-apps.form", ROOT)
    g.put("FILEB001", "b.txt", "text/plain", "SUB00001", "bravo platypus")
    g.put("SHEET001", "Budget", SHEET, "SUB00001", "gamma budget axolotl")
    g.put("SLIDE001", "Deck", SLIDES, "SUB00001", "delta slides pangolin")
    g.put("FILEC001", "c.csv", "text/csv", "DEEP0001", "k,v\nechidna,1\n")


async def _start(
    client: httpx.AsyncClient, a: Account, kb: str, cfg: dict[str, Any] | None = None
) -> httpx.Response:
    body = {"type": "gdrive", "name": "Drive", "config": cfg or {}}
    return await client.post(f"/v1/kbs/{kb}/sources/oauth/start", json=body, headers=bound(a))


def bound(a: Account, binding: str = BIND) -> dict[str, str]:
    """`a` in the browser whose binding cookie is `binding` (the header the BFF sends)."""
    return {**a.headers, "X-OAuth-Binding": binding}


async def _finish(
    client: httpx.AsyncClient, a: Account, binding: str = BIND, **body: str
) -> httpx.Response:
    """What the frontend finish page does with the provider's redirect query."""
    return await client.post("/v1/connectors/oauth/finish", json=body, headers=bound(a, binding))


async def _callback(client: httpx.AsyncClient, a: Account, **body: str) -> str:
    r = await _finish(client, a, **body)
    assert r.status_code == 200, r.text
    return FRONT + str(r.json()["redirect"])


async def _connect(
    client: httpx.AsyncClient, a: Account, g: FakeGoogle, cfg: dict[str, Any] | None = None
) -> tuple[str, str]:
    kb = await _kb(client, a)
    r = await _start(client, a, kb, cfg)
    assert r.status_code == 200, r.text
    q = parse_qs(urlsplit(r.json()["authorize_url"]).query)
    loc = await _callback(client, a, code=g.authorize(q), state=q["state"][0])
    m = re.fullmatch(rf"{FRONT}/kbs/{kb}\?tab=sources&connected=([0-9a-f-]+)", loc)
    assert m, loc
    await _hold_first_run(a, m[1])
    return kb, m[1]


async def _hold_first_run(a: Account, sid: str) -> None:
    """The callback queued the first sync (A7c). Tests drive syncs themselves, so that queued
    run is closed as if its message were dropped; run_sync then ignores it."""
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        held = await s.execute(text(
            "UPDATE sync_runs SET status = 'failed', error = 'held by test', finished_at = now() "
            "WHERE source_id = :i AND status = 'running'"), {"i": sid})  # fmt: skip
        await s.commit()
    assert held.rowcount == 1  # type: ignore[attr-defined]


async def _secret(a: Account, sid: str) -> dict[str, Any]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        enc = await s.scalar(text("SELECT secret_enc FROM sources WHERE id = :i"), {"i": sid})
    return dict(json.loads(decrypt(enc)))


# ---------------------------------------------------------------- feature flag


async def test_flag_off_hides_connector_and_refuses_start(client: httpx.AsyncClient) -> None:
    assert get_settings().connector_gdrive_enabled is False  # default
    a = await register(client)
    r = await client.get("/v1/connectors", headers=a.headers)
    assert "gdrive" not in {c["type"] for c in r.json()["items"]}
    r = await _start(client, a, await _kb(client, a))
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_connector"


async def test_flag_on_without_client_credentials_is_refused(
    client: httpx.AsyncClient, fake: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "google_oauth_client_secret", None)
    a = await register(client)
    r = await _start(client, a, await _kb(client, a))
    assert r.status_code == 422 and r.json()["error"]["code"] == "oauth_not_configured"


# ---------------------------------------------------------------- OAuth flow


async def test_start_returns_pkce_s256_url_and_stores_state(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    r = await client.get("/v1/connectors", headers=a.headers)
    meta = next(c for c in r.json()["items"] if c["type"] == "gdrive")
    assert meta["auth"] == "oauth2"
    r = await _start(client, a, kb, {"folder_ids": ["SUB00001"], "recursive": False})
    url = urlsplit(r.json()["authorize_url"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert (
        f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID and q["scope"] == SCOPE
    assert q["access_type"] == "offline" and len(q["state"]) >= 43
    assert q["redirect_uri"] == REDIRECT_URI
    pending = await oauth._redis_client().get(f"oauth:state:{q['state']}")
    stored = json.loads(pending)
    digest = hashlib.sha256(stored["verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == q["code_challenge"]
    assert stored["kb"] == kb and stored["org"] == str(a.org_id)
    assert stored["config"]["folder_ids"] == ["SUB00001"]
    assert "verifier" not in r.text  # the verifier never leaves the server


async def test_start_validates_config_and_role(client: httpx.AsyncClient, fake: FakeGoogle) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    r = await _start(client, a, kb, {"folder_ids": ["x' or name contains '"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_config"
    r = await _start(client, a, kb, {"max_files": 50_001})
    assert r.status_code == 422
    body = {"type": "website", "name": "w", "config": {}}
    r = await client.post(f"/v1/kbs/{kb}/sources/oauth/start", json=body, headers=a.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_oauth"


async def test_callback_stores_only_encrypted_tokens(
    client: httpx.AsyncClient, fake: FakeGoogle, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    a = await register(client)
    kb, sid = await _connect(client, a, fake)
    tokens = await _secret(a, sid)
    assert tokens["access_token"] in fake.access and tokens["refresh_token"] in fake.refresh_ok
    assert tokens["source_id"] == sid and tokens["org_id"] == str(a.org_id)
    assert tokens["scope"] == SCOPE and tokens["expires_at"] > time.time() + 3000
    listed = await client.get(f"/v1/kbs/{kb}/sources", headers=a.headers)
    src = listed.json()["items"][0]
    assert src["has_secret"] is True and src["config"]["folder_ids"] == []
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        row = (await s.execute(text("SELECT config::text, cursor::text, secret_enc FROM sources "
                                    "WHERE id = :i"), {"i": sid})).one()  # fmt: skip
        audit = await s.scalar(text("SELECT meta::text FROM audit_log WHERE target_id = :i"),
                               {"i": sid})  # fmt: skip
    for tok in (tokens["access_token"], tokens["refresh_token"]):  # AC11
        assert tok not in listed.text and tok not in caplog.text
        assert tok not in row[0] + row[1] + (audit or "") and tok.encode() not in row[2]
    # The client secret is scrubbed from logs like the other configured secrets.
    assert CLIENT_SECRET in get_settings().secret_values()


async def test_manual_secret_is_refused_for_oauth_connectors(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    forged = json.dumps({"access_token": "x", "source_id": str(uuid.uuid4()), "org_id": "x"})
    body = {"type": "gdrive", "name": "d", "config": {}, "secret": forged}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "oauth_only"
    _, sid = await _connect(client, a, fake)
    r = await client.patch(f"/v1/sources/{sid}", json={"secret": forged}, headers=a.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "oauth_only"


async def test_callback_rejects_unknown_reused_and_expired_state(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    a = await register(client)

    async def bad(**body: str) -> None:
        r = await _finish(client, a, **body)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_state", r.text

    await bad(code="4/0x", state="nope")
    await bad(code="4/0x", state="")
    kb = await _kb(client, a)
    q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    code = fake.authorize(q)
    assert "connected=" in await _callback(client, a, code=code, state=q["state"][0])
    # Replay of the same state (even with a fresh code) is refused: single use.
    await bad(code=fake.authorize(q), state=q["state"][0])
    q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    await oauth._redis_client().pexpire(f"oauth:state:{q['state'][0]}", 1)
    await asyncio.sleep(0.05)
    await bad(code=fake.authorize(q), state=q["state"][0])
    r = await client.get(f"/v1/kbs/{kb}/sources", headers=a.headers)
    assert len(r.json()["items"]) == 1


async def test_injected_code_from_another_flow_fails_pkce(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    a, attacker = await register(client), await register(client)
    kb = await _kb(client, a)
    victim_q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    akb = await _kb(client, attacker)
    att_q = parse_qs(urlsplit((await _start(client, attacker, akb)).json()["authorize_url"]).query)
    loc = await _callback(client, a, code=fake.authorize(att_q), state=victim_q["state"][0])
    assert loc == f"{FRONT}/kbs/{kb}?tab=sources&oauth_error=exchange_failed"
    assert (await client.get(f"/v1/kbs/{kb}/sources", headers=a.headers)).json()["items"] == []


async def test_callback_redirect_cannot_be_steered(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    r = await _finish(client, a, **{
        "error": "access_denied", "state": q["state"][0], "redirect_uri": "https://evil.example",
        "next": "//evil.example", "return_to": "https://evil.example/x",
    })  # fmt: skip
    assert r.json() == {"redirect": f"/kbs/{kb}?tab=sources&oauth_error=denied"}


# ---------------------------------------------------------------- S2: browser/user binding


async def _victim_finishes(
    client: httpx.AsyncClient, fake: FakeGoogle, finisher: Account, binding: str, starter: Account
) -> httpx.Response:
    kb = await _kb(client, starter)
    q = parse_qs(urlsplit((await _start(client, starter, kb)).json()["authorize_url"]).query)
    return await _finish(client, finisher, binding, code=fake.authorize(q), state=q["state"][0])


async def _no_sources(a: Account) -> None:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        assert await s.scalar(text("SELECT count(*) FROM sources")) == 0


async def test_s2_attacker_started_flow_finished_by_victim_is_refused(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    """S2: the attacker starts a flow and sends the authorize link; the victim's browser comes
    back signed in as the victim. Nothing is exchanged, nothing lands in the attacker's org."""
    attacker, victim = await register(client), await register(client)
    r = await _victim_finishes(client, fake, victim, "victim-browser-" + "v" * 28, attacker)
    assert r.status_code == 403 and r.json()["error"]["code"] == "oauth_not_yours", r.text
    token_calls = [x for x in fake.requests if x.url.path == "/token"]
    assert token_calls == [] and fake.access == {}  # no grant was ever created
    await _no_sources(attacker)
    await _no_sources(victim)


async def test_s2_same_user_other_browser_or_missing_binding_is_refused(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    a = await register(client)
    for binding in ("other-browser-" + "o" * 30, ""):
        r = await _victim_finishes(client, fake, a, binding, a)
        assert r.status_code == 403 and r.json()["error"]["code"] == "oauth_not_yours", r.text
    # The refused state is spent: retrying it from the right browser is a replay.
    kb = await _kb(client, a)
    q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    body = {"code": fake.authorize(q), "state": q["state"][0]}
    assert (await _finish(client, a, "x" * 40, **body)).status_code == 403
    assert (await _finish(client, a, **body)).json()["error"]["code"] == "invalid_state"
    assert fake.access == {}
    await _no_sources(a)


async def test_s2_start_needs_a_browser_binding(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    body = {"type": "gdrive", "name": "Drive", "config": {}}
    for headers in (a.headers, bound(a, "short"), bound(a, "x" * 129)):
        r = await client.post(f"/v1/kbs/{kb}/sources/oauth/start", json=body, headers=headers)
        assert r.status_code == 422 and r.json()["error"]["code"] == "oauth_binding_required"


async def test_s2_old_public_callback_is_gone(client: httpx.AsyncClient, fake: FakeGoogle) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    params = {"code": fake.authorize(q), "state": q["state"][0]}
    r = await client.get("/v1/connectors/oauth/callback", params=params)
    assert r.status_code in (404, 405) and fake.access == {}


async def test_scope_not_granted_is_refused_and_revoked(
    client: httpx.AsyncClient, fake: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = fake._issue
    monkeypatch.setattr(fake, "_issue", lambda refresh: {**issue(refresh), "scope": "openid"})
    a = await register(client)
    kb = await _kb(client, a)
    q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    loc = await _callback(client, a, code=fake.authorize(q), state=q["state"][0])
    assert loc.endswith("oauth_error=scope_denied") and len(fake.revoked) == 1


async def test_ac1_cross_org_cannot_attach_to_another_orgs_kb(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    a, b = await register(client), await register(client)
    kb_a = await _kb(client, a)
    r = await _start(client, b, kb_a)
    assert r.status_code == 404  # B cannot even begin a flow for A's KB
    # A flow started by A whose user is no longer an editor when the finish call lands.
    q = parse_qs(urlsplit((await _start(client, a, kb_a)).json()["authorize_url"]).query)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(text("UPDATE memberships SET role = 'viewer' WHERE user_id = :u"),
                        {"u": a.user_id})  # fmt: skip
        await s.commit()
    r = await _finish(client, a, code=fake.authorize(q), state=q["state"][0])
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert fake.access == {} and fake.revoked == []  # refused before any grant existed
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        assert await s.scalar(text("SELECT count(*) FROM sources")) == 0


# ---------------------------------------------------------------- discovery + sync


async def test_full_sync_recursive_paginated_with_exports(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    _tree(fake)
    a = await register(client)
    kb, sid = await _connect(client, a, fake)
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded", run
    assert run["stats"]["added"] == 7 and run["stats"]["failed"] == 0
    docs = await _docs(a, sid)
    names = {d.name for d in docs.values()}
    # The Google Form (not exportable) is skipped at discovery. photo.png is an image (B8): without
    # OCR it is listed as failed with a readable reason, never silently dropped.
    assert names == {"a.md", "Design doc.docx", "b.txt", "Budget.xlsx", "Deck.txt", "c.csv",
                     "photo.png"}  # fmt: skip
    assert (docs["PNG00001"].status, docs["PNG00001"].error) == (
        "failed",
        "OCR is not available on this server, so text in images cannot be extracted.",
    )
    assert all(d.status == "ready" for k, d in docs.items() if k != "PNG00001"), [
        (d.name, d.error) for d in docs.values()
    ]
    exports = {r.url.path.split("/")[-2]: r.url.params["mimeType"]
               for r in fake.requests if r.url.path.endswith("/export")}  # fmt: skip
    assert exports == {"GDOC0001": gdrive.DOCX, "SHEET001": gdrive.XLSX, "SLIDE001": "text/plain"}
    lists = fake.drive_calls("files")
    assert any("pageToken" in r.url.params for r in lists)  # root has 5 children, pages of 2
    assert all(r.url.params["corpora"] == "user" for r in lists)
    assert all("includeItemsFromAllDrives" not in r.url.params for r in lists)
    assert docs["GDOC0001"].meta["drive_file_id"] == "GDOC0001"
    assert docs["GDOC0001"].meta["acl_enforced"] is False
    assert any("zebra" in t for t in await _search(client, a, kb, "zebra quokka"))
    run = await _sync(client, a, sid, full=True)
    assert run["stats"]["unchanged"] == 7  # md5 / version marker: nothing downloaded again
    assert len([r for r in fake.requests if r.url.params.get("alt") == "media"]) == 4


async def test_non_recursive_and_shared_drive_flag(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    _tree(fake)
    fake.put("SHR00001", "team", FOLDER, "SUB00001", driveId="0ADRIVE")
    fake.put("SHRF0001", "team.md", "text/markdown", "SUB00001", "# t\n\nwombat", driveId="0ADRIVE")
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["SUB00001"], "recursive": False})
    await _sync(client, a, sid)
    assert set(await _docs(a, sid)) == {"FILEB001", "SHEET001", "SLIDE001"}
    _, sid2 = await _connect(client, a, fake, {"folder_ids": ["SUB00001"],
                                               "include_shared_drives": True})  # fmt: skip
    fake.requests.clear()
    await _sync(client, a, sid2)
    assert set(await _docs(a, sid2)) == {"FILEB001", "SHEET001", "SLIDE001", "SHRF0001", "FILEC001"}
    lists = fake.drive_calls("files")
    assert all(r.url.params["corpora"] == "allDrives" for r in lists)
    assert all(r.url.params["includeItemsFromAllDrives"] == "true" for r in lists)


async def test_incremental_changes_cursor_then_full_run_deletes(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    _tree(fake)
    fake.put("OUT00001", "elsewhere", FOLDER, "NOTSHARED")
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["SUB00001"]})
    assert (await _sync(client, a, sid))["stats"]["added"] == 4
    fake.put("FILEB001", "b.txt", "text/plain", "SUB00001", "bravo platypus v2")  # changed
    fake.put("NEW00001", "n.md", "text/markdown", "DEEP0001", "# n\n\nnew quetzal")  # added
    fake.put("NEWDIR01", "later", FOLDER, "SUB00001")  # folder created after the full run
    fake.put("NEW00002", "l.md", "text/markdown", "NEWDIR01", "# l\n\nlater ocelot")
    fake.put("OUTF0001", "o.md", "text/markdown", "OUT00001", "# o\n\nout of scope")
    fake.remove("SLIDE001")
    fake.trash("FILEC001")
    fake.requests.clear()
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded"
    assert {k: run["stats"][k] for k in ("discovered", "added", "updated", "deleted")} == {
        "discovered": 3, "added": 2, "updated": 1, "deleted": 0}  # fmt: skip
    # No re-list: the only files.list is the walk of the folder that entered the scope (A7b).
    assert fake.drive_calls("changes")
    assert [r.url.params["q"] for r in fake.drive_calls("files")] == [
        "'NEWDIR01' in parents and trashed = false"
    ]
    docs = await _docs(a, sid)
    assert docs["SLIDE001"].status == "ready" and "OUTF0001" not in docs  # no delete yet
    run = await _sync(client, a, sid, full=True)
    assert run["stats"]["deleted"] == 2
    docs = await _docs(a, sid)
    assert docs["SLIDE001"].status == "deleted" and docs["FILEC001"].status == "deleted"


# ---------------------------------------------------------------- tokens


async def test_refresh_near_expiry_persists_new_token(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    _tree(fake)
    fake.expires_in = 30  # under the 60 s margin: every use refreshes first
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["DEEP0001"]})
    before = await _secret(a, sid)
    fake.expires_in = 3600
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded" and fake.refreshes == 1
    after = await _secret(a, sid)
    assert after["access_token"] != before["access_token"] and after["access_token"] in fake.access
    assert after["refresh_token"] == before["refresh_token"]  # Google did not rotate it
    assert after["expires_at"] > time.time() + 3000


async def test_401_forces_one_refresh(client: httpx.AsyncClient, fake: FakeGoogle) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["DEEP0001"]})
    fake.access.clear()  # Google invalidated the access token early
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded" and fake.refreshes == 1


async def test_invalid_grant_marks_source_error_with_reconnect_message(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["DEEP0001"]})
    fake.access.clear()
    fake.refresh_ok.clear()  # user revoked the app in their Google account
    run = await _sync(client, a, sid)
    assert run["status"] == "failed"
    assert run["error"] == (
        "Sync failed: Google Drive access was revoked or has expired. Reconnect Google Drive."
    )
    r = await client.get(f"/v1/sources/{sid}", headers=a.headers)
    assert r.json()["status"] == "error"
    r = await client.post(f"/v1/sources/{sid}/test", headers=a.headers)
    assert r.json()["ok"] is False and "Reconnect Google Drive" in r.json()["message"]


async def test_test_endpoint_and_delete_revokes(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["SUB00001"]})
    r = await client.post(f"/v1/sources/{sid}/test", headers=a.headers)
    msg = "Connected as owner@example.com; 1 folder(s) readable."
    assert r.json() == {"ok": True, "message": msg, "schema": None}
    r = await client.patch(f"/v1/sources/{sid}", json={"config": {"folder_ids": ["FILEB001"]}},
                           headers=a.headers)  # fmt: skip
    assert r.status_code == 422 and "not a folder" in r.json()["error"]["message"]
    refresh = (await _secret(a, sid))["refresh_token"]
    assert (await client.delete(f"/v1/sources/{sid}", headers=a.headers)).status_code == 204
    assert fake.revoked == [refresh]
    revoke = next(r for r in fake.requests if r.url.path == "/revoke")
    assert refresh not in str(revoke.url)  # token in the body, never the URL


# ---------------------------------------------------------------- A7b: folders entering scope


async def test_incremental_walks_folders_moved_in_or_listed_after_their_files(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    _tree(fake)
    fake.put("OUT00001", "elsewhere", FOLDER, "NOTSHARED")
    fake.put("OUTF0001", "o.md", "text/markdown", "OUT00001", "# o\n\nmoved marmot")
    fake.put("OUTD0001", "inner", FOLDER, "OUT00001")
    fake.put("OUTF0002", "i.md", "text/markdown", "OUTD0001", "# i\n\ninner ibex")
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["SUB00001"]})
    assert (await _sync(client, a, sid))["stats"]["added"] == 4
    fake.put("OUT00001", "elsewhere", FOLDER, "SUB00001")  # moved in: only the folder changes
    fake.put("NEWDIR01", "later", FOLDER, "SUB00001")
    fake.put("NEW00002", "l.md", "text/markdown", "NEWDIR01", "# l\n\nlater ocelot")
    fake.put("NEWDIR01", "renamed", FOLDER, "SUB00001")  # its latest change now follows the file
    first = next(i for i, c in enumerate(fake.changes) if c["fileId"] == "NEWDIR01")
    del fake.changes[first]  # changes.list shows each item once, at its latest change
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded" and run["stats"]["added"] == 3, run
    assert {"OUTF0001", "OUTF0002", "NEW00002"} <= set(await _docs(a, sid))
    run = await _sync(client, a, sid)  # the walked folders stay in scope for later runs
    assert run["stats"]["added"] == 0


async def test_incremental_non_recursive_ignores_new_subfolders(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["SUB00001"], "recursive": False})
    assert (await _sync(client, a, sid))["stats"]["added"] == 3
    fake.put("NEWDIR01", "later", FOLDER, "SUB00001")
    fake.put("NEW00002", "l.md", "text/markdown", "NEWDIR01", "# l\n\nlater ocelot")
    fake.put("NEW00003", "top.md", "text/markdown", "SUB00001", "# t\n\ntop tapir")
    run = await _sync(client, a, sid)
    assert run["stats"]["added"] == 1
    assert "NEW00002" not in await _docs(a, sid)


# ---------------------------------------------------------------- A7c: source lifecycle


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """sync_source messages the API queued (the stub broker never runs them)."""
    out: list[tuple[str, ...]] = []
    monkeypatch.setattr(sync_source, "send", lambda *args: out.append(args))
    return out


async def _runs(client: httpx.AsyncClient, a: Account, sid: str) -> list[dict[str, Any]]:
    r = await client.get(f"/v1/sources/{sid}/runs", headers=a.headers)
    return list(r.json()["items"])


async def _reconnect_q(client: httpx.AsyncClient, a: Account, sid: str) -> dict[str, list[str]]:
    r = await client.post(f"/v1/sources/{sid}/oauth/reconnect", headers=bound(a))
    assert r.status_code == 200, r.text
    url = urlsplit(r.json()["authorize_url"])
    assert (
        f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    )
    return parse_qs(url.query)


async def test_callback_starts_first_sync(
    client: httpx.AsyncClient, fake: FakeGoogle, sent: list[tuple[str, ...]]
) -> None:
    _tree(fake)
    a = await register(client)
    kb = await _kb(client, a)
    q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    loc = await _callback(client, a, code=fake.authorize(q), state=q["state"][0])
    sid = loc.rsplit("connected=", 1)[1]
    runs = await _runs(client, a, sid)
    assert [r["status"] for r in runs] == ["running"]
    assert sent == [(runs[0]["id"], str(a.org_id))]
    await sync_mod.run_sync(uuid.UUID(runs[0]["id"]), a.org_id)
    run = (await _runs(client, a, sid))[0]
    assert run["status"] == "succeeded" and run["stats"]["added"] == 7, run  # 7th: photo.png


async def test_run_errors_land_on_the_source_and_clear_on_success(
    client: httpx.AsyncClient, fake: FakeGoogle
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["DEEP0001"]})
    blob = fake.blobs.pop("FILEC001")  # the download breaks: partial run
    assert (await _sync(client, a, sid))["status"] == "partial"
    src = (await client.get(f"/v1/sources/{sid}", headers=a.headers)).json()
    assert src["last_error"] == "1 item(s) failed; nothing was deleted."
    assert src["needs_reconnect"] is False and src["status"] == "idle"
    fake.blobs["FILEC001"] = blob
    assert (await _sync(client, a, sid))["status"] == "succeeded"
    src = (await client.get(f"/v1/sources/{sid}", headers=a.headers)).json()
    assert src["last_error"] is None and src["needs_reconnect"] is False


async def test_revoked_grant_flags_reconnect_and_reconnect_rebinds_in_place(
    client: httpx.AsyncClient, fake: FakeGoogle, sent: list[tuple[str, ...]]
) -> None:
    _tree(fake)
    a = await register(client)
    kb, sid = await _connect(client, a, fake, {"folder_ids": ["DEEP0001"]})
    assert (await _sync(client, a, sid))["stats"]["added"] == 1
    fake.access.clear()
    fake.refresh_ok.clear()  # revoked at Google
    await _sync(client, a, sid)
    src = (await client.get(f"/v1/sources/{sid}", headers=a.headers)).json()
    assert src["status"] == "error" and src["needs_reconnect"] is True
    assert src["last_error"] == (
        "Sync failed: Google Drive access was revoked or has expired. Reconnect Google Drive."
    )
    q = await _reconnect_q(client, a, sid)
    assert q["code_challenge_method"] == ["S256"] and len(q["state"][0]) >= 43
    sent.clear()
    loc = await _callback(client, a, code=fake.authorize(q), state=q["state"][0])
    assert loc == f"{FRONT}/kbs/{kb}?tab=sources&reconnected={sid}"
    items = (await client.get(f"/v1/kbs/{kb}/sources", headers=a.headers)).json()["items"]
    assert [s["id"] for s in items] == [sid]  # same source, no duplicate
    src = items[0]
    assert (src["status"], src["needs_reconnect"], src["last_error"]) == ("idle", False, None)
    tokens = await _secret(a, sid)
    assert tokens["refresh_token"] in fake.refresh_ok and tokens["source_id"] == sid
    assert fake.revoked == []  # revoking the old Google grant would kill the new one too
    assert set(await _docs(a, sid)) == {"FILEC001"}  # documents kept
    runs = await _runs(client, a, sid)  # it was in error: a sync starts by itself
    assert runs[0]["status"] == "running" and sent == [(runs[0]["id"], str(a.org_id))]
    await sync_mod.run_sync(uuid.UUID(runs[0]["id"]), a.org_id)
    assert (await _runs(client, a, sid))[0]["status"] == "succeeded"
    # A healthy source reconnected again waits for its schedule (no extra run).
    sent.clear()
    q = await _reconnect_q(client, a, sid)
    assert "reconnected=" in await _callback(client, a, code=fake.authorize(q), state=q["state"][0])
    assert sent == []


async def test_run_on_the_dead_grant_failing_after_a_reconnect_does_not_reflag_it(
    client: httpx.AsyncClient,
    fake: FakeGoogle,
    sent: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H2 race: the run read the old tokens, the user reconnects mid-run, then the run fails on
    the dead grant. The source keeps its new grant and is not flagged (or put in error) again."""
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["DEEP0001"]})
    assert (await _sync(client, a, sid))["status"] == "succeeded"
    fake.access.clear()
    fake.refresh_ok.clear()  # revoked at Google
    discover = REGISTRY["gdrive"].discover

    async def reconnect_mid_run(config: Any, secret: str | None, cursor: Any) -> Any:
        q = await _reconnect_q(client, a, sid)
        loc = await _callback(client, a, code=fake.authorize(q), state=q["state"][0])
        assert "reconnected=" in loc
        async for item in discover(config, secret, cursor):  # still the run's old tokens
            yield item

    monkeypatch.setattr(REGISTRY["gdrive"], "discover", reconnect_mid_run)
    run = await _sync(client, a, sid)
    assert run["status"] == "failed" and "Reconnect Google Drive" in run["error"]
    src = (await client.get(f"/v1/sources/{sid}", headers=a.headers)).json()
    assert (src["status"], src["needs_reconnect"], src["last_error"]) == ("idle", False, None)
    assert (await _secret(a, sid))["refresh_token"] in fake.refresh_ok  # the new grant is kept
    monkeypatch.setattr(REGISTRY["gdrive"], "discover", discover)
    assert (await _sync(client, a, sid))["status"] == "succeeded"


async def test_reconnect_refusals(
    client: httpx.AsyncClient, fake: FakeGoogle, sent: list[tuple[str, ...]]
) -> None:
    a, b = await register(client), await register(client)
    kb, sid = await _connect(client, a, fake)
    sent.clear()  # the first sync of the new source
    path = f"/v1/sources/{sid}/oauth/reconnect"
    assert (await client.post(path, headers=b.headers)).status_code == 404  # other org
    web = uuid.uuid4()
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(text(
            "INSERT INTO sources (id, org_id, kb_id, type, name, config, cursor, status) "
            "VALUES (:i, :o, :k, 'website', 'w', '{}', '{}', 'idle')"),
            {"i": web, "o": a.org_id, "k": kb})  # fmt: skip
        await s.commit()
    r = await client.post(f"/v1/sources/{web}/oauth/reconnect", headers=a.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_oauth"
    before = await _secret(a, sid)
    # The state names a source that is gone, or one in another KB: refused, new grant revoked.
    for forged in ({"source": str(uuid.uuid4())}, {"kb": str(uuid.uuid4())}):
        q = await _reconnect_q(client, a, sid)
        key = f"oauth:state:{q['state'][0]}"
        stored = json.loads(await oauth._redis_client().get(key))
        assert stored["source"] == sid and stored["type"] == "gdrive"
        await oauth._redis_client().set(key, json.dumps({**stored, **forged}))
        loc = await _callback(client, a, code=fake.authorize(q), state=q["state"][0])
        assert loc.endswith("oauth_error=source_gone"), loc
    assert len(fake.revoked) == 2
    # Demoted to viewer before the callback lands.
    q = await _reconnect_q(client, a, sid)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(text("UPDATE memberships SET role = 'viewer' WHERE user_id = :u"),
                        {"u": a.user_id})  # fmt: skip
        await s.commit()
    r = await _finish(client, a, code=fake.authorize(q), state=q["state"][0])
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert len(fake.revoked) == 2
    assert await _secret(a, sid) == before and sent == []

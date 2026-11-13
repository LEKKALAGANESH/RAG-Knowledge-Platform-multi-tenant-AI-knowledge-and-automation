"""A7b: Dropbox connector against a contract fake of Dropbox OAuth 2 + API v2.

FakeDropbox answers like the documented endpoints (www.dropbox.com/oauth2/authorize,
api.dropboxapi.com/oauth2/token, /2/auth/token/revoke, /2/users/get_current_account,
/2/files/get_metadata, /2/files/list_folder(+/continue, `reset`), content.dropboxapi.com
/2/files/download with the Dropbox-API-Arg header) and enforces PKCE S256, client credentials,
bearer expiry, refresh-token revocation and an ASCII-only argument header. Live Dropbox is
never called.
"""

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.connectors import dropbox, oauth
from app.connectors.dropbox import DropboxConnector, arg_header
from app.connectors.registry import REGISTRY
from app.core.config import get_settings
from app.core.db import SessionLocal, use_tenant
from app.ingestion.config import get_ingestion_settings
from tests.conftest import Account, register
from tests.test_connectors import _docs, _kb, _search, _sync, env  # noqa: F401
from tests.test_connectors_gdrive import _callback, _hold_first_run, _secret, bound

APP_KEY, APP_SECRET = "dbxappkey123", "dbx-app-secret-test"
FRONT = "http://localhost:3000"
SCOPE = "account_info.read files.content.read files.metadata.read"


def content_hash(data: bytes) -> str:
    """Dropbox's documented algorithm: SHA-256 over the 4 MiB blocks' SHA-256 digests."""
    blocks = [data[i : i + 4 * 1024 * 1024] for i in range(0, len(data), 4 * 1024 * 1024)]
    return hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in blocks)).hexdigest()


class FakeDropbox:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}  # path_lower -> metadata
        self.blobs: dict[str, bytes] = {}  # id -> bytes
        self.log: list[dict[str, Any]] = []  # every change, as list_folder/continue sees it
        self.snapshots: dict[str, list[dict[str, Any]]] = {}
        self.reset: set[str] = set()
        self.codes: dict[str, str] = {}
        self.access: dict[str, float] = {}
        self.refresh_ok: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.revoked: list[str] = []
        self.refreshes, self.expires_in, self.page_size, self.scope = 0, 14400, 2, SCOPE

    # ---- helpers
    def put(self, path: str, body: str | None = None, size: int | None = None) -> None:
        low, name = path.lower(), path.rsplit("/", 1)[-1]
        old = self.entries.get(low)
        e: dict[str, Any] = {"name": name, "path_lower": low, "path_display": path,
                             "id": old["id"] if old else "id:" + uuid.uuid4().hex[:12]}  # fmt: skip
        if body is None:
            e[".tag"] = "folder"
        else:
            data = body.encode()
            e.update({".tag": "file", "size": len(data) if size is None else size,
                      "rev": uuid.uuid4().hex[:9], "content_hash": content_hash(data),
                      "server_modified": "2026-11-12T00:00:00Z"})  # fmt: skip
            self.blobs[e["id"]] = data
        self.entries[low] = e
        self.log.append(dict(e))

    def delete(self, path: str) -> None:
        e = self.entries.pop(path.lower())
        self.log.append({".tag": "deleted", "name": e["name"], "path_lower": e["path_lower"],
                         "path_display": e["path_display"]})  # fmt: skip

    def id_of(self, path: str) -> str:
        return str(self.entries[path.lower()]["id"])

    def authorize(self, query: dict[str, list[str]]) -> str:
        assert query["code_challenge_method"] == ["S256"]
        code = "dbxcode" + uuid.uuid4().hex
        self.codes[code] = query["code_challenge"][0]
        return code

    @staticmethod
    def _under(e: dict[str, Any], path: str, recursive: bool) -> bool:
        low = path.lower()
        if not e["path_lower"].startswith(low + "/"):
            return False
        return recursive or "/" not in e["path_lower"][len(low) + 1 :]

    # ---- endpoints
    def _issue(self, refresh: bool) -> dict[str, Any]:
        tok = "sl." + uuid.uuid4().hex
        self.access[tok] = time.time() + self.expires_in
        out: dict[str, Any] = {"access_token": tok, "token_type": "bearer", "scope": self.scope,
                               "expires_in": self.expires_in, "uid": "1"}  # fmt: skip
        if refresh:
            out["refresh_token"] = rt = "rt" + uuid.uuid4().hex
            self.refresh_ok.add(rt)
        return out

    def _token(self, req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        bad = httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad"})
        if (form.get("client_id"), form.get("client_secret")) != (APP_KEY, APP_SECRET):
            return httpx.Response(400, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            challenge = self.codes.pop(form.get("code", ""), None)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if challenge != base64.urlsafe_b64encode(digest).decode().rstrip("="):
                return bad
            return httpx.Response(200, json=self._issue(refresh=True))
        if form.get("refresh_token") not in self.refresh_ok:
            return bad
        self.refreshes += 1
        return httpx.Response(200, json=self._issue(refresh=False))  # no new refresh token

    def _paged(self, rows: list[dict[str, Any]], key: str, pos: int) -> httpx.Response:
        page = rows[pos : pos + self.page_size]
        more = pos + self.page_size < len(rows)
        cur = f"s:{key}:{pos + self.page_size}" if more else f"c:{key}:{len(self.log)}"
        return httpx.Response(200, json={"entries": page, "cursor": cur, "has_more": more})

    def _list(self, args: dict[str, Any]) -> httpx.Response:
        assert args["include_deleted"] is False and args["limit"] <= 2000
        path, rec = str(args["path"]), bool(args["recursive"])
        if path and path.lower() not in self.entries:
            return _409("path/not_found/..", "path")
        key = f"{len(self.snapshots)}|{path}|{int(rec)}"
        self.snapshots[key] = [e for e in self.entries.values() if self._under(e, path, rec)]
        return self._paged(self.snapshots[key], key, 0)

    def _continue(self, cursor: str) -> httpx.Response:
        kind, key, pos = cursor.split(":", 2)
        if cursor in self.reset:
            return _409("reset/..", "reset")
        if kind == "s":
            return self._paged(self.snapshots[key], key, int(pos))
        _, path, rec = key.split("@")[0].split("|")
        rows = [e for e in self.log[int(pos) :] if self._under(e, path, rec == "1")]
        self.snapshots[key + "@" + pos] = rows
        return self._paged(rows, key + "@" + pos, 0) if rows else httpx.Response(
            200, json={"entries": [], "cursor": cursor, "has_more": False})  # fmt: skip

    def _api(self, route: str, req: httpx.Request) -> httpx.Response:
        if route == "auth/token/revoke":
            assert req.content == b""
            self.revoked.append(req.headers["Authorization"].removeprefix("Bearer "))
            return httpx.Response(200, json=None)
        args = json.loads(req.content)
        if route == "users/get_current_account":
            assert args is None
            return httpx.Response(200, json={"email": "owner@example.com"})
        if route == "files/get_metadata":
            e = self.entries.get(args["path"].lower())
            return httpx.Response(200, json=e) if e else _409("path/not_found/..", "path")
        if route == "files/list_folder":
            return self._list(args)
        assert route == "files/list_folder/continue", route
        return self._continue(args["cursor"])

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        host, path = req.url.host, req.url.path
        if path == "/oauth2/token":
            assert host == "api.dropboxapi.com"
            return self._token(req)
        assert req.method == "POST" and path.startswith("/2/")
        if self.access.get(req.headers.get("Authorization", "").removeprefix("Bearer "), 0) < (
            time.time()
        ):
            return _401()
        if host == "content.dropboxapi.com":
            raw = req.headers["Dropbox-API-Arg"]
            assert raw.isascii() and "\x7f" not in raw  # must be HTTP-header safe
            fid = json.loads(raw)["path"]
            return httpx.Response(200, stream=httpx.ByteStream(self.blobs[fid]))
        assert host == "api.dropboxapi.com"
        return self._api(path.removeprefix("/2/"), req)


def _401() -> httpx.Response:
    err = {".tag": "expired_access_token"}
    return httpx.Response(401, json={"error_summary": "expired_access_token/", "error": err})


def _409(summary: str, tag: str) -> httpx.Response:
    return httpx.Response(409, json={"error_summary": summary, "error": {".tag": tag}})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDropbox]:
    g = FakeDropbox()
    s = get_settings()
    monkeypatch.setattr(s, "connector_dropbox_enabled", True)
    monkeypatch.setattr(s, "dropbox_oauth_client_id", APP_KEY)
    monkeypatch.setattr(s, "dropbox_oauth_client_secret", SecretStr(APP_SECRET))
    monkeypatch.setattr(s, "oauth_redirect_base", FRONT)
    monkeypatch.setitem(REGISTRY, "dropbox", DropboxConnector())
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(g.handler))
    monkeypatch.setattr(dropbox, "BACKOFF_S", 0)
    yield g


def _tree(g: FakeDropbox) -> None:
    g.put("/Team")
    g.put("/Team/Deep")
    g.put("/a.md", "# Alpha\n\nalpha zebra quokka\n")
    g.put("/photo.png", "png")
    g.put("/Team/b.txt", "bravo platypus")
    g.put("/Team/Ünïcødé naïve.txt", "unicode narwhal")
    g.put("/Team/Deep/c.csv", "k,v\nechidna,1\n")


async def _start(
    client: httpx.AsyncClient, a: Account, kb: str, cfg: dict[str, Any] | None = None
) -> httpx.Response:
    body = {"type": "dropbox", "name": "Dropbox", "config": cfg or {}}
    return await client.post(f"/v1/kbs/{kb}/sources/oauth/start", json=body, headers=bound(a))


async def _connect(
    client: httpx.AsyncClient, a: Account, g: FakeDropbox, cfg: dict[str, Any] | None = None
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


# ---------------------------------------------------------------- flag + OAuth


async def test_flag_off_hides_connector(client: httpx.AsyncClient) -> None:
    assert get_settings().connector_dropbox_enabled is False
    a = await register(client)
    r = await client.get("/v1/connectors", headers=a.headers)
    assert "dropbox" not in {c["type"] for c in r.json()["items"]}
    r = await _start(client, a, await _kb(client, a))
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_connector"


async def test_start_url_pkce_offline_and_scopes(
    client: httpx.AsyncClient, fake: FakeDropbox
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    url = urlsplit((await _start(client, a, kb, {"paths": ["/Team"]})).json()["authorize_url"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://www.dropbox.com/oauth2/authorize"
    assert q["token_access_type"] == "offline" and q["response_type"] == "code"
    assert q["scope"] == "files.metadata.read files.content.read account_info.read"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["client_id"] == APP_KEY and len(q["state"]) >= 43
    stored = json.loads(await oauth._redis_client().get(f"oauth:state:{q['state']}"))
    assert stored["config"]["paths"] == ["/Team"]
    for bad in ({"paths": ["Team"]}, {"paths": ["/a/../b"]}, {"paths": []}):
        r = await _start(client, a, kb, bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_config", bad


async def test_callback_tokens_encrypted_never_exposed(
    client: httpx.AsyncClient, fake: FakeDropbox, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    a = await register(client)
    kb, sid = await _connect(client, a, fake)
    tokens = await _secret(a, sid)
    assert tokens["access_token"] in fake.access and tokens["refresh_token"] in fake.refresh_ok
    assert tokens["source_id"] == sid
    listed = await client.get(f"/v1/kbs/{kb}/sources", headers=a.headers)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        row = (await s.execute(text("SELECT config::text, cursor::text, secret_enc FROM sources "
                                    "WHERE id = :i"), {"i": sid})).one()  # fmt: skip
        audit = await s.scalar(text("SELECT meta::text FROM audit_log WHERE target_id = :i"),
                               {"i": sid})  # fmt: skip
    for tok in (tokens["access_token"], tokens["refresh_token"]):  # AC11
        assert tok not in listed.text and tok not in caplog.text
        assert tok not in row[0] + row[1] + (audit or "") and tok.encode() not in row[2]
    assert APP_SECRET in get_settings().secret_values()


async def test_scope_not_granted_is_refused_and_revoked_by_bearer(
    client: httpx.AsyncClient, fake: FakeDropbox
) -> None:
    fake.scope = "account_info.read files.metadata.read"
    a = await register(client)
    kb = await _kb(client, a)
    q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    loc = await _callback(client, a, code=fake.authorize(q), state=q["state"][0])
    assert loc.endswith("oauth_error=scope_denied") and len(fake.revoked) == 1
    assert fake.revoked[0] in fake.access  # the issued access token, sent as bearer


async def test_ac1_cross_org_cannot_start_for_another_orgs_kb(
    client: httpx.AsyncClient, fake: FakeDropbox
) -> None:
    a, b = await register(client), await register(client)
    assert (await _start(client, b, await _kb(client, a))).status_code == 404


def test_arg_header_is_ascii_escaped() -> None:
    raw = arg_header({"path": "/Ünï/日本" + chr(0x7F) + ".txt"})
    assert raw.isascii() and "\\u00dc" in raw and "\\u65e5" in raw and "\\u007f" in raw
    assert json.loads(raw)["path"] == "/Ünï/日本\x7f.txt"


# ---------------------------------------------------------------- discovery + sync


async def test_full_sync_recursive_paged_with_unicode_names(
    client: httpx.AsyncClient, fake: FakeDropbox
) -> None:
    _tree(fake)
    a = await register(client)
    kb, sid = await _connect(client, a, fake)
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded", run
    docs = await _docs(a, sid)
    assert {d.name for d in docs.values()} == {"a.md", "b.txt", "Ünïcødé naïve.txt", "c.csv"}
    assert all(d.status == "ready" for d in docs.values())
    uni = docs[fake.id_of("/Team/Ünïcødé naïve.txt")]
    assert uni.content_hash == "dbx:" + content_hash(b"unicode narwhal")
    assert uni.meta["path"] == "/Team/Ünïcødé naïve.txt" and uni.meta["acl_enforced"] is False
    calls = [r.url.path for r in fake.requests]
    assert "/2/files/list_folder/continue" in calls  # 7 entries, pages of 2
    assert any("zebra" in t for t in await _search(client, a, kb, "zebra quokka"))
    run = await _sync(client, a, sid, full=True)
    assert run["stats"]["unchanged"] == 4  # content_hash marker: nothing downloaded again
    assert len([r for r in fake.requests if r.url.path == "/2/files/download"]) == 4


async def test_paths_and_non_recursive(client: httpx.AsyncClient, fake: FakeDropbox) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"paths": ["/Team/"], "recursive": False})
    await _sync(client, a, sid)
    assert {d.name for d in (await _docs(a, sid)).values()} == {"b.txt", "Ünïcødé naïve.txt"}
    r = await client.patch(f"/v1/sources/{sid}", json={"config": {"paths": ["/a.md"]}},
                           headers=a.headers)  # fmt: skip
    assert r.status_code == 422 and "not a folder" in r.json()["error"]["message"]
    r = await client.patch(f"/v1/sources/{sid}", json={"config": {"paths": ["/Nope"]}},
                           headers=a.headers)  # fmt: skip
    assert r.status_code == 422 and "not found" in r.json()["error"]["message"]


async def test_incremental_cursor_then_full_run_deletes(
    client: httpx.AsyncClient, fake: FakeDropbox
) -> None:
    _tree(fake)
    fake.put("/Other")
    fake.put("/Other/o.md", "out of scope")
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"paths": ["/Team"]})
    assert (await _sync(client, a, sid))["stats"]["added"] == 3
    fake.put("/Team/b.txt", "bravo platypus v2")
    fake.put("/Team/New")
    fake.put("/Team/New/n.txt", "new quetzal")
    fake.put("/Other/o2.md", "still out of scope")
    fake.delete("/Team/Deep/c.csv")
    fake.requests.clear()
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded", run
    assert {k: run["stats"][k] for k in ("added", "updated", "deleted")} == {
        "added": 1, "updated": 1, "deleted": 0}  # fmt: skip
    routes = {r.url.path for r in fake.requests}
    assert "/2/files/list_folder" not in routes and "/2/files/list_folder/continue" in routes
    cid = fake.log[6]["id"]  # c.csv
    assert (await _docs(a, sid))[cid].status == "ready"  # incremental never deletes
    run = await _sync(client, a, sid, full=True)
    assert run["stats"]["deleted"] == 1 and (await _docs(a, sid))[cid].status == "deleted"


async def test_reset_cursor_relists_without_deleting(
    client: httpx.AsyncClient, fake: FakeDropbox
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"paths": ["/Team"]})
    await _sync(client, a, sid)
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        cur = await s.scalar(text("SELECT cursor FROM sources WHERE id = :i"), {"i": sid})
    fake.reset.add(cur["cursors"]["/Team"])
    fake.delete("/Team/b.txt")
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded" and run["stats"]["deleted"] == 0
    assert run["stats"]["unchanged"] == 2


async def test_size_cap_skips_and_fails_oversized(
    client: httpx.AsyncClient, fake: FakeDropbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_ingestion_settings.cache_clear()
    fake.put("/big.txt", "x", size=2 * 1024 * 1024)
    fake.put("/lie.txt", "y" * (1024 * 1024 + 10), size=10)
    a = await register(client)
    _, sid = await _connect(client, a, fake)
    run = await _sync(client, a, sid)
    assert run["stats"]["discovered"] == 1 and run["stats"]["failed"] == 1


# ---------------------------------------------------------------- tokens


async def test_refresh_keeps_refresh_token_and_invalid_grant_says_reconnect(
    client: httpx.AsyncClient, fake: FakeDropbox
) -> None:
    _tree(fake)
    fake.expires_in = 30
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"paths": ["/Team/Deep"]})
    before = await _secret(a, sid)
    fake.expires_in = 14400
    assert (await _sync(client, a, sid))["status"] == "succeeded" and fake.refreshes == 1
    after = await _secret(a, sid)
    assert after["refresh_token"] == before["refresh_token"]  # Dropbox does not rotate it
    assert after["access_token"] in fake.access
    fake.access.clear()
    fake.refresh_ok.clear()
    run = await _sync(client, a, sid)
    assert run["error"] == (
        "Sync failed: Dropbox access was revoked or has expired. Reconnect Dropbox."
    )
    r = await client.post(f"/v1/sources/{sid}/test", headers=a.headers)
    assert r.json()["ok"] is False and "Reconnect Dropbox" in r.json()["message"]


async def test_test_endpoint_and_delete_revokes(
    client: httpx.AsyncClient, fake: FakeDropbox
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"paths": ["/Team"]})
    r = await client.post(f"/v1/sources/{sid}/test", headers=a.headers)
    assert r.json() == {"ok": True, "message": "Connected as owner@example.com; 1 folder(s) "
                        "readable.", "schema": None}  # fmt: skip
    access = (await _secret(a, sid))["access_token"]
    assert (await client.delete(f"/v1/sources/{sid}", headers=a.headers)).status_code == 204
    assert fake.revoked == [access]
    revoke = next(r for r in fake.requests if r.url.path == "/2/auth/token/revoke")
    assert access not in str(revoke.url)


async def test_full_run_over_max_files_fails_instead_of_truncating(
    client: httpx.AsyncClient, fake: FakeDropbox
) -> None:
    _tree(fake)
    fake.page_size = 1  # a page boundary right after the 2nd file
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"paths": ["/Team"], "max_files": 2})
    run = await _sync(client, a, sid)  # 3 files: a cut-short full view could delete real docs
    assert run["status"] == "failed" and "raise max_files" in run["error"], run

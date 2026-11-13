"""A7b: OneDrive / SharePoint connector against a contract fake of Microsoft identity v2 + Graph.

FakeMicrosoft answers like the documented endpoints (login.microsoftonline.com/{tenant}/oauth2/
v2.0/authorize+token, Graph v1.0 drive/root/items/children/delta/content with the 302 to a
pre-authenticated download host) and enforces PKCE S256, client credentials, bearer expiry and
refresh-token revocation. Live Microsoft is never called.
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

from app.connectors import oauth, onedrive
from app.connectors.onedrive import GRAPH, OneDriveConnector
from app.connectors.registry import REGISTRY
from app.core.config import get_settings
from app.core.db import SessionLocal, use_tenant
from app.ingestion.config import get_ingestion_settings
from tests.conftest import Account, register
from tests.test_connectors import _docs, _kb, _search, _sync, env  # noqa: F401
from tests.test_connectors_gdrive import _callback, _hold_first_run, _secret, bound


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """photo.png is now a supported type (B8); pin OCR off so it fails the same way everywhere."""
    monkeypatch.setenv("OCR_ENABLED", "false")
    get_ingestion_settings.cache_clear()
    yield
    get_ingestion_settings.cache_clear()


CLIENT_ID, CLIENT_SECRET = "11112222-bbbb-3333-cccc-4444dddd5555", "ms~test~client~secret"
FRONT = "http://localhost:3000"
NO_OCR = "OCR is not available on this server, so text in images cannot be extracted."
DL_HOST = "contoso-my.sharepoint.com"
ROOT = "01ROOT"


class FakeMicrosoft:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {
            ROOT: {"id": ROOT, "name": "root", "root": {}, "folder": {}, "parentReference": {}}
        }
        self.blobs: dict[str, bytes] = {}
        self.changes: list[dict[str, Any]] = []
        self.codes: dict[str, str] = {}
        self.access: dict[str, float] = {}
        self.refresh_ok: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.refreshes, self.expires_in, self.page_size = 0, 3600, 2
        self.scope = "Files.Read.All User.Read openid profile"
        self.location: str | None = None  # override the 302 target
        self.next_link: str | None = None  # override children nextLink
        self.gone = False

    # ---- helpers
    def put(self, iid: str, name: str, parent: str, body: str | None = None, **kw: Any) -> None:
        it: dict[str, Any] = {"id": iid, "name": name, "parentReference": {"id": parent,
              "driveId": "b!DRIVE"}, "eTag": f'"{{{iid}}},{len(self.changes)}"',
              "webUrl": f"https://contoso.sharepoint.com/{name}",
              "createdBy": {"user": {"displayName": "Owner", "email": "owner@contoso.com"}},
              "lastModifiedDateTime": "2026-11-12T00:00:00Z", **kw}  # fmt: skip
        if body is None:
            it["folder"] = {"childCount": 0}
        else:
            data = body.encode()
            qx = base64.b64encode(hashlib.sha256(data).digest()[:20]).decode()  # opaque marker
            it.setdefault("file", {"mimeType": "text/plain", "hashes": {"quickXorHash": qx}})
            it.setdefault("size", len(data))
            self.blobs[iid] = data
        self.items[iid] = it
        self.changes.append(dict(it))

    def delete(self, iid: str) -> None:
        it = self.items.pop(iid)
        self.changes.append({"id": iid, "deleted": {}, "parentReference": it["parentReference"]})

    def authorize(self, query: dict[str, list[str]]) -> str:
        assert query["code_challenge_method"] == ["S256"]
        code = "M.C5" + uuid.uuid4().hex
        self.codes[code] = query["code_challenge"][0]
        return code

    def path_of(self, iid: str) -> str:
        it, parts = self.items[iid], []
        while "root" not in it:
            parts.append(it["name"])
            it = self.items[it["parentReference"]["id"]]
        return "/" + "/".join(reversed(parts))

    def graph(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "graph.microsoft.com"
                and path in r.url.path]  # fmt: skip

    # ---- endpoints
    def _issue(self) -> dict[str, Any]:
        tok, rt = "eyJ0." + uuid.uuid4().hex, "0.AR" + uuid.uuid4().hex
        self.access[tok] = time.time() + self.expires_in
        self.refresh_ok.add(rt)  # Microsoft rotates refresh tokens on every redemption
        return {"token_type": "Bearer", "access_token": tok, "expires_in": self.expires_in,
                "scope": self.scope, "refresh_token": rt}  # fmt: skip

    def _token(self, req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        bad = httpx.Response(400, json={"error": "invalid_grant", "error_codes": [70008]})
        if (form.get("client_id"), form.get("client_secret")) != (CLIENT_ID, CLIENT_SECRET):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            challenge = self.codes.pop(form.get("code", ""), None)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if challenge != base64.urlsafe_b64encode(digest).decode().rstrip("="):
                return bad
            assert form["redirect_uri"] == f"{FRONT}/oauth/finish"
            return httpx.Response(200, json=self._issue())
        if form.get("refresh_token") not in self.refresh_ok:
            return bad
        self.refreshes += 1
        return httpx.Response(200, json=self._issue())

    def _page(self, rows: list[dict[str, Any]], url: httpx.URL, key: str) -> dict[str, Any]:
        start = int(url.params.get(key, "0"))
        out: dict[str, Any] = {"value": rows[start : start + self.page_size]}
        if start + self.page_size < len(rows):
            out["@odata.nextLink"] = self.next_link or str(
                url.copy_set_param(key, str(start + self.page_size))
            )
        return out

    def _delta(self, url: httpx.URL, base: str) -> httpx.Response:
        tok = url.params["token"]
        if tok == "latest":
            link = f"{GRAPH}/{base}/root/delta?token={len(self.changes)}"
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": link})
        if self.gone:
            return httpx.Response(410, json={"error": {"code": "resyncRequired"}})
        start, skip = int(tok), int(url.params.get("skip", "0"))
        rows = self.changes[start + skip : start + skip + self.page_size]
        if start + skip + self.page_size < len(self.changes):
            nxt = url.copy_set_param("skip", str(skip + self.page_size))
            return httpx.Response(200, json={"value": rows, "@odata.nextLink": str(nxt)})
        link = f"{GRAPH}/{base}/root/delta?token={len(self.changes)}"
        return httpx.Response(200, json={"value": rows, "@odata.deltaLink": link})

    def _graph(self, req: httpx.Request) -> httpx.Response:
        if self.access.get(req.headers.get("Authorization", "").removeprefix("Bearer "), 0) < (
            time.time()
        ):
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        path = req.url.path.removeprefix("/v1.0/")
        m = re.fullmatch(r"(me/drive|drives/[\w!.-]+)(.*)", path)
        assert m, path
        base, rest = m[1], m[2]
        if rest == "":
            return httpx.Response(200, json={"id": "b!DRIVE", "owner": {"user": {
                "displayName": "Owner"}}})  # fmt: skip
        if rest == "/root/delta":
            return self._delta(req.url, base)
        if rest == "/root":
            return httpx.Response(200, json=self.items[ROOT])
        if (pm := re.fullmatch(r"/root:(.+):", rest)) is not None:
            hit = [i for i in self.items.values() if self.path_of(i["id"]) == pm[1]]
            return httpx.Response(200, json=hit[0]) if hit else _404()
        im = re.fullmatch(r"/items/([\w!.-]+)(/children|/content)?", rest)
        assert im, rest
        it = self.items.get(im[1])
        if it is None:
            return _404()
        if im[2] == "/children":
            kids = [i for i in self.items.values() if i["parentReference"].get("id") == im[1]]
            return httpx.Response(200, json=self._page(kids, req.url, "$skiptoken"))
        if im[2] == "/content":
            loc = self.location or f"https://{DL_HOST}/_layouts/15/download.aspx?UniqueId={im[1]}"
            return httpx.Response(302, headers={"Location": loc})
        return httpx.Response(200, json=it)

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.host == "login.microsoftonline.com":
            assert req.url.path.endswith("/oauth2/v2.0/token")
            return self._token(req)
        if req.url.host == "graph.microsoft.com":
            return self._graph(req)
        assert "Authorization" not in req.headers, "bearer leaked to the download host"
        iid = req.url.params.get("UniqueId", "")
        if iid not in self.blobs:
            return _404()
        return httpx.Response(200, stream=httpx.ByteStream(self.blobs[iid]))  # unread stream


def _404() -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": "itemNotFound"}})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMicrosoft]:
    g = FakeMicrosoft()
    s = get_settings()
    monkeypatch.setattr(s, "connector_onedrive_enabled", True)
    monkeypatch.setattr(s, "microsoft_oauth_client_id", CLIENT_ID)
    monkeypatch.setattr(s, "microsoft_oauth_client_secret", SecretStr(CLIENT_SECRET))
    monkeypatch.setattr(s, "oauth_redirect_base", FRONT)
    monkeypatch.setitem(REGISTRY, "onedrive", OneDriveConnector())
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(g.handler))
    monkeypatch.setattr(onedrive, "BACKOFF_S", 0)

    async def resolve(host: str, port: int) -> list[str]:
        return ["10.0.0.5"] if host.endswith(".internal") else ["13.107.136.9"]

    monkeypatch.setattr(onedrive, "RESOLVER", resolve)
    yield g


def _tree(g: FakeMicrosoft) -> None:
    g.put("01SUB", "Reports", ROOT)
    g.put("01DEEP", "Résumés 2026", "01SUB")
    g.put("01A", "a.md", ROOT, "# Alpha\n\nalpha zebra quokka\n",
          file={"mimeType": "text/markdown", "hashes": {"quickXorHash": "qxA="}})  # fmt: skip
    g.put("01PNG", "photo.png", ROOT, "png", file={"mimeType": "image/png", "hashes": {}})
    g.put("01ONE", "Notebook", ROOT, package={"type": "oneNote"})
    g.items["01ONE"].pop("folder")  # OneNote packages are neither file nor folder
    g.put("01B", "b.txt", "01SUB", "bravo platypus")
    g.put("01C", "c.csv", "01SUB", "k,v\nechidna,1\n")
    g.put("01D", "d.txt", "01DEEP", "delta pangolin")


async def _start(
    client: httpx.AsyncClient, a: Account, kb: str, cfg: dict[str, Any] | None = None
) -> httpx.Response:
    body = {"type": "onedrive", "name": "OneDrive", "config": cfg or {}}
    return await client.post(f"/v1/kbs/{kb}/sources/oauth/start", json=body, headers=bound(a))


async def _connect(
    client: httpx.AsyncClient, a: Account, g: FakeMicrosoft, cfg: dict[str, Any] | None = None
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
    assert get_settings().connector_onedrive_enabled is False
    a = await register(client)
    r = await client.get("/v1/connectors", headers=a.headers)
    assert "onedrive" not in {c["type"] for c in r.json()["items"]}
    r = await _start(client, a, await _kb(client, a))
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_connector"


async def test_start_url_pkce_scopes_and_tenant(
    client: httpx.AsyncClient, fake: FakeMicrosoft, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    url = urlsplit((await _start(client, a, kb)).json()["authorize_url"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    )
    assert q["scope"] == "Files.Read.All offline_access" and q["response_mode"] == "query"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["client_id"] == CLIENT_ID and len(q["state"]) >= 43
    stored = json.loads(await oauth._redis_client().get(f"oauth:state:{q['state']}"))
    digest = hashlib.sha256(stored["verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == q["code_challenge"]
    monkeypatch.setattr(get_settings(), "microsoft_oauth_tenant", "contoso.onmicrosoft.com")
    url = urlsplit((await _start(client, a, kb)).json()["authorize_url"])
    assert url.path == "/contoso.onmicrosoft.com/oauth2/v2.0/authorize"
    monkeypatch.setattr(get_settings(), "microsoft_oauth_client_secret", None)
    r = await _start(client, a, kb)
    assert r.status_code == 422 and r.json()["error"]["code"] == "oauth_not_configured"


async def test_config_validation(client: httpx.AsyncClient, fake: FakeMicrosoft) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    for bad in ({"folder_paths": ["/a/../../users"]}, {"drive": "x/../../me"},
                {"folder_ids": ["a b"]}, {"max_files": 0}):  # fmt: skip
        r = await _start(client, a, kb, bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_config", bad


async def test_callback_tokens_encrypted_never_exposed(
    client: httpx.AsyncClient, fake: FakeMicrosoft, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    a = await register(client)
    kb, sid = await _connect(client, a, fake)
    tokens = await _secret(a, sid)
    assert tokens["access_token"] in fake.access and tokens["refresh_token"] in fake.refresh_ok
    assert tokens["source_id"] == sid and tokens["org_id"] == str(a.org_id)
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
    assert CLIENT_SECRET in get_settings().secret_values()


@pytest.mark.parametrize(
    ("scope", "drop_refresh"),
    [("User.Read openid", False), ("Files.Read.All", True)],
)
async def test_missing_scope_or_offline_access_is_refused(
    client: httpx.AsyncClient, fake: FakeMicrosoft, scope: str, drop_refresh: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    fake.scope = scope
    if drop_refresh:
        issue = fake._issue
        monkeypatch.setattr(fake, "_issue", lambda: {
            k: v for k, v in issue().items() if k != "refresh_token"})  # fmt: skip
    a = await register(client)
    kb = await _kb(client, a)
    q = parse_qs(urlsplit((await _start(client, a, kb)).json()["authorize_url"]).query)
    loc = await _callback(client, a, code=fake.authorize(q), state=q["state"][0])
    assert loc.endswith("oauth_error=scope_denied")
    assert (await client.get(f"/v1/kbs/{kb}/sources", headers=a.headers)).json()["items"] == []


def test_granted_scope_forms() -> None:
    p = onedrive.microsoft()
    rt = {"refresh_token": "r"}
    assert oauth.granted(p, {**rt, "scope": "https://graph.microsoft.com/Files.Read.All"})
    assert oauth.granted(p, {**rt, "scope": "https%3A%2F%2Fgraph.microsoft.com%2Ffiles.read.all"})
    assert not oauth.granted(p, {**rt, "scope": "https://graph.microsoft.com/Files.Read"})
    assert not oauth.granted(p, {"scope": "Files.Read.All"})  # no refresh token


async def test_ac1_cross_org_cannot_start_for_another_orgs_kb(
    client: httpx.AsyncClient, fake: FakeMicrosoft
) -> None:
    a, b = await register(client), await register(client)
    assert (await _start(client, b, await _kb(client, a))).status_code == 404


# ---------------------------------------------------------------- discovery + sync


async def test_full_sync_recursive_paged_download_without_bearer(
    client: httpx.AsyncClient, fake: FakeMicrosoft
) -> None:
    _tree(fake)
    a = await register(client)
    kb, sid = await _connect(client, a, fake)
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded", run
    docs = await _docs(a, sid)
    assert {d.name for d in docs.values()} == {"a.md", "b.txt", "c.csv", "d.txt", "photo.png"}
    # photo.png is an image (B8): without OCR it is listed as failed with a reason, never dropped.
    assert (docs["01PNG"].status, docs["01PNG"].error) == ("failed", NO_OCR)
    assert all(d.status == "ready" for k, d in docs.items() if k != "01PNG")
    assert any("$skiptoken" in r.url.params for r in fake.graph("/children"))  # paging
    assert docs["01A"].content_hash == "qx:qxA="
    assert docs["01A"].meta["acl_enforced"] is False
    assert docs["01A"].meta["created_by"] == "owner@contoso.com"
    assert any("zebra" in t for t in await _search(client, a, kb, "zebra quokka"))
    downloads = [r for r in fake.requests if r.url.host == DL_HOST]
    assert len(downloads) == 5 and all("Authorization" not in r.headers for r in downloads)
    run = await _sync(client, a, sid, full=True)
    assert run["stats"]["unchanged"] == 5  # change marker: nothing downloaded again
    assert len([r for r in fake.requests if r.url.host == DL_HOST]) == 5


async def test_folder_paths_non_ascii_and_non_recursive(
    client: httpx.AsyncClient, fake: FakeMicrosoft
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_paths": ["/Reports/Résumés 2026/"]})
    await _sync(client, a, sid)
    assert set(await _docs(a, sid)) == {"01D"}
    _, sid = await _connect(client, a, fake, {"folder_paths": ["/Reports"], "recursive": False})
    await _sync(client, a, sid)
    assert set(await _docs(a, sid)) == {"01B", "01C"}
    r = await _connect_test(client, a, fake, {"folder_paths": ["/a.md"]})
    assert r.status_code == 422 and "not a folder" in r.json()["error"]["message"]


async def _connect_test(
    client: httpx.AsyncClient, a: Account, g: FakeMicrosoft, cfg: dict[str, Any]
) -> httpx.Response:
    _, sid = await _connect(client, a, g)
    return await client.patch(f"/v1/sources/{sid}", json={"config": cfg}, headers=a.headers)


async def test_incremental_delta_then_full_run_deletes(
    client: httpx.AsyncClient, fake: FakeMicrosoft
) -> None:
    _tree(fake)
    fake.put("01OUT", "Elsewhere", "01NOTSEL")
    fake.items["01NOTSEL"] = {"id": "01NOTSEL", "name": "x", "folder": {},
                              "parentReference": {"id": ROOT}}  # fmt: skip
    fake.put("01OUTF", "o.md", "01OUT", "moved marmot")
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["01SUB"]})
    assert (await _sync(client, a, sid))["stats"]["added"] == 3
    fake.put("01B", "b.txt", "01SUB", "bravo platypus v2")  # changed
    fake.put("01NEWD", "later", "01SUB")  # folder created after the full run
    fake.put("01N", "n.txt", "01NEWD", "new quetzal")
    fake.put("01OUT", "Elsewhere", "01SUB")  # moved in: its file has no own delta entry
    fake.put("01X", "x.txt", ROOT, "out of scope")
    fake.delete("01C")
    fake.requests.clear()
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded", run
    assert {k: run["stats"][k] for k in ("added", "updated", "deleted")} == {
        "added": 2, "updated": 1, "deleted": 0}  # fmt: skip
    assert fake.graph("/root/delta") and any("skip" in r.url.params for r in fake.requests)
    docs = await _docs(a, sid)
    assert docs["01C"].status == "ready" and "01X" not in docs and "01OUTF" in docs
    run = await _sync(client, a, sid, full=True)
    assert run["stats"]["deleted"] == 1 and (await _docs(a, sid))["01C"].status == "deleted"


async def test_expired_delta_token_relists_without_deleting(
    client: httpx.AsyncClient, fake: FakeMicrosoft
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["01SUB"]})
    await _sync(client, a, sid)
    fake.gone = True
    fake.delete("01C")
    run = await _sync(client, a, sid)
    assert run["status"] == "succeeded" and run["stats"]["deleted"] == 0
    assert run["stats"]["unchanged"] == 2  # re-listed, nothing downloaded again
    fake.gone = False
    assert (await _sync(client, a, sid))["status"] == "succeeded"  # new delta link works


async def test_links_off_graph_and_unsafe_downloads_are_refused(
    client: httpx.AsyncClient, fake: FakeMicrosoft
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["01SUB"]})
    fake.next_link = "https://evil.example/v1.0/me/drive/items/01SUB/children?$skiptoken=2"
    fake.put("01E", "e.txt", "01SUB", "echo")  # 3 children: page 2 needs the next link
    run = await _sync(client, a, sid)
    assert run["status"] == "failed" and "unexpected link" in run["error"]
    assert not [r for r in fake.requests if r.url.host == "evil.example"]
    fake.next_link = None
    for loc in ("http://contoso-my.sharepoint.com/x?UniqueId=01B",
                "https://files.internal/x?UniqueId=01B"):  # fmt: skip
        fake.location = loc
        run = await _sync(client, a, sid, full=True)
        # b.txt was stored before the bad link stopped the earlier run: it is unchanged now.
        assert run["status"] == "partial" and run["stats"]["failed"] == 3, run["stats"]
    assert not [r for r in fake.requests if r.url.host == "files.internal"]


async def test_size_cap_skips_and_fails_oversized(
    client: httpx.AsyncClient, fake: FakeMicrosoft, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_ingestion_settings.cache_clear()
    fake.put("01SUB", "Reports", ROOT)
    fake.put("01BIG", "big.txt", "01SUB", "x", size=2 * 1024 * 1024)  # skipped at discovery
    fake.put("01LIE", "lie.txt", "01SUB", "y" * (1024 * 1024 + 10), size=10)  # size lies
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["01SUB"]})
    run = await _sync(client, a, sid)
    assert run["stats"]["discovered"] == 1 and run["stats"]["failed"] == 1
    assert not [r for r in fake.requests if r.url.params.get("UniqueId") == "01BIG"]


# ---------------------------------------------------------------- tokens


async def test_refresh_persists_rotated_refresh_token(
    client: httpx.AsyncClient, fake: FakeMicrosoft
) -> None:
    _tree(fake)
    fake.expires_in = 30
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["01DEEP"]})
    before = await _secret(a, sid)
    fake.expires_in = 3600
    assert (await _sync(client, a, sid))["status"] == "succeeded" and fake.refreshes == 1
    after = await _secret(a, sid)
    assert after["refresh_token"] != before["refresh_token"]  # Microsoft rotated it
    assert after["access_token"] in fake.access and after["expires_at"] > time.time() + 3000


async def test_invalid_grant_says_reconnect_and_delete_needs_no_revoke(
    client: httpx.AsyncClient, fake: FakeMicrosoft
) -> None:
    _tree(fake)
    a = await register(client)
    _, sid = await _connect(client, a, fake, {"folder_ids": ["01DEEP"]})
    r = await client.post(f"/v1/sources/{sid}/test", headers=a.headers)
    assert r.json() == {"ok": True, "message": "Connected to Owner's drive; 1 folder(s) readable.",
                        "schema": None}  # fmt: skip
    fake.access.clear()
    fake.refresh_ok.clear()
    run = await _sync(client, a, sid)
    assert run["error"] == (
        "Sync failed: OneDrive access was revoked or has expired. Reconnect OneDrive."
    )
    assert (await client.get(f"/v1/sources/{sid}", headers=a.headers)).json()["status"] == "error"
    fake.requests.clear()
    assert (await client.delete(f"/v1/sources/{sid}", headers=a.headers)).status_code == 204
    assert fake.requests == []  # Microsoft v2 has no per-grant revoke endpoint

"""Dropbox connector (API v2, OAuth 2.0 code + PKCE, `token_access_type=offline` refresh).

Scopes: files.metadata.read + files.content.read (read-only), account_info.read for the test.
Full run: files/list_folder per configured path (recursive optional) + list_folder/continue
until has_more is false; the last cursor of each path is saved. Incremental run: continue from
those cursors. Deleted entries are skipped there and removed by the next full run (every
`full_sync_every`-th run, or `?full=true`): the engine never deletes on incremental runs. A
`reset` cursor error re-lists that path inside the same (no-delete) run.

Change marker: Dropbox `content_hash` (SHA-256 over 4 MiB block hashes), so unchanged files
are never downloaded. files/download takes its argument in the Dropbox-API-Arg header, which
must be ASCII: json.dumps escapes 0x7F and all non-ASCII as \\uXXXX (Dropbox's own rule).

Permissions: meta keeps the shared-folder id, but retrieval is KB-level: everyone with the KB
can retrieve every synced file. Dropbox sharing is NOT enforced per user.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import PurePath
from typing import Any

import httpx
from pydantic import BaseModel, Field, JsonValue, field_validator

from app.connectors import oauth, ssrf
from app.connectors.base import Checked, Config, ConnectorError, ConnectorMeta, Item
from app.connectors.gdrive import EXTS, read_download
from app.core.config import get_settings
from app.ingestion.config import get_ingestion_settings

API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
SCOPES = ("files.metadata.read", "files.content.read")
BACKOFF_S = 1.0


def _client() -> tuple[str, str] | None:
    s = get_settings()
    if not s.dropbox_oauth_client_id or s.dropbox_oauth_client_secret is None:
        return None
    return s.dropbox_oauth_client_id, s.dropbox_oauth_client_secret.get_secret_value()


DROPBOX = oauth.Provider(
    title="Dropbox",
    authorize_url="https://www.dropbox.com/oauth2/authorize",
    token_url="https://api.dropboxapi.com/oauth2/token",  # noqa: S106  # an endpoint
    revoke_url=f"{API}/auth/token/revoke",
    scopes=(*SCOPES, "account_info.read"),
    required_scopes=SCOPES,
    credentials=_client,
    auth_params={"token_access_type": "offline"},  # = a refresh token
    revoke_bearer=True,
)


def arg_header(args: dict[str, Any]) -> str:
    return json.dumps(args)  # ensure_ascii (default) escapes 0x7F and non-ASCII: header-safe


class _EndpointError(Exception):
    def __init__(self, tag: str, summary: str) -> None:
        super().__init__(summary)
        self.tag = tag


class DropboxConfig(BaseModel):
    paths: list[str] = Field(
        default=[""], min_length=1, max_length=100, description='Folders like /Team/Docs; "" = all'
    )
    recursive: bool = Field(default=True, description="Include subfolders")
    max_files: int = Field(default=10_000, ge=1, le=50_000)
    full_sync_every: int = Field(default=12, ge=1, le=1000, description="Runs per full re-list")

    @field_validator("paths")
    @classmethod
    def _paths(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            p = p.strip().rstrip("/")
            parts = p.split("/")[1:]
            if p and (not p.startswith("/") or any(s in ("", ".", "..") for s in parts)):
                raise ValueError('Paths look like /Team/Docs ("" = the whole Dropbox).')
            if len(p) > 1000 or any(ord(ch) < 32 for ch in p):
                raise ValueError("Path is too long or has control characters.")
            out.append(p)
        return list(dict.fromkeys(out))


def _error(r: httpx.Response) -> str:
    try:
        summary = str(r.json()["error_summary"])[:60]
    except (ValueError, KeyError, TypeError):
        summary = str(r.status_code)
    return f"Dropbox refused the request ({summary})."


def _tag(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"][".tag"])
    except (ValueError, KeyError, TypeError):
        return ""


class _Api:
    def __init__(self, c: httpx.AsyncClient, tokens: oauth.TokenSession) -> None:
        self.c, self.tokens = c, tokens

    async def _send(self, url: str, **kw: Any) -> httpx.Response:
        refreshed, headers = False, kw.pop("headers", {})
        for attempt in range(4):
            auth = {
                **headers,
                "Authorization": f"Bearer {await self.tokens.bearer()}",
                **ssrf.ACCEPT_ENCODING,
            }
            req = self.c.build_request("POST", url, headers=auth, **kw)
            r = await self.c.send(req, stream=True)
            if r.status_code == 200:
                return r
            await r.aread()
            await r.aclose()
            if r.status_code == 401 and not refreshed:  # expired_access_token / revoked
                await self.tokens.refresh()
                refreshed = True
            elif r.status_code == 409:  # endpoint-specific error, e.g. reset, path/not_found
                raise _EndpointError(_tag(r), _error(r))
            elif r.status_code == 429 or r.status_code >= 500:
                retry = r.headers.get("Retry-After", "")
                wait = min(int(retry), 30) if retry.isdigit() else BACKOFF_S * 2.0**attempt
                await asyncio.sleep(wait)
            else:
                raise ConnectorError(_error(r))
        raise ConnectorError("Dropbox is busy or unavailable. Try again later.")

    async def rpc(self, route: str, args: dict[str, Any] | None) -> dict[str, Any]:
        r = await self._send(f"{API}/{route}", content=json.dumps(args).encode(),
                             headers={"Content-Type": "application/json"})  # fmt: skip
        try:
            await r.aread()
            body = r.json()
        finally:
            await r.aclose()
        if not isinstance(body, dict):
            raise ConnectorError("Dropbox sent an unexpected response.")
        return body

    async def download(self, file_id: str, cap: int) -> bytes:
        header = {"Dropbox-API-Arg": arg_header({"path": file_id})}
        r = await self._send(f"{CONTENT}/files/download", headers=header)
        return await read_download(r, cap, "Dropbox")


def _meta(f: dict[str, Any]) -> dict[str, JsonValue]:
    return {
        "dropbox_id": f["id"],
        "path": f.get("path_display"),
        "modified_time": f.get("server_modified"),
        "rev": f.get("rev"),
        "shared_folder_id": (f.get("sharing_info") or {}).get("parent_shared_folder_id"),
        "acl_enforced": False,  # retrieval is KB-level; see the module docstring
    }


class DropboxConnector:
    meta = ConnectorMeta(
        type="dropbox",
        title="Dropbox",
        description="Files from selected Dropbox folders. Read-only.",
        category="storage",
        auth="oauth2",
        config_schema=DropboxConfig.model_json_schema(),
    )
    oauth = DROPBOX

    def validate_config(self, raw: Config) -> Config:
        return DropboxConfig.model_validate(raw).model_dump()

    async def test(self, config: Config, secret: str | None) -> str | Checked:
        cfg = DropboxConfig.model_validate(config)
        async with oauth.http() as c:
            api = _Api(c, oauth.TokenSession(DROPBOX, secret))
            who = (await api.rpc("users/get_current_account", None)).get("email", "this account")
            for p in cfg.paths:
                if not p:
                    continue  # the root has no metadata
                try:
                    md = await api.rpc("files/get_metadata", {"path": p})
                except _EndpointError as exc:
                    raise ConnectorError(f"{p} was not found in this Dropbox.") from exc
                if md.get(".tag") != "folder":
                    raise ConnectorError(f"{p} is not a folder.")
        return f"Connected as {who}; {len(cfg.paths)} folder(s) readable."

    def _item(self, api: _Api, f: dict[str, Any]) -> Item | None:
        name, fid = str(f.get("name", "untitled")), str(f["id"])
        cap = get_ingestion_settings().max_upload_mb * 1024 * 1024
        if int(f.get("size") or 0) > cap or PurePath(name).suffix.lower() not in EXTS:
            return None  # too big (would fail every run) or no parser
        h = f"dbx:{f['content_hash']}" if f.get("content_hash") else f"rev:{f.get('rev')}"

        async def fetch() -> bytes:
            return await api.download(fid, cap)

        return Item(fid, name, fetch, content_hash=h, meta=_meta(f))

    async def _pages(
        self, api: _Api, cfg: DropboxConfig, path: str, cursors: dict[str, str], left: list[float]
    ) -> AsyncIterator[dict[str, Any]]:
        """Files of one path: from its saved cursor, or a fresh listing (no cursor / reset).
        Stops when `left` (incremental runs: max_files) is used up; the cursor resumes there."""
        page: dict[str, Any] = {"has_more": True, "cursor": cursors.get(path)}
        if page["cursor"] is None:
            page = await self._list(api, cfg, path)
        else:
            try:
                page = await api.rpc("files/list_folder/continue", {"cursor": page["cursor"]})
            except _EndpointError as exc:
                if exc.tag != "reset":
                    raise ConnectorError(str(exc)) from exc
                page = await self._list(api, cfg, path)  # cursor expired: re-list this path
        while True:
            for f in page.get("entries") or []:
                if f.get(".tag") == "file":  # folders need nothing; deleted waits for full run
                    left[0] -= 1
                    yield f
            cursors[path] = str(page["cursor"])
            if not page.get("has_more") or left[0] <= 0:
                return
            page = await api.rpc("files/list_folder/continue", {"cursor": page["cursor"]})

    async def _list(self, api: _Api, cfg: DropboxConfig, path: str) -> dict[str, Any]:
        args = {"path": path, "recursive": cfg.recursive, "include_deleted": False, "limit": 2000}
        try:
            return await api.rpc("files/list_folder", args)
        except _EndpointError as exc:
            raise ConnectorError(f"{path} could not be listed: {exc}") from exc

    async def discover(
        self, config: Config, secret: str | None, cursor: Config
    ) -> AsyncIterator[Item]:
        cfg = DropboxConfig.model_validate(config)
        runs = int(cursor.get("runs", 0))
        incremental = bool(cursor.get("cursors")) and runs % cfg.full_sync_every != 0
        cursors: dict[str, str] = dict(cursor.get("cursors") or {}) if incremental else {}
        cursor["incremental"], cursor["runs"] = incremental, runs + 1
        # Full runs never stop early (a cut-short view would delete); they fail past max_files.
        left, count = [float(cfg.max_files) if incremental else float("inf")], 0
        async with oauth.http() as c:
            api = _Api(c, oauth.TokenSession(DROPBOX, secret))
            for path in cfg.paths:
                async for f in self._pages(api, cfg, path, cursors, left):
                    item = self._item(api, f)
                    if item is None:
                        continue
                    count += 1
                    if count > cfg.max_files and not incremental:
                        raise ConnectorError(f"More than {cfg.max_files} files; raise max_files.")
                    yield item
        cursor["cursors"] = cursors

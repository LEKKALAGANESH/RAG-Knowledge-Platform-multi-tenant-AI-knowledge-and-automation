"""OneDrive / SharePoint connector (Microsoft Graph v1.0, identity platform v2 OAuth + PKCE).

Scope `Files.Read.All` (delegated, read-only, no admin consent): `Files.Read` would reach only
the user's own OneDrive, while SharePoint libraries (drive ids) and files shared with the user
need `.All`. `offline_access` gives the refresh token. Nothing can write.

Full run: a `delta?token=latest` link is taken BEFORE listing (edits made during the run show
up next time), then folder children are listed (recursive optional, paged by @odata.nextLink).
Incremental run: the saved delta link. Delta covers the whole drive (folder-scoped delta is
OneDrive-personal only), so only items whose parent is a known selected folder count; a folder
that enters the set is walked at once. Deleted items are skipped there and removed by the next
full run (`full_sync_every`, or `?full=true`): the engine never deletes on incremental runs.
A 410 (delta token expired) re-lists everything inside the same run, still without deletes.

Downloads: `/content` answers 302 to a pre-authenticated URL. It is followed by hand: https
only, public IPs only, and WITHOUT the Authorization header (the bearer stays on Graph).

Permissions: meta keeps creator + sharing scope, but retrieval is KB-level: everyone with the
KB can retrieve every synced file. OneDrive/SharePoint ACLs are NOT enforced per user.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import PurePath
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, JsonValue, field_validator

from app.connectors import oauth, ssrf
from app.connectors.base import Checked, Config, ConnectorError, ConnectorMeta, Item
from app.connectors.gdrive import EXTS, read_download
from app.core.config import get_settings
from app.ingestion.config import get_ingestion_settings
from app.ingestion.mime import SUPPORTED

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "Files.Read.All"
BACKOFF_S = 1.0
RESOLVER = ssrf.system_resolve  # tests swap it: the download host must resolve to public IPs
_ID = r"^[A-Za-z0-9!_.-]{1,200}$"  # personal "ABC!123", business "01ABC...", drives "b!..."


def _client() -> tuple[str, str] | None:
    s = get_settings()
    if not s.microsoft_oauth_client_id or s.microsoft_oauth_client_secret is None:
        return None
    return s.microsoft_oauth_client_id, s.microsoft_oauth_client_secret.get_secret_value()


def microsoft() -> oauth.Provider:
    base = f"https://login.microsoftonline.com/{get_settings().microsoft_oauth_tenant}/oauth2/v2.0"
    return oauth.Provider(
        title="OneDrive",
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/token",
        revoke_url=None,  # v2 has no per-grant revoke; the user removes the app (see status)
        scopes=(SCOPE, "offline_access"),
        required_scopes=(SCOPE,),
        credentials=_client,
        auth_params={"response_mode": "query"},
    )


class OneDriveConfig(BaseModel):
    drive: str = Field(default="me", description='"me" = your OneDrive, or a SharePoint drive id')
    folder_paths: list[str] = Field(
        default=[], max_length=100, description="Folder paths like /Reports/2026"
    )
    folder_ids: list[str] = Field(
        default=[], max_length=100, description="Folder item ids; no folders = whole drive"
    )
    recursive: bool = Field(default=True, description="Include subfolders")
    max_files: int = Field(default=10_000, ge=1, le=50_000)
    full_sync_every: int = Field(default=12, ge=1, le=1000, description="Runs per full re-list")

    @field_validator("drive")
    @classmethod
    def _drive(cls, v: str) -> str:
        if v != "me" and not re.fullmatch(_ID, v):
            raise ValueError('Use "me" or a drive id like b!AbC-dEf_.')
        return v

    @field_validator("folder_ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        if any(not re.fullmatch(_ID, i) for i in v):
            raise ValueError("Folder ids look like 01ABCDEF... or ABC123!45.")
        return list(dict.fromkeys(v))

    @field_validator("folder_paths")
    @classmethod
    def _paths(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            parts = p.strip().strip("/").split("/")
            bad = any(s in (".", "..") or re.search(r"[\x00-\x1f:*?\"<>|]", s) for s in parts)
            if bad or len(p) > 400:
                raise ValueError("Folder paths look like /Documents/Reports.")
            out.append("/" + "/".join(s for s in parts if s))
        return list(dict.fromkeys(out))


def _error(r: httpx.Response) -> str:
    if r.status_code == 404:
        return "A OneDrive file or folder was not found or is not shared with this account."
    try:
        code = str(r.json()["error"]["code"])[:60]
    except (ValueError, KeyError, TypeError):
        code = str(r.status_code)
    return f"OneDrive refused the request ({code})."


def _wait(r: httpx.Response, attempt: int) -> float | None:
    """Seconds to back off for a throttled/unavailable answer, None if it is a real error."""
    if r.status_code != 429 and r.status_code < 500:
        return None
    try:
        return min(float(r.headers.get("Retry-After", "")), 30.0)  # seconds form only
    except ValueError:
        return BACKOFF_S * 2.0**attempt


class _Api:
    def __init__(self, c: httpx.AsyncClient, tokens: oauth.TokenSession, base: str) -> None:
        self.c, self.tokens, self.base = c, tokens, base

    async def send(self, url: str, ok: tuple[int, ...] = (200,)) -> httpx.Response:
        if not url.startswith(f"{GRAPH}/"):  # next/delta links come from responses: Graph only
            raise ConnectorError("OneDrive sent an unexpected link.")
        refreshed = False
        for attempt in range(4):
            auth = {"Authorization": f"Bearer {await self.tokens.bearer()}", **ssrf.ACCEPT_ENCODING}
            r = await self.c.send(self.c.build_request("GET", url, headers=auth), stream=True)
            if r.status_code in ok:
                return r
            await r.aread()
            await r.aclose()
            if r.status_code == 401 and not refreshed:
                await self.tokens.refresh()
                refreshed = True
            elif (delay := _wait(r, attempt)) is not None:
                await asyncio.sleep(delay)
            else:
                raise ConnectorError(_error(r))
        raise ConnectorError("OneDrive is busy or unavailable. Try again later.")

    async def json(self, url: str, ok: tuple[int, ...] = (200,)) -> dict[str, Any]:
        r = await self.send(url if url.startswith("https://") else f"{GRAPH}/{url}", ok)
        try:
            await r.aread()
            body = r.json()
        finally:
            await r.aclose()
        if not isinstance(body, dict):
            raise ConnectorError("OneDrive sent an unexpected response.")
        return {**body, "_status": r.status_code}

    async def content(self, item_id: str, cap: int) -> bytes:
        r = await self.send(f"{GRAPH}/{self.base}/items/{item_id}/content", ok=(200, 302))
        if r.status_code == 302:
            await r.aclose()
            r = await self._download(r.headers.get("location", ""))
        return await read_download(r, cap, "OneDrive")

    async def _download(self, location: str) -> httpx.Response:
        # ponytail: DNS is checked, not pinned (TLS needs the hostname); the URL comes from
        # Graph's authenticated 302, so rebinding would need Microsoft's answer to be hostile.
        try:
            u = ssrf.check_static(location)
            if u.scheme != "https":
                raise ssrf.BlockedURL("not https")
            await ssrf.resolve_checked(u.host, 443, resolver=RESOLVER)
        except ssrf.BlockedURL as exc:
            raise ConnectorError("OneDrive sent an unsafe download link.") from exc
        req = self.c.build_request("GET", u, headers=ssrf.ACCEPT_ENCODING)  # no Authorization
        r = await self.c.send(req, stream=True)
        if r.status_code != 200:
            await r.aclose()
            raise ConnectorError(f"OneDrive download failed ({r.status_code}).")
        return r


def _meta(f: dict[str, Any]) -> dict[str, JsonValue]:
    user = (f.get("createdBy") or {}).get("user") or {}
    return {
        "drive_item_id": f["id"],
        "web_url": f.get("webUrl"),
        "modified_time": f.get("lastModifiedDateTime"),
        "created_by": user.get("email") or user.get("displayName"),
        "shared_scope": (f.get("shared") or {}).get("scope"),  # anonymous/organization/users
        "acl_enforced": False,  # retrieval is KB-level; see the module docstring
    }


class OneDriveConnector:
    meta = ConnectorMeta(
        type="onedrive",
        title="OneDrive / SharePoint",
        description="Files from OneDrive or a SharePoint document library. Read-only.",
        category="storage",
        auth="oauth2",
        config_schema=OneDriveConfig.model_json_schema(),
    )
    oauth = property(lambda _self: microsoft())  # tenant is read from settings at each use

    def validate_config(self, raw: Config) -> Config:
        return OneDriveConfig.model_validate(raw).model_dump()

    @staticmethod
    def _base(cfg: OneDriveConfig) -> str:
        return "me/drive" if cfg.drive == "me" else f"drives/{cfg.drive}"

    async def _roots(self, api: _Api, cfg: OneDriveConfig) -> list[dict[str, Any]]:
        b = api.base
        urls = [f"{b}/items/{i}" for i in cfg.folder_ids]
        urls += [f"{b}/root" if p == "/" else f"{b}/root:{quote(p)}:" for p in cfg.folder_paths]
        roots = [await api.json(u) for u in urls or [f"{b}/root"]]
        for r in roots:
            if "folder" not in r and "root" not in r:
                raise ConnectorError(f"{str(r.get('name', r.get('id')))[:100]} is not a folder.")
        return roots

    async def test(self, config: Config, secret: str | None) -> str | Checked:
        cfg = OneDriveConfig.model_validate(config)
        async with oauth.http() as c:
            api = _Api(c, oauth.TokenSession(microsoft(), secret), self._base(cfg))
            drive = await api.json(api.base)
            roots = await self._roots(api, cfg)
        who = str(((drive.get("owner") or {}).get("user") or {}).get("displayName", "this account"))
        return f"Connected to {who}'s drive; {len(roots)} folder(s) readable."

    def _item(self, api: _Api, f: dict[str, Any]) -> Item | None:
        name, fid = str(f.get("name", "untitled")), str(f["id"])
        mime = str((f.get("file") or {}).get("mimeType", ""))
        cap = get_ingestion_settings().max_upload_mb * 1024 * 1024
        if int(f.get("size") or 0) > cap or not (
            mime in SUPPORTED or PurePath(name).suffix.lower() in EXTS
        ):
            return None  # too big (would fail every run) or no parser
        qx = ((f.get("file") or {}).get("hashes") or {}).get("quickXorHash")
        h = f"qx:{qx}" if qx else f"etag:{f.get('eTag')}"  # delta omits cTag on business

        async def fetch() -> bytes:
            return await api.content(fid, cap)

        hint = mime if mime in SUPPORTED else None
        return Item(fid, name, fetch, mime_hint=hint, content_hash=h, meta=_meta(f))

    async def _walk(
        self, api: _Api, cfg: OneDriveConfig, queue: list[str], folders: set[str]
    ) -> AsyncIterator[dict[str, Any]]:
        while queue:
            url: str | None = f"{api.base}/items/{queue.pop()}/children?$top=200"
            while url:
                page = await api.json(url)
                for f in page.get("value") or []:
                    if "folder" in f:
                        if cfg.recursive and f["id"] not in folders:
                            folders.add(f["id"])
                            queue.append(f["id"])
                    elif "file" in f:
                        yield f
                url = page.get("@odata.nextLink")

    async def _full(
        self, api: _Api, cfg: OneDriveConfig, cursor: Config
    ) -> AsyncIterator[dict[str, Any]]:
        latest = await api.json(f"{api.base}/root/delta?token=latest")
        cursor["delta_link"] = str(latest["@odata.deltaLink"])
        queue = [str(r["id"]) for r in await self._roots(api, cfg)]
        folders = set(queue)
        async for f in self._walk(api, cfg, queue, folders):
            yield f
        cursor["folders"] = sorted(folders)

    async def _delta(
        self, api: _Api, cfg: OneDriveConfig, cursor: Config
    ) -> AsyncIterator[dict[str, Any]]:
        folders, link, seen = set(cursor.get("folders") or []), str(cursor["delta_link"]), 0
        while True:
            page = await api.json(link, ok=(200, 410))
            if page["_status"] == 410:  # token expired: re-list inside this (no-delete) run
                async for f in self._full(api, cfg, cursor):
                    yield f
                return
            for f in page.get("value") or []:
                parent = (f.get("parentReference") or {}).get("id")
                if "deleted" in f or parent not in folders:
                    continue  # deletions wait for the next full run
                if "file" in f:
                    seen += 1
                    yield f
                elif "folder" in f and cfg.recursive and f["id"] not in folders:
                    folders.add(f["id"])  # created or moved in: its files have no own entry
                    async for sub in self._walk(api, cfg, [f["id"]], folders):
                        seen += 1
                        yield sub
            nxt = page.get("@odata.nextLink")
            link = str(nxt or page.get("@odata.deltaLink") or link)
            if not nxt or seen >= cfg.max_files:
                break
        cursor["delta_link"], cursor["folders"] = link, sorted(folders)

    async def discover(
        self, config: Config, secret: str | None, cursor: Config
    ) -> AsyncIterator[Item]:
        cfg = OneDriveConfig.model_validate(config)
        runs = int(cursor.get("runs", 0))
        async with oauth.http() as c:
            api = _Api(c, oauth.TokenSession(microsoft(), secret), self._base(cfg))
            incremental = bool(cursor.get("delta_link")) and runs % cfg.full_sync_every != 0
            cursor["incremental"] = incremental
            files = (self._delta if incremental else self._full)(api, cfg, cursor)
            cursor["runs"] = runs + 1
            count = 0
            async for f in files:
                item = self._item(api, f)
                if item is None:
                    continue
                count += 1
                if count > cfg.max_files and not incremental:  # _delta pages by max_files
                    raise ConnectorError(f"More than {cfg.max_files} files; raise max_files.")
                yield item

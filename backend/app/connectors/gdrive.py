"""Google Drive connector (Drive API v3, OAuth 2.0 + PKCE, read-only scope).

Full run: files.list under the selected folders (recursive optional), trashed excluded; the
changes startPageToken is taken BEFORE listing so edits made during the run show up next time.
Incremental run: changes.list from that token; only files whose parent is a known selected
folder count. Removed/trashed files are skipped there and deleted by the next full run (every
`full_sync_every`-th run, or `?full=true`), because the engine never deletes on incremental runs.

Native Google files are exported (Docs -> DOCX, Sheets -> XLSX, Slides -> plain text: there is no
PPTX parser); other files download with alt=media when the ingestion parsers support them.

Permissions: each document stores an owners/sharing summary in meta, but retrieval is KB-level:
everyone with access to the KB can retrieve every synced file. Drive ACLs are NOT enforced per user.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import PurePath
from typing import Any

import httpx
from pydantic import BaseModel, Field, JsonValue, field_validator

from app.connectors import oauth, ssrf
from app.connectors.base import Checked, Config, ConnectorError, ConnectorMeta, Item
from app.core.config import get_settings
from app.ingestion.config import get_ingestion_settings
from app.ingestion.mime import DOCX, SUPPORTED, TXT, XLSX

API = "https://www.googleapis.com/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER = "application/vnd.google-apps.folder"
NATIVE = "application/vnd.google-apps."
EXPORTS = {
    "application/vnd.google-apps.document": (DOCX, ".docx"),
    "application/vnd.google-apps.spreadsheet": (XLSX, ".xlsx"),
    "application/vnd.google-apps.presentation": (TXT, ".txt"),
}
EXPORT_LIMIT = 10 * 1024 * 1024  # files.export: "exported content is limited to 10 MB"
EXTS = {".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md", ".html", ".htm", ".json", ".jsonl"}
FILE_FIELDS = (
    "id,name,mimeType,md5Checksum,version,modifiedTime,size,parents,trashed,driveId,"
    "webViewLink,owners(emailAddress),permissions(type,role,emailAddress,domain)"
)
CHANGE_FIELDS = f"changes(fileId,removed,file({FILE_FIELDS}))"
BACKOFF_S = 1.0
_ID = r"^[A-Za-z0-9_-]{4,200}$"


def _google_client() -> tuple[str, str] | None:
    s = get_settings()
    if not s.google_oauth_client_id or s.google_oauth_client_secret is None:
        return None
    return s.google_oauth_client_id, s.google_oauth_client_secret.get_secret_value()


GOOGLE = oauth.Provider(
    title="Google Drive",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",  # noqa: S106  # an endpoint, not a secret
    revoke_url="https://oauth2.googleapis.com/revoke",
    scopes=(SCOPE,),
    credentials=_google_client,
    # offline = refresh token; consent = Google re-issues one even if the app was granted before.
    auth_params={"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
)


class GDriveConfig(BaseModel):
    folder_ids: list[str] = Field(
        default=[], max_length=100, description="Folder IDs from the Drive URL; empty = My Drive"
    )
    recursive: bool = Field(default=True, description="Include subfolders")
    include_shared_drives: bool = Field(default=False, description="Allow shared-drive folders")
    mime_allowlist: list[str] = Field(
        default=[], max_length=50, description="Only these Drive MIME types; empty = all supported"
    )
    max_files: int = Field(default=10_000, ge=1, le=50_000)
    full_sync_every: int = Field(default=12, ge=1, le=1000, description="Runs per full re-list")

    @field_validator("folder_ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        if any(not re.fullmatch(_ID, i) for i in v):  # also keeps ids safe inside the q string
            raise ValueError("Folder IDs look like 1AbC_d-EfGh (from the folder's URL).")
        return list(dict.fromkeys(v))

    @field_validator("mime_allowlist")
    @classmethod
    def _mimes(cls, v: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[\w.+-]{1,100}/[\w.+-]{1,150}", m) for m in v):
            raise ValueError("MIME types look like application/pdf.")
        return v


def _error(r: httpx.Response) -> str:
    if r.status_code == 404:
        return "A Drive file or folder was not found or is not shared with this account."
    try:
        reason = str(r.json()["error"]["errors"][0]["reason"])[:60]
    except (ValueError, KeyError, IndexError, TypeError):
        reason = str(r.status_code)
    return f"Google Drive refused the request ({reason})."


async def read_download(r: httpx.Response, cap: int, service: str) -> bytes:
    """Decoded body of a streamed download, capped as it inflates (shared with OneDrive/Dropbox):
    a small gzip body must not expand past the upload limit in memory."""
    try:
        return await ssrf.read_capped(r, cap)
    except ssrf.FetchTooLarge as exc:
        raise ConnectorError("The file is larger than the upload limit.") from exc
    except httpx.DecodingError as exc:
        msg = f"{service} sent a download that could not be decoded: {exc}"
        raise ConnectorError(msg) from exc


def _rate_limited(r: httpx.Response) -> bool:
    if r.status_code == 429 or r.status_code >= 500:
        return True
    return r.status_code == 403 and ("rateLimitExceeded" in r.text or "userRateLimit" in r.text)


class _Api:
    def __init__(self, c: httpx.AsyncClient, tokens: oauth.TokenSession) -> None:
        self.c, self.tokens = c, tokens

    async def _send(self, path: str, params: dict[str, str]) -> httpx.Response:
        refreshed = False
        for attempt in range(4):
            auth = {"Authorization": f"Bearer {await self.tokens.bearer()}", **ssrf.ACCEPT_ENCODING}
            q = {"supportsAllDrives": "true", **params}
            r = await self.c.send(self.c.build_request("GET", f"{API}/{path}", params=q,
                                                       headers=auth), stream=True)  # fmt: skip
            if r.status_code == 200:
                return r
            await r.aread()
            await r.aclose()
            if r.status_code == 401 and not refreshed:  # revoked/expired early: refresh once
                await self.tokens.refresh()
                refreshed = True
            elif _rate_limited(r):
                await asyncio.sleep(BACKOFF_S * 2**attempt)
            else:
                raise ConnectorError(_error(r))
        raise ConnectorError("Google Drive is busy or unavailable. Try again later.")

    async def json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        r = await self._send(path, params)
        try:
            await r.aread()
            body = r.json()
        finally:
            await r.aclose()
        if not isinstance(body, dict):
            raise ConnectorError("Google Drive sent an unexpected response.")
        return body

    async def content(self, path: str, params: dict[str, str], cap: int) -> bytes:
        return await read_download(await self._send(path, params), cap, "Google Drive")


def _list_params(cfg: GDriveConfig) -> dict[str, str]:
    if cfg.include_shared_drives:
        return {"includeItemsFromAllDrives": "true", "corpora": "allDrives"}
    return {"corpora": "user"}


def _meta(f: dict[str, Any]) -> dict[str, JsonValue]:
    owners = [o.get("emailAddress", "") for o in f.get("owners") or []][:10]
    shared = [
        f"{p.get('type')}:{p.get('role')}:{p.get('emailAddress') or p.get('domain') or ''}"
        for p in (f.get("permissions") or [])[:50]
    ]
    return {
        "drive_file_id": f["id"],
        "web_url": f.get("webViewLink"),
        "modified_time": f.get("modifiedTime"),
        "owners": list(owners),
        "shared_with": list(shared),  # empty when this account cannot see the file's sharing
        "acl_enforced": False,  # retrieval is KB-level; see the module docstring
    }


class GDriveConnector:
    meta = ConnectorMeta(
        type="gdrive",
        title="Google Drive",
        description="Files and Google Docs/Sheets/Slides from selected Drive folders. Read-only.",
        category="storage",
        auth="oauth2",
        config_schema=GDriveConfig.model_json_schema(),
    )
    oauth = GOOGLE

    def validate_config(self, raw: Config) -> Config:
        return GDriveConfig.model_validate(raw).model_dump()

    async def test(self, config: Config, secret: str | None) -> str | Checked:
        cfg = GDriveConfig.model_validate(config)
        async with oauth.http() as c:
            api = _Api(c, oauth.TokenSession(GOOGLE, secret))
            about = await api.json("about", {"fields": "user(emailAddress)"})
            for fid in cfg.folder_ids:
                f = await api.json(f"files/{fid}", {"fields": "id,mimeType"})
                if f.get("mimeType") != FOLDER:
                    raise ConnectorError(f"{fid} is not a folder.")
        who = str(about.get("user", {}).get("emailAddress", "this account"))
        return f"Connected as {who}; {len(cfg.folder_ids) or 'My Drive'} folder(s) readable."

    def _item(self, api: _Api, cfg: GDriveConfig, f: dict[str, Any]) -> Item | None:
        mime, name, fid = str(f.get("mimeType", "")), str(f.get("name", "untitled")), str(f["id"])
        size = int(f.get("size") or 0)
        if cfg.mime_allowlist and mime not in cfg.mime_allowlist:
            return None
        if mime in EXPORTS:
            target, ext = EXPORTS[mime]
            if size > EXPORT_LIMIT:  # the export would fail every run and block deletions
                return None
            path, params, cap = f"files/{fid}/export", {"mimeType": target}, EXPORT_LIMIT
            h, name = f"v:{f.get('version')}:{f.get('modifiedTime')}", name + ext
            hint: str | None = target
        elif mime.startswith(NATIVE) or not (
            mime in SUPPORTED or PurePath(name).suffix.lower() in EXTS
        ):
            return None  # forms, drawings, images, video...: no parser
        else:
            cap = get_ingestion_settings().max_upload_mb * 1024 * 1024
            if size > cap:
                return None
            path, params = f"files/{fid}", {"alt": "media"}
            md5 = f.get("md5Checksum")
            h = f"md5:{md5}" if md5 else f"v:{f.get('version')}:{f.get('modifiedTime')}"
            hint = mime if mime in SUPPORTED else None

        async def fetch() -> bytes:
            return await api.content(path, params, cap)

        return Item(fid, name, fetch, mime_hint=hint, content_hash=h, meta=_meta(f))

    async def _walk(
        self, api: _Api, cfg: GDriveConfig, queue: list[str], folders: set[str]
    ) -> AsyncIterator[dict[str, Any]]:
        """Files under the queued folders; subfolders join `folders` (and the walk) if recursive."""
        while queue:
            folder = queue.pop()
            params = {
                "q": f"'{folder}' in parents and trashed = false",
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "pageSize": "1000",
                **_list_params(cfg),
            }
            token: str | None = None
            while True:
                page = await api.json(
                    "files", {**params, **({"pageToken": token} if token else {})}
                )
                for f in page.get("files") or []:
                    if f.get("mimeType") == FOLDER:
                        if cfg.recursive and f["id"] not in folders:  # also stops parent cycles
                            folders.add(f["id"])
                            queue.append(f["id"])
                    else:
                        yield f
                token = page.get("nextPageToken")
                if not token:
                    break

    async def _full(
        self, api: _Api, cfg: GDriveConfig, cursor: Config
    ) -> AsyncIterator[dict[str, Any]]:
        roots = cfg.folder_ids or ["root"]
        queue = [str((await api.json(f"files/{r}", {"fields": "id"}))["id"]) for r in roots]
        folders: set[str] = set(queue)
        async for f in self._walk(api, cfg, queue, folders):
            yield f
        cursor["folders"] = sorted(folders)

    async def _changes(
        self, api: _Api, cfg: GDriveConfig, cursor: Config
    ) -> AsyncIterator[dict[str, Any]]:
        # Scope = the folder set saved by the last run. A folder that enters it (created or moved
        # in, recursive only) is walked at once: files already inside it get no change entry of
        # their own, and the changes list shows each item once, at its LATEST change, so a file
        # can come before the folder it lives in. Moved-out folders stay until the next full run.
        folders = set(cursor.get("folders") or [])
        token, seen = str(cursor["page_token"]), 0
        while True:
            page = await api.json("changes", {
                "pageToken": token, "pageSize": "1000", "includeRemoved": "true",
                "fields": f"nextPageToken,newStartPageToken,{CHANGE_FIELDS}",
                **({"includeItemsFromAllDrives": "true"} if cfg.include_shared_drives else {}),
            })  # fmt: skip
            for ch in page.get("changes") or []:
                f = ch.get("file")
                if ch.get("removed") or not f or f.get("trashed"):
                    continue  # deleted by the next full run (incremental runs never delete)
                if not folders.intersection(f.get("parents") or []):
                    continue
                if f.get("mimeType") != FOLDER:
                    seen += 1
                    yield f
                elif cfg.recursive and f["id"] not in folders:
                    folders.add(f["id"])
                    async for sub in self._walk(api, cfg, [f["id"]], folders):
                        seen += 1
                        yield sub
            token = page.get("nextPageToken") or page.get("newStartPageToken") or token
            if "nextPageToken" not in page or seen >= cfg.max_files:
                break
        cursor["page_token"], cursor["folders"] = token, sorted(folders)

    async def discover(
        self, config: Config, secret: str | None, cursor: Config
    ) -> AsyncIterator[Item]:
        cfg = GDriveConfig.model_validate(config)
        runs = int(cursor.get("runs", 0))
        async with oauth.http() as c:
            api = _Api(c, oauth.TokenSession(GOOGLE, secret))
            incremental = bool(cursor.get("page_token")) and runs % cfg.full_sync_every != 0
            if incremental:
                cursor["incremental"] = True
                files = self._changes(api, cfg, cursor)
            else:
                start = await api.json("changes/startPageToken", {})
                cursor["page_token"] = str(start["startPageToken"])
                files = self._full(api, cfg, cursor)
            cursor["runs"] = runs + 1
            count = 0
            async for f in files:
                item = self._item(api, cfg, f)
                if item is None:
                    continue
                count += 1
                if count > cfg.max_files and not incremental:  # _changes pages by max_files
                    raise ConnectorError(f"More than {cfg.max_files} files; raise max_files.")
                yield item

"""Generic REST connector (C2): records from configured JSON GET endpoints, one markdown doc each.

Responses are data. Nothing in a body is executed, and the only URLs taken from a response are
pagination next-links (Link header or cursor) on the base URL's own origin. Every request goes
through the SSRF guard (DNS checked, IP pinned) and redirects are never followed, so credentials
only reach base_url's origin and the token URL.

Incremental: with `updated_field`, runs between full ones skip records older than the saved
watermark (and send it as `since_param` when set) and never delete. Every `full_sync_every`-th
run, and the first, reads everything, and records gone from the API are deleted then.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Self
from urllib.parse import quote_plus, urlencode, urljoin

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator

from app.connectors.base import Config, ConnectorError, ConnectorMeta, Item
from app.connectors.ssrf import (
    ACCEPT_ENCODING,
    USER_AGENT,
    BlockedURL,
    Fetched,
    FetchTooLarge,
    SafeFetcher,
    _is_canonical_ip,
    check_static,
    endpoint_check,
    read_capped,
)

log = logging.getLogger("app.connectors.rest")


class _NoQueryInHttpxLogs(logging.Filter):
    """httpx logs every request URL at INFO. An api key sent as a query param (any name, so the
    log scrubber's key=value patterns can miss it) must not reach the logs: drop the query."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                a.copy_with(query=None) if isinstance(a, httpx.URL) else a for a in record.args
            )
        return True


logging.getLogger("httpx").addFilter(_NoQueryInHttpxLogs())

_TOKEN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}$")  # RFC 9110 header-name token
_PARAM = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,100}$")
_SEGMENT = re.compile(r"^([A-Za-z0-9_$@-]*)(\[\*\])?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Credentials and framing belong to the secret / the client, never to plain config headers.
RESERVED_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "host", "content-length",
    "transfer-encoding", "connection", "te", "upgrade",
}  # fmt: skip
RETRY_STATUS = {429, 503}
MAX_RETRIES = 3
MAX_WAIT_S = 60.0  # a longer Retry-After fails the run; the next scheduled run tries again
_sleep = asyncio.sleep  # tests swap this to record waits instead of sleeping


def _segments(path: str, allow_star: bool) -> list[tuple[str, bool]]:
    """`data.items[*]` -> [("data", False), ("items", True)]. A leading `$` / `$.` is allowed."""
    path = path.removeprefix("$").removeprefix(".")
    out = []
    for seg in path.split(".") if path else []:
        m = _SEGMENT.match(seg)
        if m is None or not (m[1] or m[2]) or (m[2] and not allow_star):
            raise ValueError(f"Unsupported path segment {seg[:50]!r}: use dotted keys and [*].")
        out.append((m[1], bool(m[2])))
    return out


def _field_path(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    if not _segments(v.strip(), allow_star=False):
        raise ValueError("The field path is empty.")
    return v.strip()


class EndpointConfig(BaseModel):
    path: str = Field(min_length=1, max_length=500, description="Path under base_url: /v1/items")
    method: Literal["GET"] = "GET"
    query: dict[str, str] = Field(default={}, max_length=20, description="Fixed query params")
    items_path: str = Field(
        default="", max_length=200, description="Where the records are, e.g. data[*]; empty = body"
    )
    id_field: str = Field(min_length=1, max_length=200)
    title_field: str | None = Field(default=None, max_length=200)
    text_fields: list[str] = Field(min_length=1, max_length=30)
    updated_field: str | None = Field(
        default=None, max_length=200, description="Change marker (ISO time or number)"
    )
    since_param: str | None = Field(
        default=None, description="Query param that takes the watermark, e.g. updated_since"
    )

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        v = v.strip()
        bad = v.startswith("//") or ".." in v.split("/") or re.search(r"[?#\\\s\x00-\x1f]", v)
        if not v.startswith("/") or bad:
            raise ValueError("The path starts with /, without query (use `query`), '..' or spaces.")
        return v

    @field_validator("query")
    @classmethod
    def _query(cls, v: dict[str, str]) -> dict[str, str]:
        if any(not _PARAM.match(k) or len(x) > 500 or _CONTROL.search(x) for k, x in v.items()):
            raise ValueError("Query names are plain words; values are single-line, max 500 chars.")
        return v

    @field_validator("items_path")
    @classmethod
    def _items(cls, v: str) -> str:
        _segments(v.strip(), allow_star=True)
        return v.strip()

    @field_validator("id_field", "title_field", "updated_field")
    @classmethod
    def _one(cls, v: str | None) -> str | None:
        return _field_path(v)

    @field_validator("text_fields")
    @classmethod
    def _many(cls, v: list[str]) -> list[str]:
        out = [p for p in (_field_path(x) for x in v) if p]
        if not out:
            raise ValueError("At least one text field is required.")
        return out

    @field_validator("since_param")
    @classmethod
    def _since(cls, v: str | None) -> str | None:
        if v and not _PARAM.match(v):
            raise ValueError("since_param must be a plain parameter name.")
        return v or None

    @property
    def key(self) -> str:
        """Stable endpoint identity for external keys and watermarks."""
        return self.path + (f"?{urlencode(sorted(self.query.items()))}" if self.query else "")


class Pagination(BaseModel):
    type: Literal["none", "page", "offset", "cursor", "link_header"] = "none"
    page_param: str = "page"
    start_page: int = Field(default=1, ge=0, le=1)
    offset_param: str = "offset"
    size_param: str = Field(default="limit", description="Page-size param; empty = not sent")
    page_size: int = Field(default=100, ge=1, le=1000)
    cursor_param: str = "cursor"
    cursor_path: str = Field(
        default="next_cursor", max_length=200, description="Next cursor or same-origin next URL"
    )
    max_pages: int = Field(default=50, ge=1, le=1000)

    @field_validator("page_param", "offset_param", "cursor_param")
    @classmethod
    def _param(cls, v: str) -> str:
        if not _PARAM.match(v):
            raise ValueError("Parameter names are plain words.")
        return v

    @field_validator("size_param")
    @classmethod
    def _size(cls, v: str) -> str:
        return cls._param(v) if v else v

    @field_validator("cursor_path")
    @classmethod
    def _cursor(cls, v: str) -> str:
        return _field_path(v) or "next_cursor"


class AuthConfig(BaseModel):
    """Where credentials go. The credentials themselves are the source's secret: the api key or
    bearer token as plain text; basic = {"username","password"}; oauth2 = {"client_id",
    "client_secret"} (client-credentials grant)."""

    kind: Literal["none", "api_key", "bearer", "basic", "oauth2"] = "none"
    api_key_in: Literal["header", "query"] = "header"
    api_key_name: str = "X-API-Key"
    token_url: str | None = None
    scope: str = Field(default="", max_length=500)
    client_auth: Literal["basic", "body"] = Field(
        default="basic", description="How oauth2 sends the client id/secret to the token URL"
    )

    @field_validator("api_key_name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _TOKEN.match(v) or v.lower() in RESERVED_HEADERS:
            raise ValueError("api_key_name must be a plain header/parameter name.")
        return v


def _api_url(url: str, private: bool, what: str) -> str:
    try:
        u = check_static(url.strip(), allow_nonstandard_ports=True)  # port re-checked per request
    except BlockedURL as exc:
        raise ValueError(f"{what}: {exc}") from exc
    if u.scheme != "https" and not private:
        raise ValueError(f"{what} must use https (http only with allow_private_endpoint).")
    if u.query or u.fragment:
        raise ValueError(f"{what} must not have a query or fragment.")
    return str(u).rstrip("/")


class RestConfig(BaseModel):
    base_url: str = Field(max_length=500, description="https://api.example.com/v1")
    endpoints: list[EndpointConfig] = Field(min_length=1, max_length=20)
    pagination: Pagination = Pagination()
    headers: dict[str, str] = Field(
        default={}, max_length=20, description="Non-secret headers only; credentials go in secret"
    )
    auth: AuthConfig = AuthConfig()
    rate_limit_rps: float = Field(default=2.0, gt=0, le=20)
    max_items: int = Field(default=10_000, ge=1, le=50_000)
    full_sync_every: int = Field(
        default=12, ge=1, le=1000, description="Every Nth run reads all and applies deletions"
    )
    allow_nonstandard_ports: bool = False
    allow_private_endpoint: bool = Field(
        default=False, description="Allow an API in a private network (self-hosted)."
    )

    @field_validator("headers")
    @classmethod
    def _headers(cls, v: dict[str, str]) -> dict[str, str]:
        for k, x in v.items():
            if not _TOKEN.match(k) or k.lower() in RESERVED_HEADERS:
                raise ValueError(f"Header {k[:50]!r} is not allowed here (credentials: use auth).")
            if len(x) > 1000 or _CONTROL.search(x):
                raise ValueError("Header values are single-line, max 1000 chars.")
        return v

    @model_validator(mode="after")
    def _urls(self) -> Self:
        self.base_url = _api_url(self.base_url, self.allow_private_endpoint, "base_url")
        a = self.auth
        if a.kind == "oauth2":
            if not a.token_url:
                raise ValueError("oauth2 needs auth.token_url.")
            a.token_url = _api_url(a.token_url, self.allow_private_endpoint, "auth.token_url")
        keyed = a.kind == "api_key" and a.api_key_in == "header"
        if keyed and a.api_key_name.lower() in {k.lower() for k in self.headers}:
            raise ValueError("A config header has the api key's name.")
        return self


# ---------------------------------------------------------------- credentials


class _UserPass(BaseModel):
    username: str = Field(min_length=1, max_length=256, pattern=r"^[^:\x00-\x1f\x7f]+$")
    password: SecretStr = Field(min_length=1, max_length=1024)


class _Client(BaseModel):
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: SecretStr = Field(min_length=1, max_length=2048)


@dataclass(frozen=True)
class _Creds:
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    client: tuple[str, str] | None = None  # oauth2 client id + secret


def _b64(raw: str) -> str:
    return base64.b64encode(raw.encode()).decode()


def _creds(auth: AuthConfig, secret: str | None) -> _Creds:
    """Messages never contain the secret; `from None` keeps it out of tracebacks too."""
    if auth.kind == "none":
        return _Creds()  # a stored secret from an earlier auth kind is simply not sent
    if not secret or not secret.strip():
        raise ConnectorError(f"The {auth.kind} auth needs credentials (the source's secret).")
    if auth.kind in ("api_key", "bearer"):
        s = secret.strip()
        if _CONTROL.search(s) or len(s) > 8192:
            raise ConnectorError("The credential must be a single line.")
        if auth.kind == "bearer":
            return _Creds(headers={"Authorization": f"Bearer {s}"})
        if auth.api_key_in == "header":
            return _Creds(headers={auth.api_key_name: s})
        return _Creds(query={auth.api_key_name: s})
    try:
        if auth.kind == "basic":
            up = _UserPass.model_validate_json(secret)
            raw = f"{up.username}:{up.password.get_secret_value()}"
            return _Creds(headers={"Authorization": f"Basic {_b64(raw)}"})
        c = _Client.model_validate_json(secret)
        return _Creds(client=(c.client_id, c.client_secret.get_secret_value()))
    except ValidationError:
        shape = '"username", "password"' if auth.kind == "basic" else '"client_id", "client_secret"'
        raise ConnectorError(f"Credentials must be JSON with {shape}.") from None


# ---------------------------------------------------------------- HTTP


class RestHttp(SafeFetcher):
    """SafeFetcher plus caller headers and POST (token endpoint). No redirects are followed: the
    credentials would otherwise go wherever an API's Location header points."""

    async def send(
        self, method: str, url: str, headers: dict[str, str], form: dict[str, str] | None = None
    ) -> Fetched:
        u = check_static(url, self.allow_ports)
        ip, host = await self._pinned_ip(u), u.raw_host.decode("ascii")
        req = self.client.build_request(
            method,
            u.copy_with(host=ip),
            data=form,
            headers={
                **headers,
                "Accept": "application/json",
                "Host": u.netloc.decode("ascii"),
                "User-Agent": USER_AGENT,
                **ACCEPT_ENCODING,
            },
            extensions={} if _is_canonical_ip(host) else {"sni_hostname": host},
        )
        resp = await self.client.send(req, stream=True)
        return Fetched(url, resp.status_code, resp.headers, await read_capped(resp, self.max_bytes))


def _origin(url: str) -> tuple[str, str, int | None]:
    u = httpx.URL(url)
    return u.scheme, u.host.lower(), u.port or {"http": 80, "https": 443}.get(u.scheme)


def _parse_json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise ConnectorError(f"{what} did not return JSON.") from None


def _retry_after(value: str | None, attempt: int) -> float:
    wait = float(2**attempt)
    if value and value.strip().isdigit():
        wait = float(value.strip())
    elif value:
        try:
            wait = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            pass
    if wait > MAX_WAIT_S:
        raise ConnectorError(f"The API asked to wait {int(wait)} s (rate limit); retry later.")
    return max(wait, 0.0)


# ponytail: per-process token cache keyed by a hash of (token url, client, scope); each worker
# fetches its own token. Expired entries are pruned on write.
_TOKENS: dict[str, tuple[str, float]] = {}


class _Session:
    def __init__(self, cfg: RestConfig, creds: _Creds, http: RestHttp) -> None:
        self.cfg, self.creds, self.http = cfg, creds, http
        self.origin = _origin(cfg.base_url)
        self.delay, self.next_at = 1 / cfg.rate_limit_rps, 0.0

    async def _pace(self) -> None:
        if (wait := self.next_at - time.monotonic()) > 0:
            await _sleep(wait)
        self.next_at = time.monotonic() + self.delay

    async def _send(self, method: str, url: str, headers: dict[str, str], **kw: Any) -> Fetched:
        try:
            return await self.http.send(method, url, headers, **kw)
        except BlockedURL as exc:
            raise ConnectorError(str(exc)) from None
        except FetchTooLarge:
            raise ConnectorError(f"A response is over {self.http.max_bytes} bytes.") from None
        except (OSError, httpx.HTTPError) as exc:  # their text may hold the URL (query api key)
            raise ConnectorError(f"Could not reach the API ({type(exc).__name__}).") from None

    async def _request(self, url: str) -> Fetched:
        headers = {**self.cfg.headers, **self.creds.headers}
        if self.creds.client:
            headers["Authorization"] = f"Bearer {await self._token()}"
        if self.creds.query:
            url = str(httpx.URL(url).copy_merge_params(self.creds.query))
        return await self._send("GET", url, headers)

    async def get_json(self, url: str, what: str) -> tuple[Any, Fetched]:
        """JSON body of a GET, with pacing, bounded 429/503 retries and one oauth2 re-token."""
        for attempt in range(MAX_RETRIES + 1):
            await self._pace()
            r = await self._request(url)
            if r.status == 401 and self.creds.client and attempt == 0:
                _TOKENS.pop(self._token_key(), None)  # revoked/rotated early: fetch a new one
                continue
            if r.status not in RETRY_STATUS or attempt == MAX_RETRIES:
                break
            await _sleep(_retry_after(r.headers.get("retry-after"), attempt))
        if 300 <= r.status < 400:
            raise ConnectorError(f"{what} redirected (HTTP {r.status}); use the final URL.")
        if not 200 <= r.status < 300:
            raise ConnectorError(f"{what} returned HTTP {r.status}.")
        return _parse_json(r.content, what), r

    def _token_key(self) -> str:
        cid, csec = self.creds.client or ("", "")
        parts = (self.cfg.auth.token_url or "", cid, csec, self.cfg.auth.scope)
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    async def _token(self) -> str:
        key, now = self._token_key(), time.monotonic()
        if (hit := _TOKENS.get(key)) and hit[1] > now:
            return hit[0]
        token, ttl = await self._fetch_token()
        for k in [k for k, (_, exp) in _TOKENS.items() if exp <= now]:
            del _TOKENS[k]
        _TOKENS[key] = (token, now + ttl)
        return token

    async def _fetch_token(self) -> tuple[str, float]:
        a, (cid, csec) = self.cfg.auth, self.creds.client or ("", "")
        form = {"grant_type": "client_credentials", **({"scope": a.scope} if a.scope else {})}
        headers: dict[str, str] = {}
        if a.client_auth == "basic":  # RFC 6749 2.3.1: both parts form-encoded first
            headers["Authorization"] = f"Basic {_b64(f'{quote_plus(cid)}:{quote_plus(csec)}')}"
        else:
            form |= {"client_id": cid, "client_secret": csec}
        await self._pace()
        r = await self._send("POST", a.token_url or "", headers, form=form)
        if r.status != 200:
            raise ConnectorError(f"The token endpoint returned HTTP {r.status}.")
        body = _parse_json(r.content, "The token endpoint")
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token or len(token) > 8192 or _CONTROL.search(token):
            raise ConnectorError("The token endpoint sent no usable access_token.")
        ttl = body.get("expires_in")
        ttl = float(ttl) if isinstance(ttl, int | float) and not isinstance(ttl, bool) else 3600.0
        return token, max(min(ttl, 3600.0) - 60.0, 0.0)


# ---------------------------------------------------------------- records


def extract(body: Any, items_path: str) -> list[Any]:
    """Records at `items_path` (dotted keys + [*]); a path ending on an array yields its items."""
    nodes = [body]
    for name, star in _segments(items_path, allow_star=True):
        nxt: list[Any] = []
        for node in nodes:
            if name:
                node = node.get(name) if isinstance(node, dict) else None
            if star:
                nxt.extend(node if isinstance(node, list) else [])
            elif node is not None:
                nxt.append(node)
        nodes = nxt
    return nodes[0] if len(nodes) == 1 and isinstance(nodes[0], list) else nodes


def get_path(node: Any, path: str | None) -> Any:
    if not path:
        return None
    for name, _ in _segments(path, allow_star=False):
        node = node.get(name) if isinstance(node, dict) else None
    return node


def _text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)


def render(ep: EndpointConfig, rec: dict[str, Any]) -> tuple[str, str]:
    """(single-line title, markdown). Values are data: copied as text, never interpreted."""
    raw = get_path(rec, ep.title_field)
    title = " ".join(_text(raw).split())[:300] if raw not in (None, "") else ""
    parts = [f"# {title}"] if title else []
    for f in ep.text_fields:
        v = get_path(rec, f)
        if v not in (None, "", [], {}):
            parts.append(f"## {f}\n\n{_text(v)}")
    return title, "\n\n".join(parts)


def _item(ep: EndpointConfig, rid: str, title: str, text: str) -> Item:
    data = text.encode()

    async def fetch() -> bytes:
        return data

    return Item(
        external_key=f"{ep.key}#{rid}",
        name=(title or f"{ep.path} #{rid}")[:255],
        fetch=fetch,
        mime_hint="text/markdown",
        content_hash=hashlib.sha256(data).hexdigest(),
        meta={"record": rid, "endpoint": ep.path},
    )


def _mark(v: Any) -> str | float | None:
    if isinstance(v, str) and v:
        return v[:100]
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _older(a: str | float, b: str | float) -> bool:
    """a < b when comparable (ISO strings compare as text); unlike types never count as older."""
    return type(a) is type(b) and a < b  # type: ignore[operator]


# ---------------------------------------------------------------- pagination


def _first_url(cfg: RestConfig, ep: EndpointConfig, extra: dict[str, str]) -> str:
    p, params = cfg.pagination, {**ep.query, **extra}
    if p.type != "none" and p.size_param and p.type != "link_header":
        params[p.size_param] = str(p.page_size)
    if p.type == "page":
        params[p.page_param] = str(p.start_page)
    elif p.type == "offset":
        params[p.offset_param] = "0"
    return str(httpx.URL(cfg.base_url + ep.path).copy_merge_params(params))


def _link_next(header: str) -> str | None:
    for target, params in re.findall(r"<([^>]*)>((?:\s*;\s*[^;,]*)*)", header):
        m = re.search(r'rel\s*=\s*"?([^";]*)"?', params)
        if m and "next" in m[1].lower().split():
            return str(target)
    return None


def _same_origin(s: _Session, base: str, link: str) -> str:
    try:
        url = urljoin(base, link)
        if _origin(url) == s.origin:
            return url
    except (httpx.InvalidURL, ValueError):
        pass
    raise ConnectorError("The API's next-page link points to another origin; refused.")


def _next(s: _Session, url: str, body: Any, r: Fetched, count: int) -> str | None:
    p, u = s.cfg.pagination, httpx.URL(url)
    if p.type == "none" or count == 0:
        return None
    if p.type in ("page", "offset"):
        if count < p.page_size:
            return None
        name = p.page_param if p.type == "page" else p.offset_param
        step = 1 if p.type == "page" else count
        return str(u.copy_set_param(name, str(int(u.params.get(name, "0")) + step)))
    if p.type == "link_header":
        link = _link_next(r.headers.get("link", ""))
        return _same_origin(s, url, link) if link else None
    nxt = get_path(body, p.cursor_path)
    if nxt in (None, "", False) or not isinstance(nxt, str | int) or len(str(nxt)) > 2000:
        return None
    if isinstance(nxt, str) and nxt.startswith(("http://", "https://", "/")):
        return _same_origin(s, url, nxt)
    return str(u.copy_set_param(p.cursor_param, str(nxt)))


async def _pages(
    s: _Session, ep: EndpointConfig, extra: dict[str, str]
) -> AsyncIterator[list[Any]]:
    """Records page by page. A repeated URL or an identical page body is a loop: the run fails
    (so nothing is deleted) instead of spinning to max_pages."""
    url, seen, last = _first_url(s.cfg, ep, extra), set[str](), ""
    for _ in range(s.cfg.pagination.max_pages):
        digest = hashlib.sha256(url.encode()).hexdigest()
        if digest in seen:
            raise ConnectorError(f"Pagination of {ep.path} returned to an earlier page (loop).")
        seen.add(digest)
        body, r = await s.get_json(url, f"GET {ep.path}")
        if (h := hashlib.sha256(r.content).hexdigest()) == last:
            raise ConnectorError(f"Pagination of {ep.path} does not advance (same page twice).")
        last, records = h, extract(body, ep.items_path)
        yield records
        nxt = _next(s, url, body, r, len(records))
        if nxt is None:
            return
        url = nxt
    log.warning("rest max_pages reached", extra={"endpoint": ep.path})


async def _records(
    s: _Session, ep: EndpointConfig, mark: str | float | None, marks: dict[str, Any]
) -> AsyncIterator[Item]:
    """Items of one endpoint; skips records older than `mark` and records the newest marker."""
    extra = {ep.since_param: str(mark)} if mark is not None and ep.since_param else {}
    newest, seen = mark, set()
    async for page in _pages(s, ep, extra):
        for rec in page:
            rid = get_path(rec, ep.id_field)
            if (
                isinstance(rid, bool)
                or not isinstance(rid, str | int)
                or not 0 < len(str(rid)) <= 500
            ):
                continue  # not a record (or no usable id): nothing stable to key it by
            up = _mark(get_path(rec, ep.updated_field))
            if up is not None and (newest is None or not _older(up, newest)):
                newest = up
            if str(rid) in seen or (mark is not None and up is not None and _older(up, mark)):
                continue
            seen.add(str(rid))
            title, text = render(ep, rec)
            if text:
                yield _item(ep, str(rid), title, text)
    if newest is not None:
        marks[ep.key] = newest


# ---------------------------------------------------------------- connector


class RestConnector:
    meta = ConnectorMeta(
        type="rest",
        title="REST API",
        description="Records from JSON GET endpoints of any REST API. Read-only; responses are "
        "indexed as data, never followed or executed.",
        category="api",
        auth="api_key",  # takes a secret; config.auth.kind picks how it is sent (or none)
        config_schema=RestConfig.model_json_schema(),
    )

    # S1: where the stored secret goes. `auth` covers token_url and how the key is sent.
    target_fields = ("base_url", "auth", "allow_private_endpoint")

    def __init__(self, http: Callable[[RestConfig], RestHttp] | None = None) -> None:
        # Tests inject a fetcher with a fake resolver and transport; production doesn't.
        self._http = http or (
            lambda c: RestHttp(
                allow_nonstandard_ports=c.allow_nonstandard_ports,
                ip_check=endpoint_check(c.allow_private_endpoint),
            )
        )

    def validate_config(self, raw: Config) -> Config:
        return RestConfig.model_validate(raw).model_dump()

    def _open(self, config: Config, secret: str | None) -> _Session:
        cfg = RestConfig.model_validate(config)
        creds = _creds(cfg.auth, secret)
        return _Session(cfg, creds, self._http(cfg))

    async def test(self, config: Config, secret: str | None) -> str:
        s = self._open(config, secret)
        ep = s.cfg.endpoints[0]
        try:
            body, _ = await s.get_json(_first_url(s.cfg, ep, {}), f"GET {ep.path}")
        finally:
            await s.http.aclose()
        records = [r for r in extract(body, ep.items_path) if isinstance(r, dict)]
        if records and all(get_path(r, ep.id_field) is None for r in records):
            raise ConnectorError(f"No record on the first page has the id_field {ep.id_field!r}.")
        return f"Reached the API: {len(records)} records on the first page of {ep.path}."

    async def discover(
        self, config: Config, secret: str | None, cursor: Config
    ) -> AsyncIterator[Item]:
        s = self._open(config, secret)
        runs = int(cursor.get("runs", 0))
        full = runs % s.cfg.full_sync_every == 0
        marks: dict[str, Any] = {} if full else dict(cursor.get("watermarks", {}))
        count = 0
        try:
            # ponytail: first max_items records; on a full run the rest count as gone.
            for ep in s.cfg.endpoints:
                if count >= s.cfg.max_items:
                    break
                async for item in _records(s, ep, marks.get(ep.key), marks):
                    yield item
                    count += 1
                    if count >= s.cfg.max_items:
                        break
        finally:
            await s.http.aclose()
        incremental = not full and any(ep.updated_field for ep in s.cfg.endpoints)
        cursor.update(runs=runs + 1, watermarks=marks, incremental=incremental)

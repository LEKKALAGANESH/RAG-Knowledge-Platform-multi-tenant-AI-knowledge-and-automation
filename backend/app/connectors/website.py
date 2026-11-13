"""Website connector: sitemap + link crawl within configured domains, robots.txt honored.

Pages are fetched once during discovery (links are needed to keep crawling); the Item hands the
same bytes to the engine. Auth walls are never bypassed: 401/403 pages are treated as absent.
"""

import asyncio
import fnmatch
import hashlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree.ElementTree import Element

import httpx
import trafilatura
from defusedxml import ElementTree as SafeET
from pydantic import BaseModel, Field, field_validator

from app.connectors.base import Config, ConnectorError, ConnectorMeta, Item
from app.connectors.ssrf import USER_AGENT, BlockedURL, FetchTooLarge, SafeFetcher, check_static

log = logging.getLogger("app.connectors.website")
HTML_TYPES = ("text/html", "application/xhtml+xml")
GONE = {401, 403, 404, 410, 451}  # absent or access-restricted: never bypassed, never indexed
SITEMAP_NESTING = 2
SITEMAP_FETCHES = 25  # per run: a sitemap index can list thousands of nested sitemaps
# Transient network trouble: the page counts as failed, so the run cannot delete anything.
FETCH_ERRORS = (FetchTooLarge, OSError, httpx.HTTPError)


class WebsiteConfig(BaseModel):
    start_urls: list[str] = Field(min_length=1, max_length=20, description="Pages to start from")
    sitemap_url: str | None = Field(default=None, description="Optional sitemap.xml URL")
    max_depth: int = Field(default=2, ge=0, le=5, description="Link hops from a start URL")
    max_pages: int = Field(default=100, ge=1, le=2000)
    allowed_domains: list[str] = Field(
        default=[], max_length=20, description="Empty = the start URLs' hosts (and subdomains)"
    )
    include_patterns: list[str] = Field(
        default=[], max_length=50, description="Path globs, e.g. /docs/*; empty = all"
    )
    exclude_patterns: list[str] = Field(default=[], max_length=50, description="Path globs")
    rate_limit_rps: float = Field(default=1.0, gt=0, le=5)
    allow_nonstandard_ports: bool = Field(
        default=False, description="Allow ports other than 80/443"
    )

    @field_validator("start_urls")
    @classmethod
    def _urls(cls, v: list[str]) -> list[str]:
        return [_checked(u) for u in v]

    @field_validator("sitemap_url")
    @classmethod
    def _sitemap(cls, v: str | None) -> str | None:
        return _checked(v) if v else None

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        return [d.strip().lower().lstrip(".") for d in v if d.strip()]

    def domains(self) -> list[str]:
        return self.allowed_domains or sorted({_host(u) for u in self.start_urls})


def _checked(url: str) -> str:
    try:
        check_static(url, allow_nonstandard_ports=True)  # port is re-checked per fetch
    except BlockedURL as exc:
        raise ValueError(str(exc)) from exc
    return normalize(url)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def normalize(url: str) -> str:
    """Dedupe key: no fragment, lowercase scheme/host, default port dropped, empty path -> /."""
    parts = urlsplit(urldefrag(url)[0])
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    netloc = host if parts.port in (None, default) else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.title, self._in_title, self.robots = "", False, ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "a" and a.get("href") and "nofollow" not in a.get("rel", ""):
            self.links.append(a["href"])
        elif tag == "meta" and a.get("name", "").lower() in ("robots", "ragplatformbot"):
            self.robots += a.get("content", "").lower()
        self._in_title = tag == "title" and not self.title  # first <title> only (SVGs have them)

    def handle_data(self, data: str) -> None:
        if self._in_title and len(self.title) < 300:
            self.title += data

    def handle_endtag(self, tag: str) -> None:
        self._in_title = False


def _sitemap_locs(xml: bytes) -> tuple[list[str], list[str]]:
    """(page urls, nested sitemap urls). defusedxml refuses entity expansion and DTD tricks."""
    try:
        root: Element = SafeET.fromstring(xml)
    except Exception:  # any malformed or hostile sitemap is just skipped
        return [], []
    locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
    return ([], locs) if root.tag.endswith("sitemapindex") else (locs, [])


def _page_hash(html: bytes) -> str:
    # Hash the main content, not raw bytes: nonces/timestamps in markup would re-ingest every run.
    text = trafilatura.extract(html.decode("utf-8", "replace")) or html.decode("utf-8", "replace")
    return hashlib.sha256(text.encode()).hexdigest()


class _Crawl:
    """State for one discovery pass."""

    def __init__(self, cfg: WebsiteConfig, fetcher: SafeFetcher) -> None:
        self.cfg, self.fetcher = cfg, fetcher
        self.domains = cfg.domains()
        self.robots: dict[str, RobotFileParser | None] = {}
        self.seen: set[str] = set()
        self.queue: deque[tuple[str, int]] = deque()
        self.delay = 1 / cfg.rate_limit_rps

    def in_domain(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        ok_host = any(host == d or host.endswith("." + d) for d in self.domains)
        return parts.scheme in ("http", "https") and ok_host

    def in_scope(self, url: str) -> bool:
        if not self.in_domain(url):
            return False
        path = urlsplit(url).path or "/"
        if self.cfg.include_patterns and not any(
            fnmatch.fnmatchcase(path, p) for p in self.cfg.include_patterns
        ):
            return False
        return not any(fnmatch.fnmatchcase(path, p) for p in self.cfg.exclude_patterns)

    async def fetch(
        self, url: str, scope: Callable[[str], bool] | None = None
    ) -> tuple[int, str, bytes, str]:
        """(status, content-type, body, final url), paced by the rate limit."""
        await asyncio.sleep(self.delay)
        r = await self.fetcher.get(url, allow_redirect=scope or self.in_scope)
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        return r.status, ctype, r.content, r.url

    async def robot(self, url: str) -> RobotFileParser | None:
        """None = robots.txt disallows everything (unreachable or 401/403/5xx: be conservative)."""
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin in self.robots:
            return self.robots[origin]
        try:
            status, _, body, _ = await self.fetch(origin + "/robots.txt", self.in_domain)
        except (BlockedURL, *FETCH_ERRORS):
            status, body = 503, b""
        rp: RobotFileParser | None = None
        if status not in (401, 403) and status < 500:  # 404 etc: no rules, everything allowed
            rp = RobotFileParser()
            rp.parse(body.decode("utf-8", "replace").splitlines() if status == 200 else [])
            if (cd := rp.crawl_delay(USER_AGENT)) is not None:
                self.delay = max(self.delay, min(float(cd), 10.0))
        self.robots[origin] = rp
        return rp

    async def allowed(self, url: str) -> bool:
        rp = await self.robot(url)
        return rp is not None and rp.can_fetch(USER_AGENT, url)

    async def sitemap_urls(self) -> list[str]:
        roots = [self.cfg.sitemap_url] if self.cfg.sitemap_url else []
        if not roots:
            for start in self.cfg.start_urls:
                rp = await self.robot(start)
                p = urlsplit(start)
                roots += (rp.site_maps() if rp else None) or [
                    f"{p.scheme}://{p.netloc}/sitemap.xml"
                ]
        pages: list[str] = []
        budget = SITEMAP_FETCHES
        for _ in range(SITEMAP_NESTING + 1):
            nested: list[str] = []
            for sm in dict.fromkeys(roots):
                if not self.in_domain(sm) or not await self.allowed(sm):
                    continue
                if budget == 0:
                    log.info("sitemap fetch cap reached", extra={"cap": SITEMAP_FETCHES})
                    return pages[: self.cfg.max_pages * 2]
                budget -= 1
                try:
                    status, _, body, _ = await self.fetch(sm, self.in_domain)
                except (BlockedURL, *FETCH_ERRORS) as exc:
                    log.info("sitemap skipped", extra={"reason": type(exc).__name__})
                    continue
                if status == 200:
                    found, more = _sitemap_locs(body)
                    pages += found
                    nested += more
            roots = nested
        return pages[: self.cfg.max_pages * 2]


class WebsiteConnector:
    meta = ConnectorMeta(
        type="website",
        title="Website",
        description="Crawl public pages or sitemaps. Honors robots.txt; never bypasses logins.",
        category="web",
        auth="none",
        config_schema=WebsiteConfig.model_json_schema(),
    )

    def __init__(self, fetcher: Callable[[WebsiteConfig], SafeFetcher] | None = None) -> None:
        # Tests inject a fetcher with a fake resolver to reach a local server; production doesn't.
        self._fetcher = fetcher or (
            lambda c: SafeFetcher(allow_nonstandard_ports=c.allow_nonstandard_ports)
        )

    def validate_config(self, raw: Config) -> Config:
        return WebsiteConfig.model_validate(raw).model_dump()

    async def test(self, config: Config, secret: str | None) -> str:
        cfg = WebsiteConfig.model_validate(config)
        crawl = _Crawl(cfg, self._fetcher(cfg))
        crawl.delay = 0
        try:
            url = cfg.start_urls[0]
            await crawl.fetcher.check(url)  # report SSRF refusals as such, not as robots
            if not await crawl.allowed(url):
                raise ConnectorError("robots.txt does not allow crawling the first start URL.")
            status, ctype, _, _ = await crawl.fetch(url)
        except (BlockedURL, FetchTooLarge) as exc:
            raise ConnectorError(str(exc)) from exc
        except (OSError, httpx.HTTPError) as exc:
            raise ConnectorError(f"Could not reach the site ({type(exc).__name__}).") from exc
        finally:
            await crawl.fetcher.aclose()
        if status != 200 or ctype not in HTML_TYPES:
            raise ConnectorError(f"The first start URL returned HTTP {status} ({ctype or '?'}).")
        return "Reached the site and robots.txt allows crawling."

    async def discover(
        self, config: Config, secret: str | None, cursor: Config
    ) -> AsyncIterator[Item]:
        cfg = WebsiteConfig.model_validate(config)
        crawl = _Crawl(cfg, self._fetcher(cfg))
        try:
            crawl.queue.extend((u, 0) for u in cfg.start_urls)
            crawl.queue.extend((u, cfg.max_depth) for u in await crawl.sitemap_urls())
            count = 0
            while crawl.queue and count < cfg.max_pages:
                raw, depth = crawl.queue.popleft()
                try:
                    url = normalize(raw)
                except ValueError:  # e.g. a link with a non-numeric port
                    continue
                if url in crawl.seen or not crawl.in_scope(url) or not await crawl.allowed(url):
                    continue
                crawl.seen.add(url)
                item = await self._page(crawl, url, depth)
                if item is not None:
                    count += 1
                    yield item
        finally:
            await crawl.fetcher.aclose()

    async def _page(self, crawl: _Crawl, url: str, depth: int) -> Item | None:
        try:
            status, ctype, body, final = await crawl.fetch(url)
        except BlockedURL:
            return None  # e.g. redirect off-domain or to a private address: out of scope
        except FETCH_ERRORS as exc:
            return _failed(url, f"{type(exc).__name__}")
        if status in GONE or (status == 200 and ctype not in HTML_TYPES):
            return None
        if status != 200:
            return _failed(url, f"HTTP {status}")
        crawl.seen.add(normalize(final))
        page = _PageParser()
        page.feed(body.decode("utf-8", "replace"))
        if "noindex" in page.robots:
            return None
        if depth < crawl.cfg.max_depth and "nofollow" not in page.robots:
            crawl.queue.extend((urljoin(final, h), depth + 1) for h in page.links)

        async def fetch() -> bytes:
            return body

        return Item(
            external_key=url,
            name=(page.title.strip() or url)[:255],
            fetch=fetch,
            mime_hint="text/html",
            content_hash=await asyncio.to_thread(_page_hash, body),
            meta={"url": final},
        )


def _failed(url: str, reason: str) -> Item:
    async def fetch() -> bytes:
        raise ConnectorError(f"Fetching the page failed: {reason}.")

    return Item(external_key=url, name=url[:255], fetch=fetch, meta={"url": url})

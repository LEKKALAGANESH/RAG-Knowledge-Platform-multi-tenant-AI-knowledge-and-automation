"""FX-6 P3 caps: bounded gzip inflation (N4) and a per-run sitemap fetch cap (N5)."""

import gzip
import tracemalloc
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app.connectors import dropbox, gdrive, onedrive, website
from app.connectors.base import ConnectorError
from app.connectors.ssrf import FetchTooLarge
from app.connectors.website import WebsiteConfig, _Crawl
from tests.test_connectors import Site, local_fetcher


@pytest.fixture(scope="module")
def site() -> Iterator[Site]:
    s = Site()
    yield s
    s.server.shutdown()


async def test_gzip_bomb_stops_at_the_cap_without_inflating_it(site: Site) -> None:
    site.raw(
        "/bomb", 200, "text/html", gzip.compress(b"\0" * 40 * 2**20), **{"Content-Encoding": "gzip"}
    )
    fetcher = local_fetcher(max_bytes=2**20)
    tracemalloc.start()
    try:
        with pytest.raises(FetchTooLarge):
            await fetcher.get(site.url("/bomb"))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
        await fetcher.aclose()
    assert peak < 8 * 2**20, peak  # inflating one raw chunk whole would allocate ~40 MB


async def test_gzip_page_is_decoded_and_bad_encodings_fail_as_fetch_errors(site: Site) -> None:
    site.raw(
        "/gz", 200, "text/html", gzip.compress(b"<p>hello</p>"), **{"Content-Encoding": "gzip"}
    )
    site.raw("/br", 200, "text/html", b"xx", **{"Content-Encoding": "br"})
    site.raw("/bad", 200, "text/html", b"not gzip", **{"Content-Encoding": "gzip"})
    fetcher = local_fetcher()
    try:
        assert (await fetcher.get(site.url("/gz"))).content == b"<p>hello</p>"
        for path in ("/br", "/bad"):
            with pytest.raises(httpx.DecodingError):  # an httpx.HTTPError: website FETCH_ERRORS
                await fetcher.get(site.url(path))
    finally:
        await fetcher.aclose()


async def test_sitemap_fetches_are_capped_per_run(site: Site) -> None:
    n = website.SITEMAP_FETCHES * 4
    locs = "".join(f"<sitemap><loc>{site.url(f'/sm-{i}.xml')}</loc></sitemap>" for i in range(n))
    site.raw("/index.xml", 200, "application/xml", f"<sitemapindex>{locs}</sitemapindex>".encode())
    for i in range(n):
        page = f"<urlset><url><loc>{site.url(f'/p{i}')}</loc></url></urlset>"
        site.raw(f"/sm-{i}.xml", 200, "application/xml", page.encode())
    cfg = WebsiteConfig(
        start_urls=[site.url("/")],
        sitemap_url=site.url("/index.xml"),
        allow_nonstandard_ports=True,
        max_pages=2000,
    )
    fetcher = local_fetcher(cfg)
    crawl, fetched = _Crawl(cfg, fetcher), []
    crawl.delay = 0
    real = crawl.fetch

    async def counting(url: str, scope: object = None) -> tuple[int, str, bytes, str]:
        fetched.append(url)
        return await real(url, scope)  # type: ignore[arg-type]

    crawl.fetch = counting  # type: ignore[method-assign]
    try:
        pages = await crawl.sitemap_urls()
    finally:
        await fetcher.aclose()
    sitemaps = [u for u in fetched if u.endswith(".xml")]
    assert len(sitemaps) == website.SITEMAP_FETCHES  # index + 24 nested, not 101
    assert len(pages) == website.SITEMAP_FETCHES - 1


# FX-7: the cloud downloaders share ssrf.read_capped, so the cap counts DECODED bytes too.
BOMB = gzip.compress(b"\0" * 40 * 2**20)  # ~40 KB on the wire, 40 MB inflated


class _Tokens:
    async def bearer(self, force: bool = False) -> str:
        return "t"

    async def refresh(self) -> None:
        raise AssertionError("no refresh expected")


async def _download(kind: str, body: bytes, enc: str, monkeypatch: pytest.MonkeyPatch) -> bytes:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/content"):  # OneDrive: Graph answers 302 to the file host
            return httpx.Response(302, headers={"Location": "https://dl.example.com/f"})
        return httpx.Response(200, stream=httpx.ByteStream(body), headers={"Content-Encoding": enc})

    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(onedrive, "RESOLVER", public)
    tokens: Any = _Tokens()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        if kind == "gdrive":
            return await gdrive._Api(c, tokens).content("files/f1", {"alt": "media"}, 2**20)
        if kind == "onedrive":
            return await onedrive._Api(c, tokens, "me/drive").content("f1", 2**20)
        return await dropbox._Api(c, tokens).download("id:f1", 2**20)


KINDS = ("gdrive", "onedrive", "dropbox")


@pytest.mark.parametrize("kind", KINDS)
async def test_cloud_download_gzip_bomb_fails_with_bounded_memory(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracemalloc.start()
    try:
        with pytest.raises(ConnectorError, match="larger than the upload limit"):
            await _download(kind, BOMB, "gzip", monkeypatch)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 2**20, peak  # inflating the raw chunk whole would allocate ~40 MB


@pytest.mark.parametrize("kind", KINDS)
async def test_cloud_download_decodes_gzip_and_refuses_other_encodings(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _download(kind, gzip.compress(b"hello"), "gzip", monkeypatch) == b"hello"
    with pytest.raises(ConnectorError, match=r"could not be decoded.*'br'"):
        await _download(kind, b"xx", "br", monkeypatch)

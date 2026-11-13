"""B4: connector framework, website connector, SSRF guard (AC9), sync + deletion (AC7), AC1, AC11.

The SSRF guard refuses localhost, so tests reach the local server only through a fetcher built
with a fake resolver and an IP allowlist of exactly 127.0.0.1, injected into the connector here.
"""

import asyncio
import ipaddress
import logging
import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from alembic.config import Config as AlembicConfig
from dramatiq import Message
from dramatiq.brokers.stub import StubBroker
from qdrant_client import QdrantClient
from qdrant_client.models import Filter
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.connectors import scheduler, ssrf
from app.connectors import sync as sync_mod
from app.connectors.base import Config, ConnectorError, ConnectorMeta, Item
from app.connectors.registry import REGISTRY
from app.connectors.ssrf import (
    BlockedURL,
    FetchTooLarge,
    IPAddress,
    SafeFetcher,
    ip_allowed,
)
from app.connectors.website import WebsiteConfig, WebsiteConnector
from app.core.config import get_settings
from app.core.crypto import decrypt
from app.core.db import SessionLocal, use_tenant
from app.ingestion import storage
from app.ingestion import tasks as ingest_tasks
from app.ingestion.config import get_ingestion_settings
from app.kb.models import Chunk, Document
from app.retrieval import qdrant_store
from app.retrieval.embedders import PROVIDERS, get_embedding_settings
from app.retrieval.qdrant_store import get_vector_store_settings
from tests.conftest import Account, register
from tests.test_retrieval_units import HashEmbedder

# ---------------------------------------------------------------- local site


def page(title: str, body: str, links: tuple[str, ...] = (), robots: str = "") -> bytes:
    meta = f'<meta name="robots" content="{robots}">' if robots else ""
    anchors = "".join(f'<a href="{h}">{h}</a> ' for h in links)
    para = f"<p>{body} " * 4 + "</p>"
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title>{meta}</head><body>"
        f"<nav>{anchors}</nav><article><h1>{title}</h1>{para}{para}</article></body></html>"
    ).encode()


class Site:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, str, bytes, dict[str, str]]] = {}
        self.hosts: list[str] = []
        site = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                site.hosts.append(self.headers.get("Host", ""))
                status, ctype, body, headers = site.routes.get(
                    self.path, (404, "text/plain", b"nope", {})
                )
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str, host: str = "site.test") -> str:
        return f"http://{host}:{self.port}{path}"

    def html(self, path: str, body: bytes) -> None:
        self.routes[path] = (200, "text/html; charset=utf-8", body, {})

    def raw(self, path: str, status: int, ctype: str, body: bytes, **headers: str) -> None:
        self.routes[path] = (status, ctype, body, headers)


FAKE_DNS = {
    "site.test": ["127.0.0.1"],
    "other.test": ["127.0.0.1"],
    "evil.test": ["10.0.0.7"],
    "mixed.test": ["127.0.0.1", "10.0.0.1"],
}


async def fake_resolve(host: str, port: int) -> list[str]:
    if host not in FAKE_DNS:
        raise OSError("no such host")
    return FAKE_DNS[host]


def only_loopback_extra(ip: IPAddress) -> bool:
    return ip == ipaddress.ip_address("127.0.0.1") or ip_allowed(ip)


def local_fetcher(cfg: WebsiteConfig | None = None, **kw: Any) -> SafeFetcher:
    ports = cfg.allow_nonstandard_ports if cfg else True
    return SafeFetcher(
        allow_nonstandard_ports=ports, resolver=fake_resolve, ip_check=only_loopback_extra, **kw
    )


@pytest.fixture(scope="module")
def site() -> Iterator[Site]:
    s = Site()
    yield s
    s.server.shutdown()


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> Iterator[StubBroker]:
    monkeypatch.setitem(REGISTRY, "website", WebsiteConnector(fetcher=local_fetcher))
    monkeypatch.setitem(PROVIDERS, "hash", HashEmbedder)
    for k, v in {
        "EMBEDDING_PROVIDER": "hash",
        "EMBEDDING_MODEL": "hash-c",
        "EMBEDDING_DIM": "16",
        "BLOB_BACKEND": "local",
        "BLOB_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(k, v)
    get_embedding_settings.cache_clear()
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()
    stub = StubBroker()  # type: ignore[no-untyped-call]
    for actor in (sync_mod.sync_source, ingest_tasks.ingest_document):
        monkeypatch.setattr(actor, "broker", stub)
        stub.declare_actor(actor)
    yield stub
    get_embedding_settings.cache_clear()
    get_ingestion_settings.cache_clear()
    storage.get_blob_store.cache_clear()


@pytest.fixture
def lan(monkeypatch: pytest.MonkeyPatch) -> None:
    """S3/PG tests reach compose services on localhost: turn the operator gate on and let the
    opted-in check also pass loopback (production never allows loopback)."""
    monkeypatch.setattr(get_settings(), "connectors_allow_private_endpoints", True)
    real = ssrf.ip_allowed_private
    monkeypatch.setattr(ssrf, "ip_allowed_private", lambda ip: ip.is_loopback or real(ip))


# ---------------------------------------------------------------- SSRF (AC9)

REFUSED = [
    "http://127.0.0.1/",
    "http://[::1]/",
    "http://10.1.2.3/",
    "http://172.16.5.4/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/",
    "http://[fc00::1]/",
    "http://[fd12:3456::1]/",
    "http://[fe80::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://[::7f00:1]/",  # IPv4-compatible
    "http://[64:ff9b::a9fe:a9fe]/",  # NAT64 of 169.254.169.254
    "http://[2002:7f00:1::]/",  # 6to4 of 127.0.0.1
    "http://0.0.0.0/",
    "http://224.0.0.1/",
    "http://240.0.0.1/",
    "http://2130706433/",  # decimal 127.0.0.1
    "http://0177.0.0.1/",  # octal
    "http://0x7f.0.0.1/",  # hex
    "http://017700000001/",
    "http://localhost/",  # real system resolver -> loopback
    "ftp://example.com/",
    "file:///etc/passwd",
    "http://user:pw@example.com/",
    "http://example.com:8080/",  # non-standard port, flag off
    "http://example.com:22/",
]


@pytest.mark.parametrize("url", REFUSED)
async def test_ssrf_guard_refuses(url: str) -> None:
    fetcher = SafeFetcher()  # production defaults: system DNS, public-only
    with pytest.raises(BlockedURL):
        await fetcher.get(url)
    await fetcher.aclose()


def test_public_addresses_are_allowed() -> None:
    for ip in ("8.8.8.8", "93.184.215.14", "2606:4700::1111"):
        assert ip_allowed(ipaddress.ip_address(ip)), ip


async def test_hostname_resolving_to_private_ip_is_refused() -> None:
    fetcher = SafeFetcher(resolver=fake_resolve)  # real ip_check
    for host in ("evil.test", "mixed.test"):
        with pytest.raises(BlockedURL, match="private or reserved"):
            await fetcher.get(f"http://{host}/")
    await fetcher.aclose()


async def test_any_private_answer_refuses_even_with_one_allowed_ip(site: Site) -> None:
    f = local_fetcher()
    with pytest.raises(BlockedURL):
        await f.get(site.url("/", "mixed.test"))
    await f.aclose()


async def test_redirect_to_private_ip_is_refused_at_every_hop(site: Site) -> None:
    site.raw("/r-meta", 302, "text/plain", b"", Location="http://169.254.169.254/latest/meta-data/")
    site.raw("/r-dns", 302, "text/plain", b"", Location=site.url("/x", "evil.test"))
    site.raw("/r-chain", 301, "text/plain", b"", Location="/r-meta")
    site.raw("/r-v6", 307, "text/plain", b"", Location="http://[::ffff:192.168.0.1]/")
    f = local_fetcher()
    for path in ("/r-meta", "/r-dns", "/r-chain", "/r-v6"):
        with pytest.raises(BlockedURL):
            await f.get(site.url(path))
    await f.aclose()


async def test_connection_is_pinned_to_the_validated_ip(site: Site) -> None:
    # site.test has no real DNS record: a 200 proves the request went to the IP we validated,
    # with the original Host header.
    site.html("/pin", page("Pin", "pinned"))
    f = local_fetcher()
    r = await f.get(site.url("/pin"))
    await f.aclose()
    assert r.status == 200
    assert site.hosts[-1] == f"site.test:{site.port}"


async def test_nonstandard_port_needs_the_config_flag(site: Site) -> None:
    f = local_fetcher(WebsiteConfig(start_urls=[site.url("/")]))  # flag off by default
    with pytest.raises(BlockedURL, match="ports"):
        await f.get(site.url("/pin"))
    await f.aclose()


async def test_body_size_is_capped(site: Site) -> None:
    site.raw("/big", 200, "text/html", b"x" * 5000)
    f = local_fetcher(max_bytes=1000)
    with pytest.raises(FetchTooLarge):
        await f.get(site.url("/big"))
    await f.aclose()


# ---------------------------------------------------------------- crawl rules


def build_site(site: Site) -> None:
    site.routes.clear()
    site.raw(
        "/robots.txt",
        200,
        "text/plain",
        f"User-agent: *\nDisallow: /private\nSitemap: {site.url('/sitemap.xml')}\n".encode(),
    )
    site.raw(
        "/sitemap.xml",
        200,
        "application/xml",
        (
            f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{site.url('/from-sitemap')}</loc></url>"
            f"<url><loc>{site.url('/private/s')}</loc></url></urlset>"
        ).encode(),
    )
    site.html(
        "/",
        page(
            "Home",
            "Welcome to the zebra handbook",
            (
                "/a",
                "/private/p",
                "/skip-me",
                site.url("/x", "other.test"),
                "/noindex",
                "mailto:x@y.z",
                "/a#frag",
            ),
        ),
    )
    site.html("/a", page("Alpha", "Alpha pages describe aardvark feeding", ("/b",)))
    site.html("/b", page("Beta", "Beta pages describe badger burrows", ("/c",)))
    site.html("/c", page("Gamma", "Too deep to be crawled"))
    site.html("/private/p", page("Private", "robots says no"))
    site.html("/private/s", page("Private", "robots says no"))
    site.html("/skip-me", page("Skip", "excluded by pattern"))
    site.html("/noindex", page("No", "meta noindex", robots="noindex"))
    site.html("/from-sitemap", page("Sitemap", "Found via the sitemap: quokka facts"))
    site.html("/x", page("Other", "other domain"))


def site_config(site: Site, **over: Any) -> Config:
    cfg: Config = {
        "start_urls": [site.url("/")],
        "max_depth": 2,
        "max_pages": 50,
        "rate_limit_rps": 5,
        "exclude_patterns": ["/skip*"],
        "allow_nonstandard_ports": True,
    }
    return {**cfg, **over}


async def discover_keys(site: Site, **over: Any) -> set[str]:
    conn = WebsiteConnector(fetcher=local_fetcher)
    cfg = conn.validate_config(site_config(site, **over))
    return {i.external_key async for i in conn.discover(cfg, None, {})}


async def test_crawl_honors_robots_depth_domain_patterns_and_sitemap(site: Site) -> None:
    build_site(site)
    keys = await discover_keys(site)
    assert keys == {site.url(p) for p in ("/", "/a", "/b", "/from-sitemap")}


async def test_robots_disallow_is_honored_even_for_start_urls(site: Site) -> None:
    build_site(site)
    assert await discover_keys(site, start_urls=[site.url("/private/p")]) == {
        site.url("/from-sitemap")
    }


async def test_unreachable_robots_means_crawl_nothing(site: Site) -> None:
    build_site(site)
    site.raw("/robots.txt", 503, "text/plain", b"down")
    assert await discover_keys(site) == set()


async def test_max_pages_and_depth_zero(site: Site) -> None:
    build_site(site)
    assert len(await discover_keys(site, max_pages=2)) == 2
    assert await discover_keys(site, max_depth=0) == {site.url("/"), site.url("/from-sitemap")}


async def test_sitemap_entity_expansion_is_refused(site: Site) -> None:
    build_site(site)
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><urlset><url><loc>&b;</loc></url></urlset>'
    )
    site.raw("/sitemap.xml", 200, "application/xml", bomb)
    assert await discover_keys(site, max_depth=0) == {site.url("/")}


@pytest.mark.parametrize(
    "bad",
    [
        {"max_depth": 6},
        {"max_pages": 2001},
        {"rate_limit_rps": 10},
        {"start_urls": ["ftp://example.com/"]},
        {"start_urls": ["http://user:pw@example.com/"]},
        {"start_urls": []},
    ],
)
def test_config_limits(bad: Config) -> None:
    with pytest.raises(ValueError):
        WebsiteConnector().validate_config({"start_urls": ["https://example.com/"], **bad})


# ---------------------------------------------------------------- API + sync engine


async def _kb(client: httpx.AsyncClient, a: Account) -> str:
    r = await client.post("/v1/kbs", json={"name": "web"}, headers=a.headers)
    return str(r.json()["id"])


async def _source(client: httpx.AsyncClient, a: Account, kb: str, cfg: Config, **kw: Any) -> str:
    body = {"type": "website", "name": "docs", "config": cfg, **kw}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _sync(
    client: httpx.AsyncClient, a: Account, source_id: str, full: bool = False
) -> dict[str, Any]:
    q = "?full=true" if full else ""
    r = await client.post(f"/v1/sources/{source_id}/sync{q}", headers=a.headers)
    assert r.status_code == 202, r.text
    await sync_mod.run_sync(uuid.UUID(r.json()["id"]), a.org_id)
    runs = (await client.get(f"/v1/sources/{source_id}/runs", headers=a.headers)).json()["items"]
    assert runs[0]["id"] == r.json()["id"]
    return dict(runs[0])


async def _docs(a: Account, source_id: str) -> dict[str, Document]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        rows = await s.scalars(select(Document).where(Document.source_id == uuid.UUID(source_id)))
        return {d.external_key or "": d for d in rows}


async def _search(client: httpx.AsyncClient, a: Account, kb: str, q: str) -> list[str]:
    r = await client.post(
        "/v1/search", json={"query": q, "kb_ids": [kb], "mode": "keyword"}, headers=a.headers
    )
    assert r.status_code == 200, r.text
    return [h["text"] for h in r.json()["results"]]


async def test_sync_change_detection_and_deletion(client: httpx.AsyncClient, site: Site) -> None:
    build_site(site)
    a = await register(client)
    kb = await _kb(client, a)
    sid = await _source(client, a, kb, site_config(site))

    run1 = await _sync(client, a, sid)
    assert run1["status"] == "succeeded", run1
    assert run1["stats"] == {
        "discovered": 4,
        "added": 4,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "failed": 0,
    }
    docs = await _docs(a, sid)
    assert {d.status for d in docs.values()} == {"ready"}
    assert any("aardvark" in t for t in await _search(client, a, kb, "aardvark"))
    src = (await client.get(f"/v1/sources/{sid}", headers=a.headers)).json()
    assert src["status"] == "idle" and src["last_sync_at"]

    run2 = await _sync(client, a, sid)
    assert run2["stats"]["unchanged"] == 4 and run2["stats"]["added"] == 0

    site.html("/a", page("Alpha", "Alpha pages now describe armadillo armour", ("/b",)))
    run3 = await _sync(client, a, sid)
    assert (run3["stats"]["updated"], run3["stats"]["unchanged"]) == (1, 3)
    assert (await _docs(a, sid))[site.url("/a")].version == 2
    assert await _search(client, a, kb, "aardvark") == []  # old chunks/vectors replaced
    assert any("armadillo" in t for t in await _search(client, a, kb, "armadillo"))

    # A transient failure on one page: run is partial and NOTHING is deleted, although /b is gone.
    site.raw("/a", 503, "text/html", b"busy")
    site.routes.pop("/b")
    run4 = await _sync(client, a, sid)
    assert run4["status"] == "partial" and run4["stats"]["failed"] == 1, run4
    assert run4["stats"]["deleted"] == 0
    assert (await _docs(a, sid))[site.url("/b")].status == "ready"

    # Next full run: /b is deleted and its content unretrievable (AC7).
    site.html("/a", page("Alpha", "Alpha pages now describe armadillo armour"))
    run5 = await _sync(client, a, sid)
    assert run5["status"] == "succeeded" and run5["stats"]["deleted"] == 1, run5
    b = (await _docs(a, sid))[site.url("/b")]
    assert b.status == "deleted"
    assert await _search(client, a, kb, "badger") == []
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        n = await s.scalar(select(func.count()).where(Chunk.document_id == b.id))
        vecs = await s.scalar(
            text(
                "SELECT count(*) FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
                "WHERE c.document_id = :d"
            ),
            {"d": b.id},
        )
    assert (n, vecs) == (0, 0)


class ScriptedConnector:
    """Test-only connector we own, to drive engine paths the website can't (auth, crash)."""

    meta = ConnectorMeta(
        type="scripted",
        title="t",
        description="t",
        category="api",
        auth="api_key",
        config_schema={},
    )
    target_fields = ("endpoint",)  # where the secret goes; other config keys are free to change

    def __init__(self) -> None:
        self.items: list[Item] = []
        self.crash = False
        self.test_error: str | None = None
        self.secrets: list[str | None] = []
        self.tested: list[str | None] = []  # secrets test() was called with

    def validate_config(self, raw: Config) -> Config:
        return raw

    async def test(self, config: Config, secret: str | None) -> str:
        self.tested.append(secret)
        if self.test_error:
            raise ConnectorError(self.test_error)
        return "ok"

    async def discover(
        self, config: Config, secret: str | None, cursor: Config
    ) -> AsyncIterator[Item]:
        self.secrets.append(secret)
        logging.getLogger("app.connectors.test").info("discover with config %s", config)
        for item in self.items:
            yield item
        if self.crash:
            raise ConnectorError("The source API returned an error.")


def _item(key: str, body: str) -> Item:
    async def fetch() -> bytes:
        return f"# {key}\n\n{body}\n".encode()

    return Item(key, f"{key}.md", fetch, mime_hint="text/markdown")


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> ScriptedConnector:
    c = ScriptedConnector()
    monkeypatch.setitem(REGISTRY, "scripted", c)
    return c


async def test_failed_run_never_deletes(
    client: httpx.AsyncClient, scripted: ScriptedConnector
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    r = await client.post(
        f"/v1/kbs/{kb}/sources",
        headers=a.headers,
        json={"type": "scripted", "name": "s", "config": {}},
    )
    sid = r.json()["id"]
    scripted.items = [_item("one", "walrus tusks"), _item("two", "narwhal horns")]
    assert (await _sync(client, a, sid))["stats"]["added"] == 2

    scripted.items, scripted.crash = [_item("one", "walrus tusks")], True
    run = await _sync(client, a, sid)
    assert (
        run["status"] == "failed"
        and run["error"] == "Sync failed: The source API returned an error."
    )
    assert run["stats"]["deleted"] == 0
    assert (await _docs(a, sid))["two"].status == "ready"
    assert (await client.get(f"/v1/sources/{sid}", headers=a.headers)).json()["status"] == "error"


async def test_full_sync_deletion_removes_qdrant_points(
    client: httpx.AsyncClient, scripted: ScriptedConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H5: Qdrant points do not cascade with chunks; a full run's deletion must remove them."""
    mem = QdrantClient(":memory:")
    monkeypatch.setenv("VECTOR_STORE", "qdrant")
    monkeypatch.setattr(qdrant_store, "qdrant_client", lambda: mem)
    get_vector_store_settings.cache_clear()
    try:
        a = await register(client)
        kb = await _kb(client, a)
        r = await client.post(
            f"/v1/kbs/{kb}/sources",
            headers=a.headers,
            json={"type": "scripted", "name": "s", "config": {}},
        )
        sid = r.json()["id"]
        scripted.items = [_item("one", "walrus tusks"), _item("two", "narwhal horns")]
        assert (await _sync(client, a, sid))["stats"]["added"] == 2
        docs = await _docs(a, sid)

        def points(doc: str) -> int:
            flt = Filter(must=[qdrant_store._eq("document_id", docs[doc].id)])
            return sum(
                mem.count(c.name, count_filter=flt, exact=True).count
                for c in mem.get_collections().collections
            )

        assert points("one") > 0 and points("two") > 0
        scripted.items = [_item("one", "walrus tusks")]
        assert (await _sync(client, a, sid))["stats"]["deleted"] == 1
        assert points("two") == 0 and points("one") > 0
    finally:
        get_vector_store_settings.cache_clear()


async def test_secret_is_encrypted_and_never_returned_or_logged(
    client: httpx.AsyncClient, scripted: ScriptedConnector, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "sk-live-" + uuid.uuid4().hex
    a = await register(client)
    kb = await _kb(client, a)
    r = await client.post(
        f"/v1/kbs/{kb}/sources",
        headers=a.headers,
        json={"type": "scripted", "name": "s", "config": {"x": 1}, "secret": secret},
    )
    assert r.status_code == 201 and r.json()["has_secret"] is True
    sid = r.json()["id"]
    scripted.items = [_item("k", "kiwi")]
    bodies = [r.text]
    bodies.append((await client.get(f"/v1/sources/{sid}", headers=a.headers)).text)
    bodies.append((await client.get(f"/v1/kbs/{kb}/sources", headers=a.headers)).text)
    bodies.append(
        (await client.patch(f"/v1/sources/{sid}", json={"name": "n"}, headers=a.headers)).text
    )
    bodies.append((await client.post(f"/v1/sources/{sid}/test", headers=a.headers)).text)
    bodies.append(str(await _sync(client, a, sid)))
    assert scripted.secrets == [secret]  # the connector did get it, decrypted
    assert all(secret not in b for b in bodies)
    assert secret not in caplog.text
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        enc = await s.scalar(text("SELECT secret_enc FROM sources WHERE id = :i"), {"i": sid})
        cfg = await s.scalar(text("SELECT config::text FROM sources WHERE id = :i"), {"i": sid})
    assert secret.encode() not in enc and secret not in cfg and decrypt(enc) == secret

    website = await client.post(
        f"/v1/kbs/{kb}/sources",
        headers=a.headers,
        json={
            "type": "website",
            "name": "w",
            "secret": "x",
            "config": {"start_urls": ["https://example.com/"]},
        },
    )
    assert website.status_code == 422 and website.json()["error"]["code"] == "secret_not_allowed"


async def test_one_running_sync_per_source(client: httpx.AsyncClient, site: Site) -> None:
    build_site(site)  # create runs the connector test against the start URL
    a = await register(client)
    sid = await _source(client, a, await _kb(client, a), site_config(site))
    assert (await client.post(f"/v1/sources/{sid}/sync", headers=a.headers)).status_code == 202
    r = await client.post(f"/v1/sources/{sid}/sync", headers=a.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "sync_running"


async def test_delete_source_removes_documents_chunks_vectors(
    client: httpx.AsyncClient, scripted: ScriptedConnector
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    r = await client.post(
        f"/v1/kbs/{kb}/sources",
        headers=a.headers,
        json={"type": "scripted", "name": "s", "config": {}},
    )
    sid = r.json()["id"]
    scripted.items = [_item("p", "pelican pouches")]
    await _sync(client, a, sid)
    assert await _search(client, a, kb, "pelican")
    assert (await client.delete(f"/v1/sources/{sid}", headers=a.headers)).status_code == 204
    assert await _docs(a, sid) == {}
    assert await _search(client, a, kb, "pelican") == []
    assert (await client.get(f"/v1/sources/{sid}", headers=a.headers)).status_code == 404


async def test_connectors_endpoint_lists_schema(client: httpx.AsyncClient) -> None:
    a = await register(client)
    r = await client.get("/v1/connectors", headers=a.headers)
    web = next(c for c in r.json()["items"] if c["type"] == "website")
    assert web["category"] == "web" and web["auth"] == "none"
    props = web["config_schema"]["properties"]
    assert props["max_depth"]["maximum"] == 5 and props["rate_limit_rps"]["maximum"] == 5
    assert web["config_schema"]["required"] == ["start_urls"]


async def test_invalid_config_is_422(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    r = await client.post(
        f"/v1/kbs/{kb}/sources",
        headers=a.headers,
        json={
            "type": "website",
            "name": "w",
            "config": {"start_urls": ["http://x.com/"], "max_depth": 9},
        },
    )
    assert r.status_code == 422 and "max_depth" in r.json()["error"]["message"]
    r = await client.post(
        f"/v1/kbs/{kb}/sources", headers=a.headers, json={"type": "nope", "name": "w", "config": {}}
    )
    assert r.status_code == 422


async def test_cross_org_source_access_is_404(client: httpx.AsyncClient, site: Site) -> None:
    build_site(site)
    a, b = await register(client), await register(client)
    kb = await _kb(client, a)
    sid = await _source(client, a, kb, site_config(site))
    for method, path in [
        ("GET", f"/v1/sources/{sid}"),
        ("PATCH", f"/v1/sources/{sid}"),
        ("DELETE", f"/v1/sources/{sid}"),
        ("POST", f"/v1/sources/{sid}/sync"),
        ("POST", f"/v1/sources/{sid}/test"),
        ("GET", f"/v1/sources/{sid}/runs"),
        ("GET", f"/v1/kbs/{kb}/sources"),
        ("POST", f"/v1/kbs/{kb}/sources"),
    ]:
        body = {"type": "website", "name": "x", "config": site_config(site)}
        r = await client.request(
            method, path, headers=b.headers, json=body if method != "GET" else None
        )
        assert r.status_code == 404, (method, path, r.status_code)
    assert (await client.get(f"/v1/sources/{sid}", headers=a.headers)).status_code == 200


async def test_create_reports_ssrf_refusal(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    REGISTRY["website"] = WebsiteConnector()  # production fetcher (restored by the env fixture)
    body = {"type": "website", "name": "w", "config": {"start_urls": ["http://localhost/"]}}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    assert r.status_code == 422 and r.json()["error"] == {
        **r.json()["error"],
        "code": "source_test_failed",
        "message": "The host localhost points to a private or reserved address.",
    }


async def test_create_and_patch_run_the_connector_test(
    client: httpx.AsyncClient, scripted: ScriptedConnector
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    body = {"type": "scripted", "name": "s", "config": {"v": 1}}
    scripted.test_error = "The API key was rejected."
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "source_test_failed"
    assert r.json()["error"]["message"] == "The API key was rejected."
    listed = await client.get(f"/v1/kbs/{kb}/sources", headers=a.headers)
    assert listed.json()["items"] == []  # nothing saved

    scripted.test_error = None
    sid = await _scripted_source(client, a, kb, config={"v": 1})
    scripted.test_error = "The API key was rejected."
    for patch in ({"config": {"v": 2}}, {"secret": "new"}):
        r = await client.patch(f"/v1/sources/{sid}", json=patch, headers=a.headers)
        assert r.status_code == 422 and r.json()["error"]["code"] == "source_test_failed", patch
    src = (await client.get(f"/v1/sources/{sid}", headers=a.headers)).json()
    assert src["config"] == {"v": 1} and src["has_secret"] is False  # failed PATCH saved nothing
    r = await client.patch(f"/v1/sources/{sid}", json={"name": "n"}, headers=a.headers)
    assert r.status_code == 200  # no config/secret change: no test needed

    scripted.test_error = None
    r = await client.post(f"/v1/sources/{sid}/test", headers=a.headers)
    assert r.json() == {"ok": True, "message": "ok", "schema": None}


async def test_private_endpoint_needs_the_operator_gate(
    client: httpx.AsyncClient, scripted: ScriptedConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "connectors_allow_private_endpoints", False)
    a = await register(client)
    kb = await _kb(client, a)
    private = {"allow_private_endpoint": True}
    body = {"type": "scripted", "name": "s", "config": private}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "private_endpoints_disabled"
    assert "CONNECTORS_ALLOW_PRIVATE_ENDPOINTS" in r.json()["error"]["message"]
    sid = await _scripted_source(client, a, kb)
    r = await client.patch(f"/v1/sources/{sid}", json={"config": private}, headers=a.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "private_endpoints_disabled"

    monkeypatch.setattr(get_settings(), "connectors_allow_private_endpoints", True)
    assert (
        await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    ).status_code == 201


async def _cursor(a: Account, sid: str) -> Any:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        return await s.scalar(text("SELECT cursor FROM sources WHERE id = :i"), {"i": sid})


async def test_config_patch_and_full_sync_reset_the_cursor(
    client: httpx.AsyncClient, scripted: ScriptedConnector
) -> None:
    a = await register(client)
    sid = await _scripted_source(client, a, await _kb(client, a))
    mark = '{"runs": 3, "watermarks": {"t": "2026-01-01"}}'
    await _sql(a, "UPDATE sources SET cursor = CAST(:c AS jsonb) WHERE id = :i", c=mark, i=sid)
    await client.patch(f"/v1/sources/{sid}", json={"name": "renamed"}, headers=a.headers)
    assert (await _cursor(a, sid))["runs"] == 3  # name change keeps the watermark
    r = await client.patch(f"/v1/sources/{sid}", json={"config": {"new": 1}}, headers=a.headers)
    assert r.status_code == 200 and await _cursor(a, sid) == {}

    await _sql(a, "UPDATE sources SET cursor = CAST(:c AS jsonb) WHERE id = :i", c=mark, i=sid)
    await _sync(client, a, sid)
    assert (await _cursor(a, sid))["runs"] == 3  # default sync keeps it
    await _sync(client, a, sid, full=True)
    assert await _cursor(a, sid) == {}  # full: discover starts from an empty cursor

    await _sql(a, "UPDATE sources SET status = 'syncing' WHERE id = :i", i=sid)
    r = await client.patch(f"/v1/sources/{sid}", json={"config": {"x": 2}}, headers=a.headers)
    assert r.status_code == 409  # the running sync would save its stale cursor over the reset


async def test_sync_runs_in_real_worker_actor(
    client: httpx.AsyncClient, site: Site, env: StubBroker
) -> None:
    from dramatiq import Worker

    build_site(site)
    a = await register(client)
    sid = await _source(client, a, await _kb(client, a), site_config(site, max_depth=0))
    run = (await client.post(f"/v1/sources/{sid}/sync", headers=a.headers)).json()
    worker = Worker(env, queues={"sync"}, worker_timeout=100)
    worker.start()
    try:
        await asyncio.to_thread(env.join, "sync", fail_fast=True)
    finally:
        worker.stop()
    runs = (await client.get(f"/v1/sources/{sid}/runs", headers=a.headers)).json()["items"]
    assert runs[0]["id"] == run["id"] and runs[0]["status"] == "succeeded", runs[0]
    assert runs[0]["stats"]["added"] == 2


@pytest.mark.parametrize(
    "module",
    [
        "app.ingestion.pipeline",
        "app.connectors",
        "app.connectors.sync",
        "app.retrieval.indexing",
        "app.connectors.router",
        "app.worker",
    ],
)
def test_module_imports_alone_in_a_fresh_interpreter(module: str) -> None:
    """Regression: importing pipeline first cycled pipeline -> retrieval/__init__ -> tasks ->
    app.worker -> ingestion.tasks -> pipeline (partially initialized)."""
    import subprocess
    import sys

    r = subprocess.run(  # noqa: S603  fixed module list
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]


def test_page_title_is_the_first_title_only() -> None:
    from app.connectors.website import _PageParser

    p = _PageParser()
    p.feed("<head><title>Docs</title></head><body><svg><title>icon</title></svg></body>")
    assert p.title == "Docs"


# ---------------------------------------------------------------- scheduler tick (B4.1)


async def test_target_change_without_secret_is_refused(
    client: httpx.AsyncClient, scripted: ScriptedConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1: test() must never send the stored secret to a target the editor just picked."""
    a = await register(client)
    sid = await _scripted_source(client, a, await _kb(client, a), secret="stored")
    scripted.tested.clear()
    patches = [{"config": {"endpoint": "https://elsewhere.example"}}]
    monkeypatch.delattr(ScriptedConnector, "target_fields")  # undeclared: every change counts
    patches.append({"config": {"other": 1}})
    for patch in patches:
        r = await client.patch(f"/v1/sources/{sid}", json=patch, headers=a.headers)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "secret_required_for_target_change"
    assert scripted.tested == []  # test() never ran
    body = {**patches[1], "secret": "typed"}
    assert (await client.patch(f"/v1/sources/{sid}", json=body, headers=a.headers)).is_success
    assert scripted.tested == ["typed"]


async def _scripted_source(client: httpx.AsyncClient, a: Account, kb: str, **kw: Any) -> str:
    body = {"type": "scripted", "name": "s", "config": {}, **kw}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _sql(a: Account, sql: str, **params: Any) -> None:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        await s.execute(text(sql), params)
        await s.commit()


async def _tick(a: Account) -> list[str]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, None)
        return [str(i) for i in await sync_mod.start_due_syncs(s, a.org_id)]


async def _status(client: httpx.AsyncClient, a: Account, sid: str) -> str:
    return str((await client.get(f"/v1/sources/{sid}", headers=a.headers)).json()["status"])


async def test_tick_starts_only_due_enabled_sources(
    client: httpx.AsyncClient, scripted: ScriptedConnector, env: StubBroker
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    due, fresh, manual, off, broken = [
        await _scripted_source(client, a, kb, **kw)
        for kw in ({"schedule_minutes": 15}, {"schedule_minutes": 15}, {}, {"schedule_minutes": 15},
                   {"schedule_minutes": 15})
    ]  # fmt: skip
    await _sql(a, "UPDATE sources SET created_at = now() - interval '20 minutes' WHERE id <> :f",
               f=fresh)  # fmt: skip
    await _sql(a, "UPDATE sources SET status = 'disabled' WHERE id = :i", i=off)
    await _sql(a, "UPDATE sources SET status = 'error' WHERE id = :i", i=broken)
    assert await _tick(a) == [due]
    assert env.queues["sync"].qsize() == 1 and await _status(client, a, due) == "syncing"
    assert await _tick(a) == []  # already claimed: not enqueued twice

    scripted.items = [_item("one", "otter")]
    run = (await client.get(f"/v1/sources/{due}/runs", headers=a.headers)).json()["items"][0]
    await sync_mod.run_sync(uuid.UUID(run["id"]), a.org_id)
    assert await _status(client, a, due) == "idle"
    assert await _tick(a) == []  # last_sync_at is now: next one in 15 min
    await _sql(a, "UPDATE sources SET last_sync_at = now() - interval '20 minutes' WHERE id = :i",
               i=due)  # fmt: skip
    assert await _tick(a) == [due]
    assert manual not in await _tick(a)


async def test_two_schedulers_claim_a_due_source_once(
    client: httpx.AsyncClient, scripted: ScriptedConnector, env: StubBroker
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    sids = [await _scripted_source(client, a, kb, schedule_minutes=15) for _ in range(3)]
    await _sql(a, "UPDATE sources SET created_at = now() - interval '1 hour'")
    results = await asyncio.gather(*(_tick(a) for _ in range(4)))
    assert sorted(i for r in results for i in r) == sorted(sids)
    assert env.queues["sync"].qsize() == 3


async def test_tick_skips_a_source_with_a_manual_sync_in_flight(
    client: httpx.AsyncClient, scripted: ScriptedConnector, env: StubBroker
) -> None:
    a = await register(client)
    sid = await _scripted_source(client, a, await _kb(client, a), schedule_minutes=15)
    assert (await client.post(f"/v1/sources/{sid}/sync", headers=a.headers)).status_code == 202
    await _sql(a, "UPDATE sources SET created_at = now() - interval '1 hour'")
    assert await _tick(a) == []
    assert env.queues["sync"].qsize() == 1 and await _status(client, a, sid) == "idle"


async def test_tick_reaps_a_dead_run_and_frees_the_source(
    client: httpx.AsyncClient, scripted: ScriptedConnector
) -> None:
    a = await register(client)
    sid = await _scripted_source(client, a, await _kb(client, a))
    run = (await client.post(f"/v1/sources/{sid}/sync", headers=a.headers)).json()
    await _sql(a, "UPDATE sources SET status = 'syncing' WHERE id = :i", i=sid)  # worker started
    await _sql(a, "UPDATE sync_runs SET started_at = now() - interval '3 hours' WHERE id = :i",
               i=run["id"])  # fmt: skip
    await _tick(a)
    assert await _status(client, a, sid) == "syncing"  # within the time limit: still alive
    await _sql(a, "UPDATE sync_runs SET started_at = now() - interval '5 hours' WHERE id = :i",
               i=run["id"])  # fmt: skip
    await _tick(a)  # worker killed: no POST needed to clean up
    runs = (await client.get(f"/v1/sources/{sid}/runs", headers=a.headers)).json()["items"]
    assert runs[0]["status"] == "failed" and "stopped" in runs[0]["error"]
    assert await _status(client, a, sid) == "idle"
    assert (await client.post(f"/v1/sources/{sid}/sync", headers=a.headers)).status_code == 202


# ---------------------------------------------------------------- cross-org scheduler (B4.2)


async def _due_orgs() -> set[uuid.UUID]:
    async with SessionLocal() as s:  # rag_app, no tenant
        q = text("SELECT due_sync_orgs(:stale)")
        return set(await s.scalars(q, {"stale": sync_mod.STALE_AFTER}))


async def _org_with(client: httpx.AsyncClient, *kws: dict[str, Any]) -> tuple[Account, list[str]]:
    a = await register(client)
    kb = await _kb(client, a)
    sids = [await _scripted_source(client, a, kb, **kw) for kw in kws]
    await _sql(a, "UPDATE sources SET created_at = now() - interval '1 hour'")
    return a, sids


async def test_due_sync_orgs_returns_only_due_or_stale_orgs(
    client: httpx.AsyncClient, scripted: ScriptedConnector
) -> None:
    due, _ = await _org_with(client, {"schedule_minutes": 15})
    fresh, _ = await _org_with(client, {"schedule_minutes": 120})
    unscheduled, _ = await _org_with(client, {})
    disabled, (d,) = await _org_with(client, {"schedule_minutes": 15})
    await _sql(disabled, "UPDATE sources SET status = 'disabled' WHERE id = :i", i=d)
    errored, (e,) = await _org_with(client, {"schedule_minutes": 15})
    await _sql(errored, "UPDATE sources SET status = 'error' WHERE id = :i", i=e)
    stale, (s,) = await _org_with(client, {})
    run = (await client.post(f"/v1/sources/{s}/sync", headers=stale.headers)).json()
    await _sql(stale, "UPDATE sync_runs SET started_at = now() - interval '5 hours' WHERE id = :i",
               i=run["id"])  # fmt: skip
    orgs = await _due_orgs()
    assert due.org_id in orgs and stale.org_id in orgs
    for a in (fresh, unscheduled, disabled, errored):
        assert a.org_id not in orgs
    async with SessionLocal() as sess:  # RLS unchanged: the function is the only cross-org view
        assert await sess.scalar(text("SELECT count(*) FROM sources")) == 0
        assert await sess.scalar(text("SELECT count(*) FROM sync_runs")) == 0


async def test_due_sync_orgs_execute_is_granted_to_rag_app_only() -> None:
    async with SessionLocal() as s:
        rows = await s.execute(text(
            "SELECT p.prosecdef, p.proconfig, coalesce(r.rolname, 'PUBLIC'), a.privilege_type "
            "FROM pg_proc p, aclexplode(p.proacl) a LEFT JOIN pg_roles r ON r.oid = a.grantee "
            "WHERE p.proname = 'due_sync_orgs'"
        ))  # fmt: skip
        acl = rows.all()
        owner = await s.scalar(text(
            "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = 'due_sync_orgs'"
        ))  # fmt: skip
    grants = {(r[2], r[3]) for r in acl}
    assert acl and all(r[0] for r in acl)  # SECURITY DEFINER
    assert "search_path=pg_catalog, public" in acl[0][1]
    assert grants == {("rag_app", "EXECUTE"), (owner, "EXECUTE")}  # no PUBLIC entry


async def test_scheduler_tick_spans_orgs_and_survives_a_failing_org(
    client: httpx.AsyncClient, scripted: ScriptedConnector, env: StubBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    a, (a1, _a_manual) = await _org_with(client, {"schedule_minutes": 15}, {})
    b, (b1, b2) = await _org_with(client, {"schedule_minutes": 15}, {"schedule_minutes": 30})
    started = await scheduler.tick()
    assert sorted(map(str, started[a.org_id])) == [a1]
    assert sorted(map(str, started[b.org_id])) == sorted([b1, b2])
    orgs = [Message.decode(m).args[1] for m in env.queues["sync"].queue]
    assert sum(o in (str(a.org_id), str(b.org_id)) for o in orgs) == 3
    assert not ({a.org_id, b.org_id} & set(await scheduler.tick()))  # claimed: nothing more due

    c, _ = await _org_with(client, {"schedule_minutes": 15})
    d, (d1,) = await _org_with(client, {"schedule_minutes": 15})
    real = sync_mod.start_due_syncs

    async def flaky(session: Any, org_id: uuid.UUID) -> list[uuid.UUID]:
        if org_id == c.org_id:
            raise RuntimeError("boom")
        return await real(session, org_id)

    monkeypatch.setattr(sync_mod, "start_due_syncs", flaky)
    started = await scheduler.tick()
    assert c.org_id not in started and [str(i) for i in started[d.org_id]] == [d1]


def test_migration_0005_downgrade_and_upgrade() -> None:
    async def exists() -> bool:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        async with engine.connect() as conn:
            q = text("SELECT count(*) FROM pg_proc WHERE proname = 'due_sync_orgs'")
            found = bool(await conn.scalar(q))
        await engine.dispose()
        return found

    cfg = AlembicConfig("alembic.ini")
    try:
        command.downgrade(cfg, "0004")
        assert not asyncio.run(exists())
    finally:
        command.upgrade(cfg, "head")
    assert asyncio.run(exists())


async def test_due_sync_orgs_floors_stale_after_server_side(
    client: httpx.AsyncClient, scripted: ScriptedConnector
) -> None:
    """rag_app passing interval '0' must not list every org with a running sync (0012)."""
    fresh, (f,) = await _org_with(client, {})
    old, (o,) = await _org_with(client, {})
    for a, sid, age in ((fresh, f, timedelta(minutes=30)), (old, o, timedelta(hours=2))):
        run = (await client.post(f"/v1/sources/{sid}/sync", headers=a.headers)).json()
        sql = "UPDATE sync_runs SET started_at = now() - CAST(:age AS interval) WHERE id = :i"
        await _sql(a, sql, age=age, i=run["id"])
    async with SessionLocal() as s:
        orgs = set(await s.scalars(text("SELECT due_sync_orgs(interval '0')")))
    assert fresh.org_id not in orgs and old.org_id in orgs

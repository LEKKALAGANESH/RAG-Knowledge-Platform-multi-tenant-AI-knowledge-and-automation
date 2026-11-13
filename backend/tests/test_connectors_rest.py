"""Generic REST connector (C2): auth, pagination, SSRF, retries, caps, secrets, OpenAPI import.

The API is an httpx MockTransport routed by Host header (requests go to the pinned IP); DNS is a
fake resolver, and the real SSRF ip checks decide what is reachable.
"""

import base64
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.connectors import rest
from app.connectors.base import Config, ConnectorError, Item
from app.connectors.registry import REGISTRY
from app.connectors.rest import RestConfig, RestConnector, RestHttp, extract
from app.connectors.ssrf import endpoint_check
from app.core.config import get_settings
from tests.conftest import register
from tests.test_connectors import _docs, _kb, _sync, env  # noqa: F401

IPS = {
    "api.test": "93.184.216.34",
    "auth.test": "93.184.216.35",
    "other.test": "93.184.216.36",
    "lan.test": "10.0.0.7",
    "meta.test": "169.254.169.254",
}
Handler = Callable[[httpx.Request], httpx.Response]


async def resolve(host: str, port: int) -> list[str]:
    if host not in IPS:
        raise OSError("no such host")
    return [IPS[host]]


class Api:
    """Routes by `Host` + path and records every request."""

    def __init__(self) -> None:
        self.routes: dict[str, Handler] = {}
        self.seen: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        fn = self.routes.get(req.headers["host"] + req.url.path)
        return fn(req) if fn else js({"error": "nope"}, 404)

    def hits(self, host: str) -> list[httpx.Request]:
        return [r for r in self.seen if r.headers["host"] == host]


def js(obj: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    # A stream (not content=): read_capped reads raw chunks, as from the network.
    return httpx.Response(
        status, headers=headers, stream=httpx.ByteStream(json.dumps(obj).encode())
    )


def connector(api: Api, max_bytes: int = 2**20) -> RestConnector:
    def make(cfg: RestConfig) -> RestHttp:
        h = RestHttp(
            allow_nonstandard_ports=cfg.allow_nonstandard_ports,
            resolver=resolve,
            ip_check=endpoint_check(cfg.allow_private_endpoint),
            max_bytes=max_bytes,
        )
        h.client = httpx.AsyncClient(transport=httpx.MockTransport(api), follow_redirects=False)
        return h

    return RestConnector(http=make)


def config(**over: Any) -> Config:
    ep = {
        "path": "/items",
        "items_path": "data[*]",
        "id_field": "id",
        "title_field": "title",
        "text_fields": ["body"],
    }
    return {"base_url": "https://api.test/v1", "endpoints": [ep], **over}


def recs(n: int, start: int = 0) -> list[dict[str, Any]]:
    return [{"id": i, "title": f"T{i}", "body": f"body {i}"} for i in range(start, start + n)]


async def run(c: RestConnector, cfg: Config, secret: str | None = None, **cur: Any) -> list[Item]:
    return [i async for i in c.discover(c.validate_config(cfg), secret, cur)]


@pytest.fixture(autouse=True)
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(rest, "_sleep", fake_sleep)
    rest._TOKENS.clear()
    return slept


# ---------------------------------------------------------------- records


@pytest.mark.parametrize(
    ("body", "path", "ids"),
    [
        ({"data": [{"id": 1}, {"id": 2}]}, "data[*]", [1, 2]),
        ({"data": [{"id": 1}]}, "data", [1]),
        ({"data": [{"id": 1}]}, "$.data", [1]),
        ([{"id": 1}, {"id": 2}], "", [1, 2]),
        ({"id": 9}, "", [9]),
        ({"a": {"b": [{"n": {"id": 3}}, {"n": {"id": 4}}]}}, "a.b[*].n", [3, 4]),
        ({"a": [{"b": [{"id": 5}]}, {"b": [{"id": 6}]}]}, "a[*].b[*]", [5, 6]),
        ({"data": "not a list"}, "data[*]", []),
        ({"x": 1}, "data[*]", []),
    ],
)
def test_items_path_extraction(body: Any, path: str, ids: list[int]) -> None:
    assert [r["id"] for r in extract(body, path)] == ids


async def test_records_become_markdown_docs_with_stable_keys() -> None:
    api = Api()
    rows = [*recs(2), {"title": "no id"}, "not a record", {"id": True}, {"id": 1, "body": "dup"}]
    api.routes["api.test/v1/items"] = lambda r: js({"data": rows})
    items = await run(connector(api), config())
    assert [i.external_key for i in items] == ["/items#0", "/items#1"]
    first = items[0]
    assert first.name == "T0" and first.mime_hint == "text/markdown"
    assert first.meta == {"record": "0", "endpoint": "/items"}
    assert await first.fetch() == b"# T0\n\n## body\n\nbody 0"
    again = await run(connector(api), config())
    assert again[0].content_hash == first.content_hash  # same text, same hash: no re-ingest


# ---------------------------------------------------------------- auth kinds


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


AUTH_CASES = [
    ({"kind": "none"}, None, lambda r: "authorization" not in r.headers),
    ({"kind": "none"}, "ignored-stale", lambda r: "authorization" not in r.headers),
    (
        {"kind": "api_key", "api_key_name": "X-Key"},
        "k-123",
        lambda r: r.headers["x-key"] == "k-123",
    ),
    (
        {"kind": "api_key", "api_key_in": "query", "api_key_name": "appid"},
        "k-123",
        lambda r: r.url.params["appid"] == "k-123",
    ),
    ({"kind": "bearer"}, "t-123", lambda r: r.headers["authorization"] == "Bearer t-123"),
    (
        {"kind": "basic"},
        '{"username": "u", "password": "p:w"}',
        lambda r: r.headers["authorization"] == "Basic " + _b64("u:p:w"),
    ),
]


@pytest.mark.parametrize(("auth", "secret", "check"), AUTH_CASES)
async def test_auth_kinds(
    auth: Config, secret: str | None, check: Callable[[httpx.Request], bool]
) -> None:
    api = Api()
    api.routes["api.test/v1/items"] = lambda r: js({"data": recs(1)})
    c = connector(api)
    cfg = c.validate_config(config(auth=auth))
    assert "123" not in json.dumps(cfg)  # credentials live only in the secret
    assert "1 records" in await c.test(cfg, secret)
    assert len(await run(c, cfg, secret)) == 1
    assert all(check(r) for r in api.seen)


@pytest.mark.parametrize(
    ("auth", "secret"),
    [
        ({"kind": "bearer"}, None),
        ({"kind": "bearer"}, "two\nlines"),
        ({"kind": "basic"}, "user:pass-not-json"),
        ({"kind": "oauth2", "token_url": "https://auth.test/token"}, '{"client_id": "x"}'),
    ],
)
async def test_bad_credentials_are_refused_without_echoing_them(auth: Config, secret: str) -> None:
    with pytest.raises(ConnectorError) as exc:
        await connector(Api()).test(config(auth=auth), secret)
    assert secret is None or secret not in str(exc.value)


async def test_oauth2_client_credentials_token_is_cached_and_renewed_on_401() -> None:
    api, tokens, issued = Api(), [], iter(["tok-1", "tok-2"])

    def token(r: httpx.Request) -> httpx.Response:
        tokens.append(r)
        return js({"access_token": next(issued), "token_type": "bearer", "expires_in": 3600})

    def items(r: httpx.Request) -> httpx.Response:
        if r.headers["authorization"] == "Bearer tok-1" and len(api.hits("api.test")) > 2:
            return js({"error": "revoked"}, 401)
        return js({"data": recs(1)})

    api.routes["auth.test/token"], api.routes["api.test/v1/items"] = token, items
    auth = {"kind": "oauth2", "token_url": "https://auth.test/token", "scope": "read"}
    secret = '{"client_id": "cid", "client_secret": "csec-123"}'
    c = connector(api)
    await run(c, config(auth=auth), secret)
    await run(c, config(auth=auth), secret)
    assert len(tokens) == 1  # cached across runs
    t = tokens[0]
    assert t.method == "POST" and t.headers["authorization"] == "Basic " + _b64("cid:csec-123")
    assert dict(httpx.QueryParams(t.content.decode())) == {
        "grant_type": "client_credentials",
        "scope": "read",
    }
    assert len(await run(c, config(auth=auth), secret)) == 1  # 401 -> new token -> retried
    assert len(tokens) == 2 and api.seen[-1].headers["authorization"] == "Bearer tok-2"


async def test_oauth2_token_url_is_ssrf_checked() -> None:
    auth = {"kind": "oauth2", "token_url": "https://meta.test/token"}
    with pytest.raises(ConnectorError, match="private or reserved"):
        await connector(Api()).test(config(auth=auth), '{"client_id": "a", "client_secret": "b"}')


# ---------------------------------------------------------------- pagination


def paged(api: Api, total: int, key: Callable[[httpx.Request], int], size: int = 100) -> None:
    def h(r: httpx.Request) -> httpx.Response:
        start = key(r)
        return js({"data": recs(max(0, min(size, total - start)), start)})

    api.routes["api.test/v1/items"] = h


async def test_page_pagination() -> None:
    api = Api()
    paged(api, 250, lambda r: (int(r.url.params["page"]) - 1) * 100)
    items = await run(connector(api), config(pagination={"type": "page", "page_size": 100}))
    assert len(items) == 250
    assert [(r.url.params["page"], r.url.params["limit"]) for r in api.seen] == [
        ("1", "100"),
        ("2", "100"),
        ("3", "100"),
    ]


async def test_offset_pagination_and_max_pages_cap() -> None:
    api = Api()
    paged(api, 10_000, lambda r: int(r.url.params["offset"]))
    p = {"type": "offset", "size_param": "n", "page_size": 100, "max_pages": 3}
    items = await run(connector(api), config(pagination=p))
    assert len(items) == 300  # capped at max_pages, no error
    assert [r.url.params["offset"] for r in api.seen] == ["0", "100", "200"]


async def test_cursor_pagination_with_token_and_same_origin_url() -> None:
    api = Api()

    def h(r: httpx.Request) -> httpx.Response:
        after = r.url.params.get("after")
        if after is None and "c" not in r.url.params:
            return js({"data": recs(2), "meta": {"next": "tok-b"}})
        if r.url.params.get("c") == "tok-b":
            return js({"data": recs(2, 2), "meta": {"next": "/v1/items?after=4"}})
        return js({"data": recs(1, 4), "meta": {"next": None}})

    api.routes["api.test/v1/items"] = h
    p = {"type": "cursor", "cursor_param": "c", "cursor_path": "meta.next"}
    items = await run(connector(api), config(pagination=p))
    assert [i.external_key for i in items] == [f"/items#{i}" for i in range(5)]


async def test_link_header_pagination() -> None:
    api = Api()

    def h(r: httpx.Request) -> httpx.Response:
        n = int(r.url.params.get("p", "1"))
        link = f'<https://api.test/v1/items?p={n + 1}>; rel="next", <x>; rel="last"'
        return js({"data": recs(1, n)}, headers={"Link": link} if n < 3 else None)

    api.routes["api.test/v1/items"] = h
    items = await run(connector(api), config(pagination={"type": "link_header"}))
    assert len(items) == 3 and len(api.seen) == 3


@pytest.mark.parametrize(
    ("p", "link", "body_next"),
    [
        ({"type": "link_header"}, "<https://other.test/v1/items?p=2>; rel=next", None),
        ({"type": "link_header"}, "<http://api.test/v1/items?p=2>; rel=next", None),  # scheme
        ({"type": "link_header"}, "<https://api.test:8443/v1/items>; rel=next", None),  # port
        ({"type": "cursor"}, None, "https://other.test/steal"),
        ({"type": "cursor"}, None, "//other.test/steal"),
    ],
)
async def test_cross_origin_next_link_is_refused(
    p: Config, link: str | None, body_next: str | None
) -> None:
    api = Api()
    api.routes["api.test/v1/items"] = lambda r: js(
        {"data": recs(1), "next_cursor": body_next}, headers={"Link": link} if link else None
    )
    with pytest.raises(ConnectorError, match="another origin"):
        await run(connector(api), config(pagination=p))
    assert api.hits("other.test") == [] and len(api.seen) == 1


@pytest.mark.parametrize(
    ("p", "handler"),
    [
        (  # next link points back at the same page
            {"type": "link_header"},
            lambda r: js({"data": recs(1)}, headers={"Link": f"<{r.url.path}>; rel=next"}),
        ),
        (  # cursor that never changes
            {"type": "cursor"},
            lambda r: js({"data": recs(1, int(r.url.params.get("cursor", 0))), "next_cursor": 7}),
        ),
        (  # server ignores the page param: same full page forever
            {"type": "page", "page_size": 2},
            lambda r: js({"data": recs(2)}),
        ),
    ],
)
async def test_pagination_loops_fail_the_run(p: Config, handler: Handler) -> None:
    api = Api()
    api.routes["api.test/v1/items"] = handler
    with pytest.raises(ConnectorError, match=r"loop|does not advance"):
        await run(connector(api), config(pagination={**p, "max_pages": 100}))
    assert len(api.seen) <= 3


# ---------------------------------------------------------------- SSRF, retries, caps


@pytest.mark.parametrize(
    "base",
    ["https://lan.test/v1", "https://meta.test/", "https://169.254.169.254/", "https://10.1.2.3/"],
)
async def test_private_and_metadata_hosts_are_refused(base: str) -> None:
    api = Api()
    with pytest.raises(ConnectorError, match="private or reserved"):
        await connector(api).test(config(base_url=base), None)
    assert api.seen == []


async def test_private_endpoint_needs_the_gate_and_never_reaches_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = Api()
    api.routes["lan.test/v1/items"] = lambda r: js({"data": recs(1)})
    lan = config(base_url="http://lan.test/v1", allow_private_endpoint=True)
    with pytest.raises(ConnectorError, match="Private endpoints are disabled"):
        await connector(api).test(lan, None)
    monkeypatch.setattr(get_settings(), "connectors_allow_private_endpoints", True)
    assert "1 records" in await connector(api).test(lan, None)
    meta = config(base_url="http://meta.test/", allow_private_endpoint=True)
    with pytest.raises(ConnectorError, match="private or reserved"):
        await connector(api).test(meta, None)


@pytest.mark.parametrize(
    "over",
    [
        {"base_url": "http://api.test/v1"},  # https only without the private opt-in
        {"base_url": "https://user:pw@api.test/"},
        {"base_url": "https://api.test/v1?x=1"},
        {"base_url": "file:///etc/passwd"},
        {"headers": {"Authorization": "Bearer x"}},
        {"headers": {"cookie": "a=b"}},
        {"headers": {"X-A": "line\r\nInjected: 1"}},
        {"auth": {"kind": "oauth2"}},  # no token_url
        {"auth": {"kind": "oauth2", "token_url": "http://auth.test/t"}},
        {"auth": {"kind": "api_key", "api_key_name": "X-K"}, "headers": {"x-k": "v"}},
        {"endpoints": [{"path": "//other.test/x", "id_field": "id", "text_fields": ["a"]}]},
        {"endpoints": [{"path": "/a/../b", "id_field": "id", "text_fields": ["a"]}]},
        {"endpoints": [{"path": "/a?b=1", "id_field": "id", "text_fields": ["a"]}]},
        {"endpoints": [{"path": "/a", "method": "POST", "id_field": "id", "text_fields": ["a"]}]},
        {"endpoints": [{"path": "/a", "id_field": "x[*]", "text_fields": ["a"]}]},
        {
            "endpoints": [
                {"path": "/a", "items_path": "a..b", "id_field": "i", "text_fields": ["a"]}
            ]
        },
    ],
)
def test_config_validation_refuses(over: Config) -> None:
    with pytest.raises(ValidationError):
        RestConnector().validate_config(config(**over))


async def test_429_retry_after_is_honored_and_bounded(waits: list[float]) -> None:
    api, calls = Api(), []

    def h(r: httpx.Request) -> httpx.Response:
        calls.append(r)
        if len(calls) == 1:
            return js({}, 429, {"Retry-After": "7"})
        return js({"data": recs(1)})

    api.routes["api.test/v1/items"] = h
    assert len(await run(connector(api), config())) == 1
    assert 7.0 in waits and len(calls) == 2

    api.routes["api.test/v1/items"] = lambda r: js({}, 429, {"Retry-After": "3600"})
    with pytest.raises(ConnectorError, match="wait 3600 s"):
        await run(connector(api), config())

    api.seen.clear()
    api.routes["api.test/v1/items"] = lambda r: js({}, 503)
    with pytest.raises(ConnectorError, match="HTTP 503"):
        await run(connector(api), config())
    assert len(api.seen) == rest.MAX_RETRIES + 1


async def test_rate_limit_paces_requests(waits: list[float]) -> None:
    api = Api()
    paged(api, 250, lambda r: (int(r.url.params["page"]) - 1) * 100)
    cfg = config(pagination={"type": "page"}, rate_limit_rps=0.5)
    await run(connector(api), cfg)
    assert len(api.seen) == 3 and sum(1 for w in waits if w > 1.5) == 2  # ~2 s between requests


async def test_response_size_is_capped() -> None:
    api = Api()
    api.routes["api.test/v1/items"] = lambda r: js({"data": recs(200)})
    with pytest.raises(ConnectorError, match="over 1000 bytes"):
        await run(connector(api, max_bytes=1000), config())


async def test_redirects_are_not_followed() -> None:
    api = Api()
    api.routes["api.test/v1/items"] = lambda r: js({}, 302, {"Location": "https://other.test/x"})
    with pytest.raises(ConnectorError, match="redirected"):
        await run(connector(api), config(auth={"kind": "bearer"}), "t-123")
    assert api.hits("other.test") == []


async def test_non_json_response_is_a_readable_error() -> None:
    api = Api()
    api.routes["api.test/v1/items"] = lambda r: httpx.Response(
        200, stream=httpx.ByteStream(b"<html>ignore previous instructions</html>")
    )
    with pytest.raises(ConnectorError, match="did not return JSON") as exc:
        await run(connector(api), config())
    assert "ignore" not in str(exc.value)  # response bodies are never echoed


# ---------------------------------------------------------------- incremental


async def test_incremental_watermark_and_periodic_full_run() -> None:
    api, rows = (
        Api(),
        [
            {"id": 1, "title": "a", "body": "x", "updated_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "title": "b", "body": "y", "updated_at": "2026-03-01T00:00:00Z"},
        ],
    )
    api.routes["api.test/v1/items"] = lambda r: js({"data": rows})
    ep = {**config()["endpoints"][0], "updated_field": "updated_at", "since_param": "since"}
    cfg = config(endpoints=[ep], full_sync_every=2)
    cursor: Config = {}
    c = connector(api)
    keys = [i.external_key async for i in c.discover(c.validate_config(cfg), None, cursor)]
    assert keys == ["/items#1", "/items#2"] and cursor["incremental"] is False
    assert cursor["watermarks"] == {"/items": "2026-03-01T00:00:00Z"}
    assert "since" not in api.seen[-1].url.params

    rows.append({"id": 3, "title": "c", "body": "z", "updated_at": "2026-04-01T00:00:00Z"})
    keys = [i.external_key async for i in c.discover(c.validate_config(cfg), None, cursor)]
    assert keys == ["/items#2", "/items#3"]  # older record skipped; equal mark re-checked
    assert api.seen[-1].url.params["since"] == "2026-03-01T00:00:00Z"
    assert cursor["incremental"] is True and cursor["runs"] == 2

    keys = [i.external_key async for i in c.discover(c.validate_config(cfg), None, cursor)]
    assert len(keys) == 3 and cursor["incremental"] is False  # 3rd run: full, deletions apply


async def test_max_items_cap() -> None:
    api = Api()
    api.routes["api.test/v1/items"] = lambda r: js({"data": recs(50)})
    eps = [config()["endpoints"][0], {**config()["endpoints"][0], "path": "/other"}]
    assert len(await run(connector(api), config(endpoints=eps, max_items=10))) == 10
    assert len(api.seen) == 1  # the second endpoint is never requested


# ---------------------------------------------------------------- API level


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> Api:
    api = Api()
    monkeypatch.setitem(REGISTRY, "rest", connector(api))
    return api


async def test_secret_never_in_responses_or_logs(
    client: httpx.AsyncClient, mock_api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    """AC11, with the key in the query string (httpx logs every URL at INFO)."""
    caplog.set_level(logging.DEBUG)
    secret = "qk-" + uuid.uuid4().hex
    mock_api.routes["api.test/v1/items"] = lambda r: js({"data": recs(2)})
    a = await register(client)
    kb = await _kb(client, a)
    auth = {"kind": "api_key", "api_key_in": "query", "api_key_name": "appid"}
    body = {"type": "rest", "name": "api", "config": config(auth=auth), "secret": secret}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    assert r.status_code == 201, r.text
    sid, bodies = r.json()["id"], [r.text]
    bodies.append((await client.get(f"/v1/sources/{sid}", headers=a.headers)).text)
    bodies.append(str(await _sync(client, a, sid)))
    mock_api.routes["api.test/v1/items"] = lambda r: js({"echo": str(r.url)}, 500)
    bodies.append((await client.post(f"/v1/sources/{sid}/test", headers=a.headers)).text)
    bodies.append(str(await _sync(client, a, sid)))
    assert all(r.url.params["appid"] == secret for r in mock_api.seen)  # it was really sent
    assert all(secret not in b for b in bodies), bodies
    assert secret not in caplog.text
    assert "HTTP Request" in caplog.text  # the httpx line exists, only without the query
    docs = await _docs(a, sid)
    assert set(docs) == {"/items#0", "/items#1"}
    assert docs["/items#0"].meta["endpoint"] == "/items"


async def test_cross_org_rest_source_is_invisible(client: httpx.AsyncClient, mock_api: Api) -> None:
    """AC1."""
    mock_api.routes["api.test/v1/items"] = lambda r: js({"data": recs(1)})
    a, b = await register(client), await register(client)
    kb = await _kb(client, a)
    body = {"type": "rest", "name": "api", "config": config()}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    sid = r.json()["id"]
    for method, path in [
        ("GET", f"/v1/sources/{sid}"),
        ("PATCH", f"/v1/sources/{sid}"),
        ("POST", f"/v1/sources/{sid}/test"),
        ("POST", f"/v1/sources/{sid}/sync"),
        ("DELETE", f"/v1/sources/{sid}"),
    ]:
        r = await client.request(
            method, path, headers=b.headers, json={} if method != "GET" else None
        )
        assert r.status_code == 404, (method, path)


async def test_target_change_requires_the_secret(client: httpx.AsyncClient, mock_api: Api) -> None:
    """S1: base_url or auth (token URL, key placement) changes must come with the secret."""
    for host in ("api.test", "other.test"):
        mock_api.routes[f"{host}/v1/items"] = lambda r: js({"data": recs(1)})
    a = await register(client)
    kb = await _kb(client, a)
    auth = {"kind": "bearer"}
    body = {"type": "rest", "name": "api", "config": config(auth=auth), "secret": "t-123"}
    sid = (await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)).json()["id"]
    before = len(mock_api.seen)
    moved = [
        config(auth=auth, base_url="https://other.test/v1"),
        config(auth={"kind": "api_key", "api_key_in": "query"}),
    ]
    for cfg in moved:
        r = await client.patch(f"/v1/sources/{sid}", json={"config": cfg}, headers=a.headers)
        assert (
            r.status_code == 422
            and r.json()["error"]["code"] == "secret_required_for_target_change"
        )
    assert len(mock_api.seen) == before  # the stored token was never sent anywhere new
    same_target = config(auth=auth, headers={"X-Trace": "1"})
    r = await client.patch(f"/v1/sources/{sid}", json={"config": same_target}, headers=a.headers)
    assert r.status_code == 200, r.text
    r = await client.patch(
        f"/v1/sources/{sid}", json={"config": moved[0], "secret": "t-456"}, headers=a.headers
    )
    assert r.status_code == 200 and mock_api.seen[-1].headers["host"] == "other.test"

    # No stored secret (auth none): nothing can leak, so moving needs no secret.
    body = {"type": "rest", "name": "open", "config": config()}
    sid2 = (await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)).json()["id"]
    elsewhere = config(base_url="https://other.test/v1")
    r = await client.patch(f"/v1/sources/{sid2}", json={"config": elsewhere}, headers=a.headers)
    assert r.status_code == 200, r.text


async def test_connector_is_listed_with_its_schema(client: httpx.AsyncClient) -> None:
    a = await register(client)
    items = (await client.get("/v1/connectors", headers=a.headers)).json()["items"]
    meta = next(c for c in items if c["type"] == "rest")
    assert meta["category"] == "api" and "endpoints" in meta["config_schema"]["properties"]


# ---------------------------------------------------------------- OpenAPI import

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.test/v1"}],
    "components": {
        "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}},
        "schemas": {
            "Item": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "updated_at": {"type": "string"},
                },
            }
        },
    },
    "paths": {
        "/items": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "data": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/Item"},
                                        }
                                    },
                                }
                            }
                        }
                    }
                }
            },
            "post": {"responses": {}},
            "delete": {"responses": {}},
        },
        "/items/{id}": {"get": {"responses": {}}},
    },
}


async def _import(client: httpx.AsyncClient, spec: str) -> httpx.Response:
    a = await register(client)
    return await client.post("/v1/connectors/rest/openapi", json={"spec": spec}, headers=a.headers)


async def test_openapi_import_lists_get_only_and_never_calls_the_api(
    client: httpx.AsyncClient, mock_api: Api
) -> None:
    r = await _import(client, json.dumps(SPEC))
    assert r.status_code == 200, r.text
    out = r.json()
    ep = out["config"]["endpoints"]
    assert [e["path"] for e in ep] == ["/items"] and {e["method"] for e in ep} == {"GET"}
    assert ep[0]["items_path"] == "data[*]" and ep[0]["id_field"] == "id"
    assert ep[0]["title_field"] == "title" and ep[0]["updated_field"] == "updated_at"
    assert out["config"]["auth"] == {
        "kind": "api_key",
        "api_key_in": "header",
        "api_key_name": "X-Key",
    }
    assert {(s["method"], s["path"]) for s in out["skipped"]} == {
        ("POST", "/items"),
        ("DELETE", "/items"),
        ("GET", "/items/{id}"),
    }
    RestConnector().validate_config(out["config"])  # the suggestion is a valid config
    assert mock_api.seen == []


async def test_openapi_import_accepts_yaml_and_refuses_aliases(client: httpx.AsyncClient) -> None:
    ok = "openapi: 3.0.0\npaths:\n  /a:\n    get:\n      responses: {}\n"
    r = await _import(client, ok)
    assert r.status_code == 200 and r.json()["config"]["endpoints"][0]["path"] == "/a"
    bomb = "a: &a [x, x, x]\nb: &b [*a, *a, *a]\nc: [*b, *b, *b]\npaths: {}\n"
    r = await _import(client, bomb)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_spec"
    r = await _import(
        client, "openapi: 3.0.0\npaths:\n  /a: !!python/object/apply:os.system [echo]\n"
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "ref", ["https://evil.test/schema.json", "other.yaml#/X", "file:///etc/passwd", "//evil.test/x"]
)
async def test_openapi_import_refuses_remote_refs(client: httpx.AsyncClient, ref: str) -> None:
    spec = json.loads(json.dumps(SPEC))
    spec["components"]["schemas"]["Item"] = {"$ref": ref}
    r = await _import(client, json.dumps(spec))
    assert r.status_code == 422 and "local $ref" in r.json()["error"]["message"]


async def test_openapi_import_refuses_huge_or_deep_specs(client: httpx.AsyncClient) -> None:
    from app.connectors import openapi

    big = json.dumps({"paths": {}, "x": "y" * (openapi.MAX_SPEC_CHARS + 10)})
    r = await _import(client, big)
    assert r.status_code == 422
    many = json.dumps({"paths": {}, "x": [0] * (openapi.MAX_NODES + 10)})
    r = await _import(client, many)
    assert r.status_code == 422 and "too large" in r.json()["error"]["message"]
    deep = '{"paths": {}, "x": ' + "[" * 5000 + "]" * 5000 + "}"
    r = await _import(client, deep)
    assert r.status_code == 422
    r = await _import(client, '{"openapi": "3.0.0"}')
    assert r.status_code == 422 and "paths" in r.json()["error"]["message"]


def test_openapi_ref_cycles_end() -> None:
    from app.connectors import openapi

    spec = json.loads(json.dumps(SPEC))
    spec["components"]["schemas"]["Item"] = {"$ref": "#/components/schemas/Item"}
    out = openapi.suggest(json.dumps(spec))
    assert out["config"]["endpoints"][0]["id_field"] == "id"

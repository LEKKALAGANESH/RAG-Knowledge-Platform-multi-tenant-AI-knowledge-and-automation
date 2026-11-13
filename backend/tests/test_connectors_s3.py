"""B5: S3-compatible connector against real MinIO (sync lifecycle, ETag skip, SSRF opt-in, AC11)."""

import ipaddress
import json
import logging
import os
import socket
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import httpx
import pytest

from app.connectors import s3 as s3_mod
from app.connectors.base import Config, ConnectorError
from app.connectors.ssrf import ip_allowed_private
from app.core.crypto import encrypt
from tests.conftest import Account, register
from tests.test_connectors import (  # noqa: F401
    _docs,
    _kb,
    _search,
    _source,
    _sql,
    _sync,
    env,
    lan,
)

pytestmark = [
    pytest.mark.skipif(not os.environ.get("S3_ACCESS_KEY"), reason="S3_ACCESS_KEY not set (MinIO)"),
    pytest.mark.usefixtures("lan"),
]
ENDPOINT = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")


def _secret() -> str:
    return json.dumps(
        {
            "access_key_id": os.environ.get("S3_ACCESS_KEY", ""),
            "secret_access_key": os.environ.get("S3_SECRET_KEY", ""),
        }
    )


@pytest.fixture
def bucket() -> Iterator[tuple[str, Any]]:
    name = f"rag-src-{uuid.uuid4().hex[:10]}"
    c = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name="us-east-1",
    )
    c.create_bucket(Bucket=name)
    yield name, c
    for obj in c.list_objects_v2(Bucket=name).get("Contents", []):
        c.delete_object(Bucket=name, Key=obj["Key"])
    c.delete_bucket(Bucket=name)


@pytest.fixture
def gets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every GetObject the connector's clients send (botocore event hook)."""
    calls: list[str] = []
    make = s3_mod._make_client

    def counting(cfg: s3_mod.S3Config, creds: s3_mod.S3Secret) -> Any:
        client = make(cfg, creds)
        client.meta.events.register(
            "before-call.s3.GetObject", lambda **kw: calls.append(kw["params"]["url_path"])
        )
        return client

    monkeypatch.setattr(s3_mod, "_make_client", counting)
    return calls


def s3_cfg(bucket: str, **over: Any) -> Config:
    return {
        "bucket": bucket,
        "prefix": "docs/",
        "endpoint_url": ENDPOINT,
        "allow_private_endpoint": True,
        **over,
    }


async def _s3_source(client: httpx.AsyncClient, a: Account, kb: str, cfg: Config) -> str:
    return await _source(client, a, kb, cfg, type="s3", secret=_secret())


async def test_s3_sync_lifecycle_with_etag_skip(
    client: httpx.AsyncClient, bucket: tuple[str, Any], gets: list[str]
) -> None:
    name, c = bucket
    c.put_object(Bucket=name, Key="docs/a.md", Body=b"# Alpha\n\nThe quokka smiles at visitors.")
    c.put_object(Bucket=name, Key="docs/sub/b.txt", Body=b"Notes about the wombat burrow.")
    c.put_object(Bucket=name, Key="docs/folder/", Body=b"")  # directory marker
    c.put_object(Bucket=name, Key="other/c.md", Body=b"Outside the prefix: dingo.")
    a = await register(client)
    kb = await _kb(client, a)
    sid = await _s3_source(client, a, kb, s3_cfg(name))

    r = await client.post(f"/v1/sources/{sid}/test", headers=a.headers)
    assert r.json()["ok"] is True and r.json()["schema"] is None, r.text
    run1 = await _sync(client, a, sid)
    assert run1["status"] == "succeeded", run1
    assert (run1["stats"]["added"], run1["stats"]["discovered"]) == (2, 2)
    assert len(gets) == 2
    docs = await _docs(a, sid)
    assert set(docs) == {"docs/a.md", "docs/sub/b.txt"}
    assert docs["docs/a.md"].mime == "text/markdown" and docs["docs/sub/b.txt"].mime == "text/plain"
    assert any("quokka" in t for t in await _search(client, a, kb, "quokka"))
    assert await _search(client, a, kb, "dingo") == []

    gets.clear()
    run2 = await _sync(client, a, sid)
    assert run2["stats"]["unchanged"] == 2 and gets == []  # ETag unchanged: no GetObject at all

    c.put_object(Bucket=name, Key="docs/a.md", Body=b"# Alpha\n\nThe numbat digs for termites.")
    run3 = await _sync(client, a, sid)
    assert (run3["stats"]["updated"], run3["stats"]["unchanged"]) == (1, 1)
    assert gets == ["/docs/a.md"]
    assert await _search(client, a, kb, "quokka") == []
    assert any("numbat" in t for t in await _search(client, a, kb, "numbat"))

    c.delete_object(Bucket=name, Key="docs/sub/b.txt")
    run4 = await _sync(client, a, sid)
    assert run4["status"] == "succeeded" and run4["stats"]["deleted"] == 1, run4
    assert (await _docs(a, sid))["docs/sub/b.txt"].status == "deleted"
    assert await _search(client, a, kb, "wombat") == []  # AC7

    b = await register(client)  # AC1
    for method, path in [("GET", f"/v1/sources/{sid}"), ("POST", f"/v1/sources/{sid}/sync")]:
        assert (await client.request(method, path, headers=b.headers)).status_code == 404
    r = await client.post("/v1/search", json={"query": "numbat", "kb_ids": [kb]}, headers=b.headers)
    assert r.status_code == 404


async def test_s3_include_extensions_and_max_objects(
    client: httpx.AsyncClient, bucket: tuple[str, Any]
) -> None:
    name, c = bucket
    for i in range(3):
        c.put_object(Bucket=name, Key=f"docs/{i}.md", Body=f"# Doc {i}\n\nemu {i}".encode())
    c.put_object(Bucket=name, Key="docs/x.txt", Body=b"text file")
    a = await register(client)
    kb = await _kb(client, a)
    sid = await _s3_source(client, a, kb, s3_cfg(name, include_extensions=["MD"], max_objects=2))
    run = await _sync(client, a, sid)
    assert run["stats"]["added"] == 2
    assert set(await _docs(a, sid)) == {"docs/0.md", "docs/1.md"}


async def test_s3_private_endpoint_needs_opt_in(
    client: httpx.AsyncClient, bucket: tuple[str, Any]
) -> None:
    name, _ = bucket
    a = await register(client)
    kb = await _kb(client, a)
    body = {"type": "s3", "name": "s", "secret": _secret()}
    r = await client.post(
        f"/v1/kbs/{kb}/sources",
        json={**body, "config": s3_cfg(name, allow_private_endpoint=False)},
        headers=a.headers,
    )
    assert r.status_code == 422, r.text
    msg = r.json()["error"]["message"]
    assert "private" in msg and "allow_private_endpoint" in msg

    # Saved with the opt-in, then the opt-in is dropped behind the API's back: sync re-checks.
    sid = await _s3_source(client, a, kb, s3_cfg(name))
    # Saved normalized (region default filled), as the API stores it: only the opt-in differs.
    off = json.dumps(s3_cfg(name, allow_private_endpoint=False, region="us-east-1"))
    await _sql(a, "UPDATE sources SET config = CAST(:c AS jsonb) WHERE id = :i", c=off, i=sid)
    run = await _sync(client, a, sid)
    assert run["status"] == "failed" and "private" in run["error"]

    r = await client.patch(f"/v1/sources/{sid}", json={"config": s3_cfg(name)}, headers=a.headers)
    assert r.status_code == 200
    assert (await client.post(f"/v1/sources/{sid}/test", headers=a.headers)).json()["ok"] is True

    # Even with the opt-in, cloud metadata stays unreachable.
    meta = s3_cfg(name, endpoint_url="http://169.254.169.254")
    r = await client.post(f"/v1/kbs/{kb}/sources", json={**body, "config": meta}, headers=a.headers)
    assert r.status_code == 422 and "private or reserved" in r.json()["error"]["message"]


def test_private_opt_in_still_blocks_metadata_and_link_local() -> None:
    allowed = ["10.0.0.5", "192.168.1.2", "172.20.0.3", "fd12::1", "8.8.8.8", "::ffff:10.0.0.5"]
    blocked = (
        "169.254.169.254 fe80::1 fd00:ec2::254 ::ffff:169.254.169.254 64:ff9b::a9fe:a9fe "
        "0.0.0.0 224.0.0.1 100.64.0.1 127.0.0.1 127.8.9.1 ::1 ::ffff:127.0.0.1 2002:7f00:1:: "
        "0.1.2.3 240.0.0.1 255.255.255.255 ff02::1 ::"
    ).split()
    assert all(ip_allowed_private(ipaddress.ip_address(i)) for i in allowed)
    assert not any(ip_allowed_private(ipaddress.ip_address(i)) for i in blocked)


@pytest.mark.parametrize(
    "over",
    [
        {"bucket": "Bad_Bucket!"},
        {"endpoint_url": "file:///etc/passwd"},
        {"endpoint_url": "http://user:pw@minio.example.com"},
        {"endpoint_url": "http://minio.example.com/path"},
        {"max_objects": 50_001},
        {"include_extensions": ["../x"]},
    ],
)
async def test_s3_invalid_config_is_422(client: httpx.AsyncClient, over: Config) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    body = {"type": "s3", "name": "s", "config": {**s3_cfg("good-bucket"), **over}}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=body, headers=a.headers)
    assert r.status_code == 422, r.text


async def test_s3_bad_credentials_never_leak(
    client: httpx.AsyncClient, bucket: tuple[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    name, _ = bucket
    wrong = "wrong-" + uuid.uuid4().hex
    a = await register(client)
    kb = await _kb(client, a)
    secret = json.dumps({"access_key_id": os.environ["S3_ACCESS_KEY"], "secret_access_key": wrong})
    body = {"type": "s3", "name": "s", "config": s3_cfg(name)}
    r = await client.post(
        f"/v1/kbs/{kb}/sources", json={**body, "secret": secret}, headers=a.headers
    )
    assert r.status_code == 422 and "refused the request" in r.json()["error"]["message"]
    bodies = [r.text]
    sid = await _s3_source(client, a, kb, s3_cfg(name))  # the key is rotated after saving
    await _sql(a, "UPDATE sources SET secret_enc = :e WHERE id = :i", e=encrypt(secret), i=sid)
    bodies.append((await client.get(f"/v1/sources/{sid}", headers=a.headers)).text)
    r = await client.post(f"/v1/sources/{sid}/test", headers=a.headers)
    assert r.json()["ok"] is False and "refused the request" in r.json()["message"]
    bodies += [r.text, str(await _sync(client, a, sid))]
    assert all(wrong not in b for b in bodies)
    assert wrong not in caplog.text

    bad = {**body, "secret": "not json " + wrong}
    r = await client.post(f"/v1/kbs/{kb}/sources", json=bad, headers=a.headers)
    assert r.status_code == 422 and wrong not in r.text and "JSON" in r.json()["error"]["message"]


async def test_s3_dns_rebinding_after_the_check_is_refused(
    bucket: tuple[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S13: the endpoint resolves to a public IP for _guard(), then flips to loopback (standing in
    for 169.254.169.254) when boto3 connects. The connect lands on MinIO, so without the peer
    check the request would succeed; with it the source is refused before any HTTP is sent."""
    name, _ = bucket
    host, port = "rebind.example.com", httpx.URL(ENDPOINT).port or 80
    real_check, real_gai = s3_mod.resolve_checked, socket.getaddrinfo

    async def public_once(h: str, p: int, check: Any) -> str:
        return await real_check(h, p, check, resolver=lambda *_: _answer(["93.184.216.34"]))

    def flipped(h: Any, *a: Any, **kw: Any) -> Any:
        return real_gai("127.0.0.1" if h == host else h, *a, **kw)

    monkeypatch.setattr(s3_mod, "resolve_checked", public_once)
    monkeypatch.setattr(socket, "getaddrinfo", flipped)
    cfg = s3_cfg(name, endpoint_url=f"http://{host}:{port}", allow_private_endpoint=False)
    with pytest.raises(ConnectorError, match="private or reserved"):
        await s3_mod.S3Connector().test(cfg, _secret())


async def _answer(ips: list[str]) -> list[str]:
    return ips

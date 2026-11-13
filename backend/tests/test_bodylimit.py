"""S3: oversized bodies are refused before routing, auth or form parsing reads them."""

import uuid
from collections.abc import AsyncIterator

import httpx

from tests.conftest import register  # isort: skip  (sets the test env first)
from app.ingestion.config import get_ingestion_settings
from tests.test_ingestion import stub_broker  # noqa: F401  (autouse: no real queue, tmp blobs)

CHUNK = 64 * 1024
MULTIPART = "multipart/form-data; boundary=b"
PART = b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'


class Counted:
    """Streams `total` bytes and counts how many the server actually pulled."""

    def __init__(self, total: int) -> None:
        self.total, self.sent = total, 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield PART  # a valid multipart file part that never ends
        while self.sent < self.total:
            self.sent += CHUNK
            yield b"x" * CHUNK


def _upload_url() -> str:
    return f"/v1/kbs/{uuid.uuid4()}/documents"


async def test_declared_oversize_upload_is_413_without_reading(client: httpx.AsyncClient) -> None:
    body = Counted(200 * 2**20)
    r = await client.post(
        _upload_url(),
        content=body,
        headers={"Content-Length": str(body.total), "Content-Type": MULTIPART},
    )
    assert r.status_code == 413 and r.json()["error"]["code"] == "body_too_large"
    assert body.sent <= CHUNK  # unauthenticated, and the body was never consumed


async def test_chunked_oversize_upload_stops_at_the_limit(client: httpx.AsyncClient) -> None:
    limit = (get_ingestion_settings().max_upload_mb + 1) * 2**20
    body = Counted(limit + 40 * 2**20)  # no Content-Length: chunked transfer
    r = await client.post(_upload_url(), content=body, headers={"Content-Type": MULTIPART})
    assert r.status_code == 413
    assert body.sent <= limit + 2 * CHUNK


async def test_other_routes_get_the_small_limit(client: httpx.AsyncClient) -> None:
    body = Counted(8 * 2**20)
    r = await client.post(
        "/v1/auth/login", content=body, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 413
    assert body.sent <= 2**20 + 2 * CHUNK


async def test_normal_requests_and_uploads_still_work(client: httpx.AsyncClient) -> None:
    a = await register(client)  # small JSON body passes
    kb = (await client.post("/v1/kbs", json={"name": "kb"}, headers=a.headers)).json()["id"]
    big = b"word " * (300 * 1024)  # 1.5 MB: over the default limit, fine for uploads
    r = await client.post(
        f"/v1/kbs/{kb}/documents", files={"file": ("a.txt", big)}, headers=a.headers
    )
    assert r.status_code == 201, r.text

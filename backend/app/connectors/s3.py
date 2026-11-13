"""S3-compatible storage connector: AWS S3, Cloudflare R2, Backblaze B2, MinIO, GCS (interop).

The ETag + size from the listing is the change marker, so unchanged objects are skipped without a
GetObject. GETs use If-Match on that ETag: an object rewritten between list and get fails the item
(nothing deleted) and is picked up next run, so stored bytes always match the recorded hash.
"""

import asyncio
import ipaddress
import logging
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any, NoReturn

import boto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator

from app.connectors.base import Config, ConnectorError, ConnectorMeta, Item
from app.connectors.ssrf import (
    BlockedURL,
    IpCheck,
    check_static,
    endpoint_check,
    resolve_checked,
)
from app.ingestion.config import get_ingestion_settings

log = logging.getLogger("app.connectors.s3")


class S3Config(BaseModel):
    bucket: str = Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$")
    prefix: str = Field(default="", max_length=1024, description="Only keys under this prefix")
    region: str = Field(default="us-east-1", pattern=r"^[a-z0-9-]{1,32}$")
    endpoint_url: str | None = Field(
        default=None, description="Empty = AWS. R2/B2/MinIO/GCS: the provider's S3 endpoint"
    )
    include_extensions: list[str] = Field(
        default=[], max_length=50, description="e.g. .pdf, .md; empty = every supported file"
    )
    max_objects: int = Field(default=10_000, ge=1, le=50_000)
    allow_private_endpoint: bool = Field(
        default=False,
        description="Allow an endpoint in a private network (self-hosted storage). Off by default.",
    )

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint(cls, v: str | None) -> str | None:
        if not v:
            return None
        try:
            u = check_static(v, allow_nonstandard_ports=True)
        except BlockedURL as exc:
            raise ValueError(str(exc)) from exc
        if u.path not in ("", "/") or u.query:
            raise ValueError("The endpoint URL must not have a path or query.")
        return str(u).rstrip("/")

    @field_validator("include_extensions")
    @classmethod
    def _exts(cls, v: list[str]) -> list[str]:
        out = ["." + e.strip().lower().lstrip(".") for e in v if e.strip()]
        if any(len(e) > 11 or not e[1:].isalnum() for e in out):
            raise ValueError("Extensions look like .pdf or md.")
        return out


class S3Secret(BaseModel):
    access_key_id: str = Field(min_length=1, max_length=256)
    secret_access_key: SecretStr = Field(min_length=1, max_length=1024)


def _creds(secret: str | None) -> S3Secret:
    try:
        return S3Secret.model_validate_json(secret or "")
    except ValidationError:
        # `from None`: the validation error would carry the secret's text in its traceback.
        raise ConnectorError(
            'Credentials must be JSON: {"access_key_id": "...", "secret_access_key": "..."}.'
        ) from None


async def _guard(cfg: S3Config) -> None:
    """SSRF: a custom endpoint must resolve to public addresses unless the source opted in. This
    early check gives a readable error; _check_peers() closes the DNS-rebinding window (S13)."""
    if cfg.endpoint_url is None:
        return
    u = httpx.URL(cfg.endpoint_url)
    check = endpoint_check(cfg.allow_private_endpoint)
    try:
        await resolve_checked(u.host, u.port or (443 if u.scheme == "https" else 80), check)
    except BlockedURL as exc:
        hint = (
            ""
            if cfg.allow_private_endpoint
            else " Self-hosted storage needs allow_private_endpoint."
        )
        raise ConnectorError(f"{exc}{hint}") from None


def _checked_conn(base: type, check: IpCheck) -> type:
    class Checked(base):  # type: ignore[misc]
        def _new_conn(self) -> socket.socket:
            sock: socket.socket = super()._new_conn()
            peer = ipaddress.ip_address(str(sock.getpeername()[0]).split("%")[0])
            if not check(peer):
                sock.close()
                raise BlockedURL(f"The host {self.host} points to a private or reserved address.")
            return sock

    return Checked


def _check_peers(client: Any, check: IpCheck) -> None:
    """S13: urllib3 resolves the endpoint again on every new connection, so a DNS answer that
    flips to 169.254.169.254 after _guard() would be followed. Checking the address the socket
    actually connected to (before TLS/HTTP) leaves no window, and Host/SNI stay the hostname so
    https keeps verifying. A re-resolving before-send hook would still race the connect.
    ponytail: patches botocore's pool-class map (private attrs, pinned by test_s3_dns_rebinding);
    with an HTTP(S) proxy the proxy is the peer and must itself pass `check`."""
    pools = client._endpoint.http_session._pool_classes_by_scheme  # shared with its PoolManagers
    for scheme, pool in list(pools.items()):
        conn = _checked_conn(pool.ConnectionCls, check)
        pools[scheme] = type(f"Checked{pool.__name__}", (pool,), {"ConnectionCls": conn})


def _make_client(cfg: S3Config, creds: S3Secret) -> Any:
    # Explicit keys only: a source must never fall back to the server's own AWS identity.
    client = boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        region_name=cfg.region,
        aws_access_key_id=creds.access_key_id,
        aws_secret_access_key=creds.secret_access_key.get_secret_value(),
        config=BotoConfig(
            connect_timeout=10,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path"} if cfg.endpoint_url else {},
        ),
    )
    if cfg.endpoint_url:  # AWS's own endpoints are not caller-chosen hosts
        _check_peers(client, endpoint_check(cfg.allow_private_endpoint))
    return client


def _fail(exc: Exception) -> NoReturn:
    if isinstance(exc, HTTPClientError) and isinstance(exc.kwargs.get("error"), BlockedURL):
        raise ConnectorError(str(exc.kwargs["error"])) from None
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "?")
        raise ConnectorError(f"The storage service refused the request ({code}).") from None
    raise ConnectorError(f"Could not reach the storage endpoint ({type(exc).__name__}).") from None


async def _call[T](fn: Callable[..., T], **kw: Any) -> T:
    try:
        return await asyncio.to_thread(fn, **kw)
    except (ClientError, BotoCoreError) as exc:
        _fail(exc)


class S3Connector:
    meta = ConnectorMeta(
        type="s3",
        title="S3-compatible storage",
        description="AWS S3, Cloudflare R2, Backblaze B2, MinIO or GCS (interop keys). Read-only.",
        category="storage",
        auth="api_key",
        config_schema=S3Config.model_json_schema(),
    )

    # Config fields that pick where the stored keys sign requests to (see router).
    target_fields = ("endpoint_url", "region", "bucket")

    def validate_config(self, raw: Config) -> Config:
        return S3Config.model_validate(raw).model_dump()

    async def _open(self, config: Config, secret: str | None) -> tuple[S3Config, Any]:
        cfg, creds = S3Config.model_validate(config), _creds(secret)
        await _guard(cfg)
        return cfg, await asyncio.to_thread(_make_client, cfg, creds)

    async def test(self, config: Config, secret: str | None) -> str:
        cfg, client = await self._open(config, secret)
        await _call(client.head_bucket, Bucket=cfg.bucket)
        page = await _call(client.list_objects_v2, Bucket=cfg.bucket, Prefix=cfg.prefix, MaxKeys=1)
        found = "found objects" if page.get("KeyCount") else "no objects yet"
        return f"Connected to bucket {cfg.bucket}: {found} under the prefix."

    async def discover(
        self, config: Config, secret: str | None, cursor: Config
    ) -> AsyncIterator[Item]:
        cfg, client = await self._open(config, secret)
        cap = get_ingestion_settings().max_upload_mb * 2**20
        kw: dict[str, Any] = {"Bucket": cfg.bucket, "Prefix": cfg.prefix, "MaxKeys": 1000}
        count = 0
        while True:
            page = await _call(client.list_objects_v2, **kw)
            for obj in page.get("Contents", []):
                key, size = obj["Key"], obj["Size"]
                if key.endswith("/") or not _wanted(cfg, key):
                    continue
                if size == 0 or size > cap:
                    log.info("object skipped", extra={"size": size})
                    continue
                yield _item(client, cfg.bucket, key, obj["ETag"].strip('"'), size, cap)
                count += 1
                if count >= cfg.max_objects:  # ponytail: first N keys in key order
                    return
            if not page.get("IsTruncated"):
                return
            kw["ContinuationToken"] = page["NextContinuationToken"]


def _wanted(cfg: S3Config, key: str) -> bool:
    name = key.rsplit("/", 1)[-1].lower()
    return not cfg.include_extensions or any(name.endswith(e) for e in cfg.include_extensions)


def _item(client: Any, bucket: str, key: str, etag: str, size: int, cap: int) -> Item:
    async def fetch() -> bytes:
        r = await _call(client.get_object, Bucket=bucket, Key=key, IfMatch=etag)
        try:
            data: bytes = await asyncio.to_thread(r["Body"].read, cap + 1)
        except BotoCoreError as exc:
            _fail(exc)
        finally:
            r["Body"].close()
        if len(data) > cap:
            raise ConnectorError("The object is larger than the upload size limit.")
        return data

    return Item(
        external_key=key,
        name=key.rsplit("/", 1)[-1][:255],
        fetch=fetch,
        content_hash=f"etag:{etag}:{size}",
        meta={"bucket": bucket, "key": key},
    )

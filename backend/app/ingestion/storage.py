from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError

from app.ingestion.config import IngestionSettings, get_ingestion_settings


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalBlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):  # keys are server-built, but never trust a path
            raise ValueError(f"blob key escapes the store: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3BlobStore:
    """Any S3-compatible store (AWS, MinIO, R2, B2)."""

    def __init__(self, s: IngestionSettings) -> None:
        self.bucket = s.s3_bucket
        self.client: Any = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url,
            region_name=s.s3_region,
            aws_access_key_id=s.s3_access_key.get_secret_value() if s.s3_access_key else None,
            aws_secret_access_key=s.s3_secret_key.get_secret_value() if s.s3_secret_key else None,
        )
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        body: bytes = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return body

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


@lru_cache
def get_blob_store() -> BlobStore:
    s = get_ingestion_settings()
    return S3BlobStore(s) if s.blob_backend == "s3" else LocalBlobStore(s.blob_dir)

"""Storage abstraction — local filesystem or S3, picked by config.

Usage:
    from app.services.storage import storage

    storage.save("jobs/abc/file.xlsx", data_bytes)
    data = storage.read("jobs/abc/file.xlsx")
    url  = storage.get_url("jobs/abc/file.xlsx")
"""

import mimetypes
from abc import ABC, abstractmethod
from pathlib import Path

from app.config import get_logger, settings

log = get_logger(__name__)


class StorageBackend(ABC):
    """Minimal interface every backend must implement."""

    @abstractmethod
    def save(self, path: str, data: bytes) -> None: ...

    @abstractmethod
    def read(self, path: str) -> bytes: ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def get_url(self, path: str) -> str:
        """Return a URL suitable for serving this file to authenticated users."""
        ...


# ---------------------------------------------------------------------------
# Local filesystem (development default)
# ---------------------------------------------------------------------------

class LocalStorage(StorageBackend):
    def __init__(self, base_dir: str):
        self._base = Path(base_dir).resolve()
        self._base.mkdir(parents=True, exist_ok=True)

    def _full(self, path: str) -> Path:
        resolved = (self._base / path).resolve()
        if not str(resolved).startswith(str(self._base)):
            raise ValueError(f"Path traversal blocked: {path}")
        return resolved

    def save(self, path: str, data: bytes) -> None:
        full = self._full(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)

    def read(self, path: str) -> bytes:
        return self._full(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._full(path).is_file()

    def delete(self, path: str) -> None:
        full = self._full(path)
        if full.is_file():
            full.unlink()

    def get_url(self, path: str) -> str:
        # Served by the /uploads/{path} route in main.py
        return f"/uploads/{path}"


# ---------------------------------------------------------------------------
# AWS S3
# ---------------------------------------------------------------------------

class S3Storage(StorageBackend):
    def __init__(self, bucket: str, region: str, access_key: str, secret_key: str):
        import boto3

        self._bucket_name = bucket
        self._client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        log.info("s3_storage_init", bucket=bucket, region=region)

    def save(self, path: str, data: bytes) -> None:
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self._client.put_object(
            Bucket=self._bucket_name,
            Key=path,
            Body=data,
            ContentType=content_type,
        )

    def read(self, path: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket_name, Key=path)
        return resp["Body"].read()

    def exists(self, path: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket_name, Key=path)
            return True
        except ClientError:
            return False

    def delete(self, path: str) -> None:
        self._client.delete_object(Bucket=self._bucket_name, Key=path)

    def get_url(self, path: str) -> str:
        # Pre-signed URL valid for 1 hour
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket_name, "Key": path},
            ExpiresIn=3600,
        )


# ---------------------------------------------------------------------------
# Factory — singleton for the lifetime of the process
# ---------------------------------------------------------------------------

def _build_storage() -> StorageBackend:
    if settings.aws_s3_bucket:
        return S3Storage(
            bucket=settings.aws_s3_bucket,
            region=settings.aws_region,
            access_key=settings.aws_access_key_id,
            secret_key=settings.aws_secret_access_key,
        )
    return LocalStorage(settings.upload_dir)


storage: StorageBackend = _build_storage()

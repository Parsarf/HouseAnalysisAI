"""Provider-agnostic document storage.

Application tables store an opaque document reference.  The filesystem
backend keeps the existing local-path behavior; the S3 backend uses the same
operations and stores ``s3://bucket/key`` references.  PDF/OCR libraries use
``materialize`` only for the duration of a job, so domain code never depends
on a hosting vendor or a particular storage SDK.
"""
from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from uuid import UUID

from common.settings import Settings, settings


class DocumentStorage(Protocol):
    def save_file(self, source: Path, key: str) -> str: ...
    def save_bytes(self, data: bytes, key: str) -> str: ...
    def save_text(self, text: str, key: str) -> str: ...
    def read_text(self, reference: str) -> str: ...
    def child(self, reference: str, relative: str) -> str: ...
    def list_children(self, reference: str, prefix: str) -> list[str]: ...
    @contextmanager
    def materialize(self, reference: str) -> Iterator[Path]: ...


def document_key(report_id: UUID, filename: str) -> str:
    return f"{report_id}/{filename.lstrip('/')}"


class LocalFilesystemStorage:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = Path(key)
        if path.is_absolute():
            return path
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError("storage path escapes document root")
        return resolved

    def save_file(self, source: Path, key: str) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target:
            shutil.copy2(source, target)
        return str(target)

    def save_bytes(self, data: bytes, key: str) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return str(target)

    def save_text(self, text: str, key: str) -> str:
        return self.save_bytes(text.encode("utf-8"), key)

    def read_text(self, reference: str) -> str:
        return Path(reference).read_text(encoding="utf-8")

    def child(self, reference: str, relative: str) -> str:
        return str(Path(reference).parent / relative)

    def list_children(self, reference: str, prefix: str) -> list[str]:
        directory = Path(reference).parent / prefix
        return [str(path) for path in sorted(directory.glob("[0-9]*.txt"), key=lambda item: int(item.stem))]

    @contextmanager
    def materialize(self, reference: str) -> Iterator[Path]:
        yield Path(reference)


class S3Storage:
    def __init__(self, config: Settings):
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - exercised by deployment config
            raise RuntimeError("boto3 is required for ACQ_STORAGE_BACKEND=s3") from exc
        if not config.s3_bucket or not config.s3_access_key_id or not config.s3_secret_access_key:
            raise RuntimeError("S3 storage requires bucket and access credentials")
        self.bucket = config.s3_bucket
        self.client = boto3.client(
            "s3", endpoint_url=config.s3_endpoint, region_name=config.s3_region,
            aws_access_key_id=config.s3_access_key_id,
            aws_secret_access_key=config.s3_secret_access_key,
        )

    def _parse(self, reference: str) -> tuple[str, str]:
        prefix = "s3://"
        if not reference.startswith(prefix):
            raise ValueError("S3 storage reference must start with s3://")
        bucket, _, key = reference[len(prefix):].partition("/")
        if bucket != self.bucket or not key:
            raise ValueError("invalid S3 storage reference")
        return bucket, key

    def _ref(self, key: str) -> str:
        return f"s3://{self.bucket}/{key.lstrip('/')}"

    def save_file(self, source: Path, key: str) -> str:
        self.client.upload_file(str(source), self.bucket, key)
        return self._ref(key)

    def save_bytes(self, data: bytes, key: str) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return self._ref(key)

    def save_text(self, text: str, key: str) -> str:
        return self.save_bytes(text.encode("utf-8"), key)

    def read_text(self, reference: str) -> str:
        bucket, key = self._parse(reference)
        return self.client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")

    def child(self, reference: str, relative: str) -> str:
        _bucket, key = self._parse(reference)
        return self._ref(f"{key.rsplit('/', 1)[0]}/{relative.lstrip('/')}")

    def list_children(self, reference: str, prefix: str) -> list[str]:
        bucket, key = self._parse(reference)
        base = f"{key.rsplit('/', 1)[0]}/{prefix.lstrip('/')}"
        response = self.client.list_objects_v2(Bucket=bucket, Prefix=base)
        return [self._ref(item["Key"]) for item in response.get("Contents", [])
                if Path(item["Key"]).suffix == ".txt"]

    @contextmanager
    def materialize(self, reference: str) -> Iterator[Path]:
        bucket, key = self._parse(reference)
        suffix = Path(key).suffix
        with tempfile.TemporaryDirectory(prefix="acq-storage-") as directory:
            path = Path(directory) / (Path(key).name or f"document{suffix}")
            self.client.download_file(bucket, key, str(path))
            yield path


def get_document_storage(config: Settings = settings) -> DocumentStorage:
    if config.storage_backend == "s3":
        return S3Storage(config)
    return LocalFilesystemStorage(config.document_root)

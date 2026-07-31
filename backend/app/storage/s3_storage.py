from __future__ import annotations

import os
from typing import Optional

import boto3
from botocore.config import Config


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class S3Storage:
    """Ceph object storage accessed through its S3-compatible API."""

    def __init__(self) -> None:
        self.bucket = _required_env("AWS_BUCKET")

        self.client = boto3.client(
            "s3",
            endpoint_url=_required_env("AWS_ENDPOINT"),
            aws_access_key_id=_required_env("S3_ACCESS_KEY"),
            aws_secret_access_key=_required_env("S3_SECRET_KEY"),
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            verify=_env_bool("S3_VERIFY_SSL", default=True),
            config=Config(
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                s3={
                    "addressing_style": "path",
                    "payload_signing_enabled": False,
                },
                connect_timeout=10,
                read_timeout=30,
                retries={
                    "max_attempts": 3,
                    "mode": "standard",
                },
            ),
        )

    def upload_bytes(
        self,
        *,
        object_key: str,
        data: bytes,
        content_type: str,
    ) -> str:
        self.client.put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=data,
            ContentType=content_type,
        )

        return f"s3://{self.bucket}/{object_key}"


def create_s3_storage_from_env() -> Optional[S3Storage]:
    if not _env_bool("S3_ENABLED", default=False):
        return None

    return S3Storage()
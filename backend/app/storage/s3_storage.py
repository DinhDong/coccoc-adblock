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
    """
    Ceph object storage accessed through its S3-compatible API.

    Also drives MinIO unchanged for local development: both are addressed
    path-style and signed with s3v4, which is what the config below asks for.
    """

    def __init__(self) -> None:
        self.bucket = _required_env("AWS_BUCKET")

        endpoint = _required_env("AWS_ENDPOINT")
        self.client = self._build_client(endpoint)

        # Presigned URLs are handed to the browser, so they have to name a
        # host the browser can actually reach. Under compose the backend
        # talks to storage over the container network — http://minio:9000 —
        # which resolves to nothing on the developer's machine, so every
        # screenshot in the UI would fail to load. Setting AWS_PUBLIC_ENDPOINT
        # signs the URLs against that host instead while uploads keep using
        # the internal one.
        #
        # The two cannot be collapsed into one client: an s3v4 signature
        # covers the Host header, so a URL signed for `minio` is rejected when
        # fetched from `localhost` and vice versa. Against Ceph, where one
        # address serves both sides, leave AWS_PUBLIC_ENDPOINT unset and this
        # is the same client twice.
        public_endpoint = os.getenv("AWS_PUBLIC_ENDPOINT", "").strip()
        self.presign_client = (
            self._build_client(public_endpoint)
            if public_endpoint and public_endpoint != endpoint
            else self.client
        )

    @staticmethod
    def _build_client(endpoint_url: str):
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=_required_env("S3_ACCESS_KEY"),
            aws_secret_access_key=_required_env("S3_SECRET_KEY"),
            region_name=os.getenv(
                "AWS_DEFAULT_REGION",
                "us-east-1",
            ),
            verify=_env_bool(
                "S3_VERIFY_SSL",
                default=True,
            ),
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
        """
        Upload raw bytes to Ceph.

        Returns:
            Stable S3 URI such as:
            s3://dev-capstone/images/screenshots/report-1/file.png
        """
        if not object_key.strip():
            raise ValueError("object_key must not be empty")

        if not data:
            raise ValueError("data must not be empty")

        self.client.put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=data,
            ContentType=content_type,
        )

        return f"s3://{self.bucket}/{object_key}"

    def generate_download_url(
        self,
        *,
        object_key: str,
        expires_in: int = 900,
    ) -> str:
        """
        Generate a temporary URL for viewing/downloading an object.

        Args:
            object_key: Path of the object inside the bucket.
            expires_in: URL lifetime in seconds.
        """
        if not object_key.strip():
            raise ValueError("object_key must not be empty")

        return self.presign_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": self.bucket,
                "Key": object_key,
            },
            ExpiresIn=expires_in,
        )

    def delete_object(self, *, object_key: str) -> None:
        """Delete an object from Ceph."""

        if not object_key.strip():
            raise ValueError("object_key must not be empty")

        self.client.delete_object(
            Bucket=self.bucket,
            Key=object_key,
        )


def create_s3_storage_from_env() -> Optional[S3Storage]:
    """Create S3 storage only when S3_ENABLED is true."""

    if not _env_bool("S3_ENABLED", default=False):
        return None

    return S3Storage()
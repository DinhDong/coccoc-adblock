from __future__ import annotations

import os
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value


def parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=require_env("AWS_ENDPOINT"),
        aws_access_key_id=require_env("S3_ACCESS_KEY"),
        aws_secret_access_key=require_env("S3_SECRET_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        verify=parse_bool(os.getenv("S3_VERIFY_SSL"), default=True),
        config=Config(
            signature_version="s3v4",
            s3={
                "addressing_style": "path",
            },
            connect_timeout=10,
            read_timeout=30,
            retries={
                "max_attempts": 3,
                "mode": "standard",
            },
        ),
    )


def main() -> int:
    bucket = require_env("AWS_BUCKET")
    object_key = f"adblock-test/connection-{uuid.uuid4().hex}.txt"
    test_content = b"AdBlock Rule Engine Ceph connection test\n"

    client = create_s3_client()

    try:
        print("Uploading test object...")

        client.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=test_content,
            ContentType="text/plain",
        )

        print("Checking uploaded object...")

        metadata = client.head_object(
            Bucket=bucket,
            Key=object_key,
        )

        downloaded = client.get_object(
            Bucket=bucket,
            Key=object_key,
        )["Body"].read()

        if downloaded != test_content:
            print("Upload succeeded, but downloaded content is different.")
            return 1

        print("Ceph/S3 connection successful.")
        print(f"Bucket: {bucket}")
        print(f"Object key: {object_key}")
        print(f"Object size: {metadata['ContentLength']} bytes")
        return 0

    except ClientError as exc:
        error = exc.response.get("Error", {})
        error_code = error.get("Code", "Unknown")
        error_message = error.get("Message", str(exc))

        print(
            f"Ceph/S3 request failed: {error_code} - {error_message}",
            file=sys.stderr,
        )
        return 1

    except BotoCoreError as exc:
        print(
            f"Could not communicate with the Ceph/S3 endpoint: {exc}",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
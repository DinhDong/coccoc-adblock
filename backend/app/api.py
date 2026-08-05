from __future__ import annotations

import io
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from app.storage.s3_storage import S3Storage


logger = logging.getLogger(__name__)


# Supported image formats.
# The server checks the actual image content, not only the filename.
IMAGE_FORMATS: dict[str, tuple[str, str]] = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
}


class ImageUploadResponse(BaseModel):
    """Response returned after an image is uploaded."""

    success: bool
    original_filename: str
    content_type: str
    size: int
    bucket: str
    object_key: str
    s3_uri: str
    download_url: Optional[str] = None
    download_url_expires_in: Optional[int] = None


class HealthResponse(BaseModel):
    status: str
    service: str


def _cors_origins() -> list[str]:
    """
    Read allowed frontend origins from CORS_ORIGINS.

    Example:
        CORS_ORIGINS=http://localhost:5173,http://10.0.0.5:5173
    """
    raw_value = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )

    return [
        origin.strip()
        for origin in raw_value.split(",")
        if origin.strip()
    ]


app = FastAPI(
    title="AdBlock Image Storage API",
    description="Upload crawler and validation images to Ceph object storage.",
    version="1.0.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@lru_cache(maxsize=1)
def get_s3_storage() -> S3Storage:
    """
    Create and reuse one S3 client.

    The API process keeps this instance instead of rebuilding a boto3
    client for every upload.
    """
    return S3Storage()


def require_api_key(
    x_api_key: Annotated[
        Optional[str],
        Header(alias="X-API-Key"),
    ] = None,
) -> None:
    """
    Require X-API-Key when UPLOAD_API_KEY is configured.

    For local development, leaving UPLOAD_API_KEY empty disables this check.
    Production/internal deployment should always configure a key.
    """
    expected_key = os.getenv("UPLOAD_API_KEY", "").strip()

    if not expected_key:
        return

    supplied_key = x_api_key or ""

    if not secrets.compare_digest(supplied_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )


def _max_upload_bytes() -> int:
    """Return configured maximum image size in bytes."""

    raw_value = os.getenv("MAX_IMAGE_UPLOAD_MB", "10").strip()

    try:
        max_megabytes = int(raw_value)
    except ValueError:
        max_megabytes = 10

    # Prevent an accidental invalid or extreme configuration.
    max_megabytes = max(1, min(max_megabytes, 100))

    return max_megabytes * 1024 * 1024


def _presigned_url_expiry() -> int:
    """Return temporary download URL lifetime in seconds."""

    raw_value = os.getenv(
        "PRESIGNED_URL_EXPIRES_SECONDS",
        "900",
    ).strip()

    try:
        expires_in = int(raw_value)
    except ValueError:
        expires_in = 900

    return max(60, min(expires_in, 86400))


def _safe_path_part(
    value: Optional[str],
    fallback: str,
) -> str:
    """
    Convert user input into a safe S3 path component.

    Examples:
        "Report 123"  -> "Report-123"
        "../../test"  -> "test"
    """
    text = (value or "").strip()

    text = re.sub(
        pattern=r"[^a-zA-Z0-9_-]+",
        repl="-",
        string=text,
    )

    text = text.strip("-_")

    if not text:
        return fallback

    return text[:80]


def _inspect_image(data: bytes) -> tuple[str, str]:
    """
    Validate image bytes and determine extension/content type.

    Returns:
        Tuple of (extension, content_type).
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            image_format = (image.format or "").upper()

    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="The uploaded file is not a valid supported image.",
        ) from exc

    image_info = IMAGE_FORMATS.get(image_format)

    if image_info is None:
        allowed = ", ".join(IMAGE_FORMATS.keys())

        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported image format. Allowed formats: {allowed}.",
        )

    return image_info


@app.get(
    "/health",
    response_model=HealthResponse,
)
def health_check() -> HealthResponse:
    """Basic API health check."""

    return HealthResponse(
        status="ok",
        service="image-storage-api",
    )


@app.post(
    "/api/v1/images",
    response_model=ImageUploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def upload_image(
    file: Annotated[
        UploadFile,
        File(description="PNG, JPEG or WebP image"),
    ],
    report_id: Annotated[
        Optional[str],
        Form(),
    ] = None,
    category: Annotated[
        str,
        Form(),
    ] = "uploads",
) -> ImageUploadResponse:
    """
    Upload one image to Ceph.

    Form fields:
        file: Required image.
        report_id: Optional related report/ticket ID.
        category: Optional group such as screenshots, validation or evidence.
    """
    maximum_size = _max_upload_bytes()

    try:
        # Read one extra byte so oversized files can be detected.
        data = await file.read(maximum_size + 1)
    finally:
        await file.close()

    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    if len(data) > maximum_size:
        maximum_mb = maximum_size // (1024 * 1024)

        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image exceeds the {maximum_mb} MB limit.",
        )

    extension, detected_content_type = _inspect_image(data)

    safe_report_id = _safe_path_part(
        report_id,
        fallback="unassigned",
    )
    safe_category = _safe_path_part(
        category,
        fallback="uploads",
    )

    date_path = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    unique_name = uuid.uuid4().hex

    object_key = (
        f"images/"
        f"{safe_category}/"
        f"{safe_report_id}/"
        f"{date_path}/"
        f"{unique_name}.{extension}"
    )

    try:
        storage = get_s3_storage()

        s3_uri = storage.upload_bytes(
            object_key=object_key,
            data=data,
            content_type=detected_content_type,
        )

    except Exception as exc:
        logger.exception(
            "Failed to upload image to Ceph: %s",
            object_key,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not upload image to object storage.",
        ) from exc

    expires_in = _presigned_url_expiry()
    download_url: Optional[str] = None

    # Presigned URL generation is useful but not required for upload success.
    try:
        download_url = storage.generate_download_url(
            object_key=object_key,
            expires_in=expires_in,
        )
    except Exception:
        logger.warning(
            "Image uploaded but download URL could not be generated: %s",
            object_key,
            exc_info=True,
        )
        expires_in = None

    original_filename = Path(
        file.filename or f"image.{extension}"
    ).name

    return ImageUploadResponse(
        success=True,
        original_filename=original_filename,
        content_type=detected_content_type,
        size=len(data),
        bucket=storage.bucket,
        object_key=object_key,
        s3_uri=s3_uri,
        download_url=download_url,
        download_url_expires_in=expires_in,
    )
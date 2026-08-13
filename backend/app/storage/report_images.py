"""
Uploads the three screenshots that document a report to Ceph.

The pipeline already writes them to disk; this collects them under one place
so the CMS can show a report's evidence without the moderator needing access
to the machine that ran the crawl.

    crawl         data/crawl_outputs/screenshots/<id>.png
                  the page as crawled, no rules applied
    before_boxed  data/rule_outputs/screenshots/<id>_before_boxed.png
                  same page with the detected ad boxes drawn on
    after_rules   data/rule_outputs/screenshots/<id>_with_rules.png
                  the page with every passing rule applied
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# kind -> (local path template, object key template)
IMAGE_KINDS: Dict[str, tuple] = {
    "crawl": (
        "data/crawl_outputs/screenshots/{report_id}.png",
        "reports/{report_id}/crawl.png",
    ),
    "before_boxed": (
        "data/rule_outputs/screenshots/{report_id}_before_boxed.png",
        "reports/{report_id}/before_boxed.png",
    ),
    "after_rules": (
        "data/rule_outputs/screenshots/{report_id}_with_rules.png",
        "reports/{report_id}/after_rules.png",
    ),
}


def local_image_paths(report_id: str) -> Dict[str, Path]:
    """Where each of the three images is expected on disk."""
    return {
        kind: Path(local.format(report_id=report_id))
        for kind, (local, _) in IMAGE_KINDS.items()
    }


def upload_report_images(report_id: str) -> Dict[str, str]:
    """
    Upload whichever of the three images exist, returning {kind: s3_uri}.

    Missing images are skipped rather than failing the pipeline: a run that
    produced no rules has no "after" shot, and that is not an error. Upload
    failures are logged and skipped for the same reason — the report itself
    is still valid without its pictures.
    """
    from .s3_storage import create_s3_storage_from_env

    try:
        storage = create_s3_storage_from_env()
    except Exception:
        logger.exception("Report images: could not initialise Ceph storage")
        return {}

    if storage is None:
        logger.info("Report images: S3_ENABLED is off, skipping upload for %s", report_id)
        return {}

    uploaded: Dict[str, str] = {}
    for kind, (local_template, key_template) in IMAGE_KINDS.items():
        path = Path(local_template.format(report_id=report_id))
        if not path.exists():
            logger.info("Report images: %s missing for %s (%s)", kind, report_id, path)
            continue

        try:
            data = path.read_bytes()
            if not data:
                continue
            uploaded[kind] = storage.upload_bytes(
                object_key=key_template.format(report_id=report_id),
                data=data,
                content_type="image/png",
            )
        except Exception:
            logger.exception("Report images: failed uploading %s for %s", kind, report_id)

    if uploaded:
        logger.info(
            "Report images: uploaded %d/%d for %s",
            len(uploaded),
            len(IMAGE_KINDS),
            report_id,
        )
    return uploaded


def delete_report_images(report_id: str) -> int:
    """
    Remove a report's images from Ceph, returning how many were deleted.

    Called when a report is deleted: the bucket outlives the database row, so
    without this every deleted report leaves its screenshots behind with
    nothing left to reference them.
    """
    from .s3_storage import create_s3_storage_from_env

    try:
        storage = create_s3_storage_from_env()
    except Exception:
        logger.exception("Report images: could not initialise Ceph storage for delete")
        return 0

    if storage is None:
        return 0

    removed = 0
    for _, (_, key_template) in IMAGE_KINDS.items():
        try:
            storage.delete_object(object_key=key_template.format(report_id=report_id))
            removed += 1
        except Exception:
            logger.exception("Report images: failed deleting %s image", report_id)

    return removed


def object_key_from_uri(uri: str) -> Optional[str]:
    """Strip the s3://bucket/ prefix so the key can be presigned."""
    if not uri or not uri.startswith("s3://"):
        return None
    without_scheme = uri[len("s3://"):]
    _, _, key = without_scheme.partition("/")
    return key or None


def presign_report_images(images: Dict[str, str]) -> Dict[str, str]:
    """Turn stored s3:// URIs into temporary viewable URLs."""
    from .s3_storage import create_s3_storage_from_env

    if not images:
        return {}

    try:
        storage = create_s3_storage_from_env()
    except Exception:
        logger.exception("Report images: could not initialise Ceph storage for presigning")
        return {}

    if storage is None:
        return {}

    try:
        expires = int(os.getenv("PRESIGNED_URL_EXPIRES_SECONDS", "900"))
    except ValueError:
        expires = 900

    urls: Dict[str, str] = {}
    for kind, uri in images.items():
        key = object_key_from_uri(uri)
        if not key:
            continue
        try:
            urls[kind] = storage.generate_download_url(object_key=key, expires_in=expires)
        except Exception:
            logger.exception("Report images: failed presigning %s", kind)

    return urls

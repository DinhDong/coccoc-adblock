# Saves crawler output:
# save HTML snapshot
# save screenshot
# save final JSON result
# save crawl error information

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from app.storage.s3_storage import create_s3_storage_from_env


logger = logging.getLogger(__name__)


class CrawlStorage:
    """
    Handles saving crawler outputs:
    - rendered HTML snapshot
    - screenshot
    - final JSON result
    - crawl error information

    This module is the final step of the crawler pipeline.
    It does not crawl or detect ads by itself.
    It only receives data from previous modules and stores it safely.
    """

    def __init__(self, base_dir: str = "data/crawl_outputs"):
        """
        Initialize storage folders.

        Args:
            base_dir: Root directory where crawler outputs will be saved.
        """
        self.base_dir = Path(base_dir)

        # S3/Ceph is optional. If configuration is invalid or the service is
        # temporarily unavailable, local crawler storage must still work.
        try:
            self.s3_storage = create_s3_storage_from_env()
        except Exception:
            logger.exception("Failed to initialize Ceph/S3 storage; using local storage only")
            self.s3_storage = None

        self.html_dir = self.base_dir / "html"
        self.screenshot_dir = self.base_dir / "screenshots"
        self.result_dir = self.base_dir / "results"
        self.error_dir = self.base_dir / "errors"

        self._create_directories()

    def _create_directories(self) -> None:
        """
        Create required output folders if they do not exist.
        """
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self.error_dir.mkdir(parents=True, exist_ok=True)

    def _safe_report_id(self, report_id: str) -> str:
        """
        Make report_id safe to use as a filename.

        Example:
            "R001/test" -> "R001_test"
        """
        if not report_id or not report_id.strip():
            raise ValueError("report_id must not be empty")

        unsafe_chars = ["/", "\\", ":", "*", "?", "\"", "<", ">", "|"]
        safe_id = report_id.strip()

        for char in unsafe_chars:
            safe_id = safe_id.replace(char, "_")

        return safe_id

    def _current_timestamp(self) -> str:
        """
        Return current UTC timestamp in ISO format.
        """
        return datetime.now(timezone.utc).isoformat()

    def save_html(self, report_id: str, html: str) -> str:
        """
        Save rendered HTML content to a file.

        Args:
            report_id: Unique ID of the crawl report.
            html: Rendered HTML string from browser.py.

        Returns:
            Path to the saved HTML file.
        """
        safe_id = self._safe_report_id(report_id)
        file_path = self.html_dir / f"{safe_id}.html"

        with open(file_path, "w", encoding="utf-8") as file:
            file.write(html or "")

        return str(file_path)

    def save_screenshot(self, report_id: str, screenshot_bytes: bytes) -> str:
        """
        Save screenshot bytes to a PNG file.

        Args:
            report_id: Unique ID of the crawl report.
            screenshot_bytes: PNG bytes from browser.py.

        Returns:
            Path to the saved screenshot file.
        """
        safe_id = self._safe_report_id(report_id)
        file_path = self.screenshot_dir / f"{safe_id}.png"

        with open(file_path, "wb") as file:
            file.write(screenshot_bytes or b"")

        return str(file_path)

    def upload_screenshot(
        self,
        report_id: str,
        screenshot_bytes: bytes,
    ) -> Optional[str]:
        """
        Upload a crawler screenshot to Ceph/S3.

        Local screenshot saving remains the primary fallback. This method returns
        None when S3 is disabled, the screenshot is empty, or the upload fails.

        Args:
            report_id: Unique ID of the crawl report.
            screenshot_bytes: PNG bytes from browser.py.

        Returns:
            S3 URI when the upload succeeds, otherwise None.
        """
        if self.s3_storage is None:
            return None

        if not screenshot_bytes:
            logger.warning(
                "Skipping Ceph/S3 upload because screenshot is empty for report %s",
                report_id,
            )
            return None

        safe_id = self._safe_report_id(report_id)
        object_key = f"crawl_outputs/screenshots/{safe_id}.png"

        try:
            s3_uri = self.s3_storage.upload_bytes(
                object_key=object_key,
                data=screenshot_bytes,
                content_type="image/png",
            )
            logger.info(
                "Uploaded screenshot to Ceph/S3 for report %s: %s",
                report_id,
                s3_uri,
            )
            return s3_uri
        except Exception:
            logger.exception(
                "Failed to upload screenshot to Ceph/S3 for report %s",
                report_id,
            )
            return None

    def save_result(self, report_id: str, result: Dict[str, Any]) -> str:
        """
        Save final crawl result as JSON.

        Args:
            report_id: Unique ID of the crawl report.
            result: Final result dictionary.

        Returns:
            Path to the saved JSON result file.
        """
        safe_id = self._safe_report_id(report_id)
        file_path = self.result_dir / f"{safe_id}.json"

        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2)

        return str(file_path)

    def save_error(
        self,
        report_id: str,
        url: str,
        error_message: str,
        extra_info: Optional[Dict[str, Any]] = None,
        fallbacks_used: Optional[List[str]] = None,
        alerts: Optional[List[str]] = None,
    ) -> str:
        """
        Save crawl error information as JSON.

        This is used when the crawler fails before producing a normal result.

        Args:
            report_id: Unique ID of the crawl report.
            url: URL that failed to crawl.
            error_message: Error message.
            extra_info: Extra debug information.
            fallbacks_used: List of fallback actions already tried.
            alerts: List of warning/alert messages.

        Returns:
            Path to the saved error JSON file.
        """
        safe_id = self._safe_report_id(report_id)
        file_path = self.error_dir / f"{safe_id}_error.json"

        error_data = {
            "report_id": report_id,
            "url": url,
            "status": "failed",
            "created_at": self._current_timestamp(),
            "error": error_message,
            "fallbacks_used": fallbacks_used or [],
            "alerts": alerts or [],
            "extra_info": extra_info or {},
        }

        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(error_data, file, ensure_ascii=False, indent=2)

        return str(file_path)

    def build_result(
        self,
        report_id: str,
        url: str,
        status: str,
        crawl_time_ms: int,
        title: str = "",
        network_requests: Optional[Dict[str, Any]] = None,
        ad_candidates: Optional[List[Dict[str, Any]]] = None,
        summary: Optional[Dict[str, Any]] = None,
        html_path: str = "",
        screenshot_path: str = "",
        screenshot_s3_uri: Optional[str] = None,
        errors: Optional[List[str]] = None,
        fallbacks_used: Optional[List[str]] = None,
        alerts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Build final JSON structure for one crawl result.

        This method only builds the dictionary.
        It does not save the JSON file by itself.
        Call save_result() after this method.

        Args:
            report_id: Unique ID of the crawl report.
            url: Crawled URL.
            status: success, partial_success, or failed.
            crawl_time_ms: Total crawl time in milliseconds.
            title: Page title.
            network_requests: Network request summary (grouped by domain).
            ad_candidates: List of ad candidate dicts with suggested rules.
            summary: Detection summary (networks found, confidence breakdown).
            html_path: Path to saved HTML file.
            screenshot_path: Path to saved local screenshot file.
            screenshot_s3_uri: Ceph/S3 URI for the uploaded screenshot.
            errors: List of errors found during crawling.
            fallbacks_used: List of fallback actions already tried.
            alerts: List of warning/alert messages.

        Returns:
            Final crawl result dictionary.
        """
        return {
            "report_id": report_id,
            "url": url,
            "status": status,
            "created_at": self._current_timestamp(),
            "crawl_time_ms": crawl_time_ms,

            "title": title,

            "files": {
                "html": html_path,
                "screenshot": screenshot_path,
                "screenshot_s3": screenshot_s3_uri,
            },

            "network_requests": network_requests or {},
            "ad_candidates": ad_candidates or [],
            "summary": summary or {},

            # Added based on supervisor feedback
            "fallbacks_used": fallbacks_used or [],
            "alerts": alerts or [],

            "errors": errors or [],
        }
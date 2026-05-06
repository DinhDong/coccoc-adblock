# Saves crawler output:
# save HTML snapshot
# save screenshot
# save final JSON result
# save crawl error information

import json
from pathlib import Path
from typing import Any, Dict, Optional


class CrawlStorage:
    """
    Handles saving crawler outputs:
    - HTML snapshot
    - screenshot
    - final JSON result
    - crawl error information
    """

    def __init__(self, base_dir: str = "data/crawl_outputs"):
        self.base_dir = Path(base_dir)
        self.html_dir = self.base_dir / "html"
        self.screenshot_dir = self.base_dir / "screenshots"
        self.result_dir = self.base_dir / "results"
        self.error_dir = self.base_dir / "errors"

        self._create_directories()

    def _create_directories(self) -> None:
        """Create output folders if they do not exist."""
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self.error_dir.mkdir(parents=True, exist_ok=True)

    def save_html(self, report_id: str, html: str) -> str:
        """
        Save rendered HTML content to a file.

        Returns:
            Path to the saved HTML file as a string.
        """
        file_path = self.html_dir / f"{report_id}.html"

        with open(file_path, "w", encoding="utf-8") as file:
            file.write(html)

        return str(file_path)

    def save_screenshot(self, report_id: str, screenshot_bytes: bytes) -> str:
        """
        Save screenshot bytes to a PNG file.

        Returns:
            Path to the saved screenshot file as a string.
        """
        file_path = self.screenshot_dir / f"{report_id}.png"

        with open(file_path, "wb") as file:
            file.write(screenshot_bytes)

        return str(file_path)

    def save_result(self, report_id: str, result: Dict[str, Any]) -> str:
        """
        Save final crawl result as JSON.

        Returns:
            Path to the saved JSON result file as a string.
        """
        file_path = self.result_dir / f"{report_id}.json"

        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2)

        return str(file_path)

    def save_error(
        self,
        report_id: str,
        url: str,
        error_message: str,
        extra_info: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Save crawl error information as JSON.

        Returns:
            Path to the saved error JSON file as a string.
        """
        file_path = self.error_dir / f"{report_id}_error.json"

        error_data = {
            "report_id": report_id,
            "url": url,
            "status": "failed",
            "error": error_message,
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
        extracted_data: Dict[str, Any],
        ad_signals: list,
        html_path: str = "",
        screenshot_path: str = "",
        errors: Optional[list] = None,
    ) -> Dict[str, Any]:
        """
        Build final JSON structure for one crawl result.
        """
        return {
            "report_id": report_id,
            "url": url,
            "status": status,
            "crawl_time_ms": crawl_time_ms,
            "title": extracted_data.get("title", ""),
            "html_path": html_path,
            "screenshot_path": screenshot_path,
            "scripts": extracted_data.get("scripts", []),
            "iframes": extracted_data.get("iframes", []),
            "images": extracted_data.get("images", []),
            "links": extracted_data.get("links", []),
            "css_classes": extracted_data.get("css_classes", []),
            "element_ids": extracted_data.get("element_ids", []),
            "selectors": extracted_data.get("selectors", []),
            "ad_signals": ad_signals,
            "errors": errors or [],
        }
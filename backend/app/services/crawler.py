#Calls the other crawler files in order: open page → extract data → detect ad signals → save result

import logging
from typing import Dict, Any, Optional
from pathlib import Path

from ..crawler.browser import render_url
from ..crawler.extractor import PageExtractor
from ..crawler.detector import detect_ads
from ..crawler.storage import CrawlStorage

logger = logging.getLogger(__name__)


class CrawlService:
    """
    Orchestrates the complete crawler pipeline:
    1. Render page with browser (+ capture network requests)
    2. Extract ad-relevant data from HTML
    3. Detect ad candidates from extracted data + network requests
    4. Save clean results
    """

    def __init__(self, storage_base_dir: str = "data/crawl_outputs"):
        """
        Initialize the crawler service.

        Args:
            storage_base_dir: Base directory for storing crawl outputs.
        """
        self.storage = CrawlStorage(base_dir=storage_base_dir)

    def crawl_url(self, url: str, report_id: str, **render_kwargs) -> Dict[str, Any]:
        """
        Crawl a single URL through the complete pipeline.

        Args:
            url: The URL to crawl.
            report_id: Unique identifier for this crawl report.
            **render_kwargs: Additional arguments passed to render_url().

        Returns:
            Dictionary containing the complete crawl result.
        """
        logger.info(f"Starting crawl for URL: {url} (report_id: {report_id})")

        # Step 1: Render the page (with network request capture)
        try:
            logger.debug("Rendering page...")
            render_result = render_url(url, capture_requests=True, **render_kwargs)
        except Exception as exc:
            logger.error(f"Failed to render page {url}: {exc}")
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=str(exc),
                extra_info={"stage": "render"}
            )
            return {
                "url": url,
                "report_id": report_id,
                "status": "failed",
                "error": str(exc),
                "error_file": error_path,
                "stage": "render"
            }

        # Check if rendering succeeded
        if render_result.status != "success":
            logger.warning(f"Page render failed for {url}: {render_result.error}")
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=render_result.error,
                extra_info={
                    "stage": "render",
                    "elapsed_ms": render_result.elapsed_ms
                }
            )
            return {
                "url": url,
                "report_id": report_id,
                "status": "failed",
                "error": render_result.error,
                "error_file": error_path,
                "stage": "render",
                "elapsed_ms": render_result.elapsed_ms
            }

        # Step 2: Extract ad-relevant data from HTML
        try:
            logger.debug("Extracting ad-relevant data from HTML...")
            extractor = PageExtractor(render_result.html, page_url=url)
            extracted_data = extractor.extract_all()
        except Exception as exc:
            logger.error(f"Failed to extract data from {url}: {exc}")
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=str(exc),
                extra_info={
                    "stage": "extract",
                    "html_length": len(render_result.html)
                }
            )
            return {
                "url": url,
                "report_id": report_id,
                "status": "failed",
                "error": str(exc),
                "error_file": error_path,
                "stage": "extract",
                "render": {
                    "status": render_result.status,
                    "elapsed_ms": render_result.elapsed_ms
                }
            }

        # Step 3: Detect ad candidates
        try:
            logger.debug("Detecting ad candidates...")
            # Build detector input: extracted data + network requests
            detector_input = extracted_data.to_dict()
            detector_input["url"] = url
            # Convert captured network requests to dicts for the detector
            detector_input["network_requests"] = [
                req.to_dict() for req in render_result.captured_requests
            ]

            detection_result = detect_ads(detector_input)
        except Exception as exc:
            logger.error(f"Failed to detect ads for {url}: {exc}")
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=str(exc),
                extra_info={
                    "stage": "detect",
                    "extracted_fields": list(extracted_data.to_dict().keys())
                }
            )
            return {
                "url": url,
                "report_id": report_id,
                "status": "failed",
                "error": str(exc),
                "error_file": error_path,
                "stage": "detect",
                "render": {
                    "status": render_result.status,
                    "elapsed_ms": render_result.elapsed_ms
                },
                "extracted": extracted_data.to_dict()
            }

        # Build network request summary (grouped by domain)
        from urllib.parse import urlparse as _urlparse
        from collections import defaultdict as _defaultdict

        page_domain = ""
        try:
            page_domain = _urlparse(url).hostname or ""
        except Exception:
            pass

        first_party_by_domain: dict = _defaultdict(list)
        third_party_by_domain: dict = _defaultdict(list)
        total_requests = len(render_result.captured_requests)
        for req in render_result.captured_requests:
            if req.url.startswith("data:"):
                continue
            try:
                req_host = _urlparse(req.url).hostname or ""
            except Exception:
                continue
            if req_host and page_domain and (
                req_host == page_domain or
                req_host.endswith("." + page_domain) or
                page_domain.endswith("." + req_host)
            ):
                first_party_by_domain[req_host].append(req.url)
            else:
                third_party_by_domain[req_host].append(req.url)

        # Cap URLs per domain to keep output clean
        network_summary = {
            "total": total_requests,
            "first_party": {
                "count": sum(len(urls) for urls in first_party_by_domain.values()),
                "by_domain": {
                    domain: urls[:5] for domain, urls in sorted(first_party_by_domain.items())
                }
            },
            "third_party": {
                "count": sum(len(urls) for urls in third_party_by_domain.values()),
                "by_domain": {
                    domain: urls[:5] for domain, urls in sorted(third_party_by_domain.items())
                }
            },
        }

        # Step 4: Save results
        try:
            logger.debug("Saving crawl results...")
            html_path = self.storage.save_html(report_id, render_result.html)
            screenshot_path = self.storage.save_screenshot(report_id, render_result.screenshot_bytes)

            result_data = {
                "url": url,
                "report_id": report_id,
                "timestamp": self.storage._current_timestamp(),
                "render": {
                    "status": render_result.status,
                    "elapsed_ms": render_result.elapsed_ms,
                    "html_length": len(render_result.html),
                },
                "title": extracted_data.title,
                "network_requests": network_summary,
                "ad_candidates": detection_result.get("ad_candidates", []),
                "summary": detection_result.get("summary", {}),
                "files": {
                    "html": html_path,
                    "screenshot": screenshot_path,
                }
            }

            result_path = self.storage.save_result(report_id, result_data)
            result_data["files"]["result"] = result_path

        except Exception as exc:
            logger.error(f"Failed to save results for {url}: {exc}")
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=str(exc),
                extra_info={"stage": "save"}
            )
            return {
                "url": url,
                "report_id": report_id,
                "status": "failed",
                "error": str(exc),
                "error_file": error_path,
                "stage": "save",
            }

        # Success!
        logger.info(f"Successfully crawled {url} (report_id: {report_id})")
        result_data["status"] = "success"
        return result_data


# ------------------------------------------------------------------
# Quick test (run: python -m app.services.crawler)
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json

    # Configure logging so all pipeline alerts/warnings are visible
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Get URL from command line argument, or use default
    url = sys.argv[1] if len(sys.argv) > 1 else "https://httpbin.org/html"
    report_id = sys.argv[2] if len(sys.argv) > 2 else "test-001"

    print(f"\n{'='*60}")
    print(f"  CocCoc Adblock Crawler - Test Run")
    print(f"{'='*60}")
    print(f"  URL:       {url}")
    print(f"  Report ID: {report_id}")
    print(f"{'='*60}\n")

    # Run the crawl pipeline
    service = CrawlService()
    result = service.crawl_url(
        url=url,
        report_id=report_id,
        headless=True,
        enable_scroll=True,
        page_load_delay_ms=5000,  # Wait 5s for async ad content to load
    )

    print(f"\n{'='*60}")
    print(f"  Crawl Result (status: {result.get('status', 'unknown')})")
    print(f"{'='*60}")
    print(json.dumps(result, indent=2, ensure_ascii=True))
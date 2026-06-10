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

        # Build network request summary grouped by first/third party
        from urllib.parse import urlparse as _urlparse
        from collections import defaultdict as _defaultdict

        page_domain = ""
        try:
            page_domain = _urlparse(url).hostname or ""
        except Exception:
            pass

        # domain -> {path, ...}  (paths deduplicated per domain)
        first_party_counts: dict = _defaultdict(int)
        third_party_paths: dict = _defaultdict(set)  # domain -> set of paths

        total_requests = len(render_result.captured_requests)
        for req in render_result.captured_requests:
            if req.url.startswith("data:"):
                continue
            try:
                parsed = _urlparse(req.url)
                req_host = parsed.hostname or ""
                req_path = parsed.path or "/"
            except Exception:
                continue
            if req_host and page_domain and (
                req_host == page_domain or
                req_host.endswith("." + page_domain) or
                page_domain.endswith("." + req_host)
            ):
                first_party_counts[req_host] += 1
            else:
                third_party_paths[req_host].add(req_path)

        # Third-party: domain + up to 3 unique paths (query strings stripped).
        # This is what the AI rule generator reads — keep it compact.
        third_party_list = []
        for domain in sorted(third_party_paths):
            paths = sorted(third_party_paths[domain])
            third_party_list.append({
                "domain": domain,
                "request_count": len(third_party_paths[domain]),
                "sample_paths": paths[:3],
            })

        network_summary = {
            "total": total_requests,
            "first_party_count": sum(first_party_counts.values()),
            "third_party_count": sum(len(p) for p in third_party_paths.values()),
            # Structured for AI rule generation: domain + path patterns, no query strings
            "third_party": third_party_list,
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
                "environment": render_result.environment,
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
# Quick crawl runner
# Usage: python -m app.services.crawler <url> <report_id> [--env ENV ...]
# ------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from app.crawler.browser import ENVIRONMENTS
    VALID_ENVS = list(ENVIRONMENTS.keys())

    parser = argparse.ArgumentParser(description="CocCoc Adblock Crawler")
    parser.add_argument("url", help="URL to crawl")
    parser.add_argument("report_id", help="Unique report identifier")
    parser.add_argument(
        "--env",
        nargs="+",
        default=["desktop"],
        metavar="ENV",
        help=f"Environment(s) to crawl: {', '.join(VALID_ENVS)}  (or 'all')",
    )
    args = parser.parse_args()

    selected_envs = VALID_ENVS if "all" in args.env else args.env
    unknown = [e for e in selected_envs if e not in VALID_ENVS]
    if unknown:
        print(f"Unknown environment(s): {unknown}. Valid: {VALID_ENVS}")
        raise SystemExit(1)

    service = CrawlService()

    for env in selected_envs:
        report_id_env = f"{args.report_id}-{env}"

        print(f"\n{'='*60}")
        print(f"  CocCoc Adblock Crawler")
        print(f"{'='*60}")
        print(f"  URL:         {args.url}")
        print(f"  Report ID:   {report_id_env}")
        print(f"  Environment: {env}")
        print(f"{'='*60}\n")

        result = service.crawl_url(
            url=args.url,
            report_id=report_id_env,
            headless=True,
            enable_scroll=True,
            environment=env,
        )

        print(json.dumps(result, indent=2, ensure_ascii=True))

    print(f"\n{'='*60}")
    print(f"  Crawl Result (status: {result.get('status', 'unknown')})")
    print(f"{'='*60}")
    print(json.dumps(result, indent=2, ensure_ascii=True))
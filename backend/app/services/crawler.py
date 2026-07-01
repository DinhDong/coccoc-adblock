# Calls the other crawler files in order:
# open page → extract data → detect ad signals → save result
#
# New in this version:
# - Accept ticket_context from CMS/API/CLI.
# - Persist ticket_context into crawl result JSON so AI rule generation can
#   generate a ticket-aware rule patch later.
# - Keep crawler focused on crawling only; it does not classify or solve ticket
#   logic here.

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlparse

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

    ticket_context:
        Optional user/CMS ticket metadata. The crawler does not interpret it
        deeply. It only stores it with the crawl output so later stages can
        generate ticket-aware rules.

        It may also carry a "focus_region" (or "focus") field that scopes which
        part of the page the crawler analyses. This overrides nothing else; it
        only narrows the HTML fed to extraction/detection.

        Example:
            {
                "platform": "ios",
                "problem_type": "content_broken_image",
                "focus_region": "top bar",
                "request": "[iOS][Adblock] - sayhentai.sh: images not displayed",
                "actual": "Images are not displayed when Adblock is enabled",
                "expected": "Images should be displayed",
                "steps": [
                    "Open chapter page",
                    "Enable Adblock mode"
                ],
                "target_to_preserve": [
                    "chapter images",
                    "main content"
                ],
                "target_to_block": []
            }
    """

    def __init__(self, storage_base_dir: str = "data/crawl_outputs"):
        """
        Initialize the crawler service.

        Args:
            storage_base_dir: Base directory for storing crawl outputs.
        """
        self.storage = CrawlStorage(base_dir=storage_base_dir)

    def crawl_url(
        self,
        url: str,
        report_id: str,
        ticket_context: Optional[Dict[str, Any]] = None,
        focus_region: Optional[str] = None,
        **render_kwargs,
    ) -> Dict[str, Any]:
        """
        Crawl a single URL through the complete pipeline.

        Args:
            url: The URL to crawl.
            report_id: Unique identifier for this crawl report.
            ticket_context: Optional user/CMS ticket context. This is saved
                into the crawl result JSON and later consumed by rule generation.
            **render_kwargs: Additional arguments passed to render_url().

        Returns:
            Dictionary containing the complete crawl result.
        """
        safe_ticket_context = _normalise_ticket_context(ticket_context)

        # Focus region is a property of the ticket context. An explicit
        # focus_region argument (e.g. from a CLI flag) overrides it, and the
        # effective value is written back so it is persisted with the crawl.
        effective_focus = (
            focus_region
            or safe_ticket_context.get("focus_region")
            or safe_ticket_context.get("focus")
            or ""
        )
        if effective_focus:
            safe_ticket_context["focus_region"] = effective_focus

        logger.info(
            "Starting crawl for URL: %s (report_id: %s, ticket_type: %s, focus: %s)",
            url,
            report_id,
            safe_ticket_context.get("problem_type", "unknown"),
            effective_focus or "none",
        )

        # Step 1: Render the page with network request capture. When a focus
        # region is set, render_url scopes the HTML, screenshot, and overlay
        # scan to that region so every downstream stage follows the focus.
        try:
            logger.debug("Rendering page...")
            render_result = render_url(
                url,
                capture_requests=True,
                focus_region=effective_focus or None,
                **render_kwargs,
            )
        except Exception as exc:
            logger.error("Failed to render page %s: %s", url, exc)
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=str(exc),
                extra_info={
                    "stage": "render",
                    "ticket_context": safe_ticket_context,
                },
            )
            return _failure_response(
                url=url,
                report_id=report_id,
                stage="render",
                error=str(exc),
                error_file=error_path,
                ticket_context=safe_ticket_context,
            )

        # Check if rendering succeeded.
        if render_result.status != "success":
            logger.warning(
                "Page render failed for %s: %s",
                url,
                render_result.error,
            )
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=render_result.error,
                extra_info={
                    "stage": "render",
                    "elapsed_ms": render_result.elapsed_ms,
                    "ticket_context": safe_ticket_context,
                },
            )
            return _failure_response(
                url=url,
                report_id=report_id,
                stage="render",
                error=render_result.error,
                error_file=error_path,
                ticket_context=safe_ticket_context,
                extra={
                    "elapsed_ms": render_result.elapsed_ms,
                },
            )

        # Focus scoping happens inside render_url (Step 1) so the HTML,
        # screenshot, and overlay scan are all narrowed together. Here we only
        # read back what it resolved for the crawl result metadata.
        focus_meta = getattr(render_result, "focus", None) or {}
        if effective_focus and not focus_meta.get("matched"):
            logger.warning(
                "Focus region '%s' did not match any element — crawled full page",
                effective_focus,
            )

        # Step 3: Extract ad-relevant data from rendered HTML.
        try:
            logger.debug("Extracting ad-relevant data from HTML...")
            extractor = PageExtractor(render_result.html, page_url=url)
            extracted_data = extractor.extract_all()
        except Exception as exc:
            logger.error("Failed to extract data from %s: %s", url, exc)
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=str(exc),
                extra_info={
                    "stage": "extract",
                    "html_length": len(render_result.html or ""),
                    "ticket_context": safe_ticket_context,
                },
            )
            return _failure_response(
                url=url,
                report_id=report_id,
                stage="extract",
                error=str(exc),
                error_file=error_path,
                ticket_context=safe_ticket_context,
                extra={
                    "render": {
                        "status": render_result.status,
                        "elapsed_ms": render_result.elapsed_ms,
                    },
                },
            )

        # Step 3: Detect ad candidates.
        try:
            logger.debug("Detecting ad candidates...")

            detector_input = extracted_data.to_dict()
            detector_input["url"] = url
            detector_input["network_requests"] = [
                req.to_dict() for req in render_result.captured_requests
            ]
            # Fixed/sticky elements captured by JS evaluation — invisible to HTML parsing
            detector_input["fixed_elements"] = render_result.fixed_elements

            detection_result = detect_ads(detector_input)
        except Exception as exc:
            logger.error("Failed to detect ads for %s: %s", url, exc)
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=str(exc),
                extra_info={
                    "stage": "detect",
                    "extracted_fields": list(extracted_data.to_dict().keys()),
                    "ticket_context": safe_ticket_context,
                },
            )
            return _failure_response(
                url=url,
                report_id=report_id,
                stage="detect",
                error=str(exc),
                error_file=error_path,
                ticket_context=safe_ticket_context,
                extra={
                    "render": {
                        "status": render_result.status,
                        "elapsed_ms": render_result.elapsed_ms,
                    },
                    "extracted": extracted_data.to_dict(),
                },
            )

        # Step 4: Build compact network summary for AI rule generation.
        network_summary = _build_network_summary(
            url=url,
            captured_requests=render_result.captured_requests,
        )

        # Step 5: Save results.
        try:
            logger.debug("Saving crawl results...")

            html_path = self.storage.save_html(
                report_id,
                render_result.html,
            )
            screenshot_path = self.storage.save_screenshot(
                report_id,
                render_result.screenshot_bytes,
            )

            result_data = {
                "url": url,
                "report_id": report_id,
                "status": "success",
                "timestamp": self.storage._current_timestamp(),
                "environment": getattr(render_result, "environment", "desktop"),
                "ticket_context": safe_ticket_context,
                "render": {
                    "status": render_result.status,
                    "elapsed_ms": render_result.elapsed_ms,
                    "html_length": len(render_result.html or ""),
                },
                "focus_region": {
                    "requested": focus_meta.get("requested", effective_focus),
                    "selector": focus_meta.get("selector", ""),
                    "method": focus_meta.get("method", "none"),
                    "matched": bool(focus_meta.get("matched", False)),
                } if effective_focus else None,
                "title": extracted_data.title,
                "network_requests": network_summary,
                "ad_candidates": detection_result.get("ad_candidates", []),
                "summary": detection_result.get("summary", {}),
                "files": {
                    "html": html_path,
                    "screenshot": screenshot_path,
                },
            }

            result_path = self.storage.save_result(report_id, result_data)
            result_data["files"]["result"] = result_path

        except Exception as exc:
            logger.error("Failed to save results for %s: %s", url, exc)
            error_path = self.storage.save_error(
                report_id=report_id,
                url=url,
                error_message=str(exc),
                extra_info={
                    "stage": "save",
                    "ticket_context": safe_ticket_context,
                },
            )
            return _failure_response(
                url=url,
                report_id=report_id,
                stage="save",
                error=str(exc),
                error_file=error_path,
                ticket_context=safe_ticket_context,
            )

        logger.info(
            "Successfully crawled %s (report_id: %s, env: %s)",
            url,
            report_id,
            result_data.get("environment"),
        )
        return result_data


def _normalise_ticket_context(
    ticket_context: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Make ticket_context safe to store in JSON.

    This function intentionally does not classify the ticket deeply. Ticket
    classification should live in a separate service later, for example:
        app.services.ticket_context.normalize_ticket_context()

    Here we only:
    - accept None as {},
    - accept dict-like objects,
    - prevent non-JSON-serializable values from breaking save_result().
    """
    if ticket_context is None:
        return {}

    if not isinstance(ticket_context, Mapping):
        logger.warning(
            "ticket_context should be a dict-like object. Got: %s",
            type(ticket_context).__name__,
        )
        return {
            "raw": str(ticket_context),
        }

    return _make_json_safe(dict(ticket_context))


def _make_json_safe(value: Any) -> Any:
    """
    Recursively convert values into JSON-safe data.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        return {
            str(key): _make_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            _make_json_safe(item)
            for item in value
        ]

    return str(value)


def _build_network_summary(
    url: str,
    captured_requests: list,
) -> Dict[str, Any]:
    """
    Build a compact network request summary grouped by first/third party.

    Output is intentionally compact because rule_generator.py / prompt_builder.py
    will feed this into the LLM later.

    Returns:
        {
            "total": int,
            "first_party_count": int,
            "third_party_count": int,
            "third_party": [
                {
                    "domain": "ads.example.com",
                    "request_count": 3,
                    "sample_paths": ["/ad.js", "/banner"]
                }
            ]
        }
    """
    page_domain = ""

    try:
        page_domain = urlparse(url).hostname or ""
    except Exception:
        logger.debug("Could not parse page domain from URL: %s", url)

    first_party_counts: dict[str, int] = defaultdict(int)
    third_party_counts: dict[str, int] = defaultdict(int)
    third_party_paths: dict[str, set[str]] = defaultdict(set)

    total_requests = len(captured_requests or [])

    for req in captured_requests or []:
        req_url = getattr(req, "url", "")

        if not req_url or req_url.startswith("data:"):
            continue

        try:
            parsed = urlparse(req_url)
            req_host = parsed.hostname or ""
            req_path = parsed.path or "/"
        except Exception:
            continue

        if not req_host:
            continue

        if _is_first_party(req_host=req_host, page_domain=page_domain):
            first_party_counts[req_host] += 1
            continue

        third_party_counts[req_host] += 1
        third_party_paths[req_host].add(req_path)

    third_party_list = []

    for domain in sorted(third_party_counts):
        paths = sorted(third_party_paths.get(domain, set()))
        third_party_list.append(
            {
                "domain": domain,
                "request_count": third_party_counts[domain],
                "sample_paths": paths[:3],
            }
        )

    return {
        "total": total_requests,
        "first_party_count": sum(first_party_counts.values()),
        "third_party_count": sum(third_party_counts.values()),
        "third_party": third_party_list,
    }


def _is_first_party(req_host: str, page_domain: str) -> bool:
    """
    Return True if request host appears to be first-party relative to page_domain.
    """
    if not req_host or not page_domain:
        return False

    return (
        req_host == page_domain
        or req_host.endswith("." + page_domain)
        or page_domain.endswith("." + req_host)
    )


def _failure_response(
    url: str,
    report_id: str,
    stage: str,
    error: str,
    error_file: str,
    ticket_context: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Standard failure payload for every crawler stage.
    """
    payload = {
        "url": url,
        "report_id": report_id,
        "status": "failed",
        "stage": stage,
        "error": error,
        "error_file": error_file,
        "ticket_context": ticket_context,
    }

    if extra:
        payload.update(extra)

    return payload


def _load_ticket_context_from_cli(
    ticket_context_json: str = "",
    ticket_context_file: str = "",
) -> Dict[str, Any]:
    """
    Helper for local CLI testing.

    You can pass either:
        --ticket-context-json '{"platform":"ios","problem_type":"content_broken_image"}'

    Or:
        --ticket-context-file ./ticket_context.json
    """
    if ticket_context_json and ticket_context_file:
        raise ValueError(
            "Use only one of --ticket-context-json or --ticket-context-file."
        )

    if ticket_context_json:
        return json.loads(ticket_context_json)

    if ticket_context_file:
        path = Path(ticket_context_file)
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    return {}


# ------------------------------------------------------------------
# Quick crawl runner
# Usage:
#   python -m app.services.crawler <url> <report_id> [--env ENV ...]
#
# With ticket context:
#   python -m app.services.crawler \
#       "https://example.com" \
#       "example-ios" \
#       --env ios \
#       --ticket-context-file ./ticket_context.json
#
# Or:
#   python -m app.services.crawler \
#       "https://example.com" \
#       "example-ios" \
#       --env ios \
#       --ticket-context-json '{"platform":"ios","problem_type":"content_broken_image"}'
# ------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

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
        help=(
            f"Environment(s) to crawl: {', '.join(VALID_ENVS)} "
            "(or 'all')"
        ),
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Open a visible browser window (helps bypass Cloudflare connection resets on some sites)",
    )
    parser.add_argument(
        "--ticket-context-json",
        default="",
        help="Raw JSON string containing ticket context.",
    )
    parser.add_argument(
        "--ticket-context-file",
        default="",
        help="Path to a JSON file containing ticket context.",
    )
    parser.add_argument(
        "--focus",
        default="",
        metavar="REGION",
        help=(
            "Scope extraction to a specific page region before analysis. "
            "Accepts semantic keywords (header, footer, sidebar, main, nav) "
            "or a free-form description (e.g. 'top banner area', 'right sidebar'). "
            "Network requests are always captured from the full page."
        ),
    )

    args = parser.parse_args()

    selected_envs = VALID_ENVS if "all" in args.env else args.env
    unknown = [env for env in selected_envs if env not in VALID_ENVS]

    if unknown:
        print(f"Unknown environment(s): {unknown}. Valid: {VALID_ENVS}")
        raise SystemExit(1)

    ticket_context = _load_ticket_context_from_cli(
        ticket_context_json=args.ticket_context_json,
        ticket_context_file=args.ticket_context_file,
    )

    service = CrawlService()
    last_result: Dict[str, Any] = {}

    for env in selected_envs:
        # DO NOT ADD THIS BACK — environment is stored inside the JSON, not in the filename.
        # report_id_env = f"{args.report_id}-{env}"

        print(f"\n{'=' * 60}")
        print("  CocCoc Adblock Crawler")
        print(f"{'=' * 60}")
        print(f"  URL:         {args.url}")
        print(f"  Report ID:   {args.report_id}")
        print(f"  Environment: {env}")
        print(
            "  Ticket type: "
            f"{ticket_context.get('problem_type', 'unknown')}"
        )
        print(f"{'=' * 60}\n")

        last_result = service.crawl_url(
            url=args.url,
            report_id=args.report_id,  # DO NOT change to report_id_env — env goes inside the JSON
            ticket_context=ticket_context,
            focus_region=args.focus or None,
            headless=not args.no_headless,
            enable_scroll=True,
            environment=env,
        )

        print(json.dumps(last_result, indent=2, ensure_ascii=False))

    print(f"\n{'=' * 60}")
    print(
        "  Crawl Result "
        f"(status: {last_result.get('status', 'unknown')})"
    )
    print(f"{'=' * 60}")
    print(json.dumps(last_result, indent=2, ensure_ascii=False))
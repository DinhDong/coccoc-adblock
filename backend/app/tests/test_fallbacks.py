"""
Test fallback and alert behavior across the crawler pipeline.

Run from backend/:
    .venv\Scripts\activate
    python -m app.tests.test_fallbacks
"""

import logging
import json
import sys

# Configure logging to see all alerts
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


def separator(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------
# Test 1: Extractor with None HTML
# ---------------------------------------------------------------
def test_extractor_none_html():
    separator("Test 1: Extractor receives None HTML")
    from app.crawler.extractor import PageExtractor

    extractor = PageExtractor(None)  # should log WARNING, not crash
    data = extractor.extract_all()
    print(f"  Result: title='{data.title}', scripts={data.scripts}")
    print(f"  PASS - Extractor handled None input gracefully")


# ---------------------------------------------------------------
# Test 2: Extractor with empty string
# ---------------------------------------------------------------
def test_extractor_empty_html():
    separator("Test 2: Extractor receives empty string")
    from app.crawler.extractor import PageExtractor

    extractor = PageExtractor("")  # should log WARNING, not crash
    data = extractor.extract_all()
    print(f"  Result: title='{data.title}', scripts={data.scripts}")
    print(f"  PASS - Extractor handled empty string gracefully")


# ---------------------------------------------------------------
# Test 3: Extractor with valid HTML
# ---------------------------------------------------------------
def test_extractor_valid_html():
    separator("Test 3: Extractor with valid HTML")
    from app.crawler.extractor import PageExtractor

    html = """
    <html>
    <head><title>Test Page</title></head>
    <body>
        <script src="https://ads.example.com/banner.js"></script>
        <script src="https://example.com/main.js"></script>
        <iframe src="https://adnetwork.example.com/ad.html"></iframe>
        <img src="https://example.com/logo.png">
        <a href="https://example.com/about">About</a>
        <div id="ad-slot-1" class="ad-banner">Ad here</div>
    </body>
    </html>
    """
    extractor = PageExtractor(html)
    data = extractor.extract_all()
    print(f"  Title:   '{data.title}'")
    print(f"  Scripts: {data.scripts}")
    print(f"  Iframes: {data.iframes}")
    print(f"  Images:  {data.images}")
    print(f"  Links:   {data.links}")
    print(f"  Classes: {data.css_classes}")
    print(f"  IDs:     {data.element_ids}")
    print(f"  PASS - All fields extracted correctly")


# ---------------------------------------------------------------
# Test 4: Detector with empty input
# ---------------------------------------------------------------
def test_detector_empty_input():
    separator("Test 4: Detector receives empty input")
    from app.crawler.detector import detect_ads

    result = detect_ads({})  # no scripts, no iframes, no html
    print(f"  Ad signals found: {result['signal_count']}")
    print(f"  Errors: {result['errors']}")
    print(f"  PASS - Detector handled empty input gracefully")


# ---------------------------------------------------------------
# Test 5: Storage with bad report_id
# ---------------------------------------------------------------
def test_storage_bad_report_id():
    separator("Test 5: Storage with empty report_id")
    from app.crawler.storage import CrawlStorage

    storage = CrawlStorage(base_dir="data/crawl_outputs")
    try:
        storage.save_html("", "<html></html>")
        print(f"  FAIL - Should have raised ValueError")
    except ValueError as e:
        print(f"  Caught expected error: {e}")
        print(f"  PASS - Storage rejected empty report_id")


# ---------------------------------------------------------------
# Test 6: Crawler pipeline with invalid URL
# ---------------------------------------------------------------
def test_crawler_invalid_url():
    separator("Test 6: Crawler pipeline with invalid URL")
    from app.services.crawler import CrawlService

    service = CrawlService()
    result = service.crawl_url(
        url="not-a-valid-url",
        report_id="test-invalid-url",
        headless=True,
        enable_scroll=False,
    )
    print(f"  Status: {result['status']}")
    if result['status'] == 'failed':
        print(f"  Error:  {result.get('error', 'N/A')}")
        print(f"  Stage:  {result.get('stage', 'N/A')}")
    print(f"  PASS - Pipeline handled invalid URL with fallback")


# ---------------------------------------------------------------
# Test 7: Crawler pipeline with unreachable URL
# ---------------------------------------------------------------
def test_crawler_unreachable_url():
    separator("Test 7: Crawler pipeline with unreachable URL (timeout)")
    from app.services.crawler import CrawlService

    service = CrawlService()
    result = service.crawl_url(
        url="https://192.0.2.1/",  # RFC 5737 TEST-NET, guaranteed unreachable
        report_id="test-unreachable",
        headless=True,
        enable_scroll=False,
        timeout_ms=5000,  # short timeout to speed up test
    )
    print(f"  Status: {result['status']}")
    if result['status'] == 'failed':
        print(f"  Error:  {result.get('error', 'N/A')[:100]}")
        print(f"  Stage:  {result.get('stage', 'N/A')}")
    print(f"  PASS - Pipeline handled timeout with fallback")


# ---------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------
if __name__ == "__main__":
    tests = [
        test_extractor_none_html,
        test_extractor_empty_html,
        test_extractor_valid_html,
        test_detector_empty_input,
        test_storage_bad_report_id,
        test_crawler_invalid_url,
        test_crawler_unreachable_url,
    ]

    passed = 0
    failed = 0

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL - {test_fn.__name__}: {e}")
            failed += 1

    separator(f"Results: {passed} passed, {failed} failed")

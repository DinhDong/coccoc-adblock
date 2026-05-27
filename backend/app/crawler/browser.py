#Handles the browser using Playwright:
# open URL
# wait for page load
# scroll the page
# capture screenshot
# collect rendered HTML
# capture network requests
# handle timeout or page errors

"""Browser automation helpers using Playwright."""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_MS = 30_000

# Suppress automation signals that bot detectors check for
_STEALTH_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-infobars",
    "--disable-extensions-except=",
    "--disable-plugins-discovery",
]

# Matches a real Chrome on Windows 10 (update major version periodically)
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# Injected before any page JS runs when playwright-stealth is unavailable.
# Covers the most-checked properties without the full stealth library.
_FALLBACK_STEALTH_SCRIPT = """
(() => {
    // Remove the most-flagged automation marker
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Restore chrome runtime that headless Chrome omits
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};

    // Fake a non-empty plugins list (headless has 0)
    Object.defineProperty(navigator, 'plugins', {
        get: () => { const p = [1, 2, 3]; p.item = i => p[i]; p.namedItem = n => null; p.refresh = () => {}; return p; }
    });

    // Consistent language list
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // Spoof notification permission so it doesn't return "denied" immediately
    const origQuery = window.Notification
        ? window.Notification.requestPermission.bind(window.Notification)
        : null;
    if (origQuery) {
        window.Notification.requestPermission = () => Promise.resolve('default');
    }

    // WebGL vendor/renderer — headless exposes "SwiftShader" which is flagged
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';         // VENDOR
        if (param === 37446) return 'Intel Iris OpenGL Engine'; // RENDERER
        return getParam.call(this, param);
    };
})();
"""


@dataclass
class CapturedRequest:
    """A single network request captured during page load."""
    url: str
    resource_type: str   # "script", "stylesheet", "image", "xhr", "fetch", "document", etc.
    method: str = "GET"

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "resource_type": self.resource_type,
            "method": self.method,
        }


@dataclass
class RenderResult:
    """Result of rendering a page with a browser."""
    url: str
    status: str
    html: str = ""
    screenshot_bytes: bytes = b""
    screenshot_path: str = ""
    error: str = ""
    elapsed_ms: int = 0
    captured_requests: List[CapturedRequest] = field(default_factory=list)


def _import_playwright() -> Any:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        return sync_playwright, PlaywrightError, PlaywrightTimeoutError
    except ImportError as exc:
        raise ImportError(
            "Playwright is required for browser automation. "
            "Install it with `pip install playwright` and run `playwright install`."
        ) from exc


def _apply_stealth(page: Any, user_agent: Optional[str] = None) -> None:
    """Apply stealth patches to a Playwright page to suppress bot-detection signals."""
    try:
        from playwright_stealth import Stealth
        Stealth(
            chrome_runtime=True,
            navigator_user_agent_override=user_agent or _DEFAULT_USER_AGENT,
        ).apply_stealth_sync(page)
        logger.debug("playwright-stealth applied")
    except ImportError:
        logger.debug("playwright-stealth not installed; using fallback stealth script")
        page.add_init_script(_FALLBACK_STEALTH_SCRIPT)


def _scroll_page(page: Any, scroll_step: int = 1000, max_scrolls: int = 30, delay_seconds: float = 0.1) -> None:
    """Scroll the page gradually to load lazy content."""
    previous_height = page.evaluate("() => document.documentElement.scrollHeight")
    for _ in range(max_scrolls):
        page.evaluate("(step) => window.scrollBy(0, step)", scroll_step)
        time.sleep(delay_seconds)
        current_height = page.evaluate("() => document.documentElement.scrollHeight")
        if current_height == previous_height:
            break
        previous_height = current_height


def render_url(
    url: str,
    screenshot_path: Optional[str] = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    wait_until: str = "load",
    browser_type: str = "chromium",
    headless: bool = True,
    enable_scroll: bool = True,
    scroll_step: int = 1000,
    max_scrolls: int = 10,
    network_idle_timeout_ms: Optional[int] = 8000,
    capture_requests: bool = True,
    page_load_delay_ms: int = 1500,
    stealth: bool = True,
    user_agent: Optional[str] = None,
) -> RenderResult:
    """Render a URL in Playwright and return the rendered page data."""
    sync_playwright, PlaywrightError, PlaywrightTimeoutError = _import_playwright()
    start_time = time.perf_counter()
    result = RenderResult(url=url, status="failed")

    try:
        with sync_playwright() as playwright:
            launch_kwargs: Dict[str, Any] = {"headless": headless}
            if stealth:
                launch_kwargs["args"] = _STEALTH_LAUNCH_ARGS

            browser = getattr(playwright, browser_type).launch(**launch_kwargs)

            context_kwargs: Dict[str, Any] = {}
            if stealth:
                context_kwargs.update({
                    "user_agent": user_agent or _DEFAULT_USER_AGENT,
                    "viewport": {"width": 1920, "height": 1080},
                    "locale": "en-US",
                    "timezone_id": "America/New_York",
                    "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
                })
            elif user_agent:
                context_kwargs["user_agent"] = user_agent

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            if stealth:
                _apply_stealth(page, user_agent=user_agent or _DEFAULT_USER_AGENT)

            # Capture all network requests during page load
            seen_urls: set = set()

            if capture_requests:
                def _on_request(request):
                    req_url = request.url
                    if req_url not in seen_urls:
                        seen_urls.add(req_url)
                        result.captured_requests.append(CapturedRequest(
                            url=req_url,
                            resource_type=request.resource_type,
                            method=request.method,
                        ))

                page.on("request", _on_request)

            page.goto(url, wait_until=wait_until, timeout=timeout_ms)

            # Wait for network to settle (async ad scripts, lazy JS)
            try:
                idle_timeout = network_idle_timeout_ms or timeout_ms
                page.wait_for_load_state("networkidle", timeout=idle_timeout)
            except Exception:
                logger.debug("networkidle timeout — continuing with what we have")

            # Extra delay to let remaining async content fire (ads, trackers)
            if page_load_delay_ms > 0:
                delay_sec = page_load_delay_ms / 1000
                logger.debug(f"Waiting {delay_sec:.1f}s for async content to load...")
                time.sleep(delay_sec)

            if enable_scroll:
                _scroll_page(page, scroll_step=scroll_step, max_scrolls=max_scrolls)
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(0.3)

            html = page.content()
            screenshot_bytes = page.screenshot(full_page=True, timeout=timeout_ms)

            if screenshot_path:
                screenshot_file = Path(screenshot_path)
                screenshot_file.parent.mkdir(parents=True, exist_ok=True)
                screenshot_file.write_bytes(screenshot_bytes)
                result.screenshot_path = str(screenshot_file)

            result.html = html
            result.screenshot_bytes = screenshot_bytes
            result.status = "success"

    except PlaywrightTimeoutError as exc:
        error_message = f"Timeout while loading {url}: {exc}"
        logger.exception(error_message)
        result.error = error_message
    except PlaywrightError as exc:
        error_message = f"Playwright error while rendering {url}: {exc}"
        logger.exception(error_message)
        result.error = error_message
    except Exception as exc:
        error_message = f"Unexpected error while rendering {url}: {exc}"
        logger.exception(error_message)
        result.error = error_message
    finally:
        result.elapsed_ms = int((time.perf_counter() - start_time) * 1000)

    return result


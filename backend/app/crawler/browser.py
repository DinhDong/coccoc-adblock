# Handles the browser using Playwright:
# open URL
# wait for page load
# scroll the page
# capture screenshot
# collect rendered HTML
# capture network requests
# handle timeout or page errors
#
# New in this version:
# - Capture fixed/sticky floating elements.
# - Capture fullscreen popup overlays/backdrops that block page interaction.
# - Return richer fixed_elements data for detector.py:
#     selector, position, size, z-index, background, opacity, pointer-events,
#     whether it looks like a fullscreen overlay, whether it has a close button.

"""Browser automation helpers using Playwright."""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# Device profiles for multi-environment crawling.
# Each profile sets the browser engine + context options that make the page
# behave as if it were visited from that device.
ENVIRONMENTS: Dict[str, Dict] = {
    "desktop": {
        "browser_type": "chromium",
        "user_agent": _DEFAULT_USER_AGENT,
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 1.0,
        "is_mobile": False,
        "has_touch": False,
    },
    "android": {
        "browser_type": "chromium",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
    },
    "ios": {
        # WebKit engine gives the closest match to real Mobile Safari behaviour
        "browser_type": "webkit",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.5 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
    },
}

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
    const origRequestPermission = window.Notification
        ? window.Notification.requestPermission.bind(window.Notification)
        : null;
    if (origRequestPermission) {
        window.Notification.requestPermission = () => Promise.resolve('default');
    }

    // WebGL vendor/renderer — headless exposes "SwiftShader" which is flagged
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';                // VENDOR
        if (param === 37446) return 'Intel Iris OpenGL Engine';  // RENDERER
        return getParam.call(this, param);
    };
})();
"""

# Evaluates in the live browser to find:
# - position:fixed / position:sticky floating ad banners
# - fullscreen fixed overlays / backdrops that block user interaction
#
# BeautifulSoup can't see computed styles, so these are otherwise invisible.
_FIXED_ELEMENT_SCRIPT = """
() => {
    const MIN_W = 100;
    const MIN_H = 20;
    const results = [];
    const seen = new Set();

    const viewportW = Math.max(window.innerWidth || 0, document.documentElement.clientWidth || 0, 1);
    const viewportH = Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0, 1);
    const viewportArea = Math.max(viewportW * viewportH, 1);

    const GENERATED_CLASS_PREFIXES = [
        'jsx-',
        'css-',
        'sc-',
        'style-',
        'chakra-',
        'mantine-',
        'ant-',
    ];

    const AD_OR_OVERLAY_RE = /(^|[-_\\s])(ad|ads|adserver|ad-server|adslot|ad-slot|adunit|ad-unit|banner|popup|pop|modal|overlay|backdrop|mask|interstitial|sponsor|sponsored|promo|advert|advertise|advertisement)([-_\\s]|$)/i;
    const OVERLAY_RE = /(overlay|backdrop|modal|popup|dialog|mask|layer|interstitial)/i;
    const SITE_CHROME_RE = /(header|navbar|navigation|nav-|menu|search|footer|breadcrumb)/i;
    const IGNORE_TEXT_RE = /(cookie|consent|accept cookies|newsletter|subscribe|chat support)/i;

    const escapeIdent = (value) => {
        if (window.CSS && typeof window.CSS.escape === 'function') {
            return window.CSS.escape(value);
        }
        return String(value).replace(/[^A-Za-z0-9_-]/g, (ch) => '\\\\' + ch);
    };

    const isVisible = (el) => {
        if (!el || !(el instanceof Element)) return false;
        const st = window.getComputedStyle(el);
        if (!st) return false;
        if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const parseZIndex = (value) => {
        if (!value || value === 'auto') return 0;
        const parsed = parseInt(value, 10);
        return Number.isFinite(parsed) ? parsed : 0;
    };

    const parseColor = (color) => {
        const match = /rgba?\\(([^)]+)\\)/i.exec(color || '');
        if (!match) {
            return { r: 255, g: 255, b: 255, a: 0 };
        }

        const parts = match[1].split(',').map((part) => part.trim());
        const r = Number.parseFloat(parts[0] || '255');
        const g = Number.parseFloat(parts[1] || '255');
        const b = Number.parseFloat(parts[2] || '255');
        const a = parts.length >= 4 ? Number.parseFloat(parts[3] || '1') : 1;

        return {
            r: Number.isFinite(r) ? r : 255,
            g: Number.isFinite(g) ? g : 255,
            b: Number.isFinite(b) ? b : 255,
            a: Number.isFinite(a) ? a : 1,
        };
    };

    const isDarkColor = (color) => {
        const parsed = parseColor(color);
        const luminance = (0.2126 * parsed.r) + (0.7152 * parsed.g) + (0.0722 * parsed.b);
        return parsed.a >= 0.25 && luminance <= 130;
    };

    const classListOf = (el) => {
        try {
            return Array.from(el.classList || []).filter(Boolean);
        } catch (_) {
            const raw = typeof el.className === 'string' ? el.className : '';
            return raw.split(/\\s+/).filter(Boolean);
        }
    };

    const isGeneratedClass = (cls) => {
        return GENERATED_CLASS_PREFIXES.some((prefix) => cls.startsWith(prefix));
    };

    const firstMeaningfulClass = (classes) => {
        for (const cls of classes || []) {
            if (!cls) continue;
            if (isGeneratedClass(cls)) continue;
            return cls;
        }
        return '';
    };

    const bestSemanticClass = (classes) => {
        for (const cls of classes || []) {
            if (!cls) continue;
            if (isGeneratedClass(cls)) continue;
            if (AD_OR_OVERLAY_RE.test(cls)) return cls;
        }

        return firstMeaningfulClass(classes);
    };

    const buildSelector = (el) => {
        const tag = el.tagName.toLowerCase();

        if (el.id) {
            return `${tag}#${escapeIdent(el.id)}`;
        }

        const classes = classListOf(el);
        const semanticClass = bestSemanticClass(classes);

        if (semanticClass) {
            return `${tag}.${escapeIdent(semanticClass)}`;
        }

        // Last resort: a structural selector. It is less stable than a class/id,
        // but still safer than hiding every fixed div on the page.
        const path = [];
        let current = el;

        while (
            current &&
            current.nodeType === 1 &&
            current !== document.body &&
            current !== document.documentElement &&
            path.length < 5
        ) {
            const currentTag = current.tagName.toLowerCase();
            let index = 1;
            let sibling = current.previousElementSibling;

            while (sibling) {
                if (sibling.tagName === current.tagName) {
                    index += 1;
                }
                sibling = sibling.previousElementSibling;
            }

            path.unshift(`${currentTag}:nth-of-type(${index})`);
            current = current.parentElement;
        }

        if (path.length) {
            return `body > ${path.join(' > ')}`;
        }

        return tag;
    };

    const hasCloseButton = (el) => {
        try {
            return Boolean(
                el.querySelector(
                    [
                        'button',
                        '[role="button"]',
                        '[aria-label*="close" i]',
                        '[title*="close" i]',
                        '[class*="close" i]',
                        '[id*="close" i]',
                        '[class*="dismiss" i]',
                        '[id*="dismiss" i]',
                        '[class*="times" i]',
                        '[class*="xmark" i]',
                    ].join(',')
                )
            );
        } catch (_) {
            return false;
        }
    };

    for (const el of document.querySelectorAll('*')) {
        if (!isVisible(el)) continue;

        const tag = el.tagName.toLowerCase();
        if (tag === 'html' || tag === 'body') continue;

        const st = window.getComputedStyle(el);
        const position = st.position;
        const rect = el.getBoundingClientRect();

        const width = Math.round(rect.width);
        const height = Math.round(rect.height);

        if (width < MIN_W || height < MIN_H) continue;

        const left = Math.round(rect.left);
        const top = Math.round(rect.top);
        const right = Math.round(rect.right);
        const bottom = Math.round(rect.bottom);

        const clippedW = Math.max(0, Math.min(rect.right, viewportW) - Math.max(rect.left, 0));
        const clippedH = Math.max(0, Math.min(rect.bottom, viewportH) - Math.max(rect.top, 0));
        const viewportCoverage = (clippedW * clippedH) / viewportArea;

        const classes = classListOf(el);
        const classText = classes.join(' ');
        const attrsText = `${tag} ${el.id || ''} ${classText} ${el.getAttribute('role') || ''} ${el.getAttribute('aria-label') || ''}`;

        const zIndex = parseZIndex(st.zIndex);
        const backgroundColor = st.backgroundColor || '';
        const opacity = Number.parseFloat(st.opacity || '1');
        const pointerEvents = st.pointerEvents || '';

        const overlayKeyword = OVERLAY_RE.test(attrsText);
        const adKeyword = AD_OR_OVERLAY_RE.test(attrsText);
        const siteChrome = (
            tag === 'header' ||
            tag === 'nav' ||
            tag === 'footer' ||
            SITE_CHROME_RE.test(attrsText)
        );

        const darkOverlay = isDarkColor(backgroundColor) || opacity < 0.95;
        const closeButton = hasCloseButton(el);

        const isFixedLike = position === 'fixed' || position === 'sticky';
        const isAbsoluteOverlay = position === 'absolute' && viewportCoverage >= 0.65 && zIndex >= 10;

        const isFullscreenOverlay = (
            (position === 'fixed' || isAbsoluteOverlay) &&
            viewportCoverage >= 0.65 &&
            width >= viewportW * 0.70 &&
            height >= viewportH * 0.60 &&
            left <= viewportW * 0.20 &&
            top <= viewportH * 0.25
        );

        if (!isFixedLike && !isAbsoluteOverlay) continue;

        const text = (el.innerText || '').toLowerCase().slice(0, 300);

        // Avoid noisy site UI candidates. Do not skip if it is a real overlay.
        if (
            siteChrome &&
            !isFullscreenOverlay &&
            !adKeyword &&
            !overlayKeyword
        ) {
            continue;
        }

        // Avoid cookie/chat/newsletter bars unless they are full-screen overlays
        // with ad/overlay signals.
        if (
            IGNORE_TEXT_RE.test(text) &&
            !isFullscreenOverlay &&
            !adKeyword &&
            !overlayKeyword
        ) {
            continue;
        }

        const links = [];
        for (const a of el.querySelectorAll('a[href]')) {
            try {
                const href = new URL(a.href, location.href).href;
                if (!href.startsWith(location.origin)) {
                    links.push(href);
                }
            } catch (_) {}
        }

        const iframes = [];
        for (const frame of el.querySelectorAll('iframe[src]')) {
            const src = frame.getAttribute('src') || '';
            if (!src) continue;

            try {
                const fullSrc = new URL(src, location.href).href;
                if (!fullSrc.startsWith(location.origin)) {
                    iframes.push(fullSrc);
                }
            } catch (_) {
                iframes.push(src);
            }
        }

        const isBannerShape = width >= 400 && height >= 20 && height <= 240;

        const signals = [
            Boolean(links.length),
            Boolean(iframes.length),
            adKeyword,
            overlayKeyword,
            isBannerShape,
            isFullscreenOverlay,
            darkOverlay,
            closeButton,
            zIndex >= 10,
            pointerEvents !== 'none',
        ].filter(Boolean).length;

        if (!isFullscreenOverlay && signals < 1) {
            continue;
        }

        const selector = buildSelector(el);
        const key = `${selector}|${position}|${width}|${height}|${top}|${left}`;

        if (seen.has(key)) continue;
        seen.add(key);

        results.push({
            tag,
            id: el.id || '',
            classes: classText,
            selector,
            position,
            width,
            height,
            top,
            left,
            right,
            bottom,
            viewport_width: viewportW,
            viewport_height: viewportH,
            viewport_coverage: Number(viewportCoverage.toFixed(3)),
            z_index: zIndex,
            background_color: backgroundColor,
            opacity: Number.isFinite(opacity) ? opacity : 1,
            pointer_events: pointerEvents,
            is_fullscreen_overlay: Boolean(isFullscreenOverlay),
            is_dark_overlay: Boolean(darkOverlay),
            has_close_button: Boolean(closeButton),
            overlay_keyword: Boolean(overlayKeyword),
            ad_keyword: Boolean(adKeyword),
            site_chrome: Boolean(siteChrome),
            snippet: el.outerHTML.slice(0, 500),
            text_sample: text.slice(0, 200),
            ext_links: links.slice(0, 5),
            iframes: iframes.slice(0, 3),
        });
    }

    // Higher priority candidates first: fullscreen overlay, then high z-index,
    // then larger viewport coverage.
    results.sort((a, b) => {
        if (a.is_fullscreen_overlay !== b.is_fullscreen_overlay) {
            return a.is_fullscreen_overlay ? -1 : 1;
        }
        if (a.z_index !== b.z_index) {
            return b.z_index - a.z_index;
        }
        return b.viewport_coverage - a.viewport_coverage;
    });

    return results.slice(0, 30);
}
"""


@dataclass
class CapturedRequest:
    """A single network request captured during page load."""
    url: str
    resource_type: str
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
    environment: str = "desktop"
    html: str = ""
    screenshot_bytes: bytes = b""
    screenshot_path: str = ""
    error: str = ""
    elapsed_ms: int = 0
    captured_requests: List[CapturedRequest] = field(default_factory=list)
    fixed_elements: List[Dict] = field(default_factory=list)
    focus: Optional[Dict] = None


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
    """
    Apply stealth patches to a Playwright page.

    playwright-stealth's user-agent override expects a Chromium-style user
    agent containing a Chrome version. Passing a Safari/WebKit user agent can
    make the library fail while trying to derive sec-ch-ua values.

    For non-Chromium user agents, skip the Chromium-specific stealth package.
    The browser context already carries the requested user agent and device
    profile, so skipping this patch is safer than injecting Chrome-only
    properties into WebKit.

    Any unexpected stealth failure falls back to the lightweight init script so
    browser rendering and sandbox validation can continue.
    """
    effective_user_agent = (user_agent or _DEFAULT_USER_AGENT).strip()
    is_chromium_user_agent = "Chrome/" in effective_user_agent

    if not is_chromium_user_agent:
        logger.debug(
            "Skipping playwright-stealth for non-Chromium user agent: %s",
            effective_user_agent,
        )
        return

    try:
        from playwright_stealth import Stealth

        Stealth(
            chrome_runtime=True,
            navigator_user_agent_override=effective_user_agent,
        ).apply_stealth_sync(page)

        logger.debug("playwright-stealth applied")

    except ImportError:
        logger.debug(
            "playwright-stealth not installed; using fallback stealth script"
        )
        page.add_init_script(_FALLBACK_STEALTH_SCRIPT)

    except Exception as exc:
        logger.warning(
            "playwright-stealth failed; using fallback stealth script: %s",
            exc,
        )

        try:
            page.add_init_script(_FALLBACK_STEALTH_SCRIPT)
        except Exception as fallback_exc:
            logger.warning(
                "Fallback stealth script could not be installed: %s",
                fallback_exc,
            )


def _scroll_page(
    page: Any,
    scroll_step: int = 1000,
    max_scrolls: int = 30,
    delay_seconds: float = 0.1,
) -> None:
    """Scroll the page gradually to load lazy content."""
    previous_height = page.evaluate("() => document.documentElement.scrollHeight")

    for _ in range(max_scrolls):
        page.evaluate("(step) => window.scrollBy(0, step)", scroll_step)
        time.sleep(delay_seconds)

        current_height = page.evaluate("() => document.documentElement.scrollHeight")
        if current_height == previous_height:
            break

        previous_height = current_height


_BOUNDING_BOX_JS = (
    "el => { const r = el.getBoundingClientRect();"
    " return {left: r.left, top: r.top, right: r.right, bottom: r.bottom,"
    " width: r.width, height: r.height}; }"
)


def _apply_focus_live(
    page: Any,
    html: str,
    focus_region: str,
) -> Tuple[Any, Optional[Dict[str, float]], Dict[str, Any], str]:
    """
    Resolve focus_region against the live page.

    Returns (element_handle, bounding_box, focus_meta, scoped_html). The handle
    and box are None when nothing matched; focus_meta always describes the
    outcome for the crawl result.
    """
    from .region_focus import resolve_focus

    meta: Dict[str, Any] = {
        "requested": focus_region,
        "selector": "",
        "method": "none",
        "matched": False,
    }

    try:
        resolution = resolve_focus(html, focus_region)
    except Exception as exc:
        logger.debug("Focus resolution failed for %r: %s", focus_region, exc)
        return None, None, meta, ""

    meta["selector"] = resolution.describe()
    meta["method"] = resolution.method

    if not resolution.selector:
        return None, None, meta, ""

    try:
        handles = page.query_selector_all(resolution.selector)
    except Exception as exc:
        logger.debug("Live focus query failed for %r: %s", resolution.selector, exc)
        return None, None, meta, ""

    if resolution.index >= len(handles):
        logger.debug(
            "Focus selector %r matched %d element(s); index %d out of range",
            resolution.selector,
            len(handles),
            resolution.index,
        )
        return None, None, meta, ""

    handle = handles[resolution.index]

    box = None
    try:
        box = handle.evaluate(_BOUNDING_BOX_JS)
    except Exception:
        logger.debug("Could not read focus element geometry")

    scoped_html = ""
    try:
        scoped_html = handle.evaluate("el => el.outerHTML") or ""
    except Exception:
        logger.debug("Could not read focus element outerHTML")

    meta["matched"] = True
    logger.info(
        "Focus region '%s' resolved live via %s: %s",
        focus_region,
        resolution.method,
        meta["selector"],
    )
    return handle, box, meta, scoped_html


def _filter_elements_to_region(
    elements: List[Dict],
    box: Dict[str, float],
    min_overlap: float = 0.5,
) -> List[Dict]:
    """
    Keep only fixed/overlay elements that lie substantially within the focus box.

    Overlap is measured as intersection area over the element's own area, so a
    page-wide overlay that only clips the focus region is dropped, while a
    banner sitting inside the region is kept.
    """
    kept: List[Dict] = []

    for element in elements:
        try:
            if _region_overlap_ratio(element, box) >= min_overlap:
                kept.append(element)
        except Exception:
            # Never drop data on a geometry error.
            kept.append(element)

    return kept


def _region_overlap_ratio(element: Dict, box: Dict[str, float]) -> float:
    ex0 = float(element.get("left", 0))
    ey0 = float(element.get("top", 0))
    ex1 = float(element.get("right", 0))
    ey1 = float(element.get("bottom", 0))

    inter_w = max(0.0, min(ex1, box["right"]) - max(ex0, box["left"]))
    inter_h = max(0.0, min(ey1, box["bottom"]) - max(ey0, box["top"]))
    intersection = inter_w * inter_h

    element_area = max(0.0, ex1 - ex0) * max(0.0, ey1 - ey0)
    if element_area <= 0:
        return 0.0

    return intersection / element_area


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
    environment: str = "desktop",
    focus_region: Optional[str] = None,
) -> RenderResult:
    """Render a URL in Playwright and return the rendered page data.

    When focus_region is provided, the returned HTML, screenshot, and
    fixed_elements are scoped to the matching page region so downstream
    extraction, detection, and rule generation all operate on that region.
    Network capture is never scoped — network rules are domain-scoped, not
    region-scoped, so third-party request analysis stays whole-page.
    """
    sync_playwright, PlaywrightError, PlaywrightTimeoutError = _import_playwright()
    start_time = time.perf_counter()

    profile = ENVIRONMENTS.get(environment, ENVIRONMENTS["desktop"])
    effective_ua = user_agent or profile["user_agent"]
    effective_browser_type = profile["browser_type"]

    result = RenderResult(url=url, status="failed", environment=environment)

    try:
        with sync_playwright() as playwright:
            launch_kwargs: Dict[str, Any] = {"headless": headless}

            if stealth and effective_browser_type == "chromium":
                launch_kwargs["args"] = _STEALTH_LAUNCH_ARGS

            browser = getattr(playwright, effective_browser_type).launch(**launch_kwargs)

            context_kwargs: Dict[str, Any] = {
                "user_agent": effective_ua,
                "viewport": profile["viewport"],
                "device_scale_factor": profile["device_scale_factor"],
                "is_mobile": profile["is_mobile"],
                "has_touch": profile["has_touch"],
                "locale": "en-US",
                "timezone_id": "America/New_York",
                "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
                # WebKit on Windows may fail SSL handshakes on some sites.
                # Safe to ignore here — this crawler is read-only.
                "ignore_https_errors": effective_browser_type == "webkit",
            }

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            if stealth and effective_browser_type == "chromium":
                _apply_stealth(page, user_agent=effective_ua)

            seen_urls: set = set()

            if capture_requests:
                def _on_request(request):
                    req_url = request.url

                    if req_url in seen_urls:
                        return

                    seen_urls.add(req_url)
                    result.captured_requests.append(
                        CapturedRequest(
                            url=req_url,
                            resource_type=request.resource_type,
                            method=request.method,
                        )
                    )

                page.on("request", _on_request)

            page.goto(url, wait_until=wait_until, timeout=timeout_ms)

            try:
                idle_timeout = network_idle_timeout_ms or timeout_ms
                page.wait_for_load_state("networkidle", timeout=idle_timeout)
            except Exception:
                logger.debug("networkidle timeout — continuing with what we have")

            if page_load_delay_ms > 0:
                delay_sec = page_load_delay_ms / 1000
                logger.debug("Waiting %.1fs for async content to load...", delay_sec)
                time.sleep(delay_sec)

            if enable_scroll:
                _scroll_page(
                    page,
                    scroll_step=scroll_step,
                    max_scrolls=max_scrolls,
                )
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(0.3)

            html = page.content()

            # Resolve the focus region (if any) against the live page so the
            # screenshot, HTML, and overlay scan can all be scoped to it.
            focus_handle = None
            focus_box = None
            if focus_region:
                focus_handle, focus_box, result.focus, scoped_html = _apply_focus_live(
                    page, html, focus_region
                )
                if scoped_html:
                    html = scoped_html

            try:
                if focus_handle is not None:
                    # Element screenshot captures just the focused region and
                    # scrolls it into view automatically.
                    screenshot_bytes = focus_handle.screenshot(timeout=timeout_ms)
                else:
                    screenshot_bytes = page.screenshot(
                        full_page=True,
                        timeout=timeout_ms,
                    )
            except Exception:
                logger.debug(
                    "focus/full_page screenshot failed; falling back to viewport screenshot"
                )
                screenshot_bytes = page.screenshot(
                    full_page=False,
                    timeout=timeout_ms,
                )

            # An element screenshot may have scrolled the page — reset to the top
            # so fixed-element geometry stays consistent with focus_box.
            if focus_handle is not None:
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                except Exception:
                    pass

            try:
                result.fixed_elements = page.evaluate(_FIXED_ELEMENT_SCRIPT) or []

                if focus_box and result.fixed_elements:
                    before = len(result.fixed_elements)
                    result.fixed_elements = _filter_elements_to_region(
                        result.fixed_elements, focus_box
                    )
                    logger.debug(
                        "Focus scoped fixed/overlay elements: %d -> %d",
                        before,
                        len(result.fixed_elements),
                    )

                if result.fixed_elements:
                    logger.debug(
                        "Found %d fixed/sticky/overlay element(s)",
                        len(result.fixed_elements),
                    )
            except Exception as exc:
                logger.debug("Fixed/overlay element scan failed: %s", exc)

            if screenshot_path:
                screenshot_file = Path(screenshot_path)
                screenshot_file.parent.mkdir(parents=True, exist_ok=True)
                screenshot_file.write_bytes(screenshot_bytes)
                result.screenshot_path = str(screenshot_file)

            result.html = html
            result.screenshot_bytes = screenshot_bytes
            result.status = "success"

            context.close()
            browser.close()

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
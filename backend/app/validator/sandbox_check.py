# Stage 3 of rule validation — live browser sandbox test:
# load page without rules (reference) → screenshot + DOM snapshot
# optionally load page with existing/current rules → reproduce current breakage
# apply candidate rule patch → re-screenshot + re-inspect DOM
# verify targeted ads/resources are handled
# verify critical page elements still present
# verify ticket-specific expectations such as visible images/search/menu
#
# New in this version:
# - Accept ticket_context from rule_validator.py.
# - Support ticket_context["current_rules"] / ["existing_rules"] / ["active_rules"].
#   This is important for exception rules:
#       current rules block something
#       candidate @@ exception rule should unblock it
# - Support cosmetic exception rules (#@#) against existing cosmetic hide rules.
# - Validate differently per ticket type:
#     visible ad issue        -> ads_blocked AND page_functional
#     image/video/content bug -> page_functional AND ticket assertions
#     UI hidden bug           -> page_functional AND ticket assertions
#     overlay/anti-adblock    -> page_functional AND ticket assertions
# - Avoid requiring every generic selector in validation_hints to be visible.
# - Avoid false positives where analytics/tracking-only requests are counted as
#   visible ad blocking.
# - Persist cosmetic evidence:
#     missing_ad_selectors
#     hidden_ad_selectors
#
# Ticket-aware region validation added:
# - validation_hints.must_preserve_region / must_preserve_regions:
#     Verify a preserved region still has visible text/images/links.
#     This catches broad cosmetic rules like:
#         rophim10.live##div.adserver
#     when the text "Nhà cái uy tín" remains but bookmaker logo/cards disappear.
#
# - validation_hints.must_hide_text_outside_allowed_region:
#     Verify ad-like text is gone outside allowed/preserved regions.
#     Example:
#         "Nhà Tài Trợ"
#     should be hidden outside the "Nhà cái uy tín" allowed region.

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000
NETWORK_IDLE_TIMEOUT_MS = 5_000
PAGE_SETTLE_DELAY_SECONDS = 2.0
# Cosmetic CSS injection applies synchronously; a short reflow settle is enough.
COSMETIC_SETTLE_DELAY_SECONDS = 1.0
VISIBLE_ELEMENT_DROP_FAIL_RATIO = 0.35

CRITICAL_SELECTORS = [
    "nav",
    "[role='navigation']",
    "main",
    "[role='main']",
    "article",
    "form",
    "input",
    "select",
    "textarea",
]

INTERACTIVE_SELECTOR = "button, a[href]"

RESOURCE_TYPE_OPTIONS = {
    "script": {"script"},
    "image": {"image"},
    "stylesheet": {"stylesheet"},
    "object": {"object", "other"},
    "xmlhttprequest": {"xhr", "fetch"},
    "xhr": {"xhr", "fetch"},
    "subdocument": {"document", "iframe"},
    "document": {"document"},
    "websocket": {"websocket"},
    "webrtc": {"other"},
    "ping": {"ping", "other"},
    "font": {"font"},
    "media": {"media"},
    "other": {"other"},
}

PAGE_STATE_SCRIPT = """
(payload) => {
    const isVisible = (el) => {
        if (!el || !(el instanceof Element)) return false;
        const style = window.getComputedStyle(el);
        if (!style) return false;
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const countMatches = (selector, visibleOnly = true) => {
        try {
            const matches = Array.from(document.querySelectorAll(selector));
            return visibleOnly ? matches.filter(isVisible).length : matches.length;
        } catch (err) {
            return -1;
        }
    };

    const countMap = (selectors, visibleOnly = true) => {
        const result = {};
        for (const selector of selectors || []) {
            result[selector] = countMatches(selector, visibleOnly);
        }
        return result;
    };

    return {
        visible_count: Array.from(document.querySelectorAll('body *')).filter(isVisible).length,
        critical_counts: countMap(payload.criticalSelectors || [], true),
        ad_dom_counts: countMap(payload.adSelectors || [], false),
        ad_visible_counts: countMap(payload.adSelectors || [], true),
        interactive_count: countMatches(payload.interactiveSelector || 'button, a[href]', true),
    };
}
"""

TICKET_ASSERTION_SCRIPT = """
(payload) => {
    const isVisible = (el) => {
        if (!el || !(el instanceof Element)) return false;
        const style = window.getComputedStyle(el);
        if (!style) return false;
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const normalizeText = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();

    const countVisible = (selector) => {
        try {
            return Array.from(document.querySelectorAll(selector)).filter(isVisible).length;
        } catch (err) {
            return -1;
        }
    };

    const countDom = (selector) => {
        try {
            return Array.from(document.querySelectorAll(selector)).length;
        } catch (err) {
            return -1;
        }
    };

    const visibleElements = (root, selector) => {
        try {
            return Array.from((root || document).querySelectorAll(selector)).filter(isVisible);
        } catch (err) {
            return [];
        }
    };

    const hasVisibleText = (el, text) => {
        if (!el || !text || !isVisible(el)) return false;
        return normalizeText(el.innerText || el.textContent || '').includes(normalizeText(text));
    };

    const countVisibleImages = (root) => {
        return visibleElements(root, 'img').filter((img) => {
            return img.naturalWidth > 0 && img.naturalHeight > 0;
        }).length;
    };

    const countBrokenImages = (root) => {
        return visibleElements(root, 'img').filter((img) => {
            if (!img.src) return false;
            return img.complete && (img.naturalWidth === 0 || img.naturalHeight === 0);
        }).length;
    };

    const countVisibleLinks = (root) => {
        return visibleElements(root, 'a[href]').length;
    };

    const countVisibleBackgroundImages = (root) => {
        return visibleElements(root, '*').filter((el) => {
            const style = window.getComputedStyle(el);
            if (!style || !style.backgroundImage || style.backgroundImage === 'none') return false;
            const rect = el.getBoundingClientRect();
            return rect.width >= 20 && rect.height >= 20;
        }).length;
    };

    const countVisibleImageLike = (root) => {
        return countVisibleImages(root) + countVisibleBackgroundImages(root);
    };

    const findBySelector = (selector) => {
        if (!selector) return null;
        try {
            const matches = Array.from(document.querySelectorAll(selector)).filter(isVisible);
            return matches.length ? matches[0] : null;
        } catch (err) {
            return null;
        }
    };

    const findSmallestVisibleTextElement = (text) => {
        const needle = normalizeText(text);
        if (!needle) return null;

        const candidates = Array.from(document.querySelectorAll('body *')).filter((el) => {
            if (!isVisible(el)) return false;
            const value = normalizeText(el.innerText || el.textContent || '');
            if (!value.includes(needle)) return false;

            // Prefer leaf-ish text holders rather than giant page containers.
            const childMatches = Array.from(el.children || []).some((child) => {
                if (!isVisible(child)) return false;
                return normalizeText(child.innerText || child.textContent || '').includes(needle);
            });

            return !childMatches;
        });

        return candidates.length ? candidates[0] : null;
    };

    const regionStats = (root) => {
        if (!root || !(root instanceof Element)) {
            return {
                found: false,
                selector: '',
                text: '',
                visible_images: 0,
                visible_background_images: 0,
                visible_image_like: 0,
                visible_links: 0,
                broken_images: 0,
                visible_text: '',
            };
        }

        let selector = root.tagName ? root.tagName.toLowerCase() : '';
        if (root.id) selector += '#' + root.id;
        else if (root.classList && root.classList.length) selector += '.' + Array.from(root.classList).slice(0, 3).join('.');

        const visibleImages = countVisibleImages(root);
        const visibleBackgroundImages = countVisibleBackgroundImages(root);
        const visibleLinks = countVisibleLinks(root);

        return {
            found: true,
            selector,
            text: normalizeText(root.innerText || root.textContent || '').slice(0, 300),
            visible_images: visibleImages,
            visible_background_images: visibleBackgroundImages,
            visible_image_like: visibleImages + visibleBackgroundImages,
            visible_links: visibleLinks,
            broken_images: countBrokenImages(root),
        };
    };

    const findRegion = (region) => {
        region = region || {};

        const explicitSelector =
            region.selector ||
            region.root_selector ||
            region.container_selector ||
            region.region_selector ||
            '';

        const explicit = findBySelector(explicitSelector);
        if (explicit) return explicit;

        const text =
            region.must_contain_text ||
            region.text ||
            region.title ||
            region.name ||
            '';

        const textElement = findSmallestVisibleTextElement(text);
        if (!textElement) return null;

        const maxDepth = Number(region.max_ancestor_depth || 8);
        const minImages = Number(region.min_visible_images || 0);
        const minLinks = Number(region.min_visible_links || 0);
        const minImageLike = Number(region.min_visible_image_like || 0);

        let current = textElement;
        let best = textElement;
        let bestScore = -1;
        let depth = 0;

        while (current && current instanceof Element && depth <= maxDepth) {
            const tag = (current.tagName || '').toLowerCase();

            if (tag === 'body' || tag === 'html') break;

            if (isVisible(current)) {
                const images = countVisibleImages(current);
                const links = countVisibleLinks(current);
                const imageLike = countVisibleImageLike(current);
                const score = images + links + imageLike;

                // If a nearby ancestor already satisfies the caller's region
                // requirements, use it immediately. This avoids falling back to
                // a page-wide container that may include unrelated movie cards.
                if (
                    (minImages <= 0 || images >= minImages) &&
                    (minLinks <= 0 || links >= minLinks) &&
                    (minImageLike <= 0 || imageLike >= minImageLike)
                ) {
                    return current;
                }

                if (score > bestScore) {
                    best = current;
                    bestScore = score;
                }
            }

            current = current.parentElement;
            depth += 1;
        }

        return best;
    };

    const mustShow = {};
    for (const selector of payload.mustShowSelectors || []) {
        mustShow[selector] = countVisible(selector);
    }

    const mustExist = {};
    for (const selector of payload.mustExistSelectors || []) {
        mustExist[selector] = countDom(selector);
    }

    const mustHide = {};
    for (const selector of payload.mustHideSelectors || []) {
        mustHide[selector] = countVisible(selector);
    }

    const anyGroups = {};
    for (const group of payload.mustShowAnySelectorGroups || []) {
        const name = group.name || 'unnamed_group';
        let total = 0;
        const counts = {};

        for (const selector of group.selectors || []) {
            const count = countVisible(selector);
            counts[selector] = count;
            if (count > 0) total += count;
        }

        anyGroups[name] = {
            total,
            counts,
            min: group.min || 1
        };
    }

    const visibleImages = countVisibleImages(document);
    const brokenImages = countBrokenImages(document);
    const visibleVideos = Array.from(document.querySelectorAll('video')).filter(isVisible).length;
    const visibleIframes = Array.from(document.querySelectorAll('iframe')).filter(isVisible).length;

    const visibleBodyText = normalizeText(document.body ? document.body.innerText : '');
    const preserveText = {};
    for (const text of payload.mustPreserveText || []) {
        const needle = normalizeText(text);
        if (!needle) continue;
        preserveText[text] = visibleBodyText.includes(needle);
    }

    const preserveRegions = {};
    for (const region of payload.mustPreserveRegions || []) {
        const name = region.name || region.must_contain_text || region.selector || 'unnamed_region';
        const root = findRegion(region);
        preserveRegions[name] = regionStats(root);
    }

    const allowedRegionRoots = [];
    for (const region of payload.allowedRegions || []) {
        const root = findRegion(region);
        if (root) allowedRegionRoots.push(root);
    }

    const isInsideAllowedRegion = (el) => {
        for (const root of allowedRegionRoots) {
            if (root === el || root.contains(el)) return true;
        }
        return false;
    };

    const hiddenTextOutsideAllowedRegion = {};
    for (const text of payload.mustHideTextOutsideAllowedRegion || []) {
        const needle = normalizeText(text);
        if (!needle) continue;

        let count = 0;
        const samples = [];

        for (const el of Array.from(document.querySelectorAll('body *'))) {
            if (!isVisible(el)) continue;
            if (isInsideAllowedRegion(el)) continue;

            const value = normalizeText(el.innerText || el.textContent || '');
            if (!value.includes(needle)) continue;

            // Count leaf-ish occurrences only. A parent containing a matching
            // child should not inflate the count.
            const childHasNeedle = Array.from(el.children || []).some((child) => {
                if (!isVisible(child)) return false;
                if (isInsideAllowedRegion(child)) return false;
                return normalizeText(child.innerText || child.textContent || '').includes(needle);
            });

            if (childHasNeedle) continue;

            count += 1;
            if (samples.length < 5) {
                samples.push(value.slice(0, 120));
            }
        }

        hiddenTextOutsideAllowedRegion[text] = {
            count,
            samples,
        };
    }

    return {
        must_show: mustShow,
        must_exist: mustExist,
        must_hide: mustHide,
        must_show_any_groups: anyGroups,
        visible_images: visibleImages,
        broken_images: brokenImages,
        visible_videos: visibleVideos,
        visible_iframes: visibleIframes,
        preserve_text: preserveText,
        preserve_regions: preserveRegions,
        hide_text_outside_allowed_region: hiddenTextOutsideAllowedRegion,
    };
}
"""


@dataclass
class _RuleOptions:
    resource_types: set[str] = field(default_factory=set)
    excluded_resource_types: set[str] = field(default_factory=set)
    third_party: Optional[bool] = None
    domain_includes: List[str] = field(default_factory=list)
    domain_excludes: List[str] = field(default_factory=list)
    match_case: bool = False


@dataclass
class _NetworkRule:
    original: str
    pattern: str
    regex: re.Pattern
    options: _RuleOptions
    is_exception: bool = False

    def matches(self, request_url: str, resource_type: str, document_url: str) -> bool:
        resource_type = (resource_type or "other").lower()

        if self.options.resource_types and resource_type not in self.options.resource_types:
            return False

        if resource_type in self.options.excluded_resource_types:
            return False

        if self.options.third_party is not None:
            is_third_party = _is_third_party(request_url, document_url)
            if is_third_party != self.options.third_party:
                return False

        if not _domain_option_applies(
            document_url,
            self.options.domain_includes,
            self.options.domain_excludes,
        ):
            return False

        return bool(self.regex.search(request_url))


@dataclass
class _CosmeticRule:
    original: str
    selector: str
    domain_prefix: str = ""
    is_exception: bool = False


_UNREACHABLE_PATTERNS = (
    "ERR_CONNECTION_RESET",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_NAME_RESOLUTION_FAILED",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_ABORTED",
    "SSL connect error",
)

TRACKING_ONLY_DOMAINS = {
    "google-analytics.com",
    "www.google-analytics.com",
    "googletagmanager.com",
    "www.googletagmanager.com",
    "static.cloudflareinsights.com",
}

VISIBLE_AD_DOMAINS = {
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "adservice.google.com",
    "adnxs.com",
    "adsrvr.org",
    "criteo.com",
    "criteo.net",
    "taboola.com",
    "outbrain.com",
    "mgid.com",
    "popads.net",
    "propellerads.com",
    "exoclick.com",
    "trafficjunky.net",
    "juicyads.com",
    "adskeeper.com",
}

VISIBLE_AD_URL_PATTERNS = (
    "/ad/",
    "/ads/",
    "/advert/",
    "/advertise/",
    "/advertisement/",
    "/banner/",
    "/banners/",
    "/popup/",
    "/popunder/",
    "/sponsor/",
    "/sponsored/",
    "/promo/",
    "/promotion/",
    "/storage/ads/",
    "ads%20",
    "banner",
    "popup",
    "casino",
    "bet",
    "betting",
    "affiliate",
)

VISIBLE_AD_RULE_PATTERNS = (
    "/ad/",
    "/ads/",
    "/banner",
    "/popup",
    "/sponsor",
    "/promo",
    "/storage/ads/",
    "adserver",
    "banner",
    "popup",
    "casino",
    "bet",
)


@dataclass
class SandboxResult:
    """Result of testing one set of rules against a live page."""
    url: str
    passed: bool

    ads_blocked: bool = False
    page_functional: bool = False

    ticket_assertions_passed: bool = True
    ticket_assertion_errors: List[str] = field(default_factory=list)

    baseline_ticket_assertions_passed: bool = True
    baseline_ticket_assertion_errors: List[str] = field(default_factory=list)

    existing_rules_count: int = 0
    candidate_rules_count: int = 0

    blocked_requests: List[str] = field(default_factory=list)
    candidate_blocked_requests: List[str] = field(default_factory=list)
    missing_ad_selectors: List[str] = field(default_factory=list)
    hidden_ad_selectors: List[str] = field(default_factory=list)
    broken_selectors: List[str] = field(default_factory=list)
    tested_screenshot: bytes = field(default_factory=bytes)
    error: str = ""
    unreachable: bool = False


def run_sandbox(
    url: str,
    rules: List[str],
    environment: str = "desktop",
    ticket_context: Optional[Dict[str, Any]] = None,
) -> SandboxResult:
    """
    Test candidate ABP rules against a live page.

    For normal ad-block tickets:
        passed = ads_blocked AND page_functional AND ticket_assertions_passed

    For breakage tickets such as image/video/content/UI hidden:
        passed = page_functional AND ticket_assertions_passed

    Note: when validating several rule sets against the same URL, use
    SandboxSession directly — it shares the browser launch and the
    reference/baseline page loads across calls instead of repeating them.

    Args:
        url:
            The original reported page URL.
        rules:
            List of rule strings that passed syntax/scope/policy validation.
        environment:
            Crawl environment name ("desktop", "android", "ios").
        ticket_context:
            Optional ticket context. Can include current/existing rules and validation hints.

    Returns:
        SandboxResult.
    """
    with SandboxSession(
        url,
        environment=environment,
        ticket_context=ticket_context,
    ) as session:
        return session.test_rules(rules)


class SandboxSession:
    """
    Reusable sandbox for validating several rule sets against the same URL.

    run_sandbox() previously launched a fresh browser and loaded the page three
    times (reference, baseline, test) on every call. The validator calls it
    once per rule plus once combined, so N rules cost 3*(N+1) page loads.

    A session performs the expensive work once:
      - one Playwright + Chromium launch,
      - one reference page load (kept open; its state is re-read per rule set),
      - at most one baseline load (only when existing rules apply),
    so each test_rules() call costs a single page load.
    """

    def __init__(
        self,
        url: str,
        environment: str = "desktop",
        ticket_context: Optional[Dict[str, Any]] = None,
    ):
        self.url = url
        self.environment = environment
        self.ticket_context = _safe_ticket_context(ticket_context)
        self.problem_type = str(
            self.ticket_context.get("problem_type", "unknown")
        ).strip().lower()
        self.existing_rules = _get_existing_rules(self.ticket_context)

        self._playwright: Any = None
        self._browser: Any = None
        self._apply_stealth: Any = None
        self._timeout_error_cls: Any = Exception
        self._context_kwargs: Dict[str, Any] = {}
        self._ua = ""

        self._reference_context: Any = None
        self._reference_page: Any = None
        self._reference_ticket_state: Optional[Dict[str, Any]] = None
        self._baseline_ticket_state: Optional[Dict[str, Any]] = None

        self._fatal_error = ""
        self._fatal_unreachable = False
        self._opened = False

    def __enter__(self) -> "SandboxSession":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def open(self) -> None:
        """
        Start Playwright and launch the shared browser.

        Never raises: failures are stored and surfaced as error results from
        test_rules() so the validator keeps its per-rule error handling.
        """
        if self._opened or self._fatal_error:
            return

        try:
            from ..crawler.browser import (
                ENVIRONMENTS,
                _STEALTH_LAUNCH_ARGS,
                _apply_stealth,
                _import_playwright,
            )
        except Exception as exc:
            self._fatal_error = f"browser helpers unavailable: {exc}"
            logger.exception(self._fatal_error)
            return

        try:
            sync_playwright, _, timeout_error_cls = _import_playwright()
        except ImportError as exc:
            self._fatal_error = str(exc)
            logger.exception("Playwright import failed")
            return

        self._apply_stealth = _apply_stealth
        self._timeout_error_cls = timeout_error_cls

        profile = ENVIRONMENTS.get(self.environment, ENVIRONMENTS["desktop"])
        self._ua = profile["user_agent"]
        self._context_kwargs = {
            "user_agent": self._ua,
            "viewport": profile["viewport"],
            "device_scale_factor": profile["device_scale_factor"],
            "is_mobile": profile["is_mobile"],
            "has_touch": profile["has_touch"],
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        }

        logger.debug(
            "Sandbox session starting in '%s' environment (viewport %s, mobile=%s)",
            self.environment,
            profile["viewport"],
            profile["is_mobile"],
        )

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=_STEALTH_LAUNCH_ARGS,
            )
        except Exception as exc:
            self._fatal_error = f"sandbox browser failed for {self.url}: {exc}"
            logger.exception(self._fatal_error)
            self.close()
            return

        self._opened = True

    def close(self) -> None:
        for closeable in ("_reference_context", "_browser"):
            handle = getattr(self, closeable)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
                setattr(self, closeable, None)

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._reference_page = None
        self._opened = False

    def test_rules(
        self,
        rules: List[str],
        capture_screenshot: bool = True,
    ) -> SandboxResult:
        """
        Test one candidate rule set against the live page.

        Costs a single page load once the session reference is warm.
        """
        result = SandboxResult(url=self.url, passed=False)

        candidate_rules = [
            str(rule).strip()
            for rule in rules
            if rule and str(rule).strip()
        ]

        if not candidate_rules:
            result.error = "no candidate rules supplied"
            return result

        existing_rules = self.existing_rules
        if not _should_apply_existing_rules(self.problem_type, candidate_rules):
            existing_rules = []

        all_test_rules = existing_rules + candidate_rules

        result.existing_rules_count = len(existing_rules)
        result.candidate_rules_count = len(candidate_rules)

        candidate_cosmetic_selectors = _extract_applicable_cosmetic_selectors(
            candidate_rules,
            self.url,
        )

        candidate_network_block_rules = [
            parsed for parsed in (_parse_network_rule(rule) for rule in candidate_rules)
            if parsed is not None and not parsed.is_exception
        ]

        if not self._opened:
            self.open()

        if self._fatal_error:
            result.error = self._fatal_error
            result.unreachable = self._fatal_unreachable
            return result

        if not self._ensure_reference(result):
            return result

        # The reference page stays loaded across calls; only the (cheap) state
        # evaluation is repeated because each rule set has its own selectors.
        reference_state = _capture_page_state(
            self._reference_page,
            candidate_cosmetic_selectors,
        )

        baseline_ticket_state = self._baseline_state(bool(existing_rules), result)
        if baseline_ticket_state is None:
            return result

        # ------------------------------------------------------
        # Test page: existing/current rules + candidate patch.
        # ------------------------------------------------------
        try:
            test_context = self._browser.new_context(**self._context_kwargs)
        except Exception as exc:
            self._record_error(result, exc)
            return result

        try:
            test_page = test_context.new_page()
            self._apply_stealth(test_page, user_agent=self._ua)
            setattr(test_page, "_adblock_document_url", self.url)

            _apply_network_rules(test_page, all_test_rules)
            _load_page(test_page, self.url, self._timeout_error_cls)
            _apply_cosmetic_rules(test_page, all_test_rules)

            time.sleep(COSMETIC_SETTLE_DELAY_SECONDS)

            tested_state = _capture_page_state(
                test_page,
                candidate_cosmetic_selectors,
            )
            tested_ticket_state = _capture_ticket_state(
                test_page,
                self.ticket_context,
            )

            if capture_screenshot:
                result.tested_screenshot = test_page.screenshot(
                    full_page=True,
                    timeout=DEFAULT_TIMEOUT_MS,
                )

            result.blocked_requests = list(
                getattr(test_page, "_adblock_blocked_requests", [])
            )

            blocked_by_rule = getattr(test_page, "_adblock_blocked_by_rule", {})
            result.candidate_blocked_requests = _candidate_blocked_requests(
                candidate_rules,
                blocked_by_rule,
            )
        except Exception as exc:
            self._record_error(result, exc)
            return result
        finally:
            try:
                test_context.close()
            except Exception:
                pass

        _finalize_sandbox_result(
            result=result,
            url=self.url,
            problem_type=self.problem_type,
            ticket_context=self.ticket_context,
            candidate_network_block_rules=candidate_network_block_rules,
            candidate_cosmetic_selectors=candidate_cosmetic_selectors,
            reference_state=reference_state,
            tested_state=tested_state,
            baseline_ticket_state=baseline_ticket_state,
            tested_ticket_state=tested_ticket_state,
        )

        return result

    def _ensure_reference(self, result: SandboxResult) -> bool:
        """
        Load the no-rules reference page once and keep it open for the session.
        """
        if self._reference_page is not None:
            return True

        try:
            # --------------------------------------------------
            # Reference page: no adblock rules.
            # --------------------------------------------------
            self._reference_context = self._browser.new_context(**self._context_kwargs)
            self._reference_page = self._reference_context.new_page()
            self._apply_stealth(self._reference_page, user_agent=self._ua)
            _load_page(self._reference_page, self.url, self._timeout_error_cls)
            return True
        except Exception as exc:
            # The URL is most likely unreachable — fail this and every
            # subsequent test fast instead of re-loading once per rule.
            self._record_error(result, exc)
            self._fatal_error = result.error
            self._fatal_unreachable = result.unreachable

            if self._reference_context is not None:
                try:
                    self._reference_context.close()
                except Exception:
                    pass

            self._reference_context = None
            self._reference_page = None
            return False

    def _baseline_state(
        self,
        use_existing_rules: bool,
        result: SandboxResult,
    ) -> Optional[Dict[str, Any]]:
        """
        Return the ticket state of the baseline page (existing rules only).

        Without existing rules the baseline conditions are identical to the
        reference page, so its (cached) ticket state is reused instead of
        loading the page a second time.
        """
        if not use_existing_rules:
            if self._reference_ticket_state is None:
                self._reference_ticket_state = _capture_ticket_state(
                    self._reference_page,
                    self.ticket_context,
                )
            return self._reference_ticket_state

        if self._baseline_ticket_state is not None:
            return self._baseline_ticket_state

        # --------------------------------------------------
        # Baseline page: existing/current rules only.
        # --------------------------------------------------
        try:
            baseline_context = self._browser.new_context(**self._context_kwargs)
        except Exception as exc:
            self._record_error(result, exc)
            return None

        try:
            baseline_page = baseline_context.new_page()
            self._apply_stealth(baseline_page, user_agent=self._ua)
            setattr(baseline_page, "_adblock_document_url", self.url)

            _apply_network_rules(baseline_page, self.existing_rules)
            _load_page(baseline_page, self.url, self._timeout_error_cls)
            _apply_cosmetic_rules(baseline_page, self.existing_rules)

            time.sleep(COSMETIC_SETTLE_DELAY_SECONDS)

            self._baseline_ticket_state = _capture_ticket_state(
                baseline_page,
                self.ticket_context,
            )
        except Exception as exc:
            self._record_error(result, exc)
            return None
        finally:
            try:
                baseline_context.close()
            except Exception:
                pass

        return self._baseline_ticket_state

    def _record_error(self, result: SandboxResult, exc: Exception) -> None:
        error_str = str(exc)
        result.error = f"sandbox browser failed for {self.url}: {exc}"

        if any(pat in error_str for pat in _UNREACHABLE_PATTERNS):
            result.unreachable = True
            logger.warning(
                "Sandbox unreachable (connection error) for %s: %s",
                self.url,
                error_str.splitlines()[0],
            )
        else:
            logger.exception(result.error)

def _finalize_sandbox_result(
    result: SandboxResult,
    url: str,
    problem_type: str,
    ticket_context: Dict[str, Any],
    candidate_network_block_rules: List["_NetworkRule"],
    candidate_cosmetic_selectors: List[str],
    reference_state: Dict[str, Any],
    tested_state: Dict[str, Any],
    baseline_ticket_state: Dict[str, Any],
    tested_ticket_state: Dict[str, Any],
) -> None:
    """
    Evaluate reference vs tested state and fill pass/fail fields on result.
    """
    # ------------------------------------------------------------------
    # Evaluate candidate ad blocking.
    # ------------------------------------------------------------------
    result.broken_selectors = _find_broken_critical_selectors(
        reference_state.get("critical_counts", {}),
        tested_state.get("critical_counts", {}),
    )

    network_targets_blocked = _network_targets_blocked(
        candidate_network_block_rules,
        result.candidate_blocked_requests,
    )

    cosmetic_targets_present = any(
        reference_state.get("ad_dom_counts", {}).get(selector, 0) > 0
        for selector in candidate_cosmetic_selectors
    )

    cosmetic_result = _cosmetic_targets_blocked(
        candidate_cosmetic_selectors,
        reference_state.get("ad_dom_counts", {}),
        tested_state.get("ad_dom_counts", {}),
        tested_state.get("ad_visible_counts", {}),
    )

    cosmetic_targets_blocked = bool(cosmetic_result.get("blocked"))
    result.missing_ad_selectors = list(cosmetic_result.get("missing", []))
    result.hidden_ad_selectors = list(cosmetic_result.get("hidden", []))

    result.ads_blocked = (
        bool(candidate_network_block_rules) and network_targets_blocked
    ) or (
        cosmetic_targets_present and cosmetic_targets_blocked
    )

    # Diagnostic logging: when ads are not detected as blocked, log helpful
    # context so failures such as 'ads_not_blocked' can be investigated.
    if not result.ads_blocked:
        try:
            net_rule_texts = [getattr(r, "original", str(r)) for r in candidate_network_block_rules]
        except Exception:
            net_rule_texts = [str(r) for r in candidate_network_block_rules]

        logger.warning(
            "Sandbox diagnostic for %s: cosmetic_selectors=%s network_rules=%s reference_ad_dom_counts=%s tested_ad_dom_counts=%s tested_ad_visible_counts=%s blocked_requests=%s candidate_blocked_requests=%s",
            url,
            candidate_cosmetic_selectors,
            net_rule_texts,
            reference_state.get("ad_dom_counts", {}),
            tested_state.get("ad_dom_counts", {}),
            tested_state.get("ad_visible_counts", {}),
            result.blocked_requests,
            result.candidate_blocked_requests,
        )

    # ------------------------------------------------------------------
    # Evaluate generic page functionality against reference/no-rule page.
    # ------------------------------------------------------------------
    reference_visible = max(int(reference_state.get("visible_count", 0) or 0), 1)
    tested_visible = int(tested_state.get("visible_count", 0) or 0)

    visible_ratio = tested_visible / reference_visible
    visible_count_ok = visible_ratio >= VISIBLE_ELEMENT_DROP_FAIL_RATIO

    result.page_functional = not result.broken_selectors and visible_count_ok

    # ------------------------------------------------------------------
    # Evaluate ticket-specific behavior.
    # ------------------------------------------------------------------
    baseline_ticket_ok, baseline_ticket_errors = _evaluate_ticket_assertions(
        ticket_context,
        baseline_ticket_state,
    )
    result.baseline_ticket_assertions_passed = baseline_ticket_ok
    result.baseline_ticket_assertion_errors = baseline_ticket_errors

    ticket_ok, ticket_errors = _evaluate_ticket_assertions(
        ticket_context,
        tested_ticket_state,
    )
    result.ticket_assertions_passed = ticket_ok
    result.ticket_assertion_errors = ticket_errors

    # ------------------------------------------------------------------
    # Final pass logic by ticket type.
    # ------------------------------------------------------------------
    if problem_type in {
        "content_broken_image",
        "content_broken_video",
        "content_broken",
        "ui_hidden",
    }:
        result.passed = result.page_functional and result.ticket_assertions_passed

    elif problem_type == "anti_adblock_or_overlay":
        result.passed = result.page_functional and result.ticket_assertions_passed

    else:
        result.passed = (
            result.ads_blocked
            and result.page_functional
            and result.ticket_assertions_passed
        )

    if not result.ads_blocked and problem_type not in {
        "content_broken_image",
        "content_broken_video",
        "content_broken",
        "ui_hidden",
        "anti_adblock_or_overlay",
    }:
        logger.warning("Sandbox did not verify any visible ad blocking for %s", url)

    if not result.page_functional:
        logger.warning(
            "Sandbox detected page functionality risk for %s: broken=%s visible_ratio=%.2f",
            url,
            result.broken_selectors,
            visible_ratio,
        )

    if not result.ticket_assertions_passed:
        logger.warning(
            "Sandbox ticket assertions failed for %s: %s",
            url,
            result.ticket_assertion_errors,
        )


def _apply_network_rules(page, rules: List[str]) -> None:
    """
    Register page.route() handlers that abort requests matching network block rules.
    Exception rules are respected: if an exception matches, the request continues.
    """
    parsed_rules = [
        parsed for parsed in (_parse_network_rule(rule) for rule in rules)
        if parsed is not None
    ]

    block_rules = [rule for rule in parsed_rules if not rule.is_exception]
    exception_rules = [rule for rule in parsed_rules if rule.is_exception]

    blocked_requests: List[str] = []
    blocked_by_rule: Dict[str, List[str]] = {
        rule.original: []
        for rule in block_rules
    }

    setattr(page, "_adblock_blocked_requests", blocked_requests)
    setattr(page, "_adblock_blocked_by_rule", blocked_by_rule)

    if not block_rules:
        return

    def _route_handler(route):
        request = route.request
        request_url = request.url
        resource_type = getattr(request, "resource_type", "other")
        document_url = (
            getattr(page, "_adblock_document_url", "")
            or getattr(page, "url", "")
        )

        for exception_rule in exception_rules:
            if exception_rule.matches(request_url, resource_type, document_url):
                route.continue_()
                return

        for block_rule in block_rules:
            if block_rule.matches(request_url, resource_type, document_url):
                blocked_requests.append(request_url)
                blocked_by_rule.setdefault(block_rule.original, []).append(request_url)
                route.abort()
                return

        route.continue_()

    page.route("**/*", _route_handler)


def _apply_cosmetic_rules(page, rules: List[str]) -> None:
    """
    Inject a <style> block for applicable cosmetic hiding rules.

    Cosmetic exception rules (#@#) cancel exact matching cosmetic hide selectors.
    Example:
        site.com##.search
        site.com#@#.search
    => .search will not be hidden.
    """
    document_url = (
        getattr(page, "_adblock_document_url", "")
        or getattr(page, "url", "")
    )

    hide_selectors, exception_selectors = _extract_cosmetic_hide_and_exception_selectors(
        rules,
        document_url,
    )

    final_selectors = [
        selector
        for selector in hide_selectors
        if selector not in exception_selectors
    ]

    setattr(page, "_adblock_cosmetic_selectors", final_selectors)

    if not final_selectors:
        return

    style_lines = []

    for selector in final_selectors:
        if "{" in selector or "}" in selector:
            logger.warning("Skipping unsafe cosmetic selector: %s", selector)
            continue

        style_lines.append(
            f"{selector} {{ display: none !important; visibility: hidden !important; }}"
        )

    if not style_lines:
        return

    try:
        page.add_style_tag(content="\n".join(style_lines))
    except Exception as exc:
        setattr(page, "_adblock_cosmetic_error", str(exc))
        logger.exception("Failed to inject cosmetic rules")


def _load_page(page, url: str, timeout_error_cls: Any) -> None:
    """
    Load page with a reasonable fallback when networkidle times out.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except timeout_error_cls:
        logger.warning("domcontentloaded timeout for %s", url)

    try:
        page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
    except timeout_error_cls:
        logger.debug("networkidle timeout for %s", url)

    time.sleep(PAGE_SETTLE_DELAY_SECONDS)


def _capture_page_state(page, ad_selectors: List[str]) -> Dict[str, Any]:
    payload = {
        "criticalSelectors": CRITICAL_SELECTORS,
        "adSelectors": ad_selectors,
        "interactiveSelector": INTERACTIVE_SELECTOR,
    }

    try:
        return page.evaluate(PAGE_STATE_SCRIPT, payload)
    except Exception as exc:
        logger.warning("Failed to capture page state: %s", exc)
        return {
            "visible_count": 0,
            "critical_counts": {},
            "ad_dom_counts": {},
            "ad_visible_counts": {},
            "interactive_count": 0,
        }


def _capture_ticket_state(
    page,
    ticket_context: Dict[str, Any],
) -> Dict[str, Any]:
    hints = _get_validation_hints(ticket_context)

    preserve_regions = _get_preserve_region_hints(ticket_context, hints)
    allowed_regions = _get_allowed_region_hints(ticket_context, hints, preserve_regions)

    payload = {
        "mustShowSelectors": hints.get("must_show_all_selectors", []),
        "mustExistSelectors": hints.get("must_exist_selectors", []),
        "mustHideSelectors": hints.get("must_hide_selectors", []),
        "mustShowAnySelectorGroups": hints.get("must_show_any_selector_groups", []),
        "mustPreserveText": _as_string_list(
            hints.get("must_preserve_text", [])
            or hints.get("preserve_text", [])
            or hints.get("must_contain_text", [])
        ),
        "mustPreserveRegions": preserve_regions,
        "allowedRegions": allowed_regions,
        "mustHideTextOutsideAllowedRegion": _as_string_list(
            hints.get("must_hide_text_outside_allowed_region", [])
            or hints.get("must_hide_text_outside_allowed_regions", [])
            or hints.get("must_not_show_text_outside_allowed_region", [])
        ),
    }

    try:
        return page.evaluate(TICKET_ASSERTION_SCRIPT, payload)
    except Exception as exc:
        logger.warning("Failed to capture ticket state: %s", exc)
        return {
            "must_show": {},
            "must_exist": {},
            "must_hide": {},
            "must_show_any_groups": {},
            "visible_images": 0,
            "broken_images": 0,
            "visible_videos": 0,
            "visible_iframes": 0,
            "preserve_text": {},
            "preserve_regions": {},
            "hide_text_outside_allowed_region": {},
        }


def _evaluate_ticket_assertions(
    ticket_context: Dict[str, Any],
    state: Dict[str, Any],
) -> tuple[bool, List[str]]:
    hints = _get_validation_hints(ticket_context)
    problem_type = str(ticket_context.get("problem_type", "unknown")).lower()

    errors: List[str] = []

    for selector, count in (state.get("must_show") or {}).items():
        if count == -1:
            errors.append(f"invalid must_show selector: {selector}")
        elif count == 0:
            errors.append(f"expected selector to be visible: {selector}")

    for selector, count in (state.get("must_exist") or {}).items():
        if count == -1:
            errors.append(f"invalid must_exist selector: {selector}")
        elif count == 0:
            errors.append(f"expected selector to exist: {selector}")

    for selector, count in (state.get("must_hide") or {}).items():
        if count == -1:
            errors.append(f"invalid must_hide selector: {selector}")
        elif count > 0:
            errors.append(f"expected selector to be hidden/removed: {selector}")

    for group_name, group_data in (state.get("must_show_any_groups") or {}).items():
        total = int(group_data.get("total", 0) or 0)
        min_required = int(group_data.get("min", 1) or 1)

        if total < min_required:
            errors.append(
                f"expected at least {min_required} visible element(s) in group '{group_name}', got {total}"
            )

    for text, found in (state.get("preserve_text") or {}).items():
        if not found:
            errors.append(f"expected text to remain visible: {text}")

    for region_name, region_state in (state.get("preserve_regions") or {}).items():
        if not isinstance(region_state, Mapping):
            errors.append(f"invalid preserve region state: {region_name}")
            continue

        if not bool(region_state.get("found", False)):
            errors.append(f"expected preserve region to remain visible: {region_name}")
            continue

        config = _find_preserve_region_config(ticket_context, hints, region_name)

        min_region_images = _optional_int(config.get("min_visible_images"))
        if min_region_images is not None:
            actual = int(region_state.get("visible_images", 0) or 0)
            if actual < min_region_images:
                errors.append(
                    f"expected preserve region '{region_name}' to contain at least "
                    f"{min_region_images} visible image(s), got {actual}"
                )

        min_region_links = _optional_int(config.get("min_visible_links"))
        if min_region_links is not None:
            actual = int(region_state.get("visible_links", 0) or 0)
            if actual < min_region_links:
                errors.append(
                    f"expected preserve region '{region_name}' to contain at least "
                    f"{min_region_links} visible link(s), got {actual}"
                )

        min_region_image_like = _optional_int(config.get("min_visible_image_like"))
        if min_region_image_like is not None:
            actual = int(region_state.get("visible_image_like", 0) or 0)
            if actual < min_region_image_like:
                errors.append(
                    f"expected preserve region '{region_name}' to contain at least "
                    f"{min_region_image_like} visible image-like element(s), got {actual}"
                )

        max_region_broken_images = _optional_int(config.get("max_broken_images"))
        if max_region_broken_images is not None:
            actual = int(region_state.get("broken_images", 0) or 0)
            if actual > max_region_broken_images:
                errors.append(
                    f"expected preserve region '{region_name}' to contain at most "
                    f"{max_region_broken_images} broken image(s), got {actual}"
                )

    for text, data in (state.get("hide_text_outside_allowed_region") or {}).items():
        if not isinstance(data, Mapping):
            continue

        count = int(data.get("count", 0) or 0)
        if count > 0:
            samples = data.get("samples", []) or []
            sample_text = f"; samples={samples[:3]}" if samples else ""
            errors.append(
                f"expected text to be hidden outside allowed region: {text} "
                f"(visible occurrences: {count}{sample_text})"
            )

    min_visible_images = hints.get("min_visible_images")
    if min_visible_images is not None:
        actual = int(state.get("visible_images", 0) or 0)
        if actual < int(min_visible_images):
            errors.append(
                f"expected at least {min_visible_images} visible images, got {actual}"
            )

    max_broken_images = hints.get("max_broken_images")
    if max_broken_images is not None:
        actual = int(state.get("broken_images", 0) or 0)
        if actual > int(max_broken_images):
            errors.append(
                f"expected at most {max_broken_images} broken images, got {actual}"
            )

    min_visible_videos = hints.get("min_visible_videos")
    if min_visible_videos is not None:
        visible_videos = int(state.get("visible_videos", 0) or 0)
        visible_iframes = int(state.get("visible_iframes", 0) or 0)
        total = visible_videos + visible_iframes

        if total < int(min_visible_videos):
            errors.append(
                f"expected at least {min_visible_videos} visible video/player elements, got {total}"
            )

    if problem_type in {
        "content_broken_image",
        "content_broken_video",
        "ui_hidden",
    } and not hints:
        errors.append("missing validation_hints for breakage ticket")

    return len(errors) == 0, errors


def _get_validation_hints(ticket_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return validation hints from ticket_context, or infer defaults by problem_type.

    Supported hint fields:
      - min_visible_images
      - max_broken_images
      - min_visible_videos
      - must_show_all_selectors
      - must_exist_selectors
      - must_hide_selectors
      - must_show_any_selector_groups
      - must_preserve_text
      - must_preserve_region
      - must_preserve_regions
      - must_hide_text_outside_allowed_region
    """
    raw_hints = ticket_context.get("validation_hints", {})

    if isinstance(raw_hints, Mapping) and raw_hints:
        hints = dict(raw_hints)

        # Backward compatibility:
        # Old hints used "must_show_selectors" as if all were required.
        # That is too strict for generic selector candidates, so convert them
        # into an "any group" unless caller explicitly sets must_show_mode="all".
        if "must_show_selectors" in hints and "must_show_all_selectors" not in hints:
            selectors = hints.get("must_show_selectors", [])
            mode = str(hints.get("must_show_mode", "any")).lower()

            if mode == "all":
                hints["must_show_all_selectors"] = selectors
            else:
                groups = list(hints.get("must_show_any_selector_groups", []))
                groups.append(
                    {
                        "name": "must_show_selectors",
                        "selectors": selectors,
                        "min": 1,
                    }
                )
                hints["must_show_any_selector_groups"] = groups

            hints.pop("must_show_selectors", None)

        text_hints = _as_string_list(
            hints.get("must_preserve_text", [])
            or hints.get("preserve_text", [])
            or hints.get("must_contain_text", [])
        )
        if text_hints:
            hints["must_preserve_text"] = text_hints

        return hints

    problem_type = str(ticket_context.get("problem_type", "unknown")).lower()

    if problem_type == "content_broken_image":
        return {
            "min_visible_images": 1,
            "max_broken_images": 0,
        }

    if problem_type == "content_broken_video":
        return {
            "min_visible_videos": 1,
            "must_show_any_selector_groups": [
                {
                    "name": "video_or_player",
                    "selectors": [
                        "video",
                        "iframe",
                        ".player",
                        ".video",
                        "[class*='player']",
                        "[class*='video']",
                    ],
                    "min": 1,
                }
            ],
        }

    if problem_type == "ui_hidden":
        return {
            "must_show_any_selector_groups": [
                {
                    "name": "search",
                    "selectors": [
                        ".search",
                        ".search-box",
                        "input[type='search']",
                        "[class*='search']",
                        "[id*='search']",
                    ],
                    "min": 1,
                },
                {
                    "name": "menu_or_navigation",
                    "selectors": [
                        "header",
                        "nav",
                        ".header",
                        ".navbar",
                        ".menu",
                        "[class*='menu']",
                        "[class*='nav']",
                        "[class*='header']",
                    ],
                    "min": 1,
                },
            ],
        }

    if problem_type == "anti_adblock_or_overlay":
        return {
            "must_show_any_selector_groups": [
                {
                    "name": "main_or_form",
                    "selectors": [
                        "main",
                        "form",
                        "button",
                        "input",
                        "select",
                        "[class*='download']",
                    ],
                    "min": 1,
                }
            ],
            "must_hide_selectors": [
                ".ad-overlay",
                ".popup-ad",
                ".modal-ad",
                "[class*='overlay'][class*='ad']",
                "[class*='popup'][class*='ad']",
                "[id*='overlay'][id*='ad']",
                "[id*='popup'][id*='ad']",
            ],
        }

    return {}


def _get_preserve_region_hints(
    ticket_context: Dict[str, Any],
    hints: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Return region-preservation assertions.

    Supported shapes:
      validation_hints.must_preserve_region: {...}
      validation_hints.must_preserve_regions: [{...}]
      validation_hints.preserve_regions: [{...}]
      ticket_context.preserve_regions: [{...}]

    A region can be located by explicit selector or by visible text:
      {
        "name": "trusted_bookmakers",
        "must_contain_text": "Nhà cái uy tín",
        "min_visible_images": 3,
        "min_visible_links": 3
      }
    """
    regions: List[Dict[str, Any]] = []

    for key in (
        "must_preserve_region",
        "must_preserve_regions",
        "preserve_region",
        "preserve_regions",
    ):
        regions.extend(_normalize_region_list(hints.get(key)))

    # Top-level ticket_context.preserve_regions is produced by the
    # ticket_context flow / region_focus integration.
    regions.extend(_normalize_region_list(ticket_context.get("preserve_regions")))

    # If region_focus is explicitly a preserve/allowed region, treat it as a
    # preserve-region assertion only when caller gave count requirements inside
    # it. This avoids turning generic focus metadata into a strict assertion.
    region_focus = ticket_context.get("region_focus")
    if isinstance(region_focus, Mapping):
        mode = str(region_focus.get("mode", "")).lower()
        if any(token in mode for token in ("preserve", "allow", "allowed", "protect")):
            normalized = _normalize_region_hint(region_focus)
            if normalized and any(
                key in normalized
                for key in (
                    "min_visible_images",
                    "min_visible_links",
                    "min_visible_image_like",
                    "max_broken_images",
                )
            ):
                regions.append(normalized)

    # De-duplicate by stable name/text/selector.
    unique: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for region in regions:
        key = (
            str(region.get("name", ""))
            + "|"
            + str(region.get("must_contain_text", ""))
            + "|"
            + str(region.get("selector", ""))
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(region)

    return unique


def _get_allowed_region_hints(
    ticket_context: Dict[str, Any],
    hints: Dict[str, Any],
    preserve_regions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Return regions where ad-like text is allowed to remain.

    This is used by must_hide_text_outside_allowed_region.
    """
    regions: List[Dict[str, Any]] = []

    for key in (
        "allowed_ad_region",
        "allowed_ad_regions",
        "allowed_region",
        "allowed_regions",
    ):
        regions.extend(_normalize_region_list(hints.get(key)))

    regions.extend(_normalize_region_list(ticket_context.get("allowed_regions")))

    # A preserve region is also an allowed region unless caller provides a
    # separate allowed-region list.
    regions.extend(preserve_regions)

    region_focus = ticket_context.get("region_focus")
    if isinstance(region_focus, Mapping):
        mode = str(region_focus.get("mode", "")).lower()
        if any(token in mode for token in ("preserve", "allow", "allowed", "protect")):
            normalized = _normalize_region_hint(region_focus)
            if normalized:
                regions.append(normalized)

    unique: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for region in regions:
        key = (
            str(region.get("name", ""))
            + "|"
            + str(region.get("must_contain_text", ""))
            + "|"
            + str(region.get("selector", ""))
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(region)

    return unique


def _normalize_region_list(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []

    if isinstance(value, Mapping):
        normalized = _normalize_region_hint(value)
        return [normalized] if normalized else []

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [
            {
                "name": text,
                "must_contain_text": text,
            }
        ]

    if isinstance(value, list):
        regions: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, Mapping):
                normalized = _normalize_region_hint(item)
                if normalized:
                    regions.append(normalized)
            elif isinstance(item, str) and item.strip():
                regions.append(
                    {
                        "name": item.strip(),
                        "must_contain_text": item.strip(),
                    }
                )
        return regions

    return []


def _normalize_region_hint(value: Mapping[str, Any]) -> Dict[str, Any]:
    region = dict(value)

    selector = (
        region.get("selector")
        or region.get("root_selector")
        or region.get("container_selector")
        or region.get("region_selector")
        or ""
    )
    text = (
        region.get("must_contain_text")
        or region.get("text")
        or region.get("title")
        or ""
    )
    name = region.get("name") or text or selector or "unnamed_region"

    normalized: Dict[str, Any] = {
        "name": str(name).strip(),
    }

    if selector:
        normalized["selector"] = str(selector).strip()

    if text:
        normalized["must_contain_text"] = str(text).strip()

    for key in (
        "min_visible_images",
        "min_visible_links",
        "min_visible_image_like",
        "max_broken_images",
        "max_ancestor_depth",
    ):
        if key in region:
            normalized[key] = region.get(key)

    if not normalized.get("selector") and not normalized.get("must_contain_text"):
        return {}

    return normalized


def _find_preserve_region_config(
    ticket_context: Dict[str, Any],
    hints: Dict[str, Any],
    region_name: str,
) -> Dict[str, Any]:
    target = str(region_name or "")
    for region in _get_preserve_region_hints(ticket_context, hints):
        if str(region.get("name", "")) == target:
            return region

    return {}


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None

    try:
        return int(value)
    except Exception:
        return None


def _find_broken_critical_selectors(
    reference_counts: Dict[str, int],
    tested_counts: Dict[str, int],
) -> List[str]:
    broken = []

    for selector, before_count in reference_counts.items():
        try:
            before = int(before_count or 0)
            after = int(tested_counts.get(selector, 0) or 0)
        except Exception:
            continue

        if before > 0 and after <= 0:
            broken.append(selector)

    return broken


def _network_targets_blocked(
    network_rules: List[_NetworkRule],
    blocked_requests: List[str],
) -> bool:
    """
    Return True only when blocked requests look like visible ad resources.

    Do not count analytics/tracking-only requests as successful visible ad blocking.
    This prevents false positives such as:
        ||googletagmanager.com^
        ||www.google-analytics.com^
    """
    if not network_rules or not blocked_requests:
        return False

    for request_url in blocked_requests:
        if _is_tracking_only_url(request_url):
            continue

        if _looks_like_visible_ad_url(request_url):
            return True

        for rule in network_rules:
            if _looks_like_visible_ad_rule(rule.original):
                return True

    return False


def _candidate_blocked_requests(
    candidate_rules: List[str],
    blocked_by_rule: Dict[str, List[str]],
) -> List[str]:
    candidate_set = set(candidate_rules)
    blocked: List[str] = []

    for rule, urls in blocked_by_rule.items():
        if rule in candidate_set:
            blocked.extend(urls or [])

    return blocked


def _is_tracking_only_url(url: str) -> bool:
    host = _hostname(url)

    if not host:
        return False

    host = host.lower().strip(".")

    return _host_in_domains(host, TRACKING_ONLY_DOMAINS)


def _looks_like_visible_ad_url(url: str) -> bool:
    text = str(url or "").lower()
    host = _hostname(text)

    if host and _host_in_domains(host, VISIBLE_AD_DOMAINS):
        return True

    return any(pattern in text for pattern in VISIBLE_AD_URL_PATTERNS)


def _looks_like_visible_ad_rule(rule: str) -> bool:
    text = str(rule or "").lower()

    if any(domain in text for domain in TRACKING_ONLY_DOMAINS):
        return False

    if any(domain in text for domain in VISIBLE_AD_DOMAINS):
        return True

    return any(pattern in text for pattern in VISIBLE_AD_RULE_PATTERNS)


def _cosmetic_targets_blocked(
    selectors: List[str],
    reference_dom_counts: Dict[str, int],
    tested_dom_counts: Dict[str, int],
    tested_visible_counts: Dict[str, int],
) -> Dict[str, Any]:
    """
    Check whether cosmetic rules actually hid or removed targeted ad selectors.

    Returns diagnostic details so validation output can show evidence:
      - missing: selector existed before, no longer exists after
      - hidden: selector existed before and remains in DOM, but is no longer visible
    """
    missing: List[str] = []
    hidden: List[str] = []

    for selector in selectors:
        before = int(reference_dom_counts.get(selector, 0) or 0)

        if before <= 0:
            continue

        after_dom = int(tested_dom_counts.get(selector, 0) or 0)
        after_visible = int(tested_visible_counts.get(selector, 0) or 0)

        if after_dom == 0:
            missing.append(selector)
            continue

        if after_visible == 0:
            hidden.append(selector)

    return {
        "blocked": bool(missing or hidden),
        "missing": missing,
        "hidden": hidden,
    }


def _extract_applicable_cosmetic_selectors(
    rules: List[str],
    document_url: str,
) -> List[str]:
    hide_selectors, exception_selectors = _extract_cosmetic_hide_and_exception_selectors(
        rules,
        document_url,
    )

    return [
        selector
        for selector in hide_selectors
        if selector not in exception_selectors
    ]


def _extract_cosmetic_hide_and_exception_selectors(
    rules: List[str],
    document_url: str,
) -> tuple[List[str], List[str]]:
    hide_selectors: List[str] = []
    exception_selectors: List[str] = []

    for raw_rule in rules:
        rule = str(raw_rule or "").strip()

        if not rule:
            continue

        if "#@#" in rule:
            domain_part, selector = rule.split("#@#", 1)
            target = exception_selectors
        elif "##" in rule:
            domain_part, selector = rule.split("##", 1)
            target = hide_selectors
        else:
            continue

        domain_part = domain_part.strip()
        selector = selector.strip()

        if not selector:
            continue

        if domain_part and not _cosmetic_domain_applies(domain_part, document_url):
            continue

        target.append(selector)

    return hide_selectors, exception_selectors


def _cosmetic_domain_applies(domain_part: str, document_url: str) -> bool:
    if not domain_part:
        return True

    page_host = _hostname(document_url)

    if not page_host:
        return False

    domains = [
        item.strip()
        for item in domain_part.split(",")
        if item.strip()
    ]

    if not domains:
        return True

    included = []
    excluded = []

    for domain in domains:
        if domain.startswith("~"):
            excluded.append(domain[1:])
        else:
            included.append(domain)

    for domain in excluded:
        if _host_matches_domain(page_host, domain):
            return False

    if not included:
        return True

    return any(_host_matches_domain(page_host, domain) for domain in included)


def _parse_network_rule(rule: str) -> Optional[_NetworkRule]:
    original = str(rule or "").strip()

    if not original:
        return None

    if "##" in original or "#@#" in original:
        return None

    is_exception = original.startswith("@@")
    network_rule = original[2:] if is_exception else original

    if not network_rule:
        return None

    pattern = network_rule
    options_str = ""

    if "$" in network_rule:
        pattern, options_str = network_rule.split("$", 1)

    pattern = pattern.strip()

    if not pattern:
        return None

    options = _parse_rule_options(options_str)
    regex = _pattern_to_regex(pattern, match_case=options.match_case)

    return _NetworkRule(
        original=original,
        pattern=pattern,
        regex=regex,
        options=options,
        is_exception=is_exception,
    )


def _parse_rule_options(options_str: str) -> _RuleOptions:
    options = _RuleOptions()

    if not options_str:
        return options

    for raw_option in options_str.split(","):
        option = raw_option.strip()

        if not option:
            continue

        negated = option.startswith("~")
        option_body = option[1:] if negated else option

        if option_body == "third-party":
            options.third_party = not negated
            continue

        if option_body == "first-party":
            options.third_party = False if not negated else True
            continue

        if option_body == "match-case":
            options.match_case = not negated
            continue

        if option_body.startswith("domain="):
            domain_value = option_body.split("=", 1)[1]
            includes, excludes = _parse_domain_option(domain_value)
            options.domain_includes.extend(includes)
            options.domain_excludes.extend(excludes)
            continue

        resource_types = RESOURCE_TYPE_OPTIONS.get(option_body)

        if resource_types:
            if negated:
                options.excluded_resource_types.update(resource_types)
            else:
                options.resource_types.update(resource_types)

    return options


def _parse_domain_option(value: str) -> tuple[List[str], List[str]]:
    includes = []
    excludes = []

    for raw_domain in value.split("|"):
        domain = raw_domain.strip()

        if not domain:
            continue

        if domain.startswith("~"):
            excludes.append(domain[1:])
        else:
            includes.append(domain)

    return includes, excludes


def _pattern_to_regex(pattern: str, match_case: bool = False) -> re.Pattern:
    flags = 0 if match_case else re.IGNORECASE

    if pattern.startswith("||"):
        body = pattern[2:]

        if body.endswith("^"):
            body = body[:-1]

        body = body.replace("^", "")

        escaped = re.escape(body)
        regex = r"^https?://([^/]+\.)?" + escaped.replace(r"\*", ".*")
        return re.compile(regex, flags)

    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*", ".*")
    escaped = escaped.replace(r"\^", r"(?:[^\w.%_-]|$)")

    return re.compile(escaped, flags)


def _domain_option_applies(
    document_url: str,
    includes: List[str],
    excludes: List[str],
) -> bool:
    host = _hostname(document_url)

    if not host:
        return False

    for excluded in excludes:
        if _host_matches_domain(host, excluded):
            return False

    if not includes:
        return True

    return any(_host_matches_domain(host, included) for included in includes)


def _is_third_party(request_url: str, document_url: str) -> bool:
    request_host = _hostname(request_url)
    document_host = _hostname(document_url)

    if not request_host or not document_host:
        return False

    return not (
        request_host == document_host
        or request_host.endswith("." + document_host)
        or document_host.endswith("." + request_host)
    )


def _hostname(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _host_matches_domain(host: str, domain: str) -> bool:
    host = (host or "").lower().strip(".")
    domain = (domain or "").lower().strip(".")

    if not host or not domain:
        return False

    return host == domain or host.endswith("." + domain)


def _host_in_domains(host: str, domains: set[str]) -> bool:
    host = (host or "").lower().strip(".")

    if not host:
        return False

    return any(_host_matches_domain(host, domain) for domain in domains)


def _safe_ticket_context(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return dict(value)

    return {
        "problem_type": "unknown",
        "raw": str(value),
    }


def _should_apply_existing_rules(problem_type: str, candidate_rules: List[str]) -> bool:
    """
    Existing/current rules are needed when validating exception-style fixes.

    For visible-ad tickets, applying broad current_rules can make an unrelated
    candidate fail preserve assertions, especially when current_rules are only
    supplied as review context from a previous legacy run.
    """
    normalized_problem_type = str(problem_type or "").strip().lower()

    if normalized_problem_type in {
        "content_broken_image",
        "content_broken_video",
        "content_broken",
        "ui_hidden",
    }:
        return True

    return any(_is_exception_rule(rule) for rule in candidate_rules)


def _is_exception_rule(rule: str) -> bool:
    text = str(rule or "").strip()
    return text.startswith("@@") or "#@#" in text


def _as_string_list(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, str):
        return [
            item.strip()
            for item in re.split(r"[,;\n]+", value)
            if item.strip()
        ]

    if isinstance(value, list):
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]

    return [str(value).strip()] if str(value).strip() else []


def _get_existing_rules(ticket_context: Dict[str, Any]) -> List[str]:
    """
    Return current/active rules that are already enabled in Adblock.

    These are optional, but very useful for validating exception rules.
    Accepted keys:
      - current_rules
      - existing_rules
      - active_rules
    """
    for key in ("current_rules", "existing_rules", "active_rules"):
        value = ticket_context.get(key)

        if isinstance(value, list):
            return [
                str(rule).strip()
                for rule in value
                if rule and str(rule).strip()
            ]

        if isinstance(value, str) and value.strip():
            return [
                line.strip()
                for line in value.splitlines()
                if line.strip()
            ]

    return []
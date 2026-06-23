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
#     visible ad issue        => ads_blocked AND page_functional
#     image/video/content bug => page_functional AND ticket assertions
#     UI hidden bug           => page_functional AND ticket assertions
#     overlay/anti-adblock    => page_functional AND ticket assertions
# - Avoid requiring every generic selector in validation_hints to be visible.

import io
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000
NETWORK_IDLE_TIMEOUT_MS = 5_000
PAGE_SETTLE_DELAY_SECONDS = 0.5
LAYOUT_DIFF_FAIL_THRESHOLD = 0.40
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

    const visibleImages = Array.from(document.querySelectorAll('img')).filter((img) => {
        return isVisible(img) && img.naturalWidth > 0 && img.naturalHeight > 0;
    }).length;

    const brokenImages = Array.from(document.querySelectorAll('img')).filter((img) => {
        if (!isVisible(img)) return false;
        if (!img.src) return false;
        return img.complete && (img.naturalWidth === 0 || img.naturalHeight === 0);
    }).length;

    const visibleVideos = Array.from(document.querySelectorAll('video')).filter(isVisible).length;
    const visibleIframes = Array.from(document.querySelectorAll('iframe')).filter(isVisible).length;

    return {
        must_show: mustShow,
        must_exist: mustExist,
        must_hide: mustHide,
        must_show_any_groups: anyGroups,
        visible_images: visibleImages,
        broken_images: brokenImages,
        visible_videos: visibleVideos,
        visible_iframes: visibleIframes,
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

    layout_diff_pct: float = 0.0
    blocked_requests: List[str] = field(default_factory=list)
    candidate_blocked_requests: List[str] = field(default_factory=list)
    missing_ad_selectors: List[str] = field(default_factory=list)
    hidden_ad_selectors: List[str] = field(default_factory=list)
    broken_selectors: List[str] = field(default_factory=list)
    tested_screenshot: bytes = field(default_factory=bytes)
    error: str = ""


def run_sandbox(
    url: str,
    rules: List[str],
    ticket_context: Optional[Dict[str, Any]] = None,
) -> SandboxResult:
    """
    Test candidate ABP rules against a live page.

    For normal ad-block tickets:
        passed = ads_blocked AND page_functional AND ticket_assertions_passed

    For breakage tickets such as image/video/content/UI hidden:
        passed = page_functional AND ticket_assertions_passed

    ticket_context can include:
        {
            "problem_type": "content_broken_image",
            "current_rules": ["||cdn.example.com^$image,domain=site.com"],
            "validation_hints": {...}
        }
    """
    result = SandboxResult(url=url, passed=False)

    candidate_rules = [
        str(rule).strip()
        for rule in rules
        if rule and str(rule).strip()
    ]

    safe_ticket_context = _safe_ticket_context(ticket_context)
    problem_type = str(safe_ticket_context.get("problem_type", "unknown")).strip().lower()

    existing_rules = _get_existing_rules(safe_ticket_context)
    all_test_rules = existing_rules + candidate_rules

    result.existing_rules_count = len(existing_rules)
    result.candidate_rules_count = len(candidate_rules)

    candidate_cosmetic_selectors = _extract_applicable_cosmetic_selectors(
        candidate_rules,
        url,
    )

    candidate_network_block_rules = [
        parsed for parsed in (_parse_network_rule(rule) for rule in candidate_rules)
        if parsed is not None and not parsed.is_exception
    ]

    if not candidate_rules:
        result.error = "no candidate rules supplied"
        return result

    try:
        from ..crawler.browser import (
            _DEFAULT_USER_AGENT,
            _STEALTH_LAUNCH_ARGS,
            _apply_stealth,
            _import_playwright,
        )
    except Exception as exc:
        result.error = f"browser helpers unavailable: {exc}"
        logger.exception(result.error)
        return result

    try:
        sync_playwright, _, PlaywrightTimeoutError = _import_playwright()
    except ImportError as exc:
        result.error = str(exc)
        logger.exception("Playwright import failed")
        return result

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=_STEALTH_LAUNCH_ARGS,
            )

            try:
                # ------------------------------------------------------
                # Reference page: no adblock rules.
                # Used to detect whether candidate patch breaks normal page.
                # ------------------------------------------------------
                reference_context = browser.new_context(
                    user_agent=_DEFAULT_USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                reference_page = reference_context.new_page()
                _apply_stealth(reference_page, user_agent=_DEFAULT_USER_AGENT)

                _load_page(reference_page, url, PlaywrightTimeoutError)

                reference_state = _capture_page_state(
                    reference_page,
                    candidate_cosmetic_selectors,
                )
                reference_screenshot = reference_page.screenshot(
                    full_page=True,
                    timeout=DEFAULT_TIMEOUT_MS,
                )
                reference_context.close()

                # ------------------------------------------------------
                # Baseline page: existing/current rules only.
                # This helps reproduce breakage if current_rules are passed.
                # ------------------------------------------------------
                baseline_context = browser.new_context(
                    user_agent=_DEFAULT_USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                baseline_page = baseline_context.new_page()
                _apply_stealth(baseline_page, user_agent=_DEFAULT_USER_AGENT)
                setattr(baseline_page, "_adblock_document_url", url)

                if existing_rules:
                    _apply_network_rules(baseline_page, existing_rules)
                    _load_page(baseline_page, url, PlaywrightTimeoutError)
                    _apply_cosmetic_rules(baseline_page, existing_rules)
                    time.sleep(PAGE_SETTLE_DELAY_SECONDS)
                else:
                    _load_page(baseline_page, url, PlaywrightTimeoutError)

                baseline_ticket_state = _capture_ticket_state(
                    baseline_page,
                    safe_ticket_context,
                )
                baseline_context.close()

                # ------------------------------------------------------
                # Test page: existing/current rules + candidate patch.
                # Candidate exception rules can now override existing blocks.
                # ------------------------------------------------------
                test_context = browser.new_context(
                    user_agent=_DEFAULT_USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                test_page = test_context.new_page()
                _apply_stealth(test_page, user_agent=_DEFAULT_USER_AGENT)
                setattr(test_page, "_adblock_document_url", url)

                _apply_network_rules(test_page, all_test_rules)
                _load_page(test_page, url, PlaywrightTimeoutError)
                _apply_cosmetic_rules(test_page, all_test_rules)

                time.sleep(PAGE_SETTLE_DELAY_SECONDS)

                tested_state = _capture_page_state(
                    test_page,
                    candidate_cosmetic_selectors,
                )
                tested_ticket_state = _capture_ticket_state(
                    test_page,
                    safe_ticket_context,
                )

                tested_screenshot = test_page.screenshot(
                    full_page=True,
                    timeout=DEFAULT_TIMEOUT_MS,
                )

                result.tested_screenshot = tested_screenshot
                result.blocked_requests = list(
                    getattr(test_page, "_adblock_blocked_requests", [])
                )

                blocked_by_rule = getattr(test_page, "_adblock_blocked_by_rule", {})
                result.candidate_blocked_requests = _candidate_blocked_requests(
                    candidate_rules,
                    blocked_by_rule,
                )

                test_context.close()

            finally:
                browser.close()

    except Exception as exc:
        result.error = f"sandbox runtime error: {exc}"
        logger.exception(result.error)
        return result

    # ------------------------------------------------------------------
    # Evaluate candidate ad blocking.
    # ------------------------------------------------------------------
    result.layout_diff_pct = _screenshot_diff(
        reference_screenshot,
        tested_screenshot,
    )

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

    cosmetic_targets_blocked = _cosmetic_targets_blocked(
        candidate_cosmetic_selectors,
        reference_state.get("ad_dom_counts", {}),
        tested_state.get("ad_dom_counts", {}),
        tested_state.get("ad_visible_counts", {}),
    )

    result.ads_blocked = (
        bool(candidate_network_block_rules) and network_targets_blocked
    ) or (
        cosmetic_targets_present and cosmetic_targets_blocked
    )

    # ------------------------------------------------------------------
    # Evaluate generic page functionality against reference/no-rule page.
    # ------------------------------------------------------------------
    reference_visible = max(int(reference_state.get("visible_count", 0) or 0), 1)
    tested_visible = int(tested_state.get("visible_count", 0) or 0)

    visible_ratio = tested_visible / reference_visible
    layout_ok = result.layout_diff_pct <= LAYOUT_DIFF_FAIL_THRESHOLD
    visible_count_ok = visible_ratio >= VISIBLE_ELEMENT_DROP_FAIL_RATIO

    result.page_functional = (
        not result.broken_selectors
        and layout_ok
        and visible_count_ok
    )

    # ------------------------------------------------------------------
    # Evaluate ticket-specific behavior.
    # ------------------------------------------------------------------
    baseline_ticket_ok, baseline_ticket_errors = _evaluate_ticket_assertions(
        safe_ticket_context,
        baseline_ticket_state,
    )
    result.baseline_ticket_assertions_passed = baseline_ticket_ok
    result.baseline_ticket_assertion_errors = baseline_ticket_errors

    ticket_ok, ticket_errors = _evaluate_ticket_assertions(
        safe_ticket_context,
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
        logger.warning("Sandbox did not verify any ad blocking for %s", url)

    if not result.page_functional:
        logger.warning(
            "Sandbox detected page functionality risk for %s: layout_diff=%.3f broken=%s",
            url,
            result.layout_diff_pct,
            result.broken_selectors,
        )

    if not result.ticket_assertions_passed:
        logger.warning(
            "Sandbox ticket assertions failed for %s: %s",
            url,
            result.ticket_assertion_errors,
        )

    return result


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

    payload = {
        "mustShowSelectors": hints.get("must_show_all_selectors", []),
        "mustExistSelectors": hints.get("must_exist_selectors", []),
        "mustHideSelectors": hints.get("must_hide_selectors", []),
        "mustShowAnySelectorGroups": hints.get("must_show_any_selector_groups", []),
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
    if not network_rules:
        return False

    if not blocked_requests:
        return False

    return True


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


def _cosmetic_targets_blocked(
    selectors: List[str],
    reference_dom_counts: Dict[str, int],
    tested_dom_counts: Dict[str, int],
    tested_visible_counts: Dict[str, int],
) -> bool:
    for selector in selectors:
        before = int(reference_dom_counts.get(selector, 0) or 0)

        if before <= 0:
            continue

        after_dom = int(tested_dom_counts.get(selector, 0) or 0)
        after_visible = int(tested_visible_counts.get(selector, 0) or 0)

        if after_dom == 0 or after_visible == 0:
            return True

    return False


def _screenshot_diff(reference: bytes, with_rules: bytes) -> float:
    if reference == with_rules:
        return 0.0

    if not reference or not with_rules:
        return 1.0

    try:
        from PIL import Image

        reference_img = Image.open(io.BytesIO(reference)).convert("RGB")
        with_rules_img = Image.open(io.BytesIO(with_rules)).convert("RGB")

        width = min(reference_img.width, with_rules_img.width)
        height = min(reference_img.height, with_rules_img.height)

        if width == 0 or height == 0:
            return 1.0

        reference_img = reference_img.crop((0, 0, width, height))
        with_rules_img = with_rules_img.crop((0, 0, width, height))

        reference_pixels = reference_img.load()
        with_rules_pixels = with_rules_img.load()

        changed = 0
        total = width * height

        for y in range(height):
            for x in range(width):
                if reference_pixels[x, y] != with_rules_pixels[x, y]:
                    changed += 1

        return changed / total if total else 1.0

    except Exception as exc:
        logger.warning("Screenshot diff failed: %s", exc)
        return 0.0


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


def _safe_ticket_context(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return dict(value)

    return {
        "problem_type": "unknown",
        "raw": str(value),
    }


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
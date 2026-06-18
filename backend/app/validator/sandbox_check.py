# Stage 3 of rule validation — live browser sandbox test:
# load page without rules (baseline) → screenshot + DOM snapshot
# apply network rules via page.route() to abort matching requests
# apply cosmetic rules by injecting a <style> block into the page
# load page with rules applied → re-screenshot + re-inspect DOM
# verify targeted ad elements are removed or requests are blocked
# verify critical page elements (nav, content, controls) still present
# compare screenshots to detect large unintended layout changes
#
# Input:  rule strings that passed stages 1 and 2 + original page URL
# Output: SandboxResult (ads_blocked, page_functional, layout_diff_pct, broken_selectors)
# Note:   most expensive stage — only runs after abp_syntax.py and rule_scope.py pass

import io
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000
NETWORK_IDLE_TIMEOUT_MS = 5_000
PAGE_SETTLE_DELAY_SECONDS = 2.0
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
    "subdocument": {"document"},
    "document": {"document"},
    "websocket": {"websocket"},
    "font": {"font"},
    "media": {"media"},
    "ping": {"ping", "other"},
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
        for (const selector of selectors) {
            result[selector] = countMatches(selector, visibleOnly);
        }
        return result;
    };

    return {
        visible_count: Array.from(document.querySelectorAll('body *')).filter(isVisible).length,
        critical_counts: countMap(payload.criticalSelectors || []),
        ad_dom_counts: countMap(payload.adSelectors || [], false),
        ad_visible_counts: countMap(payload.adSelectors || [], true),
        interactive_count: countMatches(payload.interactiveSelector || 'button, a[href]'),
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
        if not _domain_option_applies(document_url, self.options.domain_includes, self.options.domain_excludes):
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


@dataclass
class SandboxResult:
    """Result of testing one set of rules against a live page."""
    url: str
    passed: bool                          # True only if ads_blocked AND page_functional
    ads_blocked: bool = False             # All targeted ad elements were removed/blocked
    page_functional: bool = False         # Navigation, content, and key controls still present
    layout_diff_pct: float = 0.0          # Rough pixel-diff fraction between baseline and with-rules
    blocked_requests: List[str] = field(default_factory=list)   # Third-party URLs that were blocked
    missing_ad_selectors: List[str] = field(default_factory=list)  # Selectors that disappeared (good)
    hidden_ad_selectors: List[str] = field(default_factory=list)  # Selectors hidden by cosmetic CSS (good)
    broken_selectors: List[str] = field(default_factory=list)   # Non-ad selectors that disappeared (bad)
    tested_screenshot: bytes = field(default_factory=bytes)     # Page after rules applied
    error: str = ""
    unreachable: bool = False             # True when the target URL can't be loaded (bot-block, DNS, SSL)


def run_sandbox(url: str, rules: List[str], environment: str = "desktop") -> SandboxResult:
    """
    Test a set of candidate ABP rules against the live page.

    Steps:
        1. Load page WITHOUT rules (baseline):
           - Screenshot the full page
           - Record which ad-candidate selectors are present in the DOM
           - Record which third-party domains make network requests

        2. Load page WITH rules applied:
           - Network rules: intercept via page.route() and abort matching requests
           - Cosmetic rules: inject a <style> tag that sets display:none on matched selectors
           - Re-screenshot and re-inspect DOM

        3. Compare baseline vs. with-rules:
           - ads_blocked: targeted selectors gone OR targeted requests aborted
           - page_functional: critical nav/content selectors still present,
             layout_diff_pct below threshold (< 40% of pixels changed)
           - broken_selectors: non-ad selectors that disappeared (false positives)

    Args:
        url:         The original reported page URL.
        rules:       List of rule strings that passed stages 1 and 2.
        environment: Crawl environment name ("desktop", "android", "ios"). The sandbox
                     uses the matching viewport and user-agent so the page renders the
                     same layout as during the crawl. Always uses Chromium (page.route()
                     is not available in WebKit), so iOS uses the iOS UA/viewport on
                     Chromium as a best-effort match.

    Returns:
        SandboxResult. passed=True only if ads_blocked=True AND page_functional=True.
    """
    result = SandboxResult(url=url, passed=False)
    clean_rules = [rule.strip() for rule in rules if rule and rule.strip()]
    cosmetic_selectors = _extract_applicable_cosmetic_selectors(clean_rules, url)
    network_block_rules = [
        parsed for parsed in (_parse_network_rule(rule) for rule in clean_rules)
        if parsed is not None and not parsed.is_exception
    ]

    if not clean_rules:
        result.error = "no rules supplied"
        return result

    try:
        from ..crawler.browser import (
            ENVIRONMENTS,
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

    profile = ENVIRONMENTS.get(environment, ENVIRONMENTS["desktop"])
    ua = profile["user_agent"]
    context_kwargs: Dict[str, Any] = {
        "user_agent": ua,
        "viewport": profile["viewport"],
        "device_scale_factor": profile["device_scale_factor"],
        "is_mobile": profile["is_mobile"],
        "has_touch": profile["has_touch"],
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }

    logger.debug("Sandbox running in '%s' environment (viewport %s, mobile=%s)",
                 environment, profile["viewport"], profile["is_mobile"])

    try:
        with sync_playwright() as playwright:
            # Always use Chromium — WebKit does not support page.route() for network blocking
            browser = playwright.chromium.launch(
                headless=True,
                args=_STEALTH_LAUNCH_ARGS,
            )
            try:
                baseline_context = browser.new_context(**context_kwargs)
                baseline_page = baseline_context.new_page()
                _apply_stealth(baseline_page, user_agent=ua)
                _load_page(baseline_page, url, PlaywrightTimeoutError)
                baseline_state = _capture_page_state(baseline_page, cosmetic_selectors)
                baseline_screenshot = baseline_page.screenshot(full_page=True, timeout=DEFAULT_TIMEOUT_MS)
                baseline_context.close()

                test_context = browser.new_context(**context_kwargs)
                test_page = test_context.new_page()
                _apply_stealth(test_page, user_agent=ua)
                setattr(test_page, "_adblock_document_url", url)
                _apply_network_rules(test_page, clean_rules)
                _load_page(test_page, url, PlaywrightTimeoutError)
                _apply_cosmetic_rules(test_page, clean_rules)
                time.sleep(PAGE_SETTLE_DELAY_SECONDS)

                tested_state = _capture_page_state(test_page, cosmetic_selectors)
                tested_screenshot = test_page.screenshot(full_page=True, timeout=DEFAULT_TIMEOUT_MS)
                result.blocked_requests = list(getattr(test_page, "_adblock_blocked_requests", []))
                blocked_by_rule: Dict[str, List[str]] = dict(getattr(test_page, "_adblock_blocked_by_rule", {}))
                result.tested_screenshot = tested_screenshot
                test_context.close()
            finally:
                browser.close()
    except Exception as exc:
        error_str = str(exc)
        result.error = f"sandbox browser failed for {url}: {exc}"
        if any(pat in error_str for pat in _UNREACHABLE_PATTERNS):
            result.unreachable = True
            logger.warning("Sandbox unreachable (connection error) for %s: %s", url, error_str.splitlines()[0])
        else:
            logger.exception(result.error)
        return result

    result.layout_diff_pct = _screenshot_diff(baseline_screenshot, tested_screenshot)
    result.missing_ad_selectors = _missing_ad_selectors(
        cosmetic_selectors,
        baseline_state.get("ad_dom_counts", {}),
        tested_state.get("ad_dom_counts", {}),
    )
    result.hidden_ad_selectors = _hidden_ad_selectors(
        cosmetic_selectors,
        baseline_state.get("ad_visible_counts", {}),
        tested_state.get("ad_visible_counts", {}),
    )
    result.broken_selectors = _broken_critical_selectors(
        baseline_state,
        tested_state,
    )

    network_targets_blocked = all(
        blocked_by_rule.get(network_rule.original)
        for network_rule in network_block_rules
    )
    cosmetic_targets_present = any(
        baseline_state.get("ad_dom_counts", {}).get(selector, 0) > 0
        for selector in cosmetic_selectors
    )
    cosmetic_targets_blocked = _cosmetic_targets_blocked(
        cosmetic_selectors,
        baseline_state.get("ad_dom_counts", {}),
        tested_state.get("ad_dom_counts", {}),
        tested_state.get("ad_visible_counts", {}),
    )

    has_verified_network_block = bool(network_block_rules) and network_targets_blocked
    has_verified_cosmetic_block = cosmetic_targets_present and cosmetic_targets_blocked
    result.ads_blocked = has_verified_network_block or has_verified_cosmetic_block

    baseline_visible = max(int(baseline_state.get("visible_count", 0)), 1)
    tested_visible = int(tested_state.get("visible_count", 0))
    visible_ratio = tested_visible / baseline_visible
    layout_ok = result.layout_diff_pct <= LAYOUT_DIFF_FAIL_THRESHOLD
    visible_count_ok = visible_ratio >= VISIBLE_ELEMENT_DROP_FAIL_RATIO
    result.page_functional = not result.broken_selectors and layout_ok and visible_count_ok
    result.passed = result.ads_blocked and result.page_functional

    if not result.ads_blocked:
        logger.warning("Sandbox did not verify any ad blocking for %s", url)
    if not result.page_functional:
        logger.warning(
            "Sandbox detected page functionality risk for %s: layout_diff=%.3f broken=%s",
            url,
            result.layout_diff_pct,
            result.broken_selectors,
        )

    return result


def _apply_network_rules(page, network_rules: List[str]) -> None:
    """
    Register page.route() handlers that abort requests matching the given
    network blocking rules. Cosmetic rules are skipped here.
    """
    parsed_rules = [
        parsed for parsed in (_parse_network_rule(rule) for rule in network_rules)
        if parsed is not None
    ]
    block_rules = [rule for rule in parsed_rules if not rule.is_exception]
    exception_rules = [rule for rule in parsed_rules if rule.is_exception]

    blocked_requests: List[str] = []
    blocked_by_rule: Dict[str, List[str]] = {rule.original: [] for rule in block_rules}
    setattr(page, "_adblock_blocked_requests", blocked_requests)
    setattr(page, "_adblock_blocked_by_rule", blocked_by_rule)

    if not block_rules:
        return

    def _route_handler(route):
        request = route.request
        request_url = request.url
        resource_type = getattr(request, "resource_type", "other")
        document_url = getattr(page, "_adblock_document_url", "") or getattr(page, "url", "")

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


def _apply_cosmetic_rules(page, cosmetic_rules: List[str]) -> None:
    """
    Inject a <style> block into the page that hides elements matching
    the CSS selectors from cosmetic rules.
    """
    document_url = getattr(page, "_adblock_document_url", "") or getattr(page, "url", "")
    selectors = _extract_applicable_cosmetic_selectors(cosmetic_rules, document_url)
    setattr(page, "_adblock_cosmetic_selectors", selectors)

    if not selectors:
        return

    style_lines = []
    for selector in selectors:
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


def _screenshot_diff(baseline: bytes, with_rules: bytes) -> float:
    """
    Return the fraction of pixels that differ between two screenshot byte blobs.
    Used to detect large unintended layout changes (threshold: > 0.40 = fail).
    """
    if baseline == with_rules:
        return 0.0
    if not baseline or not with_rules:
        return 1.0

    try:
        from PIL import Image

        baseline_img = Image.open(io.BytesIO(baseline)).convert("RGB")
        with_rules_img = Image.open(io.BytesIO(with_rules)).convert("RGB")
        baseline_area = baseline_img.width * baseline_img.height
        with_rules_area = with_rules_img.width * with_rules_img.height
        width = min(baseline_img.width, with_rules_img.width)
        height = min(baseline_img.height, with_rules_img.height)
        if width == 0 or height == 0:
            return 1.0
        baseline_img = baseline_img.crop((0, 0, width, height))
        with_rules_img = with_rules_img.crop((0, 0, width, height))

        changed = 0
        total = width * height
        for left, right in zip(baseline_img.getdata(), with_rules_img.getdata()):
            if sum(abs(left[i] - right[i]) for i in range(3)) > 30:
                changed += 1

        area_delta = abs(baseline_area - with_rules_area)
        return min(1.0, (changed + area_delta) / max(total, 1))
    except Exception:
        # PNG bytes are compressed, so a byte-by-byte comparison wildly
        # overstates small visual changes. Without Pillow, use a coarse
        # encoded-size delta instead of pretending this is a pixel diff.
        max_len = max(len(baseline), len(with_rules), 1)
        return min(1.0, abs(len(baseline) - len(with_rules)) / max_len)


def _load_page(page: Any, url: str, timeout_error_type: Any) -> None:
    page.goto(url, wait_until="load", timeout=DEFAULT_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
    except timeout_error_type:
        logger.debug("networkidle timeout during sandbox load for %s", url)
    time.sleep(PAGE_SETTLE_DELAY_SECONDS)


def _capture_page_state(page: Any, ad_selectors: List[str]) -> Dict[str, Any]:
    return page.evaluate(
        PAGE_STATE_SCRIPT,
        {
            "criticalSelectors": CRITICAL_SELECTORS,
            "adSelectors": ad_selectors,
            "interactiveSelector": INTERACTIVE_SELECTOR,
        },
    )


def _missing_ad_selectors(
    selectors: List[str],
    baseline_counts: Dict[str, int],
    tested_counts: Dict[str, int],
) -> List[str]:
    missing = []
    for selector in selectors:
        baseline_count = baseline_counts.get(selector, 0)
        tested_count = tested_counts.get(selector, 0)
        if baseline_count > 0 and tested_count == 0:
            missing.append(selector)
    return missing


def _hidden_ad_selectors(
    selectors: List[str],
    baseline_visible_counts: Dict[str, int],
    tested_visible_counts: Dict[str, int],
) -> List[str]:
    hidden = []
    for selector in selectors:
        baseline_count = baseline_visible_counts.get(selector, 0)
        tested_count = tested_visible_counts.get(selector, 0)
        if baseline_count > 0 and tested_count == 0:
            hidden.append(selector)
    return hidden


def _cosmetic_targets_blocked(
    selectors: List[str],
    baseline_dom_counts: Dict[str, int],
    tested_dom_counts: Dict[str, int],
    tested_visible_counts: Dict[str, int],
) -> bool:
    applicable_selectors = [
        selector for selector in selectors
        if baseline_dom_counts.get(selector, 0) > 0
    ]
    if not applicable_selectors:
        return False

    for selector in applicable_selectors:
        if tested_dom_counts.get(selector, 0) > 0 and tested_visible_counts.get(selector, 0) > 0:
            return False
    return True


def _broken_critical_selectors(
    baseline_state: Dict[str, Any],
    tested_state: Dict[str, Any],
) -> List[str]:
    broken = []
    baseline_counts = baseline_state.get("critical_counts", {})
    tested_counts = tested_state.get("critical_counts", {})

    for selector, baseline_count in baseline_counts.items():
        tested_count = tested_counts.get(selector, 0)
        if baseline_count > 0 and tested_count <= 0:
            broken.append(selector)

    baseline_interactive = int(baseline_state.get("interactive_count", 0))
    tested_interactive = int(tested_state.get("interactive_count", 0))
    if baseline_interactive >= 3 and tested_interactive <= max(1, int(baseline_interactive * 0.2)):
        broken.append(INTERACTIVE_SELECTOR)

    return broken


def _parse_network_rule(rule: str) -> Optional[_NetworkRule]:
    clean = (rule or "").strip()
    if not clean or clean.startswith("!") or _is_cosmetic_rule(clean):
        return None

    is_exception = clean.startswith("@@")
    if is_exception:
        clean = clean[2:]

    pattern, options_str = _split_rule_options(clean)
    if not pattern:
        return None

    options = _parse_rule_options(options_str)
    regex = _compile_abp_pattern(pattern, match_case=options.match_case)
    return _NetworkRule(
        original=rule.strip(),
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
        option_name = option[1:] if negated else option
        option_key, _, option_value = option_name.partition("=")
        option_key = option_key.lower()

        if option_key in RESOURCE_TYPE_OPTIONS:
            target_set = options.excluded_resource_types if negated else options.resource_types
            target_set.update(RESOURCE_TYPE_OPTIONS[option_key])
        elif option_key == "third-party":
            options.third_party = False if negated else True
        elif option_key == "first-party":
            options.third_party = True if negated else False
        elif option_key == "domain":
            includes, excludes = _parse_domain_list(option_value)
            options.domain_includes.extend(includes)
            options.domain_excludes.extend(excludes)
        elif option_key == "match-case":
            options.match_case = not negated

    return options


def _split_rule_options(rule: str) -> Tuple[str, str]:
    if "$" not in rule:
        return rule, ""
    pattern, options = rule.split("$", 1)
    return pattern, options


def _compile_abp_pattern(pattern: str, match_case: bool = False) -> re.Pattern:
    flags = 0 if match_case else re.IGNORECASE
    anchored_domain = pattern.startswith("||")
    anchored_start = pattern.startswith("|") and not anchored_domain
    anchored_end = pattern.endswith("|") and len(pattern) > 1

    if anchored_domain:
        pattern = pattern[2:]
        prefix = r"^[a-z][a-z0-9.+-]*://(?:[^/?#]*\.)?"
    elif anchored_start:
        pattern = pattern[1:]
        prefix = r"^"
    else:
        prefix = ""

    if anchored_end:
        pattern = pattern[:-1]

    translated = []
    for char in pattern:
        if char == "*":
            translated.append(".*")
        elif char == "^":
            translated.append(r"(?:[^A-Za-z0-9_.%-]|$)")
        else:
            translated.append(re.escape(char))

    suffix = "$" if anchored_end else ""
    return re.compile(prefix + "".join(translated) + suffix, flags)


def _is_cosmetic_rule(rule: str) -> bool:
    return "##" in rule or "#@#" in rule


def _parse_cosmetic_rule(rule: str) -> Optional[_CosmeticRule]:
    clean = (rule or "").strip()
    if not clean or clean.startswith("!"):
        return None

    if "#@#" in clean:
        domain_prefix, selector = clean.split("#@#", 1)
        is_exception = True
    elif "##" in clean:
        domain_prefix, selector = clean.split("##", 1)
        is_exception = False
    else:
        return None

    selector = selector.strip()
    if not selector:
        return None

    return _CosmeticRule(
        original=clean,
        selector=selector,
        domain_prefix=domain_prefix.strip(),
        is_exception=is_exception,
    )


def _extract_applicable_cosmetic_selectors(rules: List[str], page_url: str) -> List[str]:
    selectors: List[str] = []
    exception_selectors = set()

    for rule in rules:
        parsed = _parse_cosmetic_rule(rule)
        if parsed is None:
            continue
        if not _cosmetic_domain_applies(parsed.domain_prefix, page_url):
            continue
        if parsed.is_exception:
            exception_selectors.add(parsed.selector)
        else:
            selectors.append(parsed.selector)

    return [selector for selector in selectors if selector not in exception_selectors]


def _cosmetic_domain_applies(domain_prefix: str, page_url: str) -> bool:
    if not domain_prefix:
        return True
    includes, excludes = _parse_domain_list(domain_prefix.replace(",", "|"))
    return _domain_option_applies(page_url, includes, excludes)


def _parse_domain_list(raw_value: str) -> Tuple[List[str], List[str]]:
    includes: List[str] = []
    excludes: List[str] = []
    for raw_domain in raw_value.split("|"):
        domain = raw_domain.strip().lower()
        if not domain:
            continue
        if domain.startswith("~"):
            excludes.append(domain[1:])
        else:
            includes.append(domain)
    return includes, excludes


def _domain_option_applies(page_url: str, includes: List[str], excludes: List[str]) -> bool:
    page_host = _host(page_url)
    if not page_host:
        return False if includes else True
    if any(_domain_matches(page_host, domain) for domain in excludes):
        return False
    if includes and not any(_domain_matches(page_host, domain) for domain in includes):
        return False
    return True


def _is_third_party(request_url: str, document_url: str) -> bool:
    request_host = _host(request_url)
    document_host = _host(document_url)
    if not request_host or not document_host:
        return False
    return not (
        _domain_matches(request_host, document_host)
        or _domain_matches(document_host, request_host)
    )


def _domain_matches(host: str, domain: str) -> bool:
    host = host.lower().strip(".")
    domain = domain.lower().strip(".")
    return host == domain or host.endswith("." + domain)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""

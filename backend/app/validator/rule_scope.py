# Stage 2 of rule validation — rule scope check:
# detect overly broad network rules (pattern too short, matches common URL fragments)
# detect cosmetic rules with no domain scope applied to generic selectors (div, span, a...)
# detect common element risk (bare tag selectors with no class/ID qualifier)
# detect exception rules that conflict with block rules in the same batch
#
# Input:  list of rule strings that passed abp_syntax.py
# Output: list of ScopeResult objects (safe: bool, risk: str)
# Note:   runs after abp_syntax.py, before sandbox_check.py — no browser required

import re
from dataclasses import dataclass
from typing import Literal, Optional

RiskType = Literal["overly_broad", "missing_scope", "common_element", None]

# Selectors that are too generic to be safe without a domain qualifier
GENERIC_SELECTORS = {"div", "span", "a", "img", "p", "section", "article", "ul", "li", "iframe", "body", "html"}

# Network patterns short enough to match huge swaths of URLs unintentionally
OVERLY_BROAD_PATTERNS = {"/", ".", "com", "net", "org", "http", "https", "www", "*"}


@dataclass
class ScopeResult:
    rule: str
    safe: bool
    risk: RiskType = None
    detail: Optional[str] = None


def check_scope(rule: str) -> ScopeResult:
    """
    Determine whether a rule's matching scope is acceptably narrow.

    This function assumes the rule already passed abp_syntax.py.
    It does not validate full ABP syntax again.
    """

    if not isinstance(rule, str):
        return ScopeResult(
            rule=str(rule),
            safe=False,
            risk="overly_broad",
            detail="Rule must be a string."
        )

    cleaned = rule.strip()

    if not cleaned:
        return ScopeResult(
            rule=rule,
            safe=False,
            risk="overly_broad",
            detail="Empty rule is not safe."
        )

    if _is_cosmetic_rule(cleaned):
        return _check_cosmetic_scope(cleaned)

    return _check_network_scope(cleaned)


def check_scope_batch(rules: list[str]) -> list[ScopeResult]:
    """
    Run check_scope on each rule, then add a cross-rule pass to flag
    @@ exception rules that conflict with block rules in the same batch.
    """

    results = [check_scope(rule) for rule in rules]

    blocked_domains = set()

    for rule in rules:
        cleaned = rule.strip()

        if cleaned.startswith("@@"):
            continue

        domain = _extract_network_domain(cleaned)
        if domain:
            blocked_domains.add(domain)

    final_results: list[ScopeResult] = []

    for result in results:
        rule = result.rule.strip()

        # Keep earlier failure if already unsafe
        if not result.safe:
            final_results.append(result)
            continue

        if rule.startswith("@@"):
            exception_domain = _extract_network_domain(rule[2:])

            if exception_domain and exception_domain in blocked_domains:
                final_results.append(
                    ScopeResult(
                        rule=result.rule,
                        safe=False,
                        risk="overly_broad",
                        detail=f"Exception rule conflicts with blocking rule for domain: {exception_domain}"
                    )
                )
                continue

        final_results.append(result)

    return final_results


def _is_cosmetic_rule(rule: str) -> bool:
    return "##" in rule or "#@#" in rule


def _check_cosmetic_scope(rule: str) -> ScopeResult:
    domain_part, selector = _split_cosmetic_rule(rule)

    if selector is None:
        return ScopeResult(
            rule=rule,
            safe=False,
            risk="overly_broad",
            detail="Cannot extract cosmetic selector."
        )

    selector = selector.strip()
    domain_part = domain_part.strip() if domain_part else ""

    first_selector = _get_first_selector(selector)
    base_selector = _normalise_selector(first_selector)

    # Example: ##div or ##iframe
    if not domain_part and base_selector in GENERIC_SELECTORS:
        return ScopeResult(
            rule=rule,
            safe=False,
            risk="missing_scope",
            detail=f"Generic selector '{base_selector}' has no domain scope."
        )

    # Example: example.com##div
    # Still risky even with domain scope because it hides common structural elements.
    if base_selector in GENERIC_SELECTORS and not _has_selector_qualifier(first_selector):
        return ScopeResult(
            rule=rule,
            safe=False,
            risk="common_element",
            detail=f"Selector '{first_selector}' targets a common element without class, ID, or attribute qualifier."
        )

    # Example: ##.ad across all websites. Syntax-valid, but broad.
    if not domain_part and _looks_like_ad_selector(selector):
        return ScopeResult(
            rule=rule,
            safe=False,
            risk="missing_scope",
            detail="Ad-related cosmetic selector has no domain scope."
        )

    # Example: example.com##*
    if selector == "*" or selector.startswith("*:"):
        return ScopeResult(
            rule=rule,
            safe=False,
            risk="overly_broad",
            detail="Universal cosmetic selector is too broad."
        )

    return ScopeResult(rule=rule, safe=True)


def _check_network_scope(rule: str) -> ScopeResult:
    cleaned = rule.strip()

    if cleaned.startswith("@@"):
        cleaned = cleaned[2:]

    pattern = cleaned.split("$", 1)[0].strip()

    if pattern in {"*", "/*", "||*^"}:
        return ScopeResult(
            rule=rule,
            safe=False,
            risk="overly_broad",
            detail="Network rule matches too broadly."
        )

    domain = _extract_network_domain(pattern)

    if domain:
        domain_key = domain.lower().strip(".")

        if domain_key in OVERLY_BROAD_PATTERNS:
            return ScopeResult(
                rule=rule,
                safe=False,
                risk="overly_broad",
                detail=f"Network domain pattern '{domain_key}' is too broad."
            )

        if _is_public_suffix_like(domain_key):
            return ScopeResult(
                rule=rule,
                safe=False,
                risk="overly_broad",
                detail=f"Network rule targets a public suffix or too-general domain: {domain_key}"
            )

        return ScopeResult(rule=rule, safe=True)

    cleaned_pattern = pattern.lower().strip("/^|*.")

    if cleaned_pattern in OVERLY_BROAD_PATTERNS or len(cleaned_pattern) <= 2:
        return ScopeResult(
            rule=rule,
            safe=False,
            risk="overly_broad",
            detail=f"Network pattern '{pattern}' is too short or too general."
        )

    # Example: /ads/ may be valid syntax, but it can affect many URLs.
    if pattern in {"/ads/", "/ad/", "ads", "ad"}:
        return ScopeResult(
            rule=rule,
            safe=False,
            risk="overly_broad",
            detail=f"Generic path pattern '{pattern}' is too broad."
        )

    return ScopeResult(rule=rule, safe=True)


def _split_cosmetic_rule(rule: str) -> tuple[str, Optional[str]]:
    if "#@#" in rule:
        return rule.split("#@#", 1)

    if "##" in rule:
        return rule.split("##", 1)

    return "", None


def _get_first_selector(selector: str) -> str:
    """
    For grouped selectors, inspect the first selector only.
    Example: '.ad, .banner' -> '.ad'
    """

    return selector.split(",", 1)[0].strip()


def _normalise_selector(selector: str) -> str:
    """
    Reduce selector to its base tag if possible.
    Examples:
    div.ad -> div
    iframe[src*="ad"] -> iframe
    .ad-banner -> .ad-banner
    #ad -> #ad
    """

    selector = selector.strip()

    if not selector:
        return ""

    if selector.startswith(".") or selector.startswith("#") or selector.startswith("["):
        return selector

    match = re.match(r"^[A-Za-z][A-Za-z0-9_-]*", selector)
    if match:
        return match.group(0).lower()

    return selector.lower()


def _has_selector_qualifier(selector: str) -> bool:
    """
    A selector is more specific if it contains class, ID, or attribute qualifiers.
    Examples:
    div.ad        -> qualified
    div#ad        -> qualified
    iframe[src]   -> qualified
    div           -> not qualified
    """

    return "." in selector or "#" in selector or "[" in selector


def _looks_like_ad_selector(selector: str) -> bool:
    selector_lower = selector.lower()

    ad_keywords = [
        "ad", "ads", "advert", "advertisement", "banner",
        "sponsor", "sponsored", "promo", "popup", "tracking"
    ]

    return any(keyword in selector_lower for keyword in ad_keywords)


def _extract_network_domain(rule_or_pattern: str) -> Optional[str]:
    """
    Extract domain from common network rules.
    Examples:
    ||ads.example.com^ -> ads.example.com
    @@||ads.example.com^ -> ads.example.com
    https://ads.example.com/path -> ads.example.com
    """

    text = rule_or_pattern.strip()

    if text.startswith("@@"):
        text = text[2:]

    text = text.split("$", 1)[0]

    if text.startswith("||"):
        text = text[2:]
        match = re.match(r"^([A-Za-z0-9.-]+)", text)
        return match.group(1).lower() if match else None

    url_match = re.match(r"^https?://([A-Za-z0-9.-]+)", text)
    if url_match:
        return url_match.group(1).lower()

    return None


def _is_public_suffix_like(domain: str) -> bool:
    """
    Basic public-suffix-like check.
    This does not replace a real public suffix list, but catches obvious cases
    such as com, net, org, vn, com.vn.
    """

    public_suffix_like = {
        "com", "net", "org", "edu", "gov",
        "vn", "com.vn", "net.vn", "org.vn", "edu.vn", "gov.vn"
    }

    return domain in public_suffix_like
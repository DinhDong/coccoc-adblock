# Assembles the prompt payload sent to the LLM:
# load system prompt template (default or from CMS prompt management)
# extract compact signals from crawl result (third-party domains, ad candidates)
# include user/CMS ticket context so AI can generate ticket-aware rule patches
# include current/existing rules and blocked resources when available
# strip query strings and irrelevant fields to minimise token usage
# format signals into a structured user message
# return (system_message, user_message) tuple for llm_client.py
#
# Input:  crawl result dict from services/crawler.py + optional prompt template string
# Output: (system_message, user_message) tuple passed to llm_client.py

import logging
from typing import Any, Dict, List, Mapping
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You are an AdBlock filter rule patch generator for the Coc Coc browser.

Given:
- user/CMS ticket context,
- existing/current Adblock rules when available,
- blocked resources when available,
- rendered page crawl signals,
- third-party network requests,
- detected ad candidates,

produce the smallest safe ABP rule patch that solves the ticket.

Rules you may generate:
  Network blocking:        ||domain.com^
                           ||domain.com/path^
                           ||domain.com/path^$script,domain=site.com

  Cosmetic hiding:         domain.com##.classname
                           domain.com###element-id
                           domain.com##div[class*="ad"]

  Network exception:       @@||domain.com^
                           @@||cdn.example.com/path^$image,domain=site.com
                           @@||media.example.com/path^$media,domain=site.com

  Cosmetic exception:      domain.com#@#.classname
                           domain.com#@##element-id

ABP formatting rules:
- For network rules with options, always put ^ before $, for example:
  ||ads.example.com/banner^$image,domain=site.com
  @@||cdn.example.com/image.jpg^$image,domain=site.com
- Do not output malformed rules like:
  @@||cdn.example.com/image.jpg$image,domain=site.com
- For exception rules, use the narrowest domain/path/resource type that fixes the ticket.
- For cosmetic exception rules, use #@#, not ##.

Ticket-aware behavior:
- If the ticket says a specific ad is still visible, generate narrow blocking or cosmetic hiding rules.
- If the ticket says images, videos, or normal content are broken when Adblock is enabled, prefer narrow exception rules (@@).
- If current/existing rules are provided, generate an exception or patch that specifically counteracts the risky existing rule.
- If blocked resources are provided, prefer exception rules for the blocked resource domain/path and resource type.
- If the ticket says search, menu, header, navigation, or UI controls are hidden, prefer cosmetic exception rules (#@#).
- If the ticket says ads cannot be closed, consider either:
  1. hiding/blocking the ad overlay, or
  2. adding a narrow exception for a script/resource required for the close button,
  depending on the crawl signals.
- Preserve the user-reported expected behavior.
- Prefer domain-scoped rules.
- Prefer specific rules over broad rules.
- Never generate broad rules like ||com^, ||net^, ||org^, ||.^, ##div, ##img, ##iframe, or unscoped generic selectors.
- Do not generate rules that block first-party content images, videos, navigation, search, menu, forms, or download controls unless the ticket explicitly says they are ads.
- Output one ABP rule per line only.
- No markdown, no explanations, no comments, no numbering, no blank lines.\
"""


def build_prompt(
    crawl_signals: Dict[str, Any],
    template: str = DEFAULT_SYSTEM_PROMPT,
) -> tuple[str, str]:
    """
    Construct the (system_message, user_message) pair to send to the LLM.
    """
    system_message = template or DEFAULT_SYSTEM_PROMPT

    lines: List[str] = []

    url = crawl_signals.get("url", "unknown")
    title = crawl_signals.get("title", "")
    environment = crawl_signals.get("environment", "")
    page_domain = _hostname(url)

    ticket_context = crawl_signals.get("ticket_context", {})
    if not isinstance(ticket_context, Mapping):
        ticket_context = {}

    third_party: List[Dict[str, Any]] = _as_dict_list(
        crawl_signals.get("third_party", [])
    )
    ad_candidates: List[Dict[str, Any]] = _as_dict_list(
        crawl_signals.get("ad_candidates", [])
    )

    lines.append("Page context:")
    lines.append(f"  Target page: {url}")

    if page_domain:
        lines.append(f"  Target domain: {page_domain}")

    if title:
        lines.append(f"  Page title: {title}")

    if environment:
        lines.append(f"  Environment: {environment}")

    _append_ticket_context(lines, ticket_context)
    _append_current_rules(lines, ticket_context)
    _append_blocked_resources(lines, ticket_context)

    if third_party:
        lines.append("\nThird-party domains making network requests:")
        for entry in third_party[:30]:
            domain = str(entry.get("domain", "")).strip()
            if not domain:
                continue

            count = entry.get("request_count", 0)
            paths = entry.get("sample_paths", [])
            paths = paths if isinstance(paths, list) else []
            path_str = ", ".join(str(path) for path in paths[:5]) if paths else "/"

            lines.append(
                f"  - {domain} ({count} requests, sample paths: {path_str})"
            )
    else:
        lines.append("\nThird-party domains making network requests: none detected")

    if ad_candidates:
        lines.append("\nAd-related candidates detected in page DOM/network:")
        for candidate in ad_candidates[:40]:
            confidence = candidate.get("confidence", "")
            category = candidate.get("category", "")
            suggested = candidate.get("suggested_rule", "")
            selector = candidate.get("selector", "")
            reason = candidate.get("reason", "")
            domain = candidate.get("domain", "")
            snippet = candidate.get("element_snippet", "")

            parts = []
            if confidence:
                parts.append(f"confidence={confidence}")
            if category:
                parts.append(f"category={category}")
            if domain:
                parts.append(f"domain={domain}")
            if suggested:
                parts.append(f"suggested_rule={suggested}")
            if selector:
                parts.append(f"selector={selector}")
            if reason:
                parts.append(f"reason={reason}")
            if snippet:
                parts.append(f"snippet={_truncate(str(snippet), 240)}")

            if parts:
                lines.append("  - " + " | ".join(parts))
    else:
        lines.append("\nAd-related candidates detected in page DOM/network: none detected")

    _append_generation_goal(lines, ticket_context)

    user_message = "\n".join(lines)

    logger.debug(
        "Prompt built: %s chars, %s third-party domains, %s ad candidates, problem_type=%s",
        len(user_message),
        len(third_party),
        len(ad_candidates),
        ticket_context.get("problem_type", "unknown"),
    )

    return system_message, user_message


def extract_signals(crawl_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull only the fields the LLM needs from the full crawl result dict.
    """
    compact_candidates = []

    for candidate in crawl_result.get("ad_candidates", []):
        if not isinstance(candidate, Mapping):
            continue

        compact_candidates.append(
            {
                "category": candidate.get("category", ""),
                "confidence": candidate.get("confidence", ""),
                "suggested_rule": candidate.get("suggested_rule", ""),
                "selector": candidate.get("selector", ""),
                "reason": candidate.get("reason", ""),
                "domain": candidate.get("domain", ""),
                "element_snippet": candidate.get("element_snippet", "")
                or candidate.get("outer_html_snippet", ""),
            }
        )

    return {
        "url": crawl_result.get("url", ""),
        "title": crawl_result.get("title", ""),
        "environment": crawl_result.get("environment", "desktop"),
        "ticket_context": crawl_result.get("ticket_context", {}),
        "third_party": crawl_result.get("network_requests", {}).get("third_party", []),
        "ad_candidates": compact_candidates,
    }


def _append_ticket_context(
    lines: List[str],
    ticket_context: Mapping[str, Any],
) -> None:
    if not ticket_context:
        lines.append("\nUser ticket context: none provided")
        return

    problem_type = ticket_context.get("problem_type", "unknown")
    platform = ticket_context.get("platform", "")
    request = ticket_context.get("request", "")
    description = ticket_context.get("description", "")
    actual = ticket_context.get("actual", "")
    expected = ticket_context.get("expected", "")
    steps = ticket_context.get("steps", [])
    target_to_block = ticket_context.get("target_to_block", [])
    target_to_preserve = ticket_context.get("target_to_preserve", [])

    lines.append("\nUser ticket context:")
    lines.append(f"  Problem type: {problem_type}")

    if platform:
        lines.append(f"  Platform: {platform}")

    if request:
        lines.append(f"  Request: {_truncate(str(request), 500)}")

    if description:
        lines.append(f"  Description: {_truncate(str(description), 500)}")

    if actual:
        lines.append(f"  Actual behavior: {_truncate(str(actual), 500)}")

    if expected:
        lines.append(f"  Expected behavior: {_truncate(str(expected), 500)}")

    if isinstance(steps, list) and steps:
        lines.append("  Reproduction steps:")
        for step in steps[:8]:
            lines.append(f"    - {_truncate(str(step), 240)}")

    if isinstance(target_to_preserve, list) and target_to_preserve:
        lines.append("  Must preserve:")
        for item in target_to_preserve[:10]:
            lines.append(f"    - {_truncate(str(item), 160)}")

    if isinstance(target_to_block, list) and target_to_block:
        lines.append("  Should block:")
        for item in target_to_block[:10]:
            lines.append(f"    - {_truncate(str(item), 160)}")


def _append_current_rules(
    lines: List[str],
    ticket_context: Mapping[str, Any],
) -> None:
    current_rules = ticket_context.get("current_rules", [])
    matched_rules = ticket_context.get("matched_rules", [])

    if isinstance(current_rules, str):
        current_rules = [
            line.strip()
            for line in current_rules.splitlines()
            if line.strip()
        ]

    if isinstance(matched_rules, str):
        matched_rules = [
            line.strip()
            for line in matched_rules.splitlines()
            if line.strip()
        ]

    if current_rules:
        lines.append("\nCurrent/existing Adblock rules active for this ticket:")
        for rule in list(current_rules)[:40]:
            lines.append(f"  - {rule}")

    if matched_rules:
        lines.append("\nRules suspected or observed to match the reported issue:")
        for rule in list(matched_rules)[:40]:
            lines.append(f"  - {rule}")


def _append_blocked_resources(
    lines: List[str],
    ticket_context: Mapping[str, Any],
) -> None:
    blocked_resources = ticket_context.get("blocked_resources", [])

    if not isinstance(blocked_resources, list) or not blocked_resources:
        return

    lines.append("\nBlocked resources observed when Adblock is enabled:")

    for item in blocked_resources[:40]:
        if isinstance(item, Mapping):
            url = item.get("url", "")
            resource_type = item.get("resource_type", "")
            matched_rule = item.get("matched_rule", "")
            reason = item.get("reason", "")

            parts = []
            if url:
                parts.append(f"url={_strip_query(str(url))}")
            if resource_type:
                parts.append(f"type={resource_type}")
            if matched_rule:
                parts.append(f"matched_rule={matched_rule}")
            if reason:
                parts.append(f"reason={_truncate(str(reason), 180)}")

            if parts:
                lines.append("  - " + " | ".join(parts))

        elif item:
            lines.append(f"  - url={_strip_query(str(item))}")


def _append_generation_goal(
    lines: List[str],
    ticket_context: Mapping[str, Any],
) -> None:
    problem_type = str(ticket_context.get("problem_type", "unknown")).strip().lower()

    current_rules = ticket_context.get("current_rules", [])
    blocked_resources = ticket_context.get("blocked_resources", [])

    has_current_rules = isinstance(current_rules, list) and len(current_rules) > 0
    has_blocked_resources = isinstance(blocked_resources, list) and len(blocked_resources) > 0

    lines.append("\nGeneration goal:")

    if problem_type == "content_broken_image":
        lines.append(
            "  Fix image/content breakage caused by Adblock. "
            "Prefer the narrowest network exception rule for image/CDN requests. "
            "Always format exception rules with ^ before $, for example @@||cdn.site.com/path^$image,domain=site.com. "
            "Do not generate cosmetic hiding rules for images or reading content."
        )
        if has_current_rules:
            lines.append(
                "  Current rules are provided; generate a rule patch that narrowly overrides the problematic current rule."
            )
        if has_blocked_resources:
            lines.append(
                "  Blocked image resources are provided; base the exception on the blocked resource domain/path and $image type."
            )

    elif problem_type == "content_broken_video":
        lines.append(
            "  Fix video/player breakage caused by Adblock. "
            "Prefer the narrowest network exception rule for media/player resources. "
            "Always format exception rules with ^ before $, for example @@||media.site.com/video^$media,domain=site.com. "
            "Do not block video streams, player scripts, or media controls."
        )
        if has_current_rules:
            lines.append(
                "  Current rules are provided; generate a narrow exception against the problematic current rule."
            )

    elif problem_type == "content_broken":
        lines.append(
            "  Fix normal page functionality broken by Adblock. "
            "Prefer narrow exception rules for required first-party or content resources. "
            "Always format network rules with ^ before $ when options are used."
        )

    elif problem_type == "ui_hidden":
        lines.append(
            "  Restore hidden UI such as search, menu, header, or navigation while keeping ads blocked. "
            "Prefer the narrowest cosmetic exception rule (#@#) for the affected selector."
        )

    elif problem_type == "anti_adblock_or_overlay":
        lines.append(
            "  Make the page usable with Adblock enabled. "
            "If an ad overlay blocks the page, generate a narrow cosmetic/network blocking rule for that overlay. "
            "If the close button or required flow is broken because a necessary script/resource is blocked, "
            "generate a narrow exception rule instead."
        )

    elif problem_type == "specific_ad_not_blocked":
        lines.append(
            "  Block the specific user-reported ad using the safest narrow network or cosmetic rule. "
            "Always format network rules with ^ before $ when options are used. "
            "Do not hide normal page layout or content."
        )

    else:
        lines.append(
            "  Generate the safest ABP rule patch for the ticket. "
            "If the ticket describes broken content, prefer exception rules. "
            "If it describes visible ads, prefer blocking/hiding rules. "
            "Always format network rules with ^ before $ when options are used."
        )

    lines.append("  Output only ABP rule lines.")


def _as_dict_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []

    return [
        dict(item)
        for item in value
        if isinstance(item, Mapping)
    ]


def _hostname(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _strip_query(url: str) -> str:
    try:
        parsed = urlparse(url)

        if parsed.scheme and parsed.netloc:
            return parsed._replace(query="", fragment="").geturl()

        return url.split("?", 1)[0].split("#", 1)[0]

    except Exception:
        return url.split("?", 1)[0].split("#", 1)[0]


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value

    return value[: limit - 3].rstrip() + "..."
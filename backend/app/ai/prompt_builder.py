# Assembles the prompt payload sent to the LLM:
# - load system prompt template
# - extract compact signals from crawl result
# - include user/CMS ticket context
# - include current/existing rules, matched rules, and blocked resources when available
# - include evidence priority and context sufficiency instructions
# - include problem policy / resolution strategy from app.services.problem_policy
# - strip query strings and irrelevant fields to minimise token usage
# - format signals into a structured user message
# - return (system_message, user_message) tuple for llm_client.py
#
# Input:  crawl result dict from services/crawler.py + optional prompt template string
# Output: (system_message, user_message) tuple passed to llm_client.py

import logging
from typing import Any, Dict, List, Mapping
from urllib.parse import urlparse

from app.services.problem_policy import (
    STRATEGY_ALLOW_REQUIRED_CONTENT,
    STRATEGY_BLOCK_VISIBLE_AD,
    STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE,
    STRATEGY_RESTORE_HIDDEN_UI,
    get_prompt_policy_lines,
    get_problem_policy,
    get_resolution_strategy,
    normalize_problem_type,
)

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = """\
You are an AdBlock filter rule patch generator for the Coc Coc browser.

Your job is to generate the smallest safe ABP rule patch that solves the reported issue.

You will receive:
- user/CMS ticket context,
- problem_type,
- resolution_strategy,
- evidence_level,
- current/existing Adblock rules when available,
- matched rules when available,
- blocked resources when available,
- rendered page crawl signals,
- third-party network requests,
- detected ad candidates.

Core principle:
- Generate a targeted rule patch, not a broad site-wide filter list.
- Prefer the fewest rules that directly solve the reported problem.
- Do not guess a specific root cause if the evidence is not provided.
- When evidence is weak, generate conservative candidates only.
- Preserve the user-reported expected behavior.

Evidence priority:
1. matched_rules
   - Highest priority.
   - If a rule is observed to match the broken resource or hidden element, patch that rule directly.
2. blocked_resources
   - High priority.
   - If a required image/video/script/media resource is blocked, generate a narrow exception for that resource domain/path/type.
3. current_rules / existing_rules
   - Medium priority.
   - Use them to infer what may need a narrow exception or cosmetic exception.
4. ad_candidates
   - Medium priority.
   - Use high-confidence candidates first.
   - Prefer suggested_rule only if it is domain-scoped and not too broad.
5. third_party network domains
   - Lower priority.
   - Use them only when they are clearly ad-related or supported by candidates.
6. heuristic guess
   - Lowest priority.
   - Use only conservative rules.

Allowed ABP rule formats:

Network blocking:
  ||ads.example.com^
  ||ads.example.com/path^
  ||ads.example.com/path^$script,domain=site.com
  ||ads.example.com/path^$third-party,domain=site.com

Cosmetic hiding:
  site.com##.ad-banner
  site.com###ad-content
  site.com##div[class*="ad"]

Network exception:
  @@||cdn.example.com^
  @@||cdn.example.com/images/^$image,domain=site.com
  @@||media.example.com/video/^$media,domain=site.com
  @@||site.com/script.js^$script,domain=site.com

Cosmetic exception:
  site.com#@#.search
  site.com#@#.menu
  site.com#@##header

ABP formatting rules:
- Output one ABP rule per line only.
- No markdown.
- No explanations.
- No comments.
- No numbering.
- No blank lines.
- For network rules with options, always put ^ before $, for example:
  ||ads.example.com/banner^$image,domain=site.com
  @@||cdn.example.com/image.jpg^$image,domain=site.com
- Do not output malformed rules like:
  @@||cdn.example.com/image.jpg$image,domain=site.com
  ||ads.example.com/banner$image,domain=site.com
- For cosmetic exception rules, use #@#, not ##.
- For cosmetic hiding rules, use ##, not #@#.
- Domain-scope cosmetic rules to the target domain.
- Domain-scope network rules with domain=target.com when appropriate.

Rule budget:
- Default target: 1-3 rules.
- For legacy mode with no ticket context, up to 5 rules are allowed if there are multiple independent high-confidence ad candidates.
- For ticket-aware breakage fixes, prefer 1 rule when matched_rules or blocked_resources identify the cause.
- Generate more than 3 rules only when each rule fixes a distinct confirmed target.
- Do not output duplicate or redundant rules.
- If one rule already covers another safely, output only the broader safe rule.
  Example:
    site.com##ins.adsbygoogle
    site.com##ins.adsbygoogle.adsbygoogle-noablate
  The first rule already covers the second, so output only the first rule if safe.

Strict safety limits:
- Prefer specific rules over broad rules.
- Never generate broad rules like:
  ||com^
  ||net^
  ||org^
  ||.^
  ||site.com^
  site.com##div
  site.com##img
  site.com##iframe
  ##.ad
  ##div
- Do not block first-party content images, videos, CSS, scripts, navigation, search, menu, forms, download controls, or user controls unless the ticket explicitly says that exact target is an ad.
- Do not generate exceptions that disable adblocking broadly.
- Do not generate @@||site.com^ unless there is explicit evidence that the whole site is incorrectly blocked.
- Do not generate unscoped generic cosmetic selectors.
- The crawl environment (desktop/android/ios) tells you the viewport and UA used. Mobile crawls may expose different ad slots and selectors than desktop — generate rules matching what was actually observed.
- Output one ABP rule per line only.
- No markdown, no explanations, no comments, no numbering, no blank lines.\

Resolution strategy behavior:
- block_visible_ad:
  Generate narrow network blocking rules or cosmetic hiding rules for visible ads.

- allow_required_content:
  Generate narrow exception rules to restore images, videos, scripts, media, or normal content broken by Adblock.

- restore_hidden_ui:
  Generate cosmetic exception rules (#@#) to restore hidden search, menu, header, navigation, buttons, or user controls.

- remove_overlay_or_allow_required_resource:
  Hide/block ad overlays or popups when they block the page.
  If evidence shows a required resource is blocked, generate a narrow exception instead.

- unknown_safe_patch:
  Infer the safest direction from the ticket.
  If content is broken, prefer exceptions.
  If ads are visible, prefer blocking/hiding.
  If context is insufficient, generate conservative candidates only.

Context sufficiency:
- If current_rules, matched_rules, and blocked_resources are all empty, do not claim a specific existing rule caused the issue.
- If only URL/crawl signals are available, generate rules based only on observed page signals.
- If evidence_level is legacy_no_ticket_context, behave like the old pipeline:
  crawl signals -> ad candidates -> block/hide ad-related signals.
- If evidence_level is url_only_best_effort, be conservative and avoid broad rules.
"""


def build_prompt(
    crawl_signals: Dict[str, Any],
    template: str = DEFAULT_SYSTEM_PROMPT,
) -> tuple[str, str]:
    """
    Construct the (system_message, user_message) pair to send to the LLM.
    """
    system_message = template or DEFAULT_SYSTEM_PROMPT

    url = crawl_signals.get("url", "unknown")
    title = crawl_signals.get("title", "")
    environment = crawl_signals.get("environment", "desktop")
    page_domain = _hostname(url)

    ticket_context = crawl_signals.get("ticket_context", {})
    if not isinstance(ticket_context, Mapping):
        ticket_context = {}

    third_party = _as_dict_list(crawl_signals.get("third_party", []))
    ad_candidates = _as_dict_list(crawl_signals.get("ad_candidates", []))
    ad_candidates = _sort_candidates(ad_candidates)

    lines: List[str] = []

    lines.append("Page context:")
    lines.append(f"  Target page: {url}")

    if page_domain:
        lines.append(f"  Target domain: {page_domain}")

    if title:
        lines.append(f"  Page title: {_truncate(str(title), 240)}")

    if environment:
        lines.append(f"  Environment: {environment}")

    _append_ticket_context(lines, ticket_context)
    _append_evidence_summary(lines, ticket_context, third_party, ad_candidates)
    _append_current_rules(lines, ticket_context)
    _append_blocked_resources(lines, ticket_context)
    _append_third_party(lines, third_party)
    _append_ad_candidates(lines, ad_candidates)
    _append_generation_goal(lines, ticket_context, page_domain)

    user_message = "\n".join(lines)

    logger.debug(
        "Prompt built: %s chars, %s third-party domains, %s ad candidates, problem_type=%s, strategy=%s, evidence_level=%s",
        len(user_message),
        len(third_party),
        len(ad_candidates),
        ticket_context.get("problem_type", "unknown"),
        ticket_context.get("resolution_strategy", "unknown"),
        ticket_context.get("evidence_level", "unknown"),
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

    network_requests = crawl_result.get("network_requests", {})
    if not isinstance(network_requests, Mapping):
        network_requests = {}

    return {
        "url": crawl_result.get("url", ""),
        "title": crawl_result.get("title", ""),
        "environment": crawl_result.get("environment", "desktop"),
        "ticket_context": crawl_result.get("ticket_context", {}),
        "third_party": network_requests.get("third_party", []),
        "ad_candidates": compact_candidates,
    }


def _append_ticket_context(
    lines: List[str],
    ticket_context: Mapping[str, Any],
) -> None:
    if not ticket_context:
        lines.append("\nUser ticket context: none provided")
        return

    raw_problem_type = ticket_context.get("problem_type", "unknown")
    problem_type = normalize_problem_type(raw_problem_type, fallback="unknown")

    resolution_strategy = ticket_context.get("resolution_strategy", "")
    if not resolution_strategy:
        resolution_strategy = get_resolution_strategy(problem_type)

    evidence_level = ticket_context.get("evidence_level", "")
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
    lines.append(f"  Resolution strategy: {resolution_strategy}")

    if evidence_level:
        lines.append(f"  Evidence level: {evidence_level}")

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
        for item in target_to_preserve[:12]:
            lines.append(f"    - {_truncate(str(item), 160)}")

    if isinstance(target_to_block, list) and target_to_block:
        lines.append("  Should block:")
        for item in target_to_block[:12]:
            lines.append(f"    - {_truncate(str(item), 160)}")


def _append_evidence_summary(
    lines: List[str],
    ticket_context: Mapping[str, Any],
    third_party: List[Dict[str, Any]],
    ad_candidates: List[Dict[str, Any]],
) -> None:
    current_rules = _as_rule_list(ticket_context.get("current_rules", []))
    matched_rules = _as_rule_list(ticket_context.get("matched_rules", []))
    blocked_resources = ticket_context.get("blocked_resources", [])
    evidence_level = str(ticket_context.get("evidence_level", "")).strip()

    has_blocked_resources = (
        isinstance(blocked_resources, list) and len(blocked_resources) > 0
    )

    blocked_resources_count = (
        len(blocked_resources)
        if isinstance(blocked_resources, list)
        else 0
    )

    lines.append("\nEvidence summary:")
    lines.append(f"  Evidence level: {evidence_level or 'unknown'}")
    lines.append(f"  Matched rules count: {len(matched_rules)}")
    lines.append(f"  Current/existing rules count: {len(current_rules)}")
    lines.append(f"  Blocked resources count: {blocked_resources_count}")
    lines.append(f"  Third-party domains count: {len(third_party)}")
    lines.append(f"  Ad candidates count: {len(ad_candidates)}")

    lines.append("  Evidence priority to use:")
    lines.append("    1. matched_rules")
    lines.append("    2. blocked_resources")
    lines.append("    3. current_rules")
    lines.append("    4. high-confidence ad_candidates")
    lines.append("    5. clearly ad-related third-party domains")
    lines.append("    6. conservative heuristic guess")

    if not matched_rules and not has_blocked_resources and not current_rules:
        lines.append(
            "  Context sufficiency: no matched_rules, blocked_resources, or current_rules were provided. "
            "Do not assume a specific existing rule caused the issue."
        )

    if evidence_level == "legacy_no_ticket_context":
        lines.append(
            "  Legacy fallback: no ticket context was provided. Behave like the old pipeline and block/hide observed ad-related signals."
        )

    elif evidence_level == "url_only_best_effort":
        lines.append(
            "  URL-only best effort: ticket context exists but no direct rule/resource evidence is available. Generate conservative candidates only."
        )


def _append_current_rules(
    lines: List[str],
    ticket_context: Mapping[str, Any],
) -> None:
    current_rules = _as_rule_list(ticket_context.get("current_rules", []))
    matched_rules = _as_rule_list(ticket_context.get("matched_rules", []))

    if matched_rules:
        lines.append("\nRules suspected or observed to match the reported issue:")
        for rule in matched_rules[:40]:
            lines.append(f"  - {rule}")

    if current_rules:
        lines.append("\nCurrent/existing Adblock rules active for this ticket:")
        for rule in current_rules[:40]:
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


def _append_third_party(
    lines: List[str],
    third_party: List[Dict[str, Any]],
) -> None:
    if not third_party:
        lines.append("\nThird-party domains making network requests: none detected")
        return

    lines.append("\nThird-party domains making network requests:")
    lines.append(
        "  Use this as supporting evidence only. Do not block a third-party domain unless it is clearly ad-related or supported by candidates."
    )

    for entry in third_party[:30]:
        domain = str(entry.get("domain", "")).strip()
        if not domain:
            continue

        count = entry.get("request_count", 0)
        paths = entry.get("sample_paths", [])
        paths = paths if isinstance(paths, list) else []
        path_str = ", ".join(str(path) for path in paths[:5]) if paths else "/"

        lines.append(
            f"  - {domain} ({count} requests, sample paths: {_truncate(path_str, 500)})"
        )


def _append_ad_candidates(
    lines: List[str],
    ad_candidates: List[Dict[str, Any]],
) -> None:
    if not ad_candidates:
        lines.append("\nAd-related candidates detected in page DOM/network: none detected")
        return

    lines.append("\nAd-related candidates detected in page DOM/network:")
    lines.append(
        "  Prefer high-confidence candidates. Prefer suggested_rule only if it is domain-scoped and not broad."
    )

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
            parts.append(f"reason={_truncate(str(reason), 220)}")

        if snippet:
            parts.append(f"snippet={_truncate(str(snippet), 240)}")

        if parts:
            lines.append("  - " + " | ".join(parts))


def _append_generation_goal(
    lines: List[str],
    ticket_context: Mapping[str, Any],
    page_domain: str,
) -> None:
    raw_problem_type = ticket_context.get("problem_type", "unknown")
    problem_type = normalize_problem_type(raw_problem_type, fallback="unknown")
    policy = get_problem_policy(problem_type)

    evidence_level = str(ticket_context.get("evidence_level", "")).strip()

    current_rules = _as_rule_list(ticket_context.get("current_rules", []))
    matched_rules = _as_rule_list(ticket_context.get("matched_rules", []))
    blocked_resources = ticket_context.get("blocked_resources", [])

    has_current_rules = len(current_rules) > 0
    has_matched_rules = len(matched_rules) > 0
    has_blocked_resources = (
        isinstance(blocked_resources, list) and len(blocked_resources) > 0
    )

    lines.append("\nGeneration goal:")
    _append_rule_budget(lines, problem_type, policy.strategy, evidence_level)

    policy_lines = get_prompt_policy_lines(
        problem_type,
        evidence_level=evidence_level,
        page_domain=page_domain,
        has_current_rules=has_current_rules,
        has_matched_rules=has_matched_rules,
        has_blocked_resources=has_blocked_resources,
    )

    lines.extend(policy_lines)

    _append_strategy_examples(lines, policy.strategy, problem_type)

    lines.append("  Final output requirement: output only ABP rule lines.")


def _append_rule_budget(
    lines: List[str],
    problem_type: str,
    strategy: str,
    evidence_level: str,
) -> None:
    if (
        strategy == STRATEGY_BLOCK_VISIBLE_AD
        and evidence_level == "legacy_no_ticket_context"
    ):
        lines.append(
            "  Rule budget: generate 1-5 rules. Use more than 3 only for distinct high-confidence ad targets."
        )
        return

    if strategy in {
        STRATEGY_ALLOW_REQUIRED_CONTENT,
        STRATEGY_RESTORE_HIDDEN_UI,
    }:
        lines.append(
            "  Rule budget: prefer 1 rule. Generate 2-3 only if there are multiple distinct affected targets."
        )
        return

    if strategy == STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE:
        lines.append(
            "  Rule budget: prefer 1-3 rules. Generate up to 5 only if multiple independent overlay/ad targets are confirmed."
        )
        return

    lines.append("  Rule budget: prefer 1-3 rules.")


def _append_strategy_examples(
    lines: List[str],
    strategy: str,
    problem_type: str,
) -> None:
    lines.append("  Strategy-specific syntax examples:")

    if strategy == STRATEGY_BLOCK_VISIBLE_AD:
        lines.append("    - Cosmetic hide: site.com##.ad-banner")
        lines.append("    - Cosmetic hide by id: site.com###ad-content")
        lines.append("    - Network block: ||ads.example.com^$third-party,domain=site.com")
        lines.append("    - Avoid exceptions such as @@ or #@# for visible-ad tickets.")
        return

    if strategy == STRATEGY_ALLOW_REQUIRED_CONTENT:
        if problem_type == "content_broken_image":
            lines.append("    - Image exception: @@||cdn.example.com/path^$image,domain=site.com")
            lines.append("    - Do not hide or block image/CDN resources unless explicitly marked as ads.")
        elif problem_type == "content_broken_video":
            lines.append("    - Media exception: @@||media.example.com/video^$media,domain=site.com")
            lines.append("    - Script/player exception: @@||player.example.com/script.js^$script,domain=site.com")
            lines.append("    - Do not block video streams, player scripts, iframes, or media controls.")
        else:
            lines.append("    - Network exception: @@||cdn.example.com/resource^$script,domain=site.com")
            lines.append("    - Cosmetic exception: site.com#@#.required-ui")
        return

    if strategy == STRATEGY_RESTORE_HIDDEN_UI:
        lines.append("    - Search exception: site.com#@#.search")
        lines.append("    - Menu exception: site.com#@#.menu")
        lines.append("    - Header id exception: site.com#@##header")
        lines.append("    - Do not generate ## hiding rules for search/menu/header/navigation/user controls.")
        return

    if strategy == STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE:
        lines.append("    - Overlay hide by id: site.com###ad-content")
        lines.append("    - Overlay hide by id: site.com###ad-area-1")
        lines.append("    - Sticky ad hide: site.com##.sticky_ads")
        lines.append("    - Required script exception if directly evidenced: @@||site.com/script.js^$script,domain=site.com")
        lines.append("    - Avoid broad selectors such as site.com##div, site.com##iframe, site.com##.modal.")
        return

    lines.append("    - Visible ad: site.com##.ad-banner")
    lines.append("    - Broken content: @@||cdn.example.com/resource^$script,domain=site.com")
    lines.append("    - Hidden UI: site.com#@#.search")


def _as_dict_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []

    return [
        dict(item)
        for item in value
        if isinstance(item, Mapping)
    ]


def _as_rule_list(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, str):
        return [
            line.strip()
            for line in value.splitlines()
            if line.strip()
        ]

    if isinstance(value, list):
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]

    return []


def _sort_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda item: (
            _confidence_rank(item.get("confidence", "")),
            0 if item.get("suggested_rule") else 1,
            0 if item.get("selector") else 1,
        ),
    )


def _confidence_rank(value: Any) -> int:
    text = str(value).strip().lower()

    if text in {"high", "very_high", "very-high", "strong"}:
        return 0

    if text in {"medium", "med", "moderate"}:
        return 1

    if text in {"low", "weak"}:
        return 2

    return 3


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
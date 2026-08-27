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
import re
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

Your job is to generate a safe ABP rule patch that solves the reported issue.

Important:
- Prefer a small targeted patch, but do not omit necessary independent rules.
- Correctness is more important than producing the fewest possible rules.
- If the page has multiple distinct confirmed ad targets, output one safe rule for each target.
- A popup overlay, an ad modal, an ad container, and an ad-image network path are distinct targets.
- Do not drop a high-confidence network ad rule just because a cosmetic overlay rule is also present.
- Do not drop a popup overlay/backdrop rule just because the ad image/container rule is also present.

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
- Prefer the fewest rules that fully solve the issue, not the fewest rules at all cost.
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
   - Medium/high priority.
   - Use high-confidence candidates first.
   - Treat high-confidence candidates with different categories or different suggested_rule targets as distinct confirmed targets.
   - Prefer suggested_rule only if it is domain-scoped and not too broad.
5. third_party network domains
   - Lower priority.
   - Use them only when they are clearly ad-related or supported by candidates.
6. heuristic guess
   - Lowest priority.
   - Use only conservative rules.

High-confidence candidate coverage:
- For block_visible_ad and legacy visible-ad mode, include all necessary high-confidence distinct ad candidates.
- Distinct candidate categories often need separate rules:
  - popup_overlay: hides modal/backdrop that blocks page interaction.
  - ad_container: hides DOM ad slots or parent ad wrappers.
  - floating_ad: hides sticky/floating ad elements.
  - ad_network_request: blocks ad images/scripts/iframes loaded from ad-specific paths.
- If a parent ad_container safely covers a child ad_container, output only the parent.
- If a network rule blocks ad image assets and a cosmetic rule hides the container, both can be necessary.
- If a modal/backdrop remains after blocking the ad image, include the overlay/backdrop rule too.

Allowed ABP rule formats:

Network blocking:
  ||ads.example.com^
  ||ads.example.com/path^
  ||ads.example.com/path^$script,domain=site.com
  ||ads.example.com/path^$image,domain=site.com
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
- Default target: 1-6 rules.
- For legacy mode with no ticket context, up to 8 rules are allowed if there are multiple independent high-confidence ad candidates.
- For visible-ad fixes, generate more than 3 rules when each rule fixes a distinct confirmed target.
- For overlay/popup cases, include both overlay/backdrop rules and ad content/container/network rules when needed.
- For ticket-aware breakage fixes, prefer 1 rule when matched_rules or blocked_resources identify the cause.
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
- No markdown, no explanations, no comments, no numbering, no blank lines.

Container targeting:
- Each ad candidate may include a parent_chain showing its nearest DOM ancestors, closest first.
- If a parent has a clearly ad-specific id or class, prefer targeting that container over the leaf element because it usually produces a safer, more complete block.
- Example: leaf=ins.adsbygoogle inside parent div#ad-sidebar → prefer site.com###ad-sidebar over site.com##ins.adsbygoogle.
- Do not blindly pick the outermost ancestor — pick the nearest one that is clearly ad-specific.

Resolution strategy behavior:
- block_visible_ad:
  Generate narrow network blocking rules or cosmetic hiding rules for visible ads.
  Include distinct confirmed overlay, container, floating, and network ad targets when needed.

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


CATEGORY_PRIORITY = {
    "popup_overlay": 0,
    "ad_network_request": 1,
    "ad_container": 2,
    "floating_ad": 3,
    "ad_iframe": 4,
    "tracking_script": 5,
}


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
    _append_region_context(lines, ticket_context)
    _append_validation_hints(lines, ticket_context)
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
                "parent_chain": candidate.get("parent_chain", []),
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
    focus_region = ticket_context.get("focus_region", "")
    request = ticket_context.get("request", "")
    description = ticket_context.get("description", "")
    actual = ticket_context.get("actual", "")
    expected = ticket_context.get("expected", "")
    steps = ticket_context.get("steps", [])
    target_to_block = ticket_context.get("target_to_block", [])
    target_to_block_explicit = bool(ticket_context.get("target_to_block_explicit"))
    target_to_preserve = ticket_context.get("target_to_preserve", [])
    notes = ticket_context.get("notes", "")

    lines.append("\nUser ticket context:")
    lines.append(f"  Problem type: {problem_type}")
    lines.append(f"  Resolution strategy: {resolution_strategy}")

    if evidence_level:
        lines.append(f"  Evidence level: {evidence_level}")

    if platform:
        lines.append(f"  Platform: {platform}")

    if focus_region:
        lines.append(f"  Focus region: {_truncate(str(focus_region), 160)}")
        lines.append(
            "    The crawl was scoped to this region. The page signals, DOM ad "
            "candidates, and overlays below come from that region only — target "
            "rules there and do not generate rules for other parts of the page."
        )

    if request:
        lines.append(f"  Request: {_truncate(str(request), 500)}")

    if description:
        lines.append(f"  Description: {_truncate(str(description), 500)}")

    if actual:
        lines.append(f"  Actual behavior: {_truncate(str(actual), 500)}")

    if expected:
        lines.append(f"  Expected behavior: {_truncate(str(expected), 500)}")

    # Free text the reporter wrote. It was stored and carried through the
    # pipeline but never reached the model, so the box labelled "Notes for the
    # pipeline" influenced nothing at all.
    if notes:
        lines.append(f"  Reporter notes: {_truncate(str(notes), 500)}")

    if isinstance(steps, list) and steps:
        lines.append("  Reproduction steps:")
        for step in steps[:8]:
            lines.append(f"    - {_truncate(str(step), 240)}")

    if isinstance(target_to_preserve, list) and target_to_preserve:
        lines.append("  Must preserve:")
        for item in target_to_preserve[:12]:
            lines.append(f"    - {_truncate(str(item), 160)}")

    if isinstance(target_to_block, list) and target_to_block:
        # An explicit list came from the reporter, who was asked to name the
        # ads to block and nothing else — so say so, or the model treats it as
        # one more hint and blocks whatever else it finds.
        lines.append(
            "  Block only these (named by the reporter):"
            if target_to_block_explicit
            else "  Should block:"
        )
        for item in target_to_block[:12]:
            lines.append(f"    - {_truncate(str(item), 160)}")
        if target_to_block_explicit:
            lines.append(
                "    Do not generate rules for other ads on the page, even if "
                "the signals below show them."
            )


def _append_region_context(
    lines: List[str],
    ticket_context: Mapping[str, Any],
) -> None:
    """
    Add teammate-owned region/focus context to the LLM prompt.

    Important distinction:
    - focus_region scopes the crawl itself, so extracted DOM candidates are from
      that region only.
    - preserve_regions / allowed_ad_region are constraints. They do not scope the
      crawl; they tell the generator which page regions must not be damaged.
    """
    region_focus = ticket_context.get("region_focus")
    focus_selectors = ticket_context.get("focus_selectors")
    preserve_regions = ticket_context.get("preserve_regions")
    target_regions = ticket_context.get("target_regions")

    hints = ticket_context.get("validation_hints", {})
    if not isinstance(hints, Mapping):
        hints = {}

    allowed_ad_region = hints.get("allowed_ad_region") or hints.get("allowed_regions")
    block_outside_allowed_region = hints.get("block_ads_outside_allowed_region")

    has_region_context = any(
        value not in (None, "", [], {})
        for value in (
            region_focus,
            focus_selectors,
            preserve_regions,
            target_regions,
            allowed_ad_region,
            block_outside_allowed_region,
        )
    )

    if not has_region_context:
        return

    lines.append("\nRegion focus / region constraints:")
    lines.append(
        "  Use these as region-level constraints. Do not confuse a preserved/allowed "
        "region with a target region to hide."
    )

    if region_focus not in (None, "", [], {}):
        lines.append(
            "  region_focus: "
            f"{_truncate(_format_region_value(region_focus), 700)}"
        )

    if focus_selectors not in (None, "", [], {}):
        lines.append(
            "  focus_selectors: "
            f"{_truncate(_format_region_value(focus_selectors), 700)}"
        )

    if preserve_regions not in (None, "", [], {}):
        lines.append(
            "  preserve_regions: "
            f"{_truncate(_format_region_value(preserve_regions), 900)}"
        )
        lines.append(
            "    Treat preserve_regions as hard safety boundaries: do not output "
            "cosmetic rules that hide these regions or broad ancestors that may contain them."
        )

    if target_regions not in (None, "", [], {}):
        lines.append(
            "  target_regions: "
            f"{_truncate(_format_region_value(target_regions), 900)}"
        )
        lines.append(
            "    Prioritize ad candidates in target_regions when they are supported by "
            "specific selector or URL evidence."
        )

    if allowed_ad_region not in (None, "", [], {}):
        lines.append(
            "  allowed_ad_region: "
            f"{_truncate(_format_region_value(allowed_ad_region), 900)}"
        )
        lines.append(
            "    Ads or sponsor creatives inside allowed_ad_region may remain. "
            "Block ad placements outside that region only when a rule is region-safe."
        )

    if block_outside_allowed_region not in (None, "", [], {}):
        lines.append(
            f"  block_ads_outside_allowed_region: {block_outside_allowed_region}"
        )


def _append_validation_hints(
    lines: List[str],
    ticket_context: Mapping[str, Any],
) -> None:
    hints = ticket_context.get("validation_hints", {})
    if not isinstance(hints, Mapping) or not hints:
        return

    lines.append("\nTicket validation hints:")
    lines.append(
        "  These are hard constraints for generation and sandbox validation when applicable."
    )

    must_not_generate = _as_rule_list(
        hints.get("must_not_generate_rules", [])
        or hints.get("forbidden_rules", [])
        or hints.get("disallowed_rules", [])
    )

    if must_not_generate:
        lines.append("  Must NOT generate these rules or equivalent broad variants:")
        for rule in must_not_generate[:30]:
            lines.append(f"    - {rule}")

    must_preserve_text = _as_text_list(
        hints.get("must_preserve_text", [])
        or hints.get("preserve_text", [])
        or hints.get("must_contain_text", [])
    )

    if must_preserve_text:
        lines.append("  Must preserve visible text:")
        for text in must_preserve_text[:20]:
            lines.append(f"    - {_truncate(text, 160)}")

    region_hint_keys = (
        "allowed_ad_region",
        "allowed_regions",
        "blocked_regions",
        "preserve_regions",
        "target_regions",
        "block_ads_outside_allowed_region",
    )

    wrote_region_hint = False
    for key in region_hint_keys:
        value = hints.get(key)
        if value in (None, "", [], {}):
            continue

        if not wrote_region_hint:
            lines.append("  Region assertions:")
            wrote_region_hint = True

        lines.append(f"    - {key}: {_truncate(_format_region_value(value), 700)}")

    selector_hint_keys = (
        "must_show_all_selectors",
        "must_exist_selectors",
        "must_hide_selectors",
        "must_show_any_selector_groups",
    )

    wrote_selector_hint = False
    for key in selector_hint_keys:
        value = hints.get(key)
        if value in (None, "", [], {}):
            continue

        if not wrote_selector_hint:
            lines.append("  Selector assertions:")
            wrote_selector_hint = True

        lines.append(f"    - {key}: {_truncate(_format_hint_value(value), 500)}")

    numeric_hint_keys = (
        "min_visible_images",
        "max_broken_images",
        "min_visible_videos",
    )

    for key in numeric_hint_keys:
        if key in hints:
            lines.append(f"  {key}: {hints.get(key)}")


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

    high_conf_count = sum(
        1
        for candidate in ad_candidates
        if str(candidate.get("confidence", "")).strip().lower() == "high"
    )

    lines.append("\nEvidence summary:")
    lines.append(f"  Evidence level: {evidence_level or 'unknown'}")
    lines.append(f"  Matched rules count: {len(matched_rules)}")
    lines.append(f"  Current/existing rules count: {len(current_rules)}")
    lines.append(f"  Blocked resources count: {blocked_resources_count}")
    lines.append(f"  Third-party domains count: {len(third_party)}")
    lines.append(f"  Ad candidates count: {len(ad_candidates)}")
    lines.append(f"  High-confidence ad candidates count: {high_conf_count}")

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
    lines.append(
        "  Coverage instruction: include every distinct high-confidence suggested_rule when it targets a different confirmed ad/overlay/network target."
    )
    lines.append(
        "  Distinct examples: popup_overlay + ad_container + ad_network_request may all be needed together."
    )

    for candidate in ad_candidates[:60]:
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
            parts.append(f"reason={_truncate(str(reason), 260)}")

        if snippet:
            parts.append(f"snippet={_truncate(str(snippet), 280)}")

        parent_chain = candidate.get("parent_chain", [])
        if isinstance(parent_chain, list) and parent_chain:
            ancestry_parts = []
            for parent in parent_chain:
                if not isinstance(parent, Mapping):
                    continue

                tag = parent.get("tag", "div")
                parent_id = parent.get("id", "")
                classes = parent.get("classes", [])

                if isinstance(classes, list):
                    class_part = "." + ".".join(str(item) for item in classes[:3]) if classes else ""
                elif isinstance(classes, str):
                    class_items = [item for item in classes.split() if item]
                    class_part = "." + ".".join(class_items[:3]) if class_items else ""
                else:
                    class_part = ""

                ancestry_parts.append(
                    f"{tag}"
                    + (f"#{parent_id}" if parent_id else "")
                    + class_part
                )

            if ancestry_parts:
                parts.append(
                    f"parent_chain={_truncate(' > '.join(ancestry_parts), 360)}"
                )

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

    lines.append(
        "  Completeness requirement: do not omit a safe high-confidence suggested_rule if it blocks a distinct confirmed target."
    )
    lines.append(
        "  Final output requirement: output only ABP rule lines."
    )


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
            "  Rule budget: generate 1-8 rules. Use more than 3 when each rule covers a distinct high-confidence ad/overlay/network target."
        )
        return

    if strategy == STRATEGY_BLOCK_VISIBLE_AD:
        lines.append(
            "  Rule budget: generate 1-6 rules. Use enough rules to cover all distinct confirmed visible ad targets."
        )
        return

    if strategy in {
        STRATEGY_ALLOW_REQUIRED_CONTENT,
        STRATEGY_RESTORE_HIDDEN_UI,
    }:
        lines.append(
            "  Rule budget: prefer 1 rule. Generate 2-4 only if there are multiple distinct affected targets."
        )
        return

    if strategy == STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE:
        lines.append(
            "  Rule budget: generate 1-8 rules. Include overlay/backdrop, popup content, and ad network/container rules when each target is confirmed."
        )
        return

    lines.append("  Rule budget: prefer 1-6 rules.")


def _append_strategy_examples(
    lines: List[str],
    strategy: str,
    problem_type: str,
) -> None:
    lines.append("  Strategy-specific syntax examples:")

    if strategy == STRATEGY_BLOCK_VISIBLE_AD:
        lines.append("    - Popup backdrop hide: site.com##.modal-backdrop")
        lines.append("    - Popup modal hide: site.com##.ad-modal")
        lines.append("    - Parent ad container hide: site.com##.adserver")
        lines.append("    - Cosmetic hide: site.com##.ad-banner")
        lines.append("    - Cosmetic hide by id: site.com###ad-content")
        lines.append("    - Network image block: ||cdn.example.com/ads/^$image,domain=site.com")
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
        lines.append("    - Overlay hide: site.com##.modal-backdrop")
        lines.append("    - Popup modal hide: site.com##.ad-modal")
        lines.append("    - Overlay hide by id: site.com###ad-content")
        lines.append("    - Sticky ad hide: site.com##.sticky_ads")
        lines.append("    - Network image block: ||cdn.example.com/ads/^$image,domain=site.com")
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

    if isinstance(value, Mapping):
        rule = (
            value.get("rule")
            or value.get("matched_rule")
            or value.get("filter")
            or value.get("text")
            or ""
        )
        if not str(rule).strip():
            return []

        details = []
        for key in ("problem", "reason", "action", "selector", "resource_type", "url"):
            detail = str(value.get(key, "") or "").strip()
            if detail:
                details.append(f"{key}={_truncate(detail, 140)}")

        if details:
            return [f"{str(rule).strip()} ({'; '.join(details)})"]

        return [str(rule).strip()]

    if isinstance(value, list):
        result: List[str] = []
        for item in value:
            result.extend(_as_rule_list(item))
        return result

    return []


def _as_text_list(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, str):
        return [
            part.strip()
            for part in re.split(r"[,;\n]+", value)
            if part.strip()
        ]

    if isinstance(value, list):
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]

    return [str(value).strip()] if str(value).strip() else []


def _format_region_value(value: Any) -> str:
    if isinstance(value, Mapping):
        parts = []
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            parts.append(f"{key}={_format_region_value(item)}")
        return "{" + "; ".join(parts) + "}"

    if isinstance(value, list):
        return "[" + "; ".join(_format_region_value(item) for item in value[:10]) + "]"

    return str(value)


def _format_hint_value(value: Any) -> str:
    if isinstance(value, list):
        parts = []
        for item in value[:8]:
            if isinstance(item, Mapping):
                name = item.get("name", "")
                selectors = item.get("selectors", [])
                min_required = item.get("min", 1)
                if name or selectors:
                    parts.append(
                        f"name={name or 'group'} selectors={selectors} min={min_required}"
                    )
            else:
                parts.append(str(item))
        return "; ".join(parts)

    return str(value)


def _sort_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda item: (
            _confidence_rank(item.get("confidence", "")),
            _category_rank(item.get("category", "")),
            0 if item.get("suggested_rule") else 1,
            0 if _looks_like_narrow_network_rule(item.get("suggested_rule", "")) else 1,
            0 if item.get("selector") else 1,
        ),
    )


def _category_rank(value: Any) -> int:
    return CATEGORY_PRIORITY.get(str(value).strip().lower(), 99)


def _confidence_rank(value: Any) -> int:
    text = str(value).strip().lower()

    if text in {"high", "very_high", "very-high", "strong"}:
        return 0

    if text in {"medium", "med", "moderate"}:
        return 1

    if text in {"low", "weak"}:
        return 2

    return 3


def _looks_like_narrow_network_rule(rule: Any) -> bool:
    text = str(rule or "").lower()

    return (
        text.startswith("||")
        and "/" in text[2:]
        and "$" in text
        and "domain=" in text
    )


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
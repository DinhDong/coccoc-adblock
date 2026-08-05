import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

from ..ai.llm_client import call_llm_with_fallback
from ..ai.prompt_builder import build_prompt
from ..ai.rule_parser import ParsedRule, parse_llm_response

try:
    from .ticket_context import normalize_ticket_context
except Exception:
    normalize_ticket_context = None  # type: ignore


logger = logging.getLogger(__name__)


VISIBLE_AD_STRATEGIES = {
    "block_visible_ad",
    "remove_overlay_or_allow_required_resource",
}

TRACKING_ONLY_DOMAINS = {
    "google-analytics.com",
    "www.google-analytics.com",
    "googletagmanager.com",
    "www.googletagmanager.com",
    "static.cloudflareinsights.com",
}

SAFE_DOMAINS = {
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "googleapis.com",
    "gstatic.com",
    "cloudflare.com",
    "cdnjs.cloudflare.com",
    "jsdelivr.net",
    "unpkg.com",
}

AUTO_APPEND_CATEGORY_RANK = {
    "popup_overlay": 0,
    "floating_ad": 1,
    "ad_container": 2,
    "ad_network_request": 3,
    "ad_iframe": 4,
}

AUTO_APPEND_MAX_RULES = 12

BROAD_COSMETIC_SELECTORS = {
    "html",
    "body",
    "div",
    "span",
    "section",
    "main",
    "article",
    "header",
    "footer",
    "nav",
    "img",
    "iframe",
    "video",
    "button",
    "a",
    "form",
    "input",
}

AD_HINT_WORDS = (
    "ad",
    "ads",
    "adserver",
    "ad-server",
    "adslot",
    "ad-slot",
    "adunit",
    "ad-unit",
    "adsbygoogle",
    "gpt-ad",
    "banner",
    "popup",
    "modal",
    "overlay",
    "backdrop",
    "interstitial",
    "sponsor",
    "sponsored",
    "promo",
    "advert",
    "advertise",
    "advertisement",
    "sticky_ads",
    "floating_ad",
)

AD_HINT_PATHS = (
    "/ads/",
    "/ad/",
    "/storage/ads/",
    "/banner/",
    "/banners/",
    "/popup/",
    "/sponsor/",
    "/promo/",
)

POPUP_OVERLAY_HINTS = (
    "popup",
    "pop-up",
    "modal",
    "overlay",
    "backdrop",
    "interstitial",
    "dialog",
    "monetization-dialog",
    "fc-dialog",
)

FLOATING_HINTS = (
    "floating",
    "float",
    "sticky",
    "fixed",
    "catfish",
)

IFRAME_HINTS = (
    "iframe",
    "frame",
    "subdocument",
)

OVERLAY_RELEVANT_HINTS = (
    "popup",
    "pop-up",
    "modal",
    "overlay",
    "backdrop",
    "interstitial",
    "dialog",
    "monetization",
    "rewarded",
    "close",
    "sticky",
    "floating",
    "float",
    "fixed",
    "catfish",
    "ad-content",
)

GENERIC_PAGE_AD_HINTS = (
    "adsbygoogle",
    "google-auto-placed",
    "ad-area",
    "ad-slot",
    "ad_client",
    "ad-client",
    "ad-format",
    "ad-status",
)


@dataclass
class RuleGenerationResult:
    """
    Result returned by the AI rule generation stage.

    rules:
        Parsed rules used by validation/workflow.

    token_usage:
        LLM token usage metadata used in generated *_rules.json output.

    error:
        Set when generation aborted on an exception. Distinguishes a hard
        failure (missing API key, network error, bad response) from a
        legitimate empty result — both of which produce rules == [].
    """

    rules: List[ParsedRule]
    token_usage: Optional[Dict[str, Any]] = None
    model: str = ""
    fallback_used: bool = False
    prompt_preview: str = ""
    error: str = ""

    def rule_strings(self) -> List[str]:
        return [rule.rule for rule in self.rules]


def generate_rules(
    crawl_result: Dict[str, Any],
    prompt_template: str = "",
) -> List[ParsedRule]:
    """
    Backward-compatible wrapper.

    Existing code can keep calling generate_rules() and receive only parsed rules.
    Workflow should call generate_rules_with_metadata() when it needs token_usage.
    """
    return generate_rules_with_metadata(
        crawl_result=crawl_result,
        prompt_template=prompt_template,
    ).rules


def generate_rules_with_metadata(
    crawl_result: Dict[str, Any],
    prompt_template: str = "",
) -> RuleGenerationResult:
    """
    Full AI rule generation flow for one crawl result.

    Steps:
        1. Build compact crawl signals from crawl_result.
        2. Include normalized ticket_context in compact signals.
        3. Assemble prompt via prompt_builder.build_prompt().
        4. Call LLM via llm_client.call_llm_with_fallback().
        5. Capture token usage if the LLM client returns it.
        6. Parse raw response into ParsedRule objects.
        7. Hard-filter LLM rules against ticket constraints/problem strategy.
        8. Auto-append safe high-confidence detector suggested rules that the LLM omitted.
        9. Return rules + token metadata.
    """
    try:
        logger.info(
            "Starting AI rule generation for URL: %s",
            crawl_result.get("url"),
        )

        compact_signals = _extract_signals(crawl_result)

        system_message, user_message = build_prompt(
            compact_signals,
            prompt_template,
        )

        logger.info(
            "Rule generation prompt prepared | url=%s | problem_type=%s | prompt_chars=%s",
            compact_signals.get("url", ""),
            compact_signals.get("ticket_context", {}).get("problem_type", "unknown"),
            len(user_message),
        )

        llm_response = call_llm_with_fallback(
            user_message,
            system_message=system_message,
        )

        response_text = _extract_llm_text(llm_response)

        if not response_text:
            logger.warning("LLM client returned an empty response string.")
            parsed_rules: List[ParsedRule] = []
        else:
            parsed_rules = parse_llm_response(response_text)

        parsed_rules = _filter_ticket_constrained_parsed_rules(
            rules=parsed_rules,
            ticket_context=compact_signals.get("ticket_context", {}),
            compact_signals=compact_signals,
        )

        token_usage = _build_token_usage_payload(llm_response)

        if token_usage:
            logger.info(
                "Rule generation token usage | model=%s | fallback_used=%s | prompt=%s | completion=%s | total=%s",
                token_usage.get("model", ""),
                token_usage.get("fallback_used", False),
                token_usage.get("prompt_tokens", ""),
                token_usage.get("completion_tokens", ""),
                token_usage.get("total_tokens", ""),
            )
        else:
            logger.info("Rule generation token usage unavailable from LLM client.")

        completed_rules = _merge_high_confidence_detector_rules(
            parsed_rules=parsed_rules,
            compact_signals=compact_signals,
        )

        logger.info(
            "Successfully generated %s candidate rules via AI orchestration (%s from LLM after filtering, %s after detector backfill).",
            len(completed_rules),
            len(parsed_rules),
            len(completed_rules),
        )

        return RuleGenerationResult(
            rules=completed_rules,
            token_usage=token_usage,
            model=getattr(llm_response, "model", ""),
            fallback_used=bool(getattr(llm_response, "fallback_used", False)),
            prompt_preview=_preview_prompt(user_message),
        )

    except Exception as exc:
        logger.error(
            "Failed to orchestrate AI rule generation pipeline: %s",
            str(exc),
            exc_info=True,
        )
        return RuleGenerationResult(
            rules=[],
            error=f"{type(exc).__name__}: {exc}",
        )


def _merge_high_confidence_detector_rules(
    parsed_rules: List[ParsedRule],
    compact_signals: Dict[str, Any],
) -> List[ParsedRule]:
    """
    Add safe high-confidence detector suggested rules that the LLM omitted.

    This is intentionally conservative:
    - only visible-ad strategies,
    - only block/hide rules,
    - no exceptions,
    - no tracking-only domains,
    - no broad cosmetic selectors,
    - no unsafe site-wide network blocks,
    - must respect ticket_context scope.
    """
    ticket_context = compact_signals.get("ticket_context", {})
    if not isinstance(ticket_context, Mapping):
        ticket_context = {}

    strategy = str(ticket_context.get("resolution_strategy", "") or "").strip().lower()
    problem_type = str(ticket_context.get("problem_type", "") or "").strip().lower()
    evidence_level = str(ticket_context.get("evidence_level", "") or "").strip().lower()

    if strategy not in VISIBLE_AD_STRATEGIES:
        return _dedupe_parsed_rules(parsed_rules)

    page_url = str(compact_signals.get("url", "") or "")
    page_domain = _clean_domain(_hostname(page_url))

    existing_rules = [rule.rule for rule in parsed_rules]
    auto_rules = _select_detector_backfill_rules(
        compact_signals=compact_signals,
        existing_rules=existing_rules,
        page_domain=page_domain,
        strategy=strategy,
        problem_type=problem_type,
        evidence_level=evidence_level,
        ticket_context=ticket_context,
    )

    if not auto_rules:
        return _dedupe_parsed_rules(parsed_rules)

    auto_parsed = parse_llm_response("\n".join(auto_rules))

    if auto_parsed:
        logger.info(
            "Auto-appended %s high-confidence detector rule(s) omitted by LLM: %s",
            len(auto_parsed),
            [rule.rule for rule in auto_parsed],
        )

    return _dedupe_parsed_rules(parsed_rules + auto_parsed)


def _filter_ticket_constrained_parsed_rules(
    rules: List[ParsedRule],
    ticket_context: Any,
    compact_signals: Optional[Dict[str, Any]] = None,
) -> List[ParsedRule]:
    """
    Remove LLM-generated rules that violate ticket constraints.

    Prompt instructions alone are not enough. The LLM can still output a broad
    rule that conflicts with a narrow user ticket, so we enforce ticket_context
    after parsing and before detector backfill.
    """
    if not rules:
        return []

    if not isinstance(ticket_context, Mapping):
        return rules

    problem_type = str(ticket_context.get("problem_type", "") or "").strip().lower()
    strategy = str(ticket_context.get("resolution_strategy", "") or "").strip().lower()

    kept: List[ParsedRule] = []
    removed: List[str] = []

    for rule in rules:
        rule_text = str(getattr(rule, "rule", "") or "").strip()
        if not rule_text:
            continue

        if _rule_is_forbidden_by_ticket(rule_text, ticket_context):
            removed.append(f"{rule_text} (forbidden_by_ticket)")
            continue

        if _rule_violates_ticket_candidate_scope(
            rule=rule_text,
            ticket_context=ticket_context,
            compact_signals=compact_signals,
        ):
            removed.append(f"{rule_text} (outside_ticket_candidate_scope)")
            continue

        if _rule_is_noise_for_problem_strategy(
            rule=rule_text,
            problem_type=problem_type,
            strategy=strategy,
            compact_signals=compact_signals,
        ):
            removed.append(f"{rule_text} (noise_for_problem_strategy)")
            continue

        kept.append(rule)

    if removed:
        logger.warning(
            "Removed %d LLM rule(s) violating ticket_context/problem strategy: %s",
            len(removed),
            removed,
        )

    return kept


def _select_detector_backfill_rules(
    compact_signals: Dict[str, Any],
    existing_rules: List[str],
    page_domain: str,
    strategy: str,
    problem_type: str,
    evidence_level: str,
    ticket_context: Mapping[str, Any],
) -> List[str]:
    candidates = compact_signals.get("ad_candidates", [])
    if not isinstance(candidates, list):
        return []

    existing_set = {str(rule).strip() for rule in existing_rules if str(rule).strip()}
    selected: List[str] = []

    sorted_candidates = sorted(
        [
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping)
        ],
        key=lambda candidate: _candidate_backfill_sort_key(
            candidate=candidate,
            problem_type=problem_type,
            strategy=strategy,
        ),
    )

    for candidate in sorted_candidates:
        if len(selected) >= AUTO_APPEND_MAX_RULES:
            break

        rule = str(candidate.get("suggested_rule", "") or "").strip()
        if not rule:
            continue

        if rule in existing_set or rule in selected:
            continue

        if _rule_is_forbidden_by_ticket(rule, ticket_context):
            logger.info(
                "Skipped detector backfill rule forbidden by ticket_context: %s",
                rule,
            )
            continue

        if not _candidate_allowed_by_ticket_scope(candidate, ticket_context):
            logger.info(
                "Skipped detector backfill rule outside ticket candidate scope: %s",
                rule,
            )
            continue

        if not _candidate_relevant_for_problem_strategy(
            candidate=candidate,
            problem_type=problem_type,
            strategy=strategy,
        ):
            logger.info(
                "Skipped detector backfill rule not relevant for problem strategy: %s",
                rule,
            )
            continue

        if _candidate_mentions_preserved_context(candidate, ticket_context):
            logger.info(
                "Skipped detector backfill rule because candidate overlaps preserve context: %s",
                rule,
            )
            continue

        if not _candidate_is_backfillable(candidate):
            continue

        if not _is_safe_auto_append_rule(
            rule=rule,
            candidate=candidate,
            page_domain=page_domain,
            strategy=strategy,
            problem_type=problem_type,
            evidence_level=evidence_level,
        ):
            continue

        selected.append(rule)

    return selected


def _rule_is_noise_for_problem_strategy(
    rule: str,
    problem_type: str,
    strategy: str,
    compact_signals: Optional[Dict[str, Any]],
) -> bool:
    """
    Drop rules that are technically ad-related but irrelevant to the ticket type.

    For anti_adblock_or_overlay, the goal is normally to remove the blocking
    overlay/popup/close issue, not to clean all generic ads on the page.
    """
    if problem_type != "anti_adblock_or_overlay":
        return False

    if strategy != "remove_overlay_or_allow_required_resource":
        return False

    if _is_network_rule(rule):
        return True

    if _rule_is_overlay_relevant(rule, compact_signals):
        return False

    if _rule_is_generic_page_ad(rule, compact_signals):
        return True

    categories = _candidate_categories_for_rule(rule, compact_signals)

    if categories and not any(
        category in {"popup_overlay", "floating_ad"} for category in categories
    ):
        # Keep ad_container only when it looks modal/dialog/rewarded/close-related.
        if "ad_container" in categories:
            return not _rule_or_candidate_context_has_overlay_hint(rule, compact_signals)
        return True

    return False


def _candidate_relevant_for_problem_strategy(
    candidate: Mapping[str, Any],
    problem_type: str,
    strategy: str,
) -> bool:
    """
    Decide whether a detector candidate should be considered for auto-backfill
    under the current ticket problem type/strategy.
    """
    if problem_type != "anti_adblock_or_overlay":
        return True

    if strategy != "remove_overlay_or_allow_required_resource":
        return True

    category = str(candidate.get("category", "") or "").strip().lower()

    if category in {"popup_overlay", "floating_ad"}:
        return True

    if category == "ad_container":
        return _candidate_has_overlay_relevant_context(candidate)

    # For anti-adblock/overlay, avoid automatically appending network/iframe
    # rules unless future ticket_context explicitly asks for them.
    return False


def _candidate_has_overlay_relevant_context(candidate: Mapping[str, Any]) -> bool:
    text = _candidate_text(candidate)
    normalized = _normalize_human_text(text)

    if _text_contains_any_hint(normalized, OVERLAY_RELEVANT_HINTS):
        return True

    selector = str(candidate.get("selector", "") or "").strip().lower()
    rule = str(candidate.get("suggested_rule", "") or "").strip().lower()

    return (
        "ad-content" in selector
        or "ad-content" in rule
        or "sticky_ads" in selector
        or "sticky_ads" in rule
    )


def _rule_is_overlay_relevant(
    rule: str,
    compact_signals: Optional[Dict[str, Any]],
) -> bool:
    selector = _cosmetic_selector(rule)
    normalized_rule = _normalize_human_text(rule)
    normalized_selector = _normalize_human_text(selector)

    if _text_contains_any_hint(normalized_rule, OVERLAY_RELEVANT_HINTS):
        return True

    if _text_contains_any_hint(normalized_selector, OVERLAY_RELEVANT_HINTS):
        return True

    if _rule_or_candidate_context_has_overlay_hint(rule, compact_signals):
        return True

    return False


def _rule_or_candidate_context_has_overlay_hint(
    rule: str,
    compact_signals: Optional[Dict[str, Any]],
) -> bool:
    if not compact_signals or not isinstance(compact_signals, Mapping):
        return False

    candidates = compact_signals.get("ad_candidates", [])
    if not isinstance(candidates, list):
        return False

    normalized_rule = _normalize_rule_for_ticket_compare(rule)
    rule_selector = ""
    if "##" in normalized_rule:
        rule_selector = normalized_rule.split("##", 1)[1].strip()

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue

        suggested_rule = _normalize_rule_for_ticket_compare(
            candidate.get("suggested_rule", "")
        )
        selector = _normalize_rule_for_ticket_compare(candidate.get("selector", ""))

        if suggested_rule != normalized_rule and (
            not rule_selector or selector != rule_selector
        ):
            continue

        if _candidate_has_overlay_relevant_context(candidate):
            return True

    return False


def _rule_is_generic_page_ad(
    rule: str,
    compact_signals: Optional[Dict[str, Any]],
) -> bool:
    text = _normalize_human_text(rule)

    if _text_contains_any_hint(text, GENERIC_PAGE_AD_HINTS):
        return True

    if not compact_signals or not isinstance(compact_signals, Mapping):
        return False

    candidates = compact_signals.get("ad_candidates", [])
    if not isinstance(candidates, list):
        return False

    normalized_rule = _normalize_rule_for_ticket_compare(rule)
    rule_selector = ""
    if "##" in normalized_rule:
        rule_selector = normalized_rule.split("##", 1)[1].strip()

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue

        suggested_rule = _normalize_rule_for_ticket_compare(
            candidate.get("suggested_rule", "")
        )
        selector = _normalize_rule_for_ticket_compare(candidate.get("selector", ""))

        if suggested_rule != normalized_rule and (
            not rule_selector or selector != rule_selector
        ):
            continue

        candidate_text = _normalize_human_text(_candidate_text(candidate))

        if _text_contains_any_hint(candidate_text, GENERIC_PAGE_AD_HINTS):
            return True

        parent_chain = candidate.get("parent_chain", [])
        if isinstance(parent_chain, list):
            parent_text = _normalize_human_text(str(parent_chain))
            if _text_contains_any_hint(parent_text, GENERIC_PAGE_AD_HINTS):
                return True

    return False


def _candidate_text(candidate: Mapping[str, Any]) -> str:
    text = " ".join(
        str(candidate.get(key, "") or "")
        for key in (
            "suggested_rule",
            "selector",
            "reason",
            "element_snippet",
            "domain",
        )
    )

    parent_chain = candidate.get("parent_chain", [])
    if isinstance(parent_chain, list):
        text += " " + " ".join(str(item) for item in parent_chain)

    return text


def _rule_is_forbidden_by_ticket(rule: str, ticket_context: Mapping[str, Any]) -> bool:
    forbidden_rules = _ticket_forbidden_rules(ticket_context)
    if not forbidden_rules:
        return False

    normalized_rule = _normalize_rule_for_ticket_compare(rule)
    if not normalized_rule:
        return False

    for forbidden in forbidden_rules:
        normalized_forbidden = _normalize_rule_for_ticket_compare(forbidden)
        if not normalized_forbidden:
            continue

        if normalized_rule == normalized_forbidden:
            return True

        if "*" in normalized_forbidden:
            pattern = re.escape(normalized_forbidden).replace("\\*", ".*")
            if re.fullmatch(pattern, normalized_rule):
                return True

    return False


def _ticket_forbidden_rules(ticket_context: Mapping[str, Any]) -> List[str]:
    hints = _ticket_validation_hints(ticket_context)

    result: List[str] = []
    for key in ("must_not_generate_rules", "forbidden_rules", "disallowed_rules"):
        result.extend(_as_string_list(hints.get(key, [])))

    return _dedupe_strings(result)


def _rule_violates_ticket_candidate_scope(
    rule: str,
    ticket_context: Mapping[str, Any],
    compact_signals: Optional[Dict[str, Any]],
) -> bool:
    """
    Return True when a generated rule is outside the ticket candidate scope.

    This supports two levels:
    1. Exact detector mapping:
       If the rule maps to an ad_candidate category, enforce allowed/disallowed.
    2. Heuristic fallback:
       If the rule does not map to a candidate but clearly looks like a network
       block or iframe block, still enforce disallowed categories.
    """
    if not isinstance(ticket_context, Mapping):
        return False

    allowed = _ticket_allowed_candidate_categories(ticket_context)
    disallowed = _ticket_disallowed_candidate_categories(ticket_context)

    if not allowed and not disallowed:
        return False

    categories = _candidate_categories_for_rule(rule, compact_signals)
    inferred_categories = _infer_candidate_categories_from_rule(rule)

    for category in inferred_categories:
        if category not in categories:
            categories.append(category)

    if any(category in disallowed for category in categories):
        return True

    if allowed and categories:
        return not any(category in allowed for category in categories)

    if allowed and not categories:
        if _is_network_rule(rule):
            return "ad_network_request" not in allowed

    return False


def _candidate_allowed_by_ticket_scope(
    candidate: Mapping[str, Any],
    ticket_context: Mapping[str, Any],
) -> bool:
    """
    Return whether a detector candidate is allowed to be auto-appended for this
    ticket scope.
    """
    if not isinstance(ticket_context, Mapping):
        return True

    allowed = _ticket_allowed_candidate_categories(ticket_context)
    disallowed = _ticket_disallowed_candidate_categories(ticket_context)

    if not allowed and not disallowed:
        return True

    category = str(candidate.get("category", "") or "").strip().lower()

    if category and category in disallowed:
        return False

    if allowed:
        if not category:
            return False
        return category in allowed

    return True


def _candidate_categories_for_rule(
    rule: str,
    compact_signals: Optional[Dict[str, Any]],
) -> List[str]:
    """
    Find detector candidate categories that produced or match this rule.
    """
    if not compact_signals or not isinstance(compact_signals, Mapping):
        return []

    candidates = compact_signals.get("ad_candidates", [])
    if not isinstance(candidates, list):
        return []

    normalized_rule = _normalize_rule_for_ticket_compare(rule)
    if not normalized_rule:
        return []

    rule_selector = ""
    if "##" in normalized_rule:
        rule_selector = normalized_rule.split("##", 1)[1].strip()

    categories: List[str] = []

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue

        category = str(candidate.get("category", "") or "").strip().lower()
        if not category:
            continue

        suggested_rule = _normalize_rule_for_ticket_compare(
            candidate.get("suggested_rule", "")
        )
        selector = _normalize_rule_for_ticket_compare(
            candidate.get("selector", "")
        )

        if suggested_rule and suggested_rule == normalized_rule:
            categories.append(category)
            continue

        if rule_selector and selector and selector == rule_selector:
            categories.append(category)
            continue

    return _dedupe_strings(categories)


def _infer_candidate_categories_from_rule(rule: str) -> List[str]:
    """
    Heuristically infer detector-like categories from a rule.

    This is intentionally coarse and only used as a safety guard for ticket scope.
    Detector-provided candidate categories remain the stronger signal.
    """
    text = str(rule or "").strip().lower()
    if not text:
        return []

    categories: List[str] = []

    if _is_network_rule(text):
        categories.append("ad_network_request")
        return categories

    selector = _cosmetic_selector(text)

    if not selector:
        return categories

    selector_text = _normalize_human_text(selector)

    if _text_contains_any_hint(selector_text, IFRAME_HINTS):
        categories.append("ad_iframe")

    if _text_contains_any_hint(selector_text, POPUP_OVERLAY_HINTS):
        categories.append("popup_overlay")

    if _text_contains_any_hint(selector_text, FLOATING_HINTS):
        categories.append("floating_ad")

    if _text_has_ad_hint(selector_text):
        categories.append("ad_container")

    return _dedupe_strings(categories)


def _ticket_allowed_candidate_categories(
    ticket_context: Mapping[str, Any],
) -> List[str]:
    hints = _ticket_validation_hints(ticket_context)
    return _normalize_category_list(hints.get("allowed_candidate_categories", []))


def _ticket_disallowed_candidate_categories(
    ticket_context: Mapping[str, Any],
) -> List[str]:
    hints = _ticket_validation_hints(ticket_context)
    return _normalize_category_list(hints.get("disallowed_candidate_categories", []))


def _ticket_validation_hints(ticket_context: Mapping[str, Any]) -> Mapping[str, Any]:
    hints = ticket_context.get("validation_hints", {})
    if isinstance(hints, Mapping):
        return hints
    return {}


def _normalize_category_list(value: Any) -> List[str]:
    return [
        item.strip().lower()
        for item in _as_string_list(value)
        if item.strip()
    ]


def _candidate_mentions_preserved_context(
    candidate: Mapping[str, Any],
    ticket_context: Mapping[str, Any],
) -> bool:
    protected_terms = _ticket_preserve_terms(ticket_context)
    if not protected_terms:
        return False

    candidate_text = _candidate_text(candidate)
    normalized_candidate_text = _normalize_human_text(candidate_text)

    for term in protected_terms:
        normalized_term = _normalize_human_text(term)
        if len(normalized_term) < 4:
            continue
        if normalized_term in normalized_candidate_text:
            return True

    return False


def _ticket_preserve_terms(ticket_context: Mapping[str, Any]) -> List[str]:
    terms: List[str] = []
    terms.extend(_as_string_list(ticket_context.get("target_to_preserve", [])))

    for key in ("preserve_regions", "focus_selectors"):
        terms.extend(_extract_region_terms(ticket_context.get(key)))

    region_focus = ticket_context.get("region_focus")
    if _region_value_looks_preserved(region_focus):
        terms.extend(_extract_region_terms(region_focus))

    hints = ticket_context.get("validation_hints", {})
    if isinstance(hints, Mapping):
        for key in ("must_preserve_text", "preserve_text", "must_contain_text"):
            terms.extend(_as_string_list(hints.get(key, [])))
        for key in ("allowed_ad_region", "allowed_regions", "preserve_regions"):
            terms.extend(_extract_region_terms(hints.get(key)))

    return _dedupe_strings(terms)


def _extract_region_terms(value: Any) -> List[str]:
    terms: List[str] = []

    if value in (None, "", [], {}):
        return terms

    if isinstance(value, str):
        return _as_string_list(value)

    if isinstance(value, Mapping):
        for key in (
            "name",
            "label",
            "title",
            "text",
            "must_contain_text",
            "description",
            "selector",
            "selectors",
            "region",
            "focus_region",
        ):
            terms.extend(_extract_region_terms(value.get(key)))
        return terms

    if isinstance(value, list):
        for item in value:
            terms.extend(_extract_region_terms(item))
        return terms

    return _as_string_list(value)


def _region_value_looks_preserved(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False

    mode = _normalize_human_text(
        value.get("mode")
        or value.get("type")
        or value.get("role")
        or value.get("intent")
        or ""
    )

    return any(token in mode for token in ("preserve", "allow", "protect", "safe"))


def _candidate_backfill_sort_key(
    candidate: Mapping[str, Any],
    problem_type: str,
    strategy: str,
) -> tuple[int, int, int, str]:
    category = str(candidate.get("category", "") or "").strip().lower()
    confidence = str(candidate.get("confidence", "") or "").strip().lower()
    rule = str(candidate.get("suggested_rule", "") or "")

    confidence_rank = {
        "very_high": 0,
        "very-high": 0,
        "strong": 0,
        "high": 0,
        "medium": 1,
        "med": 1,
        "moderate": 1,
        "low": 2,
    }.get(confidence, 9)

    category_rank = AUTO_APPEND_CATEGORY_RANK.get(category, 99)

    if problem_type == "anti_adblock_or_overlay":
        if category == "popup_overlay":
            category_rank = 0
        elif category == "floating_ad":
            category_rank = 1
        elif category == "ad_container" and _candidate_has_overlay_relevant_context(candidate):
            category_rank = 2
        elif category == "ad_container":
            category_rank = 50

    narrow_network_rank = 0 if _looks_like_narrow_network_rule(rule) else 1

    return (
        confidence_rank,
        category_rank,
        narrow_network_rank,
        rule,
    )


def _candidate_is_backfillable(candidate: Mapping[str, Any]) -> bool:
    category = str(candidate.get("category", "") or "").strip().lower()
    confidence = str(candidate.get("confidence", "") or "").strip().lower()

    if category == "tracking_script":
        return False

    if confidence in {"high", "very_high", "very-high", "strong"}:
        return category in {
            "popup_overlay",
            "floating_ad",
            "ad_container",
            "ad_network_request",
            "ad_iframe",
        }

    if confidence in {"medium", "med", "moderate"}:
        return category in {
            "popup_overlay",
            "floating_ad",
            "ad_container",
        }

    return False


def _is_safe_auto_append_rule(
    rule: str,
    candidate: Mapping[str, Any],
    page_domain: str,
    strategy: str,
    problem_type: str,
    evidence_level: str,
) -> bool:
    rule = str(rule or "").strip()
    if not rule:
        return False

    if rule.startswith("@@") or "#@#" in rule:
        return False

    if "##" in rule:
        return _is_safe_auto_cosmetic_rule(rule, candidate, page_domain)

    return _is_safe_auto_network_rule(rule, candidate, page_domain)


def _is_safe_auto_cosmetic_rule(
    rule: str,
    candidate: Mapping[str, Any],
    page_domain: str,
) -> bool:
    if "##" not in rule:
        return False

    domain_part, selector = rule.split("##", 1)
    domain_part = domain_part.strip()
    selector = selector.strip()

    if not domain_part or not selector:
        return False

    if not page_domain:
        return False

    if not _cosmetic_domain_mentions_page_domain(domain_part, page_domain):
        return False

    selector_lower = selector.lower().strip()

    if selector_lower in BROAD_COSMETIC_SELECTORS:
        return False

    if selector_lower.startswith(("#", ".")):
        return _text_has_ad_hint(selector_lower) or _text_has_overlay_hint(selector_lower)

    if selector_lower.startswith("div.") or selector_lower.startswith("div#"):
        return _text_has_ad_hint(selector_lower) or _text_has_overlay_hint(selector_lower)

    if selector_lower.startswith("section.") or selector_lower.startswith("section#"):
        return _text_has_ad_hint(selector_lower) or _text_has_overlay_hint(selector_lower)

    if selector_lower.startswith("aside.") or selector_lower.startswith("aside#"):
        return _text_has_ad_hint(selector_lower) or _text_has_overlay_hint(selector_lower)

    if selector_lower.startswith("button."):
        return _text_has_ad_hint(selector_lower) or _text_has_overlay_hint(selector_lower)

    if selector_lower.startswith("span."):
        return _text_has_ad_hint(selector_lower) or _text_has_overlay_hint(selector_lower)

    if selector_lower.startswith("body >"):
        category = str(candidate.get("category", "") or "").strip().lower()
        return category == "popup_overlay"

    return _text_has_ad_hint(selector_lower) or _text_has_overlay_hint(selector_lower)


def _is_safe_auto_network_rule(
    rule: str,
    candidate: Mapping[str, Any],
    page_domain: str,
) -> bool:
    text = rule.strip()

    if not text.startswith("||"):
        return False

    if text.startswith("@@"):
        return False

    host = _network_rule_host(text)
    if not host:
        return False

    if _host_in_domains(host, TRACKING_ONLY_DOMAINS):
        return False

    if _host_in_domains(host, SAFE_DOMAINS):
        return False

    if page_domain and text in {
        f"||{page_domain}^",
        f"||www.{page_domain}^",
    }:
        return False

    lower = text.lower()

    has_domain_scope = (
        not page_domain
        or f"domain={page_domain}" in lower
        or f"domain=www.{page_domain}" in lower
    )

    if _looks_like_narrow_network_rule(text) and has_domain_scope:
        return _text_has_ad_hint(lower)

    category = str(candidate.get("category", "") or "").strip().lower()
    if category == "ad_network_request" and _text_has_ad_hint(lower):
        return True

    return False


def _looks_like_narrow_network_rule(rule: str) -> bool:
    text = str(rule or "").lower()

    if not text.startswith("||"):
        return False

    return (
        "/" in text[2:]
        and "^" in text
        and "$" in text
        and "domain=" in text
    )


def _is_network_rule(rule: str) -> bool:
    text = str(rule or "").strip()
    return text.startswith("||") or text.startswith("@@||")


def _cosmetic_selector(rule: str) -> str:
    text = str(rule or "").strip()

    if "##" in text:
        return text.split("##", 1)[1].strip()

    if "#@#" in text:
        return text.split("#@#", 1)[1].strip()

    return ""


def _network_rule_host(rule: str) -> str:
    text = str(rule or "").strip()

    if text.startswith("@@"):
        text = text[2:]

    if not text.startswith("||"):
        return ""

    body = text[2:]

    for separator in ["/", "^", "$"]:
        if separator in body:
            body = body.split(separator, 1)[0]

    return body.strip().lower()


def _cosmetic_domain_mentions_page_domain(domain_part: str, page_domain: str) -> bool:
    domains = [
        item.strip()
        for item in domain_part.split(",")
        if item.strip()
    ]

    if not domains:
        return False

    for domain in domains:
        if domain.startswith("~"):
            continue

        if _host_matches_domain(page_domain, domain):
            return True

    return False


def _text_has_ad_hint(text: str) -> bool:
    value = str(text or "").lower()

    if any(path_hint in value for path_hint in AD_HINT_PATHS):
        return True

    return _text_contains_any_hint(value, AD_HINT_WORDS)


def _text_has_overlay_hint(text: str) -> bool:
    value = str(text or "").lower()
    return _text_contains_any_hint(value, POPUP_OVERLAY_HINTS + FLOATING_HINTS)


def _text_contains_any_hint(text: str, hints: tuple[str, ...]) -> bool:
    value = str(text or "").lower()

    for hint in hints:
        escaped = re.escape(hint.lower())

        if "/" in hint:
            if hint.lower() in value:
                return True
            continue

        pattern = rf"(^|[^a-z0-9]){escaped}([^a-z0-9]|$)"
        if re.search(pattern, value):
            return True

    return False


def _dedupe_parsed_rules(rules: List[ParsedRule]) -> List[ParsedRule]:
    seen: set[str] = set()
    unique: List[ParsedRule] = []

    for rule in rules:
        key = str(rule.rule or "").strip()

        if not key or key in seen:
            continue

        seen.add(key)
        unique.append(rule)

    return unique


def _dedupe_strings(values: List[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()

    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned:
            continue

        key = cleaned.lower()
        if key in seen:
            continue

        seen.add(key)
        result.append(cleaned)

    return result


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


def _normalize_rule_for_ticket_compare(rule: Any) -> str:
    return str(rule or "").strip().lower()


def _normalize_human_text(value: Any) -> str:
    text = str(value or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("đ", "d")
    return re.sub(r"\s+", " ", text).strip()


def _extract_llm_text(llm_response: Any) -> str:
    """
    Support both old and new llm_client return formats.

    Old format:
        call_llm_with_fallback(...) -> str

    New token-aware format:
        call_llm_with_fallback(...) -> LLMResponse(text=..., usage=...)
    """
    if isinstance(llm_response, str):
        return llm_response.strip()

    return (getattr(llm_response, "text", "") or "").strip()


def _build_token_usage_payload(llm_response: Any) -> Optional[Dict[str, Any]]:
    """
    Convert LLMResponse.usage into a JSON-serialisable dict.

    Returns None if current llm_client still returns plain text or does not
    expose usage metadata.
    """
    usage = getattr(llm_response, "usage", None)

    if not usage:
        return None

    return {
        "model": getattr(llm_response, "model", ""),
        "fallback_used": bool(getattr(llm_response, "fallback_used", False)),
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
    }


def _extract_signals(crawl_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull only the fields the LLM needs from the full crawl result.

    Keeps:
        - url
        - title
        - environment
        - ticket_context
        - third_party: domain, request_count, sample_paths
        - ad_candidates: category, confidence, suggested_rule, selector, reason

    Drops:
        - raw HTML
        - screenshot path
        - elapsed time
        - full URLs with query strings where possible
    """
    ticket_context = _normalize_context_for_prompt(
        crawl_result.get("ticket_context", {})
    )

    signals: Dict[str, Any] = {
        "url": crawl_result.get("url", ""),
        "title": crawl_result.get("title", ""),
        "environment": crawl_result.get("environment", "desktop"),
        "ticket_context": ticket_context,
        "third_party": [],
        "ad_candidates": [],
    }

    network_data = crawl_result.get("network_requests", {})
    if not isinstance(network_data, Mapping):
        network_data = {}

    third_party_raw = network_data.get("third_party", [])

    if isinstance(third_party_raw, list):
        for item in third_party_raw:
            if not isinstance(item, Mapping):
                continue

            signals["third_party"].append(
                {
                    "domain": str(item.get("domain", "")).strip(),
                    "request_count": _safe_int(item.get("request_count", 0)),
                    "sample_paths": _clean_sample_paths(
                        item.get("sample_paths", []),
                    ),
                }
            )

    elif isinstance(third_party_raw, Mapping):
        by_domain = third_party_raw.get("by_domain", {})

        if isinstance(by_domain, Mapping):
            for domain, urls in by_domain.items():
                paths = []

                if isinstance(urls, list):
                    paths = [_url_to_path(url) for url in urls if url]

                signals["third_party"].append(
                    {
                        "domain": str(domain).strip(),
                        "request_count": len(urls) if isinstance(urls, list) else 0,
                        "sample_paths": sorted(set(paths))[:3],
                    }
                )

    cleanup_pattern = re.compile(
        r'\s+(?:ad-events|style|impression-id)\s*=\s*[\'"].*?[\'"]',
        re.IGNORECASE,
    )

    for candidate in crawl_result.get("ad_candidates", []):
        if not isinstance(candidate, Mapping):
            continue

        raw_snippet = (
            candidate.get("element_snippet")
            or candidate.get("outer_html_snippet")
            or ""
        )

        clean_snippet = cleanup_pattern.sub("", str(raw_snippet or ""))

        if clean_snippet.startswith("<") and not clean_snippet.endswith(">"):
            clean_snippet += ">"

        signals["ad_candidates"].append(
            {
                "category": candidate.get("category", ""),
                "confidence": candidate.get("confidence", ""),
                "suggested_rule": candidate.get("suggested_rule", ""),
                "selector": candidate.get("selector", ""),
                "reason": candidate.get("reason", ""),
                "domain": candidate.get("domain", ""),
                "element_snippet": _truncate(clean_snippet, 500),
                "parent_chain": candidate.get("parent_chain", []),
            }
        )

    signals["third_party"] = [
        item
        for item in signals["third_party"]
        if item.get("domain")
    ]

    return signals


def _normalize_context_for_prompt(raw_context: Any) -> Dict[str, Any]:
    """
    Normalize ticket context before putting it into the prompt.

    If app.services.ticket_context.normalize_ticket_context exists, use it.
    Otherwise, fall back to a safe minimal dict.
    """
    if normalize_ticket_context is not None:
        try:
            return normalize_ticket_context(raw_context)
        except Exception as exc:
            logger.warning("Failed to normalize ticket_context: %s", exc)

    if isinstance(raw_context, Mapping):
        return dict(raw_context)

    if raw_context:
        return {
            "problem_type": "unknown",
            "description": str(raw_context),
        }

    return {
        "problem_type": "unknown",
    }


def _clean_sample_paths(sample_paths: Any, limit: int = 5) -> List[str]:
    """
    Clean and limit sample paths.

    Keeps only URL paths, strips query strings, and removes duplicates.
    """
    if not isinstance(sample_paths, list):
        return []

    cleaned_paths = []

    for value in sample_paths:
        if not value:
            continue

        path = _url_to_path(str(value))
        if path:
            cleaned_paths.append(path)

    return sorted(set(cleaned_paths))[:limit]


def _url_to_path(value: Any) -> str:
    """
    Convert a full URL or path-like value into a compact path without query string.
    """
    value = str(value or "")

    try:
        parsed = urlparse(value)

        if parsed.scheme and parsed.netloc:
            return parsed.path or "/"

        if value.startswith("/"):
            return value.split("?", 1)[0] or "/"

        return value.split("?", 1)[0] or "/"

    except Exception:
        return "/"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _hostname(url: str) -> str:
    try:
        return urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""


def _clean_domain(domain: str) -> str:
    domain = str(domain or "").lower().strip(".")

    if domain.startswith("www."):
        return domain[4:]

    return domain


def _host_matches_domain(host: str, domain: str) -> bool:
    host = str(host or "").lower().strip(".")
    domain = str(domain or "").lower().strip(".")

    if not host or not domain:
        return False

    return host == domain or host.endswith("." + domain)


def _host_in_domains(host: str, domains: set[str]) -> bool:
    host = str(host or "").lower().strip(".")

    if not host:
        return False

    return any(_host_matches_domain(host, domain) for domain in domains)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value

    return value[: limit - 3].rstrip() + "..."


def _preview_prompt(prompt: str, limit: int = 1200) -> str:
    """
    Keep a short prompt preview for debugging without storing huge prompts.
    """
    return _truncate(prompt, limit)
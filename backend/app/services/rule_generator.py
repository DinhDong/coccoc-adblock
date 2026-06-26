import logging
import re
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
    "ad_network_request": 1,
    "ad_container": 2,
    "floating_ad": 3,
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

AD_LIKE_RULE_HINTS = (
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
    "/ads/",
    "/ad/",
    "/storage/ads/",
    "/banner/",
    "/popup/",
    "/sponsor/",
    "/promo/",
)


@dataclass
class RuleGenerationResult:
    """
    Result returned by the AI rule generation stage.

    rules:
        Parsed rules used by validation/workflow.

    token_usage:
        LLM token usage metadata used in generated *_rules.json output.
    """

    rules: List[ParsedRule]
    token_usage: Optional[Dict[str, Any]] = None
    model: str = ""
    fallback_used: bool = False
    prompt_preview: str = ""

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
        2. Include ticket_context in compact signals.
        3. Assemble prompt via prompt_builder.build_prompt().
        4. Call LLM via llm_client.call_llm_with_fallback().
        5. Capture token usage if the LLM client returns it.
        6. Parse raw response into ParsedRule objects.
        7. Auto-append safe high-confidence detector suggested rules that the LLM omitted.
        8. Return rules + token metadata.

    Why step 7 exists:
        The LLM sometimes chooses only cosmetic overlay rules and omits an
        independent high-confidence network rule such as:
            ||cdn.site.com/storage/ads/^$image,domain=site.com

        That omission leaves visible ads loaded even though validation can see
        that each selected cosmetic rule works individually. The detector already
        produced a narrow safe suggested_rule, so the generator should not drop it.
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
            "Successfully generated %s candidate rules via AI orchestration (%s from LLM, %s after detector backfill).",
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
        return RuleGenerationResult(rules=[])


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
    - no unsafe site-wide network blocks.
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


def _select_detector_backfill_rules(
    compact_signals: Dict[str, Any],
    existing_rules: List[str],
    page_domain: str,
    strategy: str,
    problem_type: str,
    evidence_level: str,
) -> List[str]:
    candidates = compact_signals.get("ad_candidates", [])
    if not isinstance(candidates, list):
        return []

    existing_set = {str(rule).strip() for rule in existing_rules if str(rule).strip()}
    selected: List[str] = []

    sorted_candidates = sorted(
        [
            candidate for candidate in candidates
            if isinstance(candidate, Mapping)
        ],
        key=_candidate_backfill_sort_key,
    )

    for candidate in sorted_candidates:
        if len(selected) >= AUTO_APPEND_MAX_RULES:
            break

        rule = str(candidate.get("suggested_rule", "") or "").strip()
        if not rule:
            continue

        if rule in existing_set or rule in selected:
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


def _candidate_backfill_sort_key(candidate: Mapping[str, Any]) -> tuple[int, int, int, str]:
    category = str(candidate.get("category", "") or "").strip().lower()
    confidence = str(candidate.get("confidence", "") or "").strip().lower()
    rule = str(candidate.get("suggested_rule", "") or "")

    confidence_rank = {
        "high": 0,
        "very_high": 0,
        "very-high": 0,
        "strong": 0,
        "medium": 1,
        "med": 1,
        "moderate": 1,
        "low": 2,
    }.get(confidence, 9)

    category_rank = AUTO_APPEND_CATEGORY_RANK.get(category, 99)

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
            "ad_network_request",
            "ad_container",
            "floating_ad",
            "ad_iframe",
        }

    # Allow medium candidates only when the detector category is still very targeted.
    if confidence in {"medium", "med", "moderate"}:
        return category in {
            "popup_overlay",
            "ad_container",
            "floating_ad",
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
        # .adserver / #ad-modal style selectors are okay only if ad/overlay-like.
        return _text_has_ad_hint(selector_lower)

    if selector_lower.startswith("div.") or selector_lower.startswith("div#"):
        return _text_has_ad_hint(selector_lower)

    if selector_lower.startswith("section.") or selector_lower.startswith("section#"):
        return _text_has_ad_hint(selector_lower)

    if selector_lower.startswith("aside.") or selector_lower.startswith("aside#"):
        return _text_has_ad_hint(selector_lower)

    if selector_lower.startswith("body >"):
        # Structural selectors are only safe when the detector says this is a
        # fullscreen overlay. Otherwise they are too fragile.
        category = str(candidate.get("category", "") or "").strip().lower()
        return category == "popup_overlay"

    return _text_has_ad_hint(selector_lower)


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

    if text in {
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

    # Narrow path rules with explicit domain scope are safest.
    if _looks_like_narrow_network_rule(text) and has_domain_scope:
        return _text_has_ad_hint(lower)

    # Known detector ad_network_request can still be allowed if it has an ad hint.
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

    return any(hint in value for hint in AD_LIKE_RULE_HINTS)


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
        item for item in signals["third_party"]
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
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

from ..ai.prompt_builder import build_prompt
from ..ai.llm_client import call_llm_with_fallback
from ..ai.rule_parser import parse_llm_response, ParsedRule

try:
    from .ticket_context import normalize_ticket_context
except Exception:
    normalize_ticket_context = None  # type: ignore


logger = logging.getLogger(__name__)


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
        7. Return rules + token metadata.
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
            return RuleGenerationResult(
                rules=[],
                token_usage=_build_token_usage_payload(llm_response),
                model=getattr(llm_response, "model", ""),
                fallback_used=bool(getattr(llm_response, "fallback_used", False)),
                prompt_preview=_preview_prompt(user_message),
            )

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

        logger.info(
            "Successfully generated %s candidate rules via AI orchestration.",
            len(parsed_rules),
        )

        return RuleGenerationResult(
            rules=parsed_rules,
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
        - full URLs with query strings
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
                "element_snippet": _truncate(clean_snippet, 400),
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


def _clean_sample_paths(sample_paths: Any, limit: int = 3) -> List[str]:
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


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value

    return value[: limit - 3].rstrip() + "..."


def _preview_prompt(prompt: str, limit: int = 1200) -> str:
    """
    Keep a short prompt preview for debugging without storing huge prompts.
    """
    return _truncate(prompt, limit)
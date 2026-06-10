import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..ai.prompt_builder import DEFAULT_SYSTEM_PROMPT, build_prompt
from ..ai.llm_client import call_llm_with_fallback, LLMResponse
from ..ai.rule_parser import parse_llm_response, ParsedRule


logger = logging.getLogger(__name__)


@dataclass
class RuleGenerationResult:
    """
    Result returned by the AI rule generation stage.

    `rules` is used by the validator.
    `token_usage` is used by API/CMS to display LLM usage.
    """

    rules: List[ParsedRule]
    token_usage: Optional[Dict[str, Any]] = None
    model: str = ""
    fallback_used: bool = False
    prompt_chars: int = 0
    raw_response_chars: int = 0

    def rule_strings(self) -> List[str]:
        """
        Return only clean rule strings.

        Useful when passing rules into validators or storing approved rules.
        """
        return [rule.rule for rule in self.rules]

    def to_dict(self, include_token_usage: bool = True) -> Dict[str, Any]:
        """
        Convert result into JSON-serialisable payload.

        By default, this includes token usage for standalone usage.

        Pipeline/API responses should pass include_token_usage=False
        so token_usage is exposed only once at the top level.
        """
        payload: Dict[str, Any] = {
            "rules": [
                {
                    "raw": rule.raw,
                    "rule": rule.rule,
                    "rule_type": rule.rule_type,
                }
                for rule in self.rules
            ],
            "rule_strings": self.rule_strings(),
            "prompt_chars": self.prompt_chars,
            "raw_response_chars": self.raw_response_chars,
        }

        if include_token_usage:
            payload["token_usage"] = self.token_usage
            payload["model"] = self.model
            payload["fallback_used"] = self.fallback_used

        return payload


def generate_rules(
    crawl_result: Dict[str, Any],
    prompt_template: str = "",
) -> List[ParsedRule]:
    """
    Backward-compatible wrapper.

    Existing workflow/validator code can keep using this function and receive
    only parsed rules, exactly like before.

    API/CMS should use generate_rules_with_metadata() when it needs token usage.
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
        2. Assemble prompt via prompt_builder.build_prompt().
        3. Call LLM via llm_client.call_llm_with_fallback().
        4. Capture model, fallback status, and token usage.
        5. Parse raw response into ParsedRule objects.
        6. Return rules + metadata for API/CMS.

    Args:
        crawl_result:    The full dict from CrawlService.crawl_url().
        prompt_template: Optional custom system prompt from CMS.
                         Uses DEFAULT_SYSTEM_PROMPT when empty.

    Returns:
        RuleGenerationResult containing:
            - rules
            - token_usage
            - model
            - fallback_used
            - prompt_chars
            - raw_response_chars
    """
    try:
        logger.info(
            "Starting AI rule generation for URL: %s",
            crawl_result.get("url"),
        )

        # 1. Extract and clean data down to compact signals to preserve token budget.
        compact_signals = _extract_signals(crawl_result)

        # 2. Assemble prompt.
        # If CMS does not provide a prompt template, use the default system prompt.
        system_template = (
            prompt_template.strip()
            if isinstance(prompt_template, str) and prompt_template.strip()
            else DEFAULT_SYSTEM_PROMPT
        )

        system_message, user_message = build_prompt(
            compact_signals,
            system_template,
        )

        prompt_chars = len(system_message or "") + len(user_message or "")

        logger.debug(
            "Rule generation prompt prepared | url=%s | prompt_chars=%s | third_party=%s | ad_candidates=%s",
            crawl_result.get("url"),
            prompt_chars,
            len(compact_signals.get("third_party", [])),
            len(compact_signals.get("ad_candidates", [])),
        )

        # 3. Call LLM.
        llm_response = call_llm_with_fallback(
            user_message,
            system_message=system_message,
        )

        token_usage = _build_token_usage_payload(llm_response)

        model = getattr(llm_response, "model", "") or (
            token_usage.get("model", "") if token_usage else ""
        )
        fallback_used = bool(getattr(llm_response, "fallback_used", False))

        # 4. Handle empty response while still returning metadata if available.
        if not llm_response.text:
            logger.warning("LLM client returned an empty response string.")

            return RuleGenerationResult(
                rules=[],
                token_usage=token_usage,
                model=model,
                fallback_used=fallback_used,
                prompt_chars=prompt_chars,
                raw_response_chars=0,
            )

        if token_usage:
            logger.info(
                "Rule generation token usage | model=%s | fallback_used=%s | prompt=%s | completion=%s | total=%s",
                token_usage["model"],
                token_usage["fallback_used"],
                token_usage["prompt_tokens"],
                token_usage["completion_tokens"],
                token_usage["total_tokens"],
            )
        else:
            logger.info(
                "Rule generation token usage unavailable | model=%s | fallback_used=%s",
                model,
                fallback_used,
            )

        # 5. Parse the plain text response into structured rule objects.
        parsed_rules = parse_llm_response(llm_response.text)

        logger.info(
            "Successfully generated %s candidate rules via AI orchestration.",
            len(parsed_rules),
        )

        # 6. Return candidate rules + metadata for API/CMS.
        return RuleGenerationResult(
            rules=parsed_rules,
            token_usage=token_usage,
            model=model,
            fallback_used=fallback_used,
            prompt_chars=prompt_chars,
            raw_response_chars=len(llm_response.text),
        )

    except Exception as exc:
        logger.error(
            "Failed to orchestrate AI rule generation pipeline: %s",
            str(exc),
            exc_info=True,
        )
        return RuleGenerationResult(rules=[])


def _build_token_usage_payload(
    llm_response: LLMResponse,
) -> Optional[Dict[str, Any]]:
    """
    Convert LLMResponse.usage into a JSON-serialisable dict for API/CMS.

    Returns None when the provider/API response does not include token usage.
    """
    usage = getattr(llm_response, "usage", None)

    if not usage:
        return None

    return {
        "model": getattr(llm_response, "model", ""),
        "fallback_used": bool(getattr(llm_response, "fallback_used", False)),
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def _extract_signals(crawl_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull only the fields the LLM needs from the full crawl result.

    Keeps:
        - url
        - title
        - third_party: domain, request_count, sample_paths
        - ad_candidates: category, confidence, suggested_rule, selector, reason,
          cleaned element_snippet

    Drops:
        - raw HTML
        - screenshot path
        - elapsed time
        - full URLs with query strings
        - heavy inline tracking/style payloads
    """
    signals: Dict[str, Any] = {
        "url": crawl_result.get("url", ""),
        "title": crawl_result.get("title", ""),
        "third_party": [],
        "ad_candidates": [],
    }

    # Extract cleaned third-party request metrics.
    # Supports both current and older crawl output formats.
    network_data = crawl_result.get("network_requests", {})
    third_party_raw = network_data.get("third_party", [])

    if isinstance(third_party_raw, list):
        for item in third_party_raw:
            if not isinstance(item, dict):
                continue

            domain = item.get("domain", "")
            if not domain:
                continue

            signals["third_party"].append(
                {
                    "domain": domain,
                    "request_count": item.get("request_count", 0),
                    "sample_paths": _clean_sample_paths(
                        item.get("sample_paths", []),
                    ),
                }
            )

    elif isinstance(third_party_raw, dict):
        # Old format:
        # third_party = {
        #   "count": 10,
        #   "by_domain": {
        #       "ads.example.com": ["https://ads.example.com/a.js?x=1"]
        #   }
        # }
        by_domain = third_party_raw.get("by_domain", {})

        if isinstance(by_domain, dict):
            for domain, urls in by_domain.items():
                if not domain:
                    continue

                paths = []
                if isinstance(urls, list):
                    paths = [_url_to_path(url) for url in urls if url]

                signals["third_party"].append(
                    {
                        "domain": domain,
                        "request_count": len(urls) if isinstance(urls, list) else 0,
                        "sample_paths": sorted(set(paths))[:3],
                    }
                )

    # Regex sanitizer:
    # Strip heavy inline attributes that can contain tracking IDs, encrypted payloads,
    # or long style blocks. This keeps prompt/token usage lower.
    cleanup_pattern = re.compile(
        r'\s+(?:ad-events|style|impression-id)\s*=\s*[\'"].*?[\'"]',
        re.IGNORECASE,
    )

    # Process and clean DOM ad candidates.
    for candidate in crawl_result.get("ad_candidates", []):
        if not isinstance(candidate, dict):
            continue

        raw_snippet = (
            candidate.get("element_snippet")
            or candidate.get("outer_html_snippet")
            or ""
        )

        clean_snippet = cleanup_pattern.sub("", raw_snippet or "")

        # Normalize trailing bracket if snippet was truncated awkwardly.
        if clean_snippet.startswith("<") and not clean_snippet.endswith(">"):
            clean_snippet += ">"

        signals["ad_candidates"].append(
            {
                "category": candidate.get("category", ""),
                "confidence": candidate.get("confidence", ""),
                "suggested_rule": candidate.get("suggested_rule", ""),
                "selector": candidate.get("selector", ""),
                "reason": candidate.get("reason", ""),
                "element_snippet": clean_snippet,
            }
        )

    return signals


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


def _url_to_path(value: str) -> str:
    """
    Convert a full URL or path-like value into a compact path without query string.
    """
    try:
        parsed = urlparse(value)

        # Full URL case.
        if parsed.scheme and parsed.netloc:
            return parsed.path or "/"

        # Already a path-like string.
        if value.startswith("/"):
            return value.split("?", 1)[0] or "/"

        return value.split("?", 1)[0] or "/"

    except Exception:
        return "/"
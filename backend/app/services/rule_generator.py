import logging
import re
from typing import Any, Dict, List

from ..ai.prompt_builder import build_prompt
from ..ai.llm_client import call_llm_with_fallback
from ..ai.rule_parser import parse_llm_response, ParsedRule

logger = logging.getLogger(__name__)


def generate_rules(crawl_result: Dict[str, Any], prompt_template: str = "") -> List[ParsedRule]:
    """
    Full AI rule generation flow for one crawl result.

    Steps:
        1. Build compact crawl signals from crawl_result (drop raw HTML, strip query strings).
        2. Assemble prompt via prompt_builder.build_prompt().
        3. Call LLM via llm_client.call_llm_with_fallback().
        4. Parse raw response into rule objects via rule_parser.parse_llm_response().
        5. Return the list of ParsedRule objects (not yet validated).

    Args:
        crawl_result:    The full dict from CrawlService.crawl_url().
        prompt_template: Optional custom system prompt from CMS. Uses default if empty.

    Returns:
        List of ParsedRule objects. May be empty if the LLM produced no usable output.
    """
    try:
        logger.info(f"Starting AI rule generation for URL: {crawl_result.get('url')}")
        
        # 1. Extract and clean data down to compact signals to preserve API token budget
        compact_signals = _extract_signals(crawl_result)
        
        # 2. Assemble prompt using the cleaned signals
        prompt = build_prompt(compact_signals, prompt_template)
        
        # 3. Request rule generation from the LLM client
        raw_response = call_llm_with_fallback(prompt)
        if not raw_response:
            logger.warning("LLM client returned an empty response string.")
            return []
            
        # 4. Parse the plain text response into structured rule objects
        parsed_rules = parse_llm_response(raw_response)
        logger.info(f"Successfully generated {len(parsed_rules)} candidate rules via AI orchestration.")
        
        # 5. Return candidate rules for verification
        return parsed_rules

    except Exception as e:
        logger.error(f"Failed to orchestrate AI rule generation pipeline: {str(e)}", exc_info=True)
        return []


def _extract_signals(crawl_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull only the fields the LLM needs from the full crawl result[cite: 2].
    Keeps the prompt small and avoids sending irrelevant data to the API[cite: 2].

    Keeps: url, title, third_party (domains + sample_paths), ad_candidates
           (category, confidence, suggested_rule, selector)[cite: 2].
    Drops: html_length, elapsed_ms, screenshot path, first_party_count,
           full URLs with query strings[cite: 2].
    """
    signals = {
        "url": crawl_result.get("url", ""),
        "title": crawl_result.get("title", ""),
        "third_party": [],
        "ad_candidates": []
    }

    # Extract cleaned third-party request metrics (relying on your grouped domain structural design)[cite: 2]
    network_data = crawl_result.get("network_requests", {})
    third_party_raw = network_data.get("third_party", [])
    
    for item in third_party_raw:
        signals["third_party"].append({
            "domain": item.get("domain", ""),
            "request_count": item.get("request_count", 0),
            "sample_paths": item.get("sample_paths", [])
        })

    # Regex parameter to match and strip tracking 'ad-events' attributes or heavy inline payload styles[cite: 2]
    # This prevents the LLM context window from drowning in encrypted tracking IDs[cite: 2].
    cleanup_pattern = re.compile(r'\s+(?:ad-events|style|impression-id)\s*=\s*[\'"].*?[\'"]', re.IGNORECASE)

    # Process and clean up DOM layout candidates[cite: 2]
    for candidate in crawl_result.get("ad_candidates", []):
        raw_snippet = candidate.get("element_snippet", "")
        
        # Apply the regex sanitizer to strip variable tracking blocks from layout snippets[cite: 2]
        clean_snippet = cleanup_pattern.sub("", raw_snippet)
        
        # Normalize trailing brackets if string slicing or stripping disrupted formatting tags
        if not clean_snippet.endswith(">") and clean_snippet.startswith("<"):
            clean_snippet += ">"

        signals["ad_candidates"].append({
            "category": candidate.get("category", ""),
            "confidence": candidate.get("confidence", ""),
            "suggested_rule": candidate.get("suggested_rule", ""),
            "selector": candidate.get("selector", ""),
            "element_snippet": clean_snippet
        })

    return signals
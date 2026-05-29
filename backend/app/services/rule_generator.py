# Orchestrates the AI rule generation step of the pipeline:
# extract compact signals from the crawl result (drop HTML, strip query strings)
# call prompt_builder.py to assemble the LLM prompt
# call llm_client.py to get the raw model response
# call rule_parser.py to parse response into structured rule objects
# return candidate rules to rule_validator.py for pre-testing
#
# Input:  crawl result dict from services/crawler.py + optional prompt template
# Output: list of ParsedRule objects passed to services/rule_validator.py

import logging
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
    raise NotImplementedError


def _extract_signals(crawl_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull only the fields the LLM needs from the full crawl result.
    Keeps the prompt small and avoids sending irrelevant data to the API.

    Keeps: url, title, third_party (domains + sample_paths), ad_candidates
           (category, confidence, suggested_rule, selector).
    Drops: html_length, elapsed_ms, screenshot path, first_party_count,
           full URLs with query strings.
    """
    raise NotImplementedError

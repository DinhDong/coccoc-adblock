# Assembles the prompt payload sent to the LLM:
# load system prompt template (default or from CMS prompt management)
# extract compact signals from crawl result (third-party domains, ad candidates)
# strip query strings and irrelevant fields to minimise token usage
# format signals into a structured user message
# return (system_message, user_message) tuple for llm_client.py
#
# Input:  crawl result dict from services/crawler.py + optional prompt template string
# Output: (system_message, user_message) tuple passed to llm_client.py

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Default prompt template used when no custom template is configured in the CMS.
DEFAULT_SYSTEM_PROMPT = """You are an AdBlock filter rule generator.
Given ad-related signals extracted from a webpage, produce valid ABP (Adblock Plus) filter rules.

Rules you may generate:
- Network blocking rules:  ||domain.com^  or  ||domain.com/path^
- Cosmetic hiding rules:   domain.com##.selector  or  domain.com###id
- Exception rules:         @@||domain.com^  (only when explicitly needed)

Output one rule per line. No explanations, no markdown, no blank lines.
"""


def build_prompt(crawl_signals: Dict[str, Any], template: str = DEFAULT_SYSTEM_PROMPT) -> tuple[str, str]:
    """
    Construct the (system_message, user_message) pair to send to the LLM.

    Args:
        crawl_signals: Compact dict from the crawl result — third-party domains,
                       ad_candidates, and title. Do NOT pass raw HTML or full URLs
                       with query strings; keep this small to save tokens.
        template:      The system prompt template (editable via CMS prompt management).

    Returns:
        Tuple of (system_message, user_message) ready to pass to llm_client.call_llm().

    Example crawl_signals shape:
        {
            "url": "https://example.com",
            "title": "Example News",
            "third_party": [
                {"domain": "ads.example.com", "request_count": 4, "sample_paths": ["/banner.js"]},
            ],
            "ad_candidates": [
                {"category": "ad_container", "confidence": "high",
                 "suggested_rule": "example.com##div.ad-slot", "selector": "div.ad-slot"},
            ],
        }
    """
    raise NotImplementedError

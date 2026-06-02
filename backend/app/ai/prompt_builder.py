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
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You are an AdBlock filter rule generator for the Coc Coc browser.
Given ad-related signals extracted from a domestic Vietnamese webpage, produce valid ABP (Adblock Plus) filter rules.

Rules you may generate:
  Network blocking:  ||domain.com^           blocks all requests to a domain
                     ||domain.com/path^       blocks a specific path
  Cosmetic hiding:   domain.com##.classname  hides an element by CSS selector
                     domain.com###element-id hides an element by ID
  Exception:         @@||domain.com^          only when a block rule would break the page

Guidelines:
- Prefer specific rules over broad ones. Never generate ||com^ or ||.^.
- For cosmetic rules, always include the target domain (e.g. site.vn##div.ad).
- Output one rule per line. No explanations, no markdown, no blank lines, no comments.\
"""


def build_prompt(
    crawl_signals: Dict[str, Any],
    template: str = DEFAULT_SYSTEM_PROMPT,
) -> tuple[str, str]:
    """
    Construct the (system_message, user_message) pair to send to the LLM.

    Args:
        crawl_signals: Compact dict built from the crawl result. Should contain:
                         url, title, third_party (list of domain entries),
                         and ad_candidates (list of detector findings).
                       Do NOT pass raw HTML, full URLs with query strings, or timestamps.
        template:      System prompt string. Defaults to DEFAULT_SYSTEM_PROMPT.
                       Can be overridden by a custom template stored in the CMS.

    Returns:
        (system_message, user_message) tuple — pass directly to llm_client.call_llm().
    """
    lines: List[str] = []

    # --- Page identity ---
    url = crawl_signals.get("url", "unknown")
    title = crawl_signals.get("title", "")
    lines.append(f"Target page: {url}")
    if title:
        lines.append(f"Page title: {title}")

    # --- Third-party network requests (ad networks, trackers) ---
    third_party: List[Dict] = crawl_signals.get("third_party", [])
    if third_party:
        lines.append("\nThird-party domains making network requests:")
        for entry in third_party:
            domain = entry.get("domain", "")
            count = entry.get("request_count", 0)
            paths = entry.get("sample_paths", [])
            path_str = ", ".join(paths) if paths else "/"
            lines.append(f"  {domain}  ({count} requests, sample paths: {path_str})")

    # --- Ad candidates from DOM/detector ---
    ad_candidates: List[Dict] = crawl_signals.get("ad_candidates", [])
    if ad_candidates:
        lines.append("\nAd-related elements detected in page DOM:")
        for candidate in ad_candidates:
            confidence = candidate.get("confidence", "")
            category = candidate.get("category", "")
            suggested = candidate.get("suggested_rule", "")
            selector = candidate.get("selector", "")
            reason = candidate.get("reason", "")

            if suggested:
                lines.append(f"  [{confidence}] {category} — suggested: {suggested}")
            elif selector:
                lines.append(f"  [{confidence}] {category} — selector: {selector} ({reason})")

    lines.append("\nGenerate ABP filter rules to block the ads identified above.")

    user_message = "\n".join(lines)
    logger.debug(f"Prompt built: {len(user_message)} chars, "
                 f"{len(third_party)} third-party domains, "
                 f"{len(ad_candidates)} ad candidates")

    return template, user_message


def extract_signals(crawl_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull only the fields the LLM needs from the full crawl result dict.
    Keeps the prompt compact and avoids sending irrelevant data to the API.

    Kept:    url, title, third_party (domain + request_count + sample_paths),
             ad_candidates (category, confidence, suggested_rule, selector, reason)
    Dropped: render metadata, html_length, elapsed_ms, screenshot path,
             first_party_count, raw HTML, full URLs with query strings.
    """
    compact_candidates = [
        {
            "category": c.get("category", ""),
            "confidence": c.get("confidence", ""),
            "suggested_rule": c.get("suggested_rule", ""),
            "selector": c.get("selector", ""),
            "reason": c.get("reason", ""),
        }
        for c in crawl_result.get("ad_candidates", [])
    ]

    return {
        "url": crawl_result.get("url", ""),
        "title": crawl_result.get("title", ""),
        "third_party": crawl_result.get("network_requests", {}).get("third_party", []),
        "ad_candidates": compact_candidates,
    }

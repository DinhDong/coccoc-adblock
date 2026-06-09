# Parses raw LLM text output into a clean list of rule strings:
# strip markdown code fences and prose lines
# remove comment lines (starting with !)
# drop blank or whitespace-only lines
# classify each kept line as network / cosmetic / exception rule
# return structured ParsedRule objects for the validator
#
# Input:  raw text string from llm_client.py
# Output: list of ParsedRule objects (used directly by services/rule_generator.py)

import logging
import re
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)

# Lines that look like ABP rules start with one of these patterns
_NETWORK_RE = re.compile(r"^(@@)?\|\|.+|^(@@)?[\w\*\-].*\^")
_COSMETIC_RE = re.compile(r"^[^#]*##|^[^#]*#@#")
_EXCEPTION_RE = re.compile(r"^@@")
# Markdown fence — drop the line entirely
_FENCE_RE = re.compile(r"^```")
# Comment lines in ABP syntax
_COMMENT_RE = re.compile(r"^[!#\[]")


@dataclass
class ParsedRule:
    """A single rule extracted from the LLM response."""
    raw: str        # Exactly as the LLM produced it (before any normalisation)
    rule: str       # Cleaned, normalised rule string
    rule_type: str  # "network", "cosmetic", "exception", or "unknown"


def parse_llm_response(raw_response: str) -> List[ParsedRule]:
    """
    Extract valid-looking ABP rule strings from raw LLM output text.

    Handles common LLM formatting noise:
    - Markdown code fences (``` ... ```)
    - Leading/trailing whitespace and inline backticks
    - Comment lines starting with ! or #
    - Explanatory prose lines that don't look like rules

    Args:
        raw_response: The full text string returned by llm_client.call_llm().

    Returns:
        List of ParsedRule objects. Empty list if the response contained no usable rules.
    """
    rules: List[ParsedRule] = []
    skipped = 0
    in_fence = False

    for raw_line in raw_response.splitlines():
        line = raw_line.strip()

        # Toggle code fence tracking; skip the fence markers themselves
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue

        # Skip blank lines
        if not line:
            continue

        # Strip inline backticks (e.g. `||ads.example.com^`)
        line = line.strip("`").strip()

        # Skip ABP comments and bracket directives (e.g. [Adblock Plus 2.0])
        if _COMMENT_RE.match(line):
            skipped += 1
            continue

        rule_type = classify_rule(line)

        if rule_type == "unknown":
            # Looks like prose — skip it
            skipped += 1
            logger.debug(f"Skipped non-rule line: {line!r}")
            continue

        rules.append(ParsedRule(raw=raw_line, rule=line, rule_type=rule_type))

    logger.debug(f"Parsed {len(rules)} rules, skipped {skipped} lines")
    return rules


def classify_rule(rule: str) -> str:
    """
    Return the rule type for a single cleaned rule string.

    Returns: "network" | "cosmetic" | "exception" | "unknown"
    """
    if not rule:
        return "unknown"

    # Exception rules start with @@ (check before network so @@||... is caught)
    if _EXCEPTION_RE.match(rule):
        return "exception"

    # Cosmetic rules contain ## or #@# separator
    if _COSMETIC_RE.search(rule):
        return "cosmetic"

    # Network rules: ||pattern or plain URL patterns ending with ^
    if _NETWORK_RE.match(rule):
        return "network"

    return "unknown"

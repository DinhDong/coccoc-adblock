# Parses raw LLM text output into a clean list of rule strings:
# strip markdown code fences and prose lines
# remove comment lines (starting with !)
# drop blank or whitespace-only lines
# classify each kept line as network / cosmetic / exception rule
# return structured ParsedRule objects for the validator
#
# Input:  raw text string from llm_client.py
# Output: ParseResult containing a list of ParsedRule objects

import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class ParsedRule:
    """A single rule extracted from the LLM response."""
    raw: str           # Exactly as the LLM produced it (before any normalisation)
    rule: str          # Cleaned, normalised rule string
    rule_type: str     # "network", "cosmetic", "exception", or "unknown"


@dataclass
class ParseResult:
    """Full output of parse_llm_response()."""
    rules: List[ParsedRule] = field(default_factory=list)
    skipped_lines: List[str] = field(default_factory=list)  # Lines that were dropped


def parse_llm_response(raw_response: str) -> ParseResult:
    """
    Extract valid-looking ABP rule strings from raw LLM output text.

    Handles common LLM formatting noise:
    - Markdown code fences (``` ... ```)
    - Leading/trailing whitespace
    - Comment lines starting with !
    - Explanatory prose lines that don't look like rules

    Each kept line is classified into rule_type so downstream validators
    know what kind of check to apply.

    Args:
        raw_response: The full text string returned by llm_client.call_llm().

    Returns:
        ParseResult with a list of ParsedRule objects and any skipped lines.
    """
    raise NotImplementedError


def classify_rule(rule: str) -> str:
    """
    Return the rule type for a single cleaned rule string.

    Returns: "network" | "cosmetic" | "exception" | "unknown"
    """
    raise NotImplementedError

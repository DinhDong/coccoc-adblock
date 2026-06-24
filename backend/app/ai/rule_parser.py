# Parses raw LLM text output into a clean list of rule strings:
# strip markdown code fences and prose lines
# remove comment lines
# drop blank or whitespace-only lines
# strip bullets/numbering if the LLM accidentally adds them
# normalize small ABP formatting issues before validation
# classify each kept line as network / cosmetic / exception rule
# return structured ParsedRule objects for the validator
#
# Input:  raw text string from llm_client.py
# Output: list of ParsedRule objects (used directly by services/rule_generator.py)

import logging
import re
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

# Lines that look like ABP rules start with one of these patterns.
_NETWORK_RE = re.compile(r"^(@@)?\|\|.+|^(@@)?[\w\*\-./:]+.*")
_COSMETIC_RE = re.compile(r"^[^#]*##|^[^#]*#@#")
_EXCEPTION_RE = re.compile(r"^@@")

# Markdown fence — drop the line entirely.
_FENCE_RE = re.compile(r"^```")

# Common accidental LLM list prefixes:
#   - ||ads.example.com^
#   * ||ads.example.com^
#   1. ||ads.example.com^
#   1) ||ads.example.com^
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*]\s+|\d+[\.)]\s+)")

# ABP comments / header directives.
_ABP_HEADER_RE = re.compile(r"^\[.*\]$")
_ABP_COMMENT_RE = re.compile(r"^!")

# Network rule with options, used to normalize:
#   @@||example.com/image.jpg$image,domain=example.com
# into:
#   @@||example.com/image.jpg^$image,domain=example.com
_NETWORK_WITH_OPTIONS_RE = re.compile(r"^(@@)?(.+?)(\$[A-Za-z~][A-Za-z0-9_~=-]*(?:,.*)?)$")


@dataclass
class ParsedRule:
    """A single rule extracted from the LLM response."""
    raw: str        # Exactly as the LLM produced it, before normalization
    rule: str       # Cleaned, normalized rule string
    rule_type: str  # "network", "cosmetic", "exception", or "unknown"


def parse_llm_response(raw_response: str) -> List[ParsedRule]:
    """
    Extract valid-looking ABP rule strings from raw LLM output text.

    Handles common LLM formatting noise:
    - Markdown code fences
    - Leading/trailing whitespace and inline backticks
    - Bullet or numbered list prefixes
    - Comment/header lines
    - Explanatory prose lines that do not look like rules
    - Small network option formatting issues

    Args:
        raw_response: The full text string returned by llm_client.call_llm().

    Returns:
        List of ParsedRule objects. Empty list if the response contained no usable rules.
    """
    if not raw_response:
        return []

    rules: List[ParsedRule] = []
    seen: set[str] = set()
    skipped = 0
    in_fence = False

    for raw_line in raw_response.splitlines():
        line = raw_line.strip()

        # Toggle code fence tracking; skip the fence markers themselves.
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue

        # We still parse inside code fences because LLMs often put valid rules
        # inside fenced blocks. Only the fence marker lines are skipped.

        if not line:
            continue

        cleaned = clean_rule_line(line)

        if not cleaned:
            skipped += 1
            continue

        if is_comment_or_header(cleaned):
            skipped += 1
            continue

        normalized = normalize_rule(cleaned)
        rule_type = classify_rule(normalized)

        if rule_type == "unknown":
            skipped += 1
            logger.debug("Skipped non-rule line: %r", line)
            continue

        if normalized in seen:
            logger.debug("Skipped duplicate rule: %r", normalized)
            continue

        seen.add(normalized)
        rules.append(
            ParsedRule(
                raw=raw_line,
                rule=normalized,
                rule_type=rule_type,
            )
        )

    logger.debug("Parsed %d rules, skipped %d lines", len(rules), skipped)
    return rules


def clean_rule_line(line: str) -> str:
    """
    Remove common formatting around a rule line.
    """
    if not line:
        return ""

    cleaned = line.strip()

    # Strip inline code markup:
    #   `||ads.example.com^`
    cleaned = cleaned.strip("`").strip()

    # Strip markdown/list prefixes:
    #   - ||ads.example.com^
    #   1. ||ads.example.com^
    cleaned = _LIST_PREFIX_RE.sub("", cleaned).strip()

    # Strip quotes if a JSON-ish or markdown-ish output wraps a rule:
    #   "||ads.example.com^"
    #   '||ads.example.com^'
    if (
        len(cleaned) >= 2
        and cleaned[0] == cleaned[-1]
        and cleaned[0] in {"'", '"'}
    ):
        cleaned = cleaned[1:-1].strip()

    # Strip trailing commas from JSON-like output:
    #   "||ads.example.com^",
    if cleaned.endswith(","):
        cleaned = cleaned[:-1].strip()

    # Strip inline code markup again after removing comma/quotes.
    cleaned = cleaned.strip("`").strip()

    return cleaned


def is_comment_or_header(line: str) -> bool:
    """
    Return True for ABP comments/header directives.

    Important:
    - Do not treat lines starting with ## as comments because ##.ad is a
      valid cosmetic rule.
    - Do not treat #@# as comments because it is a cosmetic exception rule.
    """
    if not line:
        return True

    if _ABP_COMMENT_RE.match(line):
        return True

    if _ABP_HEADER_RE.match(line):
        return True

    # Some LLMs may output "# comment". Skip that, but keep cosmetic rules.
    if line.startswith("#") and not line.startswith(("##", "#@#")):
        return True

    return False


def normalize_rule(rule: str) -> str:
    """
    Normalize small ABP formatting issues before validation.

    This function is intentionally conservative:
    - It does not rewrite broad rules into narrow rules.
    - It does not invent domains/selectors.
    - It only fixes common formatting noise.
    """
    normalized = rule.strip()

    # Remove spaces around ABP cosmetic separators.
    normalized = normalized.replace(" #@# ", "#@#")
    normalized = normalized.replace(" ## ", "##")

    # Remove spaces around network option separator.
    normalized = re.sub(r"\s+\$", "$", normalized)
    normalized = re.sub(r"\$\s+", "$", normalized)

    # Fix common LLM mistake:
    #   @@||cdn.example.com/image.jpg$image,domain=site.com
    # should be:
    #   @@||cdn.example.com/image.jpg^$image,domain=site.com
    normalized = normalize_network_option_separator(normalized)

    return normalized


def normalize_network_option_separator(rule: str) -> str:
    """
    Add ^ before $ for anchored network rules with options when the LLM omits it.

    Examples:
        @@||example.com/img.jpg$image,domain=site.com
        -> @@||example.com/img.jpg^$image,domain=site.com

        ||ads.example.com/banner$script,domain=site.com
        -> ||ads.example.com/banner^$script,domain=site.com

    Does not modify cosmetic rules or wildcard patterns ending in *.
    """
    if "##" in rule or "#@#" in rule:
        return rule

    if "$" not in rule:
        return rule

    match = _NETWORK_WITH_OPTIONS_RE.match(rule)
    if not match:
        return rule

    exception_prefix = match.group(1) or ""
    pattern = match.group(2)
    options = match.group(3)

    if not pattern:
        return rule

    # Only normalize anchored network rules. Avoid changing arbitrary regex-like
    # or path-only rules in surprising ways.
    raw_pattern = pattern[2:] if pattern.startswith("@@") else pattern

    if not raw_pattern.startswith("||"):
        return rule

    if pattern.endswith("^") or pattern.endswith("*"):
        return rule

    return f"{exception_prefix}{pattern}^{options}"


def classify_rule(rule: str) -> str:
    """
    Return the rule type for a single cleaned rule string.

    Returns:
        "network" | "cosmetic" | "exception" | "unknown"
    """
    if not rule:
        return "unknown"

    # Cosmetic exception uses #@#, not @@.
    if _COSMETIC_RE.search(rule):
        return "cosmetic"

    # Exception rules start with @@.
    if _EXCEPTION_RE.match(rule):
        return "exception"

    # Network rules: anchored rules, URL/path rules, or simple patterns.
    if _NETWORK_RE.match(rule):
        return "network"

    return "unknown"
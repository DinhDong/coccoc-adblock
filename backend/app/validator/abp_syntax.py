# Stage 1 of rule validation — ABP syntax check:
# validate network rule URL patterns and option flags ($script, $third-party, etc.)
# validate cosmetic rule format (## or #@# separator + non-empty CSS selector)
# validate exception rule prefix (@@) and pattern
# check domain= option lists are well-formed
# reject empty or whitespace-only strings
#
# Input:  list of rule strings from rule_parser.py
# Output: list of SyntaxResult objects (valid: bool, error: str)
# Note:   runs before rule_scope.py — no browser required

import re
from dataclasses import dataclass
from typing import Optional

# Valid ABP network rule option names (subset; extend as needed)
VALID_NETWORK_OPTIONS = {
    "script", "image", "stylesheet", "object", "xmlhttprequest", "xhr",
    "subdocument", "document", "websocket", "webrtc", "ping", "font",
    "media", "other", "third-party", "first-party", "important",
    "domain", "sitekey", "match-case", "collapse", "popup",
}


@dataclass
class SyntaxResult:
    rule: str
    valid: bool
    error: Optional[str] = None


def check_syntax(rule: str) -> SyntaxResult:
    """
    Validate a single ABP rule string for correct syntax.

    Covers:
    - Network rules: ||pattern^  or  ||pattern^$options
      - Pattern must not be empty or contain illegal characters
      - $options must only contain known option names and valid domain= lists
    - Cosmetic rules: [domains]##selector  or  [domains]#@#selector
      - Separator must be ## or #@#
      - CSS selector after separator must be non-empty
    - Exception rules: @@||pattern^  (same checks as network after stripping @@)
    - Rejects empty or whitespace-only strings

    Returns:
        SyntaxResult with valid=True or valid=False + a short error description.
    """
    raise NotImplementedError


def check_syntax_batch(rules: list[str]) -> list[SyntaxResult]:
    """Run check_syntax on a list of rules and return results in the same order."""
    return [check_syntax(r) for r in rules]


def _validate_network_options(options_str: str) -> Optional[str]:
    """
    Parse the options portion of a network rule (the part after $).
    Returns an error string if invalid, or None if valid.
    """
    raise NotImplementedError


def _validate_css_selector(selector: str) -> Optional[str]:
    """
    Light check that a cosmetic rule's CSS selector is non-empty and
    doesn't contain obvious syntax errors.
    Returns an error string if invalid, or None if valid.
    """
    raise NotImplementedError

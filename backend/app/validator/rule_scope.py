# Stage 2 of rule validation — rule scope check:
# detect overly broad network rules (pattern too short, matches common URL fragments)
# detect cosmetic rules with no domain scope applied to generic selectors (div, span, a...)
# detect common element risk (bare tag selectors with no class/ID qualifier)
# detect exception rules that conflict with block rules in the same batch
#
# Input:  list of rule strings that passed abp_syntax.py
# Output: list of ScopeResult objects (safe: bool, risk: str)
# Note:   runs after abp_syntax.py, before sandbox_check.py — no browser required

from dataclasses import dataclass
from typing import Literal, Optional

RiskType = Literal["overly_broad", "missing_scope", "common_element", None]

# Selectors that are too generic to be safe without a domain qualifier
GENERIC_SELECTORS = {"div", "span", "a", "img", "p", "section", "article", "ul", "li"}

# Network patterns short enough to match huge swaths of URLs unintentionally
OVERLY_BROAD_PATTERNS = {"/", ".", "com", "net", "org", "http", "https", "www"}


@dataclass
class ScopeResult:
    rule: str
    safe: bool
    risk: RiskType = None
    detail: Optional[str] = None


def check_scope(rule: str) -> ScopeResult:
    """
    Determine whether a rule's matching scope is acceptably narrow.

    Checks (in order):
    1. Overly broad network rule — pattern is too short or matches common URL fragments.
       e.g. "||com^" or "||.^" would match almost everything.
    2. Missing domain scope on cosmetic rule — a ##selector with no domain prefix
       and a generic tag selector (div, span, a, img, ...) risks hiding legitimate content
       on unintended pages.
    3. Common element risk — cosmetic rule targets a bare generic tag with no class/ID/
       attribute qualifier. e.g. "example.com##div" is risky; "example.com##div.ad" is fine.
    4. Exception rule conflict — @@||domain^ that would whitelist a domain used in other
       block rules in the same batch (checked at batch level, not per-rule).

    Returns:
        ScopeResult with safe=True if no risk detected, or safe=False + risk type.
    """
    raise NotImplementedError


def check_scope_batch(rules: list[str]) -> list[ScopeResult]:
    """
    Run check_scope on each rule, then add a cross-rule pass to flag
    @@exception rules that conflict with block rules in the same batch.
    """
    raise NotImplementedError

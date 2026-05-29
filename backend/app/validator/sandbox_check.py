# Stage 3 of rule validation — live browser sandbox test:
# load page without rules (baseline) → screenshot + DOM snapshot
# apply network rules via page.route() to abort matching requests
# apply cosmetic rules by injecting a <style> block into the page
# load page with rules applied → re-screenshot + re-inspect DOM
# verify targeted ad elements are removed or requests are blocked
# verify critical page elements (nav, content, controls) still present
# compare screenshots to detect large unintended layout changes
#
# Input:  rule strings that passed stages 1 and 2 + original page URL
# Output: SandboxResult (ads_blocked, page_functional, layout_diff_pct, broken_selectors)
# Note:   most expensive stage — only runs after abp_syntax.py and rule_scope.py pass

import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class SandboxResult:
    """Result of testing one set of rules against a live page."""
    url: str
    passed: bool                          # True only if ads_blocked AND page_functional
    ads_blocked: bool = False             # All targeted ad elements were removed/blocked
    page_functional: bool = False         # Navigation, content, and key controls still present
    layout_diff_pct: float = 0.0          # Rough pixel-diff fraction between baseline and with-rules
    blocked_requests: List[str] = field(default_factory=list)   # Third-party URLs that were blocked
    missing_ad_selectors: List[str] = field(default_factory=list)  # Selectors that disappeared (good)
    broken_selectors: List[str] = field(default_factory=list)   # Non-ad selectors that disappeared (bad)
    error: str = ""


def run_sandbox(url: str, rules: List[str]) -> SandboxResult:
    """
    Test a set of candidate ABP rules against the live page.

    Steps:
        1. Load page WITHOUT rules (baseline):
           - Screenshot the full page
           - Record which ad-candidate selectors are present in the DOM
           - Record which third-party domains make network requests

        2. Load page WITH rules applied:
           - Network rules: intercept via page.route() and abort matching requests
           - Cosmetic rules: inject a <style> tag that sets display:none on matched selectors
           - Re-screenshot and re-inspect DOM

        3. Compare baseline vs. with-rules:
           - ads_blocked: targeted selectors gone OR targeted requests aborted
           - page_functional: critical nav/content selectors still present,
             layout_diff_pct below threshold (< 40% of pixels changed)
           - broken_selectors: non-ad selectors that disappeared (false positives)

    Args:
        url:   The original reported page URL.
        rules: List of rule strings that passed stages 1 and 2.

    Returns:
        SandboxResult. passed=True only if ads_blocked=True AND page_functional=True.
    """
    raise NotImplementedError


def _apply_network_rules(page, network_rules: List[str]) -> None:
    """
    Register page.route() handlers that abort requests matching the given
    network blocking rules. Cosmetic rules are skipped here.
    """
    raise NotImplementedError


def _apply_cosmetic_rules(page, cosmetic_rules: List[str]) -> None:
    """
    Inject a <style> block into the page that hides elements matching
    the CSS selectors from cosmetic rules.
    """
    raise NotImplementedError


def _screenshot_diff(baseline: bytes, with_rules: bytes) -> float:
    """
    Return the fraction of pixels that differ between two screenshot byte blobs.
    Used to detect large unintended layout changes (threshold: > 0.40 = fail).
    """
    raise NotImplementedError

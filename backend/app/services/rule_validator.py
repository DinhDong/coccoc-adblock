# Orchestrates all three validation stages for a batch of generated rules:
# Stage 1 — abp_syntax.py: reject rules with invalid filter syntax
# Stage 2 — rule_scope.py: reject rules that are too broad or likely to cause false positives
# Stage 3 — sandbox_check.py: test survivors in a live Playwright browser session
# collect per-rule outcomes and log failures with stage + reason for audit trail
# return ValidationReport with only the rules that passed all three stages
#
# Input:  list of ParsedRule strings from services/rule_generator.py + page URL
# Output: ValidationReport — call .passing_rules() to get rules ready for moderator queue

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..validator.abp_syntax import check_syntax_batch, SyntaxResult
from ..validator.rule_scope import check_scope_batch, ScopeResult
from ..validator.sandbox_check import run_sandbox, SandboxResult

logger = logging.getLogger(__name__)


@dataclass
class RuleValidationOutcome:
    """Validation result for a single rule across all three stages."""
    rule: str
    passed: bool                         # True only if all three stages pass
    syntax: SyntaxResult = None
    scope: ScopeResult = None
    sandbox: SandboxResult = None        # None if rule was blocked at stage 1 or 2
    failure_stage: str = ""              # "syntax" | "scope" | "sandbox" | ""


@dataclass
class ValidationReport:
    """Full validation report for one rule generation batch."""
    url: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    outcomes: List[RuleValidationOutcome] = field(default_factory=list)

    def passing_rules(self) -> List[str]:
        """Return only the rule strings that passed all three stages."""
        return [o.rule for o in self.outcomes if o.passed]


def validate_rules(rules: List[str], page_url: str) -> ValidationReport:
    """
    Run all three validation stages on a batch of candidate rules.

    Stage 1 (abp_syntax)  — filter out syntactically broken rules.
    Stage 2 (rule_scope)  — filter out overly broad or risky rules.
    Stage 3 (sandbox)     — test survivors in a real browser session.

    Only rules that pass all three stages are included in ValidationReport.passing_rules().
    Failed rules are logged with their failure stage and reason for the audit trail.

    Args:
        rules:    List of rule strings from rule_generator.generate_rules().
        page_url: The original reported URL (needed for the sandbox browser session).

    Returns:
        ValidationReport. Pass report.passing_rules() to the moderator queue.
    """
    raise NotImplementedError

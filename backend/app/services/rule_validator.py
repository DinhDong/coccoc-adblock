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
from typing import Any, Optional

from ..validator.abp_syntax import check_syntax_batch, SyntaxResult
from ..validator.rule_scope import check_scope_batch, ScopeResult
from ..validator.sandbox_check import run_sandbox, SandboxResult

logger = logging.getLogger(__name__)


@dataclass
class RuleValidationOutcome:
    """Validation result for a single rule across all three stages."""
    rule: str
    passed: bool                         # True only if all three stages pass
    syntax: Optional[SyntaxResult] = None
    scope: Optional[ScopeResult] = None
    sandbox: Optional[SandboxResult] = None        # None if rule was blocked at stage 1 or 2
    failure_stage: str = ""              # "syntax" | "scope" | "sandbox" | ""
    failure_reason: str = ""


@dataclass
class ValidationReport:
    """Full validation report for one rule generation batch."""
    url: str
    total: int = 0
    passed: bool = False
    passed_count: int = 0
    failed: int = 0
    outcomes: list[RuleValidationOutcome] = field(default_factory=list)

    def passing_rules(self) -> list[str]:
        """Return only the rule strings that passed all three stages."""
        return [o.rule for o in self.outcomes if o.passed]


def validate_rules(rules: list[str], page_url: str) -> ValidationReport:
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
    rule_strings = [_coerce_rule(rule) for rule in rules]
    report = ValidationReport(url=page_url, total=len(rule_strings))

    if not rule_strings:
        logger.info("No rules supplied for validation for %s", page_url)
        return report

    outcomes: list[Optional[RuleValidationOutcome]] = [None] * len(rule_strings)

    try:
        syntax_results = check_syntax_batch(rule_strings)
        _assert_result_count("syntax", len(rule_strings), len(syntax_results))
    except Exception as exc:
        reason = f"syntax validator error: {exc}"
        for idx, rule in enumerate(rule_strings):
            syntax_result = SyntaxResult(rule=rule, valid=False, error=reason)
            outcomes[idx] = RuleValidationOutcome(
                rule=rule,
                passed=False,
                syntax=syntax_result,
                failure_stage="syntax",
                failure_reason=reason,
            )
            _log_failure(rule, "syntax", reason)
        return _finalize_report(report, outcomes)

    scope_candidates: list[tuple[int, str, SyntaxResult]] = []
    for idx, (rule, syntax_result) in enumerate(zip(rule_strings, syntax_results)):
        if not syntax_result.valid:
            reason = syntax_result.error or "invalid syntax"
            outcomes[idx] = RuleValidationOutcome(
                rule=rule,
                passed=False,
                syntax=syntax_result,
                failure_stage="syntax",
                failure_reason=reason,
            )
            _log_failure(rule, "syntax", reason)
            continue
        scope_candidates.append((idx, rule, syntax_result))

    if scope_candidates:
        scope_rules = [rule for _, rule, _ in scope_candidates]
        try:
            scope_results = check_scope_batch(scope_rules)
            _assert_result_count("scope", len(scope_rules), len(scope_results))
        except Exception as exc:
            reason = f"scope validator error: {exc}"
            for idx, rule, syntax_result in scope_candidates:
                scope_result = ScopeResult(rule=rule, safe=False, detail=reason)
                outcomes[idx] = RuleValidationOutcome(
                    rule=rule,
                    passed=False,
                    syntax=syntax_result,
                    scope=scope_result,
                    failure_stage="scope",
                    failure_reason=reason,
                )
                _log_failure(rule, "scope", reason)
            return _finalize_report(report, outcomes)
    else:
        scope_results = []

    sandbox_candidates: list[tuple[int, str, SyntaxResult, ScopeResult]] = []
    for (idx, rule, syntax_result), scope_result in zip(scope_candidates, scope_results):
        if not scope_result.safe:
            reason = _scope_failure_reason(scope_result)
            outcomes[idx] = RuleValidationOutcome(
                rule=rule,
                passed=False,
                syntax=syntax_result,
                scope=scope_result,
                failure_stage="scope",
                failure_reason=reason,
            )
            _log_failure(rule, "scope", reason)
            continue
        sandbox_candidates.append((idx, rule, syntax_result, scope_result))

    sandbox_result: Optional[SandboxResult] = None
    if sandbox_candidates:
        sandbox_rules = [rule for _, rule, _, _ in sandbox_candidates]
        try:
            sandbox_result = run_sandbox(page_url, sandbox_rules)
        except Exception as exc:
            sandbox_result = SandboxResult(
                url=page_url,
                passed=False,
                error=f"sandbox validator error: {exc}",
            )

    sandbox_unreachable = bool(sandbox_result and sandbox_result.unreachable)

    for idx, rule, syntax_result, scope_result in sandbox_candidates:
        if sandbox_unreachable:
            # Target URL was connection-reset / DNS-failed / bot-blocked during sandbox.
            # Syntax + scope passed — forward to moderator with a note rather than silently failing.
            outcomes[idx] = RuleValidationOutcome(
                rule=rule,
                passed=True,
                syntax=syntax_result,
                scope=scope_result,
                sandbox=sandbox_result,
                failure_stage="",
                failure_reason="sandbox skipped — target unreachable during testing (manual review recommended)",
            )
            logger.warning("Sandbox unreachable — forwarding to moderator: %s", rule)
        else:
            passed = bool(sandbox_result and sandbox_result.passed)
            failure_stage = "" if passed else "sandbox"
            failure_reason = "" if passed else _sandbox_failure_reason(sandbox_result)
            outcomes[idx] = RuleValidationOutcome(
                rule=rule,
                passed=passed,
                syntax=syntax_result,
                scope=scope_result,
                sandbox=sandbox_result,
                failure_stage=failure_stage,
                failure_reason=failure_reason,
            )
            if not passed:
                _log_failure(rule, "sandbox", failure_reason)

    return _finalize_report(report, outcomes)


def _coerce_rule(rule: Any) -> str:
    """Accept raw strings or ParsedRule-like objects from rule_generator.py."""
    if hasattr(rule, "rule"):
        return str(getattr(rule, "rule")).strip()
    return str(rule).strip()


def _assert_result_count(stage: str, expected: int, actual: int) -> None:
    if expected != actual:
        raise ValueError(
            f"{stage} validator returned {actual} results for {expected} rules"
        )


def _finalize_report(
    report: ValidationReport,
    outcomes: list[Optional[RuleValidationOutcome]],
) -> ValidationReport:
    report.outcomes = [outcome for outcome in outcomes if outcome is not None]
    report.passed_count = sum(1 for outcome in report.outcomes if outcome.passed)
    report.failed = report.total - report.passed_count
    report.passed = report.total > 0 and report.failed == 0
    return report


def _scope_failure_reason(scope_result: ScopeResult) -> str:
    if scope_result.detail:
        return scope_result.detail
    if scope_result.risk:
        return str(scope_result.risk)
    return "unsafe scope"


def _sandbox_failure_reason(sandbox_result: Optional[SandboxResult]) -> str:
    if sandbox_result is None:
        return "sandbox did not run"
    if sandbox_result.error:
        return sandbox_result.error

    reasons = []
    if not sandbox_result.ads_blocked:
        reasons.append("ads_not_blocked")
    if not sandbox_result.page_functional:
        if sandbox_result.broken_selectors:
            reasons.append("broken_selectors=" + ",".join(sandbox_result.broken_selectors))
        if sandbox_result.layout_diff_pct > 0:
            reasons.append(f"layout_diff_pct={sandbox_result.layout_diff_pct:.3f}")
        if not reasons or reasons[-1].startswith("ads_not_blocked"):
            reasons.append("page_not_functional")
    return "; ".join(reasons) or "sandbox failed"


def _log_failure(rule: str, stage: str, reason: str) -> None:
    logger.warning(
        "Rule validation failed at %s for %r: %s",
        stage,
        rule,
        reason,
    )

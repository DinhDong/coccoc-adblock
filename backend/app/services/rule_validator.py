# Orchestrates validation stages for generated ABP rules:
# Stage 1 — abp_syntax.py: reject rules with invalid filter syntax
# Stage 2 — rule_scope.py: reject rules that are too broad or likely to cause false positives
# Stage 3 — problem_policy.py: reject rules with the wrong direction for the ticket strategy
# Stage 4 — sandbox_check.py: test each surviving rule in a live Playwright browser session
#
# This validator is ticket-aware:
# - visible ad / legacy mode      -> rule must block or hide an ad target
# - content breakage              -> exception rules are valid; ads_blocked is not required
# - hidden UI                     -> cosmetic exception rules are valid; ads_blocked is not required
# - overlay / anti-adblock issue  -> hide/block overlay, or allow a required resource if directly evidenced
#
# Input:  list of ParsedRule strings from services/rule_generator.py + page URL + ticket_context
# Output: ValidationReport — call .passing_rules() to get rules ready for moderator queue

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app.services.problem_policy import (
    LEGACY_DEFAULT_PROBLEM_TYPE,
    RULE_COSMETIC_HIDE,
    RULE_NETWORK_BLOCK,
    STRATEGY_ALLOW_REQUIRED_CONTENT,
    STRATEGY_BLOCK_VISIBLE_AD,
    STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE,
    STRATEGY_RESTORE_HIDDEN_UI,
    classify_rule_direction,
    get_problem_policy,
    get_resolution_strategy,
    get_rule_direction_error,
    is_rule_direction_allowed,
    normalize_problem_type,
)
from app.validator.abp_syntax import SyntaxResult, check_syntax_batch
from app.validator.rule_scope import ScopeResult, check_scope_batch
from app.validator.sandbox_check import SandboxResult, run_sandbox

logger = logging.getLogger(__name__)


@dataclass
class PolicyResult:
    """Ticket/problem-policy validation result for one rule."""
    rule: str
    valid: bool
    problem_type: str
    resolution_strategy: str
    rule_direction: str
    error: Optional[str] = None


@dataclass
class RuleValidationOutcome:
    """Validation result for a single rule across all stages."""
    rule: str
    passed: bool
    syntax: Optional[SyntaxResult] = None
    scope: Optional[ScopeResult] = None
    policy: Optional[PolicyResult] = None
    sandbox: Optional[SandboxResult] = None
    failure_stage: str = ""  # "syntax" | "scope" | "policy" | "sandbox" | ""
    failure_reason: str = ""


@dataclass
class ValidationReport:
    """Full validation report for one rule generation batch."""
    url: str
    problem_type: str = "unknown"
    resolution_strategy: str = "unknown"
    ticket_context: dict[str, Any] = field(default_factory=dict)
    total: int = 0
    passed: bool = False
    passed_count: int = 0
    failed: int = 0
    outcomes: list[RuleValidationOutcome] = field(default_factory=list)

    def passing_rules(self) -> list[str]:
        """Return only the rule strings that passed all validation stages."""
        return [outcome.rule for outcome in self.outcomes if outcome.passed]


def validate_rules(
    rules: list[Any],
    page_url: str,
    ticket_context: Optional[Mapping[str, Any]] = None,
    run_sandbox_checks: bool = True,
    problem_type: Optional[str] = None,
    **kwargs: Any,
) -> ValidationReport:
    """
    Run validation stages on candidate rules.

    Stages:
      1. ABP syntax validation.
      2. Scope / broadness validation.
      3. Ticket policy direction validation.
      4. Per-rule sandbox validation.

    Args:
        rules:
            List of rule strings or ParsedRule-like objects.
        page_url:
            Original reported URL.
        ticket_context:
            Normalized ticket context from ticket_context.py.
        run_sandbox_checks:
            If False, skip live browser sandbox and pass rules that reached stage 3.
        problem_type:
            Optional override if caller cannot pass full ticket_context.

    Supported compatibility kwargs:
        run_sandbox=True/False
        use_sandbox=True/False
        skip_sandbox=True/False

    Returns:
        ValidationReport.
    """
    if "run_sandbox" in kwargs:
        run_sandbox_checks = bool(kwargs["run_sandbox"])

    if "use_sandbox" in kwargs:
        run_sandbox_checks = bool(kwargs["use_sandbox"])

    if "skip_sandbox" in kwargs:
        run_sandbox_checks = not bool(kwargs["skip_sandbox"])

    context = _normalize_context(ticket_context)
    resolved_problem_type = _resolve_problem_type(context, problem_type)
    resolution_strategy = context.get(
        "resolution_strategy",
        get_resolution_strategy(resolved_problem_type),
    )

    rule_strings = [
        _coerce_rule(rule)
        for rule in rules
    ]
    rule_strings = [
        rule
        for rule in rule_strings
        if rule
    ]

    report = ValidationReport(
        url=page_url,
        problem_type=resolved_problem_type,
        resolution_strategy=str(resolution_strategy),
        ticket_context=context,
        total=len(rule_strings),
    )

    if not rule_strings:
        logger.info("No rules supplied for validation for %s", page_url)
        return report

    logger.info(
        "Starting rule validation | url=%s | rules=%d | problem_type=%s | strategy=%s | sandbox=%s",
        page_url,
        len(rule_strings),
        resolved_problem_type,
        resolution_strategy,
        run_sandbox_checks,
    )

    outcomes: list[Optional[RuleValidationOutcome]] = [None] * len(rule_strings)

    # ============================================================
    # Stage 1: Syntax
    # ============================================================

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

    # ============================================================
    # Stage 2: Scope
    # ============================================================

    if scope_candidates:
        scope_rules = [
            rule
            for _, rule, _ in scope_candidates
        ]

        try:
            scope_results = check_scope_batch(scope_rules)
            _assert_result_count("scope", len(scope_rules), len(scope_results))
        except Exception as exc:
            reason = f"scope validator error: {exc}"

            for idx, rule, syntax_result in scope_candidates:
                scope_result = ScopeResult(
                    rule=rule,
                    safe=False,
                    detail=reason,
                )
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

    policy_candidates: list[
        tuple[int, str, SyntaxResult, ScopeResult]
    ] = []

    for (idx, rule, syntax_result), scope_result in zip(
        scope_candidates,
        scope_results,
    ):
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

        policy_candidates.append((idx, rule, syntax_result, scope_result))

    # ============================================================
    # Stage 3: Problem policy / rule direction
    # ============================================================

    sandbox_candidates: list[
        tuple[int, str, SyntaxResult, ScopeResult, PolicyResult]
    ] = []

    has_direct_evidence = _has_direct_evidence(context)

    for idx, rule, syntax_result, scope_result in policy_candidates:
        policy_result = _check_policy_direction(
            rule=rule,
            problem_type=resolved_problem_type,
            has_direct_evidence=has_direct_evidence,
        )

        if not policy_result.valid:
            reason = policy_result.error or "wrong rule direction"
            outcomes[idx] = RuleValidationOutcome(
                rule=rule,
                passed=False,
                syntax=syntax_result,
                scope=scope_result,
                policy=policy_result,
                failure_stage="policy",
                failure_reason=reason,
            )
            _log_failure(rule, "policy", reason)
            continue

        sandbox_candidates.append(
            (
                idx,
                rule,
                syntax_result,
                scope_result,
                policy_result,
            )
        )

    # ============================================================
    # Optional: stop after policy if sandbox disabled
    # ============================================================

    if not run_sandbox_checks:
        for idx, rule, syntax_result, scope_result, policy_result in sandbox_candidates:
            outcomes[idx] = RuleValidationOutcome(
                rule=rule,
                passed=True,
                syntax=syntax_result,
                scope=scope_result,
                policy=policy_result,
                sandbox=None,
                failure_stage="",
                failure_reason="",
            )

        return _finalize_report(report, outcomes)

    # ============================================================
    # Stage 4: Per-rule sandbox
    # ============================================================

    for idx, rule, syntax_result, scope_result, policy_result in sandbox_candidates:
        try:
            sandbox_result = _run_sandbox_single_rule(
                page_url=page_url,
                rule=rule,
                ticket_context=context,
            )
        except Exception as exc:
            sandbox_result = SandboxResult(
                url=page_url,
                passed=False,
                error=f"sandbox validator error: {exc}",
            )

        passed, failure_reason = _sandbox_policy_result(
            sandbox_result=sandbox_result,
            policy_result=policy_result,
        )

        outcomes[idx] = RuleValidationOutcome(
            rule=rule,
            passed=passed,
            syntax=syntax_result,
            scope=scope_result,
            policy=policy_result,
            sandbox=sandbox_result,
            failure_stage="" if passed else "sandbox",
            failure_reason="" if passed else failure_reason,
        )

        if not passed:
            _log_failure(rule, "sandbox", failure_reason)

    return _finalize_report(report, outcomes)


def _normalize_context(
    ticket_context: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    if isinstance(ticket_context, Mapping):
        return dict(ticket_context)

    return {}


def _resolve_problem_type(
    ticket_context: Mapping[str, Any],
    override_problem_type: Optional[str],
) -> str:
    if override_problem_type:
        return normalize_problem_type(override_problem_type, fallback="unknown")

    if ticket_context:
        return normalize_problem_type(
            ticket_context.get("problem_type", "unknown"),
            fallback="unknown",
        )

    # Backward compatibility:
    # If older callers do not pass ticket_context, treat the run as legacy
    # ad-blocking mode.
    return LEGACY_DEFAULT_PROBLEM_TYPE


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


def _check_policy_direction(
    rule: str,
    problem_type: str,
    has_direct_evidence: bool,
) -> PolicyResult:
    policy = get_problem_policy(problem_type)
    rule_direction = classify_rule_direction(rule)

    error = get_rule_direction_error(
        problem_type,
        rule,
        has_direct_evidence=has_direct_evidence,
    )

    valid = is_rule_direction_allowed(
        problem_type,
        rule,
        has_direct_evidence=has_direct_evidence,
    )

    return PolicyResult(
        rule=rule,
        valid=valid,
        problem_type=policy.problem_type,
        resolution_strategy=policy.strategy,
        rule_direction=rule_direction,
        error=error or None,
    )


def _has_direct_evidence(ticket_context: Mapping[str, Any]) -> bool:
    matched_rules = ticket_context.get("matched_rules", [])
    blocked_resources = ticket_context.get("blocked_resources", [])

    return _has_items(matched_rules) or _has_items(blocked_resources)


def _has_items(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())

    if isinstance(value, list):
        return len(value) > 0

    return bool(value)


def _run_sandbox_single_rule(
    page_url: str,
    rule: str,
    ticket_context: Mapping[str, Any],
) -> SandboxResult:
    """
    Run sandbox for exactly one candidate rule.

    This keeps validation honest:
    one good rule cannot make unrelated rules pass.
    """
    rules = [rule]

    try:
        return run_sandbox(
            page_url,
            rules,
            ticket_context=dict(ticket_context),
        )
    except TypeError:
        # Backward compatibility with older sandbox_check.py that only accepts
        # run_sandbox(url, rules).
        return run_sandbox(page_url, rules)


def _sandbox_policy_result(
    sandbox_result: Optional[SandboxResult],
    policy_result: PolicyResult,
) -> tuple[bool, str]:
    """
    Evaluate sandbox result according to resolution strategy.

    We intentionally do not blindly trust sandbox_result.passed, because
    different ticket strategies have different success criteria.
    """
    if sandbox_result is None:
        return False, "sandbox did not run"

    if getattr(sandbox_result, "error", ""):
        return False, str(sandbox_result.error)

    page_functional = bool(getattr(sandbox_result, "page_functional", False))
    ticket_assertions_passed = bool(
        getattr(sandbox_result, "ticket_assertions_passed", True)
    )
    ads_blocked = bool(getattr(sandbox_result, "ads_blocked", False))

    if not page_functional:
        return False, _sandbox_failure_reason(sandbox_result)

    if not ticket_assertions_passed:
        errors = getattr(sandbox_result, "ticket_assertion_errors", []) or []
        if errors:
            return False, "ticket_assertions_failed=" + "; ".join(map(str, errors))

        return False, "ticket_assertions_failed"

    strategy = policy_result.resolution_strategy
    rule_direction = policy_result.rule_direction

    if strategy == STRATEGY_BLOCK_VISIBLE_AD:
        if not ads_blocked:
            return False, "ads_not_blocked"

        return True, ""

    if strategy == STRATEGY_ALLOW_REQUIRED_CONTENT:
        # Breakage fixes usually restore content with @@ or #@#.
        # They do not need to block ads.
        return True, ""

    if strategy == STRATEGY_RESTORE_HIDDEN_UI:
        # UI restore rules do not need to block ads.
        return True, ""

    if strategy == STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE:
        # Overlay blocking/hiding rules must prove they hide/block something.
        # Exception rules are allowed when direct resource evidence exists and
        # page assertions pass, so ads_blocked is not required for exceptions.
        if rule_direction in {
            RULE_NETWORK_BLOCK,
            RULE_COSMETIC_HIDE,
        } and not ads_blocked:
            return False, "overlay_or_ad_not_blocked"

        return True, ""

    # Unknown fallback: accept if sandbox itself says passed, otherwise explain.
    if bool(getattr(sandbox_result, "passed", False)):
        return True, ""

    return False, _sandbox_failure_reason(sandbox_result)


def _finalize_report(
    report: ValidationReport,
    outcomes: list[Optional[RuleValidationOutcome]],
) -> ValidationReport:
    report.outcomes = [
        outcome
        for outcome in outcomes
        if outcome is not None
    ]
    report.passed_count = sum(
        1
        for outcome in report.outcomes
        if outcome.passed
    )
    report.failed = report.total - report.passed_count
    report.passed = report.total > 0 and report.failed == 0

    logger.info(
        "Rule validation finished | url=%s | problem_type=%s | strategy=%s | passed=%d/%d",
        report.url,
        report.problem_type,
        report.resolution_strategy,
        report.passed_count,
        report.total,
    )

    return report


def _scope_failure_reason(scope_result: ScopeResult) -> str:
    if getattr(scope_result, "detail", None):
        return str(scope_result.detail)

    if getattr(scope_result, "risk", None):
        return str(scope_result.risk)

    return "unsafe scope"


def _sandbox_failure_reason(sandbox_result: Optional[SandboxResult]) -> str:
    if sandbox_result is None:
        return "sandbox did not run"

    if getattr(sandbox_result, "error", ""):
        return str(sandbox_result.error)

    reasons = []

    ads_blocked = bool(getattr(sandbox_result, "ads_blocked", False))
    page_functional = bool(getattr(sandbox_result, "page_functional", False))
    ticket_assertions_passed = bool(
        getattr(sandbox_result, "ticket_assertions_passed", True)
    )

    if not ads_blocked:
        reasons.append("ads_not_blocked")

    if not page_functional:
        broken_selectors = getattr(sandbox_result, "broken_selectors", []) or []
        layout_diff_pct = float(getattr(sandbox_result, "layout_diff_pct", 0.0) or 0.0)

        if broken_selectors:
            reasons.append("broken_selectors=" + ",".join(map(str, broken_selectors)))

        if layout_diff_pct > 0:
            reasons.append(f"layout_diff_pct={layout_diff_pct:.3f}")

        reasons.append("page_not_functional")

    if not ticket_assertions_passed:
        errors = getattr(sandbox_result, "ticket_assertion_errors", []) or []
        if errors:
            reasons.append("ticket_assertions_failed=" + "; ".join(map(str, errors)))
        else:
            reasons.append("ticket_assertions_failed")

    return "; ".join(reasons) or "sandbox failed"

    
def _log_failure(rule: str, stage: str, reason: str) -> None:
    logger.warning(
        "Rule validation failed at %s for %r: %s",
        stage,
        rule,
        reason,
    )
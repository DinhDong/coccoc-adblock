# Orchestrates all three validation stages for a batch of generated rules:
# Stage 1 — abp_syntax.py: reject rules with invalid filter syntax
# Stage 2 — rule_scope.py: reject rules that are too broad or likely to cause false positives
# Stage 3 — sandbox_check.py: test each survivor rule individually in a live browser session
#
# New in this version:
# - Runs sandbox per rule instead of one sandbox run for the whole batch.
# - Prevents one good rule from making the whole candidate batch pass.
# - For visible-ad / anti-overlay blocking rules, requires the candidate rule
#   to actually hide/block something in sandbox.
# - For breakage tickets, rejects accidental blocking/hiding rules.
# - Keeps support for ticket_context and existing sandbox_check.py.

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from ..validator.abp_syntax import check_syntax_batch, SyntaxResult
from ..validator.rule_scope import check_scope_batch, ScopeResult
from ..validator.sandbox_check import run_sandbox, SandboxResult

logger = logging.getLogger(__name__)


BREAKAGE_PROBLEM_TYPES = {
    "content_broken_image",
    "content_broken_video",
    "content_broken",
    "ui_hidden",
}

ANTI_OVERLAY_PROBLEM_TYPES = {
    "anti_adblock_or_overlay",
}

AD_BLOCK_PROBLEM_TYPES = {
    "specific_ad_not_blocked",
}


@dataclass
class RuleValidationOutcome:
    """Validation result for a single rule across all stages."""
    rule: str
    passed: bool
    syntax: Optional[SyntaxResult] = None
    scope: Optional[ScopeResult] = None
    sandbox: Optional[SandboxResult] = None
    failure_stage: str = ""
    failure_reason: str = ""


@dataclass
class ValidationReport:
    """Full validation report for one rule generation batch."""
    url: str
    total: int = 0
    passed: bool = False
    passed_count: int = 0
    failed: int = 0
    sandbox_mode: str = "per_rule"
    ticket_context: Dict[str, Any] = field(default_factory=dict)
    outcomes: list[RuleValidationOutcome] = field(default_factory=list)

    def passing_rules(self) -> list[str]:
        return [
            outcome.rule
            for outcome in self.outcomes
            if outcome.passed
        ]


def validate_rules(
    rules: list[str],
    page_url: str,
    environment: str = "desktop",
    ticket_context: Optional[Dict[str, Any]] = None,
) -> ValidationReport:
    """
    Run validation on candidate rules.

    Validation flow:
      1. Syntax check all rules.
      2. Scope check syntax-valid rules.
      3. Sandbox each scope-safe rule individually.

    Only rules that pass all three stages are included in ValidationReport.passing_rules().
    Failed rules are logged with their failure stage and reason for the audit trail.

    This makes the validation result stricter:
      - A rule only passes if that exact rule passes sandbox.
      - A batch cannot pass because another rule did the useful work.

    Args:
        rules:       List of rule strings from rule_generator.generate_rules().
        page_url:    The original reported URL (needed for the sandbox browser session).
        environment: Crawl environment ("desktop", "android", "ios") — passed to the
                     sandbox so it validates with the same viewport and UA as the crawl.
        ticket_context: Optional ticket context for effect verification.

    Returns:
        ValidationReport. Pass report.passing_rules() to the moderator queue.
    """
    safe_ticket_context = _safe_ticket_context(ticket_context)
    rule_strings = [
        _coerce_rule(rule)
        for rule in rules
        if _coerce_rule(rule)
    ]

    report = ValidationReport(
        url=page_url,
        total=len(rule_strings),
        ticket_context=safe_ticket_context,
        sandbox_mode="per_rule",
    )

    if not rule_strings:
        logger.info("No rules supplied for validation for %s", page_url)
        return report

    outcomes: list[Optional[RuleValidationOutcome]] = [None] * len(rule_strings)

    # ------------------------------------------------------------------
    # Stage 1 — syntax validation
    # ------------------------------------------------------------------
    try:
        syntax_results = check_syntax_batch(rule_strings)
        _assert_result_count("syntax", len(rule_strings), len(syntax_results))
    except Exception as exc:
        reason = f"syntax validator error: {exc}"

        for idx, rule in enumerate(rule_strings):
            syntax_result = SyntaxResult(
                rule=rule,
                valid=False,
                error=reason,
            )
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

    # ------------------------------------------------------------------
    # Stage 2 — scope validation
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Stage 3 — sandbox validation per rule
    # ------------------------------------------------------------------
    for idx, rule, syntax_result, scope_result in sandbox_candidates:
        sandbox_result = _run_sandbox_for_one_rule(
            page_url=page_url,
            rule=rule,
            ticket_context=safe_ticket_context,
            environment=environment,
        )

        sandbox_passed = bool(sandbox_result and sandbox_result.passed)

        effect_verified, effect_reason = _rule_effect_verified(
            rule=rule,
            sandbox_result=sandbox_result,
            ticket_context=safe_ticket_context,
        )

        passed = sandbox_passed and effect_verified

        if passed:
            failure_stage = ""
            failure_reason = ""
        elif not sandbox_passed:
            failure_stage = "sandbox"
            failure_reason = _sandbox_failure_reason(
                sandbox_result,
                safe_ticket_context,
            )
        else:
            failure_stage = "sandbox"
            failure_reason = effect_reason or "rule_effect_not_verified"

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
            _log_failure(rule, failure_stage, failure_reason)

    return _finalize_report(report, outcomes)


def _run_sandbox_for_one_rule(
    page_url: str,
    rule: str,
    ticket_context: Dict[str, Any],
    environment: str = "desktop",
) -> SandboxResult:
    """
    Run sandbox for exactly one candidate rule.
    """
    try:
        return _call_run_sandbox(
            page_url=page_url,
            rules=[rule],
            ticket_context=ticket_context,
            environment=environment,
        )
    except Exception as exc:
        return SandboxResult(
            url=page_url,
            passed=False,
            error=f"sandbox validator error: {exc}",
        )


def _call_run_sandbox(
    page_url: str,
    rules: list[str],
    ticket_context: Dict[str, Any],
    environment: str = "desktop",
) -> SandboxResult:
    """
    Call sandbox_check.run_sandbox() with ticket_context and environment.

    Backward-compatible if an older sandbox_check.py is still loaded.
    """
    try:
        signature = inspect.signature(run_sandbox)
        kwargs: Dict[str, Any] = {}

        if "ticket_context" in signature.parameters:
            kwargs["ticket_context"] = ticket_context

        if "environment" in signature.parameters:
            kwargs["environment"] = environment

        if kwargs:
            return run_sandbox(page_url, rules, **kwargs)

        logger.warning(
            "sandbox_check.run_sandbox() has no ticket_context or environment parameter yet. "
            "Calling old signature."
        )
        return run_sandbox(page_url, rules)

    except TypeError as exc:
        logger.warning(
            "Failed to call run_sandbox with extended params. "
            "Falling back to old signature: %s",
            exc,
        )
        return run_sandbox(page_url, rules)


def _rule_effect_verified(
    rule: str,
    sandbox_result: Optional[SandboxResult],
    ticket_context: Dict[str, Any],
) -> tuple[bool, str]:
    """
    Apply extra per-rule checks after sandbox_result.passed.

    This is the hardening layer.

    Why:
      For some ticket types, sandbox can say the page is functional, but that
      does not prove the specific rule did anything useful.

    Rules:
      - specific_ad_not_blocked:
          candidate must actually block/hide something.

      - anti_adblock_or_overlay:
          if candidate is a blocking/hiding rule, it must actually block/hide
          something. Exception rules may pass based on ticket assertions.

      - content_broken_*:
          blocking/hiding rules are suspicious and fail. Exception rules may
          pass based on ticket assertions.

      - ui_hidden:
          hiding/blocking rules are suspicious and fail. Cosmetic exceptions
          may pass based on ticket assertions.
    """
    if sandbox_result is None:
        return False, "sandbox did not run"

    if sandbox_result.error:
        return False, sandbox_result.error

    problem_type = str(ticket_context.get("problem_type", "unknown")).lower()

    rule_kind = _classify_rule_direction(rule)

    ads_blocked = bool(getattr(sandbox_result, "ads_blocked", False))

    if problem_type in AD_BLOCK_PROBLEM_TYPES:
        if not ads_blocked:
            return False, "rule_effect_not_verified: candidate did not block or hide any detected ad target"
        return True, ""

    if problem_type in ANTI_OVERLAY_PROBLEM_TYPES:
        if rule_kind in {"cosmetic_hide", "network_block"} and not ads_blocked:
            return False, "rule_effect_not_verified: anti-overlay blocking/hiding rule did not hide or block a detected target"
        return True, ""

    if problem_type in {
        "content_broken_image",
        "content_broken_video",
        "content_broken",
    }:
        if rule_kind in {"cosmetic_hide", "network_block"}:
            return False, "wrong_rule_direction: breakage ticket should prefer exception rules, not blocking/hiding rules"
        return True, ""

    if problem_type == "ui_hidden":
        if rule_kind in {"cosmetic_hide", "network_block"}:
            return False, "wrong_rule_direction: ui_hidden ticket should prefer cosmetic exception rules (#@#), not hiding/blocking rules"
        return True, ""

    return True, ""


def _classify_rule_direction(rule: str) -> str:
    """
    Classify the generated rule by behavior.

    Returns:
      - network_exception
      - cosmetic_exception
      - cosmetic_hide
      - network_block
      - unknown
    """
    text = str(rule or "").strip()

    if not text:
        return "unknown"

    if "#@#" in text:
        return "cosmetic_exception"

    if "##" in text:
        return "cosmetic_hide"

    if text.startswith("@@"):
        return "network_exception"

    if text:
        return "network_block"

    return "unknown"


def _coerce_rule(rule: Any) -> str:
    """
    Accept raw strings or ParsedRule-like objects from rule_generator.py.
    """
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
    return report


def _scope_failure_reason(scope_result: ScopeResult) -> str:
    if scope_result.detail:
        return scope_result.detail

    if scope_result.risk:
        return str(scope_result.risk)

    return "unsafe scope"


def _sandbox_failure_reason(
    sandbox_result: Optional[SandboxResult],
    ticket_context: Dict[str, Any],
) -> str:
    if sandbox_result is None:
        return "sandbox did not run"

    if sandbox_result.error:
        return sandbox_result.error

    problem_type = str(ticket_context.get("problem_type", "unknown")).lower()
    is_breakage_ticket = problem_type in BREAKAGE_PROBLEM_TYPES
    is_anti_overlay_ticket = problem_type in ANTI_OVERLAY_PROBLEM_TYPES

    reasons: list[str] = []

    ticket_errors = getattr(sandbox_result, "ticket_assertion_errors", None)
    if ticket_errors:
        reasons.append(
            "ticket_assertions="
            + ",".join(str(error) for error in ticket_errors)
        )

    ticket_passed = getattr(sandbox_result, "ticket_assertions_passed", True)
    if ticket_passed is False and not ticket_errors:
        reasons.append("ticket_assertions_failed")

    if (
        not is_breakage_ticket
        and not is_anti_overlay_ticket
        and not getattr(sandbox_result, "ads_blocked", False)
    ):
        reasons.append("ads_not_blocked")

    if not getattr(sandbox_result, "page_functional", False):
        broken_selectors = getattr(sandbox_result, "broken_selectors", [])

        if broken_selectors:
            reasons.append(
                "broken_selectors="
                + ",".join(str(selector) for selector in broken_selectors)
            )

        if not any(reason.startswith("broken_selectors=") for reason in reasons):
            reasons.append("page_not_functional")

    baseline_errors = getattr(sandbox_result, "baseline_ticket_assertion_errors", None)
    existing_rules_count = int(getattr(sandbox_result, "existing_rules_count", 0) or 0)

    if is_breakage_ticket and existing_rules_count > 0 and not baseline_errors:
        reasons.append("breakage_not_reproduced_with_existing_rules")

    return "; ".join(reasons) or "sandbox failed"


def _safe_ticket_context(value: Any) -> Dict[str, Any]:
    """
    Ensure ticket_context is dict-like and JSON-safe.
    """
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return _make_json_safe(dict(value))

    return {
        "problem_type": "unknown",
        "raw": str(value),
    }


def _make_json_safe(value: Any) -> Any:
    """
    Recursively convert values into JSON-safe data.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        return {
            str(key): _make_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            _make_json_safe(item)
            for item in value
        ]

    return str(value)


def _log_failure(rule: str, stage: str, reason: str) -> None:
    logger.warning(
        "Rule validation failed at %s for %r: %s",
        stage,
        rule,
        reason,
    )
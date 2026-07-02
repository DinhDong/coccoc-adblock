"""
End-to-end pipeline coordinator.

Reads a crawl result, runs AI rule generation, deduplicates candidates,
validates the remaining candidates, and writes the outputs to data/rule_outputs/.

This workflow is ticket-aware:
- normalizes ticket_context before rule generation
- persists problem_type and resolution_strategy in rule output
- validates rules using problem_policy-aware validator
- persists policy validation details in validation output

It also supports rule deduplication:
- skips rules already generated for the same domain
- skips rules already covered by public filter lists such as ABPvn/EasyList

New in this version:
- Saves the final review screenshot from a combined sandbox run that applies
  all passing rules at the same time.
- Falls back to the first per-rule sandbox screenshot only if combined sandbox
  screenshot is unavailable.

This workflow can also crawl the target itself, so a single command can crawl a
website and generate rules for it in one pass:
- pass --url to crawl first, then generate + validate rules for that crawl
- without --url, it reuses an existing crawl result (legacy behaviour)

Run from backend/:
    .venv\\Scripts\\activate

    # Crawl + generate + validate in one command:
    python -m app.services.workflow <report_id> --url https://example.com --env desktop

    # Reuse an existing crawl result:
    python -m app.services.workflow <report_id>
    python -m app.services.workflow <report_id> --no-sandbox

Input:
    data/crawl_outputs/results/<report_id>.json
    (created automatically when --url is provided)

Output:
    data/rule_outputs/results/<report_id>_rules.json
    data/rule_outputs/validation/<report_id>_validation.json
    data/rule_outputs/screenshots/<report_id>_with_rules.png
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:
    from dotenv import load_dotenv

    backend_root = Path(__file__).resolve().parents[2]
    project_root = Path(__file__).resolve().parents[3]

    load_dotenv(backend_root / ".env.local")
    load_dotenv(backend_root / ".env")
    load_dotenv(project_root / ".env.local")
    load_dotenv(project_root / ".env")
except ImportError:
    pass


logger = logging.getLogger(__name__)

OUT_RESULTS = Path("data/rule_outputs/results")
OUT_VALIDATION = Path("data/rule_outputs/validation")
OUT_SCREENSHOTS = Path("data/rule_outputs/screenshots")
CRAWL_RESULTS = Path("data/crawl_outputs/results")


def _separator(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def run_rule_generation(
    crawl_result: Dict[str, Any],
    report_id: str,
    skip_external: bool = False,
    discard_existing: bool = False,
) -> List[Any]:
    """
    Stage 1 — call the LLM, parse the response, dedupe candidates, and persist
    the new rule list.

    Dedup stages:
      1. Internal registry:
         skip rules already generated for the same domain in a previous run.
      2. External public filter lists:
         skip rules already covered by ABPvn/EasyList/EasyPrivacy/etc.

    Args:
        skip_external:    Skip external filter list (EasyList, etc.) checks.
        discard_existing: Clear the domain's internal registry before deduping
                          so all generated rules are treated as new.

    Returns:
        List of (non-duplicate) ParsedRule objects.
    """
    from app.services.external_filter_lists import filter_uncovered
    from app.services.rule_generator import generate_rules_with_metadata
    from app.services.rule_registry import (
        clear_rules,
        filter_new_rules,
        get_domain,
        normalize_rule,
        register_rules,
    )

    crawl_result = _prepare_crawl_result(crawl_result)

    ticket_context = crawl_result.get("ticket_context", {})
    problem_type = _get_problem_type(ticket_context)
    resolution_strategy = _get_resolution_strategy(ticket_context)
    url = crawl_result.get("url", "")
    domain = get_domain(url)

    generation_result = generate_rules_with_metadata(crawl_result)
    generated_rules = list(getattr(generation_result, "rules", []) or [])

    token_usage = getattr(generation_result, "token_usage", None)
    model = getattr(generation_result, "model", None)
    fallback_used = getattr(generation_result, "fallback_used", None)
    prompt_preview = getattr(generation_result, "prompt_preview", "")

    if isinstance(token_usage, Mapping):
        if model is None:
            model = token_usage.get("model")
        if fallback_used is None:
            fallback_used = token_usage.get("fallback_used")

    if not generated_rules:
        logger.warning("Stage 1: no rules generated for %s", report_id)
        return []

    if discard_existing:
        cleared = clear_rules(domain)
        if cleared:
            logger.info(
                "Stage 1: cleared %d existing rule(s) for %s (discard mode)",
                cleared,
                domain,
            )

    # Dedup 1: skip rules already generated for this domain.
    rules, internal_dupes = filter_new_rules(url, generated_rules)
    if internal_dupes:
        logger.info(
            "Stage 1: skipped %d rule(s) already in internal registry for %s",
            len(internal_dupes),
            domain,
        )

    # Dedup 2: skip rules already covered by public filter lists.
    rules, external_dupes = filter_uncovered(rules, skip=skip_external)
    if external_dupes:
        for rule, source in external_dupes:
            logger.info(
                "Stage 1: skipped rule already in %s: %s",
                source,
                _coerce_rule(rule),
            )

    total_skipped = len(internal_dupes) + len(external_dupes)

    if not rules:
        logger.info(
            "Stage 1: all %d generated rule(s) were duplicates for %s — nothing new to save",
            len(generated_rules),
            report_id,
        )
        return []

    OUT_RESULTS.mkdir(parents=True, exist_ok=True)
    rules_path = OUT_RESULTS / f"{report_id}_rules.json"

    rules_data = {
        "report_id": report_id,
        "environment": crawl_result.get("environment", "desktop"),
        "url": url,
        "ticket_context": ticket_context,
        "problem_type": problem_type,
        "resolution_strategy": resolution_strategy,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "generated",
        "rule_count": len(rules),
        "generated_rule_count": len(generated_rules),
        "token_usage": token_usage,
        "model": model,
        "fallback_used": fallback_used,
        "prompt_preview": prompt_preview,
        "duplicates_skipped": {
            "total": total_skipped,
            "internal": len(internal_dupes),
            "external": len(external_dupes),
            "internal_rules": [
                _coerce_rule(rule)
                for rule in internal_dupes
            ],
            "external_detail": [
                {
                    "rule": _coerce_rule(rule),
                    "covered_by": source,
                }
                for rule, source in external_dupes
            ],
        },
        "rules": [
            {
                "rule": _coerce_rule(rule),
                "rule_type": getattr(rule, "rule_type", ""),
                "raw": getattr(rule, "raw", _coerce_rule(rule)),
            }
            for rule in rules
        ],
    }

    with open(rules_path, "w", encoding="utf-8") as file:
        json.dump(_make_json_safe(rules_data), file, indent=2, ensure_ascii=False)

    if rules:
        register_rules(
            domain,
            [normalize_rule(_coerce_rule(rule)) for rule in rules],
        )

    _log_token_usage(token_usage)

    logger.info(
        "Stage 1: %d new rule(s) saved → %s  (%d duplicate(s) skipped) | problem_type=%s",
        len(rules),
        rules_path,
        total_skipped,
        problem_type,
    )

    return rules


def run_rule_validation(
    rules: List[Any],
    url: str,
    report_id: str,
    environment: str = "desktop",
    ticket_context: Optional[Mapping[str, Any]] = None,
    run_sandbox_checks: bool = True,
) -> Dict[str, Any]:
    """
    Stage 2 — run syntax, scope, policy, per-rule sandbox, and combined sandbox.

    Args:
        environment:    Crawl environment ("desktop", "android", "ios") — forwarded to
                        the sandbox so validation uses the same viewport/UA as the crawl.
        ticket_context: Optional ticket metadata stored in the validation JSON output.
    """
    from app.services.rule_validator import validate_rules
    from app.services.ticket_context import normalize_ticket_context

    normalized_ticket_context = normalize_ticket_context(ticket_context or {})
    problem_type = _get_problem_type(normalized_ticket_context)
    resolution_strategy = _get_resolution_strategy(normalized_ticket_context)

    rule_strings = [
        _coerce_rule(rule)
        for rule in rules
        if _coerce_rule(rule)
    ]

    report = validate_rules(
        rule_strings,
        url,
        environment=environment,
        ticket_context=normalized_ticket_context,
        run_sandbox_checks=run_sandbox_checks,
    )

    OUT_VALIDATION.mkdir(parents=True, exist_ok=True)
    validation_path = OUT_VALIDATION / f"{report_id}_validation.json"

    combined_sandbox = getattr(report, "combined_sandbox", None)
    combined_screenshot_path = _save_combined_sandbox_screenshot(
        combined_sandbox,
        report_id,
    )

    if not combined_screenshot_path:
        combined_screenshot_path = _save_first_sandbox_screenshot(
            report.outcomes,
            report_id,
        )

    validation_data = {
        "report_id": report_id,
        "url": url,
        "ticket_context": normalized_ticket_context,
        "problem_type": getattr(report, "problem_type", problem_type),
        "resolution_strategy": getattr(
            report,
            "resolution_strategy",
            resolution_strategy,
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": report.total,
        "passed": report.passed_count,
        "failed": report.failed,
        "passing_rules": report.passing_rules(),
        "combined_screenshot": combined_screenshot_path,
        "combined_sandbox": _serialize_sandbox_result(combined_sandbox),
        "outcomes": [
            _serialize_outcome(outcome)
            for outcome in report.outcomes
        ],
    }

    with open(validation_path, "w", encoding="utf-8") as file:
        json.dump(_make_json_safe(validation_data), file, indent=2, ensure_ascii=False)

    logger.info(
        "Stage 2: %d/%d rules passed validation → %s | problem_type=%s | strategy=%s | combined_screenshot=%s",
        report.passed_count,
        report.total,
        validation_path,
        validation_data["problem_type"],
        validation_data["resolution_strategy"],
        combined_screenshot_path or "n/a",
    )

    return {
        "total": report.total,
        "passed": report.passed_count,
        "failed": report.failed,
        "passing_rules": report.passing_rules(),
        "problem_type": validation_data["problem_type"],
        "resolution_strategy": validation_data["resolution_strategy"],
        "combined_screenshot": combined_screenshot_path,
        "combined_sandbox_passed": bool(
            getattr(combined_sandbox, "passed", False)
        ) if combined_sandbox else False,
    }


def run_crawl(
    url: str,
    report_id: str,
    environment: str = "desktop",
    ticket_context: Optional[Mapping[str, Any]] = None,
    focus_region: Optional[str] = None,
    verbose: bool = False,
    **render_kwargs: Any,
) -> Dict[str, Any]:
    """
    Stage 0 — crawl ``url`` and persist the crawl result so the rest of the
    pipeline can consume it.

    This wraps ``CrawlService.crawl_url`` and writes
    ``data/crawl_outputs/results/<report_id>.json`` (same file the legacy
    ``--report_id``-only mode reads). The returned dict is the crawl result;
    callers should check ``status`` before continuing.

    Args:
        environment:    Crawl environment ("desktop", "android", "ios"). Stored
                        inside the crawl JSON and reused by validation.
        focus_region:   Optional region scope forwarded to the crawler.
        **render_kwargs: Extra render options (e.g. headless, enable_scroll).
    """
    from app.services.crawler import CrawlService

    if verbose:
        _separator(f"Stage 0: Crawl — {report_id}  |  {url}  |  env: {environment}")

    service = CrawlService()
    result = service.crawl_url(
        url=url,
        report_id=report_id,
        ticket_context=dict(ticket_context) if ticket_context else None,
        focus_region=focus_region or None,
        environment=environment,
        **render_kwargs,
    )

    status = result.get("status", "unknown")
    if status == "success":
        logger.info(
            "Stage 0: crawl succeeded for %s (report_id: %s, env: %s)",
            url,
            report_id,
            result.get("environment", environment),
        )
        if verbose:
            screenshot = result.get("files", {}).get("screenshot", "")
            candidates = len(result.get("ad_candidates", []) or [])
            print(f"  Crawl OK — {candidates} ad candidate(s) detected")
            if screenshot:
                print(f"  Crawl screenshot: {screenshot}")
    else:
        logger.error(
            "Stage 0: crawl failed for %s (stage: %s): %s",
            url,
            result.get("stage", "unknown"),
            result.get("error", "unknown error"),
        )
        if verbose:
            print(
                f"  Crawl FAILED at stage '{result.get('stage', 'unknown')}': "
                f"{result.get('error', 'unknown error')}"
            )

    return result


def run_pipeline(
    report_id: str,
    verbose: bool = False,
    run_validation: bool = True,
    skip_external: bool = False,
    url: Optional[str] = None,
    environment: str = "desktop",
    ticket_context: Optional[Mapping[str, Any]] = None,
    focus_region: Optional[str] = None,
    **render_kwargs: Any,
) -> Dict[str, Any]:
    """
    Full pipeline:
        (optionally crawl url) → load crawl result → normalize ticket context
        → generate rules → optionally validate → return summary.

    Args:
        report_id:      Matches data/crawl_outputs/results/<report_id>.json
        verbose:        Print stage headers and per-rule output to stdout.
        run_validation: If False, skip validation/sandbox stage.
        skip_external:  Skip external filter list (EasyList etc.) dedup check.
        url:            If provided, crawl this URL first (Stage 0) and generate
                        rules for the resulting crawl. If omitted, an existing
                        crawl result for report_id is reused.
        environment:    Crawl environment used when url is provided.
        ticket_context: Ticket metadata forwarded to the crawl when url is
                        provided.
        focus_region:   Optional region scope forwarded to the crawl.
        **render_kwargs: Extra render options forwarded to the crawler.
    """
    from app.services.rule_registry import get_domain, get_existing_rules

    crawl_path = CRAWL_RESULTS / f"{report_id}.json"

    # Stage 0 (optional): crawl the URL so crawl + rule generation happen in one
    # pass for the same website.
    if url:
        crawl_outcome = run_crawl(
            url=url,
            report_id=report_id,
            environment=environment,
            ticket_context=ticket_context,
            focus_region=focus_region,
            verbose=verbose,
            **render_kwargs,
        )

        if crawl_outcome.get("status") != "success":
            return {
                "report_id": report_id,
                "url": url,
                "environment": environment,
                "ticket_context": dict(ticket_context) if ticket_context else {},
                "problem_type": _get_problem_type(ticket_context),
                "resolution_strategy": _get_resolution_strategy(ticket_context),
                "rules_generated": 0,
                "rules_passed": 0,
                "rules_failed": 0,
                "passing_rules": [],
                "status": "crawl_failed",
                "crawl_stage": crawl_outcome.get("stage", "unknown"),
                "crawl_error": crawl_outcome.get("error", "unknown error"),
            }

    if not crawl_path.exists():
        raise FileNotFoundError(
            f"Crawl result not found: {crawl_path}\n"
            f"Run: python -m app.services.crawler <url> {report_id} --env desktop\n"
            f"Or crawl + generate in one step: "
            f"python -m app.services.workflow {report_id} --url <url> --env desktop"
        )

    with open(crawl_path, encoding="utf-8") as file:
        crawl_result = json.load(file)

    crawl_result = _prepare_crawl_result(crawl_result)

    env = crawl_result.get("environment", "desktop")
    url = crawl_result.get("url", "unknown")
    ticket_context = crawl_result.get("ticket_context", {})
    problem_type = _get_problem_type(ticket_context)
    resolution_strategy = _get_resolution_strategy(ticket_context)

    crawl_screenshot = crawl_result.get("files", {}).get("screenshot", "")
    if crawl_screenshot:
        logger.info("Crawl screenshot → %s", crawl_screenshot)

    if verbose:
        _separator(f"Pipeline: {report_id}  |  {url}  |  env: {env}")
        print(f"  Problem type: {problem_type}")
        print(f"  Strategy: {resolution_strategy}")

    # --- Check for existing rules in the registry and prompt the user ---
    domain = get_domain(url)
    existing = get_existing_rules(domain)
    discard_existing = False

    if existing and verbose:
        print(f"  Domain '{domain}' already has {len(existing)} rule(s) in the registry.")
        print("  [1] Discard old rules — overwrite with fresh generation")
        print("  [2] Keep old rules — only add new ones  (default)")
        print("  [3] Abort")
        try:
            choice = input("  Choice [1/2/3]: ").strip() or "2"
        except (EOFError, KeyboardInterrupt):
            choice = "2"

        if choice == "1":
            discard_existing = True
            print()
        elif choice == "3":
            print("\n  Aborted.")
            return {
                "report_id": report_id,
                "url": url,
                "environment": env,
                "ticket_context": ticket_context,
                "problem_type": problem_type,
                "rules_generated": 0,
                "rules_passed": 0,
                "rules_failed": 0,
                "passing_rules": [],
                "status": "aborted",
            }
        else:
            print()

    if verbose:
        _separator(f"Stage 1: Rule Generation — {report_id}")

    rules = run_rule_generation(
        crawl_result,
        report_id,
        skip_external=skip_external,
        discard_existing=discard_existing,
    )

    if not rules:
        if verbose:
            print("  No new rules generated — pipeline stopped.")

        return {
            "report_id": report_id,
            "url": url,
            "environment": env,
            "problem_type": problem_type,
            "resolution_strategy": resolution_strategy,
            "rules_generated": 0,
            "rules_passed": 0,
            "rules_failed": 0,
            "passing_rules": [],
            "status": "no_rules",
        }

    if verbose:
        for rule in rules:
            print(f"  [{getattr(rule, 'rule_type', ''):10}] {_coerce_rule(rule)}")

        print(f"\n  {len(rules)} new rules generated")
        print(f"  Problem type: {problem_type}")
        print(f"  Strategy: {resolution_strategy}")

    if not run_validation:
        if verbose:
            print("\n  SKIP — validation stage skipped because --no-sandbox was provided")

        return {
            "report_id": report_id,
            "url": url,
            "environment": env,
            "problem_type": problem_type,
            "resolution_strategy": resolution_strategy,
            "rules_generated": len(rules),
            "rules_passed": 0,
            "rules_failed": 0,
            "passing_rules": [],
            "status": "generated",
        }

    if verbose:
        _separator(f"Stage 2: Rule Validation — {report_id}")

    validation = run_rule_validation(
        rules=rules,
        url=url,
        report_id=report_id,
        environment=env,
        ticket_context=ticket_context,
        run_sandbox_checks=True,
    )

    if verbose:
        print(
            f"  Total: {validation['total']}  "
            f"Passed: {validation['passed']}  "
            f"Failed: {validation['failed']}"
        )

        combined_screenshot = validation.get("combined_screenshot", "")
        if combined_screenshot:
            print(f"  Combined screenshot: {combined_screenshot}")

        if validation["passed"] == 0:
            print("\n  No rules passed validation.")
        else:
            print(
                f"\n  {validation['passed']}/{validation['total']} "
                "rules ready for moderator review"
            )

    return {
        "report_id": report_id,
        "url": url,
        "environment": env,
        "problem_type": validation["problem_type"],
        "resolution_strategy": validation["resolution_strategy"],
        "rules_generated": len(rules),
        "rules_passed": validation["passed"],
        "rules_failed": validation["failed"],
        "passing_rules": validation["passing_rules"],
        "combined_screenshot": validation.get("combined_screenshot", ""),
        "combined_sandbox_passed": validation.get("combined_sandbox_passed", False),
        "status": "ok",
    }


def _prepare_crawl_result(crawl_result: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Normalize ticket_context inside crawl result before generation/validation.
    """
    from app.services.ticket_context import normalize_ticket_context

    prepared = dict(crawl_result)
    prepared["ticket_context"] = normalize_ticket_context(
        prepared.get("ticket_context", {})
    )

    return prepared


def _serialize_outcome(outcome: Any) -> Dict[str, Any]:
    return {
        "rule": getattr(outcome, "rule", ""),
        "passed": bool(getattr(outcome, "passed", False)),
        "failure_stage": getattr(outcome, "failure_stage", ""),
        "failure_reason": getattr(outcome, "failure_reason", ""),
        "syntax": _serialize_syntax_result(getattr(outcome, "syntax", None)),
        "scope": _serialize_scope_result(getattr(outcome, "scope", None)),
        "policy": _serialize_policy_result(getattr(outcome, "policy", None)),
        "sandbox": _serialize_sandbox_result(getattr(outcome, "sandbox", None)),
    }


def _serialize_syntax_result(result: Any) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    return {
        "rule": getattr(result, "rule", ""),
        "valid": bool(getattr(result, "valid", False)),
        "error": getattr(result, "error", None),
    }


def _serialize_scope_result(result: Any) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    return {
        "rule": getattr(result, "rule", ""),
        "safe": bool(getattr(result, "safe", False)),
        "risk": getattr(result, "risk", None),
        "detail": getattr(result, "detail", None),
    }


def _serialize_policy_result(result: Any) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    return {
        "rule": getattr(result, "rule", ""),
        "valid": bool(getattr(result, "valid", False)),
        "problem_type": getattr(result, "problem_type", ""),
        "resolution_strategy": getattr(result, "resolution_strategy", ""),
        "rule_direction": getattr(result, "rule_direction", ""),
        "error": getattr(result, "error", None),
    }


def _serialize_sandbox_result(result: Any) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    tested_screenshot = getattr(result, "tested_screenshot", None)

    return {
        "url": getattr(result, "url", ""),
        "passed": bool(getattr(result, "passed", False)),
        "ads_blocked": bool(getattr(result, "ads_blocked", False)),
        "page_functional": bool(getattr(result, "page_functional", False)),
        "ticket_assertions_passed": bool(
            getattr(result, "ticket_assertions_passed", True)
        ),
        "ticket_assertion_errors": list(
            getattr(result, "ticket_assertion_errors", []) or []
        ),
        "baseline_ticket_assertions_passed": bool(
            getattr(result, "baseline_ticket_assertions_passed", True)
        ),
        "baseline_ticket_assertion_errors": list(
            getattr(result, "baseline_ticket_assertion_errors", []) or []
        ),
        "existing_rules_count": int(getattr(result, "existing_rules_count", 0) or 0),
        "candidate_rules_count": int(getattr(result, "candidate_rules_count", 0) or 0),
        "blocked_requests": list(getattr(result, "blocked_requests", []) or []),
        "candidate_blocked_requests": list(
            getattr(result, "candidate_blocked_requests", []) or []
        ),
        "missing_ad_selectors": list(
            getattr(result, "missing_ad_selectors", []) or []
        ),
        "hidden_ad_selectors": list(
            getattr(result, "hidden_ad_selectors", []) or []
        ),
        "broken_selectors": list(getattr(result, "broken_selectors", []) or []),
        "error": getattr(result, "error", ""),
        "unreachable": bool(getattr(result, "unreachable", False)),
        "tested_screenshot_saved": bool(tested_screenshot),
    }


def _save_combined_sandbox_screenshot(
    sandbox_result: Any,
    report_id: str,
) -> str:
    """
    Save screenshot from combined sandbox.

    This is the primary review screenshot because it applies all passing rules
    at the same time.
    """
    if not sandbox_result:
        return ""

    tested_screenshot = getattr(sandbox_result, "tested_screenshot", None)

    if not tested_screenshot:
        return ""

    OUT_SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    screenshot_path = OUT_SCREENSHOTS / f"{report_id}_with_rules.png"
    screenshot_path.write_bytes(tested_screenshot)

    logger.info("Stage 2: combined sandbox screenshot → %s", screenshot_path)
    return str(screenshot_path)


def _save_first_sandbox_screenshot(outcomes: List[Any], report_id: str) -> str:
    """
    Fallback screenshot when combined sandbox is unavailable.

    This should rarely be used. It keeps backward compatibility for cases where
    no combined sandbox screenshot exists.
    """
    sandbox_result = next(
        (
            getattr(outcome, "sandbox", None)
            for outcome in outcomes
            if getattr(outcome, "sandbox", None) is not None
        ),
        None,
    )

    if not sandbox_result:
        return ""

    tested_screenshot = getattr(sandbox_result, "tested_screenshot", None)

    if not tested_screenshot:
        return ""

    OUT_SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    screenshot_path = OUT_SCREENSHOTS / f"{report_id}_with_rules.png"
    screenshot_path.write_bytes(tested_screenshot)

    logger.info("Stage 2: fallback per-rule sandbox screenshot → %s", screenshot_path)
    return str(screenshot_path)


def _coerce_rule(rule: Any) -> str:
    if hasattr(rule, "rule"):
        return str(getattr(rule, "rule")).strip()

    return str(rule).strip()


def _get_problem_type(ticket_context: Any) -> str:
    if isinstance(ticket_context, Mapping):
        return str(ticket_context.get("problem_type", "unknown"))

    return "unknown"


def _get_resolution_strategy(ticket_context: Any) -> str:
    if isinstance(ticket_context, Mapping):
        return str(ticket_context.get("resolution_strategy", "unknown"))

    return "unknown"


def _log_token_usage(token_usage: Optional[Mapping[str, Any]]) -> None:
    """
    Log token usage after rule generation.
    """
    if not token_usage:
        logger.info("Stage 1: token usage unavailable")
        return

    logger.info(
        "Stage 1 token usage | model=%s | fallback_used=%s | prompt=%s | completion=%s | total=%s",
        token_usage.get("model", ""),
        token_usage.get("fallback_used", False),
        token_usage.get("prompt_tokens", ""),
        token_usage.get("completion_tokens", ""),
        token_usage.get("total_tokens", ""),
    )


def _make_json_safe(value: Any) -> Any:
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

    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"

    return str(value)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from app.crawler.browser import ENVIRONMENTS
    from app.services.crawler import _load_ticket_context_from_cli

    VALID_ENVS = list(ENVIRONMENTS.keys())

    parser = argparse.ArgumentParser(
        description=(
            "Crawl a URL (optional) and run the rule generation + validation "
            "pipeline for the resulting crawl."
        ),
    )

    parser.add_argument(
        "report_id",
        help="Report ID matching data/crawl_outputs/results/<report_id>.json",
    )

    parser.add_argument(
        "--url",
        default="",
        help=(
            "URL to crawl first. When provided, the crawler runs and the "
            "pipeline generates rules for that crawl in a single command. "
            "When omitted, an existing crawl result for report_id is reused."
        ),
    )

    parser.add_argument(
        "--env",
        default="desktop",
        choices=VALID_ENVS,
        metavar="ENV",
        help=(
            f"Crawl environment when --url is provided: {', '.join(VALID_ENVS)} "
            "(default: desktop)."
        ),
    )

    parser.add_argument(
        "--focus",
        default="",
        metavar="REGION",
        help=(
            "Scope crawl extraction to a page region (e.g. 'header', "
            "'right sidebar'). Only used with --url. Convenience shortcut for "
            "setting focus_region in the ticket context."
        ),
    )

    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Open a visible browser window during crawl (helps bypass some Cloudflare resets).",
    )

    parser.add_argument(
        "--ticket-context-json",
        default="",
        help="Raw JSON string containing ticket context (used with --url).",
    )

    parser.add_argument(
        "--ticket-context-file",
        default="",
        help="Path to a JSON file containing ticket context (used with --url).",
    )

    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="Skip validation/sandbox stage for faster rule generation testing.",
    )

    parser.add_argument(
        "--no-external",
        action="store_true",
        help="Skip external filter list checks (EasyList, ABPvn, etc.) — useful when download fails.",
    )

    args = parser.parse_args()

    cli_ticket_context = _load_ticket_context_from_cli(
        ticket_context_json=args.ticket_context_json,
        ticket_context_file=args.ticket_context_file,
    )

    # Focus is a property of the ticket context. A --focus flag is a shortcut
    # that populates focus_region unless the ticket context already sets one.
    if args.focus:
        cli_ticket_context = dict(cli_ticket_context or {})
        cli_ticket_context.setdefault("focus_region", args.focus)

    try:
        result = run_pipeline(
            report_id=args.report_id,
            verbose=True,
            run_validation=not args.no_sandbox,
            skip_external=args.no_external,
            url=args.url or None,
            environment=args.env,
            ticket_context=cli_ticket_context or None,
            headless=not args.no_headless,
            enable_scroll=True,
        )
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        sys.exit(1)

    if args.no_sandbox:
        _separator(
            f"Done — {result['rules_generated']} rules generated | "
            f"ticket: {result.get('problem_type', 'unknown')} | "
            f"strategy: {result.get('resolution_strategy', 'unknown')}"
        )
    else:
        _separator(
            f"Done — {result['rules_passed']}/{result['rules_generated']} rules passed | "
            f"ticket: {result.get('problem_type', 'unknown')} | "
            f"strategy: {result.get('resolution_strategy', 'unknown')}"
        )
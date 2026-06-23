"""
End-to-end pipeline coordinator.

Reads a crawl result, runs AI rule generation, validates the candidates,
and writes the outputs to data/rule_outputs/. Designed to be called
from the API layer or directly from the CLI.

New in this version:
- Normalizes crawl_result["ticket_context"] before rule generation.
- Passes normalized ticket_context into prompt/rule generation.
- Passes normalized ticket_context into validation/sandbox.
- Saves normalized ticket_context into generated *_rules.json.
- Saves normalized ticket_context into *_validation.json.
- Keeps backward compatibility with older validator/sandbox signatures.
"""

import inspect
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

try:
    from app.services.ticket_context import normalize_ticket_context
except Exception:
    try:
        from .ticket_context import normalize_ticket_context
    except Exception:
        normalize_ticket_context = None  # type: ignore


logger = logging.getLogger(__name__)

CRAWL_RESULTS_DIR = Path("data/crawl_outputs/results")
OUT_RESULTS = Path("data/rule_outputs/results")
OUT_VALIDATION = Path("data/rule_outputs/validation")
OUT_SCREENSHOTS = Path("data/rule_outputs/screenshots")


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
    Stage 1 — call the LLM, parse the response, dedupe against rules already
    known for this domain, and persist the new rule list.

    Args:
        skip_external:    Skip external filter list (EasyList, etc.) checks.
        discard_existing: Clear the domain's internal registry before deduping
                          so all generated rules are treated as new.

    Returns:
        List of (non-duplicate) ParsedRule objects.
    """
    from app.services.rule_generator import generate_rules_with_metadata
    from app.services.rule_registry import filter_new_rules, get_domain, normalize_rule, register_rules, clear_rules
    from app.services.external_filter_lists import filter_uncovered

    normalized_crawl_result = _with_normalized_ticket_context(crawl_result)
    ticket_context = normalized_crawl_result.get("ticket_context", {})
    problem_type = ticket_context.get("problem_type", "unknown")

    generation_result = generate_rules_with_metadata(normalized_crawl_result)
    rules = generation_result.rules or []

    if not rules:
        logger.warning("Stage 1: no rules generated for %s", report_id)
        return []

    url = crawl_result.get("url", "")
    domain = get_domain(url)

    if discard_existing:
        cleared = clear_rules(domain)
        if cleared:
            logger.info("Stage 1: cleared %d existing rule(s) for %s (discard mode)", cleared, domain)

    # --- Dedup 1: skip rules already generated for this domain in a prior run ---
    rules, internal_dupes = filter_new_rules(url, rules)
    if internal_dupes:
        logger.info("Stage 1: skipped %d rule(s) already in internal registry for %s",
                    len(internal_dupes), domain)

    # --- Dedup 2: skip rules already covered by public filter lists (EasyList, ABPvn, etc.) ---
    rules, external_dupes = filter_uncovered(rules, skip=skip_external)
    if external_dupes:
        for rule, source in external_dupes:
            logger.info("Stage 1: skipped rule already in %s: %s", source, rule.rule)

    total_skipped = len(internal_dupes) + len(external_dupes)

    if not rules:
        logger.info("Stage 1: all generated rule(s) were duplicates for %s — nothing new to save",
                    report_id)
        return []

    OUT_RESULTS.mkdir(parents=True, exist_ok=True)
    rules_path = OUT_RESULTS / f"{report_id}_rules.json"

    rules_data = {
        "report_id": report_id,
        "environment": normalized_crawl_result.get("environment", "desktop"),
        "url": normalized_crawl_result.get("url", ""),
        "ticket_context": ticket_context,
        "problem_type": problem_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "generated",
        "rule_count": len(rules),
        "token_usage": _make_json_safe(generation_result.token_usage),
        "model": getattr(generation_result, "model", ""),
        "fallback_used": bool(getattr(generation_result, "fallback_used", False)),
        "prompt_preview": getattr(generation_result, "prompt_preview", ""),
        "duplicates_skipped": {
            "total": total_skipped,
            "internal": len(internal_dupes),
            "external": len(external_dupes),
            "external_detail": [
                {"rule": rule.rule, "covered_by": source}
                for rule, source in external_dupes
            ],
        },
        "rules": [_rule_to_dict(rule) for rule in rules],
    }

    with open(rules_path, "w", encoding="utf-8") as file:
        json.dump(rules_data, file, indent=2, ensure_ascii=False)

    register_rules(domain, [normalize_rule(rule.rule) for rule in rules])
    _log_token_usage(generation_result.token_usage)

    logger.info(
        "Stage 1: %d new rule(s) saved → %s  (%d duplicate(s) skipped) | problem_type=%s",
        len(rules), rules_path, total_skipped, problem_type,
    )

    return rules


def run_rule_validation(
    rules: List[Any],
    url: str,
    report_id: str,
    environment: str = "desktop",
    ticket_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Stage 2 — run ABP syntax, scope, and sandbox checks on the rule list.

    Args:
        environment:    Crawl environment ("desktop", "android", "ios") — forwarded to
                        the sandbox so validation uses the same viewport/UA as the crawl.
        ticket_context: Optional ticket metadata stored in the validation JSON output.
    """
    from app.services.rule_validator import validate_rules

    safe_context = _normalize_context(ticket_context or {})
    problem_type = safe_context.get("problem_type", "unknown")
    rule_strings = [_coerce_rule_string(rule) for rule in rules]

    report = _call_validate_rules(
        validate_rules_func=validate_rules,
        rule_strings=rule_strings,
        url=url,
        environment=environment,
        ticket_context=safe_context,
    )

    OUT_VALIDATION.mkdir(parents=True, exist_ok=True)
    validation_path = OUT_VALIDATION / f"{report_id}_validation.json"

    validation_data = {
        "report_id": report_id,
        "url": url,
        "ticket_context": safe_context,
        "problem_type": problem_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": getattr(report, "total", 0),
        "passed": getattr(report, "passed_count", 0),
        "failed": getattr(report, "failed", 0),
        "passing_rules": report.passing_rules(),
        "outcomes": [
            _validation_outcome_to_dict(outcome)
            for outcome in getattr(report, "outcomes", [])
        ],
    }

    with open(validation_path, "w", encoding="utf-8") as file:
        json.dump(validation_data, file, indent=2, ensure_ascii=False)

    sandbox_result = _first_sandbox_result(getattr(report, "outcomes", []))

    if sandbox_result and getattr(sandbox_result, "tested_screenshot", b""):
        OUT_SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        screenshot_path = OUT_SCREENSHOTS / f"{report_id}_with_rules.png"
        screenshot_path.write_bytes(sandbox_result.tested_screenshot)
        logger.info("Stage 2: sandbox screenshot → %s", screenshot_path)

    logger.info(
        "Stage 2: %d/%d rules passed validation → %s | problem_type=%s",
        getattr(report, "passed_count", 0),
        getattr(report, "total", 0),
        validation_path,
        problem_type,
    )

    return {
        "total": getattr(report, "total", 0),
        "passed": getattr(report, "passed_count", 0),
        "failed": getattr(report, "failed", 0),
        "passing_rules": report.passing_rules(),
        "validation_file": str(validation_path),
    }


def run_pipeline(
    report_id: str,
    verbose: bool = False,
    run_validation: bool = True,
    skip_external: bool = False,
) -> Dict[str, Any]:
    """
    Full pipeline:
        load crawl result → normalize ticket context → generate rules
        → optionally validate → return summary.

    Args:
        report_id:      Matches data/crawl_outputs/results/<report_id>.json
        verbose:        Print stage headers and per-rule output to stdout.
        run_validation: If False, skip validation/sandbox stage.
        skip_external:  Skip external filter list (EasyList etc.) dedup check.
    """
    from app.services.rule_registry import get_domain, get_existing_rules

    crawl_path = CRAWL_RESULTS_DIR / f"{report_id}.json"

    if not crawl_path.exists():
        raise FileNotFoundError(
            f"Crawl result not found: {crawl_path}\n"
            f"Run: python -m app.services.crawler <url> {report_id} --env desktop"
        )

    with open(crawl_path, encoding="utf-8") as file:
        crawl_result = json.load(file)

    crawl_result = _with_normalized_ticket_context(crawl_result)

    env = crawl_result.get("environment", "desktop")
    url = crawl_result.get("url", "unknown")
    ticket_context = crawl_result.get("ticket_context", {})
    problem_type = ticket_context.get("problem_type", "unknown")

    crawl_screenshot = crawl_result.get("files", {}).get("screenshot", "")
    if crawl_screenshot:
        logger.info("Crawl screenshot → %s", crawl_screenshot)

    if verbose:
        _separator(
            f"Pipeline: {report_id} | {url} | env: {env} | ticket: {problem_type}"
        )

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
                "report_id": report_id, "url": url, "environment": env,
                "ticket_context": ticket_context, "problem_type": problem_type,
                "rules_generated": 0, "rules_passed": 0, "rules_failed": 0,
                "passing_rules": [], "status": "aborted",
            }
        else:
            print()

    # Stage 1 — rule generation
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
            print("  No rules generated — pipeline stopped.")
            print(f"  Problem type: {problem_type}")

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
            "status": "no_rules",
        }

    if verbose:
        for rule in rules:
            rule_type = getattr(rule, "rule_type", "unknown")
            rule_text = getattr(rule, "rule", str(rule))
            print(f"  [{rule_type:10}] {rule_text}")

        print(f"\n  {len(rules)} rules generated")
        print(f"  Problem type: {problem_type}")

    # Optional Stage 2 — validation
    if not run_validation:
        if verbose:
            print("\n  SKIP — validation stage skipped because --no-sandbox was provided")

        return {
            "report_id": report_id,
            "url": url,
            "environment": env,
            "ticket_context": ticket_context,
            "problem_type": problem_type,
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
    )

    if verbose:
        print(
            f"  Total: {validation['total']}  "
            f"Passed: {validation['passed']}  "
            f"Failed: {validation['failed']}"
        )

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
        "ticket_context": ticket_context,
        "problem_type": problem_type,
        "rules_generated": len(rules),
        "rules_passed": validation["passed"],
        "rules_failed": validation["failed"],
        "passing_rules": validation["passing_rules"],
        "validation_file": validation.get("validation_file", ""),
        "status": "ok",
    }


def _with_normalized_ticket_context(crawl_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a copy of crawl_result with normalized ticket_context.

    This makes the workflow robust even when crawler/API stored only raw fields:
      - request
      - actual
      - expected
      - steps

    After normalization, downstream stages can rely on:
      - problem_type
      - target_to_block
      - target_to_preserve
      - validation_hints
      - current_rules
      - blocked_resources
    """
    result = dict(crawl_result or {})
    result["ticket_context"] = _normalize_context(result.get("ticket_context", {}))
    return result


def _normalize_context(value: Any) -> Dict[str, Any]:
    """
    Normalize ticket_context if ticket_context.py is available.
    Otherwise, fall back to JSON-safe dict.
    """
    if normalize_ticket_context is not None:
        try:
            return normalize_ticket_context(value)
        except Exception as exc:
            logger.warning("Failed to normalize ticket_context: %s", exc)

    return _safe_ticket_context(value)


def _call_validate_rules(
    validate_rules_func: Any,
    rule_strings: List[str],
    url: str,
    environment: str = "desktop",
    ticket_context: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    Call rule_validator.validate_rules in a backward-compatible way.

    Probes the function signature and passes only the keyword arguments it accepts,
    so this works regardless of which version of validate_rules is installed:
        validate_rules(rules, page_url, environment, ticket_context)  ← current
        validate_rules(rules, page_url, environment)
        validate_rules(rules, page_url)                               ← oldest
    """
    try:
        params = inspect.signature(validate_rules_func).parameters
        kwargs: Dict[str, Any] = {}
        if "environment" in params:
            kwargs["environment"] = environment
        if "ticket_context" in params:
            kwargs["ticket_context"] = ticket_context or {}
        return validate_rules_func(rule_strings, url, **kwargs)
    except TypeError as exc:
        logger.warning("validate_rules signature mismatch, falling back: %s", exc)
        return validate_rules_func(rule_strings, url)


def _rule_to_dict(rule: Any) -> Dict[str, Any]:
    """
    Convert ParsedRule or raw rule-like object to JSON-safe dict.
    """
    return {
        "rule": getattr(rule, "rule", str(rule)),
        "rule_type": getattr(rule, "rule_type", "unknown"),
        "raw": getattr(rule, "raw", getattr(rule, "rule", str(rule))),
    }


def _coerce_rule_string(rule: Any) -> str:
    """
    Convert ParsedRule or string to a rule string.
    """
    if isinstance(rule, str):
        return rule

    return str(getattr(rule, "rule", rule)).strip()


def _validation_outcome_to_dict(outcome: Any) -> Dict[str, Any]:
    """
    Convert a RuleValidationOutcome to JSON-safe dict.
    """
    sandbox = getattr(outcome, "sandbox", None)

    return {
        "rule": getattr(outcome, "rule", ""),
        "passed": bool(getattr(outcome, "passed", False)),
        "failure_stage": getattr(outcome, "failure_stage", ""),
        "failure_reason": getattr(outcome, "failure_reason", ""),
        "syntax": _simple_result_to_dict(getattr(outcome, "syntax", None)),
        "scope": _simple_result_to_dict(getattr(outcome, "scope", None)),
        "sandbox": _sandbox_result_to_dict(sandbox),
    }


def _simple_result_to_dict(result: Any) -> Optional[Dict[str, Any]]:
    """
    Convert SyntaxResult / ScopeResult-style dataclass to JSON-safe dict.
    """
    if result is None:
        return None

    data = {}

    for key in (
        "rule",
        "valid",
        "error",
        "safe",
        "risk",
        "detail",
    ):
        if hasattr(result, key):
            data[key] = _make_json_safe(getattr(result, key))

    return data


def _sandbox_result_to_dict(sandbox: Any) -> Optional[Dict[str, Any]]:
    """
    Convert SandboxResult to JSON-safe dict without storing raw screenshot bytes.
    """
    if sandbox is None:
        return None

    fields = [
        "url",
        "passed",
        "ads_blocked",
        "page_functional",
        "ticket_assertions_passed",
        "ticket_assertion_errors",
        "baseline_ticket_assertions_passed",
        "baseline_ticket_assertion_errors",
        "existing_rules_count",
        "candidate_rules_count",
        "layout_diff_pct",
        "blocked_requests",
        "candidate_blocked_requests",
        "missing_ad_selectors",
        "hidden_ad_selectors",
        "broken_selectors",
        "error",
    ]

    data = {}

    for key in fields:
        if hasattr(sandbox, key):
            data[key] = _make_json_safe(getattr(sandbox, key))

    if hasattr(sandbox, "tested_screenshot"):
        data["tested_screenshot_saved"] = bool(getattr(sandbox, "tested_screenshot", b""))

    return data


def _first_sandbox_result(outcomes: List[Any]) -> Optional[Any]:
    """
    Return the first sandbox result attached to validation outcomes.
    """
    for outcome in outcomes:
        sandbox = getattr(outcome, "sandbox", None)
        if sandbox is not None:
            return sandbox

    return None


def _safe_ticket_context(value: Any) -> Dict[str, Any]:
    """
    Ensure ticket_context is dict-like and JSON-safe.
    """
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return _make_json_safe(dict(value))

    return {
        "raw": str(value),
        "problem_type": "unknown",
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


def _log_token_usage(token_usage: Optional[Dict[str, Any]]) -> None:
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


# ------------------------------------------------------------------
# CLI entry point
#
# Usage from backend/:
#   python -m app.services.workflow <report_id>
#   python -m app.services.workflow <report_id> --no-sandbox
#
# Example:
#   python -m app.services.workflow test-ticket-current-rules-ios --no-sandbox
# ------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Run the rule generation + validation pipeline for a crawl result.",
    )

    parser.add_argument(
        "report_id",
        help="Report ID matching data/crawl_outputs/results/<report_id>.json",
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

    try:
        result = run_pipeline(
            report_id=args.report_id,
            verbose=True,
            run_validation=not args.no_sandbox,
            skip_external=args.no_external,
        )
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        sys.exit(1)

    if args.no_sandbox:
        _separator(
            f"Done — {result['rules_generated']} rules generated "
            f"| ticket: {result.get('problem_type', 'unknown')}"
        )
    else:
        _separator(
            f"Done — {result['rules_passed']}/{result['rules_generated']} rules passed "
            f"| ticket: {result.get('problem_type', 'unknown')}"
        )
"""
End-to-end pipeline coordinator.

Reads a crawl result, runs AI rule generation, validates the candidates,
and writes the outputs to data/rule_outputs/. Designed to be called
from the API layer or directly from the CLI.

Run from backend/:
    .venv\\Scripts\\activate
    python -m app.services.workflow <report_id>
    python -m app.services.workflow <report_id> --no-sandbox

Examples:
    python -m app.services.workflow vnexpress-desktop
    python -m app.services.workflow reddit-android --no-sandbox

Input:
    data/crawl_outputs/results/<report_id>.json
    environment is read from the 'environment' field in that file.

Output:
    data/rule_outputs/results/<report_id>_rules.json
    data/rule_outputs/validation/<report_id>_validation.json
    data/rule_outputs/screenshots/<report_id>_with_rules.png  (validation only)
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _separator(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def run_rule_generation(crawl_result: Dict[str, Any], report_id: str) -> List[Any]:
    """
    Stage 1 — call the LLM, parse the response, dedupe against rules already
    known for this domain, and persist the new rule list.

    The saved rules JSON includes token_usage from the LLM response and a
    duplicate_rules_skipped count for audit/debugging.
    Returns the list of new (non-duplicate) ParsedRule objects.
    """
    from app.services.rule_generator import generate_rules_with_metadata
    from app.services.rule_registry import filter_new_rules, get_domain, normalize_rule, register_rules
    from app.services.external_filter_lists import filter_uncovered

    generation_result = generate_rules_with_metadata(crawl_result)
    generated_rules = generation_result.rules

    if not generated_rules:
        logger.warning("Stage 1: no rules generated for %s", report_id)
        return []

    url = crawl_result.get("url", "")
    domain = get_domain(url)

    # --- Dedup 1: skip rules already generated for this domain in a prior run ---
    rules, internal_dupes = filter_new_rules(url, generated_rules)
    if internal_dupes:
        logger.info("Stage 1: skipped %d rule(s) already in internal registry for %s",
                    len(internal_dupes), domain)

    # --- Dedup 2: skip rules already covered by public filter lists (EasyList, ABPvn, etc.) ---
    rules, external_dupes = filter_uncovered(rules)
    if external_dupes:
        for rule, source in external_dupes:
            logger.info("Stage 1: skipped rule already in %s: %s", source, rule.rule)

    total_skipped = len(internal_dupes) + len(external_dupes)

    if not rules:
        logger.info("Stage 1: all %d generated rule(s) were duplicates for %s — nothing new to save",
                    len(generated_rules), report_id)
        return []

    OUT_RESULTS.mkdir(parents=True, exist_ok=True)
    rules_path = OUT_RESULTS / f"{report_id}_rules.json"

    rules_data = {
        "report_id": report_id,
        "environment": crawl_result.get("environment", "desktop"),
        "url": url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rule_count": len(rules),
        "duplicates_skipped": {
            "total": total_skipped,
            "internal": len(internal_dupes),
            "external": len(external_dupes),
            "external_detail": [
                {"rule": rule.rule, "covered_by": source}
                for rule, source in external_dupes
            ],
        },
        "token_usage": generation_result.token_usage,
        "rules": [
            {
                "rule": rule.rule,
                "rule_type": rule.rule_type,
                "raw": rule.raw,
            }
            for rule in rules
        ],
    }

    with open(rules_path, "w", encoding="utf-8") as file:
        json.dump(rules_data, file, indent=2, ensure_ascii=False)

    register_rules(domain, [normalize_rule(rule.rule) for rule in rules])

    _log_token_usage(generation_result.token_usage)

    logger.info("Stage 1: %d new rule(s) saved → %s  (%d duplicate(s) skipped)",
                len(rules), rules_path, total_skipped)
    return rules


def run_rule_validation(rules: List[Any], url: str, report_id: str) -> Dict[str, Any]:
    """
    Stage 2 — run ABP syntax, scope, and sandbox checks on the rule list.

    Returns a summary dict with keys:
        total, passed, failed, passing_rules
    """
    from app.services.rule_validator import validate_rules

    rule_strings = [rule.rule for rule in rules]
    report = validate_rules(rule_strings, url)

    OUT_VALIDATION.mkdir(parents=True, exist_ok=True)
    validation_path = OUT_VALIDATION / f"{report_id}_validation.json"

    validation_data = {
        "report_id": report_id,
        "url": url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": report.total,
        "passed": report.passed_count,
        "failed": report.failed,
        "passing_rules": report.passing_rules(),
        "outcomes": [
            {
                "rule": outcome.rule,
                "passed": outcome.passed,
                "failure_stage": outcome.failure_stage,
                "failure_reason": outcome.failure_reason,
            }
            for outcome in report.outcomes
        ],
    }

    with open(validation_path, "w", encoding="utf-8") as file:
        json.dump(validation_data, file, indent=2, ensure_ascii=False)

    sandbox_result = next(
        (outcome.sandbox for outcome in report.outcomes if outcome.sandbox is not None),
        None,
    )

    if sandbox_result and sandbox_result.tested_screenshot:
        OUT_SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        screenshot_path = OUT_SCREENSHOTS / f"{report_id}_with_rules.png"
        screenshot_path.write_bytes(sandbox_result.tested_screenshot)
        logger.info("Stage 2: sandbox screenshot → %s", screenshot_path)

    logger.info(
        "Stage 2: %d/%d rules passed validation → %s",
        report.passed_count,
        report.total,
        validation_path,
    )

    return {
        "total": report.total,
        "passed": report.passed_count,
        "failed": report.failed,
        "passing_rules": report.passing_rules(),
    }


def run_pipeline(
    report_id: str,
    verbose: bool = False,
    run_validation: bool = True,
) -> Dict[str, Any]:
    """
    Full pipeline:
        load crawl result → generate rules → optionally validate → return summary.

    Args:
        report_id:       Matches data/crawl_outputs/results/<report_id>.json
        verbose:         Print stage headers and per-rule output to stdout.
        run_validation:  If False, skip validation/sandbox stage.

    Returns:
        {
            "report_id": str,
            "url": str,
            "environment": str,
            "rules_generated": int,
            "rules_passed": int,
            "rules_failed": int,
            "passing_rules": [str, ...],
            "status": "ok" | "generated" | "no_rules",
        }
    """
    crawl_path = Path("data/crawl_outputs/results") / f"{report_id}.json"

    if not crawl_path.exists():
        raise FileNotFoundError(f"Crawl result not found: {crawl_path}")

    with open(crawl_path, encoding="utf-8") as file:
        crawl_result = json.load(file)

    env = crawl_result.get("environment", "desktop")
    url = crawl_result.get("url", "unknown")

    if verbose:
        _separator(f"Pipeline: {report_id}  |  {url}  |  env: {env}")

    # Stage 1 — rule generation
    if verbose:
        _separator(f"Stage 1: Rule Generation — {report_id}")

    rules = run_rule_generation(crawl_result, report_id)

    if not rules:
        if verbose:
            print("  No rules generated — pipeline stopped.")

        return {
            "report_id": report_id,
            "url": url,
            "environment": env,
            "rules_generated": 0,
            "rules_passed": 0,
            "rules_failed": 0,
            "passing_rules": [],
            "status": "no_rules",
        }

    if verbose:
        for rule in rules:
            print(f"  [{rule.rule_type:10}] {rule.rule}")
        print(f"\n  {len(rules)} rules generated")

    # Optional Stage 2 — validation
    if not run_validation:
        if verbose:
            print("\n  SKIP — validation stage skipped because --no-sandbox was provided")

        return {
            "report_id": report_id,
            "url": url,
            "environment": env,
            "rules_generated": len(rules),
            "rules_passed": 0,
            "rules_failed": 0,
            "passing_rules": [],
            "status": "generated",
        }

    if verbose:
        _separator(f"Stage 2: Rule Validation — {report_id}")

    validation = run_rule_validation(rules, url, report_id)

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
        "rules_generated": len(rules),
        "rules_passed": validation["passed"],
        "rules_failed": validation["failed"],
        "passing_rules": validation["passing_rules"],
        "status": "ok",
    }


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
# Usage: python -m app.services.workflow <report_id> [--no-sandbox]
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

    args = parser.parse_args()

    try:
        result = run_pipeline(
            report_id=args.report_id,
            verbose=True,
            run_validation=not args.no_sandbox,
        )
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        sys.exit(1)

    if args.no_sandbox:
        _separator(f"Done — {result['rules_generated']} rules generated")
    else:
        _separator(f"Done — {result['rules_passed']}/{result['rules_generated']} rules passed")
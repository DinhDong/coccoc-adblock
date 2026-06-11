"""
End-to-end pipeline coordinator.

Reads a crawl result, runs AI rule generation, validates the candidates,
and writes the outputs to data/rule_outputs/.  Designed to be called
from the API layer (routes/rules.py) or directly from the CLI.

Run from backend/:
    .venv\\Scripts\\activate
    python -m app.services.workflow <report_id>

Examples:
    python -m app.services.workflow vnexpress-desktop
    python -m app.services.workflow reddit-android --no-sandbox

Input:   data/crawl_outputs/results/<report_id>.json
         (environment is read from the 'environment' field in that file)

Output:
    data/rule_outputs/results/<report_id>_rules.json
    data/rule_outputs/validation/<report_id>_validation.json
    data/rule_outputs/screenshots/<report_id>_with_rules.png  (sandbox only)
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env.local")
    load_dotenv(Path(__file__).parent.parent.parent / ".env")
except ImportError:
    pass

logger = logging.getLogger(__name__)

OUT_RESULTS     = Path("data/rule_outputs/results")
OUT_VALIDATION  = Path("data/rule_outputs/validation")
OUT_SCREENSHOTS = Path("data/rule_outputs/screenshots")


def _separator(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def run_rule_generation(crawl_result: dict, report_id: str) -> list:
    """
    Stage 1 — call the LLM, parse the response, persist the rule list.

    Returns the list of ParsedRule objects (empty list on failure).
    """
    from app.services.rule_generator import generate_rules

    rules = generate_rules(crawl_result)
    if not rules:
        logger.warning("Stage 1: no rules generated for %s", report_id)
        return []

    OUT_RESULTS.mkdir(parents=True, exist_ok=True)
    rules_path = OUT_RESULTS / f"{report_id}_rules.json"

    rules_data = {
        "report_id": report_id,
        "environment": crawl_result.get("environment", "desktop"),
        "url": crawl_result.get("url", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rule_count": len(rules),
        "rules": [{"rule": r.rule, "rule_type": r.rule_type, "raw": r.raw} for r in rules],
    }
    with open(rules_path, "w", encoding="utf-8") as f:
        json.dump(rules_data, f, indent=2, ensure_ascii=False)

    logger.info("Stage 1: %d rules saved → %s", len(rules), rules_path)
    return rules


def run_rule_validation(rules: list, url: str, report_id: str) -> dict:
    """
    Stage 2 — run ABP syntax, scope, and sandbox checks on the rule list.

    Returns a summary dict with keys: total, passed, failed, passing_rules.
    """
    from app.services.rule_validator import validate_rules

    rule_strings = [r.rule for r in rules]
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
                "rule": o.rule,
                "passed": o.passed,
                "failure_stage": o.failure_stage,
                "failure_reason": o.failure_reason,
            }
            for o in report.outcomes
        ],
    }
    with open(validation_path, "w", encoding="utf-8") as f:
        json.dump(validation_data, f, indent=2, ensure_ascii=False)

    sandbox_result = next(
        (o.sandbox for o in report.outcomes if o.sandbox is not None), None
    )
    if sandbox_result and sandbox_result.tested_screenshot:
        OUT_SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        screenshot_path = OUT_SCREENSHOTS / f"{report_id}_with_rules.png"
        screenshot_path.write_bytes(sandbox_result.tested_screenshot)
        logger.info("Stage 2: sandbox screenshot → %s", screenshot_path)

    logger.info(
        "Stage 2: %d/%d rules passed validation → %s",
        report.passed_count, report.total, validation_path,
    )
    return {
        "total": report.total,
        "passed": report.passed_count,
        "failed": report.failed,
        "passing_rules": report.passing_rules(),
    }


def run_pipeline(report_id: str, verbose: bool = False) -> dict:
    """
    Full pipeline: load crawl result → generate rules → validate → return summary.

    Args:
        report_id: Matches a file at data/crawl_outputs/results/<report_id>.json
        verbose:   Print stage headers and per-rule output to stdout.

    Returns:
        {
            "report_id": str,
            "url": str,
            "environment": str,
            "rules_generated": int,
            "rules_passed": int,
            "rules_failed": int,
            "passing_rules": [str, ...],
            "status": "ok" | "no_rules" | "error",
        }
    """
    crawl_path = Path("data/crawl_outputs/results") / f"{report_id}.json"
    if not crawl_path.exists():
        raise FileNotFoundError(f"Crawl result not found: {crawl_path}")

    with open(crawl_path, encoding="utf-8") as f:
        crawl_result = json.load(f)

    env = crawl_result.get("environment", "desktop")
    url = crawl_result.get("url", "unknown")

    if verbose:
        _separator(f"Pipeline: {report_id}  |  {url}  |  env: {env}")

    # Stage 1
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
        for r in rules:
            print(f"  [{r.rule_type:10}] {r.rule}")
        print(f"\n  {len(rules)} rules generated")

    # Stage 2
    if verbose:
        _separator(f"Stage 2: Rule Validation — {report_id}")

    validation = run_rule_validation(rules, url, report_id)

    if verbose:
        print(f"  Total: {validation['total']}  "
              f"Passed: {validation['passed']}  "
              f"Failed: {validation['failed']}")
        if validation["passed"] == 0:
            print("\n  No rules passed validation.")
        else:
            print(f"\n  {validation['passed']}/{validation['total']} rules ready for moderator review")

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
        description="Run the full rule generation + validation pipeline for a crawl result.",
    )
    parser.add_argument("report_id", help="Report ID (file in data/crawl_outputs/results/)")
    parser.add_argument("--no-sandbox", action="store_true", help="Skip the Playwright sandbox stage")
    args = parser.parse_args()

    try:
        result = run_pipeline(args.report_id, verbose=True)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        sys.exit(1)

    _separator(f"Done — {result['rules_passed']}/{result['rules_generated']} rules passed")

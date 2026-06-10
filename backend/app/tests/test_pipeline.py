"""
AI rule generation + validation pipeline test.

Run from backend/:
    .venv\\Scripts\\activate
    python -m app.tests.test_pipeline <report_id>
    python -m app.tests.test_pipeline <report_id> --no-sandbox

Examples:
    python -m app.tests.test_pipeline reddit-bypass-check
    python -m app.tests.test_pipeline vnexpress --no-sandbox

Input:
    data/crawl_outputs/results/<report_id>.json

Output:
    data/rule_outputs/results/<report_id>_rules.json
        - generated rules
        - token_usage

    data/rule_outputs/validation/<report_id>_validation.json
        - per-rule pass/fail validation result

    data/rule_outputs/screenshots/<report_id>_with_rules.png
        - page after rules applied, validation only
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv

    # This file lives at:
    #   backend/app/tests/test_pipeline.py
    #
    # Common env locations:
    #   backend/.env.local
    #   backend/.env
    #   project-root/.env.local
    #   project-root/.env
    backend_root = Path(__file__).resolve().parents[2]
    project_root = Path(__file__).resolve().parents[3]

    load_dotenv(backend_root / ".env.local")
    load_dotenv(backend_root / ".env")
    load_dotenv(project_root / ".env.local")
    load_dotenv(project_root / ".env")
except ImportError:
    pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


# Output directories — mirrors data/crawl_outputs/ structure
OUT_RESULTS = Path("data/rule_outputs/results")
OUT_VALIDATION = Path("data/rule_outputs/validation")
OUT_SCREENSHOTS = Path("data/rule_outputs/screenshots")


def separator(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def test_rule_generation(crawl_result: Dict[str, Any], report_id: str) -> List[Any]:
    """
    Generate adblock rules from a crawl output and save the generated rules JSON.

    The saved file includes token_usage so reviewers can see how many tokens
    were used for this rule generation run.
    """
    separator(f"Stage 1: AI Rule Generation — {report_id}")

    from app.services.rule_generator import generate_rules_with_metadata

    generation_result = generate_rules_with_metadata(crawl_result)
    rules = generation_result.rules

    if not rules:
        print("  FAIL — No rules generated")
        return []

    for rule in rules:
        print(f"  [{rule.rule_type:10}] {rule.rule}")

    OUT_RESULTS.mkdir(parents=True, exist_ok=True)
    rules_path = OUT_RESULTS / f"{report_id}_rules.json"

    rules_data = {
        "report_id": report_id,
        "environment": crawl_result.get("environment", "desktop"),
        "url": crawl_result.get("url", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rule_count": len(rules),
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

    print_token_usage(generation_result.token_usage)

    print(f"\n  PASS — {len(rules)} rules generated")
    print(f"  Saved: {rules_path}")

    return rules


def test_rule_validation(rules: List[Any], url: str, report_id: str) -> None:
    """
    Run rule validation and save validation output.

    Note:
        validate_rules() may run the Playwright sandbox stage depending on
        the validator implementation.
    """
    separator(f"Stage 2: Rule Validation — {report_id}")

    from app.services.rule_validator import validate_rules

    rule_strings = [rule.rule for rule in rules]
    report = validate_rules(rule_strings, url)

    for outcome in report.outcomes:
        status = (
            "PASS"
            if outcome.passed
            else f"FAIL [{outcome.failure_stage}] {outcome.failure_reason}"
        )
        print(f"  {status:8} {outcome.rule}")

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
                "syntax": syntax_result_to_dict(outcome.syntax),
                "scope": scope_result_to_dict(outcome.scope),
                "sandbox": sandbox_result_to_dict(outcome.sandbox),
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

    if sandbox_result:
        OUT_SCREENSHOTS.mkdir(parents=True, exist_ok=True)

        if sandbox_result.tested_screenshot:
            tested_path = OUT_SCREENSHOTS / f"{report_id}_with_rules.png"
            tested_path.write_bytes(sandbox_result.tested_screenshot)
            print(f"\n  Screenshot (with rules): {tested_path}")
            print(f"  Layout diff: {sandbox_result.layout_diff_pct:.1%}")

    print(f"\n  Total: {report.total}  Passed: {report.passed_count}  Failed: {report.failed}")
    print(f"  Saved: {validation_path}")

    if report.passed_count == 0:
        print("\n  FAIL — No rules passed validation")
    else:
        print(f"\n  PASS — {report.passed_count}/{report.total} rules ready for moderator review")


def print_token_usage(token_usage: Optional[Dict[str, Any]]) -> None:
    """
    Print token usage in terminal after rule generation.
    """
    print("\n  Token usage:")

    if not token_usage:
        print("    unavailable")
        return

    print(f"    model:             {token_usage.get('model', '-')}")
    print(f"    fallback_used:     {token_usage.get('fallback_used', False)}")
    print(f"    prompt_tokens:     {token_usage.get('prompt_tokens', '-')}")
    print(f"    completion_tokens: {token_usage.get('completion_tokens', '-')}")
    print(f"    total_tokens:      {token_usage.get('total_tokens', '-')}")


def syntax_result_to_dict(result: Any) -> Optional[Dict[str, Any]]:
    """
    Convert SyntaxResult into JSON-safe dict.
    """
    if result is None:
        return None

    return {
        "rule": getattr(result, "rule", ""),
        "valid": getattr(result, "valid", False),
        "error": getattr(result, "error", None),
    }


def scope_result_to_dict(result: Any) -> Optional[Dict[str, Any]]:
    """
    Convert ScopeResult into JSON-safe dict.
    """
    if result is None:
        return None

    return {
        "rule": getattr(result, "rule", ""),
        "safe": getattr(result, "safe", False),
        "risk": getattr(result, "risk", None),
        "detail": getattr(result, "detail", None),
    }


def sandbox_result_to_dict(result: Any) -> Optional[Dict[str, Any]]:
    """
    Convert SandboxResult into JSON-safe dict.

    tested_screenshot is intentionally omitted because it is binary data
    and is saved separately as a PNG file.
    """
    if result is None:
        return None

    return {
        "url": getattr(result, "url", ""),
        "passed": getattr(result, "passed", False),
        "ads_blocked": getattr(result, "ads_blocked", False),
        "page_functional": getattr(result, "page_functional", False),
        "layout_diff_pct": getattr(result, "layout_diff_pct", 0.0),
        "blocked_requests": getattr(result, "blocked_requests", []),
        "missing_ad_selectors": getattr(result, "missing_ad_selectors", []),
        "hidden_ad_selectors": getattr(result, "hidden_ad_selectors", []),
        "broken_selectors": getattr(result, "broken_selectors", []),
        "error": getattr(result, "error", ""),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Test AI rule generation + validation pipeline.",
    )

    parser.add_argument(
        "report_id",
        help="Report ID matching a file in data/crawl_outputs/results/",
    )

    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="Skip validation stage for faster testing of generated rules/token usage output.",
    )

    args = parser.parse_args()

    crawl_path = Path("data/crawl_outputs/results") / f"{args.report_id}.json"

    if not crawl_path.exists():
        logger.error("Crawl result not found: %s", crawl_path)
        print("\nRun crawler first, for example:")
        print(f'  python -m app.services.crawler "https://example.vn" "{args.report_id}"')
        sys.exit(1)

    with open(crawl_path, encoding="utf-8") as file:
        crawl_result = json.load(file)

    url = crawl_result.get("url", "unknown")

    separator(f"Pipeline test: {args.report_id}  |  {url}")

    passed = 0
    failed = 0

    try:
        rules = test_rule_generation(crawl_result, args.report_id)

        if rules:
            passed += 1
        else:
            failed += 1

    except Exception as exc:
        print(f"  FAIL — test_rule_generation: {exc}")
        failed += 1
        rules = []

    if rules and not args.no_sandbox:
        try:
            test_rule_validation(rules, url, args.report_id)
            passed += 1
        except Exception as exc:
            print(f"  FAIL — test_rule_validation: {exc}")
            failed += 1

    elif rules and args.no_sandbox:
        print("\n  SKIP — validation stage skipped because --no-sandbox was provided")

    separator(f"Results: {passed} passed, {failed} failed")
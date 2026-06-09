"""
AI rule generation + validation pipeline test.

Run from backend/:
    .venv\\Scripts\\activate
    python -m app.tests.test_pipeline <report_id>
    python -m app.tests.test_pipeline <report_id> --no-sandbox

Examples:
    python -m app.tests.test_pipeline reddit-bypass-check
    python -m app.tests.test_pipeline vnexpress --no-sandbox

Input:   data/crawl_outputs/results/<report_id>.json

Output:
    data/rule_outputs/results/<report_id>_rules.json         — generated rules
    data/rule_outputs/validation/<report_id>_validation.json — per-rule pass/fail
    data/rule_outputs/screenshots/<report_id>_with_rules.png — page after rules applied (sandbox only)
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent.parent / ".env.local")
    load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Output directories — mirrors data/crawl_outputs/ structure
OUT_RESULTS     = Path("data/rule_outputs/results")
OUT_VALIDATION  = Path("data/rule_outputs/validation")
OUT_SCREENSHOTS = Path("data/rule_outputs/screenshots")


def separator(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def test_rule_generation(crawl_result: dict, report_id: str) -> list:
    separator(f"Stage 1: AI Rule Generation — {report_id}")
    from app.services.rule_generator import generate_rules

    rules = generate_rules(crawl_result)

    if not rules:
        print("  FAIL — No rules generated")
        return []

    for r in rules:
        print(f"  [{r.rule_type:10}] {r.rule}")

    OUT_RESULTS.mkdir(parents=True, exist_ok=True)
    rules_path = OUT_RESULTS / f"{report_id}_rules.json"

    rules_data = {
        "report_id": report_id,
        "url": crawl_result.get("url", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rule_count": len(rules),
        "rules": [{"rule": r.rule, "rule_type": r.rule_type, "raw": r.raw} for r in rules],
    }
    with open(rules_path, "w", encoding="utf-8") as f:
        json.dump(rules_data, f, indent=2, ensure_ascii=False)

    print(f"\n  PASS — {len(rules)} rules generated")
    print(f"  Saved: {rules_path}")
    return rules


def test_rule_validation(rules: list, url: str, report_id: str) -> None:
    separator(f"Stage 2: Rule Validation — {report_id}")
    from app.services.rule_validator import validate_rules

    rule_strings = [r.rule for r in rules]
    report = validate_rules(rule_strings, url)

    for o in report.outcomes:
        status = "PASS" if o.passed else f"FAIL [{o.failure_stage}] {o.failure_reason}"
        print(f"  {status:8} {o.rule}")

    # --- Save validation JSON ---
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

    # --- Save sandbox screenshots if available ---
    sandbox_result = next(
        (o.sandbox for o in report.outcomes if o.sandbox is not None), None
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test AI rule generation + validation pipeline.")
    parser.add_argument("report_id", help="Report ID matching a file in data/crawl_outputs/results/")
    parser.add_argument("--no-sandbox", action="store_true", help="Skip the Playwright sandbox test (stages 1+2 only)")
    args = parser.parse_args()

    crawl_path = Path("data/crawl_outputs/results") / f"{args.report_id}.json"
    if not crawl_path.exists():
        logger.error(f"Crawl result not found: {crawl_path}")
        sys.exit(1)

    with open(crawl_path, encoding="utf-8") as f:
        crawl_result = json.load(f)

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
    except Exception as e:
        print(f"  FAIL — test_rule_generation: {e}")
        failed += 1
        rules = []

    if rules:
        try:
            test_rule_validation(rules, url, args.report_id)
            passed += 1
        except Exception as e:
            print(f"  FAIL — test_rule_validation: {e}")
            failed += 1

    separator(f"Results: {passed} passed, {failed} failed")

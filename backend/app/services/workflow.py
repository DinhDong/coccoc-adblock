"""
End-to-end pipeline coordinator.

Reads a crawl result, runs AI rule generation, deduplicates candidates,
validates the remaining candidates, and writes the outputs to data/rule_outputs/.

This workflow supports two processing modes:

- legacy:
    No detailed ticket context is provided. The system performs best-effort
    blocking for detected advertisements.

- ticket_aware:
    The system uses the normalized ticket context, problem type, resolution
    strategy, and preservation requirements to generate a narrower patch.

The workflow also records evaluation timing:

- crawl_elapsed_ms:
    Browser rendering time stored by the crawler.

- generation_elapsed_ms:
    Time spent building the prompt, calling the LLM, parsing the response,
    filtering ticket-incompatible rules, and adding safe detector backfill.

- validation_elapsed_ms:
    Time spent running syntax, scope, policy, per-rule sandbox, and combined
    sandbox validation.

- workflow_elapsed_ms:
    Time spent on rule generation and validation inside this workflow.
    It does not include a separately executed crawler command.

Output:
    data/rule_outputs/results/<report_id>_rules.json
    data/rule_outputs/validation/<report_id>_validation.json
    data/rule_outputs/screenshots/<report_id>_with_rules.png
"""

from __future__ import annotations

import json
import logging
import sys
import time
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

from app.database import (
    save_crawl_input,
    save_rule_output,
    save_rule_validation,
    rule_output_exists,
)
from app.tickets import update_ticket_status

logger = logging.getLogger(__name__)

OUT_RESULTS = Path("data/rule_outputs/results")
OUT_VALIDATION = Path("data/rule_outputs/validation")
OUT_SCREENSHOTS = Path("data/rule_outputs/screenshots")
CRAWL_RESULTS = Path("data/crawl_outputs/results")


def _separator(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def _ensure_rule_outputs_from_files(report_id: str, rules_path: Path, validation_path: Path) -> None:
    if rule_output_exists(report_id):
        return

    if rules_path.exists():
        try:
            with open(rules_path, encoding="utf-8") as file:
                rules_data = json.load(file)

            save_rule_output(
                report_id=report_id,
                rules=rules_data,
                input_tokens=None,
                output_tokens=None,
                status=rules_data.get("status", "generated"),
            )
        except Exception as exc:
            logger.warning(
                "Fallback: failed to save rule output from file for %s: %s",
                report_id,
                exc,
                exc_info=True,
            )

    if validation_path.exists():
        try:
            with open(validation_path, encoding="utf-8") as file:
                validation_data = json.load(file)

            save_rule_validation(
                report_id=report_id,
                validation_result=validation_data,
                after_screenshot=validation_data.get("combined_screenshot"),
                status=validation_data.get("status", "validated"),
            )
        except Exception as exc:
            logger.warning(
                "Fallback: failed to save validation output from file for %s: %s",
                report_id,
                exc,
                exc_info=True,
            )


def _elapsed_ms(started_at: float) -> int:
    """
    Return elapsed monotonic time in milliseconds.

    perf_counter is used instead of wall-clock timestamps so the measurement is
    not affected by system clock changes.
    """
    elapsed_seconds = time.perf_counter() - started_at
    return max(0, round(elapsed_seconds * 1000))


def run_rule_generation(
    crawl_result: Dict[str, Any],
    report_id: str,
    skip_external: bool = False,
    discard_existing: bool = False,
) -> List[Any]:
    """
    Stage 1 — call the LLM, parse the response, deduplicate candidates, and
    persist the new rule list.

    generation_elapsed_ms measures generate_rules_with_metadata(), including:

    - compact-signal extraction;
    - prompt construction;
    - LLM request;
    - response parsing;
    - ticket-scope filtering;
    - safe detector backfill.

    Public-list and internal-registry deduplication are performed afterwards and
    are not counted as AI generation time.
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
    processing_mode = _get_processing_mode(ticket_context)
    crawl_elapsed_ms = _extract_crawl_elapsed_ms(crawl_result)

    url = crawl_result.get("url", "")
    domain = get_domain(url)

    generation_started = time.perf_counter()

    generation_result = generate_rules_with_metadata(crawl_result)

    generation_elapsed_ms = _elapsed_ms(generation_started)

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

    generation_error = getattr(generation_result, "error", "")

    if generation_error:
        # Generation aborted (missing API key, network/LLM failure). Do not let
        # this reach the caller as an ordinary empty result — it is a failure.
        logger.error(
            "Stage 1: rule generation failed for %s: %s",
            report_id,
            generation_error,
        )
        raise RuntimeError(f"Rule generation failed: {generation_error}")

    if not generated_rules:
        logger.warning(
            "Stage 1: no rules generated for %s | generation_elapsed_ms=%d",
            report_id,
            generation_elapsed_ms,
        )
        return []

    if discard_existing:
        cleared = clear_rules(domain)

        if cleared:
            logger.info(
                "Stage 1: cleared %d existing rule(s) for %s (discard mode)",
                cleared,
                domain,
            )

    rules, internal_dupes = filter_new_rules(url, generated_rules)

    if internal_dupes:
        logger.info(
            "Stage 1: skipped %d rule(s) already in internal registry for %s",
            len(internal_dupes),
            domain,
        )

    rules, external_dupes = filter_uncovered(
        rules,
        skip=skip_external,
    )

    if external_dupes:
        for rule, source in external_dupes:
            logger.info(
                "Stage 1: skipped rule already in %s: %s",
                source,
                _coerce_rule(rule),
            )

    total_skipped = len(internal_dupes) + len(external_dupes)

    OUT_RESULTS.mkdir(parents=True, exist_ok=True)
    rules_path = OUT_RESULTS / f"{report_id}_rules.json"

    rules_data = {
        "report_id": report_id,
        "environment": crawl_result.get("environment", "desktop"),
        "url": url,
        "processing_mode": processing_mode,
        "ticket_context": ticket_context,
        "problem_type": problem_type,
        "resolution_strategy": resolution_strategy,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "generated" if rules else "no_rules",
        "crawl_elapsed_ms": crawl_elapsed_ms,
        "generation_elapsed_ms": generation_elapsed_ms,
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
                "raw": getattr(
                    rule,
                    "raw",
                    _coerce_rule(rule),
                ),
            }
            for rule in rules
        ],
    }

    with open(rules_path, "w", encoding="utf-8") as file:
        json.dump(
            _make_json_safe(rules_data),
            file,
            indent=2,
            ensure_ascii=False,
        )

    try:
        save_rule_output(
            report_id=report_id,
            rules=rules_data,
            input_tokens=(
                int(token_usage.get("prompt_tokens", 0))
                if isinstance(token_usage, Mapping)
                else None
            ),
            output_tokens=(
                int(token_usage.get("completion_tokens", 0))
                if isinstance(token_usage, Mapping)
                else None
            ),
            status="generated" if rules else "no_rules",
        )
    except Exception as exc:
        logger.warning(
            "Stage 1: failed saving rule output to DB: %s",
            exc,
            exc_info=True,
        )

    if not rules:
        # Everything the model proposed was already known. The blob written
        # above still carries duplicates_skipped so the UI can explain why
        # this run has nothing to review.
        logger.info(
            "Stage 1: all %d generated rule(s) were duplicates for %s",
            len(generated_rules),
            report_id,
        )
        _log_token_usage(token_usage)
        return []

    register_rules(
        domain,
        [
            normalize_rule(_coerce_rule(rule))
            for rule in rules
        ],
    )

    _log_token_usage(token_usage)

    logger.info(
        "Stage 1: %d new rule(s) saved → %s | "
        "%d duplicate(s) skipped | problem_type=%s | "
        "processing_mode=%s | generation_elapsed_ms=%d",
        len(rules),
        rules_path,
        total_skipped,
        problem_type,
        processing_mode,
        generation_elapsed_ms,
    )

    return rules


def run_rule_validation(
    rules: List[Any],
    url: str,
    report_id: str,
    environment: str = "desktop",
    ticket_context: Optional[Mapping[str, Any]] = None,
    run_sandbox_checks: bool = True,
    crawl_elapsed_ms: Optional[int] = None,
    generation_elapsed_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Stage 2 — run syntax, scope, policy, per-rule sandbox, and combined sandbox.

    validation_elapsed_ms measures the complete validate_rules() execution.

    average_validation_time_per_rule_ms is provided only as a normalized
    comparison value. Sandbox sessions share setup work, so it must not be
    interpreted as the exact isolated time of one rule.
    """
    from app.services.rule_validator import validate_rules
    from app.services.ticket_context import normalize_ticket_context

    normalized_ticket_context = normalize_ticket_context(
        ticket_context or {}
    )

    problem_type = _get_problem_type(normalized_ticket_context)
    resolution_strategy = _get_resolution_strategy(
        normalized_ticket_context
    )
    processing_mode = _get_processing_mode(
        normalized_ticket_context
    )

    rule_strings = [
        _coerce_rule(rule)
        for rule in rules
        if _coerce_rule(rule)
    ]

    validation_started = time.perf_counter()

    report = validate_rules(
        rule_strings,
        url,
        environment=environment,
        ticket_context=normalized_ticket_context,
        run_sandbox_checks=run_sandbox_checks,
    )

    validation_elapsed_ms = _elapsed_ms(validation_started)

    validated_rule_count = len(rule_strings)

    if validated_rule_count:
        average_validation_time_per_rule_ms = round(
            validation_elapsed_ms / validated_rule_count,
            2,
        )
    else:
        average_validation_time_per_rule_ms = 0.0

    OUT_VALIDATION.mkdir(parents=True, exist_ok=True)
    validation_path = (
        OUT_VALIDATION / f"{report_id}_validation.json"
    )

    combined_sandbox = getattr(
        report,
        "combined_sandbox",
        None,
    )

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
        "environment": environment,
        "processing_mode": processing_mode,
        "ticket_context": normalized_ticket_context,
        "problem_type": getattr(
            report,
            "problem_type",
            problem_type,
        ),
        "resolution_strategy": getattr(
            report,
            "resolution_strategy",
            resolution_strategy,
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "crawl_elapsed_ms": crawl_elapsed_ms,
        "generation_elapsed_ms": generation_elapsed_ms,
        "validation_elapsed_ms": validation_elapsed_ms,
        "validated_rule_count": validated_rule_count,
        "average_validation_time_per_rule_ms": (
            average_validation_time_per_rule_ms
        ),
        "total": report.total,
        "passed": report.passed_count,
        "failed": report.failed,
        "passing_rules": report.passing_rules(),
        "combined_screenshot": combined_screenshot_path,
        "combined_sandbox": _serialize_sandbox_result(
            combined_sandbox
        ),
        "outcomes": [
            _serialize_outcome(outcome)
            for outcome in report.outcomes
        ],
    }

    with open(
        validation_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            _make_json_safe(validation_data),
            file,
            indent=2,
            ensure_ascii=False,
        )

    try:
        save_rule_validation(
            report_id=report_id,
            validation_result=validation_data,
            after_screenshot=combined_screenshot_path,
            status="validated",
        )
    except Exception as exc:
        logger.warning(
            "Stage 2: failed saving validation result to DB: %s",
            exc,
            exc_info=True,
        )

    logger.info(
        "Stage 2: %d/%d rules passed validation → %s | "
        "problem_type=%s | strategy=%s | processing_mode=%s | "
        "validation_elapsed_ms=%d | average_per_rule_ms=%.2f | "
        "combined_screenshot=%s",
        report.passed_count,
        report.total,
        validation_path,
        validation_data["problem_type"],
        validation_data["resolution_strategy"],
        processing_mode,
        validation_elapsed_ms,
        average_validation_time_per_rule_ms,
        combined_screenshot_path or "n/a",
    )

    return {
        "total": report.total,
        "passed": report.passed_count,
        "failed": report.failed,
        "passing_rules": report.passing_rules(),
        "problem_type": validation_data["problem_type"],
        "resolution_strategy": (
            validation_data["resolution_strategy"]
        ),
        "processing_mode": processing_mode,
        "validation_elapsed_ms": validation_elapsed_ms,
        "validated_rule_count": validated_rule_count,
        "average_validation_time_per_rule_ms": (
            average_validation_time_per_rule_ms
        ),
        "combined_screenshot": combined_screenshot_path,
        "combined_sandbox_passed": (
            bool(getattr(combined_sandbox, "passed", False))
            if combined_sandbox
            else False
        ),
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
    Stage 0 — crawl URL and persist the crawl result.
    """
    from app.services.crawler import CrawlService

    if verbose:
        _separator(
            f"Stage 0: Crawl — {report_id} | "
            f"{url} | env: {environment}"
        )

    service = CrawlService()

    result = service.crawl_url(
        url=url,
        report_id=report_id,
        ticket_context=(
            dict(ticket_context)
            if ticket_context
            else None
        ),
        focus_region=focus_region or None,
        environment=environment,
        **render_kwargs,
    )

    status = result.get("status", "unknown")

    if status == "success":
        logger.info(
            "Stage 0: crawl succeeded for %s "
            "(report_id: %s, env: %s)",
            url,
            report_id,
            result.get("environment", environment),
        )

        if verbose:
            screenshot = (
                result.get("files", {}).get("screenshot", "")
            )
            candidates = len(
                result.get("ad_candidates", []) or []
            )
            crawl_elapsed_ms = _extract_crawl_elapsed_ms(
                result
            )

            print(
                f"  Crawl OK — {candidates} "
                "ad candidate(s) detected"
            )
            print(
                f"  Crawl time: {crawl_elapsed_ms} ms"
            )

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
                f"  Crawl FAILED at stage "
                f"'{result.get('stage', 'unknown')}': "
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
    interactive: bool = True,
    **render_kwargs: Any,
) -> Dict[str, Any]:
    """
    Full pipeline:

        optional crawl
        → load crawl result
        → normalize ticket context
        → generate rules
        → optionally validate
        → return summary

    workflow_elapsed_ms starts immediately before rule generation. It therefore
    represents generation plus validation and excludes a separately executed
    crawler command.

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
        interactive:    If False, keep existing rules without prompting.
        **render_kwargs: Extra render options forwarded to the crawler.
    """
    from app.services.rule_registry import (
        get_domain,
        get_existing_rules,
    )

    crawl_path = CRAWL_RESULTS / f"{report_id}.json"

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
            update_ticket_status(report_id, "crawl_failed")
            return {
                "report_id": report_id,
                "url": url,
                "environment": environment,
                "ticket_context": (
                    dict(ticket_context)
                    if ticket_context
                    else {}
                ),
                "problem_type": _get_problem_type(
                    ticket_context
                ),
                "resolution_strategy": (
                    _get_resolution_strategy(ticket_context)
                ),
                "processing_mode": _get_processing_mode(
                    ticket_context
                ),
                "crawl_elapsed_ms": (
                    _extract_crawl_elapsed_ms(
                        crawl_outcome
                    )
                ),
                "generation_elapsed_ms": None,
                "validation_elapsed_ms": None,
                "workflow_elapsed_ms": None,
                "rules_generated": 0,
                "rules_passed": 0,
                "rules_failed": 0,
                "passing_rules": [],
                "status": "crawl_failed",
                "crawl_stage": crawl_outcome.get(
                    "stage",
                    "unknown",
                ),
                "crawl_error": crawl_outcome.get(
                    "error",
                    "unknown error",
                ),
            }

    if not crawl_path.exists():
        raise FileNotFoundError(
            f"Crawl result not found: {crawl_path}\n"
            f"Run: python -m app.services.crawler "
            f"<url> {report_id} --env desktop\n"
            f"Or crawl + generate in one step: "
            f"python -m app.services.workflow "
            f"{report_id} --url <url> --env desktop"
        )

    with open(
        crawl_path,
        encoding="utf-8",
    ) as file:
        crawl_result = json.load(file)

    crawl_result = _prepare_crawl_result(crawl_result)

    env = crawl_result.get("environment", "desktop")
    page_url = crawl_result.get("url", "unknown")
    normalized_ticket_context = crawl_result.get(
        "ticket_context",
        {},
    )

    problem_type = _get_problem_type(
        normalized_ticket_context
    )
    resolution_strategy = _get_resolution_strategy(
        normalized_ticket_context
    )
    processing_mode = _get_processing_mode(
        normalized_ticket_context
    )
    crawl_elapsed_ms = _extract_crawl_elapsed_ms(
        crawl_result
    )

    crawl_screenshot = (
        crawl_result.get("files", {}).get("screenshot", "")
    )

    if crawl_screenshot:
        logger.info(
            "Crawl screenshot → %s",
            crawl_screenshot,
        )

    if verbose:
        _separator(
            f"Pipeline: {report_id} | "
            f"{page_url} | env: {env}"
        )

        print(f"  Processing mode: {processing_mode}")
        print(f"  Problem type: {problem_type}")
        print(f"  Strategy: {resolution_strategy}")
        print(f"  Crawl time: {crawl_elapsed_ms} ms")

    try:
        update_ticket_status(report_id, "generating")
    except Exception:
        logger.warning(
            "Pipeline: could not set generating status for %s",
            report_id,
        )

    try:
        save_crawl_input(
            report_id=report_id,
            domain=get_domain(page_url),
            url=page_url,
            ticket_context=normalized_ticket_context,
            status="generating",
            crawl_duration_ms=crawl_elapsed_ms,
            before_screenshot=crawl_screenshot,
        )
    except Exception:
        logger.warning(
            "Pipeline: could not update generate status for %s",
            report_id,
        )

    domain = get_domain(page_url)
    existing = get_existing_rules(domain)
    discard_existing = False

    if existing and verbose and interactive:
        print(
            f"  Domain '{domain}' already has "
            f"{len(existing)} rule(s) in the registry."
        )
        print(
            "  [1] Discard old rules — overwrite "
            "with fresh generation"
        )
        print(
            "  [2] Keep old rules — only add new ones "
            "(default)"
        )
        print("  [3] Abort")

        try:
            choice = (
                input("  Choice [1/2/3]: ").strip()
                or "2"
            )
        except (EOFError, KeyboardInterrupt):
            choice = "2"

        if choice == "1":
            discard_existing = True
            print()
        elif choice == "3":
            print("\n  Aborted.")

            return {
                "report_id": report_id,
                "url": page_url,
                "environment": env,
                "ticket_context": normalized_ticket_context,
                "problem_type": problem_type,
                "resolution_strategy": resolution_strategy,
                "processing_mode": processing_mode,
                "crawl_elapsed_ms": crawl_elapsed_ms,
                "generation_elapsed_ms": None,
                "validation_elapsed_ms": None,
                "workflow_elapsed_ms": None,
                "rules_generated": 0,
                "rules_passed": 0,
                "rules_failed": 0,
                "passing_rules": [],
                "status": "aborted",
            }
        else:
            print()

    if verbose:
        _separator(
            f"Stage 1: Rule Generation — {report_id}"
        )

    workflow_started = time.perf_counter()

    rules = run_rule_generation(
        crawl_result,
        report_id,
        skip_external=skip_external,
        discard_existing=discard_existing,
    )

    rules_path = OUT_RESULTS / f"{report_id}_rules.json"
    rules_data = _load_json_mapping(rules_path)

    generation_elapsed_ms = _optional_int(
        rules_data.get("generation_elapsed_ms")
    )

    if not rules:
        workflow_elapsed_ms = _elapsed_ms(
            workflow_started
        )

        try:
            save_crawl_input(
                report_id=report_id,
                domain=get_domain(page_url),
                url=page_url,
                ticket_context=normalized_ticket_context,
                status="review",
                crawl_duration_ms=crawl_elapsed_ms,
                before_screenshot=crawl_screenshot,
            )
        except Exception:
            logger.warning(
                "Pipeline: could not update review status for %s",
                report_id,
            )

        try:
            # Prefer the blob Stage 1 wrote — when every candidate was a
            # duplicate it carries duplicates_skipped, which the UI needs to
            # explain the empty result. Fall back to [] if Stage 1 never got
            # far enough to write one.
            save_rule_output(
                report_id=report_id,
                rules=rules_data or [],
                input_tokens=None,
                output_tokens=None,
                status="no_rules",
            )
        except Exception as exc:
            logger.warning(
                "Pipeline: failed saving empty rule output to DB: %s",
                exc,
            )

        if verbose:
            print(
                "  No new rules generated — "
                "pipeline stopped."
            )

        return {
            "report_id": report_id,
            "url": page_url,
            "environment": env,
            "problem_type": problem_type,
            "resolution_strategy": resolution_strategy,
            "processing_mode": processing_mode,
            "crawl_elapsed_ms": crawl_elapsed_ms,
            "generation_elapsed_ms": generation_elapsed_ms,
            "validation_elapsed_ms": None,
            "workflow_elapsed_ms": workflow_elapsed_ms,
            "rules_generated": 0,
            "rules_passed": 0,
            "rules_failed": 0,
            "passing_rules": [],
            "status": "no_rules",
        }

    if verbose:
        for rule in rules:
            print(
                f"  [{getattr(rule, 'rule_type', ''):10}] "
                f"{_coerce_rule(rule)}"
            )

        print(f"\n  {len(rules)} new rules generated")
        print(
            f"  Generation time: "
            f"{generation_elapsed_ms} ms"
        )
        print(f"  Problem type: {problem_type}")
        print(f"  Strategy: {resolution_strategy}")

    if not run_validation:
        workflow_elapsed_ms = _elapsed_ms(
            workflow_started
        )

        _update_json_mapping(
            rules_path,
            {
                "workflow_elapsed_ms": (
                    workflow_elapsed_ms
                ),
            },
        )

        if verbose:
            print(
                "\n  SKIP — validation stage skipped "
                "because --no-sandbox was provided"
            )

        return {
            "report_id": report_id,
            "url": page_url,
            "environment": env,
            "problem_type": problem_type,
            "resolution_strategy": resolution_strategy,
            "processing_mode": processing_mode,
            "crawl_elapsed_ms": crawl_elapsed_ms,
            "generation_elapsed_ms": generation_elapsed_ms,
            "validation_elapsed_ms": None,
            "workflow_elapsed_ms": workflow_elapsed_ms,
            "rules_generated": len(rules),
            "rules_passed": 0,
            "rules_failed": 0,
            "passing_rules": [],
            "status": "generated",
        }

    if verbose:
        _separator(
            f"Stage 2: Rule Validation — {report_id}"
        )

    try:
        update_ticket_status(report_id, "validating")
    except Exception:
        logger.warning(
            "Pipeline: could not set validating status for %s",
            report_id,
        )

    try:
        save_crawl_input(
            report_id=report_id,
            domain=get_domain(page_url),
            url=page_url,
            ticket_context=normalized_ticket_context,
            status="validating",
            crawl_duration_ms=crawl_elapsed_ms,
            before_screenshot=crawl_screenshot,
        )
    except Exception:
        logger.warning(
            "Pipeline: could not update validating status for %s",
            report_id,
        )

    validation = run_rule_validation(
        rules=rules,
        url=page_url,
        report_id=report_id,
        environment=env,
        ticket_context=normalized_ticket_context,
        run_sandbox_checks=True,
        crawl_elapsed_ms=crawl_elapsed_ms,
        generation_elapsed_ms=generation_elapsed_ms,
    )

    workflow_elapsed_ms = _elapsed_ms(
        workflow_started
    )

    validation_path = (
        OUT_VALIDATION / f"{report_id}_validation.json"
    )

    _update_json_mapping(
        rules_path,
        {
            "workflow_elapsed_ms": (
                workflow_elapsed_ms
            ),
        },
    )

    _update_json_mapping(
        validation_path,
        {
            "workflow_elapsed_ms": (
                workflow_elapsed_ms
            ),
        },
    )

    if verbose:
        print(
            f"  Total: {validation['total']}  "
            f"Passed: {validation['passed']}  "
            f"Failed: {validation['failed']}"
        )

        print(
            f"  Validation time: "
            f"{validation['validation_elapsed_ms']} ms"
        )
        print(
            "  Average validation time per rule: "
            f"{validation['average_validation_time_per_rule_ms']} ms"
        )
        print(
            f"  Generation + validation workflow time: "
            f"{workflow_elapsed_ms} ms"
        )

        combined_screenshot = validation.get(
            "combined_screenshot",
            "",
        )

        if combined_screenshot:
            print(
                f"  Combined screenshot: "
                f"{combined_screenshot}"
            )

        if validation["passed"] == 0:
            print("\n  No rules passed validation.")
        else:
            print(
                f"\n  {validation['passed']}/"
                f"{validation['total']} rules ready "
                "for moderator review"
            )

    try:
        _ensure_rule_outputs_from_files(
            report_id,
            rules_path,
            validation_path,
        )
    except Exception as exc:
        logger.warning(
            "Pipeline: fallback rule_outputs persistence failed for %s: %s",
            report_id,
            exc,
            exc_info=True,
        )

    return {
        "report_id": report_id,
        "url": page_url,
        "environment": env,
        "problem_type": validation["problem_type"],
        "resolution_strategy": (
            validation["resolution_strategy"]
        ),
        "processing_mode": processing_mode,
        "crawl_elapsed_ms": crawl_elapsed_ms,
        "generation_elapsed_ms": generation_elapsed_ms,
        "validation_elapsed_ms": (
            validation["validation_elapsed_ms"]
        ),
        "validated_rule_count": (
            validation["validated_rule_count"]
        ),
        "average_validation_time_per_rule_ms": (
            validation[
                "average_validation_time_per_rule_ms"
            ]
        ),
        "workflow_elapsed_ms": workflow_elapsed_ms,
        "rules_generated": len(rules),
        "rules_passed": validation["passed"],
        "rules_failed": validation["failed"],
        "passing_rules": validation["passing_rules"],
        "combined_screenshot": validation.get(
            "combined_screenshot",
            "",
        ),
        "combined_sandbox_passed": validation.get(
            "combined_sandbox_passed",
            False,
        ),
        "status": "review",
    }


def _prepare_crawl_result(
    crawl_result: Mapping[str, Any],
) -> Dict[str, Any]:
    """
    Normalize ticket_context inside a crawl result before generation and
    validation.
    """
    from app.services.ticket_context import (
        normalize_ticket_context,
    )

    prepared = dict(crawl_result)

    prepared["ticket_context"] = (
        normalize_ticket_context(
            prepared.get("ticket_context", {})
        )
    )

    return prepared


def _extract_crawl_elapsed_ms(
    crawl_result: Any,
) -> Optional[int]:
    """
    Read browser-render duration from the crawler output.
    """
    if not isinstance(crawl_result, Mapping):
        return None

    render = crawl_result.get("render", {})

    if not isinstance(render, Mapping):
        return None

    return _optional_int(render.get("elapsed_ms"))


def _get_processing_mode(
    ticket_context: Any,
) -> str:
    """
    Distinguish legacy best-effort mode from ticket-aware mode.

    The normalized empty-ticket context uses:
        evidence_level = legacy_no_ticket_context
    """
    if not isinstance(ticket_context, Mapping):
        return "legacy"

    evidence_level = str(
        ticket_context.get("evidence_level", "") or ""
    ).strip().lower()

    if evidence_level == "legacy_no_ticket_context":
        return "legacy"

    user_fields = (
        "request",
        "description",
        "steps",
        "actual",
        "expected",
    )

    has_user_context = any(
        bool(ticket_context.get(field))
        for field in user_fields
    )

    return (
        "ticket_aware"
        if has_user_context
        else "legacy"
    )


def _load_json_mapping(path: Path) -> Dict[str, Any]:
    """
    Load a JSON object safely.

    Returns an empty dictionary when the file does not exist, contains invalid
    JSON, or does not contain a JSON object.
    """
    if not path.exists():
        return {}

    try:
        with open(
            path,
            encoding="utf-8",
        ) as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not read JSON file %s: %s",
            path,
            exc,
        )
        return {}

    if isinstance(value, dict):
        return value

    return {}


def _update_json_mapping(
    path: Path,
    updates: Mapping[str, Any],
) -> None:
    """
    Merge top-level fields into an existing JSON object.
    """
    data = _load_json_mapping(path)

    if not data:
        return

    data.update(dict(updates))

    try:
        with open(
            path,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                _make_json_safe(data),
                file,
                indent=2,
                ensure_ascii=False,
            )
    except OSError as exc:
        logger.warning(
            "Could not update JSON file %s: %s",
            path,
            exc,
        )


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _serialize_outcome(
    outcome: Any,
) -> Dict[str, Any]:
    return {
        "rule": getattr(outcome, "rule", ""),
        "passed": bool(
            getattr(outcome, "passed", False)
        ),
        "failure_stage": getattr(
            outcome,
            "failure_stage",
            "",
        ),
        "failure_reason": getattr(
            outcome,
            "failure_reason",
            "",
        ),
        "syntax": _serialize_syntax_result(
            getattr(outcome, "syntax", None)
        ),
        "scope": _serialize_scope_result(
            getattr(outcome, "scope", None)
        ),
        "policy": _serialize_policy_result(
            getattr(outcome, "policy", None)
        ),
        "sandbox": _serialize_sandbox_result(
            getattr(outcome, "sandbox", None)
        ),
    }


def _serialize_syntax_result(
    result: Any,
) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    return {
        "rule": getattr(result, "rule", ""),
        "valid": bool(
            getattr(result, "valid", False)
        ),
        "error": getattr(result, "error", None),
    }


def _serialize_scope_result(
    result: Any,
) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    return {
        "rule": getattr(result, "rule", ""),
        "safe": bool(
            getattr(result, "safe", False)
        ),
        "risk": getattr(result, "risk", None),
        "detail": getattr(result, "detail", None),
    }


def _serialize_policy_result(
    result: Any,
) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    return {
        "rule": getattr(result, "rule", ""),
        "valid": bool(
            getattr(result, "valid", False)
        ),
        "problem_type": getattr(
            result,
            "problem_type",
            "",
        ),
        "resolution_strategy": getattr(
            result,
            "resolution_strategy",
            "",
        ),
        "rule_direction": getattr(
            result,
            "rule_direction",
            "",
        ),
        "error": getattr(result, "error", None),
    }


def _serialize_sandbox_result(
    result: Any,
) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    tested_screenshot = getattr(
        result,
        "tested_screenshot",
        None,
    )

    return {
        "url": getattr(result, "url", ""),
        "passed": bool(
            getattr(result, "passed", False)
        ),
        "ads_blocked": bool(
            getattr(result, "ads_blocked", False)
        ),
        "page_functional": bool(
            getattr(result, "page_functional", False)
        ),
        "ticket_assertions_passed": bool(
            getattr(
                result,
                "ticket_assertions_passed",
                True,
            )
        ),
        "ticket_assertion_errors": list(
            getattr(
                result,
                "ticket_assertion_errors",
                [],
            )
            or []
        ),
        "baseline_ticket_assertions_passed": bool(
            getattr(
                result,
                "baseline_ticket_assertions_passed",
                True,
            )
        ),
        "baseline_ticket_assertion_errors": list(
            getattr(
                result,
                "baseline_ticket_assertion_errors",
                [],
            )
            or []
        ),
        "existing_rules_count": int(
            getattr(
                result,
                "existing_rules_count",
                0,
            )
            or 0
        ),
        "candidate_rules_count": int(
            getattr(
                result,
                "candidate_rules_count",
                0,
            )
            or 0
        ),
        "blocked_requests": list(
            getattr(
                result,
                "blocked_requests",
                [],
            )
            or []
        ),
        "candidate_blocked_requests": list(
            getattr(
                result,
                "candidate_blocked_requests",
                [],
            )
            or []
        ),
        "missing_ad_selectors": list(
            getattr(
                result,
                "missing_ad_selectors",
                [],
            )
            or []
        ),
        "hidden_ad_selectors": list(
            getattr(
                result,
                "hidden_ad_selectors",
                [],
            )
            or []
        ),
        "broken_selectors": list(
            getattr(
                result,
                "broken_selectors",
                [],
            )
            or []
        ),
        "error": getattr(result, "error", ""),
        "unreachable": bool(
            getattr(result, "unreachable", False)
        ),
        "tested_screenshot_saved": bool(
            tested_screenshot
        ),
    }


def _save_combined_sandbox_screenshot(
    sandbox_result: Any,
    report_id: str,
) -> str:
    """
    Save the screenshot from the combined sandbox run.
    """
    if not sandbox_result:
        return ""

    tested_screenshot = getattr(
        sandbox_result,
        "tested_screenshot",
        None,
    )

    if not tested_screenshot:
        return ""

    OUT_SCREENSHOTS.mkdir(
        parents=True,
        exist_ok=True,
    )

    screenshot_path = (
        OUT_SCREENSHOTS
        / f"{report_id}_with_rules.png"
    )

    screenshot_path.write_bytes(
        tested_screenshot
    )

    logger.info(
        "Stage 2: combined sandbox screenshot → %s",
        screenshot_path,
    )

    return str(screenshot_path)


def _save_first_sandbox_screenshot(
    outcomes: List[Any],
    report_id: str,
) -> str:
    """
    Save the first available per-rule screenshot when a combined screenshot is
    unavailable.
    """
    sandbox_result = next(
        (
            getattr(outcome, "sandbox", None)
            for outcome in outcomes
            if getattr(
                outcome,
                "sandbox",
                None,
            )
            is not None
        ),
        None,
    )

    if not sandbox_result:
        return ""

    tested_screenshot = getattr(
        sandbox_result,
        "tested_screenshot",
        None,
    )

    if not tested_screenshot:
        return ""

    OUT_SCREENSHOTS.mkdir(
        parents=True,
        exist_ok=True,
    )

    screenshot_path = (
        OUT_SCREENSHOTS
        / f"{report_id}_with_rules.png"
    )

    screenshot_path.write_bytes(
        tested_screenshot
    )

    logger.info(
        "Stage 2: fallback per-rule screenshot → %s",
        screenshot_path,
    )

    return str(screenshot_path)


def _coerce_rule(rule: Any) -> str:
    if hasattr(rule, "rule"):
        return str(
            getattr(rule, "rule")
        ).strip()

    return str(rule).strip()


def _get_problem_type(
    ticket_context: Any,
) -> str:
    if isinstance(ticket_context, Mapping):
        return str(
            ticket_context.get(
                "problem_type",
                "unknown",
            )
        )

    return "unknown"


def _get_resolution_strategy(
    ticket_context: Any,
) -> str:
    if isinstance(ticket_context, Mapping):
        return str(
            ticket_context.get(
                "resolution_strategy",
                "unknown",
            )
        )

    return "unknown"


def _log_token_usage(
    token_usage: Optional[Mapping[str, Any]],
) -> None:
    if not token_usage:
        logger.info(
            "Stage 1: token usage unavailable"
        )
        return

    logger.info(
        "Stage 1 token usage | model=%s | "
        "fallback_used=%s | prompt=%s | "
        "completion=%s | total=%s",
        token_usage.get("model", ""),
        token_usage.get("fallback_used", False),
        token_usage.get("prompt_tokens", ""),
        token_usage.get("completion_tokens", ""),
        token_usage.get("total_tokens", ""),
    )


def _make_json_safe(value: Any) -> Any:
    if value is None or isinstance(
        value,
        (str, int, float, bool),
    ):
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
        format=(
            "%(asctime)s [%(levelname)s] "
            "%(name)s: %(message)s"
        ),
        datefmt="%H:%M:%S",
    )

    from app.crawler.browser import ENVIRONMENTS
    from app.services.crawler import (
        _load_ticket_context_from_cli,
    )

    valid_envs = list(ENVIRONMENTS.keys())

    parser = argparse.ArgumentParser(
        description=(
            "Crawl a URL optionally and run the rule "
            "generation and validation pipeline."
        ),
    )

    parser.add_argument(
        "report_id",
        help=(
            "Report ID matching "
            "data/crawl_outputs/results/"
            "<report_id>.json"
        ),
    )

    parser.add_argument(
        "--url",
        default="",
        help=(
            "URL to crawl first. When omitted, an "
            "existing crawl result is reused."
        ),
    )

    parser.add_argument(
        "--env",
        default="desktop",
        choices=valid_envs,
        metavar="ENV",
        help=(
            "Crawl environment: "
            f"{', '.join(valid_envs)} "
            "(default: desktop)."
        ),
    )

    parser.add_argument(
        "--focus",
        default="",
        metavar="REGION",
        help=(
            "Optional page-region focus forwarded "
            "to the crawler."
        ),
    )

    parser.add_argument(
        "--no-headless",
        action="store_true",
        help=(
            "Open a visible browser window during "
            "the optional crawl."
        ),
    )

    parser.add_argument(
        "--ticket-context-json",
        default="",
        help=(
            "Raw JSON containing ticket context."
        ),
    )

    parser.add_argument(
        "--ticket-context-file",
        default="",
        help=(
            "Path to a JSON file containing ticket "
            "context."
        ),
    )

    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help=(
            "Skip validation and sandbox testing."
        ),
    )

    parser.add_argument(
        "--no-external",
        action="store_true",
        help=(
            "Skip public external filter-list checks."
        ),
    )

    args = parser.parse_args()

    cli_ticket_context = (
        _load_ticket_context_from_cli(
            ticket_context_json=(
                args.ticket_context_json
            ),
            ticket_context_file=(
                args.ticket_context_file
            ),
        )
    )

    if args.focus:
        cli_ticket_context = dict(
            cli_ticket_context or {}
        )
        cli_ticket_context.setdefault(
            "focus_region",
            args.focus,
        )

    try:
        result = run_pipeline(
            report_id=args.report_id,
            verbose=True,
            run_validation=not args.no_sandbox,
            skip_external=args.no_external,
            url=args.url or None,
            environment=args.env,
            ticket_context=(
                cli_ticket_context or None
            ),
            headless=not args.no_headless,
            enable_scroll=True,
        )
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except Exception as exc:
        logger.exception(
            "Pipeline failed: %s",
            exc,
        )
        sys.exit(1)

    if args.no_sandbox:
        _separator(
            f"Done — {result['rules_generated']} "
            "rules generated | "
            f"mode: {result.get('processing_mode', 'unknown')} | "
            f"ticket: {result.get('problem_type', 'unknown')} | "
            f"strategy: {result.get('resolution_strategy', 'unknown')}"
        )
    else:
        _separator(
            f"Done — {result['rules_passed']}/"
            f"{result['rules_generated']} rules passed | "
            f"mode: {result.get('processing_mode', 'unknown')} | "
            f"ticket: {result.get('problem_type', 'unknown')} | "
            f"strategy: {result.get('resolution_strategy', 'unknown')}"
        )

    if result.get("workflow_elapsed_ms") is not None:
        print(
            "Timing — "
            f"crawl: {result.get('crawl_elapsed_ms')} ms | "
            f"generation: {result.get('generation_elapsed_ms')} ms | "
            f"validation: {result.get('validation_elapsed_ms')} ms | "
            f"workflow: {result.get('workflow_elapsed_ms')} ms"
        )

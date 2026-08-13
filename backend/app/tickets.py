import json
import os
import uuid
from typing import Any, Dict, Optional
from urllib.parse import urlparse

try:
    from .database import (
        get_connection,
        get_rule_decisions,
        get_rules_blob,
        save_crawl_input,
        save_rule_output,
        save_rules_blob,
        update_crawl_input_fields,
    )
except ImportError:  # pragma: no cover
    from app.database import (
        get_connection,
        get_rule_decisions,
        get_rules_blob,
        save_crawl_input,
        save_rule_output,
        save_rules_blob,
        update_crawl_input_fields,
    )


def record_run_failure(report_id: str, message: str) -> None:
    """
    Persist why a run failed so the UI can show a reason instead of a ticket
    that silently looks untouched. Best-effort: a failure to record must not
    mask the original error.
    """
    try:
        save_rule_output(
            report_id=report_id,
            rules=[],
            status="failed",
            error_message=(message or "")[:2000],
        )
    except Exception:
        pass


def _parse_ticket_context(ticket_context: Any) -> Dict[str, Any]:
    if not ticket_context:
        return {}
    if isinstance(ticket_context, dict):
        return ticket_context
    try:
        return json.loads(ticket_context)
    except Exception:
        return {}


def persist_ticket_to_db(ticket: Dict[str, Any]) -> int:
    """Persist a frontend-created ticket into the existing crawl_inputs table."""
    url = ticket.get("url") or ""
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path or "unknown"
    if domain.startswith("www."):
        domain = domain[4:]

    payload = {
        "name": ticket.get("name"),
        "env": ticket.get("env"),
        "focus": ticket.get("focus"),
        "targets": ticket.get("targets", []),
        "notes": ticket.get("notes"),
        "createdBy": ticket.get("createdBy"),
        "state": ticket.get("state"),
        "created": ticket.get("created"),
    }

    return save_crawl_input(
        report_id=(ticket.get("id") or ticket.get("name") or str(uuid.uuid4())),
        domain=domain,
        url=url,
        ticket_context=payload,
        status=ticket.get("state", "submitted"),
        crawl_duration_ms=None,
        before_screenshot=None,
    )


def _parse_json_blob(value: Any) -> Any:
    """Decode a JSON column that may arrive as str, dict, list, or None."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _derive_confidence(outcome: Dict[str, Any]) -> float:
    """
    Confidence derived from how many validation gates the rule cleared.

    This is not a model confidence score — the LLM does not emit one. It
    summarises the syntax / scope / policy / sandbox verdicts that
    rule_validator already produced.
    """
    gates = (
        bool(outcome.get("syntax", {}).get("valid")),
        bool(outcome.get("scope", {}).get("safe")),
        bool(outcome.get("policy", {}).get("valid")),
        bool(outcome.get("sandbox", {}).get("passed")),
    )
    cleared = sum(gates)
    if cleared == 0:
        return 0.0
    # 1 gate -> 0.25 ... 4 gates -> 1.0, then trimmed so a clean pass reads
    # as high-but-not-certain.
    return round(cleared / len(gates) * 0.95, 2)


def _duplicates_for_ui(rules_blob: Any) -> Dict[str, Any]:
    """
    Summarise the rules the generator produced but discarded as already known.

    'internal' rules were already in this domain's rule_registry entry;
    'external' ones matched a public filter list. Both are dropped before
    validation, so without this the UI silently shows fewer rules than the
    model actually proposed.
    """
    empty = {"total": 0, "internal": 0, "external": 0, "rules": []}

    rules_data = _parse_json_blob(rules_blob)
    if not isinstance(rules_data, dict):
        return empty

    skipped = rules_data.get("duplicates_skipped")
    if not isinstance(skipped, dict):
        return empty

    internal_rules = [
        r for r in (skipped.get("internal_rules") or []) if isinstance(r, str)
    ]

    external_rules = []
    for entry in skipped.get("external_detail") or []:
        if isinstance(entry, str):
            external_rules.append(entry)
        elif isinstance(entry, dict) and entry.get("rule"):
            external_rules.append(entry["rule"])

    return {
        "total": _optional_count(skipped.get("total")),
        "internal": _optional_count(skipped.get("internal")),
        "external": _optional_count(skipped.get("external")),
        "rules": internal_rules + external_rules,
    }


def _optional_count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rules_for_ui(
    rules_blob: Any,
    validation_blob: Any,
    decisions_blob: Any = None,
) -> list[Dict[str, Any]]:
    """
    Shape persisted rule output into the row format ReportDetail renders:
    {text, status, conf, rule_type, reason, decision}.
    """
    decisions = _parse_json_blob(decisions_blob)
    if not isinstance(decisions, dict):
        decisions = {}
    rules_data = _parse_json_blob(rules_blob)
    validation = _parse_json_blob(validation_blob)

    # save_rule_output writes the full generation blob ({"rules": [...]}) once
    # rules exist, but a bare list when the run produced none.
    if isinstance(rules_data, dict):
        candidates = rules_data.get("rules") or []
    elif isinstance(rules_data, list):
        candidates = rules_data
    else:
        candidates = []

    outcomes = {}
    if isinstance(validation, dict):
        outcomes = {
            o.get("rule"): o
            for o in (validation.get("outcomes") or [])
            if isinstance(o, dict)
        }

    rows: list[Dict[str, Any]] = []
    for entry in candidates:
        source = ""
        if isinstance(entry, str):
            text, rule_type = entry, "unknown"
        elif isinstance(entry, dict):
            text = entry.get("rule") or entry.get("raw") or ""
            rule_type = entry.get("rule_type") or "unknown"
            source = entry.get("source") or ""
        else:
            continue

        if not text:
            continue

        decision_entry = decisions.get(text) or {}
        decision = (
            decision_entry.get("decision")
            if isinstance(decision_entry, dict)
            else decision_entry
        )

        outcome = outcomes.get(text)
        if outcome is None:
            # Generated but validation has not run (or did not cover it).
            rows.append(
                {
                    "text": text,
                    "rule_type": rule_type,
                    "status": "pending",
                    "conf": None,
                    "reason": "",
                    "decision": decision,
                    "source": source,
                }
            )
            continue

        passed = bool(outcome.get("passed"))
        rows.append(
            {
                "text": text,
                "rule_type": rule_type,
                "status": "passed" if passed else "failed",
                "conf": _derive_confidence(outcome),
                "reason": (
                    outcome.get("failure_reason")
                    or outcome.get("failure_stage")
                    or ""
                ),
                "decision": decision,
                "source": source,
            }
        )

    return rows


def _metrics_for_ui(
    crawl_ms: Any,
    rules_blob: Any,
    validation_blob: Any,
    input_tokens: Any,
    output_tokens: Any,
) -> Dict[str, Any]:
    """
    Per-stage timings and token spend for one report.

    Timings live in the JSON blobs the pipeline writes; crawl duration has its
    own column. Token counts are read from the blob first because the
    dedicated columns are left NULL on runs that produced no rules.
    """
    rules_data = _parse_json_blob(rules_blob)
    validation = _parse_json_blob(validation_blob)
    rules_data = rules_data if isinstance(rules_data, dict) else {}
    validation = validation if isinstance(validation, dict) else {}

    usage = rules_data.get("token_usage")
    usage = usage if isinstance(usage, dict) else {}

    prompt = _optional_count(usage.get("prompt_tokens")) or _optional_count(input_tokens)
    completion = _optional_count(usage.get("completion_tokens")) or _optional_count(output_tokens)
    total = _optional_count(usage.get("total_tokens")) or (prompt + completion)

    crawl = _optional_count(crawl_ms) or _optional_count(rules_data.get("crawl_elapsed_ms"))
    generation = _optional_count(rules_data.get("generation_elapsed_ms"))
    validation_ms = _optional_count(validation.get("validation_elapsed_ms"))

    return {
        "crawlMs": crawl or None,
        "generationMs": generation or None,
        "validationMs": validation_ms or None,
        # The pipeline's own workflow_elapsed_ms covers generation+validation
        # only, so the wall-clock total is summed here instead.
        "totalMs": (crawl + generation + validation_ms) or None,
        "avgValidationMsPerRule": _optional_count(
            validation.get("average_validation_time_per_rule_ms")
        ) or None,
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": total,
        "model": usage.get("model") or rules_data.get("model") or "",
        "fallbackUsed": bool(
            usage.get("fallback_used") or rules_data.get("fallback_used")
        ),
    }


def _normalize_ticket_state(status: str) -> tuple[str, str | None]:
    if not status:
        return "draft", None

    status = status.lower()
    if status in {"crawling", "generating", "validating", "inprocess"}:
        stage = {
            "crawling": "crawl",
            "generating": "generate",
            "validating": "validate",
            "inprocess": "crawl",
        }[status]
        return "inprocess", stage

    if status in {"review", "generated", "validated", "no_rules"}:
        return "review", None

    if status == "done":
        return "done", None

    if status in {"failed", "crawl_failed"}:
        # Distinct from "draft": the run happened and did not complete.
        return "failed", None

    if status in {"draft", "submitted"}:
        return "draft", None

    return "draft", None


def fetch_all_tickets() -> list[Dict[str, Any]]:
    """Load tickets from the database for the frontend."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
SELECT
    ci.report_id,
    ci.domain,
    ci.url,
    ci.ticket_context,
    ci.status,
    ci.created_at,
    ci.updated_at,
    ro.rules              AS rule_blob,
    ro.validation_result  AS validation_blob,
    ro.after_screenshot   AS after_screenshot,
    ro.status             AS rule_status,
    ro.error_message      AS error_message,
    ro.decisions          AS decisions_blob,
    ro.input_tokens       AS input_tokens,
    ro.output_tokens      AS output_tokens,
    ci.crawl_duration_ms  AS crawl_duration_ms
FROM crawl_inputs ci
LEFT JOIN rule_outputs ro ON ro.input_id = ci.id
ORDER BY ci.created_at DESC
"""
            )
            rows = cur.fetchall()

    tickets: list[Dict[str, Any]] = []
    for row in rows:
        ticket_context = _parse_ticket_context(row.get("ticket_context"))
        state, stage = _normalize_ticket_state(row.get("status") or ticket_context.get("state", "draft"))
        rules = _rules_for_ui(
            row.get("rule_blob"),
            row.get("validation_blob"),
            row.get("decisions_blob"),
        )
        duplicates = _duplicates_for_ui(row.get("rule_blob"))
        ticket = {
            "id": row.get("report_id") or ticket_context.get("name") or f"db-{row.get('created_at')}",
            "name": ticket_context.get("name") or row.get("report_id"),
            "url": row.get("url"),
            "env": ticket_context.get("env") or "desktop",
            "focus": ticket_context.get("focus", ""),
            "targets": ticket_context.get("targets", []),
            "notes": ticket_context.get("notes", ""),
            "createdBy": ticket_context.get("createdBy", "unknown"),
            "state": state,
            "stage": stage,
            "created": ticket_context.get("created") or (row.get("created_at").isoformat() if row.get("created_at") else None),
            "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else None,
            "rules": rules,
            "duplicates": duplicates,
            "ruleStatus": row.get("rule_status"),
            "errorMessage": row.get("error_message"),
            "metrics": _metrics_for_ui(
                row.get("crawl_duration_ms"),
                row.get("rule_blob"),
                row.get("validation_blob"),
                row.get("input_tokens"),
                row.get("output_tokens"),
            ),
            "afterScreenshot": row.get("after_screenshot"),
        }
        tickets.append(ticket)

    return tickets


def fetch_all_rules() -> list[Dict[str, Any]]:
    """
    Every rule the pipeline has produced, flattened across reports, for the
    rule library. Each entry keeps the report it came from so a rule can be
    traced back to the run that generated it.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
SELECT
    ci.report_id,
    ci.domain,
    ci.url,
    ci.status             AS ticket_status,
    ro.rules              AS rule_blob,
    ro.validation_result  AS validation_blob,
    ro.decisions          AS decisions_blob,
    ro.updated_at         AS updated_at
FROM crawl_inputs ci
JOIN rule_outputs ro ON ro.input_id = ci.id
ORDER BY ro.updated_at DESC
"""
            )
            rows = cur.fetchall()

    library: list[Dict[str, Any]] = []
    for row in rows:
        report_state, _ = _normalize_ticket_state(row.get("ticket_status") or "")
        updated_at = row.get("updated_at")

        for rule in _rules_for_ui(
            row.get("rule_blob"),
            row.get("validation_blob"),
            row.get("decisions_blob"),
        ):
            library.append(
                {
                    **rule,
                    "reportId": row.get("report_id"),
                    "domain": row.get("domain"),
                    "url": row.get("url"),
                    "reportState": report_state,
                    # "Live" means an approved rule on a closed report — the
                    # same definition the Performance page already uses.
                    "deployed": (
                        rule.get("decision") == "approve"
                        and report_state == "done"
                    ),
                    "updatedAt": updated_at.isoformat() if updated_at else None,
                }
            )

    return library


def _normalize_url(url: str) -> str:
    """Compare links ignoring scheme, 'www.', trailing slash and case."""
    text = (url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.startswith("www."):
        text = text[4:]
    return text.rstrip("/")


def find_duplicate_targets(url: str, exclude_report_id: str = "") -> list[Dict[str, Any]]:
    """
    Reports already in the database pointing at the same link.

    Used to warn before a run, because the rule registry dedupes per domain —
    a second run against a known link can legitimately produce nothing.
    """
    target = _normalize_url(url)
    if not target:
        return []

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
SELECT
    ci.report_id,
    ci.url,
    ci.domain,
    ci.status             AS ticket_status,
    ci.created_at         AS created_at,
    ro.rules              AS rule_blob,
    ro.validation_result  AS validation_blob,
    ro.decisions          AS decisions_blob
FROM crawl_inputs ci
LEFT JOIN rule_outputs ro ON ro.input_id = ci.id
ORDER BY ci.created_at DESC
"""
            )
            rows = cur.fetchall()

    matches: list[Dict[str, Any]] = []
    for row in rows:
        if row.get("report_id") == exclude_report_id:
            continue
        if _normalize_url(row.get("url") or "") != target:
            continue

        state, _ = _normalize_ticket_state(row.get("ticket_status") or "")
        created = row.get("created_at")
        matches.append(
            {
                "reportId": row.get("report_id"),
                "url": row.get("url"),
                "domain": row.get("domain"),
                "state": state,
                "createdAt": created.isoformat() if created else None,
                "ruleCount": len(
                    _rules_for_ui(
                        row.get("rule_blob"),
                        row.get("validation_blob"),
                        row.get("decisions_blob"),
                    )
                ),
            }
        )

    return matches


IMAGE_LABELS = {
    "crawl": "Crawl result",
    "before_boxed": "Before rules (ad boxes)",
    "after_rules": "After rules applied",
}


def fetch_report_images(report_id: str) -> list:
    """
    The three screenshots for a report as temporary viewable URLs.

    Always returns all three kinds so the UI can show a placeholder for one
    that was never produced, rather than silently rendering two.
    """
    from .storage.report_images import presign_report_images

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
SELECT ro.images
FROM crawl_inputs ci
JOIN rule_outputs ro ON ro.input_id = ci.id
WHERE ci.report_id=%s
""",
                (report_id,),
            )
            row = cur.fetchone()

    stored = _parse_json_blob(row.get("images")) if row else None
    stored = stored if isinstance(stored, dict) else {}
    urls = presign_report_images(stored)

    return [
        {
            "kind": kind,
            "label": label,
            "uri": stored.get(kind),
            "url": urls.get(kind),
        }
        for kind, label in IMAGE_LABELS.items()
    ]


def count_undecided_rules(report_id: str) -> int:
    """
    Rules still awaiting a moderator call on this report.

    Auto-rejected rules are excluded — the sandbox already ruled on those.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
SELECT ro.rules, ro.validation_result, ro.decisions
FROM crawl_inputs ci
JOIN rule_outputs ro ON ro.input_id = ci.id
WHERE ci.report_id=%s
""",
                (report_id,),
            )
            row = cur.fetchone()

    if not row:
        return 0

    rules = _rules_for_ui(row.get("rules"), row.get("validation_result"), row.get("decisions"))
    return sum(1 for r in rules if r["status"] != "failed" and not r["decision"])


def merge_two_rules(
    primary: Dict[str, Any],
    secondary: Dict[str, Any],
    preview: bool = False,
) -> Dict[str, Any]:
    """
    Fold two rules into one equivalent rule, to cut duplication.

    The merged rule replaces the primary; the secondary is removed. Both are
    marked unvalidated afterwards because the combined text has never been
    through the sandbox. Raises ValueError when the pair cannot be merged.
    """
    from .services.rule_registry import merge_rule_texts

    first_id, first_rule = primary.get("reportId"), primary.get("rule")
    second_id, second_rule = secondary.get("reportId"), secondary.get("rule")
    if not (first_id and first_rule and second_id and second_rule):
        raise ValueError("both rules need a reportId and rule")

    merged, reason = merge_rule_texts(first_rule, second_rule)
    if merged is None:
        raise ValueError(reason or "these rules cannot be merged")

    if preview:
        return {"merged": merged, "keptOn": first_id}

    # Rewrite the primary in place, then drop the secondary. Doing it in this
    # order means a failure part-way leaves both rules present rather than
    # losing one.
    edit_rule(first_id, first_rule, merged)
    if not (second_id == first_id and second_rule == merged):
        try:
            delete_rule(second_id, second_rule)
        except LookupError:
            pass

    return {"merged": merged, "keptOn": first_id, "removedFrom": second_id}


def test_rules_adhoc(items: list) -> Dict[str, Any]:
    """
    Run the sandbox validator over a hand-picked set of rules.

    Only rules from one site can be tested together — the sandbox loads a
    single page, so mixing domains would test each rule against a page it was
    never meant for. Nothing is written to the reports: this is a scratch run
    to see whether rules hold up, not a re-validation of a report.
    """
    from .services.rule_registry import get_domain
    from .services.workflow import run_rule_validation

    if not items:
        raise ValueError("select at least one rule to test")

    urls: Dict[str, str] = {}
    rules: list = []
    for item in items:
        if not isinstance(item, dict):
            continue
        report_id, rule = item.get("reportId"), item.get("rule")
        if not report_id or not rule:
            raise ValueError("every entry needs a reportId and rule")
        rules.append(rule)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT url FROM crawl_inputs WHERE report_id=%s",
                    (report_id,),
                )
                row = cur.fetchone()
        if not row or not row.get("url"):
            raise LookupError(f"no URL on record for {report_id}")
        urls[get_domain(row["url"])] = row["url"]

    if len(urls) > 1:
        raise ValueError(
            "these rules come from %d different sites (%s) — the sandbox loads "
            "one page, so only rules from the same site can be tested together"
            % (len(urls), ", ".join(sorted(urls)))
        )

    domain, url = next(iter(urls.items()))

    # A synthetic id keeps the run out of every report's stored validation.
    # run_rule_validation's DB write is best-effort and simply logs when the
    # report does not exist, which is what we want here.
    scratch_id = f"adhoc-{uuid.uuid4().hex[:12]}"
    result = run_rule_validation(
        rules=rules,
        url=url,
        report_id=scratch_id,
        environment="desktop",
        ticket_context={},
        publish_images=False,
    )

    return {
        "url": url,
        "domain": domain,
        "total": result.get("total", 0),
        "passed": result.get("passed", 0),
        "failed": result.get("failed", 0),
        "passingRules": result.get("passing_rules", []),
        "validationMs": result.get("validation_elapsed_ms"),
        "outcomes": [
            {
                "rule": o.get("rule"),
                "passed": bool(o.get("passed")),
                "reason": o.get("failure_reason") or o.get("failure_stage") or "",
            }
            for o in (result.get("outcomes") or [])
        ],
    }


def delete_rules_bulk(items: list) -> Dict[str, Any]:
    """Delete many rules in one call. Reports each failure rather than aborting."""
    deleted, failed = 0, []
    for item in items:
        if not isinstance(item, dict):
            continue
        report_id = item.get("reportId") or item.get("report_id")
        rule = item.get("rule")
        if not report_id or not rule:
            failed.append({"reportId": report_id, "rule": rule, "error": "missing reportId or rule"})
            continue
        try:
            delete_rule(report_id, rule)
            deleted += 1
        except Exception as exc:
            failed.append({"reportId": report_id, "rule": rule, "error": str(exc)})

    return {"deleted": deleted, "failed": failed}


def fetch_token_usage() -> Dict[str, Any]:
    """
    Token spend per report plus totals, for the usage page.

    TOKEN_BUDGET is optional: this project has no provider-side quota wired in,
    so the budget is whatever ceiling the team chooses to track against. When
    unset, the page reports that no limit is configured rather than inventing
    one.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
SELECT
    ci.report_id,
    ci.url,
    ci.domain,
    ci.status             AS ticket_status,
    ci.crawl_duration_ms  AS crawl_duration_ms,
    ci.created_at         AS created_at,
    ro.rules              AS rule_blob,
    ro.validation_result  AS validation_blob,
    ro.input_tokens       AS input_tokens,
    ro.output_tokens      AS output_tokens
FROM crawl_inputs ci
LEFT JOIN rule_outputs ro ON ro.input_id = ci.id
ORDER BY ci.created_at DESC
"""
            )
            rows = cur.fetchall()

    runs: list[Dict[str, Any]] = []
    for row in rows:
        metrics = _metrics_for_ui(
            row.get("crawl_duration_ms"),
            row.get("rule_blob"),
            row.get("validation_blob"),
            row.get("input_tokens"),
            row.get("output_tokens"),
        )
        if not metrics["totalTokens"]:
            # No LLM call was billed for this report (never run, or it failed
            # before generation). Listing it would imply a zero-cost run.
            continue

        state, _ = _normalize_ticket_state(row.get("ticket_status") or "")
        created = row.get("created_at")
        runs.append(
            {
                "reportId": row.get("report_id"),
                "domain": row.get("domain"),
                "url": row.get("url"),
                "state": state,
                "createdAt": created.isoformat() if created else None,
                "promptTokens": metrics["promptTokens"],
                "completionTokens": metrics["completionTokens"],
                "totalTokens": metrics["totalTokens"],
                "model": metrics["model"],
                "fallbackUsed": metrics["fallbackUsed"],
                "totalMs": metrics["totalMs"],
            }
        )

    by_model: Dict[str, Dict[str, int]] = {}
    for run in runs:
        bucket = by_model.setdefault(
            run["model"] or "unknown",
            {"model": run["model"] or "unknown", "runs": 0, "prompt": 0, "completion": 0, "total": 0},
        )
        bucket["runs"] += 1
        bucket["prompt"] += run["promptTokens"]
        bucket["completion"] += run["completionTokens"]
        bucket["total"] += run["totalTokens"]

    budget = os.getenv("TOKEN_BUDGET")
    try:
        budget_value = int(budget) if budget else None
    except ValueError:
        budget_value = None

    return {
        "runs": runs,
        "byModel": sorted(by_model.values(), key=lambda m: -m["total"]),
        "totals": {
            "runs": len(runs),
            "promptTokens": sum(r["promptTokens"] for r in runs),
            "completionTokens": sum(r["completionTokens"] for r in runs),
            "totalTokens": sum(r["totalTokens"] for r in runs),
        },
        "budget": budget_value,
    }


EDITABLE_TICKET_STATES = {"draft", "submitted", "review", "failed", "crawl_failed",
                          "generated", "validated", "no_rules", "crawling",
                          "generating", "validating", "inprocess"}


def get_ticket_status(report_id: str) -> Optional[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM crawl_inputs WHERE report_id=%s",
                (report_id,),
            )
            row = cur.fetchone()
    return row.get("status") if row else None


def update_ticket_details(report_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    Edit a ticket's user-supplied fields. The report id is not editable — it
    is the key every rule, crawl artefact and decision is filed under.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url, ticket_context FROM crawl_inputs WHERE report_id=%s",
                (report_id,),
            )
            row = cur.fetchone()

    if not row:
        raise LookupError(f"ticket {report_id} not found")

    context = _parse_ticket_context(row.get("ticket_context"))
    url = (fields.get("url") or row.get("url") or "").strip()

    for key in ("name", "env", "focus", "notes", "createdBy", "created", "state"):
        if key in fields:
            context[key] = fields[key]

    if "targets" in fields:
        targets = fields["targets"]
        context["targets"] = (
            [t.strip() for t in targets.split(",") if t.strip()]
            if isinstance(targets, str)
            else list(targets or [])
        )

    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path or "unknown"
    if domain.startswith("www."):
        domain = domain[4:]

    update_crawl_input_fields(
        report_id=report_id,
        url=url,
        domain=domain,
        ticket_context=context,
    )
    return context


def _rules_list_from_blob(blob: Any) -> tuple[Dict[str, Any], list]:
    """
    Return (container, rules_list) for a stored rules blob, normalising the
    bare-list form written by runs that produced nothing into a dict so new
    rules can be appended either way.
    """
    data = _parse_json_blob(blob)
    if isinstance(data, dict):
        rules = data.get("rules")
        return data, list(rules) if isinstance(rules, list) else []
    if isinstance(data, list):
        return {"rules": []}, list(data)
    return {"rules": []}, []


def _infer_rule_type(rule: str) -> str:
    return "cosmetic" if ("##" in rule or "#@#" in rule) else "network"


def add_manual_rule(report_id: str, rule: str, rule_type: Optional[str] = None) -> Dict[str, Any]:
    """Append a moderator-written rule to a report's rule set."""
    rule = (rule or "").strip()
    if not rule:
        raise ValueError("rule text is required")

    container, rules = _rules_list_from_blob(get_rules_blob(report_id))

    for existing in rules:
        text = existing.get("rule") if isinstance(existing, dict) else existing
        if text == rule:
            raise ValueError("that rule is already on this report")

    rules.append(
        {
            "rule": rule,
            "rule_type": rule_type or _infer_rule_type(rule),
            "raw": rule,
            # Marks provenance and signals that this rule never went through
            # the sandbox, so the UI does not imply a validation result.
            "source": "manual",
        }
    )
    container["rules"] = rules
    container["rule_count"] = len(rules)
    save_rules_blob(report_id, container)

    _register_rule_for_report(report_id, rule)
    return {"rule": rule, "rule_count": len(rules)}


def edit_rule(report_id: str, old_rule: str, new_rule: str) -> Dict[str, Any]:
    """
    Rewrite a rule's text.

    The stored sandbox outcome and any decision are keyed by the old text, so
    both stop applying — the rule falls back to unvalidated and undecided,
    which is the honest state for text nobody has tested.
    """
    new_rule = (new_rule or "").strip()
    if not new_rule:
        raise ValueError("new rule text is required")

    container, rules = _rules_list_from_blob(get_rules_blob(report_id))

    found = False
    for entry in rules:
        text = entry.get("rule") if isinstance(entry, dict) else entry
        if text != old_rule:
            continue
        found = True
        if isinstance(entry, dict):
            entry["rule"] = new_rule
            entry["raw"] = new_rule
            entry["rule_type"] = _infer_rule_type(new_rule)
            entry["source"] = "edited"
        else:
            rules[rules.index(entry)] = {
                "rule": new_rule,
                "rule_type": _infer_rule_type(new_rule),
                "raw": new_rule,
                "source": "edited",
            }
        break

    if not found:
        raise LookupError(f"rule not found on {report_id}")

    container["rules"] = rules
    decisions = get_rule_decisions(report_id)
    decisions.pop(old_rule, None)
    save_rules_blob(report_id, container, decisions)

    _unregister_rule_for_report(report_id, old_rule)
    _register_rule_for_report(report_id, new_rule)
    return {"rule": new_rule}


def delete_rule(report_id: str, rule: str) -> Dict[str, Any]:
    """Remove a rule from a report and from the domain's registry."""
    container, rules = _rules_list_from_blob(get_rules_blob(report_id))

    remaining = [
        entry
        for entry in rules
        if (entry.get("rule") if isinstance(entry, dict) else entry) != rule
    ]
    if len(remaining) == len(rules):
        raise LookupError(f"rule not found on {report_id}")

    container["rules"] = remaining
    container["rule_count"] = len(remaining)

    decisions = get_rule_decisions(report_id)
    decisions.pop(rule, None)
    save_rules_blob(report_id, container, decisions)

    _unregister_rule_for_report(report_id, rule)
    return {"rule_count": len(remaining)}


def _report_domain(report_id: str) -> Optional[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url FROM crawl_inputs WHERE report_id=%s",
                (report_id,),
            )
            row = cur.fetchone()
    if not row or not row.get("url"):
        return None

    from .services.rule_registry import get_domain

    return get_domain(row["url"])


def _register_rule_for_report(report_id: str, rule: str) -> None:
    try:
        from .services.rule_registry import normalize_rule, register_rules

        domain = _report_domain(report_id)
        if domain:
            register_rules(domain, [normalize_rule(rule)])
    except Exception:
        # The registry is a dedup optimisation, not the source of truth.
        pass


def _unregister_rule_for_report(report_id: str, rule: str) -> None:
    try:
        from .services.rule_registry import unregister_rule, unregister_rule_anywhere

        domain = _report_domain(report_id)
        if domain and unregister_rule(domain, rule):
            return
        # The report's URL may have been edited since the rule was generated,
        # which files it under the previous domain.
        unregister_rule_anywhere(rule)
    except Exception:
        pass


def update_ticket_status(report_id: str, status: str) -> int:
    """Update ticket status in the database."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crawl_inputs SET status=%s, updated_at=NOW() WHERE report_id=%s",
                (status, report_id),
            )
            return cur.rowcount


def delete_ticket(report_id: str) -> int:
    """
    Delete a ticket and everything filed under it.

    Its rules are unregistered first: the registry is keyed by domain and
    outlives the report, so leaving them behind would keep deduping those
    rules out of every future run against the same site with no report left
    to explain why.
    """
    container, rules = _rules_list_from_blob(get_rules_blob(report_id))
    for entry in rules:
        text = entry.get("rule") if isinstance(entry, dict) else entry
        if text:
            _unregister_rule_for_report(report_id, text)

    # Ceph outlives the database row, so its objects have to go too.
    try:
        from .storage.report_images import delete_report_images

        delete_report_images(report_id)
    except Exception:
        pass

    with get_connection() as conn:
        with conn.cursor() as cur:
            # rule_outputs cascades on the foreign key.
            cur.execute(
                "DELETE FROM crawl_inputs WHERE report_id=%s",
                (report_id,),
            )
            return cur.rowcount

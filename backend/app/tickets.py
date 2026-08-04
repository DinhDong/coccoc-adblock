import json
import uuid
from typing import Any, Dict
from urllib.parse import urlparse

try:
    from .database import get_connection, save_crawl_input, save_rule_output
except ImportError:  # pragma: no cover
    from app.database import get_connection, save_crawl_input, save_rule_output


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


def _rules_for_ui(rules_blob: Any, validation_blob: Any) -> list[Dict[str, Any]]:
    """
    Shape persisted rule output into the row format ReportDetail renders:
    {text, status, conf, rule_type, reason}.
    """
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
        if isinstance(entry, str):
            text, rule_type = entry, "unknown"
        elif isinstance(entry, dict):
            text = entry.get("rule") or entry.get("raw") or ""
            rule_type = entry.get("rule_type") or "unknown"
        else:
            continue

        if not text:
            continue

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
            }
        )

    return rows


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
    ro.error_message      AS error_message
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
        rules = _rules_for_ui(row.get("rule_blob"), row.get("validation_blob"))
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
            "afterScreenshot": row.get("after_screenshot"),
        }
        tickets.append(ticket)

    return tickets


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
    """Delete a ticket from crawl_inputs."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM crawl_inputs WHERE report_id=%s",
                (report_id,),
            )
            return cur.rowcount

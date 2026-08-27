import json
import os
import re
import uuid
from datetime import datetime
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

try:
    from .services.rule_registry import (
        DEFAULT_ENVIRONMENT,
        get_domain,
        normalize_rule,
    )
except ImportError:  # pragma: no cover
    from app.services.rule_registry import (
        DEFAULT_ENVIRONMENT,
        get_domain,
        normalize_rule,
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


_REPORT_ID_RE = re.compile(r"^RPT-(\d{4})-0(\d{3})$")


def _report_id_exists(report_id: str) -> bool:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM crawl_inputs WHERE report_id=%s LIMIT 1",
                (report_id,),
            )
            return cur.fetchone() is not None


def allocate_report_id(year: Optional[int] = None) -> str:
    """
    Pick the next unused RPT-<year>-0NNN id, deciding it here rather than in
    the browser.

    The frontend used to mint these from a counter seeded at 148 that reset on
    every page load. save_crawl_input updates in place when report_id is taken,
    so after a reload the next "new" report silently took over an existing one,
    inheriting its rules, decisions and created_at — RPT-2026-0149 was created
    on 20 Aug and re-adopted four days later by someone who thought they were
    filing a fresh report.

    Ids that do not fit the counter shape (older timestamp-style ones) are
    ignored when picking the maximum but still block reuse via the existence
    check below.
    """
    year = year or datetime.now().year
    prefix = f"RPT-{year}-0"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT report_id FROM crawl_inputs WHERE report_id LIKE %s",
                (prefix + "%",),
            )
            rows = cur.fetchall()

    highest = 147  # so the first allocated id stays RPT-<year>-0148
    for row in rows:
        match = _REPORT_ID_RE.match(str(row.get("report_id") or ""))
        if match and int(match.group(1)) == year:
            highest = max(highest, int(match.group(2)))

    candidate = highest + 1
    while _report_id_exists(f"RPT-{year}-0{candidate:03d}"):
        candidate += 1
    return f"RPT-{year}-0{candidate:03d}"


def create_ticket(ticket: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a ticket, guaranteeing it does not overwrite an existing one.

    Returns {"id", "record_id", "renamed"}. A requested id that is already
    taken is replaced rather than merged into: creating a report must never
    mutate somebody else's, and `renamed` lets the UI say so.
    """
    requested = str(ticket.get("id") or ticket.get("name") or "").strip()
    renamed = False

    if not requested or _report_id_exists(requested):
        renamed = bool(requested)
        requested = allocate_report_id()

    payload = dict(ticket)
    payload["id"] = requested
    # The name is what the UI shows and what older code fell back to for the
    # id, so keep the two in step when the id had to change.
    if renamed or not payload.get("name"):
        payload["name"] = requested

    record_id = persist_ticket_to_db(payload)
    return {"id": requested, "record_id": record_id, "renamed": renamed}


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


def _rule_candidates(rules_blob: Any) -> list:
    """
    The raw rule entries stored for a report.

    save_rule_output writes the full generation blob ({"rules": [...]}) once
    rules exist, but a bare list when the run produced none.
    """
    rules_data = _parse_json_blob(rules_blob)
    if isinstance(rules_data, dict):
        candidates = rules_data.get("rules") or []
    elif isinstance(rules_data, list):
        candidates = rules_data
    else:
        candidates = []
    return candidates if isinstance(candidates, list) else []


def _entry_text(entry: Any) -> str:
    """The rule text of one stored entry, whichever shape it was written in."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("rule") or entry.get("raw") or ""
    return ""


def _scope_key(row: Dict[str, Any]) -> tuple[str, str]:
    """
    The (domain, environment) a report's rules belong to.

    Duplicate detection is scoped to this pair, matching how rule_registry
    scopes generation-time dedup: the same selector approved for a different
    site, or for a different platform, is deliberate coverage rather than a
    duplicate. `domain` is denormalised onto crawl_inputs but is empty on some
    older rows, so fall back to deriving it from the URL.
    """
    domain = (row.get("domain") or "").strip().lower()
    if not domain:
        domain = get_domain(row.get("url") or "")
    env = _context_environment(row.get("ticket_context")) or DEFAULT_ENVIRONMENT
    return domain, env


def _approved_rule_index(rows: list) -> Dict[tuple, list]:
    """
    Which reports have each rule *approved*, keyed by (domain, env, rule).

    Duplicate warnings used to be computed once, during generation, against
    rule_registry.json — a record of every rule ever proposed for a domain,
    approved or not. That was wrong in both directions: a rule was flagged
    because some other report had merely *suggested* the same thing, and
    because the flag was frozen into the stored blob it never changed
    afterwards. Approving the other copy, or rejecting it, left the warning
    exactly as generation-time had written it.

    Rules are now matched against approved ones only, and the match is
    recomputed on every read from these same rows, so approving a rule makes
    its twins elsewhere start warning on the next poll with no re-run.

    Only decisions whose rule still exists in the report's blob count. Deletes
    and edits drop the decision alongside the rule, so this is belt-and-braces
    against a stale entry resurrecting a rule nobody can see.
    """
    index: Dict[tuple, list] = {}

    for row in rows:
        decisions = _parse_json_blob(row.get("decisions_blob"))
        if not isinstance(decisions, dict) or not decisions:
            continue

        present = {
            text
            for text in (_entry_text(e) for e in _rule_candidates(row.get("rule_blob")))
            if text
        }
        if not present:
            continue

        report_id = row.get("report_id") or ""
        domain, env = _scope_key(row)

        for text, entry in decisions.items():
            decision = entry.get("decision") if isinstance(entry, dict) else entry
            if decision != "approve" or text not in present:
                continue
            bucket = index.setdefault((domain, env, normalize_rule(text)), [])
            if report_id and report_id not in bucket:
                bucket.append(report_id)

    return index


def _approved_duplicate_source(report_ids: list) -> str:
    """Human-readable 'where has this already been approved' note."""
    head = report_ids[0]
    if len(report_ids) == 1:
        return f"already approved on {head}"
    return f"already approved on {head} and {len(report_ids) - 1} more"


def _duplicates_for_ui(
    rules_blob: Any,
    rows: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Summarise the rules in this report that duplicate something else.

    'internal' means the same rule is already approved on another report for
    this domain+environment; 'external' means it matched a public filter list.
    Both stay in the rule list and are flagged so a moderator can still
    approve them; `dropped` marks the older rows where they were removed
    before validation instead.

    Counted off `rows` — the same live per-rule flags _rules_for_ui just
    produced — rather than off the generation-time tallies in the blob.
    Reading the blob would put a headline count next to rules that no longer
    carry a chip, so the summary would claim duplicates the table does not
    show (and would miss the ones approved since the run).
    """
    empty = {"total": 0, "internal": 0, "external": 0, "rules": [], "dropped": False}

    rules_data = _parse_json_blob(rules_blob)
    if not isinstance(rules_data, dict):
        return empty

    # Current runs keep duplicates in the rule list and flag them. Rows from
    # before that change stored them under "duplicates_skipped" and really did
    # drop them, so `dropped` tells the UI which of the two it is looking at.
    skipped = rules_data.get("duplicates_flagged")
    dropped = False
    if not isinstance(skipped, dict):
        skipped = rules_data.get("duplicates_skipped")
        dropped = True
    if not isinstance(skipped, dict):
        return empty

    # Those pre-change rows are the one case that still has to come from the
    # blob: the duplicates were dropped before the list was stored, so there
    # is no row to recount them from.
    if not dropped and rows is not None:
        flagged = [r for r in rows if r.get("duplicate")]
        internal = [r for r in flagged if r["duplicate"].get("kind") == "internal"]
        external = [r for r in flagged if r["duplicate"].get("kind") == "external"]
        return {
            "total": len(flagged),
            "internal": len(internal),
            "external": len(external),
            "rules": [r["text"] for r in internal + external],
            "dropped": False,
        }

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
        "dropped": dropped,
    }


def _generated_count(rules_blob: Any, kept: int, duplicates: Dict[str, Any]) -> int:
    """
    How many rules the generator proposed for this report.

    Prefers generated_rule_count from the stored record. Older rows and rows
    the worker flattened before that column was preserved do not carry it, so
    fall back to reconstructing it. Duplicates only add to the total when they
    were dropped from the list; on current runs they are already counted in
    `kept`, and adding them again would overstate what the model produced.
    """
    rules_data = _parse_json_blob(rules_blob)
    if isinstance(rules_data, dict):
        recorded = _optional_count(rules_data.get("generated_rule_count"))
        if recorded:
            return recorded
    if duplicates.get("dropped"):
        return kept + _optional_count(duplicates.get("total"))
    return kept


def _optional_count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rules_for_ui(
    rules_blob: Any,
    validation_blob: Any,
    decisions_blob: Any = None,
    *,
    approved_index: Optional[Dict[tuple, list]] = None,
    report_id: Optional[str] = None,
    scope: Optional[tuple] = None,
) -> list[Dict[str, Any]]:
    """
    Shape persisted rule output into the row format ReportDetail renders:
    {text, status, conf, rule_type, reason, decision}.

    Pass `approved_index` (from _approved_rule_index), the report's own id and
    its (domain, environment) `scope` to get live duplicate warnings. Without
    them the rows carry only whatever external-list coverage generation
    recorded — which is all callers that just count rows need.
    """
    decisions = _parse_json_blob(decisions_blob)
    if not isinstance(decisions, dict):
        decisions = {}
    validation = _parse_json_blob(validation_blob)
    candidates = _rule_candidates(rules_blob)

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
        duplicate = None
        if isinstance(entry, str):
            text, rule_type = entry, "unknown"
        elif isinstance(entry, dict):
            text = entry.get("rule") or entry.get("raw") or ""
            rule_type = entry.get("rule_type") or "unknown"
            source = entry.get("source") or ""
            # Only the external-list half of the generation-time note is still
            # trusted. Being in EasyList is a fact about a published list with
            # no approval state to wait on, so it cannot go stale. The
            # "internal" half is deliberately ignored and recomputed below:
            # as written it flagged rules against merely-proposed twins and
            # never changed once stored.
            dup = entry.get("duplicate")
            if isinstance(dup, dict) and str(dup.get("kind") or "") == "external":
                duplicate = {
                    "kind": "external",
                    "source": str(dup.get("source") or ""),
                }
        else:
            continue

        if not text:
            continue

        # A rule already approved elsewhere for this same domain+environment.
        # Computed here rather than read from the blob so that approving one
        # copy starts warning on its twins immediately, and un-approving it
        # stops the warning again. External coverage wins when both apply —
        # the row shows one warning either way, and "already in EasyList" is
        # the more final of the two.
        if duplicate is None and approved_index is not None and scope:
            elsewhere = [
                rid
                for rid in approved_index.get(
                    (scope[0], scope[1], normalize_rule(text)), ()
                )
                if rid != report_id
            ]
            if elsewhere:
                duplicate = {
                    "kind": "internal",
                    "source": _approved_duplicate_source(elsewhere),
                }

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
                    "duplicate": duplicate,
                }
            )
            continue

        passed = bool(outcome.get("passed"))

        # The sandbox already ruled on a failed rule, so it counts as rejected
        # without a moderator having to click it. An explicit decision still
        # wins — someone may deliberately approve a rule the sandbox disliked.
        auto_rejected = False
        if not passed and not decision:
            decision = "reject"
            auto_rejected = True

        rows.append(
            {
                "text": text,
                "rule_type": rule_type,
                "status": "passed" if passed else "failed",
                "autoRejected": auto_rejected,
                "conf": _derive_confidence(outcome),
                "reason": (
                    outcome.get("failure_reason")
                    or outcome.get("failure_stage")
                    or ""
                ),
                "decision": decision,
                "source": source,
                "duplicate": duplicate,
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

    # crawl_inputs.crawl_duration_ms does NOT hold the crawl duration. The
    # worker writes its whole-job wall clock into that column (see
    # worker.build_output_payload), so reading it as the crawl stage showed
    # 3m 26s for a crawl that took 12s, and then counted the entire run again
    # inside the total. The stage figure the pipeline actually measured lives
    # in the rules blob; the column is only a fallback for rows written
    # before it did.
    crawl = _optional_count(rules_data.get("crawl_elapsed_ms"))
    generation = _optional_count(rules_data.get("generation_elapsed_ms"))
    validation_ms = _optional_count(validation.get("validation_elapsed_ms"))
    job_ms = _optional_count(crawl_ms)
    if not crawl and not generation and not validation_ms:
        crawl = job_ms

    stage_total = crawl + generation + validation_ms
    # Wall clock can never be shorter than the stages it contains, so the
    # larger of the two is right whichever way the column was written. The
    # gap between them is real pipeline overhead (screenshot upload, DB
    # writes) that belongs in the total.
    total_ms = max(stage_total, job_ms)

    return {
        "crawlMs": crawl or None,
        "generationMs": generation or None,
        "validationMs": validation_ms or None,
        "totalMs": total_ms or None,
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
    # "processing" is set by the standalone service worker
    # (app/services/worker.py) when it claims a row; without it here a ticket
    # that worker is actively running would render as a Draft.
    # Queued is its own state, not a flavour of in-progress. With several
    # people submitting at once the difference matters: one report is actually
    # being crawled, the rest are waiting behind it.
    if status == "new":
        return "queued", None

    if status in {"crawling", "generating", "validating", "inprocess", "processing"}:
        stage = {
            "crawling": "crawl",
            "generating": "generate",
            "validating": "validate",
            "inprocess": "crawl",
            "processing": "crawl",
        }[status]
        return "inprocess", stage

    # "completed" is what the worker writes on success; without it here a
    # worker-finished report fell through to the catch-all and showed as Draft.
    if status in {"review", "completed", "generated", "validated", "no_rules"}:
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
    ci.crawl_duration_ms  AS crawl_duration_ms,
    ci.run_started_at     AS run_started_at,
    -- Elapsed is computed here rather than from the timestamp in the browser.
    -- MySQL runs on UTC and the serialised timestamp carries no offset, so a
    -- client in UTC+7 parsing it as local time reads a run that started
    -- seconds ago as seven hours old. Both sides of this subtraction come
    -- from the same server clock, so it is right whatever either timezone is.
    TIMESTAMPDIFF(SECOND, ci.run_started_at, NOW()) AS run_elapsed_s
FROM crawl_inputs ci
LEFT JOIN rule_outputs ro ON ro.input_id = ci.id
ORDER BY ci.created_at DESC
"""
            )
            rows = cur.fetchall()

    # Built once from the rows already in hand, not per ticket: every report
    # needs to be matched against every other report's approved rules, and a
    # second query per ticket would turn one page load into hundreds.
    approved = _approved_rule_index(rows)

    tickets: list[Dict[str, Any]] = []
    for row in rows:
        ticket_context = _parse_ticket_context(row.get("ticket_context"))
        state, stage = _normalize_ticket_state(row.get("status") or ticket_context.get("state", "draft"))
        rules = _rules_for_ui(
            row.get("rule_blob"),
            row.get("validation_blob"),
            row.get("decisions_blob"),
            approved_index=approved,
            report_id=row.get("report_id"),
            scope=_scope_key(row),
        )
        duplicates = _duplicates_for_ui(row.get("rule_blob"), rules)
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
            # Stamped when the worker claimed the report. The UI counts up
            # from this while the run is in flight, so the elapsed time is
            # correct even for someone who opened the page mid-run.
            "runStartedAt": (
                row.get("run_started_at").isoformat()
                if row.get("run_started_at")
                else None
            ),
            # Seconds the current run has been going, straight from the
            # database clock. The UI ticks upward from this and re-anchors on
            # every poll, so it never drifts and never depends on the
            # viewer's timezone being right.
            "runElapsedMs": (
                int(row["run_elapsed_s"]) * 1000
                if row.get("run_elapsed_s") is not None
                else None
            ),
            "rules": rules,
            "duplicates": duplicates,
            # What the model actually proposed, before dedup dropped anything.
            # The pipeline panel shows this next to the kept count so a report
            # that generated rules but kept none reads as "9 generated, 9
            # already known" rather than as a run that produced nothing.
            "generatedCount": _generated_count(row.get("rule_blob"), len(rules), duplicates),
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

    # The worker takes the oldest "new" row first (created_at ASC, id ASC), so
    # position is that ordering. 1 = next to run.
    queued = sorted(
        (t for t in tickets if t["state"] == "queued"),
        key=lambda t: (t.get("created") or "", t["id"]),
    )
    for position, ticket in enumerate(queued, start=1):
        ticket["queuePosition"] = position
        ticket["queueLength"] = len(queued)

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
    ci.ticket_context     AS ticket_context,
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

    approved = _approved_rule_index(rows)

    library: list[Dict[str, Any]] = []
    for row in rows:
        report_state, _ = _normalize_ticket_state(row.get("ticket_status") or "")
        updated_at = row.get("updated_at")
        # Rules are registered per (domain, environment), so the library and
        # the Live rules page have to show which platform a rule belongs to —
        # otherwise the same selector listed under two platforms reads as an
        # accidental duplicate rather than deliberate per-platform coverage.
        rule_env = _context_environment(row.get("ticket_context"))

        for rule in _rules_for_ui(
            row.get("rule_blob"),
            row.get("validation_blob"),
            row.get("decisions_blob"),
            approved_index=approved,
            report_id=row.get("report_id"),
            scope=_scope_key(row),
        ):
            library.append(
                {
                    **rule,
                    "reportId": row.get("report_id"),
                    "domain": row.get("domain"),
                    "url": row.get("url"),
                    # Same fallback fetch_all_tickets uses, so a rule and the
                    # report it came from never disagree about the platform.
                    # Reports predating per-platform tickets stored no env and
                    # all ran as desktop, which is where the registry
                    # migration files their rules too.
                    "env": rule_env or "desktop",
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


PLAYGROUND_PREFIX = "scratch-"


def run_playground_test(
    url: str,
    rules: list,
    environment: str = "desktop",
) -> Dict[str, Any]:
    """
    Render a page with hand-written rules applied, without a report.

    Unlike test_rules_adhoc this takes a raw URL and rule strings, so it can
    be used to try an idea before any report exists. Nothing is persisted:
    no crawl_inputs row, no Ceph upload. The screenshots stay on local disk
    and are served back through the API.
    """
    from .services.workflow import run_rule_validation

    url = (url or "").strip()
    if not url:
        raise ValueError("a URL is required")
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")

    cleaned = [r.strip() for r in (rules or []) if isinstance(r, str) and r.strip()]
    if not cleaned:
        raise ValueError("enter at least one rule to test")

    run_id = f"{PLAYGROUND_PREFIX}{uuid.uuid4().hex[:12]}"
    result = run_rule_validation(
        rules=cleaned,
        url=url,
        report_id=run_id,
        environment=environment,
        ticket_context={},
        publish_images=False,
    )

    from .storage.report_images import local_image_paths

    available = [
        kind for kind, path in local_image_paths(run_id).items() if path.exists()
    ]

    return {
        "runId": run_id,
        "url": url,
        "environment": environment,
        "total": result.get("total", 0),
        "passed": result.get("passed", 0),
        "failed": result.get("failed", 0),
        "validationMs": result.get("validation_elapsed_ms"),
        "images": available,
        "outcomes": [
            {
                "rule": o.get("rule"),
                "passed": bool(o.get("passed")),
                "reason": o.get("failure_reason") or o.get("failure_stage") or "",
            }
            for o in (result.get("outcomes") or [])
        ],
    }


def playground_image_path(run_id: str, kind: str):
    """
    Resolve a scratch screenshot for serving, refusing anything that is not a
    playground run id so this cannot be pointed at arbitrary files.
    """
    from .storage.report_images import local_image_paths

    if not run_id.startswith(PLAYGROUND_PREFIX) or "/" in run_id or "\\" in run_id:
        return None
    if not re.fullmatch(r"scratch-[0-9a-f]{12}", run_id):
        return None

    path = local_image_paths(run_id).get(kind)
    if path is None or not path.exists():
        return None
    return path


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

    return {
        "runs": runs,
        "byModel": sorted(by_model.values(), key=lambda m: -m["total"]),
        "totals": {
            "runs": len(runs),
            "promptTokens": sum(r["promptTokens"] for r in runs),
            "completionTokens": sum(r["completionTokens"] for r in runs),
            "totalTokens": sum(r["totalTokens"] for r in runs),
        },
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


def _context_environment(blob: Any) -> Optional[str]:
    """
    Read the crawl environment out of a stored ticket_context.

    The UI writes it as "env" and the pipeline's normalised context calls the
    same thing "platform"; both spellings are in the table, so both are read.
    """
    context = _parse_json_blob(blob)
    if not isinstance(context, dict):
        return None

    for key in ("env", "platform", "environment"):
        value = str(context.get(key) or "").strip().lower()
        if value in {"desktop", "android", "ios"}:
            return value
    return None


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


def _report_environment(report_id: str) -> Optional[str]:
    """
    The platform a report was crawled on, for scoping registry writes.

    The registry is keyed per (domain, environment), so a manually-added rule
    has to land in the bucket for the platform the moderator was looking at —
    filing it under the default would dedupe it out of that platform's next
    run while leaving it proposable on the one it was written for.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ticket_context FROM crawl_inputs WHERE report_id=%s",
                (report_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return _context_environment(row.get("ticket_context"))


def _register_rule_for_report(report_id: str, rule: str) -> None:
    try:
        from .services.rule_registry import normalize_rule, register_rules

        domain = _report_domain(report_id)
        if domain:
            register_rules(
                domain,
                [normalize_rule(rule)],
                _report_environment(report_id),
            )
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

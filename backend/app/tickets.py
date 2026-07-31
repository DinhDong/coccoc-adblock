import json
from typing import Any, Dict
from urllib.parse import urlparse

try:
    from .database import get_connection, save_crawl_input
except ImportError:  # pragma: no cover
    from app.database import get_connection, save_crawl_input


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
        report_id=ticket.get("id") or ticket.get("name"),
        domain=domain,
        url=url,
        ticket_context=payload,
        status=ticket.get("state", "submitted"),
        crawl_duration_ms=None,
        before_screenshot=None,
    )


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

    if status in {"draft", "submitted"}:
        return "draft", None

    return "draft", None


def fetch_all_tickets() -> list[Dict[str, Any]]:
    """Load tickets from the database for the frontend."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT report_id, domain, url, ticket_context, status, created_at, updated_at FROM crawl_inputs ORDER BY created_at DESC"
            )
            rows = cur.fetchall()

    tickets: list[Dict[str, Any]] = []
    for row in rows:
        ticket_context = _parse_ticket_context(row.get("ticket_context"))
        state, stage = _normalize_ticket_state(row.get("status") or ticket_context.get("state", "draft"))
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

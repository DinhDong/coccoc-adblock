from typing import Any, Dict
from urllib.parse import urlparse

try:
    from .database import save_crawl_input
except ImportError:  # pragma: no cover
    from app.database import save_crawl_input


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
        status="submitted",
        crawl_duration_ms=None,
        before_screenshot=None,
    )

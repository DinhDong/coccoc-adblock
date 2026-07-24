from unittest.mock import patch

from app.tickets import persist_ticket_to_db


def test_persist_ticket_to_db_uses_existing_schema_fields():
    payload = {
        "id": "u1",
        "name": "RPT-2026-0001",
        "url": "https://example.com/article",
        "env": "desktop",
        "focus": "sidebar",
        "targets": ["popup overlay"],
        "notes": "Please block the popup",
        "createdBy": "alice",
        "state": "draft",
        "created": "2026-07-24",
    }

    with patch("app.tickets.save_crawl_input", return_value=7) as save_mock:
        result = persist_ticket_to_db(payload)

    assert result == 7
    save_mock.assert_called_once()
    kwargs = save_mock.call_args.kwargs
    assert kwargs["report_id"] == "u1"
    assert kwargs["domain"] == "example.com"
    assert kwargs["url"] == "https://example.com/article"
    assert kwargs["status"] == "submitted"
    assert kwargs["ticket_context"]["name"] == "RPT-2026-0001"
    assert kwargs["ticket_context"]["env"] == "desktop"

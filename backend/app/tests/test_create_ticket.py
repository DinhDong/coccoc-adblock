import unittest
from unittest.mock import patch

import app.tickets as tickets_module
from app.services.worker import safe_report_id


class CreateTicketIdTests(unittest.TestCase):
    """The stored id must be the one the worker runs under, or a run forks the report."""

    def _create(self, ticket, taken=()):
        saved = []
        with patch.object(tickets_module, "_report_id_exists", lambda rid: rid in taken), \
             patch.object(tickets_module, "allocate_report_id", lambda: "RPT-2026-0200"), \
             patch.object(tickets_module, "persist_ticket_to_db", lambda p: saved.append(p) or 1):
            result = tickets_module.create_ticket(ticket)
        return result, saved[0]

    def test_name_with_spaces_is_stored_as_the_worker_id(self) -> None:
        result, saved = self._create({"id": "IOS test", "name": "IOS test", "env": "ios"})

        self.assertEqual(result["id"], "IOS-test")
        self.assertEqual(saved["id"], safe_report_id(saved["id"]))
        self.assertFalse(result["renamed"])
        # The label keeps what the moderator typed.
        self.assertEqual(saved["name"], "IOS test")

    def test_safe_id_is_left_alone(self) -> None:
        result, saved = self._create({"id": "RPT-2026-0150", "name": "RPT-2026-0150"})

        self.assertEqual(result["id"], "RPT-2026-0150")
        self.assertEqual(saved["name"], "RPT-2026-0150")

    def test_collision_after_cleaning_allocates_a_fresh_id(self) -> None:
        result, saved = self._create({"id": "IOS test", "name": "IOS test"}, taken={"IOS-test"})

        self.assertEqual(result["id"], "RPT-2026-0200")
        self.assertTrue(result["renamed"])
        self.assertEqual(saved["name"], "RPT-2026-0200")


if __name__ == "__main__":
    unittest.main()

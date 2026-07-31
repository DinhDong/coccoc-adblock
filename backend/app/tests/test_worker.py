import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.services.worker as worker_module
from app.services.worker import (
    FileJobSource,
    build_job_from_ticket,
    extract_url_from_ticket,
    run_worker,
)


class WorkerFileModeTests(unittest.TestCase):
    def test_extracts_url_from_steps(self) -> None:
        ticket = {
            "request": "[Desktop][Adblock] - example.com/path: Block popup",
            "steps": ["Open https://example.com/path", "Enable Adblock mode"],
        }

        self.assertEqual(extract_url_from_ticket(ticket), "https://example.com/path")

    def test_builds_job_from_ticket_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ticket_path = Path(temp_dir) / "ticket_example.json"
            ticket_path.write_text(
                json.dumps(
                    {
                        "platform": "desktop",
                        "problem_type": "specific_ad_not_blocked",
                        "steps": ["Open https://example.com/path"],
                    }
                ),
                encoding="utf-8",
            )

            job = build_job_from_ticket(ticket_path)

            self.assertEqual(job.domain, "example.com")
            self.assertEqual(job.url, "https://example.com/path")
            self.assertEqual(job.environment, "desktop")
            self.assertEqual(job.jira_ticket_code, "ticket_example")

    def test_failed_ticket_is_not_claimed_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            tickets_dir = base / "tickets"
            tickets_dir.mkdir()
            ticket_path = tickets_dir / "ticket_example.json"
            ticket_path.write_text(
                json.dumps({"steps": ["Open https://example.com/path"]}),
                encoding="utf-8",
            )
            source = FileJobSource(
                tickets_dir=tickets_dir,
                ledger_file=base / "processed_tickets.json",
            )

            job = source.claim_next()

            self.assertIsNotNone(job)
            source.mark_failed(
                job,
                "test failure",
                {"status": "failed", "error_message": "test failure"},
            )

            self.assertIsNone(source.claim_next())

    def test_worker_keeps_polling_until_explicitly_stopped(self) -> None:
        class EmptySource:
            name = "empty"

            def __init__(self) -> None:
                self.claim_count = 0

            def claim_next(self):
                self.claim_count += 1
                return None

            def mark_completed(self, job, output):
                raise AssertionError("No job should be completed")

            def mark_failed(self, job, error_message, output):
                raise AssertionError("No job should fail")

        class TestStopFlag:
            def __init__(self) -> None:
                self.value = False
                self.wait_count = 0

            def install(self) -> None:
                pass

            def wait(self, _seconds: int) -> None:
                self.wait_count += 1
                if self.wait_count == 3:
                    self.value = True

        source = EmptySource()
        with patch.object(worker_module, "StopFlag", TestStopFlag):
            processed = run_worker(
                source=source,
                sleep_seconds=0,
                once=False,
            )

        self.assertEqual(processed, 0)
        self.assertEqual(source.claim_count, 3)

if __name__ == "__main__":
    unittest.main()

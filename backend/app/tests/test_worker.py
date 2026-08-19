import unittest
from unittest.mock import patch

import app.services.worker as worker_module
from app.services.worker import run_worker


class FakeJob:
    def __init__(self, report_id: str) -> None:
        self.report_id = report_id
        self.url = "https://example.com/"
        self.environment = "desktop"
        self.input_id = 1


class StubStopFlag:
    """Stops the loop after a fixed number of idle/backoff waits."""

    def __init__(self, stop_after: int = 3) -> None:
        self.value = False
        self.waits = 0
        self.stop_after = stop_after

    def install(self) -> None:
        pass

    def wait(self, _seconds: int) -> None:
        self.waits += 1
        if self.waits >= self.stop_after:
            self.value = True


class WorkerLoopTests(unittest.TestCase):
    def test_sleeps_and_exits_when_no_work(self) -> None:
        claims = []

        def claim():
            claims.append(1)
            return None

        with patch.object(worker_module, "claim_next_job", claim), \
             patch.object(worker_module, "StopFlag", StubStopFlag):
            processed = run_worker(sleep_seconds=0)

        self.assertEqual(processed, 0)
        self.assertEqual(len(claims), 3)

    def test_continues_after_a_claim_error(self) -> None:
        """A dropped database connection must not end the service."""
        state = {"n": 0}

        def claim():
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("MySQL server has gone away")
            return None

        with patch.object(worker_module, "claim_next_job", claim), \
             patch.object(worker_module, "StopFlag", StubStopFlag):
            processed = run_worker(sleep_seconds=0)

        self.assertEqual(processed, 0)
        self.assertGreater(state["n"], 1, "worker stopped on the first claim error")

    def test_continues_to_next_record_after_a_failing_one(self) -> None:
        """One bad record must not block the ones behind it."""
        state = {"n": 0}
        done = []

        def claim():
            state["n"] += 1
            if state["n"] == 1:
                return FakeJob("bad")
            if state["n"] == 2:
                return FakeJob("good")
            return None

        def process(job, **_kwargs):
            if job.report_id == "bad":
                raise RuntimeError("pipeline crashed and could not be recorded")
            done.append(job.report_id)

        with patch.object(worker_module, "claim_next_job", claim), \
             patch.object(worker_module, "process_job", process), \
             patch.object(worker_module, "StopFlag", StubStopFlag):
            processed = run_worker(sleep_seconds=0)

        self.assertEqual(done, ["good"])
        self.assertEqual(processed, 1)


if __name__ == "__main__":
    unittest.main()

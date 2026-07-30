"""
Long-running worker service for the crawl -> generate -> validate pipeline.

Run from backend/:
    python -m app.services.worker

Run through Docker Compose:
    docker compose up backend
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol
from urllib.parse import urlparse

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


logger = logging.getLogger(__name__)

DEFAULT_TICKETS_DIR = Path("app/tests/tickets")
DEFAULT_LEDGER_FILE = Path("data/service_worker/processed_tickets.json")
DEFAULT_SLEEP_SECONDS = 300
DEFAULT_MAX_IDLE_CYCLES = 0  # 0 = run forever

SUCCESS_PIPELINE_STATUSES = {"ok", "generated", "no_rules"}
TERMINAL_FILE_STATUSES = {"completed", "failed"}

URL_RE = re.compile(r"https?://[^\s\]})\"'<>]+", re.IGNORECASE)
DOMAIN_PATH_RE = re.compile(
    r"(?<!@)\b((?:[a-z0-9-]+\.)+[a-z]{2,})(/[^\s\]})\"'<>]*)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WorkerJob:
    source_key: str
    input_id: Optional[int]
    report_id: str
    domain: str
    domain_type: str
    jira_ticket_code: str
    url: str
    ad_type: str
    ticket_context: Dict[str, Any]
    environment: str
    before_screenshot: str = ""


class JobSource(Protocol):
    name: str

    def claim_next(self) -> Optional[WorkerJob]:
        """Return one unprocessed job and mark it processing."""

    def mark_completed(self, job: WorkerJob, output: Mapping[str, Any]) -> None:
        """Persist a successful terminal result."""

    def mark_failed(
        self,
        job: WorkerJob,
        error_message: str,
        output: Mapping[str, Any],
    ) -> None:
        """Persist a failed terminal result. Failed jobs are not auto-retried."""


class FileJobSource:
    name = "files"

    def __init__(
        self,
        tickets_dir: Path = DEFAULT_TICKETS_DIR,
        ledger_file: Path = DEFAULT_LEDGER_FILE,
    ) -> None:
        self.tickets_dir = tickets_dir
        self.ledger_file = ledger_file
        self.ledger = self._load_ledger()

    def claim_next(self) -> Optional[WorkerJob]:
        for ticket_path in self._ticket_paths():
            source_key = str(ticket_path.as_posix())
            record = self.ledger.setdefault("tickets", {}).get(source_key, {})
            status = str(record.get("status") or "").lower()

            if status in TERMINAL_FILE_STATUSES or status == "processing":
                continue

            try:
                job = build_job_from_ticket(ticket_path)
            except Exception as exc:
                logger.exception("Invalid ticket %s: %s", ticket_path, exc)
                self._set_ticket_state(
                    source_key=source_key,
                    status="failed",
                    error_message=str(exc),
                )
                self._save_ledger()
                continue

            self._set_ticket_state(source_key, "processing", job=job)
            self._save_ledger()
            return job

        return None

    def mark_completed(self, job: WorkerJob, output: Mapping[str, Any]) -> None:
        self._set_ticket_state(
            source_key=job.source_key,
            status="completed",
            job=job,
            output=output,
        )
        self._save_ledger()

    def mark_failed(
        self,
        job: WorkerJob,
        error_message: str,
        output: Mapping[str, Any],
    ) -> None:
        self._set_ticket_state(
            source_key=job.source_key,
            status="failed",
            job=job,
            error_message=error_message,
            output=output,
        )
        self._save_ledger()

    def _ticket_paths(self) -> Iterable[Path]:
        if not self.tickets_dir.exists():
            logger.warning("Ticket directory does not exist: %s", self.tickets_dir)
            return []

        return sorted(self.tickets_dir.glob("*.json"))

    def _set_ticket_state(
        self,
        source_key: str,
        status: str,
        job: Optional[WorkerJob] = None,
        error_message: str = "",
        output: Optional[Mapping[str, Any]] = None,
    ) -> None:
        now = utc_now()
        tickets = self.ledger.setdefault("tickets", {})
        current = dict(tickets.get(source_key, {}))
        current["status"] = status
        current["updated_at"] = now

        if "created_at" not in current:
            current["created_at"] = now

        if status in TERMINAL_FILE_STATUSES:
            current["processed_at"] = now

        if error_message:
            current["error_message"] = error_message
        elif "error_message" in current:
            current["error_message"] = ""

        if job is not None:
            current.update(
                {
                    "report_id": job.report_id,
                    "domain": job.domain,
                    "jira_ticket_code": job.jira_ticket_code,
                    "url": job.url,
                    "environment": job.environment,
                }
            )

        if output is not None:
            current["output"] = make_json_safe(dict(output))

        tickets[source_key] = current

    def _load_ledger(self) -> Dict[str, Any]:
        if not self.ledger_file.exists():
            return {"tickets": {}}

        with open(self.ledger_file, "r", encoding="utf-8") as file:
            ledger = json.load(file)

        if not isinstance(ledger, dict):
            return {"tickets": {}}

        ledger.setdefault("tickets", {})
        return ledger

    def _save_ledger(self) -> None:
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.ledger_file.with_suffix(self.ledger_file.suffix + ".tmp")

        with open(tmp_file, "w", encoding="utf-8") as file:
            json.dump(make_json_safe(self.ledger), file, indent=2, ensure_ascii=False)

        tmp_file.replace(self.ledger_file)


class DatabaseJobSource:
    name = "db"

    def __init__(self) -> None:
        from app.database import SessionLocal
        from app.models import CrawlInput, RuleOutput

        if SessionLocal is None:
            raise RuntimeError("DATABASE_URL is not configured.")

        self.SessionLocal = SessionLocal
        self.CrawlInput = CrawlInput
        self.RuleOutput = RuleOutput

    def claim_next(self) -> Optional[WorkerJob]:
        from sqlalchemy import select

        with self.SessionLocal() as session:
            try:
                row = session.execute(
                    select(self.CrawlInput)
                    .where(self.CrawlInput.status == "new")
                    .order_by(self.CrawlInput.created_at.asc(), self.CrawlInput.id.asc())
                    .with_for_update(skip_locked=True)
                    .limit(1)
                ).scalar_one_or_none()

                if row is None:
                    session.commit()
                    return None

                row.status = "processing"
                row.error_message = ""
                row.updated_at = utc_now_naive()
                session.commit()
                session.refresh(row)

                return build_job_from_crawl_input(row)
            except Exception:
                session.rollback()
                raise

    def mark_completed(self, job: WorkerJob, output: Mapping[str, Any]) -> None:
        self._save_result(job, input_status="completed", error_message="", output=output)

    def mark_failed(
        self,
        job: WorkerJob,
        error_message: str,
        output: Mapping[str, Any],
    ) -> None:
        self._save_result(
            job,
            input_status="failed",
            error_message=error_message,
            output=output,
        )

    def _save_result(
        self,
        job: WorkerJob,
        input_status: str,
        error_message: str,
        output: Mapping[str, Any],
    ) -> None:
        if job.input_id is None:
            raise RuntimeError("Database jobs must have input_id.")

        with self.SessionLocal() as session:
            try:
                crawl_input = session.get(self.CrawlInput, job.input_id)

                if crawl_input is not None:
                    crawl_input.status = input_status
                    crawl_input.error_message = error_message
                    crawl_input.crawl_duration_ms = int(
                        output.get("crawl_duration_ms") or 0
                    )
                    crawl_input.updated_at = utc_now_naive()

                session.add(
                    self.RuleOutput(
                        input_id=job.input_id,
                        rules=json.dumps(
                            output.get("rules", []),
                            ensure_ascii=False,
                        ),
                        input_tokens=int(output.get("input_tokens") or 0),
                        output_tokens=int(output.get("output_tokens") or 0),
                        validation_result=make_json_safe(
                            output.get("validation_result", {})
                        ),
                        after_screenshot=str(output.get("after_screenshot") or ""),
                        status=str(output.get("status") or input_status),
                        error_message=error_message
                        or str(output.get("error_message") or ""),
                    )
                )
                session.commit()
            except Exception:
                session.rollback()
                raise


def run_worker(
    source: JobSource,
    sleep_seconds: int = DEFAULT_SLEEP_SECONDS,
    once: bool = False,
    max_idle_cycles: int = DEFAULT_MAX_IDLE_CYCLES,
    run_validation: bool = True,
    skip_external: bool = False,
    headless: bool = True,
) -> int:
    processed = 0
    idle_cycles = 0
    should_stop = StopFlag()
    should_stop.install()

    logger.info(
        "Worker started source=%s sleep=%ss once=%s max_idle=%s",
        source.name,
        sleep_seconds,
        once,
        max_idle_cycles or "unlimited",
    )

    try:
        while not should_stop.value:
            job = source.claim_next()

            if job is None:
                if once:
                    logger.info("No pending requests. Exiting because --once is set.")
                    return processed

                if max_idle_cycles > 0 and idle_cycles >= max_idle_cycles:
                    logger.info(
                        "Reached max idle cycles (%s). Stopping worker.",
                        max_idle_cycles,
                    )
                    return processed

                idle_cycles += 1
                logger.info(
                    "No pending requests, sleeping %ss... (idle %s/%s)",
                    sleep_seconds,
                    idle_cycles,
                    max_idle_cycles if max_idle_cycles > 0 else "unlimited",
                )

                should_stop.wait(sleep_seconds)
                continue

            # Reset idle counter when work is found
            idle_cycles = 0

            process_job(
                source=source,
                job=job,
                run_validation=run_validation,
                skip_external=skip_external,
                headless=headless,
            )
            processed += 1

            if once:
                return processed
    except KeyboardInterrupt:
        logger.info("Worker interrupted. Exiting after current loop.")

    return processed


def process_job(
    source: JobSource,
    job: WorkerJob,
    run_validation: bool = True,
    skip_external: bool = False,
    headless: bool = True,
) -> None:
    from app.services.workflow import run_pipeline

    logger.info(
        "Processing job source=%s report_id=%s url=%s env=%s",
        source.name,
        job.report_id,
        job.url,
        job.environment,
    )
    started_at = time.perf_counter()

    try:
        result = run_pipeline(
            report_id=job.report_id,
            verbose=False,
            run_validation=run_validation,
            skip_external=skip_external,
            url=job.url,
            environment=job.environment,
            ticket_context=job.ticket_context,
            headless=headless,
            enable_scroll=True,
            interactive=False,
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        error_message = pipeline_error_message(result)
        output = build_output_payload(job, result, duration_ms, error_message)

        if result.get("status") in SUCCESS_PIPELINE_STATUSES:
            source.mark_completed(job, output)
            logger.info(
                "Completed report_id=%s status=%s rules_passed=%s",
                job.report_id,
                result.get("status"),
                result.get("rules_passed", 0),
            )
            return

        source.mark_failed(job, error_message or "Pipeline failed", output)
        logger.error(
            "Failed report_id=%s status=%s error=%s",
            job.report_id,
            result.get("status", "failed"),
            error_message,
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        error_message = str(exc)
        output = build_output_payload(
            job,
            {"status": "failed"},
            duration_ms,
            error_message,
        )
        source.mark_failed(job, error_message, output)
        logger.exception("Unhandled job failure report_id=%s: %s", job.report_id, exc)


def build_job_from_ticket(ticket_path: Path) -> WorkerJob:
    with open(ticket_path, "r", encoding="utf-8") as file:
        ticket = json.load(file)

    if not isinstance(ticket, Mapping):
        raise ValueError("Ticket file must contain a JSON object.")

    url = extract_url_from_ticket(ticket)

    if not url:
        raise ValueError("Ticket does not contain a usable URL.")

    domain = str(ticket.get("domain") or hostname_from_url(url)).strip()
    jira_ticket_code = str(
        ticket.get("jira_ticket_code")
        or ticket.get("ticket_code")
        or ticket.get("ticket_id")
        or ticket_path.stem
    )
    report_id = safe_report_id(str(ticket.get("report_id") or jira_ticket_code))
    environment = resolve_environment(ticket, default="desktop")

    return WorkerJob(
        source_key=str(ticket_path.as_posix()),
        input_id=None,
        report_id=report_id,
        domain=domain,
        domain_type=str(ticket.get("domain_type") or "test_ticket"),
        jira_ticket_code=jira_ticket_code,
        url=url,
        ad_type=str(ticket.get("ad_type") or ticket.get("problem_type") or ""),
        ticket_context=make_json_safe(dict(ticket)),
        environment=environment,
        before_screenshot=str(
            ticket.get("before_screenshot")
            or ticket.get("screenshot_url")
            or ""
        ),
    )


def build_job_from_crawl_input(row: Any) -> WorkerJob:
    ticket_context = coerce_json_object(getattr(row, "ticket_context", None))
    url = normalize_url(str(getattr(row, "url", "") or ""))

    if not url:
        url = extract_url_from_ticket(ticket_context)

    if not url:
        raise ValueError(f"crawl_inputs.id={row.id} has no usable URL.")

    domain = str(getattr(row, "domain", "") or hostname_from_url(url)).strip()
    jira_ticket_code = str(getattr(row, "jira_ticket_code", "") or f"input-{row.id}")

    return WorkerJob(
        source_key=str(row.id),
        input_id=int(row.id),
        report_id=safe_report_id(jira_ticket_code),
        domain=domain,
        domain_type=str(getattr(row, "domain_type", "") or ""),
        jira_ticket_code=jira_ticket_code,
        url=url,
        ad_type=str(getattr(row, "ad_type", "") or ticket_context.get("problem_type") or ""),
        ticket_context=ticket_context,
        environment=resolve_environment(ticket_context, default="desktop"),
        before_screenshot=str(getattr(row, "before_screenshot", "") or ""),
    )


def build_output_payload(
    job: WorkerJob,
    pipeline_result: Mapping[str, Any],
    duration_ms: int,
    error_message: str,
) -> Dict[str, Any]:
    rules_artifact = load_json_if_exists(
        Path("data/rule_outputs/results") / f"{job.report_id}_rules.json"
    )
    validation_artifact = load_json_if_exists(
        Path("data/rule_outputs/validation") / f"{job.report_id}_validation.json"
    )
    token_usage = coerce_json_object(rules_artifact.get("token_usage"))
    rules = select_output_rules(pipeline_result, rules_artifact)
    status = str(pipeline_result.get("status") or "failed")

    return {
        "input_id": job.input_id,
        "report_id": job.report_id,
        "url": job.url,
        "rules": rules,
        "input_tokens": int(token_usage.get("prompt_tokens") or 0),
        "output_tokens": int(token_usage.get("completion_tokens") or 0),
        "validation_result": validation_artifact
        or make_json_safe(dict(pipeline_result)),
        "after_screenshot": str(
            pipeline_result.get("combined_screenshot")
            or validation_artifact.get("combined_screenshot")
            or ""
        ),
        "status": status,
        "error_message": error_message,
        "crawl_duration_ms": duration_ms,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def select_output_rules(
    pipeline_result: Mapping[str, Any],
    rules_artifact: Mapping[str, Any],
) -> list[str]:
    passing_rules = pipeline_result.get("passing_rules", [])

    if isinstance(passing_rules, list) and passing_rules:
        return [str(rule).strip() for rule in passing_rules if str(rule).strip()]

    generated_rules = rules_artifact.get("rules", [])
    selected: list[str] = []

    if isinstance(generated_rules, list):
        for item in generated_rules:
            if isinstance(item, Mapping):
                rule = str(item.get("rule") or "").strip()
            else:
                rule = str(item).strip()

            if rule:
                selected.append(rule)

    return selected


def pipeline_error_message(result: Mapping[str, Any]) -> str:
    if result.get("status") == "crawl_failed":
        return str(result.get("crawl_error") or "Crawl failed")

    if result.get("status") in SUCCESS_PIPELINE_STATUSES:
        return ""

    return str(result.get("error") or result.get("status") or "Pipeline failed")


def extract_url_from_ticket(ticket: Mapping[str, Any]) -> str:
    direct = normalize_url(str(ticket.get("url") or ""))

    if direct:
        return direct

    for text in iter_ticket_text(ticket):
        match = URL_RE.search(text)

        if match:
            return normalize_url(match.group(0))

    for text in iter_ticket_text(ticket):
        match = DOMAIN_PATH_RE.search(text)

        if match:
            return normalize_url(f"https://{match.group(1)}{match.group(2) or ''}")

    domain = str(ticket.get("domain") or "").strip()
    return normalize_url(domain) if domain else ""


def iter_ticket_text(ticket: Mapping[str, Any]) -> Iterable[str]:
    for key in ("request", "description", "actual", "expected", "domain"):
        value = ticket.get(key)

        if value:
            yield str(value)

    steps = ticket.get("steps", [])

    if isinstance(steps, list):
        for step in steps:
            if step:
                yield str(step)
    elif steps:
        yield str(steps)


def normalize_url(value: str) -> str:
    candidate = value.strip().strip(".,;:)]}\"'")

    if not candidate:
        return ""

    if not re.match(r"^https?://", candidate, re.IGNORECASE):
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)

    if not parsed.hostname:
        return ""

    return candidate


def hostname_from_url(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def resolve_environment(context: Mapping[str, Any], default: str) -> str:
    platform = str(context.get("platform") or "").strip().lower()

    if platform in {"desktop", "android", "ios"}:
        return platform

    return default


def load_json_if_exists(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not read JSON artifact %s: %s", path, exc)
        return {}


def coerce_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return make_json_safe(dict(value))

    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")

    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            return make_json_safe(data) if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {"raw": value}

    return {}


def make_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        return {str(key): make_json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(item) for item in value]

    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"

    return str(value)


def safe_report_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return cleaned or f"ticket-{stable_hash(value)}"


def stable_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StopFlag:
    def __init__(self) -> None:
        self.value = False

    def install(self) -> None:
        def handle_stop(signum: int, frame: Any) -> None:
            logger.info("Received signal %s. Stopping after current job.", signum)
            self.value = True

        try:
            signal.signal(signal.SIGTERM, handle_stop)
            signal.signal(signal.SIGINT, handle_stop)
        except ValueError:
            # Signals can only be installed from the main thread.
            pass

    def wait(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds

        while not self.value and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))


def database_tables_exist() -> bool:
    if not os.getenv("DATABASE_URL", "").strip():
        return False

    try:
        from sqlalchemy import inspect

        from app.database import engine

        if engine is None:
            return False

        inspector = inspect(engine)
        return (
            inspector.has_table("crawl_inputs")
            and inspector.has_table("rule_outputs")
        )
    except Exception as exc:
        logger.info("Database mode unavailable, falling back to files: %s", exc)
        return False


def build_source(args: argparse.Namespace) -> JobSource:
    if args.source == "files":
        return FileJobSource(
            tickets_dir=Path(args.tickets_dir),
            ledger_file=Path(args.ledger_file),
        )

    if args.source == "db":
        return DatabaseJobSource()

    if database_tables_exist():
        logger.info("DATABASE_URL and worker tables detected; using database mode.")
        return DatabaseJobSource()

    logger.info("Using file mode.")
    return FileJobSource(
        tickets_dir=Path(args.tickets_dir),
        ledger_file=Path(args.ledger_file),
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the crawl -> generate -> validate worker service.",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "files", "db"],
        default=os.getenv("WORKER_SOURCE", "auto"),
        help="Job source. auto uses DB when DATABASE_URL and tables exist; otherwise files.",
    )
    parser.add_argument(
        "--tickets-dir",
        default=os.getenv("WORKER_TICKETS_DIR", str(DEFAULT_TICKETS_DIR)),
        help="Ticket JSON directory for file mode.",
    )
    parser.add_argument(
        "--ledger-file",
        default=os.getenv("WORKER_LEDGER_FILE", str(DEFAULT_LEDGER_FILE)),
        help="File-mode processed_tickets.json ledger.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one job, or exit immediately if none is available.",
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=int(os.getenv("WORKER_SLEEP_SECONDS", str(DEFAULT_SLEEP_SECONDS))),
        help="Seconds to sleep when no work is available.",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="Skip validation/sandbox checks.",
    )
    parser.add_argument(
        "--no-external",
        action="store_true",
        help="Skip external filter-list duplicate checks.",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Open a visible browser window during crawl.",
    )
    parser.add_argument(
        "--max-idle",
        type=int,
        default=int(os.getenv("WORKER_MAX_IDLE_CYCLES", str(DEFAULT_MAX_IDLE_CYCLES))),
        help="Stop after this many consecutive idle sleep cycles. Use 0 for unlimited.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    log_level = os.getenv("WORKER_LOG_LEVEL", "INFO").upper()

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    source = build_source(args)
    processed = run_worker(
        source=source,
        sleep_seconds=max(args.sleep, 1),
        once=args.once,
        max_idle_cycles=max(args.max_idle, 0),
        run_validation=not args.no_sandbox,
        skip_external=args.no_external,
        headless=not args.no_headless,
    )
    logger.info("Worker stopped after processing %s job(s).", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

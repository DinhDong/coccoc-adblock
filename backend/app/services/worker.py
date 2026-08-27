"""
Long-running worker service for the crawl -> generate -> validate pipeline.

Run from backend/:
    python -m app.services.worker

Run through Docker Compose:
    docker compose up backend
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
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
DEFAULT_SLEEP_SECONDS = 5


# The only status the worker claims. Drafts are deliberately never picked up:
# a draft is someone still writing the ticket, and crawling it would spend a
# real page load and LLM call on a URL they had not finished choosing.
CLAIMABLE_STATUS = "new"


# A row in one of these was mid-run when a previous worker died. The worker is
# the only thing that runs the pipeline, so at startup nothing can legitimately
# be in progress and these are safe to requeue.
STRANDED_STATUSES = ("processing", "crawling", "generating", "validating", "inprocess")

# Must match the set the HTTP API treats as success (app/__init__.py).
# run_pipeline returns "review" on its main success path; leaving it out
# marked every completed run as failed.
SUCCESS_PIPELINE_STATUSES = {"review", "validated", "generated", "no_rules", "ok"}
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


def _dict_to_ns(d: dict) -> Any:
    """Convert a dict row to a SimpleNamespace so getattr() works."""
    from types import SimpleNamespace
    return SimpleNamespace(**d)


def requeue_stranded() -> int:
    """
    Put rows left mid-run by a dead worker back in the queue.

    Safe because this process is the only thing that runs the pipeline: if the
    worker has just started, nothing can genuinely be in progress. Previously
    the API did this at boot, which was wrong once the worker moved to its own
    container — it could reset a run that was actively happening elsewhere.
    """
    from app.database import get_connection

    placeholders = ",".join(["%s"] * len(STRANDED_STATUSES))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE crawl_inputs SET status='new', updated_at=NOW() "
                f"WHERE status IN ({placeholders})",
                STRANDED_STATUSES,
            )
            return cur.rowcount
    finally:
        conn.close()


def claim_next_job() -> Optional[WorkerJob]:
    """
    Take the oldest unclaimed request and mark it processing.

    Talks to MySQL directly — there is no source abstraction and nothing to
    configure. FOR UPDATE SKIP LOCKED means several worker containers can run
    at once without two of them grabbing the same row.
    """
    from app.database import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            conn.begin()
            cur.execute(
                "SELECT * FROM crawl_inputs "
                "WHERE status = %s "
                "ORDER BY created_at ASC, id ASC "
                "LIMIT 1 "
                "FOR UPDATE SKIP LOCKED",
                (CLAIMABLE_STATUS,),
            )
            row = cur.fetchone()

            if row is None:
                conn.commit()
                return None

            # run_started_at is stamped once, here, and not touched again for
            # the rest of the run — it is what the UI counts elapsed time from
            # while the pipeline is in flight.
            cur.execute(
                "UPDATE crawl_inputs SET status='processing', error_message='', "
                "run_started_at=NOW(), updated_at=NOW() WHERE id=%s",
                (row["id"],),
            )
            conn.commit()

            return build_job_from_crawl_input(_dict_to_ns(row))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_completed(job: WorkerJob, output: Mapping[str, Any]) -> None:
    save_job_result(job, input_status="completed", error_message="", output=output)


def mark_failed(job: WorkerJob, error_message: str, output: Mapping[str, Any]) -> None:
    save_job_result(
        job,
        input_status="failed",
        error_message=error_message,
        output=output,
    )


def _rules_column_value(output: Mapping[str, Any]) -> Any:
    """
    Decide what belongs in rule_outputs.rules.

    run_rule_generation already stores the full generation record there. This
    used to overwrite it with the flattened list from select_output_rules,
    which threw away duplicates_skipped — the only record of *why* a run has
    nothing to review. A report whose every generated rule was already in the
    registry then reached the UI as an empty ticket with no explanation.
    Prefer the record; fall back to the flat list when there is no record.
    """
    record = output.get("rules_record")
    if isinstance(record, Mapping) and record:
        return record
    return output.get("rules", [])


def save_job_result(
    job: WorkerJob,
    input_status: str,
    error_message: str,
    output: Mapping[str, Any],
) -> None:
    if job.input_id is None:
        raise RuntimeError("Worker jobs must have input_id.")

    from app.database import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            conn.begin()
            cur.execute(
                "UPDATE crawl_inputs SET status=%s, error_message=%s, "
                "crawl_duration_ms=%s, updated_at=NOW() WHERE id=%s",
                (
                    input_status,
                    error_message,
                    int(output.get("crawl_duration_ms") or 0),
                    job.input_id,
                ),
            )
            # Upsert: run_pipeline has already written a row for this input
            # via save_rule_output/save_rule_validation. Inserting again left
            # two rows per report, which duplicated every ticket in the API's
            # LEFT JOIN against rule_outputs.
            cur.execute(
                "SELECT id FROM rule_outputs WHERE input_id=%s", (job.input_id,)
            )
            existing = cur.fetchone()

            cur.execute(
                (
                    "UPDATE rule_outputs SET rules=%s, input_tokens=%s, "
                    "output_tokens=%s, validation_result=%s, after_screenshot=%s, "
                    "status=%s, error_message=%s, updated_at=NOW() WHERE input_id=%s"
                    if existing else
                    "INSERT INTO rule_outputs "
                    "(rules, input_tokens, output_tokens, validation_result, "
                    "after_screenshot, status, error_message, input_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                ),
                (
                    json.dumps(
                        make_json_safe(_rules_column_value(output)),
                        ensure_ascii=False,
                    ),
                    int(output.get("input_tokens") or 0),
                    int(output.get("output_tokens") or 0),
                    json.dumps(
                        make_json_safe(output.get("validation_result", {})),
                        ensure_ascii=False,
                    ),
                    str(output.get("after_screenshot") or ""),
                    str(output.get("status") or input_status),
                    error_message or str(output.get("error_message") or ""),
                    job.input_id,
                ),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



def run_worker(
    sleep_seconds: int = DEFAULT_SLEEP_SECONDS,
    run_validation: bool = True,
    skip_external: bool = False,
    headless: bool = True,
) -> int:
    """
    Poll the database forever, processing one request at a time.

    Every failure mode is contained to the record that caused it. Claiming,
    processing and recording a result are each wrapped so that a bad row, a
    dropped MySQL connection or a crashing pipeline logs and moves on to the
    next record rather than killing the service. Only a stop signal ends it.
    """
    processed = 0
    consecutive_errors = 0
    should_stop = StopFlag()
    should_stop.install()

    # The claim query below reads columns declared in database.py, but only
    # that module's write paths ran migrations — so against a database with a
    # column still missing the worker would fail every claim without ever
    # reaching the write that would have added it. Failures here are logged
    # and tolerated: the retry loop already handles an unreachable database.
    try:
        from app.database import ensure_schema

        ensure_schema()
    except Exception as exc:
        logger.warning("Schema check skipped at startup: %s", exc)

    logger.info("Worker started sleep=%ss (database source)", sleep_seconds)

    while not should_stop.value:
        try:
            job = claim_next_job()
        except Exception as exc:
            # Usually MySQL being unreachable. Back off and try again rather
            # than exiting, so the container does not need restarting.
            consecutive_errors += 1
            logger.exception("Could not claim a request: %s", exc)
            should_stop.wait(min(sleep_seconds, 30 * consecutive_errors) or 30)
            continue

        consecutive_errors = 0

        if job is None:
            logger.info("No pending requests, sleeping %ss...", sleep_seconds)
            should_stop.wait(sleep_seconds)
            continue

        try:
            process_job(
                job=job,
                run_validation=run_validation,
                skip_external=skip_external,
                headless=headless,
            )
            processed += 1
        except Exception as exc:
            # process_job records its own failures; reaching here means even
            # that bookkeeping failed. The row stays 'processing' and will need
            # a manual reset, but the worker keeps serving everything else.
            logger.exception(
                "Giving up on report_id=%s and continuing: %s",
                job.report_id,
                exc,
            )

    logger.info("Worker stopped after processing %d request(s).", processed)
    return processed



def process_job(
    job: WorkerJob,
    run_validation: bool = True,
    skip_external: bool = False,
    headless: bool = True,
) -> None:
    from app.services.workflow import run_pipeline

    logger.info(
        "Processing report_id=%s url=%s env=%s",
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
            # Never block on the CLI's "domain already has rules" prompt: keep
            # what is already registered and only add genuinely new rules.
            # (This replaced interactive=False, which was not a run_pipeline
            # parameter and reached render_url as an unexpected kwarg.)
            duplicate_choice="keep",
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        error_message = pipeline_error_message(result)
        output = build_output_payload(job, result, duration_ms, error_message)

        if result.get("status") in SUCCESS_PIPELINE_STATUSES:
            mark_completed(job, output)
            logger.info(
                "Completed report_id=%s status=%s rules_passed=%s",
                job.report_id,
                result.get("status"),
                result.get("rules_passed", 0),
            )
            return

        mark_failed(job, error_message or "Pipeline failed", output)
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
        logger.exception("Unhandled job failure report_id=%s: %s", job.report_id, exc)
        mark_failed(job, error_message, output)



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
        # The generation record exactly as run_rule_generation wrote it. It
        # carries duplicates_skipped, the model, and per-rule types; `rules`
        # above is only the flattened rule text. tickets._duplicates_for_ui
        # reads this record to explain a run that produced nothing because
        # every rule it generated was already known — a bare list leaves it
        # with nothing to report and the ticket looks broken instead.
        "rules_record": rules_artifact if isinstance(rules_artifact, Mapping) and rules_artifact else None,
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
    """
    Pick the crawl environment out of a ticket context.

    The UI stores it as "env"; the pipeline's normalised context calls the same
    thing "platform". Only "platform" was checked, so a ticket created as
    Android or iOS silently ran as desktop.
    """
    for key in ("platform", "env", "environment"):
        value = str(context.get(key) or "").strip().lower()
        if value in {"desktop", "android", "ios"}:
            return value

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
    from app.database import MYSQL_USER, MYSQL_DATABASE

    if not MYSQL_USER or not MYSQL_DATABASE:
        return False

    try:
        from app.database import get_connection

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                tables = {list(row.values())[0] for row in cur.fetchall()}
                return "crawl_inputs" in tables and "rule_outputs" in tables
        finally:
            conn.close()
    except Exception as exc:
        logger.info("Database mode unavailable, falling back to files: %s", exc)
        return False


def main() -> int:
    """
    Entry point: `python -m app.services.worker`.

    Takes no arguments. The database is the only source of work, and the few
    knobs that exist are read from the environment so the same image behaves
    the same whether it is started by compose or by hand.
    """
    logging.basicConfig(
        level=os.getenv("WORKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        sleep_seconds = max(1, int(os.getenv("WORKER_SLEEP_SECONDS", str(DEFAULT_SLEEP_SECONDS))))
    except ValueError:
        sleep_seconds = DEFAULT_SLEEP_SECONDS

    if not database_tables_exist():
        logger.error(
            "MySQL is not reachable or crawl_inputs/rule_outputs are missing. "
            "Start the database and try again."
        )
        return 1

    try:
        requeued = requeue_stranded()
        if requeued:
            logger.warning("Requeued %d run(s) stranded by a previous worker", requeued)
    except Exception as exc:
        logger.warning("Could not requeue stranded runs: %s", exc)

    logger.info("Claiming status: %s", CLAIMABLE_STATUS)

    run_worker(
        sleep_seconds=sleep_seconds,
        run_validation=os.getenv("WORKER_SKIP_VALIDATION", "").lower() not in {"1", "true", "yes"},
        skip_external=os.getenv("WORKER_SKIP_EXTERNAL", "").lower() in {"1", "true", "yes"},
        headless=os.getenv("WORKER_HEADFUL", "").lower() not in {"1", "true", "yes"},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

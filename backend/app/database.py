"""
Database engine and session helpers.

The worker can run without a database in file mode. Database mode is enabled
when DATABASE_URL is configured, for example:

    mysql+pymysql://adblock:adblock_pass@db:3306/adblock
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    # override=True makes the file authoritative over whatever is already in
    # the process environment. Without it, editing a key that had already been
    # loaded silently had no effect — a stale MYSQL_USER would survive while a
    # newly added MYSQL_HOST loaded, producing a host/credential mismatch.
    # Order is least-specific first, because with override the last load wins.
    # In Docker this is a no-op: .env.local is in .dockerignore, so no file is
    # present and the values come from compose's env_file instead.
    load_dotenv(repo_root / "backend" / ".env", override=True)
    load_dotenv(repo_root / "backend" / ".env.local", override=True)
    load_dotenv(repo_root / ".env", override=True)
    load_dotenv(repo_root / ".env.local", override=True)
except ImportError:
    pass

import pymysql
from pymysql.cursors import DictCursor

_db_schema_initialized = False
_schema_lock = threading.Lock()

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE")


def get_connection():
    if not MYSQL_USER or not MYSQL_PASSWORD or not MYSQL_DATABASE:
        raise RuntimeError(
            "Missing MySQL configuration. Please set MYSQL_USER, MYSQL_PASSWORD, and MYSQL_DATABASE."
        )

    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )


def _json_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute("SHOW COLUMNS FROM `%s` LIKE %%s" % table_name, (column_name,))
    return cursor.fetchone() is not None


def _ensure_schema() -> None:
    global _db_schema_initialized
    if _db_schema_initialized:
        return

    # Double-checked locking. Pipeline runs execute on a worker pool now, so
    # two threads can reach this together: both would pass the _column_exists
    # check and both would issue the same ALTER, and the second fails with
    # "duplicate column". The flag alone never guarded that window.
    with _schema_lock:
        if _db_schema_initialized:
            return
        _ensure_schema_locked()


def _ensure_schema_locked() -> None:
    global _db_schema_initialized

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
CREATE TABLE IF NOT EXISTS crawl_inputs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    domain TEXT NOT NULL,
    domain_type VARCHAR(50),
    jira_ticket_code VARCHAR(255),
    url TEXT NOT NULL,
    ad_type VARCHAR(100),
    ticket_context TEXT,
    before_screenshot TEXT,
    crawl_duration_ms INT,
    status VARCHAR(50) DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
)"""
            )
            cur.execute(
                """
CREATE TABLE IF NOT EXISTS rule_outputs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    input_id BIGINT,
    rules TEXT,
    input_tokens INT,
    output_tokens INT,
    validation_result JSON,
    after_screenshot TEXT,
    status VARCHAR(50) DEFAULT 'generated',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_rule_outputs_crawl_inputs
        FOREIGN KEY (input_id)
        REFERENCES crawl_inputs(id)
        ON DELETE CASCADE
)"""
            )

            # Per-rule approve/reject decisions, keyed by rule text:
            # {"<rule>": {"decision": "approve"|"reject", "by": str, "at": iso}}
            if not _column_exists(cur, "rule_outputs", "decisions"):
                cur.execute(
                    "ALTER TABLE rule_outputs ADD COLUMN decisions JSON AFTER validation_result"
                )

            # {kind: s3_uri} for the three screenshots that document a report.
            if not _column_exists(cur, "rule_outputs", "images"):
                cur.execute(
                    "ALTER TABLE rule_outputs ADD COLUMN images JSON AFTER after_screenshot"
                )

            if not _column_exists(cur, "crawl_inputs", "report_id"):
                cur.execute(
                    "ALTER TABLE crawl_inputs ADD COLUMN report_id VARCHAR(255) UNIQUE AFTER id"
                )

            if _column_exists(cur, "crawl_inputs", "report_id"):
                cur.execute(
                    "UPDATE crawl_inputs SET report_id = jira_ticket_code WHERE report_id IS NULL AND jira_ticket_code IS NOT NULL"
                )

    _db_schema_initialized = True


def _get_crawl_input_id(report_id: str) -> Optional[int]:
    _ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            if _column_exists(cur, "crawl_inputs", "report_id"):
                cur.execute(
                    "SELECT id FROM crawl_inputs WHERE report_id=%s",
                    (report_id,),
                )
            else:
                cur.execute(
                    "SELECT id FROM crawl_inputs WHERE jira_ticket_code=%s",
                    (report_id,),
                )
            row = cur.fetchone()
            if row:
                return row["id"]

            # Fallback: some older rows may have the report name embedded in
            # the JSON `ticket_context` text but not in `report_id` or
            # `jira_ticket_code`. Try a simple LIKE search to locate those
            # rows and return the id if found. This helps avoid creating a
            # duplicate crawl_inputs row with a generated UUID when the
            # frontend supplied a stable `name` but the DB row only contains
            # it inside `ticket_context`.
            try:
                pattern = f"%{report_id}%"
                cur.execute(
                    "SELECT id FROM crawl_inputs WHERE ticket_context LIKE %s",
                    (pattern,),
                )
                row2 = cur.fetchone()
                return row2["id"] if row2 else None
            except Exception:
                return None


def save_crawl_input(
    report_id: str,
    domain: str,
    url: str,
    ticket_context: Optional[Dict[str, Any]] = None,
    status: str = "pending",
    error_message: Optional[str] = None,
    crawl_duration_ms: Optional[int] = None,
    before_screenshot: Optional[str] = None,
    domain_type: Optional[str] = None,
    jira_ticket_code: Optional[str] = None,
    ad_type: Optional[str] = None,
) -> int:
    _ensure_schema()
    record = {
        "report_id": report_id,
        "domain": domain,
        "domain_type": domain_type,
        "jira_ticket_code": jira_ticket_code or report_id,
        "url": url,
        "ad_type": ad_type,
        "ticket_context": _json_value(ticket_context),
        "before_screenshot": before_screenshot,
        "crawl_duration_ms": crawl_duration_ms,
        "status": status,
        "error_message": error_message,
    }

    existing_id = _get_crawl_input_id(report_id)

    # Merge rather than replace. The pipeline calls this with its own
    # normalised context on every stage transition; replacing wiped the fields
    # the UI owns (env, focus, targets, createdBy), so after one run every
    # report displayed as Desktop with no targets no matter how it was created.
    if existing_id and ticket_context is not None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ticket_context FROM crawl_inputs WHERE id=%s",
                    (existing_id,),
                )
                row = cur.fetchone()
        if row and row.get("ticket_context"):
            try:
                previous = json.loads(row["ticket_context"])
                if isinstance(previous, dict):
                    merged = dict(previous)
                    merged.update(ticket_context)
                    record["ticket_context"] = _json_value(merged)
            except Exception:
                pass

    with get_connection() as conn:
        with conn.cursor() as cur:
            if _column_exists(cur, "crawl_inputs", "report_id"):
                if existing_id:
                    cur.execute(
                        """
UPDATE crawl_inputs
SET domain=%s,
    domain_type=%s,
    jira_ticket_code=%s,
    url=%s,
    ad_type=%s,
    ticket_context=%s,
    before_screenshot=%s,
    crawl_duration_ms=%s,
    status=%s,
    error_message=%s,
    updated_at=NOW()
WHERE report_id=%s
""",
                        (
                            record["domain"],
                            record["domain_type"],
                            record["jira_ticket_code"],
                            record["url"],
                            record["ad_type"],
                            record["ticket_context"],
                            record["before_screenshot"],
                            record["crawl_duration_ms"],
                            record["status"],
                            record["error_message"],
                            report_id,
                        ),
                    )
                    return existing_id

                cur.execute(
                    """
INSERT INTO crawl_inputs (
    report_id,
    domain,
    domain_type,
    jira_ticket_code,
    url,
    ad_type,
    ticket_context,
    before_screenshot,
    crawl_duration_ms,
    status,
    error_message
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
""",
                    (
                        record["report_id"],
                        record["domain"],
                        record["domain_type"],
                        record["jira_ticket_code"],
                        record["url"],
                        record["ad_type"],
                        record["ticket_context"],
                        record["before_screenshot"],
                        record["crawl_duration_ms"],
                        record["status"],
                        record["error_message"],
                    ),
                )
                return cur.lastrowid

            if existing_id:
                cur.execute(
                    """
UPDATE crawl_inputs
SET domain=%s,
    domain_type=%s,
    jira_ticket_code=%s,
    url=%s,
    ad_type=%s,
    ticket_context=%s,
    before_screenshot=%s,
    crawl_duration_ms=%s,
    status=%s,
    error_message=%s,
    updated_at=NOW()
WHERE jira_ticket_code=%s
""",
                    (
                        record["domain"],
                        record["domain_type"],
                        record["jira_ticket_code"],
                        record["url"],
                        record["ad_type"],
                        record["ticket_context"],
                        record["before_screenshot"],
                        record["crawl_duration_ms"],
                        record["status"],
                        record["error_message"],
                        report_id,
                    ),
                )
                return existing_id

            cur.execute(
                """
INSERT INTO crawl_inputs (
    domain,
    domain_type,
    jira_ticket_code,
    url,
    ad_type,
    ticket_context,
    before_screenshot,
    crawl_duration_ms,
    status,
    error_message
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
""",
                (
                    record["domain"],
                    record["domain_type"],
                    record["jira_ticket_code"],
                    record["url"],
                    record["ad_type"],
                    record["ticket_context"],
                    record["before_screenshot"],
                    record["crawl_duration_ms"],
                    record["status"],
                    record["error_message"],
                ),
            )
            return cur.lastrowid


def _get_rule_output_id(input_id: int) -> Optional[int]:
    _ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM rule_outputs WHERE input_id=%s",
                (input_id,),
            )
            row = cur.fetchone()
            return row["id"] if row else None


def rule_output_exists(report_id: str) -> bool:
    _ensure_schema()
    input_id = _get_crawl_input_id(report_id)
    if input_id is None:
        return False

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM rule_outputs WHERE input_id=%s",
                (input_id,),
            )
            return cur.fetchone() is not None


def save_rule_output(
    report_id: str,
    rules: Any,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    status: str = "generated",
    error_message: Optional[str] = None,
) -> int:
    _ensure_schema()
    input_id = _get_crawl_input_id(report_id)
    if input_id is None:
        raise RuntimeError(f"No crawl_inputs row found for report_id={report_id}")

    rules_json = _json_value(rules)
    existing_id = _get_rule_output_id(input_id)

    with get_connection() as conn:
        with conn.cursor() as cur:
            if existing_id:
                cur.execute(
                    """
UPDATE rule_outputs
SET rules=%s,
    input_tokens=%s,
    output_tokens=%s,
    status=%s,
    error_message=%s,
    updated_at=NOW()
WHERE input_id=%s
""",
                    (
                        rules_json,
                        input_tokens,
                        output_tokens,
                        status,
                        error_message,
                        input_id,
                    ),
                )
                return existing_id

            cur.execute(
                """
INSERT INTO rule_outputs (
    input_id,
    rules,
    input_tokens,
    output_tokens,
    status,
    error_message
) VALUES (%s,%s,%s,%s,%s,%s)
""",
                (
                    input_id,
                    rules_json,
                    input_tokens,
                    output_tokens,
                    status,
                    error_message,
                ),
            )
            return cur.lastrowid


def update_crawl_input_fields(
    report_id: str,
    url: str,
    domain: str,
    ticket_context: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Edit the user-supplied parts of a ticket in place.

    Deliberately narrower than save_crawl_input(), which rewrites every column
    and would blank out crawl results (screenshot, duration) that an edit must
    not touch.
    """
    _ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
UPDATE crawl_inputs
SET url=%s,
    domain=%s,
    ticket_context=%s,
    updated_at=NOW()
WHERE report_id=%s
""",
                (url, domain, _json_value(ticket_context), report_id),
            )
            return cur.rowcount


def get_rules_blob(report_id: str) -> Any:
    """Raw `rules` column for a report, JSON-decoded (None when absent)."""
    _ensure_schema()
    input_id = _get_crawl_input_id(report_id)
    if input_id is None:
        return None

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rules FROM rule_outputs WHERE input_id=%s",
                (input_id,),
            )
            row = cur.fetchone()

    if not row or row.get("rules") is None:
        return None

    stored = row["rules"]
    if isinstance(stored, (dict, list)):
        return stored
    try:
        return json.loads(stored)
    except Exception:
        return None


def save_rules_blob(report_id: str, rules: Any, decisions: Any = None) -> None:
    """
    Replace the stored rules (and optionally decisions) without disturbing
    token counts, validation output, or status.
    """
    _ensure_schema()
    input_id = _get_crawl_input_id(report_id)
    if input_id is None:
        raise RuntimeError(f"No crawl_inputs row found for report_id={report_id}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM rule_outputs WHERE input_id=%s",
                (input_id,),
            )
            exists = cur.fetchone()

            if decisions is None:
                if exists:
                    cur.execute(
                        "UPDATE rule_outputs SET rules=%s, updated_at=NOW() WHERE input_id=%s",
                        (_json_value(rules), input_id),
                    )
                else:
                    cur.execute(
                        "INSERT INTO rule_outputs (input_id, rules) VALUES (%s,%s)",
                        (input_id, _json_value(rules)),
                    )
                return

            if exists:
                cur.execute(
                    "UPDATE rule_outputs SET rules=%s, decisions=%s, updated_at=NOW() WHERE input_id=%s",
                    (_json_value(rules), _json_value(decisions), input_id),
                )
            else:
                cur.execute(
                    "INSERT INTO rule_outputs (input_id, rules, decisions) VALUES (%s,%s,%s)",
                    (input_id, _json_value(rules), _json_value(decisions)),
                )


def save_report_images(report_id: str, images: Dict[str, str]) -> None:
    """Record the Ceph URIs of a report's screenshots."""
    _ensure_schema()
    input_id = _get_crawl_input_id(report_id)
    if input_id is None or not images:
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM rule_outputs WHERE input_id=%s", (input_id,))
            if cur.fetchone():
                cur.execute(
                    "UPDATE rule_outputs SET images=%s, updated_at=NOW() WHERE input_id=%s",
                    (_json_value(images), input_id),
                )
            else:
                cur.execute(
                    "INSERT INTO rule_outputs (input_id, images) VALUES (%s,%s)",
                    (input_id, _json_value(images)),
                )


def get_rule_decisions(report_id: str) -> Dict[str, Any]:
    """Stored approve/reject decisions for a report, keyed by rule text."""
    _ensure_schema()
    input_id = _get_crawl_input_id(report_id)
    if input_id is None:
        return {}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT decisions FROM rule_outputs WHERE input_id=%s",
                (input_id,),
            )
            row = cur.fetchone()

    if not row or not row.get("decisions"):
        return {}

    stored = row["decisions"]
    if isinstance(stored, dict):
        return stored
    try:
        parsed = json.loads(stored)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_rule_decision(
    report_id: str,
    rule: str,
    decision: Optional[str],
    decided_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Record (or clear, when decision is None) one reviewer decision.

    The whole read-modify-write happens inside one transaction holding a row
    lock. The previous version read the map on one connection and wrote it back
    on another, so two clicks a few hundred milliseconds apart could both read
    the same starting value and the slower write would silently erase the
    faster one — the UI would show a decision the database never kept.
    """
    _ensure_schema()
    input_id = _get_crawl_input_id(report_id)
    if input_id is None:
        raise RuntimeError(f"No crawl_inputs row found for report_id={report_id}")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            conn.begin()

            cur.execute(
                "SELECT id, decisions FROM rule_outputs WHERE input_id=%s FOR UPDATE",
                (input_id,),
            )
            row = cur.fetchone()

            stored = row.get("decisions") if row else None
            if isinstance(stored, dict):
                decisions = dict(stored)
            elif stored:
                try:
                    parsed = json.loads(stored)
                    decisions = dict(parsed) if isinstance(parsed, dict) else {}
                except Exception:
                    decisions = {}
            else:
                decisions = {}

            if decision is None:
                decisions.pop(rule, None)
            else:
                decisions[rule] = {
                    "decision": decision,
                    "by": decided_by,
                    "at": datetime.now(timezone.utc).isoformat(),
                }

            if row:
                cur.execute(
                    "UPDATE rule_outputs SET decisions=%s, updated_at=NOW() WHERE input_id=%s",
                    (_json_value(decisions), input_id),
                )
            else:
                cur.execute(
                    "INSERT INTO rule_outputs (input_id, decisions) VALUES (%s,%s)",
                    (input_id, _json_value(decisions)),
                )

            conn.commit()
            return decisions
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_rule_validation(
    report_id: str,
    validation_result: Any,
    after_screenshot: Optional[str] = None,
    status: str = "validated",
) -> int:
    _ensure_schema()
    input_id = _get_crawl_input_id(report_id)
    if input_id is None:
        raise RuntimeError(f"No crawl_inputs row found for report_id={report_id}")

    validation_json = _json_value(validation_result)
    existing_id = _get_rule_output_id(input_id)

    with get_connection() as conn:
        with conn.cursor() as cur:
            if existing_id:
                cur.execute(
                    """
UPDATE rule_outputs
SET validation_result=%s,
    after_screenshot=%s,
    status=%s,
    updated_at=NOW()
WHERE input_id=%s
""",
                    (
                        validation_json,
                        after_screenshot,
                        status,
                        input_id,
                    ),
                )
                return existing_id

            cur.execute(
                """
INSERT INTO rule_outputs (
    input_id,
    validation_result,
    after_screenshot,
    status
) VALUES (%s,%s,%s,%s)
""",
                (
                    input_id,
                    validation_json,
                    after_screenshot,
                    status,
                ),
            )
            return cur.lastrowid

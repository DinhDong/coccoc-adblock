# AdBlock Rule Engine Generator

AI-assisted pipeline that crawls reported domestic websites, extracts ad signals, generates candidate ABP filter rules via LLM, pre-tests them automatically, and queues them for moderator review in the CMS.

---

## Running everything in Docker

Compose runs the whole system — database, API, worker, and UI. Nothing needs to
be installed on the host except Docker itself.

### Prerequisites

- Docker Desktop (Compose v2)
- An OpenAI API key

### Setup

```bash
git clone <repo-url> && cd coccoc-adblock
cp .env.example .env.local
```

Edit `.env.local`:

- set `OPENAI_API_KEY`
- set `MYSQL_HOST=db` and `MYSQL_PORT=3306` (see Configuration below — this is
  the one setting people get wrong)

Then:

```bash
docker compose up --build -d
docker compose ps
```

### What is running

| service | port | what it is |
|---|---|---|
| `frontend` | 5173 | the moderation UI (Vite dev server, hot reload) |
| `backend` | 5000 | Flask API — tickets, rules, playground, jobs |
| `api` | 8000 | FastAPI image-upload service (Ceph) |
| `worker` | — | polling daemon; claims `status='new'` rows |
| `db` | 6446 | MySQL 8 (container port 3306) |

Open **http://localhost:5173**. The UI talks to the backend on port 5000, which
compose publishes, so it works from your browser without extra configuration.

`backend` and `frontend` bind-mount their source directories, so edits on the
host reload inside the container. Everything under `backend/data/` (screenshots,
HTML, crawl JSON, the rule registry) is mounted too and survives `down`.

### Configuration (`.env.local`)

Copy `.env.example` and fill it in. Only the first two groups are required.

| variable | required | notes |
|---|---|---|
| `OPENAI_API_KEY` | yes | rule generation fails without it |
| `OPENAI_DEFAULT_MODEL` / `OPENAI_FALLBACK_MODEL` | no | defaults `gpt-5.4-mini` / `gpt-5.5` |
| `MYSQL_HOST` / `PORT` / `DATABASE` / `USER` / `PASSWORD` | yes | see the note below |
| `S3_ENABLED`, `AWS_ENDPOINT`, `AWS_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | no | Ceph screenshot upload; off when `S3_ENABLED=false` |
| `PRESIGNED_URL_EXPIRES_SECONDS` | no | default 900; the UI caches image URLs for less than this |
| `PIPELINE_MAX_WORKERS` | no | concurrent in-process runs, default 2 — each drives a real browser |
| `CRAWL_PROXY_SERVER` / `_USERNAME` / `_PASSWORD` / `_BYPASS` | no | routes only the crawler's browser, e.g. `socks5://127.0.0.1:1080` |

**Which MySQL host?** `.env.local` is shared by the containers and by anything
you run on the host, and python-dotenv takes the *last* definition of a key —
so keep exactly one MySQL block uncommented.

- **All in Docker (the normal case):** `MYSQL_HOST=db`, `MYSQL_PORT=3306`.
  `db` is the compose service name, resolved on the compose network.
- **Backend on the host, database in Docker:** `MYSQL_HOST=127.0.0.1`,
  `MYSQL_PORT=6446` — compose publishes 6446 on the host, mapped to 3306 inside.
  Use `127.0.0.1`, not `localhost`: on Windows `localhost` resolves to `::1`
  first and MySQL is only listening on IPv4.
- **Shared team server:** whatever host and port that server uses.

The schema creates and migrates itself on first request, so a fresh database
needs no manual SQL.

### Running the worker service

Compose starts it automatically. It takes **no arguments** — the database is its
only source of work:

```bash
python -m app.services.worker
```

It polls `crawl_inputs` for `status = 'new'`, claims the oldest row with
`FOR UPDATE SKIP LOCKED` (so several worker containers are safe to run at once),
marks it `processing`, runs crawl -> generate -> validate, writes `rule_outputs`,
and sets the input to `completed` or `failed`. When there is nothing to do it
sleeps and polls again until it receives a stop signal.

**It does not stop on failures.** A dropped MySQL connection, a malformed row or
a crashing pipeline is logged against that one record and the worker moves on to
the next. Only a stop signal (SIGINT/SIGTERM) ends the loop.

Configured entirely by environment variables:

| variable | default | purpose |
|---|---|---|
| `WORKER_SLEEP_SECONDS` | `5` | idle wait between polls |
| `WORKER_LOG_LEVEL` | `INFO` | logging verbosity |
| `WORKER_SKIP_VALIDATION` | unset | set truthy to skip the sandbox stage |
| `WORKER_SKIP_EXTERNAL` | unset | set truthy to skip public filter-list checks |
| `WORKER_HEADFUL` | unset | set truthy to watch the browser |

```bash
# Follow it
docker compose logs -f worker

# Run one outside compose (uses your .env.local)
cd backend && python -m app.services.worker

# Queue work for it
UPDATE crawl_inputs SET status = 'new' WHERE report_id = 'RPT-...';
```

A record left `processing` means the worker died mid-run; reset it to `new` to
have it picked up again. Failed records stay `failed` until an admin sets them
back to `new`.

This is the only runner. The Run button in the UI does not execute anything —
it marks the ticket `new` and this worker picks it up (see "How a run happens"
below).

### Running the pipeline manually

```bash
# Crawl a website AND generate rules for it in one command
docker compose run --rm backend python -m app.services.workflow <report_id> --url <url> --env desktop

# Run the full workflow for an already-crawled report
docker compose run --rm backend python -m app.services.workflow <report_id>

# Run the crawler only
docker compose run --rm backend python -m app.services.crawler <url> <report_id> --env desktop

# Run the crawler with focus on a specific page region
docker compose run --rm backend python -m app.services.crawler <url> <report_id> --focus "header"
docker compose run --rm backend python -m app.services.crawler <url> <report_id> --focus "right sidebar"
docker compose run --rm backend python -m app.services.crawler <url> <report_id> --focus "top banner area"

# Run the crawler with ticket context (scopes rules to specific problems)
docker compose run --rm backend python -m app.services.crawler <url> <report_id> --env desktop --ticket-context-file <ticket_file>
# Then run the workflow for that crawl
docker compose run --rm backend python -m app.services.workflow <report_id>

# Preview a single rule — saves before.png (targets highlighted) + after.png (rule applied)
docker compose run --rm backend python -m app.validator.rule_preview <url> "<rule>"

# Preview multiple rules from a file, android viewport
docker compose run --rm backend python -m app.validator.rule_preview <url> --rules-file rules.txt --env android

# Custom output dir
docker compose run --rm backend python -m app.validator.rule_preview <url> "<rule>" --out data/previews/motp

# Run tests
docker compose run --rm backend pytest app/tests/ -v
```

### Useful commands

```bash
# Follow a service
docker compose logs -f backend
docker compose logs -f worker

# Run the test suite (pytest ships in the image; it is not in the local venv)
docker compose run --rm backend pytest app/tests/ -v

# Just the worker tests
docker compose run --rm backend python -m unittest app.tests.test_worker -v

# A shell inside the backend container
docker compose exec backend bash

# MySQL client
docker compose exec db mysql -u adblock -p adblock

# Queue a job for the polling worker (it only claims status='new')
docker compose exec db mysql -u adblock -p adblock -e   "UPDATE crawl_inputs SET status='new' WHERE report_id='RPT-...';"

# Restart one service after changing its code
docker compose restart backend

# Rebuild after changing requirements.txt or a Dockerfile
docker compose up --build -d backend

# Stop (data preserved) / stop and wipe the database
docker compose down
docker compose down -v
```

> Crawl outputs (screenshots, HTML, JSON) live in `backend/data/`, bind-mounted
> from the host, so they persist across restarts and rebuilds.

### Running without Docker

Only needed if you are debugging something container-specific. You will need
Python 3.11+, Node 20+, and Playwright's browsers installed locally:

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium webkit
python -m flask --app app --debug run --host 0.0.0.0 --port 5000

cd ../frontend && npm install && npm run dev
```

Bind the backend to `0.0.0.0`, not the Flask default. It otherwise listens on
IPv4 only while browsers resolve `localhost` to `::1` first, which makes
requests fail intermittently.

## Project structure

```
coccoc-adblock/
│
├── readme.md
├── .env.example                        # copy to .env.local and fill in
├── docker-compose.yml                  # db + backend + api + frontend + worker
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   │
│   └── app/
│       ├── __init__.py                 # Flask app + all /api routes
│       ├── api.py                      # FastAPI image-upload service (Ceph)
│       ├── database.py                 # PyMySQL connection, schema migrations
│       ├── tickets.py                  # Ticket/rule queries + UI shaping
│       │
│       ├── services/
│       │   ├── workflow.py             # run_pipeline: crawl -> generate -> validate
│       │   ├── worker.py               # standalone polling daemon (file or db source)
│       │   ├── crawler.py              # crawl stage entry point
│       │   ├── rule_generator.py       # prompt build -> LLM -> parsed rules
│       │   ├── rule_validator.py       # syntax + scope + policy + sandbox
│       │   ├── rule_registry.py        # per-domain dedup ledger + rule merging
│       │   ├── ticket_context.py       # normalises ticket fields for prompts
│       │   ├── problem_policy.py       # problem type -> allowed rule direction
│       │   └── external_filter_lists.py# skip rules public lists already cover
│       │
│       ├── crawler/                    # Playwright automation + signal extraction
│       │   ├── browser.py              # render, stealth, proxy, network capture
│       │   ├── extractor.py            # DOM parsing, ad-signal extraction
│       │   ├── detector.py             # ad candidate detection
│       │   ├── region_focus.py         # "focus on the right sidebar" support
│       │   └── storage.py              # HTML, screenshots, result JSON
│       │
│       ├── ai/
│       │   ├── prompt_builder.py
│       │   ├── llm_client.py           # OpenAI wrapper, retries, fallback model
│       │   └── rule_parser.py
│       │
│       ├── storage/
│       │   ├── s3_storage.py           # Ceph/S3 client
│       │   └── report_images.py        # the 3 screenshots per report
│       │
│       ├── validator/                  # see "AI rule tester" below
│       │   ├── abp_syntax.py
│       │   ├── rule_scope.py
│       │   ├── sandbox_check.py
│       │   ├── rule_preview.py
│       │   └── preview_render.py
│       │
│       ├── tests/
│       │   └── tickets/                # sample tickets (legacy file mode)
│       │
│       └── data/                       # bind-mounted, survives the container
│           ├── crawl_outputs/          # html, screenshots, results
│           ├── rule_outputs/           # rules, validation, rule_registry.json
│           └── service_worker/         # processed_tickets.json ledger
│
└── frontend/
    ├── Dockerfile                      # multi-stage; "dev" target used by compose
    ├── package.json
    └── src/
        ├── App.jsx                     # state, API calls, run/poll orchestration
        ├── constants.js                # states, envs, view titles
        │
        ├── pages/
        │   ├── Reports.jsx             # the moderation queue
        │   ├── LiveRules.jsx           # approved + deployed rules, grouped by site
        │   ├── RuleLibrary.jsx         # every rule, filter/edit/merge/delete
        │   ├── Playground.jsx          # try rules on any URL, no report needed
        │   ├── TokenUsage.jsx          # LLM spend per model and per report
        │   └── Performance.jsx         # stage timings, rule outcomes, per-site
        │
        ├── components/
        │   ├── Layout.jsx              # sidebar + topbar
        │   ├── ReportDetail.jsx        # report modal, rules table, screenshots
        │   ├── ReportTable.jsx
        │   ├── NewReportModal.jsx      # doubles as the edit form
        │   ├── DuplicateTargetModal.jsx# "this link has been run before"
        │   ├── StatusBadge.jsx
        │   └── Avatar.jsx
        │
        └── styles/global.css
```

---

## How a run happens

There is one runner: `app/services/worker.py`, its own container. The API never
executes a pipeline itself — it only changes a status.

```
UI "Send to pipeline"  ->  POST /api/tickets/<id>/run   (returns 202 instantly)
                             sets status = "new"
worker polls           ->  claims the oldest "new" row (FOR UPDATE SKIP LOCKED)
                             status = "processing"
                             crawl -> generate -> validate
                             status = "completed" or "failed"
UI polls /api/tickets  ->  shows the report ready for review
```

Because queueing is just a status change, a run survives the API restarting,
and several worker containers can run side by side without two of them taking
the same row.

**Drafts are never claimed.** The worker only ever takes `new`. A draft is
someone still writing the ticket, and crawling it would spend a real page load
and an LLM call on a URL they had not finished choosing.

**Stranded runs.** If the worker dies mid-run its row stays `processing`. The
worker requeues those at startup: it is the only thing that runs pipelines, so
when it boots nothing can legitimately be in progress.

Status vocabulary:

| status | who sets it | shown in the UI as |
|---|---|---|
| `draft`, `submitted` | ticket created | Draft |
| `new` | pressing Run | Queued (with its position in the queue) |
| `processing` | worker claims it | Running |
| `crawling`, `generating`, `validating` | the pipeline, as it goes | Running, named by stage |
| `completed`, `review`, `no_rules` | run finished | Awaiting review |
| `failed`, `crawl_failed` | run failed | Run failed |
| `done` | review finished | Done |

`new` and `processing` are deliberately distinct in the UI. Only one report is
ever *running* — the worker claims a single row at a time — so everything else
that has been submitted shows as **Queued** with its place in line ("Queued ·
2 of 3"). Positions are computed per request in `fetch_all_tickets()`, ordered
by creation time, and exclude whatever is already running. When several
moderators submit at once, each of them can see whose run is in flight and how
many are ahead of their own.

## HTTP API (Flask, port 5000)

| method | path | purpose |
|---|---|---|
| GET | `/api/tickets` | all tickets with rules, metrics, duplicates |
| POST | `/api/tickets` | create a ticket |
| PATCH | `/api/tickets/<id>` | edit fields, or change status |
| DELETE | `/api/tickets/<id>` | delete a report and its rules/images |
| POST | `/api/tickets/<id>/run` | queue the report for the worker, returns `202` |
| GET | `/api/tickets/duplicates?url=` | other reports on the same link |
| GET | `/api/tickets/<id>/images` | presigned URLs for the 3 screenshots |
| POST | `/api/tickets/<id>/decisions` | approve/reject one rule |
| POST/PATCH/DELETE | `/api/tickets/<id>/rules` | add, edit, delete a rule |
| GET | `/api/rules` | flat rule library across all reports |
| POST | `/api/rules/merge` | fold two rules into one |
| POST | `/api/rules/bulk-delete` | delete many rules |
| POST | `/api/rules/test` | sandbox rules from existing reports |
| POST | `/api/playground/test` | sandbox rules against any URL |
| GET | `/api/usage` | token spend, per model and per report |

The FastAPI image service runs separately on port 8000 (`app/api.py`).

---

## Full pipeline

```
Reported URL (from CMS or API)
   ↓
workflow.py          — coordinates all stages, updates report status
   ↓
classifier.py        — domestic/foreign check; non-domestic pages are rejected
   ↓
crawler.py (service)
   ├── browser.py    — Playwright render, stealth patches, network capture
   ├── extractor.py  — BeautifulSoup DOM parse, ad-signal extraction
   ├── detector.py   — Ad candidate detection, draft rules
   └── storage.py    — Save HTML / screenshot / result JSON
   ↓
rule_generator.py
   ├── prompt_builder.py  — Assembles compact crawl signals + prompt template
   ├── llm_client.py      — Sends to OpenAI, handles retries / fallback model
   └── rule_parser.py     — Parses LLM response into structured rule objects
   ↓
rule_validator.py    — Runs all three validator checks (see below)
   ↓
CMS moderator queue  — Human reviews, edits, approves or rejects
   ↓
Rule deployed to CMS DB
```

## AI rule tester (validator/)

Runs automatically after `rule_generator.py` produces candidates, before the rules reach the moderator queue. Three sequential checks — a rule must pass all three to proceed.

### Stage 1 — ABP syntax check (`abp_syntax.py`)

Validates that each generated rule string is syntactically correct ABP filter syntax before any browser work is done.

Checks:
- Network rules: valid URL pattern, no illegal characters, valid options (`$script`, `$image`, `$third-party`, etc.)
- Cosmetic rules: `##` / `#@#` format, valid CSS selector after the separator
- Exception rules: `@@` prefix with valid pattern
- Domain scope: `domain=` option lists are well-formed
- No empty or whitespace-only rules

Output per rule: `{ "valid": true/false, "error": "..." }`

### Stage 2 — Rule scope check (`rule_scope.py`)

Checks whether a syntactically valid rule is too broad, too narrow, or likely to cause false positives — without loading a browser.

Checks:
- **Overly broad network rules**: pattern matches too many unrelated URLs (e.g. `||com^` or single-character wildcards)
- **Missing domain scope**: cosmetic rules with no `domain=` restriction applied to generic selectors (e.g. `##div` alone)
- **Common element risk**: selector targets tags that are heavily reused in normal page layout (`div`, `span`, `a`, `img` with no further qualifiers)
- **Exception rule conflicts**: `@@` rule that would whitelist a domain already in a block rule

Output per rule: `{ "safe": true/false, "risk": "overly_broad" | "missing_scope" | "common_element" | null }`

### Stage 3 — Sandbox check (`sandbox_check.py`)

Loads the original URL in a Playwright browser with the candidate rules applied as a content blocker, then compares against a baseline (no rules) to verify the rules work without breaking the page.

Steps:
1. Load page **without** rules → capture baseline screenshot + collect visible element count
2. Load page **with** rules injected via `page.route()` (network rules) and `page.add_style_tag()` (cosmetic rules)
3. Compare:
   - Were the targeted ad elements removed? (selector no longer present in DOM)
   - Did third-party ad network requests get blocked? (route handler fired)
   - Did critical page elements survive? (navigation, main content, interactive controls)
   - Screenshot diff to catch large unintended layout changes

Output per rule set: `{ "ads_blocked": true/false, "page_functional": true/false, "layout_diff_pct": 0.04, "blocked_requests": [...], "broken_selectors": [...] }`

### Validator service interface (`rule_validator.py`)

```python
validate_rules(rules: list[str], page_url: str) -> ValidationReport
```

Returns a `ValidationReport` with per-rule results from all three stages and an overall `passed: bool`. Only rules that pass all three stages are forwarded to the moderator queue. Failed rules are logged with their failure reason for audit trail.

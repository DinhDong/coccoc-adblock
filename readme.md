# AdBlock Rule Engine Generator

AI-assisted pipeline that crawls reported domestic websites, extracts ad signals, generates candidate ABP filter rules via LLM, pre-tests them automatically, and queues them for moderator review in the CMS.

---

## Docker quickstart

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows / macOS) or Docker Engine + Docker Compose (Linux)

### Setup

```bash
# 1. Clone the repo
git clone <repo-url> && cd coccoc-adblock

# 2. Create your environment file from the template
cp .env.example .env.local
# Edit .env.local — set your OPENAI_API_KEY and change the MySQL passwords

# 3. Build and start all services (MySQL + backend)
docker compose up --build -d

# 4. Verify MySQL is healthy
docker compose ps   # db should show "healthy"
```

### Running the pipeline

```bash
# Run the full workflow for a report
docker compose run --rm backend app.services.workflow vnexpress-desktop

# Run the crawler only
docker compose run --rm backend app.services.crawler https://vnexpress.net vnexpress-desktop --env desktop

# Run tests
docker compose run --rm backend pytest app/tests/ -v
```

### Useful commands

```bash
# View logs
docker compose logs -f backend

# Connect to MySQL
docker compose exec db mysql -u adblock -p adblock

# Stop services (data preserved)
docker compose down

# Stop and delete MySQL data
docker compose down -v

# Rebuild after code changes
docker compose build backend
```

> **Note:** Crawl outputs (screenshots, HTML, JSON results) are stored in `backend/data/` which is mounted as a volume — files persist on your host machine across container restarts.

---

## Project structure

```
adblock-rule-engine-generator/
│
├── README.md
├── .env.example
├── docker-compose.yml
│
├── backend/
│   ├── requirements.txt
│   ├── main.py                         # FastAPI app entry point
│   │
│   └── app/
│       ├── config.py                   # Env/settings loader
│       ├── database.py                 # DB connection + session
│       │
│       ├── routes/                     # FastAPI route handlers
│       │   ├── reports.py              # Submit / list / get reports
│       │   ├── crawler.py              # Trigger crawl for a report
│       │   ├── rules.py                # Review, approve, reject rules
│       │   └── prompts.py              # CRUD for AI prompt templates
│       │
│       ├── models/                     # SQLAlchemy / Pydantic models
│       │   ├── report.py
│       │   ├── crawl_result.py
│       │   ├── rule.py
│       │   └── prompt.py
│       │
│       ├── services/                   # Orchestration layer
│       │   ├── workflow.py             # End-to-end pipeline coordinator (run_pipeline)
│       │   ├── crawler.py              # Crawl pipeline (browser → extract → detect → store)
│       │   ├── classifier.py           # Domestic/foreign domain classification
│       │   ├── rule_generator.py       # Calls AI, parses output, stores candidates
│       │   ├── rule_validator.py       # Runs validator/ checks on generated rules
│       │   └── log.py                  # Structured pipeline event logging
│       │
│       ├── crawler/                    # Browser automation + signal extraction
│       │   ├── browser.py              # Playwright page render, stealth, network capture
│       │   ├── extractor.py            # BeautifulSoup DOM parsing, ad-signal extraction
│       │   ├── detector.py             # Ad candidate detection + suggested rule drafts
│       │   └── storage.py              # Save HTML, screenshots, result JSON
│       │
│       ├── ai/                         # LLM rule generation
│       │   ├── prompt_builder.py       # Builds prompt from crawl signals + template
│       │   ├── llm_client.py           # OpenAI API wrapper (GPT-5.4-mini / GPT-5.5)
│       │   └── rule_parser.py          # Parses + normalises raw LLM output into rules
│       │
│       ├── validator/                  # Automated rule pre-testing (see section below)
│       │   ├── abp_syntax.py
│       │   ├── rule_scope.py
│       │   └── sandbox_check.py
│       │
│       └── utils/
│           ├── domain.py               # TLD / domestic-domain helpers
│           ├── file.py                 # Path / file utilities
│           └── logger.py              # App-wide logging config
│
├── frontend/
│   ├── package.json
│   ├── index.html
│   │
│   └── src/
│       ├── main.jsx
│       ├── App.jsx
│       │
│       ├── api/
│       │   └── apiClient.js            # Axios/fetch wrapper for backend API
│       │
│       ├── pages/
│       │   ├── Dashboard.jsx           # Overview: stats, queue counts
│       │   ├── Reports.jsx             # Report list with filters + status
│       │   ├── ReportDetail.jsx        # Crawl evidence, screenshot, signals
│       │   ├── RuleReview.jsx          # Approve / reject / edit generated rules
│       │   └── PromptManagement.jsx    # Create / update AI prompt templates
│       │
│       ├── components/
│       │   ├── Layout.jsx
│       │   ├── ReportTable.jsx
│       │   ├── CrawlResultPanel.jsx    # Shows screenshot + extracted signals
│       │   ├── RuleSuggestionBox.jsx   # Displays AI-generated rule + test result
│       │   ├── RuleEditor.jsx          # Inline rule editing before approval
│       │   └── StatusBadge.jsx
│       │
│       └── styles/
│           └── global.css
│
├── data/
│   ├── sample_urls.json
│   ├── sample_rules.txt
│   │
│   └── crawl_outputs/
│       ├── html/
│       ├── screenshots/
│       └── results/
│
├── database/
│   ├── schema.sql
│   └── seed.sql
│
└── tests/
    ├── test_crawler.py
    ├── test_rule_generator.py
    ├── test_validator.py
    └── test_api.py
```

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

---

## Team responsibilities

### Crawler (current phase)

| Person | Task | Files |
|---|---|---|
| 1 | Browser control | `crawler/browser.py`, `services/crawler.py` |
| 2 | Data extraction | `crawler/extractor.py` |
| 3 | Ad signal detection | `crawler/detector.py` |
| 4 | Storage, testing, docs | `crawler/storage.py`, `tests/` |

### AI rule generator + tester (next phase)

| Person | Task | Files |
|---|---|---|
| 1 | LLM integration | `ai/llm_client.py`, `ai/prompt_builder.py` |
| 2 | Rule parsing + generator service | `ai/rule_parser.py`, `services/rule_generator.py` |
| 3 | Static validation (no browser) | `validator/abp_syntax.py`, `validator/rule_scope.py` |
| 4 | Sandbox test + validator service | `validator/sandbox_check.py`, `services/rule_validator.py` |

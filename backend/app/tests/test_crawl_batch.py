"""
Batch crawl test — 3 representative sites across user-selected environments.

Sites:
  light  — theguardian.com  (international news, few trackers)
  medium — vnexpress.net    (Vietnamese news, moderate ad load)
  heavy  — reddit.com       (social media, heavy tracking, bot-resistant)

Run from backend/:
    .venv\\Scripts\\activate

    # All sites, all environments (9 jobs):
    python -m app.tests.test_crawl_batch

    # Pick sites:
    python -m app.tests.test_crawl_batch --sites light medium

    # Pick environments:
    python -m app.tests.test_crawl_batch --env desktop ios

    # Mix:
    python -m app.tests.test_crawl_batch --sites heavy --env android ios

    # Preview without crawling:
    python -m app.tests.test_crawl_batch --dry-run

Output: data/crawl_outputs/results/<site>-<env>.json
  The environment field is also stored inside each JSON so the AI rule
  generator can identify it independently of the filename.
"""

import argparse
import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Site catalogue
# ---------------------------------------------------------------------------
SITES = {
    "light":  ("guardian",  "https://www.theguardian.com"),
    "medium": ("vnexpress", "https://vnexpress.net"),
    "heavy":  ("reddit",    "https://www.reddit.com"),
}

ALL_ENVS = ["desktop", "android", "ios"]


def separator(title: str) -> None:
    print(f"\n{'='*64}")
    print(f"  {title}")
    print(f"{'='*64}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch crawl — 3 sites × chosen environments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sites",
        nargs="+",
        default=list(SITES),
        choices=list(SITES),
        metavar="SITE",
        help=f"Sites to crawl (default: all). Choices: {', '.join(SITES)}",
    )
    parser.add_argument(
        "--env",
        nargs="+",
        default=ALL_ENVS,
        metavar="ENV",
        help=f"Environments to crawl (default: all). Choices: {', '.join(ALL_ENVS)}  or 'all'",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the job plan without crawling")
    parser.add_argument("--no-scroll", action="store_true", help="Skip page scrolling (faster, less lazy content)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=60000,
        metavar="MS",
        help="Page load timeout in ms (default: 60000)",
    )
    args = parser.parse_args()

    selected_envs = ALL_ENVS if "all" in args.env else args.env
    unknown_envs = [e for e in selected_envs if e not in ALL_ENVS]
    if unknown_envs:
        print(f"Unknown environment(s): {unknown_envs}. Valid: {ALL_ENVS}")
        sys.exit(1)

    # Build job list: always use {site}-{env} so filenames are uniform
    # The AI rule generator reads the 'environment' field inside the JSON;
    # the suffix here is just for human readability and to prevent overwriting
    # results when the same site is crawled in multiple environments.
    jobs: list[tuple[str, str, str]] = []  # (report_id, url, env)
    for label in args.sites:
        base_id, url = SITES[label]
        for env in selected_envs:
            jobs.append((f"{base_id}-{env}", url, env))

    # Print plan
    separator(f"Crawl plan — {len(jobs)} job(s)")
    col = max(len(r) for r, _, _ in jobs)
    print(f"  {'Report ID':<{col}}  {'Env':<8}  URL")
    print(f"  {'-'*col}  {'-'*8}  {'-'*40}")
    for report_id, url, env in jobs:
        print(f"  {report_id:<{col}}  {env:<8}  {url}")

    if args.dry_run:
        print("\n  --dry-run: exiting without crawling.")
        return

    from app.services.crawler import CrawlService
    service = CrawlService()

    results: list[dict] = []
    total = len(jobs)

    for idx, (report_id, url, env) in enumerate(jobs, 1):
        separator(f"[{idx}/{total}]  {report_id}  ({env})  —  {url}")
        t0 = time.perf_counter()
        status = "error"
        try:
            result = service.crawl_url(
                url=url,
                report_id=report_id,
                headless=True,
                enable_scroll=not args.no_scroll,
                environment=env,
                timeout_ms=args.timeout,
                network_idle_timeout_ms=min(args.timeout // 4, 10000),
            )
            elapsed = time.perf_counter() - t0
            status = result.get("status", "unknown")
            print(f"  {status.upper()}  ({elapsed:.1f}s)  →  {report_id}")
            if status != "success":
                print(f"  Error: {result.get('error', '—')}")
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            print(f"  ERROR  ({elapsed:.1f}s)  →  {exc}")

        results.append({
            "report_id": report_id,
            "url": url,
            "environment": env,
            "status": status,
            "elapsed_s": round(elapsed, 1),
        })

    # Summary
    separator(f"Batch summary — {len(results)} jobs")
    passed = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] != "success"]

    col2 = max(len(r["report_id"]) for r in results)
    print(f"  {'Report ID':<{col2}}  {'Env':<8}  {'Status':<8}  {'Time':>6}")
    print(f"  {'-'*col2}  {'-'*8}  {'-'*8}  {'-'*6}")
    for r in results:
        flag = "OK" if r["status"] == "success" else "FAIL"
        print(f"  {r['report_id']:<{col2}}  {r['environment']:<8}  {flag:<8}  {r['elapsed_s']:>5.1f}s")

    print(f"\n  Total: {len(results)}   Passed: {len(passed)}   Failed: {len(failed)}")

    if failed:
        print("\n  Failed jobs:")
        for r in failed:
            print(f"    - {r['report_id']}  ({r['environment']})  {r['url']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

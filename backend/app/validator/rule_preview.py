"""
Per-rule visual preview: screenshot the page with each rule's target highlighted,
then a second screenshot with the rule applied, so reviewers can see exactly what
each rule targets before approving.

Usage (CLI):
    python -m app.validator.rule_preview <url> <rule> [--env desktop|android|ios] [--out DIR]
    python -m app.validator.rule_preview <url> --rules-file rules.txt [--env android]

Usage (programmatic):
    from app.validator.rule_preview import preview_rules, RulePreviewResult

    results = preview_rules(
        url="https://example.com",
        rules=["example.com##.ad-banner", "||ads.example.com^"],
        environment="desktop",
        out_dir="data/rule_previews/report123",
    )
    for r in results:
        print(r.rule, r.matched_elements, r.after_screenshot_path)
"""

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000
PAGE_SETTLE_DELAY_SECONDS = 2.5


# ---------------------------------------------------------------------------
# JS: highlight matched elements with absolute-positioned overlays so they
#     appear correctly in full-page screenshots.
# ---------------------------------------------------------------------------

_HIGHLIGHT_COSMETIC_SCRIPT = """
(selectors) => {
    const results = [];

    const label = (el, selector, index) => {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;

        const absTop  = rect.top  + window.pageYOffset;
        const absLeft = rect.left + window.pageXOffset;

        const overlay = document.createElement('div');
        overlay.setAttribute('data-adblock-preview', '1');
        overlay.style.cssText = [
            'position:absolute',
            `top:${absTop}px`,
            `left:${absLeft}px`,
            `width:${rect.width}px`,
            `height:${rect.height}px`,
            'background:rgba(220,30,30,0.18)',
            'border:3px solid #e00',
            'z-index:2147483646',
            'pointer-events:none',
            'box-sizing:border-box',
        ].join(';');

        const tag = document.createElement('span');
        tag.textContent = selector;
        tag.style.cssText = [
            'position:absolute',
            'top:0',
            'left:0',
            'background:#e00',
            'color:#fff',
            'font:bold 11px/1.4 monospace',
            'padding:1px 4px',
            'max-width:100%',
            'white-space:nowrap',
            'overflow:hidden',
            'z-index:2147483647',
        ].join(';');

        overlay.appendChild(tag);
        document.documentElement.appendChild(overlay);

        results.push({
            selector: selector,
            tag: el.tagName.toLowerCase(),
            id: el.id || '',
            rect: {
                top:    Math.round(absTop),
                left:   Math.round(absLeft),
                width:  Math.round(rect.width),
                height: Math.round(rect.height),
            },
        });
    };

    for (const selector of selectors) {
        try {
            const els = Array.from(document.querySelectorAll(selector));
            els.forEach((el, i) => label(el, selector, i));
        } catch (e) {
            // invalid selector — skip
        }
    }

    return results;
}
"""

# JS: highlight elements that loaded a resource from a given domain list
_HIGHLIGHT_NETWORK_SCRIPT = """
(domains) => {
    const results = [];

    const matchesDomain = (src) => {
        if (!src) return false;
        try {
            const host = new URL(src).hostname;
            return domains.some(d => host === d || host.endsWith('.' + d));
        } catch(_) { return false; }
    };

    const label = (el, src) => {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;

        const absTop  = rect.top  + window.pageYOffset;
        const absLeft = rect.left + window.pageXOffset;

        const overlay = document.createElement('div');
        overlay.setAttribute('data-adblock-preview', '1');
        overlay.style.cssText = [
            'position:absolute',
            `top:${absTop}px`,
            `left:${absLeft}px`,
            `width:${rect.width}px`,
            `height:${rect.height}px`,
            'background:rgba(255,140,0,0.20)',
            'border:3px solid #f80',
            'z-index:2147483646',
            'pointer-events:none',
            'box-sizing:border-box',
        ].join(';');

        const tag = document.createElement('span');
        try { tag.textContent = new URL(src).hostname; } catch(_) { tag.textContent = src.slice(0,40); }
        tag.style.cssText = [
            'position:absolute',
            'top:0',
            'left:0',
            'background:#f80',
            'color:#fff',
            'font:bold 11px/1.4 monospace',
            'padding:1px 4px',
            'max-width:100%',
            'white-space:nowrap',
            'overflow:hidden',
        ].join(';');

        overlay.appendChild(tag);
        document.documentElement.appendChild(overlay);

        results.push({
            src: src,
            tag: el.tagName.toLowerCase(),
            rect: {
                top:    Math.round(absTop),
                left:   Math.round(absLeft),
                width:  Math.round(rect.width),
                height: Math.round(rect.height),
            },
        });
    };

    for (const img of document.querySelectorAll('img[src]'))
        if (matchesDomain(img.src)) label(img, img.src);
    for (const f of document.querySelectorAll('iframe[src]'))
        if (matchesDomain(f.src)) label(f, f.src);
    for (const s of document.querySelectorAll('script[src]'))
        if (matchesDomain(s.src)) label(s, s.src);

    return results;
}
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RulePreviewResult:
    rule: str
    rule_type: str          # "cosmetic_hide" | "cosmetic_exception" | "network_block" | "network_exception" | "unknown"
    url: str
    environment: str

    before_screenshot: bytes = field(default_factory=bytes, repr=False)  # highlighted targets
    after_screenshot: bytes  = field(default_factory=bytes, repr=False)  # rule applied

    before_screenshot_path: str = ""
    after_screenshot_path: str  = ""

    matched_elements: int = 0
    targets: List[Dict[str, Any]] = field(default_factory=list)

    error: str = ""


# ---------------------------------------------------------------------------
# Rule type classification
# ---------------------------------------------------------------------------

_COSMETIC_RE = re.compile(r"^([^#]*)(##|#@#)(.+)$")
_NETWORK_EXCEPTION_RE = re.compile(r"^@@")


def _classify_rule(rule: str) -> str:
    if _COSMETIC_RE.match(rule):
        return "cosmetic_exception" if "#@#" in rule else "cosmetic_hide"
    if _NETWORK_EXCEPTION_RE.match(rule):
        return "network_exception"
    if rule.strip():
        return "network_block"
    return "unknown"


def _cosmetic_selector(rule: str) -> str:
    m = _COSMETIC_RE.match(rule.strip())
    return m.group(3).strip() if m else ""


def _network_domains(rule: str) -> List[str]:
    """Extract the domain(s) a network rule targets."""
    raw = rule.strip().lstrip("@")   # strip @@ exception prefix
    if raw.startswith("||"):
        raw = raw[2:]
    raw = raw.split("^")[0].split("$")[0].split("/")[0]
    return [raw] if raw else []


# ---------------------------------------------------------------------------
# Core preview function
# ---------------------------------------------------------------------------

def preview_rule(
    url: str,
    rule: str,
    environment: str = "desktop",
) -> RulePreviewResult:
    """
    Capture a 'before' screenshot (targets highlighted) and an 'after'
    screenshot (rule applied) for a single ABP rule.
    """
    result = RulePreviewResult(
        rule=rule,
        rule_type=_classify_rule(rule),
        url=url,
        environment=environment,
    )

    try:
        from ..crawler.browser import (
            ENVIRONMENTS,
            _STEALTH_LAUNCH_ARGS,
            _apply_stealth,
            _import_playwright,
        )
    except Exception as exc:
        result.error = f"browser helpers unavailable: {exc}"
        return result

    try:
        sync_playwright, _, PlaywrightTimeoutError = _import_playwright()
    except ImportError as exc:
        result.error = str(exc)
        return result

    profile = ENVIRONMENTS.get(environment, ENVIRONMENTS["desktop"])
    context_kwargs: Dict[str, Any] = {
        "user_agent":          profile["user_agent"],
        "viewport":            profile["viewport"],
        "device_scale_factor": profile["device_scale_factor"],
        "is_mobile":           profile["is_mobile"],
        "has_touch":           profile["has_touch"],
        "locale":              "en-US",
        "timezone_id":         "America/New_York",
        "extra_http_headers":  {"Accept-Language": "en-US,en;q=0.9"},
    }

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=_STEALTH_LAUNCH_ARGS,
            )

            try:
                ua = profile["user_agent"]

                # ---- Before: load page, inject highlights, screenshot ----
                before_ctx = browser.new_context(**context_kwargs)
                before_page = before_ctx.new_page()
                _apply_stealth(before_page, user_agent=ua)
                _load_page(before_page, url, PlaywrightTimeoutError)

                targets = _inject_highlights(before_page, rule, result.rule_type)
                result.targets = targets
                result.matched_elements = len(targets)

                result.before_screenshot = _screenshot(before_page, DEFAULT_TIMEOUT_MS)
                before_ctx.close()

                # ---- After: load page with rule applied, screenshot ----
                after_ctx = browser.new_context(**context_kwargs)
                after_page = after_ctx.new_page()
                _apply_stealth(after_page, user_agent=ua)
                setattr(after_page, "_adblock_document_url", url)

                _apply_network_rules(after_page, [rule])
                _load_page(after_page, url, PlaywrightTimeoutError)
                _apply_cosmetic_rules(after_page, [rule])
                time.sleep(PAGE_SETTLE_DELAY_SECONDS)

                result.after_screenshot = _screenshot(after_page, DEFAULT_TIMEOUT_MS)
                after_ctx.close()

            finally:
                browser.close()

    except Exception as exc:
        result.error = f"preview browser error: {exc}"
        logger.exception(result.error)

    return result


def preview_rules(
    url: str,
    rules: List[str],
    environment: str = "desktop",
    out_dir: Optional[str] = None,
) -> List[RulePreviewResult]:
    """
    Preview a list of rules.  If out_dir is given, writes PNG files there and
    populates before_screenshot_path / after_screenshot_path on each result.
    """
    results = []

    out_path = Path(out_dir) if out_dir else None
    if out_path:
        out_path.mkdir(parents=True, exist_ok=True)

    for idx, rule in enumerate(rules, start=1):
        logger.info("Preview rule %d/%d: %s", idx, len(rules), rule)
        result = preview_rule(url, rule, environment=environment)

        if out_path and not result.error:
            safe = re.sub(r"[^\w.-]", "_", rule)[:60]
            before_file = out_path / f"rule_{idx:02d}_before_{safe}.png"
            after_file  = out_path / f"rule_{idx:02d}_after_{safe}.png"

            if result.before_screenshot:
                before_file.write_bytes(result.before_screenshot)
                result.before_screenshot_path = str(before_file)

            if result.after_screenshot:
                after_file.write_bytes(result.after_screenshot)
                result.after_screenshot_path = str(after_file)

        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _inject_highlights(page, rule: str, rule_type: str) -> List[Dict[str, Any]]:
    """Inject visual overlays for the rule's targets. Returns matched elements."""
    try:
        if rule_type in ("cosmetic_hide", "cosmetic_exception"):
            selector = _cosmetic_selector(rule)
            if not selector:
                return []
            return page.evaluate(_HIGHLIGHT_COSMETIC_SCRIPT, [selector]) or []

        if rule_type in ("network_block", "network_exception"):
            domains = _network_domains(rule)
            if not domains:
                return []
            return page.evaluate(_HIGHLIGHT_NETWORK_SCRIPT, domains) or []

    except Exception as exc:
        logger.warning("Highlight injection failed: %s", exc)

    return []


def _screenshot(page, timeout_ms: int) -> bytes:
    try:
        return page.screenshot(full_page=True, timeout=timeout_ms)
    except Exception:
        try:
            return page.screenshot(full_page=False, timeout=timeout_ms)
        except Exception as exc:
            logger.warning("Screenshot failed: %s", exc)
            return b""


def _load_page(page, url: str, timeout_error_cls: Any) -> None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except timeout_error_cls:
        logger.debug("domcontentloaded timeout for %s", url)
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except timeout_error_cls:
        logger.debug("networkidle timeout for %s", url)
    time.sleep(PAGE_SETTLE_DELAY_SECONDS)


# Reuse network/cosmetic rule appliers from sandbox_check to stay consistent
def _apply_network_rules(page, rules: List[str]) -> None:
    from .sandbox_check import _apply_network_rules as _sandbox_apply_network
    _sandbox_apply_network(page, rules)


def _apply_cosmetic_rules(page, rules: List[str]) -> None:
    from .sandbox_check import _apply_cosmetic_rules as _sandbox_apply_cosmetic
    _sandbox_apply_cosmetic(page, rules)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Preview what each ABP rule targets — before/after screenshots with highlights.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Single rule:
    python -m app.validator.rule_preview https://motp.vn "motp.vn##.ad-banner"

  Multiple rules from a file (one rule per line):
    python -m app.validator.rule_preview https://motp.vn --rules-file rules.txt --env android

  Save to a specific directory:
    python -m app.validator.rule_preview https://motp.vn "motp.vn##.ad-top" --out data/previews/motp
""",
    )
    parser.add_argument("url", help="Page URL to preview rules against")
    parser.add_argument("rule", nargs="?", default="", help="A single ABP rule to preview")
    parser.add_argument("--rules-file", default="", metavar="FILE", help="File with one rule per line")
    parser.add_argument("--env", default="desktop", choices=["desktop", "android", "ios"], help="Browser environment (default: desktop)")
    parser.add_argument("--out", default="", metavar="DIR", help="Directory to save PNG screenshots (default: auto-generated under data/rule_previews/)")
    args = parser.parse_args()

    rules: List[str] = []
    if args.rule:
        rules.append(args.rule)
    if args.rules_file:
        with open(args.rules_file, encoding="utf-8") as f:
            rules.extend(line.strip() for line in f if line.strip() and not line.startswith("!"))

    if not rules:
        print("Error: provide a rule argument or --rules-file", file=sys.stderr)
        sys.exit(1)

    from urllib.parse import urlparse as _urlparse
    page_host = _urlparse(args.url).hostname or "page"
    out_dir = args.out or f"data/rule_previews/{page_host}"

    print(f"\nPreviewing {len(rules)} rule(s) against: {args.url}")
    print(f"Environment: {args.env}")
    print(f"Output dir:  {out_dir}\n")

    results = preview_rules(
        url=args.url,
        rules=rules,
        environment=args.env,
        out_dir=out_dir,
    )

    print(f"\n{'='*60}")
    print("  Rule Preview Results")
    print(f"{'='*60}")

    for r in results:
        print(f"\n  Rule:      {r.rule}")
        print(f"  Type:      {r.rule_type}")
        print(f"  Targets:   {r.matched_elements} element(s) highlighted")
        if r.targets:
            for t in r.targets[:3]:
                rect = t.get("rect", {})
                print(f"             [{t.get('tag','')}] {t.get('selector', t.get('src', ''))} "
                      f"@ ({rect.get('left')},{rect.get('top')}) {rect.get('width')}x{rect.get('height')}")
        if r.before_screenshot_path:
            print(f"  Before:    {r.before_screenshot_path}")
        if r.after_screenshot_path:
            print(f"  After:     {r.after_screenshot_path}")
        if r.error:
            print(f"  ERROR:     {r.error}")

    print(f"\n{'='*60}\n")

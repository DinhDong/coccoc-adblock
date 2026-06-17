"""
Downloads and caches well-known public ABP filter lists, then answers
whether a generated rule is already covered by any of them.

Why this matters: users who already have uBlock Origin / AdBlock Plus
installed are already applying EasyList, ABPvn, etc.  Generating the
same rule in CocCoc's built-in blocker creates a double-block that can
break page layout (e.g. a CDN domain blocked by both engines, causing
missing images or broken scripts).

Lists are cached locally and refreshed automatically every CACHE_MAX_AGE_DAYS.

Run from backend/ to inspect or refresh:
    python -m app.services.external_filter_lists           # show coverage stats
    python -m app.services.external_filter_lists --refresh # force re-download
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/external_lists")
CACHE_MAX_AGE_DAYS = 7

# ABP-format filter lists to check against. Key = short label shown in output.
# Ordered by relevance: Vietnamese-specific lists first, then globals.
FILTER_LISTS: Dict[str, str] = {
    "abpvn":       "https://raw.githubusercontent.com/abpvn/abpvn/master/filter/src/abpvn_vn.txt",
    "easylist":    "https://easylist.to/easylist/easylist.txt",
    "easyprivacy": "https://easylist.to/easylist/easyprivacy.txt",
    "ublock-vn":   "https://raw.githubusercontent.com/uBlockOrigin/uAssets/master/filters/filters.txt",
}

# Lines in an ABP filter file that are NOT rules
_SKIP_RE = re.compile(r"^\s*(!|#|\[|$)")
_COSMETIC_SPLIT_RE = re.compile(r"^([^#]*)(##|#@#)(.+)$")


# ---------------------------------------------------------------------------
# Rule normalization  (mirrors rule_registry.normalize_rule for consistency)
# ---------------------------------------------------------------------------

def _normalize(rule_text: str) -> str:
    text = rule_text.strip()
    if not text:
        return text

    cosmetic_match = _COSMETIC_SPLIT_RE.match(text)
    if cosmetic_match:
        domains, sep, selector = cosmetic_match.groups()
        return f"{domains.lower()}{sep}{selector.strip()}"

    pattern, _, options = text.partition("$")
    if options:
        opts = sorted(o.strip().lower() for o in options.split(",") if o.strip())
        return f"{pattern.lower()}${','.join(opts)}"
    return pattern.lower()


def _generic_cosmetic(rule_text: str) -> Optional[str]:
    """
    `vnexpress.net##.ads` → `##.ads`  (the global form present in many external lists)
    Returns None for non-cosmetic rules.
    """
    m = _COSMETIC_SPLIT_RE.match(rule_text.strip())
    if m:
        _, sep, selector = m.groups()
        return f"{sep}{selector.strip()}"
    return None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{name}_cache.json"


def _load_cache(name: str) -> Optional[Dict]:
    path = _cache_path(name)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(name: str, normalized_rules: list[str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {"downloaded_at": time.time(), "rule_count": len(normalized_rules), "rules": normalized_rules}
    with open(_cache_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    logger.info("Cached %d rules for %s → %s", len(normalized_rules), name, _cache_path(name))


def _is_stale(cache: Dict) -> bool:
    age_seconds = time.time() - cache.get("downloaded_at", 0)
    return age_seconds > CACHE_MAX_AGE_DAYS * 86_400


# ---------------------------------------------------------------------------
# Download + parse
# ---------------------------------------------------------------------------

def _download_list(name: str, url: str) -> list[str]:
    """Fetch an ABP filter list and return a list of normalized rule strings."""
    logger.info("Downloading filter list: %s (%s)", name, url)
    try:
        req = Request(url, headers={"User-Agent": "CocCocAdblock/1.0 rule-dedup-check"})
        with urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except URLError as exc:
        logger.warning("Failed to download %s: %s", name, exc)
        return []

    rules = []
    for line in text.splitlines():
        if _SKIP_RE.match(line):
            continue
        normalized = _normalize(line)
        if normalized:
            rules.append(normalized)

    logger.info("Parsed %d rules from %s", len(rules), name)
    return rules


def _get_rule_set(name: str, url: str, force_refresh: bool = False) -> Set[str]:
    """
    Return the normalized rule set for one list, downloading/refreshing as needed.
    """
    cache = _load_cache(name)
    if cache and not force_refresh and not _is_stale(cache):
        return set(cache["rules"])

    rules = _download_list(name, url)
    if rules:
        _save_cache(name, rules)
        return set(rules)

    # Download failed — fall back to stale cache rather than empty set
    if cache:
        logger.warning("Download failed for %s; using stale cache (%d rules)", name, len(cache["rules"]))
        return set(cache["rules"])

    return set()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_loaded_sets: Dict[str, Set[str]] = {}


def load_all(force_refresh: bool = False) -> None:
    """Pre-load all filter lists into memory. Safe to call multiple times."""
    global _loaded_sets
    for name, url in FILTER_LISTS.items():
        if name not in _loaded_sets or force_refresh:
            _loaded_sets[name] = _get_rule_set(name, url, force_refresh=force_refresh)


def is_covered(rule_text: str) -> Tuple[bool, str]:
    """
    Check whether a rule is already present in any loaded external filter list.

    Checks both the exact normalized form AND the generic cosmetic form
    (e.g. `vnexpress.net##.ads` → also checks `##.ads`).

    Returns:
        (True, "list-name")  if covered
        (False, "")          if not covered
    """
    if not _loaded_sets:
        load_all()

    normalized = _normalize(rule_text)
    generic = _generic_cosmetic(normalized)

    for name, rule_set in _loaded_sets.items():
        if normalized in rule_set:
            return True, name
        if generic and generic in rule_set:
            return True, f"{name} (generic)"

    return False, ""


def filter_uncovered(rules: list) -> Tuple[list, list]:
    """
    Split ParsedRule objects into (uncovered, externally_covered).

    Loads lists on first call; subsequent calls use the in-memory sets.
    """
    if not _loaded_sets:
        load_all()

    uncovered, covered = [], []
    for rule in rules:
        hit, source = is_covered(rule.rule)
        if hit:
            logger.debug("External duplicate [%s]: %s", source, rule.rule)
            covered.append((rule, source))
        else:
            uncovered.append(rule)

    return uncovered, covered


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Inspect / refresh external filter list cache.")
    parser.add_argument("--refresh", action="store_true", help="Force re-download all lists")
    parser.add_argument("--check", metavar="RULE", help="Check if a specific rule is covered")
    args = parser.parse_args()

    load_all(force_refresh=args.refresh)

    if args.check:
        hit, source = is_covered(args.check)
        if hit:
            print(f"COVERED by {source}: {args.check}")
        else:
            print(f"NOT covered: {args.check}")
        sys.exit(0)

    print(f"\nLoaded filter lists:")
    for name, rule_set in sorted(_loaded_sets.items()):
        cache = _load_cache(name)
        age_h = (time.time() - cache.get("downloaded_at", 0)) / 3600 if cache else 0
        print(f"  {name:<15} {len(rule_set):>7,} rules   (cached {age_h:.1f}h ago)")

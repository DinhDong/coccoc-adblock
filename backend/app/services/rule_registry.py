"""
Tracks which ABP rules have already been generated for a domain so the
pipeline doesn't ask the LLM to re-suggest (and re-validate) the same rule
across multiple crawls of the same site — e.g. vnexpress-desktop,
vnexpress-android and vnexpress-ios all surface the same `||admicro.vn^`
network rule, but only need to go through sandbox validation once.

Persisted as a flat JSON file keyed by domain, since there's no database
yet — swap this for a `rules` table lookup once database/schema.sql lands.
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path("data/rule_outputs/rule_registry.json")

_COSMETIC_SPLIT_RE = re.compile(r"^([^#]*)(##|#@#)(.+)$")


def get_domain(url: str) -> str:
    """Extract the registrable domain from a URL (host, without 'www.')."""
    host = urlparse(url).hostname or url
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_rule(rule_text: str) -> str:
    """
    Normalize an ABP rule string for duplicate comparison.

    - Network/exception rules: lowercase the pattern, sort $options alphabetically
      so `$third-party,script` and `$script,third-party` compare equal.
    - Cosmetic rules: lowercase the domain list before ## / #@#, leave the
      selector untouched (CSS class/ID names are case-sensitive).
    """
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


def _load_registry() -> Dict[str, List[str]]:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Rule registry unreadable, starting fresh: %s", REGISTRY_PATH)
        return {}


def _save_registry(registry: Dict[str, List[str]]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False, sort_keys=True)


def get_existing_rules(domain: str) -> Set[str]:
    """Normalized rule strings already on record for this domain."""
    return set(_load_registry().get(domain, []))


def register_rules(domain: str, normalized_rules: List[str]) -> None:
    """Add newly-generated normalized rules to the domain's registry entry."""
    if not normalized_rules:
        return
    registry = _load_registry()
    existing = set(registry.get(domain, []))
    existing.update(normalized_rules)
    registry[domain] = sorted(existing)
    _save_registry(registry)


def clear_rules(domain: str) -> int:
    """Remove all known rules for a domain. Returns count of removed rules."""
    registry = _load_registry()
    removed = len(registry.pop(domain, []))
    if removed:
        _save_registry(registry)
        logger.info("Cleared %d rule(s) from registry for %s", removed, domain)
    return removed


def filter_new_rules(url: str, rules: List) -> Tuple[List, List]:
    """
    Split candidate ParsedRule objects into (new, duplicate) based on what's
    already registered for this URL's domain, and on duplicates within the
    same LLM response.

    Does not mutate the registry — call register_rules() with the returned
    new-rule set once the caller decides to keep them.
    """
    domain = get_domain(url)
    existing = get_existing_rules(domain)

    new_rules, duplicate_rules = [], []
    seen_in_batch: Set[str] = set()

    for rule in rules:
        key = normalize_rule(rule.rule)
        if key in existing or key in seen_in_batch:
            duplicate_rules.append(rule)
        else:
            new_rules.append(rule)
            seen_in_batch.add(key)

    return new_rules, duplicate_rules


# ------------------------------------------------------------------
# CLI inspection helper
# Usage: python -m app.services.rule_registry [domain]
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    registry = _load_registry()
    if len(sys.argv) > 1:
        domain = sys.argv[1].lower()
        rules = registry.get(domain, [])
        print(f"{domain}: {len(rules)} known rule(s)")
        for r in rules:
            print(f"  {r}")
    else:
        if not registry:
            print("Rule registry is empty.")
        for domain, rules in sorted(registry.items()):
            print(f"{domain}: {len(rules)} known rule(s)")

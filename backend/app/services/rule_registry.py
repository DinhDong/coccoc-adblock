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
from typing import Dict, List, Optional, Set, Tuple
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


def _dedupe(values: List[str]) -> List[str]:
    """Order-preserving de-duplication."""
    seen, out = set(), []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _split_options(options: str) -> Tuple[List[str], List[str]]:
    """Split a network rule's $options into ($domain= entries, other options)."""
    domains: List[str] = []
    others: List[str] = []
    for opt in options.split(","):
        opt = opt.strip()
        if not opt:
            continue
        if opt.lower().startswith("domain="):
            domains.extend(d.strip() for d in opt[len("domain="):].split("|") if d.strip())
        else:
            others.append(opt)
    return domains, others


def merge_rule_texts(first: str, second: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Combine two ABP rules into one, or explain why they cannot combine.

    Returns (merged_text, None) on success, (None, reason) otherwise. Only
    shapes where the union is exactly equivalent to the two originals are
    merged — anything else would silently change what gets blocked:

      same domain, different selectors   a.com##div.x + a.com##div.y
                                         -> a.com##div.x, div.y
      same selector, different domains   a.com##div.x + b.com##div.x
                                         -> a.com,b.com##div.x
      same network pattern, diff options ||x.com^$image + ||x.com^$script
                                         -> ||x.com^$image,script
      same network pattern, diff domains ||x.com^$domain=a.com + $domain=b.com
                                         -> ||x.com^$domain=a.com|b.com
    """
    a, b = (first or "").strip(), (second or "").strip()
    if not a or not b:
        return None, "both rules must be non-empty"
    if a == b:
        return a, None

    cos_a, cos_b = _COSMETIC_SPLIT_RE.match(a), _COSMETIC_SPLIT_RE.match(b)

    if bool(cos_a) != bool(cos_b):
        return None, "cannot merge a cosmetic rule with a network rule"

    if cos_a and cos_b:
        dom_a, sep_a, sel_a = cos_a.groups()
        dom_b, sep_b, sel_b = cos_b.groups()

        if sep_a != sep_b:
            return None, "cannot merge a hiding rule with an exception rule"

        if dom_a.lower() == dom_b.lower():
            selectors = _dedupe(
                [s.strip() for s in sel_a.split(",")] + [s.strip() for s in sel_b.split(",")]
            )
            return f"{dom_a}{sep_a}{', '.join(selectors)}", None

        if sel_a.strip() == sel_b.strip():
            domains = _dedupe(
                [d.strip() for d in dom_a.split(",")] + [d.strip() for d in dom_b.split(",")]
            )
            return f"{','.join(domains)}{sep_a}{sel_a.strip()}", None

        return None, (
            "cosmetic rules can only merge when they share the domain "
            "(joining selectors) or share the selector (joining domains)"
        )

    pattern_a, _, options_a = a.partition("$")
    pattern_b, _, options_b = b.partition("$")

    if pattern_a.lower() != pattern_b.lower():
        return None, (
            "network rules can only merge when the pattern before '$' is "
            "identical; these block different addresses"
        )

    domains_a, others_a = _split_options(options_a)
    domains_b, others_b = _split_options(options_b)

    # An unrestricted rule already covers every domain, so folding a
    # domain-limited one into it would widen nothing but reads as if it did.
    if bool(domains_a) != bool(domains_b):
        return None, (
            "one rule is limited with $domain= and the other is not — merging "
            "would change which sites it applies to"
        )

    merged_options = _dedupe(others_a + others_b)
    merged_domains = _dedupe(domains_a + domains_b)
    if merged_domains:
        merged_options.append("domain=" + "|".join(merged_domains))

    if not merged_options:
        return pattern_a, None
    return f"{pattern_a}${','.join(merged_options)}", None


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


def unregister_rule(domain: str, rule_text: str) -> bool:
    """
    Drop a single rule from a domain's registry entry.

    Called when a moderator deletes or rewrites a rule: without this the
    registry would keep treating it as already-known and dedupe it out of
    every future run, so the rule could never be proposed again.
    """
    registry = _load_registry()
    existing = registry.get(domain)
    if not existing:
        return False

    key = normalize_rule(rule_text)
    remaining = [r for r in existing if r != key]
    if len(remaining) == len(existing):
        return False

    if remaining:
        registry[domain] = remaining
    else:
        registry.pop(domain, None)

    _save_registry(registry)
    logger.info("Unregistered rule for %s: %s", domain, key)
    return True


def unregister_rule_anywhere(rule_text: str) -> List[str]:
    """
    Remove a rule from every domain that lists it, returning those domains.

    Needed because a rule is registered under the domain of the report's URL
    at generation time — if that URL is later edited to a different site, the
    entry is stranded under the old domain and a domain-scoped removal misses
    it, leaving the rule permanently deduped out of future runs.
    """
    registry = _load_registry()
    key = normalize_rule(rule_text)

    touched: List[str] = []
    for domain, rules in list(registry.items()):
        if key not in rules:
            continue
        remaining = [r for r in rules if r != key]
        if remaining:
            registry[domain] = remaining
        else:
            registry.pop(domain, None)
        touched.append(domain)

    if touched:
        _save_registry(registry)
        logger.info("Unregistered rule %s from %s", key, ", ".join(touched))
    return touched


def clear_rules(domain: str) -> int:
    """Remove all known rules for a domain. Returns the count of removed rules."""
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

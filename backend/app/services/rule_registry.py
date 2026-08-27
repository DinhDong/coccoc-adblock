"""
Tracks which ABP rules have already been generated for a domain *on a given
environment*, so the pipeline doesn't ask the LLM to re-suggest (and
re-validate) a rule it has already cleared for that platform.

Scoping is per (domain, environment), not per domain. Keying by domain alone
meant whichever platform crawled a site first claimed every rule for it: an
iOS run of baomoi.com registered all 13 of its rules, and the Android run
minutes later found all 9 of the rules it generated already present, skipped
every one, and finished with nothing to review. Mobile-only selectors were
reachable by exactly one platform — whichever happened to run first. Each
environment now keeps its own set, so the same site can be worked on desktop,
Android and iOS independently.

Persisted as a JSON file of {domain: {environment: [rules]}}, since there's no
database yet — swap this for a `rules` table lookup once database/schema.sql
lands. Entries written before this change were flat {domain: [rules]} lists;
they are migrated on read (see _load_registry).
"""

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

REGISTRY_PATH = Path("data/rule_outputs/rule_registry.json")

_COSMETIC_SPLIT_RE = re.compile(r"^([^#]*)(##|#@#)(.+)$")

# Mirrors the environments the crawler profiles and worker.resolve_environment
# accept. Anything unrecognised falls back to desktop rather than creating a
# junk bucket that would silently dedupe against nothing.
KNOWN_ENVIRONMENTS = ("desktop", "android", "ios")
DEFAULT_ENVIRONMENT = "desktop"


def normalize_environment(value: Optional[str]) -> str:
    """Coerce an environment label to one of KNOWN_ENVIRONMENTS."""
    text = str(value or "").strip().lower()
    return text if text in KNOWN_ENVIRONMENTS else DEFAULT_ENVIRONMENT


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


# Every mutation below is a read-modify-write of one JSON file. Pipeline runs
# used to be serialised by the HTTP request that started them; now that they
# execute on a worker pool, two finishing at once would interleave and one
# domain's rules would be silently lost. RLock because some callers already
# hold it when they call another registry function.
_REGISTRY_LOCK = threading.RLock()


def _load_registry() -> Dict[str, Dict[str, List[str]]]:
    """
    Read the registry, upgrading any pre-environment entries as it goes.

    Legacy entries are flat {domain: [rules]} lists with no environment
    recorded. They are filed under desktop: every one of them predates the fix
    to worker.resolve_environment, which read only "platform" while the UI
    wrote "env" — so those runs all executed as desktop no matter which
    platform the ticket asked for. Desktop is where they actually came from.
    """
    if not REGISTRY_PATH.exists():
        return {}
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Rule registry unreadable, starting fresh: %s", REGISTRY_PATH)
        return {}

    if not isinstance(raw, dict):
        return {}

    registry: Dict[str, Dict[str, List[str]]] = {}
    for domain, entry in raw.items():
        if isinstance(entry, list):
            registry[domain] = {DEFAULT_ENVIRONMENT: [r for r in entry if isinstance(r, str)]}
        elif isinstance(entry, dict):
            buckets: Dict[str, List[str]] = {}
            for env, rules in entry.items():
                if not isinstance(rules, list):
                    continue
                key = normalize_environment(env)
                buckets.setdefault(key, [])
                buckets[key].extend(r for r in rules if isinstance(r, str))
            registry[domain] = {k: sorted(set(v)) for k, v in buckets.items() if v}
    return registry


def _save_registry(registry: Dict[str, Dict[str, List[str]]]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling file and replace, so a crash mid-write cannot leave
    # truncated JSON behind — the loader would silently treat that as empty
    # and every known rule would look new again.
    tmp_path = REGISTRY_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp_path, REGISTRY_PATH)


def get_existing_rules(domain: str, environment: Optional[str] = None) -> Set[str]:
    """
    Normalized rule strings already on record for this domain.

    With an environment, only that platform's rules — this is what dedup runs
    against, so an Android crawl is not blocked by what an iOS crawl already
    registered. Without one, the union across every platform, which is what
    callers auditing "does this site have any rules at all" want.
    """
    buckets = _load_registry().get(domain, {})
    if environment is None:
        return {rule for rules in buckets.values() for rule in rules}
    return set(buckets.get(normalize_environment(environment), []))


def register_rules(
    domain: str,
    normalized_rules: List[str],
    environment: Optional[str] = None,
) -> None:
    """Add newly-generated normalized rules to this domain+environment entry."""
    if not normalized_rules:
        return
    env = normalize_environment(environment)
    with _REGISTRY_LOCK:
        registry = _load_registry()
        buckets = registry.setdefault(domain, {})
        existing = set(buckets.get(env, []))
        existing.update(normalized_rules)
        buckets[env] = sorted(existing)
        _save_registry(registry)


def unregister_rule(
    domain: str,
    rule_text: str,
    environment: Optional[str] = None,
) -> bool:
    """
    Drop a single rule from a domain's registry entry.

    Called when a moderator deletes or rewrites a rule: without this the
    registry would keep treating it as already-known and dedupe it out of
    every future run, so the rule could never be proposed again.

    Environment defaults to every platform. A moderator rejecting a rule is
    judging the rule, not the platform it happened to be generated on, so
    leaving copies registered under the other environments would keep it
    deduped out of their runs and it could never be proposed again there.
    """
    key = normalize_rule(rule_text)
    with _REGISTRY_LOCK:
        registry = _load_registry()
        buckets = registry.get(domain)
        if not buckets:
            return False

        targets = (
            list(buckets.keys())
            if environment is None
            else [normalize_environment(environment)]
        )

        removed = False
        for env in targets:
            rules = buckets.get(env)
            if not rules or key not in rules:
                continue
            remaining = [r for r in rules if r != key]
            if remaining:
                buckets[env] = remaining
            else:
                buckets.pop(env, None)
            removed = True

        if not removed:
            return False

        if not buckets:
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
    key = normalize_rule(rule_text)
    touched: List[str] = []

    with _REGISTRY_LOCK:
        registry = _load_registry()
        for domain, buckets in list(registry.items()):
            hit = False
            for env, rules in list(buckets.items()):
                if key not in rules:
                    continue
                remaining = [r for r in rules if r != key]
                if remaining:
                    buckets[env] = remaining
                else:
                    buckets.pop(env, None)
                hit = True

            if not hit:
                continue
            if not buckets:
                registry.pop(domain, None)
            touched.append(domain)

        if touched:
            _save_registry(registry)

    if touched:
        logger.info("Unregistered rule %s from %s", key, ", ".join(touched))
    return touched


def clear_rules(domain: str, environment: Optional[str] = None) -> int:
    """
    Remove known rules for a domain. Returns the count of removed rules.

    Scoped to one environment when given, so a "regenerate from scratch" on
    Android does not throw away the desktop rules for the same site.
    """
    with _REGISTRY_LOCK:
        registry = _load_registry()
        if environment is None:
            removed = sum(len(v) for v in registry.pop(domain, {}).values())
        else:
            env = normalize_environment(environment)
            buckets = registry.get(domain, {})
            removed = len(buckets.pop(env, []))
            if not buckets:
                registry.pop(domain, None)
        if removed:
            _save_registry(registry)
    if removed:
        logger.info(
            "Cleared %d rule(s) from registry for %s (%s)",
            removed,
            domain,
            environment or "all environments",
        )
    return removed


def filter_new_rules(
    url: str,
    rules: List,
    environment: Optional[str] = None,
) -> Tuple[List, List, List]:
    """
    Classify candidate ParsedRule objects into (new, known, repeated).

    - new:      not seen before for this domain on this environment
    - known:    already registered for this domain+environment
    - repeated: the model proposed the same rule twice in one response

    The two kinds are returned separately because they are not the same
    problem. A rule already in the registry is a real candidate a moderator
    may still want to approve, so the caller keeps it and flags it. The same
    rule appearing twice in one batch is just noise and is dropped — showing a
    moderator the identical rule twice is never useful.

    Does not mutate the registry — call register_rules() once the caller
    decides what to keep.
    """
    domain = get_domain(url)
    existing = get_existing_rules(domain, environment)

    new_rules, known_rules, repeated_rules = [], [], []
    seen_in_batch: Set[str] = set()

    for rule in rules:
        key = normalize_rule(rule.rule)
        if key in seen_in_batch:
            repeated_rules.append(rule)
            continue
        seen_in_batch.add(key)

        if key in existing:
            known_rules.append(rule)
        else:
            new_rules.append(rule)

    return new_rules, known_rules, repeated_rules


# ------------------------------------------------------------------
# CLI inspection helper
# Usage: python -m app.services.rule_registry [domain]
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    registry = _load_registry()
    if len(sys.argv) > 1:
        domain = sys.argv[1].lower()
        buckets = registry.get(domain, {})
        total = sum(len(r) for r in buckets.values())
        print(f"{domain}: {total} known rule(s)")
        for env in sorted(buckets):
            print(f"  [{env}] {len(buckets[env])} rule(s)")
            for r in buckets[env]:
                print(f"    {r}")
    else:
        if not registry:
            print("Rule registry is empty.")
        for domain, buckets in sorted(registry.items()):
            per_env = ", ".join(
                f"{env}={len(buckets[env])}" for env in sorted(buckets)
            )
            total = sum(len(r) for r in buckets.values())
            print(f"{domain}: {total} known rule(s) ({per_env})")

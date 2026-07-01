"""
Scope the crawled HTML to a specific page region before extraction.

Usage:
    focused_html, selector, method = focus_html(html, "header")
    focused_html, selector, method = focus_html(html, "right sidebar")

Strategy (in order):
  1. Semantic keyword map  — no API cost, instant
  2. AI with DOM skeleton  — LLM reads compact structural outline, returns a selector
  3. Fallback              — return original HTML unchanged

The network capture (requests) is never scoped — only the HTML fed to the
extractor/detector is narrowed, so third-party request analysis is unaffected.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# Semantic keyword → CSS selector candidates (tried in order)
_SEMANTIC_MAP = [
    # (compiled pattern, css selector)
    (re.compile(r"\bheader\b", re.I),     "header, [role='banner'], #header, .header, #site-header, .site-header"),
    (re.compile(r"\bfooter\b", re.I),     "footer, [role='contentinfo'], #footer, .footer, #site-footer, .site-footer"),
    (re.compile(r"\bnav(igation)?\b", re.I), "nav, [role='navigation'], #nav, .nav, .navbar, #navbar"),
    (re.compile(r"\b(main|body|content)\b", re.I), "main, [role='main'], #main, #content, .main-content, .content-area, article"),
    (re.compile(r"\bsidebar\b", re.I),    "aside, [role='complementary'], #sidebar, .sidebar, .side-bar, .col-sidebar"),
    (re.compile(r"\barticle\b", re.I),    "article, .article, .post, .entry, #article"),
    (re.compile(r"\b(search|searchbar)\b", re.I), ".search, #search, [role='search'], form.search-form"),
    (re.compile(r"\b(banner|top.?banner|top.?ad)\b", re.I), ".banner, #banner, .top-banner, .top-ad, .ad-top, header .ad"),
]

# Tags that carry no structural meaning — skip entirely when building skeleton
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "svg", "path", "defs", "use",
    "meta", "link", "title", "br", "hr", "wbr",
})

# Tags that are always included in the skeleton even without id/class
_STRUCTURAL_TAGS = frozenset({
    "html", "body", "header", "footer", "main", "nav", "aside",
    "section", "article", "form", "table",
    "h1", "h2", "h3", "div", "ul", "ol",
})

# Positional qualifiers — "right sidebar", "second ad", "last banner"
# Direction word → tokens searched inside a candidate element's id/class
_DIRECTION_SYNONYMS = {
    "left":   ("left",),
    "right":  ("right",),
    "top":    ("top", "upper"),
    "bottom": ("bottom", "lower", "foot"),
    "upper":  ("upper", "top"),
    "lower":  ("lower", "bottom"),
}

# Ordinal word → index into the ordered list of semantic matches (-1 = last)
_ORDINAL_MAP = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
    "last": -1,
}

_DIRECTION_RE = re.compile(
    r"\b(" + "|".join(sorted(_DIRECTION_SYNONYMS, key=len, reverse=True)) + r")\b",
    re.I,
)
_ORDINAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_ORDINAL_MAP, key=len, reverse=True)) + r")\b",
    re.I,
)

# DOM skeleton builder
def _build_dom_skeleton(html: str, max_nodes: int = 200) -> str:
    """
    Return a compact indented structural outline of the page.

    Each line is:  [indent]tag[#id][.class1.class2]
    Text content and attributes other than id/class are omitted.
    """
    soup = BeautifulSoup(html, "html.parser")
    lines: list[str] = []
    count = 0

    def _walk(node: Tag, depth: int) -> None:
        nonlocal count
        if count >= max_nodes:
            return
        if not isinstance(node, Tag):
            return
        tag = node.name
        if not tag or tag in _SKIP_TAGS:
            return

        node_id = (node.get("id") or "").strip()
        node_classes: list[str] = node.get("class", [])
        if isinstance(node_classes, str):
            node_classes = node_classes.split()

        is_structural = tag in _STRUCTURAL_TAGS
        has_identity = bool(node_id or node_classes)

        if is_structural or has_identity:
            label = tag
            if node_id:
                label += f"#{node_id}"
            if node_classes:
                label += "." + ".".join(node_classes[:3])
            lines.append("  " * depth + label)
            count += 1
            child_depth = depth + 1
        else:
            child_depth = depth  # transparent node — don't increase indent

        for child in node.children:
            _walk(child, child_depth)  # type: ignore[arg-type]

    _walk(soup, 0)
    return "\n".join(lines)

# Semantic heuristic
def _semantic_selectors(focus_region: str) -> list[str]:
    """
    Return the ordered list of CSS candidate selectors for the first semantic
    keyword that matches focus_region, or [] if no keyword matches.
    """
    for pattern, css_selector in _SEMANTIC_MAP:
        if pattern.search(focus_region):
            return [c.strip() for c in css_selector.split(",") if c.strip()]
    return []


def _try_semantic(html: str, focus_region: str) -> Optional[str]:
    """
    Try to match focus_region against known keywords and return the first
    matching CSS selector that actually finds an element in the HTML.

    Returns None if no match.
    """
    soup = BeautifulSoup(html, "html.parser")

    for candidate in _semantic_selectors(focus_region):
        try:
            if soup.select_one(candidate):
                logger.debug("Semantic selector matched '%s' → %s", focus_region, candidate)
                return candidate
        except Exception:
            continue

    return None


def _parse_qualifier(focus_region: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Detect a directional ("right", "top") and/or ordinal ("second", "last")
    qualifier in focus_region.

    Returns (direction_word_or_None, ordinal_index_or_None).
    """
    direction_match = _DIRECTION_RE.search(focus_region)
    direction = direction_match.group(1).lower() if direction_match else None

    ordinal_match = _ORDINAL_RE.search(focus_region)
    ordinal = _ORDINAL_MAP[ordinal_match.group(1).lower()] if ordinal_match else None

    return direction, ordinal


def _identity_contains(node: Tag, tokens: Tuple[str, ...]) -> bool:
    """
    Return True if any token appears in the node's id or class attributes.
    """
    node_id = (node.get("id") or "")
    classes = node.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()

    haystack = (node_id + " " + " ".join(classes)).lower()
    return any(token in haystack for token in tokens)


def _resolve_positional_index(
    soup: BeautifulSoup,
    focus_region: str,
    direction: Optional[str],
    ordinal: Optional[int],
) -> Optional[Tuple[str, int]]:
    """
    Resolve a directional/ordinal focus region to a (selector, index) pair.

    The selector is the comma-joined semantic candidate list and the index is
    the position of the chosen element within that selector's matches in DOM
    order. This maps 1:1 onto a live ``query_selector_all(selector)[index]`` so
    the same element can be located both in BeautifulSoup and in the browser.

    - direction: pick the first match whose id/class hints the direction
      (e.g. "right sidebar" → element with "right" in its class).
    - ordinal: pick by position (-1 = last), resolved to a non-negative index.

    Returns None when the qualifier cannot be satisfied, so the caller can fall
    back to the AI path.
    """
    candidates = _semantic_selectors(focus_region)
    if not candidates:
        return None

    base = ", ".join(candidates)

    # A single comma-list select gives DOM order, de-duplicated — matching what
    # the browser's query_selector_all(base) returns.
    try:
        matches = soup.select(base)
    except Exception:
        return None

    if not matches:
        return None

    # Directional: prefer a match whose id/class names the direction.
    if direction:
        tokens = _DIRECTION_SYNONYMS.get(direction, (direction,))
        for index, element in enumerate(matches):
            if _identity_contains(element, tokens):
                return base, index
        # No structural hint for the direction — defer to the AI path unless an
        # ordinal can still pin it down.
        if ordinal is None:
            return None

    # Ordinal: resolve to a non-negative index into the ordered matches.
    if ordinal is not None:
        index = ordinal if ordinal >= 0 else len(matches) + ordinal
        if 0 <= index < len(matches):
            return base, index
        return None

    return None


def _safe_select_one(soup: BeautifulSoup, selector: str) -> Optional[Tag]:
    """select_one that never raises on a malformed selector."""
    try:
        return soup.select_one(selector)
    except Exception as exc:
        logger.warning("Failed to apply selector '%s': %s", selector, exc)
        return None

# AI-based selector resolution
_AI_SYSTEM = """\
You are a DOM expert for web scraping.
Given a compact page structure skeleton and a focus description, return the single CSS selector
that best targets the described region.
Rules:
- Output only the CSS selector — no explanation, no markdown, no quotes.
- Prefer id-based selectors (#id) or semantic elements (header, footer, main, aside, nav).
- If multiple candidates exist, pick the most specific one that matches the description.
- If nothing matches, output: body
"""

def _ask_ai_for_selector(html: str, focus_region: str) -> Optional[str]:
    """
    Build a compact DOM skeleton and ask the LLM which CSS selector targets
    the requested region. Returns the selector string, or None on failure.
    """
    try:
        from app.ai.llm_client import call_llm

        skeleton = _build_dom_skeleton(html)
        if not skeleton:
            return None

        prompt = (
            f"Focus description: {focus_region}\n\n"
            f"Page structure:\n{skeleton}\n\n"
            "Return the CSS selector for the described region."
        )

        response = call_llm(prompt, system_message=_AI_SYSTEM, max_tokens=64, temperature=0.0)
        raw = (response.text or "").strip().strip("`'\"")

        # Reject nonsense responses
        if not raw or len(raw) > 200 or "\n" in raw:
            logger.warning("AI returned unexpected selector response: %r", raw)
            return None

        logger.info("AI resolved focus '%s' → selector: %s", focus_region, raw)
        return raw

    except Exception as exc:
        logger.warning("AI selector resolution failed: %s", exc)
        return None

# Public API
@dataclass
class FocusResolution:
    """
    A resolved focus target.

    - selector: a CSS selector applicable via select_one/select or a live
      query_selector_all. Empty string when nothing matched.
    - index: which match to use among ``select(selector)`` results (DOM order),
      so directional/ordinal picks map onto the same element in the browser.
    - method: "semantic", "semantic+position", "ai", or "none".
    """
    selector: str
    index: int
    method: str

    @property
    def matched(self) -> bool:
        return bool(self.selector)

    def describe(self) -> str:
        """Human-readable selector, annotated with the index when it is not 0."""
        if not self.selector:
            return ""
        return self.selector if self.index == 0 else f"{self.selector} [#{self.index}]"


def resolve_focus(html: str, focus_region: str) -> FocusResolution:
    """
    Resolve a focus description to a (selector, index) target without scoping
    the HTML.

    This is the shared core used by both the HTML-only path (focus_html) and
    the browser path, which needs a live-queryable selector to clip the
    screenshot and read element geometry.
    """
    if not focus_region or not html:
        return FocusResolution("", 0, "none")

    soup = BeautifulSoup(html, "html.parser")
    direction, ordinal = _parse_qualifier(focus_region)

    # --- Step 0: positional resolution when a direction/ordinal qualifier is
    # present (e.g. "right sidebar", "second ad", "last banner"). Plain semantic
    # matching would ignore the qualifier and always return the first match. ---
    if direction is not None or ordinal is not None:
        positional = _resolve_positional_index(soup, focus_region, direction, ordinal)
        if positional is not None:
            selector, index = positional
            return FocusResolution(selector, index, "semantic+position")

        # Qualifier present but no structural hint — the AI, reading id/class
        # names in the DOM skeleton, has the best shot at directional nuance.
        ai_selector = _ask_ai_for_selector(html, focus_region)
        if ai_selector and _safe_select_one(soup, ai_selector) is not None:
            return FocusResolution(ai_selector, 0, "ai")

    # --- Step 1: semantic keyword map ---
    selector = _try_semantic(html, focus_region)
    if selector:
        return FocusResolution(selector, 0, "semantic")

    # --- Step 2: AI fallback (skip if a qualifier already triggered one) ---
    if direction is None and ordinal is None:
        ai_selector = _ask_ai_for_selector(html, focus_region)
        if ai_selector and _safe_select_one(soup, ai_selector) is not None:
            return FocusResolution(ai_selector, 0, "ai")

    logger.warning("Could not resolve focus region '%s' — using full page", focus_region)
    return FocusResolution("", 0, "none")


def focus_html(
    html: str,
    focus_region: str,
) -> Tuple[str, str, str]:
    """
    Scope the page HTML to the element best matching focus_region.

    Returns:
        (focused_html, selector_used, method)
        - focused_html: the outerHTML of the matched element, or original html on failure
        - selector_used: the CSS selector that was applied (empty string if none)
        - method: "semantic", "semantic+position", "ai", or "none" (fallback)
    """
    if not focus_region or not html:
        return html, "", "none"

    resolution = resolve_focus(html, focus_region)
    if not resolution.selector:
        return html, "", "none"

    soup = BeautifulSoup(html, "html.parser")
    try:
        matches = soup.select(resolution.selector)
    except Exception as exc:
        logger.warning("Failed to apply selector '%s': %s", resolution.selector, exc)
        return html, resolution.selector, "none"

    if resolution.index < len(matches):
        element = matches[resolution.index]
        logger.info(
            "Focus region '%s' applied (%s): selector=%s",
            focus_region, resolution.method, resolution.describe(),
        )
        return str(element), resolution.describe(), resolution.method

    logger.warning(
        "Selector '%s' (method=%s) found no element — using full page",
        resolution.selector, resolution.method,
    )
    return html, resolution.selector, "none"

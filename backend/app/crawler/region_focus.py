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
from typing import Optional, Tuple

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Semantic keyword → CSS selector candidates (tried in order)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# DOM skeleton builder
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Semantic heuristic
# ---------------------------------------------------------------------------

def _try_semantic(html: str, focus_region: str) -> Optional[str]:
    """
    Try to match focus_region against known keywords and return the first
    matching CSS selector that actually finds an element in the HTML.

    Returns None if no match.
    """
    soup = BeautifulSoup(html, "html.parser")

    for pattern, css_selector in _SEMANTIC_MAP:
        if not pattern.search(focus_region):
            continue
        # Try each comma-separated selector in priority order
        for candidate in css_selector.split(","):
            candidate = candidate.strip()
            try:
                el = soup.select_one(candidate)
                if el:
                    logger.debug("Semantic selector matched '%s' → %s", focus_region, candidate)
                    return candidate
            except Exception:
                continue

    return None


# ---------------------------------------------------------------------------
# AI-based selector resolution
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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
        - method: "semantic", "ai", or "none" (fallback)
    """
    if not focus_region or not html:
        return html, "", "none"

    # --- Step 1: semantic keyword map ---
    selector = _try_semantic(html, focus_region)
    method = "semantic"

    # --- Step 2: AI fallback ---
    if not selector:
        selector = _ask_ai_for_selector(html, focus_region)
        method = "ai"

    if not selector:
        logger.warning("Could not resolve focus region '%s' — using full page", focus_region)
        return html, "", "none"

    # --- Apply selector ---
    try:
        soup = BeautifulSoup(html, "html.parser")
        el = soup.select_one(selector)
        if el:
            logger.info("Focus region '%s' applied (%s): selector=%s", focus_region, method, selector)
            return str(el), selector, method
        else:
            logger.warning(
                "Selector '%s' (method=%s) found no element — using full page",
                selector, method,
            )
            return html, selector, "none"
    except Exception as exc:
        logger.warning("Failed to apply selector '%s': %s — using full page", selector, exc)
        return html, selector, "none"

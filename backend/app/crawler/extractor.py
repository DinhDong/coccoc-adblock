# Extracts ad-relevant information from the loaded page:
# extract page title
# extract external scripts
# extract iframes
# extract ad-related elements (by class/ID patterns) with CSS selectors
# extract elements with data-ad-* attributes
# extract third-party resource URLs

import logging
import re
from bs4 import BeautifulSoup, Tag
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ad-related patterns used to identify ad containers in the DOM
# ---------------------------------------------------------------------------

# Patterns that indicate an element is ad-related (matched against class names and IDs)
AD_CLASS_ID_PATTERNS = [
    re.compile(r'\bad[s]?\b', re.I),              # "ad", "ads"
    re.compile(r'\bad[-_]', re.I),                 # "ad-container", "ad_slot", etc.
    re.compile(r'[-_]ad[s]?\b', re.I),             # "google-ad", "sidebar-ads"
    re.compile(r'\badvertis', re.I),               # "advertisement", "advertising"
    re.compile(r'\bsponsored?\b', re.I),           # "sponsor", "sponsored"
    re.compile(r'\bbanner\b', re.I),               # "banner"
    re.compile(r'\badsbygoogle\b', re.I),          # Google AdSense
    re.compile(r'\bgpt[-_]?ad\b', re.I),           # Google Publisher Tag
    re.compile(r'\bdfp[-_]', re.I),                # Google DFP
    re.compile(r'\btaboola\b', re.I),              # Taboola
    re.compile(r'\boutbrain\b', re.I),             # Outbrain
    re.compile(r'\bnative[-_]?ad\b', re.I),        # Native ads
    re.compile(r'\bpromo(?:tion|ted)?\b', re.I),   # Promo/promoted content
    re.compile(r'\binterstitial\b', re.I),         # Interstitial ads
    re.compile(r'\bprebid\b', re.I),               # Prebid header bidding
]

# Attributes that indicate ad configuration
AD_DATA_ATTRIBUTES = [
    "data-ad-client",
    "data-ad-slot",
    "data-ad-format",
    "data-ad-layout",
    "data-ad-region",
    "data-ad-unit",
    "data-ad-zone",
    "data-tracking-id",
    "data-sponsor",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AdElement:
    """An element in the DOM that appears to be ad-related."""
    tag: str                     # e.g. "div", "ins", "iframe"
    selector: str                # CSS selector to target this element, e.g. "div#div-gpt-ad-123"
    reason: str                  # Why this was flagged, e.g. "class 'ad-container' matches ad pattern"
    classes: List[str] = field(default_factory=list)
    element_id: str = ""
    outer_html_snippet: str = ""  # First ~200 chars of outerHTML for context
    ad_attributes: dict = field(default_factory=dict)  # data-ad-* values
    parent_chain: List[dict] = field(default_factory=list)  # Nearest ancestors with id/class

    def to_dict(self) -> dict:
        d = {
            "tag": self.tag,
            "selector": self.selector,
            "reason": self.reason,
        }
        if self.classes:
            d["classes"] = self.classes
        if self.element_id:
            d["element_id"] = self.element_id
        if self.outer_html_snippet:
            d["outer_html_snippet"] = self.outer_html_snippet
        if self.ad_attributes:
            d["ad_attributes"] = self.ad_attributes
        if self.parent_chain:
            d["parent_chain"] = self.parent_chain
        return d


@dataclass
class ExtractedData:
    """Holds ad-relevant data extracted from a crawled page."""
    title: str = ""
    scripts: List[str] = field(default_factory=list)       # External script URLs
    iframes: List[str] = field(default_factory=list)       # Iframe src URLs
    ad_elements: List[AdElement] = field(default_factory=list)  # DOM elements flagged as ad-related

    def to_dict(self) -> dict:
        """Convert to a plain dictionary for JSON serialization."""
        return {
            "title": self.title,
            "scripts": self.scripts,
            "iframes": self.iframes,
            "ad_elements": [el.to_dict() for el in self.ad_elements],
        }


class PageExtractor:
    """Extracts ad-relevant information from rendered HTML content."""

    def __init__(self, html: str, page_url: str = ""):
        """
        Initialize the extractor with raw HTML.

        Args:
            html: The rendered HTML string from the browser module.
            page_url: The URL of the page (used to distinguish first-party vs third-party).
        """
        # Fallback: guard against None or non-string input
        if not html or not isinstance(html, str):
            logger.warning("Received empty or invalid HTML input, using empty document")
            html = ""
        self.soup = BeautifulSoup(html, "html.parser")
        self.page_url = page_url
        self._page_domain = ""
        if page_url:
            try:
                self._page_domain = urlparse(page_url).hostname or ""
            except Exception:
                self._page_domain = ""

    def extract_all(self) -> ExtractedData:
        """
        Run all extraction steps and return an ExtractedData object.
        Each step is wrapped in try/except so one failure doesn't lose everything.

        Returns:
            ExtractedData containing ad-relevant page information.
        """
        data = ExtractedData()

        # --- title ---
        try:
            data.title = self.extract_title()
        except Exception as e:
            logger.warning(f"Failed to extract title: {e}")
            data.title = ""

        # --- scripts ---
        try:
            data.scripts = self.extract_scripts()
        except Exception as e:
            logger.warning(f"Failed to extract scripts: {e}")
            data.scripts = []

        # --- iframes ---
        try:
            data.iframes = self.extract_iframes()
        except Exception as e:
            logger.warning(f"Failed to extract iframes: {e}")
            data.iframes = []

        # --- ad elements (class/ID/attribute-based) ---
        try:
            data.ad_elements = self.extract_ad_elements()
        except Exception as e:
            logger.warning(f"Failed to extract ad elements: {e}")
            data.ad_elements = []

        return data

    def extract_title(self) -> str:
        """Extract the page title from the <title> tag."""
        title_tag = self.soup.find("title")
        if title_tag and title_tag.string:
            return title_tag.string.strip()
        return ""

    def extract_scripts(self) -> List[str]:
        """
        Extract all external script source URLs.

        Returns:
            List of script src URLs found on the page.
        """
        scripts = []
        for tag in self.soup.find_all("script", src=True):
            try:
                src = tag.get("src", "").strip()
                if src and not src.startswith("data:"):
                    scripts.append(src)
            except Exception as e:
                logger.warning(f"Skipped broken <script> tag: {e}")
                continue
        return scripts

    def extract_iframes(self) -> List[str]:
        """
        Extract all iframe source URLs.

        Returns:
            List of iframe src URLs found on the page.
        """
        iframes = []
        for tag in self.soup.find_all("iframe", src=True):
            try:
                src = tag.get("src", "").strip()
                if src and not src.startswith("data:") and not src.startswith("about:"):
                    iframes.append(src)
            except Exception as e:
                logger.warning(f"Skipped broken <iframe> tag: {e}")
                continue
        return iframes

    def extract_ad_elements(self) -> List[AdElement]:
        """
        Scan the DOM for elements that look like ad containers.

        Checks classes, IDs, and data-ad-* attributes against known ad patterns.
        Returns elements with their CSS selector paths for direct use in adblock rules.
        """
        ad_elements: List[AdElement] = []
        seen_selectors: set = set()

        for tag in self.soup.find_all(True):
            if not isinstance(tag, Tag):
                continue

            tag_name = tag.name
            # Skip non-visual / structural tags
            if tag_name in ("html", "head", "body", "meta", "link", "title",
                            "style", "script", "noscript"):
                continue

            element_id = (tag.get("id") or "").strip()
            tag_classes = tag.get("class", [])
            if isinstance(tag_classes, str):
                tag_classes = tag_classes.split()

            reasons = []

            # Check ID against ad patterns
            if element_id:
                for pattern in AD_CLASS_ID_PATTERNS:
                    if pattern.search(element_id):
                        reasons.append(f"id '{element_id}' matches ad pattern")
                        break

            # Check classes against ad patterns
            matched_classes = []
            for cls in tag_classes:
                for pattern in AD_CLASS_ID_PATTERNS:
                    if pattern.search(cls):
                        matched_classes.append(cls)
                        break
            if matched_classes:
                reasons.append(f"class '{', '.join(matched_classes)}' matches ad pattern")

            # Check for ad data attributes
            ad_attrs = {}
            for attr_name in AD_DATA_ATTRIBUTES:
                val = tag.get(attr_name)
                if val is not None:
                    ad_attrs[attr_name] = str(val)
            # Also check any data-ad-* attribute not in the explicit list
            for attr_name in tag.attrs:
                if isinstance(attr_name, str) and attr_name.startswith("data-ad-") and attr_name not in ad_attrs:
                    ad_attrs[attr_name] = str(tag[attr_name])

            if ad_attrs:
                reasons.append(f"has ad data attributes: {', '.join(ad_attrs.keys())}")

            # Check for <ins class="adsbygoogle"> (AdSense)
            if tag_name == "ins" and "adsbygoogle" in tag_classes:
                if not reasons:
                    reasons.append("AdSense <ins> element")

            # Check for <amp-ad>
            if tag_name == "amp-ad":
                reasons.append("AMP ad component")

            if not reasons:
                continue

            # Build CSS selector
            selector = self._build_selector(tag, element_id, tag_classes)

            # Skip duplicates
            if selector in seen_selectors:
                continue
            seen_selectors.add(selector)

            # Get outer HTML snippet (truncated)
            try:
                outer_html = str(tag)
                # Only keep the opening tag for the snippet
                if ">" in outer_html:
                    opening_end = outer_html.index(">") + 1
                    snippet = outer_html[:min(opening_end, 200)]
                else:
                    snippet = outer_html[:200]
            except Exception:
                snippet = ""

            ad_elements.append(AdElement(
                tag=tag_name,
                selector=selector,
                reason="; ".join(reasons),
                classes=list(tag_classes),
                element_id=element_id,
                outer_html_snippet=snippet,
                ad_attributes=ad_attrs,
                parent_chain=self._build_parent_chain(tag),
            ))

        return ad_elements

    def _build_parent_chain(self, tag: Tag, max_depth: int = 4) -> List[dict]:
        """
        Walk up the DOM from tag and collect ancestors that have a meaningful
        id or class.  Returns closest-first, stopping at <body>/<html>.
        """
        chain: List[dict] = []
        current = tag.parent
        depth = 0
        while current and depth < max_depth and isinstance(current, Tag):
            name = current.name
            if name in ("html", "body", "head"):
                break
            pid = (current.get("id") or "").strip()
            pclasses = current.get("class", [])
            if isinstance(pclasses, str):
                pclasses = pclasses.split()
            if pid or pclasses:
                chain.append({
                    "tag": name,
                    "id": pid,
                    "classes": list(pclasses),
                })
            current = current.parent
            depth += 1
        return chain

    def _build_selector(self, tag: Tag, element_id: str, classes: List[str]) -> str:
        """
        Build a CSS selector for the element.
        Prefers ID-based selectors (most specific), falls back to class-based.
        """
        tag_name = tag.name

        if element_id:
            return f"{tag_name}#{element_id}"

        if classes:
            # Use only the ad-related classes to keep the selector meaningful
            ad_classes = []
            for cls in classes:
                for pattern in AD_CLASS_ID_PATTERNS:
                    if pattern.search(cls):
                        ad_classes.append(cls)
                        break
            if ad_classes:
                return f"{tag_name}.{'.'.join(ad_classes)}"
            # Fall back to first class
            return f"{tag_name}.{classes[0]}"

        return tag_name
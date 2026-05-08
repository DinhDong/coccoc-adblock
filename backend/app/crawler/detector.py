#Finds possible ad-related signals:
#check for ad-related keywords
#flag suspicious scripts
#flag suspicious iframes
#flag suspicious CSS classes
#flag suspicious IDs
#flag common ad-like elements


import re
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AdSignal:
    """Represents a single detected ad signal on a page."""
    type: str          # "script" | "iframe" | "element" | "keyword" | "network"
    value: str         # The URL, selector, or raw value that triggered detection
    reason: str        # Human-readable explanation of why this was flagged
    confidence: str = "high"   # "high" | "medium" | "low"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DetectionResult:
    """Full detection result for a crawled page, passed to storage.py."""
    url: str
    ad_signals: list[AdSignal]
    has_ads: bool
    signal_count: int
    errors: list[str]

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "ad_signals": [s.to_dict() for s in self.ad_signals],
            "has_ads": self.has_ads,
            "signal_count": self.signal_count,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

# Known ad network domains (partial match against script/iframe URLs)
AD_NETWORK_DOMAINS = [
    "doubleclick.net",
    "googlesyndication.com",
    "googletagmanager.com",
    "googletagservices.com",
    "adnxs.com",           # AppNexus / Xandr
    "moatads.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "rubiconproject.com",
    "openx.net",
    "pubmatic.com",
    "smartadserver.com",
    "adform.net",
    "media.net",
    "amazon-adsystem.com",
    "adsrvr.org",          # The Trade Desk
    "advertising.com",
    "yieldmo.com",
    "sharethrough.com",
    "triplelift.com",
    "sovrn.com",
    "ads.example.com",     # From spec example
    "adnetwork.example.com",
]

# Keywords found in script/iframe URLs that strongly indicate ads
AD_URL_KEYWORDS = [
    "ad", "ads", "adserver", "adservice", "adunit", "adslot",
    "banner", "sponsor", "promo", "promotion",
    "tracking", "tracker", "pixel", "beacon",
    "affiliate", "click", "impression", "monetize",
    "prebid", "dfp", "gpt", "adsense",
    "interstitial", "popup", "popunder",
    "retarget", "remarketing",
]

# Suspicious CSS class names that strongly suggest ad containers
AD_CSS_CLASSES = [
    "ad", "ads", "ad-unit", "ad-slot", "ad-container", "ad-wrapper",
    "ad-banner", "ad-box", "ad-block", "ad-frame", "ad-label",
    "advertisement", "advertising", "advertorial",
    "adsbygoogle",                   # Google AdSense
    "dfp-ad", "dfp-slot",            # Google DFP / GAM
    "gpt-ad",                        # Google Publisher Tag
    "sponsor", "sponsored", "sponsorship", "sponsored-content",
    "promo", "promotion", "promoted",
    "banner", "leaderboard", "skyscraper", "interstitial",
    "mpu", "mrec", "halfpage",       # IAB standard ad sizes
    "prebid", "header-bid",
    "taboola-ad", "outbrain-widget",
    "native-ad", "native-advert",
    "text-ad", "text-link-ad",
]

# Suspicious HTML element IDs that suggest ad placement slots
AD_SUSPICIOUS_IDS = [
    "ad", "ads", "ad-unit", "ad-slot", "ad-container", "ad-wrapper",
    "ad-top", "ad-bottom", "ad-left", "ad-right", "ad-sidebar",
    "ad-header", "ad-footer", "ad-banner", "ad-leaderboard",
    "advertisement", "advertise",
    "google-ad", "google_ads", "google-ads-container",
    "dfp", "dfp-ad", "gpt-ad",
    "sponsor", "sponsor-box", "sponsored",
    "banner", "banner-ad",
    "taboola", "outbrain",
    "div-gpt-ad",                    # Google Publisher Tag slot naming pattern
]

# Common HTML elements / patterns that signal ad placements
AD_ELEMENT_PATTERNS = [
    # <ins class="adsbygoogle"> — AdSense standard unit
    (re.compile(r'<ins\b[^>]*adsbygoogle', re.I),
     "AdSense <ins> ad unit element"),

    # Google Publisher Tag slot divs: <div id="div-gpt-ad-...">
    (re.compile(r'<div\b[^>]*id=["\']div-gpt-ad', re.I),
     "Google Publisher Tag ad slot div"),

    # data-ad-* attributes on any element
    (re.compile(r'\bdata-ad-\w+', re.I),
     "Element with data-ad-* attribute (ad configuration)"),

    # <iframe> with known ad sizing (standard IAB ad dimensions)
    (re.compile(r'<iframe\b[^>]*(?:width=["\'](?:728|300|160|320|970)["\']'
                r'|height=["\'](?:90|250|600|50|90)["\'])', re.I),
     "iframe with standard IAB ad dimensions"),

    # Sticky/fixed positioned ad wrappers
    (re.compile(r'position\s*:\s*(?:fixed|sticky)[^}]*ad', re.I),
     "Fixed/sticky positioned ad element"),

    # <amp-ad> — AMP pages ad component
    (re.compile(r'<amp-ad\b', re.I),
     "AMP ad component (<amp-ad>)"),

    # Taboola widget container
    (re.compile(r'<div\b[^>]*id=["\']taboola', re.I),
     "Taboola widget container element"),

    # Outbrain widget
    (re.compile(r'<div\b[^>]*class=["\'][^"\']*OUTBRAIN', re.I),
     "Outbrain widget element"),
]

# Regex patterns for inline HTML / page content signals
AD_CONTENT_PATTERNS = [
    (re.compile(r'\bwindow\.__googletag\b', re.I),       "Google Publisher Tag (GPT) initialisation"),
    (re.compile(r'\bgoogletag\.cmd\b', re.I),             "Google Publisher Tag command queue"),
    (re.compile(r'\bpbjs\b', re.I),                       "Prebid.js header bidding library"),
    (re.compile(r'\badfree\s*=\s*false\b', re.I),         "Explicit ad-enabled flag"),
    (re.compile(r'\badsense\b', re.I),                    "Google AdSense reference"),
    (re.compile(r'data-ad-client', re.I),                 "AdSense data-ad-client attribute"),
    (re.compile(r'data-ad-slot', re.I),                   "AdSense data-ad-slot attribute"),
    (re.compile(r'\b_taboola\b', re.I),                   "Taboola widget initialisation"),
    (re.compile(r'\boutbrain\b', re.I),                   "Outbrain widget reference"),
    (re.compile(r'amazon-adsystem', re.I),                "Amazon Advertising pixel"),
    (re.compile(r'<ins\s[^>]*class=["\'][^"\']*adsbygoogle', re.I), "AdSense <ins> ad unit"),
]


# ---------------------------------------------------------------------------
# Core detector
# ---------------------------------------------------------------------------

class AdDetector:
    """
    Analyses data extracted by extractor.py and returns a DetectionResult.

    Expected input format (matches extractor.py output):
    {
        "url": "https://example.com",
        "scripts": ["https://...", ...],
        "iframes": ["https://...", ...],
        "html": "<html>...</html>",          # optional raw HTML
        "meta": { ... }                      # optional page metadata
    }
    """

    def __init__(self, custom_domains: Optional[list[str]] = None,
                 custom_keywords: Optional[list[str]] = None):
        self.ad_domains = AD_NETWORK_DOMAINS + (custom_domains or [])
        self.ad_keywords = AD_URL_KEYWORDS + (custom_keywords or [])

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def detect(self, extracted_data: dict) -> DetectionResult:
        """
        Main method. Pass the dict from extractor.py, receive DetectionResult.

        Returns DetectionResult whose .to_dict() matches the crawl result JSON
        schema (ad_signals array with type/value/reason fields).
        """
        url = extracted_data.get("url", "")
        scripts = extracted_data.get("scripts", [])
        iframes = extracted_data.get("iframes", [])
        html = extracted_data.get("html", "")
        errors: list[str] = []
        signals: list[AdSignal] = []

        try:
            signals += self._check_scripts(scripts)
        except Exception as exc:
            errors.append(f"script_check_error: {exc}")

        try:
            signals += self._check_iframes(iframes)
        except Exception as exc:
            errors.append(f"iframe_check_error: {exc}")

        try:
            if html:
                signals += self._check_html_content(html)
        except Exception as exc:
            errors.append(f"html_check_error: {exc}")

        try:
            if html:
                signals += self._check_css_classes(html)
        except Exception as exc:
            errors.append(f"css_class_check_error: {exc}")

        try:
            if html:
                signals += self._check_suspicious_ids(html)
        except Exception as exc:
            errors.append(f"id_check_error: {exc}")

        try:
            if html:
                signals += self._check_ad_elements(html)
        except Exception as exc:
            errors.append(f"element_check_error: {exc}")

        # Deduplicate by (type, value) to avoid noise from repeated URLs
        signals = self._deduplicate(signals)

        return DetectionResult(
            url=url,
            ad_signals=signals,
            has_ads=len(signals) > 0,
            signal_count=len(signals),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Private checkers
    # ------------------------------------------------------------------

    def _check_scripts(self, scripts: list[str]) -> list[AdSignal]:
        """Inspect script URLs for ad network domains and keywords."""
        signals = []
        for src in scripts:
            if not isinstance(src, str) or not src.strip():
                continue

            domain_match = self._match_ad_domain(src)
            if domain_match:
                signals.append(AdSignal(
                    type="script",
                    value=src,
                    reason=f"script src matches known ad network domain ({domain_match})",
                    confidence="high",
                ))
                continue  # domain match is definitive; skip keyword check

            keyword_match = self._match_ad_keyword_in_url(src)
            if keyword_match:
                signals.append(AdSignal(
                    type="script",
                    value=src,
                    reason=f"contains ad keyword",
                    confidence="medium",
                    metadata={"matched_keyword": keyword_match},
                ))

        return signals

    def _check_iframes(self, iframes: list[str]) -> list[AdSignal]:
        """Inspect iframe src URLs for ad network domains and keywords."""
        signals = []
        for src in iframes:
            if not isinstance(src, str) or not src.strip():
                continue

            domain_match = self._match_ad_domain(src)
            if domain_match:
                signals.append(AdSignal(
                    type="iframe",
                    value=src,
                    reason=f"iframe src matches known ad network domain ({domain_match})",
                    confidence="high",
                ))
                continue

            keyword_match = self._match_ad_keyword_in_url(src)
            if keyword_match:
                signals.append(AdSignal(
                    type="iframe",
                    value=src,
                    reason=f"contains ad keyword",
                    confidence="medium",
                    metadata={"matched_keyword": keyword_match},
                ))

        return signals

    def _check_html_content(self, html: str) -> list[AdSignal]:
        """Scan raw HTML for inline ad patterns (GPT, Prebid, AdSense, etc.)."""
        signals = []
        for pattern, reason in AD_CONTENT_PATTERNS:
            if pattern.search(html):
                signals.append(AdSignal(
                    type="keyword",
                    value=pattern.pattern,
                    reason=reason,
                    confidence="medium",
                ))
        return signals

    def _check_css_classes(self, html: str) -> list[AdSignal]:
        """Scan HTML for elements whose class attribute contains known ad class names."""
        signals = []
        # Extract all class attribute values from the HTML
        class_attrs = re.findall(r'class=["\']([^"\']+)["\']', html, re.I)
        found_classes: set[str] = set()
        for attr_value in class_attrs:
            classes = attr_value.lower().split()
            for cls in classes:
                cls_clean = cls.strip(".-_")
                for ad_class in AD_CSS_CLASSES:
                    if cls_clean == ad_class.lower() and ad_class not in found_classes:
                        found_classes.add(ad_class)
                        signals.append(AdSignal(
                            type="css_class",
                            value=f".{ad_class}",
                            reason=f"suspicious CSS class '{ad_class}' suggests ad container",
                            confidence="medium",
                        ))
        return signals

    def _check_suspicious_ids(self, html: str) -> list[AdSignal]:
        """Scan HTML for element IDs that match known ad slot naming patterns."""
        signals = []
        # Extract all id attribute values
        id_attrs = re.findall(r'\bid=["\']([^"\']+)["\']', html, re.I)
        found_ids: set[str] = set()
        for id_value in id_attrs:
            id_lower = id_value.lower()
            # Exact match against known ad IDs
            for ad_id in AD_SUSPICIOUS_IDS:
                if id_lower == ad_id.lower() and ad_id not in found_ids:
                    found_ids.add(ad_id)
                    signals.append(AdSignal(
                        type="element_id",
                        value=f"#{ad_id}",
                        reason=f"suspicious element ID '#{ad_id}' suggests ad slot",
                        confidence="medium",
                    ))
                    break
            # Also catch GPT pattern: div-gpt-ad-XXXXXXXXXX-0
            if re.match(r'div-gpt-ad', id_lower) and id_value not in found_ids:
                found_ids.add(id_value)
                signals.append(AdSignal(
                    type="element_id",
                    value=f"#{id_value}",
                    reason="Google Publisher Tag slot ID pattern (div-gpt-ad-*)",
                    confidence="high",
                ))
        return signals

    def _check_ad_elements(self, html: str) -> list[AdSignal]:
        """Scan HTML for common ad-placement elements and structural patterns."""
        signals = []
        for pattern, reason in AD_ELEMENT_PATTERNS:
            match = pattern.search(html)
            if match:
                signals.append(AdSignal(
                    type="element",
                    value=match.group(0)[:120],  # truncate long matches
                    reason=reason,
                    confidence="high",
                ))
        return signals

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _match_ad_domain(self, url: str) -> Optional[str]:
        """Return the matched ad domain string, or None."""
        try:
            hostname = urlparse(url).hostname or ""
        except Exception:
            hostname = url
        hostname = hostname.lower()
        for domain in self.ad_domains:
            if domain in hostname:
                return domain
        return None

    def _match_ad_keyword_in_url(self, url: str) -> Optional[str]:
        """Return the first matched ad keyword found in the URL path/query, or None."""
        url_lower = url.lower()
        for keyword in self.ad_keywords:
            # Match keyword as a whole word segment (between slashes, dots, dashes, etc.)
            pattern = rf'(?<![a-z]){re.escape(keyword)}(?![a-z])'
            if re.search(pattern, url_lower):
                return keyword
        return None

    @staticmethod
    def _deduplicate(signals: list[AdSignal]) -> list[AdSignal]:
        """Remove duplicate signals with the same (type, value) pair."""
        seen: set[tuple] = set()
        unique = []
        for s in signals:
            key = (s.type, s.value)
            if key not in seen:
                seen.add(key)
                unique.append(s)
        return unique


# ---------------------------------------------------------------------------
# Module-level convenience function (matches pipeline call convention)
# ---------------------------------------------------------------------------

def detect_ads(extracted_data: dict,
               custom_domains: Optional[list[str]] = None,
               custom_keywords: Optional[list[str]] = None) -> dict:
    """
    Convenience wrapper used by crawler_service.py in the pipeline.

    Usage:
        from backend.app.crawler.detector import detect_ads
        result = detect_ads(extractor_output)
        # result["ad_signals"] → list of {type, value, reason, confidence}

    Returns a plain dict (JSON-serialisable) matching the crawl result schema.
    """
    detector = AdDetector(custom_domains=custom_domains,
                          custom_keywords=custom_keywords)
    return detector.detect(extracted_data).to_dict()


# ---------------------------------------------------------------------------
# Quick smoke-test (run: python detector.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    sample_extractor_output = {
        "url": "https://example-news.com",
        "scripts": [
            "https://example-news.com/main.js",
            "https://ads.example.com/banner.js",          # should be flagged
            "https://www.googletagmanager.com/gtm.js",    # should be flagged
        ],
        "iframes": [
            "https://adnetwork.example.com/ad.html",      # should be flagged
        ],
        "html": """
            <html>
            <head>
              <script>googletag.cmd.push(function() {});</script>
            </head>
            <body>
              <!-- CSS class signal -->
              <div class="ad-container sponsored">Sponsored content here</div>
              <!-- ID signal -->
              <div id="div-gpt-ad-1234567890-0"></div>
              <!-- AdSense element signal -->
              <ins class="adsbygoogle" data-ad-slot="1234" data-ad-client="ca-pub-xxx"></ins>
              <!-- AMP ad element -->
              <amp-ad width="728" height="90" type="adsense"></amp-ad>
            </body>
            </html>
        """,
    }

    result = detect_ads(sample_extractor_output)
    print(json.dumps(result, indent=2))
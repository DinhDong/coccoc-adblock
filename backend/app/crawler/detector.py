# Analyses crawl data and produces grouped, actionable ad candidates.
#
# Input: extracted page data + captured network requests
# Output: ad_candidates with suggested adblock rules, grouped by domain/element


import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import List, Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AdCandidate:
    """A single detected ad candidate — ready for rule generation."""
    category: str          # "ad_network_request", "ad_container", "tracking_script", "ad_iframe"
    confidence: str        # "high", "medium", "low"
    suggested_rule: str    # Draft adblock rule, e.g. "||doubleclick.net^" or "example.com##div.ad-slot"
    reason: str            # Human-readable explanation
    domain: str = ""       # For network-based candidates: the third-party domain
    urls: List[str] = field(default_factory=list)      # URLs that triggered this candidate
    selector: str = ""     # For element-based candidates: the CSS selector
    element_snippet: str = ""  # Truncated HTML for context

    def to_dict(self) -> dict:
        d = {
            "category": self.category,
            "confidence": self.confidence,
            "suggested_rule": self.suggested_rule,
            "reason": self.reason,
        }
        if self.domain:
            d["domain"] = self.domain
        if self.urls:
            d["urls"] = self.urls
        if self.selector:
            d["selector"] = self.selector
        if self.element_snippet:
            d["element_snippet"] = self.element_snippet
        return d


@dataclass
class DetectionResult:
    """Full detection result for a crawled page."""
    url: str
    ad_candidates: List[AdCandidate]
    has_ads: bool
    summary: dict
    errors: List[str]

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "ad_candidates": [c.to_dict() for c in self.ad_candidates],
            "has_ads": self.has_ads,
            "summary": self.summary,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Known ad network domains
# ---------------------------------------------------------------------------

AD_NETWORK_DOMAINS = [
    "doubleclick.net",
    "googlesyndication.com",
    "googletagmanager.com",
    "googletagservices.com",
    "adnxs.com",
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
    "adsrvr.org",
    "advertising.com",
    "yieldmo.com",
    "sharethrough.com",
    "triplelift.com",
    "sovrn.com",
    "33across.com",
    "indexexchange.com",
    "casalemedia.com",
    "bidswitch.net",
    "adsafeprotected.com",
    "serving-sys.com",
    "ssp.yahoo.com",
    "contextweb.com",
]

# Tracking / analytics domains (separate from ads — lower confidence)
TRACKING_DOMAINS = [
    "google-analytics.com",
    "googletagmanager.com",
    "facebook.net",
    "hotjar.com",
    "mouseflow.com",
    "clarity.ms",
    "segment.io",
    "mixpanel.com",
    "amplitude.com",
]

# Domains that are NOT ads — suppress false positives
SAFE_DOMAINS = [
    "cloudflareinsights.com",   # Cloudflare analytics (not ads)
    "cloudflare.com",
    "googleapis.com",           # Google Fonts, APIs
    "gstatic.com",              # Google static content
    "jquery.com",
    "jsdelivr.net",
    "cdnjs.cloudflare.com",
    "unpkg.com",
    "wordpress.org",
    "wp.com",
    "gravatar.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
]

# Ad-related URL path keywords (must appear in path/query, not just hostname)
AD_URL_PATH_KEYWORDS = [
    "adserver", "adservice", "adunit", "adslot", "ad-slot",
    "pagead", "show_ads", "ad_iframe",
    "prebid", "dfp", "gpt", "adsense",
    "interstitial", "popup", "popunder",
]


# ---------------------------------------------------------------------------
# Core detector
# ---------------------------------------------------------------------------

class AdDetector:
    """
    Analyses extracted data + network requests to produce AdCandidates.

    Expected input:
    {
        "url": "https://example.com",
        "scripts": [...],
        "iframes": [...],
        "ad_elements": [...],           # From extractor's ad element scan
        "network_requests": [...],      # From browser's request capture
    }
    """

    def __init__(self, custom_domains: Optional[List[str]] = None):
        self.ad_domains = AD_NETWORK_DOMAINS + (custom_domains or [])

    def detect(self, extracted_data: dict) -> DetectionResult:
        """
        Main method. Pass the combined dict from extractor + browser, receive DetectionResult.
        """
        url = extracted_data.get("url", "")
        network_requests = extracted_data.get("network_requests", [])
        ad_elements = extracted_data.get("ad_elements", [])
        scripts = extracted_data.get("scripts", [])
        iframes = extracted_data.get("iframes", [])
        errors: List[str] = []
        candidates: List[AdCandidate] = []

        page_domain = ""
        try:
            page_domain = urlparse(url).hostname or ""
        except Exception:
            pass

        # 1. Analyse network requests — group by third-party domain
        try:
            candidates += self._analyse_network_requests(network_requests, page_domain, url)
        except Exception as exc:
            errors.append(f"network_analysis_error: {exc}")

        # 2. Analyse ad elements from the DOM
        try:
            candidates += self._analyse_ad_elements(ad_elements, url)
        except Exception as exc:
            errors.append(f"ad_element_analysis_error: {exc}")

        # 3. Analyse script URLs (from HTML parsing — backup for network capture)
        try:
            candidates += self._analyse_script_urls(scripts, page_domain, url)
        except Exception as exc:
            errors.append(f"script_analysis_error: {exc}")

        # 4. Analyse iframe URLs
        try:
            candidates += self._analyse_iframe_urls(iframes, page_domain, url)
        except Exception as exc:
            errors.append(f"iframe_analysis_error: {exc}")

        # 5. Analyse fixed/sticky overlay elements (floating banners, interstitials)
        try:
            fixed_elements = extracted_data.get("fixed_elements", [])
            candidates += self._analyse_fixed_elements(fixed_elements, page_domain, url)
        except Exception as exc:
            errors.append(f"fixed_element_analysis_error: {exc}")

        # Deduplicate candidates
        candidates = self._deduplicate(candidates)

        # Build summary
        ad_networks = sorted(set(c.domain for c in candidates if c.domain))
        confidence_breakdown = {"high": 0, "medium": 0, "low": 0}
        for c in candidates:
            confidence_breakdown[c.confidence] = confidence_breakdown.get(c.confidence, 0) + 1

        summary = {
            "ad_networks_found": ad_networks,
            "ad_containers_found": sum(1 for c in candidates if c.category == "ad_container"),
            "suggested_rules_count": len(candidates),
            "confidence_breakdown": confidence_breakdown,
        }

        return DetectionResult(
            url=url,
            ad_candidates=candidates,
            has_ads=len(candidates) > 0,
            summary=summary,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Analysis methods
    # ------------------------------------------------------------------

    def _analyse_network_requests(self, requests: list, page_domain: str, page_url: str) -> List[AdCandidate]:
        """Group third-party network requests by domain and identify ad networks."""
        candidates = []

        # Group requests by domain
        domain_requests: dict[str, list] = defaultdict(list)
        for req in requests:
            req_url = req.get("url", "") if isinstance(req, dict) else getattr(req, "url", "")
            if not req_url or req_url.startswith("data:"):
                continue
            try:
                req_host = urlparse(req_url).hostname or ""
            except Exception:
                continue

            # Skip first-party requests
            if req_host and page_domain and (
                req_host == page_domain or
                req_host.endswith("." + page_domain) or
                page_domain.endswith("." + req_host)
            ):
                continue

            # Skip safe domains
            if any(safe in req_host for safe in SAFE_DOMAINS):
                continue

            domain_requests[req_host].append(req_url)

        # Check each third-party domain
        for domain, urls in domain_requests.items():
            ad_match = self._match_ad_domain(domain)
            if ad_match:
                # Known ad network — high confidence
                candidates.append(AdCandidate(
                    category="ad_network_request",
                    confidence="high",
                    suggested_rule=f"||{ad_match}^",
                    reason=f"Requests to known ad network '{ad_match}' ({len(urls)} requests)",
                    domain=ad_match,
                    urls=urls[:5],  # Cap at 5 example URLs
                ))
            elif self._has_ad_path_keywords(urls):
                # URL paths suggest ad traffic
                candidates.append(AdCandidate(
                    category="ad_network_request",
                    confidence="medium",
                    suggested_rule=f"||{domain}^",
                    reason=f"URL paths contain ad-related keywords ({len(urls)} requests)",
                    domain=domain,
                    urls=urls[:5],
                ))
            elif any(t in domain for t in TRACKING_DOMAINS):
                # Tracking domain
                candidates.append(AdCandidate(
                    category="tracking_script",
                    confidence="medium",
                    suggested_rule=f"||{domain}^",
                    reason=f"Known tracking/analytics domain ({len(urls)} requests)",
                    domain=domain,
                    urls=urls[:3],
                ))

        return candidates

    def _analyse_ad_elements(self, ad_elements: list, page_url: str) -> List[AdCandidate]:
        """Convert ad elements from the extractor into ad candidates."""
        candidates = []
        page_domain = ""
        try:
            page_domain = urlparse(page_url).hostname or ""
            # Strip www. for cleaner rules
            if page_domain.startswith("www."):
                page_domain = page_domain[4:]
        except Exception:
            pass

        for elem in ad_elements:
            if isinstance(elem, dict):
                selector = elem.get("selector", "")
                reason = elem.get("reason", "")
                element_id = elem.get("element_id", "")
                snippet = elem.get("outer_html_snippet", "")
                ad_attrs = elem.get("ad_attributes", {})
            else:
                selector = getattr(elem, "selector", "")
                reason = getattr(elem, "reason", "")
                element_id = getattr(elem, "element_id", "")
                snippet = getattr(elem, "outer_html_snippet", "")
                ad_attrs = getattr(elem, "ad_attributes", {})

            if not selector:
                continue

            # Determine confidence based on signals
            confidence = "medium"
            if ad_attrs:  # Has data-ad-* attributes — very strong signal
                confidence = "high"
            if element_id and re.search(r'gpt-ad|adsense|adsbygoogle', element_id, re.I):
                confidence = "high"

            # Build suggested rule
            if element_id:
                suggested_rule = f"{page_domain}###{element_id}"
            else:
                suggested_rule = f"{page_domain}##{selector}"

            candidates.append(AdCandidate(
                category="ad_container",
                confidence=confidence,
                suggested_rule=suggested_rule,
                reason=reason,
                selector=selector,
                element_snippet=snippet,
            ))

        return candidates

    def _analyse_script_urls(self, scripts: list, page_domain: str, page_url: str) -> List[AdCandidate]:
        """Check script src URLs against ad network domains (backup for network capture)."""
        candidates = []
        for src in scripts:
            if not isinstance(src, str) or not src.strip():
                continue

            try:
                host = urlparse(src).hostname or ""
            except Exception:
                continue

            # Skip first-party
            if host and page_domain and (
                host == page_domain or host.endswith("." + page_domain)
            ):
                continue

            # Skip safe domains
            if any(safe in host for safe in SAFE_DOMAINS):
                continue

            ad_match = self._match_ad_domain(host)
            if ad_match:
                candidates.append(AdCandidate(
                    category="ad_network_request",
                    confidence="high",
                    suggested_rule=f"||{ad_match}^",
                    reason=f"Script from known ad network '{ad_match}'",
                    domain=ad_match,
                    urls=[src],
                ))

        return candidates

    def _analyse_iframe_urls(self, iframes: list, page_domain: str, page_url: str) -> List[AdCandidate]:
        """Check iframe src URLs against ad network domains."""
        candidates = []
        for src in iframes:
            if not isinstance(src, str) or not src.strip():
                continue

            try:
                host = urlparse(src).hostname or ""
            except Exception:
                continue

            # Skip first-party
            if host and page_domain and (
                host == page_domain or host.endswith("." + page_domain)
            ):
                continue

            ad_match = self._match_ad_domain(host)
            if ad_match:
                candidates.append(AdCandidate(
                    category="ad_iframe",
                    confidence="high",
                    suggested_rule=f"||{ad_match}^",
                    reason=f"Iframe from known ad network '{ad_match}'",
                    domain=ad_match,
                    urls=[src],
                ))
            elif any(kw in src.lower() for kw in AD_URL_PATH_KEYWORDS):
                candidates.append(AdCandidate(
                    category="ad_iframe",
                    confidence="medium",
                    suggested_rule=f"||{host}^",
                    reason=f"Iframe URL contains ad-related keywords",
                    domain=host,
                    urls=[src],
                ))

        return candidates

    def _analyse_fixed_elements(self, fixed_elements: list, page_domain: str, page_url: str) -> List[AdCandidate]:
        """
        Detect ad banners that use position:fixed / position:sticky.

        These are invisible to BeautifulSoup because position is a computed
        style set by CSS or JavaScript, not an HTML attribute.  The browser
        captures them via _FIXED_ELEMENT_SCRIPT during render.
        """
        candidates = []
        clean_domain = page_domain.lstrip("www.")

        for el in fixed_elements:
            if not isinstance(el, dict):
                continue

            el_id      = el.get("id", "")
            el_classes = el.get("classes", "")
            ext_links  = el.get("ext_links", [])
            iframes    = el.get("iframes", [])
            width      = el.get("width", 0)
            height     = el.get("height", 0)
            snippet    = el.get("snippet", "")
            position   = el.get("position", "fixed")

            # --- Signal 1: external links inside the element ---
            # Floating banners almost always link out to ad/gambling sites.
            external_domains = []
            for href in ext_links:
                try:
                    h = urlparse(href).hostname or ""
                    if h and not h.endswith(page_domain):
                        external_domains.append(h)
                except Exception:
                    pass

            # --- Signal 2: class/ID matches known ad patterns ---
            attrs_text = f"{el_id} {el_classes}"
            class_match = any(p.search(attrs_text) for p in AD_CLASS_ID_PATTERNS)

            # --- Signal 3: iframes from third-party domains ---
            has_ad_iframe = any(
                not urlparse(src).hostname.endswith(page_domain)
                for src in iframes
                if urlparse(src).hostname
            )

            # --- Signal 4: banner proportions (wide + short = horizontal ad) ---
            is_banner_shape = width >= 400 and 20 <= height <= 200

            # Require at least one strong signal to avoid blocking nav/cookie bars
            signals = sum([
                bool(external_domains),
                class_match,
                has_ad_iframe,
                is_banner_shape,
            ])
            if signals < 1:
                continue

            confidence = "high" if signals >= 2 else "medium"

            # Build the most specific selector available
            if el_id:
                selector = f"#{el_id}"
                suggested_rule = f"{clean_domain}###{el_id}"
            elif el_classes:
                # Use first meaningful class (skip whitespace-only tokens)
                first_class = next((c for c in el_classes.split() if c), None)
                selector = f".{first_class}" if first_class else el.get("tag", "div")
                suggested_rule = f"{clean_domain}##{selector}"
            else:
                selector = el.get("tag", "div")
                suggested_rule = f"{clean_domain}##{selector}[style*='position:{position}']"

            reason_parts = []
            if external_domains:
                reason_parts.append(f"links to external domains: {', '.join(external_domains[:3])}")
            if class_match:
                reason_parts.append("class/id matches ad pattern")
            if has_ad_iframe:
                reason_parts.append("contains third-party iframe")
            if is_banner_shape:
                reason_parts.append(f"banner proportions ({width}×{height}px)")

            candidates.append(AdCandidate(
                category="floating_ad",
                confidence=confidence,
                suggested_rule=suggested_rule,
                reason=f"position:{position} overlay — {'; '.join(reason_parts)}",
                selector=selector,
                element_snippet=snippet[:300],
            ))

        return candidates

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _match_ad_domain(self, hostname: str) -> Optional[str]:
        """Return the matched ad domain string, or None."""
        hostname = hostname.lower()
        for domain in self.ad_domains:
            if hostname == domain or hostname.endswith("." + domain):
                return domain
        return None

    def _has_ad_path_keywords(self, urls: list) -> bool:
        """Check if any URLs in the list have ad-related path keywords."""
        for url in urls:
            url_lower = url.lower()
            for kw in AD_URL_PATH_KEYWORDS:
                if kw in url_lower:
                    return True
        return False

    @staticmethod
    def _deduplicate(candidates: List[AdCandidate]) -> List[AdCandidate]:
        """Remove duplicate candidates with the same suggested_rule."""
        seen: set = set()
        unique = []
        for c in candidates:
            key = c.suggested_rule
            if key not in seen:
                seen.add(key)
                unique.append(c)
        return unique


# ---------------------------------------------------------------------------
# Module-level convenience function (matches pipeline call convention)
# ---------------------------------------------------------------------------

def detect_ads(extracted_data: dict,
               custom_domains: Optional[List[str]] = None) -> dict:
    """
    Convenience wrapper used by crawler_service.py in the pipeline.

    Usage:
        from app.crawler.detector import detect_ads
        result = detect_ads(extractor_output)

    Returns a plain dict (JSON-serialisable) with ad_candidates and summary.
    """
    detector = AdDetector(custom_domains=custom_domains)
    return detector.detect(extracted_data).to_dict()


# ---------------------------------------------------------------------------
# Quick smoke-test (run: python -m app.crawler.detector)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    sample_data = {
        "url": "https://example-news.com",
        "scripts": [
            "https://example-news.com/main.js",
            "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js",
        ],
        "iframes": [
            "https://ad.doubleclick.net/ad.html",
        ],
        "ad_elements": [
            {
                "tag": "div",
                "selector": "div#div-gpt-ad-1234567890-0",
                "reason": "id 'div-gpt-ad-1234567890-0' matches ad pattern",
                "element_id": "div-gpt-ad-1234567890-0",
                "outer_html_snippet": '<div id="div-gpt-ad-1234567890-0">',
                "ad_attributes": {},
            },
            {
                "tag": "ins",
                "selector": "ins.adsbygoogle",
                "reason": "class 'adsbygoogle' matches ad pattern",
                "element_id": "",
                "outer_html_snippet": '<ins class="adsbygoogle" data-ad-slot="1234">',
                "ad_attributes": {"data-ad-slot": "1234", "data-ad-client": "ca-pub-xxx"},
            },
        ],
        "network_requests": [
            {"url": "https://example-news.com/main.js", "resource_type": "script"},
            {"url": "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js", "resource_type": "script"},
            {"url": "https://ad.doubleclick.net/ddm/activity", "resource_type": "xhr"},
            {"url": "https://static.cloudflareinsights.com/beacon.min.js", "resource_type": "script"},
        ],
    }

    result = detect_ads(sample_data)
    print(json.dumps(result, indent=2))
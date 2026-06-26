# Analyses crawl data and produces grouped, actionable ad candidates.
#
# Input: extracted page data + captured network requests
# Output: ad_candidates with suggested adblock rules, grouped by domain/element
#
# Improvements in this version:
# - Promote ad-like parent containers from parent_chain, e.g.
#     child:  div.banner-bottom-double
#     parent: div.adserver
#   => candidate: site.com##div.adserver
# - Generate narrow network rules for ad asset paths, e.g.
#     cdn.example.com/storage/ads/banner.png
#   => ||cdn.example.com/storage/ads/^$image,domain=site.com
# - Stop treating analytics-only domains like Google Analytics / GTM as visible
#   ad-blocking candidates.
# - Detect fullscreen popup overlays/backdrops from browser fixed_elements.
# - Avoid noisy header/nav candidates such as header.fly unless there are real
#   ad/overlay signals.
# - Prefer meaningful ad classes over generated classes like jsx-xxxxx.

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AdCandidate:
    """A single detected ad candidate — ready for rule generation."""
    category: str
    confidence: str
    suggested_rule: str
    reason: str
    domain: str = ""
    urls: List[str] = field(default_factory=list)
    selector: str = ""
    element_snippet: str = ""
    parent_chain: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = {
            "category": self.category,
            "confidence": self.confidence,
            "suggested_rule": self.suggested_rule,
            "reason": self.reason,
        }

        if self.domain:
            data["domain"] = self.domain
        if self.urls:
            data["urls"] = self.urls
        if self.selector:
            data["selector"] = self.selector
        if self.element_snippet:
            data["element_snippet"] = self.element_snippet
        if self.parent_chain:
            data["parent_chain"] = self.parent_chain

        return data


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
            "ad_candidates": [candidate.to_dict() for candidate in self.ad_candidates],
            "has_ads": self.has_ads,
            "summary": self.summary,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Domain / selector knowledge
# ---------------------------------------------------------------------------

AD_NETWORK_DOMAINS = [
    "doubleclick.net",
    "googlesyndication.com",
    "googletagservices.com",
    "googleadservices.com",
    "adservice.google.com",
    "adnxs.com",
    "moatads.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "criteo.net",
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
    "mgid.com",
    "popads.net",
    "propellerads.com",
    "exoclick.com",
    "trafficjunky.net",
    "juicyads.com",
    "adskeeper.com",
]

TRACKING_ONLY_DOMAINS = [
    "google-analytics.com",
    "www.google-analytics.com",
    "googletagmanager.com",
    "www.googletagmanager.com",
    "static.cloudflareinsights.com",
    "facebook.net",
    "hotjar.com",
    "mouseflow.com",
    "clarity.ms",
    "segment.io",
    "mixpanel.com",
    "amplitude.com",
]

SAFE_DOMAINS = [
    "cloudflareinsights.com",
    "cloudflare.com",
    "googleapis.com",
    "gstatic.com",
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

AD_URL_PATH_KEYWORDS = [
    "/ad/",
    "/ads/",
    "/advert/",
    "/advertise/",
    "/advertisement/",
    "/banner/",
    "/banners/",
    "/popup/",
    "/popunder/",
    "/sponsor/",
    "/sponsored/",
    "/promo/",
    "/promotion/",
    "/storage/ads/",
    "ads%20",
    "adserver",
    "adservice",
    "adunit",
    "adslot",
    "ad-slot",
    "pagead",
    "show_ads",
    "ad_iframe",
    "prebid",
    "dfp",
    "gpt",
    "adsense",
    "interstitial",
    "popup",
    "popunder",
    "casino",
    "betting",
    "affiliate",
]

NARROW_AD_PATH_PREFIXES = [
    "/storage/ads/",
    "/ads/",
    "/ad/",
    "/advert/",
    "/advertise/",
    "/advertisement/",
    "/banner/",
    "/banners/",
    "/popup/",
    "/popunder/",
    "/sponsor/",
    "/sponsored/",
    "/promo/",
    "/promotion/",
]

AD_CLASS_ID_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"(^|[-_])ad($|[-_])",
        r"(^|[-_])ads($|[-_])",
        r"adserver",
        r"ad-server",
        r"ad_container",
        r"ad-container",
        r"adslot",
        r"ad-slot",
        r"adunit",
        r"ad-unit",
        r"adsbygoogle",
        r"gpt-ad",
        r"banner",
        r"popup",
        r"popunder",
        r"interstitial",
        r"sponsor",
        r"sponsored",
        r"promo",
        r"advert",
        r"advertise",
        r"advertisement",
        r"sticky_ads",
        r"floating_ad",
        r"float-ad",
    ]
]

OVERLAY_CLASS_ID_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"overlay",
        r"backdrop",
        r"modal",
        r"popup",
        r"dialog",
        r"mask",
        r"layer",
        r"interstitial",
    ]
]

SITE_CHROME_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"header",
        r"navbar",
        r"navigation",
        r"nav-",
        r"menu",
        r"search",
        r"footer",
        r"breadcrumb",
    ]
]

GENERATED_CLASS_PREFIXES = (
    "jsx-",
    "css-",
    "sc-",
    "style-",
    "chakra-",
    "mantine-",
    "ant-",
)


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
        "ad_elements": [...],
        "network_requests": [...],
        "fixed_elements": [...],
    }
    """

    def __init__(self, custom_domains: Optional[List[str]] = None):
        self.ad_domains = AD_NETWORK_DOMAINS + (custom_domains or [])

    def detect(self, extracted_data: dict) -> DetectionResult:
        url = extracted_data.get("url", "")
        network_requests = extracted_data.get("network_requests", [])
        ad_elements = extracted_data.get("ad_elements", [])
        scripts = extracted_data.get("scripts", [])
        iframes = extracted_data.get("iframes", [])
        fixed_elements = extracted_data.get("fixed_elements", [])

        errors: List[str] = []
        candidates: List[AdCandidate] = []

        page_domain = _hostname(url)

        try:
            candidates += self._analyse_network_requests(
                requests=network_requests,
                page_domain=page_domain,
                page_url=url,
            )
        except Exception as exc:
            errors.append(f"network_analysis_error: {exc}")

        try:
            candidates += self._analyse_ad_elements(
                ad_elements=ad_elements,
                page_url=url,
            )
        except Exception as exc:
            errors.append(f"ad_element_analysis_error: {exc}")

        try:
            candidates += self._analyse_script_urls(
                scripts=scripts,
                page_domain=page_domain,
                page_url=url,
            )
        except Exception as exc:
            errors.append(f"script_analysis_error: {exc}")

        try:
            candidates += self._analyse_iframe_urls(
                iframes=iframes,
                page_domain=page_domain,
                page_url=url,
            )
        except Exception as exc:
            errors.append(f"iframe_analysis_error: {exc}")

        try:
            candidates += self._analyse_fixed_elements(
                fixed_elements=fixed_elements,
                page_domain=page_domain,
                page_url=url,
            )
        except Exception as exc:
            errors.append(f"fixed_element_analysis_error: {exc}")

        candidates = self._deduplicate(candidates)

        confidence_breakdown = {"high": 0, "medium": 0, "low": 0}
        for candidate in candidates:
            confidence_breakdown[candidate.confidence] = (
                confidence_breakdown.get(candidate.confidence, 0) + 1
            )

        ad_networks = sorted(
            {
                candidate.domain
                for candidate in candidates
                if candidate.domain
            }
        )

        summary = {
            "ad_networks_found": ad_networks,
            "ad_containers_found": sum(
                1
                for candidate in candidates
                if candidate.category in {
                    "ad_container",
                    "floating_ad",
                    "popup_overlay",
                }
            ),
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
    # Network analysis
    # ------------------------------------------------------------------

    def _analyse_network_requests(
        self,
        requests: list,
        page_domain: str,
        page_url: str,
    ) -> List[AdCandidate]:
        candidates: List[AdCandidate] = []
        clean_page_domain = _clean_domain(page_domain)

        domain_requests: Dict[str, List[Dict[str, str]]] = defaultdict(list)

        for req in requests or []:
            req_url = _get_field(req, "url", "")
            resource_type = _get_field(req, "resource_type", "other")

            if not req_url or str(req_url).startswith("data:"):
                continue

            req_host = _hostname(req_url)
            if not req_host:
                continue

            if _is_first_party(req_host=req_host, page_domain=page_domain):
                continue

            if _host_in_domains(req_host, SAFE_DOMAINS):
                continue

            domain_requests[req_host].append(
                {
                    "url": str(req_url),
                    "resource_type": str(resource_type or "other").lower(),
                }
            )

        for domain, items in sorted(domain_requests.items()):
            urls = [item["url"] for item in items]

            # Do not produce visible-ad candidates for analytics-only traffic.
            if _host_in_domains(domain, TRACKING_ONLY_DOMAINS):
                if not self._has_ad_path_keywords(urls):
                    continue

            narrow_rule, matched_urls = self._build_narrow_ad_path_rule(
                domain=domain,
                items=items,
                page_domain=clean_page_domain,
            )

            if narrow_rule:
                candidates.append(
                    AdCandidate(
                        category="ad_network_request",
                        confidence="high",
                        suggested_rule=narrow_rule,
                        reason=(
                            "Network requests contain a narrow ad asset path "
                            f"on '{domain}' ({len(matched_urls)} matched request(s))"
                        ),
                        domain=domain,
                        urls=matched_urls[:5],
                    )
                )
                continue

            ad_match = self._match_ad_domain(domain)
            if ad_match:
                candidates.append(
                    AdCandidate(
                        category="ad_network_request",
                        confidence="high",
                        suggested_rule=f"||{ad_match}^",
                        reason=(
                            f"Requests to known ad network '{ad_match}' "
                            f"({len(urls)} requests)"
                        ),
                        domain=ad_match,
                        urls=urls[:5],
                    )
                )
                continue

            if self._has_ad_path_keywords(urls):
                suggested_rule = f"||{domain}^$third-party"
                if clean_page_domain:
                    suggested_rule += f",domain={clean_page_domain}"

                candidates.append(
                    AdCandidate(
                        category="ad_network_request",
                        confidence="medium",
                        suggested_rule=suggested_rule,
                        reason=(
                            "URL paths contain ad-related keywords "
                            f"({len(urls)} requests)"
                        ),
                        domain=domain,
                        urls=urls[:5],
                    )
                )

        return candidates

    def _build_narrow_ad_path_rule(
        self,
        domain: str,
        items: List[Dict[str, str]],
        page_domain: str,
    ) -> Tuple[str, List[str]]:
        """
        Build a narrow ABP network rule for ad asset paths.

        Example:
            https://cdn.rophim.co.com/storage/ads/foo.png
        becomes:
            ||cdn.rophim.co.com/storage/ads/^$image,domain=rophim10.live
        """
        best_prefix = ""
        matched_urls: List[str] = []
        matched_resource_types: set[str] = set()

        for item in items:
            req_url = item.get("url", "")
            parsed = urlparse(req_url)
            path = (parsed.path or "").lower()

            matched_prefix = ""

            for prefix in NARROW_AD_PATH_PREFIXES:
                if prefix in path:
                    matched_prefix = prefix
                    break

            if not matched_prefix:
                continue

            if not best_prefix:
                best_prefix = matched_prefix

            if matched_prefix == best_prefix:
                matched_urls.append(req_url)
                matched_resource_types.add(
                    str(item.get("resource_type", "other")).lower()
                )

        if not best_prefix or not matched_urls:
            return "", []

        options: List[str] = []

        if "image" in matched_resource_types:
            options.append("image")
        elif "script" in matched_resource_types:
            options.append("script")
        elif "media" in matched_resource_types:
            options.append("media")

        if page_domain:
            options.append(f"domain={page_domain}")

        options_text = ""
        if options:
            options_text = "$" + ",".join(options)

        return f"||{domain}{best_prefix}^{options_text}", matched_urls

    # ------------------------------------------------------------------
    # DOM element analysis
    # ------------------------------------------------------------------

    def _analyse_ad_elements(
        self,
        ad_elements: list,
        page_url: str,
    ) -> List[AdCandidate]:
        candidates: List[AdCandidate] = []
        page_domain = _clean_domain(_hostname(page_url))

        for elem in ad_elements or []:
            selector = _get_field(elem, "selector", "")
            reason = _get_field(elem, "reason", "")
            element_id = _get_field(elem, "element_id", "")
            snippet = _get_field(elem, "outer_html_snippet", "")
            ad_attrs = _get_field(elem, "ad_attributes", {}) or {}
            parent_chain = _get_field(elem, "parent_chain", []) or []

            if not selector:
                continue

            candidates += self._promote_parent_ad_containers(
                page_domain=page_domain,
                parent_chain=parent_chain,
                child_selector=str(selector),
                child_reason=str(reason),
            )

            confidence = "medium"

            if ad_attrs:
                confidence = "high"

            if element_id and re.search(r"gpt-ad|adsense|adsbygoogle", str(element_id), re.I):
                confidence = "high"

            if element_id:
                suggested_rule = f"{page_domain}###{element_id}"
            else:
                suggested_rule = f"{page_domain}##{selector}"

            candidates.append(
                AdCandidate(
                    category="ad_container",
                    confidence=confidence,
                    suggested_rule=suggested_rule,
                    reason=str(reason),
                    selector=str(selector),
                    element_snippet=str(snippet),
                    parent_chain=parent_chain if isinstance(parent_chain, list) else [],
                )
            )

        return candidates

    def _promote_parent_ad_containers(
        self,
        page_domain: str,
        parent_chain: Any,
        child_selector: str,
        child_reason: str,
    ) -> List[AdCandidate]:
        candidates: List[AdCandidate] = []

        if not isinstance(parent_chain, list):
            return candidates

        for parent in parent_chain:
            if not isinstance(parent, dict):
                continue

            tag = str(parent.get("tag", "div") or "div").lower()
            parent_id = str(parent.get("id", "") or "")
            classes = parent.get("classes", [])

            if isinstance(classes, str):
                class_list = [item for item in classes.split() if item]
            elif isinstance(classes, list):
                class_list = [str(item) for item in classes if str(item)]
            else:
                class_list = []

            selector = ""
            reason_signal = ""

            if parent_id and _is_ad_like_token(parent_id):
                selector = f"{tag}#{parent_id}"
                reason_signal = f"parent id '{parent_id}' matches ad pattern"
            else:
                best_class = _best_ad_like_class(class_list)
                if best_class:
                    selector = f"{tag}.{best_class}"
                    reason_signal = f"parent class '{best_class}' matches ad pattern"

            if not selector:
                continue

            candidates.append(
                AdCandidate(
                    category="ad_container",
                    confidence="high",
                    suggested_rule=f"{page_domain}##{selector}",
                    reason=(
                        f"{reason_signal}; wraps detected ad element "
                        f"'{child_selector}'. Original reason: {child_reason}"
                    ),
                    selector=selector,
                    parent_chain=parent_chain,
                )
            )

            break

        return candidates

    # ------------------------------------------------------------------
    # Script / iframe analysis
    # ------------------------------------------------------------------

    def _analyse_script_urls(
        self,
        scripts: list,
        page_domain: str,
        page_url: str,
    ) -> List[AdCandidate]:
        candidates: List[AdCandidate] = []

        for src in scripts or []:
            if not isinstance(src, str) or not src.strip():
                continue

            host = _hostname(src)
            if not host:
                continue

            if _is_first_party(req_host=host, page_domain=page_domain):
                continue

            if _host_in_domains(host, SAFE_DOMAINS):
                continue

            if _host_in_domains(host, TRACKING_ONLY_DOMAINS):
                continue

            ad_match = self._match_ad_domain(host)
            if ad_match:
                candidates.append(
                    AdCandidate(
                        category="ad_network_request",
                        confidence="high",
                        suggested_rule=f"||{ad_match}^",
                        reason=f"Script from known ad network '{ad_match}'",
                        domain=ad_match,
                        urls=[src],
                    )
                )

        return candidates

    def _analyse_iframe_urls(
        self,
        iframes: list,
        page_domain: str,
        page_url: str,
    ) -> List[AdCandidate]:
        candidates: List[AdCandidate] = []
        clean_page_domain = _clean_domain(page_domain)

        for src in iframes or []:
            if not isinstance(src, str) or not src.strip():
                continue

            host = _hostname(src)
            if not host:
                continue

            if _is_first_party(req_host=host, page_domain=page_domain):
                continue

            if _host_in_domains(host, SAFE_DOMAINS):
                continue

            if _host_in_domains(host, TRACKING_ONLY_DOMAINS):
                continue

            ad_match = self._match_ad_domain(host)
            if ad_match:
                candidates.append(
                    AdCandidate(
                        category="ad_iframe",
                        confidence="high",
                        suggested_rule=f"||{ad_match}^",
                        reason=f"Iframe from known ad network '{ad_match}'",
                        domain=ad_match,
                        urls=[src],
                    )
                )
                continue

            if self._has_ad_path_keywords([src]):
                suggested_rule = f"||{host}^$subdocument"
                if clean_page_domain:
                    suggested_rule += f",domain={clean_page_domain}"

                candidates.append(
                    AdCandidate(
                        category="ad_iframe",
                        confidence="medium",
                        suggested_rule=suggested_rule,
                        reason="Iframe URL contains ad-related keywords",
                        domain=host,
                        urls=[src],
                    )
                )

        return candidates

    # ------------------------------------------------------------------
    # Fixed / sticky / overlay element analysis
    # ------------------------------------------------------------------

    def _analyse_fixed_elements(
        self,
        fixed_elements: list,
        page_domain: str,
        page_url: str,
    ) -> List[AdCandidate]:
        """
        Detect:
        - floating ad banners,
        - sticky ad containers,
        - fullscreen popup overlays/backdrops.

        These are invisible to BeautifulSoup because position/background/z-index
        are computed styles, not static HTML attributes.
        """
        candidates: List[AdCandidate] = []
        clean_domain = _clean_domain(page_domain)

        for el in fixed_elements or []:
            if not isinstance(el, dict):
                continue

            tag = str(el.get("tag", "div") or "div").lower()
            el_id = str(el.get("id", "") or "")
            el_classes = str(el.get("classes", "") or "")
            browser_selector = str(el.get("selector", "") or "")
            ext_links = el.get("ext_links", []) or []
            iframes = el.get("iframes", []) or []
            width = int(el.get("width", 0) or 0)
            height = int(el.get("height", 0) or 0)
            snippet = str(el.get("snippet", "") or "")
            position = str(el.get("position", "fixed") or "fixed")
            z_index = int(el.get("z_index", 0) or 0)
            viewport_coverage = float(el.get("viewport_coverage", 0.0) or 0.0)
            is_fullscreen_overlay = bool(el.get("is_fullscreen_overlay", False))
            is_dark_overlay = bool(el.get("is_dark_overlay", False))
            has_close_button = bool(el.get("has_close_button", False))
            overlay_keyword = bool(el.get("overlay_keyword", False))
            ad_keyword = bool(el.get("ad_keyword", False))
            site_chrome = bool(el.get("site_chrome", False))

            external_domains = []
            for href in ext_links:
                host = _hostname(str(href))
                if host and not _is_first_party(req_host=host, page_domain=page_domain):
                    external_domains.append(host)

            has_ad_iframe = False
            for src in iframes:
                host = _hostname(str(src))
                if host and not _is_first_party(req_host=host, page_domain=page_domain):
                    has_ad_iframe = True
                    break

            attrs_text = f"{tag} {el_id} {el_classes} {browser_selector}"
            class_or_id_ad_match = _is_ad_like_token(attrs_text)
            class_or_id_overlay_match = _is_overlay_like_token(attrs_text)
            site_chrome_match = site_chrome or _is_site_chrome_token(attrs_text)

            # Avoid noisy fixed headers/nav bars like header.fly.
            # Only keep them if they also have real ad/overlay evidence.
            if (
                site_chrome_match
                and not is_fullscreen_overlay
                and not class_or_id_ad_match
                and not class_or_id_overlay_match
                and not external_domains
                and not has_ad_iframe
            ):
                continue

            # First priority: page-blocking overlay/backdrop.
            if self._looks_like_popup_overlay(
                is_fullscreen_overlay=is_fullscreen_overlay,
                is_dark_overlay=is_dark_overlay,
                has_close_button=has_close_button,
                overlay_keyword=overlay_keyword or class_or_id_overlay_match,
                z_index=z_index,
                viewport_coverage=viewport_coverage,
            ):
                selector = self._selector_from_fixed_element(
                    el=el,
                    tag=tag,
                    element_id=el_id,
                    classes=el_classes,
                    browser_selector=browser_selector,
                    prefer_overlay=True,
                )

                if not selector:
                    continue

                reason_parts = [
                    f"fullscreen/blocking overlay coverage={viewport_coverage:.2f}",
                    f"position:{position}",
                ]

                if is_dark_overlay:
                    reason_parts.append("dark backdrop")
                if has_close_button:
                    reason_parts.append("contains close button")
                if overlay_keyword or class_or_id_overlay_match:
                    reason_parts.append("class/id matches overlay pattern")
                if z_index:
                    reason_parts.append(f"z-index={z_index}")

                candidates.append(
                    AdCandidate(
                        category="popup_overlay",
                        confidence="high",
                        suggested_rule=f"{clean_domain}##{selector}",
                        reason="; ".join(reason_parts),
                        selector=selector,
                        element_snippet=snippet[:500],
                    )
                )
                continue

            # Second priority: fixed/sticky floating ad.
            is_banner_shape = width >= 400 and 20 <= height <= 240

            signals = sum(
                [
                    bool(external_domains),
                    class_or_id_ad_match,
                    has_ad_iframe,
                    is_banner_shape,
                ]
            )

            if signals < 1:
                continue

            selector = self._selector_from_fixed_element(
                el=el,
                tag=tag,
                element_id=el_id,
                classes=el_classes,
                browser_selector=browser_selector,
                prefer_overlay=False,
            )

            if not selector:
                continue

            confidence = "high" if signals >= 2 else "medium"

            reason_parts = []
            if external_domains:
                reason_parts.append(
                    f"links to external domains: {', '.join(external_domains[:3])}"
                )
            if class_or_id_ad_match:
                reason_parts.append("class/id matches ad pattern")
            if has_ad_iframe:
                reason_parts.append("contains third-party iframe")
            if is_banner_shape:
                reason_parts.append(f"banner proportions ({width}×{height}px)")
            if z_index:
                reason_parts.append(f"z-index={z_index}")

            candidates.append(
                AdCandidate(
                    category="floating_ad",
                    confidence=confidence,
                    suggested_rule=f"{clean_domain}##{selector}",
                    reason=f"position:{position} floating element — {'; '.join(reason_parts)}",
                    selector=selector,
                    element_snippet=snippet[:500],
                )
            )

        return candidates

    def _looks_like_popup_overlay(
        self,
        is_fullscreen_overlay: bool,
        is_dark_overlay: bool,
        has_close_button: bool,
        overlay_keyword: bool,
        z_index: int,
        viewport_coverage: float,
    ) -> bool:
        if not is_fullscreen_overlay:
            return False

        strong_signals = sum(
            [
                is_dark_overlay,
                has_close_button,
                overlay_keyword,
                z_index >= 10,
                viewport_coverage >= 0.75,
            ]
        )

        return strong_signals >= 2

    def _selector_from_fixed_element(
        self,
        el: Dict[str, Any],
        tag: str,
        element_id: str,
        classes: str,
        browser_selector: str,
        prefer_overlay: bool,
    ) -> str:
        if element_id and _is_safe_css_identifier(element_id):
            return f"{tag}#{element_id}"

        class_list = [
            item
            for item in str(classes or "").split()
            if item
        ]

        best_class = ""

        if prefer_overlay:
            best_class = _best_overlay_like_class(class_list)

        if not best_class:
            best_class = _best_ad_like_class(class_list)

        if not best_class:
            best_class = _first_meaningful_class(class_list)

        if best_class:
            return f"{tag}.{best_class}"

        # Browser already built a bounded structural selector such as:
        #   body > div:nth-of-type(2) > div:nth-of-type(1)
        # This is better than broad div[style*='position:fixed'] rules.
        if browser_selector and not _is_too_broad_selector(browser_selector):
            return browser_selector

        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _match_ad_domain(self, hostname: str) -> Optional[str]:
        hostname = str(hostname or "").lower().strip(".")

        for domain in self.ad_domains:
            if _host_matches_domain(hostname, domain):
                return domain

        return None

    def _has_ad_path_keywords(self, urls: list) -> bool:
        for url in urls or []:
            text = str(url or "").lower()
            for keyword in AD_URL_PATH_KEYWORDS:
                if keyword in text:
                    return True

        return False

    @staticmethod
    def _deduplicate(candidates: List[AdCandidate]) -> List[AdCandidate]:
        """
        Remove duplicate candidates with the same suggested rule.

        Higher confidence and more important categories are kept first.
        """
        category_rank = {
            "popup_overlay": 0,
            "ad_container": 1,
            "floating_ad": 2,
            "ad_iframe": 3,
            "ad_network_request": 4,
            "tracking_script": 5,
        }

        confidence_rank = {
            "high": 0,
            "medium": 1,
            "low": 2,
        }

        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (
                category_rank.get(candidate.category, 9),
                confidence_rank.get(candidate.confidence, 9),
                candidate.suggested_rule,
            ),
        )

        seen: set[str] = set()
        unique: List[AdCandidate] = []

        for candidate in sorted_candidates:
            key = candidate.suggested_rule
            if key in seen:
                continue

            seen.add(key)
            unique.append(candidate)

        return unique


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)

    return getattr(obj, key, default)


def _hostname(url: str) -> str:
    try:
        return urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""


def _clean_domain(domain: str) -> str:
    domain = str(domain or "").lower().strip(".")

    if domain.startswith("www."):
        return domain[4:]

    return domain


def _host_matches_domain(host: str, domain: str) -> bool:
    host = str(host or "").lower().strip(".")
    domain = str(domain or "").lower().strip(".")

    if not host or not domain:
        return False

    return host == domain or host.endswith("." + domain)


def _host_in_domains(host: str, domains: List[str]) -> bool:
    return any(_host_matches_domain(host, domain) for domain in domains)


def _is_first_party(req_host: str, page_domain: str) -> bool:
    req_host = str(req_host or "").lower().strip(".")
    page_domain = str(page_domain or "").lower().strip(".")

    if not req_host or not page_domain:
        return False

    return (
        req_host == page_domain
        or req_host.endswith("." + page_domain)
        or page_domain.endswith("." + req_host)
    )


def _is_ad_like_token(text: str) -> bool:
    value = str(text or "")

    if not value:
        return False

    return any(pattern.search(value) for pattern in AD_CLASS_ID_PATTERNS)


def _is_overlay_like_token(text: str) -> bool:
    value = str(text or "")

    if not value:
        return False

    return any(pattern.search(value) for pattern in OVERLAY_CLASS_ID_PATTERNS)


def _is_site_chrome_token(text: str) -> bool:
    value = str(text or "")

    if not value:
        return False

    return any(pattern.search(value) for pattern in SITE_CHROME_PATTERNS)


def _is_safe_css_identifier(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z_-][A-Za-z0-9_-]*$", str(value or "")))


def _first_meaningful_class(classes: List[str]) -> str:
    for cls in classes or []:
        cls = str(cls or "").strip()

        if not cls:
            continue

        if not _is_safe_css_identifier(cls):
            continue

        if cls.startswith(GENERATED_CLASS_PREFIXES):
            continue

        return cls

    return ""


def _best_ad_like_class(classes: List[str]) -> str:
    for cls in classes or []:
        cls = str(cls or "").strip()

        if not cls:
            continue

        if not _is_safe_css_identifier(cls):
            continue

        if cls.startswith(GENERATED_CLASS_PREFIXES):
            continue

        if _is_ad_like_token(cls):
            return cls

    return ""


def _best_overlay_like_class(classes: List[str]) -> str:
    for cls in classes or []:
        cls = str(cls or "").strip()

        if not cls:
            continue

        if not _is_safe_css_identifier(cls):
            continue

        if cls.startswith(GENERATED_CLASS_PREFIXES):
            continue

        if _is_overlay_like_token(cls):
            return cls

    return ""


def _is_too_broad_selector(selector: str) -> bool:
    value = str(selector or "").strip().lower()

    return value in {
        "",
        "html",
        "body",
        "div",
        "span",
        "section",
        "main",
        "header",
        "footer",
        "nav",
    }


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

def detect_ads(
    extracted_data: dict,
    custom_domains: Optional[List[str]] = None,
) -> dict:
    """
    Convenience wrapper used by crawler_service.py in the pipeline.

    Usage:
        from app.crawler.detector import detect_ads
        result = detect_ads(extractor_output)

    Returns a plain dict with ad_candidates and summary.
    """
    detector = AdDetector(custom_domains=custom_domains)
    return detector.detect(extracted_data).to_dict()
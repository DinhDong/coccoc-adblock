from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup


DOMESTIC = "domestic"
FOREIGN = "foreign"


VIETNAMESE_WORDS = {
    "và",
    "của",
    "các",
    "cho",
    "trong",
    "với",
    "được",
    "không",
    "một",
    "những",
    "người",
    "này",
    "đã",
    "từ",
    "khi",
    "về",
    "đến",
    "tại",
    "trên",
    "theo",
    "việt",
    "nam",
}

VIETNAMESE_DIACRITICS_RE = re.compile(
    r"[ăâđêôơư"
    r"àáạảãằắặẳẵầấậẩẫèéẹẻẽềếệểễ"
    r"ìíịỉĩòóọỏõồốộổỗờớợởỡ"
    r"ùúụủũừứựửữỳýỵỷỹ"
    r"ĂÂĐÊÔƠƯ"
    r"ÀÁẠẢÃẰẮẶẲẴẦẤẬẨẪÈÉẸẺẼỀẾỆỂỄ"
    r"ÌÍỊỈĨÒÓỌỎÕỒỐỘỔỖỜỚỢỞỠ"
    r"ÙÚỤỦŨỪỨỰỬỮỲÝỴỶỸ]"
)

WORD_RE = re.compile(r"\b[\wÀ-ỹ]+\b", re.UNICODE)


@dataclass(frozen=True)
class DomainClassification:
    """
    Domain eligibility decision.

    Final company-facing classification is binary: domestic or foreign.

    Before a non-.vn page has been rendered, classification can temporarily be
    None with requires_page_evidence=True. That is an internal crawler state,
    not a third domain classification, and is never persisted as a final result.
    """

    hostname: str
    classification: Optional[str]
    eligible: bool
    score: int
    reasons: List[str]
    requires_page_evidence: bool = False
    valid_url: bool = True

    def to_dict(self) -> dict:
        data = asdict(self)

        # Do not expose a third/null classification value. Pending and invalid
        # states are represented by their dedicated flags instead.
        if self.classification is None:
            data.pop("classification", None)

        return data


def _hostname_from_url(url: str) -> str:
    value = (url or "").strip()

    if not value:
        return ""

    if "://" not in value:
        value = f"https://{value}"

    try:
        return (urlparse(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _domains_from_env(name: str) -> set[str]:
    raw = os.getenv(name, "")

    return {
        value.strip().lower().rstrip(".")
        for value in raw.split(",")
        if value.strip()
    }


def _matches_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _matches_any(hostname: str, domains: set[str]) -> bool:
    return any(
        _matches_domain(hostname, domain)
        for domain in domains
    )


def _extract_html_signals(html: str) -> tuple[str, str, str]:
    if not html:
        return "", "", ""

    soup = BeautifulSoup(html, "html.parser")

    html_tag = soup.find("html")
    html_lang = ""

    if html_tag:
        html_lang = str(html_tag.get("lang", "")).strip().lower()

    locale = ""

    for meta in soup.find_all("meta"):
        key = str(
            meta.get("property")
            or meta.get("name")
            or meta.get("http-equiv")
            or ""
        ).strip().lower()

        if key in {
            "og:locale",
            "language",
            "content-language",
        }:
            locale = str(meta.get("content", "")).strip().lower()
            if locale:
                break

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)
    text = text[:200_000]

    return html_lang, locale, text


def classify_domain(
    url: str,
    html: Optional[str] = None,
) -> DomainClassification:
    """
    Classify a valid website as DOMESTIC or FOREIGN.

    html=None means the caller is doing the pre-render URL check. A non-.vn
    domain with no decisive override then requests page evidence instead of
    receiving a final classification.

    html="" (or any rendered HTML string) means the caller is asking for a final
    decision. At that point every valid site is classified as either DOMESTIC or
    FOREIGN. There is intentionally no third final classification.
    """
    hostname = _hostname_from_url(url)

    if not hostname:
        return DomainClassification(
            hostname="",
            classification=None,
            eligible=False,
            score=0,
            reasons=["URL does not contain a valid hostname"],
            requires_page_evidence=False,
            valid_url=False,
        )

    domestic_overrides = _domains_from_env(
        "DOMESTIC_DOMAIN_OVERRIDES"
    )
    foreign_overrides = _domains_from_env(
        "FOREIGN_DOMAIN_OVERRIDES"
    )

    # Foreign override wins if a domain is accidentally present in both lists.
    if _matches_any(hostname, foreign_overrides):
        return DomainClassification(
            hostname=hostname,
            classification=FOREIGN,
            eligible=False,
            score=-100,
            reasons=["Matched foreign domain override"],
        )

    if _matches_any(hostname, domestic_overrides):
        return DomainClassification(
            hostname=hostname,
            classification=DOMESTIC,
            eligible=True,
            score=100,
            reasons=["Matched domestic domain override"],
        )

    if hostname == "vn" or hostname.endswith(".vn"):
        return DomainClassification(
            hostname=hostname,
            classification=DOMESTIC,
            eligible=True,
            score=100,
            reasons=["Hostname uses the .vn country-code domain"],
        )

    # Pre-render check for non-.vn domains. This is not a classification; it is
    # only an instruction to the crawler to render the page before deciding.
    if html is None:
        return DomainClassification(
            hostname=hostname,
            classification=None,
            eligible=False,
            score=0,
            reasons=["Page evidence is required for non-.vn domains"],
            requires_page_evidence=True,
        )

    html_lang, locale, text = _extract_html_signals(html)

    score = 0
    reasons: List[str] = []

    if html_lang.startswith("vi"):
        score += 4
        reasons.append(
            f"HTML language is Vietnamese ({html_lang})"
        )
    elif html_lang:
        score -= 2
        reasons.append(
            f"HTML language is not Vietnamese ({html_lang})"
        )

    normalized_locale = locale.replace("-", "_")

    if normalized_locale.startswith("vi"):
        score += 4
        reasons.append(
            f"Page locale is Vietnamese ({locale})"
        )
    elif locale:
        score -= 2
        reasons.append(
            f"Page locale is not Vietnamese ({locale})"
        )

    text_lower = text.lower()

    diacritic_count = len(
        VIETNAMESE_DIACRITICS_RE.findall(text)
    )

    if diacritic_count >= 30:
        score += 3
        reasons.append(
            f"Strong Vietnamese text evidence "
            f"({diacritic_count} diacritic characters)"
        )
    elif diacritic_count >= 8:
        score += 2
        reasons.append(
            f"Vietnamese text evidence "
            f"({diacritic_count} diacritic characters)"
        )

    words = WORD_RE.findall(text_lower)

    vietnamese_word_count = sum(
        1
        for word in words
        if word in VIETNAMESE_WORDS
    )

    if vietnamese_word_count >= 20:
        score += 3
        reasons.append(
            f"Strong Vietnamese vocabulary evidence "
            f"({vietnamese_word_count} common words)"
        )
    elif vietnamese_word_count >= 8:
        score += 2
        reasons.append(
            f"Vietnamese vocabulary evidence "
            f"({vietnamese_word_count} common words)"
        )
    elif vietnamese_word_count >= 3:
        score += 1
        reasons.append(
            f"Weak Vietnamese vocabulary evidence "
            f"({vietnamese_word_count} common words)"
        )

    vietnam_markers = 0

    for marker in (
        "việt nam",
        "viet nam",
        "hà nội",
        "ha noi",
        "hồ chí minh",
        "ho chi minh",
        "đà nẵng",
        "da nang",
        "₫",
        " vnd",
        "+84",
    ):
        if marker in text_lower:
            vietnam_markers += 1

    if vietnam_markers >= 2:
        score += 2
        reasons.append(
            f"Found {vietnam_markers} Vietnam-specific markers"
        )
    elif vietnam_markers == 1:
        score += 1
        reasons.append(
            "Found one Vietnam-specific marker"
        )

    # Strong Vietnamese body content is allowed to override incorrect metadata.
    # Some domestic sites keep lang="en" or en_US by mistake.
    strong_vietnamese_content = (
        diacritic_count >= 30
        and vietnamese_word_count >= 20
    )

    if strong_vietnamese_content or score >= 4:
        classification = DOMESTIC
        eligible = True

        if strong_vietnamese_content and score < 4:
            reasons.append(
                "Strong Vietnamese content overrides conflicting page metadata"
            )
    else:
        # Company policy is binary. If a valid rendered site cannot be verified
        # as domestic, it is classified as foreign and is not processed.
        classification = FOREIGN
        eligible = False

        if not reasons:
            reasons.append(
                "No sufficient evidence that the website is domestic"
            )
        elif score > -4:
            reasons.append(
                "Evidence is insufficient to classify the website as domestic"
            )

    return DomainClassification(
        hostname=hostname,
        classification=classification,
        eligible=eligible,
        score=score,
        reasons=reasons,
    )

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import List
from urllib.parse import urlparse

from bs4 import BeautifulSoup


DOMESTIC = "domestic"
FOREIGN = "foreign"
UNKNOWN = "unknown"
INVALID = "invalid"


# Common Vietnamese words used only as supporting evidence.
# A few matching words alone are NOT enough to mark a site as domestic.
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


# Vietnamese-specific letters and diacritics.
VIETNAMESE_DIACRITICS_RE = re.compile(
    r"[ăâđêôơư"
    r"àáạảãằắặẳẵầấậẩẫ"
    r"èéẹẻẽềếệểễ"
    r"ìíịỉĩ"
    r"òóọỏõồốộổỗờớợởỡ"
    r"ùúụủũừứựửữ"
    r"ỳýỵỷỹ"
    r"ĂÂĐÊÔƠƯ"
    r"ÀÁẠẢÃẰẮẶẲẴẦẤẬẨẪ"
    r"ÈÉẸẺẼỀẾỆỂỄ"
    r"ÌÍỊỈĨ"
    r"ÒÓỌỎÕỒỐỘỔỖỜỚỢỞỠ"
    r"ÙÚỤỦŨỪỨỰỬỮ"
    r"ỲÝỴỶỸ]"
)


WORD_RE = re.compile(
    r"\b[\wÀ-ỹ]+\b",
    re.UNICODE,
)


@dataclass(frozen=True)
class DomainClassification:
    hostname: str
    classification: str
    eligible: bool
    score: int
    reasons: List[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _hostname_from_url(url: str) -> str:
    """
    Extract a normalized hostname from a URL.

    Examples:
        https://example.com/page -> example.com
        example.vn -> example.vn
    """
    value = (url or "").strip()

    if not value:
        return ""

    if "://" not in value:
        value = f"https://{value}"

    try:
        return (
            urlparse(value).hostname
            or ""
        ).lower().rstrip(".")
    except ValueError:
        return ""


def _domains_from_env(name: str) -> set[str]:
    """
    Read comma-separated domains from an environment variable.

    Example:
        DOMESTIC_DOMAIN_OVERRIDES=example.com,news.vn
    """
    raw = os.getenv(name, "")

    return {
        value.strip().lower().rstrip(".")
        for value in raw.split(",")
        if value.strip()
    }


def _matches_domain(
    hostname: str,
    domain: str,
) -> bool:
    """
    Match both a domain and its subdomains.

    example.com matches:
        example.com
        news.example.com

    It does NOT match:
        fakeexample.com
    """
    return (
        hostname == domain
        or hostname.endswith(f".{domain}")
    )


def _matches_any(
    hostname: str,
    domains: set[str],
) -> bool:
    return any(
        _matches_domain(
            hostname,
            domain,
        )
        for domain in domains
    )


def _extract_html_signals(
    html: str,
) -> tuple[str, str, str]:
    """
    Extract language-related evidence from page HTML.

    Returns:
        html_lang:
            Value of <html lang="...">

        locale:
            og:locale, language, or content-language metadata

        text:
            Visible-ish page text with scripts/styles removed
    """
    if not html:
        return "", "", ""

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    html_tag = soup.find("html")
    html_lang = ""

    if html_tag:
        html_lang = str(
            html_tag.get("lang", "")
        ).strip().lower()

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
            locale = str(
                meta.get("content", "")
            ).strip().lower()

            if locale:
                break

    # Script/style text is not useful for
    # deciding the page language.
    for tag in soup(
        [
            "script",
            "style",
            "noscript",
        ]
    ):
        tag.decompose()

    text = soup.get_text(
        " ",
        strip=True,
    )

    # Avoid analysing an unnecessarily huge page.
    text = text[:200_000]

    return (
        html_lang,
        locale,
        text,
    )


def classify_domain(
    url: str,
    html: str = "",
) -> DomainClassification:
    """
    Classify a website as:

        domestic
        foreign
        unknown
        invalid

    Rules:

    1. Manual overrides have highest priority.
    2. .vn is considered a strong domestic signal.
    3. Non-.vn domains require page evidence.
    4. Vietnamese HTML metadata and content increase the score.
    5. Foreign metadata decreases the score.
    6. Strong Vietnamese content can override incorrect lang/locale
       metadata because some domestic websites may leave lang="en".
    7. Ambiguous sites are classified as UNKNOWN instead of guessing.

    Only DOMESTIC sites are eligible for further crawler processing.
    """

    hostname = _hostname_from_url(url)

    if not hostname:
        return DomainClassification(
            hostname="",
            classification=INVALID,
            eligible=False,
            score=0,
            reasons=[
                "URL does not contain a valid hostname",
            ],
        )

    domestic_overrides = _domains_from_env(
        "DOMESTIC_DOMAIN_OVERRIDES"
    )

    foreign_overrides = _domains_from_env(
        "FOREIGN_DOMAIN_OVERRIDES"
    )

    # Foreign override has priority in case
    # the same domain accidentally appears in both lists.
    if _matches_any(
        hostname,
        foreign_overrides,
    ):
        return DomainClassification(
            hostname=hostname,
            classification=FOREIGN,
            eligible=False,
            score=-100,
            reasons=[
                "Matched foreign domain override",
            ],
        )

    if _matches_any(
        hostname,
        domestic_overrides,
    ):
        return DomainClassification(
            hostname=hostname,
            classification=DOMESTIC,
            eligible=True,
            score=100,
            reasons=[
                "Matched domestic domain override",
            ],
        )

    # .vn is a deterministic strong signal.
    if (
        hostname == "vn"
        or hostname.endswith(".vn")
    ):
        return DomainClassification(
            hostname=hostname,
            classification=DOMESTIC,
            eligible=True,
            score=100,
            reasons=[
                "Hostname uses the .vn country-code domain",
            ],
        )

    # A .com/.net/etc. domain cannot be judged
    # from the hostname alone.
    if not html:
        return DomainClassification(
            hostname=hostname,
            classification=UNKNOWN,
            eligible=False,
            score=0,
            reasons=[
                "Non-.vn domain requires page evidence",
            ],
        )

    (
        html_lang,
        locale,
        text,
    ) = _extract_html_signals(html)

    score = 0
    reasons: List[str] = []

    # ---------------------------------------------------------
    # HTML language metadata
    # ---------------------------------------------------------

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

    # ---------------------------------------------------------
    # Locale metadata
    # ---------------------------------------------------------

    normalized_locale = (
        locale.replace("-", "_")
    )

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

    # ---------------------------------------------------------
    # Vietnamese characters
    # ---------------------------------------------------------

    diacritic_count = len(
        VIETNAMESE_DIACRITICS_RE.findall(
            text
        )
    )

    if diacritic_count >= 30:
        score += 3

        reasons.append(
            "Strong Vietnamese text evidence "
            f"({diacritic_count} diacritic characters)"
        )

    elif diacritic_count >= 8:
        score += 2

        reasons.append(
            "Vietnamese text evidence "
            f"({diacritic_count} diacritic characters)"
        )

    # ---------------------------------------------------------
    # Vietnamese vocabulary
    # ---------------------------------------------------------

    text_lower = text.lower()

    words = WORD_RE.findall(
        text_lower
    )

    vietnamese_word_count = sum(
        1
        for word in words
        if word in VIETNAMESE_WORDS
    )

    if vietnamese_word_count >= 20:
        score += 3

        reasons.append(
            "Strong Vietnamese vocabulary evidence "
            f"({vietnamese_word_count} common words)"
        )

    elif vietnamese_word_count >= 8:
        score += 2

        reasons.append(
            "Vietnamese vocabulary evidence "
            f"({vietnamese_word_count} common words)"
        )

    elif vietnamese_word_count >= 3:
        score += 1

        reasons.append(
            "Weak Vietnamese vocabulary evidence "
            f"({vietnamese_word_count} common words)"
        )

    # ---------------------------------------------------------
    # Vietnam-specific supporting markers
    # ---------------------------------------------------------

    vietnam_markers = 0

    markers = (
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
    )

    for marker in markers:
        if marker in text_lower:
            vietnam_markers += 1

    if vietnam_markers >= 2:
        score += 2

        reasons.append(
            f"Found {vietnam_markers} "
            "Vietnam-specific markers"
        )

    elif vietnam_markers == 1:
        score += 1

        reasons.append(
            "Found one Vietnam-specific marker"
        )

    # ---------------------------------------------------------
    # Final classification
    # ---------------------------------------------------------

    # Some real domestic sites may accidentally keep:
    #
    #   <html lang="en">
    #   og:locale=en_US
    #
    # Strong Vietnamese content therefore has permission
    # to override conflicting metadata.
    strong_vietnamese_content = (
        diacritic_count >= 30
        and vietnamese_word_count >= 20
    )

    if strong_vietnamese_content:
        classification = DOMESTIC
        eligible = True

        reasons.append(
            "Strong Vietnamese content overrides "
            "potentially incorrect page language metadata"
        )

    elif score >= 4:
        classification = DOMESTIC
        eligible = True

    # Only classify as FOREIGN when:
    #
    # - negative evidence is strong
    # - there is almost no Vietnamese content
    #
    # Otherwise use UNKNOWN instead of guessing.
    elif (
        score <= -4
        and diacritic_count < 8
        and vietnamese_word_count < 3
    ):
        classification = FOREIGN
        eligible = False

    else:
        classification = UNKNOWN
        eligible = False

    if not reasons:
        reasons.append(
            "Not enough evidence to determine "
            "whether the domain is domestic"
        )

    return DomainClassification(
        hostname=hostname,
        classification=classification,
        eligible=eligible,
        score=score,
        reasons=reasons,
    )
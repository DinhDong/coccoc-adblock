import re
from dataclasses import dataclass
from typing import Optional

# Valid ABP network rule option names (subset; extend as needed)
VALID_NETWORK_OPTIONS = {
    "script", "image", "stylesheet", "object", "xmlhttprequest", "xhr",
    "subdocument", "document", "websocket", "webrtc", "ping", "font",
    "media", "other", "third-party", "first-party", "important",
    "domain", "sitekey", "match-case", "collapse", "popup",
}


@dataclass
class SyntaxResult:
    rule: str
    valid: bool
    error: Optional[str] = None


def check_syntax(rule: str) -> SyntaxResult:
    """
    Validate a single ABP rule string for correct syntax.

    Covers:
    - Network rules: ||pattern^ or ||pattern^$options
    - Cosmetic rules: [domains]##selector or [domains]#@#selector
    - Exception rules: @@||pattern^
    - Empty or whitespace-only strings
    """

    if not isinstance(rule, str):
        return SyntaxResult(rule=str(rule), valid=False, error="Rule must be a string")

    cleaned = rule.strip()

    if not cleaned:
        return SyntaxResult(rule=rule, valid=False, error="Rule is empty")

    if "\n" in cleaned or "\r" in cleaned:
        return SyntaxResult(rule=cleaned, valid=False, error="Rule must be a single line")

    if cleaned.startswith("```") or cleaned.endswith("```"):
        return SyntaxResult(rule=cleaned, valid=False, error="Rule contains markdown code fence")

    if cleaned.startswith("- ") or cleaned.startswith("* "):
        return SyntaxResult(rule=cleaned, valid=False, error="Rule contains bullet formatting")

    # Comments are not generated rules. rule_parser.py should normally remove them first.
    if cleaned.startswith("!"):
        return SyntaxResult(rule=cleaned, valid=False, error="Comment lines are not valid generated rules")

    # Cosmetic or cosmetic exception rule
    if "##" in cleaned or "#@#" in cleaned:
        if "##" in cleaned:
            parts = cleaned.split("##", 1)
            separator = "##"
        else:
            parts = cleaned.split("#@#", 1)
            separator = "#@#"

        if len(parts) != 2:
            return SyntaxResult(rule=cleaned, valid=False, error="Invalid cosmetic rule separator")

        domain_part, selector = parts[0].strip(), parts[1].strip()

        if separator not in {"##", "#@#"}:
            return SyntaxResult(rule=cleaned, valid=False, error="Invalid cosmetic rule separator")

        if not selector:
            return SyntaxResult(rule=cleaned, valid=False, error="Cosmetic rule has empty selector")

        domain_error = _validate_domain_list(domain_part, allow_empty=True)
        if domain_error:
            return SyntaxResult(rule=cleaned, valid=False, error=domain_error)

        selector_error = _validate_css_selector(selector)
        if selector_error:
            return SyntaxResult(rule=cleaned, valid=False, error=selector_error)

        return SyntaxResult(rule=cleaned, valid=True)

    # Exception rule
    is_exception = cleaned.startswith("@@")
    network_rule = cleaned[2:] if is_exception else cleaned

    if is_exception and not network_rule:
        return SyntaxResult(rule=cleaned, valid=False, error="Exception rule has empty pattern")

    # Split network pattern and options
    if "$" in network_rule:
        pattern, options_str = network_rule.split("$", 1)

        if not pattern:
            return SyntaxResult(rule=cleaned, valid=False, error="Network rule has empty pattern")

        option_error = _validate_network_options(options_str)
        if option_error:
            return SyntaxResult(rule=cleaned, valid=False, error=option_error)
    else:
        pattern = network_rule

    pattern = pattern.strip()

    if not pattern:
        return SyntaxResult(rule=cleaned, valid=False, error="Network rule has empty pattern")

    if re.search(r"\s", pattern):
        return SyntaxResult(rule=cleaned, valid=False, error="Network rule pattern must not contain whitespace")

    # Basic accepted network patterns:
    # ||ads.example.com^
    # ||example.com/path/ad.js
    # /banner/ad.js
    # https://example.com/ad.js
    # ads.example.com/banner.js
    if pattern.startswith("||"):
        body = pattern[2:]

        if not body:
            return SyntaxResult(rule=cleaned, valid=False, error="Anchored network rule has empty domain pattern")

        if body.startswith("^"):
            return SyntaxResult(rule=cleaned, valid=False, error="Anchored network rule is missing domain")

        # Domain-style pattern after ||
        domain_match = re.match(r"^[A-Za-z0-9.-]+", body)
        if not domain_match:
            return SyntaxResult(rule=cleaned, valid=False, error="Invalid anchored domain pattern")

        domain = domain_match.group(0)
        domain_error = _validate_single_domain(domain)
        if domain_error:
            return SyntaxResult(rule=cleaned, valid=False, error=domain_error)

    elif pattern.startswith("http://") or pattern.startswith("https://"):
        url_match = re.match(r"^https?://[A-Za-z0-9.-]+", pattern)
        if not url_match:
            return SyntaxResult(rule=cleaned, valid=False, error="Invalid URL pattern")

    elif pattern.startswith("/"):
        if len(pattern) < 2:
            return SyntaxResult(rule=cleaned, valid=False, error="Path-based rule is too short")

    else:
        # Fallback simple network pattern.
        # This allows patterns such as ads/banner.js but rejects prose.
        if not re.match(r"^[A-Za-z0-9._~:/?#\[\]@!&'()*+,;=%|^*-]+$", pattern):
            return SyntaxResult(rule=cleaned, valid=False, error="Invalid characters in network rule pattern")

        # Reject obvious prose accidentally passed from LLM output.
        if " " in pattern or pattern.lower().startswith(("here", "this", "rule", "block")):
            return SyntaxResult(rule=cleaned, valid=False, error="Rule appears to contain explanation text")

    return SyntaxResult(rule=cleaned, valid=True)


def check_syntax_batch(rules: list[str]) -> list[SyntaxResult]:
    """Run check_syntax on a list of rules and return results in the same order."""
    return [check_syntax(r) for r in rules]


def _validate_network_options(options_str: str) -> Optional[str]:
    """
    Parse the options portion of a network rule, the part after $.
    Returns an error string if invalid, or None if valid.
    """

    if options_str is None or options_str.strip() == "":
        return "Network rule has empty option section"

    options = [opt.strip() for opt in options_str.split(",")]

    for option in options:
        if not option:
            return "Network rule contains an empty option"

        # Negated options: ~$script, ~$image, etc.
        normalized = option[1:] if option.startswith("~") else option

        if "=" in normalized:
            name, value = normalized.split("=", 1)

            if name not in VALID_NETWORK_OPTIONS:
                return f"Unknown network option: {name}"

            if not value:
                return f"Option {name}= has empty value"

            if name == "domain":
                domain_error = _validate_domain_option(value)
                if domain_error:
                    return domain_error

            elif name == "sitekey":
                if not re.match(r"^[A-Za-z0-9+/=|_-]+$", value):
                    return "Invalid sitekey option value"

            else:
                # For this validator version, only domain= and sitekey= need value support.
                # Other value-based options can be added later.
                if not re.match(r"^[A-Za-z0-9_.:/|~*-]+$", value):
                    return f"Invalid value for option {name}"

        else:
            if normalized not in VALID_NETWORK_OPTIONS:
                return f"Unknown network option: {normalized}"

    return None


def _validate_css_selector(selector: str) -> Optional[str]:
    """
    Light check that a cosmetic rule's CSS selector is non-empty and
    does not contain obvious syntax errors.
    Returns an error string if invalid, or None if valid.
    """

    if selector is None or selector.strip() == "":
        return "CSS selector is empty"

    selector = selector.strip()

    if selector in {"##", "#@#"}:
        return "CSS selector is missing"

    # Obvious unmatched brackets/parentheses. This is not a full CSS parser.
    pairs = [
        ("[", "]"),
        ("(", ")"),
        ("{", "}"),
    ]

    for open_char, close_char in pairs:
        if selector.count(open_char) != selector.count(close_char):
            return f"CSS selector has unmatched {open_char}{close_char}"

    # Reject selectors with obvious line breaks or style blocks.
    if "\n" in selector or "\r" in selector:
        return "CSS selector must be a single line"

    if "<" in selector or ">" in selector:
        return "CSS selector must not contain HTML tags"

    return None


def _validate_single_domain(domain: str) -> Optional[str]:
    """Validate one simple domain name."""

    if not domain:
        return "Domain is empty"

    if len(domain) > 253:
        return "Domain is too long"

    if domain.startswith(".") or domain.endswith("."):
        return "Domain must not start or end with a dot"

    if ".." in domain:
        return "Domain must not contain consecutive dots"

    labels = domain.split(".")

    for label in labels:
        if not label:
            return "Domain contains empty label"

        if len(label) > 63:
            return "Domain label is too long"

        if label.startswith("-") or label.endswith("-"):
            return "Domain label must not start or end with hyphen"

        if not re.match(r"^[A-Za-z0-9-]+$", label):
            return "Domain contains invalid characters"

    return None


def _validate_domain_list(domain_part: str, allow_empty: bool = False) -> Optional[str]:
    """
    Validate cosmetic rule domain prefix:
    example.com##.ad
    example.com,foo.vn##.ad
    ~excluded.com,example.com##.ad
    """

    if not domain_part:
        return None if allow_empty else "Domain list is empty"

    domains = [d.strip() for d in domain_part.split(",")]

    for domain in domains:
        if not domain:
            return "Domain list contains empty value"

        normalized = domain[1:] if domain.startswith("~") else domain

        domain_error = _validate_single_domain(normalized)
        if domain_error:
            return f"Invalid cosmetic rule domain '{domain}': {domain_error}"

    return None


def _validate_domain_option(value: str) -> Optional[str]:
    """
    Validate domain= option:
    domain=example.com
    domain=example.com|foo.vn
    domain=example.com|~excluded.com
    """

    domains = [d.strip() for d in value.split("|")]

    for domain in domains:
        if not domain:
            return "domain= option contains empty value"

        normalized = domain[1:] if domain.startswith("~") else domain

        domain_error = _validate_single_domain(normalized)
        if domain_error:
            return f"Invalid domain= value '{domain}': {domain_error}"

    return None
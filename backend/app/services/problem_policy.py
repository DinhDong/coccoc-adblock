from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Tuple


# ============================================================
# Resolution strategies
# ============================================================

STRATEGY_BLOCK_VISIBLE_AD = "block_visible_ad"
STRATEGY_ALLOW_REQUIRED_CONTENT = "allow_required_content"
STRATEGY_RESTORE_HIDDEN_UI = "restore_hidden_ui"
STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE = (
    "remove_overlay_or_allow_required_resource"
)
STRATEGY_UNKNOWN_SAFE_PATCH = "unknown_safe_patch"


# ============================================================
# Rule direction / rule intent types
# ============================================================

RULE_NETWORK_BLOCK = "network_block"
RULE_COSMETIC_HIDE = "cosmetic_hide"
RULE_NETWORK_EXCEPTION = "network_exception"
RULE_COSMETIC_EXCEPTION = "cosmetic_exception"
RULE_UNKNOWN = "unknown"


LEGACY_DEFAULT_PROBLEM_TYPE = "specific_ad_not_blocked"


@dataclass(frozen=True)
class ProblemPolicy:
    """
    Central policy for one problem type.

    problem_type:
      The normalized problem type used in ticket_context and output files.

    strategy:
      Higher-level resolution strategy. Multiple problem types can share one
      strategy.

    preferred_rule_types:
      Rule directions the LLM should prefer.

    allowed_rule_types:
      Rule directions allowed by validator/prompt for this problem type.

    forbidden_rule_types:
      Rule directions that are usually wrong for this problem type.

    default_targets_to_block:
      Default "Should block" context.

    default_targets_to_preserve:
      Default "Must preserve" context.

    validation_hints:
      Default ticket-specific sandbox assertions.
    """

    problem_type: str
    strategy: str
    description: str
    preferred_rule_types: Tuple[str, ...]
    allowed_rule_types: Tuple[str, ...]
    forbidden_rule_types: Tuple[str, ...]
    default_targets_to_block: Tuple[str, ...] = ()
    default_targets_to_preserve: Tuple[str, ...] = ()
    validation_hints: Mapping[str, Any] = field(default_factory=dict)


PROBLEM_POLICIES: Dict[str, ProblemPolicy] = {
    "specific_ad_not_blocked": ProblemPolicy(
        problem_type="specific_ad_not_blocked",
        strategy=STRATEGY_BLOCK_VISIBLE_AD,
        description=(
            "A visible ad is not blocked. Generate narrow network blocking or "
            "cosmetic hiding rules for detected ad-related signals."
        ),
        preferred_rule_types=(
            RULE_NETWORK_BLOCK,
            RULE_COSMETIC_HIDE,
        ),
        allowed_rule_types=(
            RULE_NETWORK_BLOCK,
            RULE_COSMETIC_HIDE,
        ),
        forbidden_rule_types=(
            RULE_NETWORK_EXCEPTION,
            RULE_COSMETIC_EXCEPTION,
        ),
        default_targets_to_block=(
            "detected ad containers",
            "ad iframes",
            "ad network requests",
            "sponsored or popup ad elements",
        ),
        default_targets_to_preserve=(
            "main content",
            "navigation",
            "forms",
            "media",
            "user controls",
        ),
        validation_hints={},
    ),
    "content_broken_image": ProblemPolicy(
        problem_type="content_broken_image",
        strategy=STRATEGY_ALLOW_REQUIRED_CONTENT,
        description=(
            "Images or normal content images are broken when Adblock is enabled. "
            "Generate narrow network exception rules for image/CDN resources."
        ),
        preferred_rule_types=(
            RULE_NETWORK_EXCEPTION,
        ),
        allowed_rule_types=(
            RULE_NETWORK_EXCEPTION,
            RULE_COSMETIC_EXCEPTION,
        ),
        forbidden_rule_types=(
            RULE_NETWORK_BLOCK,
            RULE_COSMETIC_HIDE,
        ),
        default_targets_to_block=(),
        default_targets_to_preserve=(
            "main content",
            "chapter images",
            "article images",
            "content image CDN requests",
        ),
        validation_hints={
            "min_visible_images": 1,
            "max_broken_images": 0,
        },
    ),
    "content_broken_video": ProblemPolicy(
        problem_type="content_broken_video",
        strategy=STRATEGY_ALLOW_REQUIRED_CONTENT,
        description=(
            "Video, player, or media playback is broken when Adblock is enabled. "
            "Generate narrow exception rules for required media/player resources."
        ),
        preferred_rule_types=(
            RULE_NETWORK_EXCEPTION,
        ),
        allowed_rule_types=(
            RULE_NETWORK_EXCEPTION,
            RULE_COSMETIC_EXCEPTION,
        ),
        forbidden_rule_types=(
            RULE_NETWORK_BLOCK,
            RULE_COSMETIC_HIDE,
        ),
        default_targets_to_block=(),
        default_targets_to_preserve=(
            "main content",
            "video player",
            "video stream requests",
            "media controls",
        ),
        validation_hints={
            "min_visible_videos": 1,
            "must_show_any_selector_groups": [
                {
                    "name": "video_or_player",
                    "selectors": [
                        "video",
                        "iframe",
                        ".player",
                        ".video",
                        "[class*='player']",
                        "[class*='video']",
                    ],
                    "min": 1,
                }
            ],
        },
    ),
    "content_broken": ProblemPolicy(
        problem_type="content_broken",
        strategy=STRATEGY_ALLOW_REQUIRED_CONTENT,
        description=(
            "Normal page content or functionality is broken when Adblock is enabled. "
            "Generate narrow exception rules based on matched rules or blocked resources."
        ),
        preferred_rule_types=(
            RULE_NETWORK_EXCEPTION,
            RULE_COSMETIC_EXCEPTION,
        ),
        allowed_rule_types=(
            RULE_NETWORK_EXCEPTION,
            RULE_COSMETIC_EXCEPTION,
        ),
        forbidden_rule_types=(
            RULE_NETWORK_BLOCK,
            RULE_COSMETIC_HIDE,
        ),
        default_targets_to_block=(),
        default_targets_to_preserve=(
            "main content",
            "navigation",
            "forms",
            "media",
            "user controls",
        ),
        validation_hints={},
    ),
    "ui_hidden": ProblemPolicy(
        problem_type="ui_hidden",
        strategy=STRATEGY_RESTORE_HIDDEN_UI,
        description=(
            "Search, menu, header, navigation, buttons, or UI controls are hidden "
            "when Adblock is enabled. Generate cosmetic exception rules."
        ),
        preferred_rule_types=(
            RULE_COSMETIC_EXCEPTION,
        ),
        allowed_rule_types=(
            RULE_COSMETIC_EXCEPTION,
            RULE_NETWORK_EXCEPTION,
        ),
        forbidden_rule_types=(
            RULE_COSMETIC_HIDE,
            RULE_NETWORK_BLOCK,
        ),
        default_targets_to_block=(),
        default_targets_to_preserve=(
            "header",
            "navigation",
            "search bar",
            "menu bar",
            "main content",
        ),
        validation_hints={
            "must_show_any_selector_groups": [
                {
                    "name": "search",
                    "selectors": [
                        ".search",
                        ".search-box",
                        "input[type='search']",
                        "[class*='search']",
                        "[id*='search']",
                    ],
                    "min": 1,
                },
                {
                    "name": "menu_or_navigation",
                    "selectors": [
                        "header",
                        "nav",
                        ".header",
                        ".navbar",
                        ".menu",
                        "[class*='menu']",
                        "[class*='nav']",
                        "[class*='header']",
                    ],
                    "min": 1,
                },
            ],
        },
    ),
    "anti_adblock_or_overlay": ProblemPolicy(
        problem_type="anti_adblock_or_overlay",
        strategy=STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE,
        description=(
            "An ad overlay, popup, interstitial, anti-adblock layer, or close-button "
            "issue prevents normal page usage. Hide/block overlay targets or allow "
            "required resources when evidence shows they are needed."
        ),
        preferred_rule_types=(
            RULE_COSMETIC_HIDE,
            RULE_NETWORK_BLOCK,
            RULE_NETWORK_EXCEPTION,
        ),
        allowed_rule_types=(
            RULE_COSMETIC_HIDE,
            RULE_NETWORK_BLOCK,
            RULE_NETWORK_EXCEPTION,
            RULE_COSMETIC_EXCEPTION,
        ),
        forbidden_rule_types=(),
        default_targets_to_block=(
            "ad overlay",
            "popup ad",
            "interstitial ad",
            "anti-adblock overlay",
        ),
        default_targets_to_preserve=(
            "main content",
            "download button",
            "close button",
            "form controls",
            "quality selector",
        ),
        validation_hints={
            "must_show_any_selector_groups": [
                {
                    "name": "main_or_form",
                    "selectors": [
                        "main",
                        "form",
                        "button",
                        "input",
                        "select",
                        "[class*='download']",
                    ],
                    "min": 1,
                }
            ],
            "must_hide_selectors": [
                ".ad-overlay",
                ".popup-ad",
                ".modal-ad",
                "[class*='overlay'][class*='ad']",
                "[class*='popup'][class*='ad']",
                "[id*='overlay'][id*='ad']",
                "[id*='popup'][id*='ad']",
            ],
        },
    ),
    "unknown": ProblemPolicy(
        problem_type="unknown",
        strategy=STRATEGY_UNKNOWN_SAFE_PATCH,
        description=(
            "The ticket type is not clear. Infer the safest direction from context. "
            "If content is broken, prefer exceptions. If ads are visible, prefer "
            "blocking or hiding. If context is insufficient, generate conservative "
            "candidates only."
        ),
        preferred_rule_types=(
            RULE_NETWORK_BLOCK,
            RULE_COSMETIC_HIDE,
            RULE_NETWORK_EXCEPTION,
            RULE_COSMETIC_EXCEPTION,
        ),
        allowed_rule_types=(
            RULE_NETWORK_BLOCK,
            RULE_COSMETIC_HIDE,
            RULE_NETWORK_EXCEPTION,
            RULE_COSMETIC_EXCEPTION,
        ),
        forbidden_rule_types=(),
        default_targets_to_block=(),
        default_targets_to_preserve=(
            "main content",
            "navigation",
        ),
        validation_hints={},
    ),
}


PROBLEM_TYPE_ALIASES: Dict[str, str] = {
    # Visible ads / legacy ad blocking
    "ad_not_blocked": "specific_ad_not_blocked",
    "ads_not_blocked": "specific_ad_not_blocked",
    "specific_ads_not_blocked": "specific_ad_not_blocked",
    "visible_ad": "specific_ad_not_blocked",
    "visible_ads": "specific_ad_not_blocked",
    "banner_ad_not_blocked": "specific_ad_not_blocked",
    "popup_ad_not_blocked": "anti_adblock_or_overlay",
    # Image breakage
    "image_broken": "content_broken_image",
    "images_broken": "content_broken_image",
    "broken_image": "content_broken_image",
    "broken_images": "content_broken_image",
    "image_not_displayed": "content_broken_image",
    "images_not_displayed": "content_broken_image",
    "thumbnail_broken": "content_broken_image",
    "avatar_broken": "content_broken_image",
    "lazyload_image_broken": "content_broken_image",
    # Video breakage
    "video_broken": "content_broken_video",
    "player_broken": "content_broken_video",
    "media_broken": "content_broken_video",
    "video_not_playing": "content_broken_video",
    # Generic content breakage
    "page_broken": "content_broken",
    "site_broken": "content_broken",
    "functionality_broken": "content_broken",
    "content_missing": "content_broken",
    # UI hidden
    "hidden_ui": "ui_hidden",
    "ui_missing": "ui_hidden",
    "search_hidden": "ui_hidden",
    "menu_hidden": "ui_hidden",
    "header_hidden": "ui_hidden",
    "navigation_hidden": "ui_hidden",
    "button_hidden": "ui_hidden",
    "login_button_hidden": "ui_hidden",
    # Overlay / anti adblock
    "anti_adblock": "anti_adblock_or_overlay",
    "anti_adblock_overlay": "anti_adblock_or_overlay",
    "overlay": "anti_adblock_or_overlay",
    "popup_overlay": "anti_adblock_or_overlay",
    "ad_overlay": "anti_adblock_or_overlay",
    "ad_overlay_blocks_page": "anti_adblock_or_overlay",
    "cannot_close_ad": "anti_adblock_or_overlay",
    "unable_to_close_ad": "anti_adblock_or_overlay",
    "close_button_broken": "anti_adblock_or_overlay",
}


def normalize_problem_type(
    problem_type: Any,
    fallback: str = "unknown",
) -> str:
    """
    Normalize a raw problem_type into a known canonical problem type.

    Examples:
      "image-broken"       -> "content_broken_image"
      "Search Hidden"      -> "ui_hidden"
      "ad_not_blocked"     -> "specific_ad_not_blocked"
    """
    raw = _normalize_key(problem_type)

    if not raw:
        return fallback if fallback in PROBLEM_POLICIES else "unknown"

    if raw in PROBLEM_POLICIES:
        return raw

    if raw in PROBLEM_TYPE_ALIASES:
        return PROBLEM_TYPE_ALIASES[raw]

    return fallback if fallback in PROBLEM_POLICIES else "unknown"


def get_problem_policy(problem_type: Any) -> ProblemPolicy:
    """
    Return policy for a problem type.

    Unknown or unsupported problem types fall back to the "unknown" policy.
    """
    normalized = normalize_problem_type(problem_type, fallback="unknown")
    return PROBLEM_POLICIES.get(normalized, PROBLEM_POLICIES["unknown"])


def get_known_problem_types() -> List[str]:
    return sorted(PROBLEM_POLICIES.keys())


def get_resolution_strategy(problem_type: Any) -> str:
    return get_problem_policy(problem_type).strategy


def get_preferred_rule_types(problem_type: Any) -> List[str]:
    return list(get_problem_policy(problem_type).preferred_rule_types)


def get_allowed_rule_types(problem_type: Any) -> List[str]:
    return list(get_problem_policy(problem_type).allowed_rule_types)


def get_forbidden_rule_types(problem_type: Any) -> List[str]:
    return list(get_problem_policy(problem_type).forbidden_rule_types)


def get_default_targets_to_block(problem_type: Any) -> List[str]:
    return list(get_problem_policy(problem_type).default_targets_to_block)


def get_default_targets_to_preserve(problem_type: Any) -> List[str]:
    return list(get_problem_policy(problem_type).default_targets_to_preserve)


def get_default_validation_hints(problem_type: Any) -> Dict[str, Any]:
    """
    Return a deep copy of default validation hints so callers can safely merge
    or mutate it without changing global policy.
    """
    return copy.deepcopy(dict(get_problem_policy(problem_type).validation_hints))


def is_legacy_problem_type(problem_type: Any) -> bool:
    return normalize_problem_type(problem_type) == LEGACY_DEFAULT_PROBLEM_TYPE


def is_ad_block_strategy(problem_type: Any) -> bool:
    return get_resolution_strategy(problem_type) == STRATEGY_BLOCK_VISIBLE_AD


def is_breakage_strategy(problem_type: Any) -> bool:
    return get_resolution_strategy(problem_type) == STRATEGY_ALLOW_REQUIRED_CONTENT


def is_ui_restore_strategy(problem_type: Any) -> bool:
    return get_resolution_strategy(problem_type) == STRATEGY_RESTORE_HIDDEN_UI


def is_overlay_strategy(problem_type: Any) -> bool:
    return (
        get_resolution_strategy(problem_type)
        == STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE
    )


def classify_rule_direction(rule: Any) -> str:
    """
    Classify an ABP rule into the direction that matters for problem policy.

    This is intentionally lightweight and does not replace full ABP syntax
    validation. It is used for prompt diagnostics and validator direction checks.
    """
    text = str(rule or "").strip()

    if not text:
        return RULE_UNKNOWN

    if "#@#" in text:
        return RULE_COSMETIC_EXCEPTION

    if text.startswith("@@"):
        return RULE_NETWORK_EXCEPTION

    if "##" in text or "#?#" in text or "#$#" in text:
        return RULE_COSMETIC_HIDE

    if text.startswith("||") or text.startswith("|") or "://" in text:
        return RULE_NETWORK_BLOCK

    return RULE_UNKNOWN


def is_rule_direction_allowed(
    problem_type: Any,
    rule: Any,
    *,
    has_direct_evidence: bool = False,
) -> bool:
    """
    Return True if a rule direction is acceptable for the problem type.

    has_direct_evidence:
      True when matched_rules or blocked_resources exist. This allows a few
      edge cases, such as ui_hidden using a network exception if a blocked CSS/JS
      resource is proven to be the cause.
    """
    policy = get_problem_policy(problem_type)
    rule_direction = classify_rule_direction(rule)

    if rule_direction == RULE_UNKNOWN:
        return False

    if rule_direction in policy.forbidden_rule_types:
        # ui_hidden normally forbids network exceptions, but we allow it when
        # direct evidence proves a required resource is blocked.
        if (
            policy.strategy == STRATEGY_RESTORE_HIDDEN_UI
            and rule_direction == RULE_NETWORK_EXCEPTION
            and has_direct_evidence
        ):
            return True

        return False

    return rule_direction in policy.allowed_rule_types


def get_rule_direction_error(
    problem_type: Any,
    rule: Any,
    *,
    has_direct_evidence: bool = False,
) -> str:
    """
    Return empty string if allowed, otherwise return a user/debug-friendly reason.
    """
    policy = get_problem_policy(problem_type)
    rule_direction = classify_rule_direction(rule)

    if rule_direction == RULE_UNKNOWN:
        return (
            f"Unable to classify rule direction for problem_type={policy.problem_type}"
        )

    if is_rule_direction_allowed(
        policy.problem_type,
        rule,
        has_direct_evidence=has_direct_evidence,
    ):
        return ""

    preferred = ", ".join(policy.preferred_rule_types) or "none"
    forbidden = ", ".join(policy.forbidden_rule_types) or "none"

    return (
        f"Wrong rule direction for problem_type={policy.problem_type}; "
        f"strategy={policy.strategy}; "
        f"rule_direction={rule_direction}; "
        f"preferred={preferred}; "
        f"forbidden={forbidden}"
    )


def get_prompt_policy_lines(
    problem_type: Any,
    *,
    evidence_level: str = "",
    page_domain: str = "",
    has_current_rules: bool = False,
    has_matched_rules: bool = False,
    has_blocked_resources: bool = False,
) -> List[str]:
    """
    Build reusable prompt lines from policy.

    Later, prompt_builder.py can call this instead of hard-coding every
    problem_type strategy inside _append_generation_goal().
    """
    policy = get_problem_policy(problem_type)

    preferred = ", ".join(policy.preferred_rule_types) or "none"
    allowed = ", ".join(policy.allowed_rule_types) or "none"
    forbidden = ", ".join(policy.forbidden_rule_types) or "none"

    lines = [
        f"  Policy problem type: {policy.problem_type}",
        f"  Resolution strategy: {policy.strategy}",
        f"  Policy description: {policy.description}",
        f"  Preferred rule directions: {preferred}",
        f"  Allowed rule directions: {allowed}",
        f"  Forbidden rule directions: {forbidden}",
    ]

    if page_domain:
        lines.append(f"  Target-domain scope: prefer rules scoped to {page_domain}.")

    if evidence_level:
        lines.append(f"  Evidence level: {evidence_level}.")

    if has_matched_rules:
        lines.append(
            "  Matched rules are available. Treat them as the strongest evidence and patch them directly."
        )

    if has_blocked_resources:
        lines.append(
            "  Blocked resources are available. Prefer narrow exceptions based on resource domain/path/type when fixing breakage."
        )

    if has_current_rules:
        lines.append(
            "  Current rules are available. Avoid unrelated guesses and patch only the risky existing rule when possible."
        )

    if (
        not has_matched_rules
        and not has_blocked_resources
        and not has_current_rules
    ):
        lines.append(
            "  No direct rule/resource evidence is available. Do not claim a specific existing rule caused the issue."
        )

    if policy.strategy == STRATEGY_BLOCK_VISIBLE_AD:
        lines.extend(
            [
                "  Strategy detail: block or hide visible ad-related targets.",
                "  Use high-confidence ad candidates before third-party domain heuristics.",
                "  Avoid exception rules unless the ticket explicitly asks to restore broken content.",
            ]
        )

        if evidence_level == "legacy_no_ticket_context":
            lines.append(
                "  Legacy mode: keep old behavior and generate blocking/hiding rules for observed ad-related signals."
            )

    elif policy.strategy == STRATEGY_ALLOW_REQUIRED_CONTENT:
        lines.extend(
            [
                "  Strategy detail: restore required content broken by Adblock.",
                "  Prefer narrow exception rules.",
                "  Avoid new blocking/hiding rules unless there is a separate visible ad target.",
            ]
        )

    elif policy.strategy == STRATEGY_RESTORE_HIDDEN_UI:
        lines.extend(
            [
                "  Strategy detail: restore hidden UI while keeping ads blocked.",
                "  Prefer cosmetic exception rules using #@#.",
                "  Do not generate cosmetic hiding rules for search/menu/header/navigation/user controls.",
            ]
        )

    elif policy.strategy == STRATEGY_REMOVE_OVERLAY_OR_ALLOW_REQUIRED_RESOURCE:
        lines.extend(
            [
                "  Strategy detail: remove blocking overlays or allow required resources.",
                "  If an ad overlay blocks the page, prefer narrow cosmetic hiding or network blocking.",
                "  If a required flow is broken by a blocked resource, prefer a narrow exception.",
                "  Preserve main content, forms, download buttons, close buttons, and user controls.",
            ]
        )

    else:
        lines.extend(
            [
                "  Strategy detail: infer the safest rule direction from the ticket.",
                "  If content is broken, prefer exceptions.",
                "  If ads are visible, prefer blocking or hiding.",
                "  If context is insufficient, generate conservative candidates only.",
            ]
        )

    return lines


def make_policy_diagnostics(
    problem_type: Any,
    rules: List[str],
    *,
    evidence_level: str = "",
    has_current_rules: bool = False,
    has_matched_rules: bool = False,
    has_blocked_resources: bool = False,
) -> Dict[str, Any]:
    """
    Create lightweight diagnostics that can be stored in generation output later.

    This is useful for future prompt tuning:
      - wrong direction
      - overly broad rules
      - duplicate-looking rules
      - weak evidence
    """
    policy = get_problem_policy(problem_type)
    has_direct_evidence = has_matched_rules or has_blocked_resources

    generated_rule_types = []
    wrong_direction_rules = []

    for rule in rules:
        direction = classify_rule_direction(rule)
        generated_rule_types.append(direction)

        if not is_rule_direction_allowed(
            policy.problem_type,
            rule,
            has_direct_evidence=has_direct_evidence,
        ):
            wrong_direction_rules.append(
                {
                    "rule": rule,
                    "direction": direction,
                    "reason": get_rule_direction_error(
                        policy.problem_type,
                        rule,
                        has_direct_evidence=has_direct_evidence,
                    ),
                }
            )

    return {
        "problem_type": policy.problem_type,
        "resolution_strategy": policy.strategy,
        "evidence_level": evidence_level,
        "has_current_rules": has_current_rules,
        "has_matched_rules": has_matched_rules,
        "has_blocked_resources": has_blocked_resources,
        "preferred_rule_types": list(policy.preferred_rule_types),
        "allowed_rule_types": list(policy.allowed_rule_types),
        "forbidden_rule_types": list(policy.forbidden_rule_types),
        "generated_rule_types": generated_rule_types,
        "wrong_direction_rules": wrong_direction_rules,
        "rule_direction_correct": len(wrong_direction_rules) == 0,
    }


def _normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()

    text = text.replace("-", "_")
    text = text.replace(" ", "_")
    text = text.replace("/", "_")

    while "__" in text:
        text = text.replace("__", "_")

    return text.strip("_")
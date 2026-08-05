import re
import unicodedata
from typing import Any, Dict, List, Mapping

from app.services.problem_policy import (
    LEGACY_DEFAULT_PROBLEM_TYPE,
    get_default_targets_to_block,
    get_default_targets_to_preserve,
    get_default_validation_hints,
    get_known_problem_types,
    get_resolution_strategy,
    normalize_problem_type,
)


KNOWN_PROBLEM_TYPES = set(get_known_problem_types())


def normalize_ticket_context(raw_context: Any) -> Dict[str, Any]:
    """
    Normalize user/CMS ticket data into a compact JSON-safe context object.

    Design goal:
    - Users should only need to provide simple fields:
        platform, problem_type, request, description, steps, actual, expected.
    - Internal/debug fields remain optional:
        target_to_block, target_to_preserve, current_rules, matched_rules,
        blocked_resources, validation_hints.
    - When those optional fields are missing, this module infers safe defaults
      from the user-facing text.

    Important behavior:
    - Empty context -> legacy no-ticket mode.
    - URL-only/basic context -> best-effort ticket-aware mode.
    - "popup/floating only" tickets are narrowed automatically so rule generation
      can avoid broad visible-ad behavior.
    """
    context = _coerce_dict(raw_context)
    focus_region = _extract_focus_region(context)

    if _is_empty_context(context) or _looks_like_legacy_default_context(context):
        legacy = _legacy_no_ticket_context()
        if focus_region:
            legacy["focus_region"] = focus_region
        _copy_passthrough_fields(legacy, context)
        return _make_json_safe(legacy)

    request = _clean_text(context.get("request", ""))
    description = _clean_text(context.get("description", ""))
    actual = _clean_text(context.get("actual", ""))
    expected = _clean_text(context.get("expected", ""))
    platform = _normalize_platform(context.get("platform", ""))
    steps = _normalize_steps(context.get("steps", []))

    combined_text = " ".join(
        [
            request,
            description,
            " ".join(steps),
            actual,
            expected,
        ]
    ).strip()

    problem_type = _resolve_problem_type(
        raw_problem_type=context.get("problem_type", ""),
        combined_text=combined_text,
    )

    if problem_type == "unknown" and not combined_text:
        problem_type = LEGACY_DEFAULT_PROBLEM_TYPE

    resolution_strategy = get_resolution_strategy(problem_type)

    target_to_block = _normalize_string_list(context.get("target_to_block", []))
    if not target_to_block:
        target_to_block = infer_targets_to_block(problem_type, combined_text)

    target_to_preserve = _normalize_string_list(context.get("target_to_preserve", []))
    if not target_to_preserve:
        target_to_preserve = infer_targets_to_preserve(problem_type, combined_text)

    provided_validation_hints = context.get("validation_hints", {})
    if not isinstance(provided_validation_hints, Mapping):
        provided_validation_hints = {}

    inferred_hints = infer_validation_hints(problem_type, combined_text)
    merged_hints = _merge_validation_hints(
        inferred_hints,
        dict(provided_validation_hints),
    )

    current_rules = _collect_rule_list(
        context,
        keys=("current_rules", "existing_rules", "active_rules"),
    )
    matched_rules = _normalize_matched_rules(context.get("matched_rules", []))
    blocked_resources = _normalize_blocked_resources(
        context.get("blocked_resources", [])
    )

    evidence_level = _infer_evidence_level(
        current_rules=current_rules,
        matched_rules=matched_rules,
        blocked_resources=blocked_resources,
        has_explicit_context=True,
    )

    normalized = {
        "platform": platform,
        "problem_type": problem_type,
        "resolution_strategy": resolution_strategy,
        "focus_region": focus_region,
        "request": request,
        "description": description,
        "steps": steps,
        "actual": actual,
        "expected": expected,
        "target_to_block": target_to_block,
        "target_to_preserve": target_to_preserve,
        "validation_hints": merged_hints,
        "current_rules": current_rules,
        "matched_rules": matched_rules,
        "blocked_resources": blocked_resources,
        "evidence_level": evidence_level,
        "raw": _make_json_safe(context.get("raw", {})),
    }

    _copy_passthrough_fields(normalized, context)

    return _make_json_safe(normalized)


def infer_problem_type(text: str) -> str:
    """
    Infer the ticket type from user-facing ticket text.
    Explicit problem_type from user/CMS still wins in normalize_ticket_context().
    """
    normalized = _normalize_for_matching(text)

    if not normalized:
        return "unknown"

    image_patterns = [
        r"\bimage\b",
        r"\bimages\b",
        r"\bimg\b",
        r"\bphoto\b",
        r"\bpicture\b",
        r"\bthumbnail\b",
        r"\bavatar\b",
        r"\blazyload\b",
        r"\banh\b",
        r"khong\s+hien\s+anh",
        r"doesn'?t\s+display\s+images",
        r"not\s+display\s+images",
        r"images?\s+(?:are\s+)?(?:not|missing|hidden|blank|broken)",
    ]
    if _matches_any(normalized, image_patterns):
        return normalize_problem_type("content_broken_image")

    video_patterns = [
        r"\bvideo\b",
        r"\bplayer\b",
        r"\bstream\b",
        r"\bmedia\b",
        r"\bm3u8\b",
        r"\bmp4\b",
        r"khong\s+xem\s+duoc",
        r"video\s+(?:is\s+)?(?:not|missing|broken|blank)",
        r"player\s+(?:is\s+)?(?:not|missing|broken|blank)",
    ]
    if _matches_any(normalized, video_patterns):
        return normalize_problem_type("content_broken_video")

    ui_hidden_patterns = [
        r"\bsearch\b",
        r"\bmenu\b",
        r"\bheader\b",
        r"\bnavbar\b",
        r"\bnavigation\b",
        r"\bbutton\b",
        r"\blogin\b",
        r"\bsign\s*in\b",
        r"\bhidden\b",
        r"\bhide\b",
        r"\bmissing\b",
        r"\ban\b",
        r"khong\s+thay",
        r"khong\s+hien\s+thi",
        r"search\s*&\s*menu",
    ]
    if _matches_any(normalized, ui_hidden_patterns):
        return normalize_problem_type("ui_hidden")

    anti_adblock_patterns = [
        r"unable\s+to\s+close",
        r"cannot\s+close",
        r"can'?t\s+close",
        r"close\s+button",
        r"adblock\s+detected",
        r"disable\s+adblock",
        r"turn\s+off\s+adblock",
        r"anti[-\s]?adblock",
        r"\boverlay\b",
        r"\bpopup\b",
        r"\binterstitial\b",
        r"khong\s+dong",
        r"khong\s+tat",
        r"tat\s+adblock",
    ]
    if _matches_any(normalized, anti_adblock_patterns):
        return normalize_problem_type("anti_adblock_or_overlay")

    ad_not_blocked_patterns = [
        r"\bads?\s+appear\b",
        r"\bad\s+appear\b",
        r"\bnot\s+blocked\b",
        r"\bnot\s+block\b",
        r"\bstill\s+show",
        r"\bstill\s+visible",
        r"quang\s+cao\s+(?:van\s+)?(?:hien|xuat\s+hien)",
    ]
    if _matches_any(normalized, ad_not_blocked_patterns):
        return normalize_problem_type("specific_ad_not_blocked")

    broken_content_patterns = [
        r"\bbroken\b",
        r"\bblank\b",
        r"\bwhite\s+screen\b",
        r"\bnot\s+working\b",
        r"\bdoesn'?t\s+work\b",
        r"khong\s+hoat\s+dong",
        r"\bloi\b",
    ]
    if _matches_any(normalized, broken_content_patterns):
        return normalize_problem_type("content_broken")

    return "unknown"


def infer_targets_to_block(problem_type: str, text: str) -> List[str]:
    """
    Infer target_to_block from simple user-facing text.

    Example:
      User only provides:
        "Block popup and floating ads only"

      We enrich it into popup/modal/floating targets, but we avoid adding
      site-specific selectors or rules here.
    """
    normalized_problem_type = normalize_problem_type(problem_type, fallback="unknown")

    if _is_popup_floating_only_ticket(normalized_problem_type, text):
        return [
            "popup modal",
            "popup ad content",
            "popup ad container",
            "popup image/ad creative container",
            "modal content container",
            "modal backdrop",
            "fullscreen overlay",
            "floating ad container",
            "sticky floating ad",
        ]

    return get_default_targets_to_block(normalized_problem_type)


def infer_targets_to_preserve(problem_type: str, text: str) -> List[str]:
    """
    Infer target_to_preserve from simple user-facing text.

    If a ticket says popup/floating only, preserve normal page content by
    default so the generator does not treat every ad-like signal as in-scope.
    """
    normalized_problem_type = normalize_problem_type(problem_type, fallback="unknown")
    defaults = get_default_targets_to_preserve(normalized_problem_type)

    inferred: List[str] = []

    if _is_popup_floating_only_ticket(normalized_problem_type, text):
        inferred.extend(
            [
                "movie cards",
                "main hero area",
                "category/topic cards",
                "navigation/header/search",
                "footer",
                "normal page content",
            ]
        )

    if _mentions_do_not_block_banner_native_or_bookmaker(text):
        inferred.extend(
            [
                "banner ads",
                "native ads",
                "bookmaker ads",
                "sponsor/bookmaker sections",
            ]
        )

    return _dedupe_strings(inferred + defaults)


def infer_validation_hints(problem_type: str, text: str) -> Dict[str, Any]:
    """
    Infer generic internal validation/generation hints from user-facing text.

    Users do not need to know rules/selectors. This function turns simple text
    such as "popup/floating only" into generic scope hints.

    Important:
    - Do not put site-specific selectors, paths, or API routes here.
    - Specific selectors/URLs should come from detector/extractor evidence.
    - rule_generator can enforce these generic hints for LLM and detector backfill.
    """
    normalized_problem_type = normalize_problem_type(problem_type, fallback="unknown")
    hints = dict(get_default_validation_hints(normalized_problem_type))

    if _is_popup_floating_only_ticket(normalized_problem_type, text):
        hints = _merge_validation_hints(
            hints,
            {
                "inferred_scope": "popup_floating_only",
                "allowed_candidate_categories": [
                    "popup_overlay",
                    "floating_ad",
                    "ad_container",
                ],
                "disallowed_candidate_categories": [
                    "ad_network_request",
                    "ad_iframe",
                ],
                "must_not_generate_rules": [
                    "*adserver*",
                ],
            },
        )

    if _mentions_do_not_block_banner_native_or_bookmaker(text):
        hints = _merge_validation_hints(
            hints,
            {
                "must_not_generate_rules": [
                    "*banner*",
                    "*banners*",
                    "*native*",
                    "*bookmaker*",
                    "*sponsor*",
                    "*sponsored*",
                ],
            },
        )

    return hints


def _is_popup_floating_only_ticket(problem_type: str, text: str) -> bool:
    """
    Detect tickets that intentionally limit scope to popup/modal/floating ads.
    """
    normalized_problem_type = normalize_problem_type(problem_type, fallback="unknown")
    normalized_text = _normalize_for_matching(text)

    if normalized_problem_type not in {
        "specific_ad_not_blocked",
        LEGACY_DEFAULT_PROBLEM_TYPE,
        "anti_adblock_or_overlay",
    }:
        return False

    popup_or_floating_patterns = [
        r"\bpopup\b",
        r"\bpop[-\s]?up\b",
        r"\bmodal\b",
        r"\boverlay\b",
        r"\bbackdrop\b",
        r"\bfloating\b",
        r"\bsticky\s+floating\b",
        r"\bsticky\s+ad\b",
        r"quang\s+cao\s+(?:popup|noi|dinh|float)",
    ]

    if not _matches_any(normalized_text, popup_or_floating_patterns):
        return False

    limiting_patterns = [
        r"\bonly\b",
        r"\bjust\b",
        r"\bpopup\s*(?:/|and|&)?\s*floating\b",
        r"\bfloating\s*(?:/|and|&)?\s*popup\b",
        r"do\s+not\s+(?:try\s+to\s+)?block\s+all",
        r"do\s+not\s+block\s+(?:banner|native|bookmaker)",
        r"khong\s+block\s+tat\s+ca",
        r"khong\s+chan\s+tat\s+ca",
        r"chi\s+(?:block|chan)",
    ]

    preserve_non_popup_patterns = [
        r"\bbanner\b",
        r"\bnative\b",
        r"\bbookmaker\b",
        r"\bsponsor\b",
        r"nha\s+cai",
    ]

    return (
        _matches_any(normalized_text, limiting_patterns)
        or _matches_any(normalized_text, preserve_non_popup_patterns)
    )


def _mentions_do_not_block_banner_native_or_bookmaker(text: str) -> bool:
    normalized_text = _normalize_for_matching(text)

    patterns = [
        r"do\s+not\s+(?:try\s+to\s+)?block\s+(?:all\s+)?(?:banner|native|bookmaker|sponsor)",
        r"do\s+not\s+block\s+.*(?:banner|native|bookmaker|sponsor)",
        r"(?:banner|native|bookmaker|sponsor).*(?:should\s+remain|must\s+remain|preserve|keep)",
        r"khong\s+(?:block|chan)\s+.*(?:banner|native|nha\s+cai|sponsor)",
        r"giu\s+.*(?:banner|native|nha\s+cai|sponsor)",
    ]

    return _matches_any(normalized_text, patterns)


def _legacy_no_ticket_context() -> Dict[str, Any]:
    """
    Default context for old behavior when no ticket context is supplied.
    """
    problem_type = LEGACY_DEFAULT_PROBLEM_TYPE

    return {
        "platform": "",
        "problem_type": problem_type,
        "resolution_strategy": get_resolution_strategy(problem_type),
        "focus_region": "",
        "request": "",
        "description": "",
        "steps": [],
        "actual": "",
        "expected": "",
        "target_to_block": get_default_targets_to_block(problem_type),
        "target_to_preserve": get_default_targets_to_preserve(problem_type),
        "validation_hints": get_default_validation_hints(problem_type),
        "current_rules": [],
        "matched_rules": [],
        "blocked_resources": [],
        "evidence_level": "legacy_no_ticket_context",
        "raw": {},
    }


def _resolve_problem_type(
    raw_problem_type: Any,
    combined_text: str,
) -> str:
    raw_text = _clean_text(raw_problem_type)

    if raw_text:
        normalized = normalize_problem_type(raw_text, fallback="unknown")

        if normalized != "unknown":
            return normalized

        if combined_text:
            inferred = infer_problem_type(combined_text)
            return normalize_problem_type(inferred, fallback="unknown")

        return "unknown"

    inferred = infer_problem_type(combined_text)
    return normalize_problem_type(inferred, fallback="unknown")


def _extract_focus_region(context: Mapping[str, Any]) -> str:
    """
    Read the page region the crawler should actively scope to.

    Structured preserve/allowed region metadata is not converted into crawl
    focus, because scoping to a preserved region would make the detector inspect
    the thing we want to keep.
    """
    for key in ("focus_region", "focus"):
        value = context.get(key, "")
        if isinstance(value, str) and value.strip():
            return _clean_text(value)

    region_focus = context.get("region_focus", "")

    if isinstance(region_focus, str) and region_focus.strip():
        return _clean_text(region_focus)

    if isinstance(region_focus, Mapping):
        mode = _normalize_region_mode(region_focus)
        if mode and any(
            token in mode
            for token in ("preserve", "allow", "protect", "safe")
        ):
            return ""

        for key in ("focus_region", "region", "target", "description", "name"):
            value = region_focus.get(key, "")
            if isinstance(value, str) and value.strip():
                return _clean_text(value)

    return ""


def _normalize_region_mode(region_focus: Mapping[str, Any]) -> str:
    value = (
        region_focus.get("mode")
        or region_focus.get("type")
        or region_focus.get("role")
        or region_focus.get("intent")
        or ""
    )
    return _normalize_for_matching(str(value))


def _coerce_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return dict(value)

    return {
        "raw": value,
        "description": str(value),
    }


def _is_empty_context(context: Mapping[str, Any]) -> bool:
    if not context:
        return True

    meaningful_keys = [
        "platform",
        "problem_type",
        "resolution_strategy",
        "request",
        "description",
        "steps",
        "actual",
        "expected",
        "target_to_block",
        "target_to_preserve",
        "validation_hints",
        "current_rules",
        "existing_rules",
        "active_rules",
        "matched_rules",
        "blocked_resources",
        "region_focus",
        "focus_region",
        "focus",
    ]

    for key in meaningful_keys:
        value = context.get(key)

        if value is None:
            continue

        if isinstance(value, str) and value.strip():
            return False

        if isinstance(value, Mapping) and len(value) > 0:
            return False

        if isinstance(value, list) and len(value) > 0:
            return False

        if value not in ("", [], {}, None):
            return False

    return True


def _looks_like_legacy_default_context(context: Mapping[str, Any]) -> bool:
    """
    Detect a default/normalized context that came from URL-only legacy mode.

    This prevents a second normalization pass from relabeling:
      legacy_no_ticket_context -> url_only_best_effort
    """
    if not context:
        return True

    evidence_level = _clean_text(context.get("evidence_level", ""))

    if evidence_level == "legacy_no_ticket_context":
        return True

    problem_type = normalize_problem_type(
        context.get("problem_type", ""),
        fallback="unknown",
    )

    if problem_type and problem_type not in {"unknown", LEGACY_DEFAULT_PROBLEM_TYPE}:
        return False

    text_fields = [
        "platform",
        "request",
        "description",
        "actual",
        "expected",
    ]

    for key in text_fields:
        if _clean_text(context.get(key, "")):
            return False

    steps = context.get("steps", [])
    if isinstance(steps, list) and steps:
        return False
    if isinstance(steps, str) and steps.strip():
        return False

    for key in (
        "current_rules",
        "existing_rules",
        "active_rules",
        "matched_rules",
        "blocked_resources",
    ):
        value = context.get(key)

        if isinstance(value, list) and value:
            return False

        if isinstance(value, str) and value.strip():
            return False

    raw = context.get("raw", {})
    if isinstance(raw, Mapping) and len(raw) > 0:
        return False

    target_to_block = _normalize_string_list(context.get("target_to_block", []))
    target_to_preserve = _normalize_string_list(context.get("target_to_preserve", []))
    validation_hints = context.get("validation_hints", {})

    default_targets_to_block = get_default_targets_to_block(LEGACY_DEFAULT_PROBLEM_TYPE)
    default_targets_to_preserve = get_default_targets_to_preserve(
        LEGACY_DEFAULT_PROBLEM_TYPE
    )

    has_default_targets = (
        target_to_block == default_targets_to_block
        and target_to_preserve == default_targets_to_preserve
    )

    hints_empty = not isinstance(validation_hints, Mapping) or len(validation_hints) == 0

    if has_default_targets and hints_empty:
        return True

    return False


def _merge_validation_hints(
    inferred_hints: Mapping[str, Any],
    provided_hints: Mapping[str, Any],
) -> Dict[str, Any]:
    """
    Merge inferred/default validation hints with provided ticket hints.

    Provided hints win because CMS/debug payload should be more specific.
    """
    merged = _make_json_safe(dict(inferred_hints))

    for key, value in provided_hints.items():
        if key == "must_not_generate_rules":
            merged[key] = _dedupe_strings(
                _normalize_string_list(merged.get(key, []))
                + _normalize_string_list(value)
            )
        elif (
            key in {"allowed_candidate_categories", "disallowed_candidate_categories"}
            and key in merged
        ):
            merged[key] = _dedupe_strings(
                _normalize_string_list(merged.get(key, []))
                + _normalize_string_list(value)
            )
        else:
            merged[str(key)] = _make_json_safe(value)

    return merged


def _clean_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_platform(value: Any) -> str:
    text = _normalize_for_matching(value)

    if text in {"ios", "iphone", "ipad"}:
        return "ios"

    if text in {"android", "mobile android"}:
        return "android"

    if text in {"desktop", "windows", "mac", "linux"}:
        return "desktop"

    return text


def _normalize_steps(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, str):
        lines = [
            _clean_text(line)
            for line in re.split(r"\n+|(?:\d+\.\s+)", value)
            if _clean_text(line)
        ]
        return lines

    if isinstance(value, list):
        return [
            _clean_text(item)
            for item in value
            if _clean_text(item)
        ]

    return [_clean_text(value)] if _clean_text(value) else []


def _normalize_string_list(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, str):
        parts = [
            _clean_text(part)
            for part in re.split(r"[,;\n]+", value)
            if _clean_text(part)
        ]
        return parts

    if isinstance(value, list):
        return [
            _clean_text(item)
            for item in value
            if _clean_text(item)
        ]

    return [_clean_text(value)] if _clean_text(value) else []


def _collect_rule_list(
    context: Mapping[str, Any],
    keys: tuple[str, ...],
) -> List[str]:
    """
    Collect existing/current rules from several possible CMS field names.
    """
    for key in keys:
        value = context.get(key)

        if isinstance(value, list):
            rules: List[str] = []

            for item in value:
                if isinstance(item, Mapping):
                    rule = (
                        item.get("rule")
                        or item.get("matched_rule")
                        or item.get("filter")
                        or item.get("text")
                        or ""
                    )
                    if _clean_text(rule):
                        rules.append(_clean_text(rule))
                elif _clean_text(item):
                    rules.append(_clean_text(item))

            return rules

        if isinstance(value, str) and value.strip():
            return [
                line.strip()
                for line in value.splitlines()
                if line.strip()
            ]

    return []


def _normalize_matched_rules(value: Any) -> List[Dict[str, Any]]:
    """
    Normalize matched/suspect rule evidence while preserving useful notes.
    """
    if value is None:
        return []

    if isinstance(value, str):
        return [
            {"rule": rule}
            for rule in _normalize_string_list(value)
        ]

    if isinstance(value, list):
        result: List[Dict[str, Any]] = []

        for item in value:
            if isinstance(item, Mapping):
                rule = _clean_text(
                    item.get("rule")
                    or item.get("matched_rule")
                    or item.get("filter")
                    or item.get("text")
                    or ""
                )

                if not rule:
                    continue

                normalized_item: Dict[str, Any] = {"rule": rule}

                for key in (
                    "problem",
                    "reason",
                    "action",
                    "resource_type",
                    "url",
                    "selector",
                    "evidence",
                ):
                    if key in item and _clean_text(item.get(key, "")):
                        normalized_item[key] = _clean_text(item.get(key, ""))

                result.append(normalized_item)

            elif _clean_text(item):
                result.append({"rule": _clean_text(item)})

        return result

    return []


def _normalize_blocked_resources(value: Any) -> List[Dict[str, Any]]:
    """
    Normalize blocked resource records.

    Accepts either:
      ["https://cdn.example.com/img.jpg"]

    or:
      [{"url": "...", "resource_type": "image", "matched_rule": "..."}]
    """
    if value is None:
        return []

    if isinstance(value, str):
        return [
            {"url": item}
            for item in _normalize_string_list(value)
        ]

    if isinstance(value, list):
        result = []

        for item in value:
            if isinstance(item, Mapping):
                result.append(
                    {
                        "url": _clean_text(item.get("url", "")),
                        "resource_type": _clean_text(item.get("resource_type", "")),
                        "matched_rule": _clean_text(item.get("matched_rule", "")),
                        "reason": _clean_text(item.get("reason", "")),
                    }
                )
            elif _clean_text(item):
                result.append({"url": _clean_text(item)})

        return [
            item
            for item in result
            if item.get("url")
        ]

    return []


def _copy_passthrough_fields(
    normalized: Dict[str, Any],
    context: Mapping[str, Any],
) -> None:
    """
    Preserve integration fields that this module should forward but not interpret.

    This keeps ticket_context compatible with teammate-owned features such as
    region_focus without implementing that feature here.
    """
    for key in (
        "name",
        "ticket_id",
        "issue_id",
        "report_id",
        "severity",
        "browser_version",
        "os_version",
        "device",
        "screenshot_url",
        "notes",
        "region_focus",
        "focus_selectors",
        "preserve_regions",
        "target_regions",
        "allowed_regions",
        "blocked_regions",
    ):
        if key in context:
            normalized[key] = _make_json_safe(context.get(key))


def _infer_evidence_level(
    current_rules: List[str],
    matched_rules: List[Dict[str, Any]],
    blocked_resources: List[Dict[str, Any]],
    has_explicit_context: bool,
) -> str:
    """
    Describe how strong the generation evidence is.
    """
    if matched_rules or blocked_resources:
        return "observed_rule_or_resource_context"

    if current_rules:
        return "current_rules_context"

    if has_explicit_context:
        return "url_only_best_effort"

    return "legacy_no_ticket_context"


def _dedupe_strings(values: List[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()

    for value in values:
        cleaned = _clean_text(value)
        if not cleaned:
            continue

        key = cleaned.lower()
        if key in seen:
            continue

        seen.add(key)
        result.append(cleaned)

    return result


def _normalize_for_matching(value: Any) -> str:
    text = _clean_text(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("đ", "d")
    return re.sub(r"\s+", " ", text).strip()


def _matches_any(text: str, patterns: List[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _make_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        return {
            str(key): _make_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            _make_json_safe(item)
            for item in value
        ]

    return str(value)
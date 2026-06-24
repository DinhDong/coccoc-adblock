import re
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

    Important behavior:
    - If no ticket_context is provided, fallback to legacy ad-blocking mode:
        problem_type = specific_ad_not_blocked
        resolution_strategy = block_visible_ad

      This keeps backward compatibility with the old pipeline:
        crawl URL -> detect ad signals -> generate block/hide rules.

    - If the context is already the normalized legacy default, keep:
        evidence_level = legacy_no_ticket_context

      This prevents a second normalization pass from relabeling it as:
        url_only_best_effort

    - Problem policy is centralized in:
        app.services.problem_policy

      So adding a new problem type should usually be done there first.
    """
    context = _coerce_dict(raw_context)

    if _is_empty_context(context) or _looks_like_legacy_default_context(context):
        return _legacy_no_ticket_context()

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

    # If context exists but does not give enough information to infer a ticket
    # type, fall back to legacy ad-blocking behavior.
    #
    # Example:
    #   {"platform": "android"}
    #
    # This should still behave like:
    #   crawl -> detect ad signals -> generate block/hide rules.
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

    matched_rules = _normalize_string_list(context.get("matched_rules", []))
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

    # Preserve useful integration fields from CMS if they exist.
    for key in (
        "ticket_id",
        "issue_id",
        "report_id",
        "severity",
        "browser_version",
        "os_version",
        "device",
        "screenshot_url",
        "notes",
    ):
        if key in context:
            normalized[key] = _make_json_safe(context.get(key))

    return _make_json_safe(normalized)


def infer_problem_type(text: str) -> str:
    """
    Infer the ticket type from user-facing ticket text.

    The returned type is normalized through problem_policy aliases.
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
        r"\bảnh\b",
        r"không\s+hiện\s+ảnh",
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
        r"không\s+xem\s+được",
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
        r"\bẩn\b",
        r"không\s+thấy",
        r"không\s+hiển\s+thị",
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
        r"không\s+đóng",
        r"không\s+tắt",
        r"tắt\s+adblock",
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
        r"quảng\s+cáo\s+(?:vẫn\s+)?(?:hiện|xuất\s+hiện)",
    ]
    if _matches_any(normalized, ad_not_blocked_patterns):
        return normalize_problem_type("specific_ad_not_blocked")

    broken_content_patterns = [
        r"\bbroken\b",
        r"\bblank\b",
        r"\bwhite\s+screen\b",
        r"\bnot\s+working\b",
        r"\bdoesn'?t\s+work\b",
        r"không\s+hoạt\s+động",
        r"lỗi",
    ]
    if _matches_any(normalized, broken_content_patterns):
        return normalize_problem_type("content_broken")

    return "unknown"


def infer_targets_to_block(problem_type: str, text: str) -> List[str]:
    """
    Get default block targets from centralized problem policy.
    """
    normalized_problem_type = normalize_problem_type(problem_type, fallback="unknown")
    return get_default_targets_to_block(normalized_problem_type)


def infer_targets_to_preserve(problem_type: str, text: str) -> List[str]:
    """
    Get default preserve targets from centralized problem policy.
    """
    normalized_problem_type = normalize_problem_type(problem_type, fallback="unknown")
    return get_default_targets_to_preserve(normalized_problem_type)


def infer_validation_hints(problem_type: str, text: str) -> Dict[str, Any]:
    """
    Get default validation hints from centralized problem policy.
    """
    normalized_problem_type = normalize_problem_type(problem_type, fallback="unknown")
    return get_default_validation_hints(normalized_problem_type)


def _legacy_no_ticket_context() -> Dict[str, Any]:
    """
    Default context for old behavior when no ticket context is supplied.

    This keeps the pipeline backward-compatible:
      URL only -> generate rules to block detected ad-related signals.
    """
    problem_type = LEGACY_DEFAULT_PROBLEM_TYPE

    return {
        "platform": "",
        "problem_type": problem_type,
        "resolution_strategy": get_resolution_strategy(problem_type),
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
    """
    Resolve problem type from explicit ticket field first, then infer from text.
    """
    raw_text = _clean_text(raw_problem_type)

    if raw_text:
        normalized = normalize_problem_type(raw_text, fallback="unknown")

        if normalized != "unknown":
            return normalized

        # If the provided type is unknown but there is useful text, infer from
        # ticket content instead of keeping unknown.
        if combined_text:
            inferred = infer_problem_type(combined_text)
            return normalize_problem_type(inferred, fallback="unknown")

        return "unknown"

    inferred = infer_problem_type(combined_text)
    return normalize_problem_type(inferred, fallback="unknown")


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
    """
    Return True when no useful ticket context was provided.
    """
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

    # If the user/report has actual text fields, this is explicit context,
    # not the legacy empty fallback.
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

    # If debug evidence exists, it is not legacy-empty context.
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
        merged[str(key)] = _make_json_safe(value)

    return merged


def _clean_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_platform(value: Any) -> str:
    text = _clean_text(value).lower()

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
            return [
                _clean_text(rule)
                for rule in value
                if _clean_text(rule)
            ]

        if isinstance(value, str) and value.strip():
            return [
                line.strip()
                for line in value.splitlines()
                if line.strip()
            ]

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


def _infer_evidence_level(
    current_rules: List[str],
    matched_rules: List[str],
    blocked_resources: List[Dict[str, Any]],
    has_explicit_context: bool,
) -> str:
    """
    Describe how strong the generation evidence is.

    This is helpful for moderator review.
    """
    if matched_rules or blocked_resources:
        return "observed_rule_or_resource_context"

    if current_rules:
        return "current_rules_context"

    if has_explicit_context:
        return "url_only_best_effort"

    return "legacy_no_ticket_context"


def _normalize_for_matching(text: str) -> str:
    return _clean_text(text).lower()


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
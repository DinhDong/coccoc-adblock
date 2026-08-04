"""
Test the per-rule visual preview contract that the moderator CMS consumes.

These cover the pure parsing/serialization helpers only — the measurement
itself needs a live browser and is exercised by a sandbox run.

Run from backend/:
    .venv\\Scripts\\activate
    python -m app.tests.test_rule_preview_metadata
    pytest app/tests/test_rule_preview_metadata.py -v
"""

from app.services.workflow import (
    _build_preview_capture,
    _collect_rule_previews,
    _serialize_outcome,
    _serialize_preview,
)
from app.validator.sandbox_check import (
    SandboxResult,
    _preview_box,
    _preview_cosmetic_selectors,
)

PAGE_URL = "https://example.com/article"


class _FakeOutcome:
    """Stand-in for RuleValidationOutcome — only the read attributes matter."""

    def __init__(self, rule, sandbox=None, passed=True):
        self.rule = rule
        self.passed = passed
        self.sandbox = sandbox
        self.syntax = None
        self.scope = None
        self.policy = None
        self.failure_stage = ""
        self.failure_reason = ""


def test_cosmetic_selector_extraction_respects_domain_scope():
    assert _preview_cosmetic_selectors("example.com##.ad", PAGE_URL) == [".ad"]

    # Exception rules still need a preview: reviewers must see the region a
    # #@# rule keeps visible.
    assert _preview_cosmetic_selectors("example.com#@#.ad", PAGE_URL) == [".ad"]

    # A rule scoped to a different site does not apply to this page.
    assert _preview_cosmetic_selectors("other.com##.ad", PAGE_URL) == []

    # Negated domain.
    assert _preview_cosmetic_selectors("~example.com##.ad", PAGE_URL) == []

    # Undomained rules apply everywhere.
    assert _preview_cosmetic_selectors("##.ad", PAGE_URL) == [".ad"]

    # Network rules are not cosmetic.
    assert _preview_cosmetic_selectors("||ads.example.com^", PAGE_URL) == []

    print("  PASS - cosmetic selector extraction respects domain scope")


def test_preview_box_rounds_to_ints():
    box = _preview_box({"top": 40.6, "left": 20, "width": 300.4, "height": 250})

    assert box == {"top": 40, "left": 20, "width": 300, "height": 250}
    assert all(isinstance(value, int) for value in box.values())

    # Missing keys must not raise; a partial rect degrades to zeros.
    assert _preview_box({}) == {"top": 0, "left": 0, "width": 0, "height": 0}

    print("  PASS - boxes normalize to rounded ints")


def test_serialize_preview_normalizes_shape():
    serialized = _serialize_preview(
        {
            "boxes": [
                {"top": 10.2, "left": 5.8, "width": 100, "height": 50},
                "not-a-box",
            ],
            "evidence_urls": ["https://ads.example.com/t.js"],
            "truncated": True,
        }
    )

    assert serialized == {
        "boxes": [{"top": 10, "left": 6, "width": 100, "height": 50}],
        "evidence_urls": ["https://ads.example.com/t.js"],
        "truncated": True,
    }

    # An empty preview is still a complete, well-formed block.
    assert _serialize_preview({}) == {
        "boxes": [],
        "evidence_urls": [],
        "truncated": False,
    }

    print("  PASS - preview serialization normalizes shape")


def test_outcome_omits_preview_when_rule_was_never_measured():
    measured = _serialize_outcome(
        _FakeOutcome("example.com##.ad"),
        {"example.com##.ad": {"boxes": [], "evidence_urls": [], "truncated": False}},
    )
    assert "preview" in measured

    # Rules rejected at syntax/scope/policy never reach the sandbox.
    unmeasured = _serialize_outcome(_FakeOutcome("example.com##.ad"), {})
    assert "preview" not in unmeasured

    # Callers predating the preview feature must keep working.
    legacy = _serialize_outcome(_FakeOutcome("example.com##.ad"))
    assert "preview" not in legacy

    print("  PASS - preview omitted for rules the sandbox never measured")


def test_collect_rule_previews_merges_per_rule_and_combined():
    failed = SandboxResult(url=PAGE_URL, passed=False)
    failed.rule_previews = {
        "example.com##.too-broad": {
            "boxes": [{"top": 0, "left": 0, "width": 10, "height": 10}],
            "evidence_urls": [],
            "truncated": False,
        }
    }

    combined = SandboxResult(url=PAGE_URL, passed=True)
    combined.rule_previews = {
        "example.com##.ad": {"boxes": [], "evidence_urls": [], "truncated": False}
    }

    previews = _collect_rule_previews(
        combined,
        [_FakeOutcome("example.com##.too-broad", failed, passed=False)],
    )

    # Rules that failed the sandbox still get a preview, so a reviewer can see
    # what the rejected rule would have hit.
    assert set(previews) == {"example.com##.ad", "example.com##.too-broad"}

    print("  PASS - previews merge across per-rule and combined runs")


def test_preview_capture_absent_without_sandbox():
    # --no-sandbox and syntax-only runs produce no capture block at all.
    assert _build_preview_capture(None, [_FakeOutcome("example.com##.ad")], "") == {}

    combined = SandboxResult(url=PAGE_URL, passed=True)
    combined.preview_capture = {
        "page_width": 1920,
        "page_height": 4130,
        "environment": "desktop",
    }

    capture = _build_preview_capture(combined, [], "data/x_before.png")

    assert capture == {
        "page_width": 1920,
        "page_height": 4130,
        "environment": "desktop",
        "before_screenshot": "data/x_before.png",
    }

    print("  PASS - preview_capture omitted when the sandbox did not run")


if __name__ == "__main__":
    print(f"\n{'=' * 60}")
    print("  Rule preview metadata contract")
    print(f"{'=' * 60}\n")

    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()

    print("\nAll preview metadata tests passed.\n")

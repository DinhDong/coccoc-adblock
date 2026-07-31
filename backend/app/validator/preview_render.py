"""
Draw measured rule-preview boxes onto the clean "before" screenshot.

This is a *viewing aid*. The pipeline still saves <report_id>_before.png clean —
this module writes a separate <report_id>_before_boxed.png so the coordinates in
the validation JSON stay the single source of truth and a future CMS can render
its own interactive overlays on the clean image without double-drawing.

Usage (CLI) — re-render any completed report without re-running the pipeline:
    python -m app.validator.preview_render <report_id>
    python -m app.validator.preview_render <report_id> --out somewhere.png

Usage (programmatic):
    from app.validator.preview_render import render_preview_overlay

    png_bytes = render_preview_overlay(
        before_png=Path("..._before.png").read_bytes(),
        rule_previews={"site.com##.ad": {"boxes": [...], ...}},
        preview_capture={"page_width": 1920, "page_height": 4826},
    )
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# One stable colour per rule, assigned by position in the preview mapping.
#
# Ordered so that consecutive entries are maximally different: adjacent rule
# numbers are the ones most likely to overlap on the page, and a moderator has
# to tell them apart at a glance.
PALETTE: Sequence[Tuple[int, int, int]] = (
    (239, 68, 68),     # red
    (59, 130, 246),    # blue
    (132, 204, 22),    # lime
    (168, 85, 247),    # purple
    (249, 115, 22),    # orange
    (6, 182, 212),     # cyan
    (236, 72, 153),    # pink
    (16, 185, 129),    # emerald
    (245, 158, 11),    # amber
    (99, 102, 241),    # indigo
    (190, 242, 100),   # pale lime
    (217, 70, 239),    # fuchsia
    (20, 184, 166),    # teal
    (248, 113, 113),   # salmon
    (14, 165, 233),    # sky
    (163, 230, 53),    # yellow-green
    (139, 92, 246),    # violet
    (234, 179, 8),     # yellow
    (34, 197, 94),     # green
    (244, 63, 94),     # rose
    (2, 132, 199),     # deep blue
    (202, 138, 4),     # bronze
    (192, 132, 252),   # lavender
    (5, 150, 105),     # deep emerald
)

# Past the palette, later cycles reuse the same hues at a different lightness so
# rule 25 is still visually distinct from rule 1 instead of identical to it.
_CYCLE_LIGHTNESS_STEPS = (0.0, 0.26, -0.20, 0.44, -0.34)

FILL_ALPHA = 56
BORDER_WIDTH = 4
# Below this displayed size a box is unreadable as a rectangle (tracking pixels,
# 1x1 beacons), so it gets a ring marker instead.
TINY_BOX_PX = 14

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)


def render_preview_overlay(
    before_png: bytes,
    rule_previews: Mapping[str, Mapping[str, Any]],
    preview_capture: Mapping[str, Any],
) -> bytes:
    """
    Return PNG bytes of the before screenshot with every rule's boxes drawn.

    Returns b"" when Pillow is unavailable or there is nothing to draw, so the
    caller can treat the overlay as strictly optional.
    """
    if not before_png or not rule_previews:
        return b""

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed — skipping boxed preview render")
        return b""

    import io

    try:
        source = Image.open(io.BytesIO(before_png)).convert("RGB")
    except Exception as exc:
        logger.warning("Could not open before screenshot for overlay: %s", exc)
        return b""

    page_width = int(preview_capture.get("page_width", 0) or 0)

    # The screenshot is in device pixels; boxes are in CSS pixels. This ratio is
    # the device_scale_factor, and is exactly the scale the CMS computes as
    # displayedImgWidth / page_width.
    scale = (source.size[0] / page_width) if page_width > 0 else 1.0

    entries = _legend_entries(rule_previews)
    legend_height = _legend_height(len(entries))

    canvas = Image.new(
        "RGB",
        (source.size[0], source.size[1] + legend_height),
        (24, 24, 27),
    )
    canvas.paste(source, (0, legend_height))

    draw = ImageDraw.Draw(canvas, "RGBA")

    _draw_boxes(draw, entries, scale, legend_height)
    _draw_legend(draw, entries, canvas.size[0], legend_height, scale)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def _legend_entries(
    rule_previews: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []

    for index, (rule, preview) in enumerate(rule_previews.items()):
        boxes = [
            box
            for box in (preview.get("boxes") or [])
            if isinstance(box, Mapping)
        ]

        entries.append(
            {
                "index": index + 1,
                "rule": str(rule),
                "color": color_for_index(index),
                "boxes": boxes,
                "truncated": bool(preview.get("truncated", False)),
                "evidence_count": len(preview.get("evidence_urls") or []),
            }
        )

    return entries


def color_for_index(index: int) -> Tuple[int, int, int]:
    """
    Return a stable, distinguishable colour for the nth rule of a report.

    The first len(PALETTE) rules get the hand-tuned palette. Beyond that the
    palette repeats at a shifted lightness, so any number of rules stays
    separable rather than wrapping onto identical colours.
    """
    import colorsys

    base = PALETTE[index % len(PALETTE)]
    cycle = index // len(PALETTE)

    if cycle == 0:
        return base

    shift = _CYCLE_LIGHTNESS_STEPS[cycle % len(_CYCLE_LIGHTNESS_STEPS)]

    hue, lightness, saturation = colorsys.rgb_to_hls(
        base[0] / 255,
        base[1] / 255,
        base[2] / 255,
    )

    # Keep it off pure black/white so the border stays visible either way.
    lightness = min(0.86, max(0.22, lightness + shift))

    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)

    return (round(red * 255), round(green * 255), round(blue * 255))


def _draw_boxes(
    draw: Any,
    entries: Sequence[Mapping[str, Any]],
    scale: float,
    y_offset: int,
) -> None:
    badge_font = _load_font(20)

    for entry in entries:
        color = tuple(entry["color"])

        for box in entry["boxes"]:
            left = float(box.get("left", 0) or 0) * scale
            top = float(box.get("top", 0) or 0) * scale + y_offset
            width = float(box.get("width", 0) or 0) * scale
            height = float(box.get("height", 0) or 0) * scale
            right = left + width
            bottom = top + height

            if width < TINY_BOX_PX or height < TINY_BOX_PX:
                # A 1x1 beacon is invisible as a rectangle; ring it instead.
                radius = 16
                draw.ellipse(
                    [left - radius, top - radius, right + radius, bottom + radius],
                    outline=color + (255,),
                    width=BORDER_WIDTH,
                )
                badge_anchor = (right + radius, top - radius)
            else:
                draw.rectangle(
                    [left, top, right, bottom],
                    fill=color + (FILL_ALPHA,),
                    outline=color + (255,),
                    width=BORDER_WIDTH,
                )
                badge_anchor = (left, top)

            _draw_badge(draw, badge_anchor, str(entry["index"]), color, badge_font)


def _draw_badge(
    draw: Any,
    anchor: Tuple[float, float],
    text: str,
    color: Tuple[int, int, int],
    font: Any,
) -> None:
    """Small numbered tag keying a box back to its legend row."""
    width = 16 + 11 * len(text)
    height = 28
    x, y = anchor
    y = max(0, y - height)

    draw.rectangle([x, y, x + width, y + height], fill=color + (255,))
    draw.text((x + 8, y + 4), text, fill=(255, 255, 255), font=font)


def _draw_legend(
    draw: Any,
    entries: Sequence[Mapping[str, Any]],
    canvas_width: int,
    legend_height: int,
    scale: float,
) -> None:
    title_font = _load_font(26)
    row_font = _load_font(21)

    draw.text(
        (24, 18),
        f"Rule preview — {len(entries)} rule(s) measured on the reference page",
        fill=(244, 244, 245),
        font=title_font,
    )

    columns = _legend_columns(len(entries))
    rows = _legend_rows(len(entries), columns)
    column_width = (canvas_width - 48) // columns

    # Long rules must not run into the next column.
    max_chars = max(24, int(column_width / 11) - len("    [12 regions]"))

    for position, entry in enumerate(entries):
        column = position // rows
        row = position % rows

        x = 24 + column * column_width
        y = 60 + row * 34

        color = tuple(entry["color"])
        count = len(entry["boxes"])

        draw.rectangle([x, y + 4, x + 24, y + 28], fill=color + (255,))
        _draw_badge(draw, (x + 36, y + 32), str(entry["index"]), color, row_font)

        if count:
            detail = f"{count} region{'' if count == 1 else 's'}"
            if entry["truncated"]:
                detail += " (capped)"
        elif entry["evidence_count"]:
            detail = f"no visual footprint — {entry['evidence_count']} request(s)"
        else:
            detail = "no match"

        rule_text = entry["rule"]
        if len(rule_text) > max_chars:
            rule_text = rule_text[: max_chars - 1] + "…"

        draw.text(
            (x + 84, y + 6),
            f"{rule_text}    [{detail}]",
            fill=(228, 228, 231),
            font=row_font,
        )

    draw.text(
        (24, 60 + rows * 34 + 8),
        f"Boxes are CSS pixels from the validation JSON, scaled x{scale:.3f} to this image. "
        "The unannotated original is saved alongside as _before.png.",
        fill=(161, 161, 170),
        font=row_font,
    )


def _legend_columns(rule_count: int) -> int:
    """Keep the legend from growing into a wall on rule-heavy reports."""
    if rule_count <= 12:
        return 1

    return 2 if rule_count <= 30 else 3


def _legend_rows(rule_count: int, columns: int) -> int:
    return -(-rule_count // columns)  # ceil


def _legend_height(rule_count: int) -> int:
    rows = _legend_rows(rule_count, _legend_columns(rule_count))
    return 60 + rows * 34 + 48


def _load_font(size: int) -> Any:
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue

    return ImageFont.load_default()


def render_report_overlay(
    report_id: str,
    validation_dir: Any = None,
    screenshots_dir: Any = None,
) -> Optional[bytes]:
    """
    Re-render a finished report's overlay from files on disk.

    Returns None when the report has no preview data (for example a
    --no-sandbox run).
    """
    import json
    from pathlib import Path

    validation_dir = Path(validation_dir or "data/rule_outputs/validation")
    screenshots_dir = Path(screenshots_dir or "data/rule_outputs/screenshots")

    validation_path = validation_dir / f"{report_id}_validation.json"

    if not validation_path.exists():
        raise FileNotFoundError(f"Validation JSON not found: {validation_path}")

    data = json.loads(validation_path.read_text(encoding="utf-8"))
    capture = data.get("preview_capture")

    if not capture:
        logger.warning("Report %s has no preview_capture — nothing to draw", report_id)
        return None

    before_path = screenshots_dir / f"{report_id}_before.png"

    if not before_path.exists():
        raise FileNotFoundError(f"Before screenshot not found: {before_path}")

    rule_previews = {
        outcome.get("rule", ""): outcome["preview"]
        for outcome in data.get("outcomes", [])
        if isinstance(outcome, Mapping) and outcome.get("preview")
    }

    return render_preview_overlay(
        before_png=before_path.read_bytes(),
        rule_previews=rule_previews,
        preview_capture=capture,
    )


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description=(
            "Draw a report's measured rule-preview boxes onto its clean "
            "before screenshot."
        ),
    )
    parser.add_argument("report_id", help="Report ID of a completed validation run")
    parser.add_argument(
        "--out",
        default="",
        metavar="FILE",
        help="Output PNG (default: data/rule_outputs/screenshots/<id>_before_boxed.png)",
    )
    args = parser.parse_args()

    try:
        png = render_report_overlay(args.report_id)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not png:
        print(
            f"No preview data for report '{args.report_id}' — "
            "was it run with the sandbox enabled?",
            file=sys.stderr,
        )
        sys.exit(1)

    out_path = Path(
        args.out
        or f"data/rule_outputs/screenshots/{args.report_id}_before_boxed.png"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(png)

    print(f"Wrote {out_path} ({len(png) // 1024} KB)")

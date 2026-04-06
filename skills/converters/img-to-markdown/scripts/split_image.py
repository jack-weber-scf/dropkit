#!/usr/bin/env python3
"""
split_image.py — Two-pass image splitter for diagram ingestion.

Pass 1 (overview): Generates a single downscaled overview image for
structural analysis by the LLM.

Pass 2 (detail): Uses a sliding window with configurable viewport and
stride to produce overlapping tiles that guarantee every point in the
source image is fully contained in at least one tile.

Part of the diagram-ingestion skill. Designed to be invoked by Kiro,
Claude Code, or any agentic coding assistant that supports the
agentskills.io standard and the AWS Document Loader MCP server.

Usage — overview pass:
    python scripts/split_image.py overview \
        --input diagram.png \
        --output-dir ./processing/tiles \
        --max-dim 1200

Usage — detail pass:
    python scripts/split_image.py detail \
        --input diagram.png \
        --output-dir ./processing/tiles \
        --viewport 1200 \
        --stride 800 \
        [--focus-regions regions.json]

Usage — recommend settings:
    python scripts/split_image.py recommend \
        --input diagram.png

Outputs:
    overview mode  → overview.png + overview_manifest.json
    detail mode    → tile_W<n>_R<row>_C<col>.png + detail_manifest.json
    recommend mode → JSON to stdout
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError:
    print(
        "ERROR: Pillow is required. Install with:\n"
        "  pip install Pillow\n"
        "  uv pip install Pillow",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Constants ────────────────────────────────────────────────────────────

SUPPORTED_FORMATS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp", ".gif"}
DEFAULT_MAX_DIM = 1200       # px — overview target
DEFAULT_VIEWPORT = 1200      # px — detail window size
DEFAULT_STRIDE = 800         # px — detail step size
MIN_STRIDE = 200             # px — floor to prevent excessive tiles
MAX_TILES_WARN = 100         # warn if detail pass would exceed this


# ── Helpers ──────────────────────────────────────────────────────────────

def _validate_image(path: Path) -> Image.Image:
    if not path.exists():
        sys.exit(f"ERROR: File not found: {path}")
    if path.suffix.lower() not in SUPPORTED_FORMATS:
        sys.exit(f"ERROR: Unsupported format: {path.suffix}")
    return Image.open(path)


def _position_label(x: int, y: int, w: int, h: int, img_w: int, img_h: int) -> str:
    cx, cy = x + w // 2, y + h // 2
    v = "top" if cy < img_h / 3 else ("bottom" if cy > 2 * img_h / 3 else "middle")
    hz = "left" if cx < img_w / 3 else ("right" if cx > 2 * img_w / 3 else "center")
    if v == "middle" and hz == "center":
        return "center"
    return f"{v}-{hz}"


def _overlap_pct(viewport: int, stride: int) -> float:
    return round((viewport - stride) / viewport, 3)


# ── Pass 1: Overview ────────────────────────────────────────────────────

def generate_overview(
    input_path: Path,
    output_dir: Path,
    max_dim: int = DEFAULT_MAX_DIM,
) -> dict[str, Any]:
    """Produce a single downscaled image for structural/layout analysis."""
    output_dir.mkdir(parents=True, exist_ok=True)
    img = _validate_image(input_path)
    orig_w, orig_h = img.size

    # Determine if downscale is needed
    scale = min(max_dim / orig_w, max_dim / orig_h, 1.0)
    new_w = int(orig_w * scale)
    new_h = int(orig_h * scale)

    overview = img.resize((new_w, new_h), Image.LANCZOS)
    out_path = output_dir / "overview.png"
    overview.save(out_path, format="PNG")

    manifest = {
        "mode": "overview",
        "source_image": str(input_path.resolve()),
        "source_dimensions": {"width": orig_w, "height": orig_h},
        "overview_dimensions": {"width": new_w, "height": new_h},
        "scale_factor": round(scale, 4),
        "output_file": str(out_path.resolve()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    manifest_path = output_dir / "overview_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


# ── Pass 2: Detail (sliding window) ─────────────────────────────────────

def generate_detail_tiles(
    input_path: Path,
    output_dir: Path,
    viewport: int = DEFAULT_VIEWPORT,
    stride: int = DEFAULT_STRIDE,
    focus_regions: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Sliding-window tiling with guaranteed full coverage.

    Every pixel in the source image appears fully inside at least one tile.
    If *focus_regions* is provided (list of {x, y, w, h} dicts in source
    coordinates), an additional set of targeted crops is generated for those
    regions at full resolution, padded to *viewport* size.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    img = _validate_image(input_path)
    img_w, img_h = img.size

    stride = max(stride, MIN_STRIDE)
    if stride >= viewport:
        sys.exit(
            f"ERROR: stride ({stride}) must be smaller than viewport ({viewport}) "
            "to guarantee overlap."
        )

    # ── Sliding window grid ──────────────────────────────────────────
    tiles: list[dict] = []
    row_idx = 0

    y = 0
    while y < img_h:
        col_idx = 0
        x = 0
        # Snap last row/col so the window doesn't exceed image bounds
        if y + viewport > img_h:
            y = max(0, img_h - viewport)

        while x < img_w:
            if x + viewport > img_w:
                x = max(0, img_w - viewport)

            x1 = min(x + viewport, img_w)
            y1 = min(y + viewport, img_h)
            crop_w = x1 - x
            crop_h = y1 - y

            tile_img = img.crop((x, y, x1, y1))
            fname = f"tile_W0_R{row_idx}_C{col_idx}.png"
            tile_img.save(output_dir / fname, format="PNG")

            tiles.append({
                "filename": fname,
                "window": 0,
                "row": row_idx,
                "col": col_idx,
                "crop_box": {"x": x, "y": y, "w": crop_w, "h": crop_h},
                "position_label": _position_label(x, y, crop_w, crop_h, img_w, img_h),
                "source": "sliding_window",
            })

            col_idx += 1
            if x + viewport >= img_w:
                break
            x += stride

        row_idx += 1
        if y + viewport >= img_h:
            break
        y += stride

    # ── Focus-region crops ───────────────────────────────────────────
    if focus_regions:
        for idx, region in enumerate(focus_regions):
            rx, ry = region["x"], region["y"]
            rw, rh = region["w"], region["h"]

            # Pad to at least viewport size, centred on region
            pad_w = max(viewport, rw + 100)
            pad_h = max(viewport, rh + 100)
            cx, cy = rx + rw // 2, ry + rh // 2
            fx0 = max(0, cx - pad_w // 2)
            fy0 = max(0, cy - pad_h // 2)
            fx1 = min(img_w, fx0 + pad_w)
            fy1 = min(img_h, fy0 + pad_h)
            # Re-adjust origin if we hit the edge
            fx0 = max(0, fx1 - pad_w)
            fy0 = max(0, fy1 - pad_h)

            tile_img = img.crop((fx0, fy0, fx1, fy1))
            fname = f"tile_focus_{idx}.png"
            tile_img.save(output_dir / fname, format="PNG")

            tiles.append({
                "filename": fname,
                "window": -1,
                "row": -1,
                "col": -1,
                "crop_box": {"x": fx0, "y": fy0, "w": fx1 - fx0, "h": fy1 - fy0},
                "position_label": _position_label(fx0, fy0, fx1 - fx0, fy1 - fy0, img_w, img_h),
                "source": "focus_region",
                "focus_index": idx,
                "original_region": region,
            })

    manifest = {
        "mode": "detail",
        "source_image": str(input_path.resolve()),
        "source_dimensions": {"width": img_w, "height": img_h},
        "viewport": viewport,
        "stride": stride,
        "overlap_pct": _overlap_pct(viewport, stride),
        "sliding_window_tiles": sum(1 for t in tiles if t["source"] == "sliding_window"),
        "focus_region_tiles": sum(1 for t in tiles if t["source"] == "focus_region"),
        "total_tiles": len(tiles),
        "output_dir": str(output_dir.resolve()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tiles": tiles,
    }

    manifest_path = output_dir / "detail_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


# ── Recommend ────────────────────────────────────────────────────────────

def recommend(input_path: Path) -> dict[str, Any]:
    """Suggest viewport/stride settings based on image dimensions."""
    img = _validate_image(input_path)
    w, h = img.size

    single_pass = w <= DEFAULT_MAX_DIM and h <= DEFAULT_MAX_DIM

    if single_pass:
        vp, st = w, w  # no tiling needed
        est_tiles = 1
    else:
        vp = DEFAULT_VIEWPORT
        # Smaller stride for very large images to maintain quality
        if max(w, h) > 6000:
            st = 600
        elif max(w, h) > 4000:
            st = 700
        else:
            st = DEFAULT_STRIDE

        cols = 1 + max(0, math.ceil((w - vp) / st))
        rows = 1 + max(0, math.ceil((h - vp) / st))
        est_tiles = rows * cols

    return {
        "source_dimensions": {"width": w, "height": h},
        "single_pass": single_pass,
        "recommended_viewport": vp,
        "recommended_stride": st,
        "estimated_tiles": est_tiles,
        "overlap_pct": _overlap_pct(vp, st) if not single_pass else 0,
        "overview_max_dim": DEFAULT_MAX_DIM,
        "warning": (
            f"Detail pass will produce ~{est_tiles} tiles. "
            "Consider increasing stride or reducing viewport."
            if est_tiles > MAX_TILES_WARN
            else None
        ),
    }


# ── CLI ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Two-pass image splitter for diagram ingestion.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # overview
    p_ov = sub.add_parser("overview", help="Generate downscaled overview image")
    p_ov.add_argument("--input", required=True)
    p_ov.add_argument("--output-dir", required=True)
    p_ov.add_argument("--max-dim", type=int, default=DEFAULT_MAX_DIM)

    # detail
    p_dt = sub.add_parser("detail", help="Generate sliding-window detail tiles")
    p_dt.add_argument("--input", required=True)
    p_dt.add_argument("--output-dir", required=True)
    p_dt.add_argument("--viewport", type=int, default=DEFAULT_VIEWPORT)
    p_dt.add_argument("--stride", type=int, default=DEFAULT_STRIDE)
    p_dt.add_argument(
        "--focus-regions",
        default=None,
        help="Path to JSON file with [{x, y, w, h}, ...] focus regions",
    )

    # recommend
    p_rc = sub.add_parser("recommend", help="Print recommended settings")
    p_rc.add_argument("--input", required=True)

    args = parser.parse_args()

    if args.command == "overview":
        m = generate_overview(Path(args.input), Path(args.output_dir), args.max_dim)
        print(f"✓ Overview → {m['output_file']}")
        print(f"  Scale: {m['scale_factor']}  "
              f"({m['source_dimensions']['width']}×{m['source_dimensions']['height']} → "
              f"{m['overview_dimensions']['width']}×{m['overview_dimensions']['height']})")

    elif args.command == "detail":
        focus = None
        if args.focus_regions:
            fp = Path(args.focus_regions)
            if not fp.exists():
                sys.exit(f"ERROR: Focus regions file not found: {fp}")
            focus = json.loads(fp.read_text())

        m = generate_detail_tiles(
            Path(args.input), Path(args.output_dir),
            viewport=args.viewport, stride=args.stride,
            focus_regions=focus,
        )
        print(f"✓ Detail tiles → {m['output_dir']}")
        print(f"  Sliding window: {m['sliding_window_tiles']} tiles  "
              f"(viewport={m['viewport']}px, stride={m['stride']}px, "
              f"overlap={m['overlap_pct']*100:.0f}%)")
        if m["focus_region_tiles"]:
            print(f"  Focus regions:  {m['focus_region_tiles']} tiles")
        print(f"  Total: {m['total_tiles']} tiles")
        print(f"  Manifest: {m['output_dir']}/detail_manifest.json")

    elif args.command == "recommend":
        rec = recommend(Path(args.input))
        print(json.dumps(rec, indent=2))


if __name__ == "__main__":
    main()
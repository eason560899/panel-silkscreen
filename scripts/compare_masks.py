#!/usr/bin/env python3
"""Diagnose explicitly aligned, single-ink PNG masks. Requires Pillow and numpy.

This tool does not register images, extract ink from photographs, change artwork,
or decide whether production artwork passes. See references/mask-comparison.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw


NOTICE = "Diagnostic evidence only; no automatic pass or manufacturing certification."
COLORS = {
    "background": (255, 255, 255),
    "common": (72, 72, 72),
    "missing": (210, 0, 120),
    "extra": (0, 151, 190),
    "missing_within_tolerance": (245, 177, 215),
    "extra_within_tolerance": (159, 224, 239),
    "ignored": (255, 222, 90),
    "region_box": (227, 103, 0),
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def finite_nonnegative(value):
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected a finite nonnegative number.") from exc
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("Expected a finite nonnegative number.")
    return result


def read_mask(path, mode, threshold):
    path = Path(path)
    with Image.open(path) as image:
        if image.format != "PNG":
            raise ValueError(f"Only PNG masks are supported: {path}")
        if image.getexif().get(274, 1) != 1:
            raise ValueError("Normalize EXIF orientation explicitly before mask comparison.")
        if mode == "alpha":
            if "A" not in image.getbands() and "transparency" not in image.info:
                raise ValueError(f"Alpha mode requires a real alpha channel: {path}")
            values = np.array(image.convert("RGBA").getchannel("A"))
            if np.all(values == 255):
                raise ValueError("Fully opaque alpha has no mask boundary. Use an explicit grayscale mask for intended full-page ink.")
        else:
            if image.mode not in ("L", "1"):
                raise ValueError(f"Grayscale mode requires an L or 1 PNG, with white=ink: {path}")
            values = np.array(image.convert("L"))
        return values >= threshold


def horizontal_dilation(mask, radius):
    """Binary horizontal interval expansion, without wrapping or border padding ink."""
    width = mask.shape[1]
    radius = min(radius, width - 1)
    cumulative = np.concatenate((np.zeros((mask.shape[0], 1), dtype=np.int64),
                                 np.cumsum(mask, axis=1, dtype=np.int64)), axis=1)
    centers = np.arange(width)
    lo = np.maximum(0, centers - radius)
    hi = np.minimum(width, centers + radius + 1)
    return cumulative[:, hi] > cumulative[:, lo]


def disk_dilation(mask, radius):
    """Exact Euclidean pixel-center disk; fractional radii have grid-discrete effects.

    Uses O(HW) memory and O(min(radius,H)*HW) work; no scipy dependency.
    No image resampling, morphology cleanup, or radius rounding is performed.
    """
    if radius == 0 or not mask.any():
        return mask.copy()
    height, width = mask.shape
    if radius >= math.hypot(width - 1, height - 1):
        return np.ones_like(mask)
    result = np.zeros_like(mask)
    last_span = None
    horizontal = None
    # Symmetric rows share the same horizontal expansion; spans repeat near dy=0.
    for dy in range(min(int(math.floor(radius)), height - 1) + 1):
        span = min(width - 1, int(math.floor(math.sqrt(max(0, radius * radius - dy * dy)) + 1e-12)))
        if span != last_span:
            horizontal = horizontal_dilation(mask, span)
            last_span = span
        if dy == 0:
            result |= horizontal
        else:
            result[dy:] |= horizontal[:-dy]
            result[:-dy] |= horizontal[dy:]
    return result


def components(mask, connectivity=8):
    """Run-length union-find component inventory. Bboxes use half-open pixels."""
    if connectivity not in (4, 8):
        raise ValueError("Connectivity must be 4 or 8.")
    height, width = mask.shape
    parents, ranks, runs = [], [], []

    def find(node):
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    def join(a, b):
        a, b = find(a), find(b)
        if a == b:
            return
        if ranks[a] < ranks[b]:
            a, b = b, a
        parents[b] = a
        if ranks[a] == ranks[b]:
            ranks[a] += 1

    previous = []
    padding = 1 if connectivity == 8 else 0
    for y in range(height):
        row = np.pad(mask[y].astype(np.int8), (1, 1))
        transitions = np.flatnonzero(np.diff(row))
        current = []
        pointer = 0
        for x0, x1 in zip(transitions[::2], transitions[1::2]):
            x0, x1 = int(x0), int(x1)
            node = len(parents)
            parents.append(node)
            ranks.append(0)
            runs.append((x0, x1, y))
            current.append((x0, x1, node))
            while pointer < len(previous) and previous[pointer][1] + padding <= x0:
                pointer += 1
            cursor = pointer
            while cursor < len(previous) and previous[cursor][0] < x1 + padding:
                join(node, previous[cursor][2])
                cursor += 1
        previous = current
    records = {}
    for node, (x0, x1, y) in enumerate(runs):
        root = find(node)
        if root not in records:
            records[root] = {"pixels": 0, "bbox_px": [x0, y, x1, y + 1], "touches_canvas_border": False}
        item = records[root]
        item["pixels"] += x1 - x0
        bbox = item["bbox_px"]
        bbox[0], bbox[1] = min(bbox[0], x0), min(bbox[1], y)
        bbox[2], bbox[3] = max(bbox[2], x1), max(bbox[3], y + 1)
        item["touches_canvas_border"] |= x0 == 0 or x1 == width or y == 0 or y == height - 1
    return sorted(records.values(), key=lambda item: (-item["pixels"], item["bbox_px"][1], item["bbox_px"][0]))


def topology(mask):
    foreground = components(mask, 8)
    holes = [item for item in components(~mask, 4) if not item["touches_canvas_border"]]
    return {
        "ink_components_8_connected": len(foreground),
        "holes_background_4_connected": len(holes),
        "smallest_ink_component_px": min((item["pixels"] for item in foreground), default=None),
        "smallest_hole_px": min((item["pixels"] for item in holes), default=None),
        "ink_components_touching_canvas": sum(item["touches_canvas_border"] for item in foreground),
        "scope": "full thresholded input, including explicitly ignored pixels; no cleanup or tolerance morphology",
    }


def calibration(text, confirm, evidence, size):
    if text is None:
        if confirm or evidence:
            raise ValueError("Calibration flags require --panel-mm.")
        return None
    if not confirm or not evidence or not evidence.strip():
        raise ValueError("--panel-mm requires --confirm-panel-frame and a nonempty --scale-evidence.")
    try:
        dimensions = [float(value.strip()) for value in text.split(",")]
    except ValueError as exc:
        raise ValueError("--panel-mm must be positive width,height in mm.") from exc
    if len(dimensions) != 2 or any(not math.isfinite(x) or x <= 0 for x in dimensions):
        raise ValueError("--panel-mm must be positive width,height in mm.")
    width, height = size
    physical_width, physical_height = dimensions
    # Same isotropic rendering density must explain both dimensions to half a pixel.
    lower = max((width - 0.5) / physical_width, (height - 0.5) / physical_height)
    upper = min((width + 0.5) / physical_width, (height + 0.5) / physical_height)
    if lower > upper:
        raise ValueError("Canvas pixel aspect ratio disagrees with confirmed panel dimensions beyond half-pixel rounding.")
    return {"panel_mm": dimensions, "mm_per_pixel_xy": [physical_width / width, physical_height / height],
            "evidence": evidence, "frame_confirmation": "caller confirmed full image edges equal physical panel edges",
            "aspect_check": "common pixels/mm interval exists with at most 0.5 pixel rounding on each canvas dimension"}


def parse_roi(value, size):
    try:
        numbers = [int(item.strip()) for item in value.split(",")]
    except ValueError as exc:
        raise ValueError("Ignore ROI requires four integer pixel edges x0,y0,x1,y1.") from exc
    if len(numbers) != 4:
        raise ValueError("Ignore ROI requires four integer pixel edges x0,y0,x1,y1.")
    x0, y0, x1, y1 = numbers
    width, height = size
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("Ignore ROI must be a nonempty rectangle inside the canvas.")
    return numbers


def color_image(common, missing, extra, ignored, missing_within=None, extra_within=None):
    output = np.full((*common.shape, 3), 255, dtype=np.uint8)
    for mask, key in ((common, "common"), (missing, "missing"), (extra, "extra")):
        output[mask] = COLORS[key]
    if missing_within is not None:
        output[missing_within] = COLORS["missing_within_tolerance"]
        output[extra_within] = COLORS["extra_within_tolerance"]
    output[ignored] = COLORS["ignored"]
    return Image.fromarray(output)


def save_mask(mask, path):
    Image.fromarray(mask.astype(np.uint8) * 255).save(path)


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("alpha", "grayscale"))
    parser.add_argument("--ink-name", required=True, help="One ink layer only, e.g. blue or white.")
    parser.add_argument("--aligned", required=True, action="store_true", help="Explicit assertion of same canvas, scale, origin, orientation.")
    parser.add_argument("--threshold", type=int, default=128, help="Inclusive alpha/luminance cutoff 1..255; white=ink in grayscale mode.")
    parser.add_argument("--tolerance-px", required=True, type=finite_nonnegative, help="Euclidean pixel-center search radius; does not remove raw differences.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-regions", type=int, default=200, help="Limit only listed/labeled regions; all differences remain in full-size maps and totals.")
    parser.add_argument("--ignore-mask", type=Path, help="Aligned L/1 PNG; bright pixels explicitly excluded.")
    parser.add_argument("--ignore-roi", action="append", default=[], help="Repeatable half-open pixel rectangle x0,y0,x1,y1.")
    parser.add_argument("--ignore-reason", help="Required explanation whenever exclusions are used.")
    parser.add_argument("--panel-mm", help="Confirmed panel width,height; never inferred from a photo.")
    parser.add_argument("--confirm-panel-frame", action="store_true")
    parser.add_argument("--scale-evidence", help="Source/revision for panel dimensions and full-frame mapping.")
    return parser


def run(args):
    if not 1 <= args.threshold <= 255:
        raise ValueError("Threshold must be an integer from 1 to 255.")
    if args.max_regions < 0:
        raise ValueError("--max-regions must be nonnegative.")
    if not args.ink_name.strip():
        raise ValueError("--ink-name cannot be blank.")
    reference = read_mask(args.reference, args.mode, args.threshold)
    candidate = read_mask(args.candidate, args.mode, args.threshold)
    if reference.shape != candidate.shape:
        raise ValueError("Masks have different dimensions. Explicitly resolve alignment; no resizing is performed.")
    height, width = reference.shape
    size = (width, height)
    scale = calibration(args.panel_mm, args.confirm_panel_frame, args.scale_evidence, size)
    ignored = np.zeros_like(reference)
    exclusions = []
    if args.ignore_mask or args.ignore_roi:
        if not args.ignore_reason or not args.ignore_reason.strip():
            raise ValueError("Any exclusion requires an explicit --ignore-reason.")
    elif args.ignore_reason:
        raise ValueError("--ignore-reason requires an ignore mask or ROI.")
    if args.ignore_mask:
        mask = read_mask(args.ignore_mask, "grayscale", args.threshold)
        if mask.shape != reference.shape:
            raise ValueError("Ignore mask must have exactly the same canvas dimensions.")
        ignored |= mask
        exclusions.append({"type": "mask", "path": str(args.ignore_mask.resolve()), "sha256": sha256(args.ignore_mask), "pixels": int(mask.sum())})
    for value in args.ignore_roi:
        bbox = parse_roi(value, size)
        x0, y0, x1, y1 = bbox
        ignored[y0:y1, x0:x1] = True
        exclusions.append({"type": "roi", "bbox_px": bbox, "pixels": (x1 - x0) * (y1 - y0)})
    if ignored.all():
        raise ValueError("All pixels would be ignored; there is no evaluated region.")

    outputs = ["difference-raw.png", "difference-tolerance.png", "regions.png", "reference-mask.png", "candidate-mask.png", "ignored-mask.png", "report.json"]
    inputs = [args.reference, args.candidate] + ([args.ignore_mask] if args.ignore_mask else [])
    resolved_inputs = {path.resolve() for path in inputs}
    for name in outputs:
        path = args.output_dir / name
        if path.resolve() in resolved_inputs:
            raise ValueError(f"Refusing to overwrite input: {path}")
        if path.exists():
            raise ValueError(f"Output already exists; use a new review directory: {path}")

    checked = ~ignored
    ref_eval, cand_eval = reference & checked, candidate & checked
    common = ref_eval & cand_eval
    missing, extra = ref_eval & ~candidate, cand_eval & ~reference
    missing_within = missing & disk_dilation(cand_eval, args.tolerance_px)
    extra_within = extra & disk_dilation(ref_eval, args.tolerance_px)
    missing_outside, extra_outside = missing & ~missing_within, extra & ~extra_within
    raw_regions = components(missing | extra, 8)
    outside_regions = components(missing_outside | extra_outside, 8)

    def region_summary(regions):
        listed = []
        for index, item in enumerate(regions[:args.max_regions], 1):
            entry = dict(item, id=index)
            if scale:
                sx, sy = scale["mm_per_pixel_xy"]
                entry["bbox_mm"] = [item["bbox_px"][0] * sx, item["bbox_px"][1] * sy,
                                    item["bbox_px"][2] * sx, item["bbox_px"][3] * sy]
                entry["pixel_area_mm2"] = item["pixels"] * sx * sy
            listed.append(entry)
        omitted = regions[args.max_regions:]
        return {"total": len(regions), "listed": listed, "omitted_from_list": len(omitted),
                "omitted_from_list_pixels": sum(item["pixels"] for item in omitted),
                "all_regions_remain_in_full_size_maps": True}

    counts = {"reference_ink_full": int(reference.sum()), "candidate_ink_full": int(candidate.sum()),
              "common_checked": int(common.sum()), "missing_raw": int(missing.sum()), "extra_raw": int(extra.sum()),
              "missing_within_tolerance": int(missing_within.sum()), "extra_within_tolerance": int(extra_within.sum()),
              "missing_outside_tolerance": int(missing_outside.sum()), "extra_outside_tolerance": int(extra_outside.sum())}
    ref_topology, cand_topology = topology(reference), topology(candidate)
    report = {
        "notice": NOTICE,
        "review_status": "human_review_required",
        "ink_name": args.ink_name,
        "inputs": {"reference": {"path": str(args.reference.resolve()), "sha256": sha256(args.reference)},
                   "candidate": {"path": str(args.candidate.resolve()), "sha256": sha256(args.candidate)}},
        "canvas_px": list(size),
        "parameters": {"mode": args.mode, "threshold_inclusive": args.threshold, "tolerance_px": args.tolerance_px,
                       "distance": "Euclidean distance between pixel centers; independent directional nearest-ink tests",
                       "alignment": "caller explicitly asserted; not estimated or certified by tool",
                       "max_regions_listed": args.max_regions, "bbox_convention": "x0,y0,x1,y1; half-open pixel edges",
                       "connectivity": "8-connected ink and difference regions; 4-connected empty space for holes"},
        "calibration": scale,
        "coverage": {"canvas_pixels": width * height, "checked_pixels": int(checked.sum()), "ignored_pixels": int(ignored.sum()),
                     "ignored_fraction": float(ignored.mean()), "ignored_reference_ink_pixels": int((reference & ignored).sum()),
                     "ignored_candidate_ink_pixels": int((candidate & ignored).sum()),
                     "ignored_raw_difference_pixels": int(((reference ^ candidate) & ignored).sum()),
                     "reason": args.ignore_reason, "exclusions": exclusions},
        "pixel_counts": counts,
        "regions_raw": region_summary(raw_regions),
        "regions_outside_tolerance": region_summary(outside_regions),
        "topology": {"reference": ref_topology, "candidate": cand_topology,
                     "ink_component_delta": cand_topology["ink_components_8_connected"] - ref_topology["ink_components_8_connected"],
                     "hole_delta": cand_topology["holes_background_4_connected"] - ref_topology["holes_background_4_connected"],
                     "warning": "Counts depend on threshold, resolution, crop and connectivity. Equal totals do not prove same topology. Full-input topology includes ignored regions."},
        "legend_rgb": COLORS,
        "limitations": ["Not a whole-photograph difference test; masks must represent one independently selected ink.",
                        "Nearest-ink tolerance can hide a lost line beside another line; raw differences and topology remain mandatory.",
                        "Tolerance is not a manufacturing allowance or registration correction.",
                        "Missing and extra counts are separate; no mean similarity score decides acceptance.",
                        "Ignored pixels are unevaluated, visible in yellow, and excluded as tolerance matching sources.",
                        "Canvas crop may hide external ink; verify complete artwork bounds independently."],
    }
    if scale:
        sx, sy = scale["mm_per_pixel_xy"]
        report["calibration"]["tolerance_mm_min_max"] = [args.tolerance_px * min(sx, sy), args.tolerance_px * max(sx, sy)]
        report["calibration"]["area_note"] = "Pixel area only; not subpixel vector area or physical metrology."
    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_image = color_image(common, missing, extra, ignored)
    raw_image.save(args.output_dir / "difference-raw.png")
    color_image(common, missing, extra, ignored, missing_within, extra_within).save(args.output_dir / "difference-tolerance.png")
    region_image = raw_image.copy()
    draw = ImageDraw.Draw(region_image)
    for index, item in enumerate(raw_regions, 1):
        x0, y0, x1, y1 = item["bbox_px"]
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=COLORS["region_box"])
        if index <= args.max_regions:
            label = str(index)
            text_box = draw.textbbox((0, 0), label)
            label_width, label_height = text_box[2] - text_box[0], text_box[3] - text_box[1]
            tx, ty = min(max(0, x0), max(0, width - label_width - 2)), max(0, y0 - label_height - 3)
            draw.rectangle((tx, ty, tx + label_width + 2, ty + label_height + 2), fill="white")
            draw.text((tx + 1, ty + 1 - text_box[1]), label, fill=COLORS["region_box"])
    region_image.save(args.output_dir / "regions.png")
    save_mask(reference, args.output_dir / "reference-mask.png")
    save_mask(candidate, args.output_dir / "candidate-mask.png")
    save_mask(ignored, args.output_dir / "ignored-mask.png")
    report["outputs"] = [{"file": name, "sha256": sha256(args.output_dir / name)} for name in outputs if name != "report.json"]
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps({"report": str((args.output_dir / "report.json").resolve()),
                      "pixel_counts": report["pixel_counts"], "review_status": report["review_status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

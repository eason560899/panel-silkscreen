#!/usr/bin/env python3
"""Create auditable photo/artwork comparisons without modifying either input.

Requires Pillow and numpy. Coordinates are image-edge coordinates after EXIF
orientation: origin at top left, x right, y down; the image spans (0,0)-(W,H).
Registration is explicit and is never inferred from the artwork or photograph.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps


NOTICE = "Visual comparison only. No physical dimension or manufacturing certification."


def numbers(text: str, count: int) -> list[float]:
    try:
        values = [float(value.strip()) for value in text.split(",")]
    except ValueError as exc:
        raise ValueError("Coordinates must be comma-separated numbers.") from exc
    if len(values) != count or not all(math.isfinite(value) for value in values):
        raise ValueError(f"Expected {count} finite coordinates.")
    return values


def validate_quad(points, image_size):
    """Validate the supplied order; never silently reorder corner identities."""
    points = np.asarray(points, dtype=np.float64)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError("Quad requires four finite points in TL,TR,BR,BL order.")
    width, height = image_size
    if (points < 0).any() or (points[:, 0] > width).any() or (points[:, 1] > height).any():
        raise ValueError("Registration points must lie within the oriented reference image.")
    edges = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(edges, axis=1)
    following = np.roll(edges, -1, axis=0)
    crosses = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
    epsilon = max(1e-8, max(width, height) ** 2 * 1e-10)
    area = 0.5 * np.sum(points[:, 0] * np.roll(points[:, 1], -1)
                        - points[:, 1] * np.roll(points[:, 0], -1))
    if (lengths < 1).any() or area < 4 or (crosses <= epsilon).any():
        raise ValueError("Quad must be convex, nondegenerate, and ordered TL,TR,BR,BL (clockwise on screen).")
    return points


def homography(source_points, destination_points):
    """Normalized DLT: return the 3x3 matrix mapping source to destination."""
    def normalize(points):
        points = np.asarray(points, dtype=np.float64)
        center = points.mean(axis=0)
        rms = np.sqrt(np.mean(np.sum((points - center) ** 2, axis=1)))
        if rms < 1e-10:
            raise ValueError("Degenerate registration points.")
        scale = np.sqrt(2.0) / rms
        transform = np.array([[scale, 0, -scale * center[0]],
                              [0, scale, -scale * center[1]], [0, 0, 1]])
        homogeneous = np.column_stack((points, np.ones(len(points))))
        normalized = (transform @ homogeneous.T).T[:, :2]
        return normalized, transform

    src, src_transform = normalize(source_points)
    dst, dst_transform = normalize(destination_points)
    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.extend([[-x, -y, -1, 0, 0, 0, u*x, u*y, u],
                     [0, 0, 0, -x, -y, -1, v*x, v*y, v]])
    matrix = np.asarray(rows)
    _, singular, vt = np.linalg.svd(matrix, full_matrices=True)
    if singular[-1] < singular[0] * 1e-12:
        raise ValueError("Registration is numerically degenerate.")
    result = np.linalg.inv(dst_transform) @ vt[-1].reshape(3, 3) @ src_transform
    if abs(result[2, 2]) > 1e-12:
        result /= result[2, 2]
    else:
        result /= np.linalg.norm(result)
    if not np.isfinite(result).all():
        raise ValueError("Invalid registration matrix.")
    return result


def project_points(matrix, points):
    homogeneous = np.column_stack((np.asarray(points, dtype=np.float64), np.ones(len(points))))
    projected = (matrix @ homogeneous.T).T
    if np.any(np.abs(projected[:, 2]) < 1e-12):
        raise ValueError("Registration has an infinite projection.")
    return projected[:, :2] / projected[:, 2:3]


def warp_reference(image, output_to_reference, size):
    """Bilinear inverse warp with explicit pixel centers and a true homography."""
    width, height = size
    pixels = np.asarray(image.convert("RGB"), dtype=np.float32)
    source_h, source_w = pixels.shape[:2]
    output = np.empty((height, width, 3), dtype=np.uint8)
    matrix = output_to_reference
    x = np.arange(width, dtype=np.float64)[None, :] + 0.5
    for start in range(0, height, 192):
        stop = min(height, start + 192)
        y = np.arange(start, stop, dtype=np.float64)[:, None] + 0.5
        denominator = matrix[2, 0] * x + matrix[2, 1] * y + matrix[2, 2]
        if np.any(np.abs(denominator) < 1e-12):
            raise ValueError("Registration has an infinite projection inside the panel.")
        sx = (matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2]) / denominator - 0.5
        sy = (matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2]) / denominator - 0.5
        sx = np.clip(sx, 0, source_w - 1)
        sy = np.clip(sy, 0, source_h - 1)
        x0, y0 = np.floor(sx).astype(np.int64), np.floor(sy).astype(np.int64)
        x1, y1 = np.minimum(x0 + 1, source_w - 1), np.minimum(y0 + 1, source_h - 1)
        fx, fy = (sx - x0)[..., None], (sy - y0)[..., None]
        values = ((1-fy) * ((1-fx) * pixels[y0, x0] + fx * pixels[y0, x1])
                  + fy * ((1-fx) * pixels[y1, x0] + fx * pixels[y1, x1]))
        output[start:stop] = np.clip(np.rint(values), 0, 255).astype(np.uint8)
    return Image.fromarray(output)


def load_oriented(path):
    with Image.open(path) as source:
        raw_size = source.size
        file_format = source.format
        exif_orientation = source.getexif().get(274, 1)
        result = ImageOps.exif_transpose(source).convert("RGBA")
        result.load()
    return result, {"format": file_format, "raw_size_px": list(raw_size), "oriented_size_px": list(result.size),
                    "exif_orientation_before_transpose": exif_orientation}


def flattened(image, background=(255, 255, 255, 255)):
    result = Image.new("RGBA", image.size, background)
    result.alpha_composite(image.convert("RGBA"))
    return result.convert("RGB")


def checkerboard(image):
    width, height = image.size
    yy, xx = np.ogrid[:height, :width]
    value = np.where(((xx // 20 + yy // 20) % 2) == 0, 247, 231).astype(np.uint8)
    result = Image.fromarray(np.repeat(value[..., None], 3, axis=2)).convert("RGBA")
    result.alpha_composite(image)
    return result.convert("RGB")


def choose_font(explicit_path, size):
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Font does not exist: {path}")
        try:
            return ImageFont.truetype(str(path), size), str(path)
        except OSError as exc:
            raise ValueError(f"Cannot load font: {path}") from exc
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [windows_fonts / "msyh.ttc", windows_fonts / "simhei.ttf",
                  Path("/System/Library/Fonts/PingFang.ttc"),
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                  Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
                  Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size), str(path)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size), "Pillow bundled font (English labels)"
    except TypeError:
        return ImageFont.load_default(), "Pillow default font (English labels)"


def fit_text(draw, text, width, font):
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text + "..."


def comparison_sheet(cards, cell_size, columns, font, title, footer):
    cell_w, cell_h = cell_size
    font_size = max(12, getattr(font, "size", 20))
    gap = max(16, font_size)
    label_h = int(font_size * 2.4)
    top_h = int(font_size * 3)
    bottom_h = int(font_size * 3)
    rows = math.ceil(len(cards) / columns)
    width = columns * cell_w + (columns + 1) * gap
    height = top_h + rows * (cell_h + label_h + gap) + bottom_h
    if width * height > 100_000_000:
        raise ValueError("Comparison sheet exceeds 100 million pixels; lower --width.")
    sheet = Image.new("RGB", (width, height), "#eef1f5")
    draw = ImageDraw.Draw(sheet)
    draw.text((gap, gap // 2), fit_text(draw, title, width - 2*gap, font), fill="#172232", font=font)
    for index, (label, picture) in enumerate(cards):
        row, column = divmod(index, columns)
        left = gap + column * (cell_w + gap)
        top = top_h + row * (cell_h + label_h + gap)
        draw.rectangle((left, top, left + cell_w - 1, top + label_h + cell_h - 1), fill="white")
        draw.text((left + gap//2, top + gap//2), fit_text(draw, label, cell_w - gap, font),
                  fill="#172232", font=font)
        fitted = ImageOps.contain(picture.convert("RGB"), cell_size, Image.Resampling.LANCZOS)
        sheet.paste(fitted, (left + (cell_w - fitted.width)//2,
                            top + label_h + (cell_h - fitted.height)//2))
    draw.text((gap, height - bottom_h + gap//2),
              fit_text(draw, footer, width - 2*gap, font), fill="#445164", font=font)
    return sheet


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def panel_aspect_check(specification, source, artwork_size):
    """Test whether one pixels/mm scale can round to both canvas dimensions."""
    if specification is None:
        if source is not None:
            raise ValueError("--panel-mm-source requires --panel-mm.")
        return {"supplied": False, "physical_dimensions_certified": False}
    width_mm, height_mm = numbers(specification, 2)
    if min(width_mm, height_mm) <= 0:
        raise ValueError("--panel-mm requires a positive width and height.")
    if source is None or not source.strip():
        raise ValueError("--panel-mm requires an explicit nonempty --panel-mm-source describing the dimension source.")
    width, height = artwork_size
    x_interval = [(width - 0.5) / width_mm, (width + 0.5) / width_mm]
    y_interval = [(height - 0.5) / height_mm, (height + 0.5) / height_mm]
    if not all(math.isfinite(value) and value > 0 for value in x_interval+y_interval):
        raise ValueError("--panel-mm is outside the supported finite coordinate range.")
    common_interval = [max(x_interval[0], y_interval[0]), min(x_interval[1], y_interval[1])]
    if common_interval[0] > common_interval[1] + 1e-12 * max(common_interval):
        raise ValueError("Artwork full-panel canvas aspect ratio conflicts with --panel-mm beyond pixel rounding. "
                         "Correct the production canvas; do not stretch the reference to conceal the mismatch.")
    return {"supplied": True, "panel_size_mm": [width_mm, height_mm], "source": source,
            "artwork_canvas_size_px": [width, height], "artwork_aspect_ratio": width / height,
            "confirmed_dimension_aspect_ratio": width_mm / height_mm,
            "rounding_tolerance_px_per_dimension": 0.5,
            "compatible_uniform_pixels_per_mm_interval": common_interval,
            "canvas_aspect_compatible": True, "physical_dimensions_certified": False,
            "scope": "Checks the supplied full-panel raster canvas ratio only. Does not verify panel boundary, PDF/SVG millimetres, DPI, or actual physical dimensions."}


def tile_plan(specification, overlap, artwork_size):
    """Partition native artwork pixels, expanding every core by explicit overlap."""
    if specification is None:
        if overlap != 0.1:
            raise ValueError("--tile-overlap requires --tile-grid.")
        return []
    grid = numbers(specification, 2)
    if any(value != int(value) or not 1 <= value <= 20 for value in grid):
        raise ValueError("--tile-grid requires integer columns,rows between 1 and 20.")
    columns, rows = map(int, grid)
    if columns * rows > 100:
        raise ValueError("--tile-grid is limited to 100 tiles per run.")
    if not math.isfinite(overlap) or not 0 <= overlap <= 0.5:
        raise ValueError("--tile-overlap must be between 0 and 0.5.")
    width, height = artwork_size
    if columns > width or rows > height:
        raise ValueError("Tile grid cannot create empty pixel regions.")
    plans = []
    for row in range(rows):
        for column in range(columns):
            x0, x1 = round(column * width / columns), round((column + 1) * width / columns)
            y0, y1 = round(row * height / rows), round((row + 1) * height / rows)
            # Each side expands by half the stated fraction of its core size.
            pad_x = math.ceil((x1-x0) * overlap / 2)
            pad_y = math.ceil((y1-y0) * overlap / 2)
            bounds = [max(0, x0-pad_x), max(0, y0-pad_y), min(width, x1+pad_x), min(height, y1+pad_y)]
            tile_w, tile_h = bounds[2]-bounds[0], bounds[3]-bounds[1]
            if tile_w * tile_h > 16_000_000:
                raise ValueError("Native tile exceeds 16 million pixels; use a finer --tile-grid.")
            name = f"r{row+1:02d}_c{column+1:02d}"
            plans.append({"id": name, "row": row+1, "column": column+1,
                          "file": f"tile_{name}.png", "native_core_pixel_bounds": [x0, y0, x1, y1],
                          "native_expanded_pixel_bounds": bounds,
                          "normalized_core_bounds": [x0/width, y0/height, x1/width, y1/height],
                          "normalized_expanded_bounds": [bounds[0]/width, bounds[1]/height, bounds[2]/width, bounds[3]/height],
                          "native_display_cell_size_px": [tile_w, tile_h], "display_scale": 1.0})
    return plans


def independent_checkpoints(specifications, reference_size, artwork_size, reference_to_artwork, corners, panel_mm):
    """Measure held-out point residuals without changing the registration."""
    result = {"points": [], "automatic_pass_fail": False,
              "meaning": "Held-out correspondences supplied by the operator, excluded from the four-corner fit. Residuals indicate registration agreement, not artwork completeness or manufacturing accuracy."}
    names = set()
    locations = set()
    for specification in specifications:
        if ":" not in specification:
            raise ValueError("Checkpoint format is name:refx,refy,artx,arty.")
        name, coordinates = specification.split(":", 1)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ValueError("Checkpoint names must use 1-64 ASCII letters/digits/_/-.")
        if name.lower() in names:
            raise ValueError(f"Duplicate checkpoint name: {name}")
        names.add(name.lower())
        rx, ry, ax, ay = numbers(coordinates, 4)
        if not (0 <= rx <= reference_size[0] and 0 <= ry <= reference_size[1]
                and 0 <= ax <= artwork_size[0] and 0 <= ay <= artwork_size[1]):
            raise ValueError("Checkpoint coordinates must lie inside the respective EXIF-oriented input images.")
        if (rx, ry) in locations:
            raise ValueError("Repeated reference checkpoint location is not independent evidence.")
        locations.add((rx, ry))
        artwork_corners = np.array([[0, 0], [artwork_size[0], 0], artwork_size, [0, artwork_size[1]]])
        if (np.linalg.norm(corners-[rx, ry], axis=1) < 1e-6).any() or (np.linalg.norm(artwork_corners-[ax, ay], axis=1) < 1e-6).any():
            raise ValueError("A fitted panel corner cannot be an independent checkpoint.")
        predicted = project_points(reference_to_artwork, [[rx, ry]])[0]
        residual = predicted - [ax, ay]
        record = {"name": name, "reference_point_px": [rx, ry], "artwork_point_px": [ax, ay],
                  "reference_projected_to_artwork_px": predicted.tolist(),
                  "residual_vector_artwork_px": residual.tolist(),
                  "residual_distance_artwork_px": float(np.linalg.norm(residual))}
        if panel_mm["supplied"]:
            residual_mm = residual * np.array(panel_mm["panel_size_mm"]) / np.array(artwork_size)
            record.update(residual_vector_nominal_mm=residual_mm.tolist(),
                          residual_distance_nominal_mm=float(np.linalg.norm(residual_mm)))
        result["points"].append(record)
    if result["points"]:
        errors = [point["residual_distance_artwork_px"] for point in result["points"]]
        result["summary"] = {"count": len(errors), "maximum_artwork_px": max(errors),
                             "rms_artwork_px": math.sqrt(sum(value*value for value in errors) / len(errors))}
    return result


def annotation_canvas(picture, font, title, footer):
    """Keep the image pixels unscaled while placing explanatory bands outside it."""
    band = max(36, int(getattr(font, "size", 20) * 2.4))
    result = Image.new("RGB", (picture.width, picture.height + 2*band), "white")
    result.paste(picture.convert("RGB"), (0, band))
    draw = ImageDraw.Draw(result)
    draw.text((8, 5), fit_text(draw, title, picture.width-16, font), fill="#172232", font=font)
    draw.text((8, picture.height+band+5), fit_text(draw, footer, picture.width-16, font), fill="#445164", font=font)
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="No image similarity score is calculated. Review the evidence manually.")
    result.add_argument("--reference", required=True, help="Original panel photograph; never modified.")
    result.add_argument("--artwork", required=True, help="Final artwork PNG with a transparent background.")
    result.add_argument("--output-dir", required=True, help="New or empty directory for this review round. Nonempty directories are rejected to preserve earlier evidence.")
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--aligned", action="store_true", help="Reference already covers the identical artwork canvas extent; aspect ratios must match.")
    mode.add_argument("--frame", metavar="x0,y0,x1,y1", help="Axis-aligned panel frame in oriented reference-image edge pixels.")
    mode.add_argument("--quad", metavar="TL;TR;BR;BL", help="Four x,y corners, e.g. '10,20;500,30;480,300;20,310'; true perspective rectification.")
    result.add_argument("--width", type=int, default=1600, help="Rectified/output artwork width in pixels (default 1600); artwork aspect ratio is preserved.")
    result.add_argument("--crop", action="append", default=[], metavar="name:x0,y0,x1,y1", help="Repeatable detail crop in normalized panel coordinates [0,1]; name uses letters/digits/_/-.")
    result.add_argument("--white-background", action="store_true", help="Explicitly remove near-white pixels (all RGB channels >=245). Do not use for artwork containing white ink.")
    result.add_argument("--font", help="Optional TrueType/OpenType/TTC font path; system CJK fonts are preferred automatically.")
    result.add_argument("--overlay-opacity", type=float, default=0.65, help="Magenta coverage overlay opacity, >0 and <=1 (default .65).")
    result.add_argument("--panel-mm", metavar="W,H", help="Confirmed full-panel width,height in mm; independently checks the original artwork canvas aspect ratio within pixel rounding.")
    result.add_argument("--panel-mm-source", help="Required with --panel-mm: explicit dimension provenance, e.g. approved drawing identifier and revision.")
    result.add_argument("--tile-grid", metavar="COLS,ROWS", help="Optional overlapping native-artwork-resolution detail tiles, plus a location index; at most 100 tiles.")
    result.add_argument("--tile-overlap", type=float, default=0.1, help="Adjacent tile overlap fraction of core size, 0..0.5 (default .1); each side expands by half, rounded outward.")
    result.add_argument("--blink", action="store_true", help="Write a two-frame animated GIF alternating registered reference and artwork. Not a color proof or automatic difference test.")
    result.add_argument("--blink-background", choices=("white", "reference"), default="white", help="Artwork frame uses white (default) or the registered photograph beneath original-color ink.")
    result.add_argument("--checkpoint", action="append", default=[], metavar="name:refx,refy,artx,arty", help="Repeatable held-out point correspondence in EXIF-oriented reference and original artwork pixels; never used to fit registration.")
    return result


def run(args):
    reference_path = Path(args.reference).expanduser().resolve()
    artwork_path = Path(args.artwork).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    for path in (reference_path, artwork_path):
        if not path.is_file():
            raise ValueError(f"Input does not exist: {path}")
    if args.width < 32 or args.width > 8192:
        raise ValueError("--width must be between 32 and 8192 pixels.")
    if not math.isfinite(args.overlay_opacity) or not 0 < args.overlay_opacity <= 1:
        raise ValueError("--overlay-opacity must be >0 and <=1.")

    crops = []
    for specification in args.crop:
        if ":" not in specification:
            raise ValueError("Crop format is name:x0,y0,x1,y1.")
        name, coordinates = specification.split(":", 1)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ValueError("Crop names must contain 1-64 ASCII letters/digits/_/- and start with a letter or digit.")
        bounds = numbers(coordinates, 4)
        x0, y0, x1, y1 = bounds
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("Crop coordinates must satisfy 0<=x0<x1<=1 and 0<=y0<y1<=1.")
        if name.lower() in {crop["name"].lower() for crop in crops}:
            raise ValueError(f"Duplicate crop name: {name}")
        crops.append({"name": name, "normalized_requested_bounds": bounds, "file": f"crop_{name}.png"})

    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("--output-dir must be a directory.")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("--output-dir must be new or empty. Use a separate directory for each review round; existing evidence will not be overwritten or mixed with new outputs.")

    reference, reference_info = load_oriented(reference_path)
    artwork, artwork_info = load_oriented(artwork_path)
    if artwork_info["format"] != "PNG":
        raise ValueError("--artwork must be a PNG file.")
    panel_mm = panel_aspect_check(args.panel_mm, args.panel_mm_source, artwork.size)
    tiles = tile_plan(args.tile_grid, args.tile_overlap, artwork.size)
    if not args.blink and args.blink_background != "white":
        raise ValueError("--blink-background requires --blink.")
    output_names = ["reference_original_thumbnail.png", "reference_rectified.png", "artwork_original_color.png",
                    "overlay_magenta.png", "comparison.png", "manifest.json"] + [crop["file"] for crop in crops]
    if tiles:
        output_names += ["tile_index.png"] + [tile["file"] for tile in tiles]
    if args.blink:
        output_names.append("blink.gif")
    if args.checkpoint:
        output_names.append("checkpoints.png")
    source_keys = {os.path.normcase(str(path)) for path in (reference_path, artwork_path)}
    for name in output_names:
        target = (output_dir / name).resolve()
        aliases_input = target.exists() and any(os.path.samefile(target, source) for source in (reference_path, artwork_path))
        if os.path.normcase(str(target)) in source_keys or aliases_input:
            raise ValueError(f"Output would overwrite an input: {target}")
    pixels = np.asarray(artwork).copy()
    removed_white_pixels = 0
    if args.white_background:
        mask = np.all(pixels[:, :, :3] >= 245, axis=2) & (pixels[:, :, 3] > 0)
        removed_white_pixels = int(mask.sum())
        pixels[mask, 3] = 0
        artwork = Image.fromarray(pixels)
    if not np.any(pixels[:, :, 3] == 0):
        raise ValueError("Artwork needs a transparent background. For an intentional white background only, pass --white-background; never use this flag with white ink.")
    if not np.any(pixels[:, :, 3] > 0):
        raise ValueError("Artwork contains no visible ink after background handling.")

    width = args.width
    height = max(1, int(math.floor(width * artwork.height / artwork.width + 0.5)))
    if height > 16384 or width * height > 16_000_000:
        raise ValueError("Requested artwork canvas is too large; lower --width.")
    output_size = (width, height)
    output_corners = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float64)
    registration = {}
    if args.aligned:
        if abs(reference.width / reference.height - artwork.width / artwork.height) > 1e-6:
            raise ValueError("--aligned requires matching reference/artwork aspect ratios; use explicit --frame or --quad for a different extent.")
        points = [[0, 0], [reference.width, 0], [reference.width, reference.height], [0, reference.height]]
        registration["mode"] = "aligned"
    elif args.frame:
        x0, y0, x1, y1 = numbers(args.frame, 4)
        if not (x0 < x1 and y0 < y1):
            raise ValueError("Frame requires x0<x1 and y0<y1.")
        points = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        registration.update(mode="frame", reference_frame_px=[x0, y0, x1, y1])
    else:
        point_strings = args.quad.split(";")
        if len(point_strings) != 4:
            raise ValueError("--quad requires exactly four x,y points separated by semicolons.")
        points = [numbers(point, 2) for point in point_strings]
        registration["mode"] = "quad"
    points = validate_quad(points, reference.size)
    output_to_reference = homography(output_corners, points)
    reference_to_output = np.linalg.inv(output_to_reference)
    projection_error = float(np.max(np.linalg.norm(project_points(output_to_reference, output_corners) - points, axis=1)))
    if projection_error > 1e-5:
        raise ValueError("Registration matrix failed corner reprojection validation.")
    registration.update(reference_corners_px_TL_TR_BR_BL=points.tolist(),
                        output_corners_px_TL_TR_BR_BL=output_corners.tolist(),
                        output_to_reference_matrix=output_to_reference.tolist(),
                        reference_to_output_matrix=reference_to_output.tolist(),
                        corner_reprojection_max_error_px=projection_error,
                        sampling="Inverse homography, bilinear; pixel centers at (column+0.5,row+0.5).")
    artwork_to_output = np.diag([width/artwork.width, height/artwork.height, 1])
    artwork_to_reference = output_to_reference @ artwork_to_output
    checkpoints = independent_checkpoints(args.checkpoint, reference.size, artwork.size,
                                         np.linalg.inv(artwork_to_reference), points, panel_mm)

    reference_rgb = flattened(reference)
    rectified = warp_reference(reference_rgb, output_to_reference, output_size)
    artwork_resized = artwork.resize(output_size, Image.Resampling.LANCZOS)
    art_display = checkerboard(artwork_resized)
    overlay_ink = Image.new("RGBA", output_size, (255, 0, 255, 0))
    overlay_alpha = artwork_resized.getchannel("A").point(lambda alpha: int(round(alpha * args.overlay_opacity)))
    overlay_ink.putalpha(overlay_alpha)
    overlay = rectified.convert("RGBA")
    overlay.alpha_composite(overlay_ink)
    overlay = overlay.convert("RGB")
    original_thumbnail = reference_rgb.copy()
    original_thumbnail.thumbnail((width, width), Image.Resampling.LANCZOS)
    font, font_source = choose_font(args.font, max(16, min(40, width // 40)))
    sheet = comparison_sheet([
        ("Original photo - complete frame", original_thumbnail),
        ("Rectified reference - panel extent", rectified),
        ("Artwork - original ink colors / checkerboard", art_display),
        ("Overlay - all ink shown in magenta", overlay),
    ], output_size, 2, font, "Panel silkscreen - source comparison", NOTICE)

    images = {"reference_original_thumbnail.png": original_thumbnail,
              "reference_rectified.png": rectified, "artwork_original_color.png": artwork_resized,
              "overlay_magenta.png": overlay, "comparison.png": sheet}
    if tiles:
        index = rectified.copy()
        draw = ImageDraw.Draw(index)
        for tile in tiles:
            x0, y0, x1, y1 = tile["normalized_expanded_bounds"]
            draw.rectangle((x0*width, y0*height, min(width-1, x1*width), min(height-1, y1*height)), outline="#f500be", width=1)
            x0, y0, x1, y1 = tile["normalized_core_bounds"]
            draw.rectangle((x0*width, y0*height, min(width-1, x1*width), min(height-1, y1*height)), outline="#00b7d6", width=2)
            label_x, label_y = x0*width+4, y0*height+4
            box = draw.textbbox((label_x, label_y), tile["id"], font=font)
            draw.rectangle(box, fill="white")
            draw.text((label_x, label_y), tile["id"], fill="#172232", font=font)
        images["tile_index.png"] = annotation_canvas(index, font, "Tile index: cyan = core / magenta = expanded coverage",
            "Index only. Open each tile PNG at 100% for native pixel inspection.")
    if checkpoints["points"]:
        checkpoint_image = rectified.copy()
        draw = ImageDraw.Draw(checkpoint_image)
        radius = max(4, width//200)
        for point in checkpoints["points"]:
            ref_x, ref_y = np.array(point["reference_projected_to_artwork_px"]) * [width/artwork.width, height/artwork.height]
            art_x, art_y = np.array(point["artwork_point_px"]) * [width/artwork.width, height/artwork.height]
            draw.line((ref_x, ref_y, art_x, art_y), fill="#ff8f00", width=2)
            draw.ellipse((ref_x-radius, ref_y-radius, ref_x+radius, ref_y+radius), outline="#f500be", width=2)
            draw.line((art_x-radius, art_y, art_x+radius, art_y), fill="#00a4c7", width=2)
            draw.line((art_x, art_y-radius, art_x, art_y+radius), fill="#00a4c7", width=2)
            draw.text((art_x+radius+2, art_y+radius+2), point["name"], fill="#172232", font=font, stroke_width=1, stroke_fill="white")
        images["checkpoints.png"] = annotation_canvas(checkpoint_image, font,
            "Checkpoints: magenta circle = projected photo; cyan + = artwork",
            "Held-out residuals in manifest.json; no automatic acceptance.")
    for crop in crops:
        x0, y0, x1, y1 = crop["normalized_requested_bounds"]
        bounds = (int(math.floor(x0*width)), int(math.floor(y0*height)),
                  int(math.ceil(x1*width)), int(math.ceil(y1*height)))
        crop_w, crop_h = bounds[2]-bounds[0], bounds[3]-bounds[1]
        display_w = min(width, 1000)
        display_h = max(1, int(round(display_w * crop_h / crop_w)))
        if display_h > 8000 or display_w * display_h > 16_000_000:
            raise ValueError(f"Crop {crop['name']} is too tall/narrow; use a broader detail region.")
        images[crop["file"]] = comparison_sheet([
            ("Rectified reference", rectified.crop(bounds)),
            ("Original ink colors", art_display.crop(bounds)),
            ("Magenta overlay", overlay.crop(bounds)),
        ], (display_w, display_h), 3, font, f"Detail: {crop['name']}",
            "Raster enlargement of the registered canvas; no added source detail. Manual review required.")
        crop.update(output_canvas_pixel_bounds=list(bounds), display_cell_size_px=[display_w, display_h],
                    enlargement_factor=display_w / crop_w)

    manifest = {
        "schema_version": 2,
        "purpose": "Manual visual acceptance evidence; no similarity score or automatic pass/fail.",
        "notice": NOTICE,
        "physical_dimensions_certified": False,
        "inputs": {"reference": {"path": str(reference_path), "sha256": sha256(reference_path), **reference_info},
                   "artwork": {"path": str(artwork_path), "sha256": sha256(artwork_path), **artwork_info}},
        "coordinates": {"origin": "Top-left of the EXIF-oriented reference image / artwork panel canvas.",
                        "axes": "x increases rightward; y increases downward.",
                        "pixel_coordinates": "Image-edge coordinates; bounds (0,0)-(width,height), centers at (column+0.5,row+0.5).",
                        "quad_order": "TL, TR, BR, BL; explicit corner identity, no automatic reordering.",
                        "crop_coordinates": "Normalized panel extent [0,1]; raster bounds rounded outward.",
                        "checkpoint_coordinates": "EXIF-oriented reference and original EXIF-oriented artwork pixels before --width resampling.",
                        "units": "Pixels and normalized coordinates; optional nominal mm derived only from explicitly supplied --panel-mm."},
        "registration": registration,
        "panel_canvas_aspect_check": panel_mm,
        "independent_checkpoints": checkpoints,
        "output_canvas_size_px": list(output_size),
        "artwork_handling": {"white_background_removal_explicit": bool(args.white_background),
                             "near_white_threshold_per_RGB_channel": 245 if args.white_background else None,
                             "removed_white_pixels": removed_white_pixels,
                             "artwork_intentionally_recolored": False,
                             "resampling": "Pillow LANCZOS; edge colors and alpha may be interpolated. Output PNG keeps transparency; it does not verify source spot colors."},
        "overlay": {"color_RGB": [255, 0, 255], "opacity_multiplier": args.overlay_opacity,
                    "meaning": "All artwork alpha coverage, regardless of original ink color; comparison aid only."},
        "font": font_source, "crops": crops,
        "tiles": {"enabled": bool(tiles), "grid_columns_rows": [int(value) for value in numbers(args.tile_grid, 2)] if tiles else None,
                  "overlap_fraction_of_core": args.tile_overlap if tiles else None,
                  "overlap_rule": "Each core side expands by ceil(core size * fraction / 2), clipped to the panel. Actual overlap can differ by pixel rounding.",
                  "sampling": "Each tile is sampled directly from the original reference into original artwork pixels; it is not a crop or enlargement of the overview.",
                  "coverage": "Core rectangles partition the whole native artwork canvas without gaps; expanded regions overlap when requested.",
                  "source_detail_limit": "Native artwork pixels do not guarantee matching photo resolution. Rectification can interpolate source pixels; it cannot recover missing photographed detail.",
                  "items": tiles},
        "blink": {"enabled": bool(args.blink), "artwork_background": args.blink_background if args.blink else None,
                  "frame_duration_ms": 650 if args.blink else None,
                  "meaning": "Reference photograph alternates with original-color artwork on the stated background. Photograph hardware/text intentionally excluded from the artwork may blink; this is not an automatic defect signal.",
                  "limitations": "GIF uses a reduced palette and overview resolution; it is not a spot-color proof, fine-detail substitute, or completeness verdict. White ink may disappear on the white background; inspect the checkerboard PNG or native tiles."},
        "warnings": [NOTICE, "Rectification does not prove that selected corners are the physical printing boundary.",
                     "Review source and detail images manually; smooth appearance does not establish geometry or color accuracy."],
        "outputs": {},
    }
    if args.white_background:
        manifest["warnings"].append("Explicit near-white removal also removes white ink; this mode must not be used when white ink is intended.")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, picture in images.items():
        target = output_dir / name
        picture.save(target, "PNG")
        manifest["outputs"][name] = {"size_px": list(picture.size), "mode": picture.mode,
                                     "sha256": sha256(target)}
    # Generate and release one native tile at a time so detailed reviews do not
    # retain a second whole panel (or an entire grid of large sheets) in memory.
    for tile in tiles:
        bounds = tile["native_expanded_pixel_bounds"]
        tile_w, tile_h = tile["native_display_cell_size_px"]
        translate = np.array([[1, 0, bounds[0]], [0, 1, bounds[1]], [0, 0, 1]], dtype=float)
        tile_reference = warp_reference(reference_rgb, artwork_to_reference @ translate, (tile_w, tile_h))
        tile_artwork = artwork.crop(bounds)
        tile_overlay_ink = Image.new("RGBA", (tile_w, tile_h), (255, 0, 255, 0))
        tile_overlay_ink.putalpha(tile_artwork.getchannel("A").point(lambda alpha: int(round(alpha * args.overlay_opacity))))
        tile_overlay = tile_reference.convert("RGBA")
        tile_overlay.alpha_composite(tile_overlay_ink)
        tile_sheet = comparison_sheet([
            ("Reference / native artwork pixel grid", tile_reference),
            ("Original ink / checkerboard", checkerboard(tile_artwork)),
            ("Coverage / magenta overlay", tile_overlay),
        ], (tile_w, tile_h), 3, font, f"Native detail {tile['id']} - {bounds}",
            "One artwork pixel per displayed pixel. Photograph may be interpolated; no detail is recovered.")
        target = output_dir / tile["file"]
        tile_sheet.save(target, "PNG")
        manifest["outputs"][tile["file"]] = {"size_px": list(tile_sheet.size), "mode": tile_sheet.mode, "sha256": sha256(target)}
    if args.blink:
        blink_artwork = rectified.convert("RGBA") if args.blink_background == "reference" else Image.new("RGBA", output_size, "white")
        blink_artwork.alpha_composite(artwork_resized)
        blink_footer = "GIF preview; intentional omissions may blink. Use PNGs for color/detail."
        frames = [annotation_canvas(rectified, font, "REFERENCE - photograph", blink_footer),
                  annotation_canvas(blink_artwork, font, f"ARTWORK - original ink on {args.blink_background} background", blink_footer)]
        target = output_dir / "blink.gif"
        frames[0].save(target, "GIF", save_all=True, append_images=frames[1:], duration=650, loop=0, disposal=2)
        manifest["outputs"]["blink.gif"] = {"size_px": list(frames[0].size), "mode": "P", "frame_count": 2,
                                            "duration_ms": 650, "sha256": sha256(target)}
    with (output_dir / "manifest.json").open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return manifest


def main():
    argument_parser = parser()
    args = argument_parser.parse_args()
    try:
        manifest = run(args)
    except (ValueError, OSError, np.linalg.LinAlgError) as exc:
        argument_parser.error(str(exc))
    print(f"Created {len(manifest['outputs'])} comparison images and manifest.json in {Path(args.output_dir).resolve()}")
    print(NOTICE)
    if args.white_background:
        print("Warning: explicit near-white removal is unsuitable for white ink.", file=sys.stderr)


if __name__ == "__main__":
    main()

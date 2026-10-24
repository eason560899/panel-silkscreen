#!/usr/bin/env python3
"""Measure registered 2-D geometry. Does not parse PDF ink boundaries or certify scale."""
import argparse
import hashlib
import json
import math
from pathlib import Path


def numbers(value, length, name):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f'{name}: expected {length} finite numbers')
    result = [float(x) for x in value]
    if not all(math.isfinite(x) for x in result):
        raise ValueError(f'{name}: numbers must be finite')
    return result


def positive(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name}: expected a positive finite number')
    return value


def rectangle(value, name):
    b = numbers(value, 4, name)
    if b[2] <= b[0] or b[3] <= b[1]:
        raise ValueError(f'{name}: rectangle must have positive width and height')
    return b


def unit(value):
    x, y = numbers(value, 2, 'tangent')
    size = math.hypot(x, y)
    if size == 0:
        raise ValueError('tangent cannot be zero')
    return x / size, y / size


def measure(data):
    frame = data['frame']
    x0, y0, x1, y1 = rectangle(frame['raw'], 'frame.raw')
    w, h = numbers(frame['mm'], 2, 'frame.mm')
    w, h = positive(w, 'frame width'), positive(h, 'frame height')
    sx, sy = w / (x1 - x0), h / (y1 - y0)

    def physical(p):
        x, y = numbers(p, 2, 'point')
        return (x - x0) * sx, (y - y0) * sy

    def raw(p):
        return [p[0] / sx + x0, p[1] / sy + y0]

    result = {
        'coordinate_system': 'Panel top-left origin; x right, y down; mm output',
        'scale_mm_per_raw_unit': [sx, sy],
        'basis': frame.get('basis', 'unspecified; verify before production'),
        'limitations': [
            'Input plane must already be rectified; physical dimensions are caller-supplied.',
            'Ink bounds must already include stroke, cap, join and visible-union effects.',
            'Filled sections support straight polygon edges and even-odd fill only.',
            'Local sections do not establish a global minimum, maximum or process tolerance.',
        ],
        'margins_mm': {}, 'sections': [],
    }
    for colour, entry in data.get('ink_bounds', {}).items():
        if entry.get('includes_strokes_caps_joins') is not True:
            raise ValueError(f'{colour}: supply actual ink bounds and set includes_strokes_caps_joins=true')
        a, b, c, d = rectangle(entry['raw'], f'ink_bounds.{colour}')
        result['margins_mm'][colour] = {
            'left': (a - x0) * sx, 'right': (x1 - c) * sx,
            'top': (b - y0) * sy, 'bottom': (y1 - d) * sy,
        }

    identifiers = set()
    for kind, items in [('stroke', data.get('strokes', [])),
                        ('filled_polygon', data.get('filled_sections', []))]:
        for item in items:
            identity = str(item['id']).strip()
            if not identity or identity in identifiers:
                raise ValueError(f'Section id missing or duplicated: {identity!r}')
            identifiers.add(identity)
            ax, ay = physical(item['anchor'])
            tx, ty = unit(item['tangent'])
            transformed_length = math.hypot(sx * tx, sy * ty)
            nx, ny = -sy * ty / transformed_length, sx * tx / transformed_length
            if kind == 'stroke':
                width = positive(item['width'], 'stroke width') * sx * sy / transformed_length
                lo, hi = -width / 2, width / 2
            else:
                if not item.get('loops'):
                    raise ValueError(f'{identity}: polygon loops are required')
                cuts = []
                for loop in item['loops']:
                    points = [physical(p) for p in loop]
                    if len(points) > 1 and points[0] == points[-1]:
                        points.pop()
                    if len(set(points)) < 3:
                        raise ValueError(f'{identity}: a polygon loop needs at least 3 distinct vertices')
                    for a, b in zip(points, points[1:] + points[:1]):
                        da = nx * (a[1] - ay) - ny * (a[0] - ax)
                        db = nx * (b[1] - ay) - ny * (b[0] - ax)
                        if abs(da) < 1e-12 and abs(db) < 1e-12 and a != b:
                            raise ValueError(f'{identity}: section coincides with a boundary; choose another anchor')
                        if (da <= 0 < db) or (db <= 0 < da):
                            u = da / (da - db)
                            x = a[0] + u * (b[0] - a[0])
                            y = a[1] + u * (b[1] - a[1])
                            cuts.append((x - ax) * nx + (y - ay) * ny)
                cuts.sort()
                # Cancel duplicate crossings by parity, using a small arithmetic tolerance.
                unique = []
                for t in cuts:
                    if unique and abs(t - unique[-1]) < 1e-10:
                        unique.pop()
                    else:
                        unique.append(t)
                if len(unique) % 2:
                    raise ValueError(f'{identity}: unmatched polygon intersections')
                intervals = list(zip(unique[::2], unique[1::2]))
                selected = [(a, b) for a, b in intervals if a < -1e-10 and b > 1e-10]
                if len(selected) != 1:
                    raise ValueError(f'{identity}: anchor is outside filled ink, in a hole, or on its boundary')
                lo, hi = selected[0]
                width = hi - lo
            endpoints = [[ax + lo * nx, ay + lo * ny], [ax + hi * nx, ay + hi * ny]]
            section = {
                'id': identity, 'kind': kind, 'width_mm': width,
                'section_mm': endpoints, 'raw_section': [raw(p) for p in endpoints],
                'raw_anchor': item['anchor'], 'raw_tangent': item['tangent'],
            }
            for key in ['label', 'colour', 'color', 'object_ids']:
                if key in item:
                    section[key] = item[key]
            result['sections'].append(section)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Registered geometry JSON; see measurement reference')
    parser.add_argument('--output', type=Path, required=True, help='Measurements JSON, separate from the source')
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.output.resolve():
            raise ValueError('Input and output must differ')
        source_bytes = args.input.read_bytes()
        result = measure(json.loads(source_bytes.decode('utf-8-sig')))
        result['source_sha256'] = hashlib.sha256(source_bytes).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f'error: {exc}\n')
    print(f'Saved {len(result["sections"])} sections and {len(result["margins_mm"])} ink-bound groups.')


if __name__ == '__main__':
    main()

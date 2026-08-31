#!/usr/bin/env python3
"""Build the Singapore map the Map view draws and the API names places from.

Two artefacts come out of one source so they can never disagree:

    avian/frontend/sg-map.js   the shape the browser draws
    avian/api/sg-areas.php     the same rings, for naming a fix server-side

They are deliberately separate files rather than one shared JSON. The
browser copy has to be a plain script the webroot manifest publishes and
the cache token versions; the API copy has to be a PHP include that is
*not* in the served allowlist, because the whole point of resolving a
position server-side is that raw coordinates never leave the station.

Boundaries are Singapore's 55 planning areas, which is what makes the
place names real: a point is named by the area that actually contains
it, not by guessing the nearest landmark from a hand-written list.

The areas tile the whole country, so there is no separate coastline in
here: their union already is one. Dropping it saved a third of the file
for an outline the browser can get from the fills.

Source: geoBoundaries (https://www.geoboundaries.org), gbOpen SGP ADM2,
CC BY 4.0. Download it first - it is a Git LFS object, so the
raw.githubusercontent URL gives back a pointer file and only the media
host gives the real thing:

    base=https://media.githubusercontent.com/media/wmgeolab/geoBoundaries
    curl -sSL -o /tmp/sg-adm2.geojson \
      $base/main/releaseData/gbOpen/SGP/ADM2/geoBoundaries-SGP-ADM2.geojson
    python3 avian/scripts/build_sg_map.py /tmp/sg-adm2.geojson

Simplification is 40 m by default. At the size the map is drawn that is
comfortably under one pixel, and it takes the boundary data from 1.6 MB
to under 50 KB - which matters, because this ships to every phone that
opens the Map view.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JS_OUT = REPO / "avian" / "frontend" / "sg-map.js"
PHP_OUT = REPO / "avian" / "api" / "sg-areas.php"

# One degree of latitude, near enough, in metres. Tolerances are quoted in
# metres because that is the unit the decision is actually made in.
DEG_M = 111320.0

# Areas smaller than this are reclamation slivers and unnamed rocks; they
# cost points and add nothing anyone would recognise.
MIN_RING_M = 150.0

# Rounding. 5 decimal places is about 1.1 m - far finer than the 40 m
# simplification, so it costs nothing in accuracy and saves a third of
# the file against 7 places.
DP = 5


def douglas_peucker(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Iterative Douglas-Peucker. Iterative rather than recursive because a
    coastline ring is long enough to reach Python's recursion limit."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        x1, y1 = points[first]
        x2, y2 = points[last]
        dx, dy = x2 - x1, y2 - y1
        den = dx * dx + dy * dy
        worst, worst_i = -1.0, -1
        for i in range(first + 1, last):
            x0, y0 = points[i]
            if den:
                d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / math.sqrt(den)
            else:
                d = math.hypot(x0 - x1, y0 - y1)
            if d > worst:
                worst, worst_i = d, i
        if worst > tol:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))
    return [p for p, k in zip(points, keep) if k]


def signed_area(ring: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        total += x1 * y2 - x2 * y1
    return total / 2


def rings_of(geometry: dict) -> list[list[list[float]]]:
    polys = (geometry["coordinates"] if geometry["type"] == "MultiPolygon"
             else [geometry["coordinates"]])
    return [ring for poly in polys for ring in poly]


def simplify(geometry: dict, tol_m: float) -> list[list[tuple[float, float]]]:
    tol = tol_m / DEG_M
    floor = (MIN_RING_M / DEG_M) ** 2
    out = []
    for ring in rings_of(geometry):
        pts = [(float(p[0]), float(p[1])) for p in ring]
        if abs(signed_area(pts)) < floor:
            continue
        small = douglas_peucker(pts, tol)
        # A ring that collapses to a triangle is noise, not a shape.
        if len(small) < 4:
            continue
        if small[0] != small[-1]:
            small.append(small[0])
        out.append([(round(x, DP), round(y, DP)) for x, y in small])
    return out


def contains(rings: list[list[tuple[float, float]]], lon: float, lat: float) -> bool:
    """Even-odd crossing count over every ring at once, so a hole in an
    area (a reservoir cut out of a district, say) excludes correctly
    without having to know which ring is the hole."""
    inside = False
    for ring in rings:
        for i in range(len(ring) - 1):
            x1, y1 = ring[i]
            x2, y2 = ring[i + 1]
            if (y1 > lat) != (y2 > lat):
                x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                if lon < x_at:
                    inside = not inside
    return inside


def label_anchor(rings: list[list[tuple[float, float]]]) -> tuple[float, float]:
    """A point inside the area to hang its name and its heat blob on.

    The centroid of the largest ring is right for most districts and
    wrong for the crescent-shaped ones, where it lands offshore. When
    that happens, fall back to a coarse grid search for the interior
    point furthest from the boundary box edges - crude, but it only has
    to be somewhere clearly inside."""
    biggest = max(rings, key=lambda r: abs(signed_area(r)))
    area = signed_area(biggest)
    if area:
        cx = cy = 0.0
        for i in range(len(biggest) - 1):
            x1, y1 = biggest[i]
            x2, y2 = biggest[i + 1]
            cross = x1 * y2 - x2 * y1
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        cx /= 6 * area
        cy /= 6 * area
        if contains(rings, cx, cy):
            return round(cx, DP), round(cy, DP)

    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    best, best_score = (sum(xs) / len(xs), sum(ys) / len(ys)), -1.0
    steps = 24
    for i in range(1, steps):
        for j in range(1, steps):
            lon = min(xs) + (max(xs) - min(xs)) * i / steps
            lat = min(ys) + (max(ys) - min(ys)) * j / steps
            if not contains(rings, lon, lat):
                continue
            score = min(i, steps - i) * min(j, steps - j)
            if score > best_score:
                best, best_score = (lon, lat), score
    return round(best[0], DP), round(best[1], DP)


def display_name(raw: str) -> str:
    """geoBoundaries shouts. Title-case it, keeping the pieces of a
    hyphenated name capitalised and the small joining words lower."""
    lower = {"and", "of", "the"}
    words = []
    for index, word in enumerate(raw.strip().split()):
        parts = [p.capitalize() for p in word.split("-")]
        joined = "-".join(parts)
        if index and joined.lower() in lower:
            joined = joined.lower()
        words.append(joined)
    return " ".join(words)


def flat(ring: list[tuple[float, float]]) -> str:
    """One ring as a compact JSON array literal. Compact on purpose: the
    pretty-printer's space after every comma is a fifth of the file, and
    nobody reads this by eye anyway."""
    out: list[float] = []
    for x, y in ring:
        out.append(x)
        out.append(y)
    return json.dumps(out, separators=(",", ":"))


def build(adm2: Path, tol_m: float) -> dict:
    areas = []
    for feature in json.loads(adm2.read_text())["features"]:
        raw = str(feature["properties"].get("shapeName", "")).strip()
        if not raw:
            continue
        rings = simplify(feature["geometry"], tol_m)
        if not rings:
            continue
        lon, lat = label_anchor(rings)
        areas.append({"name": display_name(raw), "at": [lon, lat], "rings": rings})
    areas.sort(key=lambda a: a["name"])

    lons = [p[0] for a in areas for r in a["rings"] for p in r]
    lats = [p[1] for a in areas for r in a["rings"] for p in r]
    return {
        "bbox": [round(min(lons), DP), round(min(lats), DP),
                 round(max(lons), DP), round(max(lats), DP)],
        "areas": areas,
    }


HEADER = (
    "Singapore's 55 planning areas, simplified to {tol:g} m.\n"
    "\n"
    "GENERATED by avian/scripts/build_sg_map.py - edit that, not this.\n"
    "\n"
    "Boundaries from geoBoundaries (geoboundaries.org), gbOpen SGP ADM2,\n"
    "CC BY 4.0. The areas tile the country, so their union is the\n"
    "coastline; there is no separate outline in here.\n"
)


def write_js(data: dict, tol_m: float) -> int:
    lines = ["/* " + HEADER.format(tol=tol_m).strip().replace("\n\n", "\n").replace("\n", "\n * ") + "\n */",
             "window.AVIAN_SG_MAP = {",
             "  bbox: " + json.dumps(data["bbox"], separators=(",", ":")) + ",",
             "  areas: ["]
    for area in data["areas"]:
        lines.append("    { name: " + json.dumps(area["name"])
                     + ", at: " + json.dumps(area["at"], separators=(",", ":")) + ", rings: [")
        for ring in area["rings"]:
            lines.append("      " + flat(ring) + ",")
        lines.append("    ] },")
    lines.append("  ]")
    lines.append("};")
    text = "\n".join(lines) + "\n"
    JS_OUT.write_text(text)
    return len(text)


def write_php(data: dict, tol_m: float) -> int:
    lines = ["<?php", "// " + HEADER.format(tol=tol_m).strip().replace("\n\n", "\n").replace("\n", "\n// "),
             "//",
             "// Never reachable as a URL: the managed Caddy policy serves only the",
             "// endpoints on its allowlist and 404s everything else under the API",
             "// directory, so this include stays server-side where it belongs.",
             "",
             "declare(strict_types=1);",
             "",
             "return ["]
    for area in data["areas"]:
        lines.append("    " + json.dumps(area["name"]) + " => [")
        for ring in area["rings"]:
            lines.append("        " + flat(ring) + ",")
        lines.append("    ],")
    lines.append("];")
    text = "\n".join(lines) + "\n"
    PHP_OUT.write_text(text)
    return len(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("adm2", type=Path, help="geoBoundaries SGP ADM2 geojson (planning areas)")
    ap.add_argument("--tolerance", type=float, default=40.0,
                    help="simplification tolerance in metres (default 40)")
    args = ap.parse_args()

    data = build(args.adm2, args.tolerance)
    js = write_js(data, args.tolerance)
    php = write_php(data, args.tolerance)
    points = sum(len(r) for a in data["areas"] for r in a["rings"])
    print(f"{len(data['areas'])} areas, {points} points")
    print(f"{JS_OUT.relative_to(REPO)}  {js / 1024:.0f} KB")
    print(f"{PHP_OUT.relative_to(REPO)}  {php / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

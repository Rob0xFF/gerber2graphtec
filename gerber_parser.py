#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Utility to extract stroke polylines from a Gerber layer.

Supported primitives
--------------------
* Line           → two‑point polyline
* Arc            → approximated with straight segments (robust to pcb‑tools & gerbonara APIs)
* Circle         → approximated with straight segments
* Rectangle      → bounding‑box polyline
* Obround        → bounding‑box polyline
* Polygon flash  → polygon vertices
* Region/Outline → recurse into contained primitives

Returns
-------
list[list[tuple[float, float]]]
    A list of strokes; every stroke is a closed or open list of XY tuples.
"""
from __future__ import annotations

import math
from typing import List, Tuple

from gerber import load_layer
from gerber.primitives import (
    Line,
    Arc,
    Circle,
    Rectangle,
    Region,
    Obround,
    Polygon,
    Outline,  # noqa: F401 (imported for completeness)
)

# ----------------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------------

def xy(pt) -> Tuple[float, float]:
    """Return an *(x, y)* tuple regardless of *pt*'s type."""
    if hasattr(pt, "x") and hasattr(pt, "y"):
        return pt.x, pt.y
    if isinstance(pt, (tuple, list)) and len(pt) >= 2:
        return pt[0], pt[1]
    raise TypeError(f"Cannot derive (x, y) coordinates from {pt!r}")


# ----------------------------------------------------------------------------
# Approximation helpers
# ----------------------------------------------------------------------------

_DEFAULT_SEGMENTS = 32  # quality of circle/arc approximation


def _is_clockwise(a: Arc) -> bool:
    """Best‑effort check whether *a* sweeps clockwise.

    Handles multiple Arc class APIs found in *pcb‑tools*, *gerbonara*, etc.::

        • ``a.clockwise``  → bool            (legacy pcb‑tools)
        • ``a.direction``  → 'clockwise' | 'counterclockwise' (current pcb‑tools)

    If neither attribute exists we fall back to the sign of the 2‑D cross
    product *(start→center) × (end→center)*: negative means clockwise in the
    usual mathematical coordinate system.
    """
    if hasattr(a, "clockwise"):
        return bool(a.clockwise)
    if hasattr(a, "direction"):
        return str(a.direction).lower().startswith("clockwise") or str(a.direction).lower().startswith("cw")

    # Geometric fallback ------------------------------------------------------
    sx, sy = xy(a.start)
    ex, ey = xy(a.end)
    cx, cy = xy(a.center)
    cross = (sx - cx) * (ey - cy) - (sy - cy) * (ex - cx)
    return cross < 0  # negative → clockwise


def arc_points(a: Arc, segments: int = _DEFAULT_SEGMENTS) -> List[Tuple[float, float]]:
    """Approximate an *Arc* with *segments* straight line segments."""
    cx, cy = xy(a.center)
    sx, sy = xy(a.start)
    ex, ey = xy(a.end)

    theta0 = math.atan2(sy - cy, sx - cx)
    theta1 = math.atan2(ey - cy, ex - cx)

    cw = _is_clockwise(a)

    # Ensure theta1 lies "after" theta0 in the chosen direction
    if cw and theta1 > theta0:
        theta1 -= 2 * math.pi
    elif (not cw) and theta1 < theta0:
        theta1 += 2 * math.pi

    return [
        (
            cx + a.radius * math.cos(theta0 + (theta1 - theta0) * i / segments),
            cy + a.radius * math.sin(theta0 + (theta1 - theta0) * i / segments),
        )
        for i in range(segments + 1)
    ]


def circle_points(c: Circle, segments: int = _DEFAULT_SEGMENTS) -> List[Tuple[float, float]]:
    """Return *segments* points approximating the circumference of *c*."""
    cx, cy = xy(getattr(c, "position", getattr(c, "center", (0, 0))))

    if hasattr(c, "radius") and c.radius is not None:
        r = c.radius
    elif hasattr(c, "width") and c.width is not None:
        r = c.width / 2.0
    else:
        raise AttributeError("Circle primitive lacks both radius and width attributes")

    return [
        (
            cx + r * math.cos(2 * math.pi * i / segments),
            cy + r * math.sin(2 * math.pi * i / segments),
        )
        for i in range(segments + 1)
    ]


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

def extract_strokes_from_gerber(path: str) -> List[List[Tuple[float, float]]]:
    """Parse *path* and return a list of stroke polylines for all primitives."""
    layer = load_layer(path)
    strokes: List[List[Tuple[float, float]]] = []

    def handle(p):
        # 1) Straight line -----------------------------------------------------
        if isinstance(p, Line):
            strokes.append([xy(p.start), xy(p.end)])

        # 2) Circle ------------------------------------------------------------
        elif isinstance(p, Circle):
            strokes.append(circle_points(p))

        # 3) Rectangle / Obround → bounding‑box polyline -----------------------
        elif isinstance(p, (Rectangle, Obround)):
            cx, cy = xy(getattr(p, "position", getattr(p, "center", (0, 0))))
            w = p.width
            h = getattr(p, "height", w)
            strokes.append(
                [
                    (cx - w / 2, cy - h / 2),
                    (cx + w / 2, cy - h / 2),
                    (cx + w / 2, cy + h / 2),
                    (cx - w / 2, cy + h / 2),
                    (cx - w / 2, cy - h / 2),
                ]
            )

        # 4) Arc ---------------------------------------------------------------
        elif isinstance(p, Arc):
            strokes.append(arc_points(p))

        # 5) Polygon flash -----------------------------------------------------
        elif isinstance(p, Polygon):
            verts = [xy(v) for v in p.vertices]
            strokes.append(verts + [verts[0]])  # close polyline

        # 6) Containers (Region, Outline, etc.) --------------------------------
        elif hasattr(p, "primitives"):
            for sub in p.primitives:  # type: ignore[attr-defined]
                handle(sub)

        # 7) Unknown primitive --------------------------------------------------
        else:
            print("⚠️  Unhandled primitive type:", type(p))

    for prim in layer.primitives:
        handle(prim)

    return strokes


# ----------------------------------------------------------------------------
# CLI helper (optional) -------------------------------------------------------
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import argparse

    parser = argparse.ArgumentParser(description="Extract strokes from a Gerber file")
    parser.add_argument("gerber", help="Path to the Gerber file")
    parser.add_argument("--segments", type=int, default=_DEFAULT_SEGMENTS, help="Number of segments per full circle (default: 32)")
    args = parser.parse_args()

    # Update default for this run only
    _DEFAULT_SEGMENTS = max(4, args.segments)

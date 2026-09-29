"""Plane geometry helpers shared by the simulator and the control core.

Coordinates are metres in the warehouse map frame, angles are radians.
Everything is pure Python so the core runs without NumPy.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

Point = tuple[float, float]
Polygon = Sequence[Point]


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def wrap_angle(angle: float) -> float:
    """Wrap to [-pi, pi).

    >>> round(wrap_angle(3 * math.pi / 2), 6)
    -1.570796
    """
    return (angle + math.pi) % (2 * math.pi) - math.pi


def lerp(a: Point, b: Point, t: float) -> Point:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def seg_point_dist(p: Point, a: Point, b: Point) -> float:
    """Distance from point p to segment ab.

    >>> seg_point_dist((1, 1), (0, 0), (2, 0))
    1.0
    >>> seg_point_dist((3, 0), (0, 0), (2, 0))
    1.0
    """
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom == 0.0:
        return dist(p, a)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / denom
    t = max(0.0, min(1.0, t))
    return dist(p, (ax + dx * t, ay + dy * t))


def point_in_polygon(p: Point, poly: Polygon) -> bool:
    """Even-odd ray casting; points on the boundary count as inside for rectangles.

    >>> point_in_polygon((1, 1), [(0, 0), (2, 0), (2, 2), (0, 2)])
    True
    >>> point_in_polygon((3, 1), [(0, 0), (2, 0), (2, 2), (0, 2)])
    False
    """
    x, y = p
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def rect(x0: float, y0: float, x1: float, y1: float) -> list[Point]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def bbox(poly: Polygon) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Proper or touching intersection of two segments.

    >>> segments_intersect((0, 0), (2, 2), (0, 2), (2, 0))
    True
    >>> segments_intersect((0, 0), (1, 0), (0, 1), (1, 1))
    False
    """
    d1 = _orient(q1, q2, p1)
    d2 = _orient(q1, q2, p2)
    d3 = _orient(p1, p2, q1)
    d4 = _orient(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0:
        return True

    def on_seg(a: Point, b: Point, c: Point) -> bool:
        return (min(a[0], b[0]) - 1e-12 <= c[0] <= max(a[0], b[0]) + 1e-12
                and min(a[1], b[1]) - 1e-12 <= c[1] <= max(a[1], b[1]) + 1e-12)

    if d1 == 0 and on_seg(q1, q2, p1):
        return True
    if d2 == 0 and on_seg(q1, q2, p2):
        return True
    if d3 == 0 and on_seg(p1, p2, q1):
        return True
    if d4 == 0 and on_seg(p1, p2, q2):
        return True
    return False


def segment_hits_polygon(a: Point, b: Point, poly: Polygon) -> bool:
    """True when segment ab crosses the polygon boundary or lies inside it."""
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return True
    n = len(poly)
    for i in range(n):
        if segments_intersect(a, b, poly[i], poly[(i + 1) % n]):
            return True
    return False


def to_local(p: Point, origin: Point, heading: float) -> Point:
    """Express world point p in a frame at origin rotated by heading (x forward).

    >>> tuple(round(v, 6) for v in to_local((1, 1), (0, 0), math.pi / 2))
    (1.0, -1.0)
    """
    dx, dy = p[0] - origin[0], p[1] - origin[1]
    c, s = math.cos(heading), math.sin(heading)
    return (c * dx + s * dy, -s * dx + c * dy)


def circle_hits_forward_box(center: Point, radius: float, origin: Point, heading: float,
                            length: float, half_width: float, start: float = 0.0) -> bool:
    """Does a circle intersect a box ahead of a vehicle?

    The box spans local x in [start, start+length] and |y| <= half_width.
    Used for laser-scanner style protective and warning fields.
    """
    lx, ly = to_local(center, origin, heading)
    nx = min(max(lx, start), start + length)
    ny = min(max(ly, -half_width), half_width)
    return math.hypot(lx - nx, ly - ny) <= radius


def centroid(points: Iterable[Point]) -> Point:
    pts = list(points)
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def box_gap(center: Point, radius: float, origin: Point, heading: float,
            half_length: float, half_width: float) -> float:
    """Clearance between a circle and an oriented rectangle (negative when overlapping).

    >>> round(box_gap((2.0, 0.0), 0.3, (0.0, 0.0), 0.0, 0.45, 0.35), 3)
    1.25
    >>> round(box_gap((0.0, 0.8), 0.3, (0.0, 0.0), 0.0, 0.45, 0.35), 3)
    0.15
    """
    lx, ly = to_local(center, origin, heading)
    nx = min(max(lx, -half_length), half_length)
    ny = min(max(ly, -half_width), half_width)
    return math.hypot(lx - nx, ly - ny) - radius

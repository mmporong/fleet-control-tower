"""Fleet-level tracking of people and forklifts.

Inputs are noisy detections from ceiling cameras (only inside their coverage)
and from robots' onboard sensors (only in line of sight). The tracker never
reads ground truth; the evaluator compares its tracks with ground truth
separately so blind spots stay visible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from .geometry import Point

MAX_SPEED = {"human": 2.0, "forklift": 3.0}
BODY_RADIUS = {"human": 0.3, "forklift": 0.6}        # forklift: half of a 1.17 m wide truck
BODY_HALF_LENGTH = {"forklift": 1.8}                 # 3.6 m counterbalance truck including forks
# below this estimated speed a track is treated as standing still: filter noise
# on a stationary person must not turn into a 2-second prediction sweep
STILL_SPEED = 0.3
CLEARANCE = {"human": 0.35, "forklift": 0.55}


@dataclass
class Detection:
    cls: str
    x: float
    y: float
    source: str


@dataclass
class Track:
    id: str
    cls: str
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    last_seen: float = 0.0
    first_seen: float = 0.0
    hits: int = 1
    sources: set[str] = field(default_factory=set)

    def predict(self, t: float) -> Point:
        dt = max(0.0, t - self.last_seen)
        if self.speed < STILL_SPEED:
            return (self.x, self.y)
        return (self.x + self.vx * dt, self.y + self.vy * dt)

    def sigma(self, t: float) -> float:
        """Position uncertainty radius (m) that grows while the track is not observed."""
        return 0.15 + 0.3 * max(0.0, t - self.last_seen)

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)


class Tracker:
    def __init__(self, gate: float = 1.6, max_age: float = 3.0, alpha: float = 0.55, beta: float = 0.25) -> None:
        self.gate = gate
        self.max_age = max_age
        self.alpha = alpha
        self.beta = beta
        self.tracks: dict[str, Track] = {}
        self._next = 1
        self.lost_events: list[dict] = []

    @staticmethod
    def cluster(detections: list[Detection], radius: float = 0.7) -> list[Detection]:
        """Merge detections of one object seen by several sensors in the same cycle."""
        merged: list[tuple[Detection, list[Detection]]] = []
        for d in detections:
            for rep, members in merged:
                if rep.cls == d.cls and math.hypot(rep.x - d.x, rep.y - d.y) <= radius:
                    members.append(d)
                    rep.x = sum(m.x for m in members) / len(members)
                    rep.y = sum(m.y for m in members) / len(members)
                    rep.source = rep.source if d.source in rep.source else f"{rep.source},{d.source}"
                    break
            else:
                merged.append((Detection(d.cls, d.x, d.y, d.source), [d]))
        return [rep for rep, _ in merged]

    def update(self, detections: list[Detection], now: float) -> list[dict]:
        events: list[dict] = []
        dets = self.cluster(detections)
        pairs = []
        for tid, tr in self.tracks.items():
            px, py = tr.predict(now)
            gate = self.gate + tr.sigma(now)
            for j, d in enumerate(dets):
                if d.cls != tr.cls:
                    continue
                dd = math.hypot(d.x - px, d.y - py)
                if dd <= gate:
                    pairs.append((dd, tid, j))
        pairs.sort()
        used_t: set[str] = set()
        used_d: set[int] = set()
        for dd, tid, j in pairs:
            if tid in used_t or j in used_d:
                continue
            used_t.add(tid)
            used_d.add(j)
            self._correct(self.tracks[tid], dets[j], now)
        for j, d in enumerate(dets):
            if j in used_d:
                continue
            tid = f"T{self._next:04d}"
            self._next += 1
            self.tracks[tid] = Track(tid, d.cls, d.x, d.y, last_seen=now, first_seen=now,
                                     sources=set(d.source.split(",")))
            events.append({"type": "track_new", "track": tid, "cls": d.cls})
        for tid in list(self.tracks):
            tr = self.tracks[tid]
            if now - tr.last_seen > self.max_age:
                del self.tracks[tid]
                ev = {"type": "track_lost", "track": tid, "cls": tr.cls, "x": round(tr.x, 1), "y": round(tr.y, 1)}
                events.append(ev)
        return events

    def prune(self, now: float) -> list[dict]:
        """Drop tracks not seen for ``max_age`` even when no sensor cycle arrives."""
        events = []
        for tid in [t for t, tr in self.tracks.items() if now - tr.last_seen > self.max_age]:
            tr = self.tracks.pop(tid)
            events.append({"type": "track_lost", "track": tid, "cls": tr.cls, "x": round(tr.x, 1),
                           "y": round(tr.y, 1)})
        return events

    def _correct(self, tr: Track, d: Detection, now: float) -> None:
        dt = max(0.1, now - tr.last_seen)
        px, py = tr.predict(now)
        rx, ry = d.x - px, d.y - py
        tr.x = px + self.alpha * rx
        tr.y = py + self.alpha * ry
        tr.vx += self.beta * rx / dt
        tr.vy += self.beta * ry / dt
        vmax = MAX_SPEED.get(tr.cls, 2.0)
        sp = math.hypot(tr.vx, tr.vy)
        if sp > vmax:
            tr.vx *= vmax / sp
            tr.vy *= vmax / sp
        tr.last_seen = now
        tr.hits += 1
        tr.sources = set(d.source.split(","))

    def predicted_discs(self, now: float, horizon: dict[str, float],
                        step: float = 0.5) -> list[tuple[str, str, float, float, float]]:
        """(track id, cls, x, y, radius) samples over each class's prediction horizon.

        The radius is body + clearance + position uncertainty; moving tracks also
        widen with look-ahead time because heading changes are not modelled.
        """
        out = []
        for tr in self.tracks.values():
            moving = tr.speed >= STILL_SPEED
            h = horizon.get(tr.cls, 2.0) if moving else 0.0
            base = BODY_RADIUS.get(tr.cls, 0.3) + CLEARANCE.get(tr.cls, 0.35)
            half = BODY_HALF_LENGTH.get(tr.cls, 0.0)
            if half and not moving:
                base += half                  # heading unknown: bound the whole truck
                half = 0.0
            ux, uy = (tr.vx / tr.speed, tr.vy / tr.speed) if moving else (0.0, 0.0)
            k = 0
            while k * step <= h + 1e-9:
                t = now + k * step
                px, py = tr.predict(t)
                r = base + tr.sigma(t) + 0.15 * k * step
                # a long vehicle: circles along its heading cover the body without widening it
                for off in ((-half, -half / 3, half / 3, half) if half else (0.0,)):
                    out.append((tr.id, tr.cls, px + ux * off, py + uy * off, r))
                k += 1
        return out

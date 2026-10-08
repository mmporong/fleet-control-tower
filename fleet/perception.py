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
# a 3.6 m counterbalance truck (forks included) is four such circles along its heading
BODY_OFFSETS = {"forklift": (-1.2, -0.4, 0.4, 1.2)}
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
    heading: float | None = None      # last direction of travel; a stopped truck keeps its orientation
    # the direction of travel before the track last reversed and where it did (a truck backing off, or turning
    # round); cleared when it turns a corner
    heading_before: float | None = None
    reversed_xy: tuple[float, float] | None = None
    votes: dict = field(default_factory=dict)    # class -> decayed count of detections that called it so

    @property
    def confirmed(self) -> bool:
        """Two hits before a track counts: a single false detection never blocks or slows anything."""
        return self.hits >= 2

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
                dd = math.hypot(d.x - px, d.y - py)
                if dd <= gate:
                    # associate by position; a detection of the other class costs extra so that a person next
                    # to a truck still goes to the person's track, but a misread class does not start a new one
                    pairs.append((dd + (0.0 if d.cls == tr.cls else 0.8), tid, j))
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
                                     sources=set(d.source.split(",")), votes={d.cls: 1.0})
            events.append({"type": "track_new", "track": tid, "cls": d.cls})
        for tid in list(self.tracks):
            tr = self.tracks[tid]
            if now - tr.last_seen > self.max_age:
                del self.tracks[tid]
                ev = {"type": "track_lost", "track": tid, "cls": tr.cls, "x": round(tr.x, 1), "y": round(tr.y, 1)}
                events.append(ev)
        self._merge_duplicates()
        return events

    def _merge_duplicates(self, radius: float = 0.8, same_class_radius: float = 0.4) -> None:
        """Two tracks on one object: fold the younger into the older and let the summed class votes decide.

        A misread class starts a second track of the other class on the same body (a truck read as a person), so
        tracks of different classes within ``radius`` are merged. Two people side by side are two tracks of
        the same class; those merge only when closer than ``same_class_radius`` (detections closer than 0.7 m
        are already one cluster, so two real people are never that close as tracks)."""
        ids = sorted(self.tracks, key=lambda t: self.tracks[t].first_seen)
        gone: set[str] = set()
        for i, a in enumerate(ids):
            if a in gone:
                continue
            ta = self.tracks[a]
            for b in ids[i + 1:]:
                if b in gone:
                    continue
                tb = self.tracks[b]
                limit = same_class_radius if ta.cls == tb.cls else radius
                if math.hypot(ta.x - tb.x, ta.y - tb.y) > limit:
                    continue
                for k, v in tb.votes.items():
                    ta.votes[k] = ta.votes.get(k, 0.0) + v
                ta.cls = max(ta.votes, key=ta.votes.get) if ta.votes else ta.cls
                ta.hits += tb.hits
                ta.last_seen = max(ta.last_seen, tb.last_seen)
                ta.sources |= tb.sources
                gone.add(b)
        for b in gone:
            del self.tracks[b]

    def prune(self, now: float) -> list[dict]:
        """Drop tracks not seen for ``max_age`` even when no sensor cycle arrives."""
        events = []
        for tid in [t for t, tr in self.tracks.items() if now - tr.last_seen > self.max_age]:
            tr = self.tracks.pop(tid)
            events.append({"type": "track_lost", "track": tid, "cls": tr.cls, "x": round(tr.x, 1),
                           "y": round(tr.y, 1)})
        return events

    def _correct(self, tr: Track, d: Detection, now: float) -> None:
        # class by decayed majority vote: occasional misread detections do not flip a track
        for k in tr.votes:
            tr.votes[k] *= 0.9
        tr.votes[d.cls] = tr.votes.get(d.cls, 0.0) + 1.0
        tr.cls = max(tr.votes, key=tr.votes.get)
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
        if math.hypot(tr.vx, tr.vy) >= STILL_SPEED:
            heading = math.atan2(tr.vy, tr.vx)
            if tr.heading is not None:
                turned = abs(math.remainder(heading - tr.heading, 2 * math.pi))
                if turned > 2.6:
                    tr.heading_before, tr.reversed_xy = tr.heading, (tr.x, tr.y)
                elif turned > 1.0:
                    tr.heading_before = tr.reversed_xy = None
            tr.heading = heading
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
            if not tr.confirmed:
                continue
            moving = tr.speed >= STILL_SPEED
            h = horizon.get(tr.cls, 2.0) if moving else 0.0
            base = BODY_RADIUS.get(tr.cls, 0.3) + CLEARANCE.get(tr.cls, 0.35)
            offsets = BODY_OFFSETS.get(tr.cls, (0.0,))
            # a moving truck is predicted along its travel; a stopped one may turn on the spot, so its
            # whole swept circle is held (half length 1.8 m + clearance)
            heading = math.atan2(tr.vy, tr.vx) if moving else None
            if heading is None and offsets != (0.0,):
                base += max(abs(o) for o in offsets)
                offsets = (0.0,)
            ux, uy = (math.cos(heading), math.sin(heading)) if heading is not None else (0.0, 0.0)
            k = 0
            while k * step <= h + 1e-9:
                t = now + k * step
                px, py = tr.predict(t)
                r = base + tr.sigma(t) + 0.15 * k * step
                # a long vehicle: circles along its heading cover the body without widening it
                for off in offsets:
                    out.append((tr.id, tr.cls, px + ux * off, py + uy * off, r))
                k += 1
        return out

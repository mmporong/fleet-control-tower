"""Units the fleet manager cannot command.

- Human: dock workers, aisle pickers and walkers. Some are distracted on a
  given trip and do not yield to robots.
- Forklift: manually driven, fast, yields to people but claims right of way
  over robots.
- ExternalRobot: another vendor's cleaning robot. It shares its itinerary
  (read-only fleet, as in Open-RMF) but ignores our reservations.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import random

from .geometry import Point, circle_hits_forward_box, dist, point_in_polygon
from .layout import COLUMN_X, ROW_Y, Layout, grid_id


def undirected_path(layout: Layout, start: str, goal: str, allowed_kinds: set[str]) -> list[str]:
    """BFS on the roadmap ignoring edge direction (people walk against one-way aisles)."""
    prev: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        if cur == goal:
            break
        for nb in sorted(layout.neighbors[cur]):
            if nb not in prev and (layout.nodes[nb].kind in allowed_kinds or nb == goal):
                prev[nb] = cur
                queue.append(nb)
    if goal not in prev:
        return []
    path = [goal]
    while prev[path[-1]] is not None:
        path.append(prev[path[-1]])
    return path[::-1]


@dataclass
class Walker:
    """Follows a polyline at a nominal speed; the owner decides when to stop."""
    points: list[Point] = field(default_factory=list)
    idx: int = 0

    def target(self) -> Point | None:
        return self.points[self.idx] if self.idx < len(self.points) else None

    def advance(self, pos: Point, step: float) -> tuple[Point, float]:
        x, y = pos
        heading = 0.0
        while step > 1e-9 and self.idx < len(self.points):
            tx, ty = self.points[self.idx]
            d = math.hypot(tx - x, ty - y)
            heading = math.atan2(ty - y, tx - x) if d > 1e-9 else heading
            if d <= step:
                x, y = tx, ty
                step -= d
                self.idx += 1
            else:
                x += (tx - x) / d * step
                y += (ty - y) / d * step
                step = 0.0
        return (x, y), heading

    def done(self) -> bool:
        return self.idx >= len(self.points)


HUMAN_KINDS = {"spine", "cross", "queue", "storage", "inbound", "outbound"}
WALKWAY_KINDS = {"spine", "cross", "queue"}   # walkers keep to wide cross aisles and wall lanes


@dataclass
class Human:
    id: str
    role: str                 # worker | picker | walker
    x: float
    y: float
    rng: random.Random
    radius: float = 0.3
    speed: float = 1.2
    heading: float = 0.0
    v: float = 0.0
    dwell_until: float = 0.0
    distracted: bool = False
    # this trip the person does not heed vehicles at all: no yielding and no avoiding a vehicle that is moving
    # (a standing one is still walked round). The share of such trips is a swept assumption (docs/SITE.md).
    inattentive: bool = False
    violation_p: float = 0.0
    walker: Walker = field(default_factory=Walker)
    spots: list[Point] = field(default_factory=list)
    aisle_col: int | None = None
    frozen_until: float = -1.0   # operator drill: person stands still in place
    blocked_since: float | None = None
    replans: int = 0
    detour_until_idx: int = -1   # walker index where the current sidestep ends
    aside: bool = False          # picker standing at the rack face to let a robot pass
    yield_since: float | None = None
    impatient_until: float = -1.0
    aside_deadline: float = 0.0
    asides: int = 0

    @property
    def pos(self) -> Point:
        return (self.x, self.y)

    def plan_next(self, layout: Layout) -> None:
        self.distracted = self.rng.random() < 0.15
        self.inattentive = self.violation_p > 0 and self.rng.random() < self.violation_p
        self.detour_until_idx = -1
        if self.role == "worker":
            target = self.rng.choice(self.spots)
            self.walker = Walker([target])
        elif self.role == "picker":
            col = self.aisle_col if self.aisle_col is not None else 3
            rows = [r for r in range(1, 10) if r not in (1, 5, 9)]
            goal = grid_id(col, self.rng.choice(rows))
            start = layout.nearest_node(self.pos, tuple(HUMAN_KINDS))
            nodes = undirected_path(layout, start, goal, HUMAN_KINDS)
            side = self.rng.choice((-0.8, 0.8))   # picking happens at the rack face
            pts = [layout.nodes[n].pos for n in nodes[1:]]
            if pts:
                gx, gy = pts[-1]
                pts[-1] = (gx + side, gy)
            self.walker = Walker(pts)
        else:  # walker crossing the building along a cross row
            row = self.rng.choice((5, 5, 5, 1, 9))
            end_col = 0 if self.x > 30 else 9
            start = layout.nearest_node(self.pos, tuple(WALKWAY_KINDS))
            goal = grid_id(end_col, row)
            nodes = undirected_path(layout, start, goal, WALKWAY_KINDS)
            off = self.rng.choice((-1.0, 1.0)) * self.rng.uniform(0.9, 1.1)
            pts = []
            for a, b in zip([start] + nodes[1:], nodes[1:]):
                na, nb = layout.nodes[a], layout.nodes[b]
                if abs(nb.x - na.x) > abs(nb.y - na.y):      # along a row: keep to one side
                    pts.append((nb.x, nb.y + off))
                else:                                        # along a wall lane
                    pts.append((nb.x + off, nb.y))
            # wait next to the wall, not on the wall-lane intersection
            pts.append((1.0 if end_col == 0 else layout.width - 1.0, layout.nodes[goal].y + off))
            self.walker = Walker(pts)

    def _picker_yield(self, dt: float, now: float, layout: Layout, robots) -> bool:
        """Collaborative picking: step to the rack face and let a robot through the aisle."""
        col_x = COLUMN_X[self.aisle_col] if self.aisle_col is not None else self.x
        near = [(rx, ry) for _rid, rx, ry, _rr, _rv in robots
                if abs(rx - col_x) < 1.0 and abs(ry - self.y) < 3.5]
        if self.aside:
            side_x = col_x + (0.8 if self.x >= col_x else -0.8)
            if abs(self.x - side_x) > 0.05:
                step = min(abs(side_x - self.x), 1.0 * dt)
                self.x += step if side_x > self.x else -step
                self.v = 1.0
                return True
            self.v = 0.0
            if near and now < self.aside_deadline:
                return True
            self.aside = False
            return False
        in_rack_segment = 4.6 < self.y < 15.4 or 18.6 < self.y < 29.4
        if near and in_rack_segment and now >= self.dwell_until and abs(self.x - col_x) < 0.7:
            self.aside = True
            self.aside_deadline = now + 25.0
            self.asides += 1
            return True
        return False

    def _at_shelf_face(self) -> bool:
        """Overridden by collaborative pickers standing at a pick-aisle shelf face."""
        return False

    def _sidestep(self, layout: Layout, obstacle: Point, obstacle_r: float, heading: float) -> bool:
        """Walk around a stationary robot if there is floor space beside it."""
        fx, fy = math.cos(heading), math.sin(heading)
        # signed lateral offset of the robot from the walking line
        lat = (obstacle[0] - self.x) * -fy + (obstacle[1] - self.y) * fx
        # comfortable clearance first; in a 2.2 m rack aisle people squeeze past with 10 cm
        options = [(side, margin) for margin in (0.35, 0.1)
                   for side in ((-1.0, 1.0) if lat > 0 else (1.0, -1.0))]
        for side, margin in options:
            clear = obstacle_r + self.radius + margin
            nx, ny = -fy * side, fx * side
            # the robot's offset after moving `shift` toward `side` is lat - side * shift; it must end
            # at least `clear` away: away from the robot that is clear - |lat|, past it clear + |lat|
            shift = max(0.0, clear + side * lat)
            ax, ay = self.x + nx * shift, self.y + ny * shift
            along = (obstacle[0] - self.x) * fx + (obstacle[1] - self.y) * fy + obstacle_r + 0.8
            bx, by = ax + fx * along, ay + fy * along
            pts = [(ax, ay), ((ax + bx) / 2, (ay + by) / 2), (bx, by)]
            if all(0.3 < x < layout.width - 0.3 and 0.3 < y < layout.height - 0.3 and
                   not any(point_in_polygon((x, y), r) for r in layout.racks) for x, y in pts):
                self.walker.points[self.walker.idx:self.walker.idx] = [(ax, ay), (bx, by)]
                self.detour_until_idx = self.walker.idx + 2
                return True
        return False

    def step(self, dt: float, now: float, layout: Layout, robots: list[tuple[str, float, float, float, float]],
             forklifts: list["Forklift"]) -> None:
        if now < self.frozen_until:
            self.v = 0.0
            return
        if now < self.dwell_until:
            self.v = 0.0
            return
        if self.role == "picker" and self._picker_yield(dt, now, layout, robots):
            return
        if self.walker.done():
            if self.role == "worker":
                self.dwell_until = now + self.rng.uniform(20, 90)
            elif self.role == "picker":
                self.dwell_until = now + self.rng.uniform(15, 50)
            else:
                self.dwell_until = now + self.rng.uniform(2, 10)
            self.plan_next(layout)
            self.v = 0.0
            return
        # a waypoint already reached has no direction: skip it before judging what is ahead
        while self.walker.target() is not None and dist(self.walker.target(), self.pos) < 0.02:
            self.walker.idx += 1
        tgt = self.walker.target()
        if tgt is None:
            self.v = 0.0
            return
        heading = math.atan2(tgt[1] - self.y, tgt[0] - self.x)
        heedless = self.distracted or self.inattentive
        yield_dist = 0.0 if heedless else 1.2
        blocked = False
        for rid, rx, ry, rr, rv in robots:
            gap = math.hypot(rx - self.x, ry - self.y) - rr - self.radius
            if gap > 1.6 or self.inattentive and abs(rv) > 0.05:
                continue
            if abs(rv) > 0.05 and not heedless and gap < 1.2 and now > self.impatient_until and \
                    not self._at_shelf_face() and circle_hits_forward_box((rx, ry), rr, self.pos, heading, 1.8, 1.2):
                blocked = True          # a moving robot nearby, in front or crossing: wait for it
                if self.yield_since is None:
                    self.yield_since = now
                elif now - self.yield_since > 5.0:
                    # after ~5 s people walk on; the robot's safety field makes it stop for them
                    self.impatient_until = now + 6.0
                    self.yield_since = None
                break
            ahead = circle_hits_forward_box((rx, ry), rr, self.pos, heading, 1.4, self.radius + 0.1)
            if not ahead:
                continue
            if abs(rv) > 0.05 and gap < max(yield_dist, 0.05):
                blocked = True          # let a moving robot pass
                break
            if abs(rv) <= 0.05 and gap < 0.6:
                if self.walker.idx < self.detour_until_idx:
                    if gap < 0.05:
                        blocked = True      # already walking around it; never walk into it
                    break
                if self._sidestep(layout, (rx, ry), rr, heading):
                    tgt = self.walker.target()
                    heading = math.atan2(tgt[1] - self.y, tgt[0] - self.x)
                else:
                    blocked = True
                break
        for f in forklifts if not blocked else ():
            circles = f.circles()
            if not any(circle_hits_forward_box((cx, cy), cr, self.pos, heading, 2.5, self.radius + 0.3)
                       for cx, cy, cr in circles):
                continue
            gap = min(math.hypot(cx - self.x, cy - self.y) - cr for cx, cy, cr in circles) - self.radius
            if f.v > 0.05:
                blocked = True          # never cross in front of a moving forklift
                break
            if gap < 0.6 and self.walker.idx >= self.detour_until_idx:
                if self._sidestep(layout, f.pos, 0.6, heading):
                    tgt = self.walker.target()
                else:
                    blocked = True
                break
        if blocked:
            self.v = 0.0
            if self.blocked_since is None:
                self.blocked_since = now
            elif now - self.blocked_since > 6.0:
                # people do not wait forever: they turn around or pick another way
                self.blocked_since = None
                self.replans += 1
                self.plan_next(layout)
            return
        self.yield_since = None if not blocked else self.yield_since
        bodies = [(rx, ry, rr, rv) for _rid, rx, ry, rr, rv in robots] + \
            [(cx, cy, cr, f.v) for f in forklifts for cx, cy, cr in f.circles()]
        overlap = [(bx, by, br) for bx, by, br, bv in bodies
                   if math.hypot(bx - self.x, by - self.y) < br + self.radius
                   and not (self.inattentive and abs(bv) > 0.05)]
        if overlap:
            # already touching a vehicle body: step straight away from it first
            bx, by, _ = overlap[0]
            d = math.hypot(self.x - bx, self.y - by) or 1e-6
            nx, ny = self.x + (self.x - bx) / d * 0.1, self.y + (self.y - by) / d * 0.1
            if not any(point_in_polygon((nx, ny), r) for r in layout.racks):
                self.x, self.y = nx, ny
            self.v = 0.0
            return
        trial = Walker(self.walker.points, self.walker.idx)
        new_pos, heading2 = trial.advance(self.pos, self.speed * dt)
        for bx, by, br, bv in bodies:
            if self.inattentive and abs(bv) > 0.05:
                continue            # does not look: walks on into a moving vehicle's way
            d_new = math.hypot(bx - new_pos[0], by - new_pos[1]) - br - self.radius
            d_old = math.hypot(bx - self.x, by - self.y) - br - self.radius
            if d_new < 0.06 and d_new < d_old:
                # never walk into a vehicle body (margin above the contact threshold); a vehicle that
                # stands across the walking line at an angle is walked round, as people do
                if abs(bv) <= 0.05 and self.walker.idx >= self.detour_until_idx and \
                        self._sidestep(layout, (bx, by), br, heading):
                    self.v = 0.0
                    return
                blocked = True
                break
        if blocked:
            self.v = 0.0
            if self.blocked_since is None:
                self.blocked_since = now
            elif now - self.blocked_since > 6.0:
                self.blocked_since = None
                self.replans += 1
                self.plan_next(layout)
            return
        self.blocked_since = None
        self.v = self.speed
        self.walker.idx = trial.idx
        self.heading = heading2
        self.x, self.y = new_pos


def expand_loop(layout: Layout, loop: list[str], allowed: set[str] | None = None) -> list[str]:
    """Fill in the nodes between consecutive loop waypoints that are not adjacent."""
    out: list[str] = []
    for a, b in zip(loop, loop[1:] + loop[:1]):
        out.append(a)
        if b not in layout.neighbors[a]:
            path = undirected_path(layout, a, b, allowed or {"queue"})
            if not path:
                raise ValueError(f"loop waypoints {a} and {b} are not connected")
            out.extend(path[1:-1])
    return out


TURN_RATE = 0.8        # rad/s: a counterbalance truck turning on the spot (rear end at walking pace)


@dataclass
class Forklift:
    """Counterbalance truck, 3.6 m x 1.17 m including forks, modelled as four 0.6 m circles along its heading."""
    id: str
    loop: list[str]
    layout: Layout
    rng: random.Random
    radius: float = 1.9          # bounding radius (planning and coarse checks)
    speed: float = 2.0
    idx: int = 0
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0
    v: float = 0.0
    dwell_nodes: tuple[str, ...] = ()
    dwell_until: float = 0.0
    direction: int = 1
    blocked_since: float | None = None
    reversals: int = 0
    reversed_at: float = -1e9
    passing: dict = field(default_factory=dict)   # stationary AMR id -> pass-by allowed until
    passes: int = 0
    lat: float = 0.0            # lateral offset from the lane centre while passing
    lat_target: float = 0.0
    keep_offset_until: float = 0.0   # gave up returning to the lane centre: drive on at the offset
    turn_wait_since: float | None = None
    turn_committed: bool = False
    turning: bool = False
    # a standing vehicle in the circle the truck sweeps: after 10 s most drivers back out the way they came,
    # this share turns anyway (a rule violation; swept in the benchmark)
    careless_p: float = 0.1
    careless_turns: int = 0
    stall_since: float | None = None
    bx: float = 0.0             # position on the lane centre line
    by: float = 0.0

    def __post_init__(self) -> None:
        n = self.layout.nodes[self.loop[0]]
        self.bx, self.by = n.x, n.y
        self.x, self.y = n.x, n.y

    def _free_offset(self, heading: float, offset: float, robots=()) -> bool:
        """Room beside the lane for the whole truck: no racks, walls or other vehicles."""
        nx, ny = -math.sin(heading), math.cos(heading)
        for d in (-1.2, -0.4, 0.4, 1.2, 2.2):
            px = self.bx + math.cos(heading) * d + nx * offset
            py = self.by + math.sin(heading) * d + ny * offset
            if not (0.7 < px < self.layout.width - 0.7 and 0.7 < py < self.layout.height - 0.7):
                return False
            for r in self.layout.racks:
                x0, y0 = r[0]
                x1, y1 = r[2]
                if x0 - 0.6 < px < x1 + 0.6 and y0 - 0.6 < py < y1 + 0.6:
                    return False
            for _rid, rx, ry, rr, _rv in robots:
                if math.hypot(rx - px, ry - py) < rr + 0.6 + 0.3:
                    return False
        return True

    def _target(self) -> int:
        return (self.idx + self.direction) % len(self.loop)

    def _reverse(self) -> None:
        """Driver gives up on the blocked way and drives back the way they came."""
        self.idx = self._target()
        self.direction = -self.direction
        self.reversals += 1
        self.blocked_since = None
        # heading flips: keep the same world-side offset
        self.lat, self.lat_target = -self.lat, -self.lat_target

    @property
    def pos(self) -> Point:
        return (self.x, self.y)

    def circles(self) -> list[tuple[float, float, float]]:
        """3.6 m x 1.17 m counterbalance truck including forks."""
        c, s = math.cos(self.heading), math.sin(self.heading)
        return [(self.x + c * d, self.y + s * d, 0.6) for d in (-1.2, -0.4, 0.4, 1.2)]

    def step(self, dt: float, now: float, robots: list[tuple[str, float, float, float, float]],
             humans: list[Human]) -> None:
        if now < self.dwell_until:
            self.v = 0.0
            return
        nxt = self.loop[self._target()]
        tn = self.layout.nodes[nxt]
        heading = math.atan2(tn.y - self.by, tn.x - self.bx)
        # lateral manoeuvre toward the passing offset (or back to the lane centre)
        if not any(until > now for until in self.passing.values()) and now >= self.keep_offset_until:
            self.lat_target = 0.0
        if abs(self.lat - self.lat_target) > 1e-3:
            mine = self.circles()
            if any(math.hypot(rx - cx, ry - cy) < rr + cr + 0.5 for _i, rx, ry, rr, _v in robots
                   for cx, cy, cr in mine):
                self.v = 0.0          # something beside the truck: hold the sideways move
                if self.blocked_since is None:
                    self.blocked_since = now
                elif now - self.blocked_since > 8.0:
                    # the driver stops trying to straighten up and carries on where the truck is
                    self.lat_target = self.lat
                    self.keep_offset_until = now + 10.0
                    self.blocked_since = None
                self._stalled(now)
                return
            step_lat = min(abs(self.lat_target - self.lat), 0.8 * dt)
            self.lat += step_lat if self.lat_target > self.lat else -step_lat
            self._place(heading)
            self.v = 0.4
            self.stall_since = None
            return
        for h in humans:
            if circle_hits_forward_box(h.pos, h.radius, self.pos, heading, 3.0, 0.7, start=1.8):
                self.v = 0.0
                if self.blocked_since is None:
                    self.blocked_since = now
                elif now - self.blocked_since > 10.0 and now - self.reversed_at > 30.0:
                    self.reversed_at = now
                    self._reverse()
                return
        for rid, rx, ry, rr, rv in robots:
            if self.passing.get(rid, -1.0) > now:
                continue
            if circle_hits_forward_box((rx, ry), rr, self.pos, heading, 1.6, 0.75, start=1.8):
                self.v = 0.0
                if self.blocked_since is None:
                    self.blocked_since = now
                elif now - self.blocked_since > 8.0:
                    side = next((o for o in (1.3, -1.3) if self._free_offset(heading, o, robots)), None)
                    if abs(rv) < 0.05 and side is not None:
                        # driver steers around a robot that stays put, with body clearance
                        self.passing[rid] = now + 15.0
                        self.lat_target = side
                        self.passes += 1
                        self.blocked_since = None
                    elif now - self.reversed_at > 30.0:
                        self.reversed_at = now
                        self._reverse()
                return
        if self._aisle_occupied(tn, robots):
            self.v = 0.0            # aisle signal: a robot is working the narrow aisle ahead, wait at its mouth
            return
        turn = math.remainder(heading - self.heading, 2 * math.pi)
        if abs(turn) > 0.05 and self.lat == 0.0:
            inside = [rv for _i, rx, ry, rr, rv in robots if math.hypot(rx - self.bx, ry - self.by) < 1.9 + rr + 0.2]
            if not self.turn_committed:
                # a vehicle driving through the circle is always let past; one standing in it is waited for
                if any(abs(v) > 0.05 for v in inside) or inside and (
                        self.turn_wait_since is None or now - self.turn_wait_since < 10.0):
                    self.turn_wait_since = self.turn_wait_since if self.turn_wait_since is not None else now
                    self.v = 0.0
                    self.turning = False
                    return
                if inside:
                    # waited 10 s for a vehicle that does not move: most drivers back out, some turn anyway
                    if self.rng.random() >= self.careless_p:
                        if now - self.reversed_at > 30.0:
                            self.reversed_at = now
                            self._reverse()
                        self.turn_wait_since = now
                        self.v = 0.0
                        self.turning = False
                        return
                    self.careless_turns += 1
                self.turn_committed = True       # once started, the turn is finished in one go
            self.turn_wait_since = None
            self.turning = True
            # the truck turns at walking pace on the spot: its 3.6 m body sweeps the next lane gradually,
            # so the vehicles there see it coming instead of it appearing across their lane
            self.heading += max(-TURN_RATE * dt, min(TURN_RATE * dt, turn))
            self._place(self.heading)
            self.v = 0.0
            self.stall_since = None
            return
        self.turn_committed = False
        self.turning = False
        self.blocked_since = None
        self.stall_since = None
        speed = self.speed
        zone_limit = [z for z in self.layout.zones_at(self.pos) if z.kind in ("human_work", "walkway")]
        if zone_limit:
            speed = min(speed, 1.39)      # 5 km/h where people work or walk
        if self.lat != 0.0:
            speed = min(speed, 0.8)
        self.v = speed
        step = speed * dt
        d = math.hypot(tn.x - self.bx, tn.y - self.by)
        self.heading = heading
        if d <= step:
            self.bx, self.by = tn.x, tn.y
            self.idx = self._target()
            if nxt in self.dwell_nodes and self.lat == 0.0:
                self.dwell_until = now + self.rng.uniform(20, 45)
        else:
            self.bx += (tn.x - self.bx) / d * step
            self.by += (tn.y - self.by) / d * step
        self._place(heading)

    def _aisle_occupied(self, target, robots) -> bool:
        """Entering a narrow aisle (zone kind "aisle") that a robot is inside."""
        ahead = [z for z in self.layout.zones_at(target.pos) if z.kind == "aisle"]
        if not ahead or any(z.kind == "aisle" for z in self.layout.zones_at((self.bx, self.by))):
            return False
        return any(point_in_polygon((rx, ry), z.polygon) for z in ahead for _i, rx, ry, _r, _v in robots)

    def _stalled(self, now: float) -> None:
        """A sideways move that stays blocked even at the offset: after 30 s the driver backs out
        the way they came (the other blocked cases already turn back on their own timers)."""
        if self.stall_since is None:
            self.stall_since = now
        elif now - self.stall_since > 30.0 and now - self.reversed_at > 30.0:
            self.reversed_at = now
            self.stall_since = None
            self._reverse()

    def _place(self, heading: float) -> None:
        self.x = self.bx - math.sin(heading) * self.lat
        self.y = self.by + math.cos(heading) * self.lat

    def next_nodes(self, count: int = 4) -> list[str]:
        return [self.loop[(self.idx + k * self.direction) % len(self.loop)] for k in range(1, count + 1)]


@dataclass
class ExternalRobot:
    """Third-party robot: publishes position and itinerary, cannot be commanded."""
    id: str
    loop: list[str]
    layout: Layout
    radius: float = 0.5
    speed: float = 0.5
    idx: int = 0
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0
    v: float = 0.0
    direction: int = 1
    blocked_since: float | None = None
    reversals: int = 0

    def __post_init__(self) -> None:
        n = self.layout.nodes[self.loop[0]]
        self.x, self.y = n.x, n.y

    @property
    def pos(self) -> Point:
        return (self.x, self.y)

    @property
    def current_node(self) -> str:
        return self.loop[self.idx]

    def itinerary(self, count: int = 3) -> list[str]:
        """Node it is on (or just left) followed by the next nodes, as its adapter reports."""
        return [self.loop[(self.idx + k * self.direction) % len(self.loop)] for k in range(0, count + 1)]

    def step(self, dt: float, obstacles: list[tuple[str, float, float, float]], now: float = 0.0) -> None:
        nxt = self.loop[(self.idx + self.direction) % len(self.loop)]
        tn = self.layout.nodes[nxt]
        heading = math.atan2(tn.y - self.y, tn.x - self.x)
        for oid, ox, oy, orad in obstacles:
            if oid != self.id and circle_hits_forward_box((ox, oy), orad, self.pos, heading, 0.8, 0.45, start=0.5):
                self.v = 0.0
                if self.blocked_since is None:
                    self.blocked_since = now
                elif now - self.blocked_since > 12.0:
                    # its own navigation turns back; it still never takes our commands
                    self.idx = (self.idx + self.direction) % len(self.loop)
                    self.direction = -self.direction
                    self.reversals += 1
                    self.blocked_since = None
                return
        self.blocked_since = None
        self.v = self.speed
        self.heading = heading
        step = self.speed * dt
        d = math.hypot(tn.x - self.x, tn.y - self.y)
        if d <= step:
            self.x, self.y = tn.x, tn.y
            self.idx = (self.idx + self.direction) % len(self.loop)
        else:
            self.x += (tn.x - self.x) / d * step
            self.y += (tn.y - self.y) / d * step


def worker_spots(side: str) -> list[Point]:
    """Places next to the docks that sit between robot lanes (1.75 m from each stub)."""
    if side == "in":
        return [(1.4, 8.25), (1.4, 25.75), (1.4, 11.9), (1.4, 22.1), (1.4, 5.0), (1.4, 29.0)]
    return [(58.6, 8.25), (58.6, 25.75), (58.6, 11.9), (58.6, 22.1), (58.6, 5.0), (58.6, 29.0)]


def forklift_loops() -> list[tuple[list[str], tuple[str, ...]]]:
    """Forklift circuits that follow the one-way flow, with loading stops.

    West: A9 -> A5 -> B5 -> B9 -> A9 (serves inbound docks IN3/IN4 and aisle B).
    East: J5 -> J9 -> I9 -> aisle I south -> I5 -> J5 (serves outbound docks OUT3/OUT4
    and high racks in aisle I).
    """
    west = [grid_id(0, r) for r in range(9, 4, -1)] + [grid_id(1, r) for r in range(5, 10)]
    east = [grid_id(9, r) for r in range(5, 10)] + [grid_id(8, r) for r in range(9, 4, -1)]
    return [(west, (grid_id(0, 7), grid_id(1, 7))), (east, (grid_id(9, 7), grid_id(8, 7)))]


def external_loop() -> list[str]:
    """Cleaning robot circuit through aisles E and F with the one-way flow.

    F north, row 9 west, E south, row 1 east. Kept off the perimeter loop: a
    0.5 m/s unit on the main artery caps every robot queued behind it.
    """
    return ([grid_id(5, r) for r in range(1, 10)] + [grid_id(4, r) for r in range(9, 0, -1)])


__all__ = ["Human", "Forklift", "ExternalRobot", "worker_spots", "forklift_loops", "external_loop",
           "undirected_path", "COLUMN_X", "ROW_Y"]

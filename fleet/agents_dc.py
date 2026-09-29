"""People and manual equipment of the distribution centre.

- Picker: collaborative picking. Goes to the nearest tote AMR that waits at a
  pick face, works at it (the robot's pick action only counts down while a
  picker stands at it), then takes the next robot. Steps to the shelf face to
  let a moving robot through the 2 m aisle.
- Packer: stays at a pack station.
- Dock worker: moves between spots at the inbound or outbound docks.
- Walker: uses the south walkway and the x = 46 crossing.
- DockForklift: manual counterbalance truck. Inbound: truck -> floor staging.
  Outbound: floor staging -> truck. It creates and consumes the pallets the
  robots handle, and shares the staging spots with the autonomous forklifts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import random

from .agents import Human, Walker, undirected_path
from .geometry import Point, circle_hits_forward_box
from .layout import Layout
from .site_dc import (CROSSING_X, DOCK_X_IN, DOCK_X_OUT, DOCK_YS, IN_STAGE_X, OUT_STAGE_X, PICK_AISLES, WIDTH)

PICKER_KINDS = {"pickface", "cross", "queue"}
CROSS_Y = (8.9, 41.1)          # pickers change aisles at the shelf ends, inside the picking zone
AISLE_SIDE = 0.75              # pickers work and walk at the shelf face of a 2 m aisle
SERVE_DIST = 1.2               # a picker works at a robot standing this close (shared with the site)


def aisle_of(x: float) -> float | None:
    for ax in PICK_AISLES:
        if abs(x - ax) <= 1.1:
            return ax
    return None


def _pick_zone_node(layout: Layout, n: str) -> bool:
    node = layout.nodes[n]
    return 47.0 <= node.x <= 68.0


@dataclass
class DcHuman(Human):
    """Human with DC roles: picker | packer | dock_in | dock_out | walker."""
    spot_list: list[Point] = field(default_factory=list)
    serving: str | None = None          # robot this picker works at
    zone_aisles: tuple = ()             # zone picking: pick aisles this picker covers
    serve_until: float = 0.0
    served_count: int = 0
    site: object = None                 # DcProcess, for picker targets

    def plan_next(self, layout: Layout) -> None:
        self.distracted = self.rng.random() < 0.1
        self.detour_until_idx = -1
        if self.role == "picker":
            robots = self.site.waiting_robots() if self.site else {}
            if self.serving and self.serving in robots:
                r = robots[self.serving]            # blocked on the way: head for the same robot again
                node = layout.nearest_node((r["x"], r["y"]), ("pickface",))
                side = AISLE_SIDE if self.x >= layout.nodes[node].x else -AISLE_SIDE
                self._picker_walk_to(layout, node, (layout.nodes[node].x + side, layout.nodes[node].y))
            else:
                faces = [n for n in layout.nodes_of_kind("pickface")
                         if not self.zone_aisles or any(abs(layout.nodes[n].x - a) < 0.1 for a in self.zone_aisles)]
                self._picker_walk_to(layout, self.rng.choice(faces))
        elif self.role in ("packer", "dock_in", "dock_out"):
            self.walker = Walker([self.rng.choice(self.spot_list)])
        else:   # walker: along the south walkway, sometimes up the crossing and back
            pts: list[Point] = []
            if self.y > 2.6:
                # off the walkway (on the crossing): walk back down the crossing first, never across the floor
                pts.append((CROSSING_X + self.rng.uniform(-0.4, 0.4), 1.3))
            if self.rng.random() < 0.35:
                x = CROSSING_X + self.rng.uniform(-0.4, 0.4)
                pts += [(x, 1.3), (x, 47.0), (x, 1.3)]
            else:
                pts.append((self.rng.uniform(3.0, WIDTH - 3.0), self.rng.uniform(0.8, 1.9)))
            self.walker = Walker(pts)

    def _picker_walk_to(self, layout: Layout, target_node: str, target: Point | None = None) -> None:
        """Walk inside the picking zone: along the shelf face, changing aisles at the shelf ends."""
        tn = layout.nodes[target_node]
        side = AISLE_SIDE if (target is None and self.rng.random() < 0.5) or (target and target[0] > tn.x) else -AISLE_SIDE
        goal = target or (tn.x + side, tn.y)
        here_aisle = aisle_of(self.x)
        pts: list[Point] = []
        if here_aisle is not None and abs(here_aisle - tn.x) < 1e-6:
            if (self.x - here_aisle) * (goal[0] - here_aisle) < 0:     # switch shelf side in place
                pts.append((goal[0], self.y))
        else:
            end_y = min(CROSS_Y, key=lambda cy: abs(cy - self.y) + abs(cy - goal[1]))
            if here_aisle is not None:
                pts.append((self.x, end_y))
            pts.append((goal[0], end_y))
        pts.append(goal)
        self.walker = Walker(pts)

    def _at_shelf_face(self) -> bool:
        col = aisle_of(self.x)
        return (self.role == "picker" and col is not None and 9.6 < self.y < 40.4
                and abs(abs(self.x - col) - AISLE_SIDE) < 0.12)

    def _sidestep(self, layout: Layout, obstacle: Point, obstacle_r: float, heading: float) -> bool:
        if self.role == "picker" and aisle_of(self.x) is not None and 9.6 < self.y < 40.4:
            return False     # in a 2 m pick aisle the shelf face is already the side; wait for the robot
        return super()._sidestep(layout, obstacle, obstacle_r, heading)

    def _picker_yield(self, dt: float, now: float, layout: Layout, robots) -> bool:
        """Step to the shelf face when a robot comes through the aisle; cross the aisle only when clear."""
        col = aisle_of(self.x)
        if col is None or not (9.6 < self.y < 40.4):
            return False       # only inside the shelf segment; at the aisle ends pickers just cross
        tgt = self.walker.target()
        crossing = tgt is not None and (tgt[0] - col) * (self.x - col) < 0
        if crossing:
            robot_near = any(abs(rx - col) < 0.9 and abs(ry - self.y) < 3.0 for _i, rx, ry, _r, _v in robots)
            if robot_near and abs(abs(self.x - col) - AISLE_SIDE) < 0.12:
                self.v = 0.0
                return True    # wait at the shelf face until the aisle is clear, then cross
            return False       # already crossing: keep going
        near = [(rx, ry) for _rid, rx, ry, _rr, rv in robots if abs(rx - col) < 0.9 and abs(ry - self.y) < 3.0]
        if not near or abs(abs(self.x - col) - AISLE_SIDE) < 0.05:
            return False
        side_x = col + (AISLE_SIDE if self.x >= col else -AISLE_SIDE)
        step = min(abs(side_x - self.x), 1.0 * dt)
        self.x += step if side_x > self.x else -step
        self.v = 1.0
        return True

    def picker_update(self, now: float, layout: Layout, robots: dict) -> bool:
        """Collaborative picking. Returns True when the picker is busy at a robot this step."""
        if self.serving:
            r = robots.get(self.serving)
            if r is None or not r["waiting"] and now > self.serve_until:
                if r is not None:
                    self.served_count += 1
                self.serving = None
                self.site.release_picker_target(self.id)
                return False
            if self.walker.done() and math.hypot(r["x"] - self.x, r["y"] - self.y) < SERVE_DIST:
                self.v = 0.0
                self.serve_until = max(self.serve_until, now + 1.0)
                return True
            if self.walker.done():
                # arrived but the robot is not there (it moved on or stopped short): go to it again
                self.plan_next(layout)
            return False
        target = self.site.claim_picker_target(self.id, self.pos, self.zone_aisles)
        if target is not None:
            self.serving = target
            r = robots[target]
            node = layout.nearest_node((r["x"], r["y"]), ("pickface",))
            side = 0.75 if self.x >= layout.nodes[node].x else -0.75
            self._picker_walk_to(layout, node, (layout.nodes[node].x + side, layout.nodes[node].y))
        return False


@dataclass
class DockForklift:
    """Manual counterbalance forklift working between trucks and the floor staging spots."""
    id: str
    side: str                      # "in" | "out"
    rng: random.Random
    process: object
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0
    v: float = 0.0
    radius: float = 1.9
    speed: float = 1.39            # 5 km/h: dock and pedestrian areas
    state: str = "idle"            # idle | to_pick | picking | to_drop | dropping
    job: dict | None = None
    walker: Walker = field(default_factory=Walker)
    wait_until: float = 0.0
    blocked_since: float | None = None
    moves: int = 0

    def __post_init__(self) -> None:
        self.x = 1.3 if self.side == "in" else WIDTH - 1.4
        self.y = DOCK_YS[0] + 2.5

    @property
    def pos(self) -> Point:
        return (self.x, self.y)

    def circles(self) -> list[tuple[float, float, float]]:
        """3.6 m x 1.17 m counterbalance truck including forks."""
        c, s = math.cos(self.heading), math.sin(self.heading)
        return [(self.x + c * d, self.y + s * d, 0.6) for d in (-1.2, -0.4, 0.4, 1.2)]

    def _route(self, target: Point, corridor_x: float) -> None:
        pts = []
        if abs(target[1] - self.y) > 0.3:
            pts += [(corridor_x, self.y), (corridor_x, target[1])]
        pts.append(target)
        self.walker = Walker(pts)

    def step(self, dt: float, now: float, robots: list, humans: list) -> None:
        if now < self.wait_until:
            self.v = 0.0
            return
        corridor = 1.3 if self.side == "in" else WIDTH - 1.4
        if self.state == "idle":
            self.job = self.process.forklift_job(self.id, self.side)
            if self.job is None:
                self.v = 0.0
                self.wait_until = now + 3.0
                return
            self.state = "to_pick"
            self._route(self.job["pick"], corridor)
        if self.state in ("to_pick", "to_drop"):
            tgt = self.walker.target()
            if tgt is None:
                if self.state == "to_pick":
                    self.state, self.wait_until = "picking", now + self.rng.uniform(10, 16)
                else:
                    self.state, self.wait_until = "dropping", now + self.rng.uniform(7, 11)
                self.v = 0.0
                return
            heading = math.atan2(tgt[1] - self.y, tgt[0] - self.x)
            for h in humans:
                if circle_hits_forward_box(h.pos, h.radius, self.pos, heading, 2.8, 0.75, start=1.8):
                    self.v = 0.0
                    return
            for _rid, rx, ry, rr, _rv in robots:
                if circle_hits_forward_box((rx, ry), rr, self.pos, heading, 1.5, 0.75, start=1.8):
                    self.v = 0.0
                    return
            self.v = self.speed
            (self.x, self.y), h = self.walker.advance(self.pos, self.speed * dt)
            self.heading = h
            return
        if self.state == "picking":
            self.process.forklift_picked(self.id, self.job)
            self.state = "to_drop"
            self._route(self.job["drop"], corridor)
            return
        if self.state == "dropping":
            self.process.forklift_dropped(self.id, self.job, now)
            self.moves += 1
            self.state, self.job = "retreat", None
            self._route((corridor, self.y), corridor)
            return
        if self.state == "retreat":
            tgt = self.walker.target()
            if tgt is None:
                self.state = "idle"
                return
            self.v = self.speed * 0.6         # backing away from the staging spot
            (self.x, self.y), _h = self.walker.advance(self.pos, self.v * dt)


def dock_points(side: str, idx: int) -> tuple[Point, Point]:
    """(truck position, staging position) for dock ``idx`` on one side, forklift centre points."""
    y = DOCK_YS[idx]
    # a 3.6 m counterbalance truck works the pallet under the AGF forks from the wall side
    if side == "in":
        return (DOCK_X_IN - 1.5, y), (IN_STAGE_X - 1.35 - 0.2 - 1.8, y)
    return (DOCK_X_OUT + 1.5, y), (OUT_STAGE_X + 1.35 + 0.2 + 1.8, y)


__all__ = ["DcHuman", "DockForklift", "dock_points", "PICK_AISLES"]

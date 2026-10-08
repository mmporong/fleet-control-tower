"""Warehouse map: roadmap graph, racks, zones and infrastructure sensor coverage.

The roadmap is what the fleet manager plans and reserves on (VDA 5050 nodes and
edges). Racks are what blocks sight and movement. Zones carry operating rules
(speed limits, human work areas, forklift lanes) and can be locked by an operator.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math

from .geometry import Point, point_in_polygon, rect, segment_hits_polygon, segments_intersect

# sight line used to judge a blind intersection: points this far back along two approaches must see each other
BLIND_SIGHT_M = 3.0

COLUMN_X = (3.0, 9.0, 15.0, 21.0, 27.0, 33.0, 39.0, 45.0, 51.0, 57.0)
ROW_Y = (3.0, 6.5, 10.0, 13.5, 17.0, 20.5, 24.0, 27.5, 31.0)
COLUMN_NAMES = "ABCDEFGHIJ"
CROSS_ROWS = (1, 5, 9)          # 1-based rows that run the full width
SPINE_COLUMNS = (0, 9)          # indices of the two-way wall lanes
WIDTH_M = 60.0
HEIGHT_M = 34.0

SPEED_MAIN = 1.5
SPEED_AISLE = 1.2
SPEED_STUB = 0.6


@dataclass(frozen=True)
class Node:
    id: str
    x: float
    y: float
    kind: str  # spine | cross | queue | storage | inbound | outbound | charger | park

    @property
    def pos(self) -> Point:
        return (self.x, self.y)


@dataclass(frozen=True)
class Edge:
    id: str
    start: str
    end: str
    length: float
    max_speed: float
    lane: str        # undirected key shared by both directions of a two-way lane
    oneway: bool
    classes: frozenset = frozenset()   # vehicle classes allowed (VDA 5050 agvClass); empty = all


@dataclass
class Zone:
    id: str
    name: str
    kind: str        # human_work | walkway | picking | forklift_lane | charging | aisle
    polygon: list[Point]
    static_speed: float | None = None     # always applies to AMRs inside
    human_speed: float | None = None      # applies while a person is tracked inside
    locked: bool = False                  # operator lockdown: no new entry


@dataclass
class Layout:
    nodes: dict[str, Node]
    edges: dict[str, Edge]
    racks: list[list[Point]]
    zones: dict[str, Zone]
    coverage: dict[str, list[Point]]
    width: float = WIDTH_M
    height: float = HEIGHT_M
    out_edges: dict[str, list[Edge]] = field(default_factory=dict)
    in_edges: dict[str, list[Edge]] = field(default_factory=dict)
    neighbors: dict[str, set[str]] = field(default_factory=dict)
    node_zones: dict[str, list[str]] = field(default_factory=dict)
    two_way: bool = False
    site: str = "grid"
    doors: dict = field(default_factory=dict)         # id -> facility.DoorSpec
    walls: list = field(default_factory=list)         # [(x1, y1), (x2, y2)]: block sight, drawn on the map
    no_stop: set = field(default_factory=set)         # nodes a vehicle must not stop on (door thresholds)
    blind: set = field(default_factory=set)           # intersections whose approaches cannot see each other

    def __post_init__(self) -> None:
        self.reindex()

    def reindex(self) -> None:
        self.out_edges = {n: [] for n in self.nodes}
        self.in_edges = {n: [] for n in self.nodes}
        self.neighbors = {n: set() for n in self.nodes}
        for e in self.edges.values():
            self.out_edges[e.start].append(e)
            self.in_edges[e.end].append(e)
            self.neighbors[e.start].add(e.end)
            self.neighbors[e.end].add(e.start)
        self.node_zones = {
            n.id: [z.id for z in self.zones.values() if point_in_polygon(n.pos, z.polygon)]
            for n in self.nodes.values()
        }
        self.blind = self._blind_intersections()

    def _blind_intersections(self, sight: float = BLIND_SIGHT_M) -> set[str]:
        """Intersections where storage or a wall hides one approach from another.

        For every pair of lanes meeting at an intersection, the points ``sight`` metres back along each lane
        (or the lane's far end if shorter) must see each other. Racks and walls block the line.
        """
        out = set()
        for nid in self.nodes:
            if not self.is_intersection(nid):
                continue
            n = self.nodes[nid]
            probes = []
            for other in self.neighbors[nid]:
                o = self.nodes[other]
                d = math.hypot(o.x - n.x, o.y - n.y) or 1.0
                k = min(1.0, sight / d)
                probes.append((n.x + (o.x - n.x) * k, n.y + (o.y - n.y) * k))
            hidden = False
            for i, a in enumerate(probes):
                for b in probes[i + 1:]:
                    if any(segment_hits_polygon(a, b, r) for r in self.racks) or \
                            any(segments_intersect(a, b, w[0], w[1]) for w in self.walls):
                        hidden = True
                        break
                if hidden:
                    break
            if hidden:
                out.add(nid)
        return out

    # ---- lookups -------------------------------------------------------
    def edge_between(self, a: str, b: str) -> Edge:
        for e in self.out_edges[a]:
            if e.end == b:
                return e
        raise KeyError(f"no edge {a}->{b}")

    def nodes_of_kind(self, *kinds: str) -> list[str]:
        return sorted(n.id for n in self.nodes.values() if n.kind in kinds)

    def is_intersection(self, node_id: str) -> bool:
        return len(self.neighbors[node_id]) >= 3

    def must_pass(self, node_id: str) -> bool:
        """A vehicle may drive over this node but not wait on it."""
        return self.is_intersection(node_id) or node_id in self.no_stop

    def zones_at(self, p: Point) -> list[Zone]:
        return [z for z in self.zones.values() if point_in_polygon(p, z.polygon)]

    def static_speed_at(self, p: Point) -> float | None:
        limits = [z.static_speed for z in self.zones_at(p) if z.static_speed is not None]
        return min(limits) if limits else None

    def covered(self, p: Point) -> bool:
        return any(point_in_polygon(p, poly) for poly in self.coverage.values())

    def nearest_node(self, p: Point, kinds: tuple[str, ...] | None = None) -> str:
        best, best_d = "", math.inf
        for n in self.nodes.values():
            if kinds and n.kind not in kinds:
                continue
            d = math.hypot(n.x - p[0], n.y - p[1])
            if d < best_d:
                best, best_d = n.id, d
        return best

    # ---- validation ----------------------------------------------------
    def reachable_from(self, start: str) -> set[str]:
        seen = {start}
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            for e in self.out_edges[cur]:
                if e.end not in seen:
                    seen.add(e.end)
                    queue.append(e.end)
        return seen

    def allowed(self, e: Edge, vclass: str | None) -> bool:
        return not e.classes or vclass is None or vclass in e.classes

    def class_nodes(self, vclass: str) -> set[str]:
        return {n for e in self.edges.values() if self.allowed(e, vclass) for n in (e.start, e.end)}

    def reachable_from_class(self, start: str, vclass: str) -> set[str]:
        seen = {start}
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            for e in self.out_edges[cur]:
                if self.allowed(e, vclass) and e.end not in seen:
                    seen.add(e.end)
                    queue.append(e.end)
        return seen

    def validate_class(self, vclass: str) -> list[str]:
        """Every node a class may use must reach every other such node using only its edges."""
        nodes = self.class_nodes(vclass)
        if not nodes:
            return [f"no edges for {vclass}"]
        start = min(nodes)
        problems = []
        fwd = self.reachable_from_class(start, vclass)
        if nodes - fwd:
            problems.append(f"{vclass}: {len(nodes - fwd)} nodes unreachable from {start}: {sorted(nodes - fwd)[:5]}")
        for n in sorted(nodes):
            if start not in self.reachable_from_class(n, vclass):
                problems.append(f"{vclass}: {n} cannot return to {start}")
                break
        return problems

    def check(self) -> list[str]:
        """Everything a site must satisfy before vehicles run on it: the graph and each vehicle class."""
        classes = sorted({c for e in self.edges.values() for c in e.classes})
        return self.validate() + [p for c in classes for p in self.validate_class(c)]

    def validate(self) -> list[str]:
        """Return problems; an empty list means every node reaches every node."""
        problems: list[str] = []
        all_nodes = set(self.nodes)
        for e in self.edges.values():
            if e.start not in all_nodes or e.end not in all_nodes:
                problems.append(f"edge {e.id} references a missing node")
            if e.length <= 0:
                problems.append(f"edge {e.id} has non-positive length")
        start = next(iter(sorted(all_nodes)))
        forward = self.reachable_from(start)
        if forward != all_nodes:
            problems.append(f"{len(all_nodes - forward)} nodes unreachable from {start}")
        # reverse reachability: everything must be able to come back to start
        for n in sorted(all_nodes):
            if start not in self.reachable_from(n):
                problems.append(f"{n} cannot return to {start}")
                break
        return problems

    def to_json(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "two_way": self.two_way,
            "site": self.site,
            "nodes": [{"id": n.id, "x": n.x, "y": n.y, "kind": n.kind,
                       "intersection": self.is_intersection(n.id), "blind": n.id in self.blind}
                      for n in self.nodes.values()],
            "edges": [{"id": e.id, "s": e.start, "e": e.end, "lane": e.lane,
                       "oneway": e.oneway, "v": e.max_speed, "cls": sorted(e.classes)}
                      for e in self.edges.values()],
            "racks": self.racks,
            "zones": [{"id": z.id, "name": z.name, "kind": z.kind, "polygon": z.polygon,
                       "static_speed": z.static_speed, "human_speed": z.human_speed}
                      for z in self.zones.values()],
            "coverage": [{"id": k, "polygon": v} for k, v in self.coverage.items()],
            "doors": [d.to_json() for d in self.doors.values()],
            "walls": self.walls,
        }


def _lane_key(a: str, b: str) -> str:
    x, y = sorted((a, b))
    return f"{x}|{y}"


def grid_id(col: int, row: int) -> str:
    return f"{COLUMN_NAMES[col]}{row}"


ROW_DIRECTION = {1: +1, 5: +1, 9: -1}   # +1 eastbound, -1 westbound


def warehouse_layout(two_way: bool = False) -> Layout:
    """Build the reference warehouse: 60 m x 34 m, 8 rack aisles, 2 wall lanes.

    Default traffic design is one-way everywhere: columns alternate
    (A down, B up, C down, ... J up), so the wall lanes and rows 1 and 9 form a
    counter-clockwise loop, and row 5 runs east. Docks, chargers and parking
    spots are two-way dead-end stubs. ``two_way`` makes every lane two-way:
    single-lane, bidirectional traffic is the deadlock stress configuration.
    """
    nodes: dict[str, Node] = {}
    edges: dict[str, Edge] = {}

    for c, x in enumerate(COLUMN_X):
        for r, y in enumerate(ROW_Y, start=1):
            if c in SPINE_COLUMNS:
                kind = "spine"
            elif r in CROSS_ROWS:
                kind = "cross"
            else:
                kind = "storage"
            nid = grid_id(c, r)
            nodes[nid] = Node(nid, x, y, kind)

    def add_edge(a: str, b: str, speed: float, oneway: bool) -> None:
        na, nb = nodes[a], nodes[b]
        length = math.hypot(na.x - nb.x, na.y - nb.y)
        lane = _lane_key(a, b)
        edges[f"{a}>{b}"] = Edge(f"{a}>{b}", a, b, round(length, 3), speed, lane, oneway)
        if not oneway:
            edges[f"{b}>{a}"] = Edge(f"{b}>{a}", b, a, round(length, 3), speed, lane, oneway)

    def add(a: str, b: str, speed: float, oneway: bool, holding: bool = False) -> None:
        """Connect a -> b; on the main loop put a holding node halfway.

        Every node on the wall lanes and cross rows is an intersection, and a
        robot must not stop inside one. Without a holding position between
        intersections a robot would have to reserve the whole row at once.
        """
        if not holding:
            add_edge(a, b, speed, oneway)
            return
        na, nb = nodes[a], nodes[b]
        q = f"q{min(a, b)}{max(a, b)}"
        nodes[q] = Node(q, round((na.x + nb.x) / 2, 3), round((na.y + nb.y) / 2, 3), "queue")
        add_edge(a, q, speed, oneway)
        add_edge(q, b, speed, oneway)

    # vertical lanes: odd columns (B, D, F, H, J) run north, even ones south
    for c in range(len(COLUMN_X)):
        speed = SPEED_MAIN if c in SPINE_COLUMNS else SPEED_AISLE
        up = (c % 2 == 1)
        for r in range(1, len(ROW_Y)):
            a, b = grid_id(c, r), grid_id(c, r + 1)
            hold = c in SPINE_COLUMNS
            if two_way:
                add(a, b, speed, oneway=False, holding=hold)
            elif up:
                add(a, b, speed, oneway=True, holding=hold)
            else:
                add(b, a, speed, oneway=True, holding=hold)
    # cross rows
    for r in CROSS_ROWS:
        for c in range(len(COLUMN_X) - 1):
            a, b = grid_id(c, r), grid_id(c + 1, r)
            if two_way:
                add(a, b, SPEED_MAIN, oneway=False, holding=True)
            elif ROW_DIRECTION[r] > 0:
                add(a, b, SPEED_MAIN, oneway=True, holding=True)
            else:
                add(b, a, SPEED_MAIN, oneway=True, holding=True)

    def stub(nid: str, x: float, y: float, kind: str, attach: str) -> None:
        nodes[nid] = Node(nid, x, y, kind)
        add(attach, nid, SPEED_STUB, oneway=False)

    dock_rows = (2, 3, 7, 8)
    for i, r in enumerate(dock_rows, start=1):
        stub(f"IN{i}", 0.8, ROW_Y[r - 1], "inbound", grid_id(0, r))
        stub(f"OUT{i}", 59.2, ROW_Y[r - 1], "outbound", grid_id(9, r))
    for i, c in enumerate(range(2, 8), start=1):
        stub(f"CH{i}", COLUMN_X[c], 0.8, "charger", grid_id(c, 1))
    park = 1
    for c in range(len(COLUMN_X)):
        stub(f"P{park:02d}", COLUMN_X[c], 33.2, "park", grid_id(c, 9))
        park += 1
    for c in (0, 1, 8, 9):
        stub(f"P{park:02d}", COLUMN_X[c], 0.8, "park", grid_id(c, 1))
        park += 1
    for r in (2, 3, 4, 6, 7, 8):
        stub(f"P{park:02d}", 6.0, ROW_Y[r - 1], "park", grid_id(0, r))
        park += 1
    for r in (2, 3, 4, 6, 7, 8):
        stub(f"P{park:02d}", 54.0, ROW_Y[r - 1], "park", grid_id(9, r))
        park += 1

    racks: list[list[Point]] = []
    for c in range(1, 8):  # between B-C, C-D, ..., H-I
        x0, x1 = COLUMN_X[c] + 1.1, COLUMN_X[c + 1] - 1.1
        racks.append(rect(x0, 4.4, x1, 15.6))
        racks.append(rect(x0, 18.4, x1, 29.6))

    zones: dict[str, Zone] = {}

    def zone(zid: str, name: str, kind: str, poly: list[Point], static: float | None = None,
             human: float | None = None) -> None:
        zones[zid] = Zone(zid, name, kind, poly, static, human)

    # dock work areas sit between the wall and the wall lane; the lane itself is not slowed
    zone("Z-IN", "입고 도크 작업구역", "human_work", rect(0, 4.4, 2.2, 29.6), static=1.0, human=0.4)
    zone("Z-OUT", "출고 도크 작업구역", "human_work", rect(57.8, 4.4, 60, 29.6), static=1.0, human=0.4)
    # the walkway limit applies per segment, only near where people actually are
    zone("Z-WALK-W", "보행 교차 통로(서)", "walkway", rect(0, 15.8, 20.0, 18.2), human=0.6)
    zone("Z-WALK-C", "보행 교차 통로(중)", "walkway", rect(20.0, 15.8, 40.0, 18.2), human=0.6)
    zone("Z-WALK-E", "보행 교차 통로(동)", "walkway", rect(40.0, 15.8, 60, 18.2), human=0.6)
    zone("Z-PICK-D", "수동 피킹 통로 D", "picking", rect(19.9, 4.4, 22.1, 29.6), human=0.5)
    zone("Z-PICK-G", "수동 피킹 통로 G", "picking", rect(37.9, 4.4, 40.1, 29.6), human=0.5)
    zone("Z-FORK", "지게차 통로(상단)", "forklift_lane", rect(0, 29.6, 60, 32.4), static=1.2)
    zone("Z-CHG", "충전 구역", "charging", rect(12.0, 0, 48.0, 1.9))
    for c in range(1, 9):
        x = COLUMN_X[c]
        name = COLUMN_NAMES[c]
        zone(f"Z-{name}-S", f"{name} 통로 남측", "aisle", rect(x - 1.1, 4.4, x + 1.1, 15.6))
        zone(f"Z-{name}-N", f"{name} 통로 북측", "aisle", rect(x - 1.1, 18.4, x + 1.1, 29.6))

    coverage = {
        "CAM-IN": rect(0, 0, 9.0, HEIGHT_M),
        "CAM-OUT": rect(51.0, 0, WIDTH_M, HEIGHT_M),
        "CAM-ROW1": rect(0, 0, WIDTH_M, 4.4),
        "CAM-ROW5": rect(0, 15.6, WIDTH_M, 18.4),
        "CAM-ROW9": rect(0, 29.6, WIDTH_M, HEIGHT_M),
    }
    layout = Layout(nodes, edges, racks, zones, coverage)
    layout.two_way = two_way
    return layout

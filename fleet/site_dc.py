"""Pallet distribution centre: the default, industry-shaped site.

Flow (west to east): inbound docks and floor staging -> selective pallet racks
served by autonomous forklifts (AGF) -> collaborative picking zone served by
tote AMRs with human pickers -> packing -> outbound staging and docks.

Traffic design follows common practice for mixed fleets:
- two-lane main aisles (south and north) carry both directions,
- wall spines are one-way (west southbound, east northbound),
- rack aisles and pick aisles alternate direction,
- docks, staging spots, pack stations, chargers and parking bays are dead-end stubs,
- a holding node splits every lane segment of at least 4 m, so a robot can wait
  between intersections instead of inside one,
- pedestrians keep to the south walkway and cross the main aisles at x = 46;
  robots have no edges in pedestrian areas.

Dimensions are nominal values for a mid-size DC bay; the sources and the
choices behind them are in docs/SITE.md.
"""
from __future__ import annotations

import math

from .facility import DoorSpec, doors_crossing
from .geometry import Point, rect, seg_point_dist
from .layout import Edge, Layout, Node, Zone

AGF = "FORKLIFT"      # VDA 5050 agvClass of the autonomous forklift
AMR = "CARRIER"       # VDA 5050 agvClass of the tote-carrying AMR
BOTH = frozenset()

WIDTH, HEIGHT = 88.0, 53.5
S_EB, S_WB = 4.0, 6.2          # south main aisle lanes (eastbound, westbound)
N_EB, N_WB = 43.8, 46.0        # north main aisle lanes
BAY_LANE = 48.4                # westbound lane serving parking and charging bays
BAY_Y = 51.4
WEST_SPINE, TOTE_SPINE, AGF_SPINE = 10.0, 70.5, 79.0
# 4.0 m rack aisles: autonomous forklifts share them with 2.5 t counterbalance trucks
# (right-angle stacking aisle about 3.9-4.2 m); 2.6 m back-to-back rack between aisles
RACK_AISLES = (16.2, 22.8, 29.4, 36.0, 42.6)
PICK_AISLES = (50.0, 54.0, 58.0, 62.0, 66.0)     # 2.0 m aisle + 2.0 m shelving
# AGF stop positions along a rack aisle: 2.7 m chassis + protective field at standstill + margin
RACK_YS = tuple(round(10.0 + 3.8 * i, 1) for i in range(9))    # 10.0 .. 40.4
AISLE_YS = tuple(10.0 + 3.0 * i for i in range(11))           # pick faces every 3 m
DOCK_YS = (11.0, 16.0, 21.0, 26.0, 31.0, 36.0)   # 5 m door pitch (>= 4.27 m recommended)
PACK_YS = (12.0, 17.0, 22.0, 27.0, 32.0, 37.0)
# AGF centre when handling a staged pallet; the pallet sits under its forks toward the wall,
# and the manual forklift works the same pallet from the wall side
IN_STAGE_X, OUT_STAGE_X, PACK_X = 7.0, 82.0, 73.0
DOCK_X_IN, DOCK_X_OUT = 0.0, WIDTH
CROSSING_X = 47.0
# fire wall between the pallet hall and the pick/pack hall, pierced by two rapid-roll doors
# that only autonomous forklifts use (tote AMRs stay in the pick hall)
FIRE_WALL_X = 46.0
DOORS = (("D-S", "남측 고속문", 2.6, 7.4), ("D-N", "북측 고속문", 42.6, 47.2))
STAGE_LANE_CAPACITY = 4        # pallets a floor staging lane holds in front of each door

SPEED_MAIN, SPEED_AISLE, SPEED_SPINE, SPEED_STUB = 1.5, 1.0, 1.0, 0.5
HOLD_MIN_GAP = 4.0


def dc_layout() -> Layout:
    nodes: dict[str, Node] = {}
    edges: dict[str, Edge] = {}

    def node(nid: str, x: float, y: float, kind: str) -> str:
        if nid not in nodes:
            nodes[nid] = Node(nid, round(x, 3), round(y, 3), kind)
        return nid

    def edge(a: str, b: str, speed: float, classes=BOTH, oneway: bool = True) -> None:
        na, nb = nodes[a], nodes[b]
        length = round(math.hypot(na.x - nb.x, na.y - nb.y), 3)
        lane = "|".join(sorted((a, b)))
        edges[f"{a}>{b}"] = Edge(f"{a}>{b}", a, b, length, speed, lane, oneway, frozenset(classes))
        if not oneway:
            edges[f"{b}>{a}"] = Edge(f"{b}>{a}", b, a, length, speed, lane, oneway, frozenset(classes))

    def chain(ids: list[str], speed: float, classes=BOTH, hold: bool = True, seg_classes=None) -> None:
        """Connect consecutive nodes one-way, inserting holding nodes on long segments.

        No holding node is placed on a pedestrian crossing: robots must not wait there.
        """
        for a, b in zip(ids, ids[1:]):
            na, nb = nodes[a], nodes[b]
            if seg_classes is not None:
                classes = seg_classes(na.x, nb.x)
            mx = (na.x + nb.x) / 2
            # no waiting on a pedestrian crossing or where a parked vehicle would reach into a door opening
            on_crossing = abs(na.y - nb.y) < 0.1 and (abs(mx - CROSSING_X) < 1.4 or abs(mx - FIRE_WALL_X) < 2.5)
            if hold and not on_crossing and math.hypot(na.x - nb.x, na.y - nb.y) >= HOLD_MIN_GAP:
                q = node(f"q{a}{b}" if a < b else f"q{b}{a}", (na.x + nb.x) / 2, (na.y + nb.y) / 2, "queue")
                edge(a, q, speed, classes)
                edge(q, b, speed, classes)
            else:
                edge(a, b, speed, classes)

    def xid(x: float) -> str:
        return f"{x:g}".replace(".", "_")

    # ---- main aisles ------------------------------------------------------------
    junctions = sorted({WEST_SPINE, *RACK_AISLES, *PICK_AISLES, TOTE_SPINE, AGF_SPINE})
    lanes = {"SE": S_EB, "SW": S_WB, "NE": N_EB, "NW": N_WB}
    for name, y in lanes.items():
        for x in junctions:
            node(f"{name}{xid(x)}", x, y, "cross")
    # tote AMRs share the main aisles only along the picking zone; west and east ends are AGF-only
    def south_cls(x1: float, x2: float):
        return BOTH if min(x1, x2) >= PICK_AISLES[0] - 1e-6 and max(x1, x2) <= TOTE_SPINE + 1e-6 else {AGF}

    def north_cls(x1: float, x2: float):
        return BOTH if min(x1, x2) >= CROSSING_X - 1e-6 and max(x1, x2) <= TOTE_SPINE + 1e-6 else {AGF}

    for name in ("NE", "NW"):
        node(f"{name}{xid(CROSSING_X)}", CROSSING_X, lanes[name], "cross")
    north_x = sorted(set(junctions) | {CROSSING_X})
    chain([f"SE{xid(x)}" for x in junctions], SPEED_MAIN, seg_classes=south_cls)
    chain([f"SW{xid(x)}" for x in reversed(junctions)], SPEED_MAIN, seg_classes=south_cls)
    chain([f"NE{xid(x)}" for x in north_x], SPEED_MAIN, seg_classes=north_cls)
    chain([f"NW{xid(x)}" for x in reversed(north_x)], SPEED_MAIN, seg_classes=north_cls)
    spines = {WEST_SPINE, TOTE_SPINE, AGF_SPINE}
    for x in junctions:
        if x in spines:
            continue       # the one-way spine itself links its two lane nodes
        cls = {AGF} if x in RACK_AISLES else BOTH
        # lane change / crossing between the two lanes of each main aisle
        edge(f"SE{xid(x)}", f"SW{xid(x)}", SPEED_SPINE, cls, oneway=False)
        edge(f"NE{xid(x)}", f"NW{xid(x)}", SPEED_SPINE, cls, oneway=False)

    # ---- rack aisles (AGF) and pick aisles (tote AMR) ---------------------------
    def aisles(xs, ys, prefix: str, kind: str, cls: str) -> None:
        for i, x in enumerate(xs):
            ids = [node(f"{prefix}{i + 1}-{j + 1:02d}", x, y, kind) for j, y in enumerate(ys)]
            north = (i % 2 == 0)
            south_end, north_end = f"SW{xid(x)}", f"NE{xid(x)}"
            path = [south_end] + ids + [north_end] if north else [north_end] + ids[::-1] + [south_end]
            for a, b in zip(path, path[1:]):
                edge(a, b, SPEED_AISLE, {cls})

    aisles(RACK_AISLES, RACK_YS, "R", "rack", AGF)
    aisles(PICK_AISLES, AISLE_YS, "K", "pickface", AMR)

    # ---- spines with their stubs ------------------------------------------------
    def spine(x: float, name: str, ys_stub: tuple, stub_x: float, stub_kind: str, stub_prefix: str,
              cls: str, northbound: bool, bay: bool = True) -> None:
        stub_nodes = [node(f"{name}{int(y)}", x, y, "spine") for y in ys_stub]
        lower = [f"SE{xid(x)}", f"SW{xid(x)}"]
        upper = [f"NE{xid(x)}", f"NW{xid(x)}"] + ([node(f"BL{xid(x)}", x, BAY_LANE, "baylane")] if bay else [])
        seq = lower + stub_nodes + upper
        seq = seq if northbound else seq[::-1]
        chain(seq, SPEED_SPINE, {cls}, hold=True)
        for i, (sn, y) in enumerate(zip(stub_nodes, ys_stub)):
            st = node(f"{stub_prefix}{i + 1}", stub_x, y, stub_kind)
            edge(sn, st, SPEED_STUB, {cls}, oneway=False)

    spine(WEST_SPINE, "W", DOCK_YS, IN_STAGE_X, "in_stage", "SI", AGF, northbound=False)
    spine(AGF_SPINE, "E", DOCK_YS, OUT_STAGE_X, "out_stage", "SO", AGF, northbound=True, bay=False)
    spine(TOTE_SPINE, "T", PACK_YS, PACK_X, "pack", "PK", AMR, northbound=True)

    # ---- bay lanes: parking and charging, westbound, one on each side of the fire wall ------
    # AGF side: entered from the north main aisle at the last rack aisle, leaves down the west spine.
    # AMR side: entered from the top of the tote spine, leaves at the crossing.
    # 11.5 .. 39.5 at 2.8 m pitch (1.3 m wide AGFs, 1.5 m between parked trucks). The last bay stays
    # 3.1 m beside the entry, so a forklift driving in does not see a parked truck in its protective
    # field, and every bay is over 3.05 m from a turning node (the swept circle of a 2.7 m truck)
    agf_bays = [round(11.5 + 2.8 * i, 1) for i in range(11)]
    # 48.5 .. 68.5 at 2.0 m pitch (0.6 m wide tote AMRs): 11 bays plus 6 pack stations hold a 16-robot
    # shift when orders run dry, so idle robots are never left standing on a one-way lane
    amr_bays = [round(48.5 + 2.0 * i, 1) for i in range(11)]
    for points, cls in ((sorted({WEST_SPINE, *agf_bays, RACK_AISLES[-1]}, reverse=True), {AGF}),
                        (sorted({TOTE_SPINE, CROSSING_X, *amr_bays}, reverse=True), {AMR})):
        for x in points:
            node(f"BL{xid(x)}", x, BAY_LANE, "baylane")
        for a, b in zip(points, points[1:]):
            edge(f"BL{xid(a)}", f"BL{xid(b)}", SPEED_SPINE, cls)
    edge(f"NW{xid(RACK_AISLES[-1])}", f"BL{xid(RACK_AISLES[-1])}", SPEED_SPINE, {AGF})
    charger_agf = set(agf_bays[0:8:2])
    charger_amr = set(amr_bays[0:10:2])
    for x in agf_bays:
        kind = "charger" if x in charger_agf else "park"
        bid = node(f"{'CA' if kind == 'charger' else 'PA'}{xid(x)}", x, BAY_Y, kind)
        edge(f"BL{xid(x)}", bid, SPEED_STUB, {AGF}, oneway=False)
    for x in amr_bays:
        kind = "charger" if x in charger_amr else "park"
        bid = node(f"{'CT' if kind == 'charger' else 'PT'}{xid(x)}", x, BAY_Y, kind)
        edge(f"BL{xid(x)}", bid, SPEED_STUB, {AMR}, oneway=False)
    # tote AMRs leave the bay lane at the crossing: down across the westbound lane to the eastbound one
    edge(f"BL{xid(CROSSING_X)}", f"NW{xid(CROSSING_X)}", SPEED_SPINE, {AMR})
    edge(f"NW{xid(CROSSING_X)}", f"NE{xid(CROSSING_X)}", SPEED_SPINE, {AMR})

    racks = []
    # pick shelving stops short of the aisle ends so pickers can change aisles inside the zone
    shelf_y0, shelf_y1 = 9.5, 40.5
    for x in RACK_AISLES:
        racks.append(rect(x + 2.0, 8.6, x + 4.6, 41.4) if x != RACK_AISLES[-1] else rect(x + 2.0, 8.6, x + 3.2, 41.4))
    racks.append(rect(RACK_AISLES[0] - 3.2, 8.6, RACK_AISLES[0] - 2.0, 41.4))
    for i, x in enumerate(PICK_AISLES):
        right = x + 1.0
        racks.append(rect(right, shelf_y0, right + (2.0 if i < len(PICK_AISLES) - 1 else 1.0), shelf_y1))
    racks.append(rect(PICK_AISLES[0] - 2.0, shelf_y0, PICK_AISLES[0] - 1.0, shelf_y1))

    zones: dict[str, Zone] = {}

    def zone(zid: str, name: str, kind: str, poly: list[Point], static=None, human=None) -> None:
        zones[zid] = Zone(zid, name, kind, poly, static, human)

    zone("Z-IN", "입고 도크·스테이징", "human_work", rect(0, 8.0, 8.6, 39.0), static=0.8, human=0.3)
    zone("Z-OUT", "출고 스테이징·도크", "human_work", rect(80.6, 8.0, WIDTH, 39.0), static=0.8, human=0.3)
    zone("Z-PACK", "패킹 스테이션", "human_work", rect(71.8, 9.0, 76.0, 40.0), human=0.3)
    zone("Z-RACK", "팔레트 랙(자율·수동 지게차)", "forklift_lane", rect(12.0, 8.4, 46.0, 41.6), static=1.0, human=0.5)
    # people in the aisle: 0.3 m/s, the ISO 3691-4 speed that allows passing closer than the normal clearance
    zone("Z-PICK", "협업 피킹 존", "picking", rect(48.0, 8.4, 68.0, 41.6), static=1.0, human=0.3)
    # the crossings with a 2 m approach on each side: vehicles are at walking pace before they reach a
    # person on the crossing, not only once inside it (the south crossing starts right behind the door)
    zone("Z-CROSS-S", "횡단보도(남)·접근 구간", "walkway", rect(44.0, 2.6, 50.0, 7.4), human=0.3)
    zone("Z-CROSS-N", "횡단보도(북)·접근 구간", "walkway", rect(44.0, 42.6, 50.0, 47.2), human=0.3)
    zone("Z-WALK", "보행로(로봇 진입 없음)", "walkway", rect(0, 0, WIDTH, 2.6))
    zone("Z-BAY", "충전·주차", "charging", rect(10.0, 47.4, 80.0, HEIGHT))
    for i, x in enumerate(RACK_AISLES):
        zone(f"Z-R{i + 1}", f"랙 통로 R{i + 1}", "aisle", rect(x - 2.0, 8.6, x + 2.0, 41.4))
    for i, x in enumerate(PICK_AISLES):
        zone(f"Z-K{i + 1}", f"피킹 통로 K{i + 1}", "aisle", rect(x - 1.0, 8.6, x + 1.0, 41.4))

    coverage = {
        "CAM-IN": rect(0, 0, 12.0, HEIGHT),
        "CAM-OUT": rect(69.0, 0, WIDTH, HEIGHT),
        "CAM-S": rect(0, 0, WIDTH, 8.4),
        "CAM-N": rect(0, 41.6, WIDTH, HEIGHT),
        "CAM-X": rect(45.8, 0, 48.2, HEIGHT),
    }
    doors = doors_crossing([DoorSpec(did, name, (FIRE_WALL_X, y0), (FIRE_WALL_X, y1)) for did, name, y0, y1 in DOORS],
                           edges, nodes)
    walls = [[(FIRE_WALL_X, DOORS[0][3]), (FIRE_WALL_X, DOORS[1][2])], [(FIRE_WALL_X, DOORS[1][3]), (FIRE_WALL_X, HEIGHT)]]
    # a vehicle parked this close to a threshold would hold the door open or stand in the curtain
    no_stop = {n.id for n in nodes.values() for d in doors.values()
               if seg_point_dist(n.pos, d.a, d.b) < 1.9}
    layout = Layout(nodes, edges, racks, zones, coverage, width=WIDTH, height=HEIGHT, doors=doors, walls=walls,
                    no_stop=no_stop)
    layout.site = "dc"
    return layout


def dock_spots() -> dict[str, list[Point]]:
    """Where dock workers and packers stand."""
    return {
        # between two staging lanes, off the dock forklifts' travel corridor along the wall
        "in": [(3.0, y + 2.5) for y in DOCK_YS],
        "out": [(WIDTH - 3.0, y + 2.5) for y in DOCK_YS],
        "pack": [(74.6, y) for y in PACK_YS],
    }

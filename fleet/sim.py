"""Simulation world: vehicles, people, forklifts, an external robot and the evaluator.

The fleet manager is driven only through the bus and the infrastructure sensor
feeds. The evaluator reads ground truth and records what really happened
(contacts, near misses, people the manager was not tracking), so the control
room view and reality can be compared.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import math
import random

from .agents import ExternalRobot, Forklift, Human, expand_loop, external_loop, forklift_loops, worker_spots
from .agents_dc import DcHuman, DockForklift
from .process import GridProcess
from .process_dc import DcProcess
from .site_dc import AGF, AMR, PICK_AISLES, dc_layout, dock_spots
from .bus import Bus
from .control import FleetManager, ManagerConfig, clearance_violation, close_call, envelope_intrusion
from .dispatcher import Dispatcher
from .facility import DoorDevice
from .geometry import box_gap, rect
from .layout import COLUMN_X, ROW_Y, warehouse_layout
from .perception import Detection
from .scenario import Scenario
from .vehicle import FAULT_TYPES, SPEC_GRID, Obstacle, Vehicle, VehicleSpec, agf_spec, tote_amr_spec
from .vda5050 import topic

FRAME_PERIOD = 0.5
# besides every priority-1 alarm, these open an incident record with the 30 s before and 10 s after
INCIDENT_TYPES = {"NEAR_MISS", "RESERVATION_MISMATCH", "DEADLOCK_UNRESOLVED"}
FRAMES_BEFORE = 60      # 30 s before an incident
FRAMES_AFTER = 20       # 10 s after


@dataclass
class Incident:
    id: str
    kind: str
    source: str
    priority: int
    message: str
    opened_at: float
    frames: list = field(default_factory=list)
    frames_after_left: int = FRAMES_AFTER
    acked_at: float | None = None
    cleared_at: float | None = None
    closed_at: float | None = None
    note: str = ""
    truth: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"id": self.id, "kind": self.kind, "source": self.source, "priority": self.priority,
                "message": self.message, "opened_at": round(self.opened_at, 1),
                "acked_at": None if self.acked_at is None else round(self.acked_at, 1),
                "cleared_at": None if self.cleared_at is None else round(self.cleared_at, 1),
                "closed_at": None if self.closed_at is None else round(self.closed_at, 1),
                "frames": len(self.frames), "recording": self.frames_after_left > 0,
                "note": self.note, "truth": self.truth}


class _ObstacleGrid:
    """The step's obstacles bucketed in cells, so each vehicle gets only those its fields can reach.

    ``Vehicle._fields`` ignores anything more than 5 m away along either axis; ``around`` returns every
    obstacle within that box plus a margin for the vehicle's own motion in the step, in the original list
    order (the first obstacle a field finds names its cause), so the result is the same as the full list.
    """

    REACH_M = 5.5
    CELL_M = 5.5

    def __init__(self, obstacles: list[Obstacle]) -> None:
        self.obstacles = obstacles
        self.cells: dict[tuple[int, int], list[int]] = {}
        c = self.CELL_M
        for i, o in enumerate(obstacles):
            self.cells.setdefault((int(o.x // c), int(o.y // c)), []).append(i)

    def around(self, x: float, y: float) -> list[Obstacle]:
        c, r = self.CELL_M, self.REACH_M
        idx: list[int] = []
        for cx in range(int((x - r) // c), int((x + r) // c) + 1):
            for cy in range(int((y - r) // c), int((y + r) // c) + 1):
                idx.extend(self.cells.get((cx, cy), ()))
        idx.sort()
        return [self.obstacles[i] for i in idx]


class Evaluator:
    """Ground-truth safety and awareness metrics (never fed back to the manager)."""

    def __init__(self) -> None:
        self.contacts: list[dict] = []
        self.clearance_violations = 0       # compliance: above creep speed inside the 0.5 m clearance
        self.close_calls = 0                # proximity: moving and within 20 cm of a person
        self.near_misses = 0                # outcome: a person inside the moving vehicle's stopping envelope
        self.near_miss_log: deque = deque(maxlen=200)
        self._pair_contact: set[tuple[str, str]] = set()
        self._pair_nm: dict[tuple[str, str, str], float] = {}
        self.min_gap_moving = math.inf      # closest a moving vehicle came to a person
        self.min_amr_gap = math.inf
        self.tracked_samples = 0
        self.human_samples = 0
        self.blind_samples = 0

    def check(self, now: float, vehicles: list[Vehicle], humans: list[Human], forklifts: list[Forklift],
              externals: list[ExternalRobot]) -> list[dict]:
        new_contacts = []
        others = [(h.id, "human", [(h.x, h.y, h.radius)], abs(h.v) > 0.05) for h in humans]
        others += [(f.id, "forklift", f.circles(), abs(f.v) > 0.05 or getattr(f, "turning", False))
                   for f in forklifts]
        others += [(e.id, "external", [(e.x, e.y, e.radius)], abs(e.v) > 0.05) for e in externals]
        for i, v in enumerate(vehicles):
            vc = v.spec.circles(v.x, v.y, v.theta)
            for w in vehicles[i + 1:]:
                if abs(v.x - w.x) > 4 or abs(v.y - w.y) > 4:
                    self._pair_contact.discard((v.serial, w.serial))
                    continue
                wc = w.spec.circles(w.x, w.y, w.theta)
                gap = min(math.hypot(a[0] - b[0], a[1] - b[1]) - a[2] - b[2] for a in vc for b in wc)
                self.min_amr_gap = min(self.min_amr_gap, gap)
                new_contacts += self._contact(now, v, w.serial, "amr", gap, (w.x, w.y),
                                              abs(w.v) > 0.05 or abs(w.omega) > 0.05)
            for oid, kind, circles, other_moving in others:
                ox, oy = circles[len(circles) // 2][:2]
                if abs(v.x - ox) > 3.5 or abs(v.y - oy) > 3.5:
                    self._pair_contact.discard((v.serial, oid))
                    continue
                gap = min(box_gap((cx, cy), cr, (v.x, v.y), v.theta, v.spec.length / 2, v.spec.width / 2)
                          for cx, cy, cr in circles)
                new_contacts += self._contact(now, v, oid, kind, gap, (ox, oy), other_moving)
                if kind == "human":
                    speed = abs(v.v)
                    if speed > 0.05:
                        self.min_gap_moving = min(self.min_gap_moving, gap)
                    for what, hit in (("clearance", clearance_violation(speed, gap)), ("close", close_call(speed, gap)),
                                      ("envelope", envelope_intrusion(speed, gap, v.spec.stop_decel))):
                        if not hit:
                            continue
                        key = (v.serial, oid, what)
                        if now - self._pair_nm.get(key, -1e9) > 10.0:     # one event per pair per 10 s
                            if what == "clearance":
                                self.clearance_violations += 1
                            elif what == "close":
                                self.close_calls += 1
                            else:
                                self.near_misses += 1
                            self.near_miss_log.append({"t": round(now, 1), "robot": v.serial, "human": oid,
                                                       "kind": what, "gap": round(max(gap, 0.0), 2),
                                                       "v": round(v.v, 2)})
                        self._pair_nm[key] = now
        return new_contacts

    def _contact(self, now: float, v: Vehicle, other: str, kind: str, gap: float, where,
                 other_moving: bool = False) -> list[dict]:
        key = (v.serial, other)
        if gap > 0.05:
            if gap > 0.2:
                self._pair_contact.discard(key)
            return []
        if key in self._pair_contact:
            return []
        self._pair_contact.add(key)
        moving = abs(v.v) > 0.05 or abs(v.omega) > 0.05
        # "moving": our vehicle was moving; "struck": it stood still and the other party moved into it
        rec = {"t": round(now, 1), "robot": v.serial, "other": other, "kind": kind, "moving": moving,
               "other_moving": other_moving, "x": round(v.x, 2), "y": round(v.y, 2), "speed": round(v.v, 2)}
        self.contacts.append(rec)
        return [rec]

    def awareness(self, now: float, humans: list[Human], tracks) -> tuple[int, int]:
        tracked = 0
        for h in humans:
            if any(tr.cls == "human" and tr.confirmed and math.hypot(tr.x - h.x, tr.y - h.y) < 1.0 for tr in tracks):
                tracked += 1
        self.human_samples += len(humans)
        self.tracked_samples += tracked
        return tracked, len(humans)


class Simulation:
    def __init__(self, scenario: Scenario, seed: int = 7, strategy: str = "locks+resolve",
                 dt: float = 0.1, config: ManagerConfig | None = None,
                 vehicle_overrides: dict | None = None) -> None:
        # benchmark knobs (e.g. field_gain) applied on top of each vehicle class's own specification
        self.vehicle_overrides = dict(vehicle_overrides or {})
        self.scenario = scenario
        self.seed = seed
        self.dt = dt
        self.now = 0.0
        self.steps = 0
        self.layout = dc_layout(scenario.pick_limit) if scenario.site == "dc" else warehouse_layout(scenario.two_way)
        if scenario.infra == "none":
            self.layout.coverage = {}
        elif scenario.infra == "full":
            self.layout.coverage = {"CAM-ALL": rect(0.0, 0.0, self.layout.width, self.layout.height)}
        elif scenario.infra != "partial":
            raise ValueError("infra must be none, partial or full")
        self.rng_faults = random.Random(seed * 7 + 3)
        self.rng_sense = random.Random(seed * 7 + 4)
        self.rng_doors = random.Random(seed * 7 + 8)
        self.bus = Bus(random.Random(seed * 7 + 1))
        cfg = config or ManagerConfig(strategy=strategy)
        cfg.strategy = strategy
        self.manager = FleetManager(self.layout, self.bus, cfg)
        self.doors = {d.id: DoorDevice(d, self.bus) for d in self.layout.doors.values()}
        self.dispatcher = Dispatcher(self.manager, random.Random(seed * 7 + 2), policy=scenario.dispatch)
        prng = random.Random(seed * 7 + 6)
        if scenario.site == "dc":
            self.process = DcProcess(self.dispatcher, prng, trucks_h=scenario.trucks_h,
                                     out_pallets_h=scenario.out_pallets_h, tote_orders_h=scenario.tote_orders_h,
                                     tote_batch=scenario.tote_batch, handling_fail_p=scenario.handling_fail_p,
                                     inventory_error_p=scenario.inventory_error_p,
                                     truth_rng=random.Random(seed * 7 + 9))
        else:
            self.process = GridProcess(self.dispatcher, prng, scenario.rate_per_h)
        self.dispatcher.process = self.process
        self.vehicles: list[Vehicle] = []
        self.humans: list[Human] = []
        self.forklifts: list[Forklift] = []
        self.externals: list[ExternalRobot] = []
        self.evaluator = Evaluator()
        self.frames: deque = deque(maxlen=FRAMES_BEFORE)
        self.incidents: list[Incident] = []
        self._inc_seq = 0
        self._next_frame = 0.0
        self._next_sense = 0.0
        self._next_tick = 0.0
        self.tick_hooks: list = []       # called after each manager tick, e.g. the simulated operator
        self._cam_queue: deque = deque()  # camera frames on their way to the control room
        self.window_start = 0.0           # measurement window (reset_measurement after a warm-up)
        self.time_budget: dict[str, dict[str, float]] = {}
        self.task_time: dict[str, float] = {}
        self.class_time: dict[str, float] = {}
        self._odo0: dict[str, float] = {}
        self._forklift_careless0 = 0
        self.handovers: list[dict] = []
        self.manager.alarms.listeners.append(self._on_alarm)
        self._build(random.Random(seed * 7 + 5))

    # ------------------------------------------------------------ setup
    def _add_vehicle(self, serial: str, node: str, spec: VehicleSpec | None, rng: random.Random, idx: int) -> None:
        sc = self.scenario
        v = Vehicle(serial, self.layout, node, random.Random(self.seed * 1000 + idx), battery=rng.uniform(55, 98),
                    spec=spec)
        # parked the way the site parks vehicles: facing the way out of the spot
        L = self.layout
        out = [e for e in L.out_edges[node] if L.allowed(e, v.spec.vclass)] or L.out_edges[node]
        rng.choice((0, 1, 2, 3))          # same draw as the old random heading: later draws stay comparable
        v.theta = math.atan2(L.nodes[out[0].end].y - L.nodes[node].y, L.nodes[out[0].end].x - L.nodes[node].x)
        if isinstance(self.process, DcProcess):
            v.station_check = self.process.physical_check     # the load sensors meet the site's real inventory
        self.vehicles.append(v)
        self.manager.register(serial)
        self.bus.subscribe(topic("sim", serial, "order"), lambda t, m, v=v: v.on_order(m))
        self.bus.subscribe(topic("sim", serial, "instantActions"), lambda t, m, v=v: v.on_instant_actions(m, self.now))
        self.bus.link(serial).loss = sc.comms_loss
        self.bus.publish(topic("sim", serial, "connection"), v.connection_msg(0.0), 0.0)

    def _spec(self, base: VehicleSpec) -> VehicleSpec:
        return replace(base, **self.vehicle_overrides)      # always a copy: vehicles never share a spec

    def _class_spots(self, vclass: str, kinds: tuple[str, ...]) -> list[str]:
        L = self.layout
        return [n for n in L.nodes if L.nodes[n].kind in kinds
                and any(L.allowed(e, vclass) and (L.nodes[e.start].kind not in kinds or e.end == n)
                        for e in L.in_edges[n])]

    def _build(self, rng: random.Random) -> None:
        if self.scenario.site == "dc":
            self._build_dc(rng)
        else:
            self._build_grid(rng)
        # rule-breaking rates apply from each person's next trip on
        for h in self.humans:
            h.violation_p = self.scenario.violation_p
        for f in self.forklifts:
            if isinstance(f, Forklift):
                f.careless_p = self.scenario.careless_p

    def _build_dc(self, rng: random.Random) -> None:
        sc = self.scenario
        L = self.layout
        agf_q = [n for n in L.nodes_of_kind("queue")
                 if any(L.allowed(e, AGF) and e.classes for e in L.out_edges[n])]
        agf_starts = self._class_spots(AGF, ("park", "charger")) + agf_q
        lane_q = [n for n in L.nodes_of_kind("queue")
                  if any(L.allowed(e, AMR) for e in L.out_edges[n]) and L.nodes[n].x > 46.0]
        amr_starts = self._class_spots(AMR, ("park", "charger")) + L.nodes_of_kind("pack") + lane_q
        for i in range(sc.agf):
            self._add_vehicle(f"AGF-{i + 1:02d}", agf_starts[i], self._spec(agf_spec()), rng, i)
        for i in range(sc.amr):
            self._add_vehicle(f"AMR-{i + 1:02d}", amr_starts[i], self._spec(tote_amr_spec()), rng, 100 + i)
        spots = dock_spots()
        hid = 1

        def human(prefix: str, role: str, pos, spot_list, speed=1.2) -> None:
            nonlocal hid
            h = DcHuman(f"{prefix}{hid:02d}", role, pos[0], pos[1], random.Random(self.seed * 50 + hid),
                        speed=speed, spot_list=spot_list, site=self.process)
            h.dwell_until = rng.uniform(0, 20)
            h.plan_next(L)
            self.humans.append(h)
            hid += 1

        n_aisles = len(PICK_AISLES)
        for k in range(sc.pickers):
            # zone picking: aisles split between pickers (several pickers share an aisle when there are more)
            own = tuple(PICK_AISLES[i] for i in range(n_aisles) if i % max(1, sc.pickers) == k % n_aisles
                        or (sc.pickers < n_aisles and i % sc.pickers == k)) or (PICK_AISLES[k % n_aisles],)
            x = own[0] + 0.75
            human("K", "picker", (x, rng.choice([13.0, 22.0, 31.0])), [], speed=1.1)   # walking with a cart
            self.humans[-1].zone_aisles = own
        for k in range(sc.packers):
            human("PK", "packer", spots["pack"][k % len(spots["pack"])], spots["pack"], speed=0.8)
        for k in range(sc.dock_workers):
            side = "in" if k % 3 != 2 else "out"
            human("W", f"dock_{side}", rng.choice(spots[side]), spots[side])
        for k in range(sc.walkers):
            human("P", "walker", (rng.uniform(5, 79), 1.3), [], speed=1.31)   # mean free walking speed
        for k in range(sc.dock_forklifts_in):
            self.forklifts.append(DockForklift(f"FL-IN{k + 1}", "in", random.Random(self.seed * 60 + k), self.process,
                                               home=k))
        for k in range(sc.dock_forklifts_out):
            self.forklifts.append(DockForklift(f"FL-OUT{k + 1}", "out", random.Random(self.seed * 61 + k),
                                               self.process, home=k))
        lane_kinds = {"cross", "queue", "spine", "rack"}
        if sc.rack_forklift:
            # manual reach truck working rack aisles R1 and R2 with the one-way flow (R1 north, north main
            # aisle east, R2 south, south main aisle west); it stays off the AGF spine and the bay lane
            loop = expand_loop(L, ["SW16_2", "R1-01", "R1-09", "NE16_2", "NE22_8", "R2-09", "R2-01", "SW22_8"],
                               lane_kinds)
            self.forklifts.append(Forklift("FL-R1", loop, L, random.Random(self.seed * 62),
                                           dwell_nodes=("R1-05", "R2-05")))
        for k in range(sc.external):
            # floor scrubber of another vendor: south main aisle along the rack block (AGF-only segments)
            loop = expand_loop(L, ["SE10", "SE42_6", "SW42_6", "SW10"], {"cross", "queue"})
            self.externals.append(ExternalRobot(f"EXT-SCR-{k + 1}", loop, L, speed=0.6))

    def _build_grid(self, rng: random.Random) -> None:
        sc = self.scenario
        starts = self.layout.nodes_of_kind("park") + self.layout.nodes_of_kind("charger")
        extra = [n for n in self.layout.nodes_of_kind("storage")]
        rng.shuffle(extra)
        starts = starts + extra
        for i in range(sc.robots):
            serial = f"AMR-{i + 1:02d}"
            v = Vehicle(serial, self.layout, starts[i], random.Random(self.seed * 1000 + i),
                        battery=rng.uniform(55, 98),
                        spec=self._spec(SPEC_GRID))
            v.theta = rng.choice((0.0, math.pi / 2, math.pi, -math.pi / 2))
            self.vehicles.append(v)
            self.manager.register(serial)
            self.bus.subscribe(topic("sim", serial, "order"), lambda t, m, v=v: v.on_order(m))
            self.bus.subscribe(topic("sim", serial, "instantActions"),
                               lambda t, m, v=v: v.on_instant_actions(m, self.now))
            self.bus.link(serial).loss = sc.comms_loss
            self.bus.publish(topic("sim", serial, "connection"), v.connection_msg(0.0), 0.0)
        hid = 1
        for k in range(sc.workers):
            side = "in" if k % 2 == 0 else "out"
            spots = worker_spots(side)
            x, y = rng.choice(spots)
            h = Human(f"W{hid:02d}", "worker", x, y, random.Random(self.seed * 50 + hid), spots=spots)
            h.dwell_until = rng.uniform(0, 30)
            h.plan_next(self.layout)
            self.humans.append(h)
            hid += 1
        for k in range(sc.pickers):
            col = (3, 6)[k % 2]
            x = COLUMN_X[col]
            y = ROW_Y[rng.choice((1, 2, 3, 5, 6, 7))]
            h = Human(f"K{hid:02d}", "picker", x + 0.55, y, random.Random(self.seed * 50 + hid), speed=1.0,
                      aisle_col=col)
            h.plan_next(self.layout)
            self.humans.append(h)
            hid += 1
        for k in range(sc.walkers):
            x = COLUMN_X[0] if k % 2 == 0 else COLUMN_X[9]
            h = Human(f"P{hid:02d}", "walker", x, ROW_Y[4], random.Random(self.seed * 50 + hid), speed=1.3)
            h.dwell_until = rng.uniform(0, 40)
            h.plan_next(self.layout)
            self.humans.append(h)
            hid += 1
        loops = forklift_loops()
        for k in range(sc.forklifts):
            loop, dwell = loops[k % len(loops)]
            self.forklifts.append(Forklift(f"FL-{k + 1}", expand_loop(self.layout, loop), self.layout,
                                           random.Random(self.seed * 60 + k), dwell_nodes=dwell))
        for k in range(sc.external):
            self.externals.append(ExternalRobot(f"EXT-CLN-{k + 1}", expand_loop(self.layout, external_loop()),
                                                self.layout))

    # ------------------------------------------------------------ incidents
    def _on_alarm(self, event: str, alarm) -> None:
        if event in ("raised", "reactivated") and (alarm.priority == 1 or alarm.type in INCIDENT_TYPES):
            self._inc_seq += 1
            inc = Incident(f"INC-{self._inc_seq:03d}", alarm.type, alarm.source, alarm.priority,
                           alarm.message, self.now, frames=list(self.frames))
            contact = [c for c in self.evaluator.contacts if abs(c["t"] - self.now) < 1.0
                       and alarm.source.startswith(c["robot"])]
            if contact:
                inc.truth = contact[-1]
            self.incidents.append(inc)
            if len(self.incidents) > 40:
                self.incidents.pop(0)
        for inc in self.incidents:
            if inc.closed_at is not None or not alarm.key.startswith(inc.kind) or inc.source != alarm.source:
                continue
            if event == "closed":
                inc.cleared_at = inc.cleared_at or self.now
                inc.acked_at = inc.acked_at or self.now
            elif event == "rtn":
                inc.cleared_at = self.now

    def ack_incident(self, key: str) -> None:
        for inc in self.incidents:
            if f"{inc.kind}:{inc.source}" == key and inc.acked_at is None:
                inc.acked_at = self.now

    def _frame(self) -> dict:
        return {
            "t": round(self.now, 1),
            "r": [[v.serial, round(v.x, 2), round(v.y, 2), round(v.theta, 2),
                   self.manager.robots[v.serial].status, v.spec.vclass] for v in self.vehicles],
            "h": [[h.id, round(h.x, 2), round(h.y, 2)] for h in self.humans],
            "f": [[f.id, round(f.x, 2), round(f.y, 2), round(f.heading, 2)] for f in self.forklifts],
            "e": [[e.id, round(e.x, 2), round(e.y, 2)] for e in self.externals],
            "k": [[tr.id, tr.cls, round(tr.x, 2), round(tr.y, 2)] for tr in self.manager.tracker.tracks.values()],
            "d": [[d.spec.id, round(d.progress, 2)] for d in self.doors.values()],
        }

    # ------------------------------------------------------------ step
    def step(self) -> None:
        dt = self.dt
        self.now = round(self.now + dt, 6)
        now = self.now
        self.steps += 1
        bodies = {v.serial: v.spec.circles(v.x, v.y, v.theta) for v in self.vehicles}
        robots_obs = [(v.serial, cx, cy, cr, v.v) for v in self.vehicles for cx, cy, cr in bodies[v.serial]]
        pickers = [h for h in self.humans if h.role == "picker" and isinstance(h, DcHuman)]
        if pickers or isinstance(self.process, DcProcess):
            self.process.update_pickers(self.vehicles, pickers, now, dt)
        for h in self.humans:
            if isinstance(h, DcHuman) and h.role == "picker" and h.picker_update(now, self.layout,
                                                                                self.process.waiting_robots()):
                continue
            h.step(dt, now, self.layout, robots_obs, self.forklifts)
        for f in self.forklifts:
            f.step(dt, now, robots_obs, self.humans)
        ext_obs = [(v.serial, cx, cy, cr) for v in self.vehicles for cx, cy, cr in bodies[v.serial]] + \
                  [(h.id, h.x, h.y, h.radius) for h in self.humans] + \
                  [(f.id, cx, cy, cr) for f in self.forklifts for cx, cy, cr in f.circles()]
        for e in self.externals:
            e.step(dt, ext_obs, now)
        obstacles = [Obstacle(v.serial, "amr", cx, cy, cr) for v in self.vehicles for cx, cy, cr in bodies[v.serial]]
        obstacles += [Obstacle(h.id, "human", h.x, h.y, h.radius) for h in self.humans]
        obstacles += [Obstacle(f.id, "forklift", cx, cy, cr) for f in self.forklifts for cx, cy, cr in f.circles()]
        obstacles += [Obstacle(e.id, "external", e.x, e.y, e.radius) for e in self.externals]
        if self.doors:
            near = [(cx, cy, cr) for v in self.vehicles for cx, cy, cr in bodies[v.serial]] + \
                   [(h.x, h.y, h.radius) for h in self.humans] + \
                   [c for f in self.forklifts for c in f.circles()]
            for d in self.doors.values():
                d.step(dt, now, near)
                obstacles += [Obstacle(d.spec.id, "door", x, y, r) for x, y, r in d.obstacles()]
        nearby = _ObstacleGrid(obstacles)
        for v in self.vehicles:
            v.step(dt, now, nearby.around(v.x, v.y))
        for c in self.evaluator.check(now, self.vehicles, self.humans, self.forklifts, self.externals):
            # any touch with something in motion presses the bumper (also a truck swinging into a standing AGF)
            if c["moving"] or c["other_moving"]:
                for v in self.vehicles:
                    if v.serial in (c["robot"], c["other"]) and v.fault is None:
                        v.set_fault("BUMPER")
        if now >= self._next_sense:
            self._next_sense = now + 0.5
            for v in self.vehicles:
                v.sense(obstacles, self.layout.racks)
            if self.scenario.infra == "none":
                self.manager.ingest_detections([], infra=False)      # only what the vehicles saw
            else:
                # a camera frame is captured now and reaches the control room after the processing latency
                self._cam_queue.append((now + self.scenario.cam_latency_s, self._infra_detections()))
            for e in self.externals:
                self.manager.ingest_external(e.id, e.x, e.y, e.heading, e.itinerary(), e.speed)
            self.evaluator.awareness(now, self.humans, self.manager.tracker.tracks.values())
        while self._cam_queue and self._cam_queue[0][0] <= now + 1e-9:
            self.manager.ingest_detections(self._cam_queue.popleft()[1])
        self._inject(dt, now)
        for v in self.vehicles:
            for t, msg in v.publish_due(now):
                self.bus.publish(t, msg, now)
        for d in self.doors.values():
            d.publish_due(now)
        self.bus.deliver_due(now)
        if now >= self._next_tick - 1e-9:
            self._next_tick = now + 0.2
            self.manager.tick(now, 0.2)
            self.dispatcher.tick(now)
            self._account(0.2)
            for hook in self.tick_hooks:
                hook(now)
        if now >= self._next_frame:
            self._next_frame = now + FRAME_PERIOD
            frame = self._frame()
            self.frames.append(frame)
            for inc in self.incidents:
                if inc.frames_after_left > 0 and frame["t"] > inc.opened_at:
                    inc.frames.append(frame)
                    inc.frames_after_left -= 1

    def run(self, seconds: float) -> None:
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.step()

    def _infra_detections(self) -> list[Detection]:
        """One frame of the ceiling cameras' person/truck detector (positions at capture time).

        Missed detections, position noise, wrong classes and false detections follow the scenario's camera
        parameters; the tracker only acts on a track seen twice.
        """
        sc, rng = self.scenario, self.rng_sense
        dets = []
        for cls, objs, sigma in (("human", self.humans, sc.cam_sigma), ("forklift", self.forklifts, sc.cam_sigma)):
            for o in objs:
                if self.layout.covered(o.pos) and rng.random() < sc.cam_detect_p:
                    seen = cls if rng.random() >= sc.cam_misclass_p else ("forklift" if cls == "human" else "human")
                    dets.append(Detection(seen, o.x + rng.gauss(0, sigma), o.y + rng.gauss(0, sigma), "infra"))
        for poly in self.layout.coverage.values():
            if rng.random() < sc.cam_fp_p:
                xs, ys = [p[0] for p in poly], [p[1] for p in poly]
                dets.append(Detection("human", rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys)), "infra"))
        return dets

    def _inject(self, dt: float, now: float) -> None:
        sc = self.scenario
        if sc.fault_mtbf_h > 0:
            p = dt / (sc.fault_mtbf_h * 3600.0)
            for v in self.vehicles:
                if v.fault is None and v.operating_mode == "AUTOMATIC" and self.rng_faults.random() < p:
                    r = self.rng_faults.random()
                    if r < 0.4:
                        v.set_fault("DRIVE_FAULT", recoverable=self.rng_faults.random() < 0.6)
                    elif r < 0.7:
                        v.set_fault("LOCALIZATION_LOST")
                    else:
                        v.set_fault("SENSOR_FAULT")
        if sc.door_mtbf_h > 0:
            p = dt / (sc.door_mtbf_h * 3600.0)
            for d in self.doors.values():
                if d.fault is None and d.pending_fault is None and self.rng_doors.random() < p:
                    d.inject(self.rng_doors.choice(("JAM", "MOTOR", "NO_RESPONSE")),
                             now + self.rng_doors.uniform(180.0, 600.0))
        for d in self.doors.values():
            d.maintain(now)
        if sc.comms_mtbf_h > 0:
            p = dt / (sc.comms_mtbf_h * 3600.0)
            for v in self.vehicles:
                link = self.bus.link(v.serial)
                if now >= link.down_until and self.rng_faults.random() < p:
                    link.down_until = now + self.rng_faults.uniform(5.0, 25.0)

    # ------------------------------------------------------------ drills (operator training)
    def vehicle(self, serial: str) -> Vehicle:
        for v in self.vehicles:
            if v.serial == serial:
                return v
        raise KeyError(serial)

    def drill_fault(self, serial: str, fault: str, recoverable: bool = True) -> None:
        if fault not in FAULT_TYPES:
            raise ValueError(f"fault must be one of {', '.join(FAULT_TYPES)}")
        self.vehicle(serial).set_fault(fault, recoverable)

    def drill_door(self, door_id: str, fault: str) -> None:
        """Door failure for training; it stays until the field fix (door_field_fix)."""
        if door_id not in self.doors:
            raise ValueError(f"door must be one of {', '.join(self.doors) or '(none on this site)'}")
        if fault not in ("JAM", "MOTOR", "NO_RESPONSE"):
            raise ValueError("fault must be JAM, MOTOR or NO_RESPONSE")
        self.doors[door_id].inject(fault)

    def door_field_fix(self, door_id: str) -> None:
        if door_id not in self.doors:
            raise ValueError(f"unknown door {door_id}")
        self.doors[door_id].repair()

    def drill_fire(self, alarm: bool = True) -> None:
        """Fire panel signal for training (the panel publishes it like a door controller its state)."""
        self.bus.publish("facility/v1/fire/state", {"alarm": bool(alarm), "stamp": round(self.now, 3)}, self.now)

    def drill_comms(self, serial: str, seconds: float) -> None:
        self.vehicle(serial)
        self.bus.link(serial).down_until = self.now + max(1.0, min(120.0, seconds))

    def drill_intruder(self, zone_id: str, seconds: float = 45.0) -> str:
        zone = self.layout.zones[zone_id]
        nodes = [n for n, zs in self.layout.node_zones.items() if zone_id in zs
                 and self.layout.nodes[n].kind in ("storage", "cross", "spine", "rack", "pickface", "queue")]
        if not nodes:
            xs = [p[0] for p in zone.polygon]
            ys = [p[1] for p in zone.polygon]
            x, y = sum(xs) / len(xs), sum(ys) / len(ys)
        else:
            n = self.layout.nodes[self.rng_faults.choice(nodes)]
            x, y = n.x + 0.3, n.y
        hid = f"X{len(self.humans) + 1:02d}"
        h = (DcHuman(hid, "walker", x, y, random.Random(self.seed + len(self.humans)), site=self.process)
             if self.scenario.site == "dc" else Human(hid, "walker", x, y, random.Random(self.seed + len(self.humans))))
        h.frozen_until = self.now + seconds
        h.plan_next(self.layout)
        self.humans.append(h)
        return hid

    # ------------------------------------------------------------ views
    # ------------------------------------------------------------ measurement
    def _account(self, dt: float) -> None:
        """Time budget per vehicle class from the control room's own status (what a real FMS can log)."""
        for rec in self.manager.robots.values():
            cls = rec.vclass or "GENERIC"
            budget = self.time_budget.setdefault(cls, {})
            budget[rec.status] = budget.get(rec.status, 0.0) + dt
            if rec.task_id:
                self.task_time[cls] = self.task_time.get(cls, 0.0) + dt
            self.class_time[cls] = self.class_time.get(cls, 0.0) + dt

    def reset_measurement(self) -> None:
        """Start the measurement window here (after warm-up): counters restart, the world goes on unchanged."""
        m = self.manager
        self.window_start = self.now
        for k in m.counters:
            m.counters[k] = 0.0 if isinstance(m.counters[k], float) else 0
        m.repair_times.clear()
        m.alarms.total_activations = 0
        m.alarms.alerts = 0
        m.alarms.by_priority = {1: 0, 2: 0, 3: 0, 4: 0}
        m.alarms.by_type = {}
        for rec in m.robots.values():
            rec.busy_s = rec.wait_s = 0.0
        self.evaluator = Evaluator()
        self.dispatcher.reset_metrics(self.now)
        if hasattr(self.process, "reset_metrics"):
            self.process.reset_metrics()
        self.process.window_start = self.now
        self.time_budget, self.task_time, self.class_time = {}, {}, {}
        self._odo0 = {v.serial: v.odometer for v in self.vehicles}
        self._forklift_careless0 = sum(getattr(f, "careless_turns", 0) for f in self.forklifts)
        self.bus.stats = {k: 0 for k in self.bus.stats}

    def productivity(self, hours: float) -> dict:
        """Per-vehicle and per-picker rates, the units practitioners compare (docs/BENCHMARK.md)."""
        out: dict = {}
        by_cls: dict[str, list] = {}
        for v in self.vehicles:
            by_cls.setdefault(v.spec.vclass, []).append(v)
        for cls, vs in by_cls.items():
            odo = sum(v.odometer - self._odo0.get(v.serial, 0.0) for v in vs)
            t = self.class_time.get(cls, 0.0)
            out[cls] = {"vehicles": len(vs), "km": round(odo / 1000.0, 2),
                        "task_share": round(self.task_time.get(cls, 0.0) / t, 3) if t else None,
                        "time_share": {k: round(x / t, 3) for k, x in sorted(self.time_budget.get(cls, {}).items(),
                                                                              key=lambda kv: -kv[1])} if t else {}}
        site = self.process.stats(self.now)
        if self.scenario.site == "dc":
            agf = out.get("FORKLIFT")
            moves = site.get("putaway", 0) + site.get("shipped", 0)
            if agf:
                agf["moves_per_vehicle_h"] = round(moves / (agf["vehicles"] * hours), 2)
                agf["m_per_move"] = round(agf["km"] * 1000.0 / moves, 1) if moves else None
            amr = out.get("CARRIER")
            if amr:
                amr["orders_per_vehicle_h"] = round(site.get("tote_done", 0) / (amr["vehicles"] * hours), 2)
            pickers = sum(1 for h in self.humans if h.role == "picker")
            if pickers:
                out["pickers"] = {"count": pickers, "lph": round(site.get("picks", 0) / (pickers * hours), 1),
                                  "uph": round(site.get("units", 0) / (pickers * hours), 1),
                                  "robots_per_picker": round(len(by_cls.get("CARRIER", [])) / pickers, 2)}
        return out

    def kpi(self) -> dict:
        m = self.manager
        stats = self.dispatcher.stats(self.now)
        busy = sum(r.busy_s for r in m.robots.values())
        wait = sum(r.wait_s for r in m.robots.values())
        online = sum(1 for r in m.robots.values() if r.connection == "ONLINE")
        working = sum(1 for r in m.robots.values() if r.task_id)
        c = m.counters
        hours = max((self.now - self.window_start) / 3600.0, 1e-6)
        total_t = sum(self.class_time.values())
        ev = self.evaluator
        repairs = m.repair_times
        moving_contacts = [x for x in ev.contacts if x["moving"]]
        struck = [x for x in ev.contacts if not x["moving"] and x["other_moving"]]
        return {
            **stats,
            "robots": len(self.vehicles), "online": online, "working": working,
            # share of vehicle time with a task, averaged over the window (not a snapshot of the last instant)
            "utilization": round(sum(self.task_time.values()) / total_t, 3) if total_t else 0.0,
            "traffic_wait_ratio": round(wait / busy, 3) if busy > 0 else 0.0,
            "deadlocks": c["deadlocks"], "deadlocks_resolved": c["deadlocks_resolved"],
            "deadlocks_active": len(m.deadlocks),
            "deadlock_avg_s": round(c["deadlock_s"] / c["deadlocks_resolved"], 1) if c["deadlocks_resolved"] else None,
            "reroutes": c["reroutes"], "evasions": c["evasions"], "nudges": c["nudges"],
            "protective_stops": c["protective_stops"], "protective_human": c["protective_human"],
            "near_miss_fms": c["near_miss"],
            # safety outcomes (ground truth): every contact counts; what the vehicle was doing is split out
            "contacts": len(ev.contacts), "contacts_moving": len(moving_contacts), "contacts_struck": len(struck),
            "contacts_static": len(ev.contacts) - len(moving_contacts),
            "contacts_human": sum(1 for x in ev.contacts if x["kind"] == "human"),
            "contacts_human_moving": sum(1 for x in moving_contacts if x["kind"] == "human"),
            "near_misses": ev.near_misses, "close_calls": ev.close_calls,
            "clearance_violations": ev.clearance_violations,
            "min_gap_moving": None if ev.min_gap_moving == math.inf else round(max(0.0, ev.min_gap_moving), 2),
            "min_amr_gap": None if ev.min_amr_gap == math.inf else round(ev.min_amr_gap, 2),
            "tracking_ratio": round(ev.tracked_samples / ev.human_samples, 3) if ev.human_samples else None,
            "mismatch": c["mismatch"], "comms_lost": c["comms_lost"], "order_rejects": c["order_rejects"],
            "resends": c["resends"],
            "mttr_s": round(sum(repairs) / len(repairs), 1) if repairs else None,
            "alarms_per_h": round(m.alarms.total_activations / hours, 1),
            "alerts_per_h": round(m.alarms.alerts / hours, 1),
            "alarms_by_priority_h": {p: round(n / hours, 2) for p, n in m.alarms.by_priority.items()},
            "alarms_by_type": dict(sorted(m.alarms.by_type.items(), key=lambda kv: -kv[1])),
            "bus_dropped": self.bus.stats["dropped"], "bus_published": self.bus.stats["published"],
            "sim_hours": round(hours, 3),
            "careless_turns": sum(getattr(f, "careless_turns", 0) for f in self.forklifts) - self._forklift_careless0,
            "site": self.process.stats(self.now),
            "productivity": self.productivity(hours),
        }

    def snapshot(self) -> dict:
        m = self.manager
        tracks = list(m.tracker.tracks.values())
        tracked_h = {h.id for h in self.humans
                     if any(tr.cls == "human" and math.hypot(tr.x - h.x, tr.y - h.y) < 1.0 for tr in tracks)}
        locks_n, locks_l = {}, {}
        for res, owner in m.locks.holder.items():
            if res.startswith("N:"):
                locks_n[res[2:]] = owner
            else:
                locks_l[res[2:]] = owner
        return {
            "t": round(self.now, 1),
            "scenario": self.scenario.name, "scenario_title": self.scenario.title,
            "layout_key": f"{self.scenario.name}:{int(self.layout.two_way)}",
            "strategy": m.cfg.strategy, "mode": m.mode, "seed": self.seed,
            "robots": [m.robot_view(r) for r in m.robots.values()],
            "truth": {
                "robots": [[v.serial, round(v.x, 2), round(v.y, 2), round(v.theta, 2)] for v in self.vehicles],
                "humans": [[h.id, round(h.x, 2), round(h.y, 2), h.role, h.id in tracked_h,
                            round(h.v, 2)] for h in self.humans],
                "forklifts": [[f.id, round(f.x, 2), round(f.y, 2), round(f.heading, 2), round(f.v, 2)]
                              for f in self.forklifts],
                "external": [[e.id, round(e.x, 2), round(e.y, 2), round(e.heading, 2)] for e in self.externals],
            },
            "tracks": [[tr.id, tr.cls, round(tr.x, 2), round(tr.y, 2), round(tr.vx, 2), round(tr.vy, 2),
                        round(tr.sigma(self.now), 2), round(self.now - tr.last_seen, 1)] for tr in tracks],
            "external": [{"id": k, "x": round(v["x"], 2), "y": round(v["y"], 2), "itinerary": v["itinerary"]}
                         for k, v in m.external.items()],
            "locks": locks_n, "lanes": locks_l,
            "blocks": {k: list(v) for k, v in m.blocks.items()},
            # the control room's view of each door, plus the curtain position (ground truth, for the map)
            "doors": [dict(v, open=round(self.doors[v["id"]].progress, 2)) for v in m.doors.views(self.now)]
            if m.doors is not None else [],
            "zones": [{"id": z.id, "locked": z.locked, "limit": m.active_zone_limits().get(z.id),
                       "override": m.zone_speed_override.get(z.id)} for z in self.layout.zones.values()],
            "alarms": m.alarms.active_list(), "alarm_counts": m.alarms.counts(),
            "kpi": self.kpi(),
            "tasks": self.dispatcher.queue_view(self.now),
            "events": list(m.events)[-80:],
            "deadlocks": [{"members": v["members"], "since": round(v["start"], 1), "attempts": v["attempts"],
                           "method": v.get("method")} for v in m.deadlocks.values()],
            "incidents": [i.summary() for i in reversed(self.incidents)],
            "heat": {k: round(v, 1) for k, v in sorted(m.congestion.items(), key=lambda kv: -kv[1])[:40]},
            "bus": dict(self.bus.stats),
            "links_down": [s for s, q in self.bus.links.items() if self.now < q.down_until],
            "paused": sorted(m.robot_paused | m.area_paused),
            "task_rate": self.dispatcher.rate_per_h,
            "site": self.layout.site,
            "site_state": self.process.snapshot(),
            "task_kinds": self.process.task_kinds,
        }

    def incident(self, inc_id: str) -> dict:
        for inc in self.incidents:
            if inc.id == inc_id:
                return {**inc.summary(), "frame_data": inc.frames}
        raise KeyError(inc_id)

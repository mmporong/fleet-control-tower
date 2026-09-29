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
from .bus import Bus
from .control import FleetManager, ManagerConfig, near_miss
from .dispatcher import Dispatcher
from .geometry import box_gap, point_in_polygon
from .layout import COLUMN_X, ROW_Y, warehouse_layout
from .perception import Detection
from .scenario import Scenario
from .vehicle import FAULT_TYPES, Obstacle, Vehicle, VehicleSpec
from .vda5050 import topic

FRAME_PERIOD = 0.5
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


class Evaluator:
    """Ground-truth safety and awareness metrics (never fed back to the manager)."""

    def __init__(self) -> None:
        self.contacts: list[dict] = []
        self.near_misses = 0
        self.near_miss_log: deque = deque(maxlen=200)
        self._pair_contact: set[tuple[str, str]] = set()
        self._pair_nm: dict[tuple[str, str], float] = {}
        self.min_amr_gap = math.inf
        self.tracked_samples = 0
        self.human_samples = 0
        self.blind_samples = 0

    def check(self, now: float, vehicles: list[Vehicle], humans: list[Human], forklifts: list[Forklift],
              externals: list[ExternalRobot]) -> list[dict]:
        new_contacts = []
        others = [(h.id, "human", [(h.x, h.y, h.radius)]) for h in humans]
        others += [(f.id, "forklift", f.circles()) for f in forklifts]
        others += [(e.id, "external", [(e.x, e.y, e.radius)]) for e in externals]
        for i, v in enumerate(vehicles):
            r = v.spec.radius
            for w in vehicles[i + 1:]:
                if abs(v.x - w.x) > 3 or abs(v.y - w.y) > 3:
                    self._pair_contact.discard((v.serial, w.serial))
                    continue
                gap = math.hypot(v.x - w.x, v.y - w.y) - r - w.spec.radius
                self.min_amr_gap = min(self.min_amr_gap, gap)
                new_contacts += self._contact(now, v, w.serial, "amr", gap, (w.x, w.y))
            for oid, kind, circles in others:
                ox, oy = circles[len(circles) // 2][:2]
                if abs(v.x - ox) > 3.5 or abs(v.y - oy) > 3.5:
                    self._pair_contact.discard((v.serial, oid))
                    continue
                gap = min(box_gap((cx, cy), cr, (v.x, v.y), v.theta, v.spec.length / 2, v.spec.width / 2)
                          for cx, cy, cr in circles)
                new_contacts += self._contact(now, v, oid, kind, gap, (ox, oy))
                if kind == "human" and near_miss(abs(v.v), gap):
                    last = self._pair_nm.get((v.serial, oid), -1e9)
                    if now - last > 10.0:
                        self.near_misses += 1
                        self.near_miss_log.append({"t": round(now, 1), "robot": v.serial, "human": oid,
                                                   "gap": round(max(gap, 0.0), 2), "v": round(v.v, 2)})
                    self._pair_nm[(v.serial, oid)] = now
        return new_contacts

    def _contact(self, now: float, v: Vehicle, other: str, kind: str, gap: float, where) -> list[dict]:
        key = (v.serial, other)
        if gap > 0.05:
            if gap > 0.2:
                self._pair_contact.discard(key)
            return []
        if key in self._pair_contact:
            return []
        self._pair_contact.add(key)
        moving = abs(v.v) > 0.05 or abs(v.omega) > 0.05
        rec = {"t": round(now, 1), "robot": v.serial, "other": other, "kind": kind, "moving": moving,
               "x": round(v.x, 2), "y": round(v.y, 2), "speed": round(v.v, 2)}
        self.contacts.append(rec)
        return [rec]

    def awareness(self, now: float, humans: list[Human], tracks) -> tuple[int, int]:
        tracked = 0
        for h in humans:
            if any(tr.cls == "human" and math.hypot(tr.x - h.x, tr.y - h.y) < 1.0 for tr in tracks):
                tracked += 1
        self.human_samples += len(humans)
        self.tracked_samples += tracked
        return tracked, len(humans)


class Simulation:
    def __init__(self, scenario: Scenario, seed: int = 7, strategy: str = "locks+resolve",
                 dt: float = 0.1, config: ManagerConfig | None = None,
                 vehicle_spec: VehicleSpec | None = None) -> None:
        self.vehicle_spec = vehicle_spec
        self.scenario = scenario
        self.seed = seed
        self.dt = dt
        self.now = 0.0
        self.steps = 0
        self.layout = warehouse_layout(scenario.two_way)
        self.rng_faults = random.Random(seed * 7 + 3)
        self.rng_sense = random.Random(seed * 7 + 4)
        self.bus = Bus(random.Random(seed * 7 + 1))
        cfg = config or ManagerConfig(strategy=strategy)
        cfg.strategy = strategy
        self.manager = FleetManager(self.layout, self.bus, cfg)
        self.dispatcher = Dispatcher(self.manager, random.Random(seed * 7 + 2), scenario.rate_per_h)
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
        self.manager.alarms.listeners.append(self._on_alarm)
        self._build(random.Random(seed * 7 + 5))

    # ------------------------------------------------------------ setup
    def _build(self, rng: random.Random) -> None:
        sc = self.scenario
        starts = self.layout.nodes_of_kind("park") + self.layout.nodes_of_kind("charger")
        extra = [n for n in self.layout.nodes_of_kind("storage")]
        rng.shuffle(extra)
        starts = starts + extra
        for i in range(sc.robots):
            serial = f"AMR-{i + 1:02d}"
            v = Vehicle(serial, self.layout, starts[i], random.Random(self.seed * 1000 + i),
                        battery=rng.uniform(55, 98),
                        spec=replace(self.vehicle_spec) if self.vehicle_spec else None)
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
        if event in ("raised", "reactivated") and (alarm.priority == 1 or alarm.type == "NEAR_MISS"):
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
                   self.manager.robots[v.serial].status] for v in self.vehicles],
            "h": [[h.id, round(h.x, 2), round(h.y, 2)] for h in self.humans],
            "f": [[f.id, round(f.x, 2), round(f.y, 2), round(f.heading, 2)] for f in self.forklifts],
            "e": [[e.id, round(e.x, 2), round(e.y, 2)] for e in self.externals],
            "k": [[tr.id, tr.cls, round(tr.x, 2), round(tr.y, 2)] for tr in self.manager.tracker.tracks.values()],
        }

    # ------------------------------------------------------------ step
    def step(self) -> None:
        dt = self.dt
        self.now = round(self.now + dt, 6)
        now = self.now
        self.steps += 1
        robots_obs = [(v.serial, v.x, v.y, v.spec.radius, v.v) for v in self.vehicles]
        for h in self.humans:
            h.step(dt, now, self.layout, robots_obs, self.forklifts)
        for f in self.forklifts:
            f.step(dt, now, robots_obs, self.humans)
        ext_obs = [(v.serial, v.x, v.y, v.spec.radius) for v in self.vehicles] + \
                  [(h.id, h.x, h.y, h.radius) for h in self.humans] + \
                  [(f.id, cx, cy, cr) for f in self.forklifts for cx, cy, cr in f.circles()]
        for e in self.externals:
            e.step(dt, ext_obs, now)
        obstacles = [Obstacle(v.serial, "amr", v.x, v.y, v.spec.radius) for v in self.vehicles]
        obstacles += [Obstacle(h.id, "human", h.x, h.y, h.radius) for h in self.humans]
        obstacles += [Obstacle(f.id, "forklift", cx, cy, cr) for f in self.forklifts for cx, cy, cr in f.circles()]
        obstacles += [Obstacle(e.id, "external", e.x, e.y, e.radius) for e in self.externals]
        for v in self.vehicles:
            v.step(dt, now, obstacles)
        for c in self.evaluator.check(now, self.vehicles, self.humans, self.forklifts, self.externals):
            if c["moving"] or c["kind"] == "amr" and self._either_moving(c):
                for v in self.vehicles:
                    if v.serial in (c["robot"], c["other"]) and v.fault is None:
                        v.set_fault("BUMPER")
        if now >= self._next_sense:
            self._next_sense = now + 0.5
            for v in self.vehicles:
                v.sense(obstacles, self.layout.racks)
            self.manager.ingest_detections(self._infra_detections())
            for e in self.externals:
                self.manager.ingest_external(e.id, e.x, e.y, e.heading, e.itinerary(), e.speed)
            self.evaluator.awareness(now, self.humans, self.manager.tracker.tracks.values())
        self._inject(dt, now)
        for v in self.vehicles:
            for t, msg in v.publish_due(now):
                self.bus.publish(t, msg, now)
        self.bus.deliver_due(now)
        if now >= self._next_tick - 1e-9:
            self._next_tick = now + 0.2
            self.manager.tick(now, 0.2)
            self.dispatcher.tick(now)
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

    def _either_moving(self, contact: dict) -> bool:
        for v in self.vehicles:
            if v.serial in (contact["robot"], contact["other"]) and (abs(v.v) > 0.05 or abs(v.omega) > 0.05):
                return True
        return False

    def run(self, seconds: float) -> None:
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.step()

    def _infra_detections(self) -> list[Detection]:
        dets = []
        for h in self.humans:
            if self.layout.covered(h.pos) and self.rng_sense.random() > 0.03:
                dets.append(Detection("human", h.x + self.rng_sense.gauss(0, 0.12),
                                      h.y + self.rng_sense.gauss(0, 0.12), "infra"))
        for f in self.forklifts:
            if self.layout.covered(f.pos) and self.rng_sense.random() > 0.03:
                dets.append(Detection("forklift", f.x + self.rng_sense.gauss(0, 0.15),
                                      f.y + self.rng_sense.gauss(0, 0.15), "infra"))
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

    def drill_comms(self, serial: str, seconds: float) -> None:
        self.vehicle(serial)
        self.bus.link(serial).down_until = self.now + max(1.0, min(120.0, seconds))

    def drill_intruder(self, zone_id: str, seconds: float = 45.0) -> str:
        zone = self.layout.zones[zone_id]
        nodes = [n for n, zs in self.layout.node_zones.items() if zone_id in zs
                 and self.layout.nodes[n].kind in ("storage", "cross", "spine")]
        if not nodes:
            xs = [p[0] for p in zone.polygon]
            ys = [p[1] for p in zone.polygon]
            x, y = sum(xs) / len(xs), sum(ys) / len(ys)
        else:
            n = self.layout.nodes[self.rng_faults.choice(nodes)]
            x, y = n.x + 0.3, n.y
        hid = f"X{len(self.humans) + 1:02d}"
        h = Human(hid, "walker", x, y, random.Random(self.seed + len(self.humans)))
        h.frozen_until = self.now + seconds
        h.plan_next(self.layout)
        self.humans.append(h)
        return hid

    # ------------------------------------------------------------ views
    def kpi(self) -> dict:
        m = self.manager
        stats = self.dispatcher.stats(self.now)
        busy = sum(r.busy_s for r in m.robots.values())
        wait = sum(r.wait_s for r in m.robots.values())
        online = sum(1 for r in m.robots.values() if r.connection == "ONLINE")
        working = sum(1 for r in m.robots.values() if r.task_id)
        c = m.counters
        hours = max(self.now / 3600.0, 1e-6)
        ev = self.evaluator
        repairs = m.repair_times
        moving_contacts = [x for x in ev.contacts if x["moving"]]
        contacts_h = sum(1 for x in moving_contacts if x["kind"] == "human")
        return {
            **stats,
            "robots": len(self.vehicles), "online": online, "working": working,
            "utilization": round(working / max(1, len(self.vehicles)), 3),
            "traffic_wait_ratio": round(wait / busy, 3) if busy > 0 else 0.0,
            "deadlocks": c["deadlocks"], "deadlocks_resolved": c["deadlocks_resolved"],
            "deadlocks_active": len(m.deadlocks),
            "deadlock_avg_s": round(c["deadlock_s"] / c["deadlocks_resolved"], 1) if c["deadlocks_resolved"] else None,
            "reroutes": c["reroutes"], "evasions": c["evasions"], "nudges": c["nudges"],
            "protective_stops": c["protective_stops"], "protective_human": c["protective_human"],
            "near_miss_fms": c["near_miss"], "near_miss_truth": ev.near_misses,
            "contacts": len(moving_contacts), "contacts_human": contacts_h,
            "contacts_static": len(ev.contacts) - len(moving_contacts),
            "min_amr_gap": None if ev.min_amr_gap == math.inf else round(ev.min_amr_gap, 2),
            "tracking_ratio": round(ev.tracked_samples / ev.human_samples, 3) if ev.human_samples else None,
            "mismatch": c["mismatch"], "comms_lost": c["comms_lost"], "order_rejects": c["order_rejects"],
            "resends": c["resends"],
            "mttr_s": round(sum(repairs) / len(repairs), 1) if repairs else None,
            "alarms_per_h": round(m.alarms.total_activations / hours, 1),
            "bus_dropped": self.bus.stats["dropped"], "bus_published": self.bus.stats["published"],
            "sim_hours": round(hours, 3),
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
        }

    def incident(self, inc_id: str) -> dict:
        for inc in self.incidents:
            if inc.id == inc_id:
                return {**inc.summary(), "frame_data": inc.frames}
        raise KeyError(inc_id)

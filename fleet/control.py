"""Fleet manager (master control): the part a real control room runs.

It only knows what arrives on the bus and from the infrastructure sensors:
vehicle state/visualization, onboard detections, camera detections and the
external fleet's itinerary. From that it
- reserves nodes and lanes and releases a short base to each vehicle,
- blocks resources that people and forklifts are predicted to occupy,
- detects deadlocks in the wait-for graph and resolves them by re-routing
  or side-stepping the lowest-priority vehicle,
- checks that every vehicle is physically inside what it reserved,
- raises alarms with defined operator responses.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math

from .alarms import AlarmManager
from .bus import Bus
from .facility import DoorSupervisor
from .geometry import box_gap, dist, point_in_polygon, seg_point_dist
from .layout import Layout
from .perception import Detection, Tracker
from .planner import STUB_KINDS, Planner
from .traffic import LockTable, WaitInfo, find_cycles, lane_res, node_res, res_node
from .vda5050 import (HeaderIds, RouteEdge, RouteNode, action, build_instant_actions, build_order, header,
                      parse_iso_time, topic)

STRATEGIES = ("none", "locks", "locks+resolve")
LINK_WINDOW_S = 5.0          # window for the measured link quality shown per vehicle


def near_miss(speed: float, gap: float, margin: float = 0.0) -> bool:
    """Close pass faster than the reduced speed allowed near people.

    ISO 3691-4 lets a driverless vehicle pass people with reduced clearance
    only at or below 0.3 m/s; above that a gap under 0.3 m is a near miss.
    Any gap under 0.1 m while moving counts too. ``margin`` tightens the gap
    threshold when positions are noisy estimates (tracks, not ground truth).
    """
    return (speed > 0.35 and gap < 0.3 - margin) or (speed > 0.1 and gap < 0.1 - margin / 2)
STATUS_LABEL = {
    "MOVING": "주행", "WAITING": "교통 대기", "BLOCKED": "차단 대기", "ACTION": "작업 동작",
    "CHARGING": "충전", "IDLE": "대기", "PARKED": "주차", "PAUSED": "일시정지", "ESTOP": "비상정지",
    "FAULT": "고장", "MANUAL": "원격 조종", "OFFLINE": "통신 두절", "PROTECTIVE": "보호정지",
}


@dataclass
class ManagerConfig:
    strategy: str = "locks+resolve"
    lookahead_nodes: int = 3
    horizon_nodes: int = 4
    clear_dist: float = 1.3
    deadlock_confirm_s: float = 2.0
    deadlock_alarm_s: float = 30.0
    reroute_after_s: float = 8.0
    blocked_alarm_s: float = 60.0
    # in a collaborative picking zone robots wait for people by design: alarm only when it lasts
    collab_blocked_alarm_s: float = 180.0
    comms_timeout_s: float = 3.0
    order_resend_s: float = 2.5
    human_horizon_s: float = 2.0
    forklift_horizon_s: float = 4.0
    mismatch_confirm_s: float = 1.5
    human_zones: bool = True
    congestion_aware: bool = True      # route around stationary holders and recent queues
    congestion_half_life_s: float = 180.0


@dataclass
class RobotRecord:
    serial: str
    manufacturer: str = "sim"
    # ---- view from the bus
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0
    v: float = 0.0
    battery: float = 100.0
    charging: bool = False
    mode: str = "AUTOMATIC"
    paused: bool = False
    estop: bool = False
    safety_field: str = "CLEAR"
    field_cause: str = ""
    last_node: str | None = None
    last_seq: int = 0
    dist_since: float = 0.0
    driving: bool = False
    loaded: bool = False
    fatal: str | None = None
    errors: list = field(default_factory=list)
    stop_reason: str = ""
    positioned: bool = True
    state_order_id: str = ""
    state_update_id: int = 0
    actions: dict = field(default_factory=dict)
    detections: list = field(default_factory=list)
    last_seen: float = -1e9
    connection: str = "OFFLINE"
    rx: deque = field(default_factory=lambda: deque(maxlen=120))   # (received at, latency s) of state/vis
    has_state: bool = False
    # ---- plan
    route: list = field(default_factory=list)
    order_id: str = ""
    update_id: int = 0
    base_end: int = 0
    cur_idx: int = 0
    sent_base_end: int = 0
    goal_actions: dict = field(default_factory=dict)
    purpose: str = ""
    task_id: str | None = None
    order_pending: bool = False
    last_order: dict | None = None
    last_order_at: float = -1e9
    pending_block: WaitInfo | None = None
    wait: WaitInfo | None = None
    reroute_next: float = 0.0
    resume: tuple | None = None
    status: str = "IDLE"
    reason: str = ""
    mismatch_since: float | None = None
    state_base_seq: int = 0
    state_remaining: int = 0
    protective_since: float | None = None
    yield_next: float = 0.0
    vclass: str | None = None          # from the VDA 5050 factsheet
    rot_zone: dict = field(default_factory=dict)   # route index -> resources reserved for turning there
    length: float = 0.9
    width: float = 0.7
    factsheet: dict | None = None
    reach: float | None = None           # batteryState.reach: metres the vehicle can still drive
    mode_sent_at: float = -1e9
    pause_sent_at: float = -1e9
    fault_since: float | None = None
    busy_s: float = 0.0
    wait_s: float = 0.0
    route_start: float = 0.0

    @property
    def goal(self) -> str | None:
        return self.route[-1] if self.route else None

    def at_base_end(self) -> bool:
        return (self.state_order_id == self.order_id and self.cur_idx == self.base_end
                and not self.driving and self.dist_since < 1e-6)


class FleetManager:
    def __init__(self, layout: Layout, bus: Bus, config: ManagerConfig | None = None,
                 manufacturer: str = "sim") -> None:
        if config and config.strategy not in STRATEGIES:
            raise ValueError(config.strategy)
        self.layout = layout
        self.bus = bus
        self.cfg = config or ManagerConfig()
        self.manufacturer = manufacturer
        self.planner = Planner(layout)
        self.locks = LockTable()
        self.tracker = Tracker()
        self.alarms = AlarmManager()
        self.robots: dict[str, RobotRecord] = {}
        self.now = 0.0
        self.ids = HeaderIds()
        self._order_seq = 0
        self._action_seq = 0
        self.events: deque = deque(maxlen=600)
        self.blocks: dict[str, tuple[str, str]] = {}
        self.external: dict[str, dict] = {}
        self.mode = "RUNNING"           # RUNNING | HOLD | ESTOP
        self.area_paused: set[str] = set()
        self.robot_paused: set[str] = set()
        self.repair_times: list[float] = []
        self._nm_last: dict[str, float] = {}
        self.infra_last: float | None = None    # last infrastructure detection cycle
        self.infra_lost = False
        self.zone_speed_override: dict[str, float] = {}
        self.zone_human_active: dict[str, float] = {}
        self._zone_broadcast: dict[str, float | None] = {}
        self._last_zone_rebroadcast = 0.0
        self.deadlocks: dict[frozenset, dict] = {}
        self._congested_since: float | None = None
        self.dispatcher = None           # set by the simulation
        self.congestion: dict[str, float] = {}          # cumulative waiting seconds per node
        self.congestion_recent: dict[str, float] = {}   # exponentially decayed waiting seconds
        self.counters = {"deadlocks": 0, "deadlocks_resolved": 0, "deadlock_s": 0.0, "reroutes": 0,
                         "evasions": 0, "orders": 0, "order_updates": 0, "order_rejects": 0,
                         "resends": 0, "protective_stops": 0, "protective_human": 0, "near_miss": 0,
                         "nudges": 0, "mismatch": 0, "comms_lost": 0}
        self._grid = self._build_resource_grid()
        bus.subscribe(f"uagv/v2/{manufacturer}/+/state", self._on_state)
        bus.subscribe(f"uagv/v2/{manufacturer}/+/visualization", self._on_vis)
        bus.subscribe(f"uagv/v2/{manufacturer}/+/connection", self._on_connection)
        bus.subscribe(f"uagv/v2/{manufacturer}/+/factsheet", self._on_factsheet)
        self.doors = DoorSupervisor(layout.doors, bus, self.alarms, self.event) if layout.doors else None

    # ================================================================ events
    def event(self, kind: str, source: str, msg: str, level: str = "info", **data) -> None:
        self.events.append({"t": round(self.now, 1), "type": kind, "source": source, "msg": msg,
                            "level": level, **data})

    # ================================================================ inbound
    def register(self, serial: str) -> RobotRecord:
        rec = self.robots.setdefault(serial, RobotRecord(serial, self.manufacturer))
        return rec

    def _serial(self, t: str) -> str:
        return t.split("/")[3]

    def _on_connection(self, t: str, msg: dict) -> None:
        rec = self.register(self._serial(t))
        rec.connection = msg["connectionState"]
        rec.last_seen = self.now

    def _on_factsheet(self, t: str, msg: dict) -> None:
        rec = self.register(self._serial(t))
        rec.factsheet = msg
        rec.vclass = msg["typeSpecification"]["agvClass"]
        rec.length = float(msg["physicalParameters"]["length"])
        rec.width = float(msg["physicalParameters"]["width"])

    def supports(self, rec: RobotRecord, action_type: str) -> bool:
        if not rec.factsheet:
            return True
        return any(a["actionType"] == action_type for a in rec.factsheet["protocolFeatures"]["agvActions"])

    def _on_vis(self, t: str, msg: dict) -> None:
        rec = self.register(self._serial(t))
        p = msg["agvPosition"]
        rec.x, rec.y, rec.theta = p["x"], p["y"], p["theta"]
        rec.v = msg["velocity"]["vx"]
        self._seen(rec, msg)

    def _seen(self, rec: RobotRecord, msg: dict | None = None) -> None:
        if msg is not None:
            received = max(self.now, self.bus.now)
            rec.rx.append((received, max(0.0, received - parse_iso_time(msg["timestamp"]))))
        if rec.connection == "CONNECTIONBROKEN":
            self.event("comms_restored", rec.serial, "통신 복구", "info")
            self.alarms.set("COMMS_LOST", rec.serial, False, self.now)
        rec.connection = "ONLINE"
        rec.last_seen = self.now

    def _on_state(self, t: str, msg: dict) -> None:
        rec = self.register(self._serial(t))
        self._seen(rec, msg)
        rec.has_state = True
        p = msg["agvPosition"]
        rec.x, rec.y, rec.theta = p["x"], p["y"], p["theta"]
        rec.positioned = p["positionInitialized"]
        rec.v = msg["velocity"]["vx"]
        rec.battery = msg["batteryState"]["batteryCharge"]
        rec.reach = msg["batteryState"].get("reach")
        rec.charging = msg["batteryState"]["charging"]
        rec.mode = msg["operatingMode"]
        rec.paused = msg["paused"]
        rec.estop = msg["safetyState"]["eStop"] != "NONE"
        prev_field = rec.safety_field
        rec.safety_field = msg.get("x_safetyField", {}).get("state", "CLEAR")
        rec.field_cause = msg.get("x_safetyField", {}).get("cause", "")
        if rec.safety_field == "PROTECTIVE" and prev_field != "PROTECTIVE" and msg["driving"]:
            self.counters["protective_stops"] += 1
            if rec.field_cause.startswith("human"):
                self.counters["protective_human"] += 1
        rec.last_node = msg["lastNodeId"] or None
        rec.last_seq = msg["lastNodeSequenceId"]
        rec.dist_since = msg["distanceSinceLastNode"]
        rec.driving = msg["driving"]
        rec.loaded = bool(msg["loads"])
        rec.state_order_id = msg["orderId"]
        rec.state_update_id = msg["orderUpdateId"]
        rec.detections = msg.get("x_detections", [])
        info = msg.get("information", [])
        rec.stop_reason = info[0]["infoDescription"] if info else ""
        for a in msg["actionStates"]:
            rec.actions[a["actionId"]] = a["actionStatus"]
        if len(rec.actions) > 200:
            for k in list(rec.actions)[:-100]:
                del rec.actions[k]
        released = [n["sequenceId"] for n in msg["nodeStates"] if n["released"]]
        rec.state_base_seq = released[-1] if released else rec.last_seq
        rec.state_remaining = len(msg["nodeStates"])
        fatal = [e for e in msg["errors"] if e["errorLevel"] == "FATAL"]
        new_fatal = fatal[0]["errorType"].upper() if fatal else None
        if new_fatal != rec.fatal:
            self._fatal_changed(rec, new_fatal)
        for e in msg["errors"]:
            if e["errorLevel"] == "WARNING" and e["errorType"] in (
                    "orderError", "orderUpdateError", "noRouteError", "validationError"):
                self.counters["order_rejects"] += 1
                self.event("order_rejected", rec.serial, f"{e['errorType']}: {e['errorDescription']}", "warn")
                self._resync(rec)
        rec.errors = msg["errors"]

    def _fatal_changed(self, rec: RobotRecord, fatal: str | None) -> None:
        old = rec.fatal
        rec.fatal = fatal
        if fatal:
            rec.fault_since = self.now
            self.event("fault", rec.serial, f"고장 발생: {fatal}", "crit")
            if fatal == "LOCALIZATION_LOST":
                self.alarms.set("LOCALIZATION_LOST", rec.serial, True, self.now, "위치 추정 실패로 정지")
            elif fatal == "BUMPER":
                near_human = [tr for tr in self.tracker.tracks.values()
                              if tr.cls == "human" and dist((tr.x, tr.y), (rec.x, rec.y)) < 1.8]
                if near_human:
                    self._human_contact(rec, near_human[0].id)
                else:
                    self.alarms.set("ROBOT_COLLISION", rec.serial, True, self.now,
                                    f"{rec.serial} 범퍼 접촉")
            else:
                msg = f"{fatal}" + (" · 적재물 보유" if rec.loaded else "")
                self.alarms.set("ROBOT_FAULT", rec.serial, True, self.now, msg, {"loaded": rec.loaded})
        else:
            if rec.fault_since is not None:
                self.event("fault_cleared", rec.serial, f"고장 해제: {old}", "info",
                           duration=round(self.now - rec.fault_since, 1))
                self.repair_times.append(self.now - rec.fault_since)
            rec.fault_since = None
            for t in ("LOCALIZATION_LOST", "ROBOT_FAULT", "ROBOT_COLLISION"):
                self.alarms.set(t, rec.serial, False, self.now)
            if old == "BUMPER":
                self.alarms.set("HUMAN_CONTACT", rec.serial, False, self.now)

    def _human_contact(self, rec: RobotRecord, track_id: str) -> None:
        paused = []
        for other in self.robots.values():
            if other.serial != rec.serial and dist((other.x, other.y), (rec.x, rec.y)) <= 6.0:
                self.area_paused.add(other.serial)
                paused.append(other.serial)
        self.alarms.set("HUMAN_CONTACT", rec.serial, True, self.now,
                        f"{rec.serial} 사람({track_id}) 접촉 추정 · 주변 {len(paused)}대 일시정지",
                        {"paused": paused, "x": round(rec.x, 1), "y": round(rec.y, 1)})
        self.event("human_contact", rec.serial, f"사람 접촉 추정, 주변 {len(paused)}대 일시정지", "crit")

    def _resync(self, rec: RobotRecord) -> None:
        """After a rejected order, fall back to what the vehicle actually holds."""
        if not rec.route:
            return
        if rec.state_order_id != rec.order_id:
            # the vehicle never took this order: send it again as a new order once possible
            rec.last_order = None
            rec.update_id = 0
            rec.sent_base_end = 0
            rec.order_pending = True
            return
        vehicle_end = min(rec.state_base_seq // 2, rec.base_end)
        rec.sent_base_end = max(vehicle_end, rec.cur_idx)
        rec.update_id = max(rec.update_id, rec.state_update_id)
        rec.order_pending = True

    # ================================================================ infra inputs
    def ingest_detections(self, detections: list[Detection]) -> None:
        self.infra_last = self.now
        dets = list(detections)
        for rec in self.robots.values():
            for d in rec.detections:
                dets.append(Detection(d["cls"], d["x"], d["y"], f"amr:{rec.serial}"))
        for ev in self.tracker.update(dets, self.now):
            if ev["type"] == "track_lost":
                self.event("track_lost", ev["track"],
                           f"{'사람' if ev['cls'] == 'human' else '지게차'} 추적 소실 ({ev['x']}, {ev['y']})", "warn")

    def ingest_external(self, ext_id: str, x: float, y: float, theta: float, itinerary: list[str],
                        speed: float = 0.5) -> None:
        """Read-only fleet adapter input: position plus the nodes it will use next."""
        prev = self.external.get(ext_id)
        moved = self.now
        if prev is not None and dist((prev["x"], prev["y"]), (x, y)) < 0.05:
            moved = prev.get("moved", self.now)
        self.external[ext_id] = {"x": x, "y": y, "theta": theta, "itinerary": itinerary, "seen": self.now,
                                 "moved": moved, "speed": speed}

    # ================================================================ outbound
    def _aid(self, kind: str) -> str:
        self._action_seq += 1
        return f"{kind}-{self._action_seq}"

    def _instant(self, rec: RobotRecord, actions: list[dict]) -> None:
        t = topic(self.manufacturer, rec.serial, "instantActions")
        msg = build_instant_actions(header(self.ids, t, self.manufacturer, rec.serial, self.now), actions)
        self.bus.publish(t, msg, self.now)

    def instant(self, serial: str, action_type: str, params: dict | None = None) -> None:
        self._instant(self.robots[serial], [action(action_type, self._aid(action_type), "NONE", params)])

    def _send_order(self, rec: RobotRecord) -> None:
        new = rec.last_order is None or rec.last_order["orderId"] != rec.order_id
        i0 = 0 if new else rec.sent_base_end
        i_end = min(len(rec.route) - 1, rec.base_end + self.cfg.horizon_nodes)
        nodes = []
        for i in range(i0, i_end + 1):
            n = self.layout.nodes[rec.route[i]]
            nodes.append(RouteNode(n.id, n.x, n.y, list(rec.goal_actions.get(i, []))))
        edges = []
        for a, b in zip(rec.route[i0:i_end], rec.route[i0 + 1:i_end + 1]):
            e = self.layout.edge_between(a, b)
            edges.append(RouteEdge(e.id, a, b, e.max_speed))
        if not new:
            rec.update_id += 1
        t = topic(self.manufacturer, rec.serial, "order")
        msg = build_order(header(self.ids, t, self.manufacturer, rec.serial, self.now), rec.order_id,
                          rec.update_id, nodes, edges, 2 * i0, rec.base_end - i0 + 1)
        self.bus.publish(t, msg, self.now)
        self.counters["order_updates" if not new else "orders"] += 1
        rec.last_order = msg
        rec.last_order_at = self.now
        rec.sent_base_end = rec.base_end
        rec.order_pending = False

    # ================================================================ routes
    def can_take_route(self, rec: RobotRecord) -> bool:
        if rec.mode != "AUTOMATIC" or rec.fatal or rec.connection != "ONLINE" or not rec.has_state:
            return False
        if rec.last_node is None or rec.driving or rec.dist_since > 1e-6:
            return False
        if rec.route and not rec.at_base_end():
            return False
        return not self._goal_action_running(rec)

    def _goal_action_running(self, rec: RobotRecord) -> bool:
        return any(rec.actions.get(a["actionId"]) in ("RUNNING", "INITIALIZING")
                   for acts in rec.goal_actions.values() for a in acts)

    def assign_route(self, rec: RobotRecord, goal: str, purpose: str, goal_actions: list[dict] | None = None,
                     task_id: str | None = None, start_actions: list[dict] | None = None,
                     avoid_nodes: set[str] | None = None, avoid_lanes: set[str] | None = None,
                     strict_doors: bool = False) -> bool:
        """``strict_doors``: only accept a route that avoids every unusable door (used for detours, so a
        vehicle waiting at one closed door is not sent to wait at another)."""
        start = rec.last_node
        if start is None:
            return False
        prev = rec.route[rec.cur_idx - 1] if rec.route and rec.cur_idx > 0 and rec.cur_idx < len(rec.route) \
            and rec.route[rec.cur_idx] == start else None
        penalty = self._penalties(rec, goal)
        path = None
        closed = self.doors.unavailable_lanes(self.now) if self.doors is not None else set()
        if closed:
            # a failed or disabled door: go around it if the site allows, otherwise wait at it
            path = self.planner.plan(start, goal, avoid_nodes=avoid_nodes or set(),
                                     avoid_lanes=(avoid_lanes or set()) | closed, node_penalty=penalty, prev=prev,
                                     vclass=rec.vclass)
        if path is None and not (strict_doors and closed):
            path = self.planner.plan(start, goal, avoid_nodes=avoid_nodes or set(),
                                     avoid_lanes=avoid_lanes or set(), node_penalty=penalty, prev=prev,
                                     vclass=rec.vclass)
        if path is None:
            return False
        self._order_seq += 1
        rec.route = path
        rec.rot_zone = {}
        rec.order_id = f"{rec.serial}-O{self._order_seq}"
        rec.update_id = 0
        rec.base_end = 0
        rec.cur_idx = 0
        rec.sent_base_end = 0
        rec.last_order = None
        rec.goal_actions = {}
        if start_actions:
            rec.goal_actions[0] = list(start_actions)
        if goal_actions:
            rec.goal_actions.setdefault(len(path) - 1, []).extend(goal_actions)
        rec.purpose = purpose
        rec.task_id = task_id
        rec.order_pending = True
        rec.pending_block = None
        rec.wait = None
        rec.route_start = self.now
        return True

    def clear_route(self, rec: RobotRecord) -> None:
        rec.route = []
        rec.goal_actions = {}
        rec.purpose = ""
        rec.task_id = None
        rec.order_pending = False
        rec.wait = None
        rec.pending_block = None
        rec.resume = None

    def _penalties(self, rec: RobotRecord, goal: str | None = None) -> dict[str, float]:
        """Soft costs: nodes held by other robots, blocked nodes and recent queues."""
        pen: dict[str, float] = {}
        for res, owner in self.locks.holder.items():
            n = res_node(res)
            if n and owner != rec.serial:
                other = self.robots.get(owner)
                stuck = other is None or other.fatal or other.connection != "ONLINE" or other.mode != "AUTOMATIC"
                if stuck:
                    cost = 120.0
                elif self.cfg.congestion_aware and not other.driving:
                    cost = 25.0      # stationary holder: working at a station or queued itself
                else:
                    cost = 6.0
                pen[n] = pen.get(n, 0.0) + cost
        goal = goal or rec.goal
        goal_zones = set(self.layout.node_zones.get(goal, [])) if goal else set()
        for n, zones in self.layout.node_zones.items():
            for z in zones:
                kind = self.layout.zones[z].kind
                if kind == "picking" and z not in goal_zones:
                    pen[n] = pen.get(n, 0.0) + 40.0   # people pick here: no through traffic
        if self.cfg.congestion_aware:
            for n, sec in self.congestion_recent.items():
                if sec > 1.0:
                    pen[n] = pen.get(n, 0.0) + min(60.0, 0.3 * sec)
        for res, (kind, _ref) in self.blocks.items():
            n = res_node(res)
            if n:
                pen[n] = pen.get(n, 0.0) + (30.0 if kind in ("human", "forklift") else 200.0)
        return pen

    def route_done(self, rec: RobotRecord) -> bool:
        if rec.purpose == "recover" and rec.route and rec.state_remaining == 0 and not rec.driving \
                and rec.dist_since < 1e-6:
            return True   # canceled order: the vehicle stopped wherever it could
        if not rec.route or rec.state_order_id != rec.order_id or rec.cur_idx != len(rec.route) - 1:
            return False
        if rec.driving:
            return False
        for a in rec.goal_actions.get(len(rec.route) - 1, []):
            if rec.actions.get(a["actionId"]) not in ("FINISHED", "FAILED"):
                return False
        return True

    def goal_action_failed(self, rec: RobotRecord) -> bool:
        return any(rec.actions.get(a["actionId"]) == "FAILED"
                   for a in rec.goal_actions.get(len(rec.route) - 1, []))

    # ================================================================ tick
    def tick(self, now: float, dt: float) -> None:
        self.now = now
        self._check_connections()
        for ev in self.tracker.prune(now):
            self.event("track_lost", ev["track"], f"추적 만료 ({ev['x']}, {ev['y']})", "warn")
        self._check_infra()
        self._update_blocks()
        self._update_external_locks()
        if self.doors is not None:
            self.doors.tick(now, *self._door_demand())
            self._door_blocks()
        decay = 0.5 ** (dt / self.cfg.congestion_half_life_s)
        for n in list(self.congestion_recent):
            self.congestion_recent[n] *= decay
            if self.congestion_recent[n] < 0.05:
                del self.congestion_recent[n]
        order = sorted(self.robots.values(), key=self._priority, reverse=True)
        for rec in order:
            self._progress(rec)
        for rec in order:
            self._extend(rec)
        for rec in order:
            self._classify(rec, dt)
        if self.cfg.strategy != "none":
            self._deadlocks()
        self._reroute_blocked()
        self._yield_to_uncontrolled()
        self._nudge_idle_blockers()
        self._monitor_occupancy()
        for rec in order:
            if rec.order_pending and rec.route and rec.connection == "ONLINE":
                if rec.last_order is None or rec.last_order["orderId"] != rec.order_id:
                    if self.can_take_route_now(rec):
                        self._send_order(rec)
                else:
                    self._send_order(rec)
            elif rec.route and rec.last_order is not None and rec.connection == "ONLINE" \
                    and now - rec.last_order_at > self.cfg.order_resend_s \
                    and (rec.state_order_id != rec.last_order["orderId"]
                         or rec.state_update_id < rec.last_order["orderUpdateId"]):
                self.bus.publish(topic(self.manufacturer, rec.serial, "order"), rec.last_order, now)
                rec.last_order_at = now
                self.counters["resends"] += 1
        self._zone_speeds()
        self._near_misses()
        self._robot_alarms()
        self.alarms.tick(now)
        self._enforce_fleet_mode()

    def can_take_route_now(self, rec: RobotRecord) -> bool:
        """A new order id is only accepted once the vehicle's base is finished."""
        if rec.mode != "AUTOMATIC" or rec.fatal or rec.driving or rec.dist_since > 1e-6:
            return False
        return rec.last_node == rec.route[0]

    def _priority(self, rec: RobotRecord) -> float:
        p = 0.0
        if self.dispatcher is not None and rec.task_id:
            p += 100.0 * self.dispatcher.task_priority(rec.task_id)
        if rec.loaded:
            p += 20.0
        if rec.purpose == "charge" and rec.battery < 15:
            p += 60.0
        if rec.wait is not None:
            p += min(60.0, (self.now - rec.wait.since) / 2.0)
        return p

    def _check_connections(self) -> None:
        for rec in self.robots.values():
            if rec.connection == "ONLINE" and self.now - rec.last_seen > self.cfg.comms_timeout_s:
                rec.connection = "CONNECTIONBROKEN"
                self.counters["comms_lost"] += 1
                self.event("comms_lost", rec.serial, "상태 수신 끊김(예약 유지)", "crit")
                self.alarms.set("COMMS_LOST", rec.serial, True, self.now,
                                f"{rec.serial} {self.cfg.comms_timeout_s:.0f}초 이상 무응답 · 예약 {len(self.locks.resources_of(rec.serial))}건 유지")

    def _check_infra(self) -> None:
        """Camera/RTLS feed watchdog: without it people in blind spots are invisible."""
        if self.infra_last is None:
            return
        lost = self.now - self.infra_last > 3.0
        if lost != self.infra_lost:
            self.infra_lost = lost
            self.event("infra_feed", "infra", "인프라 감지 두절: 사람 구역 전체 감속" if lost
                       else "인프라 감지 복구", "crit" if lost else "info")
        self.alarms.set("INFRA_FEED_LOST", "infra", lost, self.now,
                        f"인프라 감지 {self.now - self.infra_last:.0f}초 무응답" if lost else "")

    # ---------------------------------------------------------------- blocks
    def _build_resource_grid(self, cell: float = 2.0) -> dict:
        grid: dict[tuple[int, int], list[tuple[str, str]]] = {}
        for n in self.layout.nodes.values():
            grid.setdefault((int(n.x // cell), int(n.y // cell)), []).append(("N", n.id))
        seen = set()
        for e in self.layout.edges.values():
            if e.lane in seen:
                continue
            seen.add(e.lane)
            a, b = self.layout.nodes[e.start], self.layout.nodes[e.end]
            steps = max(1, int(math.hypot(b.x - a.x, b.y - a.y) // (cell / 2)))
            cells = set()
            for k in range(steps + 1):
                px = a.x + (b.x - a.x) * k / steps
                py = a.y + (b.y - a.y) * k / steps
                cells.add((int(px // cell), int(py // cell)))
            for c in cells:
                grid.setdefault(c, []).append(("L", e.lane))
        self._cell = cell
        return grid

    def _query(self, x: float, y: float, r: float) -> set[tuple[str, str]]:
        c = self._cell
        out: set[tuple[str, str]] = set()
        for i in range(int((x - r) // c), int((x + r) // c) + 1):
            for j in range(int((y - r) // c), int((y + r) // c) + 1):
                out.update(self._grid.get((i, j), ()))
        return out

    def _lane_points(self, lane: str):
        a, b = lane.split("|")
        return self.layout.nodes[a].pos, self.layout.nodes[b].pos

    def _update_blocks(self) -> None:
        blocks: dict[str, tuple[str, str]] = {}
        horizon = {"human": self.cfg.human_horizon_s, "forklift": self.cfg.forklift_horizon_s}
        collab = [z.polygon for z in self.layout.zones.values() if z.kind == "picking"]
        if self.cfg.strategy != "none":
            for tid, cls, x, y, r in self.tracker.predicted_discs(self.now, horizon):
                if cls == "human" and any(point_in_polygon((x, y), poly) for poly in collab):
                    # collaborative picking zone: people and robots share the aisle by design; block only when
                    # the person stands in the robot's corridor, slow passes are left to the safety fields
                    r -= 0.25
                for kind, ident in self._query(x, y, r):
                    if kind == "N":
                        n = self.layout.nodes[ident]
                        if math.hypot(n.x - x, n.y - y) <= r:
                            blocks.setdefault(node_res(ident), (cls, tid))
                    else:
                        a, b = self._lane_points(ident)
                        if seg_point_dist((x, y), a, b) <= r - 0.25:
                            blocks.setdefault(lane_res(ident), (cls, tid))
        for z in self.layout.zones.values():
            if z.locked:
                for nid, zones in self.layout.node_zones.items():
                    if z.id in zones:
                        blocks[node_res(nid)] = ("zone", z.id)
        self.blocks = blocks

    def _door_demand(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """(vehicles that will pass each door soon, vehicles already holding a lane through it).

        Soon means within PREREQUEST_M along the route from the vehicle's position, or waiting for the door.
        """
        want: dict[str, set[str]] = {}
        held: dict[str, set[str]] = {}
        reach = self.doors.PREREQUEST_M
        for rec in self.robots.values():
            if not rec.route or rec.cur_idx >= len(rec.route) - 1 or rec.fatal:
                continue
            for w in (rec.wait, rec.pending_block):
                if w is not None and w.kind == "door":
                    want.setdefault(w.holder, set()).add(rec.serial)
            # distance still to drive: the edge in progress counts from the vehicle, earlier edges not at all
            travelled = -rec.dist_since
            for k in range(rec.cur_idx + 1, len(rec.route)):
                e = self.layout.edge_between(rec.route[k - 1], rec.route[k])
                door = self.doors.lane_door.get(e.lane)
                if door is not None:
                    want.setdefault(door, set()).add(rec.serial)
                travelled += e.length
                if travelled > reach:
                    break
        for res, owner in self.locks.holder.items():
            if res.startswith("L:") and res[2:] in self.doors.lane_door:
                held.setdefault(self.doors.lane_door[res[2:]], set()).add(owner)
        return want, held

    def _door_blocks(self) -> None:
        """A door lane can be reserved only while its door reports OPEN (the physical curtain is up)."""
        for lane, door in self.doors.lane_door.items():
            if not self.doors.is_open(door, self.now):
                # the curtain is the lasting cause: it overrides a person predicted on the crossing next to it,
                # so the wait (and the operator's view) names the door, whose own alarm covers it
                self.blocks[lane_res(lane)] = ("door", door)

    def _update_external_locks(self) -> None:
        for ext_id, info in self.external.items():
            if self.now - info["seen"] > 3.0:
                self.locks.release_all(ext_id)
                continue
            want: list[str] = []
            itin = self._external_eta_nodes(info)
            for a, b in zip(itin, itin[1:]):
                want.append(lane_res(self._lane_between(a, b)))
            want += [node_res(n) for n in itin]
            self.locks.retain_only(ext_id, set(want))
            for r in want:
                if self.locks.holder_of(r) is None:
                    self.locks.try_acquire(ext_id, [r])

    def _lane_between(self, a: str, b: str) -> str:
        for e in self.layout.out_edges[a]:
            if e.end == b:
                return e.lane
        for e in self.layout.out_edges[b]:
            if e.end == a:
                return e.lane
        raise KeyError(f"no lane {a}-{b}")

    def _external_eta_nodes(self, info: dict, horizon_s: float = 8.0) -> list[str]:
        """Itinerary prefix the external unit reaches within ``horizon_s`` (at least 2 nodes)."""
        itin = info["itinerary"]
        speed = max(0.3, info.get("speed", 0.5))
        out = list(itin[:2])
        pos = (info["x"], info["y"])
        travelled = dist(pos, self.layout.nodes[itin[1]].pos) if len(itin) > 1 else 0.0
        for a, b in zip(itin[1:], itin[2:]):
            travelled += dist(self.layout.nodes[a].pos, self.layout.nodes[b].pos)
            if travelled / speed > horizon_s:
                break
            out.append(b)
        return out

    # ---------------------------------------------------------------- progress
    def _needed(self, rec: RobotRecord) -> set[str] | None:
        if not rec.has_state or rec.connection != "ONLINE":
            return None                      # keep everything while we cannot see the robot
        if rec.mode != "AUTOMATIC":
            return self._occupied(rec)
        if not rec.route or rec.state_order_id != rec.order_id:
            if rec.last_node is None:
                return self._occupied(rec)
            if rec.route and rec.state_order_id != rec.order_id:
                return None                  # new order in flight: release nothing yet
            need = {node_res(rec.last_node)}
            return need
        need = set()
        for i in range(rec.cur_idx, rec.base_end + 1):
            need.add(node_res(rec.route[i]))
        for i in range(rec.cur_idx, rec.base_end):
            need.add(lane_res(self.layout.edge_between(rec.route[i], rec.route[i + 1]).lane))
        left_node = rec.dist_since > max(self.cfg.clear_dist, rec.length / 2 + 0.7) and rec.cur_idx < rec.base_end
        if left_node:
            need.discard(node_res(rec.route[rec.cur_idx]))
        for idx, rs in rec.rot_zone.items():
            if rec.cur_idx <= idx <= rec.base_end and not (idx == rec.cur_idx and left_node):
                need.update(rs)
        return need

    def _occupied(self, rec: RobotRecord) -> set[str]:
        """Resources physically covered by a robot, from its reported pose.

        Within 0.9 m of a node the robot is in that node's area; a lane counts
        only when the robot is on it and clear of both end nodes, otherwise a
        robot arriving at an intersection would appear to sit on every lane
        that touches it.
        """
        occ = set()
        p = (rec.x, rec.y)
        node_r = max(0.9, rec.length / 2)
        lane_r = max(0.6, rec.width / 2 + 0.1)
        near_node = False
        for kind, ident in self._query(rec.x, rec.y, node_r + 0.1):
            if kind == "N":
                n = self.layout.nodes[ident]
                if math.hypot(n.x - rec.x, n.y - rec.y) <= node_r:
                    occ.add(node_res(ident))
                    near_node = True
        if near_node:
            return occ
        for kind, ident in self._query(rec.x, rec.y, 1.0):
            if kind == "L":
                a, b = self._lane_points(ident)
                if seg_point_dist(p, a, b) <= lane_r:
                    occ.add(lane_res(ident))
        return occ

    def _progress(self, rec: RobotRecord) -> None:
        if rec.route and rec.state_order_id == rec.order_id:
            idx = rec.last_seq // 2
            if 0 <= idx < len(rec.route) and rec.route[idx] == rec.last_node:
                rec.cur_idx = idx
                rec.base_end = max(rec.base_end, idx)
        need = self._needed(rec)
        if need is None:
            return
        if rec.mode != "AUTOMATIC" or (not rec.route and rec.last_node is None):
            # manual robots: claim what they physically cover so traffic avoids them
            self.locks.retain_only(rec.serial, need)
            for r in need:
                ok, holder, _ = self.locks.try_acquire(rec.serial, [r])
            return
        self.locks.retain_only(rec.serial, need)
        # a robot always owns the node it stands on (initial placement, after manual driving)
        for r in need:
            if self.locks.holder_of(r) is None:
                self.locks.try_acquire(rec.serial, [r])

    def _extend(self, rec: RobotRecord) -> None:
        if not rec.route or rec.mode != "AUTOMATIC" or rec.connection != "ONLINE" or rec.fatal:
            return
        if rec.last_order is None or rec.last_order["orderId"] != rec.order_id:
            if not rec.order_pending or not self.can_take_route_now(rec):
                return
        if self.cfg.strategy == "none":
            if rec.base_end < len(rec.route) - 1:
                rec.base_end = len(rec.route) - 1
                rec.order_pending = True
            return
        blocked: WaitInfo | None = None
        first = True
        while rec.base_end < len(rec.route) - 1 and rec.base_end - rec.cur_idx < self.cfg.lookahead_nodes:
            j = rec.base_end + 1
            chain = [j]
            # do not stop inside an intersection: a robot waiting there blocks crossing
            # traffic and, next to a dock or parking stub, the stub's only exit
            while (self.layout.must_pass(rec.route[chain[-1]]) and chain[-1] < len(rec.route) - 1
                   and (len(chain) < 4 or rec.route[chain[-1]] in self.layout.no_stop and len(chain) < 8)):
                chain.append(chain[-1] + 1)
            gate = self._stub_gate(rec, chain[-1])
            if gate is not None:
                blocked = self._wait(rec, self._holder_kind(gate), node_res(rec.route[-1]), gate)
                break
            resources: list[str] = []
            for k in chain:
                resources.append(lane_res(self.layout.edge_between(rec.route[k - 1], rec.route[k]).lane))
                resources.append(node_res(rec.route[k]))
            for r in resources:
                if r in self.blocks and self.locks.holder_of(r) != rec.serial:
                    kind, ref = self.blocks[r]
                    blocked = self._wait(rec, kind, r, ref)
                    break
            if blocked:
                break
            # swept area of turns: the start node (turning on the spot) and every sharp corner in the chain
            zones: dict[int, list[str]] = {}
            turn_idx = ([j - 1] if first and rec.at_base_end() and self._turns_at(rec, j - 1) else []) + \
                [k for k in chain if self._turns_at(rec, k)]
            for k in turn_idx:
                zones[k] = [r for r in self._turn_zone(rec, k) if r not in resources]
            extra = [r for rs in zones.values() for r in rs]
            ok, holder, res = self.locks.try_acquire(rec.serial, resources + extra)
            if not ok:
                blocked = self._wait(rec, self._holder_kind(holder), res, holder)
                break
            for k, rs in zones.items():
                rec.rot_zone.setdefault(k, set()).update(rs)
            first = False
            rec.base_end = chain[-1]
            rec.order_pending = True
        if blocked is None:
            rec.pending_block = None
        stationary = rec.at_base_end() and rec.base_end < len(rec.route) - 1
        rec.wait = rec.pending_block if stationary else None

    def _stub_gate(self, rec: RobotRecord, stop_idx: int) -> str | None:
        """Holder of the robot's goal stub when stopping at route[stop_idx] would block that holder's way out.

        A vehicle leaving a dead-end stub turns at the junction; a vehicle waiting for the same stub inside
        that turning circle blocks it and the two wait for each other. It waits one node earlier instead.
        """
        goal_idx = len(rec.route) - 1
        if stop_idx >= goal_idx or goal_idx - stop_idx > 3 or goal_idx < 1:
            return None
        goal = rec.route[goal_idx]
        holder = self.locks.holder_of(node_res(goal))
        if holder in (None, rec.serial) or self.layout.nodes[goal].kind not in STUB_KINDS:
            return None
        junction = self.layout.nodes[rec.route[goal_idx - 1]]
        other = self.robots.get(holder)
        reach = self._turn_reach(other) if other else 3.0
        stop = self.layout.nodes[rec.route[stop_idx]]
        return holder if math.hypot(stop.x - junction.x, stop.y - junction.y) < reach else None

    def _turn_reach(self, rec: RobotRecord) -> float:
        """Radius around a turning point inside which another vehicle's node counts as swept."""
        others = max((r.length for r in self.robots.values() if r.serial != rec.serial), default=rec.length)
        return math.hypot(rec.length, rec.width) / 2 + others / 2 + 0.2

    def _heading(self, a: str, b: str) -> float:
        na, nb = self.layout.nodes[a], self.layout.nodes[b]
        return math.atan2(nb.y - na.y, nb.x - na.x)

    def _turns_at(self, rec: RobotRecord, idx: int) -> bool:
        """Will the robot turn on the spot at route[idx]?"""
        if idx >= len(rec.route) - 1:
            return False
        out = self._heading(rec.route[idx], rec.route[idx + 1])
        if idx == 0 or idx == rec.cur_idx and rec.at_base_end():
            heading_in = rec.theta if idx == rec.cur_idx else out
        else:
            heading_in = self._heading(rec.route[idx - 1], rec.route[idx])
        d = abs((out - heading_in + math.pi) % (2 * math.pi) - math.pi)
        return d > 0.5

    def _turn_zone(self, rec: RobotRecord, idx: int) -> list[str]:
        """Nodes another vehicle could stand on inside the circle this robot sweeps when turning.

        A node on a lane is judged by its centre (a vehicle there may face any way). A vehicle standing in a
        dead-end stub lies along the stub, so a stub node counts only if that body reaches into the circle:
        two forklifts leaving neighbouring bays do not block each other.
        """
        node = self.layout.nodes[rec.route[idx]]
        radius = self._turn_reach(rec)
        sweep = math.hypot(rec.length, rec.width) / 2 + 0.2
        big = max(self.robots.values(), key=lambda r: r.length, default=rec)
        out = []
        for kind, ident in self._query(node.x, node.y, radius):
            if kind != "N" or ident == node.id:
                continue
            other = self.layout.nodes[ident]
            if math.hypot(other.x - node.x, other.y - node.y) > radius:
                continue
            if other.kind in STUB_KINDS and len(self.layout.neighbors[ident]) == 1:
                if self._stub_body_gap(ident, (node.x, node.y), big.length, big.width) >= sweep:
                    continue
            out.append(node_res(ident))
        return out

    def _stub_body_gap(self, stub: str, point: tuple[float, float], length: float, width: float) -> float:
        """Distance from ``point`` to a length x width vehicle parked on ``stub``, aligned with the stub.

        """
        n = self.layout.nodes[stub]
        j = self.layout.nodes[next(iter(self.layout.neighbors[stub]))]
        d = math.hypot(n.x - j.x, n.y - j.y) or 1.0
        ux, uy = (n.x - j.x) / d, (n.y - j.y) / d
        px, py = point[0] - n.x, point[1] - n.y
        along, across = px * ux + py * uy, -px * uy + py * ux
        return math.hypot(max(0.0, abs(along) - length / 2), max(0.0, abs(across) - width / 2))

    def _wait(self, rec: RobotRecord, kind: str, resource: str, holder: str | None) -> WaitInfo:
        pb = rec.pending_block
        if pb is None or pb.resource != resource or pb.holder != holder:
            since = pb.since if pb is not None else self.now
            rec.pending_block = WaitInfo(since, kind, resource, holder)
        else:
            pb.kind = kind
        return rec.pending_block

    def _holder_kind(self, holder: str | None) -> str:
        if holder is None:
            return "robot"
        if holder in self.external:
            return "external"
        other = self.robots.get(holder)
        if other is None:
            return "robot"
        if other.connection != "ONLINE":
            return "offline"
        if other.fatal:
            return "fault"
        if other.mode != "AUTOMATIC":
            return "manual"
        return "robot"

    # ---------------------------------------------------------------- status
    def _classify(self, rec: RobotRecord, dt: float) -> None:
        busy = rec.task_id is not None
        if rec.connection != "ONLINE" and rec.has_state:
            st, why = "OFFLINE", f"마지막 수신 {self.now - rec.last_seen:.0f}초 전"
        elif rec.fatal:
            st, why = "FAULT", rec.fatal
        elif rec.mode == "MANUAL":
            st, why = "MANUAL", "원격 조종 인계"
        elif rec.estop:
            st, why = "ESTOP", "관제 비상정지"
        elif rec.paused:
            st, why = "PAUSED", "구역 일시정지" if rec.serial in self.area_paused else "관제 일시정지"
        elif rec.safety_field == "PROTECTIVE" and not rec.driving:
            st, why = "PROTECTIVE", f"보호필드: {self._describe(rec.field_cause)}"
        elif rec.wait is not None:
            w = rec.wait
            st = "WAITING" if w.kind == "robot" else "BLOCKED"
            why = self._wait_text(w)
        elif rec.charging:
            st, why = "CHARGING", f"충전 {rec.battery:.0f}%"
        elif self._goal_action_running(rec) and not rec.driving:
            st, why = "ACTION", rec.stop_reason or "작업 동작 수행"
        elif rec.route and not self.route_done(rec):
            st, why = "MOVING", {"pickup": "적재 위치로", "dropoff": "하역 위치로", "charge": "충전기로",
                                 "park": "주차 위치로", "evade": "교착 회피 이동",
                                 "reposition": "재배치"}.get(rec.purpose, "이동")
        elif rec.last_node and self.layout.nodes[rec.last_node].kind == "park":
            st, why = "PARKED", rec.last_node
        else:
            st, why = "IDLE", ""
        rec.status, rec.reason = st, why
        if busy:
            rec.busy_s += dt
            if st in ("WAITING", "BLOCKED"):
                rec.wait_s += dt
        if rec.wait is not None and rec.last_node:
            self.congestion[rec.last_node] = self.congestion.get(rec.last_node, 0.0) + dt
            self.congestion_recent[rec.last_node] = self.congestion_recent.get(rec.last_node, 0.0) + dt

    def _describe(self, cause: str) -> str:
        kind, _, ident = cause.partition(":")
        return {"human": "사람", "forklift": "지게차", "amr": "로봇", "external": "외부 로봇"}.get(kind, kind) + \
            (f" {ident}" if ident else "")

    def _wait_text(self, w: WaitInfo) -> str:
        target = res_node(w.resource) or w.resource[2:].replace("|", "-")
        label = {"robot": "로봇", "human": "사람 예상 점유", "forklift": "지게차 예상 점유", "zone": "봉쇄 구역",
                 "door": "문 열림 대기",
                 "external": "외부 로봇", "offline": "통신 두절 로봇", "fault": "고장 로봇",
                 "manual": "원격 조종 로봇"}[w.kind]
        who = f" {w.holder}" if w.holder else ""
        return f"{target} {label}{who} · {self.now - w.since:.0f}초"

    # ---------------------------------------------------------------- deadlocks
    def _deadlocks(self) -> None:
        wait_for = {r.serial: r.wait.holder for r in self.robots.values()
                    if r.wait is not None and r.wait.kind == "robot" and r.wait.holder in self.robots}
        live: set[frozenset] = set()
        for cycle in find_cycles(wait_for):
            members = [self.robots[s] for s in cycle]
            if any(self.now - m.wait.since < self.cfg.deadlock_confirm_s for m in members):
                continue
            key = frozenset(cycle)
            live.add(key)
            info = self.deadlocks.get(key)
            if info is None:
                info = {"start": self.now, "members": cycle, "attempts": 0, "next": self.now}
                self.deadlocks[key] = info
                self.counters["deadlocks"] += 1
                nodes = [m.last_node for m in members]
                self.event("deadlock", ",".join(cycle), f"교착 탐지: {' → '.join(cycle)} @ {','.join(map(str, nodes))}",
                           "warn", members=cycle, nodes=nodes)
            if self.cfg.strategy == "locks+resolve" and self.now >= info["next"]:
                info["attempts"] += 1
                info["next"] = self.now + 2.0
                method = self._resolve(members)
                if method:
                    info["method"] = method
            if self.now - info["start"] > self.cfg.deadlock_alarm_s:
                self.alarms.set("DEADLOCK_UNRESOLVED", ",".join(sorted(cycle)), True, self.now,
                                f"{len(cycle)}대 교착 {self.now - info['start']:.0f}초 · {' → '.join(cycle)}",
                                {"members": cycle})
        for key in list(self.deadlocks):
            if key not in live:
                info = self.deadlocks.pop(key)
                duration = self.now - info["start"]
                self.counters["deadlock_s"] += duration
                if info.get("method"):
                    self.counters["deadlocks_resolved"] += 1
                    self.event("deadlock_resolved", ",".join(info["members"]),
                               f"교착 해소({info['method']}) {duration:.1f}초", "info")
                self.alarms.set("DEADLOCK_UNRESOLVED", ",".join(sorted(info["members"])), False, self.now)

    def _resolve(self, members: list[RobotRecord]) -> str | None:
        candidates = sorted(members, key=lambda r: (self._priority(r) - (self.now - r.wait.since) / 2.0,
                                                    r.loaded, r.serial))
        held_by_others = {}
        for m in members:
            held_by_others[m.serial] = {res_node(r) for o in members if o is not m
                                        for r in self.locks.resources_of(o.serial) if res_node(r)}
        for victim in candidates:
            avoid = set(held_by_others[victim.serial])
            blocked_node = res_node(victim.wait.resource)
            if blocked_node:
                avoid.add(blocked_node)
            avoid.discard(victim.last_node)
            if victim.goal in avoid:
                continue
            if self._replan(victim, avoid):
                self.counters["reroutes"] += 1
                self.event("reroute", victim.serial, "교착 해소: 우회 경로", "info")
                return "우회"
        for victim in candidates:
            forbidden = set(held_by_others[victim.serial])
            for o in members:
                if o is not victim:
                    forbidden.update(o.route[o.cur_idx:o.cur_idx + 4])
            spot = self._evasion_spot(victim, forbidden, impassable=held_by_others[victim.serial])
            if spot:
                resume = (victim.goal, victim.purpose, victim.goal_actions.get(len(victim.route) - 1, []),
                          victim.task_id)
                if self.assign_route(victim, spot, "evade", task_id=victim.task_id):
                    victim.resume = resume
                    self.counters["evasions"] += 1
                    self.event("evade", victim.serial, f"교착 해소: {spot}로 비켜서기", "info")
                    return "비켜서기"
        return None

    def _replan(self, rec: RobotRecord, avoid: set[str], avoid_lanes: set[str] | None = None,
                strict_doors: bool = False) -> bool:
        goal = rec.goal
        actions = rec.goal_actions.get(len(rec.route) - 1, [])
        purpose, task, resume = rec.purpose, rec.task_id, rec.resume
        old_route = list(rec.route)
        old_cur = rec.cur_idx
        old = (rec.order_id, rec.update_id, rec.base_end, rec.cur_idx, rec.sent_base_end, rec.last_order,
               dict(rec.goal_actions), rec.order_pending, rec.pending_block, rec.wait)
        if not self.assign_route(rec, goal, purpose, actions, task, avoid_nodes=avoid,
                                 avoid_lanes=avoid_lanes, strict_doors=strict_doors):
            return False
        if rec.route == old_route[old_cur:] or len(rec.route) < 2:
            rec.route = old_route
            (rec.order_id, rec.update_id, rec.base_end, rec.cur_idx, rec.sent_base_end, rec.last_order,
             rec.goal_actions, rec.order_pending, rec.pending_block, rec.wait) = old
            return False
        rec.resume = resume
        return True

    def _evasion_spot(self, rec: RobotRecord, forbidden_spots: set,
                      impassable: set | None = None) -> str | None:
        """Nearest refuge reachable over free nodes.

        ``forbidden_spots`` may be driven through but not stopped at (the other
        robots' next nodes); ``impassable`` and nodes held by anyone else cannot
        be entered at all.
        """
        start = rec.last_node
        frontier = [(start, 0)]
        seen = {start}
        best = None
        blocked = impassable or set()
        while frontier:
            cur, depth = frontier.pop(0)
            if depth >= 6:
                continue
            for e in self.layout.out_edges[cur]:
                n = e.end
                if n in seen or n in blocked or not self.layout.allowed(e, rec.vclass):
                    continue
                seen.add(n)
                if self.locks.holder_of(node_res(n)) not in (None, rec.serial) or node_res(n) in self.blocks:
                    continue
                if self.locks.holder_of(lane_res(e.lane)) not in (None, rec.serial):
                    continue
                kind = self.layout.nodes[n].kind
                if kind in ("inbound", "outbound", "charger"):
                    continue
                if kind == "park":
                    # a free dead-end parking stub is the best refuge; skip ones promised to a robot
                    owners = self.dispatcher.park_owner if self.dispatcher is not None else {}
                    if owners.get(n, rec.serial) == rec.serial and n not in forbidden_spots:
                        return n
                    continue
                if not self.layout.must_pass(n) and n != rec.goal and n not in forbidden_spots:
                    best = n
                    return best
                frontier.append((n, depth + 1))
        return best

    def _reroute_blocked(self) -> None:
        for rec in self.robots.values():
            w = rec.wait
            if w is None or self.now < rec.reroute_next:
                continue
            if w.kind == "door":
                # a door is judged by its supervisor (open request timeout, silence, operator):
                # wait while it may still open, go round as soon as it is declared unusable
                if self.doors is None or self.doors.available(w.holder, self.now):
                    continue
            elif self.now - w.since < self.cfg.reroute_after_s:
                continue
            if w.kind == "robot":
                continue   # handled by deadlock logic or just normal queueing
            rec.reroute_next = self.now + 10.0
            avoid: set[str] = set()
            avoid_lanes: set[str] = set()
            n = res_node(w.resource)
            if n:
                avoid.add(n)
            else:
                avoid_lanes.add(w.resource[2:])
            if w.holder and w.holder in self.robots:
                avoid.update(res_node(r) for r in self.locks.resources_of(w.holder) if res_node(r))
            avoid.discard(rec.last_node)
            avoid = {a for a in avoid if a}
            if rec.goal in avoid:
                continue
            if self._replan(rec, avoid, avoid_lanes, strict_doors=w.kind == "door"):
                self.counters["reroutes"] += 1
                self.event("reroute", rec.serial, f"우회: {self._wait_text(w)}", "info")

    def _yield_to_uncontrolled(self) -> None:
        """Step aside when an uncommandable unit is stuck in front of a stationary robot.

        Forklifts and the external robot have right of way: we cannot command
        them, so a standoff is only broken by moving our own robot.
        """
        if self.cfg.strategy == "none":
            return
        stalled_ext = {k: v for k, v in self.external.items() if self.now - v.get("moved", self.now) > 4.0}
        stalled_fl = [tr for tr in self.tracker.tracks.values() if tr.cls == "forklift" and tr.speed < 0.2]
        for rec in self.robots.values():
            if self.now < rec.yield_next or not self.can_take_route(rec):
                continue
            here = rec.last_node
            forbidden: set[str] = set()
            reason = ""
            for ext_id, info in stalled_ext.items():
                if here in info["itinerary"][:3]:
                    forbidden.update(info["itinerary"])
                    reason = f"외부 로봇 {ext_id} 진로"
            w = rec.wait
            if w is not None and w.kind == "forklift" and self.now - w.since > 4.0:
                for tr in stalled_fl:
                    if dist((tr.x, tr.y), (rec.x, rec.y)) < 4.5:
                        forbidden.update(n for n in self.layout.nodes
                                         if dist(self.layout.nodes[n].pos, (tr.x, tr.y)) < 4.5)
                        reason = f"지게차 {tr.id} 대치"
            if not forbidden:
                continue
            rec.yield_next = self.now + 8.0
            spot = self._evasion_spot(rec, forbidden, impassable=forbidden)
            if spot is None:
                continue
            resume = None
            if rec.route and rec.goal:
                resume = (rec.goal, rec.purpose, rec.goal_actions.get(len(rec.route) - 1, []), rec.task_id)
            if self.assign_route(rec, spot, "evade", task_id=rec.task_id):
                rec.resume = resume
                self.counters["evasions"] += 1
                self.event("yield", rec.serial, f"양보 회피: {reason} → {spot}", "info")

    def _nudge_idle_blockers(self) -> None:
        if self.dispatcher is None:
            return
        for rec in self.robots.values():
            w = rec.wait
            if w is None or w.kind != "robot" or self.now - w.since < 3.0:
                continue
            other = self.robots.get(w.holder)
            if other and not other.task_id and other.purpose in ("", "park") and not other.route \
                    and not other.charging and self.can_take_route(other):
                if self.dispatcher.send_away(other, exclude={rec.goal} | set(rec.route[rec.cur_idx:])):
                    self.counters["nudges"] += 1
                    self.event("nudge", other.serial, f"{rec.serial} 진로 확보를 위해 이동", "info")

    # ---------------------------------------------------------------- safety monitor
    def _monitor_occupancy(self) -> None:
        if self.cfg.strategy == "none":
            return
        for rec in self.robots.values():
            if rec.connection != "ONLINE" or not rec.has_state or not rec.positioned:
                rec.mismatch_since = None
                continue
            occ = self._occupied(rec)
            held = set(self.locks.resources_of(rec.serial))
            if rec.mode == "MANUAL":
                foreign = {r for r in occ if self.locks.holder_of(r) not in (None, rec.serial)}
                self.alarms.set("RESERVATION_MISMATCH", rec.serial, bool(foreign), self.now,
                                f"원격 조종 {rec.serial}가 예약 구간 진입: {', '.join(sorted(foreign))}")
                continue
            foreign = {r for r in occ if r not in held and self.locks.holder_of(r) is not None}
            if foreign:
                if rec.mismatch_since is None:
                    rec.mismatch_since = self.now
                elif self.now - rec.mismatch_since >= self.cfg.mismatch_confirm_s:
                    a = self.alarms.alarms.get(f"RESERVATION_MISMATCH:{rec.serial}")
                    if a is None or not a.active:
                        self.counters["mismatch"] += 1
                        self.robot_paused.add(rec.serial)
                        self.event("mismatch", rec.serial,
                                   f"점유-예약 불일치: {', '.join(sorted(foreign))[:60]}", "crit")
                    self.alarms.set("RESERVATION_MISMATCH", rec.serial, True, self.now,
                                    f"{rec.serial} 점유 {', '.join(sorted(foreign))} (다른 유닛 예약)",
                                    {"resources": sorted(foreign)})
            else:
                rec.mismatch_since = None
                self.alarms.set("RESERVATION_MISMATCH", rec.serial, False, self.now)

    def _near_misses(self) -> None:
        for rec in self.robots.values():
            if abs(rec.v) < 0.1 or rec.connection != "ONLINE":
                continue
            for tr in self.tracker.tracks.values():
                if tr.cls != "human":
                    continue
                if abs(tr.x - rec.x) > 3 or abs(tr.y - rec.y) > 3:
                    continue
                gap = box_gap((tr.x, tr.y), 0.3, (rec.x, rec.y), rec.theta, rec.length / 2, rec.width / 2)
                if near_miss(abs(rec.v), gap, margin=0.1):
                    key = rec.serial
                    if self.now - self._nm_last.get(key, -1e9) > 20.0:
                        self._nm_last[key] = self.now
                        self.counters["near_miss"] += 1
                        self.alarms.set("NEAR_MISS", key, True, self.now,
                                        f"{rec.serial} {abs(rec.v):.1f} m/s 주행 중 사람 {max(gap, 0):.2f} m",
                                        {"x": round(rec.x, 1), "y": round(rec.y, 1)})
                        self.alarms.set("NEAR_MISS", key, False, self.now)

    def wait_chains(self) -> dict[str, list[str]]:
        """Root robot -> robots queued behind it (directly or transitively)."""
        roots: dict[str, list[str]] = {}
        for rec in self.robots.values():
            seen = [rec.serial]
            cur = rec
            while cur.wait is not None and cur.wait.kind == "robot" and cur.wait.holder in self.robots \
                    and cur.wait.holder not in seen:
                seen.append(cur.wait.holder)
                cur = self.robots[cur.wait.holder]
            if cur.wait is not None and cur.wait.kind == "robot" and cur.wait.holder in seen:
                continue   # deadlock cycle: handled by the deadlock alarm
            roots.setdefault(seen[-1], [])
            if len(seen) > 1:
                roots[seen[-1]].append(rec.serial)
        return roots

    def _robot_alarms(self) -> None:
        waiting = 0
        active = 0
        chains = self.wait_chains()
        for rec in self.robots.values():
            if rec.connection == "ONLINE":
                active += 1
            w = rec.wait
            if w is not None:
                waiting += 1
            behind = chains.get(rec.serial, [])
            # waiting behind a faulted, silent or hand-driven vehicle or a failed door is a consequence of
            # that unit's own alarm (ROBOT_FAULT, COMMS_LOST, operator takeover, FACILITY_FAULT): suppressed
            covered = w is not None and w.kind == "door" and self.alarms.is_active(f"FACILITY_FAULT:{w.holder}")
            root_stopped = w is not None and w.kind not in ("robot", "fault", "offline", "manual") and not covered or (
                w is None and bool(behind) and rec.status in ("PROTECTIVE", "FAULT", "PAUSED", "OFFLINE", "MANUAL"))
            since = w.since if w is not None else (rec.protective_since or rec.fault_since or self.now)
            limit = self.cfg.blocked_alarm_s
            if (w is not None and w.kind == "human" or rec.field_cause.startswith("human")) and \
                    any(z.kind == "picking" for z in self.layout.zones_at((rec.x, rec.y))):
                limit = self.cfg.collab_blocked_alarm_s
            long_block = root_stopped and self.now - since >= limit
            note = ""
            if long_block and w is not None and w.kind == "door" and self.doors is not None \
                    and self.doors.view[w.holder].disabled:
                note = " · 문 사용 중지 중(구역 제어 탭에서 재시도)"
            self.alarms.set("BLOCKED_LONG", rec.serial, long_block, self.now,
                            f"{rec.serial} {rec.reason} · 뒤 {len(behind)}대 대기{note}" if long_block else "",
                            {"behind": behind})
            self.alarms.set("ZONE_LOCK_WAITING", rec.serial, w is not None and w.kind == "zone"
                            and self.now - w.since > 10.0, self.now,
                            f"{rec.serial} 봉쇄 구역 {w.holder or ''} 앞 대기" if w else "")
            if rec.status == "PROTECTIVE":
                rec.protective_since = rec.protective_since if rec.protective_since is not None else self.now
            else:
                rec.protective_since = None
            standoff = rec.protective_since is not None and self.now - rec.protective_since >= 20.0
            self.alarms.set("STANDOFF", rec.serial, standoff, self.now,
                            f"{rec.serial} {rec.reason} {self.now - (rec.protective_since or self.now):.0f}초"
                            if standoff else "")
            crit = rec.has_state and rec.battery < 10.0 and not rec.charging
            self.alarms.set("BATTERY_CRITICAL", rec.serial, crit, self.now,
                            f"{rec.serial} 배터리 {rec.battery:.0f}%" if crit else "")
        ratio = waiting / max(1, active)
        # deadband and on-delay (ISA-18.2 against chattering): raise above 35 % held for 60 s,
        # clear only below 25 %
        high = ratio > 0.35 and active >= 5
        raised = self.alarms.is_active("CONGESTION:fleet")
        if high:
            self._congested_since = self._congested_since if self._congested_since is not None else self.now
        elif not raised or ratio < 0.25:
            self._congested_since = None
        on = self._congested_since is not None and (self.now - self._congested_since >= 60.0 or raised)
        self.alarms.set("CONGESTION", "fleet", on, self.now, f"대기 로봇 {waiting}/{active}대 ({ratio:.0%})")

    # ---------------------------------------------------------------- zones & fleet mode
    def _zone_speeds(self) -> None:
        if not self.cfg.human_zones:
            return
        humans = [tr for tr in self.tracker.tracks.values() if tr.cls == "human"]
        desired: dict[str, float | None] = {}
        for z in self.layout.zones.values():
            limit = self.zone_speed_override.get(z.id)
            if z.human_speed is not None:
                # without the infrastructure feed assume people may be anywhere in their zones
                inside = self.infra_lost or any(point_in_polygon((tr.x, tr.y), z.polygon) for tr in humans)
                if inside:
                    self.zone_human_active[z.id] = self.now
                if self.now - self.zone_human_active.get(z.id, -1e9) < 5.0:
                    limit = z.human_speed if limit is None else min(limit, z.human_speed)
            desired[z.id] = limit
        rebroadcast = self.now - self._last_zone_rebroadcast > 10.0
        for zid, limit in desired.items():
            if self._zone_broadcast.get(zid, None) != limit or (rebroadcast and limit is not None):
                if self._zone_broadcast.get(zid, None) != limit:
                    self.event("zone_speed", zid, f"{self.layout.zones[zid].name} 속도 제한 "
                               f"{'해제' if limit is None else f'{limit:.1f} m/s'}", "info")
                self._zone_broadcast[zid] = limit
                for rec in self.robots.values():
                    self._instant(rec, [action("x_fleetSpeedLimit", self._aid("x_speed"), "NONE",
                                               {"zoneId": zid, "limit": limit})])
        if rebroadcast:
            self._last_zone_rebroadcast = self.now

    def active_zone_limits(self) -> dict[str, float]:
        return {k: v for k, v in self._zone_broadcast.items() if v is not None}

    def _enforce_fleet_mode(self) -> None:
        for rec in self.robots.values():
            if rec.connection != "ONLINE" or not rec.has_state:
                continue
            want_estop = self.mode == "ESTOP"
            want_pause = (self.mode == "HOLD" or rec.serial in self.area_paused
                          or rec.serial in self.robot_paused)
            if rec.estop != want_estop and self.now - rec.mode_sent_at > 1.0:
                self._instant(rec, [action("x_remoteStop", self._aid("x_estop"), "HARD", {"active": want_estop})])
                rec.mode_sent_at = self.now
            if rec.paused != want_pause and self.now - rec.pause_sent_at > 1.0:
                kind = "startPause" if want_pause else "stopPause"
                self._instant(rec, [action(kind, self._aid("pause" if want_pause else "resume"), "HARD")])
                rec.pause_sent_at = self.now

    # ---------------------------------------------------------------- views
    def link_quality(self, rec: RobotRecord) -> tuple[int | None, int | None]:
        """What the control room can measure about a vehicle's link without its Wi-Fi driver.

        Returns (received share of the expected state + visualization messages in the last
        LINK_WINDOW_S, in %, and the median one-way delay in ms). The expected rate comes from
        the factsheet's protocolLimits.timing, so a vehicle that publishes less often by design is
        not reported as a weak link.
        """
        timing = ((rec.factsheet or {}).get("protocolLimits") or {}).get("timing") or {}
        state_s = timing.get("defaultStateInterval") or 1.0
        vis_s = timing.get("visualizationInterval") or 0.2
        rate = 1.0 / max(0.05, float(state_s)) + 1.0 / max(0.05, float(vis_s))
        recent = [lat for t, lat in rec.rx if t > self.now - LINK_WINDOW_S]
        if self.now < LINK_WINDOW_S:
            return None, None
        share = min(100, round(100 * len(recent) / (rate * LINK_WINDOW_S)))
        delay = round(1000 * sorted(recent)[len(recent) // 2]) if recent else None
        return share, delay

    def robot_view(self, rec: RobotRecord) -> dict:
        base = rec.route[rec.cur_idx:rec.base_end + 1] if rec.route else []
        horizon = rec.route[rec.base_end + 1:rec.base_end + 1 + self.cfg.horizon_nodes] if rec.route else []
        link, lat_ms = self.link_quality(rec)
        return {
            "id": rec.serial, "cls": rec.vclass, "len": rec.length, "wid": rec.width,
            "x": round(rec.x, 2), "y": round(rec.y, 2), "th": round(rec.theta, 2),
            "v": round(rec.v, 2), "st": rec.status, "label": STATUS_LABEL.get(rec.status, rec.status),
            "reach": rec.reach,
            "why": rec.reason, "bat": round(rec.battery, 1), "chg": rec.charging, "mode": rec.mode,
            "conn": rec.connection, "link": link, "lat_ms": lat_ms, "field": rec.safety_field, "fault": rec.fatal, "load": rec.loaded,
            "task": rec.task_id, "purpose": rec.purpose, "goal": rec.goal, "node": rec.last_node,
            "base": base, "hz": horizon, "route": rec.route[rec.cur_idx:] if rec.route else [],
            "wait_s": round(self.now - rec.wait.since, 1) if rec.wait else 0.0,
            "wait_kind": rec.wait.kind if rec.wait else None,
            "wait_holder": rec.wait.holder if rec.wait else None,
            "locks": len(self.locks.resources_of(rec.serial)),
            "order": rec.order_id, "upd": rec.update_id, "age": round(self.now - rec.last_seen, 1),
            "stop": rec.stop_reason, "positioned": rec.positioned,
        }

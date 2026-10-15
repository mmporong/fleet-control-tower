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
from .clock import Clock, SimClock
from .facility import DoorSupervisor
from .geometry import bbox, dist, path_gap, point_in_polygon, seg_point_dist
from .layout import Layout, Zone
from .perception import Detection, Tracker
from .planner import STUB_KINDS, Planner
from .traffic import LockTable, WaitInfo, find_cycles, lane_res, node_res, res_node
from .vda5050 import HeaderIds, RouteEdge, RouteNode, action, build_instant_actions, build_order, header, topic

STRATEGIES = ("none", "locks", "locks+resolve")
SWING_RADIUS = 1.9           # half the length of a 3.6 m counterbalance truck plus margin: its turning circle
CLEARANCE_TURN = 1.0         # beyond the circle: half a robot body plus margin, so no part of a robot is inside
# a truck backs off up to 6 m (radioed) to let a robot out of its turning circle; plus the tracking error
BACKED_OFF_M = 7.0
DRIVE_PACE = 1.1             # m/s: faster than a truck backs off (0.5) or drives back up (0.8), slower than it drives
LINK_WINDOW_S = 5.0          # window for the measured link quality shown per vehicle


CLEARANCE_M = 0.5        # ISO 3691-4 Annex A (via TUV whitepaper): clearance a vehicle above 0.3 m/s must keep
CREEP_SPEED = 0.3        # at or below this speed reduced clearance is allowed (operating hazard zone)
CLOSE_CALL_M = 0.2       # proximity measure: a moving vehicle came this close to a person
NM_TRACK_MARGIN = 0.3    # control-room near miss: estimated gap must be this far inside the stopping envelope
COLLAB_HOLD_M = 0.5      # in a collaborative picking zone a person holds a robot stop only when standing this close


def clearance_violation(speed: float, gap: float, margin: float = 0.0) -> bool:
    """A compliance check, not an outcome: faster than creep speed with less than the required clearance.

    ``margin`` tightens the gap threshold when positions are noisy estimates (tracks, not ground truth).

    >>> clearance_violation(1.0, 0.45), clearance_violation(0.3, 0.15), clearance_violation(1.0, 0.6)
    (True, False, False)
    """
    return speed > CREEP_SPEED + 1e-6 and gap < CLEARANCE_M - margin


def close_call(speed: float, gap: float) -> bool:
    """Raw proximity: the vehicle was moving and within 20 cm of a person. In a 2 m pick aisle every pass of a
    picker at the shelf face (15 cm) counts, so it describes how close the work is, not how unsafe it was.

    >>> close_call(0.2, 0.15), close_call(0.0, 0.05), close_call(1.0, 0.3)
    (True, False, False)
    """
    return speed > 0.05 and gap < CLOSE_CALL_M


def stop_envelope(speed: float, decel: float, reaction_s: float = 0.2, margin: float = 0.05) -> float:
    """Distance a vehicle needs to stop from ``speed``: reaction travel plus braking, plus a margin.

    >>> round(stop_envelope(1.0, 1.5), 2), round(stop_envelope(0.3, 1.5), 2)
    (0.58, 0.14)
    """
    return speed * reaction_s + speed * speed / (2.0 * decel) + margin


def envelope_intrusion(speed: float, gap: float | None, decel: float) -> bool:
    """The outcome measure of a near miss: a person in the vehicle's path (``gap`` from ``path_gap``, None when
    not in it) was inside the distance the moving vehicle needed to stop, so a step toward it could not have
    been avoided by braking. Physics, not a site speed rule, sets the line.

    >>> envelope_intrusion(1.0, 0.45, 1.5), envelope_intrusion(0.3, 0.15, 1.5), envelope_intrusion(1.0, None, 1.5)
    (True, False, False)
    """
    return speed > 0.05 and gap is not None and gap < stop_envelope(speed, decel)


def near_miss(speed: float, gap: float | None, margin: float = 0.0, decel: float = 1.5) -> bool:
    """What the control room flags from its (noisy) tracks: a person in the path inside the stopping envelope,
    with ``margin`` taken off the gap threshold for the track's position uncertainty."""
    return speed > 0.05 and gap is not None and gap < stop_envelope(speed, decel) - margin
STATUS_LABEL = {
    "MOVING": "주행", "WAITING": "교통 대기", "BLOCKED": "차단 대기", "ACTION": "작업 동작",
    "CHARGING": "충전", "IDLE": "대기", "PARKED": "주차", "PAUSED": "일시정지", "ESTOP": "일괄 정지",
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
    # waits behind manual trucks at docks and aisle mouths last a minute or two in normal mixed traffic: an
    # alarm (operator action) only past that; collaborative picking waits for people by design, longer still
    blocked_alarm_s: float = 120.0
    collab_blocked_alarm_s: float = 300.0
    # work released to the picking hall (CONWIP): one cap over the whole hall, this many tote trips per pick
    # aisle (a trip visits several aisles); the extra tote AMRs stay parked. Without it they queue on the tote
    # spine and the main aisles the forklifts use for a hall that cannot take them yet (0: off; swept in the
    # benchmark)
    pick_aisle_capacity: int = 3
    standoff_alarm_s: float = 60.0      # protective stop lasting this long: someone has to clear the vehicle's way
    comms_timeout_s: float = 3.0       # silence after which the vehicle counts as CONNECTIONBROKEN (alert)
    comms_alarm_s: float = 30.0        # silence that needs the operator (alarm); tasks are reassigned then too
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
    estop_local: bool = False
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
    state_has_pose: bool = False          # the latest state itself carried agvPosition
    action_results: dict = field(default_factory=dict)   # actionId -> resultDescription of failed actions
    factsheet_asked: float = -1e9
    swing_since: float | None = None      # standing inside a stopped forklift's swept circle since
    head_since: float | None = None       # at the head of a queue (others wait behind it) since
    escape_from: str | None = None        # track id of the truck whose circle this vehicle is leaving
    prev_speed: float = 0.0               # speed before the latest protective stop
    state_nodes: list = field(default_factory=list)      # (nodeId, sequenceId, released) still ahead
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
    manual_handoff_at: float = -1e9
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
                 manufacturer: str = "sim", clock: Clock | None = None, validator=None) -> None:
        """``validator(kind, msg) -> list[str]`` checks inbound VDA 5050 messages (official JSON schemas in the
        runtime); ``clock`` turns ``now`` into message timestamps and back."""
        if config and config.strategy not in STRATEGIES:
            raise ValueError(config.strategy)
        self.layout = layout
        self.bus = bus
        self.cfg = config or ManagerConfig()
        self.manufacturer = manufacturer
        self.clock = clock or SimClock()
        self.validator = validator
        self._invalid_logged: dict = {}
        # runtime: prefixes order and action ids so a restarted manager never reuses an id a vehicle has seen
        self.run_id = ""
        self.resync_until: float | None = None       # RESYNC mode: rebuilding the picture after a restart
        self._restored: dict[str, dict] = {}         # serial -> snapshot waiting for the vehicle's first state
        self.on_order = None                          # callback(rec) just before an order is published
        self.cancel_pending: dict[str, tuple[str, float]] = {}   # serial -> (canceled orderId, when sent)
        self.cancel_action: dict[str, str] = {}        # serial -> latest cancelOrder actionId awaiting confirmation
        self._standoff_recovery: dict[str, dict] = {}  # serial -> task/order data kept across a confirmed cancel
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
        self.block_src: dict[str, str] = {}
        self.aisle_requests: dict[str, tuple[str, str]] = {}
        self.external: dict[str, dict] = {}
        self.mode = "RUNNING"           # RUNNING | HOLD | ESTOP | FIRE | RESYNC
        # the fire panel's alarm, kept apart from the mode: a fleet stop (ESTOP) stays in force through it and
        # a restart finishes its resync under it; FIRE is the mode only when nothing stronger is active
        self.fire_alarm = False
        self.area_paused: set[str] = set()
        self.robot_paused: set[str] = set()
        self.repair_times: list[float] = []
        self._nm_last: dict[str, float] = {}
        self._nm_pending: dict[tuple[str, str], float] = {}     # person in an envelope -> the observation time
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
                         "nudges": 0, "mismatch": 0, "comms_lost": 0, "invalid_msgs": 0}
        self._grid = self._build_resource_grid()
        # every manufacturer on the site: "+" in the manufacturer level (orders go back under each one's own)
        bus.subscribe("uagv/v2/+/+/state", self._on_state)
        bus.subscribe("uagv/v2/+/+/visualization", self._on_vis)
        bus.subscribe("uagv/v2/+/+/connection", self._on_connection)
        bus.subscribe("uagv/v2/+/+/factsheet", self._on_factsheet)
        bus.subscribe("facility/v1/fire/state", self._on_fire)
        self.doors = DoorSupervisor(layout.doors, bus, self.alarms, self.event) if layout.doors else None
        self._zone_nodes: dict[str, list[str]] = {}
        for nid, zones in layout.node_zones.items():
            for zid in zones:
                self._zone_nodes.setdefault(zid, []).append(nid)
        self._edge_zone_cache: dict[str, list[str]] = {}
        self._fl_still_since: dict[str, float] = {}
        # truck track -> the intersection it last stood on; dropped once it drives off at driving pace
        self._fl_stood: dict[str, str] = {}
        self._field_since: dict[str, float] = {}     # robot -> protective-stopped facing another robot since
        # aisles a manual truck may work: the rack aisles (pick aisles are tote-AMR and picker space)
        self._aisle_zones = [z for z in layout.zones.values() if z.kind == "aisle"
                             and any(self.layout.edges[e].classes and "CARRIER" not in self.layout.edges[e].classes
                                     for nid in self._zone_nodes.get(z.id, ()) for e in
                                     [x.id for x in layout.out_edges[nid]])]

    # ================================================================ events
    def event(self, kind: str, source: str, msg: str, level: str = "info", **data) -> None:
        self.events.append({"t": round(self.now, 1), "type": kind, "source": source, "msg": msg,
                            "level": level, **data})

    # ================================================================ inbound
    def register(self, serial: str, manufacturer: str | None = None) -> RobotRecord:
        """Record of a vehicle; serial numbers are assumed unique across manufacturers on one site."""
        rec = self.robots.setdefault(serial, RobotRecord(serial, manufacturer or self.manufacturer))
        if manufacturer:
            rec.manufacturer = manufacturer
        return rec

    def _serial(self, t: str) -> str:
        return t.split("/")[3]

    def _rec(self, t: str) -> RobotRecord:
        """Record for a message topic uagv/v2/<manufacturer>/<serial>/<name> (several vendors share a site)."""
        parts = t.split("/")
        return self.register(parts[3], parts[2])

    def _on_connection(self, t: str, msg: dict) -> None:
        if not self._accept(t, "connection", msg):
            return
        rec = self._rec(t)
        rec.connection = msg["connectionState"]
        rec.last_seen = self.now

    def _on_factsheet(self, t: str, msg: dict) -> None:
        if not self._accept(t, "factsheet", msg):
            return
        rec = self._rec(t)
        rec.factsheet = msg
        rec.vclass = msg["typeSpecification"]["agvClass"]
        rec.length = float(msg["physicalParameters"]["length"])
        rec.width = float(msg["physicalParameters"]["width"])

    def _on_fire(self, _t: str, msg: dict) -> None:
        """Fire panel. On alarm nothing new is released or assigned: every vehicle stops at the end of what
        it already has, which is never a door threshold or an intersection, so fire doors can close."""
        alarm = bool(msg.get("alarm"))
        if alarm and not self.fire_alarm:
            self.fire_alarm = True
            if self.mode in ("RUNNING", "HOLD"):
                self.mode = "FIRE"
            self.event("fire", "facility", "화재 경보: 신규 해제·배정 중단, 차량은 해제 구간 끝에서 정지", "crit")
        elif not alarm and self.fire_alarm:
            self.fire_alarm = False
            if self.mode == "FIRE":
                self.mode = "HOLD"       # all clear: the operator resumes once the site is checked
            self.event("fire_clear", "facility", "화재 경보 해제: 전체 일시정지 유지, 현장 확인 후 재개", "warn")
        self.alarms.set("FIRE_ALARM", "facility", alarm, self.now, "화재 경보 수신" if alarm else "")

    def supports(self, rec: RobotRecord, action_type: str) -> bool:
        """What the factsheet declares. Without a factsheet only the standard actions are assumed."""
        if not rec.factsheet:
            return not action_type.startswith("x_")
        feats = (rec.factsheet.get("protocolFeatures") or {}).get("agvActions") or []
        return any(a.get("actionType") == action_type for a in feats)

    def _accept(self, t: str, kind: str, msg) -> bool:
        """Inbound gate: a malformed message is logged and dropped, never allowed to break the manager."""
        problems = self.validator(kind, msg) if self.validator is not None else []
        if not isinstance(msg, dict):
            problems = problems or ["not a JSON object"]
        if problems:
            self.counters["invalid_msgs"] += 1
            serial = t.split("/")[3] if t.count("/") >= 4 else t
            key = (serial, kind)
            if self.now - self._invalid_logged.get(key, -1e9) > 30.0:
                self._invalid_logged[key] = self.now
                self.event("invalid_msg", serial, f"{kind} 메시지 형식 오류로 폐기: {problems[0][:80]}", "warn")
            return False
        return True

    def _on_vis(self, t: str, msg: dict) -> None:
        if not self._accept(t, "visualization", msg):
            return
        rec = self._rec(t)
        p = msg.get("agvPosition")            # optional in VDA 5050: without it the last pose stays
        if p:
            rec.x, rec.y, rec.theta = p["x"], p["y"], p["theta"]
        vel = msg.get("velocity")
        if vel is not None:
            rec.v = vel.get("vx", 0.0)
        self._seen(rec, msg)

    def _seen(self, rec: RobotRecord, msg: dict | None = None) -> None:
        if msg is not None:
            received = max(self.now, self.bus.now)
            sent = self.clock.parse(msg.get("timestamp", "")) if msg.get("timestamp") else None
            rec.rx.append((received, max(0.0, received - sent) if sent is not None else 0.0))
        if rec.connection == "CONNECTIONBROKEN":
            self.event("comms_restored", rec.serial, "통신 복구", "info")
            self.alarms.set("COMMS_LOST", rec.serial, False, self.now)
            self.alarms.set("COMMS_DEGRADED", rec.serial, False, self.now)
        rec.connection = "ONLINE"
        rec.last_seen = self.now

    def _on_state(self, t: str, msg: dict) -> None:
        if not self._accept(t, "state", msg):
            return
        rec = self._rec(t)
        self._seen(rec, msg)
        rec.has_state = True
        # optional fields (agvPosition, velocity, loads, paused, distanceSinceLastNode, safetyState parts) keep
        # a safe default when a vehicle leaves them out; the schema gate already rejected malformed ones
        p = msg.get("agvPosition")
        rec.state_has_pose = bool(p)
        if p:
            rec.x, rec.y, rec.theta = p["x"], p["y"], p["theta"]
            rec.positioned = bool(p.get("positionInitialized", True))
        vel = msg.get("velocity")
        rec.v = vel.get("vx", 0.0) if vel else 0.0
        bat = msg.get("batteryState") or {}
        rec.battery = bat.get("batteryCharge", rec.battery)
        rec.reach = bat.get("reach")
        rec.charging = bool(bat.get("charging", False))
        rec.mode = msg.get("operatingMode", rec.mode)
        rec.paused = bool(msg.get("paused", False))
        safety = msg.get("safetyState") or {}
        estop = safety.get("eStop", "NONE")
        rec.estop = estop == "REMOTE"                       # the fleet's own remote stop
        rec.estop_local = estop not in ("NONE", "REMOTE")   # pressed at the vehicle: shown, never overridden
        prev_field = rec.safety_field
        # the standard flag decides; a vendor extension may add the warning state and what caused it
        ext = msg.get("x_safetyField")
        ext = ext if isinstance(ext, dict) else {}
        if safety.get("fieldViolation", False):
            rec.safety_field = "PROTECTIVE"
        elif ext.get("state") == "WARNING":
            rec.safety_field = "WARNING"
        else:
            rec.safety_field = "CLEAR"
        rec.field_cause = str(ext.get("cause") or "")
        driving = bool(msg.get("driving", False))
        if rec.safety_field == "PROTECTIVE" and prev_field != "PROTECTIVE" and driving:
            self.counters["protective_stops"] += 1
            if rec.field_cause.startswith("human"):
                self.counters["protective_human"] += 1
        rec.last_node = msg.get("lastNodeId") or None
        rec.last_seq = msg.get("lastNodeSequenceId", 0)
        rec.dist_since = float(msg.get("distanceSinceLastNode") or 0.0)
        rec.driving = driving
        rec.loaded = bool(msg.get("loads"))
        rec.state_order_id = msg.get("orderId", "")
        rec.state_update_id = msg.get("orderUpdateId", 0)
        rec.detections = msg.get("x_detections", [])
        info = msg.get("information") or []
        rec.stop_reason = info[0].get("infoDescription", "") if info else ""
        for a in msg.get("actionStates") or []:
            rec.actions[a["actionId"]] = a["actionStatus"]
            if a["actionStatus"] == "FAILED":
                rec.action_results[a["actionId"]] = a.get("resultDescription", "") or ""
        if len(rec.actions) > 200:
            for k in list(rec.actions)[:-100]:
                del rec.actions[k]
                rec.action_results.pop(k, None)
        node_states = msg.get("nodeStates") or []
        released = [n["sequenceId"] for n in node_states if n.get("released")]
        rec.state_base_seq = released[-1] if released else rec.last_seq
        rec.state_remaining = len(node_states)
        rec.state_nodes = [(n["nodeId"], n["sequenceId"], bool(n.get("released"))) for n in node_states]
        errors = msg.get("errors") or []
        fatal = [e for e in errors if e.get("errorLevel") == "FATAL"]
        new_fatal = fatal[0]["errorType"].upper() if fatal else None
        if new_fatal != rec.fatal:
            self._fatal_changed(rec, new_fatal)
        for e in errors:
            if e.get("errorLevel") == "WARNING" and e.get("errorType") in (
                    "orderError", "orderUpdateError", "noRouteError", "validationError"):
                self.counters["order_rejects"] += 1
                self.event("order_rejected", rec.serial, f"{e['errorType']}: {e.get('errorDescription', '')}", "warn")
                self._resync(rec)
        rec.errors = errors

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
    def ingest_detections(self, detections: list[Detection], infra: bool = True) -> None:
        """One sensing cycle: infrastructure detections (camera/RTLS) plus what the vehicles reported.

        ``infra`` False: no infrastructure feed exists on this site, only the vehicles' own sensors.
        """
        if infra:
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
        return f"{self.run_id}{kind}-{self._action_seq}"

    def _instant(self, rec: RobotRecord, actions: list[dict]) -> None:
        t = topic(rec.manufacturer, rec.serial, "instantActions")
        msg = build_instant_actions(header(self.ids, t, rec.manufacturer, rec.serial, self.now, self.clock),
                                    actions)
        self.bus.publish(t, msg, self.now)

    def instant(self, serial: str, action_type: str, params: dict | None = None,
                action_id: str | None = None) -> str:
        action_id = action_id or self._aid(action_type)
        self._instant(self.robots[serial], [action(action_type, action_id, "NONE", params)])
        return action_id

    def _send_cancel(self, rec: RobotRecord, order_id: str) -> None:
        """Persist and publish one cancelOrder attempt, retaining its acknowledgement identity."""
        action_id = self._aid("cancelOrder")
        self.cancel_pending[rec.serial] = (order_id, self.now)
        self.cancel_action[rec.serial] = action_id
        if self.on_order is not None:
            self.on_order(rec)
        self.instant(rec.serial, "cancelOrder", action_id=action_id)

    def _send_order(self, rec: RobotRecord) -> None:
        new = rec.last_order is None or rec.last_order["orderId"] != rec.order_id
        i0 = 0 if new else rec.sent_base_end
        i_end = min(len(rec.route) - 1, rec.base_end + self.cfg.horizon_nodes)
        nodes = []
        for i in range(i0, i_end + 1):
            n = self.layout.nodes[rec.route[i]]
            # a station is approached along its stub: the order states that heading with a tolerance
            theta = self._heading(rec.route[i - 1], rec.route[i]) \
                if i > 0 and n.kind in STUB_KINDS and i == len(rec.route) - 1 else None
            nodes.append(RouteNode(n.id, n.x, n.y, list(rec.goal_actions.get(i, [])), theta))
        edges = []
        for a, b in zip(rec.route[i0:i_end], rec.route[i0 + 1:i_end + 1]):
            e = self.layout.edge_between(a, b)
            edges.append(RouteEdge(e.id, a, b, self._edge_speed(rec, e)))
        if not new:
            rec.update_id += 1
        t = topic(rec.manufacturer, rec.serial, "order")
        msg = build_order(header(self.ids, t, rec.manufacturer, rec.serial, self.now, self.clock), rec.order_id,
                          rec.update_id, nodes, edges, 2 * i0, rec.base_end - i0 + 1)
        rec.last_order = msg
        rec.last_order_at = self.now
        rec.sent_base_end = rec.base_end
        rec.order_pending = False
        if self.on_order is not None:
            self.on_order(rec)          # the runtime saves the vehicle's snapshot before the order can be driven
        self.bus.publish(t, msg, self.now)
        self.counters["order_updates" if not new else "orders"] += 1

    def _edge_zones(self, e) -> list[str]:
        zones = self._edge_zone_cache.get(e.id)
        if zones is None:
            a, b = self.layout.nodes[e.start], self.layout.nodes[e.end]
            pts = [a.pos, ((a.x + b.x) / 2, (a.y + b.y) / 2), b.pos]
            zones = sorted({z.id for p in pts for z in self.layout.zones_at(p)})
            self._edge_zone_cache[e.id] = zones
        return zones

    def _edge_speed(self, rec: RobotRecord, e) -> float:
        """maxSpeed of an order edge: the lane's own limit and the static zone limits along it.

        Zone limits travel in the standard edge field, so a vehicle without the x_fleetSpeedLimit extension
        still obeys them; for such a vehicle the people-in-zone limit is applied all the time (it cannot be
        slowed later on edges it has already been given)."""
        speed = e.max_speed
        dynamic = not self.supports(rec, "x_fleetSpeedLimit")
        for zid in self._edge_zones(e):
            z = self.layout.zones[zid]
            for lim in (z.static_speed, z.human_speed if dynamic and self.cfg.human_zones else None,
                        self.zone_speed_override.get(zid) if dynamic else None):
                if lim is not None:
                    speed = min(speed, lim)
        return speed

    def capability_gaps(self, rec: RobotRecord) -> list[str]:
        """Extensions this vehicle lacks and the standard fallback used instead (shown to the operator)."""
        if not rec.factsheet:
            return []
        gaps = []
        if not self.supports(rec, "x_remoteStop"):
            gaps.append("일괄 정지 → startPause")
        if not self.supports(rec, "x_fleetSpeedLimit"):
            gaps.append("구역 감속 → 경로 maxSpeed 상시 적용")
        if not self.supports(rec, "x_resetFault"):
            gaps.append("원격 리셋 없음 → 현장 조치")
        return gaps

    # ================================================================ restart: snapshot, restore, resync
    def robot_snapshot(self, rec: RobotRecord) -> dict:
        """What a restarted manager needs to take a vehicle back without releasing anything twice."""
        pending = self.cancel_pending.get(rec.serial)
        return {"serial": rec.serial, "manufacturer": rec.manufacturer, "route": list(rec.route),
                "order_id": rec.order_id, "update_id": rec.update_id, "base_end": rec.base_end,
                "cur_idx": rec.cur_idx, "sent_base_end": rec.sent_base_end, "purpose": rec.purpose,
                "task_id": rec.task_id, "last_node": rec.last_node, "last_order": rec.last_order,
                "goal_actions": {str(k): v for k, v in rec.goal_actions.items()},
                "resume": list(rec.resume) if rec.resume else None,
                "cancel_order_id": pending[0] if pending else None,
                "cancel_action_id": self.cancel_action.get(rec.serial) if pending else None,
                # the reservations exactly as the lock table held them, and when: on a restart the newest
                # snapshot that claims a resource wins (it can only have been taken after the other released it)
                "held": sorted(self.locks.resources_of(rec.serial)), "saved_at": round(self.now, 3)}

    def restore(self, snapshots: dict[str, dict], resync_s: float = 15.0) -> None:
        """Start in RESYNC: hold every reservation the vehicles may still be driving on, assign nothing new.

        Each vehicle's own state decides what is kept (``_reconcile``); a vehicle that never reports keeps its
        reservations (as for any vehicle that has gone silent) and the mode returns to RUNNING after ``resync_s``.
        """
        for serial, snap in snapshots.items():
            rec = self.register(serial, snap.get("manufacturer"))
            rec.route = list(snap.get("route") or [])
            rec.order_id = snap.get("order_id", "")
            rec.update_id = int(snap.get("update_id", 0))
            rec.base_end = int(snap.get("base_end", 0))
            rec.cur_idx = int(snap.get("cur_idx", 0))
            rec.sent_base_end = int(snap.get("sent_base_end", 0))
            rec.purpose = snap.get("purpose", "")
            rec.task_id = snap.get("task_id")
            rec.last_node = snap.get("last_node")
            rec.last_order = snap.get("last_order")
            rec.last_order_at = self.now
            rec.last_seen = self.now          # silence is counted from the restart, not from the epoch
            rec.goal_actions = {int(k): v for k, v in (snap.get("goal_actions") or {}).items()}
            rec.resume = tuple(snap["resume"]) if snap.get("resume") else None
            rec.manual_handoff_at = self.now if rec.purpose == "manual_handoff" else -1e9
            rec.order_pending = False
            cancel_order_id = snap.get("cancel_order_id")
            if cancel_order_id:
                self.cancel_pending[serial] = (str(cancel_order_id), self.now)
                if snap.get("cancel_action_id"):
                    self.cancel_action[serial] = str(snap["cancel_action_id"])
            self._restored[serial] = snap
        # reservations: each resource goes to the newest snapshot that claims it
        claims: dict[str, tuple[float, str]] = {}
        for serial, snap in snapshots.items():
            for r in self._snapshot_held(snap):
                at = float(snap.get("saved_at", 0.0))
                if r not in claims or at > claims[r][0]:
                    claims[r] = (at, serial)
        for r, (_at, serial) in sorted(claims.items()):
            self.locks.force(serial, r)
        lost = sorted({s for serial, snap in snapshots.items() for r in self._snapshot_held(snap)
                       for s in [serial] if claims[r][1] != serial})
        if lost:
            self.event("resync", "fleet", f"재기동: 스냅숏 예약 충돌은 최신 기록 기준으로 정리({', '.join(lost)})",
                       "warn")
        self.mode = "RESYNC"
        self.resync_until = self.now + resync_s
        self.event("resync", "fleet", f"재기동: 차량 {len(snapshots)}대 상태 재동기화 중(신규 배정 보류)", "warn")

    def _snapshot_held(self, snap: dict) -> list[str]:
        """Reservations a snapshot claims (older snapshots without ``held``: the released part of the route)."""
        if snap.get("held") is not None:
            return list(snap["held"])
        route, held = list(snap.get("route") or []), []
        cur, end = int(snap.get("cur_idx", 0)), int(snap.get("base_end", 0))
        if route:
            for i in range(cur, min(end, len(route) - 1) + 1):
                held.append(node_res(route[i]))
                if i < end:
                    held.append(lane_res(self.layout.edge_between(route[i], route[i + 1]).lane))
        elif snap.get("last_node"):
            held.append(node_res(snap["last_node"]))
        return held

    def _hold_reported(self, rec: RobotRecord) -> list[str]:
        """Reserve what the vehicle says it may still drive (its released nodes); returns what another holds."""
        taken = []
        nodes = [n for n, _seq, released in rec.state_nodes if released]
        for n in ([rec.last_node] if rec.last_node else []) + nodes:
            if n not in self.layout.nodes:
                continue
            r = node_res(n)
            holder = self.locks.holder_of(r)
            if holder in (None, rec.serial):
                self.locks.force(rec.serial, r)
            else:
                taken.append(f"{n}({holder})")
        return taken

    def _reconcile(self, rec: RobotRecord) -> None:
        """First state after a restart: keep the restored order if the vehicle is still on it."""
        snap = self._restored.pop(rec.serial, None)
        if snap is None:
            return
        if rec.purpose == "manual_handoff":
            if not self._finish_manual_handoff(rec):
                rec.manual_handoff_at = self.now
                if self.on_order is not None:
                    self.on_order(rec)
                if rec.mode != "MANUAL":
                    self.instant(rec.serial, "x_setOperatingMode", {"mode": "MANUAL"})
            return
        if rec.purpose == "recover" and not rec.task_id and not rec.resume:
            pending = self.cancel_pending.get(rec.serial)
            expected_order = pending[0] if pending is not None else rec.order_id
            stopped = rec.state_remaining == 0 and not rec.driving and rec.dist_since <= 1e-6 \
                and rec.last_node is not None
            if stopped:
                self.cancel_pending.pop(rec.serial, None)
                self.cancel_action.pop(rec.serial, None)
                self.clear_route(rec)
                self.locks.retain_only(rec.serial, {node_res(rec.last_node)})
            elif rec.state_order_id == expected_order and rec.state_remaining > 0:
                action_id = self.cancel_action.get(rec.serial)
                status = rec.actions.get(action_id) if action_id else None
                if status in (None, "FAILED"):
                    self._send_cancel(rec, rec.state_order_id)
                    self.event("resync_mismatch", rec.serial, "재기동 뒤 작업 해제 주문 취소 재요청", "warn")
            else:
                self._guard_cancel_mismatch(rec, expected_order)
            return
        standoff_resume = rec.resume and (
            rec.purpose == "recover" and len(rec.resume) in (4, 5, 6)
            or rec.purpose == "evade" and len(rec.resume) == 6)
        if standoff_resume:
            goal, purpose, goal_actions, task_id = rec.resume[:4]
            avoid = set(rec.resume[4]) if len(rec.resume) >= 5 else set()
            blocked = str(rec.resume[5]) if len(rec.resume) == 6 and rec.resume[5] else ""
            stopped = rec.state_remaining == 0 and not rec.driving and rec.dist_since <= 1e-6 \
                and rec.last_node is not None
            recovery = {"goal": goal, "purpose": purpose, "goal_actions": list(goal_actions),
                        "task_id": task_id, "avoid": avoid,
                        "confirmed": rec.purpose == "evade" or stopped, "blocked": blocked}
            self._standoff_recovery[rec.serial] = recovery
            if blocked:
                self.robot_paused.add(rec.serial)
                self.alarms.set("RESERVATION_MISMATCH", rec.serial, True, self.now, blocked)
            elif rec.purpose == "evade" and rec.state_order_id == rec.order_id and rec.state_remaining > 0:
                self.event("resync_ok", rec.serial, f"재동기화: 대치 회피 주문 {rec.order_id} 이어서 진행", "info")
            elif stopped and rec.state_order_id == rec.order_id:
                self._resume_standoff_order(rec, recovery)
            elif rec.state_order_id == rec.order_id and rec.state_remaining > 0:
                action_id = self.cancel_action.get(rec.serial)
                status = rec.actions.get(action_id) if action_id else None
                if status in (None, "FAILED"):
                    self._send_cancel(rec, rec.state_order_id)
                    self.event("standoff_cancel", rec.serial, "재기동 뒤 대치 주문 취소 재요청", "warn")
            else:
                self.robot_paused.add(rec.serial)
                self._block_standoff_recovery(
                    rec, recovery, f"{rec.serial} 대치 복구 중 다른 주문 {rec.state_order_id} 진행 감지")
            return
        if not snap.get("route") and not rec.task_id:
            if rec.state_order_id and rec.state_remaining > 0:
                # driving an order the snapshot never saw (sent, then the manager stopped before saving):
                # hold what the vehicle reports it may drive, then stop it there and start over
                self._cancel_unknown(rec, "기록에 없는 주문 진행 중: 보고된 구간 예약 후 취소")
            return                          # nothing was in progress: the vehicle's own state is all there is
        if rec.route and rec.state_order_id == rec.order_id:
            self.event("resync_ok", rec.serial, f"재동기화: 주문 {rec.order_id} 이어서 진행", "info")
            return
        unsent = rec.last_order is None or rec.last_order.get("orderId") != rec.order_id
        if rec.route and unsent and rec.state_remaining == 0 and rec.last_node == rec.route[0]:
            # the manager stopped between planning the next leg and sending it: the vehicle waits at its start
            rec.order_pending = True
            self.event("resync_ok", rec.serial, f"재동기화: 보내지 못한 주문 {rec.order_id} 전송", "info")
            return
        task = rec.task_id
        if rec.state_remaining == 0:
            # the vehicle has finished whatever it had: it stands at its last node with nothing released
            self.clear_route(rec)
            self.locks.retain_only(rec.serial, {node_res(rec.last_node)} if rec.last_node else set())
            why = "재기동 사이 주문 종료"
        else:
            # an order the snapshot does not know: stop it at its next node and start from there
            self._cancel_unknown(rec, "")
            rec.route = rec.route[: rec.base_end + 1] if rec.route else []
            rec.purpose = "recover"
            rec.task_id = None
            why = "알 수 없는 주문 진행 중: 취소 후 재배정"
        if task and self.dispatcher is not None:
            self.dispatcher.reconcile_task(task, rec, why)
        self.event("resync_mismatch", rec.serial, why, "warn")

    def _cancel_unknown(self, rec: RobotRecord, why: str) -> None:
        """Cancel an order the manager does not know, after reserving what the vehicle may still drive."""
        taken = self._hold_reported(rec)
        if taken:
            # never take a reservation from another vehicle: stop this one and let the operator look
            self.robot_paused.add(rec.serial)
            self.alarms.set("RESERVATION_MISMATCH", rec.serial, True, self.now,
                            f"{rec.serial} 재기동 뒤 보고 구간이 다른 차량 예약과 겹침: {', '.join(taken)}")
        self._send_cancel(rec, rec.state_order_id)
        if why:
            self.event("resync_mismatch", rec.serial, why, "warn")

    def _confirm_cancels(self) -> None:
        """cancelOrder goes out at QoS 0: resend until the vehicle's state no longer runs that order."""
        for serial, (order_id, sent) in list(self.cancel_pending.items()):
            rec = self.robots.get(serial)
            recovery = self._standoff_recovery.get(serial)
            fresh = rec is not None and rec.last_seen > sent
            stopped = fresh and rec.state_remaining == 0 \
                and not rec.driving and rec.dist_since <= 1e-6 and rec.last_node is not None
            finished = stopped
            if finished:
                del self.cancel_pending[serial]
                self.cancel_action.pop(serial, None)
                if recovery is not None and rec is not None:
                    if stopped and rec.state_order_id == order_id:
                        recovery["confirmed"] = True
                    else:
                        self._block_standoff_recovery(
                            rec, recovery, f"{serial} 대치 취소 중 다른 주문 {rec.state_order_id} 진행 감지")
            elif recovery is not None and fresh and rec.state_order_id != order_id:
                del self.cancel_pending[serial]
                self.cancel_action.pop(serial, None)
                self._block_standoff_recovery(
                    rec, recovery, f"{serial} 대치 취소 중 다른 주문 {rec.state_order_id} 진행 감지")
            elif recovery is None and fresh and rec.state_order_id and rec.state_order_id != order_id:
                self._guard_cancel_mismatch(rec, order_id)
            elif rec is not None and self.now - sent > 3.0 and rec.connection == "ONLINE":
                action_id = self.cancel_action.get(serial)
                status = rec.actions.get(action_id) if action_id else None
                if status in (None, "FAILED"):
                    self._send_cancel(rec, order_id)
        for rec in self.robots.values():
            if rec.purpose != "manual_handoff" or rec.serial in self._restored:
                continue
            if self._finish_manual_handoff(rec):
                continue
            if rec.mode != "MANUAL" and rec.connection == "ONLINE" \
                    and self.now - rec.manual_handoff_at > 3.0:
                rec.manual_handoff_at = self.now
                if self.on_order is not None:
                    self.on_order(rec)
                self.instant(rec.serial, "x_setOperatingMode", {"mode": "MANUAL"})
        for serial, recovery in list(self._standoff_recovery.items()):
            if not recovery.get("confirmed") or recovery.get("blocked"):
                continue
            rec = self.robots.get(serial)
            if rec is None:
                del self._standoff_recovery[serial]
                continue
            self._resume_standoff_order(rec, recovery)

    def _finish_manual_handoff(self, rec: RobotRecord) -> bool:
        """Finish a persisted handoff only after a newer, stopped MANUAL state arrives."""
        if rec.purpose != "manual_handoff" or rec.last_seen <= rec.manual_handoff_at \
                or rec.mode != "MANUAL" or rec.state_remaining != 0 \
                or rec.driving or rec.dist_since > 1e-6 or not rec.state_has_pose or not rec.positioned \
                or rec.factsheet is None or rec.length <= 0.0 or rec.width <= 0.0:
            return False
        self.cancel_pending.pop(rec.serial, None)
        self.cancel_action.pop(rec.serial, None)
        keep = self._occupied(rec)
        self.clear_route(rec)
        rec.manual_handoff_at = -1e9
        self.locks.retain_only(rec.serial, keep)
        if self.on_order is not None:
            self.on_order(rec)
        self.event("manual_handoff", rec.serial, "원격 조종 인계 상태 확인", "info")
        return True

    def _guard_cancel_mismatch(self, rec: RobotRecord, expected_order: str) -> None:
        """Hold a newly reported order, stop it, and replace only the cancel aimed at the stale order."""
        taken = self._hold_reported(rec)
        reason = f"{rec.serial} 작업 해제 중 다른 주문 {rec.state_order_id} 진행 감지"
        if taken:
            reason += f" · 보고 구간 예약 충돌: {', '.join(taken)}"
        self.robot_paused.add(rec.serial)
        self.alarms.set("RESERVATION_MISMATCH", rec.serial, True, self.now, reason)
        if rec.state_order_id and rec.state_order_id != expected_order:
            self._send_cancel(rec, rec.state_order_id)

    def _block_standoff_recovery(self, rec: RobotRecord, recovery: dict, reason: str) -> None:
        """Persist a recovery that needs operator review and keep its old order frozen."""
        recovery["blocked"] = reason
        rec.purpose = "recover"
        rec.resume = (recovery["goal"], recovery["purpose"], recovery["goal_actions"],
                      recovery["task_id"], sorted(recovery["avoid"]), reason)
        self.robot_paused.add(rec.serial)
        self.alarms.set("RESERVATION_MISMATCH", rec.serial, True, self.now, reason)
        if self.on_order is not None:
            self.on_order(rec)

    def _resume_standoff_order(self, rec: RobotRecord, recovery: dict | None = None) -> bool:
        """Release a confirmed-cancelled base and restore the same task with fresh action ids."""
        recovery = recovery or self._standoff_recovery.get(rec.serial)
        if self.mode != "RUNNING" or recovery is None or recovery.get("blocked") \
                or not recovery.get("confirmed") or rec.state_remaining != 0 \
                or rec.driving or rec.dist_since > 1e-6 or rec.last_node is None:
            return False
        if rec.purpose == "evade" and rec.route and not self.route_done(rec):
            return False                 # finish the safe fallback route before restoring the original task
        task_id = recovery["task_id"]
        task = self.dispatcher.tasks.get(task_id) if self.dispatcher is not None and task_id else None
        if rec.task_id != task_id or self.dispatcher is not None \
                and (task is None or task.state != "ACTIVE" or task.robot != rec.serial):
            self._block_standoff_recovery(
                rec, recovery, f"{rec.serial} 대치 취소 뒤 작업 소유권 변경: {task_id}")
            return False
        keep = self._occupied(rec)
        if rec.last_node:
            keep.add(node_res(rec.last_node))
        self.locks.retain_only(rec.serial, keep)
        rec.route = []
        rec.goal_actions = {}
        rec.rot_zone = {}
        rec.base_end = rec.cur_idx = rec.sent_base_end = 0
        rec.order_pending = False
        rec.wait = rec.pending_block = None
        rec.purpose = "recover"
        rec.task_id = recovery["task_id"]
        actions = []
        unsafe = []
        for a in recovery["goal_actions"]:
            state = rec.actions.get(a["actionId"])
            if state == "FINISHED":
                continue
            cancel_failed = state == "FAILED" and rec.action_results.get(a["actionId"]) == "cancelOrder"
            if state in ("RUNNING", "INITIALIZING") or state == "FAILED" and not cancel_failed:
                unsafe.append(a["actionId"])
                continue
            actions.append({**a, "actionId": self._aid(f"recover-{a['actionType']}")})
        if unsafe:
            self._block_standoff_recovery(
                rec, recovery, f"{rec.serial} 대치 취소 뒤 동작 상태 불일치: {', '.join(unsafe)}")
            return False
        if rec.safety_field == "PROTECTIVE":
            return False                 # keep the original task frozen while a safe fallback evasion may run
        if not self.can_take_route(rec):
            return False
        avoid = set(recovery["avoid"])
        avoid.discard(rec.last_node)
        avoid.discard(recovery["goal"])
        resumed = self.assign_route(rec, recovery["goal"], recovery["purpose"], actions,
                                    recovery["task_id"], avoid_nodes=avoid)
        if not resumed and avoid:
            # The snapshot describes the truck at the original standoff. Once the protective field is clear,
            # retry without that stale cut; current blocks and reservations still gate route extension.
            resumed = self.assign_route(rec, recovery["goal"], recovery["purpose"], actions,
                                        recovery["task_id"])
        if not resumed:
            return False
        rec.resume = None
        self._standoff_recovery.pop(rec.serial, None)
        self.event("standoff_replan", rec.serial,
                   f"대치 주문 취소 확인 후 {recovery['goal']} 재계획", "warn")
        return True

    def _cancel_for_standoff(self, rec: RobotRecord, avoid_nodes: set[str]) -> bool:
        """Cancel a released order at a node, retaining its task until cancellation is confirmed."""
        if rec.serial in self.cancel_pending or rec.serial in self._standoff_recovery or not rec.route or not rec.goal:
            return False
        task = self.dispatcher.tasks.get(rec.task_id) if self.dispatcher is not None and rec.task_id else None
        if not rec.task_id or self.dispatcher is not None \
                and (task is None or task.state != "ACTIVE" or task.robot != rec.serial):
            return False
        goal_actions = list(rec.goal_actions.get(len(rec.route) - 1, []))
        states = [rec.actions.get(a["actionId"]) for a in goal_actions]
        if any(state in ("FAILED", "RUNNING", "INITIALIZING") for state in states):
            return False
        goal_actions = [a for a, state in zip(goal_actions, states) if state != "FINISHED"]
        goal, purpose, task_id = rec.goal, rec.purpose, rec.task_id
        self._standoff_recovery[rec.serial] = {
            "goal": goal,
            "purpose": purpose,
            "goal_actions": goal_actions,
            "task_id": task_id,
            "avoid": set(avoid_nodes),
            "confirmed": False,
        }
        rec.resume = (goal, purpose, goal_actions, task_id, sorted(avoid_nodes), "")
        rec.purpose = "recover"
        if self.on_order is not None:
            self.on_order(rec)              # persist the recovery marker before cancelOrder can take effect
            if self.mode != "RUNNING":
                rec.purpose, rec.resume = purpose, None
                del self._standoff_recovery[rec.serial]
                return False
        self._send_cancel(rec, rec.state_order_id)
        self.event("standoff_cancel", rec.serial, "지게차 장기 대치: 현재 노드에서 주문 취소 요청", "warn")
        return True

    def _orphan_tasks(self) -> None:
        """Tasks restored as ACTIVE whose vehicle no longer runs them: requeue (or fail if loaded)."""
        if self.dispatcher is None:
            return
        running = {rec.task_id for rec in self.robots.values() if rec.task_id}
        for task in list(self.dispatcher.tasks.values()):
            if task.state == "ACTIVE" and task.id not in running:
                self.dispatcher.reconcile_task(task.id, self.robots.get(task.robot or ""),
                                               "재기동 뒤 이 작업을 진행하는 차량 없음")

    def _check_resync(self) -> None:
        self._confirm_cancels()
        if self.resync_until is None:      # not restarting (a fleet stop during the resync does not end it)
            return
        for rec in self.robots.values():
            if rec.serial in self._restored and rec.has_state:
                self._reconcile(rec)
        if not self._restored or self.now >= (self.resync_until or 0.0):
            silent = sorted(self._restored)
            self._restored.clear()
            self._orphan_tasks()
            if self.mode == "RESYNC":
                self.mode = "FIRE" if self.fire_alarm else "RUNNING"
            self.resync_until = None
            self.event("resync_done", "fleet", "재동기화 완료" + (f" · 무응답 {', '.join(silent)}(예약 유지)"
                                                               if silent else ""), "warn" if silent else "info")

    def _request_factsheets(self) -> None:
        """A vehicle whose factsheet did not arrive (connected before us) is asked for it (VDA 5050 2.0)."""
        for rec in self.robots.values():
            if rec.factsheet is None and rec.has_state and rec.connection == "ONLINE" \
                    and self.now - rec.factsheet_asked > 5.0:
                rec.factsheet_asked = self.now
                self._instant(rec, [action("factsheetRequest", self._aid("factsheet"), "NONE")])

    # ================================================================ routes
    def can_take_route(self, rec: RobotRecord) -> bool:
        if rec.purpose == "manual_handoff":
            return False
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
        rec.escape_from = None
        rec.route = path
        rec.rot_zone = {}
        rec.order_id = f"{rec.serial}-{self.run_id}O{self._order_seq}"
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
        rec.escape_from = None
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
        if rec.purpose == "manual_handoff":
            return False
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

    def goal_action_failed(self, rec: RobotRecord) -> str | None:
        """Why the goal action failed (the vehicle's resultDescription), or None when it did not."""
        for a in rec.goal_actions.get(len(rec.route) - 1, []):
            if rec.actions.get(a["actionId"]) == "FAILED":
                return rec.action_results.get(a["actionId"]) or "동작 실패"
        return None

    # ================================================================ tick
    def tick(self, now: float, dt: float) -> None:
        self.now = now
        self._check_connections()
        self._check_resync()
        self._request_factsheets()
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
            recovery = self._standoff_recovery.get(rec.serial)
            recovery_holds_order = recovery is not None and (
                self.mode != "RUNNING" or not recovery.get("confirmed")
                or recovery.get("blocked") or rec.purpose != "evade")
            if rec.serial in self.cancel_pending or recovery_holds_order \
                    or rec.purpose in ("recover", "manual_handoff"):
                continue
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
                self.bus.publish(topic(rec.manufacturer, rec.serial, "order"), rec.last_order, now)
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
            silent = self.now - rec.last_seen
            if rec.connection == "ONLINE" and silent > self.cfg.comms_timeout_s:
                rec.connection = "CONNECTIONBROKEN"
                self.counters["comms_lost"] += 1
                self.event("comms_lost", rec.serial, "상태 수신 끊김(예약 유지)", "warn")
                self.alarms.set("COMMS_DEGRADED", rec.serial, True, self.now,
                                f"{rec.serial} {self.cfg.comms_timeout_s:.0f}초 이상 무응답 · 예약 유지")
            if rec.connection == "CONNECTIONBROKEN" and silent >= self.cfg.comms_alarm_s:
                self.alarms.set("COMMS_LOST", rec.serial, True, self.now,
                                f"{rec.serial} {silent:.0f}초 무응답 · 예약 {len(self.locks.resources_of(rec.serial))}건 유지")

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
        src: dict[str, str] = {}               # what made each block: disc | turn | aisle | zone
        requests: dict[str, tuple[str, str]] = {}   # aisle requests: resource -> (aisle zone, truck)

        def put(res: str, kind: str, ref: str, why: str) -> None:
            if res not in blocks:
                blocks[res] = (kind, ref)
                src[res] = why
        horizon = {"human": self.cfg.human_horizon_s, "forklift": self.cfg.forklift_horizon_s}
        collab = [z.polygon for z in self.layout.zones.values() if z.collaborative]
        if self.cfg.strategy != "none":
            for tid, cls, x, y, r in self.tracker.predicted_discs(self.now, horizon):
                if cls == "human" and any(point_in_polygon((x, y), poly) for poly in collab):
                    # collaborative picking zone: people and robots share the aisle by design. A person holds
                    # only the robot stop they stand on; passing them is left to the aisle's 0.3 m/s limit and
                    # the vehicle's narrow field set
                    for kind, ident in self._query(x, y, COLLAB_HOLD_M):
                        if kind == "N" and math.hypot(self.layout.nodes[ident].x - x,
                                                      self.layout.nodes[ident].y - y) <= COLLAB_HOLD_M:
                            put(node_res(ident), cls, tid, "disc")
                    continue
                for kind, ident in self._query(x, y, r):
                    if kind == "N":
                        n = self.layout.nodes[ident]
                        if math.hypot(n.x - x, n.y - y) <= r:
                            put(node_res(ident), cls, tid, "disc")
                    else:
                        a, b = self._lane_points(ident)
                        if seg_point_dist((x, y), a, b) <= r - 0.25:
                            put(lane_res(ident), cls, tid, "disc")
            # turn zones ahead of a moving truck: a counterbalance truck may turn at any intersection it reaches
            # within its horizon, sweeping a 1.9 m circle; robots must not be standing in that circle when it
            # arrives (a robot inside stops the truck, and the truck in its field stops the robot)
            for tr in self.tracker.tracks.values():
                if tr.cls != "forklift" or not tr.confirmed or tr.speed < 0.3 or tr.resting and tr.speed < 0.6:
                    continue
                ux, uy = tr.vx / tr.speed, tr.vy / tr.speed
                reach = tr.speed * self.cfg.forklift_horizon_s + 1.0
                for kind, ident in self._query(tr.x + ux * reach / 2, tr.y + uy * reach / 2, reach / 2 + 1.0):
                    if kind != "N" or not self.layout.is_intersection(ident):
                        continue
                    n = self.layout.nodes[ident]
                    along = (n.x - tr.x) * ux + (n.y - tr.y) * uy
                    if not (0.0 <= along <= reach) or abs(-(n.x - tr.x) * uy + (n.y - tr.y) * ux) > 1.0:
                        continue
                    zone_r = SWING_RADIUS + CLEARANCE_TURN
                    for k2, i2 in self._query(n.x, n.y, zone_r):
                        if k2 == "N" and math.hypot(self.layout.nodes[i2].x - n.x, self.layout.nodes[i2].y - n.y) <= zone_r:
                            put(node_res(i2), "forklift", tr.id, "turn")
            # aisle signal: while a manual truck works a rack aisle no robot enters it (robots already
            # inside finish their move); a truck and a robot meeting in a 4 m aisle cannot pass each other.
            # Only confirmed tracks: one misread person must not close an aisle
            still = {}
            for tid in [t for t in self._fl_stood if t not in self.tracker.tracks]:
                del self._fl_stood[tid]
            for tr in self.tracker.tracks.values():
                if tr.cls != "forklift" or not tr.confirmed:
                    continue
                if tr.resting:
                    # a truck that backed off from one intersection may come to rest beside the next one: it still
                    # waits for the first (it has not driven off since)
                    stood, prev = self._intersection_at(tr.x, tr.y, 0.8), self._fl_stood.get(tr.id)
                    if stood is not None and (prev is None or math.hypot(
                            self.layout.nodes[prev].x - tr.x, self.layout.nodes[prev].y - tr.y) > BACKED_OFF_M):
                        self._fl_stood[tr.id] = stood
                elif tr.pace > DRIVE_PACE:
                    self._fl_stood.pop(tr.id, None)
                for z in self._aisle_zones:
                    if point_in_polygon((tr.x, tr.y), z.polygon):
                        for nid in self._zone_nodes.get(z.id, ()):
                            put(node_res(nid), "forklift", tr.id, "aisle")
                # aisle request: a truck stopped at an aisle mouth for 3 s is waiting to go in, and one driving
                # up to a mouth is about to. New robots are held out of that aisle; robots already in it are not
                # (they finish and leave), otherwise a robot inside and the truck outside would wait for each other
                if not tr.stopped:
                    z = self._aisle_at_mouth(tr.x, tr.y, toward=(tr.vx, tr.vy))
                    if z is not None:
                        for nid in self._zone_nodes.get(z.id, ()):
                            requests.setdefault(node_res(nid), (z.id, tr.id))
                    continue
                still[tr.id] = self._fl_still_since.get(tr.id, self.now)
                if self.now - still[tr.id] < 3.0:
                    continue
                turn_at = self._turn_ahead(tr)
                # a truck known to wait at an intersection asks for the aisle that intersection leads into
                # (below), not for one whose mouth it happens to stand in front of after backing off
                z = self._aisle_at_mouth(tr.x, tr.y) if turn_at is None else None
                if z is not None:
                    for nid in self._zone_nodes.get(z.id, ()):
                        requests.setdefault(node_res(nid), (z.id, tr.id))
                # turn request: a truck stopped on a lane just short of an intersection waits to turn there. On a
                # busy main aisle a robot is always passing through its swing circle, so the driver never gets
                # a gap; new robots are held out of the circle (those in it carry on) until the truck has turned
                # the FMS cannot tell a truck that waits to turn from one that just stands there: the request
                # holds robots for 20 s at a time, then lets them through for 30 s
                waited = self.now - still[tr.id] - 3.0
                if turn_at is not None and waited % 50.0 < 20.0:
                    n = self.layout.nodes[turn_at]
                    zone_r = SWING_RADIUS + CLEARANCE_TURN
                    for k2, i2 in self._query(n.x, n.y, zone_r):
                        if k2 == "N" and math.hypot(self.layout.nodes[i2].x - n.x,
                                                    self.layout.nodes[i2].y - n.y) <= zone_r:
                            put(node_res(i2), "forklift", tr.id, "turn")
                if turn_at is not None:
                    # the junction leads into a rack aisle: the truck may be waiting for that aisle's light. Kept
                    # on for as long as the truck stands there (new robots only; those inside finish), so the light
                    # can turn green
                    for z in self._aisle_zones:
                        if any(m in self._zone_nodes.get(z.id, ()) for m in self.layout.neighbors[turn_at]):
                            for nid in self._zone_nodes.get(z.id, ()):
                                requests.setdefault(node_res(nid), (z.id, tr.id))
            self._fl_still_since = still
        for z in self.layout.zones.values():
            if z.locked:
                for nid, zones in self.layout.node_zones.items():
                    if z.id in zones:
                        blocks[node_res(nid)] = ("zone", z.id)
                        src[node_res(nid)] = "zone"
        self.blocks = blocks
        self.block_src = src
        self.aisle_requests = requests

    def _aisle_at_mouth(self, x: float, y: float, toward: tuple[float, float] | None = None) -> Zone | None:
        """The rack aisle whose end (short side) a truck at (x, y) stands in front of, within 4.5 m.

        A truck that stops on the cross aisle beside an aisle, or between two aisles, is not at a mouth. With
        ``toward`` (a velocity) the truck must also be heading into that aisle.
        """
        for z in self._aisle_zones:
            x0, y0, x1, y1 = bbox(z.polygon)
            if (y1 - y0) >= (x1 - x0):          # aisle runs north-south: its mouths face y
                lateral_ok = x0 - 0.5 <= x <= x1 + 0.5
                gap = (y0 - y) if y < y0 else (y - y1) if y > y1 else -1.0
                inward = (1.0 if y < y0 else -1.0) * (toward[1] if toward else 1.0)
                speed = abs(toward[1]) if toward else 1.0
            else:
                lateral_ok = y0 - 0.5 <= y <= y1 + 0.5
                gap = (x0 - x) if x < x0 else (x - x1) if x > x1 else -1.0
                inward = (1.0 if x < x0 else -1.0) * (toward[0] if toward else 1.0)
                speed = abs(toward[0]) if toward else 1.0
            heading_in = toward is None or (inward > 0 and speed > 0.7 * math.hypot(*toward))
            if lateral_ok and 0.0 < gap <= 4.5 and heading_in:
                return z
        return None

    def _intersection_at(self, x: float, y: float, r: float) -> str | None:
        """The intersection node within ``r`` of (x, y), nearest first (ties by id), if any."""
        best = None
        for kind, nid in self._query(x, y, r):
            if kind == "N" and self.layout.is_intersection(nid):
                d = math.hypot(self.layout.nodes[nid].x - x, self.layout.nodes[nid].y - y)
                if d <= r and (best is None or (d, nid) < best):
                    best = (d, nid)
        return best[1] if best else None

    def _on_line_into(self, nid: str, x: float, y: float, reach: float) -> bool:
        """(x, y) lies on a lane into intersection ``nid``, or further back along the same straight line."""
        n = self.layout.nodes[nid]
        for m in self.layout.neighbors[nid]:
            mx, my = self.layout.nodes[m].x - n.x, self.layout.nodes[m].y - n.y
            k = reach / max(1e-9, math.hypot(mx, my))
            if seg_point_dist((x, y), n.pos, (n.x + mx * k, n.y + my * k)) <= 1.0:
                return True
        return False

    def _turn_ahead(self, tr) -> str | None:
        """The intersection a stopped truck waits to turn at: one it stands short of, on a lane into it.

        A truck that stood on an intersection and has since moved only at backing-off pace (it backs off at
        0.5 m/s and drives back up at 0.8) still waits for that one, up to 7 m back: drivers back off to let
        a robot out of the circle they need. Otherwise it is the intersection ahead in its last direction of
        travel (within 4.5 m). Neither the lane direction nor the last motion alone tells: a driver who turned
        back drives the loop against the one-way lanes, and a backed-off truck last moved away. A truck standing
        on an intersection needs nothing more: its own swing circles cover the turn.
        """
        stood = self._fl_stood.get(tr.id)
        if stood is not None:
            n = self.layout.nodes[stood]
            if 0.6 < math.hypot(n.x - tr.x, n.y - tr.y) <= BACKED_OFF_M and \
                    self._on_line_into(stood, tr.x, tr.y, BACKED_OFF_M + 0.5):
                return stood                    # (6 m back it may stand on the next intersection)
        if self._intersection_at(tr.x, tr.y, 0.6) is not None:
            return None
        if tr.heading is None:
            return None
        hx, hy = math.cos(tr.heading), math.sin(tr.heading)
        best = None
        for kind, nid in self._query(tr.x, tr.y, 4.5):
            if kind != "N" or not self.layout.is_intersection(nid):
                continue
            n = self.layout.nodes[nid]
            dx, dy = n.x - tr.x, n.y - tr.y
            d = math.hypot(dx, dy)
            if d > 4.5 or d < 0.3 or (dx * hx + dy * hy) / d < 0.85 or best is not None and (d, nid) >= best:
                continue                        # not in line, or farther than one already found
            if self._on_line_into(nid, tr.x, tr.y, 4.5):
                best = (d, nid)
        return best[1] if best else None

    @property
    def pick_aisles(self) -> list[Zone]:
        return [z for z in self.layout.zones.values() if z.kind == "aisle" and z not in self._aisle_zones]

    def aisle_free(self, zone_id: str) -> bool:
        """The signal light at a rack aisle mouth for a manual truck: green when no robot is in the aisle or
        holds a reservation in it (a robot about to enter counts, so truck and robot never enter together)."""
        zone = self.layout.zones.get(zone_id)
        if zone is None:
            return True
        for nid in self._zone_nodes.get(zone_id, ()):
            if self.locks.holder_of(node_res(nid)) in self.robots:
                return False
        return not any(point_in_polygon((r.x, r.y), zone.polygon) for r in self.robots.values())

    def _inside_aisle(self, rec: RobotRecord, zone_id: str) -> bool:
        zone = self.layout.zones[zone_id]
        if point_in_polygon((rec.x, rec.y), zone.polygon):
            return True
        return any(self.locks.holder_of(node_res(nid)) == rec.serial for nid in self._zone_nodes.get(zone_id, ()))

    def _block_for(self, rec: RobotRecord, res: str) -> tuple[str, str] | None:
        """What keeps ``rec`` out of ``res`` right now, if anything (its own reservations never do)."""
        if self.locks.holder_of(res) == rec.serial:
            return None
        hit = self.blocks.get(res)
        if hit is not None:
            kind, ref = hit
            # leaving a stopped truck's swept circle: that truck's own occupancy disc and turn request do not hold
            # us, as long as it stands still (its aisle signal and a moving truck's path still do)
            escaping = rec.purpose == "evade" and ref is not None and ref == rec.escape_from and \
                self.block_src.get(res) in ("disc", "turn") and ref in self.tracker.tracks and \
                self.tracker.tracks[ref].stopped
            if not escaping:
                return hit
        req = self.aisle_requests.get(res)
        if req is not None and not self._inside_aisle(rec, req[0]):
            return ("forklift", req[1])
        return None

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
        if rec.purpose == "manual_handoff":
            return None                      # keep the released base until a stopped MANUAL state confirms handoff
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
        if rec.serial in self.cancel_pending or rec.purpose in ("recover", "manual_handoff"):
            return                    # the released base is frozen until cancelOrder is confirmed
        if self.fire_alarm or self.mode in ("HOLD", "ESTOP", "FIRE"):
            return                    # fire alarm or fleet stop: release nothing beyond what vehicles already have
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
                hit = self._block_for(rec, r)
                if hit is not None:
                    blocked = self._wait(rec, hit[0], r, hit[1])
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
        elif rec.estop_local:
            st, why = "ESTOP", "차량 비상정지(현장 조작)"
        elif rec.estop:
            st, why = "ESTOP", "관제 일괄 정지(비안전)"
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
    def _field_blocker(self, rec: RobotRecord) -> str | None:
        """A reported robot blocker, or a pose-based estimate when standard state gives no field cause."""
        if rec.safety_field != "PROTECTIVE" or rec.driving or rec.charging or not rec.route \
                or self.route_done(rec) or self._goal_action_running(rec):
            return None
        if rec.field_cause:
            kind, _, ident = rec.field_cause.partition(":")
            other = self.robots.get(ident) if kind == "amr" else None
            return ident if other is not None and other is not rec and other.has_state else None
        hx, hy = math.cos(rec.theta), math.sin(rec.theta)
        best = None
        for o in self.robots.values():
            if o is rec or not o.has_state:
                continue
            dx, dy = o.x - rec.x, o.y - rec.y
            d = math.hypot(dx, dy)
            if 0.0 < d <= 4.0 and (dx * hx + dy * hy) / d >= 0.5 and (best is None or (d, o.serial) < best):
                best = (d, o.serial)
        return best[1] if best else None

    def _deadlocks(self) -> None:
        wait_for = {r.serial: r.wait.holder for r in self.robots.values()
                    if r.wait is not None and r.wait.kind == "robot" and r.wait.holder in self.robots}
        # a robot stopped by its protective field waits for the robot in front of it just as much: a cycle through
        # such a stop (A holds the node B needs to turn, B stands in A's field) is a deadlock too
        for r in self.robots.values():
            if r.serial not in wait_for:
                blocker = self._field_blocker(r)
                if blocker is not None:
                    wait_for[r.serial] = blocker
                    self._field_since.setdefault(r.serial, self.now)
                    continue
            self._field_since.pop(r.serial, None)
        live: set[frozenset] = set()
        for cycle in find_cycles(wait_for):
            members = [self.robots[s] for s in cycle]
            if any(self.now - self._waiting_since(m) < self.cfg.deadlock_confirm_s for m in members):
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
                # one detected before the measurement window is neither counted nor timed in it
                if not info.get("before_window"):
                    self.counters["deadlock_s"] += duration
                    if info.get("method"):
                        self.counters["deadlocks_resolved"] += 1
                if info.get("method"):
                    self.event("deadlock_resolved", ",".join(info["members"]),
                               f"교착 해소({info['method']}) {duration:.1f}초", "info")
                self.alarms.set("DEADLOCK_UNRESOLVED", ",".join(sorted(info["members"])), False, self.now)

    def _waiting_since(self, rec: RobotRecord) -> float:
        if rec.wait is not None:
            return rec.wait.since
        return self._field_since.get(rec.serial, self.now)

    def _resolve(self, members: list[RobotRecord]) -> str | None:
        candidates = sorted((r for r in members if self.can_take_route(r) and not r.charging),
                            key=lambda r: (self._priority(r) - (self.now - self._waiting_since(r)) / 2.0,
                                           r.loaded, r.serial))
        held_by_others = {}
        for m in members:
            held_by_others[m.serial] = {res_node(r) for o in members if o is not m
                                        for r in self.locks.resources_of(o.serial) if res_node(r)}
        for victim in candidates:
            avoid = set(held_by_others[victim.serial])
            blocked_node = res_node(victim.wait.resource) if victim.wait is not None else None
            if blocked_node:
                avoid.add(blocked_node)
            avoid.discard(victim.last_node)
            if victim.goal is None or victim.goal in avoid:
                continue                        # (a robot without a goal, stopped in a cycle by its field)
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
                          victim.task_id) if victim.goal is not None else None
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
                if self.locks.holder_of(node_res(n)) not in (None, rec.serial) or node_res(n) in self.blocks \
                        or node_res(n) in self.aisle_requests:
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
        if self.cfg.strategy == "none" or self.mode != "RUNNING":
            return
        stalled_ext = {k: v for k, v in self.external.items() if self.now - v.get("moved", self.now) > 4.0}
        stalled_fl = [tr for tr in self.tracker.tracks.values()
                      if tr.cls == "forklift" and (tr.resting or tr.speed < 0.2)]
        # where a stopped truck will swing: around itself, and around the intersection it waits to turn at (a
        # truck that backed off to let robots pass stands metres away from the circle it needs clear)
        circles = []
        for tr in stalled_fl:
            if tr.confirmed:
                circles.append((tr, (tr.x, tr.y)))
                turn_at = self._turn_ahead(tr)
                if turn_at is not None:
                    circles.append((tr, self.layout.nodes[turn_at].pos))
        for rec in self.robots.values():
            recovery = self._standoff_recovery.get(rec.serial)
            if recovery is not None and recovery.get("blocked"):
                continue
            released_standoff = self._released_standoff_truck(rec, stalled_fl)
            if released_standoff is not None:
                avoid = {n for n in self.layout.nodes
                         if dist(self.layout.nodes[n].pos, (released_standoff.x, released_standoff.y)) < 4.5}
                if self._cancel_for_standoff(rec, avoid):
                    continue
            in_swing = not rec.driving and any(
                dist(c, (rec.x, rec.y)) < SWING_RADIUS + rec.length / 2 + 0.2 for _tr, c in circles)
            if not in_swing:
                rec.swing_since = None        # the 5 s count starts again the next time it stands in a circle
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
            # standing inside the circle a stopped truck sweeps when it turns: the driver either waits or swings
            # into us, so we leave after 5 s whatever we are waiting for
            swing = [(tr, c) for tr, c in circles if dist(c, (rec.x, rec.y)) < SWING_RADIUS + rec.length / 2 + 0.2]
            impassable: set[str] = set()
            escape = None
            if swing and not rec.driving:
                rec.swing_since = rec.swing_since if rec.swing_since is not None else self.now
                if self.now - rec.swing_since >= 5.0:
                    tr, centre = swing[0]
                    near = {n: dist(self.layout.nodes[n].pos, centre) for n in self.layout.nodes}
                    # not through the circle itself, and no stopping close enough to be inside it again
                    impassable = {n for n, d in near.items() if d < SWING_RADIUS + 0.3 and n != here}
                    forbidden.update(n for n, d in near.items() if d < SWING_RADIUS + 2.0)
                    reason = f"지게차 {tr.id} 선회 반경 안"
                    escape = tr.id
            else:
                rec.swing_since = None
            if not forbidden:
                continue
            rec.yield_next = self.now + 8.0
            spot = self._evasion_spot(rec, forbidden, impassable=impassable)
            if spot is None:
                continue
            resume = None
            if rec.route and rec.goal:
                resume = (rec.goal, rec.purpose, rec.goal_actions.get(len(rec.route) - 1, []), rec.task_id)
            if self.assign_route(rec, spot, "evade", task_id=rec.task_id):
                if rec.serial not in self._standoff_recovery:
                    rec.resume = resume
                rec.escape_from = escape
                self.counters["evasions"] += 1
                self.event("yield", rec.serial, f"양보 회피: {reason} → {spot}", "info")

    def _released_standoff_truck(self, rec: RobotRecord, stalled_fl: list):
        """Confirmed stopped truck holding a node-stationary robot inside an already released base."""
        if self.mode != "RUNNING":
            return None
        if rec.serial in self.cancel_pending or rec.serial in self._standoff_recovery:
            return None
        if rec.connection != "ONLINE" or rec.mode != "AUTOMATIC" or rec.fatal or rec.charging or not rec.task_id:
            return None
        task = self.dispatcher.tasks.get(rec.task_id) if self.dispatcher is not None else None
        if self.dispatcher is not None and (task is None or task.state != "ACTIVE" or task.robot != rec.serial):
            return None
        if rec.safety_field != "PROTECTIVE" or not rec.field_cause.startswith("forklift:"):
            return None
        if rec.protective_since is None or self.now - rec.protective_since < self.cfg.standoff_alarm_s:
            return None
        if rec.last_node is None or rec.driving or rec.dist_since > 1e-6 or self._goal_action_running(rec):
            return None
        if not rec.route or rec.state_order_id != rec.order_id or rec.state_remaining <= 0 \
                or rec.cur_idx >= rec.base_end or rec.route[rec.cur_idx] != rec.last_node:
            return None
        near = [tr for tr in stalled_fl if tr.confirmed and dist((tr.x, tr.y), (rec.x, rec.y)) < 4.5]
        return min(near, key=lambda tr: (dist((tr.x, tr.y), (rec.x, rec.y)), tr.id)) if near else None

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
        """Near misses the control room can know of.

        - the vehicle reports a protective stop caused by a person while it was driving faster than creep
          speed (it had to stop for someone in its way: the event safety teams log);
        - a confirmed person track inside the vehicle's stopping envelope on two consecutive cycles (one noisy
          camera frame is not enough).
        """
        seen_now: dict[tuple[str, str], float] = {}
        for rec in self.robots.values():
            if rec.connection != "ONLINE":
                continue
            stopped_for_person = rec.safety_field == "PROTECTIVE" and rec.field_cause.startswith("human") \
                and rec.prev_speed > CREEP_SPEED
            if stopped_for_person:
                self._flag_near_miss(rec, f"{rec.serial} {rec.prev_speed:.1f} m/s에서 사람 때문에 보호정지")
            rec.prev_speed = abs(rec.v) if rec.safety_field != "PROTECTIVE" else 0.0
            if abs(rec.v) < 0.1:
                continue
            # decelerationMax: the factsheet's maximum deceleration (the vehicle's braking for a stop)
            decel = float(((rec.factsheet or {}).get("physicalParameters") or {}).get("decelerationMax") or 1.0)
            heading = rec.theta if rec.v >= 0 else rec.theta + math.pi
            for tr in self.tracker.tracks.values():
                if tr.cls != "human" or not tr.confirmed:
                    continue
                if abs(tr.x - rec.x) > 4 or abs(tr.y - rec.y) > 4:
                    continue
                gap = path_gap((tr.x, tr.y), 0.3, (rec.x, rec.y), heading, rec.length / 2, rec.width / 2,
                               side=-NM_TRACK_MARGIN)
                # the margin is the camera track's position uncertainty (about 1.2 sigma of 0.25 m): the control
                # room only claims a near miss its own estimate can support
                if near_miss(abs(rec.v), gap, margin=NM_TRACK_MARGIN, decel=decel):
                    key = (rec.serial, tr.id)
                    seen_now[key] = tr.last_seen
                    # two separate observations, not the same camera frame seen by two control cycles
                    if key in self._nm_pending and self._nm_pending[key] != tr.last_seen:
                        self._flag_near_miss(rec, f"{rec.serial} {abs(rec.v):.1f} m/s 주행 중 추적된 사람 "
                                                  f"{max(gap, 0):.2f} m(정지거리 안)")
                    elif key in self._nm_pending:
                        seen_now[key] = self._nm_pending[key]
        self._nm_pending = seen_now

    def _flag_near_miss(self, rec: RobotRecord, msg: str) -> None:
        if self.now - self._nm_last.get(rec.serial, -1e9) <= 20.0:
            return
        self._nm_last[rec.serial] = self.now
        self.counters["near_miss"] += 1
        self.alarms.set("NEAR_MISS", rec.serial, True, self.now, msg, {"x": round(rec.x, 1), "y": round(rec.y, 1)})
        self.alarms.set("NEAR_MISS", rec.serial, False, self.now)

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
            # the head of a queue that is not itself waiting: stopped, idle in the lane, or busy with an action
            # (a tote AMR waiting for its picker) holds everyone behind it
            head = w is None and bool(behind)
            rec.head_since = (rec.head_since if rec.head_since is not None else self.now) if head else None
            root_stopped = w is not None and w.kind not in ("robot", "fault", "offline", "manual") and not covered or (
                head and rec.status in ("PROTECTIVE", "FAULT", "PAUSED", "OFFLINE", "MANUAL", "IDLE", "ACTION"))
            since = w.since if w is not None else max(rec.head_since or self.now,
                                                      rec.protective_since or rec.fault_since or -1e18)
            limit = self.cfg.blocked_alarm_s
            # waiting for a person or a manual truck is expected and clears by itself; only a long one needs the
            # operator (collaborative zones wait for people by design)
            collab_here = any(z.collaborative for z in self.layout.zones_at((rec.x, rec.y)))
            if w is not None and w.kind in ("human", "forklift") or \
                    collab_here and (rec.field_cause.startswith("human") or rec.status == "ACTION"):
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
            standoff = rec.protective_since is not None and self.now - rec.protective_since >= self.cfg.standoff_alarm_s
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
        humans = [tr for tr in self.tracker.tracks.values() if tr.cls == "human" and tr.confirmed]
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
                    if self.supports(rec, "x_fleetSpeedLimit"):     # others carry it in edge maxSpeed
                        # no "limit" parameter lifts the zone's limit (parameter values may not be null)
                        params = {"zoneId": zid} if limit is None else {"zoneId": zid, "limit": limit}
                        self._instant(rec, [action("x_fleetSpeedLimit", self._aid("x_speed"), "NONE", params)])
        if rebroadcast:
            self._last_zone_rebroadcast = self.now

    def active_zone_limits(self) -> dict[str, float]:
        return {k: v for k, v in self._zone_broadcast.items() if v is not None}

    def _enforce_fleet_mode(self) -> None:
        """Fleet-wide stop (non-safety: a software stop over the wireless link, not an ISO 13850 e-stop).

        Vehicles with the x_remoteStop extension get it; the others get the standard startPause.
        """
        for rec in self.robots.values():
            if rec.connection != "ONLINE" or not rec.has_state:
                continue
            remote_stop = self.supports(rec, "x_remoteStop")
            want_estop = self.mode == "ESTOP" and remote_stop
            want_pause = (self.mode == "HOLD" or self.mode == "ESTOP" and not remote_stop
                          or rec.serial in self.area_paused or rec.serial in self.robot_paused)
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

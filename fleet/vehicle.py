"""Simulated AMR speaking VDA 5050 as the vehicle side.

What the vehicle owns (and the fleet manager cannot override):
- it drives only the released base of its order and stops at the base end;
- its onboard safety fields stop or slow it regardless of fleet commands;
- faults latch until an explicit reset.

The fleet manager only sees what arrives on the bus (state/visualization), with
latency and loss; ground truth is used by the evaluator, never by the manager.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import random

from .geometry import box_gap, circle_hits_forward_box, dist, point_in_polygon, segment_hits_polygon, wrap_angle
from .layout import Layout
from .vda5050 import (INSTANT_ACTIONS, NODE_ACTIONS, HeaderIds, build_connection, header, topic,
                      validate_order)

# Automatic restart after a protective stop once the field has been clear this long
# (MiR fleet documentation describes a 2 s clear period before resuming).
PROTECTIVE_RESET_S = 2.0

FAULT_TYPES = {
    "DRIVE_FAULT": "구동부 과전류",
    "LOCALIZATION_LOST": "위치 추정 실패",
    "SENSOR_FAULT": "안전 스캐너 이상",
    "BUMPER": "범퍼 접촉(충돌)",
    "BATTERY_DEPLETED": "배터리 방전",
}


@dataclass
class VehicleSpec:
    length: float = 0.9
    width: float = 0.7
    radius: float = 0.45          # collision circle used by the evaluator
    max_speed: float = 1.5
    accel: float = 0.8
    decel: float = 1.2
    stop_decel: float = 2.5       # protective stop / e-stop
    turn_rate: float = 1.6        # rad/s, in-place rotation of a differential drive
    drain_per_m: float = 0.02     # battery % per metre (nominal)
    drain_idle_s: float = 0.003   # battery % per second
    charge_rate_s: float = 0.08   # battery % per second on a charger
    manual_max_speed: float = 0.5
    sense_range: float = 6.0
    # blind-corner rule: slow down within this distance of an intersection (None disables)
    intersection_speed: float | None = 0.6
    intersection_radius: float = 1.5


@dataclass
class Obstacle:
    id: str
    kind: str      # amr | human | forklift | external
    x: float
    y: float
    r: float


@dataclass
class ActionState:
    action_id: str
    action_type: str
    status: str = "WAITING"
    remaining_s: float = 0.0
    result: str = ""
    blocking: str = "HARD"
    params: dict = field(default_factory=dict)


class Vehicle:
    def __init__(self, serial: str, layout: Layout, start_node: str, rng: random.Random,
                 manufacturer: str = "sim", battery: float = 90.0,
                 spec: VehicleSpec | None = None) -> None:
        self.serial = serial
        self.manufacturer = manufacturer
        self.layout = layout
        self.rng = rng
        self.spec = spec or VehicleSpec()
        node = layout.nodes[start_node]
        self.x, self.y = node.x, node.y
        self.theta = 0.0
        self.v = 0.0
        self.omega = 0.0
        self.battery = battery
        self.charging = False
        self.odometer = 0.0
        self.perf = rng.uniform(0.86, 1.0)       # execution variance between units
        self.operating_mode = "AUTOMATIC"
        self.paused = False
        self.estop = False                      # remote (fleet) software stop
        self.fault: str | None = None
        self.fault_recoverable = True
        self.position_initialized = True
        self.order_id = ""
        self.order_update_id = 0
        self.nodes: list[dict] = [self._node_msg(start_node, 0, True)]
        self.edges: list[dict] = []
        self.last_node_id: str | None = start_node
        self.last_node_seq = 0
        self.progress = 0.0                     # metres along edges[0]
        self.actions: list[ActionState] = []
        self.finished_actions: list[ActionState] = []
        self.pending_cancel: ActionState | None = None
        self.field = "CLEAR"
        self.field_cause = ""
        self.clear_since = 0.0
        self.speed_limits: dict[str, float] = {}
        self.load = False
        self.manual_cmd = (0.0, 0.0, -1.0)      # v, omega, valid-until (sim time)
        self.errors: list[dict] = []
        self.detections: list[dict] = []
        self.protective_stops = 0
        self._ids = HeaderIds()
        self._state_dirty = True
        self._next_state = 0.0
        self._next_vis = 0.0
        self.stop_reason = ""

    # ------------------------------------------------------------------ helpers
    def _node_msg(self, node_id: str, seq: int, released: bool) -> dict:
        n = self.layout.nodes[node_id]
        return {"nodeId": node_id, "sequenceId": seq, "released": released,
                "nodePosition": {"x": n.x, "y": n.y, "mapId": "warehouse-a"}, "actions": []}

    @property
    def pos(self) -> tuple[float, float]:
        return (self.x, self.y)

    def base_end_index(self) -> int:
        idx = 0
        for i, n in enumerate(self.nodes):
            if n["released"]:
                idx = i
        return idx

    def base_finished(self) -> bool:
        return (self.base_end_index() == 0 and self.progress == 0.0
                and not any(a.status == "RUNNING" and a.blocking == "HARD" for a in self.actions))

    def driving(self) -> bool:
        return abs(self.v) > 1e-3 or abs(self.omega) > 1e-3

    def _error(self, error_type: str, description: str, level: str = "WARNING",
               refs: list[dict] | None = None) -> None:
        self.errors.append({"errorType": error_type, "errorLevel": level,
                            "errorDescription": description, "errorReferences": refs or []})
        self.errors = self.errors[-6:]
        self._state_dirty = True

    def set_fault(self, fault: str, recoverable: bool = True) -> None:
        if fault not in FAULT_TYPES:
            raise ValueError(fault)
        self.fault = fault
        self.fault_recoverable = recoverable
        if fault == "LOCALIZATION_LOST":
            self.position_initialized = False
        self.errors = [e for e in self.errors if e["errorLevel"] != "FATAL"]
        self._error(fault.lower(), FAULT_TYPES[fault], "FATAL")

    # ------------------------------------------------------------------ inbound
    def on_order(self, msg: dict) -> None:
        problems = validate_order(msg)
        if problems:
            self._error("validationError", "; ".join(problems[:3]))
            return
        if self.operating_mode != "AUTOMATIC":
            self._error("orderError", f"operatingMode {self.operating_mode} rejects orders")
            return
        if self.fault:
            self._error("orderError", f"fatal error {self.fault} active")
            return
        if msg["orderId"] == self.order_id:
            if msg["orderUpdateId"] < self.order_update_id:
                self._error("orderUpdateError", "orderUpdateId lower than current")
                return
            if msg["orderUpdateId"] == self.order_update_id:
                return  # duplicate delivery; state already reflects it
            end = self.nodes[self.base_end_index()]
            first = msg["nodes"][0]
            if first["nodeId"] != end["nodeId"] or first["sequenceId"] != end["sequenceId"]:
                self._error("orderUpdateError",
                            f"update must start at base end {end['nodeId']}/{end['sequenceId']}",
                            refs=[{"referenceKey": "nodeId", "referenceValue": first["nodeId"]}])
                return
            keep = self.base_end_index()
            end_seq = self.nodes[keep]["sequenceId"]
            self.actions = [a for a in self.actions
                            if not (a.status == "WAITING" and a.params.get("_seq", -1) > end_seq)]
            self.nodes = self.nodes[: keep + 1] + [dict(n) for n in msg["nodes"][1:]]
            self.edges = self.edges[:keep] + [dict(e) for e in msg["edges"]]
            self.order_update_id = msg["orderUpdateId"]
            for n in msg["nodes"][1:]:
                self._register_actions(n)
            self._state_dirty = True
            return
        # new order id
        if not self.base_finished():
            self._error("orderError", "new orderId while base not finished")
            return
        first = msg["nodes"][0]
        if self.last_node_id is None or first["nodeId"] != self.last_node_id:
            fx, fy = first["nodePosition"]["x"], first["nodePosition"]["y"]
            if dist((fx, fy), self.pos) > first["nodePosition"].get("allowedDeviationXY", 0.3):
                self._error("noRouteError", f"first node {first['nodeId']} is not the current node")
                return
            self.last_node_id = first["nodeId"]
        self.order_id = msg["orderId"]
        self.order_update_id = msg["orderUpdateId"]
        self.nodes = [dict(n) for n in msg["nodes"]]
        self.edges = [dict(e) for e in msg["edges"]]
        self.last_node_seq = first["sequenceId"]
        self.progress = 0.0
        self.actions = [a for a in self.actions if a.status in ("RUNNING",) and a.blocking != "HARD"]
        for n in msg["nodes"]:
            self._register_actions(n)
        self._start_node_actions()
        self._state_dirty = True

    def _register_actions(self, node: dict) -> None:
        for a in node.get("actions", []):
            if a["actionType"] not in NODE_ACTIONS:
                self._error("actionError", f"unsupported action {a['actionType']}")
                continue
            params = {p["key"]: p["value"] for p in a.get("actionParameters", [])}
            st = ActionState(a["actionId"], a["actionType"], "WAITING", blocking=a["blockingType"],
                             params={**params, "_node": node["nodeId"], "_seq": node["sequenceId"]})
            self.actions.append(st)

    def on_instant_actions(self, msg: dict, now: float) -> None:
        for a in msg.get("actions", []):
            kind = a.get("actionType")
            params = {p["key"]: p["value"] for p in a.get("actionParameters", [])}
            st = ActionState(a.get("actionId", "?"), kind, "FINISHED", blocking=a.get("blockingType", "NONE"))
            if kind not in INSTANT_ACTIONS:
                st.status, st.result = "FAILED", "unsupported"
            elif kind == "startPause":
                self.paused = True
            elif kind == "stopPause":
                self.paused = False
            elif kind == "cancelOrder":
                if self.progress > 0.0:
                    st.status = "RUNNING"         # finishes on the next node
                    self.pending_cancel = st
                    for n in self.nodes[2:]:
                        n["released"] = False
                    for e in self.edges[1:]:
                        e["released"] = False
                else:
                    self._drop_order("cancelOrder")
            elif kind == "stateRequest":
                pass
            elif kind == "x_fleetSpeedLimit":
                zone = str(params.get("zoneId"))
                limit = params.get("limit")
                if limit is None:
                    self.speed_limits.pop(zone, None)
                else:
                    self.speed_limits[zone] = float(limit)
            elif kind == "x_remoteStop":
                self.estop = bool(params.get("active", True))
            elif kind == "x_resetFault":
                if self.fault is None:
                    st.result = "no fault"
                elif self.fault in ("DRIVE_FAULT",) and not self.fault_recoverable:
                    st.status, st.result = "FAILED", "재발: 원격 리셋 불가, 현장 조치 필요"
                elif self.fault in ("LOCALIZATION_LOST", "BATTERY_DEPLETED"):
                    st.status, st.result = "FAILED", "리셋 대상 아님: initPosition 또는 충전 필요"
                else:
                    self._clear_fault()
            elif kind == "x_fieldServiceDone":
                if self.fault in ("DRIVE_FAULT", "SENSOR_FAULT", "BUMPER"):
                    self._clear_fault()
                elif self.fault == "BATTERY_DEPLETED":
                    self.battery = max(self.battery, 30.0)
                    self._clear_fault()
            elif kind == "initPosition":
                tx, ty = float(params.get("x", self.x)), float(params.get("y", self.y))
                if dist((tx, ty), self.pos) <= 1.0:
                    node = str(params.get("lastNodeId", self.layout.nearest_node((tx, ty))))
                    self.position_initialized = True
                    self.last_node_id = node
                    if self.fault == "LOCALIZATION_LOST":
                        self._clear_fault()
                else:
                    st.status, st.result = "FAILED", "스캔 정합 실패: 입력 자세가 실제와 1 m 이상 차이"
            elif kind == "x_setOperatingMode":
                self._set_mode(str(params.get("mode", "AUTOMATIC")), st)
            elif kind == "x_teleop":
                if self.operating_mode == "MANUAL":
                    vx = max(-0.3, min(self.spec.manual_max_speed, float(params.get("vx", 0.0))))
                    om = max(-1.2, min(1.2, float(params.get("omega", 0.0))))
                    self.manual_cmd = (vx, om, now + 0.6)
                else:
                    st.status, st.result = "FAILED", "MANUAL 모드가 아님"
            self.finished_actions.append(st)
            self.finished_actions = self.finished_actions[-8:]
        self._state_dirty = True

    def _clear_fault(self) -> None:
        self.fault = None
        self.fault_recoverable = True
        self.position_initialized = True
        self.errors = [e for e in self.errors if e["errorLevel"] != "FATAL"]
        self._state_dirty = True

    def _set_mode(self, mode: str, st: ActionState) -> None:
        if mode == "MANUAL":
            self.operating_mode = "MANUAL"
            self._drop_order("manual takeover")
            self.last_node_id = None
        elif mode == "AUTOMATIC":
            node = self.layout.nearest_node(self.pos)
            n = self.layout.nodes[node]
            if dist(self.pos, n.pos) > 0.6:
                st.status, st.result = "FAILED", f"노드 {node}에서 {dist(self.pos, n.pos):.1f} m: 0.6 m 이내로 이동 필요"
                return
            self.operating_mode = "AUTOMATIC"
            self.x, self.y = n.x, n.y
            self.last_node_id = node
            self.nodes = [self._node_msg(node, 0, True)]
            self.edges = []
            self.order_id = ""
            self.order_update_id = 0
            self.progress = 0.0
        self.manual_cmd = (0.0, 0.0, -1.0)
        self.v = self.omega = 0.0

    def _drop_order(self, reason: str) -> None:
        for a in self.actions:
            if a.status in ("WAITING", "RUNNING", "PAUSED", "INITIALIZING"):
                a.status, a.result = "FAILED", reason
                self.finished_actions.append(a)
        self.finished_actions = self.finished_actions[-8:]
        self.actions = []
        if self.last_node_id is not None:
            seq = self.nodes[0]["sequenceId"] if self.nodes else 0
            self.nodes = [self._node_msg(self.last_node_id, seq, True)]
        self.edges = []
        self.progress = 0.0
        if self.pending_cancel:
            self.pending_cancel.status = "FINISHED"
            self.pending_cancel = None
        self._state_dirty = True

    # ------------------------------------------------------------------ sensing
    def sense(self, obstacles: list[Obstacle], racks: list) -> None:
        """Onboard detections of people and forklifts, blocked by racks."""
        found = []
        rng2 = self.spec.sense_range
        for o in obstacles:
            if o.id == self.serial or o.kind not in ("human", "forklift") or abs(o.x - self.x) > rng2 or abs(o.y - self.y) > rng2:
                continue
            if math.hypot(o.x - self.x, o.y - self.y) > rng2:
                continue
            if any(segment_hits_polygon(self.pos, (o.x, o.y), r) for r in racks
                   if not point_in_polygon((o.x, o.y), r)):
                continue
            found.append({"cls": o.kind, "x": round(o.x + self.rng.gauss(0, 0.05), 2),
                          "y": round(o.y + self.rng.gauss(0, 0.05), 2)})
        self.detections = found

    def _fields(self, obstacles: list[Obstacle], reverse: bool = False) -> tuple[str, str]:
        s = self.spec
        heading = self.theta + (math.pi if reverse else 0.0)
        speed = abs(self.v)
        # clearing needs extra distance so a stopped vehicle does not creep back into its own field
        p_len = 0.35 + speed * 0.9 + (0.3 if self.field == "PROTECTIVE" else 0.0)
        w_len = max(2.5, 1.8 + speed * 1.2) + (0.5 if self.field != "CLEAR" else 0.0)
        # field-set switching: in rack aisles at creep speed the protective field narrows to the
        # body (reduced clearance is only allowed at <= 0.3 m/s)
        narrow = speed <= 0.35 and any(z.kind in ("aisle", "picking") for z in self.layout.zones_at(self.pos))
        p_half = s.width / 2 + (0.08 if narrow else 0.25)
        flank = 0.03 if narrow else 0.25
        rotating = abs(self.omega) > 1e-3 and speed < 0.05
        worst, cause = "CLEAR", ""
        for o in obstacles:
            if o.id == self.serial or abs(o.x - self.x) > 5.0 or abs(o.y - self.y) > 5.0:
                continue
            if rotating:
                if math.hypot(o.x - self.x, o.y - self.y) <= 0.75 + o.r:
                    return "PROTECTIVE", f"{o.kind}:{o.id}"
                continue
            if circle_hits_forward_box((o.x, o.y), o.r, self.pos, heading, p_len, p_half,
                                       start=s.length / 2):
                return "PROTECTIVE", f"{o.kind}:{o.id}"
            # 360-degree close-range field while moving: someone stepping into the flank
            if speed > 0.05 and box_gap((o.x, o.y), o.r, self.pos, self.theta,
                                        s.length / 2, s.width / 2) < flank:
                return "PROTECTIVE", f"{o.kind}:{o.id}"
            if worst == "CLEAR" and circle_hits_forward_box((o.x, o.y), o.r, self.pos, heading, w_len,
                                                             s.width / 2 + 0.45, start=s.length / 2):
                worst, cause = "WARNING", f"{o.kind}:{o.id}"
        return worst, cause

    # ------------------------------------------------------------------ motion
    def _speed_cap(self) -> float:
        s = self.spec
        cap = s.max_speed * self.perf
        static = self.layout.static_speed_at(self.pos)
        if static is not None:
            cap = min(cap, static)
        for zone in self.layout.zones_at(self.pos):
            if zone.id in self.speed_limits:
                cap = min(cap, self.speed_limits[zone.id])
        if self.edges:
            cap = min(cap, float(self.edges[0].get("maxSpeed") or cap))
            if s.intersection_speed is not None:
                e = self.edges[0]
                length = self._edge_len(e)
                near_start = self.progress < s.intersection_radius and self.layout.is_intersection(e["startNodeId"])
                near_end = length - self.progress < s.intersection_radius and self.layout.is_intersection(e["endNodeId"])
                if near_start or near_end:
                    cap = min(cap, s.intersection_speed)
        if self.field == "WARNING":
            cap = min(cap, 0.3 if self.field_cause.startswith(("human", "forklift")) else 0.6)
        return cap

    def _stop_distance_ahead(self) -> float:
        """Distance to the next point where the vehicle must be stationary."""
        end = self.base_end_index()
        if end == 0:
            return 0.0
        remaining = self._edge_len(self.edges[0]) - self.progress
        for i in range(1, end + 1):
            n = self.nodes[i]
            must_stop = (i == end or bool(n.get("actions")))
            if not must_stop and i < len(self.edges):
                h_in = self._edge_heading(self.edges[i - 1])
                h_out = self._edge_heading(self.edges[i])
                must_stop = abs(wrap_angle(h_out - h_in)) > 0.5
            if must_stop:
                return remaining
            if i < len(self.edges):
                remaining += self._edge_len(self.edges[i])
        return remaining

    def _edge_len(self, e: dict) -> float:
        if "_len" not in e:
            a = self.layout.nodes[e["startNodeId"]]
            b = self.layout.nodes[e["endNodeId"]]
            e["_len"] = math.hypot(b.x - a.x, b.y - a.y)
        return e["_len"]

    def _edge_heading(self, e: dict) -> float:
        a = self.layout.nodes[e["startNodeId"]]
        b = self.layout.nodes[e["endNodeId"]]
        return math.atan2(b.y - a.y, b.x - a.x)

    def step(self, dt: float, now: float, obstacles: list[Obstacle]) -> None:
        s = self.spec
        moved = 0.0
        # battery
        if self.charging and self.v == 0.0:
            self.battery = min(100.0, self.battery + s.charge_rate_s * dt)
        else:
            self.battery -= s.drain_idle_s * dt
        if self.battery <= 0.0 and self.fault != "BATTERY_DEPLETED":
            self.battery = 0.0
            self.set_fault("BATTERY_DEPLETED", recoverable=False)

        halted = self.fault is not None or self.estop or (self.paused and self.operating_mode == "AUTOMATIC")
        if self.operating_mode == "MANUAL":
            moved = self._step_manual(dt, now, obstacles, halted)
        else:
            moved = self._step_auto(dt, now, obstacles, halted)
        self.odometer += moved
        self.battery -= s.drain_per_m * moved
        self._step_actions(dt)

    def _update_field(self, now: float, obstacles: list[Obstacle], reverse: bool = False) -> None:
        state, cause = self._fields(obstacles, reverse)
        if state == "PROTECTIVE":
            if self.field != "PROTECTIVE" and abs(self.v) > 0.05:
                self.protective_stops += 1
            self.field, self.field_cause = "PROTECTIVE", cause
            self.clear_since = now
            self._state_dirty = True
        elif self.field == "PROTECTIVE" and now - self.clear_since < PROTECTIVE_RESET_S:
            pass  # auto-reset only after the field has stayed clear
        else:
            if state != self.field:
                self._state_dirty = True
            self.field, self.field_cause = state, cause

    def _step_manual(self, dt: float, now: float, obstacles: list[Obstacle], halted: bool) -> float:
        vx, om, until = self.manual_cmd
        if now > until or halted:
            vx, om = 0.0, 0.0
        self.omega = om if abs(vx) < 1e-3 else 0.0
        self.v = vx
        self._update_field(now, obstacles, reverse=vx < 0)
        if self.field == "PROTECTIVE":
            self.v = 0.0
            self.omega = 0.0 if abs(vx) > 1e-3 else self.omega
        self.theta = wrap_angle(self.theta + om * dt)
        nx = self.x + math.cos(self.theta) * self.v * dt
        ny = self.y + math.sin(self.theta) * self.v * dt
        if any(point_in_polygon((nx, ny), r) for r in self.layout.racks) or not (
                0.3 <= nx <= self.layout.width - 0.3 and 0.3 <= ny <= self.layout.height - 0.3):
            self.v = 0.0
            self.stop_reason = "수동 주행: 랙·벽 경계"
            return 0.0
        moved = math.hypot(nx - self.x, ny - self.y)
        self.x, self.y = nx, ny
        self.stop_reason = "원격 조종 중"
        return moved

    def _step_auto(self, dt: float, now: float, obstacles: list[Obstacle], halted: bool) -> float:
        s = self.spec
        running_hard = any(a.status == "RUNNING" and a.blocking == "HARD" for a in self.actions)
        end = self.base_end_index()
        if halted or running_hard or end == 0 or not self.position_initialized:
            self.omega = 0.0
            decel = s.stop_decel if (self.estop or self.fault) else s.decel
            self.v = max(0.0, self.v - decel * dt)
            moved = 0.0
            if self.v > 0.0 and self.edges and self.progress > 0.0:
                moved = self._advance(self.v * dt)
            self._update_field(now, obstacles)
            if self.fault:
                self.stop_reason = f"고장: {FAULT_TYPES[self.fault]}"
            elif self.estop:
                self.stop_reason = "관제 비상정지"
            elif self.paused:
                self.stop_reason = "관제 일시정지"
            elif running_hard:
                self.stop_reason = "작업 동작 수행"
            elif end == 0:
                self.stop_reason = "경로 해제 대기" if len(self.nodes) > 1 else ""
            return moved
        edge = self.edges[0]
        target = self._edge_heading(edge)
        if self.progress == 0.0:
            err = wrap_angle(target - self.theta)
            if abs(err) > 0.05:
                self.v = 0.0
                self.omega = math.copysign(min(s.turn_rate, abs(err) / dt), err)
                self._update_field(now, obstacles)
                if self.field == "PROTECTIVE":
                    self.omega = 0.0
                    self.stop_reason = f"보호필드 회전 정지({self.field_cause})"
                    return 0.0
                self.theta = wrap_angle(self.theta + self.omega * dt)
                self.stop_reason = ""
                return 0.0
            self.theta = target
        self.omega = 0.0
        self._update_field(now, obstacles)
        cap = self._speed_cap()
        d_stop = self._stop_distance_ahead()
        cap = min(cap, math.sqrt(max(0.0, 2 * s.decel * max(0.0, d_stop - 0.02))) + 0.05)
        if self.field == "PROTECTIVE":
            self.v = max(0.0, self.v - s.stop_decel * dt)
            self.stop_reason = f"보호필드 정지({self.field_cause})"
        elif self.v < cap:
            self.v = min(cap, self.v + s.accel * dt)
            self.stop_reason = ""
        else:
            self.v = max(cap, self.v - s.decel * dt)
            self.stop_reason = "감속 구역" if cap < 0.7 else ""
        return self._advance(self.v * dt)

    def _advance(self, step: float) -> float:
        if not self.edges or step <= 0.0:
            return 0.0
        edge = self.edges[0]
        length = self._edge_len(edge)
        a = self.layout.nodes[edge["startNodeId"]]
        b = self.layout.nodes[edge["endNodeId"]]
        new_progress = min(length, self.progress + step)
        moved = new_progress - self.progress
        self.progress = new_progress
        t = self.progress / length if length else 1.0
        self.x = a.x + (b.x - a.x) * t
        self.y = a.y + (b.y - a.y) * t
        if self.progress >= length - 1e-9:
            self._arrive()
        return moved

    def _arrive(self) -> None:
        self.nodes.pop(0)
        self.edges.pop(0)
        node = self.nodes[0]
        self.last_node_id = node["nodeId"]
        self.last_node_seq = node["sequenceId"]
        self.progress = 0.0
        n = self.layout.nodes[node["nodeId"]]
        self.x, self.y = n.x, n.y
        if self.pending_cancel is not None:
            self._drop_order("cancelOrder")
            self.v = 0.0
        self._start_node_actions()
        self._state_dirty = True

    def _start_node_actions(self) -> None:
        for a in self.actions:
            if a.status == "WAITING" and a.params.get("_node") == self.last_node_id \
                    and a.params.get("_seq") == self.last_node_seq:
                a.status = "RUNNING"
                a.remaining_s = float(a.params.get("duration", 0.0))
                if a.action_type == "startCharging":
                    if self.layout.nodes[self.last_node_id].kind == "charger":
                        self.charging = True
                        a.remaining_s = 0.0
                    else:
                        a.status, a.result = "FAILED", "not on a charger"
                elif a.action_type == "stopCharging":
                    self.charging = False
                    a.remaining_s = 0.0
                self._state_dirty = True

    def _step_actions(self, dt: float) -> None:
        if self.fault or self.estop or self.paused:
            return
        for a in self.actions:
            if a.status != "RUNNING":
                continue
            a.remaining_s -= dt
            if a.remaining_s <= 0.0:
                a.status = "FINISHED"
                if a.action_type == "pick":
                    self.load = True
                elif a.action_type == "drop":
                    self.load = False
                self._state_dirty = True
        done = [a for a in self.actions if a.status in ("FINISHED", "FAILED")]
        if done:
            self.finished_actions.extend(done)
            self.finished_actions = self.finished_actions[-8:]
            self.actions = [a for a in self.actions if a.status not in ("FINISHED", "FAILED")]

    # ------------------------------------------------------------------ outbound
    def state_msg(self, now: float) -> dict:
        t = topic(self.manufacturer, self.serial, "state")
        base = self.base_end_index()
        remaining_base = sum(self._edge_len(e) for e in self.edges[:base]) - self.progress
        msg = {
            **header(self._ids, t, self.manufacturer, self.serial, now),
            "orderId": self.order_id,
            "orderUpdateId": self.order_update_id,
            "zoneSetId": "warehouse-a",
            "lastNodeId": self.last_node_id or "",
            "lastNodeSequenceId": self.last_node_seq,
            "driving": self.driving(),
            "paused": self.paused,
            "newBaseRequest": bool(len(self.nodes) - 1 > base and remaining_base < 2.0),
            "distanceSinceLastNode": round(self.progress, 3),
            "operatingMode": self.operating_mode,
            "nodeStates": [{"nodeId": n["nodeId"], "sequenceId": n["sequenceId"], "released": n["released"]}
                           for n in self.nodes[1:]],
            "edgeStates": [{"edgeId": e["edgeId"], "sequenceId": e["sequenceId"], "released": e["released"]}
                           for e in self.edges],
            "agvPosition": {"x": round(self.x, 3), "y": round(self.y, 3), "theta": round(self.theta, 3),
                            "mapId": "warehouse-a", "positionInitialized": self.position_initialized,
                            "localizationScore": 0.9 if self.position_initialized else 0.1},
            "velocity": {"vx": round(self.v, 3), "vy": 0.0, "omega": round(self.omega, 3)},
            "loads": [{"loadId": f"{self.serial}-L", "loadType": "tote"}] if self.load else [],
            "actionStates": [{"actionId": a.action_id, "actionType": a.action_type,
                              "actionStatus": a.status, "resultDescription": a.result}
                             for a in (self.finished_actions[-4:] + self.actions)],
            "batteryState": {"batteryCharge": round(self.battery, 1), "charging": self.charging,
                             "reach": round(max(0.0, self.battery - 5.0) / self.spec.drain_per_m)},
            "errors": list(self.errors),
            "information": ([{"infoType": "stopReason", "infoLevel": "INFO",
                              "infoDescription": self.stop_reason}] if self.stop_reason else []),
            "safetyState": {"eStop": "REMOTE" if self.estop else "NONE",
                            "fieldViolation": self.field == "PROTECTIVE"},
            "x_safetyField": {"state": self.field, "cause": self.field_cause},
            "x_detections": self.detections,
        }
        # warnings are reported once, FATAL errors stay until cleared
        self.errors = [e for e in self.errors if e["errorLevel"] == "FATAL"]
        return msg

    def visualization_msg(self, now: float) -> dict:
        t = topic(self.manufacturer, self.serial, "visualization")
        return {**header(self._ids, t, self.manufacturer, self.serial, now),
                "agvPosition": {"x": round(self.x, 3), "y": round(self.y, 3), "theta": round(self.theta, 3),
                                "mapId": "warehouse-a", "positionInitialized": self.position_initialized},
                "velocity": {"vx": round(self.v, 3), "vy": 0.0, "omega": round(self.omega, 3)}}

    def connection_msg(self, now: float, state: str = "ONLINE") -> dict:
        t = topic(self.manufacturer, self.serial, "connection")
        return build_connection(header(self._ids, t, self.manufacturer, self.serial, now), state)

    def publish_due(self, now: float, state_period: float = 1.0, vis_period: float = 0.2) -> list[tuple[str, dict]]:
        out = []
        if self._state_dirty or now >= self._next_state:
            out.append((topic(self.manufacturer, self.serial, "state"), self.state_msg(now)))
            self._state_dirty = False
            self._next_state = now + state_period
        if now >= self._next_vis:
            out.append((topic(self.manufacturer, self.serial, "visualization"), self.visualization_msg(now)))
            self._next_vis = now + vis_period
        return out

"""Facility integration: rapid-roll doors that autonomous forklifts drive through.

Two sides, joined only by the bus, the way a door PLC and a fleet manager are:

- ``DoorDevice`` is the door itself (simulation ground truth). It opens on
  request, closes a few seconds after the last request, will not close on
  something standing in the opening (presence sensor) and publishes its state.
  When it is not fully open, its leaf is a physical obstacle, so a vehicle that
  drives at a closed door is stopped by its own protective field.
- ``DoorSupervisor`` is the control-room side. It asks for the door early
  enough that a forklift normally does not stop, lets the reservation layer
  release a door lane only while the door reports OPEN, and turns a door that
  does not answer into an alarm and a detour.

Messages follow Open-RMF's door adapter (DoorRequest / DoorState with modes
CLOSED, MOVING, OPEN); the topic names are this project's own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from .geometry import Point, seg_point_dist

DOOR_TOPIC = "facility/v1/door"
FIRE_TOPIC = "facility/v1/fire/state"      # the fire panel: {"alarm": bool}
OPEN_S = 3.0            # rapid-roll door, 4.5 m high curtain (nominal, see docs/SITE.md)
CLOSE_S = 3.0
CLOSE_DELAY_S = 5.0     # stays open this long after the last request
STATE_PERIOD_S = 1.0
PRESENCE_M = 0.9        # presence sensor: no closing while anything is this close to the threshold


@dataclass(frozen=True)
class DoorSpec:
    id: str
    name: str
    a: Point            # threshold line, one jamb to the other
    b: Point
    lanes: frozenset = frozenset()      # roadmap lanes that pass through the opening

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "a": list(self.a), "b": list(self.b), "lanes": sorted(self.lanes)}


class DoorDevice:
    """The door controller and its curtain."""

    def __init__(self, spec: DoorSpec, bus) -> None:
        self.spec = spec
        self.bus = bus
        self.progress = 0.0              # 0 closed, 1 open
        self.moving = 0                  # +1 opening, -1 closing
        self.last_request = -1e9
        self.requesters: set[str] = set()
        # "JAM": curtain stuck shut, the controller does not notice; "MOTOR": stuck shut and reported;
        # "NO_RESPONSE": controller silent
        self.fault: str | None = None
        self.pending_fault: str | None = None      # curtain faults take effect when the door next closes
        self.repair_at: float | None = None        # random faults: when the field team has fixed it
        self._next_state = 0.0
        self._last_sent: str | None = None
        self.fire = False                          # fire alarm: close as soon as the opening is clear, stay shut
        bus.subscribe(f"{DOOR_TOPIC}/{spec.id}/request", self._on_request)
        bus.subscribe(FIRE_TOPIC, self._on_fire)

    def _on_fire(self, _topic: str, msg: dict) -> None:
        self.fire = bool(msg.get("alarm"))

    def inject(self, fault: str, repair_at: float | None = None) -> None:
        """A curtain fault (JAM, MOTOR) strikes on the next close, so it never drops onto a vehicle;
        NO_RESPONSE (network to the controller lost) is immediate."""
        self.repair_at = repair_at
        if fault == "NO_RESPONSE" or self.progress <= 0.0:
            self.fault = fault
        else:
            self.pending_fault = fault

    def repair(self) -> None:
        self.fault = None
        self.pending_fault = None
        self.repair_at = None

    def maintain(self, now: float) -> None:
        if self.repair_at is not None and now >= self.repair_at:
            self.repair()

    @property
    def mode(self) -> str:
        if self.progress >= 1.0:
            return "OPEN"
        if self.progress <= 0.0 and self.moving == 0:
            return "CLOSED"
        return "MOVING"

    def _on_request(self, _topic: str, msg: dict) -> None:
        if self.fault or self.fire:
            return
        if msg["mode"] == "OPEN":
            self.last_request = self.bus.now
            self.requesters = set(msg.get("requesters", []))

    def occupied(self, bodies: list[tuple[float, float, float]]) -> bool:
        return any(seg_point_dist((x, y), self.spec.a, self.spec.b) < PRESENCE_M + r for x, y, r in bodies)

    def step(self, dt: float, now: float, bodies: list[tuple[float, float, float]]) -> None:
        if self.fault in ("JAM", "MOTOR"):
            self.moving = 0
            return
        want_open = now - self.last_request < CLOSE_DELAY_S and not self.fire
        if want_open and self.progress < 1.0:
            self.moving = 1
        elif not want_open and self.progress > 0.0:
            self.moving = 0 if self.occupied(bodies) else -1
        if self.moving > 0:
            self.progress = min(1.0, self.progress + dt / OPEN_S)
        elif self.moving < 0:
            self.progress = max(0.0, self.progress - dt / CLOSE_S)
        if self.progress in (0.0, 1.0):
            self.moving = 0
        if self.progress <= 0.0 and self.pending_fault:
            self.fault, self.pending_fault = self.pending_fault, None

    def obstacles(self) -> list[tuple[float, float, float]]:
        """The curtain while it is not fully open: a line of small circles across the opening."""
        if self.progress >= 1.0:
            return []
        (ax, ay), (bx, by) = self.spec.a, self.spec.b
        n = max(2, int(math.hypot(bx - ax, by - ay) / 0.25) + 1)
        return [(ax + (bx - ax) * i / (n - 1), ay + (by - ay) * i / (n - 1), 0.12) for i in range(n)]

    def publish_due(self, now: float) -> None:
        if self.fault == "NO_RESPONSE":
            return
        if now >= self._next_state or self.mode != self._last_sent:
            self._next_state = now + STATE_PERIOD_S
            self._last_sent = self.mode
            self.bus.publish(f"{DOOR_TOPIC}/{self.spec.id}/state", {
                "doorName": self.spec.id, "currentMode": self.mode, "stamp": round(now, 3),
                "fault": "MOTOR_OVERCURRENT" if self.fault == "MOTOR" else "", "fire": self.fire,
                "requesters": sorted(self.requesters)}, now)


@dataclass
class DoorView:
    """What the control room knows about one door."""
    mode: str = "UNKNOWN"
    fault: str = ""
    last_rx: float = -1e9
    requested_since: float | None = None
    requesters: set = field(default_factory=set)
    disabled: bool = False
    fail_cause: str = ""          # "", "stale", "fault" or "timeout"
    opens: int = 0


class DoorSupervisor:
    """Door handling inside the fleet manager.

    A failure has one of three causes. "stale" (controller silent) and "fault"
    (controller reports one) clear by themselves when the door reports healthy
    again. "timeout" (asked to open, stayed shut, no fault reported: a jammed
    curtain) only clears when the door is seen OPEN or an operator retries after
    the field fix; clearing it by itself would send traffic back into the jam.
    """

    RESPONSE_TIMEOUT_S = 10.0      # requested but not open by then: treat the door as failed
    STALE_S = 3.0                  # no state message for this long: controller not answering
    PREREQUEST_M = 15.0            # ask for the door this far ahead along the route
    REQUEST_PERIOD_S = 1.0

    def __init__(self, doors: dict[str, DoorSpec], bus, alarms, event) -> None:
        self.specs = doors
        self.bus = bus
        self.alarms = alarms
        self.event = event
        self.view = {d: DoorView() for d in doors}
        self.lane_door = {lane: d.id for d in doors.values() for lane in d.lanes}
        self._next_request: dict[str, float] = {d: 0.0 for d in doors}
        bus.subscribe(f"{DOOR_TOPIC}/+/state", self._on_state)

    def _on_state(self, topic: str, msg: dict) -> None:
        v = self.view.get(topic.split("/")[3])
        if v is None:
            return
        if v.mode != "OPEN" and msg["currentMode"] == "OPEN":
            v.opens += 1
        v.mode = msg["currentMode"]
        v.fault = msg.get("fault", "")
        v.last_rx = self.bus.now

    def _fresh(self, v: DoorView, now: float) -> bool:
        if v.last_rx < -1e8:
            return now <= self.STALE_S        # start-up grace before the first state message
        return now - v.last_rx <= self.STALE_S

    def is_open(self, door_id: str, now: float) -> bool:
        v = self.view[door_id]
        return v.mode == "OPEN" and not v.disabled and self._fresh(v, now)

    def available(self, door_id: str, now: float) -> bool:
        """Usable for planning: enabled, answering and not failed."""
        v = self.view[door_id]
        return not v.disabled and not v.fail_cause and self._fresh(v, now)

    def unavailable_lanes(self, now: float) -> set[str]:
        return {lane for lane, d in self.lane_door.items() if not self.available(d, now)}

    def tick(self, now: float, wanted: dict[str, set[str]], holders: dict[str, set[str]] | None = None) -> None:
        """``wanted``: vehicles approaching each door; ``holders``: vehicles already holding a lane through it.

        A disabled or failed door gets no new traffic, but a vehicle that reserved its lane while it was open
        is still let through: taking the door out of use must not shut it in front of that vehicle.
        """
        holders = holders or {}
        for door_id, v in self.view.items():
            who = wanted.get(door_id, set())
            held = holders.get(door_id, set())
            fresh = self._fresh(v, now)
            name = self.specs[door_id].name
            if held or who and not v.disabled and not v.fail_cause:
                if now >= self._next_request[door_id]:
                    self._next_request[door_id] = now + self.REQUEST_PERIOD_S
                    self.bus.publish(f"{DOOR_TOPIC}/{door_id}/request",
                                     {"doorName": door_id, "mode": "OPEN", "requesters": sorted(who),
                                      "stamp": round(now, 3)}, now)
                if v.mode == "OPEN" and fresh:
                    v.requested_since = None
                elif v.requested_since is None:
                    v.requested_since = now
            else:
                v.requested_since = None
            v.requesters = set(who)
            cause, why = "", ""
            if not fresh:
                cause, why = "stale", f"상태 {now - v.last_rx:.0f}초 무응답"
            elif v.fault:
                cause, why = "fault", f"제어기 고장 신호 {v.fault}"
            elif v.requested_since is not None and now - v.requested_since > self.RESPONSE_TIMEOUT_S:
                cause, why = "timeout", f"열림 요청 후 {now - v.requested_since:.0f}초 미개방"
            if cause and not v.fail_cause:
                v.fail_cause = cause
                v.requested_since = None
                self.event("door_fault", door_id, f"{name} 이상: {why} · 우회 경로로 전환", "crit")
                self.alarms.set("FACILITY_FAULT", door_id, True, now,
                                f"{name}: {why} · 영향 차량 {', '.join(sorted(who)) or '없음'}")
            elif v.fail_cause and not cause and (v.fail_cause != "timeout" or v.mode == "OPEN" and fresh):
                v.fail_cause = ""
                self.event("door_ok", door_id, f"{name} 정상 응답 복구", "info")
                self.alarms.set("FACILITY_FAULT", door_id, False, now)

    def retry(self, door_id: str, now: float) -> None:
        """Operator retries after the field fix: forget the failure; a door still jammed fails again."""
        v = self.view[door_id]
        v.disabled = False
        v.requested_since = None
        if v.fail_cause:
            v.fail_cause = ""
            self.alarms.set("FACILITY_FAULT", door_id, False, now)

    def views(self, now: float) -> list[dict]:
        out = []
        for door_id, v in self.view.items():
            spec = self.specs[door_id]
            out.append({"id": door_id, "name": spec.name, "mode": v.mode, "fault": v.fault,
                        "age": round(now - v.last_rx, 1) if v.last_rx > -1e8 else None,
                        "available": self.available(door_id, now), "disabled": v.disabled,
                        "cause": v.fail_cause, "requesters": sorted(v.requesters),
                        "waiting_s": round(now - v.requested_since, 1) if v.requested_since is not None else 0.0,
                        "opens": v.opens, "a": list(spec.a), "b": list(spec.b)})
        return out


def doors_crossing(doors: list[DoorSpec], edges, nodes) -> dict[str, DoorSpec]:
    """Attach to each door the lanes whose segment crosses its threshold."""
    from .geometry import segments_intersect
    out = {}
    for d in doors:
        lanes = frozenset(e.lane for e in edges.values()
                          if segments_intersect(d.a, d.b, nodes[e.start].pos, nodes[e.end].pos))
        out[d.id] = DoorSpec(d.id, d.name, d.a, d.b, lanes)
    return out

"""Task generation, assignment, charging and parking.

Assignment is greedy by task priority then age: the idle robot with the lowest
estimated travel time to the pickup wins, if its battery can finish the task
and still reach a charger with a reserve left. The battery model is nominal
(per-metre drain), not a measured discharge curve.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import random

from .control import FleetManager, RobotRecord
from .vda5050 import action

TASK_LABEL = {"QUEUED": "대기", "TO_PICKUP": "적재 이동", "TO_DROPOFF": "하역 이동", "DONE": "완료",
              "FAILED": "실패", "CANCELED": "취소"}


@dataclass
class Task:
    id: str
    kind: str                  # PICK (storage -> outbound) | PUTAWAY (inbound -> storage)
    source: str
    dest: str | None
    priority: int
    created: float
    state: str = "QUEUED"
    robot: str | None = None
    assigned_at: float | None = None
    picked_at: float | None = None
    done_at: float | None = None
    attempts: int = 0
    note: str = ""

    def to_json(self, now: float) -> dict:
        return {"id": self.id, "kind": self.kind, "src": self.source, "dst": self.dest, "pri": self.priority,
                "state": self.state, "label": TASK_LABEL.get(self.state, self.state), "robot": self.robot,
                "age": round(now - self.created, 1), "attempts": self.attempts, "note": self.note}


class Dispatcher:
    def __init__(self, manager: FleetManager, rng: random.Random, rate_per_h: float = 300.0,
                 pick_s: float = 6.0, drop_s: float = 5.0, charge_low: float = 30.0, charge_full: float = 90.0,
                 reserve: float = 12.0, overdue_s: float = 600.0) -> None:
        self.m = manager
        self.layout = manager.layout
        self.planner = manager.planner
        self.rng = rng
        self.rate_per_h = rate_per_h
        self.pick_s = pick_s
        self.drop_s = drop_s
        self.charge_low = charge_low
        self.charge_full = charge_full
        self.reserve = reserve
        self.overdue_s = overdue_s
        self.tasks: dict[str, Task] = {}
        self.finished: deque = deque(maxlen=2000)
        self._seq = 0
        self._aseq = 0
        self.next_arrival = 0.0
        self.charger_owner: dict[str, str] = {}
        self.park_owner: dict[str, str] = {}
        self.charge_wait_since: dict[str, float] = {}
        self.storage = self.layout.nodes_of_kind("storage")
        self.inbound = self.layout.nodes_of_kind("inbound")
        self.outbound = self.layout.nodes_of_kind("outbound")
        self.chargers = self.layout.nodes_of_kind("charger")
        self.parks = self.layout.nodes_of_kind("park")
        self.counts = {"created": 0, "done": 0, "failed": 0, "canceled": 0, "requeued": 0}
        manager.dispatcher = self

    # ------------------------------------------------------------ tasks
    def task_priority(self, task_id: str) -> int:
        t = self.tasks.get(task_id)
        return t.priority if t else 0

    def add_task(self, kind: str, source: str, dest: str | None = None, priority: int = 0) -> Task:
        if kind not in ("PICK", "PUTAWAY"):
            raise ValueError("kind must be PICK or PUTAWAY")
        if source not in self.layout.nodes or (dest is not None and dest not in self.layout.nodes):
            raise ValueError("unknown node")
        self._seq += 1
        t = Task(f"T{self._seq:05d}", kind, source, dest, max(0, min(2, int(priority))), self.m.now)
        self.tasks[t.id] = t
        self.counts["created"] += 1
        return t

    def _spawn(self, now: float) -> None:
        if self.rate_per_h <= 0:
            self.next_arrival = now + 60.0
            return
        while self.next_arrival <= now:
            r = self.rng.random()
            pri = 2 if r < 0.08 else 1 if r < 0.25 else 0
            if self.rng.random() < 0.6:
                self.add_task("PICK", self.rng.choice(self.storage), None, pri)
            else:
                self.add_task("PUTAWAY", self.rng.choice(self.inbound), self.rng.choice(self.storage), pri)
            self.next_arrival += self.rng.expovariate(self.rate_per_h / 3600.0)

    def _aid(self, kind: str) -> str:
        self._aseq += 1
        return f"{kind}-{self._aseq}"

    # ------------------------------------------------------------ tick
    def tick(self, now: float) -> None:
        self._spawn(now)
        for rec in self.m.robots.values():
            self._advance_job(rec, now)
        self._fault_requeue(now)
        if self.m.mode == "RUNNING":
            self._assign(now)
            for rec in self.m.robots.values():
                self._idle_policy(rec, now)
        overdue = [t for t in self.tasks.values() if t.state == "QUEUED" and now - t.created > self.overdue_s]
        self.m.alarms.set("TASK_OVERDUE", "tasks", bool(overdue), now,
                          f"{len(overdue)}건이 {self.overdue_s / 60:.0f}분 넘게 배정 대기" if overdue else "")
        starving = [s for s, t0 in self.charge_wait_since.items() if now - t0 > 60.0]
        self.m.alarms.set("CHARGER_SHORTAGE", "chargers", bool(starving), now,
                          f"충전 대기 {len(starving)}대: {', '.join(starving[:5])}" if starving else "")

    def _advance_job(self, rec: RobotRecord, now: float) -> None:
        if not rec.route or not self.m.route_done(rec):
            return
        task = self.tasks.get(rec.task_id) if rec.task_id else None
        purpose = rec.purpose
        failed = self.m.goal_action_failed(rec)
        if purpose == "pickup" and task:
            if failed:
                self._requeue(task, "적재 실패")
                self.m.clear_route(rec)
                return
            task.state = "TO_DROPOFF"
            task.picked_at = now
            if task.dest is None:
                task.dest = self._choose_outbound(task.source)
            drop = action("drop", self._aid(f"{task.id}-drop"), "HARD", {"duration": self.drop_s})
            if not self.m.assign_route(rec, task.dest, "dropoff", [drop], task.id):
                task.state, task.note = "FAILED", "하역 경로 없음"
                self.counts["failed"] += 1
                self.m.clear_route(rec)
        elif purpose == "dropoff" and task:
            task.state = "FAILED" if failed else "DONE"
            task.done_at = now
            self.counts["failed" if failed else "done"] += 1
            self.finished.append(task)
            del self.tasks[task.id]
            self.m.clear_route(rec)
            self.m.event("task_done", rec.serial, f"{task.id} 완료 {now - task.created:.0f}초", "info")
        elif purpose == "evade" and rec.resume:
            goal, p, acts, task_id = rec.resume
            if self.m.assign_route(rec, goal, p, list(acts), task_id):
                rec.resume = None
            else:
                rec.purpose = "evade"
        elif purpose == "charge":
            self.m.clear_route(rec)
            rec.purpose = "charging"
        elif purpose == "park":
            self.m.clear_route(rec)
            rec.purpose = "parked"
        else:
            self.m.clear_route(rec)

    def _choose_outbound(self, near: str) -> str:
        load = {d: 0 for d in self.outbound}
        for t in self.tasks.values():
            if t.dest in load and t.state in ("TO_PICKUP", "TO_DROPOFF"):
                load[t.dest] += 1
        return min(self.outbound, key=lambda d: (load[d], self.planner.travel_time(near, d)))

    def _energy(self, metres: float) -> float:
        return metres * 0.02 * 1.3

    def _feasible(self, rec: RobotRecord, task: Task) -> bool:
        start = rec.last_node
        dest = task.dest or self._choose_outbound(task.source)
        d = self.planner.distance(start, task.source) + self.planner.distance(task.source, dest)
        d += min(self.planner.distance(dest, c) for c in self.chargers)
        return rec.battery - self._energy(d) >= self.reserve

    def _available(self, rec: RobotRecord) -> bool:
        if rec.task_id or rec.serial in self.m.robot_paused or rec.serial in self.m.area_paused:
            return False
        if rec.purpose not in ("", "parked", "park", "charging"):
            return False
        if not self.m.can_take_route(rec):
            return False
        floor = 60.0 if rec.charging else self.charge_low
        return rec.battery >= floor

    def _dock_busy(self, dock: str, ignore: str | None = None) -> bool:
        return any(t.source == dock and t.state == "TO_PICKUP" and t.id != ignore for t in self.tasks.values())

    def _assign(self, now: float) -> None:
        idle = [r for r in self.m.robots.values() if self._available(r)]
        if not idle:
            return
        queue = sorted((t for t in self.tasks.values() if t.state == "QUEUED"),
                       key=lambda t: (-t.priority, t.created))
        for task in queue:
            if not idle:
                break
            if task.kind == "PUTAWAY" and self._dock_busy(task.source):
                continue
            best, best_cost = None, math.inf
            for rec in idle:
                if not self._feasible(rec, task):
                    continue
                cost = self.planner.travel_time(rec.last_node, task.source)
                cost += max(0.0, 80.0 - rec.battery) * 1.5
                if cost < best_cost:
                    best, best_cost = rec, cost
            if best is None:
                continue
            start_actions = []
            if best.charging:
                start_actions.append(action("stopCharging", self._aid(f"{best.serial}-stopchg"), "HARD"))
            pick = action("pick", self._aid(f"{task.id}-pick"), "HARD", {"duration": self.pick_s})
            if not self.m.assign_route(best, task.source, "pickup", [pick], task.id, start_actions=start_actions):
                continue
            self._release_spots(best.serial)
            task.state, task.robot, task.assigned_at = "TO_PICKUP", best.serial, now
            if task.kind == "PICK" and task.dest is None:
                task.dest = self._choose_outbound(task.source)
            idle.remove(best)
            self.charge_wait_since.pop(best.serial, None)

    def _release_spots(self, serial: str) -> None:
        for d in (self.charger_owner, self.park_owner):
            for k in [k for k, v in d.items() if v == serial]:
                del d[k]

    def _idle_policy(self, rec: RobotRecord, now: float) -> None:
        if rec.task_id or rec.route or not self.m.can_take_route(rec):
            return
        if rec.serial in self.m.robot_paused or rec.serial in self.m.area_paused:
            return
        here = rec.last_node
        kind = self.layout.nodes[here].kind if here else ""
        if kind == "charger" and not rec.charging and rec.battery < self.charge_full:
            start = action("startCharging", self._aid(f"{rec.serial}-chg"), "HARD")
            if self.m.assign_route(rec, here, "charge", [start]):
                self.charger_owner[here] = rec.serial
            return
        if rec.charging or kind == "charger":
            self.charger_owner[here] = rec.serial
            if rec.battery >= self.charge_full and any(s for s in self.charge_wait_since if s != rec.serial):
                self.send_away(rec, exclude=set(), leave_charger=True)
            return
        if rec.battery < self.charge_low:
            free = [c for c in self.chargers if c not in self.charger_owner
                    and self.m.locks.holder_of(f"N:{c}") in (None, rec.serial)]
            if free:
                c = min(free, key=lambda c: self.planner.travel_time(here, c))
                start = action("startCharging", self._aid(f"{rec.serial}-chg"), "HARD")
                if self.m.assign_route(rec, c, "charge", [start]):
                    self._release_spots(rec.serial)
                    self.charger_owner[c] = rec.serial
                    self.charge_wait_since.pop(rec.serial, None)
                    return
            self.charge_wait_since.setdefault(rec.serial, now)
        if kind == "park":
            self.park_owner[here] = rec.serial
            rec.purpose = "parked"
            return
        self.send_away(rec, exclude=set())

    def send_away(self, rec: RobotRecord, exclude: set[str], leave_charger: bool = False) -> bool:
        """Move an idle robot to a free parking spot (or aside) so it stops blocking traffic."""
        here = rec.last_node
        if here is None:
            return False
        free = [p for p in self.parks if p not in self.park_owner and p not in exclude
                and self.m.locks.holder_of(f"N:{p}") in (None, rec.serial)]
        start = [action("stopCharging", self._aid(f"{rec.serial}-stopchg"), "HARD")] if rec.charging else []
        for p in sorted(free, key=lambda p: self.planner.travel_time(here, p))[:3]:
            if self.m.assign_route(rec, p, "park", start_actions=start):
                self._release_spots(rec.serial)
                self.park_owner[p] = rec.serial
                return True
        spot = self.m._evasion_spot(rec, exclude)
        if spot and self.m.assign_route(rec, spot, "reposition", start_actions=start):
            self._release_spots(rec.serial)
            return True
        return False

    # ------------------------------------------------------------ failures and operator actions
    def _requeue(self, task: Task, why: str) -> None:
        task.state, task.robot, task.assigned_at = "QUEUED", None, None
        task.attempts += 1
        task.note = why
        if task.kind == "PICK" and task.picked_at is None:
            task.dest = None
        self.counts["requeued"] += 1
        self.m.event("task_requeued", task.id, f"{task.id} 재대기: {why}", "warn")

    def detach(self, rec: RobotRecord, why: str) -> None:
        """Take the task away from a robot that keeps its reservations until it stops."""
        task = self.tasks.get(rec.task_id) if rec.task_id else None
        if task and task.picked_at is None:
            self._requeue(task, why)
        rec.task_id = None
        rec.goal_actions = {}
        if rec.route:
            rec.route = rec.route[: rec.base_end + 1]
            rec.purpose = "recover"
        else:
            rec.purpose = ""
        self.m.instant(rec.serial, "cancelOrder")

    def _fault_requeue(self, now: float) -> None:
        for rec in self.m.robots.values():
            task = self.tasks.get(rec.task_id) if rec.task_id else None
            if task is None or task.picked_at is not None:
                continue
            fault_long = rec.fatal and rec.fault_since is not None and now - rec.fault_since > 90.0
            offline_long = rec.connection != "ONLINE" and now - rec.last_seen > 30.0
            if fault_long or offline_long:
                self.detach(rec, "고장 장기화" if fault_long else "통신 두절 30초")

    def cancel(self, task_id: str) -> str:
        task = self.tasks[task_id]
        if task.state == "QUEUED":
            task.state = "CANCELED"
        elif task.picked_at is not None:
            raise ValueError("적재 중인 작업은 취소할 수 없습니다: 하역 완료 후 처리하거나 현장 회수로 종결")
        else:
            rec = self.m.robots[task.robot]
            self.detach(rec, "운영자 취소")
            task.state = "CANCELED"
        self.counts["canceled"] += 1
        self.finished.append(task)
        del self.tasks[task_id]
        return f"{task_id} 취소"

    def reassign(self, task_id: str) -> str:
        task = self.tasks[task_id]
        if task.robot is None:
            return f"{task_id}는 배정 대기 중"
        rec = self.m.robots[task.robot]
        if task.picked_at is not None:
            task.state, task.note = "FAILED", "적재물 현장 회수"
            self.counts["failed"] += 1
            self.finished.append(task)
            del self.tasks[task_id]
            self.detach(rec, "현장 회수")
            return f"{task_id} 실패 처리(적재물 현장 회수)"
        self.detach(rec, "운영자 재할당")
        return f"{task_id} 재할당 대기열로 이동"

    def reprioritize(self, task_id: str, priority: int) -> str:
        task = self.tasks[task_id]
        task.priority = max(0, min(2, int(priority)))
        return f"{task_id} 우선순위 {task.priority}"

    # ------------------------------------------------------------ views
    def queue_view(self, now: float, limit: int = 40) -> list[dict]:
        order = {"TO_DROPOFF": 0, "TO_PICKUP": 1, "QUEUED": 2}
        items = sorted(self.tasks.values(), key=lambda t: (order.get(t.state, 3), -t.priority, t.created))
        return [t.to_json(now) for t in items[:limit]]

    def stats(self, now: float, window: float = 3600.0) -> dict:
        recent = [t for t in self.finished if t.state == "DONE" and t.done_at is not None and now - t.done_at <= window]
        cycles = sorted(t.done_at - t.created for t in recent)
        waits = sorted(t.assigned_at - t.created for t in recent if t.assigned_at is not None)
        span = min(window, max(now, 1.0))

        def pct(values: list[float], q: float) -> float | None:
            if not values:
                return None
            return round(values[min(len(values) - 1, int(q * len(values)))], 1)

        return {
            "queued": sum(1 for t in self.tasks.values() if t.state == "QUEUED"),
            "active": sum(1 for t in self.tasks.values() if t.state in ("TO_PICKUP", "TO_DROPOFF")),
            "throughput_h": round(len(recent) * 3600.0 / span, 1),
            "cycle_avg": round(sum(cycles) / len(cycles), 1) if cycles else None,
            "cycle_p90": pct(cycles, 0.9),
            "wait_avg": round(sum(waits) / len(waits), 1) if waits else None,
            **self.counts,
        }

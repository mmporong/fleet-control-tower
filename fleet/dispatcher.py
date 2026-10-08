"""Task execution, assignment, charging and parking.

A task is a list of stops, each with one node action (VDA 5050 ``pick``,
``drop``, tote actions...), and needs a vehicle class. The site process
decides which tasks exist and what completing a stop means (a pallet leaves the
staging floor, a rack slot fills). The dispatcher only matches tasks to capable
vehicles and walks each task leg by leg.

Assignment ("cost", the default) matches idle robots and ready tasks on one
cost: empty travel time to the first stop, minus a credit for priority and
waiting time. The cheapest pair is assigned first, so a forklift that has just
put a pallet away takes the retrieval in the same aisle rather than the oldest
task across the building (the classic dual-command effect). "fifo" keeps the
older rule (oldest task first, nearest robot) as a comparison baseline. A robot
is only eligible if its battery can finish every leg and still reach a charger
with a reserve left; the battery model is nominal (per-metre drain), not a
measured discharge curve.

Every task state change is written to an outbox the upper system (WMS) reads
with a cursor, so it learns about completions and exceptions without polling
the task list.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import random

from .control import FleetManager, RobotRecord
from .vda5050 import action

TASK_LABEL = {"QUEUED": "대기", "ACTIVE": "진행", "DONE": "완료", "FAILED": "실패", "CANCELED": "취소"}


@dataclass
class Stop:
    node: str
    action: str
    params: dict = field(default_factory=dict)
    label: str = ""


@dataclass
class Task:
    id: str
    kind: str
    vclass: str | None
    stops: list[Stop]
    priority: int
    created: float
    state: str = "QUEUED"
    robot: str | None = None
    leg: int = 0
    assigned_at: float | None = None
    started_at: float | None = None     # first stop finished: load on board, no cancel after this
    done_at: float | None = None
    attempts: int = 0
    note: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def source(self) -> str:
        return self.stops[0].node

    @property
    def dest(self) -> str | None:
        return self.stops[-1].node if len(self.stops) > 1 else None

    @property
    def picked_at(self) -> float | None:
        return self.started_at

    def to_json(self, now: float) -> dict:
        return {"id": self.id, "kind": self.kind, "cls": self.vclass, "src": self.source,
                "dst": self.stops[-1].node if len(self.stops) > 1 else None,
                "stops": [s.label or s.node for s in self.stops], "leg": self.leg, "legs": len(self.stops),
                "pri": self.priority, "state": self.state, "label": TASK_LABEL.get(self.state, self.state),
                "robot": self.robot, "age": round(now - self.created, 1), "attempts": self.attempts,
                "note": self.note}


class Dispatcher:
    AGE_CREDIT_S = 0.25          # cost credit per second a task has waited (aging keeps old tasks from starving)
    PRIORITY_CREDIT_S = 90.0     # cost credit per priority level

    def __init__(self, manager: FleetManager, rng: random.Random, process=None, charge_low: float = 30.0,
                 charge_full: float = 90.0, reserve: float = 12.0, overdue_s: float = 600.0,
                 policy: str = "cost") -> None:
        if policy not in ("cost", "fifo"):
            raise ValueError("policy must be cost or fifo")
        self.m = manager
        self.layout = manager.layout
        self.planner = manager.planner
        self.rng = rng
        self.policy = policy
        self.charge_low = charge_low
        self.charge_full = charge_full
        self.reserve = reserve
        self.overdue_s = overdue_s
        self.tasks: dict[str, Task] = {}
        self.finished: deque = deque(maxlen=3000)
        self._seq = 0
        self._aseq = 0
        self.charger_owner: dict[str, str] = {}
        self.park_owner: dict[str, str] = {}
        self.charge_wait_since: dict[str, float] = {}
        self.chargers = self.layout.nodes_of_kind("charger")
        self.parks = self.layout.nodes_of_kind("park")
        self.counts = {"created": 0, "done": 0, "failed": 0, "canceled": 0, "requeued": 0, "exceptions": 0}
        self.process = process
        self.outbox: deque = deque(maxlen=5000)     # WMS-facing task events, read with a cursor
        self._evseq = 0
        self.sinks: list = []                       # e.g. the persistent store
        manager.dispatcher = self

    # ------------------------------------------------------------ WMS event outbox
    def emit(self, kind: str, task: Task, now: float, **data) -> dict:
        self._evseq += 1
        ev = {"seq": self._evseq, "t": round(now, 1), "type": kind, "task": task.id, "kind": task.kind,
              "ref": task.meta.get("ref"), "robot": task.robot, **data}
        self.outbox.append(ev)
        if kind == "EXCEPTION":
            self.counts["exceptions"] += 1
            self.m.event("task_exception", task.id, f"{task.id} 예외 {data.get('code', '')}: "
                         f"{data.get('detail', '')}", "warn")
        for sink in self.sinks:
            sink(ev)
        return ev

    def events_after(self, seq: int, limit: int = 200) -> list[dict]:
        return [e for e in self.outbox if e["seq"] > seq][:limit]

    # ------------------------------------------------------------ helpers
    @property
    def rate_per_h(self) -> float:
        return self.process.rate_per_h if self.process else 0.0

    @rate_per_h.setter
    def rate_per_h(self, value: float) -> None:
        if self.process:
            self.process.set_rate(value, self.m.now)

    def task_priority(self, task_id: str) -> int:
        t = self.tasks.get(task_id)
        return t.priority if t else 0

    def next_id(self) -> str:
        self._seq += 1
        return f"T{self._seq:05d}"

    def aid(self, kind: str) -> str:
        self._aseq += 1
        return f"{kind}-{self._aseq}"

    _aid = aid

    def submit(self, task: Task) -> Task:
        self.tasks[task.id] = task
        self.counts["created"] += 1
        self.emit("CREATED", task, self.m.now)
        return task

    def add_task(self, kind: str, source: str, dest: str | None = None, priority: int = 0,
                 ref: str | None = None) -> Task:
        """Operator / WMS entry: the site process validates and builds the stops; ``ref`` is the upper
        system's order id, carried by every outbox event of the task."""
        if self.process is None:
            raise ValueError("no site process")
        task = self.process.make_task(kind, source, dest, max(0, min(2, int(priority))))
        if ref:
            task.meta["ref"] = ref
        return self.submit(task)

    def spots_for(self, kind: str, vclass: str | None) -> list[str]:
        """Chargers or parking bays this vehicle class may enter."""
        out = []
        for n in (self.chargers if kind == "charger" else self.parks):
            if any(self.layout.allowed(e, vclass) for e in self.layout.in_edges[n]):
                out.append(n)
        return out

    # ------------------------------------------------------------ tick
    def tick(self, now: float) -> None:
        if self.process:
            self.process.tick(now)
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

    def _stop_action(self, task: Task, idx: int) -> dict:
        st = task.stops[idx]
        return action(st.action, self.aid(f"{task.id}-{idx}-{st.action}"), "HARD", st.params)

    def _purpose(self, task: Task) -> str:
        return "pickup" if task.leg == 0 else "dropoff" if task.leg == len(task.stops) - 1 else "stop"

    def _advance_job(self, rec: RobotRecord, now: float) -> None:
        if not rec.route or not self.m.route_done(rec):
            return
        task = self.tasks.get(rec.task_id) if rec.task_id else None
        purpose = rec.purpose
        failed = self.m.goal_action_failed(rec)
        if task is not None and purpose in ("pickup", "stop", "dropoff"):
            if failed:
                self._stop_failed(rec, task, failed, now)
                return
            if self.process:
                self.process.on_stop_done(task, task.leg, now, rec.serial)
            stop = task.stops[task.leg]
            self.emit("STOP_DONE", task, now, stop=task.leg, location=stop.label or stop.node, action=stop.action)
            if task.leg == 0:
                task.started_at = now
            task.leg += 1
            if task.leg >= len(task.stops):
                self._end(task, "DONE", now)
                self.m.clear_route(rec)
                self.m.event("task_done", rec.serial, f"{task.id} {task.kind} 완료 {now - task.created:.0f}초", "info")
                return
            nxt = task.stops[task.leg]
            if not self.m.assign_route(rec, nxt.node, self._purpose(task), [self._stop_action(task, task.leg)],
                                       task.id):
                self._end(task, "FAILED", now, f"{nxt.node} 경로 없음")
                self.m.clear_route(rec)
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

    def _stop_failed(self, rec: RobotRecord, task: Task, reason: str, now: float) -> None:
        """The vehicle reported the stop's action FAILED: the site process decides what happens next."""
        stop = task.stops[task.leg]
        if self.process:
            verdict, note = self.process.on_action_failed(task, task.leg, reason, now, rec.serial)
        else:
            verdict, note = ("requeue" if task.started_at is None else "fail"), reason
        self.m.event("action_failed", rec.serial, f"{task.id} {stop.label or stop.node} {stop.action}: {note}", "warn")
        self.m.clear_route(rec)
        if verdict in ("retry", "redirect"):
            nxt = task.stops[task.leg]
            if self.m.assign_route(rec, nxt.node, self._purpose(task), [self._stop_action(task, task.leg)], task.id):
                return
            verdict, note = ("requeue" if task.started_at is None else "fail"), f"{note} · 경로 없음"
        if verdict == "requeue" and task.started_at is None:
            self._requeue(task, note)
        else:
            self._end(task, "FAILED", now, note if verdict == "fail" else f"동작 실패(적재 상태): {note}")

    def _end(self, task: Task, state: str, now: float, note: str = "") -> None:
        task.state = state
        task.done_at = now
        if note:
            task.note = note
        self.counts[{"DONE": "done", "FAILED": "failed", "CANCELED": "canceled"}[state]] += 1
        if self.process:
            self.process.on_task_end(task, state, now)
        self.emit({"DONE": "COMPLETED", "FAILED": "FAILED", "CANCELED": "CANCELED"}[state], task, now,
                  note=task.note)
        self.finished.append(task)
        self.tasks.pop(task.id, None)

    def _energy(self, metres: float, rec: RobotRecord) -> float:
        per_m = 0.012 if rec.vclass == "FORKLIFT" else 0.02
        return metres * per_m * 1.3

    def _feasible(self, rec: RobotRecord, task: Task) -> bool:
        pts = [rec.last_node] + [s.node for s in task.stops]
        d = sum(self.planner.distance(a, b, rec.vclass) for a, b in zip(pts, pts[1:]))
        chargers = self.spots_for("charger", rec.vclass) or self.chargers
        d += min(self.planner.distance(pts[-1], c, rec.vclass) for c in chargers)
        return math.isfinite(d) and rec.battery - self._energy(d, rec) >= self.reserve

    def _available(self, rec: RobotRecord) -> bool:
        if rec.task_id or rec.serial in self.m.robot_paused or rec.serial in self.m.area_paused:
            return False
        if rec.purpose not in ("", "parked", "park", "charging"):
            return False
        if not self.m.can_take_route(rec):
            return False
        floor = 60.0 if rec.charging else self.charge_low
        return rec.battery >= floor

    def _eligible(self, rec: RobotRecord, task: Task) -> bool:
        if task.vclass is not None and rec.vclass not in (task.vclass, None):
            return False
        if not all(self.m.supports(rec, s.action) for s in task.stops):
            return False
        return self._feasible(rec, task)

    def _pair_cost(self, rec: RobotRecord, task: Task, now: float) -> float:
        cost = self.planner.travel_time(rec.last_node, task.source, rec.vclass)
        cost += max(0.0, 80.0 - rec.battery) * 1.5
        if self.policy == "cost":
            cost -= self.PRIORITY_CREDIT_S * task.priority + self.AGE_CREDIT_S * (now - task.created)
        return cost

    def _assign(self, now: float) -> None:
        idle = [r for r in self.m.robots.values() if self._available(r)]
        if not idle:
            return
        queue = sorted((t for t in self.tasks.values() if t.state == "QUEUED"
                        and (not self.process or self.process.ready(t))),
                       key=lambda t: (-t.priority, t.created))
        if self.policy == "fifo":
            pairs = []
            for task in queue:
                best = min((r for r in idle if self._eligible(r, task)),
                           key=lambda r: self._pair_cost(r, task, now), default=None)
                if best is not None:
                    pairs.append((task, best))
                    idle.remove(best)
            for task, rec in pairs:
                self._start(task, rec, now)
            return
        # cost policy: the cheapest robot-task pair first (greedy matching on the cost matrix); the queue is
        # capped so a long backlog does not make each tick quadratic
        cand = [(self._pair_cost(r, t, now), i, j) for i, t in enumerate(queue[:60]) for j, r in enumerate(idle)
                if math.isfinite(self.planner.travel_time(r.last_node, t.source, r.vclass))]
        cand.sort()
        used_t, used_r = set(), set()
        for cost, i, j in cand:
            if i in used_t or j in used_r:
                continue
            task, rec = queue[i], idle[j]
            if not self._eligible(rec, task):
                continue
            if self._start(task, rec, now):
                used_t.add(i)
                used_r.add(j)

    def _start(self, task: Task, best: RobotRecord, now: float) -> bool:
        start_actions = []
        if best.charging:
            start_actions.append(action("stopCharging", self.aid(f"{best.serial}-stopchg"), "HARD"))
        task.leg = 0
        if not self.m.assign_route(best, task.source, "pickup", [self._stop_action(task, 0)], task.id,
                                   start_actions=start_actions):
            return False
        self._release_spots(best.serial)
        task.state, task.robot, task.assigned_at = "ACTIVE", best.serial, now
        if self.process:
            self.process.on_assigned(task, now)
        self.emit("ASSIGNED", task, now)
        self.charge_wait_since.pop(best.serial, None)
        return True

    def _release_spots(self, serial: str) -> None:
        for d in (self.charger_owner, self.park_owner):
            for k in [k for k, v in d.items() if v == serial]:
                del d[k]

    def _idle_policy(self, rec: RobotRecord, now: float) -> None:
        if rec.task_id or rec.route or not self.m.can_take_route(rec):
            return
        if rec.serial in self.m.robot_paused or rec.serial in self.m.area_paused or rec.purpose == "evacuate":
            return
        here = rec.last_node
        kind = self.layout.nodes[here].kind if here else ""
        if kind == "charger" and not rec.charging and rec.battery < self.charge_full:
            start = action("startCharging", self.aid(f"{rec.serial}-chg"), "HARD")
            if self.m.assign_route(rec, here, "charge", [start]):
                self.charger_owner[here] = rec.serial
            return
        if rec.charging or kind == "charger":
            self.charger_owner[here] = rec.serial
            if rec.battery >= self.charge_full and any(s for s in self.charge_wait_since if s != rec.serial):
                self.send_away(rec, exclude=set(), leave_charger=True)
            return
        if rec.battery < self.charge_low:
            free = [c for c in self.spots_for("charger", rec.vclass) if c not in self.charger_owner
                    and self.m.locks.holder_of(f"N:{c}") in (None, rec.serial)]
            if free:
                c = min(free, key=lambda c: self.planner.travel_time(here, c, rec.vclass))
                start = action("startCharging", self.aid(f"{rec.serial}-chg"), "HARD")
                if self.m.assign_route(rec, c, "charge", [start]):
                    self._release_spots(rec.serial)
                    self.charger_owner[c] = rec.serial
                    self.charge_wait_since.pop(rec.serial, None)
                    return
            self.charge_wait_since.setdefault(rec.serial, now)
            if rec.battery < 7.0:
                # critically low: free a charger held by a robot that is already well charged
                donors = [self.m.robots[o] for c, o in self.charger_owner.items() if o in self.m.robots
                          and c in self.spots_for("charger", rec.vclass)]
                donors = [d for d in donors if d.battery >= 60.0 and not d.task_id and self.m.can_take_route(d)]
                if donors:
                    donor = max(donors, key=lambda d: d.battery)
                    if self.send_away(donor, exclude=set(), leave_charger=True):
                        self.m.event("charger_preempt", donor.serial,
                                     f"{rec.serial} 배터리 {rec.battery:.0f}%: 충전기 양보", "warn")
        if kind == "park":
            self.park_owner[here] = rec.serial
            rec.purpose = "parked"
            return
        # no bay yet: go to a free one; without one, wait on a holding node (a robot that needs it nudges
        # this one away) and step aside only from places others must pass or work at. Moving from one free
        # node to the next instead keeps idle robots circling the lanes and walking into other routes.
        if self._free_parks(rec) or kind != "queue" or self.layout.must_pass(here):
            self.send_away(rec, exclude=set())

    def _free_parks(self, rec: RobotRecord, exclude: set[str] = frozenset()) -> list[str]:
        return [p for p in self.spots_for("park", rec.vclass) if p not in self.park_owner and p not in exclude
                and self.m.locks.holder_of(f"N:{p}") in (None, rec.serial)]

    def send_away(self, rec: RobotRecord, exclude: set[str], leave_charger: bool = False) -> bool:
        """Move an idle robot to a free parking bay (or aside) so it stops blocking traffic."""
        here = rec.last_node
        if here is None:
            return False
        free = self._free_parks(rec, exclude)
        start = [action("stopCharging", self.aid(f"{rec.serial}-stopchg"), "HARD")] if rec.charging else []
        for p in sorted(free, key=lambda p: self.planner.travel_time(here, p, rec.vclass))[:3]:
            if self.m.assign_route(rec, p, "park", start_actions=start):
                self._release_spots(rec.serial)
                self.park_owner[p] = rec.serial
                return True
        # aside: never onto the next stretch of another robot's route
        busy = {n for r in self.m.robots.values() if r.serial != rec.serial and r.route
                for n in r.route[r.cur_idx:r.cur_idx + 8]}
        spot = self.m._evasion_spot(rec, set(exclude) | busy)
        if spot and self.m.assign_route(rec, spot, "reposition", start_actions=start):
            self._release_spots(rec.serial)
            return True
        return False

    # ------------------------------------------------------------ failures and operator actions
    def _requeue(self, task: Task, why: str) -> None:
        task.state, task.robot, task.assigned_at, task.leg = "QUEUED", None, None, 0
        task.attempts += 1
        task.note = why
        self.counts["requeued"] += 1
        if self.process:
            self.process.on_requeue(task)
        self.emit("REQUEUED", task, self.m.now, note=why)
        self.m.event("task_requeued", task.id, f"{task.id} 재대기: {why}", "warn")

    def detach(self, rec: RobotRecord, why: str) -> None:
        """Take the task away from a robot that keeps its reservations until it stops."""
        task = self.tasks.get(rec.task_id) if rec.task_id else None
        if task and task.started_at is None:
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
            if task is None or task.started_at is not None:
                continue
            fault_long = rec.fatal and rec.fault_since is not None and now - rec.fault_since > 90.0
            offline_long = rec.connection != "ONLINE" and now - rec.last_seen > 30.0
            if fault_long or offline_long:
                self.detach(rec, "고장 장기화" if fault_long else "통신 두절 30초")

    def cancel(self, task_id: str) -> str:
        task = self.tasks[task_id]
        if task.state == "QUEUED":
            self._end(task, "CANCELED", self.m.now, "운영자 취소")
            return f"{task_id} 취소"
        if task.started_at is not None:
            raise ValueError("적재 후에는 취소할 수 없습니다: 하역 완료 후 처리하거나 현장 회수로 종결")
        rec = self.m.robots[task.robot]
        self.detach(rec, "운영자 취소")
        task = self.tasks.get(task_id)
        if task:
            self._end(task, "CANCELED", self.m.now, "운영자 취소")
        return f"{task_id} 취소"

    def reassign(self, task_id: str) -> str:
        task = self.tasks[task_id]
        if task.robot is None:
            return f"{task_id}는 배정 대기 중"
        rec = self.m.robots[task.robot]
        if task.started_at is not None:
            self._end(task, "FAILED", self.m.now, "적재물 현장 회수")
            self.detach(rec, "현장 회수")
            return f"{task_id} 실패 처리(적재물 현장 회수)"
        self.detach(rec, "운영자 재할당")
        return f"{task_id} 재할당 대기열로 이동"

    def reprioritize(self, task_id: str, priority: int) -> str:
        task = self.tasks[task_id]
        task.priority = max(0, min(2, int(priority)))
        return f"{task_id} 우선순위 {task.priority}"

    # ------------------------------------------------------------ views
    def queue_view(self, now: float, limit: int = 60) -> list[dict]:
        order = {"ACTIVE": 0, "QUEUED": 1}
        items = sorted(self.tasks.values(), key=lambda t: (order.get(t.state, 2), -t.priority, t.created))
        return [t.to_json(now) for t in items[:limit]]

    def reset_metrics(self, now: float) -> None:
        """Start a measurement window: finished-task history and counters restart, live tasks stay."""
        self.finished.clear()
        for k in self.counts:
            self.counts[k] = 0
        self._window_start = now

    def stats(self, now: float, window: float = 3600.0) -> dict:
        start = getattr(self, "_window_start", 0.0)
        window = min(window, max(1.0, now - start))
        recent = [t for t in self.finished if t.state == "DONE" and t.done_at is not None and now - t.done_at <= window]
        cycles = sorted(t.done_at - t.created for t in recent)
        waits = sorted(t.assigned_at - t.created for t in recent if t.assigned_at is not None)
        execs = sorted(t.done_at - t.assigned_at for t in recent if t.assigned_at is not None)
        span = min(window, max(now - start, 1.0))

        def pct(values: list[float], q: float) -> float | None:
            if not values:
                return None
            return round(values[min(len(values) - 1, int(q * len(values)))], 1)

        by_kind: dict[str, int] = {}
        for t in recent:
            by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
        return {
            "queued": sum(1 for t in self.tasks.values() if t.state == "QUEUED"),
            "active": sum(1 for t in self.tasks.values() if t.state == "ACTIVE"),
            "throughput_h": round(len(recent) * 3600.0 / span, 1),
            "by_kind_h": {k: round(v * 3600.0 / span, 1) for k, v in by_kind.items()},
            "cycle_avg": round(sum(cycles) / len(cycles), 1) if cycles else None,
            "cycle_p90": pct(cycles, 0.9),
            "wait_avg": round(sum(waits) / len(waits), 1) if waits else None,     # created -> assigned
            "exec_avg": round(sum(execs) / len(execs), 1) if execs else None,     # assigned -> done
            **self.counts,
        }

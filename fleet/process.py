"""Site processes: which work exists and what completing a stop means.

``GridProcess`` drives the algorithm test grid (generic PICK / PUTAWAY tote
moves). The distribution centre process lives in ``process_dc``.
"""
from __future__ import annotations

import random

from .dispatcher import Dispatcher, Stop, Task


class Process:
    """Interface the dispatcher calls. Subclasses override what they need."""

    task_kinds: list[dict] = []

    def __init__(self, dispatcher: Dispatcher, rng: random.Random, rate_per_h: float) -> None:
        self.d = dispatcher
        self.layout = dispatcher.layout
        self.rng = rng
        self.rate_per_h = rate_per_h
        self.next_arrival = 0.0

    def set_rate(self, rate: float, now: float) -> None:
        self.rate_per_h = rate
        self.next_arrival = now

    def tick(self, now: float) -> None: ...

    def make_task(self, kind: str, source: str, dest: str | None, priority: int) -> Task:
        raise ValueError(f"unknown task kind {kind}")

    def ready(self, task: Task) -> bool:
        return True

    def on_assigned(self, task: Task, now: float) -> None: ...

    def on_stop_done(self, task: Task, idx: int, now: float, robot: str) -> None: ...

    def on_task_end(self, task: Task, state: str, now: float) -> None: ...

    def on_requeue(self, task: Task) -> None: ...

    def on_action_failed(self, task: Task, idx: int, reason: str, now: float, robot: str) -> tuple[str, str]:
        """What to do when the vehicle reports a stop's action FAILED.

        Returns (verdict, note): "retry" the same stop, "redirect" to a stop the process has just rewritten,
        "requeue" the task for any vehicle, or "fail" it. Default: requeue while nothing is on board.
        """
        return ("requeue" if task.started_at is None else "fail"), reason

    def stats(self, now: float) -> dict:
        return {}

    def snapshot(self) -> dict:
        return {}


class GridProcess(Process):
    task_kinds = [
        {"kind": "PICK", "label": "PICK · 보관→출고", "src": ["storage"], "dst": ["outbound"], "dst_optional": True},
        {"kind": "PUTAWAY", "label": "PUTAWAY · 입고→보관", "src": ["inbound"], "dst": ["storage"]},
    ]

    def __init__(self, dispatcher: Dispatcher, rng: random.Random, rate_per_h: float) -> None:
        super().__init__(dispatcher, rng, rate_per_h)
        self.storage = self.layout.nodes_of_kind("storage")
        self.inbound = self.layout.nodes_of_kind("inbound")
        self.outbound = self.layout.nodes_of_kind("outbound")

    def tick(self, now: float) -> None:
        if self.rate_per_h <= 0:
            self.next_arrival = now + 60.0
            return
        while self.next_arrival <= now:
            r = self.rng.random()
            pri = 2 if r < 0.08 else 1 if r < 0.25 else 0
            if self.rng.random() < 0.6:
                self.d.submit(self.make_task("PICK", self.rng.choice(self.storage), None, pri))
            else:
                self.d.submit(self.make_task("PUTAWAY", self.rng.choice(self.inbound), self.rng.choice(self.storage),
                                             pri))
            self.next_arrival += self.rng.expovariate(self.rate_per_h / 3600.0)

    def make_task(self, kind: str, source: str, dest: str | None, priority: int) -> Task:
        if kind not in ("PICK", "PUTAWAY"):
            raise ValueError("kind must be PICK or PUTAWAY")
        if source not in self.layout.nodes or (dest is not None and dest not in self.layout.nodes):
            raise ValueError("unknown node")
        stops = [Stop(source, "pick", {"stationName": source}, source)]
        if dest is not None:
            stops.append(Stop(dest, "drop", {"stationName": dest}, dest))
        t = Task(self.d.next_id(), kind, None, stops, priority, self.d.m.now)
        if dest is None:
            t.meta["dest_open"] = True
        return t

    def ready(self, task: Task) -> bool:
        if task.kind == "PUTAWAY":
            # one robot per inbound dock at a time: a second one would wait on the wall lane
            return not any(t.source == task.source and t.state == "ACTIVE" and t.started_at is None
                           for t in self.d.tasks.values() if t.id != task.id)
        return True

    def on_assigned(self, task: Task, now: float) -> None:
        if task.meta.pop("dest_open", False):
            dest = self._choose_outbound(task.source)
            task.stops.append(Stop(dest, "drop", {"stationName": dest}, dest))
            task.meta["auto_dest"] = True

    def on_requeue(self, task: Task) -> None:
        if task.kind == "PICK" and task.started_at is None and len(task.stops) == 2 and task.meta.get("auto_dest"):
            task.stops.pop()
            task.meta["dest_open"] = True

    def _choose_outbound(self, near: str) -> str:
        load = {d: 0 for d in self.outbound}
        for t in self.d.tasks.values():
            if t.dest in load and t.state == "ACTIVE":
                load[t.dest] += 1
        return min(self.outbound, key=lambda d: (load[d], self.d.planner.travel_time(near, d)))

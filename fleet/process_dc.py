"""Distribution centre process: pallets, trucks, staging spots and orders.

Inbound: trucks arrive, the manual dock forklift moves each pallet to a free
floor staging spot, and only then does a PUTAWAY task exist for an autonomous
forklift (to a reserved rack slot). Outbound: PALLET_OUT orders move a pallet
from its rack slot to a free outbound staging spot; the manual forklift then
loads it on the truck. Tote orders (TOTE_PICK) send a tote AMR from a pack
station through two or three pick faces, where a picker must come to the
robot, and back to packing.

The process keeps the ground truth of where every pallet is, so KPIs such as
dock-to-stock time are measured, not assumed.
"""
from __future__ import annotations

from collections import deque
import random

import math

from .agents_dc import SERVE_DIST, dock_points
from .dispatcher import Dispatcher, Stop, Task
from .process import Process
from .site_dc import AGF, AMR, DOCK_YS, STAGE_LANE_CAPACITY
from .vehicle import agf_drop_time, agf_pick_time

LEVEL_HEIGHT = 1.6       # m between beam levels
LEVELS = 3               # floor + two beam levels per bay side


class DcProcess(Process):
    task_kinds = [
        {"kind": "PALLET_OUT", "label": "PALLET_OUT · 랙→출고 스테이징", "src": ["rack"], "dst": ["out_stage"],
         "dst_optional": True},
        {"kind": "TOTE_PICK", "label": "TOTE_PICK · 패킹→피킹면→패킹", "src": ["pickface"], "dst": ["pack"],
         "dst_optional": True},
    ]

    def __init__(self, dispatcher: Dispatcher, rng: random.Random, trucks_h: float = 5.0, pallets_per_truck=(10, 16),
                 out_pallets_h: float = 45.0, tote_orders_h: float = 150.0, fill: float = 0.6) -> None:
        super().__init__(dispatcher, rng, tote_orders_h)
        L = self.layout
        # floor staging lanes in front of each door: FIFO queues of pallets
        self.in_stage: dict[str, list[str]] = {n: [] for n in L.nodes_of_kind("in_stage")}
        self.out_stage: dict[str, list[str]] = {n: [] for n in L.nodes_of_kind("out_stage")}
        self.in_pending: dict[str, int] = {n: 0 for n in self.in_stage}     # dock forklifts on their way
        self.out_pending: dict[str, int] = {n: 0 for n in self.out_stage}   # AGF tasks that will drop here
        self.packs = L.nodes_of_kind("pack")
        self.pickfaces = L.nodes_of_kind("pickface")
        self.rack_nodes = L.nodes_of_kind("rack")
        self.slots: dict[tuple[str, str, int], str | None] = {}
        self.slot_reserved: dict[tuple, str] = {}
        self.pallets: dict[str, dict] = {}
        self._pid = 0
        for n in self.rack_nodes:
            for side in ("L", "R"):
                for lvl in range(LEVELS):
                    pid = self._new_pallet(("rack", (n, side, lvl)), 0.0) if rng.random() < fill else None
                    self.slots[(n, side, lvl)] = pid
        self.trucks_h = trucks_h
        self.pallets_per_truck = pallets_per_truck
        self.out_pallets_h = out_pallets_h
        self.next_truck = rng.uniform(10.0, 60.0)
        self.next_out = rng.uniform(5.0, 30.0)
        self.docks_in: dict[int, int] = {}          # dock index -> pallets still on the truck
        self.docks_out: dict[int, int] = {}          # dock index -> pallets loaded on the current truck
        self.truck_queue: deque = deque()
        self.picker_claims: dict[str, str] = {}     # robot -> picker
        self.metrics = {"trucks": 0, "pallets_unloaded": 0, "putaway": 0, "shipped": 0, "out_orders": 0,
                        "out_backlog": 0, "tote_orders": 0, "tote_done": 0, "picks": 0, "d2s": deque(maxlen=500),
                        "picker_wait_s": 0.0, "picker_waits": 0}
        self.forklift_jobs: dict[str, dict] = {}
        self._waiting: dict[str, dict] = {}

    # ------------------------------------------------------------ pallets
    def _new_pallet(self, loc, now: float) -> str:
        self._pid += 1
        pid = f"P{self._pid:05d}"
        self.pallets[pid] = {"loc": loc, "created": now, "staged": None, "stored": None}
        return pid

    def set_rate(self, rate: float, now: float) -> None:
        super().set_rate(rate, now)

    # ------------------------------------------------------------ tick: arrivals and orders
    def tick(self, now: float) -> None:
        while self.trucks_h > 0 and self.next_truck <= now:
            self.truck_queue.append(self.rng.randint(*self.pallets_per_truck))
            self.metrics["trucks"] += 1
            self.next_truck += self.rng.expovariate(self.trucks_h / 3600.0)
        free_docks = [i for i in range(len(DOCK_YS)) if i not in self.docks_in]
        while self.truck_queue and free_docks:
            self.docks_in[free_docks.pop(0)] = self.truck_queue.popleft()
        while self.out_pallets_h > 0 and self.next_out <= now:
            self.metrics["out_orders"] += 1
            self.metrics["out_backlog"] += 1
            self.next_out += self.rng.expovariate(self.out_pallets_h / 3600.0)
        while self.metrics["out_backlog"] > 0:
            task = self._make_pallet_out(priority=0)
            if task is None:
                break
            self.metrics["out_backlog"] -= 1
            self.d.submit(task)
        if self.rate_per_h <= 0:
            self.next_arrival = now + 60.0
        while self.rate_per_h > 0 and self.next_arrival <= now:
            r = self.rng.random()
            self.d.submit(self._make_tote(priority=2 if r < 0.05 else 1 if r < 0.2 else 0))
            self.next_arrival += self.rng.expovariate(self.rate_per_h / 3600.0)

    # ------------------------------------------------------------ task builders
    def _free_slot(self) -> tuple | None:
        free = [k for k, v in self.slots.items() if v is None and k not in self.slot_reserved]
        if not free:
            return None
        # fill lower levels first, like a slotting rule would
        lvl = min(k[2] for k in free)
        return self.rng.choice([k for k in free if k[2] == lvl])

    def _rack_params(self, slot: tuple, pid: str, pick: bool) -> dict:
        n, side, lvl = slot
        h = lvl * LEVEL_HEIGHT
        return {"duration": round(agf_pick_time(h) if pick else agf_drop_time(h), 1), "loadId": pid,
                "loadType": "T11", "lhd": side, "height": round(h, 2), "stationType": "RACK",
                "stationName": f"{n}-{side}{lvl}"}

    def _make_putaway(self, spot: str, pid: str, now: float) -> Task | None:
        slot = self._free_slot()
        if slot is None:
            return None
        t = Task(self.d.next_id(), "PUTAWAY", AGF,
                 [Stop(spot, "pick", {"duration": agf_pick_time(0.0), "loadId": pid, "loadType": "T11",
                                      "stationType": "FLOOR", "stationName": spot}, f"{spot}"),
                  Stop(slot[0], "drop", self._rack_params(slot, pid, pick=False), f"{slot[0]}-{slot[1]}{slot[2]}")],
                 1, now, meta={"pallet": pid, "slot": slot, "spot": spot})
        self.slot_reserved[slot] = t.id
        return t

    def _make_pallet_out(self, priority: int, slot: tuple | None = None, spot: str | None = None) -> Task | None:
        if slot is None:
            full = [k for k, v in self.slots.items() if v is not None and k not in self.slot_reserved]
            if not full:
                return None
            slot = self.rng.choice(full)
        if spot is None:
            free = [s for s, lane in self.out_stage.items()
                    if len(lane) + self.out_pending[s] < STAGE_LANE_CAPACITY]
            if not free:
                return None
            spot = min(free, key=lambda s: (len(self.out_stage[s]) + self.out_pending[s], self.rng.random()))
        pid = self.slots[slot]
        n, side, lvl = slot
        t = Task(self.d.next_id(), "PALLET_OUT", AGF,
                 [Stop(n, "pick", self._rack_params(slot, pid, pick=True), f"{n}-{side}{lvl}"),
                  Stop(spot, "drop", {"duration": agf_drop_time(0.0), "loadId": pid, "loadType": "T11",
                                      "stationType": "FLOOR", "stationName": spot}, spot)],
                 priority, self.d.m.now, meta={"pallet": pid, "slot": slot, "spot": spot})
        self.slot_reserved[slot] = t.id
        self.out_pending[spot] += 1
        return t

    def _make_tote(self, priority: int, faces: list[str] | None = None, drop: str | None = None) -> Task:
        start = self.rng.choice(self.packs)
        faces = faces or sorted(self.rng.sample(self.pickfaces, self.rng.choice((2, 2, 3))))
        drop = drop or self.rng.choice(self.packs)
        stops = [Stop(start, "x_toteInduct", {"duration": 6.0}, start)]
        for f in faces:
            items = self.rng.randint(1, 4)
            stops.append(Stop(f, "x_pickItems", {"duration": 4.0 + 2.5 * items, "items": items}, f))
        stops.append(Stop(drop, "x_toteDrop", {"duration": 6.0}, drop))
        self.metrics["tote_orders"] += 1
        return Task(self.d.next_id(), "TOTE_PICK", AMR, stops, priority, self.d.m.now)

    def make_task(self, kind: str, source: str, dest: str | None, priority: int) -> Task:
        L = self.layout
        if kind == "PALLET_OUT":
            if source not in L.nodes or L.nodes[source].kind != "rack":
                raise ValueError("PALLET_OUT source는 랙 통로 노드(R*-**)여야 함")
            full = [k for k, v in self.slots.items() if k[0] == source and v is not None and k not in self.slot_reserved]
            if not full:
                raise ValueError(f"{source}에 출고 가능한 팔레트 없음")
            if dest is not None and (dest not in self.out_stage or
                                     len(self.out_stage[dest]) + self.out_pending[dest] >= STAGE_LANE_CAPACITY):
                raise ValueError("dest는 자리가 남은 출고 스테이징 레인(SO*)이어야 함")
            t = self._make_pallet_out(priority, slot=full[0], spot=dest)
            if t is None:
                raise ValueError("비어 있는 출고 스테이징 없음")
            self.metrics["out_orders"] += 1
            return t
        if kind == "TOTE_PICK":
            if source not in L.nodes or L.nodes[source].kind != "pickface":
                raise ValueError("TOTE_PICK source는 피킹면 노드(K*-**)여야 함")
            if dest is not None and dest not in self.packs:
                raise ValueError("dest는 패킹 스테이션(PK*)이어야 함")
            return self._make_tote(priority, faces=[source], drop=dest)
        raise ValueError("kind는 PALLET_OUT 또는 TOTE_PICK (PUTAWAY는 입고 하역 시 자동 생성)")

    def ready(self, task: Task) -> bool:
        if task.kind == "PUTAWAY":
            # one AGF at a time works a staging lane, and only pallets already on the floor
            spot = task.meta["spot"]
            if not self.in_stage[spot]:
                return False
            return not any(t.kind == "PUTAWAY" and t.meta.get("spot") == spot and t.state == "ACTIVE"
                           and t.started_at is None and t.id != task.id for t in self.d.tasks.values())
        return True

    # ------------------------------------------------------------ stop and task hooks
    def on_stop_done(self, task: Task, idx: int, now: float, robot: str) -> None:
        pid = task.meta.get("pallet")
        if task.kind == "PUTAWAY":
            if idx == 0:
                lane = self.in_stage[task.meta["spot"]]
                pid = lane.pop(0) if lane else pid        # the AGF takes the front pallet of the lane
                task.meta["pallet"] = pid
                self.pallets[pid]["loc"] = ("agf", robot)
            else:
                slot = task.meta["slot"]
                self.slots[slot] = pid
                self.slot_reserved.pop(slot, None)
                self.pallets[pid].update(loc=("rack", slot), stored=now)
                self.metrics["putaway"] += 1
                staged = self.pallets[pid]["staged"]
                if staged is not None:
                    self.metrics["d2s"].append(now - staged)
        elif task.kind == "PALLET_OUT":
            if idx == 0:
                slot = task.meta["slot"]
                self.slots[slot] = None
                self.slot_reserved.pop(slot, None)
                self.pallets[pid]["loc"] = ("agf", robot)
            else:
                spot = task.meta["spot"]
                self.out_stage[spot].append(pid)
                self.out_pending[spot] = max(0, self.out_pending[spot] - 1)
                self.pallets[pid]["loc"] = ("out_stage", spot)
        elif task.kind == "TOTE_PICK" and task.stops[idx].action == "x_pickItems":
            self.metrics["picks"] += 1

    def on_task_end(self, task: Task, state: str, now: float) -> None:
        if task.kind == "TOTE_PICK" and state == "DONE":
            self.metrics["tote_done"] += 1
        if state == "DONE":
            return
        # failed or canceled: give back what is still reserved
        slot = task.meta.get("slot")
        if slot is not None and self.slot_reserved.get(slot) == task.id:
            self.slot_reserved.pop(slot, None)
        if task.kind == "PALLET_OUT" and (task.leg < 2):
            spot = task.meta["spot"]
            self.out_pending[spot] = max(0, self.out_pending[spot] - 1)
        pid = task.meta.get("pallet")
        if pid and task.started_at is not None:
            self.pallets[pid]["loc"] = ("recovery", task.robot)   # on the vehicle: needs a person to recover it

    # ------------------------------------------------------------ manual dock forklifts
    def forklift_job(self, fid: str, side: str) -> dict | None:
        if side == "in":
            busy = {j["dock"] for j in self.forklift_jobs.values() if j["side"] == "in"}
            # lanes with room, not being worked by an AGF right now
            free = [s for s in sorted(self.in_stage)
                    if len(self.in_stage[s]) + self.in_pending[s] < STAGE_LANE_CAPACITY
                    and self.d.m.locks.holder_of(f"N:{s}") is None]
            for dock, left in sorted(self.docks_in.items()):
                if left <= 0 or dock in busy or not free:
                    continue
                spot = min(free, key=lambda s: (abs(int(s[2:]) - 1 - dock), len(self.in_stage[s])))
                truck, _ = dock_points("in", dock)
                _, stage = dock_points("in", int(spot[2:]) - 1)
                job = {"side": "in", "dock": dock, "spot": spot, "pick": truck, "drop": stage}
                self.in_pending[spot] += 1
                self.forklift_jobs[fid] = job
                return job
            return None
        for spot, lane in sorted(self.out_stage.items()):
            if not lane or any(j.get("spot") == spot for j in self.forklift_jobs.values()):
                continue
            if self.d.m.locks.holder_of(f"N:{spot}") is not None:
                continue            # an AGF is dropping there: wait for it to leave
            dock = int(spot[2:]) - 1
            truck, stage = dock_points("out", dock)
            job = {"side": "out", "dock": dock, "spot": spot, "pick": stage, "drop": truck, "pallet": lane[0]}
            self.forklift_jobs[fid] = job
            return job
        return None

    def forklift_picked(self, fid: str, job: dict) -> None:
        if job["side"] == "out":
            lane = self.out_stage[job["spot"]]
            if lane and lane[0] == job["pallet"]:
                lane.pop(0)

    def forklift_dropped(self, fid: str, job: dict, now: float) -> None:
        self.forklift_jobs.pop(fid, None)
        if job["side"] == "in":
            pid = self._new_pallet(("in_stage", job["spot"]), now)
            self.pallets[pid]["staged"] = now
            self.in_stage[job["spot"]].append(pid)
            self.in_pending[job["spot"]] = max(0, self.in_pending[job["spot"]] - 1)
            self.docks_in[job["dock"]] -= 1
            self.metrics["pallets_unloaded"] += 1
            if self.docks_in[job["dock"]] <= 0:
                del self.docks_in[job["dock"]]
            task = self._make_putaway(job["spot"], pid, now)
            if task is not None:
                self.d.submit(task)
        else:
            self.pallets[job["pallet"]]["loc"] = ("shipped", job["dock"])
            self.metrics["shipped"] += 1

    # ------------------------------------------------------------ collaborative picking
    def waiting_robots(self) -> dict[str, dict]:
        return self._waiting

    def update_pickers(self, vehicles: list, pickers: list, now: float, dt: float) -> None:
        """Called by the simulation each step: who waits for a picker, who is being served."""
        self._waiting = {v.serial: {"x": v.x, "y": v.y, "waiting": v.waiting_for_picker() or
                                    any(a.status == "RUNNING" and a.action_type == "x_pickItems" for a in v.actions)}
                         for v in vehicles if v.spec.vclass == AMR}
        for v in vehicles:
            if v.spec.vclass != AMR:
                continue
            running = any(a.status == "RUNNING" and a.action_type == "x_pickItems" for a in v.actions)
            v.served = running and any(p.serving == v.serial and p.walker.done()
                                       and math.hypot(p.x - v.x, p.y - v.y) < SERVE_DIST for p in pickers)
            if running and not v.served:
                self.metrics["picker_wait_s"] += dt
        for rid in [r for r in self.picker_claims if not self._waiting.get(r, {}).get("waiting")]:
            del self.picker_claims[rid]

    def claim_picker_target(self, picker_id: str, pos, aisles: tuple = ()) -> str | None:
        """Zone picking: a picker takes the nearest waiting robot inside its own aisles."""
        waiting = [(rid, r) for rid, r in getattr(self, "_waiting", {}).items()
                   if r["waiting"] and rid not in self.picker_claims
                   and (not aisles or any(abs(r["x"] - a) < 0.3 for a in aisles))]
        if not waiting:
            return None
        rid, _ = min(waiting, key=lambda kv: abs(kv[1]["x"] - pos[0]) + abs(kv[1]["y"] - pos[1]))
        self.picker_claims[rid] = picker_id
        return rid

    def release_picker_target(self, picker_id: str) -> None:
        for rid in [r for r, p in self.picker_claims.items() if p == picker_id]:
            del self.picker_claims[rid]

    # ------------------------------------------------------------ views
    def stats(self, now: float) -> dict:
        m = self.metrics
        hours = max(now / 3600.0, 1e-6)
        d2s = sorted(m["d2s"])
        return {
            "trucks": m["trucks"], "pallets_unloaded": m["pallets_unloaded"], "putaway": m["putaway"],
            "shipped": m["shipped"], "out_backlog": m["out_backlog"], "tote_done": m["tote_done"],
            "picks": m["picks"],
            "putaway_h": round(m["putaway"] / hours, 1), "shipped_h": round(m["shipped"] / hours, 1),
            "tote_h": round(m["tote_done"] / hours, 1),
            "dock_to_stock_avg": round(sum(d2s) / len(d2s), 1) if d2s else None,
            "dock_to_stock_p90": round(d2s[int(0.9 * (len(d2s) - 1))], 1) if d2s else None,
            "picker_wait_s": round(m["picker_wait_s"], 1),
            "rack_fill": round(sum(1 for v in self.slots.values() if v) / len(self.slots), 3),
            "staged_in": sum(len(v) for v in self.in_stage.values()),
            "staged_out": sum(len(v) for v in self.out_stage.values()),
            "trucks_waiting": len(self.truck_queue),
        }

    def snapshot(self) -> dict:
        per_node: dict[str, int] = {}
        for (n, _side, _lvl), pid in self.slots.items():
            if pid:
                per_node[n] = per_node.get(n, 0) + 1
        return {
            "in_stage": {k: len(v) for k, v in self.in_stage.items()},
            "out_stage": {k: len(v) for k, v in self.out_stage.items()},
            "lane_capacity": STAGE_LANE_CAPACITY,
            "rack": per_node, "slots_per_node": 2 * LEVELS,
            "docks_in": {f"IN{k + 1}": v for k, v in self.docks_in.items()},
            "trucks_waiting": len(self.truck_queue),
            "picker_claims": dict(self.picker_claims),
        }

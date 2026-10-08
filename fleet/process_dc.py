"""Distribution centre process: the WMS side and the physical truth of the site.

Inbound: trucks arrive, the manual dock forklift moves each pallet to a free
floor staging lane, and only then does a PUTAWAY task exist for an autonomous
forklift, to a rack location the WMS chooses (lowest level and nearest aisle
first). Outbound: PALLET_OUT orders take the oldest stored pallet (FIFO) to a
free outbound staging lane; the manual forklift then loads it on the truck.
Tote orders are released to tote AMRs in batches of several orders (multi-order
picking, as collaborative picking robots carry several totes): the robot
inducts its totes at a pack station, visits the pick faces in aisle order, where
a picker must come to it, and drops the totes at packing.

Two views of the rack are kept apart: ``slots`` is what the WMS believes,
``phys`` is what is really there. A share of locations disagree (inventory
error), and a pallet pocket is sometimes not recognised (handling failure). The
vehicle's load sensors find these out (``physical_check``); the WMS answers with
a retry, another location or a requeue (``on_action_failed``) and reports the
exception upstream.
"""
from __future__ import annotations

from collections import deque
import math
import random

from .agents_dc import SERVE_DIST, dock_points
from .dispatcher import Dispatcher, Stop, Task
from .process import Process
from .site_dc import AGF, AMR, DOCK_YS, PICK_AISLES, RACK_AISLES, STAGE_LANE_CAPACITY

LEVEL_HEIGHT = 1.4       # m between beam levels: 0, 1.4, 2.8, 4.2 m (AGF lift 4.5 m)
LEVELS = 4
POSITIONS = 2            # pallet positions per bay side at each AGF stop (2.7-2.8 m bay, two T11 pallets)
SIDES = ("L", "R")
TOTE_BATCH = 3           # orders a tote AMR carries per trip
BATCH_WAIT_S = 45.0      # release a smaller batch once its oldest order has waited this long


def location_id(slot: tuple) -> str:
    """WMS location name: aisle node, side, level and position, e.g. ``R1-05-L-2-1``."""
    node, side, lvl, pos = slot
    return f"{node}-{side}-{lvl + 1}-{pos + 1}"


class DcProcess(Process):
    task_kinds = [
        {"kind": "PALLET_OUT", "label": "PALLET_OUT · 랙→출고 스테이징", "src": ["rack"], "dst": ["out_stage"],
         "dst_optional": True},
        {"kind": "TOTE_PICK", "label": "TOTE_PICK · 패킹→피킹면→패킹", "src": ["pickface"], "dst": ["pack"],
         "dst_optional": True},
    ]

    def __init__(self, dispatcher: Dispatcher, rng: random.Random, trucks_h: float = 5.0, pallets_per_truck=(10, 16),
                 out_pallets_h: float = 45.0, tote_orders_h: float = 150.0, fill: float = 0.7,
                 tote_batch: int = TOTE_BATCH, handling_fail_p: float = 0.01, inventory_error_p: float = 0.01,
                 truth_rng: random.Random | None = None) -> None:
        super().__init__(dispatcher, rng, tote_orders_h)
        L = self.layout
        # the physical world draws from its own stream, so the WMS's choices stay comparable across settings
        self.truth_rng = truth_rng or random.Random(rng.random())
        self.handling_fail_p = handling_fail_p
        self.inventory_error_p = inventory_error_p
        self.tote_batch = max(1, int(tote_batch))
        # floor staging lanes in front of each door: FIFO queues of pallets
        self.in_stage: dict[str, list[str]] = {n: [] for n in L.nodes_of_kind("in_stage")}
        self.out_stage: dict[str, list[str]] = {n: [] for n in L.nodes_of_kind("out_stage")}
        self.in_pending: dict[str, int] = {n: 0 for n in self.in_stage}     # dock forklifts on their way
        self.out_pending: dict[str, int] = {n: 0 for n in self.out_stage}   # AGF tasks that will drop here
        self.packs = L.nodes_of_kind("pack")
        self.pickfaces = L.nodes_of_kind("pickface")
        self.rack_nodes = L.nodes_of_kind("rack")
        self.slots: dict[tuple, str | None] = {}         # WMS record
        self.phys: dict[tuple, str | None] = {}          # what is physically in the location
        self.loc_index: dict[str, tuple] = {}
        self.slot_reserved: dict[tuple, str] = {}
        self.pallets: dict[str, dict] = {}
        self._pid = 0
        aisle_rank = {x: i for i, x in enumerate(RACK_AISLES)}
        self._slot_order: list[tuple] = []
        for n in self.rack_nodes:
            for side in SIDES:
                for lvl in range(LEVELS):
                    for pos in range(POSITIONS):
                        slot = (n, side, lvl, pos)
                        pid = None
                        if rng.random() < fill:
                            pid = self._new_pallet(("rack", slot), -rng.uniform(0, 7 * 86400))
                            self.pallets[pid]["stored"] = self.pallets[pid]["created"]
                        self.slots[slot] = pid
                        self.phys[slot] = pid
                        self.loc_index[location_id(slot)] = slot
        # slotting rule of the WMS: lower levels first, then the aisle nearest the inbound staging
        self._slot_order = sorted(self.slots, key=lambda s: (s[2], aisle_rank.get(L.nodes[s[0]].x, 9),
                                                             s[0], s[1], s[3]))
        # inventory record errors: a location the WMS believes empty holds a pallet, or the reverse
        self.metrics = {"trucks": 0, "pallets_unloaded": 0, "putaway": 0, "shipped": 0, "out_orders": 0,
                        "out_backlog": 0, "tote_orders": 0, "tote_done": 0, "picks": 0, "units": 0,
                        "d2s": deque(maxlen=500), "picker_wait_s": 0.0, "picker_waits": 0,
                        "exceptions": 0, "retries": 0, "redirects": 0, "inventory_fixes": 0}
        for slot in sorted(self.slots):
            if self.truth_rng.random() < inventory_error_p:
                if self.phys[slot] is None:
                    self.phys[slot] = "UNRECORDED"
                else:
                    self.phys[slot] = None
        self.trucks_h = trucks_h
        self.pallets_per_truck = pallets_per_truck
        self.out_pallets_h = out_pallets_h
        self.next_truck = rng.uniform(10.0, 60.0)
        self.next_out = rng.uniform(5.0, 30.0)
        self.docks_in: dict[int, int] = {}          # dock index -> pallets still on the truck
        self.docks_out: dict[int, int] = {}          # dock index -> pallets loaded on the current truck
        self.truck_queue: deque = deque()
        self.picker_claims: dict[str, str] = {}     # robot -> picker
        self.tote_backlog: list[dict] = []          # orders waiting to be batched onto a robot
        self._oid = 0
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
            self.tote_backlog.append(self._tote_order(2 if r < 0.05 else 1 if r < 0.2 else 0, now))
            self.next_arrival += self.rng.expovariate(self.rate_per_h / 3600.0)
        self._release_batches(now)

    # ------------------------------------------------------------ task builders
    def _free_slot(self) -> tuple | None:
        """WMS slotting: the lowest free level first; among aisles with such a location, the one with the
        fewest open tasks (spreads forklift traffic over the aisles), then the nearest to inbound."""
        free = [s for s in self._slot_order if self.slots[s] is None and s not in self.slot_reserved]
        if not free:
            return None
        lvl = free[0][2]
        busy: dict[float, int] = {}
        for s in self.slot_reserved:
            x = self.layout.nodes[s[0]].x
            busy[x] = busy.get(x, 0) + 1
        candidates = [s for s in free if s[2] == lvl]
        return min(candidates, key=lambda s: (busy.get(self.layout.nodes[s[0]].x, 0), self._slot_order.index(s)))

    def _rack_params(self, slot: tuple, pid: str) -> dict:
        n, side, lvl, _pos = slot
        return {"loadId": pid, "loadType": "T11", "lhd": "FORKS", "side": "LEFT" if side == "L" else "RIGHT",
                "height": round(lvl * LEVEL_HEIGHT, 2), "stationType": "RACK", "stationName": location_id(slot)}

    def _floor_params(self, spot: str, pid: str) -> dict:
        return {"loadId": pid, "loadType": "T11", "lhd": "FORKS", "height": 0.0, "stationType": "FLOOR",
                "stationName": spot}

    def _make_putaway(self, spot: str, pid: str, now: float) -> Task | None:
        slot = self._free_slot()
        if slot is None:
            return None
        t = Task(self.d.next_id(), "PUTAWAY", AGF,
                 [Stop(spot, "pick", self._floor_params(spot, pid), spot),
                  Stop(slot[0], "drop", self._rack_params(slot, pid), location_id(slot))],
                 1, now, meta={"pallet": pid, "slot": slot, "spot": spot})
        self.slot_reserved[slot] = t.id
        return t

    def _oldest_full_slot(self, exclude: set | frozenset = frozenset()) -> tuple | None:
        full = [k for k, v in self.slots.items() if v is not None and v != "UNKNOWN"
                and k not in self.slot_reserved and k not in exclude]
        if not full:
            return None
        return min(full, key=lambda k: (self.pallets[self.slots[k]]["stored"], k))

    def _make_pallet_out(self, priority: int, slot: tuple | None = None, spot: str | None = None) -> Task | None:
        if slot is None:
            slot = self._oldest_full_slot()          # FIFO: the oldest stored pallet ships first
            if slot is None:
                return None
        if spot is None:
            free = [s for s, lane in self.out_stage.items()
                    if len(lane) + self.out_pending[s] < STAGE_LANE_CAPACITY]
            if not free:
                return None
            spot = min(free, key=lambda s: (len(self.out_stage[s]) + self.out_pending[s], self.rng.random()))
        pid = self.slots[slot]
        t = Task(self.d.next_id(), "PALLET_OUT", AGF,
                 [Stop(slot[0], "pick", self._rack_params(slot, pid), location_id(slot)),
                  Stop(spot, "drop", self._floor_params(spot, pid), spot)],
                 priority, self.d.m.now, meta={"pallet": pid, "slot": slot, "spot": spot})
        self.slot_reserved[slot] = t.id
        self.out_pending[spot] += 1
        return t

    def _tote_order(self, priority: int, now: float, faces: list[str] | None = None) -> dict:
        self._oid += 1
        faces = faces or sorted(self.rng.sample(self.pickfaces, self.rng.choice((2, 2, 3))))
        self.metrics["tote_orders"] += 1
        return {"id": f"O{self._oid:05d}", "lines": [(f, self.rng.randint(1, 4)) for f in faces],
                "created": now, "priority": priority}

    def _aisle_order(self, face: str) -> tuple:
        """Pick path: aisles in turn, each walked in its travel direction (north in even aisles)."""
        n = self.layout.nodes[face]
        idx = min(range(len(PICK_AISLES)), key=lambda i: abs(PICK_AISLES[i] - n.x))
        return (idx, n.y if idx % 2 == 0 else -n.y)

    def _release_batches(self, now: float) -> None:
        while self.tote_backlog:
            self.tote_backlog.sort(key=lambda o: (-o["priority"], o["created"]))
            urgent = self.tote_backlog[0]["priority"] >= 2
            oldest = min(o["created"] for o in self.tote_backlog)
            if len(self.tote_backlog) < self.tote_batch and now - oldest < BATCH_WAIT_S and not urgent:
                return
            batch, self.tote_backlog = self.tote_backlog[:self.tote_batch], self.tote_backlog[self.tote_batch:]
            self.d.submit(self._tote_task(batch, now))

    def _tote_task(self, batch: list[dict], now: float, drop: str | None = None) -> Task:
        start = self.rng.choice(self.packs)
        drop = drop or self.rng.choice(self.packs)
        items: dict[str, int] = {}
        for o in batch:
            for face, n in o["lines"]:
                items[face] = items.get(face, 0) + n
        stops = [Stop(start, "x_toteInduct", {"totes": len(batch)}, start)]
        for face in sorted(items, key=self._aisle_order):
            stops.append(Stop(face, "x_pickItems", {"items": items[face]}, face))
        stops.append(Stop(drop, "x_toteDrop", {"totes": len(batch)}, drop))
        return Task(self.d.next_id(), "TOTE_PICK", AMR, stops, max(o["priority"] for o in batch), now,
                    meta={"orders": batch, "units": sum(items.values()),
                          "lines": sum(len(o["lines"]) for o in batch)})

    def make_task(self, kind: str, source: str, dest: str | None, priority: int) -> Task:
        L = self.layout
        if kind == "PALLET_OUT":
            if source in self.loc_index:
                slots = [self.loc_index[source]]
            elif source in L.nodes and L.nodes[source].kind == "rack":
                slots = [k for k in self.slots if k[0] == source]
            else:
                raise ValueError("PALLET_OUT source는 랙 위치(R1-05-L-2-1) 또는 랙 통로 노드(R*-**)여야 함")
            full = [k for k in slots if self.slots[k] not in (None, "UNKNOWN") and k not in self.slot_reserved]
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
            order = self._tote_order(priority, self.d.m.now, faces=[source])
            return self._tote_task([order], self.d.m.now, drop=dest)
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

    # ------------------------------------------------------------ physical truth
    def physical_check(self, serial: str, action) -> str | None:
        """What the forklift's load sensors find when a pick or drop starts (None: all clear)."""
        p = action.params
        slot = self.loc_index.get(str(p.get("stationName", "")))
        if action.action_type == "pick":
            if slot is not None and self.phys[slot] is None:
                return "적재물 없음(위치 비어 있음)"
            if self.truth_rng.random() < self.handling_fail_p:
                return "팔레트 포켓 인식 실패"
        elif action.action_type == "drop" and slot is not None and self.phys[slot] is not None:
            return "적치 위치 점유(다른 팔레트 있음)"
        return None

    def on_action_failed(self, task: Task, idx: int, reason: str, now: float, robot: str) -> tuple[str, str]:
        self.metrics["exceptions"] += 1
        tries = task.meta.setdefault("tries", {})
        tries[idx] = tries.get(idx, 0) + 1
        slot = task.meta.get("slot")
        if "포켓" in reason and tries[idx] <= 1:
            self.metrics["retries"] += 1
            self.d.emit("EXCEPTION", task, now, code="PICK_RETRY", detail=reason, location=task.stops[idx].label)
            return "retry", f"{reason}: 재시도"
        if task.kind == "PUTAWAY" and idx == 1 and "점유" in reason:
            # the WMS record was wrong: book the unknown pallet for a count and store to the next location
            self.slots[slot] = "UNKNOWN"
            self.slot_reserved.pop(slot, None)
            self.metrics["inventory_fixes"] += 1
            new = self._free_slot()
            self.d.emit("EXCEPTION", task, now, code="SLOT_OCCUPIED", detail=reason, location=location_id(slot))
            if new is None:
                return "fail", "빈 적치 위치 없음: 현장 회수"
            pid = task.meta["pallet"]
            task.stops[idx] = Stop(new[0], "drop", self._rack_params(new, pid), location_id(new))
            task.meta["slot"] = new
            self.slot_reserved[new] = task.id
            self.metrics["redirects"] += 1
            return "redirect", f"{location_id(slot)} 점유 → {location_id(new)}"
        if task.kind == "PALLET_OUT" and idx == 0:
            if "없음" in reason:
                self.slots[slot] = None       # the record was wrong: the location is empty
                self.metrics["inventory_fixes"] += 1
                code = "PALLET_NOT_FOUND"
            else:
                code = "PICK_FAILED"
            self.slot_reserved.pop(slot, None)
            self.d.emit("EXCEPTION", task, now, code=code, detail=reason, location=location_id(slot))
            new = self._oldest_full_slot(exclude={slot})
            if new is None:
                return "fail", "출고 가능한 다른 팔레트 없음"
            pid = self.slots[new]
            task.stops[0] = Stop(new[0], "pick", self._rack_params(new, pid), location_id(new))
            task.meta.update(slot=new, pallet=pid)
            self.slot_reserved[new] = task.id
            self.metrics["redirects"] += 1
            return "redirect", f"{location_id(slot)} {code} → {location_id(new)}"
        if task.kind == "PUTAWAY" and idx == 0:
            self.d.emit("EXCEPTION", task, now, code="PICK_FAILED", detail=reason, location=task.meta["spot"])
            return "requeue", f"{reason}: 현장 확인 후 재시도"
        return super().on_action_failed(task, idx, reason, now, robot)

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
                self.phys[slot] = pid
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
                self.phys[slot] = None
                self.slot_reserved.pop(slot, None)
                self.pallets[pid]["loc"] = ("agf", robot)
            else:
                spot = task.meta["spot"]
                self.out_stage[spot].append(pid)
                self.out_pending[spot] = max(0, self.out_pending[spot] - 1)
                self.pallets[pid]["loc"] = ("out_stage", spot)
        elif task.kind == "TOTE_PICK" and task.stops[idx].action == "x_pickItems":
            self.metrics["picks"] += 1
            self.metrics["units"] += int(task.stops[idx].params.get("items", 1))

    def on_task_end(self, task: Task, state: str, now: float) -> None:
        if task.kind == "TOTE_PICK":
            orders = task.meta.get("orders", [])
            if state == "DONE":
                self.metrics["tote_done"] += len(orders)
            else:
                # the batch did not finish: its orders go back to be picked again
                self.tote_backlog.extend(orders)
            return
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
        hours = max((now - self.window_start) / 3600.0, 1e-6)
        d2s = sorted(m["d2s"])
        known = [v for v in self.slots.values()]
        return {
            "trucks": m["trucks"], "pallets_unloaded": m["pallets_unloaded"], "putaway": m["putaway"],
            "shipped": m["shipped"], "out_backlog": m["out_backlog"], "tote_done": m["tote_done"],
            "tote_orders": m["tote_orders"], "tote_backlog": len(self.tote_backlog),
            "picks": m["picks"], "units": m["units"],
            "putaway_h": round(m["putaway"] / hours, 1), "shipped_h": round(m["shipped"] / hours, 1),
            "tote_h": round(m["tote_done"] / hours, 1),
            "dock_to_stock_avg": round(sum(d2s) / len(d2s), 1) if d2s else None,
            "dock_to_stock_p90": round(d2s[int(0.9 * (len(d2s) - 1))], 1) if d2s else None,
            "picker_wait_s": round(m["picker_wait_s"], 1),
            "rack_fill": round(sum(1 for v in known if v) / len(known), 3),
            "staged_in": sum(len(v) for v in self.in_stage.values()),
            "staged_out": sum(len(v) for v in self.out_stage.values()),
            "trucks_waiting": len(self.truck_queue),
            "exceptions": m["exceptions"], "retries": m["retries"], "redirects": m["redirects"],
            "inventory_fixes": m["inventory_fixes"],
        }

    def reset_metrics(self) -> None:
        """Start a measurement window (after warm-up): counters restart, the site state stays."""
        for k, v in list(self.metrics.items()):
            if isinstance(v, deque):
                v.clear()
            else:
                self.metrics[k] = 0.0 if isinstance(v, float) else 0

    def snapshot(self) -> dict:
        per_node: dict[str, int] = {}
        for (n, _side, _lvl, _pos), pid in self.slots.items():
            if pid:
                per_node[n] = per_node.get(n, 0) + 1
        return {
            "in_stage": {k: len(v) for k, v in self.in_stage.items()},
            "out_stage": {k: len(v) for k, v in self.out_stage.items()},
            "lane_capacity": STAGE_LANE_CAPACITY,
            "rack": per_node, "slots_per_node": len(SIDES) * LEVELS * POSITIONS,
            "docks_in": {f"IN{k + 1}": v for k, v in self.docks_in.items()},
            "trucks_waiting": len(self.truck_queue),
            "picker_claims": dict(self.picker_claims),
            "tote_backlog": len(self.tote_backlog),
        }

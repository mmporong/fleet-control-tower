"""Simulated operator for unattended runs and benchmarks.

It follows the same SOPs a person would, through the same audited commands,
with response delays drawn from ranges (acknowledge, remote reset, walking to
the robot). This keeps long benchmark runs from freezing on alarms that need
a human and makes MTTA/MTTR measurable. It is labelled in the audit log and is
off by default in the live control room.
"""
from __future__ import annotations

import random

from .ops import Operations


class AutoOperator:
    NAME = "가상 운영자"

    def __init__(self, ops: Operations, rng: random.Random, ack_delay=(10.0, 40.0),
                 reset_delay=(40.0, 90.0), field_delay=(240.0, 480.0)) -> None:
        self.ops = ops
        self.sim = ops.sim
        self.rng = rng
        self.ack_delay = ack_delay
        self.reset_delay = reset_delay
        self.field_delay = field_delay
        self.plans: dict[str, dict] = {}
        # doors this operator took out of use -> when the field fix is done; kept apart from the alarm, which
        # may clear by itself (controller answers again) while the door is still out of use
        self.door_plans: dict[str, float] = {}
        self.ack_times: list[float] = []

    def _do(self, cmd: str, **args) -> dict:
        return self.ops.execute({"cmd": cmd, **args}, operator=self.NAME)

    def tick(self, now: float) -> None:
        alarms = self.sim.manager.alarms.alarms
        for key, alarm in list(alarms.items()):
            plan = self.plans.get(key)
            if plan is None or plan["raised"] != alarm.raised_at + alarm.count:
                plan = {"raised": alarm.raised_at + alarm.count, "stage": 0,
                        "ack_at": now + self.rng.uniform(*self.ack_delay),
                        "act_at": now + self.rng.uniform(*self.reset_delay)}
                self.plans[key] = plan
            if alarm.state in ("UNACK", "RTN_UNACK") and now >= plan["ack_at"]:
                self.ack_times.append(now - alarm.last_change)
                self._do("ack_alarm", key=key, by=self.NAME)
                if key not in alarms:
                    continue
            if alarm.active and now >= plan["act_at"]:
                self._respond(alarm, plan, now)
        for key in [k for k in self.plans if k not in alarms]:
            del self.plans[key]
        doors = self.sim.manager.doors
        for door_id, fix_at in list(self.door_plans.items()):
            if now >= fix_at:
                del self.door_plans[door_id]
                if doors is not None and doors.view[door_id].disabled:
                    self._do("door_field_fix", door=door_id)
                    self._do("door_retry", door=door_id)

    def _respond(self, alarm, plan: dict, now: float) -> None:
        t, src, stage = alarm.type, alarm.source, plan["stage"]
        m = self.sim.manager
        rec = m.robots.get(src)
        if t == "ROBOT_FAULT" and rec is not None:
            if stage == 0 and rec.fatal != "BATTERY_DEPLETED":
                self._do("reset_fault", robot=src)
                plan.update(stage=1, act_at=now + 30.0)
            else:
                if rec.task_id:
                    self._do("reassign_task", robot=src)
                plan.update(stage=2, act_at=now + self.rng.uniform(*self.field_delay))
                if stage >= 2:
                    self._do("field_service_done", robot=src)
                    plan["act_at"] = now + 120.0
        elif t == "LOCALIZATION_LOST" and rec is not None:
            # SOP: re-localize remotely at the nearest node; when it is too far from one, the field team
            # drives it onto a node and re-localizes there
            if stage == 0 and self._do("init_position", robot=src)["ok"]:
                plan["act_at"] = now + 60.0
            elif stage == 0:
                if rec.task_id:
                    self._do("reassign_task", robot=src)
                plan.update(stage=1, act_at=now + self.rng.uniform(*self.field_delay))
            else:
                self._do("field_service_done", robot=src)
                plan.update(stage=0, act_at=now + 120.0)
        elif t in ("HUMAN_CONTACT", "ROBOT_COLLISION") and rec is not None:
            if stage == 0:
                plan.update(stage=1, act_at=now + self.rng.uniform(120.0, 300.0))
            else:
                self._do("field_service_done", robot=src)
                if t == "HUMAN_CONTACT":
                    self._do("resume_area")
                plan["act_at"] = now + 120.0
        elif t == "RESERVATION_MISMATCH" and rec is not None:
            self._do("resume_robot", robot=src)
            plan["act_at"] = now + 60.0
        elif t == "DEADLOCK_UNRESOLVED":
            members = alarm.data.get("members", [])
            victims = [m.robots[s] for s in members if s in m.robots and m.robots[s].task_id]
            unloaded = [r for r in victims if not r.loaded]
            if unloaded:
                victim = min(unloaded, key=lambda r: m.dispatcher.task_priority(r.task_id))
                self._do("cancel_task", task=victim.task_id)
            plan["act_at"] = now + 90.0
        elif t == "FACILITY_FAULT":
            # SOP: take the door out of use, call the facility technician, retry after the fix
            if src not in self.door_plans:
                self._do("door_disable", door=src)
                self.door_plans[src] = now + self.rng.uniform(*self.field_delay)
            plan["act_at"] = now + 3600.0
        else:
            plan["act_at"] = now + 3600.0

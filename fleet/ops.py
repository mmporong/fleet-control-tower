"""Operator commands with validation and an audit trail.

Every command a person (or the simulated operator) issues goes through
``Operations.execute``: it is validated, executed under the simulation lock by
the caller, and written to the audit log with the outcome, including refusals.
A read-only deployment rejects every command before it touches the fleet.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import math

from .sim import Simulation
from .traffic import node_res
from .vehicle import SAFETY_FAULTS

FAULTS_FIELD = ("DRIVE_FAULT", "SENSOR_FAULT", "BUMPER", "BATTERY_DEPLETED", "LOCALIZATION_LOST")


AUDIT_ARGS_CHARS = 1000      # command arguments kept in the audit log (the rest is cut off)


def _audit_args(args: dict) -> dict:
    """Arguments as the audit log keeps them: short values, at most AUDIT_ARGS_CHARS in all.

    >>> _audit_args({"robot": "AMR-01"})
    {'robot': 'AMR-01'}
    >>> list(_audit_args({"note": "x" * 5000}))
    ['_truncated']
    """
    text = json.dumps(args, ensure_ascii=False, default=str)
    if len(text) <= AUDIT_ARGS_CHARS:
        return args
    return {"_truncated": text[:AUDIT_ARGS_CHARS]}


class CommandError(ValueError):
    pass


class Operations:
    # commands that act on more than one robot need an explicit confirmation flag from the UI
    CONFIRM = {"fleet_estop", "fleet_estop_release", "fleet_hold", "fleet_resume", "zone_evacuate",
               "zone_lock", "set_mode_manual"}
    QUIET = {"teleop"}   # high-rate commands: audited when mode changes, not per packet

    def __init__(self, sim: Simulation, read_only: bool = False, store=None) -> None:
        """``store`` (fleet.store.Store) keeps the audit log durable and hash-chained; without it the log is
        only the in-memory tail (benchmarks, tests)."""
        self.sim = sim
        self.read_only = read_only
        self.store = store
        self.audit: deque = deque(store.audit_tail(200) if store is not None else (), maxlen=2000)
        self.counts = {"commands": 0, "interventions": 0}

    def record(self, entry: dict) -> dict:
        """Append to the audit log (durable when a store is attached)."""
        if self.store is not None:
            entry = self.store.append_audit(entry)
        self.audit.append(entry)
        return entry

    # ------------------------------------------------------------ helpers
    def _robot(self, args: dict):
        serial = str(args.get("robot", ""))
        rec = self.sim.manager.robots.get(serial)
        if rec is None:
            raise CommandError(f"알 수 없는 로봇: {serial!r}")
        return rec

    def _zone(self, args: dict):
        zid = str(args.get("zone", ""))
        zone = self.sim.layout.zones.get(zid)
        if zone is None:
            raise CommandError(f"알 수 없는 구역: {zid!r}")
        return zone

    def _door(self, args: dict) -> str:
        did = str(args.get("door", ""))
        if self.sim.manager.doors is None or did not in self.sim.manager.doors.view:
            raise CommandError(f"알 수 없는 문: {did!r}")
        return did

    def _task(self, args: dict):
        tid = str(args.get("task", ""))
        if tid not in self.sim.dispatcher.tasks:
            raise CommandError(f"진행 중인 작업이 아님: {tid!r}")
        return tid

    # ------------------------------------------------------------ entry point
    def execute(self, cmd: dict, operator: str = "operator") -> dict:
        name = str(cmd.get("cmd", ""))[:40]
        # who acted is the signed-in account, never a name the client sends along ("by")
        args = {k: v for k, v in cmd.items() if k not in ("cmd", "confirm", "by")}
        entry = {"t": round(self.sim.now, 1), "wall": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "operator": operator[:40], "cmd": name, "args": _audit_args(args)}
        args["by"] = operator[:40]
        try:
            if self.read_only:
                raise CommandError("관찰 전용 모드: 명령을 보낼 수 없습니다")
            handler = getattr(self, f"cmd_{name}", None)
            if handler is None:
                raise CommandError(f"알 수 없는 명령: {name!r}")
            if name in self.CONFIRM and not cmd.get("confirm"):
                raise CommandError("확인이 필요한 명령입니다 (confirm=true)")
            msg = handler(args) or "완료"
            ok = True
        except (CommandError, KeyError, ValueError, TypeError) as exc:
            msg, ok = str(exc).strip("'\""), False
        entry.update(ok=ok, msg=msg)
        if name not in self.QUIET or not ok:
            self.record(entry)
        if ok:
            self.counts["commands"] += 1
            if not name.startswith(("ack_", "shelve_", "close_")) and name not in self.QUIET:
                self.counts["interventions"] += 1
        return {"ok": ok, "msg": msg}

    # ------------------------------------------------------------ alarms & incidents
    def cmd_ack_alarm(self, a: dict) -> str:
        key = str(a.get("key", ""))
        if key not in self.sim.manager.alarms.alarms:
            raise CommandError("활성 알람이 아님")
        self.sim.manager.alarms.ack(key, a["by"], self.sim.now)
        self.sim.ack_incident(key)
        return f"{key} 확인"

    def cmd_ack_all(self, a: dict) -> str:
        n = 0
        for key, alarm in list(self.sim.manager.alarms.alarms.items()):
            if alarm.state in ("UNACK", "RTN_UNACK") and alarm.priority >= int(a.get("min_priority", 2)):
                self.sim.manager.alarms.ack(key, a["by"], self.sim.now)
                n += 1
        return f"{n}건 확인"

    def cmd_shelve_alarm(self, a: dict) -> str:
        key = str(a.get("key", ""))
        minutes = float(a.get("minutes", 10))
        if not 1 <= minutes <= 120:
            raise CommandError("보류 시간은 1~120분")
        self.sim.manager.alarms.shelve(key, a["by"], self.sim.now, minutes * 60,
                                       str(a.get("reason", "")))
        return f"{key} {minutes:.0f}분 보류"

    def cmd_close_incident(self, a: dict) -> str:
        for inc in self.sim.incidents:
            if inc.id == a.get("id"):
                note = str(a.get("note", "")).strip()
                if not note:
                    raise CommandError("종결 사유(원인·조치)를 입력하세요")
                inc.closed_at = self.sim.now
                inc.note = note[:500]
                return f"{inc.id} 종결"
        raise CommandError("알 수 없는 사고 기록")

    # ------------------------------------------------------------ shift handover
    def handover_summary(self) -> dict:
        """What the next shift must know, generated from live state (K11/K15-style handover)."""
        sim, m = self.sim, self.sim.manager
        k = sim.kpi()
        robots = m.robots.values()
        return {
            "sim_t": round(sim.now, 1), "scenario": sim.scenario.title, "mode": m.mode,
            "alarms": [f"[{a['priority_label']}] {a['title']} · {a['source']} · {a['message']} ({a['state']})"
                       for a in m.alarms.active_list() if a["priority"] <= 3],
            "incidents_open": [f"{i.id} {i.kind} {i.source}" for i in sim.incidents if i.closed_at is None],
            "faulted": [f"{r.serial} {r.fatal}" for r in robots if r.fatal],
            "offline": [r.serial for r in robots if r.connection != "ONLINE" and r.has_state],
            "manual": [r.serial for r in robots if r.mode == "MANUAL"],
            "paused": sorted(m.robot_paused | m.area_paused),
            "zones_locked": [z.name for z in sim.layout.zones.values() if z.locked],
            "zone_limits": {sim.layout.zones[z].name: v for z, v in m.zone_speed_override.items()},
            "deadlocks": [" → ".join(d["members"]) for d in m.deadlocks.values()],
            "tasks_queued": k["queued"], "tasks_active": k["active"],
            "kpi": {"throughput_h": k["throughput_h"], "traffic_wait_ratio": k["traffic_wait_ratio"],
                    "contacts": k["contacts"], "near_misses": k.get("near_misses"), "alarms_per_h": k["alarms_per_h"]},
        }

    def cmd_handover(self, a: dict) -> str:
        note = str(a.get("note", "")).strip()
        if not note:
            raise CommandError("인수인계 메모를 입력하세요(조치 중인 사항, 주의할 구역·로봇)")
        summary = self.handover_summary()
        self.sim.handovers.append({"t": round(self.sim.now, 1), "by": a["by"], "note": note[:1000],
                                   "summary": summary})
        return f"인수인계 기록: 알람 {len(summary['alarms'])}건, 미종결 사고 {len(summary['incidents_open'])}건"

    # ------------------------------------------------------------ fleet mode
    def cmd_fleet_hold(self, a: dict) -> str:
        if self.sim.manager.mode in ("ESTOP", "FIRE"):
            raise CommandError("일괄 정지·화재 경보 중에는 일시정지로 바꿀 수 없습니다")
        self.sim.manager.mode = "HOLD"
        self.sim.manager.event("fleet_hold", "fleet", "전체 일시정지", "warn")
        return "전체 일시정지"

    def cmd_fleet_resume(self, a: dict) -> str:
        m = self.sim.manager
        if m.mode == "ESTOP":
            raise CommandError("일괄 정지 중: 해제 후 재개하세요")
        if m.mode == "FIRE" or m.fire_alarm:
            raise CommandError("화재 경보 중: 경보 해제와 현장 확인 뒤 재개하세요")
        if m.mode == "RESYNC":
            raise CommandError("재기동 재동기화 중: 끝나면 자동으로 정상 운영")
        m.mode = "RUNNING"
        # resuming after an internal error is the operator's judgement that the cause is understood
        m.alarms.set("FMS_INTERNAL_ERROR", "fms", False, self.sim.now)
        m.event("fleet_resume", "fleet", "전체 재개", "info")
        return "전체 재개"

    def cmd_fleet_estop(self, a: dict) -> str:
        reason = str(a.get("reason", "")).strip()
        if not reason:
            raise CommandError("일괄 정지 사유를 입력하세요(감사 로그와 사고 기록에 남음)")
        self.sim.manager.mode = "ESTOP"
        self.sim.manager.alarms.set("FLEET_ESTOP", "fleet", True, self.sim.now, f"일괄 정지: {reason}")
        self.sim.manager.event("fleet_estop", "fleet", f"관제 일괄 정지(비안전): {reason}", "crit")
        return "일괄 정지 명령 전송(무선 소프트웨어 정지: 차량 안전 기능을 대신하지 않음)"

    def cmd_fleet_estop_release(self, a: dict) -> str:
        if self.sim.manager.mode != "ESTOP":
            raise CommandError("일괄 정지 상태가 아님")
        # release never restarts motion by itself; under a fire alarm the fleet goes back to the fire mode
        self.sim.manager.mode = "FIRE" if self.sim.manager.fire_alarm else "HOLD"
        self.sim.manager.alarms.set("FLEET_ESTOP", "fleet", False, self.sim.now)
        self.sim.manager.event("fleet_estop_release", "fleet", "일괄 정지 해제 → 일시정지 유지", "warn")
        return "일괄 정지 해제(일시정지 유지, 재개는 별도 명령)"

    def cmd_radio_yield(self, a: dict) -> str:
        """Ask the driver of the manual truck next to a robot, over the radio, to give way (standoff SOP)."""
        rec = self._robot(a)
        radio = getattr(self.sim, "radio_yield", None)
        if radio is None:
            return f"{rec.serial} 근처 지게차 운전자에게 무전 양보 요청(기록)"
        gave, answer = radio(rec.serial)
        if not gave:
            raise CommandError(f"{rec.serial} 근처 무전 양보 요청: {answer}")
        return f"{rec.serial} 근처 무전 양보 요청: {answer}"

    def cmd_resume_area(self, a: dict) -> str:
        n = len(self.sim.manager.area_paused)
        self.sim.manager.area_paused.clear()
        return f"구역 일시정지 {n}대 해제"

    # ------------------------------------------------------------ single robot
    def cmd_pause_robot(self, a: dict) -> str:
        rec = self._robot(a)
        self.sim.manager.robot_paused.add(rec.serial)
        return f"{rec.serial} 일시정지"

    def cmd_resume_robot(self, a: dict) -> str:
        rec = self._robot(a)
        self.sim.manager.robot_paused.discard(rec.serial)
        self.sim.manager.area_paused.discard(rec.serial)
        return f"{rec.serial} 재개"

    def cmd_reset_fault(self, a: dict) -> str:
        rec = self._robot(a)
        if not rec.fatal:
            raise CommandError("고장 상태가 아님")
        if rec.fatal in SAFETY_FAULTS:
            raise CommandError("안전 관련 고장(스캐너·범퍼)은 원격 리셋 불가: 현장 확인 후 차량에서 리셋")
        if not self.sim.manager.supports(rec, "x_resetFault"):
            raise CommandError("이 차량은 원격 리셋을 지원하지 않음(제원표): 현장 조치")
        self.sim.manager.instant(rec.serial, "x_resetFault")
        return f"{rec.serial} 원격 리셋 전송(결과는 상태 메시지로 확인)"

    def cmd_field_service_done(self, a: dict) -> str:
        rec = self._robot(a)
        if rec.fatal not in FAULTS_FIELD:
            raise CommandError("현장 조치 대상 고장이 아님")
        if rec.fatal in SAFETY_FAULTS:
            # a safety fault is cleared by the technician at the vehicle; the vehicle refuses a radio release.
            # The simulation models that local reset; a live site's vehicle reports it in its state
            local = getattr(self.sim, "field_service", None)
            if local is None:
                return f"{rec.serial} 현장 조치 등록: 안전 관련 고장은 차량에서 리셋해야 해제됨(상태 메시지로 확인)"
            local(rec.serial)
            return f"{rec.serial} 현장 조치 완료(차량에서 리셋)"
        self.sim.manager.instant(rec.serial, "x_fieldServiceDone")
        return f"{rec.serial} 현장 조치 완료 등록"

    def cmd_init_position(self, a: dict) -> str:
        rec = self._robot(a)
        node = a.get("node") or self.sim.layout.nearest_node((rec.x, rec.y))
        if node not in self.sim.layout.nodes:
            raise CommandError("알 수 없는 노드")
        n = self.sim.layout.nodes[node]
        if math.hypot(n.x - rec.x, n.y - rec.y) > 1.0:
            raise CommandError(f"마지막 보고 위치가 {node}에서 1 m 이상 떨어져 있음: 수동 이동 필요")
        self.sim.manager.instant(rec.serial, "initPosition",
                                 {"x": n.x, "y": n.y, "theta": rec.theta, "mapId": "warehouse-a", "lastNodeId": node})
        return f"{rec.serial} 위치 재지정 {node}"

    def cmd_set_mode_manual(self, a: dict) -> str:
        rec = self._robot(a)
        if rec.task_id:
            self.sim.dispatcher.detach(rec, "원격 조종 인계")
        self.sim.manager.clear_route(rec)
        self.sim.manager.instant(rec.serial, "x_setOperatingMode", {"mode": "MANUAL"})
        return f"{rec.serial} 원격 조종 인계(작업 재대기, 1배속 고정)"

    def cmd_set_mode_auto(self, a: dict) -> str:
        rec = self._robot(a)
        self.sim.manager.instant(rec.serial, "x_setOperatingMode", {"mode": "AUTOMATIC"})
        return f"{rec.serial} 자동 복귀 요청(노드 0.6 m 이내에서만 수락)"

    def cmd_teleop(self, a: dict) -> str:
        rec = self._robot(a)
        if rec.mode != "MANUAL":
            raise CommandError("MANUAL 모드가 아님")
        vx = max(-0.3, min(0.5, float(a.get("vx", 0.0))))
        om = max(-1.2, min(1.2, float(a.get("omega", 0.0))))
        self.sim.manager.instant(rec.serial, "x_teleop", {"vx": vx, "omega": om})
        return "ok"

    def cmd_send_to_park(self, a: dict) -> str:
        rec = self._robot(a)
        if rec.task_id:
            self.sim.dispatcher.detach(rec, "운영자 주차 지시")
            return f"{rec.serial} 작업 분리: 정지 후 주차"
        if not self.sim.manager.can_take_route(rec):
            raise CommandError("지금은 새 경로를 받을 수 없음(주행 중이거나 고장)")
        if not self.sim.dispatcher.send_away(rec, exclude=set()):
            raise CommandError("빈 주차 위치 없음")
        return f"{rec.serial} 주차 이동"

    def cmd_send_to_charge(self, a: dict) -> str:
        rec = self._robot(a)
        if not self.sim.manager.can_take_route(rec) or rec.task_id:
            raise CommandError("작업 없이 정지한 로봇만 충전 지시 가능")
        d = self.sim.dispatcher
        free = [c for c in d.spots_for("charger", rec.vclass) if c not in d.charger_owner
                and self.sim.manager.locks.holder_of(node_res(c)) in (None, rec.serial)]
        if not free:
            raise CommandError("이 차종이 쓸 수 있는 빈 충전기 없음")
        c = min(free, key=lambda c: d.planner.travel_time(rec.last_node, c, rec.vclass))
        from .vda5050 import action
        start = action("startCharging", d._aid(f"{rec.serial}-chg"), "HARD")
        if not self.sim.manager.assign_route(rec, c, "charge", [start]):
            raise CommandError("경로 없음")
        d._release_spots(rec.serial)
        d.charger_owner[c] = rec.serial
        return f"{rec.serial} → {c} 충전"

    # ------------------------------------------------------------ zones
    def cmd_zone_lock(self, a: dict) -> str:
        zone = self._zone(a)
        zone.locked = True
        self.sim.manager.event("zone_lock", zone.id, f"{zone.name} 봉쇄(신규 진입 차단)", "warn")
        return f"{zone.name} 봉쇄"

    def cmd_zone_unlock(self, a: dict) -> str:
        zone = self._zone(a)
        zone.locked = False
        self.sim.manager.event("zone_unlock", zone.id, f"{zone.name} 봉쇄 해제", "info")
        return f"{zone.name} 봉쇄 해제"

    def cmd_zone_speed(self, a: dict) -> str:
        zone = self._zone(a)
        limit = a.get("limit")
        if limit in (None, "", "null"):
            self.sim.manager.zone_speed_override.pop(zone.id, None)
            return f"{zone.name} 속도 제한 해제"
        limit = float(limit)
        if not 0.1 <= limit <= 1.5:
            raise CommandError("속도 제한은 0.1~1.5 m/s")
        self.sim.manager.zone_speed_override[zone.id] = limit
        return f"{zone.name} {limit:.1f} m/s 제한"

    def cmd_zone_evacuate(self, a: dict) -> str:
        """Lock the zone and move robots inside it to the nearest node outside."""
        zone = self._zone(a)
        zone.locked = True
        m = self.sim.manager
        inside_nodes = {n for n, zs in self.sim.layout.node_zones.items() if zone.id in zs}
        moved, stuck = [], []
        for rec in m.robots.values():
            if rec.last_node not in inside_nodes:
                continue
            if rec.task_id:
                self.sim.dispatcher.detach(rec, f"{zone.name} 대피")
            if not m.can_take_route(rec):
                stuck.append(rec.serial)
                continue
            outside = sorted((n for n in self.sim.layout.nodes if n not in inside_nodes
                              and self.sim.layout.nodes[n].kind not in ("inbound", "outbound", "charger", "in_stage",
                                                                        "out_stage", "pack")),
                             key=lambda n: m.planner.travel_time(rec.last_node, n, rec.vclass))
            for target in outside[:5]:
                if m.assign_route(rec, target, "evacuate"):
                    moved.append(rec.serial)
                    break
            else:
                stuck.append(rec.serial)
        m.event("zone_evacuate", zone.id, f"{zone.name} 대피: 이동 {len(moved)}대, 대기 {len(stuck)}대", "crit")
        return f"{zone.name} 봉쇄·대피: 이동 {len(moved)}대" + (f", 수동 확인 필요 {', '.join(stuck)}" if stuck else "")

    # ------------------------------------------------------------ tasks
    def cmd_cancel_task(self, a: dict) -> str:
        return self.sim.dispatcher.cancel(self._task(a))

    def cmd_reassign_task(self, a: dict) -> str:
        if a.get("robot") and not a.get("task"):
            rec = self._robot(a)
            if not rec.task_id:
                raise CommandError("작업이 없는 로봇")
            return self.sim.dispatcher.reassign(rec.task_id)
        return self.sim.dispatcher.reassign(self._task(a))

    def cmd_reprioritize_task(self, a: dict) -> str:
        return self.sim.dispatcher.reprioritize(self._task(a), int(a.get("priority", 1)))

    def cmd_add_task(self, a: dict) -> str:
        t = self.sim.dispatcher.add_task(str(a.get("kind", "PICK")), str(a.get("source", "")),
                                         a.get("dest") or None, int(a.get("priority", 0)))
        return f"{t.id} 등록"

    def cmd_set_task_rate(self, a: dict) -> str:
        rate = float(a.get("rate", 360))
        if not 0 <= rate <= 2000:
            raise CommandError("투입률은 0~2000건/시간")
        self.sim.dispatcher.rate_per_h = rate
        self.sim.dispatcher.next_arrival = self.sim.now
        return f"투입률 {rate:.0f}건/시간"

    # ------------------------------------------------------------ facility (rapid-roll doors)
    def cmd_door_disable(self, a: dict) -> str:
        did = self._door(a)
        self.sim.manager.doors.view[did].disabled = True
        return f"{self.sim.layout.doors[did].name} 사용 중지: 새 경로는 이 문을 피함"

    def cmd_door_retry(self, a: dict) -> str:
        did = self._door(a)
        self.sim.manager.doors.retry(did, self.sim.now)
        return f"{self.sim.layout.doors[did].name} 사용 재개: 다음 통과 차량이 열림을 다시 요청"

    def cmd_door_field_fix(self, a: dict) -> str:
        did = self._door(a)
        self.sim.door_field_fix(did)
        return f"[현장 모의] {self.sim.layout.doors[did].name} 수리 완료"

    # ------------------------------------------------------------ drills (simulation only)
    def cmd_drill_door(self, a: dict) -> str:
        did = self._door(a)
        fault = str(a.get("fault", "JAM"))
        self.sim.drill_door(did, fault)
        return f"[훈련] {self.sim.layout.doors[did].name} {fault} 주입"

    def cmd_drill_fault(self, a: dict) -> str:
        rec = self._robot(a)
        self.sim.drill_fault(rec.serial, str(a.get("fault", "DRIVE_FAULT")), bool(a.get("recoverable", True)))
        return f"[훈련] {rec.serial} {a.get('fault', 'DRIVE_FAULT')} 주입"

    def cmd_drill_fire(self, a: dict) -> str:
        on = bool(a.get("alarm", True))
        self.sim.drill_fire(on)
        return f"[훈련] 화재 경보 {'발령' if on else '해제'}"

    def cmd_drill_comms(self, a: dict) -> str:
        rec = self._robot(a)
        seconds = float(a.get("seconds", 20))
        self.sim.drill_comms(rec.serial, seconds)
        return f"[훈련] {rec.serial} 통신 {seconds:.0f}초 차단"

    def cmd_drill_intruder(self, a: dict) -> str:
        zone = self._zone(a)
        hid = self.sim.drill_intruder(zone.id, float(a.get("seconds", 45)))
        return f"[훈련] {zone.name}에 사람 {hid} 진입"

    # ------------------------------------------------------------ WMS-facing API
    def _task_known(self, task_id: str, ref: str) -> bool:
        """The task the key points to exists and was made for this order (ids restart with a new simulation)."""
        d = self.sim.dispatcher
        for t in list(d.tasks.values()) + list(d.finished):
            if t.id == task_id:
                return t.meta.get("ref") == ref
        if self.store is not None and getattr(self.sim, "simulated", True) is False:
            stored = self.store.task(task_id)
            return stored is not None and (stored.get("meta") or {}).get("ref") == ref
        return False

    def _task_for_ref(self, ref: str) -> str | None:
        d = self.sim.dispatcher
        for t in list(d.tasks.values()) + list(d.finished):
            if t.meta.get("ref") == ref:
                return t.id
        if self.store is not None and getattr(self.sim, "simulated", True) is False:
            return self.store.task_by_ref(ref)
        return None

    def submit_wms_order(self, order: dict, seen, operator: str = "WMS") -> dict:
        """Idempotent task intake for an upper system (WMS/WCS).

        ``order_id`` is the idempotency key: the same id with the same content
        returns the existing task, with different content it is refused.
        """
        oid = str(order.get("order_id", "")).strip()
        if not oid or len(oid) > 64 or not all(c.isalnum() or c in "._:-" for c in oid):
            raise CommandError("order_id: 1~64자 영문·숫자·._:-")
        body = {"kind": order.get("kind"), "source": order.get("source"), "dest": order.get("dest"),
                "priority": int(order.get("priority", 0))}
        if oid in seen:
            prev = seen[oid]
            if prev["body"] != body:
                raise CommandError(f"order_id {oid}가 다른 내용으로 이미 접수됨")
            if self._task_known(prev["task"], oid):
                return {"ok": True, "duplicate": True, "task": prev["task"]}
            # the key was written but its task is gone (a restart of the simulation, or a crash between the two
            # writes): the order is taken again below
        else:
            existing = self._task_for_ref(oid)
            if existing is not None:
                # the task was created but the key was not written (a crash between the two): no second task
                seen[oid] = {"body": body, "task": existing}
                return {"ok": True, "duplicate": True, "task": existing}
        kinds = {k["kind"]: k for k in self.sim.process.task_kinds}
        if body["kind"] not in kinds:
            raise CommandError(f"kind는 {', '.join(kinds)} 중 하나")
        spec = kinds[body["kind"]]
        layout = self.sim.layout
        # src/dst None: any layout node (the runtime's transport orders name stations by node id)
        if body["source"] not in layout.nodes or \
                spec["src"] is not None and layout.nodes[body["source"]].kind not in spec["src"]:
            raise CommandError(f"source는 {'/'.join(spec['src'] or ['레이아웃'])} 노드여야 함")
        if body["dest"] is None and not spec.get("dst_optional"):
            raise CommandError("dest가 필요함")
        if body["dest"] is not None and (body["dest"] not in layout.nodes or spec["dst"] is not None
                                         and layout.nodes[body["dest"]].kind not in spec["dst"]):
            raise CommandError(f"dest는 {'/'.join(spec['dst'] or ['레이아웃'])} 노드여야 함")
        t = self.sim.dispatcher.add_task(body["kind"], body["source"], body["dest"], body["priority"], ref=oid)
        t.note = f"WMS {oid}"
        seen[oid] = {"body": body, "task": t.id}
        self.record({"t": round(self.sim.now, 1), "wall": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "operator": operator, "cmd": "wms_order", "args": {"order_id": oid, **body},
                     "ok": True, "msg": t.id})
        return {"ok": True, "duplicate": False, "task": t.id}

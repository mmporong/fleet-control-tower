"""Alarm lifecycle following ISA-18.2 / IEC 62682 terms.

Every alarm type has a priority and a defined operator response (SOP): an
alarm without a response is an event, not an alarm. States:

  UNACK      active, not acknowledged
  ACKED      active, acknowledged
  RTN_UNACK  returned to normal, still needs acknowledgement
  SHELVED    temporarily hidden by an operator, with reason and expiry

Flood detection uses the standard's rule of thumb: more than 10 new alarms in
10 minutes per operator position.

Priorities follow consequence and the time the operator has (rationalised against ISA-18.2's
guidance that most annunciated alarms are low priority): 1 people or the control system itself
are at stake (contact, collision, fire, internal error); 2 the site's traffic integrity
(reservation mismatch, unresolved deadlock, a failed door, no infrastructure feed); 3 one
vehicle or task needs attention within 30 minutes; 4 alerts: informative, counted apart, no
acknowledgement (congestion, short comms dropouts, the operator's own fleet stop).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

PRIORITY_LABEL = {1: "긴급", 2: "높음", 3: "보통", 4: "알림"}
RESPONSE_TIME = {1: "즉시", 2: "5분 이내", 3: "30분 이내", 4: "사후 검토"}
# ISA-18.2 keeps "alerts" apart from alarms: they need review, not a timely response,
# so they never count toward alarm rates or flood detection and need no acknowledgement.
ALERT = 4


@dataclass(frozen=True)
class AlarmDef:
    priority: int
    title: str
    auto: str                 # what the system already did
    steps: tuple[str, ...]    # operator response
    commands: tuple[str, ...] = ()   # command types the UI offers next to the SOP
    off_delay_s: float = 0.0  # condition must stay false this long before the alarm clears (anti-chatter)


ALARM_DEFS: dict[str, AlarmDef] = {
    "HUMAN_CONTACT": AlarmDef(
        1, "사람 접촉", "해당 로봇 범퍼 정지, 반경 6 m 로봇 일시정지",
        ("현장 인원에게 무전으로 부상 여부 확인, 부상 시 응급 절차 우선",
         "주변 일시정지 유지, 필요하면 해당 구역 봉쇄",
         "로봇을 수동 모드로 전환해 격리, 사고 전후 기록 보존",
         "원인 분류: 사각지대 진입 / 부주의 보행 / 보호필드 설정",
         "안전 담당 확인 후 현장 조치 완료 처리, 주변 로봇 재개"),
        ("zone_lock", "set_mode_manual", "field_service_done", "resume_area")),
    "ROBOT_COLLISION": AlarmDef(
        1, "충돌(범퍼 접촉)", "관련 로봇 범퍼 정지(안전 관련 고장: 원격 리셋 불가)",
        ("사고 재생으로 두 유닛의 진입 순서와 예약 상태 확인",
         "현장 인원이 차량·적재물·범퍼 손상과 주변을 확인",
         "현장 인원이 차량에서 리셋한 뒤 '현장 조치 완료' 등록",
         "예약 불일치·통신 지연이 원인이면 해당 구간 봉쇄 후 보고"),
        ("field_service_done", "zone_lock")),
    "FLEET_ESTOP": AlarmDef(
        4, "관제 일괄 정지 중(비안전)", "전체 로봇 정지 명령 전송",
        ("운영자가 건 정지: 사유는 감사 로그에 기록됨",
         "현장 안전 확인(사람·지게차 동선)",
         "일괄 정지 해제 후 일시정지 상태로 전환",
         "구역별로 순차 재개"),
        ("fleet_estop_release", "fleet_resume")),
    "RESERVATION_MISMATCH": AlarmDef(
        2, "점유-예약 불일치", "해당 로봇 일시정지",
        ("로봇 위치와 예약된 노드·레인 비교(지도 예약 레이어)",
         "통신 지연·상태 누락 여부 확인(메시지 검사기)",
         "위치가 맞으면 재개, 아니면 수동 모드로 노드 복귀 후 자동 전환"),
        ("resume_robot", "set_mode_manual")),
    "DEADLOCK_UNRESOLVED": AlarmDef(
        2, "교착 자동 해소 실패", "교착 로봇 목록 고정, 신규 진입 차단 없음",
        ("사이클에 포함된 로봇과 대기 자원 확인",
         "우선순위 낮은 로봇 1대를 작업 취소 또는 수동 이동",
         "해소 후 동일 지점 반복 여부 확인(혼잡 지도)"),
        ("cancel_task", "set_mode_manual", "send_to_park")),
    "ROBOT_FAULT": AlarmDef(
        3, "로봇 고장", "로봇 자체 정지, 예약 유지",
        ("고장 종류 확인: 구동부 과전류 같은 비안전 고장만 원격 리셋 1회 시도",
         "안전 스캐너 이상은 원격 리셋 불가: 현장 인원이 확인하고 차량에서 리셋",
         "리셋 실패나 현장 조치가 필요하면 작업 재할당, 적재물이 있으면 현장 회수 요청",
         "복구 후 자동 모드와 위치 확인"),
        ("reset_fault", "reassign_task", "field_service_done")),
    "LOCALIZATION_LOST": AlarmDef(
        3, "위치 추정 실패", "로봇 정지, 주문 거부 상태",
        ("마지막 확인 노드와 지도 위치 대조",
         "위치 재지정(initPosition) 실행: 노드 1 m 안이어야 성공",
         "실패 시 원격 조종으로 가까운 노드 0.6 m 안으로 옮긴 뒤 자동 전환, 또는 현장 인원이 옮기고 재위치 추정",
         "현장 조치를 마치면 '현장 조치 완료'로 복귀, 막고 있던 작업은 재할당"),
        ("init_position", "set_mode_manual", "field_service_done", "reassign_task")),
    "COMMS_LOST": AlarmDef(
        3, "통신 두절(30초 이상)", "예약 유지, 적재 전 작업은 자동 재할당(로봇은 해제 구간 끝에서 자체 정지)",
        ("마지막 수신 위치와 해제 구간 확인",
         "무선 AP·로봇 전원 상태 현장 확인 요청",
         "적재 중이면 현장 회수 여부 판단",
         "복구 후 상태 동기화(주문 ID·위치) 확인"),
        ("reassign_task",)),
    # a short silence (Wi-Fi roaming) needs no action: the vehicle stops at its base end by itself and the
    # reservations stay; it becomes the COMMS_LOST alarm only when it lasts
    "COMMS_DEGRADED": AlarmDef(
        4, "통신 지연", "예약 유지, 30초 지속 시 통신 두절 알람으로 승격",
        ("반복되는 위치면 무선 커버리지 점검 대상으로 기록",),
        ()),
    "INFRA_FEED_LOST": AlarmDef(
        2, "인프라 감지 두절", "사람 작업·보행·피킹 구역 전체에 사람 감속 적용",
        ("카메라·위치 서버 상태와 네트워크 확인",
         "복구 전까지 사각지대 구역 봉쇄 여부 판단",
         "복구 후 추적 수가 평소 수준으로 돌아왔는지 확인"),
        ("zone_lock",)),
    "FACILITY_FAULT": AlarmDef(
        2, "설비(고속문) 이상", "해당 문을 경로에서 제외하고 대기 차량을 우회 경로로 재계획",
        ("문 상태·마지막 응답 시각과 영향 차량 확인(설비 목록)",
         "우회 경로가 없어 대기 중인 차량이 있으면 작업 재배정 검토",
         "설비 담당 호출: 문 제어기 전원·통신, 커튼 걸림 확인",
         "수리 동안 계속 쓰지 않을 문은 '사용 중지'로 두어 재계획 반복을 막음",
         "현장 조치 완료 후 '재시도'로 다시 열림을 요청해 정상 동작 확인"),
        ("door_disable", "door_retry")),
    "STANDOFF": AlarmDef(
        3, "대치(보호정지 지속)", "로봇은 보호필드 정지 유지, 관제는 비켜서기 재시도",
        ("원인 유닛 확인: 사람·지게차·외부 로봇(지도·카메라)",
         "지게차면 운전자에게 무전으로 진로 양보 요청",
         "사람이면 현장 안내, 로봇 쪽 공간이 있으면 원격 조종으로 후진",
         "같은 지점에서 반복되면 동선 분리 검토"),
        ("set_mode_manual", "cancel_task"), off_delay_s=30.0),
    "BATTERY_CRITICAL": AlarmDef(
        3, "배터리 위험", "신규 작업 배정 중단",
        ("현재 작업 유지 가능 여부 확인",
         "불가하면 작업 재할당 후 충전 복귀",
         "충전기 점유 현황 확인"),
        ("send_to_charge", "reassign_task")),
    "BLOCKED_LONG": AlarmDef(
        3, "장시간 정지", "우회 경로 재탐색 반복",
        ("대기 원인 확인(사람·고장 로봇·구역 봉쇄·외부 로봇)",
         "원인 제거 또는 작업 취소·재할당",
         "같은 지점이 반복되면 동선 설계 검토"),
        ("cancel_task", "reassign_task", "zone_unlock"), off_delay_s=30.0),
    "ALARM_FLOOD": AlarmDef(
        2, "알람 홍수", "없음",
        ("우선순위 1부터 처리, 같은 원인 알람은 묶어 판단",
         "근본 원인 구역을 일시정지해 신규 알람 억제",
         "해소 후 보류 알람 재검토"),
        ("fleet_hold",)),
    "NEAR_MISS": AlarmDef(
        ALERT, "근접 사고", "사고 기록에 전후 40초 저장",
        ("사고 재생으로 경위 확인",
         "반복 지점이면 구역 속도 제한 검토",
         "근접 사고 보고 작성"),
        ("zone_speed",)),
    "CONGESTION": AlarmDef(
        4, "혼잡", "없음",
        ("혼잡 지도에서 대기 집중 노드 확인",
         "작업 투입률 조정 또는 구역 속도·방향 검토"),
        ("set_task_rate",), off_delay_s=30.0),
    "TASK_OVERDUE": AlarmDef(
        3, "작업 지연", "없음",
        ("배정 불가 사유 확인(가용 로봇·배터리·도크 점유)",
         "우선순위 조정 또는 수동 배정"),
        ("reprioritize_task",), off_delay_s=30.0),
    "ZONE_LOCK_WAITING": AlarmDef(
        4, "봉쇄 구역 대기", "봉쇄 구역 신규 진입 차단",
        ("봉쇄 사유가 끝났는지 확인",
         "필요하면 대기 로봇 작업 취소 또는 봉쇄 해제"),
        ("zone_unlock", "cancel_task"), off_delay_s=30.0),
    "FIRE_ALARM": AlarmDef(
        1, "화재 경보", "신규 해제·배정 중단, 차량은 해제 구간 끝(문턱·교차로 아님)에서 정지, 방화문 폐쇄",
        ("대피 상황 확인: 피난로·방화문 앞에 정지한 차량이 있는지 지도에서 확인",
         "방화문 문턱이나 피난로를 막은 차량이 있으면 원격 조종으로 비켜 세움",
         "경보 해제 후 현장 확인을 마치고 전체 재개"),
        ("set_mode_manual", "fleet_resume")),
    "FMS_INTERNAL_ERROR": AlarmDef(
        1, "관제 내부 오류", "전체 일시정지(차량은 해제 구간 끝에서 정지), 제어 주기는 계속",
        ("이벤트 기록과 서비스 로그의 오류 내용 확인",
         "현장 안전 확인 후 원인이 해소됐으면 전체 재개, 반복되면 관제 서비스 재기동",
         "재기동은 재동기화(RESYNC)를 거쳐 예약을 복원함"),
        ("fleet_resume",)),
    "CHARGER_SHORTAGE": AlarmDef(
        4, "충전기 부족", "충전 대기열 운영",
        ("충전기 고장·점유 로봇 확인",
         "충전 완료 로봇을 작업에 우선 배정"),
        (), off_delay_s=30.0),
}


@dataclass
class Alarm:
    key: str
    type: str
    source: str
    priority: int
    message: str
    raised_at: float
    state: str = "UNACK"
    active: bool = True
    count: int = 1
    last_change: float = 0.0
    clear_pending: float | None = None
    acked_by: str = ""
    shelved_until: float = -1.0
    shelve_reason: str = ""
    data: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d = ALARM_DEFS[self.type]
        return {"key": self.key, "type": self.type, "source": self.source, "priority": self.priority,
                "priority_label": PRIORITY_LABEL[self.priority], "title": d.title, "message": self.message,
                "raised_at": round(self.raised_at, 1), "state": self.state, "active": self.active,
                "count": self.count, "acked_by": self.acked_by,
                "shelved_until": round(self.shelved_until, 1) if self.state == "SHELVED" else None,
                "shelve_reason": self.shelve_reason, "auto": d.auto, "steps": list(d.steps),
                "commands": list(d.commands), "response": RESPONSE_TIME[self.priority], "data": self.data}


class AlarmManager:
    FLOOD_WINDOW = 600.0
    FLOOD_LIMIT = 10

    def __init__(self) -> None:
        self.alarms: dict[str, Alarm] = {}
        self.history: deque = deque(maxlen=400)
        self.activations: deque = deque()
        self.total_activations = 0
        self.alerts = 0
        self.by_priority = {1: 0, 2: 0, 3: 0, 4: 0}
        self.by_type: dict[str, int] = {}
        self.listeners: list = []   # called with (event, alarm)

    def _emit(self, event: str, alarm: Alarm) -> None:
        for fn in self.listeners:
            fn(event, alarm)

    def set(self, type_: str, source: str, active: bool, now: float, message: str = "",
            data: dict | None = None) -> Alarm | None:
        if type_ not in ALARM_DEFS:
            raise KeyError(type_)
        key = f"{type_}:{source}"
        alarm = self.alarms.get(key)
        if active:
            if alarm is None:
                alarm = Alarm(key, type_, source, ALARM_DEFS[type_].priority, message, now, last_change=now,
                              data=data or {})
                self.alarms[key] = alarm
                self._activated(alarm, now, "raised")
            elif not alarm.active:
                alarm.active = True
                alarm.count += 1
                alarm.message = message or alarm.message
                alarm.last_change = now
                if alarm.state != "SHELVED":
                    alarm.state = "UNACK"
                    alarm.acked_by = ""
                self._activated(alarm, now, "reactivated")
            else:
                alarm.clear_pending = None
                if message:
                    alarm.message = message
                if data:
                    alarm.data = data
            return alarm
        if alarm is None or not alarm.active:
            return alarm
        delay = ALARM_DEFS[type_].off_delay_s
        if delay > 0:
            if alarm.clear_pending is None:
                alarm.clear_pending = now
                return alarm
            if now - alarm.clear_pending < delay:
                return alarm
        alarm.clear_pending = None
        alarm.active = False
        alarm.last_change = now
        if alarm.state in ("ACKED",):
            self._close(alarm, now)
        elif alarm.state == "UNACK":
            alarm.state = "RTN_UNACK"
            self._emit("rtn", alarm)
        elif alarm.state == "SHELVED":
            self._close(alarm, now)
        return alarm

    def _activated(self, alarm: Alarm, now: float, how: str) -> None:
        self.by_priority[alarm.priority] = self.by_priority.get(alarm.priority, 0) + 1
        self.by_type[alarm.type] = self.by_type.get(alarm.type, 0) + 1
        if alarm.priority == ALERT:
            alarm.state = "ACKED"          # alerts are informational: no acknowledgement required
            self.alerts += 1
            self._emit(how, alarm)
            return
        self.activations.append(now)
        self.total_activations += 1
        self._emit(how, alarm)

    def _close(self, alarm: Alarm, now: float) -> None:
        self.alarms.pop(alarm.key, None)
        rec = alarm.to_json()
        rec["closed_at"] = round(now, 1)
        self.history.append(rec)
        self._emit("closed", alarm)

    def is_active(self, key: str) -> bool:
        alarm = self.alarms.get(key)
        return alarm is not None and alarm.active

    def ack(self, key: str, operator: str, now: float) -> Alarm:
        alarm = self.alarms[key]
        alarm.acked_by = operator
        alarm.last_change = now
        if alarm.state == "RTN_UNACK":
            self._close(alarm, now)
        elif alarm.state == "UNACK":
            alarm.state = "ACKED"
        return alarm

    def shelve(self, key: str, operator: str, now: float, duration_s: float, reason: str) -> Alarm:
        alarm = self.alarms[key]
        if alarm.priority == 1:
            raise ValueError("긴급(1) 알람은 보류할 수 없습니다")
        if not reason.strip():
            raise ValueError("보류 사유가 필요합니다")
        alarm.state = "SHELVED"
        alarm.shelved_until = now + duration_s
        alarm.shelve_reason = f"{operator}: {reason.strip()}"
        alarm.last_change = now
        return alarm

    def tick(self, now: float) -> None:
        for alarm in list(self.alarms.values()):
            if alarm.state == "SHELVED" and now >= alarm.shelved_until:
                if alarm.active:
                    alarm.state = "UNACK"
                    alarm.last_change = now
                else:
                    self._close(alarm, now)
        while self.activations and now - self.activations[0] > self.FLOOD_WINDOW:
            self.activations.popleft()
        flood = len(self.activations) > self.FLOOD_LIMIT
        self.set("ALARM_FLOOD", "fleet", flood, now,
                 f"최근 10분 신규 알람 {len(self.activations)}건" if flood else "")

    def active_list(self) -> list[dict]:
        order = {"UNACK": 0, "RTN_UNACK": 1, "ACKED": 2, "SHELVED": 3}
        items = sorted(self.alarms.values(), key=lambda a: (a.state == "SHELVED", a.priority,
                                                             order[a.state], -a.raised_at))
        return [a.to_json() for a in items]

    def counts(self) -> dict:
        c = {"p1": 0, "p2": 0, "p3": 0, "p4": 0, "unack": 0, "shelved": 0, "standing": 0}
        for a in self.alarms.values():
            if a.state == "SHELVED":
                c["shelved"] += 1
                continue
            c[f"p{a.priority}"] += 1
            if a.state in ("UNACK", "RTN_UNACK"):
                c["unack"] += 1
            if a.active:
                c["standing"] += 1
        c["last10min"] = len(self.activations)
        return c

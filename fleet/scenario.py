"""Scenario presets: fleet size, people, forklifts, demand and failure rates."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    robots: int = 30
    workers: int = 4
    pickers: int = 2
    walkers: int = 2
    forklifts: int = 2
    external: int = 1
    rate_per_h: float = 360.0
    two_way: bool = False
    fault_mtbf_h: float = 0.0        # per robot; 0 disables random faults
    comms_mtbf_h: float = 0.0        # per robot; 0 disables random outages
    comms_loss: float = 0.0          # random message loss probability per message

    def to_json(self) -> dict:
        return asdict(self)


PRESETS: dict[str, Scenario] = {s.name: s for s in (
    Scenario("standard", "표준 교대", "AMR 30대, 사람 8명, 지게차 2대, 외부 청소로봇 1대, 시간당 120건",
             rate_per_h=120.0, fault_mtbf_h=8.0, comms_mtbf_h=6.0),
    Scenario("peak", "피크 출고", "AMR 40대, 시간당 240건. 용량 한계 부근의 도크·교차로 혼잡",
             robots=40, rate_per_h=240.0, fault_mtbf_h=8.0, comms_mtbf_h=6.0),
    Scenario("crowded", "사람 밀집", "작업자·피커·보행자 20명. 사람 예상 점유와 구역 감속",
             workers=8, pickers=4, walkers=8, rate_per_h=120.0),
    Scenario("deadlock", "교착 스트레스", "모든 레인 1차선 양방향 + AMR 36대, 시간당 240건. 정면 교착 유발(비교 기준선)",
             robots=36, workers=0, pickers=0, walkers=2, forklifts=0, external=0, rate_per_h=240.0,
             two_way=True),
    Scenario("faults", "장애 훈련", "고장·통신 두절이 잦은 교대(로봇당 평균 1시간), 메시지 손실 2%",
             rate_per_h=120.0, fault_mtbf_h=1.0, comms_mtbf_h=1.0, comms_loss=0.02),
    Scenario("night", "야간 무인", "사람·지게차 없이 AMR 20대와 외부 청소로봇, 시간당 120건",
             robots=20, workers=0, pickers=0, walkers=0, forklifts=0, rate_per_h=120.0),
)}


def get(name: str, **overrides) -> Scenario:
    if name not in PRESETS:
        raise KeyError(f"unknown scenario {name!r}; choose from {', '.join(PRESETS)}")
    base = PRESETS[name]
    clean = {k: v for k, v in overrides.items() if v is not None}
    return replace(base, **clean) if clean else base

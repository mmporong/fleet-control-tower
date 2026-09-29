"""Scenario presets.

``dc`` scenarios run the pallet distribution centre (autonomous forklifts, tote
AMRs, pickers, dock forklifts). ``grid`` scenarios run the algorithm test grid
with one generic AMR class; they isolate traffic control from the site process.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    site: str = "dc"
    # ---- dc fleet and process
    agf: int = 10
    amr: int = 16
    trucks_h: float = 5.0
    out_pallets_h: float = 45.0
    tote_orders_h: float = 150.0
    pickers: int = 6
    packers: int = 4
    dock_workers: int = 3
    walkers: int = 2
    dock_forklifts_in: int = 2
    dock_forklifts_out: int = 1
    rack_forklift: int = 1
    # ---- grid fleet and process
    robots: int = 30
    workers: int = 4
    forklifts: int = 2
    rate_per_h: float = 120.0
    two_way: bool = False
    # ---- shared
    external: int = 1
    fault_mtbf_h: float = 0.0        # per robot; 0 disables random faults
    comms_mtbf_h: float = 0.0        # per robot; 0 disables random outages
    comms_loss: float = 0.0          # random message loss probability per message

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def fleet_size(self) -> int:
        return self.agf + self.amr if self.site == "dc" else self.robots


PRESETS: dict[str, Scenario] = {s.name: s for s in (
    Scenario("standard", "표준 교대", "물류센터 · 자율 지게차 10 · 토트 AMR 16 · 피커 6 · 수동 지게차 4 · 입고 트럭 5대/h",
             fault_mtbf_h=8.0, comms_mtbf_h=6.0),
    Scenario("peak", "피크 출고", "자율 지게차 12 · 토트 AMR 22 · 피커 9 · 출고 70팔레트/h · 토트 240건/h",
             agf=12, amr=22, pickers=9, trucks_h=6.0, out_pallets_h=70.0, tote_orders_h=240.0,
             fault_mtbf_h=8.0, comms_mtbf_h=6.0),
    Scenario("crowded", "사람 밀집", "피커 12 · 도크 작업자 6 · 보행자 6: 협업 피킹과 사람 구역 감속",
             pickers=12, dock_workers=6, walkers=6, packers=6),
    Scenario("faults", "장애 훈련", "고장·통신 두절이 잦은 교대(로봇당 평균 1시간), 메시지 손실 2%",
             fault_mtbf_h=1.0, comms_mtbf_h=1.0, comms_loss=0.02),
    Scenario("night", "야간 입출고", "피킹 없음 · 입고 트럭 6대/h와 출고만, 사람 최소",
             amr=0, tote_orders_h=0.0, pickers=0, packers=0, walkers=0, dock_workers=2, trucks_h=6.0,
             out_pallets_h=30.0, rack_forklift=0),
    Scenario("grid", "격자 창고(알고리즘 검증)", "단일 차종 AMR 30대 · 일방통행 격자 · 사람 8명 · 지게차 2대",
             site="grid", robots=30, workers=4, pickers=2, walkers=2, forklifts=2, rate_per_h=120.0,
             fault_mtbf_h=8.0, comms_mtbf_h=6.0),
    Scenario("grid-night", "격자 창고 · 무인", "사람·지게차 없이 AMR 20대(교통 코어만 검증)",
             site="grid", robots=20, workers=0, pickers=0, walkers=0, forklifts=0, rate_per_h=120.0),
    Scenario("grid-deadlock", "교착 스트레스", "모든 레인 1차선 양방향 + AMR 36대, 시간당 240건. 정면 교착 유발(비교 기준선)",
             site="grid", robots=36, workers=0, pickers=0, walkers=2, forklifts=0, external=0, rate_per_h=240.0,
             two_way=True),
)}


def get(name: str, **overrides) -> Scenario:
    if name not in PRESETS:
        raise KeyError(f"unknown scenario {name!r}; choose from {', '.join(PRESETS)}")
    base = PRESETS[name]
    clean = {k: v for k, v in overrides.items() if v is not None}
    return replace(base, **clean) if clean else base

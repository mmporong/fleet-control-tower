"""Scenario presets.

``dc`` scenarios run the pallet distribution centre (autonomous forklifts, tote
AMRs, pickers, dock forklifts). ``grid`` scenarios run the algorithm test grid
with one generic AMR class; they isolate traffic control from the site process.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace


# design basis of the distribution centre (assumptions, see the Scenario field comments)
DESIGN_PEAK = {"trucks_h": 3.0, "out_pallets_h": 39.0, "tote_orders_h": 103.0}
DESIGN_AVG = {"trucks_h": 2.4, "out_pallets_h": 31.0, "tote_orders_h": 82.0}


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    site: str = "dc"
    # ---- dc fleet and process
    agf: int = 10
    amr: int = 16
    # demand: the site's design basis (docs/SITE.md section 5), fixed before any capacity was measured.
    # Inbound: 6 doors, one truck per door every 2 h (unloading, paperwork, swap) = 3.0 trucks/h at peak,
    # 2.4 in an average hour (10-16 pallets each). Outbound: inventory balance, the same pallets out.
    # Totes: 6 pickers x 100 units/h (lower end of robot-assisted cases, 98-200 UPH) / 5.83 units per order
    # = 103 orders/h at peak, 82 in an average hour. The fleet is sized to the peak (docs/BENCHMARK.md).
    trucks_h: float = DESIGN_AVG["trucks_h"]
    out_pallets_h: float = DESIGN_AVG["out_pallets_h"]
    tote_orders_h: float = DESIGN_AVG["tote_orders_h"]
    pickers: int = 6
    packers: int = 4
    dock_workers: int = 3
    walkers: int = 2
    dock_forklifts_in: int = 2
    dock_forklifts_out: int = 1
    rack_forklift: int = 1
    # ---- dc operating methods (docs/BENCHMARK.md compares the alternatives)
    pick_limit: str = "aisle"        # zone | aisle | vehicle: where pickers slow the tote AMRs
    tote_batch: int = 3              # orders per tote AMR trip (1 = one order per trip)
    dispatch: str = "cost"           # cost | fifo task assignment
    # ---- dc exceptions (assumptions, swept in the benchmark): pallet pocket not recognised per pick,
    # share of rack locations whose WMS record disagrees with what is there
    handling_fail_p: float = 0.01
    inventory_error_p: float = 0.01
    # ---- people and manual trucks breaking rules (assumptions, swept): share of a person's trips with no heed
    # of vehicles; share of blocked turns a truck driver makes anyway after waiting 10 s
    violation_p: float = 0.02
    careless_p: float = 0.1
    # ---- infrastructure sensing: "none" (vehicles' own sensors only), "partial" (docks, main aisles and the
    # crossing), "full" (every hall). Ceiling camera person detection: latency, position noise, detection and
    # wrong-class probability, false detections per camera and cycle (assumptions, swept)
    infra: str = "partial"
    cam_latency_s: float = 0.3
    cam_sigma: float = 0.25
    cam_detect_p: float = 0.95
    cam_misclass_p: float = 0.03
    cam_fp_p: float = 0.01
    # ---- grid fleet and process
    robots: int = 30
    workers: int = 4
    forklifts: int = 2
    rate_per_h: float = 120.0
    two_way: bool = False
    # ---- shared
    # third-party floor scrubber (read-only fleet): sites clean the main aisles off-peak, so it runs in the
    # night shift and the crowded drill, not in the day shifts
    external: int = 0
    fault_mtbf_h: float = 0.0        # per robot; 0 disables random faults
    comms_mtbf_h: float = 0.0        # per robot; 0 disables random outages
    comms_loss: float = 0.0          # random message loss probability per message
    door_mtbf_h: float = 0.0         # per door; 0 disables random door faults

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def fleet_size(self) -> int:
        return self.agf + self.amr if self.site == "dc" else self.robots


PRESETS: dict[str, Scenario] = {s.name: s for s in (
    Scenario("standard", "표준 교대", "물류센터 · 자율 지게차 10 · 토트 AMR 16 · 피커 6 · 수동 지게차 4 · 설계 평균 수요",
             fault_mtbf_h=24.0, comms_mtbf_h=6.0),
    Scenario("peak", "피크 시간", "자율 지게차 12 · 토트 AMR 22 · 피커 9 · 설계 피크 수요(입고 3대·출고 39팔레트·토트 103건/h)",
             agf=12, amr=22, pickers=9, **DESIGN_PEAK,
             fault_mtbf_h=24.0, comms_mtbf_h=6.0),
    Scenario("crowded", "사람 밀집", "피커 12 · 도크 작업자 6 · 보행자 6 · 타사 청소 로봇: 협업 피킹과 사람 구역 감속",
             pickers=12, dock_workers=6, walkers=6, packers=6, external=1),
    Scenario("faults", "장애 훈련", "고장·통신 두절이 잦은 교대(로봇당 평균 1시간), 메시지 손실 2%, 고속문 고장",
             fault_mtbf_h=1.0, comms_mtbf_h=1.0, comms_loss=0.02, door_mtbf_h=1.0),
    Scenario("night", "야간 입출고", "피킹 없음 · 입고 트럭 2.5대/h와 출고 15팔레트/h · 사람 최소 · 바닥 청소 로봇",
             amr=0, tote_orders_h=0.0, pickers=0, packers=0, walkers=0, dock_workers=2, trucks_h=2.5,
             out_pallets_h=15.0, rack_forklift=0, external=1),
    Scenario("grid", "격자 창고(알고리즘 검증)", "단일 차종 AMR 30대 · 일방통행 격자 · 사람 8명 · 지게차 2대",
             site="grid", robots=30, workers=4, pickers=2, walkers=2, forklifts=2, rate_per_h=120.0, external=1,
             fault_mtbf_h=8.0, comms_mtbf_h=6.0),
    Scenario("grid-night", "격자 창고 · 무인", "사람·지게차 없이 AMR 20대(교통 코어만 검증)",
             site="grid", robots=20, workers=0, pickers=0, walkers=0, forklifts=0, rate_per_h=120.0, external=1),
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

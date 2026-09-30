# Fleet Control Tower — 다중 AMR 관제 시스템

팔레트 물류센터에서 자율 지게차(AGF) 10대와 토트 AMR 16대를 VDA 5050 마스터 컨트롤로 운영하는 관제 시스템입니다. 수동 지게차·피커·보행자·타사 청소 로봇처럼 명령할 수 없는 유닛이 같은 공간을 씁니다. 교통 제어(예약·교착 해소), 사람·지게차 인지, 고속문 설비 연동, ISA-18.2 알람과 SOP, 사고 재생, 교대 인계를 한 화면에서 다룹니다.

현장 규모·차량 제원·운영 절차는 제조사 데이터시트, 표준, 채용 공고 53건을 조사한 결과([docs/research/](docs/research/))에 맞췄고, 성능 수치는 모두 시드를 고정한 벤치마크로 재현할 수 있습니다.

![관제 화면](docs/img/overview.png)

## 핵심 기능

| 영역 | 내용 |
|---|---|
| 현장 | 88 × 53.5 m 팔레트 물류센터: 입고 도크 6 → 팔레트 랙(4 m 통로, AGF 전용) → 방화벽 고속문 → 협업 피킹(토트 AMR + 피커) → 패킹 → 출고 도크 6. 일방통행 척추, 2차선 주통로, 차종별 통행 제한([SITE.md](docs/SITE.md)) |
| 차량 | VDA 5050 2.0 클라이언트. factsheet로 차종(FORKLIFT·CARRIER)과 지원 액션을 알림. 속도 비례 보호·경고 필드, 360° 근접 필드, 보호정지 2초 자동 재개, 고장 래치, 원격 조종(0.6초 데드맨) |
| 교통 제어 | 노드·차선 예약, 교차로·문턱은 통과까지 한 번에 예약, 회전 영역 예약, 교착 탐지(대기 관계 순환)와 재계획·비켜서기, 점유된 스텁 앞 대기 규칙, 혼잡 회피 경로 |
| 통제 불가 유닛 | 카메라 + 차량 센서 융합 추적, 사람·지게차 예측 점유로 예약 차단, 사각지대, 인프라 피드 두절 시 사람 구역 전체 감속. 타사 로봇은 공개 일정(ETA)으로 예약. 대치 시 우리 로봇이 비킴 |
| 설비 | 방화벽 고속문 2개. 미리 열림 요청, 열림 보고 뒤에만 예약, 걸림·무응답·고장 신호 판정, 알람과 즉시 우회, 사용 중지·재시도 |
| 이상 상황 | 알람 19종(ISA-18.2 수명주기·우선순위·보류·홍수·파생 알람 억제), 알람마다 자동 조치·절차·명령 버튼, 사고 전 30초·후 10초 재생, 근접사고(ISO 3691-4 기준) 자동 기록 |
| 운영 | 확인이 필요한 전체 명령, 비상정지 해제 후 자동 재개 금지, 감사 로그, 읽기 전용 모드, 교대 인계 자동 요약, 구역 봉쇄·대피·감속, 작업 재할당·우선순위, 가상 운영자(SOP 자동 대응, MTTA 측정) |
| 연동 | WMS 주문 REST(멱등 키), SSE 실시간 스트림, VDA 5050 메시지 검사기, 실물 JD-AMR 1대 상태 읽기(명령 없음), Docker 한 줄 기동 |

## 채용 요구와의 대응

로봇 관제 채용 공고 53건(한국 33, 해외 20)에서 뽑은 역량 순위([research/02 A-5](docs/research/02_jobs_and_incident_response.md))를 기준으로, 이 저장소에서 확인할 수 있는 곳을 정리했습니다.

| 순위 | 요구 역량 | 확인할 곳 |
|---|---|---|
| 1 | 이상 상황 처리 파이프라인(감지 → 자동 대응 → 확인·조치 → 현장 → 재개 → 기록·인계) | `fleet/alarms.py`, [RUNBOOK.md](docs/RUNBOOK.md), 화면 알람 대기열·사고 재생·교대 인계 |
| 2 | 교통 관리(충돌·교착 방지와 해소, 통제 불가 유닛) | `fleet/control.py`, [DESIGN.md §2–5](docs/DESIGN.md), 전략 비교 벤치 |
| 3 | 작업 할당과 확장성 | `fleet/dispatcher.py`, `fleet/process_dc.py`, 대수 확장 벤치 |
| 4 | 실시간 관제 UI(국내 관제장치 요건: 식별·위치·속력·배터리·통신 감도·원격 정지) | `web/`, 차량 목록·상세 |
| 5 | 표준 메시징·API | `fleet/vda5050.py`, `fleet/bus.py`, `fleet/server.py` |
| 6 | 상위 시스템·설비 연동과 실패 주입 | `POST /api/wms/orders`, `fleet/facility.py`, 문 고장 훈련 |
| 7 | 데이터·KPI | 상단 KPI 12개, 추세 탭, `fleet/bench.py` |
| 8 | 결함 주입 기반 검증 | 훈련 명령 4종, 테스트 88개(Python)·7개(node), [BENCHMARK.md](docs/BENCHMARK.md) |
| 9 | 배포·운영성 | `Dockerfile`(nobody 사용자, 헬스체크), 읽기 전용 모드 |
| 10 | 사고 조사와 문서화 | 사고 재생, 보고서·인계 양식, 비상 계획표([RUNBOOK.md](docs/RUNBOOK.md)) |

## 실행

Python 3.11 이상만 있으면 됩니다(외부 패키지 없음).

```bash
cd ~/fleet-control-tower
python3 -m fleet.server --scenario standard --speed 5
# 브라우저에서 http://127.0.0.1:8095
```

Docker로도 실행할 수 있습니다. 인증이 없으므로 호스트 루프백에만 엽니다.

```bash
cd ~/fleet-control-tower
docker build -t fleet-control-tower .
docker run --rm -p 127.0.0.1:8095:8095 fleet-control-tower
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--scenario` | `standard` | 아래 시나리오 중 하나 |
| `--strategy` | `locks+resolve` | `none`(관제 없음) · `locks`(예약만) · `locks+resolve` |
| `--speed` | `1` | 0.5 · 1 · 2 · 5 · 10배속(원격 조종 중에는 1배속 고정) |
| `--seed` | `7` | 같은 시드면 같은 결과 |
| `--auto-operator` | 꺼짐 | 가상 운영자가 SOP대로 알람을 확인·조치 |
| `--read-only` | 꺼짐 | 관찰 전용: 모든 명령 거부(감사 로그에는 남음) |
| `--port`, `--host` | `8095`, `127.0.0.1` | |
| `--no-jdamr`, `--jdamr-url` | 루프백 8090 | 실물 JD-AMR 상태 읽기 |

| 시나리오 | 내용 |
|---|---|
| `standard` | 표준 교대. AGF 10, 토트 AMR 16, 피커 6, 수동 지게차 4. 입고 약 29·출고 15팔레트/h(AGF 과부하 처리량의 약 70%), 토트 45건/h(토트 처리 한계 근처) |
| `peak` | AGF 12, 토트 AMR 22, 피커 9, 용량 근처 수요 |
| `crowded` | 피커 12, 도크 작업자 6, 보행자 6, 타사 청소 로봇 |
| `faults` | 로봇 고장·통신 두절 로봇당 평균 1시간, 메시지 손실 2%, 고속문 고장 |
| `night` | 피킹 없음, 입출고만, 바닥 청소 로봇 |
| `grid`, `grid-night`, `grid-deadlock` | 알고리즘 검증용 격자 창고(단일 차종). `grid-deadlock`은 1차선 양방향 스트레스 기준선 |

## 결과 요약

시드를 고정한 1시간 시뮬레이션의 평균 ± 표준편차입니다. 방법·표 전체·해석은 [docs/BENCHMARK.md](docs/BENCHMARK.md)에 있습니다.

| 질문 | 결과 |
|---|---|
| 표준 교대는 어떻게 도나(DC, 6시드) | 처리량 70 ± 21건/h, 교통 대기 비율 0.20, 교착 0, 근접사고 0, 이동 중 접촉 6시간에 1건, 알람 22건/h |
| 고장이 잦으면(로봇당 1시간에 1회, 메시지 손실 2%, 고속문 고장) | 처리량 약 30% 감소, 알람 약 110건/h. 점유-예약 불일치·접촉 0, MTTR 약 3분 |
| 교통 제어가 없으면 | DC 처리량 14건/h(안전 필드끼리 막힘). 교착이 계속 생기는 1차선 양방향 격자에서 예약만 41건/h, 교착 해소를 더하면 92건/h(시간당 약 1,800건 자동 해소) |
| 몇 대가 적정한가 | 포화 수요 격자는 20–30대에서 최대(약 275건/h), 40대부터 혼잡 붕괴. DC 토트 AMR은 피커 6명 기준 16–20대에서 최대 |
| 안전 준수의 비용 | 협업 피킹 존 사람 감속을 0.6 → 0.3 m/s(ISO 3691-4 조건)로 낮춰 근접사고 51 → 0건/h, 대신 토트 처리 한계 약 54 → 41건/h |
| 측정이 찾아낸 것 | 결함 23건(교착 반복, 사람·지게차 상호 대기, 경계 조건)과 코드 리뷰 결함 5건을 재현·수정·회귀 테스트로 고정. 남측 고속문 단일 장애점, 명령할 수 없는 유닛과의 밀집 상호 대기 같은 설계 약점을 기록 |

## 구조

```
fleet/
  site_dc.py      물류센터 레이아웃(노드·차선·차종 제한·구역·카메라·고속문)
  layout.py       그래프·구역·검증, 격자 창고
  vda5050.py      메시지 생성·검증(order/instantActions/state/factsheet)
  bus.py          프로세스 내 MQTT 모사(지연·손실·두절 주입)
  vehicle.py      VDA 5050 차량(주행·안전 필드·고장·충전·원격 조종)과 차종 제원
  control.py      관제 코어(예약·교착·우회·인지·점유 감시·알람 판정)
  geometry.py     거리·다각형·차체 간격 계산       scenario.py  시나리오 프리셋
  traffic.py      잠금 테이블·순환 탐지      planner.py   시간 기반 A*
  perception.py   추적·예측 점유              facility.py  고속문 장치와 감독
  alarms.py       ISA-18.2 알람과 SOP 정의    dispatcher.py 작업 배정·충전·주차
  process_dc.py   입고·적치·출고·토트 피킹 공정  process.py   공정 인터페이스와 격자 공정
  agents*.py      사람·지게차·타사 로봇
  ops.py          운영 명령·감사·WMS·교대 인계  autoop.py    가상 운영자
  sim.py          현장 시뮬레이션·평가기·사고 기록  server.py  HTTP·SSE 서버
  bench.py        벤치마크(멀티프로세스, 결과 JSON에 git rev 기록)   jdamr.py  실물 로봇 읽기
web/              관제 화면(바닐라 JS, 캔버스 지도)
tests/            단위·시스템·HTTP 테스트(Python 88, node 7)
docs/             설계·현장·운영·벤치마크 문서와 리서치 원문
results/          벤치마크 결과 JSON
```

## HTTP API

| 메서드·경로 | 설명 |
|---|---|
| `GET /api/stream` | SSE: 0.25초 주기 스냅숏(차량·알람·예약·추적·문·KPI) |
| `GET /api/snapshot`, `/api/layout`, `/api/meta`, `/api/history` | 현재 상태, 레이아웃(차선·구역·문·벽), 메타, 10초 추세 |
| `POST /api/command` | 운영 명령(`{"cmd": ..., "confirm": true}`). 헤더 `X-Operator`(퍼센트 인코딩)에 운영자 이름 |
| `POST /api/wms/orders` | WMS 주문. 새 주문 201, 같은 `order_id`·같은 내용 200, 다른 내용 409 |
| `GET /api/audit`, `/api/handover` | 감사 로그, 교대 인계 기록 |
| `GET /api/incident/{id}` | 사고 전후 재생 프레임 |
| `GET /api/vda5050/{serial}`, `/api/local/{serial}` | 차량별 최근 VDA 5050 메시지, 원격 조종용 로컬 뷰 |
| `POST /api/control` | 시뮬레이션 배속·일시정지·재시작·가상 운영자 |
| `GET /health` | 헬스체크 |

## 검증

```bash
cd ~/fleet-control-tower
python3 -m unittest discover -s tests      # Python 88개
node --test tests/test_logic.cjs            # 화면 계산 함수 7개
python3 -m fleet.bench --scenario standard --strategies none,locks,locks+resolve \
    --seeds 1,2,3 --duration 3600 --out results/dc_strategies.json
```

벤치 결과 JSON에는 설정 전체와 git 커밋(작업 트리가 바뀌어 있으면 `+dirty`)이 함께 기록됩니다.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | 구성, 충돌 관리 3계층, 교통 제어 결정, VDA 5050 매핑, 통제 불가 유닛, 설비 연동, 통신 두절, 알람 설계 |
| [docs/SITE.md](docs/SITE.md) | 현장 배치, 차량·현장 치수와 출처, 통제 불가 유닛, 모델 한계 |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | 교대 흐름, 알람 처리 원칙, 상황별 대응표, 비상 계획표, 사고 보고서·인계 양식, 훈련 |
| [docs/BENCHMARK.md](docs/BENCHMARK.md) | 재현 방법, 지표 정의, 결과, 측정으로 찾아 고친 결함 |
| [docs/research/](docs/research/) | 표준·교통 제어·안전(01), 채용 공고·이상 대처(02), 현장·차량 제원(03) 조사 원문 |

## 한계

- 현장은 시뮬레이션입니다. 실물 연동은 JD-AMR 1대의 상태 읽기뿐이고, 사람·지게차 행동은 규칙 기반 모델입니다.
- 메시지 버스는 프로세스 안의 모사입니다. MQTT 브로커, 데이터베이스 영속화, 인증·권한, 다중 관제석은 없습니다.
- AGF의 원호 선회는 같은 시간의 제자리 회전으로 근사합니다.
- 엘리베이터 연동과 VDA 5050 3.0(zone·map 배포)은 구현하지 않았습니다.
- 관제가 추정한 근접사고는 실제(평가기)보다 많이 잡힙니다. 추적 위치의 불확실성을 보수적으로 두었기 때문이며, 그만큼 사고 기록이 많이 쌓입니다.
- 남측 고속문이 고장 나면 출고 스테이징에 갈 수 없습니다. 고장 훈련으로 찾은 설계상의 단일 장애점이며 테스트로 고정해 두었습니다.
- 알람은 표준 교대에서 시간당 약 22건으로 ISA-18.2 목표(시간당 6–12건)보다 많습니다. 대부분 무작위 고장·통신 두절 알람입니다.
- 명령할 수 없는 유닛(보행자·수동 지게차·타사 로봇)이 차로에 끼어드는 경우의 접촉 위험이 드물게 남습니다(표준 6시간에 0–1건, 사람 밀집 6시간에 5건). 관제 예약으로는 막지 못하고 차량 안전 필드와 속도 제한에 기댑니다.

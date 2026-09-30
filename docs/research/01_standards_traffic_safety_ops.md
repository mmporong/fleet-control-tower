# 수십 대 AMR 교통 제어 + 사람·수동 차량 혼재 환경 관제 시스템 리서치

- 작성일: 2026-09-29
- 목적: "수십 대 AMR의 교통 제어"와 "명령을 내릴 수 없는 유닛(사람, 수동 지게차, 타사 로봇)이 섞인 환경"을 반영한 관제 시스템을 설계·구현하기 위한 근거 자료
- 범위 조정: 조정 지시에 따라 섹션 4에서 한국·해외 채용 공고 수집은 제외했다(별도 에이전트 담당). 섹션 4는 관제실 운영 규범(ISA-18.2/IEC 62682, ISO 11064, ISA-101, 상황 인식)과 KPI 정의에 집중한다.
- 표기 규칙
  - **[사실]**: 출처로 확인한 내용. 괄호 안 `[R번호]`는 문서 끝 출처 목록의 링크다.
  - **[추론]**: 확인한 사실에서 이끌어 낸 해석. 출처가 이 결론을 명시하지 않는다.
  - **[제안]**: 이 프로젝트 설계에 대한 권고.
  - 최신성 주장(표준 버전, 제품 기능)은 2026-09-29 기준으로 1차 출처(표준 원문·GitHub 릴리스·공식 페이지)를 우선 확인했고, 1차 출처에 접근하지 못한 항목은 2차 출처라고 표시했다.

---

## 0. 한눈에 보는 결론

1. **관제의 교통 권한은 "자원 예약 + 순차 릴리스"로 구현한다.** VDA 5050은 경로를 released(base)와 not released(horizon)로 나눠 마스터가 앞으로 갈 수 있는 구간만 풀어 준다. Open-RMF는 미래 궤적을 모아 두는 스케줄 DB와 충돌 협상, 체크포인트 의존성(traffic dependency)을 쓴다. 두 방식 모두 "어느 로봇이 어느 공간을 언제까지 쓸 권리가 있는가"를 중앙에서 관리한다. [사실, R30, R1, R7]
2. **VDA 5050 3.0.0(2026-03)은 자율 AMR용 zone 개념과 경로 공유를 표준에 넣었다.** BLOCKED, RELEASE(허가제), SPEED_LIMIT, DIRECTED 등 구역 유형과 GRANTED/QUEUED/REJECTED/REVOKED 요청-응답, lease 만료가 정의돼 있어 관제 명령 모델의 뼈대로 그대로 쓸 수 있다. [사실, R30, R31]
3. **수십 대 규모에서는 MAPF 최적해보다 실행 견고성이 병목이다.** RHCR·PIBT·LaCAM 계열은 수백~수천 대를 초 단위로 푼다. 실제 차량의 지연과 속도 편차는 ADG(action dependency graph)처럼 "통과 순서"만 지키게 하는 실행 계층으로 흡수한다. [사실, R76, R73, R74, R80]
4. **사람·수동 지게차는 "명령 불가·응답 없음(unresponsive)" 참가자로 모델링하고, 로봇이 양보한다.** Open-RMF read-only 어댑터가 정확히 이 방식이며(Rx::Unresponsive, vicinity 반경), 한 공간에 read-only 함대는 최대 하나만 허용한다고 명시한다. 명령할 수 없는 참가자끼리는 서로 비켜 줄 수 없기 때문이다. [사실, R2, R8, R11]
5. **안전은 관제 소프트웨어가 아니라 차량 탑재 안전 기능이 책임진다.** ISO 3691-4는 인원 감지와 제동을 PLr d로 요구하고, VDA 5050은 스스로 "안전 표준이 아니다"라고 밝힌다. 관제는 감속 구역·진입 금지·구역 봉쇄로 위험 노출을 줄이는 운영 계층이다. [사실, R90, R91, R30]
6. **관제 화면은 예외 기반으로 만든다.** ISA-18.2의 알람 정의("응답이 필요한 것")와 목표치(운영자당 10분에 1~2건, 우선순위 80/15/5%), ISA-101의 회색 기본 화면, 4단계 화면 계층을 로봇 관제에 옮기면 수십 대를 한 화면에서 다룰 수 있다. [사실, R120, R121, R125]

---

## 1. 표준·오픈소스 관제 아키텍처

### 1.1 Open-RMF

#### 1.1.1 전체 구성

**[사실]** Open-RMF는 여러 제조사 함대를 한 시설에서 운영하기 위한 오픈소스 프레임워크다. 핵심 구성은 다음과 같다. [R1, R2, R15]

| 구성 요소 | 역할 | 확인 출처 |
|---|---|---|
| Traffic Schedule Database | 모든 참가자의 "의도된 미래 궤적"을 모은 중앙 DB. 지연·취소·경로 변경을 반영하며 계속 바뀐다. 현재 위치가 아니라 미래를 본다. | R1, R2 |
| Negotiation | 스케줄에서 향후 충돌을 찾으면 관련 함대에 conflict notice를 보내고, 각 함대가 제안·수용안을 내면 시스템 통합자가 배치한 judge가 선택한다. | R1, R2 |
| Fleet Adapter | 제조사 API와 RMF 핵심(스케줄·협상)을 잇는다. 문·엘리베이터·디스펜서 같은 인프라와의 통신도 담당한다. | R2 |
| Task Dispatcher | 작업 요청이 오면 BidNotice를 모든 어댑터에 보내고, 어댑터가 비용을 담은 BidProposal로 응답하면 평가해 낙찰한다. | R17 |
| rmf-web | api-server + 대시보드 프레임워크. 지도, 로봇, 작업, 문·엘리베이터, 알림을 보여 준다. | R13, R14 |

**[사실]** 현재 공식 지원 배포판은 ROS 2 Humble, Jazzy, Kilted, Rolling이다. GitHub의 마지막 태그 릴리스는 "Open-RMF on Jazzy Jalisco"(2024-06-17)이고, 이 릴리스에 mutex group 수동 해제, 이벤트 기반 문·엘리베이터 로직, 속도 제한 요청이 포함됐다. rmf-web main 브랜치는 Ubuntu 26.04 + ROS 2 Lyrical을 기준으로 개발 중이다. [R15, R16, R13]

#### 1.1.2 교통 스케줄의 실제 데이터 구조

**[사실]** `rmf_traffic_msgs` 메시지 정의에서 확인한 구조다. [R3]

```
Itinerary      = Route[] routes
Route          = { string map; Trajectory trajectory; uint64[] checkpoints; TrafficDependency[] dependencies }
Trajectory     = TrajectoryWaypoint[] waypoints   # 웨이포인트 사이를 piecewise cubic spline으로 해석
TrajectoryWaypoint = { int64 time; float64[3] position (x,y,yaw); float64[3] velocity }
ParticipantDescription = { string name; string owner;
                           uint8 responsiveness (RX_INVALID=0, RX_UNRESPONSIVE=1, RX_RESPONSIVE=2);
                           Profile profile }
Profile        = { ConvexShape footprint; ConvexShape vicinity; ConvexShapeContext shape_context }
TrafficDependency = { uint64 dependent_checkpoint; uint64 on_participant; uint64 on_plan;
                      uint64 on_route; uint64 on_checkpoint }
ItinerarySet   = { participant; plan; Route[] itinerary; storage_base; itinerary_version }
ItineraryDelay = { participant; int64 delay; itinerary_version }
```

- 스케줄 변경은 ItinerarySet / Extend / Delay / Erase / Clear / Reached 메시지로 버전(`itinerary_version`)과 함께 전달된다. 조회는 ScheduleQuery·ScheduleQuerySpacetime, 복제는 MirrorUpdate·SchedulePatch 계열로 한다. [R3]
- `footprint`는 차체, `vicinity`는 다른 참가자가 들어오면 안 되는 주변 영역이다. [R3]
- `responsiveness`가 **RX_UNRESPONSIVE**인 참가자는 충돌 협상에 응하지 않는다. read-only 어댑터는 로봇을 이 값으로 등록한다(`FleetAdapterNode.cpp` 101행 `ParticipantDescription::Rx::Unresponsive`). [R3, R11]

#### 1.1.3 충돌 협상(negotiation)의 메시지

**[사실]** [R3, R6]

```
NegotiationNotice     = { uint64 conflict_version; uint64[] participants }
NegotiationProposal   = { conflict_version; proposal_version; for_participant;
                          NegotiationKey[] to_accommodate; plan_id; Route[] itinerary }
NegotiationRejection  = { conflict_version; NegotiationKey[] table; rejected_by; Itinerary[] alternatives }
NegotiationForfeit    = { conflict_version; NegotiationKey[] table }
NegotiationConclusion = { conflict_version; bool resolved; NegotiationKey[] table }
```

- `to_accommodate` 배열의 순서가 의미를 가진다. `[]`는 "나에게 최선인 경로", `[3]`은 "참가자 3의 이상 경로를 수용하는 나의 최선 경로", `[3, 7]`은 "3의 이상 경로와, 그것을 수용한 7의 최선 경로를 모두 수용하는 경로"다. 즉 협상은 참가자 순열마다 가지를 치는 트리(negotiation table)다. [R6]
- 긴급 작업은 일부러 스케줄에 충돌을 올려 협상을 강제하고, judge가 항상 고우선순위 참가자를 고르도록 구현하는 방식을 문서가 제시한다. [R2]

**[사실]** 2022년에 **traffic dependency 시스템**이 추가됐다. 시간 예측이 어긋나면 협상이 제때 촉발되지 않아 충돌 위험이 남는 문제가 있었고, 참가자가 "상대의 몇 번 체크포인트 통과를 기다린다"는 의존성을 명시하게 해 협상 빈도와 계획 비용을 줄이는 것이 목적이다. [R7]
**[추론]** 이 구조는 2.6절의 ADG(통과 순서 의존 그래프)와 같은 발상이다. 시간표 대신 순서를 지키게 해 지연에 강해진다.

#### 1.1.4 Fleet adapter 유형

**[사실]** 문서의 제어 유형 표를 요약했다. [R2, R8, R9, R10]

| 유형 | RMF가 할 수 있는 것 | 로봇/함대에 필요한 것 | 특이점 |
|---|---|---|---|
| Full Control | 경로를 지정하고 언제든 새 경로로 교체, 실시간 위치 수신 | 위치 읽기, 좌표 이동 명령, 일시정지/재개, 경로·ETA·배터리, 도킹, 맵 전환, 프로세스 시작·중지 | 효율이 가장 높다. EasyFullControl API로 콜백만 구현하면 된다. |
| Traffic Light | 로봇별 일시정지/재개만 가능 | 위치, 일시정지/재개, 배터리, 프로세스 제어 | TrafficLight / EasyTrafficLight API. 문·엘리베이터 연동은 이 유형에서 지원하지 않으며 벤더가 해결해야 한다(2025-09 Open Robotics 포럼 답변). 구간 예약은 Blockade 메시지(`BlockadeSet{participant, reservation, radius, path: BlockadeCheckpoint{position, map_name, can_hold}}`)를 쓴다. |
| Read Only | 제어 불가, 위치·의도 경로만 수신 | 위치, 목적지까지 경로(추론 가능하면 됨), 평균 속도/ETA, 배터리 | "한 공유 공간에 Read Only 함대는 최대 하나만 허용, 없는 것이 이상적". 둘 이상이면 교착·자원 충돌 회피가 거의 불가능하다고 명시. |

**[사실] Read-only 통합 방식** [R8]
- Fleet Driver가 `rmf_fleet_msgs/FleetState`를 `fleet_states` 토픽에 발행한다. `RobotState`의 핵심 필드는 `name`, `location`, `path`다.
- `Location.t`(시간)는 read-only 어댑터가 대체로 무시한다. Fleet Driver에게 시간 예측을 맡기지 않고, 어댑터가 설정된 운동학 한계(`linear_velocity`, `angular_velocity`, 가속도)로 도착 시각을 추정한다. 문서는 스케줄링 효과를 위해 운동학 값을 **과대 설정**하라고 권한다.
- `footprint_radius`(차체)와 `vicinity_radius`(다른 로봇 진입 금지 반경)를 설정한다.

#### 1.1.5 함대 운영 메시지(관제 개입에 쓰는 것)

**[사실]** `rmf_fleet_msgs` 정의에서 확인했다. [R4]

| 메시지 | 필드·의미 | 관제 기능 대응 |
|---|---|---|
| `RobotState` | name, model, task_id, seq, mode, battery_percent, location, path | 로봇 목록·지도 표시 |
| `RobotMode` | IDLE, CHARGING, MOVING, PAUSED, WAITING, EMERGENCY, GOING_HOME, DOCKING, ADAPTER_ERROR, CLEANING, PERFORMING_ACTION | 상태 색상·필터 |
| `PauseRequest` | TYPE_PAUSE_IMMEDIATELY, TYPE_PAUSE_AT_CHECKPOINT(+at_checkpoint), TYPE_RESUME | 단일 로봇 일시정지. "체크포인트에서 정지"는 교차로 한가운데 멈추는 문제를 피한다. |
| `LaneRequest` / `ClosedLanes` | open_lanes[], close_lanes[] (lane index) | 구역·차선 봉쇄 |
| `SpeedLimitRequest` | SpeedLimitedLane{lane_index, speed_limit}[], remove_limits[] | 감속 구역 |
| `EmergencySignal` | fleet_names[] (비면 전체), is_emergency | 전체 비상 동작 |
| `MutexGroupRequest` | group, claimant, claim_time, MODE_LOCK/RELEASE. 점유 중 주기적으로 재전송(heartbeat)해야 하며 lease가 만료된다. **먼저 요청한 claim이 우선**. | 교차로·좁은 통로 락 |
| `MutexGroupManualRelease` | 수동 해제 | 락이 꼬였을 때 운영자 개입 |
| `InterruptRequest` | TYPE_INTERRUPT / TYPE_RESUME, labels | 작업 중단·재개 |
| `DeliveryAlert` | category, tier, task_id, action, message | 운영자 확인이 필요한 작업 알림 |

#### 1.1.6 문·엘리베이터·디스펜서 연동

**[사실]** [R5]
- 문: `DoorRequest{request_time, requester_id, door_name, requested_mode}`, `DoorState{door_time, door_name, current_mode}`, `DoorMode ∈ {CLOSED, MOVING, OPEN, OFFLINE, UNKNOWN}`.
- 엘리베이터: `LiftRequest{lift_name, request_time, session_id, request_type ∈ {END_SESSION, AGV_MODE, HUMAN_MODE}, destination_floor, door_state}`. AGV 모드는 정지 시 문을 계속 열어 두고, HUMAN 모드는 문 열림을 명시적으로 요청해야 한다. `LiftState`에는 `current_mode ∈ {UNKNOWN, HUMAN, AGV, FIRE, OFFLINE, EMERGENCY}`와 제어권을 가진 `session_id`가 있다. FIRE·EMERGENCY는 읽기 전용 모드다.
- 디스펜서(워크셀): `DispenserRequest{request_guid, target_guid, transporter_type, items}`, `DispenserResult{status ∈ {ACKNOWLEDGED, SUCCESS, FAILED}}`, `DispenserState{mode ∈ {IDLE, BUSY, OFFLINE}, request_guid_queue, seconds_remaining}`.

**[추론]** 엘리베이터는 사람과 로봇이 함께 쓰는 대표 자원이다. `session_id` 단위 독점 + 화재·비상 모드 읽기 전용이라는 설계는 "사람 우선 자원"을 관제에 넣을 때 그대로 참고할 수 있다.

#### 1.1.7 장애물·사람 감지 → 차선 봉쇄

**[사실]** `rmf_obstacle_msgs/Obstacle`은 `{header, id, source, level_name, classification("human" 등), bbox(BoundingBox3D), octree data, lifetime, action ∈ {ADD, DELETE, DELETEALL}}`다. [R5]
**[사실]** `rmf_obstacle_detectors` 저장소에는 다음이 있다. [R12]
- `rmf_human_detector`: 기존 CCTV 같은 단안 카메라 영상에서 YOLOv4로 사람을 찾고 휴리스틱으로 위치를 추정한다(용도: 시설 CCTV로 군중 감지).
- `rmf_human_detector_oakd`: OAK-D 온칩 추론.
- `rmf_obstacle_detector_laserscan`: 시작 시 몇 프레임으로 "빈 상태"를 보정한 뒤 변화를 장애물로 판단.
- `rmf_obstacle_ros2/lane_blocker_node`: `/rmf_obstacles`를 구독해 장애물이 차선과 겹치면 해당 함대의 차선을 닫고, 사라지면 다시 연다. 기본값은 `lane_closure_threshold=1`(장애물 1개면 폐쇄), `speed_limit_threshold=3`(3개 이상이면 감속), `speed_limit=0.5 m/s`, `obstacle_lane_threshold=0.25 m`, `lane_width=0.5 m`, `process_rate=1 Hz`.

**[추론]** "사람 감지 → 차선 폐쇄 또는 감속"이라는 관제 규칙이 오픈소스 참조 구현으로 존재한다. 이 프로젝트의 MVP 규칙 엔진 설계에 바로 차용할 수 있다. 다만 1 Hz 처리와 카메라 추정 위치는 안전 기능 수준이 아니다(3.1절).

#### 1.1.8 rmf-web 대시보드

**[사실]** [R13, R14]
- 패키지: `api-server`, `api-client`, `rmf-dashboard-framework`(React/TypeScript 컴포넌트), `ros-translator`, `rmf-models`, `dashboard-e2e`.
- 흐름: fleet adapter가 api-server의 `/_internal` 엔드포인트로 상태를 보내고, 대시보드는 REST(`:8000`)와 궤적 서버 WebSocket(`:8006`)을 쓴다.
- 저장소: tortoise-orm 기반으로 PostgreSQL, SQLite, MySQL, MariaDB 지원(기본은 메모리 SQLite).
- 인증: 자체 계정 관리 없이 OpenID Connect 공급자(Keycloak, Auth0 등)의 JWT를 검증하고, 권한 판단은 앱 안에서 한다.
- 화면: 지도, 로봇 추적, 작업 생성·모니터링, 문·엘리베이터·비콘 제어, 시스템 알림.

#### 1.1.9 한계와 차세대 방향(2025~2026 메인테이너 발언)

**[사실]**
- 2026-09-10 메인테이너(grey)는 좁은 양방향 통로가 많은 반도체 팹형 환경 질문에 대해, 기존 협상 시스템이 **혼잡으로 생기는 지연을 과소평가**해 어려움을 겪는다고 인정했다. 당장의 우회책으로 `rmf_traffic_editor`에서 **일방통행 차선**을 만들거나 **동적 차선 폐쇄**를 쓰라고 권했다. [R22]
- 2026-03 공항 규모(10~50대 이상) 질문에는 "Open-RMF는 연속 모션 제어가 아니라 비침습적 조정 계층"이며, 넓은 공간은 로봇 자체 내비게이션에 맡기고 RMF는 작업 조율과 병목(choke point) 관리에 쓰라고 답했다. 예측 정확도는 크게 중요하지 않고 교통 시스템이 지연에 계속 적응한다고 했다. [R21]
- 차세대 설계(2025-06 제안)는 `~/destination`, `~/plan` 같은 모듈형 인터페이스, 교통 인지 costmap(safe zone), `~/delay` 토픽, VDA 5050·MassRobotics 브리지를 목표로 한다. [R19] GSoC 2026 프로토타입은 Destination Server, Path Server, Plan Executor로 나누고, 같은 목적지에 여러 로봇이 몰리면 주차 구역으로 돌려 대기시키는 예약 로직과 로봇 센서의 free-space 관측 공유를 구현했다. [R20]
- 로드맵에는 전역 최적 예약 시스템(주차·엘리베이터·워크셀), 대기열 시스템, 병목으로 영역을 나누는 계층적 계획, Zenoh 전송이 있다. [R18]
- 별도로 Rust 기반 실험 저장소 `open-rmf/mapf`가 있다. 협력 경로 탐색에 초점을 둔 다중 에이전트 계획 라이브러리이며 README는 "현재 실험 단계"라고 밝힌다. [R23]

### 1.2 VDA 5050

#### 1.2.1 버전(최신성 확인)

**[사실]** GitHub 공식 저장소 릴리스 기준이다. [R31, R32]

| 버전 | 공개일 | 핵심 |
|---|---|---|
| 2.0.0 | GitHub 2021-11-25 (VDA 문서 2020-01) | order/state/instantActions 기본 구조 |
| 2.1.0 | 2024-08-19 | corridor(장애물 회피 허용 폭), 맵 배포, 에러 힌트, factsheet 차량 구성 |
| **3.0.0** | GitHub 2026-03-18, VDA 공개 2026-03-19, 보도자료 2026-04 | 자율 주행 로봇 지원(planned/intermediate path 공유, zone 개념), 운전 모드 확장(STARTUP, INTERVENED), 에러 레벨 URGENT·CRITICAL 추가, 타임스탬프 1/1000 s, `positionInitialized`→`localized`, 액션 상태 RETRIABLE, startPause·stopPause·cancelOrder 필수화 |

- 3.0.0 문서는 용어를 "AGV"에서 "mobile robot", "master control"에서 "fleet control"로 바꿨다. MQTT 토픽 예시는 `vda5050/v3/KIT/0001/order`다. [R30]

#### 1.2.2 통신 구조

**[사실]** [R30]
- 전송: MQTT 3.1.1 이상 + JSON. `connection`만 QoS 1, 나머지(order, instantActions, state, factsheet, zoneSet, responses, visualization)는 QoS 0.
- 토픽 계층: `interfaceName/majorVersion/manufacturer/serialNumber/topic`.

| 토픽 | 발행 → 구독 | 용도 | 필수 |
|---|---|---|---|
| order | fleet control → robot | 주행·작업 명령(노드-엣지 그래프 조각) | 필수 |
| instantActions | fleet control → robot | 즉시 실행 액션(pause, cancelOrder 등) | 필수 |
| state | robot → fleet control | 단일 상태 메시지(이벤트 발생 시 + 최소 30초마다) | 필수 |
| visualization | robot → 시각화 시스템 | 고빈도 위치·계획 경로 | 선택 |
| connection | broker/robot → fleet control | 연결 끊김 감지(MQTT last will, retained) | 필수 |
| factsheet | robot → fleet control | 제원·지원 기능(retained) | 필수 |
| zoneSet | fleet control → robot | 구역 집합 전송 | 선택 |
| responses | fleet control → robot | robot 요청(zone·corridor)에 대한 응답 | 선택 |

- connection: 접속 시 last will을 `connectionState=CONNECTION_BROKEN`으로 설정하고 `ONLINE`을 보낸다. 정상 종료 시 `OFFLINE`. fleet control은 이 토픽을 로봇 건강 확인용으로 쓰지 말라고 명시한다(MQTT 수준 연결 확인용). [R30]
- 경로망 정의는 표준 밖이며, VDMA의 LIF(Layout Interchange Format)로 가져오거나 수동 입력한다. 일방통행, 차종별 제한 등은 fleet control이 들고 있고 로봇에는 전달하지 않는다. fleet control은 해당 로봇이 지나갈 수 있는 엣지만 order에 넣는다. [R30]

#### 1.2.3 base/horizon: 교통 제어가 마스터에 있다는 설계

**[사실]** [R30]
- order는 `nodes[]`, `edges[]` 두 리스트다. `sequenceId`는 노드·엣지가 공유하며 노드 0, 엣지 1, 노드 2 … 순서다.
- 모든 노드·엣지에 `released` 불리언이 있다. **released 집합이 base, 미릴리스 집합이 horizon**이다. base의 마지막 노드가 **decision point**이며, base가 더 늘지 않으면 로봇은 여기서 선다.
- 엣지는 양 끝 노드가 모두 released일 때만 released가 될 수 있고, 미릴리스 엣지 뒤에 released 요소가 올 수 없다.
- **base는 바꿀 수 없다.** MQTT가 비동기이고 무선이 불안정하므로 fleet control은 base가 이미 실행됐다고 가정해야 한다. horizon은 order update(`orderUpdateId` 증가)로 바꾸거나 지울 수 있다.
- order update의 첫 노드는 이전 base의 마지막 노드(stitching node)여야 한다. 업데이트가 유실돼도 로봇이 연속성을 검증할 수 있다.
- 로봇은 base가 부족해질 것 같으면 state의 `newBaseRequest=true`로 감속 전에 확장을 요청할 수 있다.
- 표준 5.3절은 fleet control의 최소 기능으로 작업 할당, 경로 계산, **교착 탐지·해소**, 에너지 관리, **교통 제어(버퍼 경로·대기 위치)**, 일시적 환경 변경(구역 해제, 최고 속도 변경), 문·게이트·엘리베이터 통신, 통신 오류 대응을 든다.
- 동시에 2장 범위에서 **교통 관리 로직(라우팅, 우선순위, 혼잡, 교착 해소 알고리즘)은 다루지 않으며, 안전 요구사항도 다루지 않고 안전 표준으로 쓰면 안 된다**고 밝힌다.

**[추론]** VDA 5050은 "교통 권한은 마스터가 가진다"는 원칙만 정하고 알고리즘은 구현자에게 맡긴다. base 릴리스는 곧 **자원 예약의 전달 수단**이다. 관제의 예약 관리자는 "어떤 노드·엣지를 누구에게 언제 release할지"를 결정하고, 그 결과를 order update로 내보내는 구조가 된다.

#### 1.2.4 state 메시지와 운영 모드

**[사실]** [R30]
- 전송 조건: order 수신, `errors`/`operatingMode`/`driving`/`paused`/`safetyState`/`newBaseRequest`/`lastNodeId`/`zoneRequests`/`edgeRequests`/`nodeStates`/`edgeStates`/액션 상태 변화 시, 그리고 최소 30초마다.
- 에러 레벨(3.0):
  - WARNING: 즉시 조치 불필요, 현재 order 계속, 새 order 가능(예: 라이다 오염).
  - URGENT: 즉시 주의 필요, 계속 주행 가능(예: 배터리 부족).
  - CRITICAL: 즉시 주의, 현재 order는 계속 못 하나 새 order는 받을 수 있음(예: 집을 물체가 없음).
  - FATAL: 사용자 개입 필요, 주행·새 order 모두 불가(예: 위치 추정 상실).
  - 어떤 레벨이든 로봇은 에러 때문에 order를 스스로 지우지 않는다.
- `safetyState = { activeEmergencyStop ∈ {MANUAL, REMOTE, NONE}, fieldViolation: bool }`. fieldViolation은 레이저·범퍼 보호 필드 침범이다.
- 운영 모드:

| 모드 | fleet control 제어권 | 진입 시 order 삭제 | 허용 명령 |
|---|---|---|---|
| AUTOMATIC | 예 | 아니오 | order, instantActions |
| SEMIAUTOMATIC | 예(속도는 HMI) | 아니오 | order, instantActions |
| **INTERVENED** (3.0 신설) | 아니오(HMI로 조향·속도 조작) | 아니오, zone 요청은 삭제 | order 가능(복귀 후 실행), instantAction은 cancelOrder만 |
| MANUAL | 아니오 | 예 | 없음 |
| STARTUP (3.0 신설) | 아니오 | 예 | 없음 |
| SERVICE / TEACH_IN | 아니오 | 예 | 없음 |

**[추론]** INTERVENED는 "사람이 잠깐 넘겨받아 빼내고 다시 자동으로 돌리는" 원격·현장 개입을 order를 잃지 않고 표현한다. 관제의 "개입 횟수" KPI를 이 모드 전이로 셀 수 있다.

#### 1.2.5 3.0의 zone 개념

**[사실]** [R30]
- zone set은 fleet control만 변경·배포한다. `zoneSetId`가 같으면 내용이 바뀌면 안 되고(불변), 바꾸려면 새 ID를 쓴다. 새 zone set은 DISABLED로 들어가며 `enableZoneSet` instant action으로 켠다. 로봇은 factsheet의 `supportedZones`로 지원 유형을 알린다.
- 윤곽(contour) 기준 zone(차체+적재물 윤곽이 닿으면 진입):

| 유형 | 동작 |
|---|---|
| BLOCKED | 진입 금지. 안에 있으면 정지하고 `BLOCKED_ZONE_VIOLATION`(CRITICAL) |
| LINE_GUIDED | 자유 주행 금지, 사전 정의 궤적만 |
| RELEASE | fleet control 허가 후 진입. `releaseLossBehavior ∈ {STOP, CONTINUE, EVACUATE}` |
| COORDINATED_REPLANNING | 구역 안 자율 재계획 금지, 허가된 경로만 |
| SPEED_LIMIT | `maximumSpeed`(m/s), 진입 시점에 이미 제한 속도여야 함 |
| ACTION | 진입·통과·이탈 시 액션 실행 |

- 운동학 중심(kinematic center) 기준 zone: PRIORITY(`priorityFactor` 0~1, 경로 유인), PENALTY(`penaltyFactor` 0~1, 1이면 다른 길이 없을 때만), DIRECTED(선호 방향, SOFT/RESTRICTED/STRICT), BIDIRECTED(방향과 역방향만).
- 겹침 규칙: BLOCKED가 모두를 이기고, SPEED_LIMIT이 겹치면 가장 낮은 값, DIRECTED·BIDIRECTED끼리는 겹치면 안 된다.
- 요청-응답: 로봇이 state의 `zoneRequests`에 `REQUESTED`로 올리면 fleet control이 `responses` 토픽으로 `GRANTED / QUEUED / REJECTED / REVOKED`와 선택적 `leaseExpiry`를 보낸다. **응답이 제때 오지 않으면 로봇은 진입하지 않는다.** lease가 지나면 `EXPIRED`가 되고, 이미 안에 있으면 `releaseLossBehavior`대로 행동한다.
- 경로 공유: 자유 주행 로봇은 `plannedPath`(NURBS, 최소 현재 base를 덮음)와 `intermediatePath`(ETA가 붙은 폴리라인, 매 state/visualization마다 갱신)를 보낸다.
- 노드 도달이 불가능하면 `NODE_UNREACHABLE`(CRITICAL)을 보내고 재시도하지 않고 지시를 기다린다.

**[추론]** RELEASE zone + lease는 교차로·좁은 통로 락, 사람 작업 구역 보호, 수동 지게차 횡단 구역을 표준 메시지로 표현하는 수단이다. SPEED_LIMIT·BLOCKED zone은 "사람 감지 시 감속·진입 금지" 규칙의 출력 형식으로 쓸 수 있다.

### 1.3 MassRobotics 상호운용 표준과 ISO 동향

**[사실] MassRobotics AMR Interoperability Standard** [R40, R41]
- v1.0은 2021-05 공개. GitHub 스키마의 최종 커밋은 2021-10이다.
- 목적은 **명령이 아니라 상태 공유**다. 서로 다른 제조사 로봇이 위치·속도·방향·건강·작업 가용성을 공유하고, **사람도 휴대 기기로 같은 정보를 제공**해 로봇과 함께 조율되게 한다.
- 스키마는 `identityReport`(제조사, 모델, 시리얼, 최대 속도, 적재 용량, 충전기 유형, 연락처 등)와 `statusReport`(location, velocity, batteryPercentage, remainingRunTime, `operationalState`, path, destinations, errorCodes, loadPercentageStillAvailable, `predictedLocation{timestamp, x, y, angle}`)로 나뉜다.
- `operationalState` 열거값: navigating, idle, disabled, offline, charging, **waitingHumanEvent**, waitingExternalEvent, waitingInternalEvent, **manualOverride**.
- 2022년 v2.0 작업(경로 쏠림을 막는 미션 통신 API)이 발표됐다(2차 출처).

**[사실] ISO 21423 (Robotics — Industrial mobile robots — Communications and interoperability)** [R42, R43, R44, R45, R46, R47]
- ISO/TC 299 WG 10에서 약 90명이 개발. VDA 5050과 MassRobotics를 기반으로 MQTT·JSON을 쓴다.
- ISO/DIS 21423은 2025-07, FDIS 투표는 2026-05-25~07-20, **2026-07-21부터 단계 60.00(발행 준비 중)**이다(ISO 프로젝트 추적 사이트 기준). 따라서 2026-09-29 현재 정식 발행 여부는 ISO 공식 페이지 확인이 필요하다(ISO 페이지는 접근 차단으로 미확인).
- 2차 출처 설명: 중앙 집중형(단일 FMS)과 분산형(P2P) 구조를 모두 지원하고, 벤더마다 다른 지도 원점을 맞추는 공통 좌표계(Common Coordinate System)를 다룬다.
- InOrbit은 2026-09-15 Apache 2.0 오픈소스 OpenRobOps를 공개하며 ISO 21423의 첫 참조 구현이라고 발표했다. Automate 2026에서 10개 회사가 이 표준 기반 멀티벤더 연동을 시연했다(벤더 발표).

**[추론]** 이 프로젝트에서 "타사 로봇"은 명령 가능 여부에 따라 VDA 5050(명령 가능)과 MassRobotics/ISO 21423식 상태 공유(관찰만 가능)로 나뉜다. 관찰 전용 입력 포맷을 MassRobotics `statusReport`에 맞춰 두면 사람(휴대 기기)·수동 지게차 태그도 같은 스키마로 받을 수 있다.

### 1.4 상용 FMS와 운영 플랫폼이 보여 주는 것

아래는 공식 페이지·보도자료 기준이며 대부분 벤더 주장이다.

| 제품 | 지도·교통 | 미션·작업 | 알람·사고 | KPI·분석 | 원격 개입·기타 | 출처 |
|---|---|---|---|---|---|---|
| MiR Fleet / Fleet Enterprise | 최대 100대(Fleet), Enterprise는 100대 이상. 엘리베이터 연동 다층 주행 | 위치·가용성 기반 자동 배정, ERP/WMS/MES REST 연동 | 장애물·경로 차단 알림 | - | SSO, 감사 로그(audit logging), IEC 62443-4-2 SL-C 3 (2024-10) | R50, R51 |
| OTTO Fleet Manager | AMR 간 정보 교환으로 교차로 접근을 예측해 막힘 방지, 속도 제한·진입 금지 구역 규칙 | 배터리·유휴 시간 기반 자동 배정, 기회 충전 | - | 대형 TV용 Fleet Dashboard, 느리거나 실패 잦은 작업·장소·로봇 분석 | 로봇이 "무엇을 보고 생각했는지"를 영상 형태로 재생하는 로그 플레이백(2022-06). VDA 5050, OPC-UA | R52, R53 |
| Locus (LocusONE/LocusHub) | - | 주문 풀 추적, 작업 흐름 라우팅 | - | UPH, 시간당 피킹, 로봇·작업자 생산성, 20종 이상 보고서(LocusHub 2024-03) | WMS 연동 | R54 |
| Geek+ RMS | MAPF로 처리량 15% 이상 향상 주장, 5,000대 이상·초당 10,000 작업(2024-11). 2026-02 업그레이드는 전방향 이동·동적 경로 | 수요 기반 동적 배차, 워크스테이션 도착 예측 | 접근 제어, 명령 검증, **명령 감사(command auditing)** | 3D 모니터링(LOD) | 원격으로 로봇·컨테이너·설비 실시간 상태 확인 | R55, R56 |
| Seegrid Fleet Central | 교차로·혼잡 관리, VDA 5050로 타사 장비 통합 | 작업 시퀀스 생성, 자동 충전 | - | 비효율 식별 분석 | PLC/WMS/ERP/MES 연동 | R57 |
| BlueBotics ANT server / monitor / locator | 교차로·문·엘리베이터 교통 관리, VDA 5050 차량 관리, 레거시 AGV는 인터록으로 교통 관리 | 스케줄러가 차량 선택, 충전 관리 | ANT monitor: 실시간 함대 개요, 차량 상태, 미션, 알람, 통계 | - | **ANT locator(2025-06 출시): 수동 지게차의 실시간 좌표를 ANT server에 통합하고, 신호등 시스템과 사전 정의 구역으로 자동 차량·수동 트럭·사람의 상호작용을 조율** | R58, R59 |
| Formant | 지도 뷰, 리스트 뷰 | - | 임계치 기반 이벤트, 인앱·Slack·SMS·PagerDuty·웹훅, 이벤트로 로봇 명령·설정 변경 자동 실행 | 가동 시간 보고 | 개입 요청(선택형·라벨링형), 원격 조종 | R60 |
| InOrbit | 다중 지도 | 미션 추적(진행·실패, 도움 필요 상황 식별) | 사고(incident) 관리, 조건별 필터 | KPI 대시보드, 로봇 단위 드릴다운 | "몇 초만" 넘겨받아 궤도로 돌려놓는 다단계 원격 조종, 한 번 클릭 원격 조치 | R61 |
| Foxglove | - | - | - | - | 기록 데이터 검색·큐레이션(2026-04), 원격 접속, 실행 간 비교. 실시간 관제보다 사후 분석·디버깅 도구 | R62 |

**[추론] 공통 분모**
1. 지도 위 실시간 위치 + 상태 목록 + 미션 큐 + 알람이 기본 4요소다.
2. 교통 기능은 "교차로·문·엘리베이터 같은 병목 자원 관리"와 "속도 제한·진입 금지 구역"이 중심이다.
3. 운영 성숙 제품일수록 **사후 재생(playback)**, **감사 로그**, **원격 개입의 단계화**를 강조한다.
4. 사람·수동 차량 혼재를 제품 기능으로 명시한 사례는 BlueBotics ANT locator처럼 "수동 차량 위치를 FMS에 넣고 신호등·구역으로 조율"하는 형태다.

### 1.5 설계에 주는 시사점

- 로봇 인터페이스는 VDA 5050 3.0 모델(order base/horizon, instantActions, state, connection, zoneSet/responses)을 내부 표준으로 삼으면, 실물·시뮬레이터·타사 로봇을 같은 추상화로 붙일 수 있다.
- 교통 권한 모델은 "자원 예약 → base 릴리스"로 구현하고, Open-RMF의 `Route{checkpoints, dependencies}`처럼 체크포인트 기반 의존성을 함께 저장한다.
- 관찰만 가능한 유닛용 입력은 MassRobotics `statusReport`형(위치·속도·예측 위치·operationalState)으로 받고, 스케줄에는 Open-RMF처럼 unresponsive 참가자 + vicinity로 올린다.
- 관제 명령 목록(일시정지 즉시/체크포인트, 차선 폐쇄, 속도 제한, 비상 신호, mutex 수동 해제)은 Open-RMF 메시지 목록을 체크리스트로 쓴다.
- Open-RMF 메인테이너도 좁은 양방향 통로 혼잡은 일방통행·동적 폐쇄로 푸는 것을 권한다. 레이아웃·규칙 설계를 알고리즘보다 먼저 한다.

---

## 2. 수십 대 AMR 교통 제어 알고리즘

### 2.1 자원 모델: 무엇을 예약하는가

**[추론]** 문헌과 표준의 방식을 자원 관점으로 정리하면 세 층이 된다.

| 층 | 예약 단위 | 대표 구현 | 장점 | 약점 |
|---|---|---|---|---|
| A. 공간 락(zone/intersection lock) | 노드·엣지·구역을 "지금 누가 쓰는가" | zone control(구역별 허용 대수 제한 + permit 방식 이동) [R85], Open-RMF MutexGroup(lease·heartbeat, 먼저 요청한 쪽 우선) [R4], VDA 5050 RELEASE zone [R30] | 단순, 지연에 강함, 사람 구역과 같은 틀 | 보수적이라 처리량 손실, 순환 대기로 교착 가능 |
| B. 시공간 예약 테이블 | (자원, 시간 구간) | 노드별 시간 창 그래프 위에서 Dijkstra 최단 경로로 비어 있는 시간 창만 지나게 라우팅(Kim & Tanchoco 1991, 이후 실시간 제어 계층 추가 연구로 확장) [R87], Open-RMF 스케줄 DB [R1], RHCR 창 [R76] | 흐름이 부드럽고 처리량 높음 | 예측 시간이 어긋나면 충돌·재계획 폭주 |
| C. 순서 기반 의존성 | "A가 체크포인트 k를 지난 뒤 B 진입" | ADG [R80], Open-RMF traffic dependency [R7] | 지연·속도 편차에 강함 | 순서 자체가 나쁘면 재정렬 필요(2.6절) |

**[제안]** MVP는 A(공간 락) + C(순서 의존)로 시작하고, B(시공간 계획)는 경로 후보 평가와 ETA 예측용으로 쓴다. 예약 레코드 예시는 다음과 같다.

```json
{
  "reservation_id": "rsv-0192",
  "resource": {"type": "edge", "id": "E-17", "direction": "N12->N13"},
  "holder": {"kind": "robot", "id": "amr-07"},
  "order_ref": {"order_id": "o-5521", "sequence_id": 7},
  "window": {"start": "2026-09-29T10:02:03.120Z", "end_est": "2026-09-29T10:02:09.800Z"},
  "lease_expiry": "2026-09-29T10:02:15.000Z",
  "state": "GRANTED",
  "depends_on": [{"holder": "amr-03", "checkpoint": "N13", "event": "passed"}]
}
```

### 2.2 MAPF 계열 비교

| 방식 | 성질(최적성·완전성) | 문헌이 보고한 규모·지연 | 창고 적용 관점 | 출처 |
|---|---|---|---|---|
| CBS | 2단계 탐색(충돌 트리 + 단일 에이전트 탐색), 최적 | - | [추론] 에이전트 수가 늘면 충돌 트리가 커져 lifelong 실시간 재계획의 주 알고리즘으로는 부담. 소수 로봇 국소 충돌 해소에 적합 | R70 |
| ECBS | focal search를 쓰는 유계 준최적(bounded-suboptimal), RHCR 논문 기준 대표적 유계 준최적 해법 | RHCR 실험: 창 w=5, 60대, 준최적 계수 1.5에서 재계획 1회 평균 0.35 s | 품질 보장과 속도의 절충 | R71, R76 |
| PBS | 우선순위 순서 공간을 탐색. 불완전·준최적 | RHCR+PBS: w=∞면 700대까지, **w=5면 1,000대 이상** | 우선순위 규칙을 체계적으로 찾는 방식. 대수가 많을 때 강함 | R72, R76 |
| PIBT | 한 스텝씩 반복 계획하는 우선순위 상속·백트래킹. 이중 연결(biconnected) 그래프에서 모든 에이전트가 유한 시간 내 목적지 도달 보장 | 수백 대에서 거의 즉시 해를 냄 | 매 틱 계획(1-step)이라 lifelong에 자연스러움 | R73 |
| LaCAM | PIBT를 하위 생성기로 쓰는 완전(complete)·준최적 탐색 | 32×32(장애물 20%) 400대 전 인스턴스 중앙값 1 s. warehouse 맵 **10,000대를 최대 30 s**(노트북 i9 단일 스레드는 최대 10 s)에 해결. **폭 1 통로에서 두 쌍이 자리 바꾸는 상황에 약함** | 대규모·고밀도에 강하나 좁은 통로 레이아웃은 피해야 함 | R74 |
| Real-Time LaCAM | 스텝당 ms 단위 시간 예산 안에서 반복 계획하면서 완전성(교착·livelock 없음) 보장 | 전체 지평 LaCAM과 같은 성공률(SoCS 2025) | 고정 주기 실시간 제어 루프에 적합 | R75 |
| RHCR | lifelong MAPF를 창 w 안의 충돌만 푸는 Windowed MAPF 연속으로 분해, h 스텝마다 재계획 | 1,000대(빈 셀의 38.9%), 비교 실험 w=20·h=5. 작은 w도 처리량 변화 대부분 1% 미만이면서 계산량은 크게 줄어듦. 1분 넘으면 실패로 간주 | "가까운 미래만 확정, 먼 미래는 대략"이라는 base/horizon과 같은 구조 | R76 |
| WPPL | PIBT 초기해 + 병렬 LNS 개선 + RHCR 창 | League of Robot Runners 2023 우승. 대회는 **스텝마다 1초 안에 수천 대(최대 10,000대, 밀도 최대 97.7%) 계획** 요구 | 실시간 대규모 기준점 | R77, R78 |
| SILLM(학습 기반) | 모방 학습 + 충돌 방지 PIBT | 10,000대, GPU에서 스텝당 약 0.35 s(WPPL 4.14 s), WPPL 대비 처리량 +16% | 연구 단계, 참고용 | R79 |

**[사실] 현실과의 간극** [R77]
- 대회·벤치마크는 격자·단위 시간 동작 모델이다. 회전 동작을 넣은 것만으로도 상당한 공학이 필요했고, 복잡한 운동학, 실행 지연, 불균일한 작업 분포는 미해결 과제로 남아 있다.
- 창 기반 방식은 창 밖 충돌을 무시하므로 근시안적 혼잡이 누적될 수 있다.

**[추론] 수십 대 규모에서의 선택**
- 수십 대는 PBS·ECBS·PIBT·LaCAM 모두 계산량 면에서 여유가 있다. 병목은 계산이 아니라 **계획과 실제 주행의 불일치**와 **통제 불가 유닛에 의한 막힘**이다.
- 따라서 "어떤 MAPF를 쓰느냐"보다 "계획을 어떻게 릴리스하고 실행 중 어긋남을 어떻게 흡수하느냐"(2.6절)가 성패를 가른다.

### 2.3 교착(deadlock) 탐지·해소

**[사실]**
- zone control 방식은 로봇이 permit을 받아 노드에서 노드로 이동하고 구역별 동시 허용 대수를 제한해 교착·충돌을 막거나 푼다. [R85, 2차]
- zone 제어 AGV 시스템의 순환 교착 예측: 각 차량의 다음 한 구역 이동 후 위치를 투영해 사이클이 생기는지 검사하며, 이동 명령마다 O(V²)로 예측할 수 있다(Moorthy 외, IJPE 2003). [R85]
- RHCR은 창 w 안의 충돌만 풀기 때문에 교착을 막으려고 진행도를 평가하는 잠재 함수(potential function)를 두고, 진행이 부족하면 창을 늘린다. 교차로에서 한 방향만 계속 우선하면 반대 방향이 영원히 못 가는 문제는 w=∞여도 생긴다고 논문이 밝힌다. [R76]
- PIBT는 이중 연결 그래프에서 도달을 보장하고, LaCAM은 완전하다. [R73, R74]
- Open-RMF의 BlockadeCheckpoint에는 `can_hold`(이 지점에서 대기 가능한가)가 있다. [R3]
- Bonetti 외(IJRR 2026)는 NURBS 로드맵 위 lifelong MAPF(수정 bounded-horizon CBS + RHCR)에 교착 탐지·해소 메커니즘을 넣어 좁은 양방향 통로에서 규칙 기반·우선순위 기반 대비 처리량을 최대 11% 높였다고 보고했다. [R86]

**[추론] 설계 절차**
1. **탐지: wait-for graph(WFG).** 노드는 로봇(과 관찰 에이전트), 엣지 `A → B`는 "A가 요청한 자원을 B가 점유 중". 이벤트(예약 거절, 대기 진입)마다 DFS로 사이클을 찾는다. 수십 대면 비용이 무시할 수준이다.
2. **사이클 분류.**
   - 로봇만으로 된 사이클 → 알고리즘이 해소.
   - 관찰 전용 에이전트(사람·지게차)가 포함된 사이클 → 그 에이전트는 비킬 수 없으므로 로봇 쪽만 움직여 해소하고, 일정 시간 지나면 운영자 알람.
3. **예방.**
   - 일방통행 레인, 교차로 안 정지 금지(`can_hold=false` 지점에는 base를 끊지 않음).
   - 구역 수용량 제한(admission control): 출구 자원을 확보하지 못하면 병목 진입 자체를 release하지 않는다.
   - 대기 지점(holding point)을 병목 앞에 명시한다.
4. **해소 순서.** 우선순위 규칙(오래 기다린 쪽 우선, 먼저 요청한 쪽 우선) → 후퇴(가장 가까운 `can_hold` 지점이나 우회 포켓으로 back-off) → 재경로 → 작업 재할당 → 운영자 개입.
5. **기록.** 교착 발생 수, 해소 시간, 원인 유형을 KPI로 남긴다(4.5절).

### 2.4 우선순위 규칙

**[사실]**
- Open-RMF judge는 고우선순위 참가자를 항상 택하도록 구현할 수 있다(긴급 작업). [R2]
- Open-RMF MutexGroupRequest는 `claim_time`이 빠른 요청이 우선이고, claim 시각을 바꾸면 새 요청으로 취급돼 뒤로 밀릴 수 있다. [R4]
- VDA 5050 RELEASE zone은 QUEUED 응답으로 대기열에 넣었음을 알릴 수 있다. [R30]

**[제안]** 우선순위 = (안전·비상 > 작업 마감 임박 > 적재 상태 > 대기 시간 누적(aging) > 요청 순서). aging을 넣어야 특정 방향이 영구히 밀리는 기아(starvation)를 막는다(RHCR의 교차로 사례와 같은 문제).

### 2.5 레이아웃과 일방통행

**[사실]**
- 창고 레이아웃을 최적화하면 혼잡이 줄고, 같은 MAPF 알고리즘으로도 **로봇 수를 일부 경우 두 배**까지 늘릴 수 있었다(IJCAI 2023). [R83]
- Guidance Graph Optimization은 엣지 가중치(와 후속 연구에서 방향)를 최적화해 처리량(시간 스텝당 목표 도달 수)을 높이며, 93×91 맵·3,000대까지 가이던스를 생성했다(IJCAI 2024). [R84]
- RHCR 실험은 교대로 방향이 정해진 열(일방통행) 같은 수작업 방향 규칙을 사용했다. [R76]
- Open-RMF 메인테이너도 좁은 양방향 통로 혼잡 해법으로 일방통행 차선을 권했다. [R22]

**[추론]** 수십 대 규모에서 가장 비용 대비 효과가 큰 조치는 (1) 폭 1 통로 제거 또는 일방통행화, (2) 교차로 앞 대기 지점, (3) 충전·주차 구역을 주 동선 밖으로 빼기다. LaCAM이 폭 1 통로 스왑에 약하다는 결과도 같은 방향을 가리킨다. [R74]

### 2.6 실행 불확실성: ADG와 실행 시점 동기화

**[사실]**
- **ADG(Hönig 외, RA-L 2019, Amazon)**: 기존 단발(single-shot) MAPF 계획을 받아, 계획 속 **통과 순서 제약을 명시한 Action Dependency Graph**로 바꾼다. 알 수 없거나 변하는 동역학 한계, 예상 못 한 감속, 갑작스런 장애물에도 견고하게 실행되고, 재계획과 실행을 겹칠 수 있으며, 로봇과 중앙 계획기 사이 통신량이 적다. 창고 시뮬레이션과 혼합현실 차동구동 로봇 실험으로 검증했다. [R80]
- **Switchable ADG(Berndt 외, 2020)**: 실행 중 통과 순서를 다시 최적화하는 피드백 방식. [R81]
- **Switchable-Edge Search(Feng 외, ICAPS 2024)**: 지연이 생기면 경로 전체를 다시 짜지 않고 같은 위치를 지나는 순서만 재조정한다. 최적 통과 순서를 찾는 A* 계열이며 중소 규모는 1초 미만, 대규모는 기준 대비 최대 4배 빠르다. [R82]
- Open-RMF traffic dependency는 같은 아이디어를 스케줄 DB에 넣었다. [R7]
- VDA 5050 base/horizon과 `newBaseRequest`는 실행 시점 동기화를 위한 표준 수단이다. [R30]

**[추론] 실행 계층 설계**
```
계획(경로+시간) ──▶ ADG 변환(자원별 통과 순서 추출)
                     │
                     ▼
각 로봇의 다음 구간 release 조건 = (자기 이전 동작 완료) ∧ (선행 로봇이 해당 자원을 떠났음)
                     │ 만족 시
                     ▼
VDA 5050 order update로 base 확장(엣지·노드 released=true)
                     │ 지연 감지(ETA 편차 > 임계)
                     ▼
통과 순서 재정렬(SES/sADG) 또는 재계획 → horizon 교체
```
- 시간표는 예측·UI 표시용, 실제 진입 허가는 순서 조건으로 판단한다.
- 관찰 전용 에이전트가 자원을 점유하면(예: 사람이 교차로에 있음) 그 자원의 "해제 이벤트"가 올 때까지 release를 보류한다.

### 2.7 설계에 주는 시사점

- 수십 대에서는 알고리즘 성능보다 **릴리스 정책(얼마나 앞까지 base를 풀어 줄지)**과 **순서 기반 실행(ADG)**이 처리량·안정성을 좌우한다.
- 교착은 WFG 사이클 탐지 + 예방 규칙(일방통행, 교차로 내 정지 금지, 수용량 제한) + 단계적 해소(우선순위→후퇴→재경로→운영자)로 다룬다.
- 경로 계획기는 교체 가능하게 인터페이스를 분리하고, 초기에는 우선순위 기반(PBS/PIBT류) + 창(RHCR식)으로 충분하다. LaCAM/Real-Time LaCAM은 밀도가 높아질 때 교체 후보다.
- 레이아웃(폭 1 통로 제거, 일방통행, 대기 지점)을 알고리즘 튜닝보다 먼저 검토한다.
- 모든 알고리즘 비교는 격자 벤치마크 수치이므로, 실제 차량 운동학·지연을 넣은 시뮬레이터에서 다시 측정해야 한다.

---

## 3. 통제 불가 유닛(사람·수동 지게차·타사 로봇)과의 혼재

### 3.1 안전 표준

#### 3.1.1 ISO 3691-4:2023 (무인 산업차량과 그 시스템)

**[사실] 원문 샘플에서 확인한 내용** [R90]
- ISO/TC 110/SC 2 작성, **2판(2023)**이 1판(2020)을 대체. 새 용어로 "active detection field", "operational stop"이 추가됐고 4.1.16~4.1.27에 새 요구사항이 들어갔다.
- 적용 대상 예시: AGV, **AMR**, bots, 자동 카트, 터널 터거, 언더 카트. 자동+수동 겸용(hybrid) 모드 트럭과 탑승자(rider) 포함 차량도 다룬다. 원격 조종 차량과 기계식 가이드 차량은 대상이 아니다.
- "driverless truck system"은 차량 + 부대 구성요소이며, 부대 구성요소에는 **교통 제어(traffic control)**, 통신, 가드, 표지, 바닥 마킹이 포함된다. → 관제 시스템도 시스템 범위 안에 들어간다.
- **공공 구역(public zone)** 운행 추가 위험은 범위 밖이며 ISO 13482:2014를 보라고 한다.
- 구역 용어 정의:
  - operating zone: 차량이 운행하는 정의된 영역(내비게이션·표지·바닥 마킹·펜스로 표시).
  - **operating hazard zone**: 운행 구역 중 사람이 위험에 노출될 수 있는 영역(예: 적재 이송 구역, 낮은 여유 공간). ISO 12100의 위험 구역으로 간주.
  - **restricted zone**: 무단 인원 접근을 막도록 둘러싼 공간.
  - **confined zone**: 인원 접근을 막도록 둘러싼 공간.
  - public zone: 훈련·안내 없이 누구나 드나드는 공간.
- virtual bumper: 비접촉 보호 장치(ESPE, 예: AOPDDR 레이저 스캐너)로, 작동 시 정지 또는 경로·속도 변경 신호를 낸다.

**[사실] 2차 출처(TÜV Rheinland 백서, Fabrico 해설)** [R91, R92]
- 인원 감지는 "정지해 있는 사람과 차체·적재물의 강체 부분이 **접촉하기 전에 정지**"해야 한다(EN 1525의 "신호 생성"에서 강화). 인원 감지 시스템과 제동 제어의 안전 관련 제어부는 **PLr d**(ISO 13849-1).
- 운행 구역 기본 여유 폭은 경로 양쪽 0.5 m(높이 2.1 m). 인원 감지를 뮤트한 operating hazard zone에서는 최고 속도 **0.3 m/s** 등 조건이 붙는다(Annex A 표).
- 시험체는 서 있는 사람·누운 사람을 모사한 두 원통(반사율 2~6%)이다. 해설 자료가 전하는 치수는 지름 200 mm×600 mm, 지름 70 mm×400 mm인데 방향 표기가 자료마다 달라 원문 확인이 필요하다.
- 한계: 사람이 옆에서 차량 경로로 뛰어드는 상황은 범위 밖이다.
- 책임 분담: 제조사는 차량을, 통합자·운영사는 경로·구역·위험성 평가를 책임진다.

#### 3.1.2 ANSI/A3 R15.08 (산업용 이동 로봇, IMR)

**[사실]** [R93, R94, R95]
- Part 1(제조사 요구사항): R15.08-1-2020, 2026년 기술 변경 없이 재확인(R2026).
- Part 2(IMR 시스템·응용, 통합자): 2023-10 발표. IMR 함대(IMRF)를 포함한 시스템 통합 요구사항. **위험성 평가는 IMR 시스템 통합자가 수행**. 유형 A(부착물 없는 AMR), B(수동·능동 부착물, 매니퓰레이터 없음), C(로봇 팔 탑재). 사용자가 시스템을 개조하면 통합자·제조사 책임을 진다.
- Part 3(사용자, IMR 응용의 사용): **R15.08-3-2026** 발행.

#### 3.1.3 ISO 13482 (서비스 로봇)

**[사실]** ISO 13482:2014(개인 돌봄 로봇)의 개정판 "Robotics — Safety requirements for service robots"가 **FDIS 투표 단계(50.20, 2026-09-15 시작)**에 있다. 개인용뿐 아니라 전문·상업용 서비스 로봇까지 범위를 넓히고 물리적 인간-로봇 접촉 조건과 기능 안전 정보를 다룬다. [R96, R97]
**[추론]** 공공 공간(병원·쇼핑몰·배달)을 다니는 로봇은 ISO 3691-4 범위 밖이므로 개정 ISO 13482 발행을 추적해야 한다.

#### 3.1.4 보호 필드·경고 필드와 이격 거리

**[사실]**
- 안전 레이저 스캐너는 IEC 61496-3의 AOPDDR(Type 3)이다. AGV에서는 안쪽 **보호 필드**가 이중 채널 OSSD로 안전 정지를 걸고, 바깥 **경고 필드**는 안전 등급이 없는 보조 출력으로 감속을 건다. 필드 세트는 운전 조건(속도 등)에 따라 전환된다. [R100]
- 보호 필드 길이는 정지 거리(차량 제동 거리 + 제어 시스템 응답 거리 + 스캐너 응답 거리) + 보정치로 잡는다. 다중 샘플링은 가용성을 높이지만 응답 시간을 늘린다. 인코더와 결합하면 경고 필드로 사람과의 거리에 따라 감속·가속할 수 있다. [R99]
- 속도·이격 감시(SSM, ISO/TS 15066): 최소 보호 거리 S = (사람 접근 속도 기여) + (로봇 반응 시간 동안 이동) + (정지 거리) + (C + Z_R + Z_S). 이는 ISO 13855의 S = vT + C에서 나왔고, 사람 접근 속도는 ISO 13855 기준 2,000 mm/s(조건부 1,600 mm/s)를 쓴다. [R98]

**[추론] 관제에 주는 의미**
- 보호 필드 크기는 속도에 비례해 커진다. 관제가 사람 많은 구역의 속도 상한을 낮추면(SPEED_LIMIT zone) 필드가 작아져 불필요한 정지(fieldViolation)가 줄고 흐름이 좋아진다.
- VDA 5050 `safetyState.fieldViolation`의 빈도·위치를 모으면 "사람과 로봇이 자주 부딪히는 지점" 지도를 만들 수 있다.
- 관제의 감속·정지 명령은 무선·소프트웨어 경로라 PLr d 안전 기능이 될 수 없다. 안전 기능은 차량 탑재 스캐너·제동이 맡고, 관제는 노출을 줄이는 운영 계층이다. 이 구분을 요구사항 문서에 명시한다.

### 3.2 관제 레벨의 처리

#### 3.2.1 관측 소스

| 소스 | 무엇을 주나 | 근거 | 주의 |
|---|---|---|---|
| 로봇 탑재 센서 공유 | 로봇이 본 장애물·빈 공간 | Open-RMF 차세대 프로토타입의 free-space 관측 공유(빈 공간 보고가 이전 장애물 보고를 지움) [R20], VDA 5050 `intermediatePath` [R30] | 로봇 시야 밖은 모름 |
| 시설 CCTV·카메라 | 사람 검출·대략 위치 | `rmf_human_detector`(YOLOv4, CCTV 용도) [R12] | 단안 위치 추정은 휴리스틱, 가림 |
| 고정 라이다 변화 감지 | 새 물체 존재 | `rmf_obstacle_detector_laserscan` [R12] | 분류 없음 |
| UWB/RTLS 태그 | 지게차·사람 위치(서브미터급) | UWB는 적재물·랙 너머로도 태그를 추적 [R114] | [추론] 경보 보조 장치이며 안전 등급(PL)이 확인되지 않은 경우가 많음. 적용 전 제품별 인증 확인 필요 |
| 착용 장치 | 작업자 존재 | Amazon Robotic Tech Vest(2019): 이전에는 작업자가 일할 격자 셀을 표시해 교통 계획기가 우회하게 했고, 조끼 도입 후 로봇이 먼 거리에서 사람을 감지해 경로를 바꾼다. 25개 이상 현장, 100만 회 이상 활성화 [R112] | 착용 누락 시 무효 |
| 수동 지게차 위치 연동 | 수동 트럭 실시간 좌표 | BlueBotics ANT locator → ANT server 통합, 신호등·구역으로 조율 [R59] | 벤더 종속 |
| 타사 FMS 상태 공유 | 타사 로봇 위치·경로·상태 | MassRobotics `statusReport`, Open-RMF read-only `FleetState` [R40, R8] | 갱신 주기·지연 |

#### 3.2.2 관찰 에이전트 데이터 모델 [제안]

```json
{
  "agent_id": "obs-forklift-12",
  "class": "manual_forklift",            
  "controllable": false,
  "sources": ["uwb", "cctv-cam-3"],
  "pose": {"x": 41.2, "y": 17.8, "yaw": 1.57, "frame": "site_map", "level": "L1"},
  "velocity": {"vx": 1.8, "vy": 0.0},
  "covariance_xy": [[0.09, 0], [0, 0.09]],
  "last_seen": "2026-09-29T10:02:03.500Z",
  "ttl_s": 2.0,
  "footprint_radius_m": 1.2,
  "vicinity_radius_m": 3.0,
  "predicted_occupancy": [{"t_offset_s": 1, "cells": ["E-17", "N13"], "p": 0.8}],
  "zone": "Z-DOCK-A"
}
```
- Open-RMF `Obstacle.lifetime`, read-only의 `vicinity_radius`, MassRobotics `predictedLocation`을 합친 형태다.
- 마지막 관측 이후 시간이 지날수록 불확실성을 키우고, TTL을 넘기면 "위치 불명"으로 바꿔 해당 구역을 보수적으로 다룬다.

#### 3.2.3 예측

**[사실]**
- 사람 궤적 예측은 모델링 방식(물리 기반, 패턴 기반, 계획 기반)과 사용하는 맥락 정보 수준으로 분류된다(Rudenko 외, IJRR 2020 서베이). [R110]
- Maps of Dynamics(예: CLiFF-map)는 위치마다 속도(방향·빠르기) 분포를 가우스 혼합으로 저장해 장기 흐름을 모델링한다. 실제 창고에서 대부분 우측 통행을 따르지만 구역마다 고유 규칙이 있는 흐름이 관측됐다(Kucner 외, IJRR 2023 서베이). [R111]

**[추론/제안]**
- 관제 레벨(1 Hz 전후, 수 초 지평)에서는 등속 모델 + 시간에 따라 커지는 불확실성 원이면 충분하다. 목적은 충돌 회피가 아니라 "이 자원을 곧 사람이 쓸 가능성"을 예약에 반영하는 것이다.
- 장기적으로는 로그로 시간대별 흐름 지도(MoD)를 만들어 PENALTY zone 가중치나 경로 비용에 반영한다.

#### 3.2.4 구역 설계와 반응 정책 [제안]

ISO 3691-4 구역 용어와 VDA 5050 zone 유형, Open-RMF 명령을 대응시켰다.

| 구역 성격 | ISO 3691-4 용어 | 사람 출입 | 관제 기본 정책 | 표현 수단 |
|---|---|---|---|---|
| 로봇 전용 | confined zone | 불가(펜스) | 최고 속도, 조밀한 예약 | 일반 lane |
| 허가자만 | restricted zone | 훈련된 인원만 | 인원 진입 시(태그·문 인터록) 해당 구역 로봇 감속 또는 정지 | SPEED_LIMIT / BLOCKED zone, LaneRequest |
| 혼용 동선 | operating zone | 가능 | 속도 상한, 교차로는 RELEASE 락, 사람 감지 시 차선 감속 | SPEED_LIMIT, RELEASE, lane_blocker 규칙 |
| 위험 작업 구역 | operating hazard zone | 가능 | 적재 이송 중 사람 감지 시 정지, 경고등, 저속 | RELEASE(`releaseLossBehavior=STOP`), ACTION zone(경고 액션) |
| 사람 전용 통로 | - | 사람만 | 로봇 진입 금지 | BLOCKED zone |
| 공공 구역 | public zone | 누구나 | ISO 3691-4 범위 밖, ISO 13482 검토 | 별도 위험성 평가 |

반응 규칙 예시(Open-RMF lane_blocker 기본값을 참고):
- 사람 1명이 차선과 0.25 m 이내로 겹침 → 해당 차선 폐쇄(예약 불가), 이미 진입한 로봇은 다음 `can_hold` 지점에서 정지.
- 관측 3건 이상 누적 → 주변 차선 속도 0.5 m/s 제한.
- 수동 지게차가 교차로 RELEASE 구역에 접근 → 로봇 요청을 QUEUED로 보류, 지게차 통과(관측 소멸) 후 GRANTED.
- 관측 소스 끊김(TTL 초과) → 해당 구역을 "불명"으로 보고 감속 + 알람.

#### 3.2.5 혼잡 지도

**[추론/제안]**
- 셀·엣지 단위로 (a) 예약 점유율, (b) 대기 시간 누적, (c) 관찰 에이전트 밀도, (d) fieldViolation·비상정지 발생 수를 시간창별로 집계한다.
- 이 값을 경로 비용(VDA PENALTY zone의 `penaltyFactor`에 대응)으로 되먹이고, UI에는 히트맵 레이어로 보인다.
- Open-RMF 메인테이너가 지적한 "혼잡 지연 과소평가" 문제를 피하려면 ETA 예측에 (b)를 반영한다. [R22]

### 3.3 "명령은 못 하고 관찰만 가능한" 유닛을 교통 스케줄에 넣는 법

**[사실] Open-RMF 방식** [R2, R8, R11]
1. 관찰 대상의 위치와 경로를 `FleetState`로 받는다.
2. 어댑터가 과대 설정한 운동학 한계로 미래 궤적을 예측해 스케줄에 올린다.
3. 참가자는 `RX_UNRESPONSIVE`로 등록돼 협상에 응하지 않는다. 결과적으로 **다른(명령 가능한) 함대가 피한다.**
4. `vicinity_radius` 안에는 다른 로봇이 들어오지 못한다.
5. 공간당 read-only 함대는 최대 하나. 둘 이상이면 교착·자원 충돌 회피가 거의 불가능하다.

**[추론] 일반화**
- 사람·수동 지게차·타사 로봇은 모두 "unresponsive 참가자"다. 이들끼리의 충돌은 관제가 풀 수 없고 그들 고유의 규칙(보행 규칙, 지게차 운전 규칙, 타사 FMS)이 푼다.
- 그러므로 관제의 역할은 (1) 로봇이 항상 양보하도록 예약에서 그들의 예측 점유를 먼저 반영하고, (2) 서로 다른 통제 불가 유닛이 만나는 지점(예: 지게차 동선과 보행 통로 교차)은 **물리 신호(신호등·차단기)나 운영 규칙**으로 분리하는 것이다. BlueBotics가 수동 트럭 연동에 신호등을 쓰는 것도 같은 맥락이다. [R59]
- Traffic Light형 연동(일시정지·재개만 가능)의 타사 로봇은 관찰 전용보다 한 단계 낫다. 교차로 RELEASE 락에 대해 "진입 전 일시정지" 방식으로 참여시킬 수 있다. 다만 Open-RMF는 이 유형의 문·엘리베이터 연동을 지원하지 않는다. [R10]

### 3.4 설계에 주는 시사점

- 요구사항 문서에 "관제는 안전 기능이 아니다(PL 등급 없음), 안전은 차량 탑재 PLr d 기능과 현장 구역 설계가 담당"을 명시한다.
- 통제 불가 유닛은 `controllable=false` + 예측 점유 + vicinity로 스케줄에 넣고, 로봇이 항상 양보한다. 관찰 끊김(TTL)은 보수적 처리 + 알람으로 연결한다.
- 구역은 ISO 3691-4 용어(confined/restricted/operating/hazard/public)로 분류하고, 각 구역 유형에 기본 속도 상한·진입 규칙·사람 감지 시 반응을 표로 정의한다.
- Open-RMF lane_blocker처럼 "관측 → 차선 폐쇄/감속" 규칙 엔진을 두고, 임계값(겹침 거리, 인원 수, 해제 지연)을 설정값으로 뺀다.
- 서로 다른 통제 불가 유닛끼리의 교차는 소프트웨어가 아니라 레이아웃·신호등·운영 규칙으로 분리한다.

---

## 4. 관제실 운영 규범과 KPI

(범위 조정: 채용 공고의 업무·우대사항 조사는 별도 에이전트 담당으로 이 문서에서 제외)

### 4.1 알람 관리: ISA-18.2 / IEC 62682

**[사실]**
- ISA-18.2의 알람 정의: "장비 고장, 공정 이탈, 비정상 상태를 운영자에게 알리는 청각·시각 수단으로, **응답이 필요한 것**." [R120]
- 수명주기: 철학(philosophy) → 식별 → 합리화(rationalization) → 상세 설계 → 구현 → 운전 → 유지보수 → 모니터링·평가 → 변경 관리 → 감사. [R120, R123]
- 알람 상태: 정상, 미확인(unacknowledged), 확인(acknowledged), 복귀 후 미확인(RTN unack), 그리고 어느 상태에서든 갈 수 있는 **shelved**(운영자가 수동으로 임시 억제, 관리 요건으로 추적), **suppressed by design**(설계된 조건에 따른 억제, 상태 기반 알람 포함), **out of service**(장비 문제로 비작동). [R120]
- IEC 62682:2022(2판)은 2014년 1판을 대체하며 ANSI/ISA-18.2-2016 변경을 반영했다. [R122, R123]
- ISA-18.2 권고 목표치(비강제, 최소 30일 데이터 기준): [R121]

| 지표 | 목표 |
|---|---|
| 운영 위치당 10분간 발생 알람 | 1~2건 |
| 10분 구간 중 10건 초과 비율 | 1% 미만 |
| 10분간 최대 알람 수 | 10건 이하 |
| 알람 홍수(flood) 상태 시간 비율 | 1% 미만 |
| 우선순위 분포(저/중/고) | 약 80% / 15% / 5% |
| 채터링·순간(fleeting) 알람 | 0 |
| 24시간 넘게 활성인 stale 알람 | 하루 5건 미만 |

- 알람 홍수: 10분에 10건 이상으로 시작, 10분에 5건 미만으로 떨어지면 종료. 심각도는 10분 최대치 기준 10~20 경미, 20~30 중간, 30 초과 심각. [R121]

**[추론/제안] 로봇 관제로 옮기기**
- 알람은 "운영자가 무엇을 해야 하는 것"만이다. 로봇이 교차로에서 30초 대기하는 것은 이벤트(로그)이지 알람이 아니다. 대기가 교착으로 판정되고 자동 해소가 실패했을 때 알람이 된다.
- VDA 5050 에러 레벨을 우선순위에 대응: FATAL → 고, CRITICAL → 중(작업 정지), URGENT → 중/저, WARNING → 알람 아님(이벤트) 또는 저.
- **원인 단위 묶기(suppressed by design)**: 통로 하나가 막혀 로봇 12대가 멈추면 알람 12건이 아니라 "통로 E-17 막힘(영향 12대)" 1건으로 낸다. 이렇게 해야 홍수 기준(10분 10건)을 지킬 수 있다.
- shelving에는 만료 시간과 담당자를 강제한다.
- 알람 시스템 자체의 KPI(10분당 건수, 홍수 시간 비율, 우선순위 분포, stale 수)를 대시보드에 둔다.

### 4.2 관제실 인간공학: ISO 11064와 ISA-101

**[사실]**
- ISO 11064는 7부로 구성된다: 1부 설계 원칙, 2부 관제 공간(suite) 배치, 3부 관제실 배치, 4부 작업대 배치·치수, 5부 표시장치·조작장치, 6부 환경(온도·조명·음향·환기), 7부 평가 원칙. 공정 산업, 교통, 긴급 서비스 배차실 등 모든 유형의 관제 센터에 적용된다. [R124]
- ISA-101.01-2015(공정 자동화 HMI): [R125]
  - 화면 계층 4단계: Level 1 전체 개요(원거리 감독, KPI·이상 표시), Level 2 단위·구역, Level 3 설비 상세(조치 수행), Level 4 진단·정비.
  - 정상 상태는 회색·흰색 계열로 차분하게 그리고, **색은 이상 상태와 운영자 상태 변화에만** 쓴다. 장식용 색 금지.
  - 순간값보다 추세와 변화율을 강조해 운영자가 미래 상태를 예측(projection)하게 한다.

### 4.3 상황 인식(Situation Awareness)

**[사실]** Endsley(1995) 모델은 3단계다: 1) 지각(환경 요소의 상태·속성·동역학 인지), 2) 이해(의미 파악), 3) 예측(가까운 미래 상태 투영). 주의와 작업 기억이 병목이며, 정신 모델과 목표 지향 행동이 이를 보완한다. [R126]

**[추론] UI 대응**
| SA 단계 | 로봇 관제 화면 요소 |
|---|---|
| 지각 | 지도 위 로봇·사람·지게차 위치, 상태 색, 구역 상태 |
| 이해 | "왜 멈췄나": 대기 원인(누구의 어떤 자원 때문에), 교착 사이클 표시, 알람 원인 묶음 |
| 예측 | base/horizon 경로, ETA, 예측 혼잡 히트맵, 배터리 소진 예상, 곧 만료될 lease |

### 4.4 운영자 부하: fan-out과 요청 대기열

**[사실]**
- Fan-out(Olsen & Goodrich, 2004): 한 운영자가 동시에 다룰 수 있는 로봇 수. neglect time(방치해도 되는 시간)이 길고 interaction time(한 번 개입에 드는 시간)이 짧을수록 커진다. [R127]
- Chien 외(IROS 2012): 로봇이 도움 요청을 보내는 구조를 운영자가 서버인 대기열로 보고 비교했다. 요청을 하나씩만 보여 주는 FIFO는 이전 실험에서 전체 대기열(Open-queue)보다 성능이 나빴고, 최단 작업 우선(SJF) 우선순위 대기열은 대부분 지표에서 최고 조건과 통계적으로 구별되지 않았다. [R128]

**[추론]** 관제 인력 산정은 "로봇 수 ÷ fan-out"으로 추정할 수 있다. fan-out을 올리는 방법은 (1) 자동 해소로 neglect time을 늘리고, (2) 원클릭 조치로 interaction time을 줄이는 것이다. 알람 대기열은 전체를 보여 주되 우선순위·예상 처리 시간 순으로 정렬한다.

### 4.5 KPI 정의

표기: [표준] 공인 정의 있음, [문헌] 연구·업계 문헌 정의, [제안] 이 프로젝트용 정의.

| KPI | 정의(계산식) | 구분·출처 | 비고 |
|---|---|---|---|
| 처리량(throughput) | 완료 작업 수 / 시간(시간당). MAPF 문헌은 "시간 스텝당 목표 도달 수" | [문헌] R84, R76 | 창고 업계는 UPH·시간당 피킹(Locus) [R54] |
| 미션 성공률 | 성공 종료 미션 / 시작 미션 (취소·재할당·수동 완료는 별도 분류) | [제안], 미션 추적 개념은 R61 | 실패 원인 코드 필수 |
| 가용도(availability) | 운영 가능 시간 / 계획 운영 시간 | [표준] OEE의 Availability [R131] | 충전은 계획 정지로 분리 |
| 이용률(utilization) | 작업 수행 시간 / 가용 시간 | [제안] | 유휴·대기·충전 분리 표시 |
| MTBF | 운영 시간 합 / 고장 수 | [표준] IEC 60050-192 [R130] | 고장 정의(FATAL, 수동 복구 필요)를 먼저 고정 |
| MTTR | 복구(restoration) 시간 합 / 고장 수. IEC 60050-192는 수리 시간과 준비·대기를 포함한 복구 시간을 구분 | [표준] R130 | 관제 관점은 "알람 발생→자동 운전 복귀" |
| MTBI / 개입률 | 자율 운영 시간 / 개입 수, 또는 로봇·시간당 개입 수. 개입 = 사람이 시스템 상태를 바꾼 행위. minor(정상 사용: 지도 수정, 추가 미션), serious(운영 중 수정, 일부 재시작), fatal(전체 재시작)로 분류, minor 제외 계산 | [문헌] AutoInspect: 78 h, 140 h 보고 [R129] | VDA 5050 INTERVENED/MANUAL 전이, 원격 조작, 수동 해제 명령을 자동 집계 |
| 평균 대기 시간 | 예약 거절·QUEUED로 정지한 시간 합 / 이동 수 | [제안] | 자원별로 나눠 병목 식별 |
| 혼잡 지수 | 엣지별 (대기 시간 + 점유율) 시간창 평균 | [제안] | 히트맵과 같은 데이터 |
| 교착 발생 수·해소 시간 | WFG 사이클 탐지 수, 탐지→해소 시간, 자동/수동 해소 비율 | [제안] | 사람 포함 사이클 분리 |
| 안전 이벤트 | fieldViolation, 비상정지(MANUAL/REMOTE) 횟수·위치 | [제안], 필드는 VDA 5050 [R30] | 사람-로봇 접촉 위험 지도 |
| 알람 시스템 지표 | 10분당 알람, 홍수 시간 비율, 우선순위 분포, stale 수 | [표준] ISA-18.2 권고 [R121] | 4.1절 목표치 |
| OEE | 가용도 × 성능 × 품질 | [표준] R131 | 로봇 함대에는 품질=미션 성공률로 치환 [추론] |

**[추론] 측정상 주의**
- 새 지표는 "아무 조치를 안 한 기준선(예: 고정 우선순위 FIFO 락)"에서도 좋아 보이는지 먼저 확인해야 개선 효과를 말할 수 있다.
- 로봇 로그의 "성공" 표시는 거짓일 수 있으므로, 성공률은 관제가 독립적으로 관측한 도착·완료 이벤트로 판정한다.

### 4.6 설계에 주는 시사점

- 알람은 "운영자 응답이 필요한 것"만으로 한정하고, 이벤트·로그와 데이터 모델을 분리한다.
- 원인 단위 묶기와 shelving 만료를 처음부터 넣어 ISA-18.2 목표치(10분 1~2건, 우선순위 80/15/5)를 설계 목표로 삼는다.
- 화면은 ISA-101 4단계 계층과 회색 기본 + 이상만 색 원칙을 따른다.
- KPI는 이벤트 소싱으로 모든 상태 전이(주문, 예약, 대기, 개입, 알람)를 저장하면 사후에 대부분 계산할 수 있다.
- 운영 인력은 fan-out(neglect time, interaction time)으로 추정하고, 자동 해소율과 원클릭 조치로 fan-out을 키운다.

---

## 5. 관제 UI/대시보드 설계 요소

### 5.1 한 화면에 수십 대: 정보 설계

**[추론/제안]** ISA-101 계층(R125)을 로봇 관제에 적용한 구성이다.

| 계층 | 화면 | 핵심 내용 |
|---|---|---|
| Level 1 개요(벽면·대형 TV) | 전체 지도 + KPI 타일 + 알람 카운트 | 정상 로봇은 회색 점, 이상만 색. 처리량, 가동 대수, 활성 알람 수(우선순위별), 교착 수. OTTO의 대형 TV용 Fleet Dashboard와 같은 용도 [R53] |
| Level 2 구역 | 구역·층별 지도 확대 | 차선 방향, 예약 상태(점유/대기열), 구역 상태(BLOCKED, SPEED_LIMIT, RELEASE 보유자), 사람·지게차 관측과 불확실성 원, 혼잡 히트맵, 문·엘리베이터 상태 |
| Level 3 로봇 상세 | 로봇 한 대 | 운영 모드, base/horizon 경로, 현재 대기 원인(누구의 어떤 자원), 에러 목록(레벨·힌트), 배터리 예측, 카메라, 조치 버튼 |
| Level 4 진단 | 로그·재생 | 이벤트 타임라인, 시간 되감기 재생(OTTO 플레이백 [R53]), 원시 메시지 |

**공통 레이아웃 요소**
1. **지도 레이어**(켜고 끄기): 경로망·방향, 구역, 예약(base 실선, horizon 점선), 예측 경로, 관찰 에이전트(마지막 관측 후 경과 시간을 투명도로), 혼잡 히트맵, 안전 이벤트 발생 지점.
2. **예외 목록**: 정상 로봇은 접고, 이상 로봇만 우선순위·경과 시간 순으로 위에 둔다. 상태 그룹(자동/대기/개입 중/수동/오프라인).
3. **알람 대기열**: 전체를 보여 주되 우선순위·예상 처리 시간 순 정렬(Chien 외 결과 [R128]). 항목마다 영향 로봇 수, 위치(클릭 시 지도 이동), 권장 조치, 확인·shelve(만료·담당자 필수).
4. **타임라인**: 로봇별 스윔레인에 주문·예약·대기·알람·개입을 시간축으로. 교착·알람 원인 분석과 교대 인수인계에 쓴다.

### 5.2 예외 기반 관리

**[사실]** 로봇이 도움 요청을 스스로 알리게 하면 HRI 성능이 좋아졌고, 요청 대기열의 표시 방식(전체 표시, FIFO 1건, 우선순위)에 따라 성과가 달라졌다. [R128]
**[추론/제안]**
- 기본 화면에서 "정상"은 숨기고 "사람의 판단이 필요한 것"만 보인다.
- 자동 해소 가능한 문제(짧은 대기, 자동 교착 해소)는 로그로만 남기고, 실패하거나 시간 임계를 넘을 때 알람으로 올린다.
- 알람을 열면 판단에 필요한 맥락(지도 위치, 관련 로봇, 원인 사이클, 최근 타임라인)이 한 번에 보여야 한다. 상황 파악 시간이 fan-out을 깎기 때문이다.

### 5.3 원격 개입 흐름

**[사실] 표준·오픈소스가 제공하는 개입 수단** [R30, R4]

| 개입 | VDA 5050 | Open-RMF |
|---|---|---|
| 단일 로봇 일시정지/재개 | instantAction `startPause` / `stopPause`(3.0 필수) | `PauseRequest` IMMEDIATELY / AT_CHECKPOINT / RESUME |
| 작업 취소·재할당 | `cancelOrder`(가능한 한 빨리 정지, 취소 불가 액션은 끝까지) | `InterruptRequest`, 작업 재할당 API |
| 구역 봉쇄 | 새 zoneSet(BLOCKED) + `enableZoneSet`, 또는 RELEASE 요청 REJECTED/REVOKED(안에 있으면 `releaseLossBehavior` STOP/CONTINUE/EVACUATE) | `LaneRequest` close_lanes |
| 감속 구역 | SPEED_LIMIT zone | `SpeedLimitRequest` |
| 전체 정지 | [추론] 표준 브로드캐스트는 없어 모든 로봇에 `startPause` 발행 | `EmergencySignal`(fleet_names 비우면 전체) |
| 원격·현장 인계 | 운영 모드 `INTERVENED`(order 유지, cancelOrder만 허용) → AUTOMATIC 복귀 | - |
| 락 강제 해제 | - | `MutexGroupManualRelease` |

**[사실]** InOrbit은 원격 운영자가 상황을 파악하고 필요하면 "보통 몇 초 동안만" 넘겨받아 로봇을 궤도로 돌려놓은 뒤 자율 운전으로 되돌리는 다단계 원격 조종을 제공한다. [R61]
**[사실]** ISO 3691-4의 비상정지 장치는 "수동으로 작동하는 조작 장치"(ISO 13850)로 정의된다. VDA 5050 `activeEmergencyStop`은 차량의 MANUAL(차량에서 수동 확인)과 시설의 REMOTE(원격 확인) 비상정지를 구분해 보고만 한다. [R90, R30]

**[제안] 개입 단계 사다리(가벼운 것부터)**
1. **관찰·확인**: 알람 열기 → 맥락 확인(로봇 카메라, 지도, 타임라인).
2. **자동 제안 실행**: 권장 조치(재경로, 후퇴, 재할당) 원클릭.
3. **단일 로봇 일시정지**: 기본은 "다음 체크포인트에서 정지"(교차로 한가운데 정지 방지), 긴급 시 "즉시".
4. **구역 봉쇄·감속**: 지도에서 영역을 그려 BLOCKED/SPEED_LIMIT 적용. 영향받는 로봇과 재경로 결과를 미리 보여 준 뒤 확정.
5. **전체 정지**: 두 단계 확인 + 사유 입력. 재개는 구역별 순차 재개(동시 재개 시 혼잡 폭증 방지).
6. **원격 조종 인계**: INTERVENED 전이, 인계 중에는 해당 로봇 주변 자원을 예약으로 잠가 다른 로봇이 들어오지 않게 함. 종료 시 위치 재확인(localized) 후 AUTOMATIC 복귀.
7. **현장 인력 파견**: 원격으로 안 되면 작업 지시(위치, 안전 주의) 발행.

설계 원칙
- 관제의 "전체 정지"는 운영 정지(operational stop)이며 안전 등급 비상정지가 아니다. 화면 문구에서도 구분한다. [추론, R90 근거]
- 모든 명령은 RBAC + 감사 로그(누가, 언제, 무엇을, 왜)를 남긴다. MiR Fleet Enterprise(감사 로그), Geek+ RMS(명령 감사)가 같은 방향이다. [R51, R56]
- 명령 결과를 확인하는 폐루프를 둔다: 명령 발행 → 로봇 state에서 반영 확인(예: `paused=true`) → 시간 내 미반영이면 알람.

### 5.4 설계에 주는 시사점

- 화면은 지도 + 예외 목록 + 알람 대기열 + 타임라인 4요소로 구성하고, ISA-101 4단계 계층으로 확대·축소한다.
- 정상은 회색, 이상만 색. 알람 대기열은 전체 표시 + 우선순위·예상 처리 시간 정렬.
- 개입은 가벼운 조치부터 무거운 조치까지 사다리로 만들고, 전체 정지·구역 봉쇄는 영향 미리보기와 두 단계 확인을 거친다.
- 원격 인계는 VDA 5050 INTERVENED 의미론(주문 유지, cancelOrder만 허용)을 따르고, 인계 중 주변 자원을 예약으로 보호한다.
- 모든 명령은 감사 로그와 반영 확인 루프를 갖는다.

---

## 6. 구현 우선순위 제안

### 6.1 참조 아키텍처 [제안]

```
[L6 운영자 UI]  지도·예외목록·알람대기열·타임라인 / 개입 명령(확인·감사)
        │ REST + WebSocket
[L5 운영 서비스] 알람 서비스(ISA-18.2 상태) · KPI/이벤트 저장소 · 명령 게이트웨이(RBAC, 감사, 반영 확인)
        │
[L4 작업]       작업 접수 · 배정(비용 입찰식) · 충전 관리
        │
[L3 교통]       예약 관리자(공간 락 + lease) · 경로 계획기(교체 가능) · 실행 조정기(ADG/base 릴리스)
                교착 감시(WFG) · 구역 규칙 엔진(관측→폐쇄/감속)
        │
[L2 세계 모델]  경로 그래프·구역·자원 상태 · 제어 가능 로봇 · 관찰 에이전트(TTL, 불확실성, 예측 점유)
        │
[L1 어댑터]     VDA 5050(MQTT) · 시뮬레이터 · Open-RMF 브리지 · 관찰 입력(CCTV, UWB, 타사 FMS 상태)
        │
[L0 차량 안전]  탑재 스캐너·제동(PLr d) — 관제 범위 밖, 관제는 상태만 수신
```

### 6.2 MVP에 꼭 넣을 것

| 순서 | 항목 | 완료 기준(검증 방법) | 근거 |
|---|---|---|---|
| 1 | 경로 그래프·구역 모델 | 노드/엣지 방향, `can_hold`, 구역 유형(ISO 3691-4 분류 + VDA zone 유형) 로드 | 1.2.5, 3.2.4 |
| 2 | VDA 5050식 로봇 인터페이스 + 시뮬레이션 로봇 | order(base/horizon, orderUpdateId), state(최소 30초·이벤트), instantActions(startPause/stopPause/cancelOrder), connection(last will) 왕복 테스트 | 1.2 |
| 3 | 예약 관리자 | 노드·엣지·구역 락, lease 만료, 요청 상태(GRANTED/QUEUED/REJECTED/REVOKED), base 확장 정책 | 2.1, 1.2.5 |
| 4 | 교착 감시 | WFG 사이클 탐지, 해소 정책(우선순위+aging → 후퇴 → 재경로 → 알람), 사람 포함 사이클 분리 | 2.3 |
| 5 | 관찰 에이전트 입력과 규칙 엔진 | 사람·지게차 위치 입력(TTL·불확실성), 차선 폐쇄/감속 규칙(임계값 설정화), TTL 초과 시 보수 처리 | 3.2, 3.3 |
| 6 | 알람 서비스 | 3단계 우선순위, 미확인/확인/복귀/shelved(만료·담당자), 원인 단위 묶기 | 4.1 |
| 7 | 운영 UI | 지도(로봇·관찰 에이전트·구역·예약), 예외 목록, 알람 대기열, 이벤트 타임라인, 개입(일시정지 즉시/체크포인트, 구역 봉쇄, 전체 정지) + 두 단계 확인 + 감사 로그 | 5.1, 5.3 |
| 8 | KPI·이벤트 저장 | 처리량, 성공률, 대기 시간, 교착 수·해소 시간, 개입 수(minor/serious/fatal), MTTR, 알람 지표. 기준선(단순 FIFO 락) 대비 비교 | 4.5 |
| 9 | 시나리오 시뮬레이션 | 로봇 수십 대 + 사람·지게차 에이전트(무작위 보행, 교차 동선), 좁은 통로·교차로 포함 맵에서 교착·홍수·개입 흐름 재현 | Open-RMF crowdsim/Menge 참고 [R24] |

### 6.3 나중에 할 것

| 항목 | 이유·조건 | 근거 |
|---|---|---|
| 창 기반 MAPF(PBS/PIBT → LaCAM 계열) 교체 | 밀도가 높아져 락 방식 처리량이 한계에 닿을 때 | 2.2 |
| ADG 통과 순서 재정렬(sADG/SES) | 지연으로 인한 대기가 KPI상 병목일 때 | 2.6 |
| 흐름 지도(MoD) 기반 예측·경로 비용 | 로그가 몇 주 쌓인 뒤 | 3.2.3 |
| Open-RMF 브리지 / ISO 21423·MassRobotics 상태 내보내기 | 타 시스템과 연동 요구가 생길 때, ISO 21423 정식 발행 확인 후 | 1.1, 1.3 |
| 문·엘리베이터 연동 | 다층·출입문 경로가 필요할 때(Open-RMF Door/Lift 메시지 모델 참고) | 1.1.6 |
| 영상 기반 원격 조종 인계 | 네트워크 지연·안전 절차 검토 후 | 5.3 |
| 작업 배정 최적화(입찰식) | 작업 종류·로봇 이기종성이 늘 때 | 1.1.1 |
| 사후 재생(time travel) | 이벤트 저장이 안정된 뒤, 운영 분석 요구가 생길 때 | 1.4 |
| 다중 운영자·SSO·세분 권한 | 운영 인력이 둘 이상일 때 | 1.4, 5.3 |
| 알람 합리화(rationalization) 문서화 | 실제 운영 데이터 30일 이상 확보 후 | 4.1 |

---

## 7. 출처 목록

(접근일 2026-09-29. "2차"는 1차 출처에 접근하지 못해 해설·보도로 확인한 항목)

### Open-RMF
- [R1] RMF Core Overview, Programming Multiple Robots with ROS 2: https://osrf.github.io/ros2multirobotbook/rmf-core.html
- [R2] rmf-core.md 원문(제어 유형 표, Read Only 제한, judge): https://github.com/osrf/ros2multirobotbook/blob/master/src/rmf-core.md
- [R3] rmf_traffic_msgs 메시지 정의: https://github.com/open-rmf/rmf_internal_msgs/tree/main/rmf_traffic_msgs/msg
- [R4] rmf_fleet_msgs 메시지 정의: https://github.com/open-rmf/rmf_internal_msgs/tree/main/rmf_fleet_msgs/msg
- [R5] rmf_internal_msgs(문·엘리베이터·디스펜서·장애물): https://github.com/open-rmf/rmf_internal_msgs
- [R6] NegotiationProposal.msg: https://github.com/open-rmf/rmf_internal_msgs/blob/main/rmf_traffic_msgs/msg/NegotiationProposal.msg
- [R7] Traffic dependency system PR #70 (2022-03-20 병합): https://github.com/open-rmf/rmf_traffic/pull/70
- [R8] Read-Only Fleets: https://osrf.github.io/ros2multirobotbook/integration_read-only.html
- [R9] Mobile Robot Fleet Integration: https://osrf.github.io/ros2multirobotbook/integration_fleets.html
- [R10] Feature and limitation of traffic-light adapter (2025-09-22): https://discourse.openrobotics.org/t/feature-and-limitation-of-traffic-light-adapter/50134/3
- [R11] read_only FleetAdapterNode.cpp: https://github.com/open-rmf/rmf_ros2/blob/main/rmf_fleet_adapter/src/read_only/FleetAdapterNode.cpp
- [R12] rmf_obstacle_detectors: https://github.com/open-rmf/rmf_obstacle_detectors
- [R13] rmf-web: https://github.com/open-rmf/rmf-web
- [R14] rmf-web api-server README: https://github.com/open-rmf/rmf-web/tree/main/packages/api-server
- [R15] open-rmf/rmf README(지원 배포판): https://github.com/open-rmf/rmf
- [R16] open-rmf/rmf releases: https://github.com/open-rmf/rmf/releases
- [R17] Tasks in RMF(입찰): https://osrf.github.io/ros2multirobotbook/task.html
- [R18] Roadmap: https://osrf.github.io/ros2multirobotbook/roadmap.html
- [R19] Traffic Management Interfaces for the Next Generation (2025-06-22): https://discourse.openrobotics.org/t/traffic-management-interfaces-for-the-next-generation/44414
- [R20] Next-Gen Open-RMF Traffic Management, GSoC 2026 (2026-08-25): https://discourse.openrobotics.org/t/next-gen-open-rmf-traffic-management/57646
- [R21] Scalability of OpenRMF for large multi-robot deployments (2026-03~04): https://discourse.openrobotics.org/t/scalability-of-openrmf-for-large-multi-robot-deployments-in-dynamic-environments-e-g-airports/53697
- [R22] Congestion-aware traffic planning for dense and narrow industrial environments (2026-08~09): https://discourse.openrobotics.org/t/congestion-aware-traffic-planning-for-dense-and-narrow-industrial-environments/57410
- [R23] open-rmf/mapf: https://github.com/open-rmf/mapf
- [R24] rmf_crowdsim: https://github.com/open-rmf/rmf_crowdsim , menge_vendor: https://github.com/open-rmf/menge_vendor

### VDA 5050
- [R30] VDA 5050 3.0.0 명세(VDA5050_EN.md): https://github.com/VDA5050/VDA5050/blob/main/VDA5050_EN.md
- [R31] VDA 5050 릴리스 목록: https://github.com/VDA5050/VDA5050/releases
- [R32] VDA 보도자료, Version 3.0 of VDA 5050 released (2026-04): https://www.vda.de/en/press/press-releases/2026/260421_PM_VDA_5050_EN

### MassRobotics · ISO 21423
- [R40] MassRobotics AMR Interop Standard: https://github.com/MassRobotics-AMR/AMR_Interop_Standard
- [R41] (2차) MassRobotics AMR Interoperability Version 2.0, Logistics Viewpoints (2022-09): https://logisticsviewpoints.com/2022/09/14/massrobotics-amr-interoperability-version-2-0/
- [R42] ISO 21423 공식 페이지(접근 차단, 제목·범위만 검색 결과로 확인): https://www.iso.org/standard/86749.html
- [R43] ISO 21423 프로젝트 단계(60.00, 2026-07-21): https://isme.me/en/project/show/iso:proj:86749
- [R44] (2차) When Robots Speak the Same Language: Inside ISO 21423 (2025-10-10): https://sixdegreesofrobotics.substack.com/p/when-robots-speak-the-same-language
- [R45] (2차) What Is ISO 21423, AGV Network: https://www.agvnetwork.com/agv-technology/iso21423
- [R46] InOrbit.AI Releases OpenRobOps (2026-09-15): https://www.roboticstomorrow.com/news/2026/09/15/inorbitai-releases-openrobops-solving-the-robotics-build-trap-and-delivering-industrys-first-iso-21423-reference-implementation/27096
- [R47] ISO/DIS 21423 2025-07, DIN Media: https://www.dinmedia.de/en/draft-standard/iso-dis-21423/394598591

### 상용 FMS · 운영 플랫폼
- [R50] MiR Fleet: https://mobile-industrial-robots.com/products/software/mir-fleet
- [R51] New MiR Fleet Enterprise (2024-10-18): https://mobile-industrial-robots.com/blog/new-mir-fleet-enterprise
- [R52] OTTO Fleet Manager: https://ottomotors.com/fleet-manager/
- [R53] OTTO Motors software release (2022-06-07): https://ottomotors.com/company/newsroom/press-releases/otto-motors-software-release-gives-unparalleled-visibility-into-fleet-performance-of-autonomous-mobile-robots/
- [R54] LocusHub: https://locusrobotics.com/locusone/warehouse-agentic-ai/locushub , (2차) FreightWaves LocusHub 출시: https://www.freightwaves.com/news/locus-robotics-launches-business-intelligence-tool-locushub
- [R55] Geek+ RMS 발표 (2024-11-26): https://www.prnewswire.com/news-releases/geek-launches-worlds-most-powerful-robot-management-system-to-combat-peak-season-holiday-rush-302316116.html
- [R56] Geek+ RMS Major Upgrade (2026-02-12): https://www.geekplus.com/resources/robotics-blogs/geek-rms-major-upgrade-redefine-flexibility-safety-and-ease-of-use
- [R57] Seegrid Fleet Central: https://seegrid.com/fleet-central/
- [R58] BlueBotics ANT server: https://bluebotics.com/autonomous-navigation-technology/ant-server-fleet-software
- [R59] (2차) BlueBotics ANT locator (2025-03 발표, 2025-06 출시): https://www.telematicswire.net/bluebotics-ant-locator-enables-asset-tracing-and-transport-flow-optimization/
- [R60] Formant fleet observability: https://docs.formant.io/docs/fleet-observability , incident management: https://docs.formant.io/docs/incident-management
- [R61] InOrbit Operations: https://www.inorbit.ai/operations , InOrbit key features: https://www.inorbit.ai/hubfs/PDFs/InOrbit_2-pager_summary_Key-Features.pdf
- [R62] Foxglove Data Search and Curation (2026-04-21): https://www.businesswire.com/news/home/20260421818840/en/Foxglove-Launches-Unified-Data-Search-and-Curation-Platform-to-Accelerate-Physical-AI-Development

### MAPF · 교통 제어
- [R70] Sharon et al., Conflict-Based Search: https://ojs.aaai.org/index.php/AAAI/article/view/8140 (AIJ 2015판: https://www.researchgate.net/publication/278400742_Conflict-based_search_for_optimal_multi-agent_pathfinding)
- [R71] Barer et al., Suboptimal Variants of CBS (ECBS), SoCS 2014: https://ojs.aaai.org/index.php/SOCS/article/view/18315
- [R72] Ma et al., Searching with Consistent Prioritization (PBS), AAAI 2019: https://ojs.aaai.org/index.php/AAAI/article/view/4758
- [R73] Okumura et al., PIBT, AIJ 2022: https://arxiv.org/abs/1901.11282
- [R74] Okumura, LaCAM, AAAI 2023: https://arxiv.org/abs/2211.13432
- [R75] Liang et al., Real-Time LaCAM, SoCS 2025: https://arxiv.org/abs/2504.06091
- [R76] Li et al., Lifelong MAPF in Large-Scale Warehouses (RHCR), AAAI 2021: https://arxiv.org/abs/2005.07371
- [R77] Jiang et al., Scaling Lifelong MAPF to More Realistic Settings (2024): https://arxiv.org/abs/2404.16162
- [R78] WPPL (League of Robot Runners 2023 우승): https://github.com/DiligentPanda/MAPF-LRR2023
- [R79] Jiang et al., SILLM(Deploying Ten Thousand Robots): https://arxiv.org/abs/2410.21415
- [R80] Hönig et al., Persistent and Robust Execution of MAPF Schedules in Warehouses, RA-L 2019: https://www.amazon.science/publications/persistent-and-robust-execution-of-mapf-schedules-in-warehouses
- [R81] Berndt et al., Switchable Action Dependency Graph (2020): https://arxiv.org/abs/2010.05254
- [R82] Feng et al., A Real-Time Rescheduling Algorithm for Multi-robot Plan Execution, ICAPS 2024: https://arxiv.org/abs/2403.18145
- [R83] Zhang et al., Multi-Robot Coordination and Layout Design for Automated Warehousing, IJCAI 2023: https://arxiv.org/abs/2305.06436
- [R84] Zhang et al., Guidance Graph Optimization for Lifelong MAPF, IJCAI 2024: https://arxiv.org/abs/2402.01446
- [R85] Moorthy et al., Cyclic deadlock prediction and avoidance for zone-controlled AGV system (IJPE 2003, 초록 기준): https://www.sciencedirect.com/science/article/abs/pii/S0925527302003705 ; 관련 zone control·시간창 라우팅 설명(2차): https://www.mdpi.com/2076-3417/14/17/7817
- [R86] Bonetti et al., A traffic management system for large and heterogeneous vehicles in narrow industrial environments, IJRR 2026: https://arxiv.org/abs/2609.10400
- [R87] Vivaldini et al., Automatic Routing System for Intelligent Warehouses(관련 연구 절에서 Kim & Tanchoco 1991 시간 창 라우팅, Maza & Castagna 실시간 계층, Möhring et al. 확장 정리): https://arxiv.org/abs/2307.06893

### 안전 표준
- [R90] ISO 3691-4:2023 원문 샘플(iTeh): https://cdn.standards.iteh.ai/samples/83545/a3d9d057a08d4f9c8e8e87cdc947583c/ISO-3691-4-2023.pdf
- [R91] (2차) TÜV Rheinland, ISO 3691-4 whitepaper: https://www.tuv.com/content-media-files/master-content/services/industrial-services/pdf/tuv-rheinland-automatic-guided-vehicles-whitepaper-en_neu.pdf
- [R92] (2차) Fabrico, ISO 3691-4 Explained: https://www.fabrico.io/blog/iso-3691-4-driverless-industrial-trucks/
- [R93] ANSI/A3 R15.08-3-2026 (ANSI Blog): https://blog.ansi.org/ansi/ansi-a3-r15-08-3-2026-industrial-mobile-robot/
- [R94] ANSI/A3 R15.08-1-2020 (R2026) (ANSI Blog): https://blog.ansi.org/ansi/ansi-a3-r15-08-1-2020-r2026-mobile-robot-safety/
- [R95] (2차) The Robot Report, R15.08-2 발표 (2023-10): https://www.therobotreport.com/new-amr-safety-standard-available-with-release-of-ansi-a3-r15-08-2/
- [R96] ISO/FDIS 13482 단계(50.20, 2026-09-15): https://iss.rs/en/project/show/iso:proj:83498
- [R97] ISO 13482:2014: https://www.iso.org/standard/53820.html
- [R98] Marvel & Norcross, Implementing speed and separation monitoring (NIST, RCIM 2017): https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=914783
- [R99] SICK, The perfect size of the protective field on an industrial autonomous vehicle: https://www.sick.com/ch/en/the-perfect-size-of-the-protective-field-on-an-industrial-autonomous-vehicle/w/blog-size-protective-field-industrial-autonomous-vehicle
- [R100] (2차) Safety Laser Scanner for AGV & AMR guide: https://industrialsafetysensor.com/blog/safety-laser-scanner-for-agv-amr-guide/

### 사람 관측 · 예측
- [R110] Rudenko et al., Human motion trajectory prediction: a survey, IJRR 2020: https://journals.sagepub.com/doi/abs/10.1177/0278364920917446
- [R111] Kucner et al., Survey of maps of dynamics for mobile robots, IJRR 2023: https://journals.sagepub.com/doi/10.1177/02783649231190428
- [R112] TechCrunch, Amazon Robotic Tech Vest (2019-01-18): https://techcrunch.com/2019/01/18/amazon-built-an-electronic-vest-to-improve-worker-robot-interactions
- [R114] (2차) ELOKON ELOshield UWB: https://www.elokon.com/en-US/material-handling/eloshield-vehicle-pedestrian-proximity-detection

### 관제실 운영 · HMI · KPI
- [R120] PAS, Understanding and Applying the ANSI/ISA 18.2 Alarm Management Standard: https://www.isa.org/getmedia/55b4210e-6cb2-4de4-89f8-2b5b6b46d954/PAS-Understanding-ISA-18-2.pdf
- [R121] Kim VanCamp, Alarm System Performance Metrics (ISA-18.2 권고 목표치): https://isa.ie/wp-content/uploads/2016/06/Alarm_System_Performance_Metrics_Kim_Van_camp.pdf
- [R122] IEC 62682:2022: https://webstore.iec.ch/en/publication/65543
- [R123] (2차) Yokogawa, Implementing Alarm Management per IEC 62682: https://blog.yokogawa.com/blog/implementing-alarm-management-per-iec-62682-standard
- [R124] ISO 11064 시리즈(BSI): https://landingpage.bsigroup.com/LandingPage/Series?UPI=BS+EN+ISO+11064 , HSE Control rooms: https://www.hse.gov.uk/humanfactors/topics/control-room.htm
- [R125] (2차) ISA-101 HMI Design Standard guide: https://hmilibrary.com/standards/isa-101
- [R126] Endsley, Toward a Theory of Situation Awareness in Dynamic Systems, Human Factors 1995: https://journals.sagepub.com/doi/10.1518/001872095779049543
- [R127] Olsen & Goodrich, Fan-out: measuring human control of multiple robots, CHI 2004: https://dl.acm.org/doi/10.1145/985692.985722
- [R128] Chien, Lewis, Mehrotra, Scheduling Operator Attention for Multi-Robot Control, IROS 2012: https://www.ri.cmu.edu/pub_files/2012/10/Chien_IROS12_Final_Version.pdf
- [R129] Staniaszek et al., AutoInspect (MTBI 정의·분류), 2024: https://arxiv.org/abs/2404.12785
- [R130] IEC 60050-192 Dependability vocabulary (Electropedia): https://www.electropedia.org/iev/iev.nsf/index?openform=&part=192
- [R131] Overall equipment effectiveness: https://en.wikipedia.org/wiki/Overall_equipment_effectiveness

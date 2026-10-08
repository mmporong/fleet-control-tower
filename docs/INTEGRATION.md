# 연동

관제를 실제 브로커·차량·상위 시스템에 붙이는 방법입니다. 저장소에는 실행 형태가 둘 있습니다.

| 실행 형태 | 명령 | 현장 | 시간 | 용도 |
|---|---|---|---|---|
| 시뮬레이션 관제탑 | `python3 -m fleet.server` | 프로세스 안의 시뮬레이션(사람·지게차·카메라 포함) | 배속 가능, 시드로 재현 | 설계 검증, 벤치마크, 훈련, 시연 |
| 실운영 관제 서비스 | `python3 -m fleet.fms` | MQTT 브로커 건너편의 차량(실차, 벤더 시뮬레이터, `fleet.agvsim`) | 실시간 | 연동 시험, 파일럿 준비 |

두 형태는 같은 관제 코어(`FleetManager`), 작업 배정(`Dispatcher`), 운영 명령(`Operations`), 웹 화면을 씁니다. 실운영 서비스에서 달라지는 점은 다음과 같습니다.

- 메시지가 실제 MQTT 브로커를 지나고, 들어오는 VDA 5050 메시지를 공식 JSON 스키마로 검사합니다.
- 레이아웃을 VDMA LIF 파일로 받습니다.
- 작업·이벤트·감사 로그·차량 스냅숏을 SQLite에 저장하고, 재기동하면 재동기화합니다.
- 사람·지게차 인지(카메라 피드)와 고속문 제어기는 별도 장치의 몫입니다. 이 저장소의 `fleet.agvsim --doors`는 문 제어기를 모의하며, 카메라 피드 모의는 실운영 경로에 없습니다.

```
 ┌──────────── 차량 쪽 ─────────────┐       ┌──── 브로커 ────┐      ┌──────── 관제 서비스(fleet.fms) ────────┐
 │ 실차 / 벤더 시뮬레이터 /          │ MQTT  │ mosquitto 등    │ MQTT │ 스키마 검사 → FleetManager → Dispatcher │
 │ fleet.agvsim(차량 N대 + 고속문)   ├──────►│ VDA 5050 2.0    │◄─────┤ SQLite(작업·이벤트·감사·스냅숏)        │
 └───────────────────────────────────┘       └────────────────┘      │ HTTP: 화면·운영 명령·WMS API            │
                                                                     └───────────────▲────────────────────────┘
                                                                                     │ HTTPS 앞단(역방향 프록시)
                                                                       관제원 브라우저 · WMS(API 키)
```

## 1. 로컬에서 실행

Python 3.11 이상과 MQTT 브로커가 필요합니다. 브로커는 Docker의 mosquitto를 예로 듭니다.

```bash
cd ~/fleet-control-tower
docker run -d --rm --name fct-mqtt -p 127.0.0.1:1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf

# 관제 서비스: 첫 실행에 감독자 계정 admin의 비밀번호를 한 번 출력합니다(FCT_ADMIN_PASSWORD로 지정 가능)
python3 -m fleet.fms --broker 127.0.0.1:1883 --site sites/dc.lif.json --port 8096

# 다른 터미널: 차량 2대와 고속문 제어기 모의
python3 -m fleet.agvsim --broker 127.0.0.1:1883 --site sites/dc.lif.json --doors \
    --vehicle AGF-01:FORKLIFT:PA14_3 --vehicle AGF-02:FORKLIFT:PA19_9
# 브라우저에서 http://127.0.0.1:8096 → admin으로 로그인
```

끝낼 때는 두 프로세스를 Ctrl+C로 멈추고 `docker stop fct-mqtt`로 브로커를 내립니다.

`--doors` 없이 실행하면 문 제어기가 응답하지 않으므로, 관제는 두 고속문을 "상태 보고 없음(제어기 미연결)"으로 판정하고 경로에서 뺍니다. 그러면 방화벽을 건너는 작업(랙 → 출고 스테이징)이 끝나지 않습니다. 의도된 안전 측 동작입니다.

## 2. Docker로 실행

이미지 하나로 세 역할을 띄웁니다. 아래 명령은 2026-10-09에 이 저장소 이미지로 실행해, 주문 접수·멱등 중복·이벤트 조회·감사 체인 검증까지 확인했습니다.

```bash
cd ~/fleet-control-tower
docker build -t fleet-control-tower .
docker network create fct
docker run -d --name fct-mqtt --network fct eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
docker run -d --name fct-fms --network fct -p 127.0.0.1:8096:8095 -v fct-data:/data \
    --entrypoint python3 fleet-control-tower -m fleet.fms --broker fct-mqtt:1883 \
    --site sites/dc.lif.json --host 0.0.0.0 --port 8095 --db /data/fms.sqlite3
docker run -d --name fct-agv --network fct --no-healthcheck --entrypoint python3 fleet-control-tower \
    -m fleet.agvsim --broker fct-mqtt:1883 --site sites/dc.lif.json --doors \
    --vehicle AGF-01:FORKLIFT:PA14_3 --vehicle AGF-02:FORKLIFT:PA19_9
docker logs fct-fms          # 첫 실행의 admin 비밀번호
# 정리
docker rm -f fct-agv fct-fms fct-mqtt && docker network rm fct
```

- 관제 서비스는 컨테이너 안에서 8095번을 써야 이미지의 헬스체크가 맞습니다. 차량 시뮬레이터는 HTTP가 없으므로 `--no-healthcheck`로 띄웁니다.
- 계정·감사 로그·작업은 `/data` 볼륨에 남습니다. 볼륨을 지우면 새 admin 비밀번호가 만들어집니다.
- 기본 명령(`docker run -p 127.0.0.1:8095:8095 fleet-control-tower`)은 시뮬레이션 관제탑입니다.

## 3. MQTT

`fleet/mqtt.py`는 표준 라이브러리로 만든 MQTT 3.1.1 클라이언트입니다.

| 항목 | 내용 |
|---|---|
| 지원 | CONNECT(Last Will, 사용자 이름·비밀번호, TLS), PUBLISH·SUBSCRIBE QoS 0·1, keepalive, 지수 백오프 재접속, 재접속 뒤 구독 복원과 미확인 QoS 1 재전송(DUP) |
| 미지원 | QoS 2, MQTT 5 |
| 토픽별 QoS | `connection` QoS 1·retained(Last Will이 CONNECTIONBROKEN을 남김). 고속문 state는 QoS 0·retained. 나머지는 QoS 0(VDA 5050 2.0 §6) |
| 관제 자신의 상태 | `fleet/v1/fms/state`에 ONLINE·OFFLINE(retained, Last Will) |
| 스레드 | 네트워크 스레드는 받은 메시지를 큐에 넣기만 하고, 관제 주기가 꺼내 처리합니다. 관제 코어는 시뮬레이션과 같이 단일 스레드로 돕니다 |

브로커 인증과 TLS는 두 서비스가 같은 옵션을 씁니다. 비밀번호는 프로세스 목록에 남지 않도록 환경변수로만 받습니다.

```bash
FCT_MQTT_PASSWORD='...' python3 -m fleet.fms --broker mq.site.local:8883 --mqtt-user fms \
    --cafile /etc/fct/broker-ca.pem --site sites/dc.lif.json
```

- 인증이 거부되면(잘못된 비밀번호 등) 로그에 WARNING을 남기고 백오프로 재시도합니다. 사용자 인증을 켠 mosquitto 2.1로 접속 성공과 거부를 확인했습니다.
- TLS는 서버 인증서를 검증합니다(호스트 이름 포함). 자체 CA로 서명한 인증서의 mosquitto 2.1 TLS 리스너에 `--cafile`로 접속되는 것과, CA를 주지 않으면 인증서 검증 실패로 접속하지 않는 것을 확인했습니다. 클라이언트 인증서(상호 TLS)는 `ssl.SSLContext`를 넘기는 코드 경로만 있고 명령줄 옵션은 없습니다.
- 운영 브로커와의 호환은 `tools/mqtt_interop_check.py`로 점검합니다. 접속, 왕복, 와일드카드, retained, 소켓 끊김 시 Last Will, 정상 종료 시 Will 미발행, keepalive, 100 KiB 페이로드, 재접속·재구독의 9개 항목입니다. mosquitto 2.1의 익명·사용자 인증·TLS 세 구성에서 모두 9/9 통과했습니다.

```bash
python3 tools/mqtt_interop_check.py --host 127.0.0.1 --port 1883 [--username U --password P] [--cafile CA]
```

## 4. VDA 5050 적합성

기준은 VDA 5050 2.0.0입니다. 공식 JSON 스키마 6종을 바이트 그대로 저장소에 두고(`fleet/schemas/vda5050-2.0.0/`, 출처는 그 폴더의 SOURCE.md), 표준 라이브러리 검증기(`fleet/schema.py`)로 검사합니다.

| 항목 | 동작 |
|---|---|
| 수신 검사 | 실운영 서비스는 들어오는 모든 state·connection·factsheet·visualization을 스키마로 검사합니다. 위반 메시지는 버리고 차량별로 셉니다(`invalid_msgs`). 관제 주기를 멈추지 않습니다 |
| 송신 검사 | 관제와 차량 모델이 보내는 메시지 전체가 스키마를 통과하는지 시험으로 고정했습니다(`test_every_published_message_passes_the_official_schema`) |
| 2.0.0 정오표 | 2.0.0 태그의 스키마에는 명세 본문과 다른 곳이 있습니다(`actionName`→`actionType`, `allowedDeviationXy`→`XY`, 운영 모드 `PAUSED`). 검증기는 이 셋만 명세 본문을 따르도록 고친 규칙(`SPEC_ERRATA`)을 쓰고, `errata=False`면 원래 스키마 그대로 검사합니다 |
| 선택 필드 | `velocity`, `agvPosition`, `loads` 같은 선택 필드가 없어도 동작합니다. 위치가 없으면 마지막 노드로 판단합니다 |
| 차량 능력 | factsheet의 `protocolFeatures.agvActions`로 차량이 지원하는 액션을 봅니다. factsheet가 오기 전에는 표준 액션만 쓴다고 가정하고 `factsheetRequest`를 보냅니다 |
| 표준 폴백 | 확장(`x_`)을 지원하지 않는 차량에는 표준만 씁니다. 일괄 정지는 `startPause`, 구역 감속은 order 간선의 `maxSpeed`, 보호정지 판정은 `safetyState.fieldViolation`입니다. 관제 화면의 차량 상세에 "표준 폴백" 목록이 보입니다 |
| 다중 제조사 | `uagv/v2/+/+/…`로 구독하므로 제조사 이름이 달라도 받습니다. 차량 식별은 (제조사, 일련번호) |
| order·action id | 실행 ID를 접두사로 붙여(`AGF-01-4ca035-O3`), 재기동한 관제가 차량이 이미 본 id를 다시 쓰지 않습니다 |
| 액션 파라미터 | pick·drop에 `lhd`(FORKS)·`side`·`stationName`·높이를 넣고, 소요 시간은 차량이 정합니다 |

구현하지 않은 것: VDA 5050 2.1·3.0의 신규 항목(zone·map 배포, 회랑, 응답 메시지), QoS 2.

## 5. 레이아웃(VDMA LIF)

실운영 서비스와 차량 시뮬레이터는 VDMA LIF 1.0.0 파일로 레이아웃을 받습니다(`fleet/lif.py`).

- LIF에 담기는 것: 노드, 방향 간선, 차종별 통행 가능 여부와 최고 속도, 스테이션(정차점과 그 자리에서 하는 액션).
- LIF에 자리가 없는 것: 구역, 랙, 카메라 범위, 고속문, 벽, 정차 금지 노드, 노드 종류. LIF 1.0.0 문서가 통합자 데이터는 별도 문서로 두라고 하므로 사이드카 파일(`*.site.json`)에 같은 id로 둡니다.
- 기동할 때 레이아웃을 검증합니다(차종별 강연결, 도달 불가 노드). 실패하면 서비스가 뜨지 않습니다.

시뮬레이션 레이아웃에서 LIF와 사이드카를 다시 만드는 명령입니다.

```bash
cd ~/fleet-control-tower
python3 tools/export_lif.py --site dc --out sites/dc.lif.json      # sites/dc.site.json도 함께 생성
```

레이아웃 파일 변경은 위험성 평가 갱신 대상입니다([SAFETY.md](SAFETY.md) 6절).

## 6. WMS 연동

| 순서 | 방법 |
|---|---|
| API 키 발급 | `python3 -m fleet.fms --site sites/dc.lif.json --create-api-key wms-main`(키는 한 번만 출력, 저장소에는 해시만) |
| 주문 | `POST /api/wms/orders`, 헤더 `Authorization: Bearer <키>` |
| 이벤트 | `GET /api/wms/events?after=<마지막 seq>`로 커서를 넘기며 읽음(한 번에 200건) |

주문 본문은 다음과 같습니다. `order_id`가 멱등 키입니다. 같은 id·같은 내용이면 기존 작업을 돌려주고(200), 새 주문이면 201, 같은 id에 다른 내용이면 409입니다. 멱등 키는 SQLite에 있어 재기동 뒤에도 유지됩니다.

```json
{"order_id": "W-20261009-0001", "kind": "TRANSPORT", "source": "R1-03", "dest": "SO2", "priority": 0}
```

| kind | 동작 |
|---|---|
| `TRANSPORT` | `source`에서 pick, `dest`에서 drop |
| `MOVE` | `source`로 이동만(`dest` 생략) |

이벤트는 작업 수명주기 순서로 나갑니다: `CREATED` → `ASSIGNED` → `STOP_DONE`(정차점마다) → `COMPLETED` 또는 `FAILED`·`CANCELED`. 고장으로 작업이 대기열로 돌아가면 `REQUEUED`, 현장 예외는 `EXCEPTION`(물류센터 공정: `PICK_RETRY`, `SLOT_OCCUPIED`, `PICK_FAILED` 등)입니다. 모든 이벤트에 `seq`, 작업 id, 주문의 `ref`(= `order_id`), 차량이 들어 있습니다. 실운영 서비스의 이벤트는 SQLite에 쌓이므로, 관제가 재기동해도 WMS는 마지막 `seq`부터 이어 읽을 수 있습니다(최소 한 번 전달, 중복 제거는 `seq`로).

## 7. 인증과 권한

`fleet/auth.py`가 두 서버(`fleet.server`, `fleet.fms`)에 공통으로 적용됩니다.

| 역할 | 할 수 있는 일 |
|---|---|
| viewer | 화면·상태·감사 로그 조회 |
| operator | 운영 명령(알람 확인, 일시정지, 재할당, 구역 봉쇄, 일괄 정지 등) |
| supervisor | 위에 더해 훈련 주입, 시뮬레이션 제어, 화면에서 WMS 주문 |
| wms(API 키) | WMS 주문과 이벤트 조회만 |

| 항목 | 값 |
|---|---|
| 비밀번호 | 10자 이상, PBKDF2-HMAC-SHA256 20만 회, 임의 salt |
| 잠금 | 5회 실패 시 5분 |
| 세션 | HttpOnly·SameSite=Strict 쿠키 `fct_session`, 토큰은 SHA-256만 저장, 최대 8시간(한 교대), 30분 무활동 시 만료 |
| 상태 변경 요청 | `X-CSRF-Token`(세션 토큰), JSON 본문, Origin이 있으면 Host와 같아야 함 |
| 감사 로그 | 인증된 계정 이름으로 기록(클라이언트가 보낸 이름을 믿지 않음). SQLite에 해시 체인으로 저장, `GET /api/audit/verify`로 변조 검사 |
| 계정 추가 | `FCT_NEW_PASSWORD=... python3 -m fleet.fms --site sites/dc.lif.json --add-user kim:operator` |
| 개발용 | `--no-auth`는 루프백 주소에 묶을 때만 허용 |

서버는 평문 HTTP만 엽니다. 현장에서는 TLS를 종단하는 역방향 프록시 뒤에 두고 `--secure-cookie`로 세션 쿠키에 Secure 속성을 붙입니다.

## 8. 재기동과 복구

| 저장 항목(SQLite) | 쓰임 |
|---|---|
| 작업 | 대기·진행 중 작업을 재기동 뒤 되살림 |
| 이벤트(WMS outbox) | 재기동 뒤에도 커서로 이어 읽기 |
| 차량 스냅숏 | 차량별 order id, released 노드, 마지막 노드 |
| 멱등 키, 계정, 세션, API 키, 감사 로그 | 6·7절 |

재기동 절차(RESYNC)는 자동입니다.

1. 마지막 스냅숏에서 각 차량이 아직 달릴 수 있는 구간(released base)을 예약으로 먼저 되살립니다. 차량이 아무 말도 하기 전에 다른 차량이 그 자리를 받지 않게 하려는 것입니다.
2. 차량마다 첫 state를 기다리며 신규 배정을 멈춥니다(기본 15초).
3. state를 받으면 대조합니다. 차량이 이전 order를 계속 달리면 이어받고, 보내지 못한 order는 다시 보내고, 관제가 모르는 order는 취소한 뒤 작업을 대기열로 돌립니다. 적재물을 실은 채 끊긴 작업은 FAILED로 두고 현장 확인을 요청합니다.
4. 모든 차량이 대조되거나 시간이 지나면 정상 운영으로 돌아갑니다.

관제 주기 안에서 예외가 나면 `FMS_INTERNAL_ERROR`(1순위) 알람을 올리고 전체를 일시정지(HOLD)로 둔 채 주기를 계속 돕니다. 차량은 받은 base 끝에서 멈춥니다. 원인을 확인한 뒤 운영자가 재개합니다.

## 9. 한계

- 관제 서비스는 단일 프로세스입니다. 이중화(대기 서버), 데이터베이스 복제, 다중 관제석 동기화는 없습니다.
- 사람·지게차 인지 피드(카메라 서버)의 실운영 입력 형식은 정하지 않았습니다. 시뮬레이션에서만 들어옵니다.
- 고속문 메시지는 Open-RMF DoorRequest·DoorState 형식을 따른 이 프로젝트의 토픽입니다. 실제 문 제어기(PLC)에는 이 형식으로 바꾸는 어댑터가 필요합니다.
- 실차와의 연동 시험은 하지 않았습니다. 확인한 상대는 이 저장소의 차량 시뮬레이터와 mosquitto 브로커입니다.

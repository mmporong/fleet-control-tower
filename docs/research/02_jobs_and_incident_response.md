# 로봇 관제(FMS·Fleet Operations) 직무 리서치 — 채용 요구와 이상 상황 대처 실무

- 작성일·확인일: 2026-09-29 (모든 공고의 "확인" 시점은 이 날짜)
- 범위: A. 채용 공고 53건(한국 33, 해외 20) 수집·집계 / B. 상용 FMS·AMR 문서 기반 이상 상황 대처 흐름 / C. 포트폴리오 결론
- 제외 범위(다른 에이전트 담당): Open-RMF·VDA 5050 세부, MAPF 알고리즘 비교, 안전 표준 해설. 여기서는 채용 빈도와 운영 흐름 안에서 필요한 만큼만 언급했다.

## 0. 핵심 결론 먼저

1. **채용 공고에서 가장 많이 등장한 요구는 "장애 대응·트러블슈팅·에스컬레이션"(53건 중 26건)** 이다. 운영형뿐 아니라 개발형 공고도 "장애 분석·운영 이슈 대응", "예외·복구 흐름", "통신 단절 시나리오 통합 테스트"를 요구한다. 해피 패스만 도는 관제 데모는 현업 요구와 어긋난다.
2. **한국 개발형 공고의 중심은 "상위 시스템(WMS·WCS·MES) 연동 + 산업 프로토콜 + MQTT/REST/DB"** 이다(한국 33건 중 상위 연동 16, 산업 프로토콜 11, DB 12, MQTT 10). 알고리즘(MAPF·교통 제어)은 소수지만 핵심 포지션(클로봇 CROMS, 리보틱스 FMS, 레인보우 멀티로봇, 아마존 Fleet Autonomy)에서 명시적으로 요구된다.
3. **Open-RMF를 요건으로 적은 공고는 이 표본에서 0건, VDA 5050은 4건(모두 한국)** 이다. 표준 이름보다 "이기종 로봇·설비를 어댑터로 붙이는 구조"와 "현장 연동 경험"이 더 자주 요구된다.
4. **상용 FMS 문서(MiR Fleet, OTTO)는 충돌 관리를 3계층으로 나눈다**: (a) 관제 서버의 자원·구역 예약과 교착 감지, (b) 로봇 간 footprint 공유 같은 fleet 수준 회피, (c) 로봇 자체 안전 스캐너의 보호정지. 관제는 (c)를 대체하지 않는다.
5. **한국 실외이동로봇 운행안전인증 고시(2025-11-24 개정)는 "관제장치" 요구사항을 명문화**했다: 전 로봇 식별, 위치·속력·배터리·통신 감도·상태 실시간 표시, 통행 불가 시 관제 통보와 관리자 조치, 원격 정지 수단, 원격 수동조작 시 영상 확인. 국내 관제 포트폴리오의 최소 체크리스트로 쓸 수 있다.

---

## A. 채용 공고 조사

### A-0. 방법과 한계

- 수집 경로: 원티드 공개 API(`/api/v4/jobs/{id}`, `/api/chaos/search`), 사람인 검색·상세(`relay/view-detail`), Ninehire(클로봇) API, GreetingHR 페이지 데이터, Greenhouse·Ashby·Lever 공개 API, 아마존 잡스 검색 JSON, 각 사 채용 페이지. 이미지로만 된 공고(레인보우로보틱스·삼익THK·벰로보틱스·스피드플로어·클로봇 일부)는 이미지를 내려받아 판독했다.
- 검색 키워드: 로봇 관제, 관제 시스템, FMS, ACS, RCS, Fleet Management, 원격관제, 로봇 운영, 멀티로봇, 교통 관제 등.
- **한계(중요)**: 표본은 키워드 검색으로 모은 것이라 무작위 표본이 아니다. 빈도는 "이 53건 안에서의 등장 횟수"이며 시장 점유율로 읽으면 안 된다. 해외 표본에는 자율주행차(Waymo·Nuro) 운영 조직이 포함돼 있어 안전·교대 근무·문서화 비중이 높게 나온다.
- 원문 접근이 막혀 검색엔진 요약에 의존한 공고는 표에 **(요약 기반)** 으로 표시했다: K24 토르드라이브, K31 쿠팡, O18 스타쉽, O19 MiR.
- 직군 구분: **운영형**(관제 모니터링·현장 대응·운영 관리), **개발형**(관제 시스템·플랫폼 개발), **혼합**(개발+현장 커미셔닝/운영).

### A-1. 한국 공고 (33건)

| ID | 회사 | 직무명 | 직군 | 주요 업무 | 자격요건 | 우대사항 | 기술 스택 | 링크 | 상태(확인 2026-09-29) |
|---|---|---|---|---|---|---|---|---|---|
| K1 | 클로봇 | 모바일로봇 교통 관제 시스템 설계 및 개발자 (CROMS개발팀) | 개발 | MAPF 알고리즘 설계·구현·최적화, 다수 로봇 경로 충돌 방지·경로 배정 교통 관제 로직 고도화 | 석사(신입) 또는 경력 2년+, 알고리즘 설계·최적화, 자료구조·경로 탐색, Java 등 OOP | 로봇·AI, MAPF 연구·실무, 대규모 멀티로봇·물류 자동화 이해 | Java, FMS | [Ninehire](https://clobotroas-recruit.com/job_posting/x18gaBlY) | 게시 2025-07-07, 마감 2026-01-05. 1차 면접 후 실무 과제, 2차 PT 가능 |
| K2 | 클로봇 | 시스템 연동 플랫폼 아키텍트 (선행기술개발팀) | 개발 | Cloud/On-Prem/Edge 배치·연동 아키텍처, REST·Message Broker 기반 이종 장비 인터페이스, WAS·DB·브로커 배치, 연동 어댑터·공통 모듈, CI/CD·장애 대응 체계 가이드, 통합 과정 성능·장애 분석 | 6년+, 아키텍처 의사결정, Linux/Windows·Docker, 분산 환경 REST/MB 연동, CI/CD, 실환경 연동·장애 분석·안정화 | 로봇·IoT 이종 장비 연계, 미들웨어·SI 아키텍처, Kubernetes, 고가용성 | REST, Message Broker, Docker, K8s | [Ninehire](https://clobotroas-recruit.com/job_posting/ab4C1y8t) | 게시 2026-09-21, 진행 중 |
| K3 | 클로봇 | 로봇 미들웨어 SW 개발 리더 (서비스개발팀) | 개발 | 팀 리딩, 로봇 미들웨어 SW, 로봇 HW 검수·품질, 현장 이행 프로젝트 | 리딩 경험, AMR·SW 아키텍처, 백엔드/프론트엔드 설계, CAN·RS232·RS485·Modbus | JIRA, Git·CI/CD, Linux·Docker, C# | C#, CAN, Modbus | [Ninehire](https://clobotroas-recruit.com/job_posting/YuFpbLbp) | 게시 2026-09-02, 진행 중 |
| K4 | 클로봇 | 로봇 필드 엔지니어 | 운영 | 필드 구축·셋업, **서비스 시나리오 수행 관제**, 로봇 제어·매핑, 운용 매뉴얼, 유지보수, 현장 이슈 대응·이슈 리포트 | 신입 가능, 초대졸+, 운전면허 | 로봇 제어 전공, 셋업 경험, Linux·Python·Git, 태블릿 제어 | Linux, Python | [원티드 201676](https://www.wanted.co.kr/wd/201676) | 마감 |
| K5 | 우아한형제들 | 자율주행 로봇 관제 및 현장 지원 | 운영 | 본문 상세 미기재 | 경력 무관 | 미기재 | 미기재 | [잡코리아](https://m.jobkorea.co.kr/Recruit/GI_Read/49142297?PageGbn=MST) | 2026-05-11 게시, 마감. 계약직 7개월, 연 2,800만원 이상 |
| K6 | 폴라리스쓰리디 | 자율주행로봇 관제 시스템 백엔드 (서울) | 개발 | Spring Boot 백엔드, REST API·외부 연동, DB 모델링·성능 개선, AWS 운영 | Spring Boot 3년+, REST, Git, RDBMS·SQL 튜닝, **MQTT 브로커(Mosquitto) 연동**, Nginx·Tomcat | React/Vue, AWS, Docker·CI/CD, Grafana·ELK·Prometheus, **VDA5050 AMR 연동·OPC-UA**, 데이터 레이크 | Java, Spring Boot, MySQL/PostgreSQL, MQTT, AWS | [원티드 382969](https://www.wanted.co.kr/wd/382969) | 진행 중 |
| K7 | 로아스 | 모바일 로봇 웹 관제 서비스 SW 파트리더 (ARQOS) | 개발(리더) | FE/BE/API/DB/실시간 통신 아키텍처, Cloud/On-Prem/Edge, **Multi-Site·Multi-Robot 확장 설계**, 모듈 표준화 | 7년+, 조직 리딩, 웹 플랫폼, REST·WebSocket 인터페이스 설계 | AMR/AGV, SCADA·MES·IIoT, MSA·EDA, WebGL·Three.js, K8s·Docker·Edge | REST, WebSocket, Three.js | [원티드 380740](https://www.wanted.co.kr/wd/380740) | 진행 중 |
| K8 | 리보틱스 | FMS System Engineer | 개발 | **MAPF, 충돌·교착·병목 예방 경로·자원 할당**, 제약 기반 스케줄링, WMS/MES 연동(VDA5050·MQTT·OPC UA), 시뮬레이션·실로봇 검증, 현장 FMS·트래픽 제어 문제 분석·개선 | 멀티로봇·FMS 실무 3년+, C++ 필수·Python, A*/Dijkstra, LP/CP, 그래프·조합최적화 | OR-Tools·Gurobi·CPLEX, AI 예측 스케줄링, 대규모 현장 적용, RTOS, 석사 | C++, Python, VDA5050, MQTT, OPC UA | [원티드 379576](https://www.wanted.co.kr/wd/379576) | 진행 중 |
| K9 | 랩투마켓(Machine-Wise) | 로봇/물류 자동화 플랫폼 개발 (웹, Node.js) | 개발 | TS 백엔드/풀스택, **WCS·ACS·RMS** 개발, 장비 인터페이스(TCP/IP·PLC·MQTT·SECS/GEM), MES·RTD 연동, 화면 개발 | 전문연구요원 신규편입 대상, Node.js·TS | 물류 솔루션·사이트 적용, 장비 제어 | TypeScript, Node.js, PostgreSQL, Angular | [원티드 372864](https://www.wanted.co.kr/wd/372864) | 진행 중 |
| K10 | 현대자동차그룹 | [제조로보틱스] 휴머노이드 로봇 운영 솔루션 개발 | 개발 | 로봇-현장 연계 아키텍처·표준 인터페이스, HTTP/gRPC/TCP/Modbus 어댑터, **작업 실패·결품·인식 오류 등 예외 원인 판단·대안 제시 AI Agent**, 작업 재할당·우선순위 오케스트레이션, LLM 이상 분석·알림, ISO 13849·IEC 61508 기반 위험 감지·자동 긴급정지, 실시간 이벤트 모니터링·알림, **통신 단절 등 예외 시나리오 통합 테스트**, Mock/Simulator 사전 검증 | 학사+, 미들웨어·플랫폼 5년+, 영어, 소켓·비동기·MQ, 좌표계·제어주기 소통 | 석사, 현장 구축·운영, LLM 에이전트, 실시간 작업 할당 알고리즘 | gRPC, Modbus, MQ, LLM | [원티드 387499](https://www.wanted.co.kr/wd/387499) | 진행 중 |
| K11 | 도구로보틱스 | 자율주행로봇 원격관제·모니터링 운영지원 (4조 3교대) | 운영 | 운영상태 원격 모니터링, **이상상황 1차 조치 및 보고**, 개발·현장 담당자 지원 요청, **조치 후 정상화 확인·인수인계** | 학력·경력 무관, PC 사용, 교대·야간 가능, 절차대로 꼼꼼히 수행 | - | - | [사람인 55087389](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=55087389) | 접수 2026-09-21~10-21, 정규직, 대구 |
| K12 | 도구로보틱스 | 서비스 운영 매니저 (RDE팀, 대구) | 운영(현장) | 설치·셋업·검증, Camera·LiDAR·센서·네트워크 캘리브레이션, Linux/ROS 오류 분석·현장 트러블슈팅, **로그 수집·이슈 재현·원인 분석**, PoC·유지보수·기술 리포트 | 신입 가능, 공학 학사, HW·센서·SW 통합 디버깅, 출장·운전 | Linux·ROS/ROS2, Git·Jira·원격 디버깅, 현장 셋업 | Linux, ROS | [사람인 54684653](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54684653) | 접수 2026-08-07~10-06 |
| K13 | 모빈(현대차 스핀오프) | 로봇 시스템 오퍼레이팅 엔지니어 (Robot Fleet Operator) | 운영(엔지니어) | 자율주행 시스템 운영·현장 기술지원, **주행 로그 분석·문제 재현·원인 파악**, 로봇·관제 시스템·서비스 개선점 리포트, 센서·HW 점검, 위치추정·경로계획·주행제어 모듈 테스트, 데이터 획득 | 대졸+, 전기전자·기계·컴퓨터·제어 전공 | ROS/ROS2·Git·Docker, Linux, SLAM·Localization·Navigation | ROS2, Docker | [사람인 54783646](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54783646) | 본문 표기 "2025년 8월 19일(수)~10월 16일(금)". 표기된 요일은 2026년 달력과 일치해 연도 오기 가능성이 있음. 2026-09-29 사람인 검색 목록에 노출 중 |
| K14 | 삼익THK | 로봇 백엔드 개발 엔지니어 | 개발 | 로봇·AMR·설비 **통합 관제 시스템(RCS)** 백엔드 API(FastAPI·gRPC), ROS2↔상위 시스템 인터페이스(rosbridge/rclpy), MES·LIMS·ERP 연동 API·SECS/GEM, DB 설계·구축, Git CI/CD·Docker | 학사, Python/Java/C# REST 서버, SQL(JOIN·GROUP BY·서브쿼리), RDBMS, Git | FastAPI/DRF, PostgreSQL 고급(인덱스·EXPLAIN·파티셔닝), docker-compose, C#/.NET PLC 장비 통신, SECS/GEM, OPC UA·MQTT·TimescaleDB, gRPC+Protobuf. **필요 역량: kHz 고빈도 데이터 트래픽 관리, 장시간 무중단 운용의 예외·타임아웃·재시도 처리, API 명세·DB 스키마 변경 이력관리** | Python, FastAPI, gRPC, ROS2, PostgreSQL, Docker | [사람인 55036947](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=55036947) | ~10/29 표기, 평택 |
| K15 | 벰로보틱스 | 물류로봇 운영 담당자 (부산 / 천안·아산) | 운영 | AGV/AMR 설치·셋업(주행경로·스테이션·충전 위치), **ACS로 운행상태·이송작업 모니터링, 알람·장애 1차 대응, 개발/제어 담당 전달, 장애내용·조치결과·인수인계 기록·공유**, 해외 관제시스템 설치·인터페이스 테스트·시운전·운영자 교육 | 중장기 현장 근무, 현장상황 기록·공유, 협업, 안전수칙 준수 | 미국 B1 비자, AGV/AMR 설치·운영, ACS·WCS·WMS·MES 사용 경험 | ACS, WCS/WMS/MES | [사람인 55065532](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=55065532) / [55065279](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=55065279) | ~11/16 표기 |
| K16 | 레인보우로보틱스 | 자율주행팀 멀티로봇 SW 개발 엔지니어 (모바일로봇사업부) | 개발 | Global/Local Path Planning, **Multi-AMR 제어 시스템**, 상태·작업 조건 고려 작업 할당·경로 계획, **교차로·협소 구간 다중 로봇 교통 제어, 충돌·정체·교착 방지·복구**, 대수·물류량 증가 대응 확장성, Fleet 모듈·인터페이스, 시뮬레이션·실환경 검증·현장 트러블슈팅 | 석사+, 2년+, C/C++, Linux, 자료구조·알고리즘·네트워크, **로그·운영 데이터 기반 문제 분석**, 고객 현장 투입 | Multi-AMR·Fleet Management·물류 관제, 스케줄링, 교착 해결, 운영 시뮬레이션, 분산·실시간, 상위 시스템·물류 설비 연동, 현장 배포·장애 분석, 코드리뷰 | C/C++, Linux | [사람인 54949131](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54949131) | 채용 시 마감, 세종 |
| K17 | 레인보우로보틱스 | Web 백엔드 개발 | 개발 | **ACS/FMS 및 로봇 플랫폼** 설계·개발·유지보수, 웹 백엔드·API, ERP·MES·설비 연동 인터페이스, DB 설계·성능, 서버 아키텍처·안정성, **장애 분석·운영 이슈 대응**, 코드리뷰 | 학사, 컴퓨터공학, 5년+, Python/Node.js, REST·WebSocket, MySQL/PostgreSQL, Linux, Git | AMR·AGV·물류, ACS·FMS·WCS, MSA, **MQTT·Zenoh·Kafka**, Docker·K8s, 실시간·HA, 멀티스레드, 프론트엔드 | Python, Node.js, WebSocket, MQTT, Zenoh, Kafka, K8s | [사람인 54650330](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54650330) | 채용 시 마감. **포트폴리오 제출 필수(실제 수행 프로젝트 위주)** |
| K18 | 레인보우로보틱스 | Web 프론트 개발 (과장) | 개발 | React 로봇 탑재 UI, Three.js 시각화, **다중 로봇 관제·로봇 제어·작업 관리 UI와 실시간 데이터 연동**, 프론트 아키텍처·표준 | 학사, 7년+, React·TS, HTML/CSS/JS | Three.js 3D·로봇/장비 UI, **WebSocket 실시간 관제·대시보드** | React, TypeScript, Three.js, WebSocket | [사람인 54999399](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54999399) | ~10/23 표기 |
| K19 | 로보티즈AI | RX 시스템 아키텍처 엔지니어 | 개발(아키텍처) | 공정을 설비 상태·로봇 행동·품질·**예외·복구 흐름**으로 모델링, **성공률·사이클타임·가동률 Acceptance 기준**, 공통 런타임·상태 모델·표준 API·어댑터, 오케스트레이션·**오류 감지·복구·로그·관측·원격 진단·개입·배포/롤백**, 시뮬레이션·Reference Cell 사전 검증, 회귀시험 | SI·제조 자동화 통합·검증, 이종 시스템 연동, 상태기계·오케스트레이션·자동화 시험 설계 | FAT·SAT, 복구 시간·OEE를 요구사항으로 설계, ROS 2·ros2_control, HMI·MES·FMS, OPC UA·Modbus TCP, SIL·HIL·엣지 배포·원격 진단·롤백 | ROS 2, OPC UA | [사람인 54769632](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54769632) | 채용 시 마감, 마곡 |
| K20 | 인아텍앤코포 | 스마트팩토리 로봇관제 S/W 개발자 | 개발 | 로봇-FMS/ACS 통신 프로토콜, **VDA5050 MQTT 어댑터**, 로봇 진단·상태 보고·원격 명령 수신, REST/gRPC 연동, MCS·MES·WMS 연계, **시뮬레이션 환경·테스트 자동화**, 필드 테스트 | 학사+, Python/Java/C#/JS 중 하나, MQTT·WebSocket·REST·gRPC 중 하나, 상태관리·작업 스케줄링 이해, API 서버, Git | FA, MCS/MES/WMS, Modbus·OPC UA, 제조사 로봇·ROS API/SDK 제어, VDA5050 v2.0+ 실무, Spring Boot/ASP.NET Core, CI/CD | C#, Java, Python, MQTT, gRPC | [사람인 54899181](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54899181) | 마감 2026-09-30, 인천 |
| K21 | 시스콘로보틱스 | 로봇 소프트웨어 개발(응용) | 개발 | **AMR 제어소프트웨어(ACS)** 개발, AR/MR/VR SW, MES 개발·연동 | 컴퓨터공학 학위, SW 3년+, C#·C/C++ | 모바일, 프론트(Unity3D·WPF), 백엔드(Spring·C#.NET·AWS/Azure), DB, TCP/IP, Linux·ROS, Python | C#, C++, Java, ROS | [사람인 55150773](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=55150773) | 마감 2026-10-28, 성남 |
| K22 | 모비어스 | [Robotics] 통합 관제 프로그램 개발 및 운영 (경력) | 혼합 | 물류로봇 관제 프로그램 개발·운영, 관제시스템-타 시스템 I/F, 신규 기능·유지보수 | 전문학사+, ACS·MCS·MES·WMS 개발·운영 2~10년, JS·Vue·Python·C# | ACS/MCS, MES/WMS 구축, 스마트팩토리, PLC 통신 | JavaScript, Vue, Python, C# | [GreetingHR](https://mobyus.career.greetinghr.com/ko/o/154677) | 게시 2025-04-22, 부산 |
| K23 | 스피드플로어 | 로봇 소프트웨어 엔지니어 (Robotics Software·Backend) | 혼합 | 무빙플로어·도킹스테이션·**무인지게차**·AMR·AGV·컨베이어 통합 제어·데이터 연동 시나리오, 다기종 벤더 인터페이스(RCS·WCS·PLC·필드버스), 현장 커미셔닝·시운전·안정화, **안전 인터록·비상정지·회전율 KPI(Pallet/Hour) 검증·실증 데이터 분석** | 산업용 로봇 현장 세팅·커미셔닝, **다수 AGV 군집 운영(Fleet Management)**, PLC·Modbus·EtherCAT·MQTT 실무 | ROS2·벤더 RCS↔WCS·WMS 연동, 물류 자동화, ACS/FMS 운영, **VDA 5050**, **교착 해결·트래픽 제어·경로 최적화**, 현장 트러블슈팅 | ROS2, PLC, Modbus, EtherCAT, MQTT, VDA 5050 | [사람인 55062561](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=55062561) | ~11/17 표기 |
| K24 | 토르드라이브 | FMS 설계 및 개발 SW 엔지니어 (전문연구요원 가능) **(요약 기반)** | 개발 | 고객사 AGV·AMR 통합 관제, 차량 정보·권한·API 서버, Go 서버 간 gRPC, React 웹 대시보드, gRPC·MQTT·WebRTC 차량 통신 | Debian/Ubuntu, C++ 또는 Go, 컴퓨터 구조·OS·네트워크·보안, Docker | 확인 못함 | Go, gRPC, React, MQTT, WebRTC, Docker | [thordrive.team](https://www.thordrive.team/recruit/fms) | 원문 접속 실패(DNS), 게시일 미상 |
| K25 | ㈜하다 | 과수방제로봇 FMS(관제 시스템) 및 클라우드 플랫폼 개발자 | 개발 | 관제·클라우드 플랫폼(상세 미기재) | 3년+, 대졸+, C·Linux·ROS2·SW 아키텍처·CodeSys·GPU·CUDA | 관련 자격증, 유관 5년+ | C, ROS2, CodeSys, CUDA | [잡코리아](https://www.jobkorea.co.kr/Recruit/GI_Read/48283996) | 2025-12-23~2026-01-22, 마감 |
| K26 | 유진로봇 | GoCart 팀 Front-end / Back-end / DevOps | 개발 | FE: 멀티 로봇 서비스 설정·모니터링·보고 UI / BE: **로봇 상태·작업 관리·감독 SW**, 리소스 관리, **IoT 디바이스(엘리베이터·자동문·센서) 연동** / DevOps: CI/CD·테스트 자동화·서버 모니터링 | FE: HTML·JS·CSS, Angular/Vue/React / BE: JS(ES6+), Node.js API, DB | 풀스택, Linux, REST·네트워크·보안, 클라우드, Ansible·Docker/K8s | Angular, D3.js, Node.js, Express, MySQL·MongoDB | [GoCart Careers](https://yujinrobot.github.io/GoCart-Careers/) | 게시일 미표기 |
| K27 | 베어로보틱스 | Senior Platform Engineer (ACS) | 개발 | **ACS(dispatch·traffic control·monitoring 플랫폼)** — A: 온프레미스 k3s 배포, Ansible, Envoy, **장애 대응·observability** / B: MQTT·gRPC 데이터 수집 파이프라인, MySQL/PostgreSQL, Go API, **Grafana 모니터링·alerting**, 아카이빙 | A: Platform/DevOps 5년+, K8s/k3s, Ansible, Linux, Go/Python / B: 백엔드 4년+, MQTT·Kafka 이벤트 기반, REST/gRPC. 영어 | 로보틱스·실시간 운영, EMQX, Grafana, 고객사 배포 | Go, k3s, Ansible, Envoy, MQTT(EMQX), gRPC, Grafana | [Breezy](https://bear-robotics.breezy.hr/p/e4bc781179c4-senior-platform-engineer-acs) | 진행 중, 성수 |
| K28 | 베어로보틱스 | Field Software Engineer (Senior/Junior) | 혼합 | 고객 WMS·WCS·MES·MCS 분석·연동 요구 정의, **ACS-상위 시스템 Middleware**, 운영 프로세스 business logic, **설비(도킹스테이션·컨베이어·엘리베이터·도어) integration**, 맵 설정·HW 트러블슈팅, 현장 상주, **사이트별 문서화·운영 이관**, 반복 패턴을 core 팀에 피드백 | SW 4년+, Python/Go/Java, REST/gRPC 연동, 상위 시스템 연동, 출장·상주 | 물류·제조, AMR/AGV, MQTT·EDA, SI, 외주 관리 | Python, Go, gRPC, MQTT | [Breezy](https://bear-robotics.breezy.hr/p/de4ea61cc31c-field-software-engineer-senior-junior) | 진행 중 |
| K29 | 베어로보틱스 | Robotics Field Engineer (AMR) | 운영(현장) | 현장 실사, 설치·맵핑·커미셔닝, SW/HW 이슈 진단, 네트워크 설정·운영 시나리오, PLC·컨베이어·자동문·MES/API 연동 지원, **시스템 로그 수집·엔지니어링 협업** | 1년+, 현장 트러블슈팅, Linux·네트워크, 장기 출장 | 영어, AMR/AGV, PLC/IO, ROS, 데이터 분석 | Linux, ROS, PLC | [Breezy](https://bear-robotics.breezy.hr/p/8e210cb04bc5-robotics-field-engineer-amr) | 진행 중 |
| K30 | CJ대한통운 | 로봇 IT 시스템 개발 (TES물류기술연구소) | 개발 | 로봇·자동화 설비 연계 물류 운영 시스템, WMS·Picking·**로봇운영시스템**·미들웨어 | 백엔드 5년+, SQL DBMS 5년+, SQL·C#·Java, Windows/Linux, Git/SVN | 영어/중국어 | Java, Spring, React, Oracle | [원티드 142697](https://www.wanted.co.kr/wd/142697) | 마감(게시일 미상) |
| K31 | 쿠팡풀필먼트서비스 | 물류 자동화 로봇(AGV,AMR) S/W 엔지니어 (대구3) **(요약 기반)** | 운영(유지보수) | AMR/AGV SW 점검·유지보수, 트러블슈팅·데이터 관리, 해외 벤더와 즉각 이슈 대응 | PLC/제어 3년+, Python/Java/C++, **PowerBI·Tableau 대시보드**, 주/야 교대, 영어 회의 | 확인 못함 | PLC, Python, PowerBI | [startup.jobs](https://startup.jobs/%EC%BF%A0%ED%8C%A1%ED%92%80%ED%95%84%EB%A8%BC%ED%8A%B8%EC%84%9C%EB%B9%84%EC%8A%A4-%EB%AC%BC%EB%A5%98-%EC%9E%90%EB%8F%99%ED%99%94-%EB%A1%9C%EB%B4%87agvamr-s-w-%EC%97%94%EC%A7%80%EB%8B%88%EC%96%B4-%EB%8C%80%EA%B5%AC3-coupang-4529033) | 원문 403 |
| K32 | 인티그리트 | AI 클라우드 개발팀 Back-end / Front-end (관제 플랫폼) | 개발 | **로봇 원격 모니터링·관제** BE/FE, **원격 제어·미션 수행**, 송수신 데이터·경로 분석, 센서 기반 디지털 트윈·상황 인지, **WebRTC 영상** | BE: AWS, Spring Boot 5년+, RDBMS / FE: React 2년+, WebSocket·gRPC | Node.js, Python, WebRTC, Kafka, ElasticSearch/Kibana, Jenkins, 3D | Spring Boot, AWS, React, WebRTC, Kafka | [사람인 55023730](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=55023730) | 채용 시 마감, 강동 |
| K33 | 네이버랩스 | Robotics Software Engineer, Backend (과거 공고) | 개발 | Brainless 서비스 로봇 제어·관리 Backend, **다중 로봇 제어 시스템** 프레임워크 | Linux·클라우드 로봇 응용 또는 Backend, 통신·네트워크 설계, C++/Python/Go | 석사+ 또는 5년+, 서비스 로봇·자율주행, 실시간 데이터, 클라우드 | C++, Python, Go | [비즈니스피플](https://www.bzpp.co.kr/biz/businessDetailView/BR210805A00081) | 2021-08-05 게시. 2026-09-29 기준 네이버랩스 공개 공고 2건은 관제와 무관 |

### A-2. 해외 공고 (20건)

| ID | 회사 | 직무명 | 직군 | 주요 업무 | 자격요건 | 우대사항 | 기술 스택 | 링크 | 상태 |
|---|---|---|---|---|---|---|---|---|---|
| O1 | Locus Robotics | Onsite Support Engineer, Robotics | 운영(현장) | 고객 사이트 로봇 fleet의 **시스템 상태·로봇 활동·작업 실행 모니터링(uptime·안정성)**, 처리량 KPI 달성 워크플로 최적화, 현장 트러블슈팅·수리, 테스트·검증, **Throughput·UPH·Lines/Hour·Utilization 분석**, SOP·playbook 작성 | 학사, 창고·로보틱스·자동화 1년+, 피킹 지표 이해, HW/SW 트러블슈팅, 데이터 분석 | - | - | [Greenhouse](https://job-boards.greenhouse.io/locusrobotics/jobs/5213412007) | 2026-08-17 갱신 (Fort Worth 외 2곳) |
| O2 | Agility Robotics | Robot Operator II | 운영(테스트) | 일일 로봇 테스트 워크플로, Python 테스트 스크립트, 네트워크 설정, Linux·bash, Jira 기록, **하루 4시간+ happy path·corner case 운영으로 failure mode 발굴** | 네트워크·산업제어 자격/준학사, Linux, Jira | - | Python, Linux | [Agility](https://www.agilityrobotics.com/about/job-post?gh_jid=6014586004) | 2026-07-29, $102–133K |
| O3 | Agility Robotics | Staff Robotics SWE, Teleoperations Architecture | 개발 | **teleop·데이터 수집 시스템 아키텍처**, 저지연 제어·스트리밍 경로, sim·HW 검증·**fault handling**·회귀 테스트, 텔레메트리 기반 운영자 효율 개선 | 7년+, 저지연 스트리밍, C++/Rust | VR·모캡·원격 제어 인터페이스 | C++, Rust, Python | [Agility](https://www.agilityrobotics.com/about/job-post?gh_jid=5972788004) | 2026-08-06 |
| O4 | Nuro | Full Stack SWE, Fleet Platform and Operations Tooling | 개발 | 운영·엔지니어링·파트너용 **fleet 모니터링·vehicle health·정비 워크플로 툴**, React/TS 실시간 UI, 차량·클라우드·파트너 API 백엔드, 테스트·모니터링·알림·프로덕션 지원 | 풀스택 3년+, React·TS, REST/gRPC, PostgreSQL, GCP/AWS | WebSocket·Pub/Sub, Protobuf, K8s·CI/CD | React, TS, gRPC, PostgreSQL | [Nuro](https://nuro.ai/careersitem?gh_jid=7917839) | 2026-08-26 |
| O5 | Nuro | Mission Operations Coordinator (Houston) | 운영 | 계획→일정(운영자 배정) 변환·재최적화, shift 보고·인수인계, 체크리스트·에스컬레이션, **기술·SW 이슈와 긴급상황의 1차 에스컬레이션 지점**, plan vs actual 분석·자동화 | fleet·AV·로봇 운영의 자원 계획·배정 1년+ | - | - | [Nuro](https://nuro.ai/careersitem?gh_jid=7400446) | 2026-09-22 |
| O6 | Waymo | Senior SWE, Fleet Response Infrastructure | 개발 | 차량이 요청하면 Fleet Response Operator가 **실시간 영상·오디오·텔레메트리를 받고 가이던스를 보내는 웹앱**의 신뢰성·성능, FE/BE·API, SRE 협업 | 웹 4년+, Angular·TS·RxJS, 신뢰성 지향 | C++, 고신뢰 분산·RPC | Angular, TS, RxJS, C++ | [Waymo](https://careers.withwaymo.com/jobs?gh_jid=7947243) | 2026-09-21, $213–263K |
| O7 | Waymo | Specialist, Incident Readiness | 운영(안전) | 사고 대응 훈련 설계·실행·평가(외부 공공안전 기관 포함), SMS·HSEEP 기반 로드맵, **운영자 숙련도·워크플로 지연·도구 활용 평가, AAR, RCA·시정조치 추적·종결** | 10년+ 위기훈련·BCP, HSEEP, 디스패치·사고관리 툴 | - | - | [Waymo](https://careers.withwaymo.com/jobs?gh_jid=8140624) | 2026-09-21 |
| O8 | Waymo | Product Manager, Fleet Management Tools | 제품(개발형 집계) | Fleet Operations 툴 로드맵, 운영 pain point→UX 도구, **실시간 지표·모니터링** | PM 5년+(대규모 운영 자동화) | AV·fleet 제품 | - | [Waymo](https://careers.withwaymo.com/jobs?gh_jid=8073643) | 2026-09-24 |
| O9 | Serve Robotics | Operations Coordinator | 운영 | Field Agents·**Piloting 팀** 조율 일일 배치, **실시간 운영 모니터링·해결·에스컬레이션**, SOP·안전 절차 준수, depot 개폐, **deployment efficiency·service uptime·utilization 지표** | 학사 또는 운영·물류 3년+ | - | - | [Ashby](https://jobs.ashbyhq.com/serverobotics/eba1e2b0-2900-4506-ba0f-6f1d0c9b28f5) | 2026-09-23 (Chicago 외) |
| O10 | Serve Robotics | Operations Associate | 운영(현장) | 장애물·기술 문제 시 현장 지원, 배치 준비·센서 청소, **에스컬레이션 티켓 기록·종료**, 차량으로 **로봇 회수·재배치**, 안전 이벤트 에스컬레이션, 교대 종료 회수·충전·자산 확인 | 운전면허, 50lbs, 야간·주말 | - | - | [Ashby](https://jobs.ashbyhq.com/serverobotics/d1e62cb0-c826-469c-9457-4b221ed34cc0) | 2026-09-14 |
| O11 | Serve Robotics | Regional Operations & Fleet Expansion Manager | 운영(관리) | **fleet 성능·uptime·throughput·MTTR·SLA·fleet health**, 현장팀·원격 운영자·depot 관리, 절차·안전·규제 준수, 엔지니어링과 체계적 신뢰성 이슈 해결 | (본문 일부) | - | - | [Ashby](https://jobs.ashbyhq.com/serverobotics/aea916ef-5e4d-4854-95f0-73ea21bad720) | 2026-08-20 |
| O12 | Serve Robotics | Field Development Test Associate | 운영(테스트) | 테스트 계획 기반 현장 테스트, 로봇 배치·모니터링·회수, **SW/HW/운영 이슈 식별·문서화·재현**, 테스트 데이터·운영 로그 | 고졸+, 절차 준수·문서화 | 공학 학위 | - | [Ashby](https://jobs.ashbyhq.com/serverobotics/28bbd767-93f5-4df1-8917-ca3cc339041b) | 2026-08-05 |
| O13 | Amazon Robotics | Applied Science Manager, Fleet Autonomy (Proteus) | 개발(연구) | 수천 대 이기종 AMR의 **교차로·복도 협조, 도로망 교통 형성, 작업-로봇 배정**, 운영과 상호작용하는 fleet orchestration 계층 | PhD, 5년+, 관리 2년+, AMR 항법, 현장 배포 | 물류 fleet, 실시간 의사결정 알고리즘 | Java/C++/Python | [amazon.jobs](https://www.amazon.jobs/en/jobs/10536870/applied-science-manager-fleet-autonomy-amazon-robotics) | 2026-09-10 |
| O14 | Amazon Robotics | Sr. Applied Scientist, Structured Field Coordinated Planning & Control | 개발(연구) | 경로계획·fleet 협조·제어 연구→프로덕션, 수천 대 모션 조정, 자원 할당, 효율·신뢰성 개선 측정 | PhD 또는 석사+5년 | A*·RRT·최적화 계획·multi-agent coordination | Java/C++/Python | [amazon.jobs](https://www.amazon.jobs/en/jobs/3198491/sr-applied-scientist-amazon-robotics-structured-field-coordinated-planning-control) | 2026-03-06 |
| O15 | Symbotic | Principal Software Engineer – Fleet Orchestration | 개발 | 실시간 오케스트레이션·로봇 협조·**운영 가시성** 플랫폼, 이벤트 기반 실시간 처리, 메트릭·관측, **교차 서비스 프로덕션 이슈 진단** | 12년+, 분산 시스템, Kafka/RabbitMQ | 창고 자동화, 실시간 제어, 시뮬레이션·최적화 | Kafka, RabbitMQ | [Symbotic](https://www.symbotic.com/careers/open-positions/R7269/) | 2026-07-15, $180–247.5K |
| O16 | Dexory | Full Stack Platform Engineer | 개발 | Go 텔레메트리 수집 서비스, **"새벽 2시에 필드 엔지니어가 로봇이 왜 멈췄는지 확인하는" React/TS 인터페이스**, WebRTC·WebSocket·gRPC·Zenoh 실시간 텔레메트리·영상 | Go, React·TS | three.js·deck.gl, WebRTC, Protobuf·Zenoh·MQTT | Go, React, WebRTC, Zenoh | [Greenhouse](https://job-boards.eu.greenhouse.io/dexory/jobs/4953042101) | 2026-09-04 |
| O17 | Robust AI | Support Escalation Engineer (Tier 3) | 운영(지원) | 진단 도구·프로세스, 에스컬레이션·응답 가이드라인, **Tier1/2 진단 데이터로 RCA**, 제품 hardening 피드백, **해결 내역 KB 문서화** | 고객 대면 기술직 3년+, Linux 심화, GitHub·Jira | Python, ROS, 클라우드·Docker | Linux, ROS, Python | [Lever](https://jobs.lever.co/robust-ai/0d8612e4-0153-4476-97f2-a85b9b728cf4) | 2026-08-25 |
| O18 | Starship Technologies | Remote Assistance Specialist(구 Remote Operator) **(요약 기반)** | 운영(원격) | 컴퓨터 스테이션으로 자율주행 로봇 감독, 특정 교통 상황·경로 막힘 시 **원격 조종**, 배터리 교체·충전 등 기술 이슈 지원 | 주 3회 이상 야간, 24/7 교대(6–10h), 적·녹 색 구분, 영어 | - | - | [startup.jobs](https://startup.jobs/remote-assistance-specialist-starshiprobots-3963495) | 원문 403 |
| O19 | MiR (Teradyne Robotics) | Software Engineer (Odense) — Fleet Management Systems **(요약 기반)** | 개발 | Fleet Management System 개선 | C++ | C#·.NET | C++, C# | [Teradyne](https://jobs.teradyne.com/MiR/job/Odense-Software-Engineer/1304843600/) | "position has been filled" |
| O20 | Bear Robotics (US) | Robotics Software Engineer II, Mission | 개발 | 로봇 행동 커스터마이즈 엔진, 고객용 API, 원격 커스터마이즈 클라우드 연동, 백엔드 인프라, **post-mortem** | 2년+, Go 또는 C++, 분산·클라우드 | ROS | Go, C++ | [Breezy](https://bear-robotics.breezy.hr/p/f0861772b503-robotics-software-engineer-ii-mission) | 진행 중, $176–215K |

### A-3. 찾지 못했거나 집계에서 뺀 곳

| 대상 | 결과 | 근거 |
|---|---|---|
| 트위니 | 공개 공고 4건(전략기획·IR·기구설계·교육) 중 관제 직무 없음. 관제·운영 플랫폼 TCS는 제품으로 존재 | [GreetingHR](https://twinny.career.greetinghr.com/ko/home), [헬로티 TCS](https://www.hellot.net/news/article.html?no=99480) |
| 뉴빌리티 | 진행 중 5건(QA 리더·구매·품질·임베디드·생산) 중 관제 직무 없음 | [잡코리아](https://m.jobkorea.co.kr/company/42108130/recruit) |
| 네이버랩스 | 현재 공개 공고 2건(Generative AI Engineer, 상시 인재풀) | [NAVER LABS Careers](https://recruit.naverlabs.com/) |
| LG전자·두산로보틱스·한화·에이딘로보틱스·코가로보틱스·알지티·하이브리드 | 관제 공고 확인 못함 | 검색 결과 없음 |
| 현대무벡스 | 점핏 "AMS사업부 ACS 개발자" 검색 노출, 원문 404 | [점핏 48428](https://jumpit.saramin.co.kr/position/48428) |
| 현대위아 | 검색엔진 요약에 "FMS SW 개발(다수 로봇 관제 운영)" 언급, 원문 미확인 → 제외 | - |
| 브릴스 | 경력 수시채용은 SI·설비 SW·셋업 직무 위주, 관제 아님 | [사람인 54635530](https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54635530) |
| OTTO/Rockwell, Formant, InOrbit, Kiwibot, Coco | 현재 공고 원문 확인 못함 | - |
| Brain Corp | Greenhouse 10건 중 관제 직무 없음(엔지니어링 운영 매니저 등) | [Greenhouse API](https://boards-api.greenhouse.io/v1/boards/braincorporation/jobs) |
| Geek+ | "Systems Software Engineer: 프로젝트 구현용 시스템 컴포넌트 커스터마이즈" 요약만 확인 | [WayUp](https://www.wayup.com/i-j-Geek-881998874437667/) |
| 인접 직무(집계 제외) | 홀리데이로보틱스 SW/펌웨어 운영 엔지니어(OTA·릴리즈·재발 방지), 시스콘 AMR Software Engineer(마감), Waymo Fleet Orchestration TLM·Fleet Monitoring & Response ML·System Safety(Ops & Fleet Response), Nuro Fleet Connectivity TL·Software Reliability Engineer·Shift Supervisor | [원티드 388832](https://www.wanted.co.kr/wd/388832), [원티드 181868](https://www.wanted.co.kr/wd/181868), [Waymo 보드](https://boards-api.greenhouse.io/v1/boards/waymo/jobs), [Nuro 보드](https://boards-api.greenhouse.io/v1/boards/nuro/jobs) |

### A-4. 빈도 집계 (53건)

- 집계 규칙: 공고의 주요 업무·자격·우대 중 어디에든 해당 항목이 명시되면 1회. 운영형 N=21(ops 18 + 혼합 3), 개발형 N=35(dev 32 + 혼합 3). 혼합 3건은 양쪽에 모두 들어가 합이 53을 넘는다.
- 태그 정의와 공고별 태그는 스크래치 파일 `tags.py`에 남겼다(같은 디렉터리).

| 항목 | 전체 N=53 | 한국 N=33 | 해외 N=20 | 개발형 N=35 | 운영형 N=21 |
|---|---:|---:|---:|---:|---:|
| 장애 대응·트러블슈팅·에스컬레이션 | **26** | 16 | 10 | 12 | 16 |
| 현장 셋업·커미셔닝·현장 대응 | 18 | 13 | 5 | 6 | 14 |
| 상위 시스템 연동(WMS·WCS·MES·ERP·MCS) | 16 | **16** | 0 | 14 | 5 |
| 문서화·SOP·보고·인수인계 | 14 | 6 | 8 | 1 | 14 |
| 웹 관제 UI·대시보드 | 13 | 10 | 3 | 13 | 1 |
| REST API | 13 | 10 | 3 | 13 | 1 |
| DB·SQL | 13 | 12 | 1 | 13 | 0 |
| Docker·Kubernetes | 13 | 11 | 2 | 11 | 2 |
| ROS/ROS 2 | 11 | 9 | 2 | 7 | 5 |
| MQTT | 11 | 10 | 1 | 11 | 2 |
| 산업 프로토콜(PLC·Modbus·OPC UA·SECS/GEM·CAN) | 11 | 11 | 0 | 10 | 3 |
| 클라우드(AWS·GCP·Azure) | 11 | 8 | 3 | 10 | 1 |
| 운영 KPI·데이터 분석 | 11 | 5 | 6 | 5 | 7 |
| 실시간 통신(WebSocket·WebRTC·스트리밍) | 10 | 6 | 4 | 10 | 0 |
| 관제 모니터링 업무(사람) | 10 | 6 | 4 | 1 | 10 |
| 작업 할당·스케줄링·오케스트레이션 | 9 | 5 | 4 | 8 | 1 |
| gRPC | 9 | 7 | 2 | 9 | 1 |
| CI/CD·배포·롤백 | 9 | 8 | 1 | 9 | 0 |
| 모니터링·알림·관측(Grafana·ELK 등) | 9 | 5 | 4 | 9 | 0 |
| 시뮬레이션·사전 검증·회귀 테스트 | 9 | 5 | 4 | 7 | 2 |
| 메시지 브로커·이벤트 기반(Kafka 등) | 8 | 6 | 2 | 8 | 0 |
| 로그 분석·문제 재현·RCA | 8 | 4 | 4 | 2 | 6 |
| 안전(비상정지·인터록·안전 규정) | 8 | 3 | 5 | 3 | 6 |
| 교통 제어·충돌/교착 방지 | 6 | 4 | 2 | 6 | 1 |
| 원격 조종·원격 개입 | 6 | 2 | 4 | 4 | 2 |
| 무중단·고가용성·예외/재시도 처리 | 6 | 3 | 3 | 6 | 0 |
| MAPF·멀티로봇 경로계획 | 5 | 3 | 2 | 5 | 0 |
| 3D/지도 시각화(Three.js·WebGL·D3) | 5 | 4 | 1 | 5 | 0 |
| 설비 연동(엘리베이터·도어·컨베이어·도킹) | 5 | 5 | 0 | 4 | 3 |
| 교대·야간 근무 | 5 | 2 | 3 | 0 | 5 |
| VDA 5050 | 4 | 4 | 0 | 4 | 1 |
| 온프레미스·엣지 배포 | 4 | 4 | 0 | 4 | 0 |
| 이기종 로봇·장비 통합(어댑터) | 4 | 4 | 0 | 4 | 1 |
| Open-RMF | **0** | 0 | 0 | 0 | 0 |

| 언어 | 전체 | 한국 | 해외 | 개발형 | 운영형 |
|---|---:|---:|---:|---:|---:|
| Python | 17 | 12 | 5 | 13 | 6 |
| C/C++ | 13 | 7 | 6 | 12 | 1 |
| Java | 11 | 9 | 2 | 10 | 2 |
| JS/TS/Node.js | 10 | 7 | 3 | 10 | 1 |
| C#/.NET | 7 | 6 | 1 | 7 | 1 |
| Go | 6 | 4 | 2 | 6 | 1 |

**읽는 법(사실과 해석 구분)**

- 사실: 한국 공고는 상위 시스템 연동 16/33, 산업 프로토콜 11/33, VDA 5050 4/33. 해외 공고는 상위 연동 0/20이며 운영형이 10/20이다.
- 해석(추론): 한국 관제 개발 채용은 제조·물류 현장 SI 성격이 강해 "로봇만의 관제"보다 "설비·상위 시스템과 붙는 관제"를 찾는다. 해외 표본은 배달 로봇·자율주행차 운영 조직이 많아 운영 KPI·문서화·안전 절차 비중이 높다.
- 사실: 운영형 공고의 공통 흐름은 "모니터링 → 1차 조치 → 담당자 전달(에스컬레이션) → 정상화 확인 → 기록·인수인계"다(K11, K15, O5, O9, O10).
- 사실: MAPF·교통 제어를 명시한 공고는 6건이지만 모두 관제 핵심 포지션이다(K1 클로봇 CROMS, K8 리보틱스, K16 레인보우, K23 스피드플로어, O13·O14 아마존).

### A-5. 포트폴리오에서 증명해야 할 역량 Top 10

순위는 A-4 빈도와 "핵심 포지션에서의 명시 여부"를 함께 반영한 판단이다(판단 자체는 추론, 근거 수치는 사실).

| 순위 | 역량 | 증명 방식(포트폴리오에서 보여줄 것) | 근거 |
|---|---|---|---|
| 1 | 이상 상황 처리 파이프라인 | 경보 발생 → 1차 자동 대응 → 관제원 확인(ack)·조치 → 현장 출동 티켓 → 재개 조건 확인 → 기록·인수인계까지 한 흐름으로 시연 | 장애 대응 26/53, 문서화 14/53. K11·K15 운영 흐름, K10 예외 AI Agent·통신 단절 시나리오 테스트, K17·K27 장애 대응 |
| 2 | 교통 관리(충돌·교착 방지와 해소) | 구역·노드 예약 기반 충돌 방지, 교착 감지 경보와 해소 전략, 사람·지게차 같은 통제 불가 유닛 처리 | K1·K8·K16·K23·O13·O14. MiR Fleet도 교착을 경보 1순위 사건으로 둠([MiR Fleet 문서](https://jk.de/media/a2/50/ce/1738939330/mir_fleet_enterprise_documentation_1.2_en.pdf)) |
| 3 | 작업 할당·스케줄링과 확장성 | 대수 증가(예: 10→30→50대) 시 처리량·교착·대기시간 곡선을 시뮬레이션으로 제시 | 스케줄링 9/53, K16 "로봇 대수와 물류량 증가에 따른 운영 효율·확장성", O13 |
| 4 | 실시간 관제 UI | 지도 위 로봇·예약 구역·경보, 로봇 상세(배터리·통신 감도·상태), 원격 정지, 원격 조작 인계(영상) | 웹 UI 13, 실시간 통신 10, 3D 5. K18·K7·K32·O4·O6·O16, 국내 고시 관제장치 요건([고시](https://www.motir.go.kr/kor/article/ATCL0c554f816/64952/view)) |
| 5 | 표준 메시징·API 계층 | MQTT 토픽 설계, REST/gRPC API, ROS 2 브리지, (선택) VDA 5050 어댑터 | MQTT 11, REST 13, gRPC 9, ROS 11, VDA 4. K6·K14·K20·K27 |
| 6 | 상위 시스템·설비 연동 | 모의 WMS/MES가 주문을 넣고 결과를 받는 인터페이스, 엘리베이터·자동문·충전기 모의 장치와 실패 주입 | 상위 연동 16(한국 16/33), 설비 연동 5. K8·K14·K17·K20·K23·K26·K28 |
| 7 | 데이터·KPI·관측성 | DB 스키마·이벤트 로그, Grafana식 대시보드, uptime·throughput·MTTR·개입률 지표 | DB 13, KPI 11, 관측 9. O1(UPH·utilization), O9(uptime), O11(MTTR·SLA), K19(성공률·사이클타임·가동률), K23(Pallet/Hour) |
| 8 | 결함 주입 기반 검증 | 통신 단절·로봇 정지·지게차 난입·충전 실패 시나리오를 자동 테스트로 돌리고 통과 기준을 표로 제시 | 시뮬레이션·회귀 9. K10(통신 단절 등 예외 시나리오 통합 테스트), K20(테스트 자동화), O2(corner case로 failure mode 발굴) |
| 9 | 배포·운영성 | Docker Compose/K8s로 한 번에 기동, 설정 분리, 장시간 운용 시 예외·타임아웃·재시도 처리 | Docker·K8s 13, CI/CD 9, 무중단 6. K14 필요역량, K27 온프레미스 k3s |
| 10 | 사고 조사와 문서화 | 사고 전후 재생(블랙박스 로그), 사고 보고서·RCA 템플릿, 상황별 Runbook·SOP, 비상 계획표 | 로그·RCA 8, 문서화 14. K12·K13·O7·O17, MiR 비상 계획표 형식 |

---

## B. 이상 상황 대처(incident response) 실무

### B-0. 출처 약어

| 약어 | 출처 | 비고 |
|---|---|---|
| [MiR-F] | [MiR Fleet Enterprise Documentation v1.2 (2025-01)](https://jk.de/media/a2/50/ce/1738939330/mir_fleet_enterprise_documentation_1.2_en.pdf) | PDF 본문 확인. 경보(교착·E-stop/보호정지·오류), 경보 context, 자동 충전, Unavailable 로봇, fleet 상태, 대피, 비상 계획표, 충돌 회피, 자원·구역 |
| [MiR-S] | [MiR600 EU Pallet Lift User Guide — Types of stop](https://supportportal.mobile-industrial-robots.com/support-files/manuals/HTML/en/mir600_eu_pallet_lift_user_guide_1.4/Content/manuals/usage/means_for_stopping.htm) | 정지 유형별 재개 조건 |
| [MiR-FA] | [MiR AI Smart Fire Alarm Module](https://mobile-industrial-robots.com/products/mir-go/ai-automation-ai-smart-fire-alarm-module) | 화재 경보 연동 대피 |
| [OTTO] | [OTTO 1500 v1.2 Operations & Maintenance Manual, Rev E (Clearpath, 2020-02)](https://pmsql01.perfectionmachinery.com/pisweb/OMM-000003_E.pdf) | PDF 본문 확인. 상태 표시(Blocked·Lost·Lost Connection 등), 속도별 안전 필드 |
| [SEEG] | [Seegrid Supervisor](https://seegrid.com/fleet-central/supervisor/) | 알림·재경로·교차로 교통 관리 |
| [BB] | [BlueBotics ANT server](https://bluebotics.com/autonomous-navigation-technology/ant-server-fleet-software), [ANT locator 발표(2025-03)](https://www.roboticstomorrow.com/news/2025/03/10/-bluebotics-enables-precise-asset-tracing-and-transport-flow-optimization-with-launch-of-ant-locator-/24398/) | 교차로·도어·엘리베이터 교통 관리, 수동 지게차 위치 연동 |
| [BRAIN] | [BrainOS Mobile](https://www.braincorp.com/resources/brainos-mobile-the-robot-tool-connecting-operators-to-their-machines-05ac4) | assist 알림·협업 해소 |
| [KR-고시] | [산업통상부 고시 제2025-021호 실외이동로봇 운행안전인증 절차 및 기준](https://www.motir.go.kr/kor/article/ATCL0c554f816/64952/view) (첨부 HWP 본문 확인), [KIRIA 안내](https://www.kiria.org/portal/cert/portalCertEstiSafe.do) | 비상정지·횡단 중 통신 단절·관제장치 요건 |
| [WAYMO] | [TechCrunch 2026-03-25](https://techcrunch.com/2026/03/25/waymo-robotaxi-roadside-assistance-emergency-first-responders), O6·O7 공고 | 원격 지원(운전 아님)·현장 지원·AAR |
| [SIDE] | [GovTech 2022-03-30](https://www.govtech.com/fs/who-drives-the-food-delivery-bot-when-something-goes-wrong), O18 요약 | 배달 로봇 원격 인계 |
| [ITR] | [InOrbit Time Capsule (2021-05-19)](https://www.inorbit.ai/blog/time-capsule-gives-you-time-travel-powers) | 사고 전후 구간 재생 |
| [ROSBAG] | [rosbag2 snapshot mode 설계 문서](https://github.com/ros2/rosbag2/blob/rolling/docs/design/rosbag2_snapshot_mode.md) | 링버퍼 + 트리거 저장 |
| [OSHA] | [OSHA Incident Investigation](https://www.osha.gov/incident-investigation), [Near-Miss Report Form 템플릿](https://www.osha.gov/sites/default/files/2021-07/Template%20for%20Near%20Miss%20Report%20Form.pdf) | 근접사고 조사 권고 |
| [WOOWA] | [AWS 기술 블로그: 우아한형제들 실외배달 로봇 연결 구축기](https://aws.amazon.com/ko/blogs/tech/woowabros-deliveryrobot-with-awsiotcore/) | 텔레메트리·명령 토픽·로그 경로 |

표기: **[사실]** 은 출처 원문에서 확인한 내용, **(추론)** 은 출처를 바탕으로 한 설계 권고다.

### B-1. 상황별 대처 흐름

| 상황 | 감지 신호 | 1차 자동 대응 | 관제원 조치 | 현장 조치 | 재개 조건 | 사후 기록 |
|---|---|---|---|---|---|---|
| **보호정지(사람·물체가 보호 필드 진입, 범퍼)** | 로봇 상태 "Scanners blocked"/Protective stop, 관제 경보 "Robot E-stop"(보호정지 포함) [MiR-F] | 로봇 안전 시스템이 즉시 정지 [MiR-S] | 발생 위치·빈도 확인. 같은 구역 반복이면 구역 설정 재검토 [MiR-F: "Location-based issues can often be solved with zones"] | 물체 제거 | **필드가 2초간 비면 자동 재개** [MiR-S][MiR-F]. 기동·모드 전환 뒤 보호정지는 Resume 버튼 필요 [MiR-S] | 보호정지 횟수·위치 히트맵(추론: 근접사고 지표로 활용) |
| **비상정지(E-stop 버튼)** | 로봇 상태 Emergency stop, 관제 경보 [MiR-F][OTTO: Full Flashing Red] | 전 동작 차단 | 원인 확인, 현장 인원 안전 확인 | 버튼 해제 | **E-stop 해제 + Resume(OTTO는 Safety Reset)** 수동 조작 [MiR-S][OTTO]. 국내 고시: "비상정지 상태는 수동에 의해서만 해제", "해제 후 사용자의 조작 없이 임의로 동작하지 않을 것", "비상정지는 제어시스템에서 가장 높은 우선권" [KR-고시] | E-stop 이벤트 로그, 누가 해제했는지 기록(추론) |
| **로봇 고장(구동·센서·HW 결함)** | 로봇 Error 상태, 관제 경보 "Robot error" + context(로봇·맵) [MiR-F]. OTTO는 BMS가 셀 전압·온도 이상 보고 [OTTO] | 오류 시 보호정지 진입(MiR 사용자 가이드 요약), 관제는 해당 로봇에 신규 미션 배정 중단(Not operational) [MiR-F] | Hardware health·System log 확인 후 원격 Reset 또는 현장 요청 [MiR-F] | 수리·교체. 고장 로봇이 통로를 막으면 이동 | "Resolve the error and clear the state" [MiR-F] | 로봇·Fleet error log 자동 생성, 기술지원 제출 [MiR-F]. Locus는 고장 로봇 작업을 가까운 로봇에 재배정한다고 설명(검색 요약, [Locus](https://locusrobotics.com/locusone/automated-warehouse-software)) |
| **위치 추정 실패(Localization lost)** | OTTO 상태 "Lost: can't determine its location relative to its loaded Map" [OTTO]. MiR는 위치 추정 실패를 오류 사례로 둠(사용자 가이드 요약) | 정지(오류 상태) | 원격으로 초기 위치 재지정 가능 여부 판단(추론), 반복 지점이면 맵·반사 특징 점검(추론) | 로봇을 알려진 위치로 수동 이동, 재위치 추정 | 위치 신뢰도 회복 확인 후 재개(추론) | 발생 위치·맵 버전 기록(추론) |
| **통신 두절** | 관제에서 "Unavailable"(MiR), OTTO "Lost Connection to WiFi/Fleet Manager" 표시 [MiR-F][OTTO] | MiR: 두절 로봇은 **점유 자원을 유지**, 신규 자원·미션·충전 배정 불가, 충돌 회피 데이터 교환 불가, **이미 받은 미션은 계속 수행**. 필요한 자원이 없으면 대기하다가 **5분간 통신이 없으면 자원 배정 없이 진행 허용**, 재연결 시 다시 자원 대기 [MiR-F] | 네트워크 복구, 교착 경보·미션 스케줄러 확인 [MiR-F 비상 계획표] | AP·전원 점검. Wi-Fi 채널 사용률 60% 초과 시 지연·패킷 손실 [MiR-F] | 재연결 후 상태 동기화 | (추론) 두절 구간·지속 시간·마지막 상태 기록. 국내 고시: **횡단보도 통행 중 통신 단절 시 횡단 완료 후 안전한 곳에 정차** [KR-고시] |
| **관제 서버 정전·장애** | 서버 다운 | 로봇에 미션 배정 중단 | UPS 복전 후 **교착 경보와 미션 스케줄러 확인** [MiR-F] | UPS 점검 | 서버 재기동·로봇 재연결 | Event viewer로 변경 이력 확인 [MiR-F] |
| **배터리 저하·충전 실패** | 배터리 경보. Seegrid는 저배터리 문자·메일 알림 [SEEG] | MiR: **배터리 7% 미만 로봇을 최우선 충전 배정**, 자동 충전 미션 실패 시 충전·대기 배정 재평가 [MiR-F]. OTTO "Failed Target Find"(도크 탐지 실패) 상태 [OTTO] | 충전기 상태 확인, 다른 충전기 재배정 | 방전 로봇은 켜서 가까운 충전기로 보내거나 밀어서 케이블 충전 [MiR-F 비상 계획표] | SOC 회복 | 충전 실패 충전기·로봇 기록(추론) |
| **경로 막힘·장시간 정지(Blocked/Stuck)** | OTTO "Blocked: blocked from proceeding on its planned path" [OTTO]. 국내 고시: **보행자 밀집 등으로 통행 불가 시 관제장치에 알리고 관리자 조치 가능해야** [KR-고시]. Brain Corp는 "assist" 알림과 이미지로 위치 안내 [BRAIN] | 우회 경로 탐색. Seegrid Supervisor는 막힌 구간을 무시하고 우회, "Caution Tape"로 구간 차단(뉴스 요약) [SEEG] | 원격 해소: Brain Corp는 지게차·카트 같은 장애물이 치워진 뒤 "path is blocked" assist를 앱에서 원격 해소한다고 소개(검색 요약) | 장애물 제거, 필요 시 로봇 수동 이동 | 경로 해제 확인 | 막힘 지점 빈도·원인(사람·지게차·적재물) 기록(추론) |
| **교착(Deadlock)** | 관제 경보 "Deadlock detected" + context: **관련 로봇, 수행 미션, 맵, 관련 자원, 활성 구역** [MiR-F] | (추론) 우선순위 기반 한 대 후퇴·재계획 | 자원 점유 상태 보고 해소. 자원 배정 거리(기본 10 m, 최소 3 m) 축소가 일부 구역 교착 해소에 도움 [MiR-F] | 필요 시 한 대를 수동 이동 | 자원 해제 확인 | 교착 경보 이력, 반복 구역은 Limit-robots zone·Directional zone 재설계 [MiR-F] |
| **로봇-로봇 충돌/근접** | 충돌·보호정지 경보 | fleet 충돌 회피: 스캐너로 다른 로봇을 감지하면 관제가 그 로봇의 footprint를 전달해 장애물로 반영 [MiR-F] | "충돌 회피를 켜도 충돌이 자주 나면 Directional zone으로 흐름 제어" [MiR-F] | 손상 점검 | 점검 완료 | 사고 보고서(추론) |
| **로봇-사람 접촉 우려** | 보호정지, 범퍼 | 즉시 정지 | 인명 확인 우선, 해당 로봇·주변 로봇 정지(추론) | 부상 대응·현장 통제 | 안전 확인 후 수동 재개(추론) | 사고·근접사고 보고, 조사 [OSHA] |
| **화재·건물 비상** | 화재 경보 신호 | MiR: 경보 수신 시 자동 대피 [MiR-FA]. 대피 시 **대피 구역 안 로봇은 가장 가까운 대피 위치로, 구역 밖 로봇은 일시정지, 대피 중 신규 미션 불가, 종료 시 오류 로그 자동 생성** [MiR-F] | 건물 비상 절차에 로봇 관리 포함 [MiR-F 비상 계획표] | 대피 경로 확인 | 경보 해제 후 **작업 재개 또는 중단 미션 복구** [MiR-FA] | 대피 로그 [MiR-F]. FORT는 SIL 등급 무선 안전 명령으로 전 로봇 일괄 정지 권고 ([FORT](https://www.fortrobotics.com/news/fire-safety-for-warehouse-amrs)) |
| **엘리베이터·자동문 연동 실패** | 호출 응답 없음, 도어 상태 불일치(추론) | (추론) 탑승 전 대기 위치에서 정지·재시도, 타임아웃 시 경로 재계획 | (추론) 설비 상태 확인, 수동 호출 | 설비 점검 | 설비 정상 응답 | (추론) 설비별 실패율 기록. 사실로 확인된 것은 연동 자체의 존재뿐: BlueBotics 교통 관리가 교차로·도어·엘리베이터를 조율 [BB], 유진로봇 백엔드 공고의 엘리베이터·자동문 연동(K26), 베어 Field SW의 도어·엘리베이터 integration(K28), 국내 KS B 7317 "이동 로봇의 엘리베이터 탑승을 위한 안전 요구사항 및 평가 방법" 존재([KSSN](https://www.kssn.net/search/stddetail.do?itemNo=K001010135682)). 실패 처리 절차를 공개 문서에서 확인하지 못함 |
| **적재물 낙하·과적재** | (추론) 적재 센서·중량 변화, 카메라 | 정지 | 위치 통보 | 적재물 수거 | 점검 후 재개 | 사실: OTTO는 **적재물 높이 초과를 감지하지 못하므로 설치 현장에서 높이를 검증**하라고 명시, 적재물이 footprint를 넘으면 내비게이션 사양 조정 필요 [OTTO]. 국내 고시는 30 mm 턱을 "전도, 적재물의 이탈이나 낙하 없이" 통과하도록 요구 [KR-고시] |
| **원격 조종 인계(teleop takeover)** | 로봇의 도움 요청, 막힘 | - | Starship: 로봇이 풀지 못하면 "calls home", 지켜보던 사람이 인계 [SIDE]. Coco는 전 구간 사람이 원격 조종(2022 기사) [SIDE]. Waymo 원격 지원은 **운전하지 않고 조언**만 하며 차량이 수용 여부 결정, 중간값 지연 약 150 ms(미국)·250 ms(해외) [WAYMO] | - | 인계 해제 후 자율 복귀 | 국내 고시: **원격 수동조작으로 이동시킬 때는 카메라 등 영상으로 주변 확인 필수**(육안 확인 시 예외), **원격 정지 수단 필수** [KR-고시] |
| **로봇 구출(현장 출동)** | 원격 해소 불가 | - | 출동 티켓 발행·에스컬레이션 | Serve Operations Associate: 차량으로 로봇 회수·재배치, 에스컬레이션 티켓 기록·종료 (O10). Waymo는 전담 Roadside Assistance 팀 운영, 규모·응답 지표는 비공개 [WAYMO] | 회수 완료 | 티켓 종료 기록(O10) |

**운영 조직의 공통 흐름(사실, 공고 원문 기준)**

- 도구로보틱스(K11): 원격 모니터링 → 이상상황 확인 시 1차 조치·보고 → 개발·현장 담당자 지원 요청 → 조치 후 정상화 확인·인수인계.
- 벰로보틱스(K15): ACS로 운행상태 모니터링 → 알람·장애 1차 대응 → 기술 조치 필요 사항을 개발/제어 담당자에게 전달 → 장애내용·조치결과·인수인계 기록·공유.
- Nuro(O5): 기술·SW 이슈와 긴급상황의 1차 에스컬레이션 지점, 체크리스트·에스컬레이션 절차, shift 보고·인수인계.
- Serve(O9·O10·O11): 실시간 모니터링·에스컬레이션, 현장 회수, uptime·MTTR·SLA 관리.

### B-2. 충돌 검사: 계획 단계와 실행 단계를 현업이 나누는 방식

| 계층 | 누가 | 무엇을 | 근거 |
|---|---|---|---|
| **① 계획 단계(관제 서버)** | FMS/ACS | 위치·마커·Limit-robots zone·Lock zone을 **"자원"으로 두고 점유·대기열 관리**. 경로상 자원까지 10 m(설정 3 m까지)일 때 배정. 도킹 전 점유 구역 진입 여부 확인. 구역 해제는 로봇 중심이 경계를 넘을 때 | [MiR-F] |
| ① 계획 단계 | FMS | 교차로 "automated traffic cop", 막힌 구간 우회·차단 | [SEEG] |
| ① 계획 단계 | FMS | 교차로·도어·엘리베이터에서 차량 조율(내장 교통 관리자) | [BB] |
| ① 계획 단계 | FMS 개발 공고 | "경로 충돌 방지·경로 배정 Traffic Control"(K1), "충돌·교착·병목 예방 경로·자원 할당"(K8), "교차로·협소 구간 교통 제어, 충돌·정체·교착 방지·복구"(K16) | 공고 원문 |
| **② 실행 단계(fleet 수준 감시)** | FMS↔로봇 | 로봇이 스캐너로 다른 로봇을 감지하면 관제가 그 로봇의 footprint를 전달해 장애물 구름에 추가(적재물이 footprint를 넘는 경우 대비). 통신 두절 로봇은 이 데이터 교환 불가 | [MiR-F] |
| ② 실행 단계 | FMS | 교착 경보와 context, 반복 구역은 Directional zone | [MiR-F] |
| ② 실행 단계(통제 불가 유닛) | FMS + 인프라 | 수동 지게차 위치를 ANT locator로 fleet 관리자에 넣어 **신호등 등으로 AGV·수동 트럭·사람의 상호작용 관리**(2025-06 출시 예정 발표) | [BB] |
| **③ 로봇 자체 안전(최후 계층)** | 로봇 안전 PLC·스캐너 | 보호 필드 진입 시 보호정지, 필드가 2초 비면 자동 재개 | [MiR-S][MiR-F] |
| ③ 로봇 자체 안전 | 로봇 | 속도 구간별 안전 필드 세트 전환, 2.1 m/s 초과 명령 시 비상정지(유도 SW의 잘못된 속도 명령 대비) | [OTTO] |
| ③ 한계 명시 | 로봇 | **LiDAR 평면 밖 장애물은 안전 시스템이 식별하지 못함, 3D 카메라는 내비게이션 보조일 뿐 안전 시스템이 아님**, 적재물 높이 미감지 | [OTTO] |
| ②↔③ 경계 설정 | 설정 | footprint가 보호 필드보다 작으면 장애물에 붙어 달리다 보호정지 → **footprint를 최소 보호 필드보다 10~15% 크게** | [MiR-F] |
| 국내 기준 | 로봇 | 사람·장애물 감지 시 안전하게 감속·정지·회피(KS B 7320 5.6 방법), 지정 운행구역 준수·금지구역 미진입 | [KR-고시] |

**해석(추론)**

- 계획 단계 검사는 "예약이 겹치는가"를 보고, 실행 단계 감시는 "지금 실제로 가까워지는가"를 본다. 통제 불가 유닛(사람·지게차)은 계획 단계 예약에 들어오지 않으므로 ②(관측 기반 이격 감시)와 ③(로봇 안전)으로만 다뤄진다. 지게차 위치를 받을 수 있으면(ANT locator 사례) ①에 "외부 점유"로 반영할 수 있다.
- 포트폴리오에서는 세 계층을 분리해 보여주는 것이 설득력이 있다: ① 예약 충돌 검사기(정점·간선 충돌, 구역 용량), ② 런타임 이격 감시기(로봇-로봇·로봇-지게차 최소 거리, 임계 미만이면 감속·정지 명령과 근접사고 이벤트 기록), ③ 로봇 측 보호정지 모의(관제와 무관하게 작동).

### B-3. 사고 조사와 재발 방지 관행

| 관행 | 확인된 사례 | 포트폴리오 적용(추론) |
|---|---|---|
| 경보에 조사 맥락을 붙인다 | MiR 경보 context: 교착이면 관련 로봇·미션·맵·자원·활성 구역, 오류면 로봇·맵 [MiR-F] | 경보 레코드에 로봇 상태 스냅숏·예약 테이블·주변 유닛을 함께 저장 |
| 자동 오류 로그 | 관제가 오류를 감지하면 Fleet error log 자동 생성, 로봇 로그 병행 제출, 대피 종료 시 로그 자동 생성 [MiR-F] | 사건 발생 시 자동 번들 생성 |
| 변경 이력 확인 | 비상 후 Event viewer로 사이트 수정 여부·추가 문제 점검 [MiR-F] | 맵·구역·설정 변경 감사 로그 |
| 블랙박스(링버퍼) | rosbag2 snapshot 모드: 순환 버퍼에 계속 쓰다가 서비스 호출 시 저장, 사건 전후만 남겨 디스크 절약 [ROSBAG] | 경보 트리거 시 전후 N초 저장 |
| 사고 전후 재생 | InOrbit Time Capsule: 사건 전후 구간의 데이터 시각화·지도 경로·로그, 히트맵으로 문제 지점 파악 [ITR]. Dexory 공고: 필드 엔지니어가 새벽 2시에 로봇이 왜 멈췄는지 확인하는 인터페이스(O16) | 타임라인 슬라이더로 사건 재생 |
| 반복 지점 분석 | "같은 구역에서 경보가 많으면 그 구역의 원인을 검토, 위치 기반 문제는 구역으로 해결" [MiR-F]. Locus는 혼잡 지점을 붉은 점으로 표시(검색 요약) | 경보·보호정지·근접사고 히트맵 |
| 근접사고 보고 | OSHA는 근접사고 보고를 의무화하지 않지만 조사를 권고, 근접사고 보고 양식 제공 [OSHA]. 혼합 교통 벤더 Essensium은 "기록되지 않는 근접사고"를 문제로 지적(벤더 주장, 2026-05-26, [Essensium](https://essensium.com/mixed-fleet-safety/)) | 최소 이격 거리 임계 미만을 근접사고로 자동 기록 |
| 훈련·AAR·시정조치 추적 | Waymo Incident Readiness: 훈련 설계·실행, AAR, RCA, 시정조치 할당·종결 추적(O7) | 사고 보고서 템플릿에 시정조치 항목과 상태 |
| 비상 계획 문서화 | MiR 비상 계획표: 상황 / 위험 / 예방 조치 / 현장 조치 (관제 정전, 로봇 정전, Wi-Fi 정전, 건물 비상, 사이트 데이터 삭제, DB 손상) [MiR-F] | 같은 4열 형식으로 자체 비상 계획표 작성 |
| 현장 인사이트 환류 | 베어 Field SW: 사이트 반복 패턴을 core 팀에 피드백(K28). Robust AI: 해결 내역 KB화·제품 hardening(O17). 모빈: 관제 시스템 개선점 리포트(K13) | "재발 방지 조치" 칸을 필수로 |
| 통신·로그 경로 | 우아한형제들 딜리: MQTT `+/telemetry`를 IoT Rule로 모아 SQS→Kafka→백엔드, 로봇별 `<robotId>/topic/command` 구독, CloudWatch Logs·S3 로그, 로봇별 토픽 권한 격리 [WOOWA] | 토픽 설계·권한 분리를 문서에 명시 |

---

## C. 결론: 관제 포트폴리오가 채용 담당자에게 설득력을 가지려면

### C-1. 반드시 보여줘야 할 것

**기능**

1. **3계층 충돌 관리**: 예약 기반 계획 충돌 검사, 런타임 이격 감시(사람·지게차 포함), 로봇 측 보호정지 모의. 각 계층이 막는 사고와 못 막는 사고를 표로 설명(근거: B-2).
2. **로봇 상태 기계와 경보 수명주기**: 로봇 상태(운행·대기·충전·보호정지·비상정지·오류·통신 두절·위치 상실·원격 조작 중)와 경보 상태(발생→확인→담당 지정→조치→종결). 경보마다 1차 자동 대응과 관제원 조치를 연결(근거: K11·K15, MiR·OTTO 상태 목록).
3. **통신 두절 정책**: 마지막 상태 유지, 점유 자원 보존, 타임아웃 정책, 재연결 시 상태 동기화와 중복 명령 방지(근거: MiR 5분 규칙, 국내 고시 횡단 중 단절 규정).
4. **원격 정지와 원격 조작 인계**: 개별·전체 정지, 영상 확인 후 수동 조작, 재개 조건(E-stop은 수동 해제, 해제 후 자동 출발 금지)(근거: 국내 고시).
5. **교착 감지·해소**와 경보 context(관련 로봇·미션·자원)(근거: MiR 경보).
6. **충전 관리**: 긴급 배터리 우선 배정, 충전 실패 재배정(근거: MiR 7% 규칙, OTTO Failed Target Find).
7. **설비 연동과 실패 주입**: 모의 엘리베이터·자동문·충전기, 응답 지연·거부 시 대기·재시도·재계획(공개 문서에서 절차를 확인하지 못한 영역이므로 자체 설계 근거를 문서화).
8. **대피 모드**: 대피 구역→대피 위치, 구역 밖 일시정지, 신규 미션 차단, 종료 후 미션 복구, 자동 로그(근거: MiR 대피).
9. **상위 시스템 인터페이스**: 모의 WMS/MES 주문 입력·결과 회신, MQTT/REST/gRPC, 필요하면 VDA 5050 어댑터(근거: 한국 공고 16/33).
10. **국내 관제장치 요건 충족 화면**: 전 로봇 식별, 위치·속력·배터리(주행가능 거리)·통신 감도·상태 실시간 표시, 통행 불가 통보(근거: 국내 고시 8항).

**지표(측정해서 표로 제시)**

| 지표 | 왜 필요한가(근거) |
|---|---|
| 처리량(작업/시간), 사이클타임, 성공률 | O1 UPH·Lines/Hour, K19 성공률·사이클타임, K23 Pallet/Hour |
| 가동률(uptime)·가용률, utilization | O1, O9, O11, K19 |
| MTTR(경보 발생→정상화), 경보 확인까지 시간 | O11 MTTR·SLA, K11 정상화 확인 |
| 개입률(로봇·시간당 원격 개입·현장 출동 수) | 원격 지원·현장 회수 조직 존재(O10, O18, WAYMO) — 지표화는 추론 |
| 교착 발생 수·해소 시간 | MiR 교착 경보, K16 교착 방지·복구 |
| 보호정지·근접사고 수와 위치 분포 | MiR 위치 기반 경보 분석, OSHA 근접사고 조사 권고 |
| 대수 증가에 따른 처리량·대기시간·교착 곡선 | K16 확장성, O13 대규모 fleet |
| 원격 정지 명령 지연, 통신 두절 복구 시간 | WAYMO 지연 수치 공개 사례, MiR Wi-Fi 영향 |
| plan vs actual | O5 |

**문서화**

1. 아키텍처 문서와 설계 결정 기록(왜 예약 방식인지, 왜 이 타임아웃인지).
2. 인터페이스 명세: 메시지 스키마·API 버전과 변경 이력(K14 "API 명세·DB 스키마 변경 이력관리").
3. 상황별 Runbook·SOP: B-1 표 형식(감지→1차 자동→관제원→현장→재개→기록).
4. 비상 계획표: MiR 형식(상황/위험/예방/현장 조치).
5. 사고 보고서 템플릿: 타임라인, 재생 링크, 원인, 시정조치와 상태(O7 AAR·RCA).
6. 시험 보고서: 결함 주입 시나리오별 통과 기준과 결과(K10 통신 단절 시나리오 테스트, K20 테스트 자동화).
7. 인수인계 노트 양식(K11, K15, O5).
8. 한계와 미검증 항목 명시.
9. 제출 형태: 한국 공고는 포트폴리오 제출을 필수로 적거나(K17) 실무 과제·PT를 둔다(K1). README·시연 영상·재현 명령을 한곳에 둔다(추론).

### C-2. 흔히 빠지는 것 (추론, 근거 병기)

| 빠지는 것 | 왜 문제인가 |
|---|---|
| 해피 패스만 있는 데모 | 채용 요구 1순위가 장애 대응(26/53)이고, 개발형 공고도 예외·복구·통신 단절 테스트를 요구(K10·K17·K19) |
| "경로 계획에서 충돌 검사했으니 안전" 주장 | 상용 문서는 로봇 자체 안전 계층을 따로 두고 한계도 명시([OTTO], [MiR-F]) |
| 통신 두절·재접속 시 자원·명령 정합성 | MiR는 두절 로봇의 자원 보존·5분 규칙을 문서화할 만큼 중요하게 다룸 |
| 재개 조건 미정의(E-stop 해제 후 자동 출발 등) | 국내 고시가 명시적으로 금지 |
| 경보 확인·담당·인수인계가 없는 UI | 운영형 공고의 핵심 업무가 1차 조치·전달·인수인계(K11·K15·O5) |
| 수치 없는 성능 주장, 대수 확장 곡선 부재 | KPI 요구 11/53, 확장성 명시(K16·O13) |
| 로그·재생 부재로 RCA 불가 | 로그·RCA 8/53, 재생 도구가 상용 제품 기능(InOrbit, Dexory) |
| 상위 시스템·설비 연동 부재 | 한국 개발형 공고의 최다 요구(16/33) |
| 배포·운영성 부재(한 줄 기동, 설정 분리, 장시간 운용) | Docker·K8s 13/53, K14 무중단·재시도 요구 |
| 통제 불가 유닛을 단순 장애물로만 처리하고 근접사고를 기록하지 않음 | 혼합 교통 관리 제품(ANT locator)과 근접사고 조사 관행(OSHA) |

---

## 부록: 수집 도구 메모

- 공고별 태그 집계 스크립트: `tags.py`(이 보고서와 같은 디렉터리). 태그 판단은 수작업이며 "or" 목록에 나열된 언어는 모두 1회씩 셌다.
- 이미지 공고 판독분: K2·K3(클로봇), K14(삼익THK), K15(벰로보틱스), K16·K17·K18(레인보우로보틱스), K23(스피드플로어).
- 원문 접근 실패: 토르드라이브(DNS), startup.jobs(403, 쿠팡·스타쉽), Teradyne(채용 완료), 점핏 일부(404), OTTO 헬프센터(로그인 필요).

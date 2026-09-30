# 팔레트 물류센터 로봇 관제 시뮬레이터용 실제 제원 조사

- 조사일: 2026-09-30
- 목적: 입고 도크 → 랙 적치 → 피킹 → 출고 흐름에서 AMR(자율 지게차 AGF, 토트/카트 AMR)·사람·수동 지게차가 섞인 현장을 VDA 5050 마스터 컨트롤로 모델링하기 위한 수치 확보
- 표기 규칙
  - **[원문]** 제조사 데이터시트·표준·법령 원문(PDF/페이지)을 열어 수치를 확인함
  - **[요약]** 원문 페이지를 열었으나 도구 요약으로만 확인함(표·문장 단위 대조는 못 함)
  - **[2차]** 벤더 블로그·기사·유통사 페이지 등 2차 출처, 또는 검색 결과 요약
  - **[추론]** 위 수치로 계산하거나 해석한 값
  - **[가정]** 공개 근거를 찾지 못해 시뮬레이터 기본값으로 제안하는 값
- 단위 환산: 1 in = 25.4 mm, 1 ft = 0.3048 m, 1 mph = 0.44704 m/s, km/h ÷ 3.6 = m/s

---

## 0. 한눈에 보기 (시뮬레이터 권장값)

### 0.1 차종별 권장 파라미터

| 파라미터 | AGF-CB (카운터밸런스형 자율지게차, 기본) | AGF-PJ (팔레트잭형 AMR, 선택) | AMR-T (토트/카트 AMR) | 수동 CB 지게차 2.5 t | 수동 리치트럭 1.4–1.6 t | 보행자 |
|---|---|---|---|---|---|---|
| 길이(포크 포함) | 2.70 m | 1.93 m | 0.80 m | 3.60 m | 2.42 m | 원형 Ø0.5 m [가정] |
| 폭 | 1.30 m | 0.82 m | 0.60 m | 1.17 m | 1.12 m | 〃 |
| 적재 시 길이 | 포크면까지 차체 ≈1.5 m + 팔레트 1.1–1.2 m ≈ 2.7 m [추론] | 1.93 m (팔레트가 포크 위) | 카트 결합 시 1.2×0.8 m [가정] | 포크면까지 2.61 m + 팔레트 1.2 m ≈ 3.8 m [추론] | 포크면까지 1.27 m + 팔레트(리치 후퇴 시 차체 안) | — |
| 최고속도 | 공차 1.5 / 적재 1.3 m/s 전진, 포크 방향 0.6 m/s, 선회 중 0.6(공차)/0.3(적재) m/s | 1.5 m/s(최대 적재) | 설계 2.0, 운영 1.5 m/s | 설계 5.1–5.6 m/s → 현장 제한 2.78 m/s(10 km/h), 보행·도크 구역 1.39 m/s(5 km/h) | 설계 3.9 m/s → 현장 제한 동일 | 1.31 m/s (범위 1.0–1.7) |
| 가속/감속 | 0.3 / 0.5 m/s² [추론+가정] | 0.3 / 0.5 m/s² | 0.5 / 0.8 m/s² [가정] | ≈1.0 (적재)–1.3 (공차) / 1.5 m/s² [추론+가정] | ≈0.9 / 1.5 m/s² [추론+가정] | 1.0 m/s² [가정] |
| 각속도 상한 | 0.5 rad/s [가정] | 0.5 rad/s [가정] | 1.0 rad/s | 조향 모델 | 조향 모델 | — |
| 회전 방식 | THREEWHEEL, 제자리 회전 불가, 최소 선회반경 1.42 m, 자율 모드 계획 반경 2.0 m | 선회반경 1.56 m, 제자리 회전 불가 [추론] | DIFF, 제자리 회전 | 후륜 조향, Wa 2.26 m | Wa 1.61 m | 전방향 |
| 리프트 속도 | 상승 0.10(적재)/0.15(공차), 하강 0.18(적재)/0.10(공차) m/s | 시간으로 모델링(아래) | 리프트 4 s | 상승 0.60/0.65, 하강 0.58/0.50 m/s | 상승 0.44/0.70, 하강 0.50, 리치 0.20 m/s | — |
| 최대 리프트 | 3.0–4.5 m | 자동 0.85 m | 0.06 m | 3.2–4.5 m | 6–11 m | — |
| 픽/드롭 시간 | 바닥 픽 30–40 s, 드롭 25–30 s. 높이 h[m]: pick = 30 + h/0.15 + h/0.18, drop = 25 + h/0.10 + h/0.10 [추론] | 전면 픽 ≤40 s, 드롭 ≤30 s; 측면 ≤55/≤50 s | 카트 결합·해제 8–15 s [가정], 리프트 4 s | 15–25 s + 리프트 [가정] | 20–30 s + 리프트 [가정] | 작업 정지 시간 분포로 |
| 필요 통로 | 적치 3.15 m, 선회 통로 1.85 m, 일방 3.15 m(전속)/2.30 m(감속), 양방 5.39/4.11 m, 90° 진입 4.27 m | 일방 2.10/1.55 m, 90° 선회 1.90 m, 양방 3.80/2.90 m | 일방 1.10–1.45 m, 양방 1.98–3.0 m | Ast 3.93–4.15 m | Ast 2.52 m(+0.2 여유 = 2.72 m) | 보행로 ≥1.0 m |
| 안전 스캐너 | 3대 360° (전·후·측) + 상부 3D | 3대 360° + 3D 카메라 5대 | 2대 전·후 | 운전자 | 운전자 | — |
| 에너지 | Li 약 8 h 운전 [가정], 1 h로 10→90% 충전 | 7 h 40 min(혼합 부하), 도킹 18 s·언도킹 9 s | 6–13 h 운전, 18–50 min 충전 | 교대 시 배터리 교체 [가정] | 〃 | — |
| VDA 5050 2.x | agvClass FORKLIFT, agvKinematic THREEWHEEL | FORKLIFT, THREEWHEEL [추론] | CARRIER(리프트·카트) 또는 CONVEYOR(롤러탑), DIFF | 관제 대상 아님(비연결 이동체) | 〃 | 〃 |

근거는 1~5장, 계산은 8장에 있다.

### 0.2 현장 치수 권장값

| 항목 | 권장값 | 근거 요약 |
|---|---|---|
| 랙 통로(AGF + 2.5 t CB 혼용) | 4.0 m | 2.5 t CB Ast 3.93–4.15 m, AGF 적치 3.15 m |
| 랙 통로(1.5–2 t CB·AGF만) | 3.3–3.5 m | Mecalux CB 3.2–3.5 m, VNP15 3.15 m |
| 랙 통로(리치 전용) | 2.9 m | R-MATIC·REACHY 2.9 m(EUR), Mecalux 2.7–2.9 m |
| 랙 통로(VNA) | 1.6–1.8 m | Mecalux 1.5–1.9 m |
| 주 통로(크로스 통로) | 차량 4.5 m + 분리 보행로 1.0–1.2 m [추론] | AGF 양방향 4.11(감속)–5.39 m(전속), KOSHA 2대 통행 = 2×최대폭+0.9 m |
| 크로스 통로 간격 | 랙 30–38 m마다 | St. Onge: 100–125 ft 뒤 약 12 ft 크로스 통로 |
| 도크 도어 중심 간격 | 4.27 m (최소 3.66 m) | McGuire 12 ft 최소·14 ft 권장 |
| 도크 도어 크기 | 2.74 m × 3.05 m | Steelco 9 × 10 ft |
| 레벨러 뒤 차량 이동대 | ≥4.6 m | McGuire 15 ft |
| 도어당 스테이징 | 11 t 윙바디 1대분 16 팔레트 = 2열 × 8칸, 깊이 약 9.5–10 m [추론] | Sendy 11 t 16 팔레트, Precision Integrators 도크~첫 기둥 30 ft(구형)/50–60 ft(신형) |
| 속도 구역 | 주 통로 10 km/h, 보행 혼재·도크·교차로 5 km/h | KOSHA 교육자료 구내 10 km/h 이하, UKMHA 8 km/h·보행 구역 5 km/h |
| 팔레트 | T11 1100×1100 (기본), T12 1200×1000, EUR 1200×800 | ISO 6780, 기술표준원 고시 |

---

## 1. 자율 지게차(AGF)·팔레트 AMR 제원

### 1.1 요약 표

| 제품 | 형식 | 적재 | 최대 리프트 | 속도 | 치수(L×W×H) | 선회·통로 | 센서 | 등급 |
|---|---|---|---|---|---|---|---|---|
| OTTO Lifter (Rockwell) | 카운터밸런스 스태커 AGF | 1,200 kg (하중중심 623 mm) | 수동 2.7 m, 자율 1.0 m | 1.5 m/s | 2841(=차체 1607 + 포크 1234) × 1201 × 2155 mm, 2521 kg | 선회반경 1420 mm(수동)/1985 mm(자율); 최소 통로 일방 3152 mm(전속)/2300 mm(감속), 양방 5391/4113 mm; 90° 진입 4268 mm | 5× RealSense, 3× SICK microScan3(360°) | [원문] |
| VisionNav VNP15(L)-07 | 카운터밸런스 스태커 AGF | 1,500 kg (하중중심 500 mm) | 4.5 m (2.5–4.5 m 마스트 선택) | 전진 1.3(적재)/1.5(공차), 후진 0.6/0.6, 전진 선회 0.3/0.6, 후진 선회 0.3/0.6 m/s | 2543 × 1344 × 2085 mm, 2900 kg, 포크 1040 mm | 최소 선회반경 1423 mm; 최소 적치 통로 3150 mm, 최소 선회 통로 1850 mm (1200×1000 팔레트) | 전·후·측 레이저 스캐너, 상부 3D 레이저 | [원문] |
| VisionNav VNSL14 | 슬림 팔레트 스태커 | 1,400 kg | 1.6 m (표준), 3.0 m 옵션 | 1.3 m/s | — | 최소 적치 통로 2100 mm, 선회 통로 1.8 m; 회전반경 1173 mm, 최소 통로 약 2.4 m(마로솔 비교 글) | 360° 충돌 방지 | [2차] |
| Seegrid Lift CR1 | 카운터밸런스 AMR | 4,000 lb (1,814 kg) | 15 ft (4.57 m) | 최대 자동 5 mph (2.24 m/s) | 미공개 | 미공개 | 2D·3D LiDAR 5개, E-stop 3개 | [요약] |
| Seegrid Lift RS1 | 스태커 AMR | 3,500 lb (1,588 kg) | 6 ft (1.83 m) | 5 mph | 포크 42 in 표준 | 미공개 | 2D·3D LiDAR 5개 | [요약] |
| Seegrid Palion Pallet Truck S8 | 팔레트 트럭 | 3,629 kg | — | 1,800 mm/s | 914 × 1524 × 2921 mm로 표기(축 구분 모호, 폭×높이×길이로 보임 [추론]), 1,157 kg | — | — | [2차] |
| MiR1200 Pallet Jack | 팔레트잭 AMR | 1,200 kg | 1,120 mm(PDF 2025-06-10) / 1,140 mm(현 웹페이지); 자동 픽·플레이스 850 mm까지 | 1.5 m/s(최대 적재), 최대 가속 0.3 m/s²(무부하) | 1934 × 820 × 2120 mm, 750 kg, 포크 1182 mm | 선회반경 1564 mm; 운행 통로 2.10 m(전속)/1.55 m(감속), 90° 선회 1.90 m, U턴 1.95 m, 피벗 2.75 m, 문 1.40 m, 2대 교행 3.80/2.90 m | 안전 레이저 3대(360°), 3D 카메라 5, 3D LiDAR 1, 초음파 1 | [원문] |
| Linde R-MATIC | 자동 리치트럭 | 1.6 t | 최대 11.45 m | 최대 2 m/s | — | EUR 팔레트 통로 2.90 m | 3D 카메라, 레이저 스캐너 | [원문]+[요약] |
| BALYO REACHY | 로봇 리치트럭 | 1,400–1,600 kg | 최대 11 m | 최대 2 m/s | 마스트 접힘 높이 2350–4950 mm | 최소 통로 2.9 m | 360° PL-d 센서, 상부 3D LiDAR(64채널) | [요약] |
| Jungheinrich EKS 215a | 자동 스태커(고양정) | 1,500 kg (6 m에서 700 kg이라는 보도도 있음) | 6.0 m | 7 km/h (UK 페이지) / 9 km/h(다른 페이지 요약) | 전장 2492 mm | 미공개 | 인체 보호 스캐너 | [요약] |
| Toyota CB18 AGF | 카운터밸런스 AGF | 4,000 lb (1,814 kg) | 176 in (4.47 m) | 최대 6.7 mph (3.0 m/s, 만재) | 미공개 | 선회반경 56 in (1422 mm), basic right angle stack 74.85 in (1901 mm) | 360° LiDAR | [요약] |
| 현대위아 무인지게차 | 카운터밸런스형(보도 기준) | 최대 4 t | 미공개 | 약 6.5 km/h (1.81 m/s) | 미공개 | 미공개 | 라이다·비전·4면 안전 스캐너 | [2차] |

### 1.2 제품별 세부와 출처

**OTTO Lifter** — 데이터시트 OTTO-DS001C-EN-FEB2024 [원문]
- 적재 1200 kg, 하중중심 623 mm, 최고속도 1.5 m/s
- 선회반경 1420 mm(수동)/1985 mm(자율), 리프트 2.7 m(수동)/1.0 m(자율)
- 최소 통로 폭: 일방 3152 mm(전속)/2300 mm(감속), 양방 5391/4113 mm, 90° 진입 4268 mm, 팔레트 주변 최소 여유 200 mm
- 도킹 정밀도 ±50 mm, 도킹 타깃 사용 시 X ±13 mm·Y ±10 mm·Yaw ±1.0°
- 경사 1°(1.7 %) 초과 금지
- 치수 2841 mm(차체 1607 + 포크 1234) × 1201 × 2155 mm, 축거 1150 mm, 질량 2521 kg
- 배터리: 납산 24 V 375 Ah(교체식 또는 교대 사이 기회충전, 10→90 % 약 6 h), 리튬 25.6 V 270 Ah(자율 슈퍼차저 200 A, 10→90 % 약 1 h)
- 센서: RealSense 카메라 5, SICK microScan3 3대(360°), 청색 방향등 2, 비상정지 4
- 참고: 2022년 보도 요약에는 자율 리프트 76 cm(30 in)로 나오지만 2024 데이터시트는 1.0 m(39 in)이다. 최신 데이터시트 값을 쓴다.
- 출처: https://www.atlantaforklifts.com/wp-content/uploads/2022/09/2024-OTTOLIFTER-DS001C-EN-FEB2024.pdf

**VisionNav VNP15(L)-07** — 데이터시트 [원문]
- 속도(만재/공차): 직진 전진 1.3/1.5 m/s, 직진 후진 0.6/0.6, 전진 선회 0.3/0.6, 후진 선회 0.3/0.6 m/s
- 리프트 상승 100/150 mm/s, 하강 180/100 mm/s (만재/공차)
- 적치 정밀도 ±10 mm/±0.5°, 픽업 자기 보정 범위 ±100 mm/±10°
- 최소 적치 통로 3150 mm, 최소 선회 통로 1850 mm (팔레트 1200×1000, 포크가 길이 방향)
- 등판 5 %(3°), 단차 10 mm, 홈 30 mm
- 배터리 LFP 24 V/270 Ah, 수동·자동 충전, 배터리 교체 트롤리 옵션
- 안전: 전·후·측면 레이저 스캐너, 상부 3D 레이저, 비상정지 + 음향·광 경보
- 출처: https://howtorobot.com/sites/default/files/2023-04/VNP15(L)-07-CE-EN-2023V1.1.pdf
- VisionNav 공식 제품 페이지(visionnav.com/product/detail/...)는 조사 시점에 DB 오류로 열리지 않았다.

**VisionNav VNSL14** [2차]
- 1.4 t, 1.3 m/s, 최소 적치 통로 2100 mm, 리프트 1600 mm(표준) — https://www.agvnetwork.com/robot-showroom-2/ad/stacker,9/vnsl-14-v-07-slim-pallet-stacker-agv,205
- 최소 회전반경 1173 mm, 최소 통로 약 2.4 m(국내 유통사 비교 글), 같은 글에 시어로보틱스 SSR-1400 회전반경 1480 mm·통로 약 1.4 m, VNR16 리치형 최대 2.0 m/s·5.75 m(최대 9.455 m) — https://www.myrobotsolution.com/contents/success-story/yjN1oyVOHOW3QnHPrM3M

**Seegrid** [요약]
- Lift CR1: 15 ft, 4,000 lb, 최대 자동 속도 5 mph, 포크 42 in 표준(36–72 in 옵션), 2D·3D LiDAR 5개, E-stop 3 — https://seegrid.com/autonomous-mobile-robots/lift-cr1-amr/
- Lift RS1: 6 ft, 3,500 lb, 5 mph — https://seegrid.com/autonomous-mobile-robots/palion-lift/
- 라인업(EL1 5 ft 2 in/2,700 lb, Tow Tractor S7 10,000 lb) — https://seegrid.com/autonomous-mobile-robots/
- Palion Pallet Truck S8 [2차]: 3,629 kg, 1,800 mm/s, 1,157 kg, 자동 충전 — https://www.automatedwarehouseonline.com/seegrid-introduces-palion-amr-product-line/
- CR1 차체 치수·통로 폭은 공개 페이지에서 찾지 못했다. 브로셔 링크(hub.seegrid.com)는 404였다.

**MiR1200 Pallet Jack** — 사양서 2025-06-10 [원문]
- 픽업·플레이스 시간: 전면 진입 픽업 최대 40 s, 플레이스 최대 30 s; 좌·우 진입 픽업 최대 55 s, 플레이스 최대 50 s
- 충전 스테이션 도킹 최대 18 s, 언도킹 최대 9 s
- 최대 가속(무부하) 0.3 m/s², 최대 속도 1.5 m/s(평지 최대 적재)
- 공간: 운행 통로 2.10 m(전속)/1.55 m(감속), 90° 선회 1.90 m, U턴 1.95 m, 피벗 2.75 m, 문 1.40 m, 2대 교행 3.80 m(전속)/2.90 m(감속), 바닥 팔레트 전면 진입 앞 공간 3.5 m, 측면 진입 2.7 m, 거치대 앞 2.9 m, 팔레트 간 측면 0.30 m·끝단 0.25 m
- 충전 스테이션 앞 4.00 m(단일)/4.60 m(열), 스테이션 간 0.60 m
- 배터리 Li-ion 3개, 47.7 V, 102.6 Ah, 혼합 부하(90→10 %) 7 h 40 min, 경사 0 %(정격 하중)
- 출처: https://www.konicaminolta.com.au/wp-content/uploads/2025/06/MiR1200-Pallet-Jack-Specifications-2.101.pdf , https://mobile-industrial-robots.com/products/robots/mir1200-pallet-jack/specifications
- 불일치: 최대 리프트 1,120 mm(PDF) vs 1,140 mm(웹), 치수 1934×820×2120(사양서) vs 1902×830×1998(2023 제품군 원페이저 https://www.rarukautomation.com/wp-content/uploads/2024/03/MiR-One-pager-product-overview-full-range.pdf). 최신 사양서를 쓴다.

**Linde R-MATIC** [원문 일부]
- "pick-and-drop rates of just 66-85 seconds", 최대 2 m/s, EUR 팔레트 통로 2.90 m, 리프트 최대 11.45 m — https://www.linde-mh.com/en/Landingpage/R-Matic/
- 적재 1.6 t [요약] — https://www.linde-mh.com/en/Products/Automated-Trucks/R-Matic/
- 66–85 s가 픽 1회+드롭 1회인지, 높이 조건이 무엇인지는 원문에 없다.

**BALYO REACHY** [요약] — https://www.balyo.com/agv-reach-trucks/reachy
- 1400–1600 kg, 최대 11 m, 최대 2 m/s(4.4 mph), 최소 통로 2.9 m, 마스트 접힘 2350–4950 mm, 납산·TPPL·NMC 자동 도킹 충전

**Jungheinrich EKS 215a** [요약] — https://www.jungheinrich.co.uk/automation/mobile-robots/eks-215a-990468
- 1,500 kg, 6,000 mm, 7 km/h, 24 V Li-ion, 전장 2,492 mm, 반사판 레이저 내비게이션
- 다른 페이지 요약은 9.0 km/h·리프트 0.61 m/s, 6 m 높이 700 kg — https://www.jungheinrich.com/en/press-events/innovative-jungheinrich-solution-954860 [2차]

**Toyota CB18 AGF** [요약] — https://www.toyotaforklift.com/lifts/automated-guided-vehicles/cb18-automated-guided-forklift
- 4,000 lb, 176 in, 만재 최고 6.7 mph, 선회반경 56 in, basic right angle stack 74.85 in, 48 V Li-ion, 360° LiDAR
- 6.7 mph는 차량 최고속도이며 자동 운행 중 속도는 별도로 제한될 가능성이 높다 [추론].

**국내 제품**
- 현대위아 무인지게차 [2차]: 최대 4 t, 최고 약 6.5 km/h, 라이다·비전·안전 스캐너, 4면 스캐너, 팔레트 250 mm 이탈·10° 틀어짐까지 인식, 2027년 5월 기아 오토랜드 화성 투입 예정(2026-09-07 보도) — https://www.fnnews.com/news/202609070849505835 , https://www.hyundaimotorgroup.com/ko/news/hyundai-wia-forklift-logistics-robot-lineup
- 현대무벡스 [2차]: 2027년까지 가반하중 2 t·5 t·10 t 초대형 AMR 플랫폼 개발 국책과제(2024-10-07 보도). 기존 제품 제원은 공개되지 않음 — https://www.hyundaigroup.com/media/list.asp?mode=view&idx=13872
- 트위니 [2차]: 나르고 100 — 100 kg, 1.2 m/s, H×W×D 690×960×1470 mm, 8 h 이상 사용·2 h 이내 충전 (https://www.myrobotsolution.com/store/detail/71); 나르고 500 — 500 kg, 1.2 m/s, 치수 "1.37X1.06X1.38"(단위 표기 오류로 보이며 m로 추정 [추론]) (https://www.myrobotsolution.com/store/detail/73). twinny.ai/amr은 404.
- 시스콘 [2차]: SR(저중량)·HP(고중량)·DLF(무인지게차) 시리즈. 공개 페이지에 수치 제원 없음 — http://sysconrobotics.com/tech_1.html
- 리보틱스: 검색으로 해당 회사의 물류 AMR 제원을 찾지 못했다.
- 두산 로지피아 AGF 페이지(logipia.doosan-iv.com/automation-and-industrial/AGF)는 HTTP 530으로 열리지 않았다.

---

## 2. 토트/카트/컨베이어 탑 AMR 제원

| 제품 | 치수 L×W×H (mm) | 자중 | 적재 | 최고속도 | 회전 | 통로 | 운전·충전 | 센서 | 등급 |
|---|---|---|---|---|---|---|---|---|---|
| MiR250 | 800 × 580 × 300 | 94 kg | 250 kg | 2.0 m/s | 차동(제자리 회전 가능 [추론]) | 운행 통로 1450 mm(기본)/850 mm(최소화), 2대 교행 3000/1700 mm, 문 1500 mm | 최대 13 h(최대 적재)·17 h 30 min(무부하), 충전비 최대 1:16(10 min → 2 h 40 min) | nanoScan3 2대(전·후) | [원문] |
| MiR600 | 1350 × 910 × 322 | 240 kg | 600 kg | 2.0 m/s | 〃 | 2대 교행 3500 mm(기본)/2700 mm | 8 h 30 min(최대 적재)·11 h(무부하), 충전비 최대 1:12(10 min → 2 h) | microScan3 2대 | [원문] |
| OTTO 100 | 740 × 550 × 308 | 107 kg | 150 kg | 2.0 m/s, 도킹 0.3 m/s, 최대 회전 1.25 rad/s | Turn in place | 일방 1099 mm, 양방 1975 mm | 6 h(90→10 %), 18 min(10→90 %), 98 A | Hokuyo 안전 스캐너, 3D 카메라(전방 90°), ToF(후방) | [원문] |
| OTTO 600 | 1050 × 700 × 320 | 250 kg | 600 kg | 1.4 m/s, 도킹 0.3 m/s, 최대 회전 0.8 rad/s | Turn in place | — | 30 min(10→90 %), 자율 기회충전 기본 | 3D 카메라 2, LiDAR 2 | [원문] |
| Locus Origin | 518 × 579 × 1575 (20.4×22.8×62 in) | — | 36 kg | 공식 페이지 미기재 | — | — | 14 h/회 충전, 완충 50 min | 센서·카메라 8개 | [요약] |
| Geek+ P 시리즈(P500R 표) | 950 × 702 × 275 | 170 kg | 600 kg | "적재 2 m/s, 공차 1.6 m/s"로 표기 | 차동(선반 회전형 GTP) | — | — | — | [원문] |
| Fetch/Zebra Freight500 | 1452 × 1025 × 356 | 301 kg | 500 kg | 1.5 m/s | Turn in place | — | 약 9 h, 1 h에 90 % | SICK 2대(30 m, 275°), 3D 카메라 8(360°) | [원문] |
| Fetch/Zebra Freight1500 | 1688 × 1335 × 356 | 471 kg | 1500 kg | 1.5 m/s | Turn in place | — | 〃 | 〃 | [원문] |
| Omron LD-60 / LD-90 | — | 62 kg(LD-60/90) | 60 / 90 kg | 1800 / 1350 mm/s, 최대 회전 180°/s | 차동 | — | 15/12 h(LD-60 무부하/만재) | 안전 레이저 + 전방 저위 레이저 | [원문] |
| Omron LD-250 | 969 × 718 × 383 | 148 kg | 250 kg | 1200 mm/s, 최대 회전 120°/s | 차동 | — | 13 h(무부하)/10 h(만재), 20→80 % 2 h 10 min | 〃 | [원문] |

세부와 출처
- MiR250: https://mobile-industrial-robots.com/products/robots/mir250/specifications (경사 ±5 %@0.5 m/s, 충전 전류 최대 35 A)
- MiR600: https://mobile-industrial-robots.com/products/robots/mir600/specifications (도구 요약은 "30 min → 5 h 45 min"으로 달랐으나 페이지 원문 텍스트는 "10 min charging gives 2 h runtime"이다. 원문 텍스트를 쓴다.)
- OTTO 100 (OTTO-DS001F-EN-MAR2026): https://www.rockwellautomation.com/content/dam/rockwell-automation/documents/pdf/events/automate/otto-100-datasheet.pdf — 도킹 정밀도 ±10 mm(표준)/±5 mm(HADS), 통합 리프트 62 mm
- OTTO 600 (OTTO-DS001D-EN-JUL2024): https://cdn.wajax.com/wp-content/uploads/wpfiles/2024/08/OTTO600-DATA-SHEET.pdf — 48 V 21 Ah, 6,000 사이클, 적응형 PL-d 필드셋
- Locus Origin: https://locusrobotics.com/locusone/fleet/locus-origin-collaborative-robot . 2차 사이트(9srobotics 등)의 "5 m/s"는 협동 피킹 로봇으로 비현실적이라 채택하지 않는다 [추론]. 최고속도는 미확인.
- Geek+ P 시리즈: https://www.geekplus.com/technology-detail-page/p-series — 네 모델 탭이 같은 표(P500R 값으로 보임)를 반복하고, 적재 속도가 공차 속도보다 높게 표기되어 있어 라벨이 뒤바뀌었을 가능성이 있다 [추론]. 최소 리프트 시간 4 s, 리프트 60 mm, 최대 선반 880×880 mm.
- Fetch/Zebra: https://cssi.com/wp-content/uploads/cssi_fetch-freight500-1500-AMR-brochure.pdf (CartConnect500 272 kg, CartConnect100 57 kg, HMIShelf 78 kg)
- Omron LD: https://assets.omron.com/m/1bf71ca06f961b4b/original/LD-Series-Mobile-Robot-Datasheet.pdf , LD-250 리플릿 https://files.omron.eu/downloads/latest/brochure/en/i849_ld-250_leaflet_en.pdf?v=1 — 정지 반복정밀도(단일) 위치 ±65–75 mm, 표준 타깃 ±25 mm ±2°, 단차 10 mm, 경사 3 %. 90 °/s 이상 선속도 100 mm/s 초과 시 회전 45°/s 제한이라는 2차 요약도 있다(데이터시트 각주 미확인).

가감속: 토트 AMR 제조사 데이터시트에서 최대 가속도(m/s²)를 공개한 사례를 찾지 못했다. 팔레트급 MiR1200의 0.3 m/s²(무부하)만 확인했다. 토트 AMR 0.5 m/s²·감속 0.8 m/s²는 [가정]이며 실측 또는 벤더 확인으로 교체해야 한다.

---

## 3. 수동 지게차와 보행자

### 3.1 카운터밸런스 2.0–2.5 t (VDI 2198 형식 데이터시트)

| 모델 | 적재 | l1 전장 | l2 포크면까지 | b1 전폭 | Wa 선회반경 | Ast 1000×1200 가로 | Ast 800×1200 세로 | 주행(적재/공차) | 상승(적재/공차) | 하강 | 가속시간 | 등급 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Hyster H2.0A (디젤/LPG) | 2.0 t | 3534 | 2534 | 1160 | 2191 | 3861 | 4061 | 18.5/18.7 km/h | 0.61/0.65 m/s | 0.58/0.50 | 15 m까지 5.2/4.7 s | [원문] |
| Hyster H2.5A | 2.5 t | 3610 | 2610 | 1160 | 2263 | 3933 | 4133 | 18.4/18.7 km/h | 0.60/0.65 | 0.58/0.50 | 5.5/4.8 s | [원문] |
| 두산 D20SE-9 | 2.0 t | 3580 | 2580 | 1170 | 2220 | 3910 | 4110 | 20/22 km/h | 610/650 mm/s | 510/450 mm/s | — | [원문] |
| 두산 D25SE-9 | 2.5 t | 3635 | 2635 | 1170 | 2265 | 3950 | 4150 | 20/21 km/h | 600/650 mm/s | 510/450 mm/s | — | [원문] |
| Toyota Traigo48 8FBET20 (3륜 전동) | 2.0 t | — | 2005 | 1050 | 1650 | 미기재 | 미기재 | 16/16 km/h | 0.38/0.54 m/s | — | — | [원문] |
| Toyota Traigo48 8FBMT20 (4륜 전동) | 2.0 t | — | 2185 | 1050 | 1845 | 미기재 | 미기재 | 20/20 km/h | 0.38/0.54 m/s | — | — | [원문] |

- Hyster H2.5A는 90° 교차 통로(1200×1000 팔레트) 2057 mm도 표기한다.
- 출처: Hyster https://www.hyster.com/globalassets/coms/hyster/emea/documents/trucks-literature/internal-combustion-trucks-pneumatic-tyre/h2.0-3.5a/3991940-h2.0-3.5a-r177-tg-rev04-en-low-res.pdf ; 두산 https://www.doosan-iv.com/common/images/sub/product/diesel/D25SE-9_Brochure.pdf ; Toyota https://www.permatt.com/wp-content/uploads/2014/06/Traigo48_Brochure.pdf
- 해석 [추론]: 2.5 t급 Ast는 3.9–4.15 m, 3륜 전동 1.5–2 t급은 선회반경이 약 0.6 m 작아 Ast가 3.3–3.5 m 수준으로 내려간다(Mecalux CB 3.2–3.5 m와 일치). 사용자 가설 "CB 3.5–4 m"는 2.5 t 엔진식 기준으로 맞다.
- 가속 [추론]: Hyster H2.5A 적재 15 m 5.5 s, 최고 5.11 m/s를 넣고 "등가속 후 정속"으로 풀면 a ≈ 1.0 m/s², 공차는 ≈ 1.35 m/s².

### 3.2 리치트럭

| 모델 | 적재 | l1 | l2 | b1 | Wa | Ast 40×48 in 팔레트 | 주행 | 상승(적재/공차) | 하강 | 리치 | 가속시간 | 등급 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Jungheinrich ETV 214 | 1.4 t | 95.2 in (2418 mm) | 49.9 in (1267 mm) | 44.1 in (1120 mm) | 63.5 in (1613 mm) | 99.2 in (2520 mm), 바닥 적치 89.9 in (2283 mm) | 8.7 mph (14 km/h, 3.89 m/s) | 86.6/137.8 ft/min (0.44/0.70 m/s) | 98.4 ft/min (0.50 m/s) | 39.4 ft/min (0.20 m/s) | 4.8/4.4 s(거리 미표기) | [원문] |

- Ast 각주: "without 8 inch maneuvering space; second value is for floor storage". 즉 VDI 기본 여유 200 mm를 더하면 2.72 m [추론].
- 출처: https://www.kellytractor.com/eng/images/pdf/forklifts/jungheinrich/reach-trucks/ETV-ETM-214-216.pdf
- Mecalux: 리치트럭 작업 통로 2,700–2,900 mm, 10.5 m까지 — https://www.mecalux.com/blog/forklifts

### 3.3 실내 제한속도

- 한국: 산업안전보건기준에 관한 규칙 제98조는 최대제한속도 10 km/h 이하 차량을 제외한 차량계 하역운반기계에 대해 사업주가 작업장소에 맞는 제한속도를 정하고 운전자가 지키게 하도록 한다(수치를 정하지 않음) [2차: law.go.kr 검색 요약] — https://www.law.go.kr/LSW//lsSideInfoP.do?lsiSeq=263747&joNo=0098&joBrNo=00&docCls=jo&urlMode=lsScJoRltInfoR
- KOSHA 교육자료(2011): "구내 제한속도 표지판 부착 및 규정 속도(10km/h 이하) 준수" [원문] — https://oshri.kosha.or.kr/kosha/data/business/serviceSafetyBusinessData.do?mode=download&articleNo=399205&attachNo=222048
- 영국 UKMHA(BITA·FLTA): 창고 상한 5 mph(8 km/h), 보행자 밀집·고위험 구역 3 mph(5 km/h). 같은 글의 구역표: 주 통로 5–8 mph, 보행 구역 ≤3 mph, 교차로 2–3 mph, 랙 구역 3–5 mph, 도크 ≤3 mph, 코너 ≤3 mph [2차: 벤더 블로그] — https://www.transmon.co.uk/news/managing-forklift-speed-limits/
- 권장 [추론]: 시뮬레이터 수동 지게차 주 통로 10 km/h(2.78 m/s), 보행 혼재·도크·교차로 5 km/h(1.39 m/s), 랙 통로 내 5–8 km/h.

### 3.4 사람 보행 속도

- 건강한 성인 평상 보행 1.31 ± 0.02 m/s(95 % CI 1.27–1.35, 111개 연구군, n=13,609), 빠른 걸음 1.72 m/s(CI 1.64–1.81) [원문] — https://pmc.ncbi.nlm.nih.gov/articles/PMC7806575/
- 실내 보행은 실외보다 느리다는 보고가 있다 [2차] — https://ascelibrary.org/doi/10.1061/%28ASCE%29TE.1943-5436.0000251
- 권장 [추론+가정]: 평균 1.3 m/s, 1.0–1.7 m/s 범위(작업자 피킹 중 이동은 1.0–1.2 m/s가 현실적이라는 것은 [가정]).

---

## 4. 물류센터 레이아웃 표준 수치

### 4.1 팔레트 랙 통로

| 장비 | 통로 폭 | 근거 |
|---|---|---|
| 카운터밸런스 | 3,200–3,500 mm (Mecalux) / 2.5 t VDI Ast 3,861–4,150 mm | [원문] Mecalux 블로그, Hyster·두산 데이터시트 |
| AGF 카운터밸런스 | 3,150 mm (VNP15 적치), 3,152 mm (OTTO 일방 전속) | [원문] |
| 리치트럭 | 2,700–2,900 mm (Mecalux), 2,520 mm + 200 mm 여유 (ETV 214), 2.90 m (R-MATIC·REACHY, EUR 팔레트) | [원문]/[요약] |
| VNA(터렛) | 1,500–1,900 mm (Mecalux) | [원문] |

- Mecalux 통행 규칙 [원문]: 일방 통로는 차량 또는 최대 화물 치수 + 600 mm 이상(터렛 제외), 보행자도 쓰면 보행자 전용 3.3 ft(약 1.0 m)를 따로 확보, 양방 통로는 + 900 mm 이상 — https://www.mecalux.com/warehouse-manual/warehouse-design/forklift-aisle-width
- 같은 페이지: 마스트 접힘 상태 천장·보까지 500 mm 이상, 최상단 적재 레벨은 최대 리프트보다 200 mm 이상 낮게.

### 4.2 크로스 통로·건물

- St. Onge [원문 요약]: 보관 통로 100–125 ft(30.5–38.1 m)마다 약 12 ft(3.66 m) 크로스 통로, 대형 창고는 250–300 ft 이상 통로도 있음, 건물 깊이 350–400 ft 이상, 기둥 간격 35–60 ft(최근 50 ft 이상), 평균 층고 약 32 ft, 52 ft 트레일러 적·하역 팔레트를 스테이징할 깊이 확보 — https://stonge.com/insights/blog/dont-judge-a-distribution-center-by-its-cover/
- 권장 [추론]: 크로스 통로에 AGF 양방향 + 수동 CB 교행이 있으면 4.5 m(OTTO 감속 양방 4.11 m, KOSHA 2.5 t 2대 = 2×1.17+0.9 = 3.24 m보다 여유) + 분리 보행로 1.0–1.2 m.

### 4.3 도크

| 항목 | 값 | 출처 |
|---|---|---|
| 도어 중심 간격 | 최소 12 ft(3.66 m), 권장 14 ft(4.27 m) | McGuire 도크 설계 가이드 [원문] https://www.wbmcguire.com/files/2021-07/McGuire%20Dock%20Planning%20Guide.pdf |
| 〃 | 최소 12 ft, 최신 시설 14–16 ft(4.27–4.88 m) | Steelco [요약] https://www.steelcobuildings.com/warehouse-loading-dock-design-layout-door-spacing-and-truck-court-planning/ |
| 도어 크기 | 표준 9 × 10 ft(2.74 × 3.05 m), 대형 10 × 12 ft, 경량 8 × 8 ft | Steelco [요약] |
| 도크 높이 | 48–52 in(1.22–1.32 m) | Steelco [요약] |
| 레벨러 뒤 이동 공간 | 최소 15 ft(4.57 m), 도크면과 평행한 양방향 통행용 | McGuire [원문] |
| 도크~첫 기둥(스테이징 깊이) | 구형 약 30 ft(9.1 m), 신형 50–60 ft(15.2–18.3 m) | Precision Integrators [요약] https://www.precisionintegrators.com/blog-posts/staging-area-design-reducing-bottlenecks-at-receiving-and-shipping |
| 도어당 면적 | 최소 510 sq ft(60 ft 트레일러 기준), 계획 범위 500–1,500 sq ft(46–139 m²) | 〃 |
| 수신·출하·스테이징 면적 비중 | 전체 바닥의 15–25 % | 〃 |
| 도크 수 비율 | 물류센터 5,000–10,000 sq ft당 1개 | Steelco [요약] |

한국 차량 기준 [2차]: 5 t 윙바디 적재함 약 6,200 × 2,340 × 2,300 mm·팔레트 10개, 11 t 윙바디 약 9,100 × 2,400 × 2,400 mm·팔레트 16개 이하 — https://sendy.ai/blog/%ED%83%91%EC%B0%A8-%EC%9C%99%EB%B0%94%EB%94%94-%EC%A0%81%EC%9E%AC%ED%95%A8-%ED%81%AC%EA%B8%B0 . 윙바디는 측면이 열려 지게차가 옆에서 상하차하는 경우가 많으므로, 국내 현장을 모델링할 때는 "레벨러 도크"와 "측면 상하차 구역"을 따로 둘지 결정해야 한다 [추론].

스테이징 [추론]: 11 t 1대분 T11 16개를 2열 × 8칸으로 두면 1.1 m × 8 + 팔레트 간격 0.1 m × 7 ≈ 9.5 m, 레인 폭 2 × 1.1 + 여유 ≈ 2.5–2.8 m. 도어 간격 4.27 m 안에 2열 레인이 들어간다.

### 4.4 보행 통로

- 한국 산업안전보건기준에 관한 규칙은 일반 보행 통로의 최소 폭 수치를 두지 않았다(제22조는 통로 설치·유지와 통로면에서 높이 2 m 이내 장애물 금지). 7장 참고.
- KOSHA CODE M-51-2002: 지게차 1대 통로 = 최대 폭 + 60 cm 이상, 2대 통로 = 2대 최대 폭 + 90 cm 이상 [원문].
- Mecalux: 공유 통로 내 보행자 전용 약 1.0 m(3.3 ft) [원문].
- 권장 [추론]: 보행로 1.0–1.2 m, 차량 통로와 물리적 또는 도색 분리, 교차 지점 속도 5 km/h 구역.

### 4.5 팔레트 규격

| 규격 | 치수 | 출처 |
|---|---|---|
| T11 (한국 국가표준, 아시아) | 1100 × 1100 mm | 카고뉴스 2013-12-18: 기존 유일 국가표준 파렛트 [요약] http://www.cargonews.co.kr/news/articleView.html?idxno=22292 |
| T12 (2013년 국가표준 추가) | 1200 × 1000 mm | 〃 (KS T 1006 유닛로드 시스템통칙 등 개정) |
| EUR/EPAL | 1200 × 800 mm | ISO 6780:2003 [원문] https://cdn.standards.iteh.ai/samples/30524/ec0a6ce1ee454b98b00c8551c8ecb7df/ISO-6780-2003.pdf |
| ISO 6780 6종 | 1200×800, 1200×1000, 1219×1016, 1067×1067, 1100×1100, 1140×1140 | 〃 |

---

## 5. 운영 수치

### 5.1 AGF 사이클

| 근거 | 값 | 등급 |
|---|---|---|
| MiR1200 픽업/플레이스 | 전면 ≤40 s / ≤30 s, 측면 ≤55 s / ≤50 s | [원문] |
| Linde R-MATIC | "pick-and-drop rates of just 66-85 seconds" | [원문] |
| VNP15 리프트 속도 | 상승 0.10/0.15, 하강 0.18/0.10 m/s(만재/공차) | [원문] |
| 벤더 블로그 계획값 | 1대가 1200×1000 팔레트 기준 시간당 36회 이동, 리프트·적치 평균 25 s·이동 포함 1회 120 s | [2차] https://ifactoryapp.com/industries/fmcg/Autonomous-forklift-agv-fmcg-warehouse |
| 벤더 블로그 | FMCG 물류센터 2교대 하루 3,500–5,200 팔레트 | [2차] https://oxmaint.com/industries/fmcg/autonomous-forklift-agv-fmcg-warehouse |
| 트레일러 하역 | 53 ft 트레일러 30–45 min | [요약] Precision Integrators |

사이클 모델 [추론] (8장 계산):
- 높이 h에서 pick = 30 + h/0.15(공차 상승) + h/0.18(적재 하강), drop = 25 + h/0.10(적재 상승) + h/0.10(공차 하강) [s]
- h = 0 → 30/25 s, h = 3 m → 67/85 s, h = 4.5 m → 85/115 s
- h = 3 m 결과(67–85 s)가 R-MATIC "66–85 s"와 같은 범위에 든다. 우연일 수 있으나 기본값으로 쓰기에 무리가 없다는 근거로 본다.
- 편도 60 m 운반 예: 적재 60/1.3 = 46 s, 공차 복귀 60/1.5 = 40 s, 바닥 픽 30 s + 3 m 드롭 85 s, 선회·가감속 손실 20 s [가정] → 약 220 s, 이론 16회/h, 가동률 80 % 적용 시 약 13회/h. 블로그의 36회/h는 짧은 거리·바닥 간 이송 기준으로 보이며 랙 적치 흐름에는 과대할 수 있다 [추론].

### 5.2 AMR 피킹 처리량 [2차, 벤더 사례]

- Locus: 생산성 2배 이상, "2-3x" — https://locusrobotics.com/locusone/fleet/locus-origin-collaborative-robot
- GEODIS Dallas(Locus Vector): UPH 65 → 98+ — https://locusrobotics.com/wp-content/uploads/2024/03/GEODIS-Dallas-TX-Vector-Case-Study.pdf
- 그 밖에 검색 요약: GEODIS 약 100 → 175–200 UPH, Staples Canada 42 → 82 UPH, nGroup 124 → 285 LPH(출처 페이지별 원문 대조는 못 함)
- Geek+ P800: 스테이션당 최대 100 orders/h, 크로스도킹 300–500 cases/h(검색 요약, 원문 미확인)

### 5.3 충전·배터리

| 차종 | 방식 | 값 | 출처 |
|---|---|---|---|
| OTTO Lifter | 납산 교체 또는 교대 사이 기회충전(약 6 h, 10→90 %), 리튬 자율 슈퍼차저 | 리튬 약 1 h(10→90 %) | [원문] |
| VNP15 | 수동·자동 충전, 교체 트롤리 옵션 | 24 V 270 Ah | [원문] |
| MiR1200 | 자동 충전 스테이션 | 7 h 40 min(혼합), 도킹 18 s·언도킹 9 s | [원문] |
| BALYO REACHY | 도킹 스테이션 자동 충전 | 납산·TPPL·NMC | [요약] |
| OTTO 100 / 600 | 자율 기회충전 기본 | 18 min / 30 min(10→90 %), OTTO 100 운전 6 h | [원문] |
| MiR250 / MiR600 | 충전비 | 1:16 / 1:12 | [원문] |
| Locus Origin | — | 14 h, 완충 50 min | [요약] |
| Fetch Freight | 자율 도킹 | 약 9 h, 1 h에 90 % | [원문] |
| Omron LD-250 | 자동·수동 | 10–13 h, 20→80 % 2 h 10 min | [원문] |

시뮬레이터 권장 [추론]: AMR은 SoC 30 % 이하 또는 유휴 시 기회충전, 충전 속도는 "10 min 충전 ≈ 2 h 운전"(MiR600 수준)으로 시작, AGF 리튬은 1 h에 80 %p, 납산 AGF는 교대 시 교체 이벤트 10–15 min [가정].

---

## 6. VDA 5050 factsheet 구조

원문: VDA5050 GitHub 저장소 태그 2.0.0, 2.1.0, 3.0.0 (https://github.com/VDA5050/VDA5050). 2.1.0 본문 https://raw.githubusercontent.com/VDA5050/VDA5050/2.1.0/VDA5050_EN.md 6.15절, 스키마 https://raw.githubusercontent.com/VDA5050/VDA5050/2.1.0/json_schemas/factsheet.schema [원문].

### 6.1 최상위 (2.1.0)

`headerId`, `timestamp`, `version`, `manufacturer`, `serialNumber`, `typeSpecification`, `physicalParameters`, `protocolLimits`, `protocolFeatures`, `agvGeometry`, `loadSpecification`, `vehicleConfig`(선택)

- 스키마 필수: version, manufacturer, serialNumber, typeSpecification, physicalParameters, protocolLimits, protocolFeatures, agvGeometry, loadSpecification
- 토픽 메시지는 retained 플래그로 보내고, 마스터 컨트롤은 instantAction `factsheetRequest`로 요청한다.
- 본문은 factsheet가 "planning, dimensioning, and simulation of an AGV system"에 쓰일 수 있다고 적는다.

### 6.2 typeSpecification

| 필드 | 형 | 내용 |
|---|---|---|
| seriesName | string | 필수 |
| seriesDescription | string | 선택 |
| agvKinematic | string | `DIFF`(차동), `OMNI`(전방향), `THREEWHEEL`(3륜 구동 또는 유사 기구학) |
| agvClass | string | `FORKLIFT`, `CONVEYOR`(컨베이어 탑재), `TUGGER`(견인), `CARRIER`(리프트 유무 무관 적재 운반) |
| maxLoadMass | float64 | [kg] |
| localizationTypes | string[] | NATURAL, REFLECTOR, RFID, DMC, SPOT, GRID |
| navigationTypes | string[] | PHYSICAL_LINE_GUIDED, VIRTUAL_LINE_GUIDED, AUTONOMOUS (우선순위 순) |

### 6.3 physicalParameters

| 필드 | 단위 | 필수 여부(스키마) |
|---|---|---|
| speedMin | m/s, 최소 제어 연속 속도 | 필수 |
| speedMax | m/s | 필수 |
| angularSpeedMin / angularSpeedMax | rad/s | 선택 |
| accelerationMax | m/s², 최대 적재 기준 | 필수 |
| decelerationMax | m/s², 최대 적재 기준 | 필수 |
| heightMin | m | 본문 표는 필수 표기, 스키마 required에는 없음 |
| heightMax | m | 필수 |
| width | m | 필수 |
| length | m | 필수 |

### 6.4 protocolLimits

- `maxStringLens` { msgLen, topicSerialLen, topicElemLen, idLen, idNumericalOnly, enumLen, loadIdLen }
- `maxArrayLens` { "order.nodes", "order.edges", "node.actions", "edge.actions", "actions.actionsParameters", "instantActions", "trajectory.knotVector", "trajectory.controlPoints", "state.nodeStates", "state.edgeStates", "state.loads", "state.actionStates", "state.errors", "state.information", "error.errorReferences", "information.infoReferences" }
- `timing` { minOrderInterval(필수), minStateInterval(필수), defaultStateInterval, visualizationInterval } [s]

### 6.5 protocolFeatures

- `optionalParameters[]` { parameter(예: "order.nodes.nodePosition.allowedDeviationTheta"), support: SUPPORTED | REQUIRED, description }
- `agvActions[]` { actionType, actionDescription, actionScopes[]: INSTANT | NODE | EDGE, actionParameters[] { key, valueDataType: BOOL | NUMBER | INTEGER | FLOAT | STRING | OBJECT | ARRAY, description, isOptional }, resultDescription, blockingTypes[]: NONE | SOFT | HARD }
- 2.1 사전 정의 액션: startPause, stopPause, startCharging, stopCharging, initPosition, enableMap, downloadMap, deleteMap, stateRequest, logReport, pick, drop, detectObject, finePositioning, waitForTrigger, cancelOrder, factsheetRequest
- pick 파라미터: lhd(선택), stationType, stationName(선택), loadType, loadId(선택), height(선택, 바닥 기준 화물 하단), depth(선택, 지게차용), side(선택, 컨베이어 등). pick·drop은 instant 불가, node·edge 가능.

### 6.6 agvGeometry

- `wheelDefinitions[]` { type: DRIVE | CASTER | FIXED | MECANUM, isActiveDriven, isActiveSteered, position { x, y, theta }, diameter, width, centerDisplacement, constraints }
- `envelopes2d[]` { set, polygonPoints[] { x, y }, description } — 비적재·적재 외곽, 속도별 안전 필드 등
- `envelopes3d[]` { set, format(예: DXF), data, url, description }

### 6.7 loadSpecification

- `loadPositions[]` — state.loads[].loadPosition과 pick/drop 파라미터 lhd의 허용값. 없거나 비면 적재 장치가 없는 차량.
- `loadSets[]` { setName, loadType(예: EPAL), loadPositions[], boundingBoxReference { x, y, z, theta }, loadDimensions { length, width, height }, maxWeight [kg], minLoadhandlingHeight / maxLoadhandlingHeight [m], minLoadhandlingDepth / maxLoadhandlingDepth [m], minLoadhandlingTilt / maxLoadhandlingTilt [rad], agvSpeedLimit [m/s], agvAccelerationLimit / agvDecelerationLimit [m/s²], pickTime [s], dropTime [s], description }
- 시뮬레이터에 필요한 "적재 시 속도 제한"과 "픽·드롭 시간"을 담을 표준 자리가 여기다.

### 6.8 vehicleConfig (2.1 신설)

- `versions[]` { key, value }, `network` { dnsServers[], ntpServers[], localIpAddress, netmask, defaultGateway }
- 2.0.0 본문(VDA5050_EN_V1.md, 태그 2.0.0)에는 vehicleConfig가 없고 typeSpecification·physicalParameters 필드명은 2.1과 같다 [원문 grep].

### 6.9 3.0.0에서 바뀐 이름 (참고)

| 2.x | 3.0.0 |
|---|---|
| agvKinematic: DIFF / OMNI / THREEWHEEL | mobileRobotKinematics: DIFFERENTIAL / OMNIDIRECTIONAL / THREE_WHEEL (확장 가능 enum) |
| agvClass | mobileRobotClass (FORKLIFT, CONVEYOR, TUGGER, CARRIER, …) |
| maxLoadMass | maximumLoadMass |
| navigationTypes: AUTONOMOUS | FREELY_NAVIGATING |
| speedMin/Max, accelerationMax, decelerationMax, heightMin/Max | minimumSpeed/maximumSpeed, maximumAcceleration, maximumDeceleration, minimumHeight/maximumHeight |
| — | supportedZones[] (BLOCKED, LINE_GUIDED, RELEASE, COORDINATED_REPLANNING, SPEED_LIMIT, ACTION, PRIORITY, PENALTY, DIRECTED, BIDIRECTED) |

pickTime·dropTime은 3.0.0에도 남아 있다.

### 6.10 스키마 검증 결과와 주의점 [원문 확인 + 로컬 검증]

- 9절의 예시 factsheet 두 개(AGF-CB, 토트 AMR)를 2.1.0 `factsheet.schema`로 jsonschema 4.10.3(Draft 2020-12)에서 검증했고 오류 0건이었다.
- **2.1.0 스키마 결함**: `protocolFeatures.agvActions[].blockingTypes`가 `"type": "array"`와 `"enum": ["NONE","SOFT","HARD"]`를 같은 레벨에 둔다. 배열 값 자체를 enum과 비교하므로 `["HARD"]` 같은 정상 값이 검증에 실패한다(로컬 재현). 본문 표는 "array of enum"이다. 시뮬레이터 검증기에서는 blockingTypes를 빼거나 스키마를 `items.enum`으로 고쳐 써야 한다.
- **3.0.0 스키마 결함**: 태그 3.0.0의 `json_schemas/factsheet.schema`는 174행 `"unit": "m",` 뒤 후행 쉼표 때문에 표준 JSON 파서(Python json)로 읽히지 않는다.
- 3.0.0 main 브랜치 본문 버전은 3.0.0이며 dev/3.0.1, dev/3.1.0 브랜치가 있다.

---

## 7. 한국 규정

### 7.1 산업안전보건기준에 관한 규칙 (고용노동부령)

| 조문 | 내용 | 시뮬레이터 반영 | 등급 |
|---|---|---|---|
| 제11조(작업장의 출입구) | 주된 목적이 하역운반기계용인 출입구에는 보행자용 출입구를 따로 설치, 하역운반기계 통로와 인접한 출입구에서 접촉 위험이 있으면 비상등·비상벨 등 경보장치 | 도크·창고 출입구를 차량용/보행자용으로 분리, 인접 시 경보 이벤트 | [2차: 칼럼 인용] http://www.kmecnews.co.kr/news/articleView.html?idxno=44290 |
| 제21조(통로의 조명) | 통로에 75럭스 이상 채광 또는 조명시설(갱도·상시 통행하지 않는 지하실 등에서 휴대용 조명기구 사용 시 예외) | 조도 모델링 시 하한 75 lx | [2차] https://lbox.kr/v2/statute/%EC%82%B0%EC%97%85%EC%95%88%EC%A0%84%EB%B3%B4%EA%B1%B4%EA%B8%B0%EC%A4%80%EC%97%90%EA%B4%80%ED%95%9C%EA%B7%9C%EC%B9%99/%EB%B3%B8%EB%AC%B8%20%3E%20%EC%A0%9C1%ED%8E%B8%20%3E%20%EC%A0%9C3%EC%9E%A5%20%3E%20%EC%A0%9C21%EC%A1%B0 |
| 제22조(통로의 설치) | 근로자가 사용할 안전한 통로를 설치·유지, 통로면으로부터 높이 2 m 이내 장애물 금지 | 보행로 상부 2 m 공간 비움, 폭 수치 규정 없음 | [2차] https://www.law.go.kr/%EB%B2%95%EB%A0%B9/%EC%82%B0%EC%97%85%EC%95%88%EC%A0%84%EB%B3%B4%EA%B1%B4%EA%B8%B0%EC%A4%80%EC%97%90%20%EA%B4%80%ED%95%9C%20%EA%B7%9C%EC%B9%99/%EC%A0%9C22%EC%A1%B0 |
| 제38조(사전조사 및 작업계획서) | 차량계 하역운반기계등 작업은 사전조사와 작업계획서 작성 | 시나리오 문서화 | [2차] https://www.safechem.co.kr/2026/07/blog-post_27.html |
| 제39조(작업지휘자) | 작업지휘자 지정 | 제172조 예외 조건 | [2차] 〃 |
| 제98조(제한속도의 지정 등) | 최대제한속도 10 km/h 이하 차량 제외, 사업주가 작업장소에 맞는 제한속도를 정하고 운전자가 준수 | 속도 구역 파라미터화 | [2차] 위 3.3 링크 |
| 제172조(접촉의 방지) | 하역·운반 중 화물이나 기계에 접촉되어 위험해질 우려가 있는 장소에 근로자 출입 금지, 작업지휘자 또는 유도자를 배치해 유도하면 예외 | AGF 픽·드롭 작업 영역에 보행자 진입 금지 구역(동적 zone) | [2차] https://www.law.go.kr/LSW//lsSideInfoP.do?lsiSeq=280187&joNo=0172&joBrNo=00&docCls=jo&urlMode=lsScJoRltInfoR |
| 제173조 | 운전자 시야를 가리지 않도록 화물 적재 | 적재 높이 제한 | [2차] safechem 요약 |
| 제177조 | 100 kg 이상 화물 싣고 내리는 작업 시 관계 근로자 아닌 사람 출입 금지 | 도크 작업 구역 출입 통제 | [2차] safechem 요약 |

법령 조문은 국가법령정보센터 원문 페이지가 스크립트로 렌더링되어 도구로 본문을 가져오지 못했다. 위 문구는 law.go.kr 검색 결과 요약과 칼럼·해설 페이지에서 확인한 것이다. 인용해 외부 문서에 쓰려면 https://www.law.go.kr 에서 현행 조문을 다시 대조해야 한다.

### 7.2 KOSHA 지침

- **KOSHA CODE M-51-2002 「지게차의 안전작업계획서 작성지침」** [원문]
  - 6.1 운행통로의 폭: (1) 지게차 1대 통로는 운행 지게차 최대 폭에 60 cm 이상 여유, (2) 2대 통로는 2대 최대 폭에 90 cm 이상 여유
  - 주행 시 좌우 안정도 (15 + 1.1V) % 이내(최대 40 %), V는 구내 최고속도 km/h; 하역 시 좌우 안정도 6 % 이내
  - 포크 간격은 팔레트 폭의 1/2 이상 3/4 이하
  - 2002년 제정 지침으로 이후 KOSHA GUIDE 체계에서 통폐합됐을 수 있다. 현행 효력 여부는 확인하지 못했다.
  - 출처: https://www.moel.go.kr/local/busanbukbu/common/downloadFile.do?file_seq=21171050354&bbs_seq=62541&bbs_id=LOCAL5
- **KOSHA GUIDE G-10-2023 「작업장 내 운반차량의 운행에 관한 안전지침」** [원문]: 하역·적재 구역 일방통행 권장, 일방통행식(drive-through) 선적·하역으로 후진 제거, 보행 구역을 명확히 표시하고 가능하면 차량 도로와 떨어뜨림, 후진 구역 표시·경고음·감지 장치. 수치 규정은 없다 — https://oshri.kosha.or.kr/extappKosha/kosha/guidance/fileDownload.do?sfhlhTchnlgyManualNo=G-10-2023&fileOrdrNo=2
- **KOSHA 교육미디어(2011) 「지게차란?」** [원문]: 지게차 전용통로 확보, 구내 제한속도 표지판과 10 km/h 이하 준수, 모퉁이·출입구·사람 근처 감속과 경고음 — 위 3.3 링크

### 7.3 무인운반차 표준·인증

- **KS B ISO 3691-4** 「산업용 트럭 — 안전 요구사항 및 검증 — 제4부: 무인 산업용 트럭 및 시스템」(ISO 3691-4 부합) [요약] — https://www.kssn.net/search/stddetail.do?itemNo=K001010135700 . 검색 요약에는 "출발 전 최소 2 s 음향·광 신호" 요구가 언급되었으나 표준 본문은 유료라 확인하지 못했다.
- KS B ISO 3691-1(자주식 산업용 트럭), 3691-6(화물·인원 운반차), KS B ISO 13849-1(제어 시스템 안전 부품) — https://www.kssn.net
- MiR1200은 EN ISO 3691-4:2023·ISO 13849-1:2023 원칙에 따라 설계되었다고 표기 [원문], OTTO 100은 ISO 3691-4·ANSI/ITSDF B56.5·RIA R15.08-1 준수 표기 [원문].
- 국내 기업 사례: LG전자 저상형 AMR이 DNV에서 ISO 3691-4 인증(국내 기업 최초, 2024-07 보도) [2차] — https://www.aitimes.kr/news/articleView.html?idxno=31656
- 한국로봇산업진흥원 운행안전인증은 지능형 로봇법에 따른 **실외**이동로봇 대상이다. 실내 물류 AMR·AGF에 대한 국내 의무 인증 제도는 이번 조사에서 확인하지 못했다 [2차] — https://www.kiria.org/portal/cert/portalCertEstiSafe.do

---

## 8. 계산 기록 [추론]

- 인치·피트·mph 환산은 Python으로 계산했다(ETV 214: l1 2418, l2 1267, b1 1120, Wa 1613, Ast 2520/2283 mm, 14.0 km/h = 3.89 m/s, 상승 0.44/0.70 m/s, 하강 0.50 m/s, 리치 0.20 m/s).
- 수동 CB 가속: Hyster H2.5A 15 m 5.5 s(적재), vmax 18.4 km/h = 5.11 m/s. 등가속 후 정속 모델 d = vmax·t − vmax²/(2a) → a = vmax² / (2(vmax·t − d)) = 26.1 / 26.2 ≈ 1.0 m/s². 공차 4.8 s, 5.19 m/s → ≈ 1.36 m/s².
- ETV 214 가속시간 4.8 s는 측정 거리가 표에 없다. 10 m로 가정하면 ≈ 0.9 m/s².
- KOSHA 통로: 최대 폭 b1 → 일방 b1 + 0.6, 양방 2·b1 + 0.9. 2.5 t CB(1.17 m) 1.77 / 3.24 m, AGF VNP15(1.344 m) 1.94 / 3.59 m, OTTO Lifter(1.201 m) 1.80 / 3.30 m. 제조사 운행 통로(OTTO 일방 전속 3.152 m)가 이보다 넓으므로 AGF는 제조사 값을 쓴다.
- AGF 픽·드롭 시간 모델: pick(h) = 30 + h/0.15 + h/0.18, drop(h) = 25 + h/0.10 + h/0.10. 기준 30 s·25 s는 MiR1200 전면 픽 ≤40 s·플레이스 ≤30 s를 바닥 작업 대표값으로 줄여 잡은 [가정]이다.
- AGF 선회: VNP15 최소 선회반경 1.423 m와 OTTO 수동 1.42 m가 같다. OTTO는 자율 모드 1.985 m를 따로 제시하므로 경로 계획 반경은 2.0 m로 둔다.
- AGF 가감속: AGF 제조사 공개 가속도는 찾지 못했다. MiR1200 0.3 m/s²(무부하, 1.2 t급)를 하한 근거로 0.3 m/s²를 쓰고, 감속 0.5 m/s²는 [가정]이다. 비상정지 감속은 안전 필드 설계값이므로 별도 파라미터(예: 1.0 m/s²)로 두는 것이 좋다 [가정].

---

## 9. VDA 5050 2.1 factsheet 예시 (스키마 검증 통과)

값은 0장 권장값을 옮긴 것으로 제조사 값이 아니다. `blockingTypes`는 6.10의 스키마 결함 때문에 넣지 않았다.

```json
{
  "headerId": 1,
  "timestamp": "2026-09-30T00:00:00.00Z",
  "version": "2.1.0",
  "manufacturer": "SimFleet",
  "serialNumber": "AGF-CB15-001",
  "typeSpecification": {
    "seriesName": "AGF-CB15",
    "seriesDescription": "Sim model of 1.5 t counterbalance stacker AGF (VNP15/OTTO Lifter class)",
    "agvKinematic": "THREEWHEEL",
    "agvClass": "FORKLIFT",
    "maxLoadMass": 1500,
    "localizationTypes": ["NATURAL"],
    "navigationTypes": ["AUTONOMOUS", "VIRTUAL_LINE_GUIDED"]
  },
  "physicalParameters": {
    "speedMin": 0.05, "speedMax": 1.5,
    "angularSpeedMin": 0.02, "angularSpeedMax": 0.5,
    "accelerationMax": 0.3, "decelerationMax": 0.5,
    "heightMin": 2.1, "heightMax": 5.5, "width": 1.3, "length": 2.7
  },
  "protocolLimits": {
    "maxStringLens": {"msgLen": 0, "idLen": 0},
    "maxArrayLens": {"order.nodes": 0, "order.edges": 0},
    "timing": {"minOrderInterval": 0.5, "minStateInterval": 0.1, "defaultStateInterval": 1.0}
  },
  "protocolFeatures": {
    "optionalParameters": [{"parameter": "order.edges.maxSpeed", "support": "SUPPORTED"}],
    "agvActions": [
      {"actionType": "pick", "actionScopes": ["NODE"],
       "actionParameters": [
         {"key": "stationType", "valueDataType": "STRING"},
         {"key": "loadType", "valueDataType": "STRING"},
         {"key": "height", "valueDataType": "FLOAT", "isOptional": true}]},
      {"actionType": "drop", "actionScopes": ["NODE"],
       "actionParameters": [
         {"key": "stationType", "valueDataType": "STRING", "isOptional": true},
         {"key": "height", "valueDataType": "FLOAT", "isOptional": true}]},
      {"actionType": "startCharging", "actionScopes": ["INSTANT", "NODE"]},
      {"actionType": "stopCharging", "actionScopes": ["INSTANT", "NODE"]},
      {"actionType": "startPause", "actionScopes": ["INSTANT"]},
      {"actionType": "stopPause", "actionScopes": ["INSTANT"]},
      {"actionType": "cancelOrder", "actionScopes": ["INSTANT"]},
      {"actionType": "factsheetRequest", "actionScopes": ["INSTANT"]}
    ]
  },
  "agvGeometry": {
    "envelopes2d": [
      {"set": "unloaded", "polygonPoints": [
        {"x": 1.2, "y": 0.65}, {"x": 1.2, "y": -0.65}, {"x": -1.5, "y": -0.65}, {"x": -1.5, "y": 0.65}]}
    ]
  },
  "loadSpecification": {
    "loadPositions": ["LHD1"],
    "loadSets": [
      {"setName": "T11", "loadType": "T11", "loadDimensions": {"length": 1.1, "width": 1.1, "height": 1.4},
       "maxWeight": 1000, "maxLoadhandlingHeight": 4.5, "agvSpeedLimit": 1.3,
       "agvAccelerationLimit": 0.3, "agvDecelerationLimit": 0.5, "pickTime": 35, "dropTime": 30},
      {"setName": "EPAL", "loadType": "EPAL", "loadDimensions": {"length": 1.2, "width": 0.8, "height": 1.4},
       "maxWeight": 1000, "maxLoadhandlingHeight": 4.5, "agvSpeedLimit": 1.3, "pickTime": 35, "dropTime": 30}
    ]
  }
}
```

토트 AMR 예시(요지): agvKinematic `DIFF`, agvClass `CARRIER`, maxLoadMass 250, speedMax 1.5, angularSpeedMax 1.0, accelerationMax 0.5, decelerationMax 0.8, width 0.6, length 0.8, heightMax 0.36, loadSets `CART` pickTime 10 / dropTime 8. 이 예시도 같은 스키마 검증에서 오류 0건이었다.

---

## 10. 미확인·불일치 목록

1. AGF·토트 AMR 가감속도: 공개 수치가 MiR1200 0.3 m/s²(무부하) 하나뿐이다. 나머지는 [가정].
2. Locus Origin 최고속도: 공식 페이지 미기재, 2차 "5 m/s"는 채택하지 않음.
3. Seegrid CR1 치수·통로 폭: 공개 자료 미발견.
4. Linde R-MATIC 66–85 s의 정의(픽+드롭 1쌍인지, 높이 조건)가 원문에 없음.
5. Jungheinrich EKS 215a 속도 7 km/h vs 9 km/h, 적재 1.5 t vs 6 m에서 700 kg: 페이지별 상이.
6. MiR1200 최대 리프트 1,120 vs 1,140 mm, 치수 2025 사양서 vs 2023 원페이저 상이.
7. Geek+ P 시리즈 표의 속도 라벨(적재 2.0 > 공차 1.6)이 뒤바뀐 것으로 보임.
8. 국내 제품(현대무벡스, 시스콘, 리보틱스, 두산 AGF): 수치 제원 공개 자료 부족. 현대위아는 4 t·6.5 km/h만 공개.
9. 법령 조문: law.go.kr 본문을 도구로 가져오지 못해 검색 요약·해설 인용(제21조는 LBOX 검색 요약)에 기댐.
10. KOSHA M-51-2002의 현행 효력(통폐합 여부) 미확인.
11. AGF 시간당 처리량: 신뢰할 만한 1차 사례 수치를 찾지 못했다. 5.1의 사이클 모델로 산출하고 현장 데이터로 보정해야 한다.
12. KS B ISO 3691-4 세부 요구(출발 전 2 s 신호 등)는 표준 본문 미확인.

---

## 11. 출처 목록

차량(AGF·팔레트)
- OTTO Lifter DS001C: https://www.atlantaforklifts.com/wp-content/uploads/2022/09/2024-OTTOLIFTER-DS001C-EN-FEB2024.pdf
- VisionNav VNP15(L)-07: https://howtorobot.com/sites/default/files/2023-04/VNP15(L)-07-CE-EN-2023V1.1.pdf
- VisionNav VNSL14: https://www.agvnetwork.com/robot-showroom-2/ad/stacker,9/vnsl-14-v-07-slim-pallet-stacker-agv,205
- 무인지게차 비교(마로솔): https://www.myrobotsolution.com/contents/success-story/yjN1oyVOHOW3QnHPrM3M
- Seegrid CR1: https://seegrid.com/autonomous-mobile-robots/lift-cr1-amr/ , RS1: https://seegrid.com/autonomous-mobile-robots/palion-lift/ , 라인업: https://seegrid.com/autonomous-mobile-robots/
- Seegrid Palion: https://www.automatedwarehouseonline.com/seegrid-introduces-palion-amr-product-line/
- MiR1200: https://www.konicaminolta.com.au/wp-content/uploads/2025/06/MiR1200-Pallet-Jack-Specifications-2.101.pdf , https://mobile-industrial-robots.com/products/robots/mir1200-pallet-jack/specifications
- MiR 제품군 원페이저: https://www.rarukautomation.com/wp-content/uploads/2024/03/MiR-One-pager-product-overview-full-range.pdf
- Linde R-MATIC: https://www.linde-mh.com/en/Landingpage/R-Matic/ , https://www.linde-mh.com/en/Products/Automated-Trucks/R-Matic/
- BALYO REACHY: https://www.balyo.com/agv-reach-trucks/reachy
- Jungheinrich EKS 215a: https://www.jungheinrich.co.uk/automation/mobile-robots/eks-215a-990468 , https://www.jungheinrich.com/en/press-events/innovative-jungheinrich-solution-954860
- Toyota CB18: https://www.toyotaforklift.com/lifts/automated-guided-vehicles/cb18-automated-guided-forklift
- 현대위아: https://www.fnnews.com/news/202609070849505835 , https://www.hyundaimotorgroup.com/ko/news/hyundai-wia-forklift-logistics-robot-lineup
- 현대무벡스: https://www.hyundaigroup.com/media/list.asp?mode=view&idx=13872
- 트위니: https://www.myrobotsolution.com/store/detail/71 , https://www.myrobotsolution.com/store/detail/73
- 시스콘: http://sysconrobotics.com/tech_1.html

차량(토트·카트)
- MiR250: https://mobile-industrial-robots.com/products/robots/mir250/specifications
- MiR600: https://mobile-industrial-robots.com/products/robots/mir600/specifications
- OTTO 100: https://www.rockwellautomation.com/content/dam/rockwell-automation/documents/pdf/events/automate/otto-100-datasheet.pdf
- OTTO 600: https://cdn.wajax.com/wp-content/uploads/wpfiles/2024/08/OTTO600-DATA-SHEET.pdf
- Locus Origin: https://locusrobotics.com/locusone/fleet/locus-origin-collaborative-robot
- Geek+ P: https://www.geekplus.com/technology-detail-page/p-series
- Fetch/Zebra Freight: https://cssi.com/wp-content/uploads/cssi_fetch-freight500-1500-AMR-brochure.pdf
- Omron LD: https://assets.omron.com/m/1bf71ca06f961b4b/original/LD-Series-Mobile-Robot-Datasheet.pdf , https://files.omron.eu/downloads/latest/brochure/en/i849_ld-250_leaflet_en.pdf?v=1

수동 지게차·보행
- Hyster H2.0–3.5A: https://www.hyster.com/globalassets/coms/hyster/emea/documents/trucks-literature/internal-combustion-trucks-pneumatic-tyre/h2.0-3.5a/3991940-h2.0-3.5a-r177-tg-rev04-en-low-res.pdf
- 두산 D20/25/30SE-9: https://www.doosan-iv.com/common/images/sub/product/diesel/D25SE-9_Brochure.pdf
- Toyota Traigo48: https://www.permatt.com/wp-content/uploads/2014/06/Traigo48_Brochure.pdf
- Jungheinrich ETV 214/216: https://www.kellytractor.com/eng/images/pdf/forklifts/jungheinrich/reach-trucks/ETV-ETM-214-216.pdf
- 속도(UKMHA 인용): https://www.transmon.co.uk/news/managing-forklift-speed-limits/
- 보행 속도 메타분석: https://pmc.ncbi.nlm.nih.gov/articles/PMC7806575/

레이아웃·운영
- Mecalux: https://www.mecalux.com/warehouse-manual/warehouse-design/forklift-aisle-width , https://www.mecalux.com/blog/forklifts
- St. Onge: https://stonge.com/insights/blog/dont-judge-a-distribution-center-by-its-cover/
- McGuire: https://www.wbmcguire.com/files/2021-07/McGuire%20Dock%20Planning%20Guide.pdf
- Steelco: https://www.steelcobuildings.com/warehouse-loading-dock-design-layout-door-spacing-and-truck-court-planning/
- Precision Integrators: https://www.precisionintegrators.com/blog-posts/staging-area-design-reducing-bottlenecks-at-receiving-and-shipping
- 센디 화물차 규격: https://sendy.ai/blog/%ED%83%91%EC%B0%A8-%EC%9C%99%EB%B0%94%EB%94%94-%EC%A0%81%EC%9E%AC%ED%95%A8-%ED%81%AC%EA%B8%B0
- 팔레트: http://www.cargonews.co.kr/news/articleView.html?idxno=22292 , https://cdn.standards.iteh.ai/samples/30524/ec0a6ce1ee454b98b00c8551c8ecb7df/ISO-6780-2003.pdf
- 처리량(2차): https://ifactoryapp.com/industries/fmcg/Autonomous-forklift-agv-fmcg-warehouse , https://oxmaint.com/industries/fmcg/autonomous-forklift-agv-fmcg-warehouse , https://locusrobotics.com/wp-content/uploads/2024/03/GEODIS-Dallas-TX-Vector-Case-Study.pdf

VDA 5050
- 저장소: https://github.com/VDA5050/VDA5050
- 2.1.0 본문: https://raw.githubusercontent.com/VDA5050/VDA5050/2.1.0/VDA5050_EN.md
- 2.1.0 스키마: https://raw.githubusercontent.com/VDA5050/VDA5050/2.1.0/json_schemas/factsheet.schema
- 2.0.0 본문: https://raw.githubusercontent.com/VDA5050/VDA5050/2.0.0/VDA5050_EN_V1.md
- 3.0.0 본문·스키마: https://raw.githubusercontent.com/VDA5050/VDA5050/3.0.0/VDA5050_EN.md , https://raw.githubusercontent.com/VDA5050/VDA5050/3.0.0/json_schemas/factsheet.schema

한국 규정·표준
- 제98조: https://www.law.go.kr/LSW//lsSideInfoP.do?lsiSeq=263747&joNo=0098&joBrNo=00&docCls=jo&urlMode=lsScJoRltInfoR
- 제172조: https://www.law.go.kr/LSW//lsSideInfoP.do?lsiSeq=280187&joNo=0172&joBrNo=00&docCls=jo&urlMode=lsScJoRltInfoR
- 제22조: https://www.law.go.kr/%EB%B2%95%EB%A0%B9/%EC%82%B0%EC%97%85%EC%95%88%EC%A0%84%EB%B3%B4%EA%B1%B4%EA%B8%B0%EC%A4%80%EC%97%90%20%EA%B4%80%ED%95%9C%20%EA%B7%9C%EC%B9%99/%EC%A0%9C22%EC%A1%B0
- 제11조(칼럼 인용): http://www.kmecnews.co.kr/news/articleView.html?idxno=44290
- 제38·39·172·173·177조 해설: https://www.safechem.co.kr/2026/07/blog-post_27.html
- KOSHA M-51-2002: https://www.moel.go.kr/local/busanbukbu/common/downloadFile.do?file_seq=21171050354&bbs_seq=62541&bbs_id=LOCAL5
- KOSHA G-10-2023: https://oshri.kosha.or.kr/extappKosha/kosha/guidance/fileDownload.do?sfhlhTchnlgyManualNo=G-10-2023&fileOrdrNo=2
- KOSHA 교육미디어: https://oshri.kosha.or.kr/kosha/data/business/serviceSafetyBusinessData.do?mode=download&articleNo=399205&attachNo=222048
- KS B ISO 3691-4: https://www.kssn.net/search/stddetail.do?itemNo=K001010135700
- LG ISO 3691-4 인증: https://www.aitimes.kr/news/articleView.html?idxno=31656
- KIRIA 운행안전인증: https://www.kiria.org/portal/cert/portalCertEstiSafe.do

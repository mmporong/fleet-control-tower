# 회전 예약 실험 최종 문서 교차검토

## 판정

**PASS** — `README.md`, `docs/INCIDENT_REPLAY.md`, 실험 JSON, manifest의 핵심 진술과 수치가 서로 일치한다. 기존 후보는 기각됐고, 마지막 네 조건은 로컬 실험 후보를 보존하기 위한 제한된 결과이며, 안전 지표 일부가 악화됐고 공개 데모에는 반영되지 않았다는 한계가 문서에 명시돼 있다. 독립 코어 리뷰도 로컬 원인 검증용 보존은 승인하고 메인 반영·배포는 보류했다.

검토 시점의 HEAD는 후보 소스를 보존한 로컬 커밋 `af1687096f7aaadc96e18e74bb48c32350d5824e`다. 결과 JSON의 `72f95e3+dirty`는 실행 당시 식별자로 유지됐으며, 네 변경 파일의 해시가 로컬 커밋과 완전히 같다. 배포, 재촬영, 추가 28·148 조건 실험은 이 판정 범위에 포함하지 않았다.

## 진술과 근거

| 확인 항목 | 근거 | 판정 |
|---|---|---|
| 첫 후보 기각 | `rejected-margin051-sizing.json`은 `git=a08423a`, 28회 실행이다. AGF 10·AMR 16에서 seed 1 출고는 기준 38→후보 3건/h, seed 2는 35→0건/h다. `docs/INCIDENT_REPLAY.md` 13~15행의 설명과 일치한다. | PASS |
| 마지막 후보의 범위 | `candidate-exact-circles-4jobs.json`은 준비 1,800초, 측정 3,600초인 네 조건만 담는다. `docs/INCIDENT_REPLAY.md` 21·34행은 네 조건이 배포 판단이나 전체 28·148회 재실험을 대신하지 않는다고 밝힌다. | PASS |
| 표의 처리량·안전 수치 | 문서 25~28행의 적치·출고·토트·접촉·이동 중 접촉 값이 후보 JSON과 각 기준 JSON에 모두 일치한다. | PASS |
| 안전 악화 공개 | AGF 11 조건은 이동 중 접촉 0→1, AGF 10 seed 2는 접촉 4→5, AMR 10 조건은 접촉 0→2와 이동 중 접촉 0→1이다. 문서 30행은 새 이동 중 접촉과 전체 안전 개선 미입증을 명시한다. | PASS |
| 일부 처리량 악화 공개 | AGF 10 seed 1은 적치 36→33, 출고 38→36, 토트 108→107이다. 문서 30행은 일부 조건의 처리량 감소를 명시한다. | PASS |
| 무결성 지표 | 후보 네 조건에서 `deadlocks=0`, `lock_problems=0`, `mismatch=0`이다. 문서 30행과 일치한다. | PASS |
| 기준값 출처 | AGF 11 seed 1과 AMR 10 seed 1은 `d7cacc9` 재실행 JSON, AGF 10·AMR 16 두 조건은 `07ac6b5`의 `results/dc_sizing.json`에서 가져왔다. 문서 32행과 일치한다. | PASS |
| 기준 소스 차이 | `git diff --name-only 07ac6b5 d7cacc9 -- fleet` 결과는 `fleet/fms.py`, `fleet/server.py`, `fleet/sim.py`이며 `fleet/control.py`와 `fleet/vehicle.py`는 없다. 문서 32행의 제어·차량 동작 코드 동일 진술을 지지한다. | PASS |
| 8시간 기준 감사 | `baseline-shift-audit.json`의 세 시드가 모두 `kpiDifferences={}`, `reversalsMatch=true`다. 문서는 이 결과가 마지막 후보의 8시간 검증은 아니라고 구분한다. | PASS |
| 후보 소스 해시와 커밋 | 후보 JSON의 실행 전후 네 파일 SHA-256이 같고 `source_unchanged=true`다. 현재 파일과 로컬 커밋 `af16870`에서 추출한 파일 해시도 JSON 값과 일치한다. 문서 34행의 실행 당시 식별자와 보존 커밋 설명이 정확하다. | PASS |
| 전체 테스트 | `qa/turn-exact-full-test.log`는 `Ran 368 tests in 230.104s`, `OK`다. JSON은 `exitCode=0`, `changedDuringTest=[]`를 기록한다. 저장소의 `candidate-full-test.json/.log`는 QA 원본과 SHA-256이 각각 같다. README 세 곳도 368개로 정정됐다. | PASS |
| manifest와 코어 리뷰 | `manifest.json`은 `status=local-experiment-only`, `deploymentApproved=false`, 후보 커밋 `af16870`, 테스트 368/19/17을 기록한다. manifest가 나열한 17개 파일의 실제 SHA-256이 모두 일치한다. `core-review.md`는 로컬 보존 승인과 메인 반영·배포 보류를 구분하고 안전 회귀를 표에 노출한다. | PASS |
| 공개 데모 미반영 | 포트폴리오의 `public/fleet-3d/replay.json`은 기존 `120.0~240.0초`, `playbackRate=8` 기록 하나이며 같은 폴더에는 비교 기록이나 새 후보 기록이 없다. README 23행과 문서 40행은 새 재생 기능·회전 예약 후보·재촬영·GIF·포트폴리오 반영이 없다고 밝힌다. | PASS |
| 문서 형식 | `git diff --check -- README.md docs/INCIDENT_REPLAY.md` 통과. 실험 폴더의 모든 JSON은 `jq` 구문 검사에 통과했다. | PASS |

## 네 조건 수치 대조

| 조건 | 적치 | 출고 | 토트 | 접촉 | 이동 중 접촉 | 판정 |
|---|---:|---:|---:|---:|---:|---|
| AGF 11·AMR 16·seed 1 | 3→36 | 15→35 | 95→104 | 6→1 | 0→1 | 대표 정체 처리량 회복, 새 이동 중 접촉 발생 |
| AGF 10·AMR 16·seed 1 | 36→33 | 38→36 | 108→107 | 1→1 | 1→0 | 세 처리량 지표 감소 |
| AGF 10·AMR 16·seed 2 | 39→39 | 35→39 | 92→92 | 4→5 | 4→0 | 출고 증가, 전체 접촉 증가 |
| AGF 10·AMR 10·seed 1 | 12→12 | 46→46 | 82→82 | 0→2 | 0→1 | 처리량 동일, 두 안전 지표 악화 |

## 해시와 보존 상태

- 후보 결과: `candidate-exact-circles-4jobs.json` SHA-256 `a1d2286d152012ddc1687224bc723213996f070581b64934cd3c8af27d57f1ee`
- 후보 소스 보존 커밋: `af1687096f7aaadc96e18e74bb48c32350d5824e`
- 전체 테스트 JSON: QA 원본과 저장소 복사본 SHA-256 `910f412dc1d58e6b38eeea57fea08fe4ce8b8668b1f40ef42a204a3538c8eef5`
- 전체 테스트 로그: QA 원본과 저장소 복사본 SHA-256 `7f129d69a6810929fd8e29d715ed3562c3dcc7050899b8184bc7cac53ca548c9`
- 후보 소스: `fleet/control.py` `9834ef23…`, `fleet/geometry.py` `3d79d500…`, `fleet/vehicle.py` `377cd2e9…`, `tests/test_incident_recovery.py` `8915baaf…`; 후보 JSON의 실행 전후 값, 현재 파일, `af16870` 커밋의 파일이 모두 일치한다.
- 포트폴리오 worktree는 `feat/portfolio-3d-20261023...origin/master`에서 변경 파일 없이 clean 상태다.

## 남은 제약

- 마지막 후보는 네 조건의 실험 결과다. 전체 28·148회, 후보 8시간 교대, 커스텀 보호필드, 실차·현장 안전 검증 근거는 없다.
- 안전 지표가 일부 악화됐으므로 개선안 확정이나 배포 근거로 사용할 수 없다.
- 앞서 생성된 `qa/turn-circle-full-test.log`의 363개 결과는 이후 추가된 테스트를 포함하지 않는다. 최종 근거는 exact-source 368개 로그와 JSON이다.
- 사용자 중단 지시에 따라 새 벤치마크, 촬영, GIF, 포트폴리오 반영, 배포를 수행하지 않는다.

# VDMA LIF JSON 스키마 출처

| 항목 | 값 |
| --- | --- |
| 저장소 | https://github.com/Intralogistics-2X-LIF/Layout-Interchange-Format (VDMA 공식) |
| 파일 | `json_schemas/LIF.schema` → 이 폴더의 `LIF.schema` (바이트 단위 원본 그대로) |
| 기준 커밋 | `main` `983d482742a4aeb6a000570213693cbb14d2d88c` (2025-12-17) |
| 스키마를 추가한 커밋 | `c7e65705a133d4b56835195d7b5e320ff80ae321` (2025-11-06, "Added json schema for layout interchange format version 1.0") |
| 원본 URL | https://raw.githubusercontent.com/Intralogistics-2X-LIF/Layout-Interchange-Format/983d482742a4aeb6a000570213693cbb14d2d88c/json_schemas/LIF.schema |
| SHA-256 | `c1faf2665cecfd4b1fbaf213a20a8f7df89fefa58fbaf438766d8e46eaa0e0bd` |
| 스키마 방언 | JSON Schema draft-07 |
| 라이선스 | MIT, Copyright (c) 2025 VDMA e.V. 전문은 같은 폴더의 `LICENSE` (원본 SHA-256 `9239af5d…c8a1`) |

## 버전 표기

- 명세 문서 `LIF.md`: "Version 1.0.0 - September 2023". `release/V1.0` 브랜치의 커밋 `9aaa8a3`(2025-10-07)에서 1.0.0으로 표기가 바뀌었습니다.
- 스키마 파일: 버전 필드와 `$id`가 없습니다. 커밋 메시지에만 "version 1.0"이 적혀 있고, `lifVersion`의 예시 값은 `"1.0.0"`입니다.
- 명세 예제(10.1–10.19)의 `lifVersion`은 모두 `"0.11.0"`이고, 8.3.2절 설명에도 `(0.11.0)`이 남아 있습니다. 1.0 이전 초안 번호가 예제에 남은 것입니다.
- 이 저장소가 내보내는 파일은 `lifVersion: "1.0.0"`을 씁니다. 가져오기는 주 버전 1과, 명세 예제에 쓰인 0을 받습니다.
- `dev/V2` 브랜치(`35d377b2b3e6f9e1e02875f33343cdb879d14480`, 2026-09-24)에는 "Version 2.0.0 - September 2026" 초안이 있습니다. `vehicleType`을 `mobileRobotType`으로 바꾸고 draft 2020-12 스키마를 쓰지만, `main`에 공개되지 않은 작업본이라 여기서는 쓰지 않았습니다.

## 명세 본문과 스키마가 다른 곳

| 항목 | 명세 본문(8.3절 표) | 스키마 |
| --- | --- | --- |
| `layout.stations` | 선택(기울임꼴) | 필수 |
| `node.mapId` | 필수 | 선택 |
| `action.requirementType` | 필수 | 선택 |
| 8.4절 전체 구조 | 노드 액션에 `"required": boolean`, `"stationPosition "`(끝 공백) | `requirementType`, `stationPosition` |

명세 예제 19개(10.1–10.19)를 이 스키마로 검증하면 모두 실패합니다. `stations`가 없는 예제와 `stationHeight`를 문자열(`"0.55"`)로 쓴 예제가 섞여 있기 때문입니다. 명세 11.2절(V2 초안)도 스키마가 표준의 공식 일부는 아니라고 밝힙니다. 그래서 `fleet/lif.py`는 내보낼 때 스키마와 명세 본문을 모두 만족하게 쓰고(`stations`·`mapId`·`requirementType` 항상 포함), 가져올 때는 명세 본문 기준으로 검사합니다.

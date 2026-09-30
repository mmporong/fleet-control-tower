# VDA 5050 2.0.0 공식 JSON 스키마 출처

이 폴더의 스키마 6개는 VDA 5050 공식 저장소에서 받은 파일을 한 바이트도 바꾸지 않고 둔 것입니다. `fleet/schema.py`가 읽어 메시지를 검증합니다.

## 원본

| 항목 | 값 |
|---|---|
| 저장소 | https://github.com/VDA5050/VDA5050 |
| 태그 | `2.0.0` (lightweight tag) |
| 커밋 | `a8974b1f8bc412f72cca237f14c5578fe1b01224` (2021-11-25, "fix(typo): node Position -> nodePosition") |
| 경로 | https://github.com/VDA5050/VDA5050/tree/2.0.0/json_schemas |
| 받은 날 | 2026-09-30 (`git clone` 후 태그에서 추출) |
| JSON Schema 초안 | draft 2020-12 (`$schema`) |

| 파일 | 토픽 | git blob id (태그의 파일과 같음) | SHA-256 |
|---|---|---|---|
| `connection.schema` | connection | `2c4ab6d6c01d138d4c0f83ed028798f3c16481bb` | `80e65293bc3efa16593da425163909a063d8079b04050b2b9441cffbc7f9417e` |
| `factsheet.json` | factsheet | `3400d0aaa3f395a6a09566dcd7fdc06fa052dae1` | `9286a507eceb0d8784183d44878678742b0a33ec0c3a7ad5cefed2536d7f16d1` |
| `instantActions.schema` | instantActions | `699aba29451e2467bae79275ce99f24ad6a16b4d` | `08170d872dd73c50e2f1a050667a710080cae0f508d6e3da5790e180e9bd766a` |
| `order.schema` | order | `2c364b8d6a866f7cf407652c5faf83281f302be5` | `4cf263a1320e72f940ca5fdab4f4d17e8dc781a68519bf4b6d11c3cc554a3e83` |
| `state.schema` | state | `5b84a897529be0213f5eec1bf640b83cd8e35771` | `37c4b740c8808f961d4202749bc382308f862385b5487cf9408b81d064400850` |
| `visualization.schema` | visualization | `ee461497ed9253f295c9e318a666dd8e2251c028` | `fcf497ca73a2e286c9ea6beaf0bf1aaacb298f2223996815eabf2969246f9f39` |

2.0.0 태그는 factsheet만 확장자가 `.json`입니다(2.1.0부터 `factsheet.schema`).

## 라이선스와 고지 요구사항

- **2.0.0 태그 시점**: 저장소에 라이선스 파일이 없습니다. 같은 태그의 명세 문서(`VDA5050_EN_V1.md`)에 다음 저작권 고지가 있습니다.
  > **Copyright** Association of the Automotive Industry (VDA). Reproduction and any other form of reproduction is only permitted with specification of the source.

  출처를 밝히면 복제와 재배포가 허용됩니다. 이 문서가 그 출처 표기입니다.
- **이후 저장소 라이선스**: 2023-02-21 커밋 `578b26d1b7e1056eadb2f439c21f1693172724f3`에서 저장소 전체에 MIT License(저작권자 Verband der Automobilindustrie)가 붙었고, 현재 `main`의 `LICENSE.txt`도 MIT입니다. MIT는 복제본에 아래 저작권 고지와 허가 고지를 함께 넣으라고 요구합니다.
- 두 조건을 모두 지키도록 출처와 MIT 전문을 함께 둡니다. 아래 전문은 현재 `main`(2.1.0 태그와 같음)의 `LICENSE.txt`이며, 2023년 최초 판은 연도만 2023입니다.

```text
Copyright 2024 Verband der Automobilindustrie

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## 읽을 때 적용하는 보정

파일은 그대로 두고, `fleet/schema.py`의 `load_vda5050_schemas("2.0.0")`가 읽은 뒤 한 가지만 보정합니다(`UPSTREAM_REPAIRS`).

- `factsheet.json`은 루트와 `protocolLimits.maxArrayLens` 두 곳에서 속성 정의(`headerId`, `typeSpecification`, `order.nodes` 등)를 `properties` 안이 아니라 키워드 자리에 적었습니다. 표준 검증기는 이 키들을 모르는 키워드로 보고 무시하므로, 원본 그대로면 factsheet는 루트의 `type`과 `required`만 검사됩니다. 로더는 이 정의들을 `properties` 아래로 옮깁니다. 2.1.0 태그의 `factsheet.schema`가 같은 구조로 고쳐져 있습니다.
- 원본 파일을 그대로 컴파일하면 `SchemaError`가 나고, 테스트가 이를 확인합니다.

## 명세 본문과 다른 점(원본 그대로 둠)

검증 결과를 읽을 때 알아야 할 2.0.0 스키마의 특성입니다. 스키마 규칙이므로 보정하지 않았습니다.

- `instantActions`의 action 항목은 `actionType`이 아니라 `actionName`을 필수로 요구합니다. 명세 본문과 `order` 스키마, 2.1.0 스키마는 `actionType`입니다.
- `state.actionStates[].actionStatus` enum에 `PAUSED`가 없습니다. 명세 본문은 `PAUSED`를 허용합니다(2.1.0 스키마도 빠져 있음).
- `order.nodes[].nodePosition`의 편차 필드 이름이 `allowedDeviationXy`입니다. 명세 본문과 2.1.0은 `allowedDeviationXY`입니다.
- 어떤 스키마도 `additionalProperties`를 쓰지 않으므로 정의되지 않은 필드(`x_` 확장 포함)는 모든 위치에서 허용됩니다.
- `timestamp`의 `format: date-time`은 JSON Schema에서 주석이므로 검사되지 않습니다.

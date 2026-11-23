# 사실 계약·표 머리글 경로 실험

기준 커밋: `2b5a86a5b5ea4660d2c06020593a8efc2f991282`.
실험 브랜치: `codex/trace-table-contracts` (2026-11-03).
모델·추론 강도·실제 `.env`는 변경하지 않습니다.

## 원문에서 차용한 범위

- [TRACE, 2026-09-30](https://arxiv.org/html/2609.38861v1), §3.3–3.4:
  관측 단위를 먼저 구분하고 생성 결과의 식별자·근거·자료형을 코드가 검사하는 원칙을 차용합니다.
  학술문헌 검색, 논문 선택, 해당 벤치마크·성능 수치는 이 프로젝트에 이식하지 않습니다.
- [DeepTable, 2026-09-07](https://arxiv.org/html/2609.07707v1), §3.2–3.4:
  행·열 머리글의 상위 경로라는 **입력 표현**만 차용합니다.
  학습, LoRA, attention bias, 임베딩 변경은 구현하지 않습니다.

10월 29일 수정에서 이미 목표범위·기준연도·단위를 감축목표의 병합 키에 반영했습니다.
이번 변경은 그 수정의 효과를 새 연구 결과로 다시 주장하지 않습니다. 지표별 식별자,
기간·계획 문맥, 근거 셀 검증과 다른 수치 시트까지 공통 규칙을 확장하는 실험입니다.

## 두 독립 설정

| 설정 | 기본값 | 작동 |
|---|---|---|
| `FACT_CONTRACT_MODE` | `off` | `off`: 기본 추출·공통 필드 저장. `audit`: 계약 지침과 진단 추가. `enforce`: 진단 + 엄격 병합·잘못된 제안 보류 |
| `TABLE_HEADER_PATHS_ENABLED` | `0` | 기존 추출 입력에 표 셀 경로를 추가. 객체별 호출을 활성화하지 않음 |
| `TABLE_HEADER_PATHS_MAX_CHARS` | `6000` | 페이지 배치 또는 표 청크에 추가할 경로 문자의 상한. 토큰 상한이 아님 |

기본 OFF는 기존 재현 조건을 보존하기 위한 것입니다. `audit`도 **실호출 시 계약 지침이
추가**되므로 모델 응답은 달라질 수 있습니다. 저장 응답 재사용 실험에서는 새 프롬프트를
모델에 전달하지 않습니다. `audit`의 업무 셀 처리·기본 열은 OFF와 같습니다.

## 실제 데이터 흐름

1. `build_document_objects`: HTML rowspan/colspan을 확장하고 명시 머리글 여부·병합 셀 위치를 보존합니다.
2. `table_paths`: 원본 확장 표의 1-based 행·열, 행 경로, 열 경로, 값 원문, 명시 단위,
   객체/근거ID, 페이지를 구성합니다. 좌표 bbox는 **객체 범위**이며 셀 좌표로 포장하지 않습니다.
3. `ExtractorAgent._extract_sheet`: 기존 1회 요청에서 먼저 행의 대상을 구분하도록 요청합니다.
   별도 계획 모델 호출은 없습니다. `_fact_cells`에 수치별 원문 셀 참조를 받습니다.
   검증된 셀의 객체/근거ID는 기존 출처 계보에도 연결해 인벤토리와 근거 검사에서 소비합니다.
4. `annotate_row`: 모델이 만든 계약·사실ID는 신뢰하지 않고 코드가 다시 생성합니다.
   한 행의 목표배출량·목표감축량·감축률은 서로 다른 사실입니다. 옆 지표 유무로
   동일 수치의 사실ID가 변하지 않도록 했습니다.
5. `merge_extraction_rows`, `merge_rows_stably`, Organizer: 같은 식별 기준을 사용합니다.
   사업·부문/세부부문 + 지표 + 범위 + 기준/목표연도·기간·시간기준 + 단위 차원/배율 +
   계획구분·계획문맥·재원·시나리오를 구분합니다. CO2와 CO2eq, 톤과 천톤을 임의 환산·병합하지 않습니다.
6. 정제 **전** 검사로 잘못된 원행을 보존하고, 의미 라우팅 **후** 최종 저장 전에 재검사합니다.
   동일 사실·동일 값의 출처는 합치며, 값 충돌은 양쪽 행과 충돌 기록을 유지합니다.
7. Excel 저장 후 실제 셀을 다시 읽어 연결을 확인합니다. run-state OFF도 감사 파일을 저장합니다.

지원 수치 시트: 지역여건, 배출현황 2개, 배출전망, 감축목표, 연차별 이행계획,
정량감축량, 재정투자계획. 다른 정성 시트는 이번 계약으로 차단하지 않습니다.

2026-11-05 이후 감축목표 단위·세부부문과 지원 시트의 지표종류·계획문맥·시간기준·
기간시작/종료 등의 저장 및 감축률 별칭 연결은 off/audit/enforce 공통입니다.
`enforce`는 추가 검증·보류·병합만 담당합니다. [공통 필드 안내](shared_extraction_fields.md)를 참고하세요.

## 보류와 미평가의 차이

- 값 불일치, 다른 지표 셀, 잘못된 행 대상·연도·기간·단위, 다른 목표범위,
  잘못된 좌표/객체ID는 확정된 **연결 계약 위반**입니다. enforce는 원행을 보류합니다.
- 셀 참조·단위·범위가 없는 과거 응답, 알 수 없는 머리글, 해소되지 않은 문맥은 검토 대상으로
  유지합니다. 누락된 정보를 자동으로 채우거나 판독 성공으로 표시하지 않습니다.
- 다른 계획 문맥은 다른 사실입니다. 서로 다른 지표 집합의 넓은 행을 자동으로 합쳐
  완전한 행을 만드는 기능은 포함하지 않습니다.
- 출처 셀과 일치해도 원본 객체의 OCR/표 파싱 자체가 잘못됐을 수 있습니다.
  `cell_verified`는 **해당 입력 객체와의 계약 검사**이며 골든 정확도 판정이 아닙니다.

## 표 구조의 지원 한계

명시 HTML의 다단 `<thead>`·rowspan/colspan과 이미 펼쳐진 머리글을 지원합니다.
빈 머리글을 왼쪽 값으로 자동 채우지 않으며, 구조가 불명확하면 진단을 남깁니다.
네이티브 PDF 파서가 이미 한 줄로 축약한 머리글의 원래 계층을 새로 복원하지는 않습니다.
이미지만 있는 표도 새 OCR 없이 경로를 생성하지 않습니다.

경로 입력 상한을 넘으면 일부 **보조 경로**만 생략하고 원래 표를 유지합니다.
일반 페이지 배치에서는 시트/배치 호출 횟수를 늘리지 않습니다. 객체 추출에서 별도 문자·셀
상한을 설정한 경우에는 경로 추가로 청크 수가 증가하거나 한 행이 상한을 초과할 수 있습니다.
입력 문자량·시간·실제 호출 수를 함께 비교해야 합니다. 속도/정확도 향상은 아직 실측하지 않았습니다.

## 실행

현재 브랜치의 프로젝트 폴더에서 기존 `.env`를 사용합니다. 모델을 바꾸는 옵션은 없습니다.
출력은 새 이름을 사용하세요.
예시의 발췌 PDF는 실행 폴더에 두거나 실제 절대 경로로 바꾸세요. 개발 worktree에
원본 프로젝트의 모든 테스트 PDF가 복사되어 있는 것은 아닙니다.

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py main.py ".\테스트파일_2.pdf" --retries 1 --evaluation-mode off `
  --fact-contract audit --table-header-paths on `
  --output ".\output\사실계약_audit_$stamp.xlsx"
```

적용 실험은 `--fact-contract enforce`, 대조군은 `--fact-contract off --table-header-paths off`입니다.
실제 `.env`에서 정한 provider/모델/추론 설정을 그대로 사용하므로 실호출 비용·시간은 기존과 같이 발생합니다.
첫 실측은 같은 소표본에서 OFF / 계약만 / 경로만 / 둘 다의 네 조건으로 비교하는 것을 권장합니다.
응답 변동이 있으므로 1회 결과만으로 원인을 단정하지 않습니다.

저장 응답으로 연결만 비교할 때는 다음 명령을 사용합니다. 추가 모델 호출은 없습니다.

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss_ffffff"
py scripts/replay_fact_contracts.py ".\output\실행명_raw_business.json.gz" `
  --objects-audit ".\output\실행명_pipeline_audit.json" `
  --output-dir ".\output\사실계약_응답고정_$stamp"
```

입력은 실제 `raw_data` JSON이어야 합니다. 감사 요약이나 Excel을 원시 응답으로 간주하지 않습니다.
원문 객체가 입력에 있으면 `--objects-audit`는 생략 가능합니다. 구형 raw에 원문 SHA가 없으면
별도로 제공한 감사 파일과의 문서 대응은 `user_paired_document_binding_unverified`로 표시합니다.
이 실행기는 해당 원문/골든의 정확도를 새로 채점하거나 머리글 프롬프트 효과를 검증하지 않습니다.
기존 Excel을 고정한 증분 복구 실행기와도 달리, **제공된 행만 새 Excel로 재작성**합니다.

## 산출물

- `실행명_pipeline_audit.json`: `fact_contract_review`, `fact_contract_summary`,
  `table_path_diagnostics`, 행별 계약/셀 참조/충돌 기록.
  `pre_clean`·`post_clean`·`pre_save` 단계는 구분하며, 원장 기록 수는 고유 업무 행 수가 아닙니다.
- `실행명_fact_contracts.json`: 계약 원행과 실제 Excel 행·셀 연결 검사. 모델 정확도는 false.
- `실행명_run_outcome.json`: 계약 감사 파일 경로와 저장 확인/미확인 행 수 (일반 main 실행).
- 재사용 실행기: `off.xlsx`, `audit.xlsx`, `enforce.xlsx`, 각 감사 파일,
  `header_path_preview.txt`, `header_path_diagnostics.json`, `comparison.json`.

감사 저장 실패는 경고와 사유로 알리고 이미 저장한 Excel을 삭제하지 않습니다.
독립 원문 감사는 프로젝트 설정을 import하지 않습니다. `scripts/audit_source_coverage.py`의
`--table-header-paths on|off`로 연도 상위 경로 처리를 선택하거나 생산 실행의 기록을 승계합니다.

## 유료 호출 없는 검증

```powershell
py -m pytest tests/test_fact_contracts.py tests/test_fact_contract_pipeline.py -q
```

외부 모델 경계만 대체하고 실제 PDF 파싱·행 전달·정제·병합·체크포인트·Excel 작성·재조회·
감사 저장을 확인합니다. 재사용 검증과 모델 정확도·속도의 실측은 구분합니다.

"""v9-1 — 05 전망범위 축 필드 신설과 03/04 축 enum 강제."""

from __future__ import annotations

import config
from agents import extractor_agent
from agents.organizer_agent import OrganizerAgent, _normalize_forecast_scope
from scripts.golden_score_contract import 행, 키, 필수채점필드
from utils.pdf_reader import PDFContent, PageContent
from utils.semantic_routing import validate_and_reclassify


def test_contract_carries_forecast_scope() -> None:
    assert "전망범위" in config.EXCEL_HEADERS["05_배출전망"]
    assert '"전망범위"' in extractor_agent._SHEET_CONFIGS["emissions_forecast"]["prompt"]
    # 전망범위는 행 단위 폴백 키 성분 — 골든 필수 컬럼은 아니다(구 골든 v1 호환).
    assert "전망범위" not in 필수채점필드("05_배출전망")


def test_scope_normalization_folds_notation_but_never_guesses() -> None:
    assert _normalize_forecast_scope("관리 권한 내 배출량") == "관리권한"
    assert _normalize_forecast_scope("관리권한") == "관리권한"
    assert _normalize_forecast_scope("지역 전체") == "지역전체"
    assert _normalize_forecast_scope("") == ""
    assert _normalize_forecast_scope("서울시 산정") == "서울시 산정"  # 매핑 불가 → 원값


def _forecast_row(**overrides) -> dict:
    row = {"전망범위": "지역전체", "시나리오": "BAU", "부문": "건물", "연도": 2030,
           "전망값": 100, "단위": "천톤CO2eq", "출처페이지": 10}
    row.update(overrides)
    return row


def test_05_scope_separates_keys_and_blank_scope_absorbs() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_forecast": [
            _forecast_row(),
            _forecast_row(전망범위="관리 권한 내", 전망값=80),  # 같은 [시나리오,부문,연도], 다른 축
            _forecast_row(전망범위="", 전망값=100),  # 빈 축 → 동일 값의 지역전체 행에 흡수
        ],
    })
    rows = cleaned["emissions_forecast"]
    assert sorted((r["전망범위"], r["전망값"]) for r in rows) == [("관리권한", 80.0), ("지역전체", 100.0)]


def test_axis_enum_enforcement_blanks_invalid_labels_then_rules_refill() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_management": [
            {"관리부문": "건물", "세부부문": "가정", "직간접구분": "에너지",  # enum 밖 → 비움
             "연도": 2030, "배출량": 1, "단위": "천톤CO2eq", "출처페이지": 10},
            {"관리부문": "건물", "세부부문": "가정(전력)", "직간접구분": "부문합계",  # 비움 → 토큰 규칙이 간접으로
             "연도": 2030, "배출량": 2, "단위": "천톤CO2eq", "출처페이지": 10},
        ],
        "emissions_regional": [
            {"부문": "폐기물", "세부부문": "소각", "배출유형": "에너지",  # enum 밖 → 비움, 재충전 불가 → 빈칸
             "연도": 2030, "배출량": 3, "단위": "천톤CO2eq", "출처페이지": 11},
        ],
    })
    mgmt = {r["세부부문"]: r for r in cleaned["emissions_management"]}
    assert mgmt["가정"].get("직간접구분", "") == ""
    assert mgmt["가정(전력)"]["직간접구분"] == "간접"
    assert cleaned["emissions_regional"][0].get("배출유형", "") == ""
    issues = [i for i in cleaned["validation_report"] if i["항목"] == "축 필드 enum 밖 값 제거"]
    assert {i["대상시트키"] for i in issues} == {"emissions_management", "emissions_regional"}


def test_reclassified_forecast_rows_carry_scope_from_source_sheet() -> None:
    table = """
    <table>
      <tr><th>부문</th><th>2021</th><th>2030</th></tr>
      <tr><td>건물</td><td>120</td><td>100</td></tr>
    </table>
    """
    text = "제4장 온실가스 배출 전망\n표 4-2 부문별 온실가스 배출량 전망(BAU)\n"
    page = PageContent(page_number=10, text=text, tables=[table], images=[])
    document = PDFContent(total_pages=10, pages=[page], full_text=text)
    final_data = {
        "document_meta": [{"지자체명": "테스트시", "계획명": "기본계획", "계획시작연도": 2024, "계획종료연도": 2033}],
        "emissions_regional": [{"지자체명": "테스트시", "배출유형": "", "부문": "건물", "연도": 2030,
                                "배출량": 100, "단위": "천톤CO2eq", "출처페이지": 10}],
        "emissions_management": [{"지자체명": "테스트시", "관리부문": "건물", "직간접구분": "", "연도": 2031,
                                  "배출량": 90, "단위": "천톤CO2eq", "출처페이지": 10}],
        "emissions_forecast": [],
    }
    validate_and_reclassify(final_data, document, auto_reclassify=True)
    scopes = {row["연도"]: row["전망범위"] for row in final_data["emissions_forecast"]}
    assert scopes == {2030: "지역전체", 2031: "관리권한"}


def _score_row(values: dict) -> 행:
    return 행(2, values, "텍스트표", "10")


def test_scorer_uses_extended_key_only_when_scope_is_filled() -> None:
    filled = _score_row({"전망범위": "관리권한", "시나리오": "BAU", "부문": "건물", "연도": 2030})
    regional = _score_row({"전망범위": "지역전체", "시나리오": "BAU", "부문": "건물", "연도": 2030})
    blank = _score_row({"전망범위": "", "시나리오": "BAU", "부문": "건물", "연도": 2030})
    key_managed = 키(filled, "05_배출전망", relaxed=False)
    key_regional = 키(regional, "05_배출전망", relaxed=False)
    key_blank = 키(blank, "05_배출전망", relaxed=False)
    assert key_managed != key_regional  # 축이 키를 분리
    assert key_blank is not None and "전망범위" not in key_blank  # 폴백 키
    assert 키(filled, "05_배출전망", relaxed=True) == 키(regional, "05_배출전망", relaxed=True)  # 완화키 불변

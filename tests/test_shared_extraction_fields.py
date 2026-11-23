"""External models mocked; conversion, cleaning, audit and Excel are real."""
from copy import deepcopy
from pathlib import Path
import json
import re

import fitz
import openpyxl
import pytest

import config
from agents.extractor_agent import ExtractorAgent, _SHEET_CONFIGS
from agents.image_agent import ImageAgent, CHART_TABLE_SYSTEM
from agents.organizer_agent import OrganizerAgent
from agents.supervisor import Supervisor
from utils.excel_writer import write_excel
from utils.extraction_schema import canonical_fields, DISPLAY_ALIASES, METRIC_UNITS, VISION_TARGETS
from utils.quality_adapters import compare_storage, read_saved_workbook
from utils.pdf_reader import PageContent


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external model call")
    for method in ("call_text", "call_vision", "call_vision_batch"):
        monkeypatch.setattr("utils.llm_client." + method, forbidden)
    monkeypatch.setattr(config, "FACT_CONTRACT_MODE", "off")
    monkeypatch.setattr(config, "SEMANTIC_CONTRACT_GUARDS_ENABLED", False)


def rows(path, sheet):
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    values = list(book[sheet].values)
    book.close()
    return [dict(zip(values[0], row)) for row in values[1:]]


def chart(sheet, items, title="원문 표", **extra):
    return {"type": "chart_table", "target_sheet": sheet, "chart_type": "표", "title": title,
            "page_number": 307, "object_id": "source-table", "evidence_id": "source-evidence",
            "confidence": "high", "table": items, **extra}


def save_chart(tmp_path, analysis):
    raw = ImageAgent()._merge_image_results({}, [analysis], "서울특별시")
    cleaned = OrganizerAgent().organize(raw)
    path = write_excel(cleaned, tmp_path / "result.xlsx")
    return raw, cleaned, path


def test_all_text_schemas_and_excel_share_canonical_fields():
    assert set(_SHEET_CONFIGS) == set(config.EXTRACTION_SHEETS)
    for sheet, spec in _SHEET_CONFIGS.items():
        for field in canonical_fields(sheet):
            assert '"' + field + '":' in spec["prompt"]
        aliases = DISPLAY_ALIASES.get(sheet, {})
        assert set(canonical_fields(sheet)) <= {aliases.get(h, h) for h in config.EXCEL_HEADERS[config.SHEET_KEY_TO_NAME[sheet]]}
    prompt = _SHEET_CONFIGS["financial_plan"]["prompt"]
    assert "구비" in prompt and "기간시작" in prompt and "미표기" in prompt
    assert "- 사업유형: 정량/정성" not in _SHEET_CONFIGS["mitigation_projects"]["prompt"]


def test_single_and_batch_vision_use_same_current_fields(monkeypatch):
    prompts = []
    def single(image, prompt, **kwargs):
        prompts.append(prompt)
        return '{"type":"해당없음"}'
    def batch(images, prompt, **kwargs):
        prompts.append(prompt)
        return '{"analyses":[{"image_index":1,"type":"해당없음"},{"image_index":2,"type":"해당없음"}]}'
    monkeypatch.setattr("utils.llm_client.call_vision", single)
    monkeypatch.setattr("utils.llm_client.call_vision_batch", batch)
    agent = ImageAgent()
    agent._chart_to_table({"base64": "fixture"}, 1, "서울")
    agent._chart_to_table_batch([(PageContent(n, "문맥", [], []), {"base64": "fixture"}) for n in (1, 2)], "서울")
    assert "vehicle, energy" not in CHART_TABLE_SYSTEM
    for prompt in prompts:
        assert all(key in prompt for key in VISION_TARGETS)
        assert all(field in prompt for field in ("재원구분", "사업명", "목표감축량", "목표배출량", "기간종료"))


@pytest.mark.parametrize("mode", ["off", "audit", "enforce"])
@pytest.mark.parametrize("rate,expected", [(0, 0), (39.8, 39.8), ("39.8%", 39.8)])
def test_rate_alias_and_unit_reach_saved_cells_in_all_modes(tmp_path, monkeypatch, mode, rate, expected):
    monkeypatch.setattr(config, "FACT_CONTRACT_MODE", mode)
    original = {"reduction_targets": [{"부문": "건물", "목표범위": "지역전체", "목표연도": 2030,
                "목표배출량": 6005, "감축률(%)": rate, "단위": "천톤CO2eq", "출처페이지": 187}]}
    before = deepcopy(original)
    clean = OrganizerAgent().organize(original)
    saved = write_excel(clean, tmp_path / "rate.xlsx")
    row = rows(saved, "06_감축목표")[0]
    assert row["감축률(%)"] == expected and row["단위"] == "천톤CO2eq"
    assert clean["reduction_targets"][0]["감축률"] == expected
    actual, _ = read_saved_workbook(saved)
    diagnostics, _ = compare_storage(actual, clean)
    assert not diagnostics
    assert before == original


@pytest.mark.parametrize("mode", ["off", "audit", "enforce"])
def test_conflicting_alias_keeps_originals_and_holds_only_conflicting_cell(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(config, "FACT_CONTRACT_MODE", mode)
    source = {"municipality_name": "서울", "reduction_targets": [{"부문": "건물", "목표연도": 2030,
              "목표배출량": 6005, "감축률": 40, "감축률(%)": 41, "출처페이지": 187}]}
    cleaned = OrganizerAgent().organize(source)
    path = write_excel(cleaned, tmp_path / "conflict.xlsx")
    row = rows(path, "06_감축목표")[0]
    assert row["목표배출량"] == 6005 and row["감축률(%)"] is None
    assert cleaned["field_contract_review"][0]["row"]["감축률(%)"] == 41
    assert cleaned["field_contract_review"][0]["row"]["감축률"] == 40
    assert any(r["항목"] == "공통 필드 별칭 충돌" for r in cleaned["validation_report"])
    from utils.fact_contracts import persist_contract_report
    report = persist_contract_report(path, cleaned)
    assert report["missing_rows"] == 0 and report["held_field_rows"] == 1


def test_archived_workbook_missing_new_axes_is_unassessed_not_structurally_wrong(tmp_path):
    path = write_excel({}, tmp_path / "old.xlsx")
    book = openpyxl.load_workbook(path)
    sheet = book["06_감축목표"]
    from utils.extraction_schema import EXTRA_FIELDS
    for index in range(sheet.max_column, 0, -1):
        if sheet.cell(1, index).value in EXTRA_FIELDS["reduction_targets"]:
            sheet.delete_cols(index)
    book.save(path)
    book.close()
    _, diagnostics = read_saved_workbook(path)
    assert diagnostics and all(d["certainty"] == "unassessed" for d in diagnostics)


@pytest.mark.parametrize("fund", ["시비", "구비", None])
def test_finance_fields_are_not_replaced_by_title_or_total(tmp_path, fund):
    analysis = chart("financial_plan", [{"연도": 2025, "항목": "C1-2", "값": 2880,
        "단위": "백만원", "fields": {"사업명": "공공선별시설 확충", "부문": "폐기물", "재원구분": fund,
        "계획구분": "온실가스감축대책", "행경로": ["폐기물", "C1-2", str(fund)]}}], title="재정투자 계획 총괄")
    raw, _, path = save_chart(tmp_path, analysis)
    row = rows(path, "11_재정투자계획")[0]
    assert (row["사업명"], row["부문"], row["재원구분"], row["관리번호"]) == ("공공선별시설 확충", "폐기물", fund, "C1-2")
    assert row["예산액"] == 2880 and row["객체ID"] == "source-table"
    assert json.loads(row["행경로"])[0] == "폐기물"
    repeated = ImageAgent()._merge_image_results(raw, [analysis], "서울특별시")
    assert len(repeated["financial_plan"]) == 1 and len(repeated["vision_analyses"]) == 1
    assert len(repeated["chart_observations"]) == 1
    conflicting = deepcopy(analysis)
    conflicting["table"][0]["값"] = 2881
    out = ImageAgent()._merge_image_results(repeated, [conflicting], "서울특별시")
    assert {r["예산액"] for r in out["financial_plan"]} == {2880, 2881}
    assert out["merge_conflicts"]


def test_reduction_fields_and_baseline_context_survive(tmp_path):
    analysis = chart("reduction_targets", [{"연도": 2030, "항목": "건물", "종류": "목표", "값": 6005,
        "단위": "천톤CO2eq", "fields": {"기준연도": 2005, "기준배출량": 10000, "목표배출량": 6005,
        "목표감축량": 3995, "감축률": 39.95, "목표범위": "관리권한+추가감축", "값출처": "원문"}}])
    _, _, path = save_chart(tmp_path, analysis)
    row = rows(path, "06_감축목표")[0]
    assert (row["기준연도"], row["목표배출량"], row["목표감축량"], row["감축률(%)"]) == (2005, 6005, 3995, 39.95)
    assert row["목표범위"] == "관리권한+추가감축"


def test_annual_reduction_is_not_target_emissions_and_ambiguous_goal_is_held(tmp_path):
    analysis = chart("reduction_targets", [{"연도": 2024, "항목": "건물", "종류": "목표", "값": 683,
        "단위": "천톤CO2eq"}], title="부문별 연간 감축량")
    _, _, path = save_chart(tmp_path, analysis)
    row = rows(path, "06_감축목표")[0]
    assert row["목표감축량"] == 683 and row["목표배출량"] is None
    ambiguous = deepcopy(analysis)
    ambiguous["title"] = "감축목표"
    result = ImageAgent()._merge_image_results({}, [ambiguous], "서울")
    assert not result["reduction_targets"]
    assert "수치 지표 종류 미확정" in result["chart_observations"][0]["보류사유"]
    assert result["vision_analyses"][0]["table"][0]["값"] == 683


def test_explicit_period_and_baseline_are_preserved_without_invented_goal_year(tmp_path):
    analysis = chart("financial_plan", [{"연도": None, "값": 36000, "단위": "백만원", "fields": {
        "사업명": "기간예산 사업", "기간시작": 2024, "기간종료": 2033, "시간기준": "기간합계"}}])
    _, _, path = save_chart(tmp_path, analysis)
    row = rows(path, "11_재정투자계획")[0]
    assert (row["연도"], row["기간시작"], row["기간종료"], row["예산액"]) == (None, 2024, 2033, 36000)
    baseline = chart("reduction_targets", [{"연도": 2005, "항목": "합계", "종류": "현황", "값": 52342,
        "단위": "천톤CO2eq", "fields": {"기준연도": 2005, "기준배출량": 52342}}])
    result = ImageAgent()._merge_image_results({}, [baseline], "서울")
    assert result["reduction_targets"][0]["기준연도"] == 2005
    assert result["reduction_targets"][0].get("목표연도") is None


@pytest.mark.parametrize("fields", [{}, {"기간시작": 2033, "기간종료": 2024}])
def test_missing_or_invalid_time_remains_review(fields):
    analysis = chart("financial_plan", [{"값": 36000, "단위": "백만원", "fields": {"사업명": "검토사업", **fields}}])
    result = ImageAgent()._merge_image_results({}, [analysis], "서울")
    assert not result["financial_plan"]
    assert result["chart_observations"][0]["반영여부"] == "검토"


def test_mixed_table_routes_activity_reduction_and_budget_separately(tmp_path):
    common = {"사업명": "BRP", "부문": "건물", "연도": 2025}
    analysis = chart("mitigation_projects", [
        {"target_sheet": "annual_implementation", "fields": {**common, "목표물량": 1500, "목표단위": "백만원"}},
        {"target_sheet": "quantitative_reductions", "fields": {**common, "활동량": 1500, "활동단위": "백만원",
            "예상감축량": 2437, "단위": "tCO2eq", "감축원단위값": 1.625, "감축원단위단위": "tCO2eq/백만원", "값출처": "원문"}},
        {"target_sheet": "financial_plan", "fields": {**common, "예산액": 30000, "예산단위": "백만원", "재원구분": "시비"}},
    ])
    _, _, path = save_chart(tmp_path, analysis)
    assert rows(path, "09_연차별이행계획")[0]["목표물량"] == 1500
    quantitative = rows(path, "10_정량감축량")[0]
    assert quantitative["활동량"] == 1500 and quantitative["예상감축량"] == 2437
    assert quantitative["활동단위"] == "백만원" and quantitative["단위"] == "tCO2eq"
    assert rows(path, "11_재정투자계획")[0]["예산액"] == 30000
    assert not rows(path, "03_배출현황_지역")


def test_unsupported_target_or_conflicting_nested_fields_cannot_fall_back_to_emissions():
    analysis = chart("governance_feedback", [{"연도": 2030, "항목": "건물", "값": 100}])
    result = ImageAgent()._merge_image_results({}, [analysis], "서울")
    assert not result["emissions_regional"] and result["chart_observations"][0]["반영여부"] == "검토"
    conflict = chart("financial_plan", [{"연도": 2025, "값": 100, "fields": {"사업명": "사업", "연도": 2026}}])
    result = ImageAgent()._merge_image_results({}, [conflict], "서울")
    assert not result["financial_plan"]
    assert any("충돌" in reason for reason in result["chart_observations"][0]["보류사유"])


@pytest.mark.parametrize("override", [{"target_sheet": ["financial_plan"]}, {"항목": ["건물"]}])
def test_invalid_identifiers_are_reviewable_without_crashing(override):
    item = {"연도": 2030, "항목": "건물", "값": 100, "종류": "현황", **override}
    result = ImageAgent()._merge_image_results({}, [chart("emissions_regional", [item])], "서울")
    assert not result["emissions_regional"]
    assert result["chart_observations"][0]["반영여부"] == "검토"


def test_regional_subcategory_and_period_do_not_disappear_in_dedup(tmp_path):
    base = {"지표명": "평년기온", "지표범주": "자연환경", "값": 12.8, "단위": "℃", "연도": None}
    cleaned = OrganizerAgent().organize({"regional_conditions": [
        {**base, "기간시작": 1961, "기간종료": 1990}, {**base, "기간시작": 1991, "기간종료": 2020},
        {**base, "지표세부범주": "도심", "기간시작": 1991, "기간종료": 2020}]})
    path = write_excel(cleaned, tmp_path / "periods.xlsx")
    assert len(rows(path, "02_지역여건")) == 3


def test_quality_required_fields_follow_baseline_activity_and_sector_subtotal_roles():
    from utils.extraction_schema import required_for_row, alternatives_for_row
    baseline = {"기준연도": 2005, "기준배출량": 52342}
    assert "목표연도" not in required_for_row("reduction_targets", baseline)
    assert alternatives_for_row("reduction_targets", baseline) == ["기준배출량"]
    assert "활동단위" in required_for_row("quantitative_reductions", {"활동량": 1500})
    assert "예상감축량" not in required_for_row("quantitative_reductions", {"활동량": 1500})
    assert "사업명" not in required_for_row("financial_plan", {"부문": "폐기물"})
    assert "사업명" in required_for_row("financial_plan", {"부문": "C1-2"})


def test_supervisor_real_pdf_to_excel_and_audit_with_fixed_external_responses(tmp_path, monkeypatch):
    settings = {"FACT_CONTRACT_MODE": "off", "FULL_DOCUMENT_SCAN": True, "RUN_STATE_ENABLED": False,
        "SELECTIVE_OCR_ENABLED": False, "EXTRACTION_OBJECT_BATCHING_ENABLED": False,
        "SEMANTIC_ROUTING_ENABLED": False, "SOURCE_COVERAGE_MODE": "off", "EVALUATION_MODE": "off",
        "IMAGE_TRIAGE_ENABLED": False, "IMAGE_CHART_TABLE_EXTRACTION": True, "IMAGE_ANALYSIS_BATCH_SIZE": 1,
        "MAX_IMAGES": None, "TABLE_HEADER_PATHS_ENABLED": False}
    for key, value in settings.items():
        monkeypatch.setattr(config, key, value)
    source = tmp_path / "source.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 50), "Figure 1 탄소중립 재정투자 계획 (단위: 백만원)", fontname="korea")
        for y in (100, 140, 180):
            page.draw_line((40, y), (440, y))
        for x in (40, 180, 300, 440):
            page.draw_line((x, 100), (x, 180))
        for x, value in ((50, "사업"), (190, "2025"), (310, "2880")):
            page.insert_text((x, 130), value, fontname="korea")
        doc.save(source)
    def text(prompt, **kwargs):
        if "municipality_name" in prompt:
            return '{"municipality_name":"서울특별시"}'
        matches = re.findall(r'\{\s*"([a-z_]+)"\s*:\s*\[', prompt)
        key = matches[-1] if matches else None
        if key not in _SHEET_CONFIGS:
            return '{"data_findings":[],"verification_notes":[],"next_checks":[]}'
        data = []
        if key == "document_meta":
            data = [{"지자체명": "서울특별시", "계획명": "기본계획", "계획시작연도": 2024, "계획종료연도": 2033}]
        if key == "reduction_targets":
            data = [{"부문": "건물", "목표범위": "지역전체", "기준연도": 2005,
                     "목표연도": 2030, "목표배출량": 6005, "감축률": 40, "단위": "천톤CO2eq", "출처페이지": 1}]
        return json.dumps({key: data}, ensure_ascii=False)
    payload = chart("financial_plan", [
        {"연도": 2025, "항목": "C1-2", "값": 2880, "단위": "백만원",
         "fields": {"사업명": "공공선별시설 확충", "부문": "폐기물", "재원구분": "시비"}},
        {"target_sheet": "reduction_targets", "연도": 2024, "항목": "건물", "값": 683, "단위": "천톤CO2eq",
         "fields": {"목표감축량": 683}},
        {"target_sheet": "reduction_targets", "연도": 2025, "항목": "수송", "값": 100, "종류": "목표"},
    ], title="감축목표", page_number=1)
    monkeypatch.setattr("utils.llm_client.call_text", text)
    monkeypatch.setattr("utils.llm_client.call_vision", lambda *a, **k: json.dumps(payload, ensure_ascii=False))
    supervisor = Supervisor()
    path = supervisor.run(source, tmp_path / "result.xlsx", include_images=True, max_pipeline_retries=1)
    finance = rows(path, "11_재정투자계획")[0]
    assert finance["재원구분"] == "시비" and finance["사업명"] == "공공선별시설 확충"
    targets = rows(path, "06_감축목표")
    assert any(r["감축률(%)"] == 40 and r["단위"] == "천톤CO2eq" for r in targets)
    assert any(r["목표감축량"] == 683 and r["목표배출량"] is None for r in targets)
    assert not any(r["목표배출량"] == 100 for r in targets)
    audit = json.loads(path.with_name(path.stem + "_pipeline_audit.json").read_text(encoding="utf-8"))
    assert audit["ledgers"]["vision_analyses"][0]["table"][0]["fields"]["재원구분"] == "시비"
    observations = audit["ledgers"]["chart_observations"]
    assert any(r["반영여부"] == "검토" and r["보류사유"] for r in observations)
    # Run-state is OFF, but literal responses, hold decisions and saved-cell
    # links still survive in a separate durable trace.
    trace = supervisor.last_outcome["vision_trace"]
    assert trace["call_counts"] == {"complete": 1}
    assert trace["decision_counts"] == {"eligible": 2, "held": 1}
    assert trace["excel_check"]["counts"] == {"excel_linked_values_equal": 2, "held_before_merge": 1}
    assert audit["ledgers"]["vision_trace"]["index_path"] == trace["index_path"]
    assert json.loads(Path(trace["calls"][0]["response_path"]).read_text(encoding="utf-8")) == payload
    assert not supervisor.last_outcome["model_accuracy_evaluated"]

"""No real model calls: exercise literal responses through real saved cells."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import openpyxl
import pytest

import config
from agents.image_agent import ImageAgent
from agents.organizer_agent import OrganizerAgent
from utils import llm_client
from utils.evidence_merge import merge_extraction_rows
from utils.excel_writer import write_excel
from utils.pdf_reader import PageContent
from utils.vision_trace import VisionTrace


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external model call")
    for method in ("call_text", "call_vision", "call_vision_batch"):
        monkeypatch.setattr(llm_client, method, forbidden)
    for key, value in {"FACT_CONTRACT_MODE": "off", "SEMANTIC_CONTRACT_GUARDS_ENABLED": False,
                       "IMAGE_TRIAGE_ENABLED": False, "MAX_IMAGES": None,
                       "IMAGE_CHART_TABLE_EXTRACTION": True, "IMAGE_ANALYSIS_BATCH_SIZE": 1}.items():
        monkeypatch.setattr(config, key, value)


def chart(sheet, items, **kwargs):
    return {"type": "chart_table", "target_sheet": sheet, "title": "원문 표", "confidence": "high",
            "page_number": 1, "table": items, **kwargs}


def sector_item(**kwargs):
    return {"연도": 2024, "항목": "건물", "종류": "목표", "값": 683, "단위": "천 톤CO₂eq.",
            "fields": {"부문": "건물", "예상감축량": 683, "시간기준": "연간"}, **kwargs}


def read_rows(path, name):
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    values = list(book[name].values)
    book.close()
    return [dict(zip(values[0], row)) for row in values[1:]]


@pytest.mark.parametrize("response,status", [
    ('  ```json\n{"type":"해당없음","설명":"사진"}\n```\r\n', "no_data"),
    ('{"type":"chart_table","table":', "invalid_json"),
    ("", "empty_response"), ("{}", "invalid_shape"),
    ('{"type":"chart_table","table":[]}', "invalid_shape"),
])
def test_literal_response_saved_before_parse_even_no_data_or_failure(tmp_path, monkeypatch, response, status):
    trace = VisionTrace(tmp_path / "result.xlsx", input_sha256="fixture-document")
    original_parse = llm_client.parse_json
    def parse(value):
        record = next(iter(trace.calls.values()))
        assert record["raw_saved"]
        assert Path(record["response_path"]).read_bytes() == response.encode("utf-8")
        return original_parse(value)
    monkeypatch.setattr(llm_client, "call_vision", lambda *a, **k: response)
    monkeypatch.setattr(llm_client, "parse_json", parse)
    agent = ImageAgent(trace=trace)
    assert agent._chart_to_table({"base64": "not-saved-image", "bbox": [1, 2, 3, 4]}, 7, "서울") is None
    record = next(iter(trace.calls.values()))
    assert record["status"] == status
    assert record["response_sha256"] == hashlib.sha256(response.encode()).hexdigest()
    assert "base64" not in record["request"]["images"][0]
    assert record["request"]["images"][0]["bbox"] == [1, 2, 3, 4]
    assert bool(agent._batch_input_reviews) == (status != "no_data")


def test_partial_batch_keeps_good_analysis_and_missing_image_reason(tmp_path, monkeypatch):
    payload = {"analyses": [chart("financial_plan", [{"연도": 2025, "값": 10,
        "fields": {"사업명": "실제 사업", "재원구분": "시비"}}], image_index=1)]}
    literal = json.dumps(payload, ensure_ascii=False)
    monkeypatch.setattr(llm_client, "call_vision_batch", lambda *a, **k: literal)
    trace = VisionTrace(tmp_path / "batch.xlsx")
    agent = ImageAgent(trace=trace)
    results = agent._chart_to_table_batch([(PageContent(n, "페이지 문맥", [], []),
        {"base64": f"image{n}", "object_id": f"obj{n}", "evidence_id": f"ev{n}"}) for n in (1, 2)], "서울")
    assert len(results) == 1 and results[0]["object_id"] == "obj1"
    assert next(iter(trace.calls.values()))["status"] == "partial"
    assert any(d["code"] == "missing_image_result" for d in agent._batch_input_reviews)


def test_earlier_response_and_hold_reasons_survive_later_interrupt(tmp_path, monkeypatch):
    payload = chart("reduction_targets", [{"연도": 2030, "항목": "건물", "값": 100, "종류": "목표"}])
    def call(*args, **kwargs):
        if call.count:
            raise KeyboardInterrupt()
        call.count += 1
        return json.dumps(payload, ensure_ascii=False)
    call.count = 0
    monkeypatch.setattr(llm_client, "call_vision", call)
    trace = VisionTrace(tmp_path / "interrupted.xlsx")
    pages = [PageContent(n, "", [], [{"base64": str(n), "width": 300, "height": 300}]) for n in (1, 2)]
    with pytest.raises(KeyboardInterrupt):
        ImageAgent(trace=trace).extract(pages, {}, "서울")
    assert [c["status"] for c in trace.calls.values()] == ["complete", "interrupted"]
    journal = [json.loads(line) for line in (trace.directory / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(journal) == 1 and journal[0]["merge_status"] == "held"
    assert "수치 지표 종류 미확정" in journal[0]["hold_reasons"]


@pytest.mark.parametrize("mode", ["off", "audit", "enforce"])
def test_sector_reduction_and_budget_children_reach_distinct_cells(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(config, "FACT_CONTRACT_MODE", mode)
    sector = chart("quantitative_reductions", [sector_item()], title="부문별 연도별 온실가스 감축량")
    finance_items = [{"연도": 2024, "항목": label, "값": amount, "단위": "백만원", "fields": {
        "사업명": "친환경 버스 보급", "부문": "수송", "재원구분": "합계", "예산액": amount,
        "예산단위": "백만원", "계획구분": "온실가스감축대책"}}
        for label, amount in (("전기버스 보급", 78968), ("수소버스 보급", 11368))]
    trace = VisionTrace(tmp_path / "business.xlsx")
    agent = ImageAgent(trace=trace)
    original = deepcopy(sector)
    raw = agent._merge_image_results({"municipality_name": "서울특별시"}, [sector, chart("financial_plan", finance_items)], "서울특별시")
    # Reusing the exact response does not add rows or duplicate trace decisions.
    agent._merge_image_results(raw, [sector, chart("financial_plan", finance_items)], "서울특별시")
    cleaned = OrganizerAgent().organize(raw)
    path = write_excel(cleaned, tmp_path / "business.xlsx")
    summary = trace.finalize(path)
    targets = read_rows(path, "06_감축목표")
    assert len(targets) == 1 and targets[0]["목표감축량"] == 683
    assert targets[0]["목표배출량"] is None and targets[0]["목표연도"] == 2024
    assert targets[0]["목표범위"] is None and not read_rows(path, "10_정량감축량")
    finance = read_rows(path, "11_재정투자계획")
    assert len(finance) == 2 and {r["예산액"] for r in finance} == {78968, 11368}
    assert {json.loads(r["행경로"])[0] for r in finance} == {"전기버스 보급", "수소버스 보급"}
    assert all(r["사업명"] == "친환경 버스 보급" for r in finance)
    assert summary["excel_check"]["counts"] == {"excel_linked_values_equal": 3}
    assert summary["decision_counts"] == {"eligible": 3} and sector == original


@pytest.mark.parametrize("change", [
    {"title": "사업별 온실가스 감축량"}, {"title": "부문별 감축원단위"},
    {"item": sector_item(단위="대")},
    {"item": sector_item(fields={"부문": "건물", "사업명": "개별 사업", "예상감축량": 683})},
    {"item": sector_item(fields={"부문": "미상", "예상감축량": 683})},
    {"item": sector_item(fields={"부문": "건물", "예상감축량": 683, "활동량": 10})},
])
def test_sector_repair_never_guesses_ambiguous_or_project_roles(change):
    payload = chart("quantitative_reductions", [change.get("item", sector_item())],
                    title=change.get("title", "부문별 연도별 온실가스 감축량"))
    data = ImageAgent()._merge_image_results({}, [payload], "서울")
    assert not data["reduction_targets"]
    if "사업명" not in payload["table"][0]["fields"]:
        assert not data["quantitative_reductions"]
        assert data["chart_observations"][0]["보류사유"]


def test_child_identity_differs_but_same_child_conflict_still_retained(monkeypatch):
    from utils.fact_contracts import row_identity
    from utils.run_state import stable_entity_id
    base = {"지자체명": "서울", "사업명": "친환경 버스 보급", "연도": 2024, "재원구분": "시비", "예산단위": "백만원"}
    first, second = {**base, "행경로": ["전기버스 보급"], "예산액": 100}, {**base, "행경로": ["수소버스 보급"], "예산액": 100}
    assert row_identity("financial_plan", first) != row_identity("financial_plan", second)
    assert stable_entity_id("financial_plan", first) != stable_entity_id("financial_plan", second)
    for mode in ("off", "audit", "enforce"):
        monkeypatch.setattr(config, "FACT_CONTRACT_MODE", mode)
        conflicts = []
        merged = merge_extraction_rows("financial_plan", [], [first, second], conflicts)
        assert len(merged) == 2 and not conflicts
        conflicts = []
        merged = merge_extraction_rows("financial_plan", [first], [{**first, "예산액": 101}], conflicts)
        assert len(merged) == 2 and conflicts


def test_explicit_uncertainty_and_malformed_rows_are_durable(tmp_path):
    trace = VisionTrace(tmp_path / "review.xlsx")
    analysis = chart("financial_plan", [{"연도": 2025, "값": 100,
        "fields": {"사업명": "사업", "review_reasons": ["재원 머리글 불명확"]}}, "bad row"])
    data = ImageAgent(trace=trace)._merge_image_results({}, [analysis], "서울")
    assert not data["financial_plan"] and len(trace.decisions) == 2
    assert any("재원 머리글 불명확" in reason for d in trace.decisions.values() for reason in d["hold_reasons"])
    assert any(d["observation"]["원시행"] == "bad row" for d in trace.decisions.values())


def test_trace_write_failure_keeps_business_rows_and_excel(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("fixture", encoding="utf-8")
    trace = VisionTrace(blocker / "result.xlsx")
    data = ImageAgent(trace=trace)._merge_image_results({}, [chart("financial_plan", [
        {"연도": 2025, "값": 100, "fields": {"사업명": "실제 사업", "예산단위": "백만원"}}])], "서울")
    path = write_excel(OrganizerAgent().organize(data), tmp_path / "survived.xlsx")
    trace.finalize(path)
    assert path.exists() and trace.errors and read_rows(path, "11_재정투자계획")[0]["예산액"] == 100


def test_no_lineage_header_is_unassessed_instead_of_claimed_loss(tmp_path):
    trace = VisionTrace(tmp_path / "old.xlsx")
    ImageAgent(trace=trace)._merge_image_results({}, [chart("financial_plan", [
        {"연도": 2025, "값": 100, "fields": {"사업명": "사업"}}])], "서울")
    book = openpyxl.Workbook()
    book.active.title = "11_재정투자계획"
    book.active.append(["사업명", "연도", "예산액"])
    book.active.append(["사업", 2025, 100])
    path = tmp_path / "old.xlsx"
    book.save(path)
    assert trace.finalize(path)["excel_check"]["counts"] == {"unassessed": 1}


def test_post_clean_hold_keeps_its_reason_and_is_not_called_model_failure(tmp_path):
    trace = VisionTrace(tmp_path / "held.xlsx")
    raw = ImageAgent(trace=trace)._merge_image_results({}, [chart("financial_plan", [
        {"연도": 2025, "값": 100, "fields": {"사업명": "사업", "예산단위": "백만원"}}])], "서울")
    # A downstream validator's established quarantine format, after a valid read.
    final = {"_semantic_contract_quarantine": [{"sheet_key": "financial_plan",
        "row": deepcopy(raw["financial_plan"][0]), "reason_codes": ["fixture_downstream_conflict"]}]}
    path = write_excel(final, tmp_path / "held.xlsx")
    summary = trace.finalize(path, final)
    assert summary["decision_counts"] == {"eligible": 1}
    assert summary["excel_check"]["counts"] == {"held_after_cleaning": 1}
    saved = json.loads((trace.directory / "excel_links.jsonl").read_text(encoding="utf-8"))
    assert saved["post_processing_reviews"][0]["reason_codes"] == ["fixture_downstream_conflict"]


def test_same_output_name_keeps_previous_raw_response(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_client, "call_vision", lambda *a, **k: '{"type":"해당없음"}')
    first, second = VisionTrace(tmp_path / "same.xlsx"), VisionTrace(tmp_path / "same.xlsx")
    for trace in (first, second):
        ImageAgent(trace=trace)._chart_to_table({"base64": "fixture"}, 1, "서울")
    assert first.directory != second.directory
    assert all(Path(next(iter(t.calls.values()))["response_path"]).exists() for t in (first, second))


def test_guideline_scope_and_whole_blocks_without_extra_calls():
    from agents.guideline_agent import GuidelineAgent
    from utils.extraction_schema import BUSINESS_SHEETS
    agent = GuidelineAgent()
    assert set(agent.get_all_prompts()) == set(BUSINESS_SHEETS)
    assert all(agent.get_all_prompts().values())
    agent.guideline_text = "### 2.3 배출 현황\n\n배출량은 현황입니다.\n\n### 2.10 재정투자 계획\n\n예산은 연도·재원별로 구분합니다.\n\n국비와 시비는 구별합니다."
    snippets = agent._get_relevant_snippets(["예산", "국비"], sheet_key="financial_plan")
    assert snippets and all("[2.10 재정투자 계획]" in s for s in snippets)
    assert any(s.endswith("예산은 연도·재원별로 구분합니다.") for s in snippets)
    assert not any("배출 현황" in s for s in snippets)


def test_single_batch_and_text_instructions_share_roles_without_requiring_unknowns(monkeypatch):
    from agents.extractor_agent import _SHEET_CONFIGS
    from utils.extraction_schema import vision_contract
    instructions = vision_contract()
    assert all(word in instructions for word in ("부문별", "목표감축량", "사업명", "전기버스/수소버스", "review_reasons"))
    assert "선택 필드는 생략" in instructions
    assert "사업별 요청에는 넣지 마세요" in _SHEET_CONFIGS["quantitative_reductions"]["prompt"]
    prompts = []
    monkeypatch.setattr(llm_client, "call_vision", lambda image, prompt, **kwargs: prompts.append(prompt) or '{"type":"해당없음"}')
    ImageAgent()._chart_to_table_batch([(PageContent(1, "상위 사업: 버스 보급", [], []), {"base64": "fixture"})], "서울")
    assert "상위 사업: 버스 보급" in prompts[0] and instructions in prompts[0]


@pytest.mark.parametrize("sector", ["에너지", "흡수", "합계"])
def test_explicit_sector_alias_or_total_label_can_be_preserved(sector):
    payload = chart("quantitative_reductions", [sector_item(항목=sector, fields={"예상감축량": 683})],
                    title="부문별 연도별 온실가스 감축량")
    data = ImageAgent()._merge_image_results({}, [payload], "서울")
    assert data["reduction_targets"][0]["부문"] == sector
    assert data["reduction_targets"][0]["목표감축량"] == 683


def test_response_lineage_is_preserved_when_trace_lineage_is_added(tmp_path):
    source = {"object_id": "upstream-object", "evidence_id": "upstream-evidence", "bbox": [1, 2, 3, 4]}
    analysis = chart("financial_plan", [{"연도": 2025, "값": 100, "fields": {
        "사업명": "사업", "출처계보": json.dumps([source], ensure_ascii=False)}}])
    raw = ImageAgent(trace=VisionTrace(tmp_path / "lineage.xlsx"))._merge_image_results({}, [analysis], "서울")
    lineage = json.loads(raw["financial_plan"][0]["출처계보"])
    assert source in lineage and any(r.get("vision_decision_id") for r in lineage)


@pytest.mark.parametrize("legacy", [False, True])
def test_transport_exception_is_recorded_without_a_fabricated_response(tmp_path, monkeypatch, legacy):
    monkeypatch.setattr(config, "IMAGE_CHART_TABLE_EXTRACTION", not legacy)
    def timeout(*args, **kwargs):
        raise TimeoutError("fixture timeout")
    monkeypatch.setattr(llm_client, "call_vision", timeout)
    trace = VisionTrace(tmp_path / "transport.xlsx")
    with pytest.raises(TimeoutError):
        ImageAgent(trace=trace)._analyze_image({"base64": "fixture"}, 1, "서울")
    record = next(iter(trace.calls.values()))
    assert record["status"] == "transport_error" and not record["raw_saved"]
    assert record["error"]["type"] == "TimeoutError" and not Path(record["response_path"]).exists()

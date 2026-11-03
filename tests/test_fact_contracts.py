"""Real contracts/merge/cleaning/Excel; only the model transport is mocked."""
from copy import deepcopy
import json
from unittest.mock import patch

import openpyxl
import pytest

import config
from agents.excel_agent import ExcelAgent
from agents.extractor_agent import ExtractorAgent
from agents.organizer_agent import OrganizerAgent
from utils.document_objects import DocumentObject, build_document_objects, render_table_object_chunks
from utils.evidence_merge import merge_extraction_rows
from utils.fact_contracts import (annotate_row, apply_contracts, persist_contract_report,
                                  row_identity, unit_key)
from utils.pdf_reader import PageContent
from utils.run_state import RunState, merge_rows_stably, stable_entity_id
from utils.table_paths import render_paths, table_paths


HTML = '''<table><thead><tr><th rowspan="2">부문</th><th colspan="2">2030</th></tr>
<tr><th>목표배출량</th><th>목표감축량</th></tr></thead><tbody>
<tr><td>건물</td><td>6,005</td><td>1,000</td></tr>
<tr><td>수송</td><td>4,420</td><td>500</td></tr></tbody></table>'''


@pytest.fixture(autouse=True)
def experiment(monkeypatch):
    monkeypatch.setattr(config, "FACT_CONTRACT_MODE", "enforce")
    monkeypatch.setattr(config, "TABLE_HEADER_PATHS_ENABLED", True)
    monkeypatch.setattr(config, "TABLE_HEADER_PATHS_MAX_CHARS", 6000)
    monkeypatch.setattr(config, "SEMANTIC_CONTRACT_GUARDS_ENABLED", False)
    monkeypatch.setattr(config, "MAX_BATCH_CHARS", None)
    monkeypatch.setattr(config, "MAX_BATCH_TABLE_CELLS", None)
    monkeypatch.setattr(config, "EXTRACTION_OBJECT_BATCHING_ENABLED", False)


def source(html=HTML, caption="표 1 관리권한 감축목표 (단위: 천톤CO2eq)"):
    page = PageContent(1, caption, [html], [], table_records=[{"html": html, "bbox": [0, 0, 100, 100]}])
    objects = build_document_objects([page])
    obj = next(o for o in objects if o.object_type == "table")
    obj.caption = caption
    return page, obj


def target(**changes):
    row = {"지자체명": "테스트시", "목표수준": "부문", "목표범위": "관리권한", "부문": "건물",
           "기준연도": 2018, "목표연도": 2030, "목표배출량": 6005, "단위": "천톤CO2eq",
           "출처페이지": 1, "객체ID": "p1_table_1", "근거ID": "ev-p1_table_1",
           "_fact_cells": {"목표배출량": {"object_id": "p1_table_1", "row": 3, "column": 2}}}
    row.update(changes)
    return row


def read_rows(path, sheet="06_감축목표"):
    book = openpyxl.load_workbook(path, read_only=True, data_only=False)
    values = list(book[sheet].values)
    book.close()
    return [dict(zip(values[0], row)) for row in values[1:]]


def test_explicit_merged_header_paths_and_original_coordinates():
    _, obj = source()
    result = table_paths(obj)
    assert result["diagnostics"] == []
    first = result["cells"][0]
    assert first["column_path"] == ["2030", "목표배출량"]
    assert first["row_path"] == ["건물"] and first["raw_value"] == "6,005"
    assert first["unit"] == "천톤CO2eq" and first["row"] == 3
    assert first["bbox_scope"] == "object_not_cell"
    chunks = render_table_object_chunks(obj, rows_per_chunk=3)
    assert len(chunks) == 2
    assert '"row":4' in chunks[1] and '"row":3' not in chunks[1]


def test_blank_headers_not_forward_filled_and_budget_is_visible():
    _, obj = source()
    obj.metadata.pop("table_layout")
    obj.rows[0][2] = ""
    result = table_paths(obj)
    assert any(d["code"] == "ambiguous_blank_header" for d in result["diagnostics"])
    assert result["cells"][1]["column_path"] == ["목표감축량"]
    text, diagnostics = render_paths([obj], max_chars=300)
    assert len(text) <= 300
    assert any(d["code"] == "path_prompt_budget" for d in diagnostics)
    assert obj.rows[2][1] == "6,005"  # original is never truncated


def test_object_chunk_path_budget_is_persistable(monkeypatch):
    monkeypatch.setattr(config, "TABLE_HEADER_PATHS_MAX_CHARS", 300)
    _, obj = source()
    diagnostics = []
    chunks = render_table_object_chunks(obj, diagnostics=diagnostics)
    assert "6,005" in chunks[0] and "4,420" in chunks[0]
    assert any(d["code"] == "path_prompt_budget" for d in diagnostics)


def test_cell_contract_verified_without_learning_or_extra_calls():
    _, obj = source()
    row = annotate_row("reduction_targets", target(), [obj])
    contract = row["_fact_contract"]
    assert contract["status"] == "cell_verified"
    assert contract["facts"][0]["source_cell"]["column_path"] == ["2030", "목표배출량"]
    assert row["목표배출량"] == 6005


@pytest.mark.parametrize("changes,code", [
    ({"목표배출량": 9999}, "source_value_mismatch"),
    ({"목표연도": 2031}, "year_path_mismatch"),
    ({"단위": "tCO2eq"}, "unit_path_mismatch"),
    ({"단위": "천톤CO2"}, "unit_path_mismatch"),
    ({"부문": "수송"}, "row_subject_mismatch"),
    ({"목표범위": "관리권한+추가감축"}, "scope_path_mismatch"),
    ({"_fact_cells": {"목표배출량": {"object_id": "other", "row": 3, "column": 2}}}, "unknown_source_object"),
    ({"_fact_cells": {"목표배출량": {"object_id": "p1_table_1", "row": [3], "column": 2}}}, "invalid_cell_coordinates"),
    ({"목표배출량": 1000, "_fact_cells": {"목표배출량": {"object_id": "p1_table_1", "row": 3, "column": 3}}}, "metric_path_mismatch"),
])
def test_bad_proposal_held_before_excel_with_raw_value_preserved(changes, code, tmp_path):
    _, obj = source()
    original = target(**changes)
    data = OrganizerAgent().organize({"reduction_targets": [original], "document_objects": [obj.to_dict()]})
    assert data["reduction_targets"] == []
    held = [r for r in data["fact_contract_review"] if r["decision"] == "held"]
    assert code in held[0]["contract"]["errors"]
    assert held[0]["row"]["목표배출량"] == original["목표배출량"]
    path = ExcelAgent().write(data, tmp_path / "held.xlsx")
    assert read_rows(path) == []
    assert read_rows(path, "17_검증리포트")


def test_period_total_cannot_be_annual_budget():
    html = '<table><thead><tr><th>사업명</th><th>2024~2033 기간합계 예산액</th></tr></thead><tr><td>주택</td><td>10</td></tr></table>'
    _, obj = source(html, "표 2 투자계획 (단위: 백만원)")
    row = {"지자체명": "테스트시", "사업명": "주택", "예산액": 10, "연도": 2024, "예산단위": "백만원",
           "_fact_cells": {"예산액": {"object_id": obj.object_id, "row": 2, "column": 2}}}
    assert "period_total_as_annual" in annotate_row("financial_plan", row, [obj])["_fact_contract"]["errors"]
    row.update(연도=None, 기간시작=2024, 기간종료=2033, 시간기준="기간합계")
    assert not annotate_row("financial_plan", row, [obj])["_fact_contract"]["errors"]


def test_scope_plan_period_scale_and_metric_never_collapse():
    base = target(_fact_cells={})
    rows = [base, target(_fact_cells={}, 목표범위="관리권한+추가감축", 목표배출량=4420),
            target(_fact_cells={}, 계획문맥="추가계획"), target(_fact_cells={}, 단위="tCO2eq"),
            target(_fact_cells={}, 기준연도=2020), target(_fact_cells={}, 목표배출량=None, 목표감축량=6005)]
    # No invented cell IDs are required for frozen responses; unknown != verified.
    clean = OrganizerAgent().organize({"reduction_targets": rows})["reduction_targets"]
    assert len(clean) == 6
    assert len({stable_entity_id("reduction_targets", r) for r in clean}) == 6
    assert all(r["_fact_contract"]["status"] == "review" for r in clean)
    for ordered in (rows, list(reversed(rows))):
        assert len(merge_extraction_rows("reduction_targets", [], ordered)) == 6
    assert unit_key("tCO2eq") == unit_key("톤CO2eq")
    assert unit_key("tCO2eq") != unit_key("천톤CO2eq") != unit_key("천톤CO2")


def test_duplicate_and_checkpoint_resume_idempotent_conflicts_retained(tmp_path):
    row = target(_fact_cells={})
    duplicate = target(_fact_cells={}, 근거ID="ev-copy", 객체ID="copy", 출처페이지=2)
    conflicts = []
    merged = merge_extraction_rows("reduction_targets", [row], [duplicate], conflicts)
    assert len(merged) == 1 and set(merged[0]["source_evidence_ids"]) == {"ev-p1_table_1", "ev-copy"}
    conflict = target(_fact_cells={}, 목표배출량=6010)
    rows, conflicts = merge_rows_stably("reduction_targets", merged, [conflict])
    assert len(rows) == 2 and len(conflicts) == 1
    state = RunState(run_id="contract", root=tmp_path / "state", manifest={}, resume=True, retry_failed_only=False)
    kwargs = dict(batch_id="fixed", kind="sheet", sheet_keys=["reduction_targets"], page_nums=[1], status="ok", result=rows)
    state.record_batch(**kwargs)
    restored = state.restored_result("fixed")
    assert len(merge_extraction_rows("reduction_targets", restored, rows)) == 2


def test_off_and_audit_preserve_original_business_cells(tmp_path, monkeypatch):
    source_data = {"reduction_targets": [target()]}
    monkeypatch.setattr(config, "FACT_CONTRACT_MODE", "off")
    baseline = OrganizerAgent().organize(deepcopy(source_data))
    p1 = ExcelAgent().write(baseline, tmp_path / "off.xlsx")
    monkeypatch.setattr(config, "FACT_CONTRACT_MODE", "audit")
    audited = OrganizerAgent().organize(deepcopy(source_data))
    p2 = ExcelAgent().write(audited, tmp_path / "audit.xlsx")
    assert read_rows(p1) == read_rows(p2)
    assert "단위" not in read_rows(p2)[0]  # baseline target schema unchanged
    assert audited["fact_contract_review"]


def test_page_batch_paths_do_not_enable_object_calls_and_excel_unit_is_saved(tmp_path):
    page, obj = source()
    agent = ExtractorAgent()
    agent.document_objects = [obj]
    response = json.dumps({"reduction_targets": [target()]}, ensure_ascii=False)
    with patch("utils.llm_client.call_text", return_value=response) as model:
        rows = agent._extract_native_batch("reduction_targets", [page], "테스트시", "")
    assert model.call_count == 1
    prompt = model.call_args.args[0]
    assert "[사실 계약" in prompt and '"column_path":["2030","목표배출량"]' in prompt
    assert not config.EXTRACTION_OBJECT_BATCHING_ENABLED
    clean = OrganizerAgent().organize({"reduction_targets": rows, "document_objects": [obj.to_dict()]})
    path = ExcelAgent().write(clean, tmp_path / "actual.xlsx")
    actual = read_rows(path)
    assert actual[0]["목표배출량"] == 6005 and actual[0]["단위"] == "천톤CO2eq"
    saved = persist_contract_report(path, clean)
    report = json.loads(__import__('pathlib').Path(saved["path"]).read_text(encoding="utf-8"))
    assert saved["saved_rows"] == 1 and saved["missing_rows"] == 0
    assert report["saved_cell_checks"][0]["excel_rows"] == [2]
    assert not report["model_accuracy_evaluated"]


def test_audit_year_ancestor_enabled_only_when_explicitly_requested(monkeypatch):
    from utils.source_coverage import _header_years
    assert _header_years(["2030 / 예산액", "2024 / 2033 / 예산액", "2024~2033 / 합계"], None, header_paths=True) == {0: 2030}
    monkeypatch.setattr(config, "TABLE_HEADER_PATHS_ENABLED", False)
    assert _header_years(["2030 / 예산액"], None) == {}


def test_cell_identity_independent_of_neighbour_metrics_and_untrusted_ids():
    first = annotate_row("reduction_targets", target(_fact_cells={}))
    second = annotate_row("reduction_targets", target(_fact_cells={}, 목표감축량=1000,
        _fact_contract={"row_id": "model-invented", "status": "cell_verified"}))
    a = first["_fact_contract"]["facts"][0]["fact_id"]
    b = next(f["fact_id"] for f in second["_fact_contract"]["facts"] if f["key"]["metric"] == "목표배출량")
    assert a == b
    assert second["_fact_contract"]["row_id"] != "model-invented"
    assert second["_fact_contract"]["status"] == "review"


def test_rate_reaches_existing_percent_header_and_stable_repeat(tmp_path):
    raw = {"reduction_targets": [target(_fact_cells={}, 감축률=40)]}
    first = OrganizerAgent().organize(raw)
    again = OrganizerAgent().organize(first)
    assert first["reduction_targets"] == again["reduction_targets"]
    assert len(first["fact_contract_review"]) == len(again["fact_contract_review"])
    path = ExcelAgent().write(again, tmp_path / "rate.xlsx")
    assert read_rows(path)[0]["감축률(%)"] == 40
    assert persist_contract_report(path, again)["missing_rows"] == 0


def test_frozen_replay_command_and_input_preservation(tmp_path, monkeypatch):
    from scripts.replay_fact_contracts import replay
    monkeypatch.setattr("utils.llm_client.call_text", lambda *a, **kw: pytest.fail("no new call"))
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"raw_data": {"reduction_targets": [target(_fact_cells={})]}}, ensure_ascii=False), encoding="utf-8")
    original = raw.read_bytes()
    previous = config.FACT_CONTRACT_MODE
    summary = replay(raw, tmp_path / "compare")
    assert summary["new_model_calls"] == 0 and not summary["model_accuracy_evaluated"]
    assert set(summary["modes"]) == {"off", "audit", "enforce"}
    assert config.FACT_CONTRACT_MODE == previous and raw.read_bytes() == original
    with pytest.raises(ValueError, match="비어 있지"):
        replay(raw, tmp_path / "compare")


def test_year_cell_not_a_measure_and_annual_funding_total_not_period():
    html = '<table><thead><tr><th>사업명</th><th>목표연도</th><th>2030 예산액 합계</th></tr></thead><tr><td>주택</td><td>2030</td><td>10</td></tr></table>'
    _, obj = source(html, "표 2 투자계획 (단위: 백만원)")
    base = {"지자체명": "테스트시", "사업명": "주택", "예산액": 10, "연도": 2030, "예산단위": "백만원",
            "_fact_cells": {"예산액": {"object_id": obj.object_id, "row": 2, "column": 3}}}
    assert "period_total_as_annual" not in annotate_row("financial_plan", base, [obj])["_fact_contract"]["errors"]
    base.update(예산액=2030, _fact_cells={"예산액": {"object_id": obj.object_id, "row": 2, "column": 2}})
    assert "metric_path_mismatch" in annotate_row("financial_plan", base, [obj])["_fact_contract"]["errors"]


def test_typed_time_failures_use_existing_quarantine_and_empty_is_not_verified():
    raw = {"reduction_targets": [target(_fact_cells={}, 목표연도=[2030])]}
    data = OrganizerAgent().organize(raw)
    assert not data["reduction_targets"]
    assert any("fact_contract:invalid_time_value:목표연도" in r["reason_codes"] for r in data["_semantic_contract_quarantine"])
    empty = annotate_row("reduction_targets", target(_fact_cells={}, 목표배출량=None))
    assert empty["_fact_contract"]["status"] == "review"
    assert "no_measured_values" in empty["_fact_contract"]["notes"]

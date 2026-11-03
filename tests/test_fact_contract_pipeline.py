"""A real PDF -> extraction -> Organizer -> saved workbook and audit contract."""
import json
import re
from unittest.mock import patch

import fitz
import openpyxl

import config
from agents.supervisor import Supervisor


def test_pdf_to_final_cells_and_persisted_held_proposal_without_run_state(tmp_path, monkeypatch):
    for name, value in {"FACT_CONTRACT_MODE": "enforce", "TABLE_HEADER_PATHS_ENABLED": True,
                        "TABLE_HEADER_PATHS_MAX_CHARS": 6000, "FULL_DOCUMENT_SCAN": True,
                        "EXTRACTION_OBJECT_BATCHING_ENABLED": False, "MAX_BATCH_CHARS": None,
                        "MAX_BATCH_TABLE_CELLS": None, "SELECTIVE_OCR_ENABLED": False,
                        "RUN_STATE_ENABLED": False, "SEMANTIC_ROUTING_ENABLED": False,
                        "SEMANTIC_CONTRACT_GUARDS_ENABLED": False, "SOURCE_COVERAGE_MODE": "off",
                        "EVALUATION_MODE": "off"}.items():
        monkeypatch.setattr(config, name, value)
    pdf = tmp_path / "source.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((40, 50), "표 1 관리권한 감축목표 (단위: 천톤CO2eq)", fontname="korea")
        for y in (100, 135, 170):
            page.draw_line((40, y), (440, y))
        for x in (40, 170, 310, 440):
            page.draw_line((x, 100), (x, 170))
        for x, text in ((50, "부문"), (180, "목표연도"), (320, "목표배출량")):
            page.insert_text((x, 123), text, fontname="korea")
        for x, text in ((50, "건물"), (180, "2030"), (320, "6005")):
            page.insert_text((x, 158), text, fontname="korea")
        document.save(pdf)
    prompts = []
    def model(prompt, **kwargs):
        prompts.append(prompt)
        if "municipality_name" in prompt:
            return '{"municipality_name":"테스트시"}'
        keys = re.findall(r'\{\s*"([a-z_]+)"\s*:\s*\[', prompt)
        if not keys:
            return '{"assessment":"fixture only"}'
        key = keys[-1]
        rows = []
        if key == "document_meta":
            rows = [{"지자체명": "테스트시", "계획명": "기본계획"}]
        elif key == "reduction_targets":
            base = {"지자체명": "테스트시", "부문": "건물", "목표범위": "관리권한", "목표연도": 2030,
                    "단위": "천톤CO2eq", "출처페이지": 1,
                    "_fact_cells": {"목표배출량": {"object_id": "p1_table_1", "row": 2, "column": 3}}}
            rows = [{**base, "목표배출량": 6005}, {**base, "목표배출량": 4420}]
        return json.dumps({key: rows}, ensure_ascii=False)
    with patch("utils.llm_client.call_text", side_effect=model), patch(
            "utils.llm_client.call_vision", side_effect=AssertionError("no paid Vision call")):
        supervisor = Supervisor()
        path = supervisor.run(pdf, tmp_path / "result.xlsx", include_images=False, max_pipeline_retries=1)
    book = openpyxl.load_workbook(path, read_only=True)
    values = list(book["06_감축목표"].values)
    headers = values[0]
    assert len(values) == 2
    assert values[1][headers.index("목표배출량")] == 6005
    assert values[1][headers.index("단위")] == "천톤CO2eq"
    book.close()
    assert sum('"reduction_targets": [' in p for p in prompts) == 1
    assert any('"object_id":"p1_table_1"' in p and "[표 셀 경로" in p for p in prompts)
    audit = json.loads(path.with_name(path.stem + "_pipeline_audit.json").read_text(encoding="utf-8"))
    assert audit["ledgers"]["fact_contract_summary"]["held_rows"] == 1
    held = next(r for r in audit["ledgers"]["fact_contract_review"] if r["decision"] == "held")
    assert held["row"]["목표배출량"] == 4420 and "source_value_mismatch" in held["contract"]["errors"]
    assert audit["ledgers"]["object_processing_states"]["p1_table_1"]["body_reflected"]
    assert supervisor.last_outcome["fact_contracts"]["saved_rows"] == 1
    assert not supervisor.last_outcome["fact_contracts"]["missing_rows"]
    assert not supervisor.last_outcome["model_accuracy_evaluated"]

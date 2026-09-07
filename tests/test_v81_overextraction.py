"""v8-1 S2 — 과잉 추출 격리 확대: 추출 원천 태깅·표 우선 병합·배치 상한 분리·가드 확장."""

from __future__ import annotations

import config
from unittest.mock import patch

from agents.extractor_agent import ExtractorAgent, _batch_char_limit, _build_semantic_batches
from utils.pdf_reader import PageContent
from agents.organizer_agent import OrganizerAgent
from utils.run_state import (
    EXTRACTION_SOURCE_FIELD,
    EXTRACTION_SOURCE_TABLE_OBJECT,
    EXTRACTION_SOURCE_TEXT_CHUNK,
    merge_rows_stably,
    stable_row_id,
)

PREFER = (EXTRACTION_SOURCE_TABLE_OBJECT, EXTRACTION_SOURCE_TEXT_CHUNK)


def _mgmt_row(value: float, source: str | None, page: int = 10) -> dict:
    row = {
        "지자체명": "테스트시", "관리부문": "건물", "직간접구분": "직접", "연도": 2018,
        "배출량": value, "단위": "천톤CO2eq", "출처페이지": page,
    }
    if source:
        row[EXTRACTION_SOURCE_FIELD] = source
    return row


def test_table_object_row_wins_over_text_chunk_row_of_same_entity_and_page() -> None:
    table_row = _mgmt_row(100, EXTRACTION_SOURCE_TABLE_OBJECT)
    text_row = _mgmt_row(101, EXTRACTION_SOURCE_TEXT_CHUNK)

    rows, conflicts = merge_rows_stably("emissions_management", [table_row], [text_row], prefer=PREFER)
    assert [row["배출량"] for row in rows] == [100]
    assert conflicts[0]["resolution"] == "table_object_preferred"

    # 순서가 바뀌어도(본문청크가 먼저 있어도) 표객체 행이 남는다.
    rows, conflicts = merge_rows_stably("emissions_management", [text_row], [table_row], prefer=PREFER)
    assert [row["배출량"] for row in rows] == [100]
    assert conflicts[0]["resolution"] == "table_object_preferred"
    assert conflicts[0]["kept_row_id"] == stable_row_id("emissions_management", table_row)


def test_conflicts_from_different_pages_or_without_tags_keep_both_rows() -> None:
    table_row = _mgmt_row(100, EXTRACTION_SOURCE_TABLE_OBJECT, page=10)
    text_row = _mgmt_row(101, EXTRACTION_SOURCE_TEXT_CHUNK, page=11)
    rows, conflicts = merge_rows_stably("emissions_management", [table_row], [text_row], prefer=PREFER)
    assert len(rows) == 2 and "resolution" not in conflicts[0]

    rows, conflicts = merge_rows_stably(
        "emissions_management", [_mgmt_row(100, None)], [_mgmt_row(101, None)], prefer=PREFER,
    )
    assert len(rows) == 2 and "resolution" not in conflicts[0]

    # prefer 미지정이면 태그가 있어도 기존 동작(둘 다 보존).
    rows, _ = merge_rows_stably("emissions_management", [table_row], [_mgmt_row(101, EXTRACTION_SOURCE_TEXT_CHUNK)])
    assert len(rows) == 2


def test_source_tag_is_volatile_for_row_identity_and_stripped_by_organizer() -> None:
    assert stable_row_id("emissions_management", _mgmt_row(1, EXTRACTION_SOURCE_TABLE_OBJECT)) == stable_row_id(
        "emissions_management", _mgmt_row(1, None)
    )
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_management": [_mgmt_row(1, EXTRACTION_SOURCE_TABLE_OBJECT)],
    })
    assert EXTRACTION_SOURCE_FIELD not in cleaned["emissions_management"][0]
    assert all(EXTRACTION_SOURCE_FIELD not in headers for headers in config.EXCEL_HEADERS.values())


def test_child_rows_are_tagged_by_object_id() -> None:
    table_rows = ExtractorAgent._tag_child_rows({"object_id": "p3_table_2"}, [{"a": 1}])
    text_rows = ExtractorAgent._tag_child_rows({"object_id": "p3_text"}, [{"a": 1}])
    plain_rows = ExtractorAgent._tag_child_rows({}, [{"a": 1}])
    assert table_rows[0][EXTRACTION_SOURCE_FIELD] == EXTRACTION_SOURCE_TABLE_OBJECT
    assert text_rows[0][EXTRACTION_SOURCE_FIELD] == EXTRACTION_SOURCE_TEXT_CHUNK
    assert EXTRACTION_SOURCE_FIELD not in plain_rows[0]


def _pages(count: int, chars: int) -> list[PageContent]:
    return [PageContent(page_number=i, text="가" * chars, tables=[], images=[]) for i in range(1, count + 1)]


def test_api_backend_has_no_batch_char_limit_by_default_but_local_agent_keeps_it() -> None:
    with patch.object(config, "LLM_PROVIDER", "gemini"), patch.object(config, "STAGE_PROVIDERS", {}):
        assert _batch_char_limit(0) is None
        assert len(_build_semantic_batches(_pages(15, 5000), 15)) == 1
        assert _batch_char_limit(1) == config.EXTRACTION_RECOVERY_MAX_BATCH_CHARS
    with patch.object(config, "LLM_PROVIDER", "codex"), patch.object(config, "STAGE_PROVIDERS", {}):
        assert _batch_char_limit(0) == config.EXTRACTION_MAX_BATCH_CHARS
        assert len(_build_semantic_batches(_pages(15, 5000), 15)) > 1
    with patch.object(config, "LLM_PROVIDER", "gemini"), patch.object(config, "STAGE_PROVIDERS", {}), \
            patch.object(config, "EXTRACTION_MAX_BATCH_CHARS_API", 12000):
        assert _batch_char_limit(0) == 12000
        assert len(_build_semantic_batches(_pages(15, 5000), 15)) > 1
    # 단계별 백엔드 지정이 우선한다.
    with patch.object(config, "LLM_PROVIDER", "gemini"), \
            patch.object(config, "STAGE_PROVIDERS", {"extraction": "codex"}):
        assert _batch_char_limit(0) == config.EXTRACTION_MAX_BATCH_CHARS

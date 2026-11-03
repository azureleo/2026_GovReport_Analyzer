"""Bounded, source-addressed header paths; no learned DeepTable components.

Coordinates are one-based in the ORIGINAL expanded table (not a chunk). Empty
headers are never forward-filled without explicit rowspan/colspan evidence.
"""
from __future__ import annotations

import json
import re


def _text(value):
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _number(value):
    return bool(re.fullmatch(r"[-+−]?\d[\d,]*(?:\.\d+)?\s*[%％]?", _text(value)))


def _path(values):
    return list(dict.fromkeys(_text(v) for v in values if _text(v)))


def table_paths(obj):
    """Return cells plus honest structural diagnostics, without guessing spans."""
    rows, meta = obj.rows, obj.metadata
    if not rows or meta.get("table_parse_error"):
        return {"cells": [], "diagnostics": [{"code": "unsupported_table_structure"}]}
    from utils.document_objects import table_header_row_count
    headers = int(meta.get("header_row_count") or table_header_row_count(rows))
    if headers < 1 or headers >= len(rows) or len({len(r) for r in rows}) != 1:
        return {"cells": [], "diagnostics": [{"code": "unresolved_table_headers"}]}
    explicit = bool(meta.get("header_rows_explicit"))
    diagnostics = [] if explicit else [{"code": "heuristic_header_boundary", "header_rows": headers}]
    columns = [_path(rows[r][c] for r in range(headers)) for c in range(len(rows[0]))]
    # A blank inside a header path is ambiguous unless it belongs to a known span.
    for c in range(len(columns)):
        if any(not _text(rows[r][c]) for r in range(headers)):
            diagnostics.append({"code": "ambiguous_blank_header", "column": c + 1})
    units = re.findall(r"단위\s*[:：]\s*([^\n)）]+)", obj.caption + "\n" + obj.section)
    unit = _text(meta.get("unit") or (units[0] if len(set(units)) == 1 else ""))
    if re.search(r"[,/·]", unit):
        diagnostics.append({"code": "mixed_table_unit_unresolved", "literal": unit})
        unit = ""
    name_headers = {"구분", "부문", "세부부문", "사업명", "관리번호", "항목", "지표명"}
    row_cols = meta.get("row_header_columns")
    if row_cols is None:
        row_cols = [c + 1 for c, path in enumerate(columns) if any(v in name_headers for v in path)]
    if not row_cols:
        diagnostics.append({"code": "unresolved_row_header_columns"})
    cells = []
    for r in range(headers, len(rows)):
        # Repeated headers inside a split table are context, never value cells.
        if rows[r] in rows[:headers]:
            continue
        row_path = _path(rows[r][c - 1] for c in row_cols if 0 < c <= len(rows[r]))
        for c, value in enumerate(rows[r]):
            if c + 1 in row_cols or not _number(value):
                continue
            inline = re.findall(r"[（(]([^()（）]+)[)）]", " ".join(columns[c]))
            row_units = [rows[r][cc] for cc, path in enumerate(columns) if path == ["단위"] and _text(rows[r][cc])]
            cell_unit = row_units[0] if len(set(row_units)) == 1 else ""
            from utils.source_coverage import _embedded_unit
            inline_unit = _embedded_unit("(" + inline[-1] + ")") if inline else ""
            cell_unit = cell_unit or inline_unit or unit
            if re.search(r"감축률|감축율", " ".join(columns[c])) and not inline_unit:
                cell_unit = "%" if _text(value).endswith(("%", "％")) else ""
            cells.append({"object_id": obj.object_id, "evidence_id": meta.get("evidence_id", ""),
                "page": obj.page_number, "row": r + 1, "column": c + 1,
                "row_path": row_path, "column_path": columns[c], "raw_value": _text(value),
                "unit": cell_unit, "table_unit": unit,
                "bbox": list(obj.bbox) if obj.bbox else None, "bbox_scope": "object_not_cell",
                "structure_status": "explicit_headers" if explicit else "heuristic_headers"})
    return {"cells": cells, "diagnostics": diagnostics, "header_rows": headers,
            "row_header_columns": row_cols}


def render_paths(objects, *, max_chars=6000, row_numbers=None):
    """Bounded enrichment only. Original table is always retained by the caller."""
    records, diagnostics, omitted = [], [], 0
    for obj in objects:
        result = table_paths(obj)
        diagnostics.extend({"object_id": obj.object_id, **d} for d in result["diagnostics"])
        for cell in result["cells"]:
            if row_numbers is not None and cell["row"] not in row_numbers:
                continue
            records.append(cell)
    heading = "[표 셀 경로: 원본 표의 1-based 행·열; 불명확한 경로는 추정 금지]\n"
    lines = []
    size = len(heading)
    for cell in records:
        # Bbox stays in the audit; avoid repeating it for every prompt cell.
        shown = {k: v for k, v in cell.items() if k not in {"bbox", "bbox_scope", "table_unit"}}
        line = json.dumps(shown, ensure_ascii=False, separators=(",", ":"))
        if size + len(line) + 1 > max_chars:
            omitted += 1
            continue
        lines.append(line)
        size += len(line) + 1
    if omitted:
        diagnostics.append({"code": "path_prompt_budget", "omitted_cells": omitted,
                            "original_table_preserved": True})
    return heading + "\n".join(lines) if lines else "", diagnostics

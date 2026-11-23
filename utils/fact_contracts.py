"""TRACE-inspired observation identities, source-cell checks and conservative merge.

The paper's retrieval/training system is NOT implemented here. A business row
can carry several metrics: each receives its own identity and source locator.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import re

from utils.extraction_schema import METRIC_UNITS, display_field, headers_for, normalize_row

VERSION = "fact-contract-v1"
METRICS = METRIC_UNITS
CONTEXT_FIELDS = ("계획구분", "계획문맥", "재원구분", "시나리오", "배출유형", "직간접구분",
                  "인벤토리출처", "지표종류", "감축량유형", "모니터링인자", "목표수준")
SCOPES = ("목표범위", "전망범위", "배출범위")
TIME_FIELDS = ("연도", "목표연도", "기준연도", "기간시작", "기간종료", "시간기준")
ENTITY_FIELDS = ("지자체명", "관리번호", "사업명", "부문", "관리부문", "세부부문", "지표명", "지표범주", "지표세부범주")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _token(value):
    return re.sub(r"\s+", "", str(value if value is not None else ""))


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def mode():
    import config
    return getattr(config, "FACT_CONTRACT_MODE", "off")


def unit_key(value):
    from utils.source_coverage import unit
    if _token(value) in {"%", "％", "퍼센트"}:
        return {"dimension": "ratio_percent", "scale": "1"}
    if not _token(value):
        return {"dimension": "unknown", "scale": "unknown"}
    dimension, scale = unit(value)
    return {"dimension": dimension, "scale": str(scale)}


def row_identity(sheet, row):
    """A normalized tuple shared by merge and checkpoints; never guesses context."""
    fields = ENTITY_FIELDS + SCOPES + CONTEXT_FIELDS + TIME_FIELDS
    key = {f: _token(row.get(f)) for f in fields}
    if sheet == "financial_plan" and row.get("행경로") not in (None, "", []):
        key["행경로"] = canonical(row["행경로"])
    from utils.source_coverage import number
    for field in TIME_FIELDS:
        if field != "시간기준" and (parsed := number(row.get(field))) is not None:
            key[field] = str(parsed.normalize())
    key["sheet"] = sheet
    key["metrics"] = {f: unit_key(unit_field if unit_field == "%" else row.get(unit_field))
                      for f, unit_field in METRICS.get(sheet, {}).items() if row.get(f) not in (None, "")}
    # Unknown identities can agree only under the same source anchor, not globally.
    required = ["지자체명"]
    if sheet == "reduction_targets":
        required += ["목표범위", "목표연도", "부문"]
    elif sheet in {"financial_plan", "annual_implementation", "quantitative_reductions"}:
        required += ["사업명"]
    if any(not _token(row.get(f)) for f in required) or any(v["dimension"] == "unknown" for v in key["metrics"].values()):
        key["unresolved_source_anchor"] = row.get("_fact_cells") or {
            f: row.get(f) for f in ("객체ID", "근거ID", "출처페이지")}
    return key


def _metric_kind(text):
    text = _token(text)
    if re.search(r"감축률|감축율", text):
        return "감축률"
    if "목표배출" in text:
        return "목표배출량"
    if re.search(r"예산|사업비|투자액|투자비", text):
        return "예산액"
    if re.search(r"목표감축|예상감축|감축량", text):
        return "감축량"
    if re.search(r"배출전망|전망값|BAU", text, re.I):
        return "전망값"
    if "기준배출" in text:
        return "기준배출량"
    if re.search(r"(?:기준|목표|점검)?연도", text):
        return "year"
    return ""


def _check_cell(field, row, reference, objects, cell_index=None):
    from utils.source_coverage import number
    from utils.table_paths import table_paths
    errors, notes = [], []
    if not isinstance(reference, dict):
        return None, ["invalid_cell_reference"], notes
    oid = reference.get("object_id")
    obj = objects.get(oid) if isinstance(oid, str) else None
    if obj is None:
        return None, ["unknown_source_object"], notes
    if reference.get("page") is not None and reference["page"] != obj.page_number:
        errors.append("source_page_mismatch")
    if reference.get("evidence_id") is not None and reference["evidence_id"] != obj.metadata.get("evidence_id"):
        errors.append("source_evidence_mismatch")
    rr, cc = reference.get("row"), reference.get("column")
    if type(rr) is not int or type(cc) is not int or rr < 1 or cc < 1:
        return None, ["invalid_cell_coordinates"], notes
    if cell_index is not None:
        if oid not in cell_index:
            cell_index[oid] = table_paths(obj)
        result = cell_index[oid]
    else:
        result = table_paths(obj)
    cell = next((c for c in result["cells"] if c["row"] == rr and c["column"] == cc), None)
    if cell is None:
        return None, ["cell_not_numeric_body_value"], notes
    if number(row.get(field)) is None or number(row.get(field)) != number(cell["raw_value"].rstrip("%％")):
        errors.append("source_value_mismatch")
    path = " ".join(cell["column_path"] + cell["row_path"])
    actual_kind = _metric_kind(path)
    expected = {"목표감축량": "감축량", "예상감축량": "감축량", "배출전망": "전망값"}.get(field, field)
    if not actual_kind and any(re.sub(r"[（(].*?[)）]", "", _token(p)) == field for p in cell["column_path"]):
        actual_kind = expected
    if actual_kind and actual_kind != expected:
        errors.append("metric_path_mismatch")
    if not actual_kind:
        notes.append("metric_path_unresolved")
    years = set(re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", path))
    expected_year = row.get("기준연도") if field == "기준배출량" else row.get("연도") or row.get("목표연도")
    period = bool(re.search(r"누계|누적|기간합계|(?:19|20)\d{2}\s*[~～–—-]\s*(?:19|20)\d{2}", path))
    if expected_year and years and str(expected_year) not in years:
        errors.append("year_path_mismatch")
    if len(years) > 1 and not period:
        notes.append("year_path_ambiguous")
    if period and years and any(row.get(f) and str(row[f]) not in years for f in ("기간시작", "기간종료")):
        errors.append("period_path_mismatch")
    if period and row.get("연도") not in (None, ""):
        errors.append("period_total_as_annual")
    if not years:
        notes.append("year_path_unresolved")
    unit_field = METRICS.get(row.get("_contract_sheet"), {}).get(field, "단위")
    raw_unit = "%" if unit_field == "%" else row.get(unit_field)
    if raw_unit and cell["unit"] and unit_key(raw_unit) != unit_key(cell["unit"]):
        errors.append("unit_path_mismatch")
    if not raw_unit or not cell["unit"]:
        notes.append("unit_unresolved")
    subject = row.get("사업명") or row.get("부문") or row.get("관리부문") or row.get("지표명")
    if subject and cell["row_path"] and _token(subject) not in {_token(x) for x in cell["row_path"]}:
        errors.append("row_subject_mismatch")
    if not cell["row_path"]:
        notes.append("row_subject_unresolved")
    context = _token(path + " " + obj.caption + " " + obj.section)
    scope = row.get("목표범위")
    scopes = set(re.findall(r"관리권한\+추가감축|지역전체|관리권한", context))
    if scope and scopes and _token(scope) not in scopes:
        errors.append("scope_path_mismatch")
    elif scope and not scopes:
        notes.append("scope_not_attested")
    if scope and len(scopes) > 1:
        local_scopes = set(re.findall(r"관리권한\+추가감축|지역전체|관리권한", _token(path)))
        if local_scopes != {_token(scope)}:
            notes.append("scope_path_ambiguous")
    for field in ("계획구분", "계획문맥", "재원구분"):
        if row.get(field) and _token(row[field]) not in context:
            notes.append(field + "_not_attested")
    if cell["structure_status"] != "explicit_headers" or any(
            d["code"] == "ambiguous_blank_header" and d["column"] == cc for d in result["diagnostics"]):
        notes.append("header_structure_unresolved")
    return cell, sorted(set(errors)), sorted(set(notes))


def annotate_row(sheet, row, objects=(), *, cell_index=None):
    """Rebuild from business fields and actual objects, never trust model contracts."""
    if sheet not in METRICS:
        return deepcopy(row)
    row = deepcopy(row)
    objects = objects if isinstance(objects, dict) else {obj.object_id: obj for obj in objects}
    identity = row_identity(sheet, row)
    facts, errors, notes = [], [], []
    refs = row.get("_fact_cells", {})
    from utils.source_coverage import number
    for field in TIME_FIELDS:
        if field != "시간기준" and row.get(field) not in (None, ""):
            value = number(row[field])
            if value is None or value != value.to_integral_value() or not 1 <= value <= 9999:
                errors.append("invalid_time_value:" + field)
    start, end = number(row.get("기간시작")), number(row.get("기간종료"))
    if start is not None and end is not None and start > end:
        errors.append("invalid_period_range")
    if not isinstance(refs, dict):
        errors.append("invalid_fact_cells")
        refs = {}
    for field, unit_field in METRICS[sheet].items():
        if row.get(field) in (None, ""):
            continue
        field_errors, field_notes, cell = [], [], None
        if number(row[field]) is None:
            field_errors.append("invalid_numeric_value")
        if field in refs:
            cell, more_errors, field_notes = _check_cell(field, {**row, "_contract_sheet": sheet}, refs[field], objects, cell_index)
            field_errors.extend(more_errors)
        else:
            field_notes.append("source_cell_unassessed")
        raw_unit = "%" if unit_field == "%" else row.get(unit_field)
        if not raw_unit:
            field_notes.append("unit_unresolved")
        dimension = unit_key(raw_unit)["dimension"]
        allowed = ({"krw"} if field == "예산액" else {"ratio_percent"} if field == "감축률" else
                   {"co2", "co2eq"} if sheet in {"reduction_targets", "emissions_regional", "emissions_management", "emissions_forecast"}
                   or field == "예상감축량" else None)
        if allowed and dimension != "unknown" and dimension not in allowed:
            (field_notes if dimension.startswith("literal:") else field_errors).append("metric_unit_dimension_mismatch")
        # A cell's ID must not change merely because a neighbouring metric was
        # absent in another response (wide row != single fact).
        metric_row = {k: v for k, v in row.items() if k not in METRICS[sheet] or k == field}
        metric_identity = row_identity(sheet, metric_row)
        metric_identity.pop("metrics", None)
        key = {"identity": metric_identity, "metric": field, "unit": unit_key(raw_unit)}
        fact = {"fact_id": _hash(key), "key": key, "value": row[field], "unit_literal": raw_unit,
                "source_cell": cell, "errors": sorted(set(field_errors)), "notes": sorted(set(field_notes)),
                "status": "invalid" if field_errors else "review" if field_notes else "cell_verified"}
        facts.append(fact)
        errors.extend(field_errors)
        notes.extend(field_notes)
    if sheet == "reduction_targets" and not row.get("목표범위"):
        notes.append("target_scope_unresolved")
    if sheet == "reduction_targets" and not row.get("목표연도"):
        notes.append("target_year_unresolved")
    if not row.get("지자체명"):
        notes.append("municipality_unresolved")
    if not facts:
        notes.append("no_measured_values")
    if sheet in {"annual_implementation", "financial_plan", "quantitative_reductions"} and not any(row.get(f) for f in ("연도", "기간시작", "기간종료")):
        notes.append("time_unresolved")
    row["_fact_contract"] = {"version": VERSION, "row_key": identity, "row_id": _hash(identity),
        "facts": facts, "errors": sorted(set(errors)), "notes": sorted(set(notes)),
        "status": "invalid" if errors else "review" if notes else "cell_verified"}
    if mode() == "enforce":
        # Existing inventory/semantic/source consumers must also see the real
        # table anchors, not only the enclosing page-batch synthetic object ID.
        from utils.evidence_merge import _merge_lineage
        for fact in facts:
            cell = fact["source_cell"]
            if cell is None or fact["errors"]:
                continue
            lineage = {"source": "fact_contract_cell", "object_id": cell["object_id"],
                       "evidence_id": cell["evidence_id"], "page_numbers": [cell["page"]],
                       "table_row": cell["row"], "table_column": cell["column"],
                       "row_path": cell["row_path"], "column_path": cell["column_path"],
                       "bbox": cell["bbox"], "bbox_scope": "object_not_cell"}
            _merge_lineage(row, {"_extraction_sources": [lineage],
                "source_object_ids": [cell["object_id"]],
                "source_evidence_ids": [cell["evidence_id"]] if cell["evidence_id"] else []})
    return row


def merge_rows(sheet, existing, incoming, conflicts=None):
    """Dedup only matching identity AND values. Complementary facts stay separate.

    A missing metric/context is not a wildcard. No source rank overwrites values.
    """
    from utils.evidence_merge import _merge_lineage, _payload
    output = []
    known = {c.get("conflict_id") for c in conflicts or []}
    for original in [*existing, *incoming]:
        if not isinstance(original, dict):
            continue
        row = deepcopy(original)
        key = row_identity(sheet, row)
        duplicate = next((r for r in output if row_identity(sheet, r) == key and _payload(r) == _payload(row)), None)
        if duplicate is not None:
            _merge_lineage(duplicate, row)
            aliases = duplicate.get("_fact_cell_aliases", []) + row.get("_fact_cell_aliases", [])
            aliases += [r["_fact_cells"] for r in (duplicate, row) if r.get("_fact_cells")]
            if aliases:
                duplicate["_fact_cell_aliases"] = list({canonical(a): a for a in aliases}.values())
            continue
        for previous in output:
            if row_identity(sheet, previous) != key:
                continue
            differences = {f: {"existing": previous[f], "incoming": row[f]} for f in METRICS[sheet]
                           if previous.get(f) not in (None, "") and row.get(f) not in (None, "")
                           and previous[f] != row[f]}
            if not differences:
                continue
            conflict = {"sheet_key": sheet, "status": "fact_contract_conflict_retained",
                        "entity_key": key, "differences": differences}
            cid = _hash({"sheet": sheet, "key": key, "values": {
                f: sorted([canonical(d["existing"]), canonical(d["incoming"])]) for f, d in differences.items()}})
            conflict["conflict_id"] = cid
            if conflicts is not None and cid not in known:
                conflicts.append(conflict)
                known.add(cid)
            for r in (row, previous):
                r["_merge_conflicts"] = sorted(set(r.get("_merge_conflicts", []) + [cid]))
        output.append(row)
    return output


def apply_contracts(data, *, stage="final"):
    """Final pre-save gate, also supports frozen legacy responses and run-state OFF."""
    if mode() == "off":
        return data
    from utils.document_objects import DocumentObject
    objects = [DocumentObject.from_dict(o) if isinstance(o, dict) else o for o in data.get("document_objects", [])]
    objects = {obj.object_id: obj for obj in objects}
    cell_index = {}
    from utils.source_coverage import number
    def ledger_key(record):
        row = record.get("row", {})
        sheet = record.get("sheet_key")
        values = {field: str(n.normalize()) if (n := number(row.get(field))) is not None else row.get(field)
                  for field in METRICS.get(sheet, {}) if row.get(field) not in (None, "")}
        return canonical({"sheet": sheet, "stage": record.get("stage", "final"),
            "decision": record.get("decision"), "key": record.get("contract", {}).get("row_id"),
            "values": values, "cells": row.get("_fact_cells"),
            "source": {k: row.get(k) for k in ("객체ID", "근거ID", "출처페이지")}})
    previous = data.get("fact_contract_review", [])
    ledger = {ledger_key(r): deepcopy(r) for r in previous}
    counts = Counter()
    for sheet in METRICS:
        rows = []
        for original in data.get(sheet, []):
            row = annotate_row(sheet, original, objects, cell_index=cell_index)
            contract = row["_fact_contract"]
            held = mode() == "enforce" and bool(contract["errors"])
            counts["held_rows" if held else "retained_rows"] += 1
            counts[contract["status"]] += 1
            record = {"sheet_key": sheet, "stage": stage, "decision": "held" if held else "retained",
                      "row": row, "contract": contract}
            ledger.setdefault(ledger_key(record), record)
            if contract["errors"] or contract["notes"]:
                issue = {"대상시트": sheet, "항목": "사실 계약 보류" if held else "사실 계약 검토",
                         "사유": ",".join(contract["errors"] + contract["notes"]),
                         "출처페이지": row.get("출처페이지"), "근거ID": row.get("근거ID")}
                if issue not in data.setdefault("validation_report", []):
                    data["validation_report"].append(issue)
            if not held:
                rows.append(row)
            else:
                # Existing downstream coverage/quality consumers already know
                # this quarantine schema. Keep the dedicated detailed ledger too.
                quarantine = {"sheet_key": sheet, "reason_codes": ["fact_contract:" + e for e in contract["errors"]],
                              "row": deepcopy(row), "producer": VERSION}
                if quarantine not in data.setdefault("_semantic_contract_quarantine", []):
                    data["_semantic_contract_quarantine"].append(quarantine)
        if mode() == "enforce":
            conflicts = data.setdefault("merge_conflicts", [])
            rows = merge_rows(sheet, [], rows, conflicts)
        data[sheet] = rows
    data["fact_contract_review"] = list(ledger.values())
    counts["retained_rows"] = sum(len(data.get(sheet, [])) for sheet in METRICS)
    counts["held_rows"] = sum(r["decision"] == "held" for r in ledger.values())
    data["fact_contract_summary"] = {"version": VERSION, "mode": mode(), "stage": stage,
        "ledger_records": len(ledger), **counts,
        "model_accuracy_evaluated": False}
    return data


def excel_headers(sheet, headers):
    """Output preservation is independent of optional fact enforcement."""
    return headers_for(sheet, headers)


def persist_contract_report(output_path, data):
    """Re-read saved cells. This checks wiring, not PDF truth/model accuracy."""
    from pathlib import Path
    import openpyxl
    import config
    from utils.integration_audit import save_json
    from utils.source_coverage import number
    checks = []
    book = openpyxl.load_workbook(output_path, read_only=True, data_only=False)
    try:
        for sheet, fields in METRICS.items():
            name = config.SHEET_KEY_TO_NAME[sheet]
            records = list(book[name].values)
            headers = records[0]
            saved = [dict(zip(headers, r)) for r in records[1:]]
            for source_row in data.get(sheet, []):
                row = normalize_row(sheet, source_row)
                contract = row.get("_fact_contract") or annotate_row(sheet, row)["_fact_contract"]
                display = lambda f: display_field(sheet, f)
                held_fields = {item["canonical"] for item in row.get("_field_alias_conflicts", [])}
                compare = [f for f in (*ENTITY_FIELDS, *SCOPES, *CONTEXT_FIELDS, *TIME_FIELDS, *fields)
                           if display(f) in headers and row.get(f) not in (None, "") and f not in held_fields]
                def agrees(actual):
                    return all(number(actual.get(display(f))) == number(row[f]) if f in fields else
                               _token(actual.get(f)) == _token(row[f]) for f in compare)
                matches = [index + 2 for index, actual in enumerate(saved) if agrees(actual)]
                omitted = [f for f in (*ENTITY_FIELDS, *SCOPES, *CONTEXT_FIELDS, *TIME_FIELDS, *fields)
                           if display(f) not in headers and row.get(f) not in (None, "")]
                checks.append({"sheet": sheet, "row_id": contract["row_id"], "excel_rows": matches,
                               "status": ("saved_with_held_fields" if matches and held_fields else
                                          "saved_with_unwritten_axes" if matches and omitted else "saved" if matches else "missing"),
                               "checked_fields": compare, "unwritten_fields": omitted,
                                "held_fields": sorted(held_fields),
                               "fact_ids": [f["fact_id"] for f in contract["facts"]]})
    finally:
        book.close()
    path = Path(output_path).with_name(Path(output_path).stem + "_fact_contracts.json")
    payload = {"version": VERSION, "mode": mode(), "model_accuracy_evaluated": False,
               "summary": data.get("fact_contract_summary", {}), "saved_cell_checks": checks,
               "review": data.get("fact_contract_review", [])}
    save_json(path, payload)
    return {"path": str(path), "saved_rows": sum(c["status"] == "saved" for c in checks),
            "missing_rows": sum(c["status"] == "missing" for c in checks),
            "unwritten_axis_rows": sum(c["status"] == "saved_with_unwritten_axes" for c in checks),
             "held_field_rows": sum(c["status"] == "saved_with_held_fields" for c in checks),
            "model_accuracy_evaluated": False}


CONTRACT_PROMPT = """
[사실 계약: 값을 채우기 전에 행의 관측 대상을 구분]
사업·부문/세부부문, 지표 종류, 목표범위, 기준연도와 목표연도 또는 기간,
단위(배율 포함), 계획구분·계획문맥을 원문에 명시된 경우만 기록하세요.
관리권한과 관리권한+추가감축, 목표배출량과 감축량·감축률, 예산과 감축량,
연간값과 기간합계를 합치지 마세요. 없는 범위·연도·단위는 null로 두세요.
표 셀 경로가 제공된 경우 각 수치의 _fact_cells를 다음 형식으로 추가하세요:
"_fact_cells":{"목표배출량":{"object_id":"원문 객체ID","row":3,"column":2}}
좌표는 청크가 아닌 원본 확장 표의 1부터 시작하는 행·열입니다. 경로가 없으면
좌표를 만들지 마세요. 계약·사실ID는 프로그램이 생성하므로 직접 생성하지 마세요.
"""

"""Convert explicit Vision fields into shared business rows without guessing roles."""
from __future__ import annotations

from copy import deepcopy
import math
import re

from utils.extraction_schema import (FINANCE_SOURCES, METRIC_UNITS, PROVENANCE_FIELDS, VISION_BODY_SHEETS,
    canonical_fields, normalize_row, present)


def numeric(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(str(value).replace(",", "").strip().rstrip("%"))
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def flatten_item(item):
    """Explicit row keys and fields must agree; null wrappers cannot erase fields."""
    flat = deepcopy(item.get("fields")) if isinstance(item.get("fields"), dict) else {}
    conflicts = []
    for key, value in item.items():
        if key == "fields" or not present(value):
            continue
        if key in flat and present(flat[key]) and flat[key] != value:
            a, b = numeric(flat[key]), numeric(value)
            if a is None or b is None or a != b:
                conflicts.append("중첩 필드 충돌:" + key)
        flat[key] = deepcopy(value)
    return flat, conflicts


def _metric_hint(sheet, flat, analysis):
    metrics = METRIC_UNITS.get(sheet, {})
    # A precise declared field takes precedence over a multi-metric table title.
    for key in ("지표종류", "값필드", "종류"):
        hint = re.sub(r"\s+", "", str(flat.get(key) or ""))
        if sheet == "reduction_targets":
            hint = {"감축률(%)": "감축률", "감축량": "목표감축량"}.get(hint, hint)
        if hint in metrics:
            return hint
    text = " ".join(str(flat.get(k) or "") for k in ("항목", "지표종류"))
    text += " " + str(analysis.get("title") or "")
    found = [field for field in metrics if field in text]
    if sheet == "reduction_targets" and "감축량" in text and "목표감축량" not in found:
        found.append("목표감축량")
    return found[0] if len(set(found)) == 1 else None


def resolve_row_target(sheet, item, analysis):
    """Repair only explicitly sector-wide reduction rows, never invent a project.

    This compatibility rule can also consume archived responses. Missing scope,
    year, confidence and source restrictions remain the downstream gate's job.
    """
    if sheet != "quantitative_reductions":
        return sheet, item, ""
    flat, conflicts = flatten_item(item)
    title = re.sub(r"\s+", "", str(analysis.get("title") or ""))
    sector = flat.get("부문") if present(flat.get("부문")) else flat.get("항목")
    unit = re.sub(r"\s+", "", str(flat.get("단위") or analysis.get("unit") or "")).casefold().rstrip(".")
    unit = unit.replace("₂", "2")
    ghg_unit = bool(re.fullmatch(r"(?:천|만|백만)?(?:t|톤)co2(?:eq)?", unit))
    known_sector = isinstance(sector, str) and sector in {
        "건물", "수송", "농축산", "폐기물", "흡수원", "흡수", "전환", "에너지", "산업", "수소", "합계"}
    precise_metric = flat.get("지표종류") or flat.get("값필드")
    conflicting_role = any(present(flat.get(k)) for k in
        ("사업명", "관리번호", "감축사업명", "활동량", "감축원단위값", "목표배출량", "감축률", "배출량"))
    if (conflicts or not known_sector or not ghg_unit or conflicting_role
            or not re.search(r"부문별|부문총괄|부문합계", title) or "감축량" not in title
            or re.search(r"사업별|감축원단위|활동량|실적|잠재량", title)
            or (precise_metric and (not isinstance(precise_metric, str) or precise_metric not in {"예상감축량", "목표감축량", "감축량"}))
            or flat.get("종류") not in (None, "", "목표", "감축량", "예상감축량", "목표감축량")
            or numeric(flat.get("예상감축량", flat.get("값"))) is None):
        return sheet, item, ""
    converted = deepcopy(item)
    fields = converted.setdefault("fields", {})
    if not isinstance(fields, dict):
        return sheet, item, ""
    fields.setdefault("부문", sector)
    # Explicit metric declarations are renamed only after the role is established.
    for container in (converted, fields):
        if container.get("target_sheet") == "quantitative_reductions":
            container["target_sheet"] = "reduction_targets"
        if "예상감축량" in container:
            container["목표감축량"] = container.pop("예상감축량")
        if container.get("지표종류") in {"예상감축량", "감축량"}:
            container["지표종류"] = "목표감축량"
        if container.get("값필드") in {"예상감축량", "감축량"}:
            container["값필드"] = "목표감축량"
    converted["target_sheet"] = "reduction_targets"
    return "reduction_targets", converted, "명시 부문별 감축량·온실가스 단위, 사업 식별자 없음: 부문 목표로 분리"


def convert_chart_row(sheet, item, analysis, municipality):
    flat, reasons = flatten_item(item)
    if sheet not in VISION_BODY_SHEETS:
        return {}, reasons + ["검토 전용/미지원 대상시트"]
    flat = normalize_row(sheet, flat)
    if flat.get("_field_alias_conflicts"):
        reasons.append("공통 필드 별칭 충돌")
    allowed = set(canonical_fields(sheet)) | set(PROVENANCE_FIELDS)
    row = {key: deepcopy(value) for key, value in flat.items() if key in allowed and present(value)}
    row.setdefault("지자체명", municipality)
    label = flat.get("항목")
    if sheet == "regional_conditions":
        row.setdefault("지표명", label)
    elif sheet == "emissions_management":
        row.setdefault("관리부문", label)
    elif sheet in {"emissions_regional", "emissions_forecast", "reduction_targets"}:
        row.setdefault("부문", label)
    elif sheet == "financial_plan":
        # A code is an identifier, never a sector. A known sector can identify a subtotal.
        if isinstance(label, str) and re.fullmatch(r"[A-Z]+\d*(?:-\d+)+", label.strip()):
            row.setdefault("관리번호", label)
        elif isinstance(label, str) and label in {"건물", "수송", "농축산", "폐기물", "흡수원", "전환", "산업", "수소", "합계", "대응기반"}:
            row.setdefault("부문", label)
        elif (isinstance(label, str) and label.strip() and not present(row.get("행경로"))
              and label.strip() != str(row.get("사업명") or "").strip()
              and label.strip() not in {*FINANCE_SOURCES, "계", "총계", "예산", "예산액", "사업비", "총사업비"}
              and numeric(label) is None and not re.fullmatch(r"\d{4}년?", label.strip())):
            # Preserve the explicit child label without guessing a full hierarchy
            # or replacing an explicitly returned parent project name.
            row["행경로"] = [label.strip()]

    for field in ("지자체명", "사업명", "부문", "관리부문", "세부부문", "지표명", "재원구분", "목표범위", "전망범위", "시간기준"):
        if present(row.get(field)) and not isinstance(row[field], str):
            reasons.append("식별/분류 필드 자료형 오류:" + field)

    if sheet in {"financial_plan", "annual_implementation", "quantitative_reductions", "mitigation_projects"}:
        if not row.get("사업명") and not (sheet == "financial_plan" and row.get("부문")):
            reasons.append("사업/부문 귀속 근거 부족")
    if sheet in {"emissions_regional", "emissions_forecast", "reduction_targets"} and not row.get("부문"):
        reasons.append("부문 귀속 근거 부족")
    if sheet == "emissions_management" and not row.get("관리부문"):
        reasons.append("관리부문 귀속 근거 부족")
    if sheet == "regional_conditions" and not row.get("지표명"):
        reasons.append("지표 식별 근거 부족")

    metrics = METRIC_UNITS.get(sheet, {})
    declared = [field for field in metrics if numeric(flat.get(field)) is not None]
    for field in declared:
        row[field] = numeric(flat[field])
    generic = numeric(flat.get("값"))
    metric = _metric_hint(sheet, flat, analysis)
    if not declared and generic is not None:
        if not metric and len(metrics) == 1:
            metric = next(iter(metrics))
        if metric:
            row[metric] = generic
            declared = [metric]
        else:
            reasons.append("수치 지표 종류 미확정")
    elif declared and generic is not None:
        if metric and metric not in declared:
            row[metric] = generic
            declared.append(metric)
        elif generic not in [row[field] for field in declared]:
            reasons.append("일반값/명시 수치 필드 충돌")
    if metrics and not declared:
        reasons.append("유효한 업무 수치 없음")
    unit = flat.get("단위") or analysis.get("unit")
    for field in declared:
        unit_field = metrics[field]
        shared_unit = len(declared) == 1 or field == metric or unit_field == "단위"
        if unit_field != "%" and unit_field not in row and unit and shared_unit:
            row[unit_field] = unit
    if sheet == "reduction_targets":
        # Baseline years are not goal years; do not synthesize either.
        if row.get("목표연도") is None and present(flat.get("연도")):
            if set(declared) == {"기준배출량"}:
                row.setdefault("기준연도", flat["연도"])
            else:
                row["목표연도"] = flat["연도"]
    return row, sorted(set(reasons))

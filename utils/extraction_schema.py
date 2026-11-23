"""Shared extraction fields and lossless aliases; no model/provider settings.

Business rows use canonical Korean keys. Display aliases are applied only at
the workbook boundary. Missing context stays missing; no inferred fund/scope.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

VERSION = "extraction-fields-v1"
REQUIRED_FIELDS = {"document_meta":["지자체명","계획명","계획시작연도","계획종료연도"],"plan_overview":["개요유형","항목명","항목값"],"regional_conditions":["지표명","값"],"emissions_regional":["배출유형","부문","연도","배출량","단위"],"emissions_management":["관리부문","직간접구분","연도","배출량","단위"],"emissions_forecast":["시나리오","연도","전망값","단위"],"reduction_targets":["목표수준","기준연도","목표연도"],"vision_strategy":["전략수준"],"mitigation_projects":["사업명","부문"],"annual_implementation":["사업명"],"quantitative_reductions":["사업명","연도","예상감축량","단위"],"financial_plan":["사업명","연도","예산액","예산단위"],"foundation_measures":["대응기반영역","과제명"],"governance_feedback":["거버넌스기구","역할"],"monitoring_performance":["점검연도","사업명","이행실적"],"changes_actions":["점검연도","사업명"]}
ALTERNATIVE_FIELDS = {"vision_strategy":["전략명","비전문구"],"annual_implementation":["연간계획","목표물량"],"reduction_targets":["목표감축량","목표배출량","감축률(%)"],"changes_actions":["변경사유","지연미달성사유","조치계획"]}
BUSINESS_SHEETS = ["document_meta","plan_overview","regional_conditions","emissions_regional","emissions_management","emissions_forecast","reduction_targets","vision_strategy","mitigation_projects","annual_implementation","quantitative_reductions","financial_plan","foundation_measures","governance_feedback","monitoring_performance","changes_actions"]
SHEET_NAMES = {
    "document_meta": "00_문서메타",
    "plan_overview": "01_계획개요",
    "regional_conditions": "02_지역여건",
    "emissions_regional": "03_배출현황_지역",
    "emissions_management": "04_배출현황_관리권한",
    "emissions_forecast": "05_배출전망",
    "reduction_targets": "06_감축목표",
    "vision_strategy": "07_비전전략",
    "mitigation_projects": "08_감축사업목록",
    "annual_implementation": "09_연차별이행계획",
    "quantitative_reductions": "10_정량감축량",
    "financial_plan": "11_재정투자계획",
    "foundation_measures": "12_대응기반강화",
    "governance_feedback": "13_이행관리환류",
    "monitoring_performance": "14_점검실적",
    "changes_actions": "15_변경과제_조치",
    "visual_inventory": "16_시각자료목록"
}
BASE_DISPLAY_HEADERS = {
    "00_문서메타": [
        "지자체명",
        "지자체유형",
        "계획명",
        "발간일",
        "발간기관",
        "계획시작연도",
        "계획종료연도",
        "기준연도",
        "목표연도",
        "법적근거",
        "점검보고서여부"
    ],
    "01_계획개요": [
        "지자체명",
        "개요유형",
        "항목명",
        "항목값",
        "일자",
        "이해관계자",
        "관련법령_계획"
    ],
    "02_지역여건": [
        "지자체명",
        "지표범주",
        "지표세부범주",
        "지표명",
        "연도",
        "값",
        "단위",
        "출처"
    ],
    "03_배출현황_지역": [
        "지자체명",
        "인벤토리출처",
        "배출범위",
        "배출유형",
        "부문",
        "세부부문",
        "연도",
        "배출량",
        "단위",
        "흡수원여부"
    ],
    "04_배출현황_관리권한": [
        "지자체명",
        "인벤토리출처",
        "관리부문",
        "세부부문",
        "직간접구분",
        "연도",
        "배출량",
        "단위",
        "합계포함여부"
    ],
    "05_배출전망": [
        "지자체명",
        "전망범위",
        "시나리오",
        "전망방법코드",
        "전망방법원문",
        "부문",
        "세부부문",
        "연도",
        "전망값",
        "단위",
        "주요가정"
    ],
    "06_감축목표": [
        "지자체명",
        "목표수준",
        "목표범위",
        "부문",
        "기준연도",
        "기준배출량",
        "목표연도",
        "배출전망",
        "목표감축량",
        "목표배출량",
        "감축률(%)"
    ],
    "07_비전전략": [
        "지자체명",
        "비전문구",
        "전략수준",
        "전략명",
        "부문",
        "설명",
        "키워드"
    ],
    "08_감축사업목록": [
        "지자체명",
        "관리번호",
        "부문",
        "핵심과제",
        "사업명",
        "사업유형",
        "주관부서",
        "협조부서",
        "사업개요",
        "성과지표명",
        "성과지표단위",
        "정량여부"
    ],
    "09_연차별이행계획": [
        "지자체명",
        "관리번호",
        "사업명",
        "기간시작",
        "기간종료",
        "연도",
        "연간계획",
        "목표물량",
        "목표단위",
        "규제혁신계획",
        "입법계획"
    ],
    "10_정량감축량": [
        "지자체명",
        "관리번호",
        "사업명",
        "연도",
        "모니터링인자",
        "활동량",
        "활동단위",
        "감축원단위ID",
        "감축원단위값",
        "예상감축량",
        "단위"
    ],
    "11_재정투자계획": [
        "지자체명",
        "계획구분",
        "부문",
        "사업명",
        "재원구분",
        "연도",
        "예산액",
        "예산단위"
    ],
    "12_대응기반강화": [
        "지자체명",
        "대응기반영역",
        "과제ID",
        "과제명",
        "정책방향",
        "주요내용",
        "대상",
        "주관부서",
        "기간"
    ],
    "13_이행관리환류": [
        "지자체명",
        "거버넌스기구",
        "역할",
        "담당부서",
        "절차단계",
        "기한",
        "산출물"
    ],
    "14_점검실적": [
        "지자체명",
        "점검연도",
        "부문",
        "관리번호",
        "사업명",
        "연간계획",
        "이행실적",
        "소요예산",
        "달성여부",
        "사업유형"
    ],
    "15_변경과제_조치": [
        "지자체명",
        "점검연도",
        "부문",
        "관리번호",
        "사업명",
        "변경전",
        "변경후",
        "변경사유",
        "지연미달성사유",
        "조치계획"
    ],
    "16_시각자료목록": [
        "지자체명",
        "시각자료ID",
        "캡션",
        "유형",
        "데이터포함여부",
        "추출값요약",
        "디지타이징필요",
        "관련시트"
    ]
}

METRIC_UNITS = {
    "reduction_targets": {"기준배출량": "단위", "배출전망": "단위", "목표감축량": "단위", "목표배출량": "단위", "감축률": "%"},
    "emissions_regional": {"배출량": "단위"},
    "emissions_management": {"배출량": "단위"},
    "emissions_forecast": {"전망값": "단위"},
    "quantitative_reductions": {"활동량": "활동단위", "감축원단위값": "감축원단위단위", "예상감축량": "단위"},
    "annual_implementation": {"목표물량": "목표단위"},
    "financial_plan": {"예산액": "예산단위"},
    "regional_conditions": {"값": "단위"},
}
COMMON_AXES = ["지표종류", "계획문맥", "시간기준", "기간시작", "기간종료", "행경로", "열경로", "값출처"]
EXTRA_FIELDS = {sheet: list(COMMON_AXES) for sheet in METRIC_UNITS}
EXTRA_FIELDS["reduction_targets"] += ["단위", "세부부문"]
EXTRA_FIELDS["emissions_forecast"] += ["직간접구분", "상위부문"]
EXTRA_FIELDS["financial_plan"] += ["관리번호"]
EXTRA_FIELDS["quantitative_reductions"] += ["감축량유형", "감축원단위단위"]

DISPLAY_ALIASES = {"reduction_targets": {"감축률(%)": "감축률"}}
INPUT_ALIASES = {
    "reduction_targets": {"감축률(%)": "감축률"},
    "mitigation_projects": {"감축사업명": "사업명", "감축전략_부문": "부문", "성과지표": "성과지표명"},
}
FINANCE_SOURCES = ("합계", "국비", "도비", "시도비", "시비", "군비", "구비", "시군구비", "민간", "기금", "기타", "미표기")
VISION_BODY_SHEETS = (
    "regional_conditions", "emissions_regional", "emissions_management",
    "emissions_forecast", "reduction_targets", "mitigation_projects",
    "annual_implementation", "quantitative_reductions", "financial_plan",
)
VISION_TARGETS = (*VISION_BODY_SHEETS, "vision_strategy")  # strategy is review-only
PROVENANCE_FIELDS = ("출처페이지", "근거ID", "객체ID", "추출원천", "검수상태", "추출엔진", "OCR보완여부", "출처계보")


def headers_for(sheet, headers=None):
    base = list(headers if headers is not None else BASE_DISPLAY_HEADERS[SHEET_NAMES[sheet]])
    return list(dict.fromkeys([*base, *EXTRA_FIELDS.get(sheet, [])]))


def make_excel_headers():
    return {name: headers_for(key, BASE_DISPLAY_HEADERS[name]) if key in BUSINESS_SHEETS else list(BASE_DISPLAY_HEADERS[name])
            for key, name in SHEET_NAMES.items()}


def canonical_fields(sheet):
    return [DISPLAY_ALIASES.get(sheet, {}).get(field, field) for field in headers_for(sheet)]


def present(value):
    return value is not None and value != ""


def baseline_only(row):
    return present(row.get("기준배출량")) and not any(present(row.get(k)) for k in
        ("목표감축량", "목표배출량", "감축률", "감축률(%)", "배출전망", "목표연도"))


def required_for_row(sheet, row):
    fields = list(REQUIRED_FIELDS[sheet])
    if sheet == "reduction_targets" and baseline_only(row):
        return ["부문", "기준연도", "기준배출량", "단위"]
    if sheet == "financial_plan" and present(row.get("부문")) and not re.fullmatch(r"[A-Z]+\d*(?:-\d+)+", str(row["부문"])):
        fields = [f for f in fields if f != "사업명"]
    if sheet == "quantitative_reductions" and not present(row.get("예상감축량")):
        if present(row.get("활동량")):
            return ["사업명", "연도", "활동량", "활동단위"]
        if present(row.get("감축원단위값")):
            return ["사업명", "연도", "감축원단위값", "감축원단위단위"]
    return fields


def alternatives_for_row(sheet, row):
    if sheet == "reduction_targets" and baseline_only(row):
        return ["기준배출량"]
    return ALTERNATIVE_FIELDS.get(sheet)


def _same(left, right):
    if left == right:
        return True
    try:
        return float(str(left).replace(",", "").rstrip("%")) == float(str(right).replace(",", "").rstrip("%"))
    except (ValueError, TypeError):
        return False


def normalize_row(sheet, source):
    """Copy, resolve exact aliases, retain conflicting originals for review."""
    row = deepcopy(source)
    conflicts = list(row.get("_field_alias_conflicts", []))
    for alias, canonical in INPUT_ALIASES.get(sheet, {}).items():
        if not present(row.get(alias)):
            continue
        if not present(row.get(canonical)):
            row[canonical] = row[alias]
        elif not _same(row[canonical], row[alias]):
            record = {"canonical": canonical, "alias": alias, "canonical_value": row[canonical], "alias_value": row[alias]}
            if record not in conflicts:
                conflicts.append(record)
        # Preserve conflicting originals. Nonconflicting aliases are redundant;
        # removing them prevents them from changing strict merge identities.
        if not any(c["alias"] == alias for c in conflicts):
            row.pop(alias, None)
    if sheet == "financial_plan" and present(row.get("재원구분")):
        label = str(row["재원구분"]).strip()
        row["재원구분"] = {"계": "합계", "총계": "합계"}.get(label, label)
    if sheet == "reduction_targets" and isinstance(row.get("감축률"), str) and row["감축률"].strip().endswith("%"):
        try:
            row["감축률"] = float(row["감축률"].strip()[:-1].replace(",", ""))
        except ValueError:
            pass
    if conflicts:
        row["_field_alias_conflicts"] = conflicts
    return row


def export_value(sheet, row, header):
    key = DISPLAY_ALIASES.get(sheet, {}).get(header, header)
    normalized = normalize_row(sheet, row)
    if any(c["canonical"] == key for c in normalized.get("_field_alias_conflicts", [])):
        return None  # retain both originals in the field-contract review ledger
    return normalized.get(key)


def display_field(sheet, canonical):
    return next((header for header, key in DISPLAY_ALIASES.get(sheet, {}).items() if key == canonical), canonical)


def export_row(sheet, row):
    normalized = normalize_row(sheet, row)
    for display, canonical in DISPLAY_ALIASES.get(sheet, {}).items():
        if present(normalized.get(canonical)) or canonical in normalized:
            normalized[display] = export_value(sheet, normalized, display)
    return normalized


_NUMBERS = {"연도", "기준연도", "목표연도", "계획시작연도", "계획종료연도", "점검연도", "기간시작", "기간종료",
            "값", "배출량", "전망값", "기준배출량", "배출전망", "목표감축량", "목표배출량", "감축률",
            "목표물량", "활동량", "감축원단위값", "예상감축량", "예산액", "계획값", "실적값"}
_EXAMPLES = {
    "재원구분": "|".join(FINANCE_SOURCES), "계획구분": "총계|온실가스감축대책|대응기반강화|기후위기적응대책|기타",
    "목표범위": "지역전체|관리권한|관리권한+추가감축", "전망범위": "지역전체|관리권한",
    "배출유형": "직접배출|간접배출|흡수원", "직간접구분": "직접|간접|흡수",
    "시간기준": "연간|기간합계|기준값", "값출처": "원문|계산|미확인",
    "사업유형": "기존|변경|신규|계속|확대|기타", "행경로": ["원문 상위 행", "세부 행"], "열경로": ["2030", "목표배출량"],
    "지자체유형": "광역|기초", "인벤토리출처": "GIR|자체산정|기타",
    "부문": "건물|수송|농축산|폐기물|흡수원|전환|산업|수소|합계|기타",
    "관리부문": "건물|수송|농축산|폐기물|흡수원|전환|산업|수소|합계",
    "지표범주": "자연환경|인문사회|경제산업|에너지", "시나리오": "BAU|정책반영|추가조치",
    "전략수준": "비전|목표|전략|과제", "달성여부": "달성|정상추진|지연|미달성",
    "점검보고서여부": "true|false|null", "흡수원여부": "true|false|null",
    "합계포함여부": "true|false|null", "정량여부": "true|false|null",
}


def field_prompt(sheet):
    example = {field: deepcopy(_EXAMPLES.get(field, None if field in _NUMBERS else "..."))
               for field in canonical_fields(sheet)}
    return (
        "JSON 형식 (null은 확인 불가 값, |는 허용 예시 구분이며 그대로 반환하지 마세요):\n"
        + json.dumps({sheet: [example]}, ensure_ascii=False)
        + "\n데이터가 없으면: " + json.dumps({sheet: []}, ensure_ascii=False)
        + "\n[공통 필드 정의]\n"
        "숫자는 숫자형, 단위는 원문 배율 그대로 별도 필드에 기록하세요. 없는 필드·문맥은 null입니다. "
        "시트 제목을 사업명으로 바꾸거나 구비를 도비로 치환하지 마세요. 사업유형과 정량여부는 별개입니다.\n"
        "여부 필드는 JSON true/false/null, 연도·수량·금액·비율 필드는 숫자 또는 null로 반환하세요.\n"
        "원문에서 확인되지 않는 선택 문맥 필드는 생략하거나 null로 두고, 같은 문맥을 설명 문장으로 행마다 반복하지 마세요. "
        "다만 행을 구별하는 사업·세부항목·범위·연도·재원·단위는 해당 행에서 확인 가능하게 보존하세요.\n"
        + reading_role_rules(sheet) + "\n"
        + ("지표종류는 실제 수치 필드명으로 구분하고, 원문에 있는 연간값·기간합계·기준값을 합치지 마세요. "
           "기간합계/평년값은 연도=null, 기간시작/기간종료에 실제 범위를 기록하세요. "
           "행경로/열경로는 상위 머리글부터 순서대로, 계획문맥은 명시된 문맥만 기록하세요. "
           "값출처는 원문/계산/미확인으로 구분하고 여러 수치의 출처가 다르면 필드별 객체로 기록하세요. "
           "예상감축량을 계산했다면 원문값과 구별하고 계산식·입력 근거를 계획문맥에 남기세요.\n"
           if sheet in METRIC_UNITS else "")
    )


def reading_role_rules(sheet=None):
    rules = {
        "reduction_targets": "부문별·부문 총괄의 연간 온실가스 감축량은 reduction_targets의 목표감축량입니다. "
            "부문·목표연도·단위를 보존하고, 이를 목표배출량이나 사업별 예상감축량으로 바꾸지 마세요. "
            "목표범위·기준연도는 원문에 있을 때만 기록하세요.",
        "quantitative_reductions": "quantitative_reductions는 식별 가능한 개별 사업의 활동량·감축원단위·예상감축량입니다. "
            "부문 합계에 임의 사업명을 만들지 마세요. 부문별 연간 감축량은 reduction_targets에 해당하며 사업별 요청에는 넣지 마세요. "
            "활동량·원단위·감축량의 단위와 원문값/계산값을 구별하세요.",
        "financial_plan": "financial_plan의 사업명은 원문에서 식별되는 가장 구체적인 사업·세부사업명입니다. "
            "전기버스/수소버스처럼 같은 상위 사업 안의 세부항목을 합치지 말고 항목·행경로에 그대로 보존하세요. "
            "상위 사업명·계획 문맥은 행경로/계획문맥으로 보존하고, 국비·시비·구비 등 재원은 세부사업명과 구분하세요. "
            "예산액과 활동물량·감축량, 연간 예산과 기간 합계를 서로 분리하세요.",
    }
    return rules.get(sheet, "") if sheet else "\n".join(rules.values())


def vision_contract():
    targets = "|".join(VISION_TARGETS)
    return (
        f"target_sheet 허용값: {targets}. vision_strategy는 검토 전용입니다.\n"
        "표 행의 target_sheet로 이미지 안의 사업물량/감축량/예산을 각각 분리할 수 있습니다. "
        "fields에는 다음 해당 업무 필드명을 사용하고 원문에서 확인한 값만 보존하세요.\n"
        + "\n".join(f"- {sheet}: " + ", ".join(METRIC_UNITS.get(sheet, {}))
                    for sheet in VISION_BODY_SHEETS if sheet in METRIC_UNITS)
        + "\nfields 공통: 사업명·관리번호·부문·세부부문·지표명·지표종류·목표범위·전망범위·"
          "시나리오·직간접구분·상위부문·계획구분·재원구분·기준연도·목표연도·기간시작·기간종료·"
          "시간기준·행경로·열경로·값출처 및 해당 단위 필드. "
          "재원 예시: " + "|".join(FINANCE_SOURCES)
        + ". 미표기 재원을 합계로 추정하지 마세요. "
          "지표종류는 값이 들어갈 실제 필드명(목표감축량/목표배출량/감축률/활동량/예산액 등)입니다. "
          "목표라는 표현만으로 목표배출량을 추정하지 마세요. 여러 지표는 fields에서 각각 구분하세요. "
          "단위·배율을 그대로 기록하고, 단일 연도가 없는 기간값은 연도=null과 명시된 기간을 사용하세요. "
          "읽을 수 없는 수치·미확정 귀속은 null로 두고 review_reasons에 구체적인 이유를 기록하세요. "
          "표에 없는 사업명·범위·재원·연도를 보충하지 마세요. 확인되지 않은 선택 필드는 생략해도 됩니다.\n"
        + reading_role_rules() + "\n"
          "사진·참고자료와 불확실한 값은 기존 제외·null 규칙을 따르세요."
    )

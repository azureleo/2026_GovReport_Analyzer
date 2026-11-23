"""
에이전트 2-b: 이미지·그래프 분석 에이전트

PDF에서 추출한 이미지(그래프, 차트, 도표)를 선택된 로컬 에이전트/비전 백엔드로 분석하여
텍스트 추출로 놓친 수치 데이터를 보완합니다.
"""

import logging
import base64
import hashlib
import io
import json
import re
import time
from copy import deepcopy

import config
from utils.pdf_reader import PageContent
from utils import llm_client
from utils.extraction_schema import (METRIC_UNITS, VISION_BODY_SHEETS, VISION_TARGETS,
                                     vision_contract)
from utils.vision_rows import convert_chart_row, flatten_item, numeric, resolve_row_target
from utils.vision_trace import image_reference
from PIL import Image, ImageFilter, ImageStat

logger = logging.getLogger(__name__)


IMAGE_SYSTEM = """당신은 한국 지자체 탄소중립 계획 보고서의 그래프와 차트를 분석하는 전문가입니다.

이미지에서 보이는 수치, 연도, 부문, 단위 정보를 최대한 정확하게 읽어내세요.

규칙:
1. 그래프 축의 값을 꼼꼼히 읽어 숫자로 반환하세요.
2. 단위(tCO2eq, 천 tCO2eq 등)를 반드시 기록하세요.
3. 값이 불분명하면 "약 XXX" 형태로 표현하세요.
4. 텍스트가 포함된 표(도표)도 분석하세요.
5. 그래프가 아닌 이미지(사진, 로고 등)는 type을 "해당없음"으로 반환하세요.
6. 반드시 JSON만 반환하세요."""


CHART_TABLE_SYSTEM = """당신은 한국 지자체 탄소중립 계획 보고서의 그래프를 표로 변환하는 전문가입니다.

DePlot 방식처럼 그래프 이미지를 먼저 선형화된 표 데이터로 바꾸세요.
추론이나 보간보다 이미지에 보이는 축, 범례, 라벨, 수치만 우선합니다.

규칙:
1. 막대그래프, 꺾은선그래프, 영역그래프, 원그래프, 데이터 표만 변환하세요.
2. 지도, 사진, 포스터, 목차, 설명용 삽화는 type을 "해당없음"으로 반환하세요.
3. target_sheet와 fields는 사용자 메시지의 공통 업무 필드 정의를 사용하세요.
4. 단위가 "천 tCO2eq", "천톤CO2eq"이면 unit에 그대로 적고 값은 이미지에 보이는 숫자 그대로 반환하세요.
5. 25,432.000처럼 천 단위/소수 표기가 있으면 25432000으로 붙이지 말고 25432 또는 25432.0으로 반환하세요.
6. 수치가 불분명하면 null로 두고 confidence를 낮추세요.
7. 막대/선의 값이 축 눈금만으로 추정된 값이면 fields에 {"estimated": true}를 넣고 confidence는 medium 이하로 두세요.
8. 반드시 JSON만 반환하세요.

시트별 fields 예시:
- reduction_targets: {"부문": "건물", "목표연도": 2030, "목표감축량": 123, "단위": "천톤CO2eq"}
- financial_plan: {"사업명": "원문 사업명", "부문": "건물", "재원구분": "구비", "예산액": 123, "예산단위": "백만원"}
"""


def _is_relevant_image(image: dict) -> bool:
    w, h = image.get("width", 0), image.get("height", 0)
    if w > 0 and h > 0 and (w / h > 5 or h / w > 5):
        return False
    return w >= 200 and h >= 150


def _index_only_page(page: PageContent) -> bool:
    """Exclude confirmed index render proxies, not mixed pages with real tables."""
    lines = [line.strip() for line in (page.text or "").splitlines() if line.strip()]
    heading = any(re.fullmatch(r"(?:표|그림)?\s*목\s*차", line) for line in lines)
    entries = sum(bool(re.search(r"(?:표|그림).*?(?:\.{2,}|…|·{2,})\s*\d+\s*$", line)) for line in lines)
    embedded = any("full render" not in str(image.get("caption", "")).lower() for image in page.images)
    return heading and entries >= 5 and not page.tables and not embedded


def _coverage_reduce_images(raw_images: list[tuple[PageContent, dict]]) -> list[tuple[PageContent, dict]]:
    """
    전수 분석을 유지하면서 중복 호출을 줄인다.

    같은 페이지에 전체 렌더 이미지가 있으면 그 페이지의 embedded 조각 이미지는 전체 렌더에
    포함되므로 전체 렌더만 분석한다. 전체 렌더가 없는 페이지는 이미지 해시 기준 중복만 제거한다.
    """
    by_page: dict[int, list[tuple[PageContent, dict]]] = {}
    for page, image in raw_images:
        by_page.setdefault(page.page_number, []).append((page, image))

    reduced: list[tuple[PageContent, dict]] = []
    seen_hashes: set[str] = set()
    for page_num in sorted(by_page):
        items = by_page[page_num]
        full_renders = [
            item for item in items
            if "full render" in str(item[1].get("caption", "")).lower()
        ]
        candidates = full_renders or items
        for page, image in candidates:
            digest = hashlib.sha1(str(image.get("base64", "")).encode("utf-8")).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            reduced.append((page, image))
    return reduced


_CHART_CONTEXT_KEYWORDS = [
    "그림", "그래프", "차트", "도표", "표", "추이", "전망", "현황", "목표",
    "배출량", "온실가스", "감축", "BAU", "NDC", "tCO2", "CO2eq", "비율", "%",
]

_NEGATIVE_CONTEXT_KEYWORDS = [
    "목차", "표 목차", "그림 목차", "사진", "행사", "공모전", "모집", "설문", "자문회의",
]

_LOCAL_DIRECT_DATA_KEYWORDS = [
    "배출량", "배출 현황", "배출 전망", "감축목표", "감축사업", "추진계획",
    "최종에너지", "에너지 소비", "자동차 등록", "주행거리",
]


def _visual_title_from_text(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if re.match(r"^\[(그림|표)\s*[\d\-]+", stripped):
            return stripped
    for line in (text or "").splitlines():
        stripped = line.strip()
        if any(token in stripped for token in ("사업개요", "추진계획", "배출 현황", "감축목표")):
            return stripped[:80]
    return "이미지/표 후보"


def _target_sheet_from_visual_text(text: str, title: str) -> str:
    combined = f"{title}\n{text}"
    if any(k in combined for k in ["자동차", "차량", "등록대수", "주행거리"]):
        return "regional_conditions"
    if any(k in combined for k in ["연료", "에너지", "전력", "도시가스", "석유", "철도", "통행량"]):
        return "regional_conditions"
    if any(k in combined for k in ["감축사업", "사업개요", "성과지표", "지원금", "예산", "보일러", "LED"]):
        return "mitigation_projects"
    if any(k in combined for k in ["전망", "BAU", "예측"]):
        return "emissions_forecast"
    if any(k in combined for k in ["감축목표", "목표배출량", "감축률"]):
        return "reduction_targets"
    if any(k in combined for k in ["재정", "투자", "예산계획"]):
        return "financial_plan"
    return "emissions_regional"


def _compact_page_context(page: PageContent, limit: int = 900) -> str:
    text = re.sub(r"\s+", " ", page.text or "").strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _fallback_visual_observations(page: PageContent, municipality: str) -> list[dict]:
    """
    Vision/로컬 비전 에이전트가 표·그래프 이미지를 '해당없음'으로 돌려보내는 경우의 안전망.

    이미지가 있는 페이지의 주변 텍스트/표에서 명시적으로 보이는 수치만 '검토' 후보로 남긴다.
    본 시트에 자동 병합하지 않고 이미지·그래프 판독결과 시트에 감사 가능한 관찰값으로 보존한다.
    """
    text = page.text or ""
    title = _visual_title_from_text(text)
    target_sheet = _target_sheet_from_visual_text(text, title)
    observations: list[dict] = []

    # 연도별 변화율 표: 헤더 연도와 수치 행이 모두 텍스트로 잡힌 경우.
    table_match = re.search(r"연도별\s*통행량\s*변화율(?P<body>.*?)(?:※|\n\s*부문의|\Z)", text, re.S)
    if table_match:
        body = table_match.group("body")
        years = [int(y) for y in re.findall(r"(20\d{2})(?=\s*[~년])", body)]
        value_match = re.search(r"철도\s+([0-9.%\-\s]+)", body)
        if years and value_match:
            values = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", value_match.group(1))]
            for year, value in zip(years, values):
                observations.append({
                    "지자체명": municipality,
                    "페이지": page.page_number,
                    "대상시트": target_sheet,
                    "그래프유형": "표",
                    "제목": title,
                    "단위": "%",
                    "항목": "철도",
                    "연도": year,
                    "값": value,
                    "신뢰도": "medium",
                    "반영여부": "검토",
                    "근거": "이미지 분석 fallback: 페이지 렌더/표 주변 텍스트에서 연도별 통행량 변화율 행을 판독",
                })

    # 사업개요형 이미지/도표 주변에 보이는 정량 지표.
    boiler_metrics = [
        ("지원금", r"보조금\s*([0-9.]+)\s*만원", "만원"),
        ("친환경보일러 NOx 배출농도", r"NOX?\s*배출농도\s*([0-9.]+)\s*ppm", "ppm"),
        ("노후 일반보일러 NOx 배출농도", r"노후\s*일반보일러\(?([0-9.]+)\s*ppm", "ppm"),
        ("온실가스 감축량", r"온실가스\s*감축\(?([0-9.]+)\s*만\s*tCO2", "만 tCO2/년"),
        ("대당 감축량", r"대당\s*([0-9.]+)\s*kg/년", "kgCO2/년"),
    ]
    if "친환경" in text and "보일러" in text:
        for item, pattern, unit in boiler_metrics:
            match = re.search(pattern, text, re.I)
            if match:
                observations.append({
                    "지자체명": municipality,
                    "페이지": page.page_number,
                    "대상시트": target_sheet,
                    "그래프유형": "표/이미지",
                    "제목": title,
                    "단위": unit,
                    "항목": item,
                    "연도": None,
                    "값": float(match.group(1)),
                    "신뢰도": "medium",
                    "반영여부": "검토",
                    "근거": "이미지 분석 fallback: 사업 이미지 주변 정량 설명에서 직접 판독",
                })

    return observations


def _decode_image(image: dict) -> Image.Image | None:
    try:
        raw = base64.b64decode(image.get("base64", ""))
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


def _image_feature_score(image: dict) -> tuple[int, list[str]]:
    """
    ChartQA식 triage의 로컬 근사.

    ChartQA가 강조하는 차트의 핵심 단서(축/격자/범례/데이터 마크/텍스트-숫자 구조)를
    비용 없는 이미지 특징으로 근사한다. 이 단계는 정답 추출이 아니라 Vision 분석 대상
    선별만 담당한다.
    """
    pil = _decode_image(image)
    if pil is None:
        return 0, ["decode_failed"]

    score = 0
    reasons: list[str] = []
    w, h = pil.size
    area = w * h
    if area >= 120_000:
        score += 1
        reasons.append("large")

    small = pil.copy()
    small.thumbnail((256, 256))
    gray = small.convert("L")

    stat = ImageStat.Stat(small)
    color_std = sum(stat.stddev) / max(len(stat.stddev), 1)
    gray_stat = ImageStat.Stat(gray)
    gray_std = gray_stat.stddev[0] if gray_stat.stddev else 0

    hist = gray.histogram()
    total = sum(hist) or 1
    white_ratio = sum(hist[235:]) / total
    dark_ratio = sum(hist[:65]) / total

    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_hist = edges.histogram()
    edge_total = sum(edge_hist) or 1
    edge_ratio = sum(edge_hist[80:]) / edge_total

    # 차트/표는 대개 흰 배경 + 선/글자/격자 조합을 가진다.
    if white_ratio >= 0.35:
        score += 2
        reasons.append("white_background")
    if 0.02 <= dark_ratio <= 0.45:
        score += 1
        reasons.append("text_or_axis_pixels")
    if edge_ratio >= 0.025:
        score += 2
        reasons.append("line_grid_density")
    if gray_std >= 25:
        score += 1
        reasons.append("contrast")

    # 사진/삽화는 색 분산이 크고 흰 배경 비율이 낮은 경우가 많다.
    if color_std >= 70 and white_ratio < 0.25:
        score -= 3
        reasons.append("photo_like_color_variance")

    caption = image.get("caption", "")
    if "full render" in caption.lower():
        score += 2
        reasons.append("page_render")

    return score, reasons


def _context_score(page: PageContent) -> tuple[int, list[str]]:
    text = page.text or ""
    score = 0
    reasons: list[str] = []
    hits = [kw for kw in _CHART_CONTEXT_KEYWORDS if kw in text]
    if hits:
        score += min(len(hits), 5)
        reasons.append("context:" + ",".join(hits[:4]))

    negatives = [kw for kw in _NEGATIVE_CONTEXT_KEYWORDS if kw in text]
    if negatives:
        score -= min(len(negatives) * 2, 6)
        reasons.append("negative_context:" + ",".join(negatives[:3]))

    if page.tables:
        score += 2
        reasons.append("page_has_tables")
    return score, reasons


def _has_reference_context(page: PageContent, image: dict, municipality: str) -> tuple[bool, list[str]]:
    """
    보고서 작성 지자체의 직접 데이터가 아니라 참고자료/해외사례/목차성 페이지인지 판별한다.

    이 단계는 Vision 호출 전 비용 절감용 필터다. 지자체명과 직접 데이터 키워드가 함께 있으면
    참고 키워드가 일부 있어도 보존한다.
    """
    text = " ".join([page.text or "", image.get("caption", "") or ""])
    lowered = text.casefold()
    hits = [
        keyword
        for keyword in config.IMAGE_CHART_REFERENCE_KEYWORDS
        if keyword.casefold() in lowered
    ]
    if not hits:
        return False, []

    local_names = {municipality, municipality.replace("특별시", ""), municipality.replace("광역시", "")}
    has_local_name = any(name and name in text for name in local_names)
    has_direct_data = any(keyword in text for keyword in _LOCAL_DIRECT_DATA_KEYWORDS)

    if has_local_name and has_direct_data:
        return False, hits
    return True, hits


def _triage_image(page: PageContent, image: dict) -> dict:
    image_score, image_reasons = _image_feature_score(image)
    context_score, context_reasons = _context_score(page)
    score = image_score + context_score

    caption = image.get("caption", "")
    keep_rendered = (
        config.IMAGE_TRIAGE_KEEP_RENDERED_CONTEXT
        and "full render" in caption.lower()
        and context_score >= 3
    )
    passed = score >= config.IMAGE_TRIAGE_MIN_SCORE or keep_rendered

    return {
        "page": page,
        "image": image,
        "score": score,
        "passed": passed,
        "reasons": image_reasons + context_reasons,
    }


def _parse_chart_value(value, unit: str | None = None) -> float | None:
    """차트 표 변환 결과의 값을 숫자로 정규화."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    else:
        text = str(value).strip()
        if not text or text.lower() in {"null", "none", "nan", "-"}:
            return None
        text = text.replace("약", "").replace(",", "").replace(" ", "")
        try:
            numeric = float(text)
        except ValueError:
            return None

    # LLM이 25,432.000 같은 표기를 25432000으로 붙여 반환하는 경우가 있어
    # 차트 단위가 천톤 계열이면 Organizer에서 한 번 더 스케일 검증하도록 원값을 유지한다.
    return numeric


def _normalize_chart_year(year) -> str | None:
    """차트 표 변환 결과의 연도를 config.YEARS 문자열 키로 정규화."""
    try:
        year_int = int(float(str(year).strip()))
    except (ValueError, TypeError):
        return None
    if year_int not in config.YEARS:
        return None
    return str(year_int)


def _chart_year_int(year) -> int | None:
    value = numeric(year)
    return int(value) if value is not None and value.is_integer() else None


def _is_reference_chart(analysis: dict) -> bool:
    """해외사례/참고자료성 이미지는 본 데이터 자동 반영에서 제외."""
    text = " ".join(
        str(analysis.get(k, "") or "")
        for k in ["title", "summary", "unit", "chart_type"]
    ).casefold()
    return any(keyword.casefold() in text for keyword in config.IMAGE_CHART_REFERENCE_KEYWORDS)


def _is_table_like_chart(analysis: dict) -> bool:
    return str(analysis.get("chart_type", "")).strip() == "표"


def _has_chart_value(item: dict) -> bool:
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    if numeric(item.get("값")) is not None:
        return True
    if any(numeric(item.get(k, fields.get(k))) is not None
           for metrics in METRIC_UNITS.values() for k in metrics):
        return True
    return any(
        fields.get(k) is not None
        for k in [
            "대수", "주행거리", "석유_에너지유", "석유_LPG",
            "석유_비에너지유", "가스", "전력", "열", "신재생",
        ]
    )


def _infer_chart_kind(item: dict, analysis: dict) -> str:
    """차트 행의 종류(현황/전망/목표)를 제목·요약·행 텍스트에서 보강 추론."""
    kind = item.get("종류")
    if kind in ("현황", "전망", "목표"):
        return kind
    text = " ".join(
        str(v or "") for v in [
            item.get("항목"),
            analysis.get("title"),
            analysis.get("summary"),
        ]
    )
    if any(token in text for token in ("목표", "감축목표", "NDC")):
        return "목표"
    if any(token in text for token in ("전망", "BAU", "추정")):
        return "전망"
    return "현황"


def _declared_scenario(item: dict, analysis: dict) -> str:
    """Do not apply a mixed chart's BAU label to every series."""
    if item.get("시나리오"):
        return item["시나리오"]
    title = str(analysis.get("title", ""))
    bau = bool(re.search(r"\bBAU\b", title, re.I))
    policy = any(token in title for token in ("정책반영", "정책 반영", "정책적용", "정책 적용"))
    if bau != policy:
        return "BAU" if bau else "정책반영"
    return ""


class ImageAgent:
    """에이전트 2-b: 이미지·그래프 분석 에이전트"""

    def __init__(self, *, trace=None, attempt=1):
        self._image_results: list[dict] = []
        self._triage_stats: dict = {}
        self._batch_input_reviews: list[dict] = []
        self.trace = trace
        self.attempt = attempt

    def _vision_response(self, batch, prompt, system, mode):
        """Persist the request and literal client result before any JSON parsing."""
        call_id = self.trace.begin(prompt=prompt, system=system, attempt=self.attempt, mode=mode,
            images=[image_reference(page, image, idx) for idx, (page, image) in enumerate(batch, 1)]) if self.trace else None
        try:
            images = [image["base64"] for _, image in batch]
            response = (llm_client.call_vision(images[0], prompt, system=system) if len(images) == 1 else
                        llm_client.call_vision_batch(images, prompt, system=system))
        except BaseException as error:
            if self.trace:
                self.trace.finish(call_id, "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else
                                  "transport_error", error=error)
            raise
        if self.trace:
            self.trace.response(call_id, response)
        return response, call_id

    def _finish_vision(self, call_id, status, results=(), diagnostics=()):
        self._batch_input_reviews.extend(diagnostics)
        if self.trace:
            self.trace.finish(call_id, status, analyses=results, diagnostics=diagnostics)
        for result in results:
            if call_id:
                result["_vision_call_id"] = call_id
                self._attach_image_reference(call_id, result)
        return results

    def _attach_image_reference(self, call_id, result):
        images = self.trace.calls[call_id]["request"]["images"]
        reference = next((r for r in images if r["image_index"] == result.get("image_index", 1)), {})
        result["_vision_input_reference"] = deepcopy(reference)
        for key in ("object_id", "evidence_id"):
            if reference.get(key) and not result.get(key):
                result[key] = reference[key]

    @staticmethod
    def _invalid_response_status(response):
        if not isinstance(response, str) or not response.strip():
            return "empty_response"
        # parse_json also accepts fenced JSON; {} may be the client's failure sentinel.
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip(), flags=re.I)
        try:
            json.loads(cleaned)
        except (TypeError, ValueError):
            return "invalid_json"
        return "invalid_shape"

    def _analyze_image(self, image: dict, page_num: int, municipality: str) -> dict | None:
        if config.IMAGE_CHART_TABLE_EXTRACTION:
            return self._chart_to_table(image, page_num, municipality)

        prompt = f"""이 이미지는 '{municipality}' 탄소중립 기본계획 보고서 {page_num}페이지에서 추출되었습니다.

이미지를 분석하여 다음 JSON 형식으로 반환하세요:
{{
  "type": "그래프유형(막대/꺾은선/파이/표/기타/해당없음)",
  "title": "그래프/표 제목",
  "unit": "단위 (예: tCO2eq, 천tCO2eq, %)",
  "description": "이미지 내용 요약 (1~3문장)",
  "ghg_data": [
    {{
      "label": "부문 또는 계열명",
      "data_type": "현황|전망|목표",
      "전망범위": "지역전체|관리권한|null",
      "시나리오": "BAU|정책반영|추가조치|null",
      "배출유형": "직접배출|간접배출|흡수원|null",
      "직간접구분": "직접|간접|흡수|직접+간접|null",
      "세부부문": "원문에 명시된 세부부문 또는 null",
      "values": {{"2018": 숫자, "2019": 숫자, ..., "2030": 숫자}}
    }}
  ],
  "other_data": {{}}
}}"""

        resp, call_id = self._vision_response([(page_num, image)], prompt, IMAGE_SYSTEM, "legacy_single")
        parsed = llm_client.parse_json(resp or "")
        # list나 빈 값이 반환되면 건너뜀
        if not parsed or not isinstance(parsed, dict):
            self._finish_vision(call_id, self._invalid_response_status(resp), diagnostics=[
                {"code": "invalid_single_response", "page_number": page_num,
                 "message": "빈/비정상 응답; {}는 CLI 실패 대체값일 수 있어 실행 로그 확인 필요"}])
            return None
        if parsed.get("type") == "해당없음":
            self._finish_vision(call_id, "no_data")
            return None
        parsed["page_number"] = page_num
        parsed["municipality"] = municipality
        self._finish_vision(call_id, "complete", [parsed])
        return parsed

    def _chart_to_table(self, image: dict, page_num: int, municipality: str, *, page_context="") -> dict | None:
        prompt = f"""이 이미지는 '{municipality}' 탄소중립 기본계획 보고서 {page_num}페이지에서 추출되었습니다.

이미지가 그래프/차트/표라면 DePlot 방식으로 다음 JSON 형식의 표 데이터로 변환하세요:
{{
  "type": "chart_table|해당없음",
  "target_sheet": "{'|'.join(VISION_TARGETS)}",
  "chart_type": "막대|꺾은선|영역|원|표|복합|기타",
  "title": "그래프/표 제목",
  "unit": "단위 원문",
  "page_number": {page_num},
  "table": [
    {{
      "target_sheet": null,
      "연도": 2030,
      "항목": "계열명 또는 부문명",
      "종류": "현황|전망|목표|기타",
      "값": 12345,
      "단위": "단위 원문",
      "fields": {{"시트별 추가 필드": "값"}}
    }}
  ],
  "summary": "이미지 내용 요약 1문장",
  "confidence": "high|medium|low"
}}

온실가스 현황/전망/목표는 emissions_regional 또는 emissions_management / emissions_forecast / reduction_targets로 구분하고, 항목에는 부문명을 넣으세요.
전망 fields에는 전망범위·시나리오·세부부문을 원문대로 보존하세요. 혼합 BAU·정책반영 제목으로 개별 행을 BAU로 고정하지 마세요. 미확정 값은 null로 두세요.
현황 fields에는 원문에 명시된 배출유형(03) 또는 직간접구분·관리부문(04)을 보존하세요.
감축사업 표는 mitigation_projects, 예산 표는 financial_plan으로 구분하고 fields에 사업명과 부문을 넣으세요.
차량 등록대수/주행거리 및 에너지 소비량 표는 regional_conditions로 두세요.
이미지에 숫자축만 있고 정확한 값을 읽기 어려우면 대략값을 만들지 말고 null로 반환하세요."""

        prompt += "\n\n" + vision_contract()
        if page_context:
            prompt += "\n[같은 페이지의 보조 문맥; 이미지 수치와 다르면 추정하지 말고 검토 요청]\n" + page_context
        resp, call_id = self._vision_response([(page_num, image)], prompt, CHART_TABLE_SYSTEM, "chart_single")
        parsed = llm_client.parse_json(resp or "")
        if not parsed or not isinstance(parsed, dict):
            self._finish_vision(call_id, self._invalid_response_status(resp), diagnostics=[
                {"code": "invalid_single_response", "page_number": page_num,
                 "message": "빈/비정상 응답; {}는 CLI 실패 대체값일 수 있어 실행 로그 확인 필요"}])
            return None
        if parsed.get("type") == "해당없음":
            self._finish_vision(call_id, "no_data")
            return None
        table = parsed.get("table", [])
        if not isinstance(table, list) or not table:
            self._finish_vision(call_id, "invalid_shape", diagnostics=[
                {"code": "invalid_chart_table", "page_number": page_num, "message": "비어 있거나 잘못된 table"}])
            return None
        parsed["type"] = "chart_table"
        parsed["page_number"] = page_num
        parsed["municipality"] = municipality
        self._finish_vision(call_id, "complete", [parsed])
        return parsed

    def _chart_to_table_batch(
        self,
        batch: list[tuple[PageContent, dict]],
        municipality: str,
    ) -> list[dict]:
        if not batch:
            return []
        if len(batch) == 1:
            page, image = batch[0]
            result = self._chart_to_table(image, page.page_number, municipality, page_context=_compact_page_context(page))
            return [result] if result else []

        image_lines = []
        for idx, (page, _image) in enumerate(batch, start=1):
            image_lines.append(
                f"- image_index={idx}, page_number={page.page_number}, "
                f"page_context={_compact_page_context(page)}"
            )

        prompt = f"""아래 첨부 이미지는 '{municipality}' 탄소중립 기본계획 보고서에서 추출한 서로 다른 페이지 렌더/이미지입니다.
각 첨부 이미지를 빠짐없이 순서대로 확인하고, 그래프/차트/표로 볼 수 있는 이미지만 표 데이터로 변환하세요.
관련 없는 사진/홍보물/장식 이미지는 해당 image_index에 대해 type을 "해당없음"으로 반환하세요.

[첨부 이미지 매핑]
{chr(10).join(image_lines)}

반환 JSON 형식:
{{
  "analyses": [
    {{
      "image_index": 1,
      "page_number": 123,
      "type": "chart_table|해당없음",
      "target_sheet": "{'|'.join(VISION_TARGETS)}",
      "chart_type": "막대|꺾은선|영역|원|표|복합|기타",
      "title": "그래프/표 제목",
      "unit": "단위 원문",
      "table": [
        {{
          "target_sheet": null,
          "연도": 2030,
          "항목": "계열명 또는 부문명",
          "종류": "현황|전망|목표|기타",
          "값": 12345,
          "단위": "단위 원문",
          "fields": {{"시트별 추가 필드": "값"}}
        }}
      ],
      "summary": "이미지 내용 요약 1문장",
      "confidence": "high|medium|low"
    }}
  ]
}}

규칙:
- analyses에는 반드시 모든 image_index를 한 번씩 포함하세요.
- 전망 행의 fields에는 전망범위·시나리오·세부부문을 원문대로 구분하세요. 여러 시나리오가 있는 제목만으로 개별 행을 BAU로 고정하지 마세요. 확인되지 않은 값은 null로 두세요.
- 현황 행의 배출유형(03) 또는 직간접구분·관리부문(04)은 원문에 명시된 경우 fields에 보존하세요. 목표 행은 전망과 분리하세요.
- 이미지에 숫자축만 있고 정확한 값을 읽기 어려우면 값을 추정하지 말고 null로 반환하세요.
- 표/그래프가 아니거나 지자체 직접 데이터가 아니면 type은 "해당없음"으로 두세요.
- 반드시 JSON만 반환하세요."""

        prompt += "\n\n" + vision_contract()
        response, call_id = self._vision_response([(page.page_number, image) for page, image in batch],
                                                 prompt, CHART_TABLE_SYSTEM, "chart_batch")
        parsed = llm_client.parse_json(response or "")
        raw_analyses = parsed.get("analyses") if isinstance(parsed, dict) else parsed
        if not isinstance(raw_analyses, list):
            self._finish_vision(call_id, self._invalid_response_status(response), diagnostics=[
                {"code": "invalid_batch_response", "message": "analyses must be a list"}])
            return []

        results: list[dict] = []
        review_start = len(self._batch_input_reviews)
        page_by_index = {idx: page.page_number for idx, (page, _) in enumerate(batch, start=1)}
        indexed = {}
        for raw in raw_analyses:
            if not isinstance(raw, dict):
                self._batch_input_reviews.append({"code": "invalid_analysis", "value": raw})
                continue
            try:
                image_index = int(raw.get("image_index"))
            except (TypeError, ValueError):
                image_index = 0
            if image_index not in page_by_index:
                self._batch_input_reviews.append({"code": "invalid_image_index", "image_index": image_index})
                continue
            indexed.setdefault(image_index, []).append(raw)
        for missing in sorted(set(page_by_index) - set(indexed)):
            self._batch_input_reviews.append({"code": "missing_image_result", "image_index": missing})
        for image_index, records in indexed.items():
            if len(records) != 1:
                self._batch_input_reviews.append({"code": "duplicate_image_result", "image_index": image_index, "records": records})
                continue
            raw = records[0]
            if raw.get("type") == "해당없음":
                continue
            table = raw.get("table", [])
            if not isinstance(table, list):
                self._batch_input_reviews.append({"code": "invalid_chart_table", "image_index": image_index})
                continue
            if not table:
                self._batch_input_reviews.append({"code": "empty_chart_table", "image_index": image_index})
                continue
            page_number = page_by_index[image_index]
            raw["type"] = "chart_table"
            raw["page_number"] = page_number
            raw["municipality"] = municipality
            results.append(raw)
        diagnostics = self._batch_input_reviews[review_start:]
        # Diagnostics are already in the batch journal; do not append them twice.
        if self.trace:
            self.trace.finish(call_id, "partial" if diagnostics else "complete" if results else "no_data",
                              analyses=results, diagnostics=diagnostics)
        for result in results:
            if call_id:
                result["_vision_call_id"] = call_id
                self._attach_image_reference(call_id, result)
        return results

    def _infer_target_sheet(self, analysis: dict) -> str:
        target = analysis.get("target_sheet")
        if target is not None and not isinstance(target, str):
            return ""  # invalid identifiers must not become a fallback body sheet
        # 새 시트 키 매핑
        _LEGACY_MAP = {
            "vehicle": "regional_conditions",
            "energy": "regional_conditions",
            "ghg": "emissions_regional",
            "strategy": "mitigation_projects",
            "summary": "vision_strategy",
        }
        _VALID_KEYS = set(VISION_TARGETS)
        if target in _VALID_KEYS:
            return target
        if target in _LEGACY_MAP:
            return _LEGACY_MAP[target]
        if target:
            return target  # explicitly unsupported targets remain reviewable
        text = " ".join(str(analysis.get(k, "")) for k in ["title", "summary", "unit"])
        if any(k in text for k in ["자동차", "차량", "등록대수", "주행거리"]):
            return "regional_conditions"
        if any(k in text for k in ["에너지", "전력", "도시가스", "석유", "신재생", "TJ", "toe"]):
            return "regional_conditions"
        if any(k in text for k in ["감축사업", "성과지표", "이행", "계획(감축량)"]):
            return "mitigation_projects"
        if any(k in text for k in ["예산", "재정", "투자"]):
            return "financial_plan"
        if any(k in text for k in ["전망", "BAU"]):
            return "emissions_forecast"
        if any(k in text for k in ["감축목표", "목표배출량"]):
            return "reduction_targets"
        return "emissions_regional"

    def _confidence_rank(self, confidence: str | None) -> int:
        return {"low": 1, "medium": 2, "high": 3}.get(str(confidence or "").lower(), 1)

    def _chart_merge_decision(self, analysis: dict, item: dict, target_sheet: str) -> tuple[bool, str, list[str]]:
        """
        그래프 판독값의 본 시트 자동 반영 여부를 보수적으로 결정한다.

        LLM이 confidence를 과하게 high로 주는 경우가 있어, 프로젝트 범위/참고자료/표 여부를
        코드에서 한 번 더 확인한다.
        """
        reasons: list[str] = []
        min_conf = getattr(config, "IMAGE_CHART_MERGE_MIN_CONFIDENCE", "medium")
        confidence = analysis.get("confidence", "low")
        if self._confidence_rank(confidence) < self._confidence_rank(min_conf):
            reasons.append("신뢰도 기준 미달")

        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        if fields.get("estimated") is True:
            reasons.append("축 기반 추정값")
        if target_sheet == "summary":
            reasons.append("요약/참고성 자료")
        if _is_reference_chart(analysis):
            reasons.append("참고자료/해외사례")
        if not _has_chart_value(item):
            reasons.append("값 없음")

        year_int = _chart_year_int(item.get("연도"))
        year_int = year_int or _chart_year_int(item.get("목표연도")) or _chart_year_int(item.get("기준연도"))
        start, end = _chart_year_int(item.get("기간시작")), _chart_year_int(item.get("기간종료"))
        period = start is not None and end is not None and 1 <= start <= end <= 9999
        if (start is not None or end is not None) and not period:
            reasons.append("기간 범위 불명확")
        if item.get("시간기준") in ("기간합계", "평년값") and year_int is not None:
            reasons.append("기간값/단일 연도 혼용")
        if year_int is None and not period:
            reasons.append("연도 없음/비숫자")
        explicit_baseline = (target_sheet == "reduction_targets" and item.get("기준배출량") is not None
                             and year_int == _chart_year_int(item.get("기준연도")))
        if year_int is not None and year_int not in config.IMAGE_CHART_MERGE_YEARS and not explicit_baseline:
            reasons.append("연도 범위 외")

        chart_type = str(analysis.get("chart_type", "") or "")
        if chart_type in {"꺾은선", "막대", "영역", "복합"} and not _is_table_like_chart(analysis):
            # 그래프 축에서 읽은 값은 감사 시트에는 남기되, 본 데이터 자동 병합은 표보다 훨씬 보수적으로 한다.
            reasons.append("그래프 추정 후보")

        can_merge = not reasons
        if can_merge:
            return True, "high", []
        if _has_chart_value(item) and not any(r in reasons for r in ["참고자료/해외사례", "연도 범위 외"]):
            return False, "medium", reasons
        return False, "low", reasons

    def _can_merge_chart(self, analysis: dict, item: dict, target_sheet: str) -> bool:
        can_merge, _, _ = self._chart_merge_decision(analysis, item, target_sheet)
        return can_merge

    def _append_chart_observation(
        self,
        text_results: dict,
        analysis: dict,
        item: dict,
        target_sheet: str,
        merged: bool,
        confidence: str | None = None,
        reasons: list[str] | None = None,
    ):
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        reason_text = "; ".join(reasons or [])
        base_evidence = json.dumps(fields, ensure_ascii=False) if fields else analysis.get("summary", "")
        evidence_text = f"{base_evidence} | {reason_text}" if reason_text else base_evidence
        evidence = {
            "지자체명": analysis.get("municipality", ""),
            "페이지": analysis.get("page_number"),
            "대상시트": target_sheet,
            "그래프유형": analysis.get("chart_type", ""),
            "제목": analysis.get("title", ""),
            "단위": item.get("단위") or analysis.get("unit", ""),
            "항목": item.get("항목") or fields.get("용도") or fields.get("감축사업명") or "",
            "연도": item.get("연도"),
            "값": item.get("값"),
            "신뢰도": confidence or analysis.get("confidence", "low"),
            "반영여부": "반영" if merged else "검토",
            "근거": evidence_text,
        }
        text_results.setdefault("chart_observations", []).append(evidence)

    def _merge_image_results(self, text_results: dict, analyses: list[dict], municipality: str) -> dict:
        from utils.evidence_merge import merge_extraction_rows, _lineage
        for key in config.EXTRACTION_SHEETS:
            text_results.setdefault(key, [])
        raw_analyses = text_results.setdefault("vision_analyses", [])
        def fingerprint_of(value):
            content = {k: v for k, v in value.items() if not k.startswith("_vision_")}
            return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        known = {fingerprint_of(a)
                 for a in raw_analyses}
        for analysis in analyses:
            fingerprint = fingerprint_of(analysis)
            if fingerprint not in known:
                raw_analyses.append(deepcopy(analysis))
                known.add(fingerprint)
            chart_target = self._infer_target_sheet(analysis)
            before_counts = {key: len(text_results.get(key, [])) for key in config.EXTRACTION_SHEETS}
            items = list(analysis.get("table", [])) if analysis.get("type") == "chart_table" else []
            # Legacy series use the same semantic converter and conservative gate.
            for series in analysis.get("ghg_data", []):
                if not isinstance(series, dict) or not isinstance(series.get("values"), dict):
                    continue
                for year, value in series["values"].items():
                    fields = {k: deepcopy(v) for k, v in series.items() if k not in {"values", "label", "data_type"}}
                    items.append({"연도": year, "값": value, "항목": series.get("label"),
                                  "종류": series.get("data_type"), "fields": fields})
            for item_index, item in enumerate(items, 1):
                raw_item = deepcopy(item)
                malformed_item = not isinstance(item, dict)
                if not isinstance(item, dict):
                    item = {}
                flat, input_reasons = flatten_item(item)
                if malformed_item:
                    input_reasons.append("표 행 자료형 오류: 객체가 아님")
                target = flat.get("target_sheet") or chart_target
                target = self._infer_target_sheet({"target_sheet": target}) if target else target
                explicit_metrics = any(numeric(flat.get(field)) is not None
                                       for field in METRIC_UNITS.get(chart_target, {}))
                if not flat.get("target_sheet") and not explicit_metrics and chart_target in {
                        "emissions_regional", "emissions_management", "emissions_forecast", "reduction_targets"}:
                    kind = flat.get("종류")
                    if isinstance(kind, str) and kind in {"현황", "전망", "목표"}:
                        target = {"현황": "emissions_management" if chart_target == "emissions_management" else "emissions_regional",
                                  "전망": "emissions_forecast", "목표": "reduction_targets"}[kind]
                original_target = target
                target, converted_item, route_reason = resolve_row_target(target, item, analysis)
                row, conversion_reasons = convert_chart_row(target, converted_item, analysis, municipality)
                if target == "emissions_forecast" and not row.get("시나리오"):
                    row["시나리오"] = _declared_scenario(flat, analysis)
                # Gate on canonical year/period/value fields as well as raw flags.
                gate_item = {**flat, **row, "fields": item.get("fields", {})}
                can_merge, final_confidence, reasons = self._chart_merge_decision(analysis, gate_item, target)
                reasons = sorted(set([*reasons, *input_reasons, *conversion_reasons]))
                declared_reviews = flat.get("review_reasons") or analysis.get("review_reasons")
                if declared_reviews:
                    # Preserve explicit model uncertainty, without elevating an
                    # otherwise eligible row merely because it has a number.
                    declared_reviews = declared_reviews if isinstance(declared_reviews, list) else [declared_reviews]
                    reasons += ["모델 검토 요청:" + str(reason) for reason in declared_reviews]
                reasons = sorted(set(reasons))
                can_merge = can_merge and not reasons
                observed = {**flat, "fields": item.get("fields", {})}
                self._append_chart_observation(text_results, analysis, observed, target,
                                               can_merge, final_confidence, reasons)
                observation = text_results["chart_observations"][-1]
                observation.update({"원시행": raw_item, "변환후행": deepcopy(row), "보류사유": reasons,
                                    "객체ID": analysis.get("object_id"), "근거ID": analysis.get("evidence_id"),
                                    "observation_id": f"{fingerprint}:{item_index}",
                                    "vision_call_id": analysis.get("_vision_call_id"),
                                    "원래대상시트": original_target, "분류보완사유": route_reason,
                                    "판정단계": "정제 전 병합 허용 여부; 최종 Excel 저장과 구분"})
                decision_id = hashlib.sha256((str(analysis.get("_vision_call_id") or "parsed-replay") + ":" +
                                             observation["observation_id"]).encode()).hexdigest()
                observation["vision_decision_id"] = decision_id
                if self.trace:
                    self.trace.decision(decision_id, observation, row)
                previous = next((o for o in text_results["chart_observations"][:-1]
                                 if o.get("observation_id") == observation["observation_id"]), None)
                if previous is not None:
                    previous.update(observation)
                    text_results["chart_observations"].pop()
                if not can_merge:
                    continue
                row.setdefault("출처페이지", analysis.get("page_number"))
                row.setdefault("근거ID", analysis.get("evidence_id") or
                               f"vision-p{analysis.get('page_number')}-i{analysis.get('image_index', 1)}")
                row.setdefault("객체ID", analysis.get("object_id") or row["근거ID"])
                row.setdefault("추출원천", "visual")
                lineage = {"vision_decision_id": decision_id, "vision_call_id": analysis.get("_vision_call_id"),
                           "객체ID": row["객체ID"], "근거ID": row["근거ID"], "출처페이지": row["출처페이지"]}
                prior_lineage = _lineage(row) if row.get("출처계보") else []
                row["_extraction_sources"] = [*prior_lineage, lineage]
                row["출처계보"] = json.dumps(row["_extraction_sources"], ensure_ascii=False, sort_keys=True)
                text_results.setdefault(target, []).append(row)
            for key, count in before_counts.items():
                rows = text_results.get(key, [])
                if len(rows) > count:
                    rows[:] = merge_extraction_rows(key, rows[:count], rows[count:],
                                                    text_results.setdefault("merge_conflicts", []))
        return text_results

    def extract(self, pages: list[PageContent], text_results: dict, municipality: str) -> dict:
        started = time.perf_counter()
        index_pages = {p.page_number for p in pages if _index_only_page(p)}
        raw_images = [
            (p, img)
            for p in pages
            for img in p.images
            if _is_relevant_image(img) and p.page_number not in index_pages
        ]
        coverage_images = _coverage_reduce_images(raw_images)
        if len(coverage_images) != len(raw_images):
            print(
                "[에이전트2b 이미지분석] 페이지 렌더 기준 중복 축소: "
                f"{len(raw_images)}개 → {len(coverage_images)}개"
            )
        raw_images = coverage_images

        if config.IMAGE_TRIAGE_ENABLED:
            triaged = []
            reference_filtered = 0
            for page, image in raw_images:
                item = _triage_image(page, image)
                if config.IMAGE_TRIAGE_EXCLUDE_REFERENCE_CONTEXT:
                    is_reference, ref_hits = _has_reference_context(page, image, municipality)
                    if is_reference:
                        item["passed"] = False
                        item["score"] -= 20
                        item["reasons"].append("reference_context:" + ",".join(ref_hits[:4]))
                        reference_filtered += 1
                triaged.append(item)
            passed = [item for item in triaged if item["passed"]]
            passed.sort(key=lambda item: item["score"], reverse=True)

            self._triage_stats = {
                "raw": len(raw_images),
                "passed": len(passed),
                "filtered": len(raw_images) - len(passed),
                "min_score": config.IMAGE_TRIAGE_MIN_SCORE,
                "reference_filtered": reference_filtered,
            }

            pages_with_images = [(item["page"], item["image"]) for item in passed]
            print(
                "[에이전트2b 이미지분석] ChartQA-style triage: "
                f"후보 {len(raw_images)}개 → 통과 {len(pages_with_images)}개 "
                f"(필터 {len(raw_images) - len(pages_with_images)}개, "
                f"참고자료 {reference_filtered}개, 기준 {config.IMAGE_TRIAGE_MIN_SCORE})"
            )
            for item in passed[:5]:
                print(
                    f"  - triage 상위 후보: p{item['page'].page_number}, "
                    f"score={item['score']}, {', '.join(item['reasons'][:4])}"
                )
        else:
            pages_with_images = raw_images
            self._triage_stats = {
                "raw": len(raw_images),
                "passed": len(raw_images),
                "filtered": 0,
                "min_score": None,
            }

        # 상한 적용은 명시적으로 설정한 경우에만 수행한다. 기본값은 전수 분석이다.
        max_images = getattr(config, "MAX_IMAGES", None)
        if isinstance(max_images, int) and max_images > 0 and len(pages_with_images) > max_images:
            print(
                f"[에이전트2b 이미지분석] triage 통과 이미지 {len(pages_with_images)}개 중 "
                f"상위 {max_images}개만 분석"
            )
            pages_with_images = pages_with_images[:max_images]
        else:
            print("[에이전트2b 이미지분석] 이미지 분석 상한 없음: 후보 전수 분석")

        total = len(pages_with_images)
        self._triage_stats["index_pages_excluded"] = sorted(index_pages)
        self._triage_stats["render_variants"] = {
            "full_page": sum("full render" in str(image.get("caption", "")).lower() for _, image in pages_with_images),
            "embedded": sum("full render" not in str(image.get("caption", "")).lower() for _, image in pages_with_images),
        }
        self._triage_stats["scheduled_batches"] = 0
        self._triage_stats["completed_batches"] = 0
        self._triage_stats["partial_batches"] = 0
        self._triage_stats["failed_batches"] = []
        print(f"[에이전트2b 이미지분석] 분석 대상 이미지: {total}개")

        if total == 0:
            print("[에이전트2b 이미지분석] 분석할 이미지 없음.")
            self._triage_stats["elapsed_seconds"] = time.perf_counter() - started
            text_results["vision_execution"] = dict(self._triage_stats)
            return text_results

        analyses = []
        batch_size = max(1, int(getattr(config, "IMAGE_ANALYSIS_BATCH_SIZE", 1) or 1))
        image_batches = [
            pages_with_images[i:i + batch_size]
            for i in range(0, len(pages_with_images), batch_size)
        ]
        processed = 0
        self._triage_stats["scheduled_batches"] = len(image_batches)
        for batch_num, image_batch in enumerate(image_batches, start=1):
            processed += len(image_batch)
            page_nums = [page.page_number for page, _ in image_batch]
            print(
                f"[에이전트2b 이미지분석] 배치 {batch_num}/{len(image_batches)} "
                f"(누적 {processed}/{total}, 페이지 {page_nums[0]}~{page_nums[-1]}) 분석 중..."
            )
            try:
                self._batch_input_reviews = []
                if config.IMAGE_CHART_TABLE_EXTRACTION:
                    batch_results = self._chart_to_table_batch(image_batch, municipality)
                else:
                    batch_results = []
                    for page, image in image_batch:
                        result = self._analyze_image(image, page.page_number, municipality)
                        if result:
                            batch_results.append(result)
            except Exception as error:
                self._triage_stats["failed_batches"].append({"batch": batch_num, "pages": page_nums, "error": str(error)})
                print(f"[Vision 경고] 배치 실패, 이전 성공 결과 유지: {error}")
                continue
            analyses.extend(batch_results)
            # Save hold reasons and eligible rows after each successful boundary,
            # so a later interruption does not erase earlier decisions.
            self._merge_image_results(text_results, batch_results, municipality)
            if self.trace:
                self.trace.checkpoint()
            if self._batch_input_reviews:
                self._triage_stats["partial_batches"] += 1
                self._triage_stats["failed_batches"].append({"batch": batch_num, "pages": page_nums,
                    "error": "시각 배치 응답 일부 미완료", "status": "partial", "diagnostics": self._batch_input_reviews})
            else:
                self._triage_stats["completed_batches"] += 1
            if batch_results:
                titles = ", ".join(str(r.get("title", "제목없음"))[:30] for r in batch_results[:3])
                print(f"  → 유효 {len(batch_results)}건: {titles}")
            else:
                print("  → 유효 분석 없음/응답 미완료: 원응답·입력 진단 확인" if self._batch_input_reviews else
                      "  → 관련 있는 표/그래프 없음 (해당없음 응답)")

        self._image_results = analyses
        print(f"[에이전트2b 이미지분석] 유효 분석 {len(analyses)}개 완료")

        merged_results = text_results
        observed_pages = {
            row.get("페이지")
            for row in merged_results.get("chart_observations", [])
            if isinstance(row, dict)
        }
        fallback_count = 0
        fallback_limit = getattr(config, "IMAGE_FALLBACK_MAX_OBSERVATIONS", None)
        for page, _ in pages_with_images:
            if page.page_number in observed_pages:
                continue
            for observation in _fallback_visual_observations(page, municipality):
                merged_results.setdefault("chart_observations", []).append(observation)
                fallback_count += 1
                if isinstance(fallback_limit, int) and fallback_limit > 0 and fallback_count >= fallback_limit:
                    break
            if isinstance(fallback_limit, int) and fallback_limit > 0 and fallback_count >= fallback_limit:
                break
        if fallback_count:
            print(
                "[에이전트2b 이미지분석] Vision 미판독 페이지에서 "
                f"페이지 렌더/표 주변 텍스트 기반 검토 후보 {fallback_count}건 보존"
            )

        self._triage_stats["elapsed_seconds"] = time.perf_counter() - started
        merged_results["vision_execution"] = dict(self._triage_stats)
        return merged_results

    def report(self) -> str:
        types: dict[str, int] = {}
        for a in self._image_results:
            t = a.get("type", "기타")
            types[t] = types.get(t, 0) + 1
        type_str = ", ".join(f"{k}: {v}개" for k, v in types.items()) or "없음"
        triage = self._triage_stats or {}
        triage_line = ""
        if triage:
            triage_line = (
                f"\n  - triage: 원본 {triage.get('raw', 0)}개 → "
                f"통과 {triage.get('passed', 0)}개 / 필터 {triage.get('filtered', 0)}개"
                f" / 참고자료 제외 {triage.get('reference_filtered', 0)}개"
            )
        return (
            f"[에이전트2b 이미지분석] 완료\n"
            f"  - 분석된 이미지: {len(self._image_results)}개\n"
            f"  - 유형별: {type_str}"
            f"{triage_line}"
        )

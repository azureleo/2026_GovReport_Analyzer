"""Compare contracts on the SAME frozen extraction input; never calls an LLM."""
from __future__ import annotations

import argparse
from copy import deepcopy
import gzip
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from agents.organizer_agent import OrganizerAgent
from agents.excel_agent import ExcelAgent
from utils.fact_contracts import METRICS, persist_contract_report
from utils.integration_audit import document_hash, persist_audit, save_json
from utils.document_objects import DocumentObject
from utils.table_paths import render_paths


def read_json(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig") as stream:
        return json.load(stream)


def replay(raw_path, output_dir, *, objects_audit=None, selected_mode="compare"):
    raw_path, output_dir = Path(raw_path).resolve(), Path(output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("출력 폴더가 비어 있지 않습니다. 새 폴더를 지정하세요.")
    source = read_json(raw_path)
    if not isinstance(source, dict):
        raise ValueError("입력은 추출 결과 JSON 객체여야 합니다.")
    raw = source.get("raw_data", source)
    if not isinstance(raw, dict) or not any(isinstance(raw.get(k), list) for k in METRICS):
        raise ValueError("지원 시트가 없는 입력입니다. Excel/감사 요약을 원시 추출 결과로 취급하지 않습니다.")
    inputs = {str(raw_path): document_hash(raw_path)}
    binding = "objects_embedded_in_provided_input" if raw.get("document_objects") else "no_source_objects"
    if objects_audit:
        objects_audit = Path(objects_audit).resolve()
        audit = read_json(objects_audit)
        objects = audit.get("ledgers", {}).get("document_objects")
        if not isinstance(objects, list):
            raise ValueError("감사 파일에 document_objects 배열이 없습니다.")
        # Legacy raw snapshots have no PDF hash. Record that limitation instead
        # of presenting a manually paired audit as a verified source binding.
        claimed = source.get("input_sha256")
        actual = audit.get("context", {}).get("input_sha256")
        if claimed and claimed != actual:
            raise ValueError("추출 입력과 문서 객체의 원문 해시가 다릅니다.")
        binding = "matching_document_sha256" if claimed and claimed == actual else "user_paired_document_binding_unverified"
        raw = {**raw, "document_objects": objects}
        inputs[str(objects_audit)] = document_hash(objects_audit)
    objects = [DocumentObject.from_dict(o) for o in raw.get("document_objects", [])]
    output_dir.mkdir(parents=True, exist_ok=True)
    # The path preview is an input-format diagnostic, not a new model result.
    preview, path_diagnostics = render_paths([o for o in objects if o.rows], max_chars=config.TABLE_HEADER_PATHS_MAX_CHARS)
    (output_dir / "header_path_preview.txt").write_text(preview, encoding="utf-8")
    save_json(output_dir / "header_path_diagnostics.json", path_diagnostics)
    previous = config.FACT_CONTRACT_MODE
    summary = {"input_hashes": inputs, "input_scope": source.get("scope", "provided_rows_only"),
               "source_object_binding": binding,
               "new_model_calls": 0, "model_accuracy_evaluated": False, "modes": {},
               "limitation": "Frozen responses test contracts/connection only; header prompt effects need a later matched live test."}
    try:
        for current in (["off", "audit", "enforce"] if selected_mode == "compare" else [selected_mode]):
            config.FACT_CONTRACT_MODE = current
            started = time.perf_counter()
            data = OrganizerAgent().organize(deepcopy(raw))
            path = ExcelAgent().write(data, output_dir / (current + ".xlsx"))
            saved = persist_contract_report(path, data) if current != "off" else {"status": "disabled"}
            context = {"warnings": [], "model_accuracy_evaluated": False, "frozen_replay": True,
                       "input_hashes": inputs, "fact_contracts": saved}
            audit_path = persist_audit(path, raw_path, data, context)
            summary["modes"][current] = {"excel": str(path), "audit": str(audit_path), "saved_check": saved,
                "body_rows": {k: len(data.get(k, [])) for k in METRICS},
                "contract_summary": data.get("fact_contract_summary", {}),
                "elapsed_seconds": round(time.perf_counter() - started, 4)}
    finally:
        config.FACT_CONTRACT_MODE = previous
    if any(document_hash(p) != value for p, value in inputs.items()):
        raise RuntimeError("검증 도중 입력 해시가 변경됐습니다.")
    save_json(output_dir / "comparison.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_input", help="동결한 raw_data JSON 또는 raw_business.json.gz")
    parser.add_argument("--objects-audit", help="같은 실행의 pipeline_audit.json (문서 객체 보완)")
    parser.add_argument("--output-dir", required=True, help="새 출력 폴더 (기존 파일 덮어쓰기 금지)")
    parser.add_argument("--mode", choices=["compare", "off", "audit", "enforce"], default="compare")
    args = parser.parse_args()
    try:
        summary = replay(args.raw_input, args.output_dir, objects_audit=args.objects_audit, selected_mode=args.mode)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps({"output": str(Path(args.output_dir).resolve()), "new_model_calls": summary["new_model_calls"],
                      "model_accuracy_evaluated": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()

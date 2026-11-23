"""Durable Vision boundary responses and decisions, independent of run-state.

The response is the literal text returned by llm_client, not the provider's wire
payload or its individual internal retries. Image data and credentials are never
written here. Saved-cell links document preservation, not factual accuracy.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import uuid

logger = logging.getLogger(__name__)
VERSION = "vision-trace-v1"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def image_reference(page_number, image, index=1):
    reference = {key: deepcopy(image[key]) for key in
                 ("object_id", "evidence_id", "bbox", "caption", "width", "height") if key in image}
    reference.update(image_index=index, page_number=page_number,
                     image_base64_sha256=hashlib.sha256(str(image.get("base64", "")).encode()).hexdigest())
    return reference


class VisionTrace:
    def __init__(self, output_path, *, input_sha256="", reporter=None):
        base = Path(output_path).resolve().with_suffix("")
        session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        self.directory = base.with_name(base.name + "_vision_trace") / session
        self.index_path = base.with_name(base.name + "_vision_trace.json")
        self.calls = {}
        self.decisions = {}
        self.errors = []
        self.input_sha256 = input_sha256
        self.reporter = reporter
        self.started_at = _now()
        self.excel_check = {"status": "not_run"}

    def _io(self, operation, fn):
        try:
            fn()
            return True
        except (OSError, TypeError, ValueError) as error:
            message = f"Vision 기록 저장/조회 실패 ({operation}; 추출·Excel 보존): {error}"
            if message not in self.errors:
                self.errors.append(message)
                logger.warning(message)
                if self.reporter:
                    self.reporter("[경고] " + message)
            return False

    def _write_json(self, path, value):
        def write():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(_json(value) + "\n", encoding="utf-8")
                temporary.replace(path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return self._io(str(path), write)

    def summary(self):
        return {"schema_version": VERSION, "started_at": self.started_at,
                "input_sha256": self.input_sha256, "index_path": str(self.index_path),
                "directory": str(self.directory), "decisions_path": str(self.directory / "decisions.jsonl"),
                "excel_links_path": str(self.directory / "excel_links.jsonl"),
                "capture_scope": "llm_client return text; internal retries/wire payload not captured",
                "calls": [{k: c[k] for k in ("call_id", "attempt", "status", "record_path", "response_path",
                           "response_sha256", "raw_saved", "analysis_count") if k in c} for c in self.calls.values()],
                "call_counts": dict(Counter(c["status"] for c in self.calls.values())),
                "decision_counts": dict(Counter(d["merge_status"] for d in self.decisions.values())),
                "hold_reason_counts": dict(Counter(r for d in self.decisions.values() for r in d["hold_reasons"])),
                "excel_check": deepcopy(self.excel_check), "persistence_errors": list(self.errors),
                "model_accuracy_evaluated": False}

    def checkpoint(self):
        def ensure_journal():
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / "decisions.jsonl").touch(exist_ok=True)
        self._io("initialize decisions.jsonl", ensure_journal)
        self._write_json(self.index_path, self.summary())
        return self.summary()

    def begin(self, *, prompt, system, images, attempt, mode):
        call_id = f"a{attempt}-c{len(self.calls) + 1:05d}-{uuid.uuid4().hex[:8]}"
        record_path = self.directory / "calls" / (call_id + ".json")
        response_path = self.directory / "responses" / (call_id + ".txt")
        record = {"schema_version": VERSION, "call_id": call_id, "attempt": attempt,
                  "started_at": _now(), "status": "requested", "mode": mode,
                  "record_path": str(record_path), "response_path": str(response_path),
                  "request": {"prompt": prompt, "system": system, "images": images}, "raw_saved": False}
        self.calls[call_id] = record
        self._write_json(record_path, record)
        self.checkpoint()
        return call_id

    def response(self, call_id, response):
        record = self.calls[call_id]
        payload = (response if isinstance(response, str) else "").encode("utf-8")
        path = Path(record["response_path"])
        def write():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Unique call IDs; never replace another call's literal response.
            with path.open("xb") as stream:
                stream.write(payload)
        record.update(status="response_received", response_type=type(response).__name__,
                      response_sha256=hashlib.sha256(payload).hexdigest(), response_bytes=len(payload),
                      raw_saved=self._io(str(path), write))
        self._write_json(Path(record["record_path"]), record)
        self.checkpoint()

    def finish(self, call_id, status, *, diagnostics=(), analyses=(), error=None):
        record = self.calls[call_id]
        record.update(status=status, finished_at=_now(), diagnostics=deepcopy(list(diagnostics)),
                      analysis_count=len(analyses))
        if error is not None:
            record["error"] = {"type": type(error).__name__, "message": str(error)}
        self._write_json(Path(record["record_path"]), record)
        self.checkpoint()

    def decision(self, decision_id, observation, row):
        record = {"schema_version": VERSION, "decision_id": decision_id,
                  "call_id": observation.get("vision_call_id"), "observation_id": observation["observation_id"],
                  "attempt": self.calls.get(observation.get("vision_call_id"), {}).get("attempt"),
                  "stage": "pre_organizer_merge_gate", "target_sheet": observation["대상시트"],
                  "merge_status": "eligible" if observation["반영여부"] == "반영" else "held",
                  "hold_reasons": deepcopy(observation["보류사유"]), "observation": deepcopy(observation),
                  "proposal": deepcopy(row)}
        if self.decisions.get(decision_id) == record:
            return
        self.decisions[decision_id] = record
        def append():
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / "decisions.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(_json(record) + "\n")
        self._io("decisions.jsonl", append)

    def finalize(self, excel_path, final_data=None):
        """Locate decision IDs in actual saved lineage; check each proposed metric."""
        from utils.extraction_schema import BUSINESS_SHEETS, METRIC_UNITS, SHEET_NAMES, display_field
        from utils.vision_rows import numeric
        import openpyxl

        linked = {}
        headers_available = set()
        unreadable_lineage = Counter()
        post_reviews = {}
        def decision_ids(value):
            found = set()
            if isinstance(value, dict):
                if isinstance(value.get("vision_decision_id"), str):
                    found.add(value["vision_decision_id"])
                for key, child in value.items():
                    if key == "출처계보" and isinstance(child, str):
                        try:
                            child = json.loads(child)
                        except ValueError:
                            continue
                    if isinstance(child, (dict, list)):
                        found.update(decision_ids(child))
            elif isinstance(value, list):
                for child in value:
                    found.update(decision_ids(child))
            return found
        for ledger in ("semantic_contract_review", "_semantic_contract_quarantine", "fact_contract_review",
                       "field_contract_review", "reduction_target_context", "merge_conflicts"):
            records = (final_data or {}).get(ledger, [])
            for record in records if isinstance(records, list) else []:
                if not isinstance(record, dict):
                    continue
                ids = decision_ids(record) & self.decisions.keys()
                if not ids:
                    continue
                contract = record.get("contract") or {}
                if ledger == "fact_contract_review" and not (contract.get("errors") or contract.get("notes")
                                                             or record.get("decision") == "held"):
                    continue
                reference = {"ledger": ledger, "sheet_key": record.get("sheet_key"),
                             "decision": record.get("decision"), "reason": record.get("reason"),
                             "reason_codes": record.get("reason_codes", []),
                             "contract_errors": contract.get("errors", []), "contract_notes": contract.get("notes", [])}
                for did in ids:
                    if reference not in post_reviews.setdefault(did, []):
                        post_reviews[did].append(reference)
        def read():
            book = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
            try:
                for sheet in BUSINESS_SHEETS:
                    name = SHEET_NAMES[sheet]
                    if name not in book.sheetnames:
                        continue
                    values = book[name].iter_rows(values_only=True)
                    headers = next(values, ())
                    if "출처계보" not in headers:
                        continue
                    headers_available.add(sheet)
                    for n, cells in enumerate(values, 2):
                        row = dict(zip(headers, cells))
                        try:
                            lineage = json.loads(row.get("출처계보") or "[]")
                        except (ValueError, TypeError):
                            unreadable_lineage[sheet] += 1
                            continue
                        if not isinstance(lineage, list):
                            unreadable_lineage[sheet] += 1
                            continue
                        for source in lineage:
                            did = source.get("vision_decision_id") if isinstance(source, dict) else None
                            if did in self.decisions:
                                expected = self.decisions[did]["proposal"]
                                checks = [{"field": field, "expected": expected[field],
                                           "saved": row.get(display_field(sheet, field)),
                                           "equal_numeric": numeric(expected[field]) == numeric(row.get(display_field(sheet, field)))}
                                          for field in METRIC_UNITS.get(sheet, {}) if numeric(expected.get(field)) is not None]
                                linked.setdefault(did, []).append({"sheet": name, "excel_row": n, "metrics": checks})
            finally:
                book.close()
        read_ok = self._io("saved Excel lineage", read)
        results = []
        last_attempt = max((call["attempt"] for call in self.calls.values()), default=None)
        for did, decision in self.decisions.items():
            if decision["merge_status"] == "held":
                status = "held_before_merge"
            elif not read_ok or decision["target_sheet"] not in headers_available:
                status = "unassessed"
            elif did in linked:
                checks = [c for link in linked[did] for c in link["metrics"]]
                status = "excel_linked_values_equal" if checks and all(c["equal_numeric"] for c in checks) else "excel_linked_review"
            elif last_attempt is not None and decision["attempt"] is not None and decision["attempt"] < last_attempt:
                status = "superseded_attempt"
            elif unreadable_lineage[decision["target_sheet"]]:
                status = "unassessed"
            elif any(r["decision"] in {"hold", "held"} or r["ledger"] == "_semantic_contract_quarantine"
                     for r in post_reviews.get(did, [])):
                status = "held_after_cleaning"
            else:
                status = "not_located_in_excel_lineage"
            results.append({"decision_id": did, "status": status, "links": linked.get(did, []),
                            "hold_reasons": decision["hold_reasons"], "post_processing_reviews": post_reviews.get(did, [])})
        self.excel_check = {"status": "checked" if read_ok else "failed",
                            "excel_path": str(Path(excel_path).resolve()),
                            "counts": dict(Counter(r["status"] for r in results)),
                            "unreadable_lineage_cells": dict(unreadable_lineage),
                            "post_processing_review_decisions": len(post_reviews),
                            "scope": "decision lineage + proposed numeric fields; not PDF fact accuracy"}
        # JSONL is convenient for large runs and filtering held decisions.
        def save_links():
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / "excel_links.jsonl").write_text("".join(_json(r) + "\n" for r in results), encoding="utf-8")
        self._io("excel_links.jsonl", save_links)
        return self.checkpoint()

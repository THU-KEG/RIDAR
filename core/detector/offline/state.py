"""Asynchronous state machine for the offline Analyzer–Verifier detector."""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...collector.records import record_text, validate_record
from ..taxonomy import behavior_phase
from ..verbatim import NUMBER, number_in
from . import schemas
from .agents import CodexVerifier, Verifier
from .auto import AutoApproveVerifier
from .config import DetectorConfig
from ..rubric import CHECKS_BY_KIND, KIND_OF_CHECK, REFERENCE_KINDS, RUBRIC_IDS
from .files import write_json_atomic
from .index import SEGMENT_CHARS, segments as trace_segments


MAX_COMPLETION_CHALLENGES = 3
MAX_COMPLETION_FAILURES = 3
OBJECT_VERDICTS = ("clean", "violation")



class OpError(Exception):
    def __init__(self, message: str, **data: Any):
        super().__init__(message)
        self.data = data


class DetectionState:
    """Own one run, its asynchronous review queue, and accepted behaviors."""

    def __init__(
        self,
        verifier: Verifier | None = None,
        config: DetectorConfig | None = None,
        log_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        bound_run_dir: str | Path | None = None,
        verifier_workdir: str | Path | None = None,
        segment_chars: int = SEGMENT_CHARS,
    ):
        self.log_dir = Path(log_dir).resolve() if log_dir is not None else None
        self.output_dir = Path(output_dir).resolve() if output_dir is not None else None
        self.bound_run_dir = (
            Path(bound_run_dir).resolve() if bound_run_dir is not None else None
        )
        self.verifier_workdir = (
            Path(verifier_workdir).resolve() if verifier_workdir is not None else None
        )
        self.segment_chars = segment_chars
        self.mode = getattr(config, "mode", "default") if config is not None else "default"
        self._configured_runtime = (
            {
                "analyzer": {
                    "runtime": config.analyzer.executable,
                    "model": config.analyzer.model,
                },
                "verifier": {
                    "runtime": config.verifier.executable,
                    "model": config.verifier.model,
                },
            }
            if config is not None
            else {}
        )
        if config is not None and self.mode in ("analyzer-only", "verifier-only"):
            # The ablated role did not run; the report must not claim it did.
            ablated, stand_in = (
                ("verifier", "auto-approve") if self.mode == "analyzer-only"
                else ("analyzer", "auto-confirm")
            )
            self._configured_runtime[ablated] = {"runtime": stand_in}
        if verifier is None:
            if config is None:
                raise ValueError("config is required when verifier is not supplied")
            if self.mode == "analyzer-only":
                verifier = AutoApproveVerifier()
            else:
                verifier = CodexVerifier.from_config(
                    config.verifier,
                    api_key=_credential(config.verifier.api_key_env),
                    log_path=(self.log_dir / "verifier.log") if self.log_dir else None,
                )
        self.verifier = verifier
        self._lock = threading.RLock()
        self._updates_ready = threading.Condition(self._lock)
        self._verifier_tasks: queue.Queue[dict[str, Any]] = queue.Queue()
        self._verifier_worker: threading.Thread | None = None
        self._closing = False
        self._reset()

    def _reset(self) -> None:
        self.run_dir: Path | None = None
        self.run_id: str | None = None
        self.metadata: dict[str, Any] = {}
        self.records: list[dict[str, Any]] = []
        self.runtime: dict[str, Any] = {}
        self.started_at: str | None = None
        self.completed_at: str | None = None
        self.total_trace_chars = 0
        self.segments: list[dict[str, Any]] = []
        self.segment_summaries: dict[str, dict[str, Any]] = {}
        self.cases: dict[str, dict[str, Any]] = {}
        self.next_proposal_number = 1
        self.omissions: dict[str, dict[str, Any]] = {}
        self.next_omission_number = 1
        self.completion_challenges = 0
        self.completion_capped = False
        self.completion_failures = 0
        self.completion_error: str | None = None
        self.accepted: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.timings: dict[str, Any] = {}
        self.finish_pending = False
        self.finished = False
        self.report: dict[str, Any] | None = None
        self.detections_dir: Path | None = None

    # ------------------------------------------------------------------ setup

    def start_detection(
        self,
        run_path: str | None = None,
    ) -> dict[str, Any]:
        if self.run_dir is not None:
            raise OpError(
                "this MCP session already owns a detection; start a new session for another run"
            )
        if self.bound_run_dir is not None:
            if run_path is not None and Path(run_path).expanduser().resolve() != self.bound_run_dir:
                raise OpError("this detector session is bound to its isolated evidence view")
            run_dir = self.bound_run_dir
        else:
            if not isinstance(run_path, str) or not run_path:
                raise OpError("run_path must be a collected run directory")
            run_dir = Path(run_path).expanduser().resolve()
        run_json = run_dir / "extracted" / "run.json"
        trace_jsonl = run_dir / "extracted" / "trace.jsonl"
        if not run_json.is_file() or not trace_jsonl.is_file():
            raise OpError(
                f"no extracted/run.json and extracted/trace.jsonl under {run_dir}"
            )
        metadata = json.loads(run_json.read_text(encoding="utf-8"))
        if metadata.get("status") == "failed":
            raise OpError("collection status is failed; this run cannot be detected")
        with self._lock:
            self._closing = False
            self._verifier_tasks = queue.Queue()
            self._reset()
            self.run_dir = run_dir
            self.run_id = str(metadata.get("id") or run_dir.name)
            self.metadata = metadata
            self.records = _load_trace(trace_jsonl)
            self.total_trace_chars = sum(len(record_text(record)) for record in self.records)
            self.segments = trace_segments(self.records, self.segment_chars)
            self.runtime = dict(self._configured_runtime)
            self.started_at = _now()
            self.timings = {"completion_requested_at": []}
            detections = self.output_dir or (run_dir / "detections")
            self.detections_dir = detections
            if detections.exists():
                preserved = (
                    {path.resolve() for path in self.log_dir.glob("*.log")}
                    if self.log_dir == detections.resolve()
                    else set()
                )
                for child in detections.iterdir():
                    if child.resolve() in preserved:
                        continue
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            detections.mkdir(parents=True, exist_ok=True)
            (detections / "dialogue.jsonl").write_text("", encoding="utf-8")
            self._write_report_locked(status="running")

        try:
            self.verifier.start(run_dir=run_dir, workdir=self.verifier_workdir)
        except Exception as error:
            self._event("verifier_error", operation="start", error=_error_text(error))
            raise OpError(f"Verifier failed to start: {error}") from error

        self._verifier_worker = threading.Thread(
            target=self._verifier_worker_loop,
            name="ridar-verifier-worker",
            daemon=True,
        )
        self._verifier_worker.start()
        overview = self._overview()
        self._event("detection_started", **overview)
        return overview

    # ----------------------------------------------------------- trace reading

    def mark_segment_read(
        self,
        segment_id: str,
        summary: str,
        references: list[dict[str, Any]],
        checks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Confirm one reading segment: summary, reference tables, and the rubric table."""
        self._require_active()
        if not isinstance(summary, str) or not summary.strip():
            raise OpError("summary must be a non-empty string")
        with self._lock:
            segment_id = _strip_space(segment_id)
            segment = self._segment(segment_id)
            references = self._validate_references(segment, references)
            checks = self._validate_checks(segment, checks)
            self.segment_summaries[segment_id] = {
                "summary": summary.strip(),
                "references": references,
                "checks": checks,
                "marked_at": _now(),
            }
            self._write_record_locked()
            violations = _violation_refs(segment_id, checks)
            self._event(
                "segment_marked",
                segment_id=segment_id,
                start=segment["start"],
                end=segment["end"],
                summary=summary.strip(),
                references=len(references),
                objects=sum(len(item["objects"]) for item in checks),
                violations=violations,
                marked=len(self.segment_summaries),
                total=len(self.segments),
            )
            complete = self.analyzer_trace_scan_complete
            if complete and "scan_completed_at" not in self.timings:
                self.timings["scan_completed_at"] = _now()
                self._event(
                    "analyzer_trace_scan_completed", total_lines=len(self.records)
                )
            self._write_report_locked(status="running")
            return {
                "segment_id": segment_id,
                "record_path": str(self._record_path),
                "violations": violations,
                "marked": len(self.segment_summaries),
                "total": len(self.segments),
                "remaining": [
                    item["id"] for item in self.segments if item["id"] not in self.segment_summaries
                ],
                "scan_complete": complete,
            }

    def _segment(self, segment_id: str) -> dict[str, Any]:
        segment = next((item for item in self.segments if item["id"] == segment_id), None)
        if segment is None:
            raise OpError(
                f"unknown segment_id {segment_id!r}",
                segments=[item["id"] for item in self.segments],
            )
        return segment

    def _validate_references(
        self, segment: dict[str, Any], references: Any
    ) -> list[dict[str, Any]]:
        if not isinstance(references, list):
            raise OpError("references must be a list of {kind, line, text}")
        cleaned: list[dict[str, Any]] = []
        for index, item in enumerate(references):
            if not isinstance(item, dict):
                raise OpError(f"references[{index}] must be an object")
            kind = item.get("kind")
            if kind not in REFERENCE_KINDS:
                raise OpError(f"references[{index}].kind must be one of {REFERENCE_KINDS}")
            line = item.get("line")
            if not isinstance(line, int) or not segment["start"] <= line <= segment["end"]:
                raise OpError(
                    f"references[{index}].line must be a trace line inside "
                    f"{segment['id']} (L{segment['start']}-L{segment['end']})"
                )
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                raise OpError(f"references[{index}].text must be a non-empty string")
            if kind == "result":
                self._require_verbatim_numbers(text, line, f"references[{index}]")
            cleaned.append({"kind": kind, "line": line, "text": text.strip()})
        return sorted(cleaned, key=lambda item: (item["line"], item["kind"]))

    def _require_verbatim_numbers(self, text: str, line: int, label: str) -> None:
        """Every precision-bearing number in ``text`` must appear verbatim in trace line ``line``."""
        record = record_text(self.records[line - 1])
        for number in NUMBER.findall(text):
            if not number_in(number, record):
                raise OpError(
                    f"{label}: number {number} is not in line {line} verbatim; copy numbers at the recorded precision"
                )

    def _validate_sources(self, sources: Any, label: str, *, own_line: int | None = None) -> list[dict[str, Any]]:
        if not isinstance(sources, list):
            raise OpError(f"{label}.sources must be a list of {{line, text}}")
        out = []
        for j, src in enumerate(sources):
            sline, stext = (src or {}).get("line"), (src or {}).get("text")
            if not isinstance(sline, int) or not 1 <= sline <= len(self.records):
                raise OpError(f"{label}.sources[{j}].line must be a trace line")
            if sline == own_line:
                raise OpError(f"{label}.sources[{j}]: a claim cannot cite its own line as the record that produced it")
            if not isinstance(stext, str) or not stext.strip():
                raise OpError(f"{label}.sources[{j}].text must be the record's own words")
            self._require_verbatim_numbers(stext, sline, f"{label}.sources[{j}]")
            out.append({"line": sline, "text": " ".join(stext.split())})
        return out

    def _validate_checks(self, segment: dict[str, Any], checks: Any) -> list[dict[str, Any]]:
        if not isinstance(checks, list):
            raise OpError("checks must be a list of {id, objects, note}")
        by_id: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(checks):
            label = f"checks[{index}]"
            if not isinstance(item, dict):
                raise OpError(f"{label} must be an object")
            check_id = _strip_space(item.get("id")) if isinstance(item.get("id"), str) else item.get("id")
            if check_id not in RUBRIC_IDS:
                raise OpError(f"{label}.id must be one of {RUBRIC_IDS}")
            if check_id in by_id:
                raise OpError(f"check {check_id} appears more than once")
            objects = item.get("objects", [])
            if not isinstance(objects, list):
                raise OpError(f"check {check_id}: objects must be a list of {{line, text, verdict}}")
            note = item.get("note", "")
            if not isinstance(note, str):
                raise OpError(f"check {check_id}: note must be a string")
            rows: list[dict[str, Any]] = []
            for oindex, obj in enumerate(objects):
                olabel = f"check {check_id} objects[{oindex}]"
                if not isinstance(obj, dict):
                    raise OpError(f"{olabel} must be an object")
                line = obj.get("line")
                if not isinstance(line, int) or not segment["start"] <= line <= segment["end"]:
                    raise OpError(
                        f"{olabel}.line must be a trace line inside {segment['id']} "
                        f"(L{segment['start']}-L{segment['end']})"
                    )
                text = obj.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise OpError(f"{olabel}.text must describe the object in a few words")
                verdict = obj.get("verdict")
                if verdict not in OBJECT_VERDICTS:
                    raise OpError(f"{olabel}.verdict must be one of {OBJECT_VERDICTS}")
                onote = obj.get("note", "")
                if not isinstance(onote, str):
                    raise OpError(f"{olabel}.note must be a string")
                if not onote.strip():
                    # The note belongs to the object. A model that wrote it on the item instead is
                    # not asked again when the item has this one object: the note is unambiguous.
                    if len(objects) == 1 and note.strip():
                        onote = note
                    else:
                        raise OpError(
                            f"{olabel}: a {verdict} verdict needs a note naming the record it was compared against; "
                            "put it on the object entry ({line, text, verdict, note}), not on the item"
                        )
                row = {"line": line, "text": text.strip(), "verdict": verdict, "note": onote.strip()}
                kind = KIND_OF_CHECK[check_id]
                has_source = "source_line" in obj or "source_text" in obj
                if kind == "external" and has_source:
                    sline, stext = obj.get("source_line"), obj.get("source_text")
                    if not isinstance(sline, int) or not 1 <= sline <= len(self.records):
                        raise OpError(f"{olabel}.source_line must be the trace line where the source itself is recorded; omit both source fields when the trace never shows the source")
                    if not isinstance(stext, str) or not stext.strip():
                        raise OpError(f"{olabel}.source_text must be the source's own words at source_line")
                    self._require_verbatim_numbers(stext, sline, f"{olabel}.source_text")
                    row["source_line"], row["source_text"] = sline, " ".join(stext.split())
                elif has_source:
                    raise OpError(f"{olabel}: only external entries (X items) carry source_line and source_text")
                if kind == "claim":
                    numbers = NUMBER.findall(text)
                    sources = obj.get("sources")
                    if sources is None:
                        if numbers:
                            raise OpError(
                                f"{olabel}: claim carries {numbers}; list sources (trace line and verbatim words that produced each number), or an empty list when the trace has no origin"
                            )
                    else:
                        checked = self._validate_sources(sources, olabel, own_line=line)
                        row["sources"] = checked
                        if numbers and not checked:
                            row["unsourced_numbers"] = numbers
                elif "sources" in obj:
                    raise OpError(f"{olabel}: only claim entries (C items) carry sources")
                rows.append(row)
            by_id[check_id] = {
                "id": check_id,
                "kind": KIND_OF_CHECK[check_id],
                "verdict": (
                    "not_present" if not rows
                    else "violation" if any(row["verdict"] == "violation" for row in rows)
                    else "clean"
                ),
                "objects": rows,
                "note": note.strip(),
            }
        missing = [check_id for check_id in RUBRIC_IDS if check_id not in by_id]
        if missing:
            raise OpError(
                f"checks must answer every rubric item; {len(missing)} missing",
                missing_checks=missing,
            )
        return [by_id[check_id] for check_id in RUBRIC_IDS]

    def _violation_checks(self) -> list[str]:
        found: list[str] = []
        for segment in self.segments:
            marked = self.segment_summaries.get(segment["id"])
            if marked is not None:
                found.extend(_violation_refs(segment["id"], marked["checks"]))
        return found

    def _write_record_locked(self) -> None:
        """Write the Analyzer's notes file from every marked segment, in trace order."""
        lines = [
            "# Reading record",
            "",
            "Written by the Analyzer while reading each segment: a summary, the "
            "reference tables it recorded, and its rubric table — for each question, "
            "the objects it checked with a verdict each. Not evidence; confirm "
            "against `extracted/trace.jsonl`.",
            "",
        ]
        for segment in self.segments:
            marked = self.segment_summaries.get(segment["id"])
            if marked is None:
                continue
            lines.append(f"## {segment['id']} (L{segment['start']}-L{segment['end']})")
            lines.append("")
            lines.append(marked["summary"])
            lines.append("")
            lines.append("### References")
            for item in marked["references"] or []:
                lines.append(f"- {item['kind']} L{item['line']}: {item['text']}")
            if not marked["references"]:
                lines.append("- none")
            lines.append("")
            lines.append("### Checks")
            for item in marked["checks"]:
                head = f"- {item['id']} {item['verdict']}"
                if item["note"]:
                    head += f" — {item['note']}"
                lines.append(head)
                for obj in item["objects"]:
                    row = f"  · L{obj['line']} {obj['verdict']}: {obj['text']}"
                    if obj["note"]:
                        row += f" — {obj['note']}"
                    lines.append(row)
            lines.append("")
        assert self.detections_dir is not None
        self._record_path.write_text("\n".join(lines), encoding="utf-8")

    @property
    def _record_path(self) -> Path:
        assert self.detections_dir is not None
        return self.detections_dir / "record.md"

    @property
    def analyzer_trace_scan_complete(self) -> bool:
        return all(item["id"] in self.segment_summaries for item in self.segments)

    # -------------------------------------------------------------- proposals

    def propose(
        self, behavior: str, evidence: str, checks: list[str] | None = None
    ) -> dict[str, Any]:
        """Queue one Analyzer proposal for the Verifier."""
        self._require_ready_for_proposals()
        return self._queue_new_case(behavior, evidence, origin="analyzer", checks=checks)

    def _validate_check_refs(self, checks: Any) -> list[str]:
        if checks is None:
            return []
        if not isinstance(checks, list) or not all(isinstance(item, str) for item in checks):
            raise OpError("checks must be a list of 'S6:E3@L416'-style references")
        # Model output can arrive with stray whitespace or control characters inside
        # tokens; references are compared without them.
        checks = [_strip_space(item) for item in checks]
        known = set(self._violation_checks())
        recorded = {
            ref
            for segment in self.segments
            for ref in _object_refs(
                segment["id"], (self.segment_summaries.get(segment["id"]) or {}).get("checks", [])
            )
        }
        unknown = [item for item in checks if item not in recorded]
        if unknown:
            raise OpError("checks reference rubric items that were not recorded", unknown=unknown)
        return sorted(set(checks), key=lambda item: (item not in known, item))

    def _queue_new_case(
        self,
        behavior: str,
        evidence: str,
        *,
        origin: str,
        checks: list[str] | None = None,
    ) -> dict[str, Any]:
        errors = schemas.validate_analyzer_proposal({"behavior": behavior, "evidence": evidence})
        if errors:
            raise OpError("invalid Analyzer proposal", errors=errors)
        with self._lock:
            check_refs = self._validate_check_refs(checks)
            proposal_id = f"p-{self.next_proposal_number:04d}"
            self.next_proposal_number += 1
            case = {
                "proposal_id": proposal_id,
                "revision": 1,
                "behavior": behavior.strip(),
                "evidence": evidence.strip(),
                "origin": origin,
                "checks": check_refs,
                "status": "queued",
                "submitted_at": _now(),
            }
            self.cases[proposal_id] = case
            self.timings.setdefault("first_proposal_at", case["submitted_at"])
            self._event("analyzer_proposal", **_proposal_event(case))
            self._verifier_tasks.put(
                {"kind": "review", "proposal_id": proposal_id, "revision": 1}
            )
            return {
                "proposal_id": proposal_id,
                "revision": 1,
                "status": "queued",
                "completed_updates": self._drain_updates_locked(),
            }

    def revise_proposal(
        self,
        proposal_id: str,
        behavior: str,
        evidence: str,
    ) -> dict[str, Any]:
        self._require_ready_for_proposals()
        errors = schemas.validate_analyzer_proposal({"behavior": behavior, "evidence": evidence})
        if errors:
            raise OpError("invalid Analyzer proposal", errors=errors)
        with self._lock:
            case = self._case(proposal_id)
            if case["status"] != "challenged":
                raise OpError(
                    "only a challenged proposal can be revised",
                    proposal_id=proposal_id,
                    status=case["status"],
                )
            case.update(
                revision=int(case["revision"]) + 1,
                behavior=behavior.strip(),
                evidence=evidence.strip(),
                status="queued",
                submitted_at=_now(),
            )
            self._event("analyzer_revision", **_proposal_event(case))
            self._verifier_tasks.put(
                {
                    "kind": "review",
                    "proposal_id": proposal_id,
                    "revision": case["revision"],
                }
            )
            return {
                "proposal_id": proposal_id,
                "revision": case["revision"],
                "status": "queued",
                "completed_updates": self._drain_updates_locked(),
            }

    def withdraw_proposal(self, proposal_id: str) -> dict[str, str]:
        self._require_active()
        with self._lock:
            case = self._case(proposal_id)
            # An auto-approved proposal (analyzer-only ablation) stays withdrawable:
            # with no independent review, self-correction is the only filter.
            if case["status"] == "withdrawn" or (
                case["status"] == "accepted"
                and not getattr(self.verifier, "trusted", False)
            ):
                raise OpError(
                    "proposal can no longer be withdrawn",
                    proposal_id=proposal_id,
                    status=case["status"],
                )
            case["status"] = "withdrawn"
            if case.get("findings"):
                # Only auto-approved cases reach here with findings; they leave with
                # the proposal instead of staying in the report marked withdrawn.
                self.accepted = [
                    finding
                    for finding in self.accepted
                    if finding["proposal_id"] != proposal_id
                ]
            self._write_report_locked(status="running")
            self._event(
                "proposal_withdrawn",
                proposal_id=proposal_id,
                revision=case["revision"],
            )
            return {"proposal_id": proposal_id, "status": "withdrawn"}

    def get_verifier_updates(self, wait_seconds: float = 0) -> dict[str, Any]:
        self._require_run()
        if wait_seconds < 0 or wait_seconds > 60:
            raise OpError("wait_seconds must be in 0..60")
        with self._updates_ready:
            if not self.updates and wait_seconds:
                self._updates_ready.wait(timeout=wait_seconds)
            return {
                "updates": self._drain_updates_locked(),
                **self._queue_status_locked(),
            }

    # -------------------------------------------------------------- omissions

    def resolve_omission(
        self,
        omission_id: str,
        decision: str,
        behavior: str | None = None,
        evidence: str | None = None,
    ) -> dict[str, Any]:
        """Confirm or reject one omission candidate from the completion review."""
        self._require_active()
        if decision not in schemas.OMISSION_DECISIONS:
            raise OpError(f"decision must be one of {schemas.OMISSION_DECISIONS}")
        with self._lock:
            if self.finish_pending:
                raise OpError("the completion review is already in progress")
            omission = self.omissions.get(omission_id)
            if omission is None:
                raise OpError(f"unknown omission_id {omission_id!r}")
            if omission["status"] != "open":
                raise OpError(
                    "omission is already resolved",
                    omission_id=omission_id,
                    status=omission["status"],
                )
            if decision == "reject":
                if not isinstance(evidence, str) or not evidence.strip():
                    raise OpError("reject requires evidence describing the counterevidence")
                omission.update(
                    status="rejected", resolution=evidence.strip(), resolved_at=_now()
                )
                self._event(
                    "omission_resolved",
                    omission_id=omission_id,
                    decision="reject",
                    evidence=evidence.strip(),
                )
                self._write_report_locked(status="running")
                return {"omission_id": omission_id, "status": "rejected"}
            final_behavior = (behavior or omission["behavior"]).strip()
            final_evidence = (evidence or omission["evidence"]).strip()
        queued = self._queue_new_case(
            final_behavior, final_evidence, origin=f"omission:{omission_id}"
        )
        with self._lock:
            omission.update(
                status="confirmed",
                proposal_id=queued["proposal_id"],
                resolved_at=_now(),
            )
            self._event(
                "omission_resolved",
                omission_id=omission_id,
                decision="confirm",
                proposal_id=queued["proposal_id"],
            )
            self._write_report_locked(status="running")
            return {
                "omission_id": omission_id,
                "status": "confirmed",
                "proposal_id": queued["proposal_id"],
                "completed_updates": queued["completed_updates"],
            }

    # -------------------------------------------------------------- completion

    def finish_analysis(self) -> dict[str, Any]:
        self._require_run()
        with self._lock:
            if self.finished:
                return {"status": "finished", "report": "detections/report.json"}
            self._require_analyzer_scan_complete()
            # A withdrawn proposal still covers its verdicts: they were submitted, and the
            # withdrawal (usually after a Verifier challenge) is their disposition. Excluding it
            # would block completion on a verdict the Analyzer has already given up.
            covered = {ref for case in self.cases.values() for ref in case.get("checks", [])}
            unsubmitted = [ref for ref in self._violation_checks() if ref not in covered]
            if unsubmitted:
                return {
                    "status": "blocked",
                    "reason": "every violation check must be submitted through propose "
                    "(pass its reference in checks) before completion",
                    "unsubmitted_checks": unsubmitted,
                    "completed_updates": self._drain_updates_locked(),
                }
            challenged = sorted(
                key for key, case in self.cases.items() if case["status"] == "challenged"
            )
            if challenged:
                return {
                    "status": "blocked",
                    "reason": "challenged proposals require revision or withdrawal",
                    "proposal_ids": challenged,
                    "completed_updates": self._drain_updates_locked(),
                }
            open_omissions = sorted(
                key for key, item in self.omissions.items() if item["status"] == "open"
            )
            if open_omissions:
                return {
                    "status": "blocked",
                    "reason": "open omissions require resolve_omission",
                    "omission_ids": open_omissions,
                    "completed_updates": self._drain_updates_locked(),
                }
            status = self._queue_status_locked()
            if status["pending"]:
                return {
                    "status": "waiting",
                    **status,
                    "completed_updates": self._drain_updates_locked(),
                }
            if self.finish_pending:
                return {
                    "status": "queued",
                    "scope": "completion",
                    "completed_updates": self._drain_updates_locked(),
                }
            if self.completion_challenges >= MAX_COMPLETION_CHALLENGES:
                self.completion_capped = True
                self._event(
                    "completion_capped",
                    completion_rounds=self.completion_challenges,
                    findings=len(self.accepted),
                )
                self._finalize_report_locked()
                return {
                    "status": "finished",
                    "capped": True,
                    "report": "detections/report.json",
                    "completed_updates": self._drain_updates_locked(),
                }
            self.finish_pending = True
            self.timings["completion_requested_at"].append(_now())
            self._event("completion_requested", round=self.completion_challenges + 1)
            self._verifier_tasks.put({"kind": "finish"})
            return {
                "status": "queued",
                "scope": "completion",
                "round": self.completion_challenges + 1,
                "completed_updates": self._drain_updates_locked(),
            }

    def close(self) -> None:
        worker = self._verifier_worker
        self._verifier_worker = None
        self._closing = True
        if worker and worker.is_alive():
            self._verifier_tasks.put({"kind": "stop"})
        try:
            self.verifier.close()
        except Exception:
            pass
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2)

    # --------------------------------------------------------- verifier worker

    def _verifier_worker_loop(self) -> None:
        while not self._closing:
            task = self._verifier_tasks.get()
            try:
                kind = task.get("kind")
                if kind == "stop":
                    return
                elif kind == "review":
                    self._process_review(task)
                elif kind == "finish":
                    self._process_finish()
            finally:
                self._verifier_tasks.task_done()

    def _process_review(self, task: dict[str, Any]) -> None:
        proposal_id = str(task["proposal_id"])
        revision = int(task["revision"])
        with self._lock:
            case = self.cases.get(proposal_id)
            if not case or case["status"] != "queued" or case["revision"] != revision:
                return
            case["status"] = "verifying"
            proposal = {"behavior": case["behavior"], "evidence": case["evidence"]}
            self._event("verifier_started", proposal_id=proposal_id, revision=revision)
        try:
            response = self.verifier.review(
                run_dir=self._active_run_dir,
                proposal_id=proposal_id,
                proposal=proposal,
                revision=revision,
                origin=case["origin"],
                accepted=list(self.accepted),
            )
            # The schema polices model output; a trusted in-process stand-in
            # (analyzer-only ablation) echoes proposals the server already validated.
            if not getattr(self.verifier, "trusted", False):
                errors = schemas.validate_verifier_response(response, len(self.records))
                if errors:
                    raise ValueError("; ".join(errors))
        except Exception as error:
            self._case_error(proposal_id, revision, "verifier_error", error)
            return
        review = {
            "decision": response["decision"],
            "cause": response["cause"],
            "reason": str(response["reason"]).strip(),
        }
        with self._lock:
            case = self.cases.get(proposal_id)
            if not case or case["revision"] != revision or case["status"] == "withdrawn":
                return
            case["verifier_review"] = review
            self._event("verifier_review", proposal_id=proposal_id, revision=revision, **review)
            if review["decision"] == "challenge":
                case["status"] = "challenged"
                self._add_update_locked({"proposal_id": proposal_id, "revision": revision, **review})
                return
            split = len(response["findings"]) > 1
            findings = []
            for index, item in enumerate(response["findings"], 1):
                finding = {
                    "finding_id": f"{proposal_id}.{index}" if split else proposal_id,
                    "proposal_id": proposal_id,
                    "revision": revision,
                    "origin": case["origin"],
                    "description": str(item["description"]).strip(),
                    "evidence": _located_evidence(item["evidence"]),
                    "taxonomy": str(item["taxonomy"]),
                    "basis": str(item["basis"]).strip(),
                    "phase": behavior_phase(str(item["taxonomy"])),
                    "verifier_reason": review["reason"],
                    "accepted_at": _now(),
                }
                findings.append(finding)
                self._event(
                    "finding_accepted",
                    **{key: finding[key] for key in ("finding_id", "proposal_id", "revision", "origin", "description", "taxonomy", "basis")},
                )
            case["findings"] = findings
            case["status"] = "accepted"
            self.accepted.extend(findings)
            self._write_report_locked(status="running")
            self._add_update_locked(
                {
                    "proposal_id": proposal_id,
                    "revision": revision,
                    "decision": "submit",
                    "cause": "none",
                    "reason": review["reason"],
                    "findings": [
                        {key: finding[key] for key in ("finding_id", "taxonomy", "description")}
                        for finding in findings
                    ],
                }
            )

    def _process_finish(self) -> None:
        try:
            with self._lock:
                rejected = [
                    {
                        "omission_id": item["omission_id"],
                        "behavior": item["behavior"],
                        "resolution": item.get("resolution", ""),
                    }
                    for item in self.omissions.values()
                    if item["status"] == "rejected"
                ]
                round_number = self.completion_challenges + 1
            response = self.verifier.finish(
                run_dir=self._active_run_dir,
                round=round_number,
                rounds_remaining=MAX_COMPLETION_CHALLENGES - round_number,
                rejected=rejected,
                accepted=list(self.accepted),
            )
            errors = schemas.validate_completion_response(response, len(self.records))
            if errors:
                raise ValueError("; ".join(errors))
        except Exception as error:
            with self._lock:
                self.finish_pending = False
                self.completion_failures += 1
                self.completion_error = _error_text(error)
                self._event("verifier_error", operation="finish", error=self.completion_error)
                if self.completion_failures >= MAX_COMPLETION_FAILURES:
                    self._finalize_report_locked(status="completion_failed")
                self._add_update_locked(
                    {
                        "scope": "completion",
                        "status": "error",
                        "reason": f"Verifier completion review failed: {error}",
                        "failures": self.completion_failures,
                        "finalized": self.finished,
                    }
                )
            return
        with self._lock:
            self.finish_pending = False
            decision = str(response["decision"])
            reason = str(response["reason"]).strip()
            if decision == "submit":
                self._event("completion_review", decision=decision, reason=reason, omissions=0)
                self._finalize_report_locked()
                self._add_update_locked(
                    {"scope": "completion", "decision": decision, "reason": reason}
                )
                return
            self.completion_challenges += 1
            proposed = []
            for source in response["omissions"]:
                omission_id = f"o-{self.next_omission_number:04d}"
                self.next_omission_number += 1
                omission = {
                    "omission_id": omission_id,
                    "round": self.completion_challenges,
                    "behavior": str(source["behavior"]).strip(),
                    "evidence": str(source["evidence"]).strip(),
                    "located_evidence": _located_evidence(source["located_evidence"]),
                    "status": "open",
                    "proposed_at": _now(),
                }
                self.omissions[omission_id] = omission
                proposed.append(
                    {
                        key: omission[key]
                        for key in ("omission_id", "behavior", "evidence", "located_evidence")
                    }
                )
                self._event(
                    "omission_proposed",
                    **{key: value for key, value in omission.items() if key != "status"},
                )
            self._event(
                "completion_review",
                decision=decision,
                reason=reason,
                round=self.completion_challenges,
                omissions=len(proposed),
            )
            self._write_report_locked(status="running")
            self._add_update_locked(
                {
                    "scope": "completion",
                    "decision": decision,
                    "reason": reason,
                    "round": self.completion_challenges,
                    "rounds_remaining": MAX_COMPLETION_CHALLENGES - self.completion_challenges,
                    "omissions": proposed,
                }
            )

    # ----------------------------------------------------------------- helpers

    def _overview(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_path": str(self.run_dir),
            "collection_status": self.metadata.get("status"),
            "collection_warnings": self.metadata.get("warnings", []),
            "task_id": self.metadata.get("task_id"),
            "total_lines": len(self.records),
            "segments": self.segments,
            "records_by_type": _counts(self.records, "type"),
            "tool_calls_by_name": _counts(
                [record for record in self.records if record["type"] == "tool_call"],
                "name",
            ),
            "agents": sorted(
                {str(record["agent_id"]) for record in self.records if record.get("agent_id")}
            ),
            "total_chars": self.total_trace_chars,
        }

    def _queue_status_locked(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for case in self.cases.values():
            counts[case["status"]] = counts.get(case["status"], 0) + 1
        pending = sum(counts.get(status, 0) for status in ("queued", "verifying"))
        open_omissions = sorted(
            key for key, item in self.omissions.items() if item["status"] == "open"
        )
        return {
            "pending": pending,
            "by_status": dict(sorted(counts.items())),
            "open_omissions": open_omissions,
        }

    def _drain_updates_locked(self) -> list[dict[str, Any]]:
        updates, self.updates = self.updates, []
        return updates

    def _add_update_locked(self, update: dict[str, Any]) -> None:
        self.updates.append(update)
        self._updates_ready.notify_all()

    def _case_error(
        self, proposal_id: str, revision: int, event_type: str, error: Exception
    ) -> None:
        with self._lock:
            case = self.cases.get(proposal_id)
            if not case or case["revision"] != revision or case["status"] == "withdrawn":
                return
            case["status"] = "challenged"
            detail = _error_text(error)
            self._event(
                event_type,
                proposal_id=proposal_id,
                revision=revision,
                error=detail,
            )
            self._add_update_locked(
                {
                    "proposal_id": proposal_id,
                    "revision": revision,
                    "decision": "challenge",
                    "status": "verifier_error",
                    "reason": f"Verifier failed: {detail}",
                }
            )

    def _finalize_report_locked(self, *, status: str = "completed") -> None:
        """End the run. ``status`` is anything but ``completed`` when the run stopped for a reason
        that leaves the verdict incomplete; only a completed report is exported or scored."""
        self.finished = True
        self.completed_at = _now()
        self._write_report_locked(status=status)
        self._event(
            "detection_completed" if status == "completed" else "detection_failed",
            status=status,
            findings=len(self.accepted),
            completion_rounds=self.completion_challenges,
            completion_capped=self.completion_capped,
            report="detections/report.json",
        )

    def _write_report_locked(self, *, status: str) -> None:
        self.report = {
            "status": status,
            "run_id": self.run_id,
            "runtime": self.runtime,
            "collection_status": self.metadata.get("status"),
            "collection_warnings": self.metadata.get("warnings", []),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "timings": self.timings,
            "usage": {"analyzer": None, "verifier": self.verifier.usage},
            "analyzer_full_trace_review_verified": self.analyzer_trace_scan_complete,
            "analyzer_trace_records_total": len(self.records),
            "analyzer_trace_chars_total": self.total_trace_chars,
            "segments": [
                {
                    **item,
                    "summary": self.segment_summaries.get(item["id"], {}).get("summary"),
                    "references": self.segment_summaries.get(item["id"], {}).get("references"),
                    "checks": self.segment_summaries.get(item["id"], {}).get("checks"),
                }
                for item in self.segments
            ],
            "completion_rounds": self.completion_challenges,
            "completion_capped": self.completion_capped,
            "completion_error": self.completion_error,
            "omissions": list(self.omissions.values()),
            "findings": self.accepted,
        }
        assert self.detections_dir is not None
        path = self.detections_dir / "report.json"
        write_json_atomic(path, self.report)

    def _event(self, event_type: str, **fields: Any) -> None:
        if self.run_dir is None:
            return
        event = {"time": _now(), "type": event_type, **fields}
        assert self.detections_dir is not None
        path = self.detections_dir / "dialogue.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def _case(self, proposal_id: str) -> dict[str, Any]:
        case = self.cases.get(proposal_id)
        if case is None:
            raise OpError(f"unknown proposal_id {proposal_id!r}")
        return case

    @property
    def _active_run_dir(self) -> Path:
        self._require_run()
        assert self.run_dir is not None
        return self.run_dir

    def _require_run(self) -> None:
        if self.run_dir is None:
            raise OpError("start_detection must be called first")

    def _require_active(self) -> None:
        self._require_run()
        if self.finished:
            raise OpError("detection is already finished")

    def _require_ready_for_proposals(self) -> None:
        self._require_active()
        self._require_analyzer_scan_complete()
        if self.finish_pending:
            raise OpError("the completion review is already in progress")

    def _require_analyzer_scan_complete(self) -> None:
        if self.mode == "verifier-only":
            # No Analyzer session exists to mark segments (verifier-only ablation);
            # the driver requests completion directly.
            return
        if not self.analyzer_trace_scan_complete:
            remaining = [
                item["id"] for item in self.segments if item["id"] not in self.segment_summaries
            ]
            raise OpError(
                "complete the mandatory trace scan: read every segment of "
                "extracted/trace.jsonl and confirm it with mark_segment_read before "
                "submitting or finishing",
                remaining_segments=remaining,
                total_segments=len(self.segments),
            )


def _load_trace(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for source_line, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise OpError(f"{path}:{source_line}: invalid JSON: {error}") from error
            try:
                validate_record(record)
            except ValueError as error:
                raise OpError(
                    f"{path}:{source_line}: invalid trace record", errors=[str(error)]
                ) from error
            records.append({"line": len(records) + 1, **record})
    return records


def _proposal_event(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": case["proposal_id"],
        "revision": case["revision"],
        "origin": case["origin"],
        "behavior": case["behavior"],
        "evidence": case["evidence"],
        "checks": case.get("checks", []),
    }


def _located_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "start": int(item["start"]),
            "end": int(item["end"]),
            "note": str(item["note"]).strip(),
        }
        for item in items
    ]


def _counts(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(key, ""))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _credential(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"required credential environment variable is missing: {name}")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _object_refs(segment_id: str, checks: list[dict[str, Any]]) -> list[str]:
    """One reference per checked object, 'S6:E3@L416' (with '#2' for repeats on a line)."""
    refs: list[str] = []
    for item in checks:
        seen: dict[int, int] = {}
        for obj in item["objects"]:
            seen[obj["line"]] = seen.get(obj["line"], 0) + 1
            suffix = f"#{seen[obj['line']]}" if seen[obj["line"]] > 1 else ""
            refs.append(f"{segment_id}:{item['id']}@L{obj['line']}{suffix}")
    return refs


def _violation_refs(segment_id: str, checks: list[dict[str, Any]]) -> list[str]:
    pairs = [(item, obj) for item in checks for obj in item["objects"]]
    return [
        ref
        for ref, (_, obj) in zip(_object_refs(segment_id, checks), pairs)
        if obj["verdict"] == "violation"
    ]


def _strip_space(value: str) -> str:
    """Remove whitespace and control characters that transport can insert into identifiers."""
    return "".join(ch for ch in value if not ch.isspace() and ch.isprintable())

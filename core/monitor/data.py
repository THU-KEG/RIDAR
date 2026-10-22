from __future__ import annotations

import json
import os
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RunView:
    metadata: dict[str, Any]
    events: list[dict[str, Any]]
    items: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    raw_records: int

    @classmethod
    def load(cls, run_dir: Path) -> "RunView":
        metadata_path, trace_path = _run_files(run_dir)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path else _live_metadata(run_dir)
        events = []
        with trace_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"trace.jsonl:{line_number}: expected JSON object")
                value["_trace_line"] = line_number
                events.append(value)
        raws = run_dir / "raws"
        raw_records = (
            sum(1 for path in raws.rglob("*") if path.is_file()) if raws.is_dir() else 0
        )
        return cls(
            metadata,
            events,
            _display_items(events),
            _sections(events),
            raw_records,
        )

    def detail(self) -> dict[str, Any]:
        tools = sorted(
            {
                str(item["name"])
                for item in self.items
                if item["kind"] == "tool" and item.get("name")
            }
        )
        by_type = Counter(str(event.get("type") or "unknown") for event in self.events)
        by_agent = Counter(str(event.get("agent_id") or "main") for event in self.events)
        by_tool = Counter(
            str(item["name"])
            for item in self.items
            if item["kind"] == "tool" and item.get("name")
        )
        pair_status = Counter(
            str(item.get("pair_status") or "unknown")
            for item in self.items
            if item["kind"] in {"tool", "tool_result"}
        )
        return {
            "metadata": self.metadata,
            "sections": self.sections,
            "tools": tools,
            "display_items": len(self.items),
            "summary": {
                "trace_records": len(self.events),
                "raw_records": self.raw_records,
                "by_type": dict(sorted(by_type.items())),
                "by_agent": dict(by_agent.most_common()),
                "by_tool": dict(by_tool.most_common()),
                "pair_status": dict(sorted(pair_status.items())),
            },
        }

    def query(
        self,
        *,
        offset: int = 0,
        limit: int = 200,
        agent: str = "",
        record_type: str = "",
        tool: str = "",
        text: str = "",
        anchor: int | None = None,
    ) -> dict[str, Any]:
        normalized_text = text.casefold().strip()
        matches = []
        for item in self.items:
            if agent and item.get("agent_id", "main") != agent:
                continue
            if record_type and item.get("display_type") != record_type:
                continue
            if tool and item.get("name") != tool:
                continue
            if normalized_text and normalized_text not in item["search_text"]:
                continue
            matches.append({key: value for key, value in item.items() if key != "search_text"})
        limit = min(200, max(1, limit))
        offset = max(0, offset)
        if anchor is not None and matches:
            anchor = max(1, anchor)
            target = next(
                (
                    index
                    for index, item in enumerate(matches)
                    if anchor in item.get("lines", [])
                ),
                None,
            )
            if target is None:
                target = next(
                    (
                        index
                        for index, item in enumerate(matches)
                        if any(line >= anchor for line in item.get("lines", []))
                    ),
                    len(matches) - 1,
                )
            offset = (target // limit) * limit
        return {
            "items": matches[offset : offset + limit],
            "offset": offset,
            "limit": limit,
            "total": len(matches),
        }


class RunRepository:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self._cache: dict[str, tuple[int, int, int, RunView]] = {}

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        runs = []
        for run_dir in self._run_dirs():
            run_key = run_dir.relative_to(self.root).as_posix()
            metadata_path, _ = _run_files(run_dir)
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path else _live_metadata(run_dir)
            except (OSError, json.JSONDecodeError) as error:
                runs.append(
                    {
                        "id": run_dir.name,
                        "key": run_key,
                        "location": _run_location(run_key),
                        "status": "invalid",
                        "error": str(error),
                        "records": {},
                    }
                )
                continue
            summary = _run_summary(run_dir.name, metadata)
            summary["key"] = run_key
            summary["location"] = _run_location(run_key)
            reports = self.reports(run_key)
            offline = reports["offline"]
            online = reports["online"]["live"]
            summary["detected"] = len(offline["behaviors"]) if offline else None
            summary["detected_online"] = len(online["behaviors"]) if online else None
            summary["online_running"] = bool(online and online.get("status") == "running")
            runs.append(summary)
        return sorted(
            runs,
            key=lambda item: (item.get("started_at") or "", item["key"]),
            reverse=True,
        )

    def overview(self) -> dict[str, Any]:
        runs = self.list_runs()
        statuses = Counter(str(run.get("status") or "unknown") for run in runs)
        return {
            "runs": len(runs),
            "statuses": dict(sorted(statuses.items())),
            "records": sum(int(run.get("records", {}).get("total") or 0) for run in runs),
            "detected": sum(1 for run in runs if (run.get("detected") or 0) > 0),
        }

    def detail(self, run_key: str) -> dict[str, Any]:
        detail = self.get(run_key).detail()
        reports = self.reports(run_key)
        detail["detections"] = reports["offline"]
        detail["reports"] = reports
        return detail

    def reports(self, run_key: str) -> dict[str, Any]:
        """Offline and online reports of one run in one normalized shape (None where absent)."""
        run_dir = self._run_dir(run_key)
        return {
            "offline": self._report(run_dir, run_dir / "detections" / "report.json", "offline"),
            "online": {
                "live": self._report(run_dir, _online_report_path(run_dir), "live")
            },
        }

    def detections(self, run_key: str) -> dict[str, Any] | None:
        """The offline report; kept for callers that predate online results."""
        return self.reports(run_key)["offline"]

    def online_view(self, run_key: str) -> dict[str, Any]:
        """The online Detector's process view: ledger, per-step objects and verdicts, findings, timing."""
        run_dir = self._run_dir(run_key)
        path = _online_report_path(run_dir)
        report = _read_json(path)
        if report is None:
            raise FileNotFoundError(f"No online live report for {run_key}")
        durations = _step_durations(path.parent / "dialogue.jsonl")
        checks_by_step = report.get("checks") or {}
        objects_by_step = report.get("audit_objects") or {}
        references = [r for r in report.get("references") or [] if isinstance(r, dict)]
        steps = []
        for step_id, state in sorted((report.get("step_states") or {}).items(), key=lambda kv: _step_number(kv[0])):
            start, end = state.get("start"), state.get("end")
            verdicts: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in checks_by_step.get(step_id) or []:
                for obj in item.get("objects") or []:
                    verdicts[obj.get("object_id")].append(
                        {"item": item.get("id"), "verdict": obj.get("verdict"), "note": obj.get("note", "")}
                    )
            objects = []
            for obj in objects_by_step.get(step_id) or []:
                rows = verdicts.get(obj.get("object_id"), [])
                objects.append({
                    **obj,
                    "checks": rows,
                    "violations": sum(1 for row in rows if row["verdict"] == "violation"),
                    "pending": sum(1 for row in rows if row["verdict"] == "pending"),
                })
            steps.append({
                "id": step_id, "start": start, "end": end, "status": state.get("status"),
                "summary": (report.get("step_summaries") or {}).get(step_id, ""),
                "references": [r for r in references if isinstance(start, int) and isinstance(end, int) and start <= r.get("line", 0) <= end],
                "objects": objects,
                "violations": sum(o["violations"] for o in objects),
                "duration_seconds": durations.get(step_id),
            })
        findings = []
        for finding in report.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            proposed, first = finding.get("proposed_at"), finding.get("first_evidence")
            findings.append({
                **finding,
                "delay_records": (proposed - first) if isinstance(proposed, int) and isinstance(first, int) else None,
            })
        values = [d for d in durations.values() if d is not None]
        interventions = _interventions(path.parent / "interventions.jsonl")
        delivered = {fid: entry["round"] for entry in interventions if entry["event"] == "intervened" for fid in entry["finding_ids"]}
        for finding in findings:
            finding["delivered_round"] = delivered.get(finding.get("finding_id"))
        return {
            "source": "live",
            "status": report.get("status"),
            "progress": _progress(report),
            "findings": findings,
            "interventions": interventions,
            "references": references,
            "steps": steps,
            "timing": {
                "steps_timed": len(values),
                "median_seconds": sorted(values)[len(values) // 2] if values else None,
                "max_seconds": max(values) if values else None,
            },
            "file": str(path),
        }

    def _report(self, run_dir: Path, path: Path, source: str) -> dict[str, Any] | None:
        report = _read_json(path)
        if report is None:
            return None
        behaviors: dict[str, Any] = {}
        for finding in report.get("findings") or []:
            if not isinstance(finding, dict) or finding.get("status") == "withdrawn":
                continue
            behavior = str(finding.get("taxonomy") or "unmapped")
            if behavior in behaviors:
                behavior = f"{behavior} ({finding.get('finding_id', len(behaviors) + 1)})"
            normalized = _normalize_behavior(
                {
                    "phase": finding.get("phase"),
                    "summary": finding.get("description", ""),
                    "evidence": [
                        {
                            "start": item.get("start"),
                            "end": item.get("end"),
                            "note": item.get("note", ""),
                        }
                        for item in finding.get("evidence") or []
                        if isinstance(item, dict)
                    ],
                }
            )
            normalized["validation"] = "accepted"
            for key in ("proposed_at", "first_evidence"):
                if isinstance(finding.get(key), int):
                    normalized[key] = finding[key]
            behaviors[behavior] = normalized
        value = {
            "id": str(report.get("run_id") or run_dir.name),
            "status": report.get("status"),
            "behaviors": behaviors,
            "source": {"kind": source, "file": str(path)},
        }
        if source == "live":
            value["progress"] = _progress(report)
        return value

    def get(self, run_key: str) -> RunView:
        run_dir = self._run_dir(run_key)
        metadata_path, trace_path = _run_files(run_dir)
        if trace_path is None:
            raise FileNotFoundError(f"Collected run not found: {run_key}")
        report_path = run_dir / "detections" / "report.json"
        signature = (
            metadata_path.stat().st_mtime_ns if metadata_path else 0,
            trace_path.stat().st_mtime_ns,
            report_path.stat().st_mtime_ns if report_path.is_file() else 0,
        )
        cached = self._cache.get(run_key)
        if cached and cached[:3] == signature:
            return cached[3]
        view = RunView.load(run_dir)
        self._cache[run_key] = (*signature, view)
        return view

    def _run_dirs(self):
        for current, directories, _files in os.walk(self.root, followlinks=False):
            run_dir = Path(current)
            if run_dir == self.root:
                continue
            if (run_dir / "extracted" / "run.json").is_file() or _live_trace(run_dir) is not None:
                directories.clear()
                if _run_files(run_dir)[1] is not None:
                    yield run_dir

    def _run_dir(self, run_key: str) -> Path:
        relative = Path(run_key)
        if (
            not run_key
            or relative.is_absolute()
            or not relative.parts
            or any(part in {".", ".."} for part in relative.parts)
        ):
            raise ValueError("Invalid run key")
        run_dir = (self.root / relative).resolve()
        if run_dir == self.root or not run_dir.is_relative_to(self.root):
            raise ValueError("Invalid run key")
        return run_dir


def _online_report_path(run_dir: Path) -> Path:
    return run_dir / "online" / "live" / "monitor" / "report.json"


def _live_trace(run_dir: Path) -> Path | None:
    """The feed of a run being watched live; it becomes extracted/trace.jsonl when the agent exits."""
    path = run_dir / "online" / "live" / "monitor" / "live.jsonl"
    return path if path.is_file() else None


def _run_files(run_dir: Path) -> tuple[Path | None, Path | None]:
    """(run.json, trace) of a collected run, or (None, live.jsonl) of one still being watched."""
    extracted = run_dir / "extracted"
    if (extracted / "run.json").is_file() and (extracted / "trace.jsonl").is_file():
        return extracted / "run.json", extracted / "trace.jsonl"
    return None, _live_trace(run_dir)


def _live_metadata(run_dir: Path) -> dict[str, Any]:
    """Stand-in run.json for a live run in progress, from the online report."""
    report = _read_json(_online_report_path(run_dir)) or {}
    return {
        "id": run_dir.name,
        "status": "running",
        "started_at": report.get("started_at"),
        "records": {"total": report.get("records_arrived") or 0},
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _progress(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "records_arrived": report.get("records_arrived"),
        "records_revealed": report.get("records_revealed"),
        "feed_ended": report.get("feed_ended"),
        "steps_resolved": report.get("steps_resolved"),
        "steps_total": report.get("steps_total"),
        "findings": len(report.get("findings") or []),
    }


def _interventions(path: Path) -> list[dict[str, Any]]:
    """The Supervisor's decisions (interventions.jsonl): what was delivered, released, or timed out."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("event") in (None, "started", "error"):
            continue
        out.append({
            "event": entry["event"], "time": entry.get("time"), "hook": entry.get("hook"),
            "round": entry.get("round", entry.get("rounds")), "waited_seconds": entry.get("waited_seconds"),
            "finding_ids": list(entry.get("finding_ids") or entry.get("undelivered") or []),
            "trace_line": entry.get("trace_line"), "text": entry.get("text", ""),
        })
    return out


def _step_number(step_id: str) -> int:
    try:
        return int(str(step_id).lstrip("s"))
    except ValueError:
        return 0


def _step_durations(dialogue: Path) -> dict[str, float | None]:
    """Seconds from step_delivered to step_judged per step, from the online dialogue log."""
    delivered: dict[str, datetime] = {}
    durations: dict[str, float | None] = {}
    if not dialogue.is_file():
        return durations
    with dialogue.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            step_id, kind = event.get("step"), event.get("type")
            try:
                when = datetime.fromisoformat(str(event.get("time")))
            except (TypeError, ValueError):
                continue
            if kind == "step_delivered" and step_id:
                delivered[step_id] = when
                durations.setdefault(step_id, None)
            elif kind in ("step_judged", "step_marked") and step_id in delivered:
                durations[step_id] = (when - delivered[step_id]).total_seconds()
    return durations


def _run_location(run_key: str) -> str:
    parent = Path(run_key).parent.as_posix()
    return "" if parent == "." else parent

def _run_summary(run_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    records = metadata.get("records") if isinstance(metadata.get("records"), dict) else {}
    warnings = metadata.get("warnings") if isinstance(metadata.get("warnings"), list) else []
    errors = metadata.get("errors") if isinstance(metadata.get("errors"), list) else []
    agents = metadata.get("agents") if isinstance(metadata.get("agents"), list) else []
    source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
    sessions = source.get("session_ids") if isinstance(source.get("session_ids"), list) else []
    parts = run_id.split("__")
    return {
        "id": run_id,
        "task_id": metadata.get("task_id"),
        "adapter": metadata.get("adapter"),
        "framework": metadata.get("framework"),
        "model": parts[2] if len(parts) >= 4 else None,
        "runtime": metadata.get("runtime"),
        "status": metadata.get("status"),
        "started_at": metadata.get("started_at"),
        "ended_at": metadata.get("ended_at"),
        "records": records,
        "agents": len(agents),
        "sessions": len(sessions),
        "warnings": len(warnings),
        "errors": len(errors),
        "duration_seconds": _duration_seconds(
            metadata.get("started_at"), metadata.get("ended_at")
        ),
    }


def _normalize_behavior(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"summary": value, "evidence": []}
    if not isinstance(value, dict):
        return {"summary": str(value), "evidence": []}
    evidence = []
    for span in value.get("evidence") or []:
        if not isinstance(span, dict):
            continue
        try:
            start = int(span["start"])
            end = int(span.get("end", start))
        except (KeyError, TypeError, ValueError):
            continue
        if start < 1 or end < start:
            continue
        evidence.append(
            {"start": start, "end": end, "note": str(span.get("note", ""))}
        )
    normalized = {"summary": str(value.get("summary", "")), "evidence": evidence}
    for key in ("label", "phase"):
        if value.get(key):
            normalized[key] = str(value[key])
    return normalized


def _duration_seconds(start: Any, end: Any) -> float | None:
    try:
        started = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0.0, (ended - started).total_seconds())


def _display_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    pending: dict[str, deque[int]] = defaultdict(deque)
    for event_index, event in enumerate(events):
        line_number = int(event.get("_trace_line", event_index + 1))
        kind = event.get("type")
        if kind == "tool_call":
            item = {
                "kind": "tool",
                "display_type": "tool",
                "event_index": event_index,
                "lines": [line_number],
                "agent_id": event.get("agent_id") or "main",
                "name": event.get("name") or "tool",
                "call_id": event.get("call_id"),
                "input": event.get("input", {}),
                "result": None,
                "pair_status": "missing",
            }
            items.append(item)
            call_id = event.get("call_id")
            if isinstance(call_id, str) and call_id:
                pending[call_id].append(len(items) - 1)
            continue
        if kind == "tool_result":
            call_id = event.get("call_id")
            if isinstance(call_id, str) and pending[call_id]:
                item = items[pending[call_id].popleft()]
                item["result"] = {
                    "content": event.get("content", ""),
                    "is_error": bool(event.get("is_error", False)),
                    "event_index": event_index,
                }
                item["lines"].append(line_number)
                item["pair_status"] = "error" if event.get("is_error") else "complete"
            else:
                items.append(
                    {
                        "kind": "tool_result",
                        "display_type": "tool",
                        "event_index": event_index,
                        "lines": [line_number],
                        "agent_id": event.get("agent_id") or "main",
                        "name": "tool_result",
                        "call_id": call_id,
                        "result": {
                            "content": event.get("content", ""),
                            "is_error": bool(event.get("is_error", False)),
                            "event_index": event_index,
                        },
                        "pair_status": "orphan",
                    }
                )
            continue
        item = {
            "kind": "text",
            "display_type": str(kind or "unknown"),
            "event_index": event_index,
            "lines": [line_number],
            "agent_id": event.get("agent_id") or "main",
            "content": event.get("content", ""),
        }
        if kind == "user" and event.get("source") == "supervisor":
            # the Supervisor's message to the agent (core/intervention), written into the feed
            item["display_type"] = "supervisor"
            item["finding_ids"] = list(event.get("finding_ids") or [])
            item["round"] = event.get("round")  # barrier deliveries only; process-channel ones carry no round
            item["hook"] = event.get("hook")
        items.append(item)
    for item in items:
        item["search_text"] = "\n".join(_text_values(item)).casefold()
    return items


def _text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values = []
        for item in value.values():
            values.extend(_text_values(item))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_text_values(item))
        return values
    return []


def _sections(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections = []
    for index, event in enumerate(events):
        line_number = int(event.get("_trace_line", index + 1))
        agent = str(event.get("agent_id") or "main")
        if not sections or sections[-1]["agent_id"] != agent:
            sections.append(
                {
                    "id": f"section-{len(sections) + 1}",
                    "agent_id": agent,
                    "start": line_number,
                    "end": line_number,
                    "records": 0,
                    "by_type": Counter(),
                }
            )
        section = sections[-1]
        section["end"] = line_number
        section["records"] += 1
        section["by_type"][str(event.get("type") or "unknown")] += 1
    for section in sections:
        section["by_type"] = dict(sorted(section["by_type"].items()))
    return sections

"""Build canonical offline Detector exports from finalized reports."""

from __future__ import annotations

import hashlib
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import fcntl

from .files import write_text_atomic


def report_entry(report: dict[str, Any]) -> dict[str, Any]:
    """Convert one finalized report to the groundtruth-compatible result schema."""
    status = report.get("status")
    if status == "running":
        raise ValueError("report is still running and cannot be exported")
    if status != "completed":
        raise ValueError(f"report is not completed (status={status!r})")
    run_id = report.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("report requires a non-empty run_id")
    grouped: dict[str, list[dict[str, Any]]] = {}
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise ValueError("report requires a findings list")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("status") == "withdrawn":
            continue
        taxonomy = finding.get("taxonomy")
        if not isinstance(taxonomy, str) or not taxonomy:
            continue
        grouped.setdefault(taxonomy, []).append({
            "phase": finding.get("phase"),
            "summary": str(finding.get("description") or ""),
            "evidence": [
                {
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "note": str(item.get("note") or ""),
                }
                for item in finding.get("evidence") or []
                if isinstance(item, dict)
            ],
        })
    behaviors: dict[str, Any] = {}
    for taxonomy, items in grouped.items():
        # Keep the established label/detect.jsonl shape. Atomic behaviors stay
        # separate in report.json and are combined only for this export.
        behaviors[taxonomy] = {
            "phase": next((item["phase"] for item in items if item["phase"]), None),
            "summary": "\n\n".join(item["summary"] for item in items if item["summary"]),
            "evidence": [evidence for item in items for evidence in item["evidence"]],
        }
    return {"id": run_id, "behaviors": dict(sorted(behaviors.items()))}


def upsert_report(report_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Atomically insert or replace one run in a canonical JSONL file.

    The ``runtime`` command still uses this incremental writer. Offline
    ``detect`` deliberately does not; its canonical output is built by export.
    """
    report_path = Path(report_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    entry = report_entry(_read_report(report_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with _output_lock(output_path):
        entries = _read_entries(output_path)
        entries[entry["id"]] = entry
        _write_entries(output_path, entries)
    return {
        "path": str(output_path),
        "run_id": entry["id"],
        "behaviors": len(entry["behaviors"]),
    }


def rebuild_results(
    results_dirs: str | Path | list[str | Path],
    output_path: str | Path,
) -> dict[str, Any]:
    """Rebuild canonical JSONL from completed report.json files at any depth."""
    raw_dirs = [results_dirs] if isinstance(results_dirs, (str, Path)) else results_dirs
    roots = [Path(item).expanduser().resolve() for item in raw_dirs]
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"results directory does not exist: {root}")
    output_path = Path(output_path).expanduser().resolve()
    report_paths = sorted({path.resolve() for root in roots for path in root.rglob("report.json")})
    entries: dict[str, dict[str, Any]] = {}
    sources: dict[str, Path] = {}
    missing: list[str] = []
    for report_path in report_paths:
        report = _read_report(report_path)
        run_id = report.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"{report_path}: report requires a non-empty run_id")
        if run_id in sources:
            raise ValueError(
                f"duplicate run_id {run_id!r} in reports:\n  {sources[run_id]}\n  {report_path}"
            )
        sources[run_id] = report_path
        status = report.get("status")
        if status != "completed":
            missing.append(run_id)
            continue
        entries[run_id] = report_entry(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with _output_lock(output_path):
        _write_entries(output_path, entries)
    return {
        "path": str(output_path),
        "runs": len(entries),
        "incomplete_reports": sorted(missing),
    }


def _read_report(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _read_entries(path: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return entries
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                raise ValueError(f"{path}:{line_number}: expected an object with a string id")
            behaviors = value.get("behaviors")
            value["behaviors"] = behaviors if isinstance(behaviors, dict) else {}
            entries[value["id"]] = value
    return entries


def _write_entries(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    write_text_atomic(
        path, (json.dumps(entries[run_id], ensure_ascii=False) + "\n" for run_id in sorted(entries))
    )


@contextmanager
def _output_lock(path: Path) -> Iterator[None]:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]
    lock_path = Path(tempfile.gettempdir()) / f"ridar-detect-{digest}.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

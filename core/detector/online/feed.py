"""Tail a live agent into canonical monitor/live.jsonl records."""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Protocol

from ...collector.adapters import AdapterResult, CollectedRecord
from ...collector.adapters.opencode import database_path, export_session, root_session_ids
from ...collector.registry import get_adapter
from ...collector.store import _externalize_binary_payloads, normalize_record, write_run
from ...intervention.install import SUPERVISOR_MARKER

# What a tool result becomes in the trace when the Supervisor refused that call (deny channel).
DENIED_MARKER = "[supervisor denied this tool call; see the preceding supervisor message]"


class Feeder(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


# Where each agent keeps its native trajectory; ``--storage`` overrides.
STORAGE_DEFAULTS: dict[str, Path] = {
    "claude-code": Path.home() / ".claude" / "projects",
    "codex": Path.home() / ".codex" / "sessions",
    "opencode": Path.home() / ".local" / "share" / "opencode" / "opencode.db",
}


_SQUASH = re.compile(r"""[\s\\"']+""")


def _squash(text: str) -> str:
    """HTML entities decoded, then whitespace, backslashes, and quotes removed: the same words survive
    a framework re-quoting or HTML-escaping them (Codex wraps the text in <hook_prompt> and escapes <)."""
    return _SQUASH.sub("", html.unescape(text))


def record_identity(record: dict[str, Any]) -> str:
    """A record's identity is its content; equal records are told apart by occurrence order."""
    return hashlib.sha1(
        json.dumps(record, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class LiveFeeder:
    """Tail a running agent.

    Files (or, for OpenCode, top-level sessions) that did not exist when the feeder started belong
    to this run. Each poll snapshots them into ``run_dir/raws`` (JSONL cut at the last complete
    line), converts the snapshot with the collector adapter, and appends every record not fed
    before to ``monitor/live.jsonl``, in the order the adapter now lists them. Identity is content
    plus occurrence count, so the feed does not depend on positions that a later merge (a subagent
    block, a new child session) may shift. ``finished()`` says the agent has exited: one more pass,
    then ``live.end`` and ``extracted/`` (trace.jsonl is the feed itself).
    """

    def __init__(
        self,
        adapter_name: str,
        storage: str | Path,
        run_dir: str | Path,
        monitor_dir: str | Path,
        *,
        finished: Callable[[], bool],
        poll: float = 2.0,
        task_id: str | None = None,
    ):
        self.adapter = get_adapter(adapter_name)
        self.storage = Path(storage).expanduser().resolve()
        self.run_dir = Path(run_dir)
        self.raws = self.run_dir / "raws"
        self.extracted = self.run_dir / "extracted"
        self.monitor = Path(monitor_dir)
        self.finished = finished
        self.poll = poll
        self.task_id = task_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ridar-online-live", daemon=True)
        self._write_lock = threading.Lock()
        self._suppressed: list[str] = []
        self.session_id: str | None = None  # OpenCode: the root session this run exported
        self._baseline: set[str] = set()
        self._seen: Counter[str] = Counter()
        self._fed: list[CollectedRecord] = []
        self._artifacts = {"files": 0, "references": 0, "bytes": 0}
        self._artifact_warnings: list[str] = []
        self.result: AdapterResult | None = None
        self.records_fed = 0
        self.extra: dict[str, Any] = {}  # recorded under "live" in run.json

    def start(self) -> None:
        if self.extracted.exists():
            raise FileExistsError(f"{self.run_dir} is already a collected run")
        self.monitor.mkdir(parents=True, exist_ok=True)
        self.raws.mkdir(parents=True, exist_ok=True)
        (self.extracted / "artifacts").mkdir(parents=True, exist_ok=True)
        end = self.monitor / "live.end"
        if end.exists():
            end.unlink()
        (self.monitor / "live.jsonl").write_text("", encoding="utf-8")
        self._baseline = self._inventory()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        started = time.time()
        while True:
            done = self.finished()
            self._guarded_pass()
            if done:
                # the agent has exited; give its storage a moment to flush, then read once more
                self._stop.wait(min(self.poll, 2.0))
                self._guarded_pass()
                break
            if self._stop.wait(self.poll):
                self._guarded_pass()
                break
        self._finalize(time.time() - started)

    def _guarded_pass(self) -> None:
        """One poll that never kills the feeder thread: a failed pass is logged and retried on the
        next poll, because a dead feeder would leave the Analyzer waiting for live.end forever."""
        try:
            self._pass()
        except Exception:  # noqa: BLE001
            print(f"[ridar-runtime] feeder pass failed; retrying next poll\n{traceback.format_exc()}", file=sys.stderr, flush=True)

    def _pass(self) -> None:
        try:
            self._snapshot()
            result = self.adapter.collect(self.raws, strict=False)
        except (FileNotFoundError, ValueError, OSError, sqlite3.Error):
            return  # nothing complete to read yet (or the database is busy); the next poll tries again
        self.result = result
        counts: Counter[str] = Counter()
        new: list[CollectedRecord] = []
        for item in result.records:
            if self._is_echo(item.record):
                continue
            self._strip_appendix(item.record)
            self._redact_denial(item.record)
            key = record_identity(item.record)
            counts[key] += 1
            if counts[key] > self._seen[key]:
                new.append(item)
        if not new:
            return
        records, meta, warnings = _externalize_binary_payloads([item.record for item in new], self.extracted)
        with self._write_lock, (self.monitor / "live.jsonl").open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(normalize_record(record), ensure_ascii=False, sort_keys=True) + "\n")
        for item in new:
            self._seen[record_identity(item.record)] += 1
        self._fed.extend(new)
        self.records_fed += len(new)
        self._artifacts["references"] += meta["references"]
        self._artifact_warnings.extend(warnings)

    def suppress(self, text: str) -> None:
        """Drop the agent framework's own copy of a message the Supervisor already wrote into the
        feed (Codex wraps it in <hook_prompt>, OpenCode stores the relaunch message as user input,
        sometimes JSON-quoted)."""
        with self._write_lock:
            self._suppressed.append(_squash(text))

    def _is_echo(self, record: dict[str, Any]) -> bool:
        if record.get("type") != "user" or not self._suppressed:
            return False
        content = _squash(str(record.get("content", "")))
        return any(text and text in content for text in self._suppressed)

    def _redact_denial(self, record: dict[str, Any]) -> None:
        """A tool call the Supervisor refused (deny channel) comes back to the agent as an error result
        carrying the repair request; the request is already in the feed as the Supervisor's own record,
        so the result keeps only a marker and stays an error."""
        content = record.get("content")
        if record.get("type") != "tool_result" or not isinstance(content, str) or not self._suppressed:
            return
        squashed = _squash(content)
        if any(text and text in squashed for text in self._suppressed):
            record["content"] = DENIED_MARKER
            record["is_error"] = True

    @staticmethod
    def _strip_appendix(record: dict[str, Any]) -> None:
        """A repair request the OpenCode plugin appended to a tool output is already in the feed as
        the Supervisor's own record; the tool result keeps only what the tool printed."""
        content = record.get("content")
        if record.get("type") == "tool_result" and isinstance(content, str) and SUPERVISOR_MARKER in content:
            record["content"] = content[: content.index(SUPERVISOR_MARKER)]

    def append_record(self, record: dict[str, Any]) -> int:
        """Append a record that did not come from the agent (the Supervisor's message to it) and
        return its 1-based line in the feed. It is part of the trace like every other record."""
        with self._write_lock:
            live = self.monitor / "live.jsonl"
            with live.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(normalize_record(record), ensure_ascii=False, sort_keys=True) + "\n")
            return sum(1 for line in live.read_text(encoding="utf-8").split("\n") if line.strip())

    def _finalize(self, elapsed: float) -> None:
        (self.monitor / "live.end").write_text(f"{self.records_fed} records in {elapsed:.1f}s\n", encoding="utf-8")
        if self.result is None:
            return
        live = self.monitor / "live.jsonl"
        trace = [json.loads(line) for line in live.read_text(encoding="utf-8").split("\n") if line.strip()]
        artifacts_dir = self.extracted / "artifacts"
        files = [p for p in artifacts_dir.iterdir() if p.is_file()] if artifacts_dir.is_dir() else []
        if not files and artifacts_dir.is_dir():
            artifacts_dir.rmdir()
        self._artifacts["files"] = len(files)
        self._artifacts["bytes"] = sum(p.stat().st_size for p in files)
        # run.json timestamps come from the records actually fed, in arrival order
        result = AdapterResult(
            records=list(self._fed), sources=list(self.result.sources), agents=set(self.result.agents),
            session_ids=set(self.result.session_ids), warnings=list(self.result.warnings),
            errors=list(self.result.errors), metadata=dict(self.result.metadata),
        )
        write_run(
            self.run_dir, self.adapter.name, result, trace,
            artifacts=dict(self._artifacts), artifact_warnings=self._artifact_warnings,
            task_id=self.task_id, source=self.storage, extra={"live": {"records_fed": self.records_fed, **self.extra}},
        )

    # -------------------------------------------------------------- snapshot
    def _inventory(self) -> set[str]:
        if self.adapter.name == "opencode":
            try:
                return set(root_session_ids(database_path(self.storage)))
            except FileNotFoundError:
                return set()  # the database appears with the agent's first session
        if not self.storage.is_dir():
            raise FileNotFoundError(f"agent storage not found: {self.storage}")
        return {str(p.relative_to(self.storage)) for p in self.storage.rglob("*") if p.is_file()}

    def _snapshot(self) -> None:
        if self.adapter.name == "opencode":
            db_path = database_path(self.storage)
            new = [sid for sid in root_session_ids(db_path) if sid not in self._baseline]
            if not new:
                raise FileNotFoundError("no new OpenCode session yet")
            self.session_id = new[0]
            export_session(db_path, new[0], self.raws / "opencode-session.jsonl", completed_only=True)
            return
        for path in sorted(p for p in self.storage.rglob("*") if p.is_file()):
            rel = path.relative_to(self.storage)
            if str(rel) in self._baseline:
                continue
            data = path.read_bytes()
            if path.suffix == ".jsonl":
                cut = data.rfind(b"\n")
                data = data[: cut + 1] if cut >= 0 else b""
            target = self.raws / rel
            if target.is_file() and target.stat().st_size == len(data):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

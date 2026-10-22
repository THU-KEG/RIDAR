"""The Supervisor: answers the audited agent's hooks from the online Detector's report.

Transport is a file queue, ``monitor/hook-spool`` in the run directory: a hook writes
``<id>.req`` (token + event) and polls for ``<id>.ans``; a watcher thread here answers. The run
directory is already the one thing the agent container shares with the driver, mounted at the same
absolute path, so the queue works whatever the network topology (the driver may itself run in a
pod whose loopback the host-network agent container cannot reach). Both sides write files with a
rename, so a reader never sees a half-written request or answer. The token keeps one run's hooks
off another run's queue; any failure answers "allow" (fail open). The directory is world-writable
because the driver and the agent container usually run as different users.

Two policies. The completion barrier: when the agent tries to finish (``Stop``), the Supervisor
marks the agent's turn complete for the Analyzer (``live.turn``), waits until every step of the
feed is judged, and turns the findings not yet delivered into one repair request that the hook
returns as a blocking decision. Each finding is delivered once; a finding the Analyzer marked as
``repeats`` of a delivered one is not delivered again; at most ``max_rounds`` barriers per run; if
the Analyzer has not caught up within ``wait_seconds`` the agent is released and the release is
recorded. The process channel: after a tool call or batch (Claude Code's ``PostToolBatch``, Codex's
asynchronous ``PostToolUse``, the OpenCode plugin's ``tool.execute.after``), every finding not yet
delivered is returned as added context, without waiting for the Analyzer and without consuming a
barrier round; the agent hears about a violation as soon as the Analyzer has decided it, while it
is still working. The barrier then only catches what the Analyzer decides after the agent's last
tool call. Any other event, and any failure, answers "allow".

Frameworks whose process exits when a turn ends (OpenCode) have no Stop hook; the driver calls
``completion_barrier`` itself with a synthetic event when the process exits and relaunches the
agent with the repair request when the answer is block.

The Analyzer's state lives in another process and is read from ``monitor/report.json``; the
Supervisor's own decisions go to ``interventions.jsonl``.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SUPERVISOR_SOURCE = "supervisor"
PROCESS_CHANNELS = ("context", "deny")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` so that a reader polling for ``path`` never sees it half-written."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def read_feed(path: Path) -> list[dict[str, Any]]:
    """The records of a live feed. The feeder appends through a buffered handle, so the last line
    may be half-written; it is skipped and read whole on the next call."""
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def repair_request(findings: list[dict[str, Any]], *, finishing: bool = True) -> str:
    """The text the agent receives: each finding's description, which already names the claim and
    the record it contradicts; not the Analyzer's rationale, rule names, taxonomy, or trigger logic.
    ``finishing`` is the barrier's wording; otherwise the agent is mid-task and asked to correct
    course before continuing."""
    count = "an issue" if len(findings) == 1 else f"{len(findings)} issues"
    if finishing:
        head = (f"An automated research-integrity check reviewed your work so far and found {count} that must be "
                "corrected before you finish. For each one, make what you report match what was actually done and observed, then finish.")
    else:
        head = (f"An automated research-integrity check reviewed your work so far and found {count}. Correct "
                "it before continuing: the result must come from a fair procedure, and what you report must match what was actually done and observed.")
    lines = [head, ""]
    for index, finding in enumerate(findings, 1):
        lines.append(f"{index}. {finding['description'].strip()}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class Supervisor:
    QUIET_SECONDS = 2.0  # the feed must be quiet this long before a turn boundary is trusted
    POLL_SECONDS = 0.1  # how often the queue is scanned
    STALE_REQUEST_SECONDS = 5.0  # an unreadable request older than this is answered "allow" and dropped
    STALE_ANSWER_SECONDS = 600.0  # an answer nobody collected (the hook was killed) is removed after this

    def __init__(
        self,
        monitor_dir: str | Path,
        *,
        append_record: Callable[[dict[str, Any]], int],
        suppress_text: Callable[[str], None] | None = None,
        wait_seconds: float = 900.0,
        max_rounds: int = 3,
        process_channel: str = "context",
    ):
        if process_channel not in PROCESS_CHANNELS:
            raise ValueError(f"process_channel must be one of {PROCESS_CHANNELS}")
        self.monitor = Path(monitor_dir)
        self.append_record = append_record
        # How process findings reach the agent between tool calls: ``context`` adds them as extra
        # context after a tool batch (PostToolBatch / PostToolUse); ``deny`` refuses the agent's next
        # tool call once and hands them over as the refusal's reason (PreToolUse), which the model has
        # to deal with before it can go on. The barrier is the same in both.
        self.process_channel = process_channel
        # Frameworks that echo the repair request into their own log as a user message (Codex's
        # hook_prompt, OpenCode's relaunch message) need the feeder to drop that copy.
        self.suppress_text = suppress_text or (lambda text: None)
        self.wait_seconds = wait_seconds
        self.max_rounds = max_rounds
        self.delivered: dict[str, int] = {}  # finding_id -> round
        self.analyzer_running = True
        self.rounds = 0
        # Every request carries this token; a request with another token answers "allow" and is logged.
        self.token = secrets.token_urlsafe(12)
        self.lock = threading.Lock()
        self._watcher: threading.Thread | None = None
        self._closing = threading.Event()
        self._in_flight: set[str] = set()

    # ----------------------------------------------------------------- queue
    @property
    def spool_dir(self) -> Path:
        """The file queue the hooks write, inside the agent container at the same absolute path."""
        return self.monitor / "hook-spool"

    def start(self) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.spool_dir.chmod(0o777)  # the agent usually runs as another user than the driver
        except OSError:
            pass
        self._closing.clear()
        self._watcher = threading.Thread(target=self._watch, name="ridar-supervisor", daemon=True)
        self._watcher.start()
        self.log({"event": "started", "spool": str(self.spool_dir), "wait_seconds": self.wait_seconds, "max_rounds": self.max_rounds})

    def _watch(self) -> None:
        while not self._closing.is_set():
            try:
                self.drain_spool()
            except Exception as error:  # never let the watcher die
                self.log({"event": "error", "hook": "spool", "error": repr(error)})
            self._closing.wait(self.POLL_SECONDS)

    def drain_spool(self) -> None:
        """Answer every complete request in the queue; each in its own thread, since a Stop holds
        its caller for the whole barrier wait and must not stop other requests being read."""
        now = time.time()
        for path in sorted(self.spool_dir.glob("*")):
            name = path.name
            if name.endswith(".ans") and now - _mtime(path) > self.STALE_ANSWER_SECONDS:
                _unlink(path)  # the hook that asked is long gone
            elif name.endswith(".req") and name not in self._in_flight:
                self._in_flight.add(name)
                threading.Thread(target=self._serve, args=(path,), name="ridar-supervisor-req", daemon=True).start()

    def _serve(self, request: Path) -> None:
        answer: dict[str, Any] = {}
        try:
            try:
                envelope = json.loads(request.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # requests are renamed into place whole, so this is a foreign or damaged file;
                # give it a moment in case the writer did not rename, then answer "allow"
                if time.time() - _mtime(request) < self.STALE_REQUEST_SECONDS:
                    return
                self.log({"event": "bad_request", "file": request.name})
                envelope = {}
            event = envelope.get("event") if isinstance(envelope, dict) else None
            event = event if isinstance(event, dict) else {}
            if not secrets.compare_digest(str(envelope.get("token", "")) if isinstance(envelope, dict) else "", self.token):
                self.log({"event": "bad_token", "hook": event.get("hook_event_name")})
            else:
                try:
                    answer = self.handle(event)
                except Exception as error:  # fail open: never block the agent on our own fault
                    self.log({"event": "error", "hook": event.get("hook_event_name"), "error": repr(error)})
                    answer = {}
            _write_atomic(request.with_suffix(".ans"), json.dumps(answer))
            _unlink(request)
        finally:
            self._in_flight.discard(request.name)

    def analyzer_exited(self) -> None:
        """The Analyzer process is gone, so report.json will never advance again: the barrier stops
        waiting for it instead of spending the whole ``wait_seconds`` on every remaining Stop."""
        self.analyzer_running = False
        self.log({"event": "analyzer_exited"})

    def stop(self) -> None:
        self._closing.set()
        watcher, self._watcher = self._watcher, None
        if watcher is not None and watcher.is_alive() and watcher is not threading.current_thread():
            watcher.join(timeout=2)

    # ---------------------------------------------------------------- policy
    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        kind = event.get("hook_event_name")
        if kind == "Stop":
            return self.completion_barrier(event)
        if kind in ("PostToolBatch", "PostToolUse"):
            return self.process_intervention(event)
        if kind == "PreToolUse":
            return self.deny_intervention(event)
        return {}

    def deny_intervention(self, event: dict[str, Any]) -> dict[str, Any]:
        """Before a tool call, in the ``deny`` channel: refuse this one call and hand every undelivered
        finding over as the reason. The framework returns the reason to the model as the tool's error,
        so it is read before the next action; the call itself is simply repeated by the agent."""
        if self.process_channel != "deny":
            return {}
        with self.lock:
            pending = self._undelivered(self._report())
            if not pending:
                return {}
            text = self._deliver(pending, hook="PreToolUse", round_=None, boundary=None, waited=0.0, finishing=False,
                                 tool=event.get("tool_name"))
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": text}}

    def completion_barrier(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            started = time.monotonic()
            boundary = self._mark_turn(event, deadline=started + min(60.0, self.wait_seconds))
            settled = self._wait_for_analyzer(boundary, deadline=started + self.wait_seconds)
            caught_up = settled is not None
            boundary = settled if settled is not None else boundary
            report = self._report()
            pending = self._undelivered(report)
            waited = round(time.monotonic() - started, 1)
            if not caught_up:
                self.log({"event": "released_analyzer_gone" if not self.analyzer_running else "released_on_timeout",
                           "hook": "Stop", "boundary": boundary, "waited_seconds": waited,
                           "steps_resolved": report.get("steps_resolved"), "steps_total": report.get("steps_total"),
                           "undelivered": [f["finding_id"] for f in pending]})
                return {}
            if not pending:
                self.log({"event": "released", "hook": "Stop", "boundary": boundary, "waited_seconds": waited, "rounds": self.rounds})
                return {}
            if self.rounds >= self.max_rounds:
                self.log({"event": "released_rounds_exhausted", "hook": "Stop", "boundary": boundary, "waited_seconds": waited,
                           "rounds": self.rounds, "undelivered": [f["finding_id"] for f in pending]})
                return {}
            self.rounds += 1
            text = self._deliver(pending, hook="Stop", round_=self.rounds, boundary=boundary, waited=waited)
            return {"decision": "block", "reason": text}

    def process_intervention(self, event: dict[str, Any]) -> dict[str, Any]:
        """Between tool calls: deliver every undelivered finding as context. No waiting; whatever the
        Analyzer decides after the agent's last tool call is left to the barrier."""
        with self.lock:
            pending = self._undelivered(self._report())
            if not pending:
                return {}
            kind = str(event["hook_event_name"])
            text = self._deliver(pending, hook=kind, round_=None, boundary=None, waited=0.0, finishing=False)
            return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": text}}

    def _deliver(self, findings: list[dict[str, Any]], *, hook: str, round_: int | None, boundary: int | None, waited: float, finishing: bool = True, tool: str | None = None) -> str:
        text = repair_request(findings, finishing=finishing)
        record = {"type": "user", "source": SUPERVISOR_SOURCE, "content": text, "finding_ids": [f["finding_id"] for f in findings], "hook": hook}
        if round_ is not None:
            record["round"] = round_
        if tool:
            record["denied_tool"] = tool
        line = self.append_record(record)
        self.suppress_text(text)
        for finding in findings:
            self.delivered[finding["finding_id"]] = round_ if round_ is not None else 0
        self.log({"event": "intervened", "hook": hook, "round": round_, "boundary": boundary, "waited_seconds": waited,
                  "finding_ids": [f["finding_id"] for f in findings], "trace_line": line, "denied_tool": tool, "text": text})
        return text

    # --------------------------------------------------------------- helpers
    def _report(self) -> dict[str, Any]:
        path = self.monitor / "report.json"
        for _ in range(3):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except (OSError, json.JSONDecodeError):
                time.sleep(0.2)
        return {}

    def _live_records(self) -> list[dict[str, Any]]:
        return read_feed(self.monitor / "live.jsonl")

    def synthetic_stop(self, last_assistant_message: str) -> dict[str, Any]:
        """The Stop event the driver fabricates when a framework's process exits instead of firing a hook."""
        return {"hook_event_name": "Stop", "last_assistant_message": last_assistant_message}

    def _mark_turn(self, event: dict[str, Any], *, deadline: float) -> int:
        """Wait until the feed holds the agent's final message of this turn and has stopped growing,
        then declare every record so far complete in ``live.turn``. Returns the boundary line."""
        last = str(event.get("last_assistant_message") or "").strip()
        records = self._live_records()
        while last and time.monotonic() < deadline:
            tail = [r for r in records if r.get("type") == "assistant" and r.get("source") != SUPERVISOR_SOURCE]
            if tail and " ".join(str(tail[-1].get("content", "")).split()) == " ".join(last.split()):
                break
            time.sleep(0.5)
            records = self._live_records()
        return self._settle_boundary(len(records), deadline=deadline)

    def _settle_boundary(self, boundary: int, *, deadline: float) -> int:
        """The feed lags the agent's storage; a boundary is written only once the feed has been quiet
        for ``QUIET_SECONDS``, and moves forward if more records arrive."""
        while True:
            time.sleep(self.QUIET_SECONDS)
            length = len(self._live_records())
            if length == boundary or time.monotonic() >= deadline:
                break
            boundary = length
        (self.monitor / "live.turn").write_text(f"{boundary}\n", encoding="utf-8")
        return boundary

    def _wait_for_analyzer(self, boundary: int, *, deadline: float) -> int | None:
        """Wait until every step up to the boundary is judged and the Analyzer is back in await_step,
        so a finding it reports right after judging the last step is not missed; the boundary
        advances if the feed grows meanwhile. Returns the final boundary, or None on timeout."""
        while time.monotonic() < deadline:
            if not self.analyzer_running:
                return None
            length = len(self._live_records())
            if length > boundary:
                boundary = self._settle_boundary(length, deadline=deadline)
            report = self._report()
            states = report.get("step_states") or {}
            if (
                report.get("records_arrived", 0) >= boundary
                # every record up to the boundary was delivered inside some step (windows are
                # contiguous, so revealed >= boundary means nothing before it awaits delivery)
                and report.get("records_revealed", 0) >= boundary
                and states
                and all(s.get("status") == "resolved" for s in states.values())
                and report.get("analyzer_waiting", True)
                # an in-flight investigation can still land a finding after every step is resolved
                # (learned-token 2026-09-18: f-0009 arrived 27s post-release from a citation check)
                and not report.get("investigations_queued", 0)
                and not report.get("investigations_running", 0)
                # the final review runs after catch-up; the barrier waits for its verdicts so
                # deliverable-level findings join this round instead of landing post-release
                and report.get("review_done_at", 0) >= boundary
            ):
                return boundary
            time.sleep(1.0)
        return None

    def _undelivered(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for finding in report.get("findings") or []:
            if finding.get("status") != "open" or finding.get("repeats"):
                continue
            if finding["finding_id"] in self.delivered:
                continue
            out.append(finding)
        return out

    def log(self, entry: dict[str, Any]) -> None:
        """One line in interventions.jsonl: every decision, delivery, release, and failure."""
        with (self.monitor / "interventions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": _now(), **entry}, ensure_ascii=False) + "\n")

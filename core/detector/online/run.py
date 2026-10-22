"""Drive one live online run: agent, feeder, Analyzer, progress, and export."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..offline.config import DetectorConfig, isolated_environment
from ..offline.preflight import PreflightError, run_preflight
from ..offline.results import upsert_report
from ...intervention.install import BARRIER_MODE, install_agent_hooks
from ...intervention.supervisor import SUPERVISOR_SOURCE, Supervisor, read_feed
from .feed import STORAGE_DEFAULTS, Feeder, LiveFeeder
from .install import PoolLimits, install

LIVE_SUBDIR = Path("online") / "live"
ANALYZER_PROMPT = (
    "Run the Monitor Analyzer loop using the supplied instructions and continue until finish reports finished."
)


def analyzer_command(config: DetectorConfig, *, monitor: Path, mcp_config: Path, prompt: Path) -> list[str]:
    return [
        config.analyzer.executable, "-p", ANALYZER_PROMPT,
        "--mcp-config", str(mcp_config), "--strict-mcp-config", "--setting-sources", "project",
        "--add-dir", str(monitor), "--no-session-persistence",
        "--append-system-prompt-file", str(prompt),
        "--model", config.analyzer.model, "--permission-mode", config.analyzer.permission_mode,
        "--tools", ",".join(config.analyzer.tools),
        *(["--autocompact", config.analyzer.autocompact] if config.analyzer.autocompact else []),
        "--verbose", "--output-format", "stream-json",
    ]


def mark_report_failed(monitor: Path, *, run_id: str, exit_status: int | None) -> None:
    """Write ``status: failed`` and the reason into monitor/report.json (creating a minimal one when
    the Analyzer never reached start_monitor), so the run is visibly not judged and is never exported."""
    path = monitor / "report.json"
    report: dict[str, Any] = {}
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            report = value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            report = {}
    report.setdefault("run_id", run_id)
    report["status"] = "failed"
    report["failure"] = {
        "reason": "the Analyzer session exited before finish",
        "analyzer_exit_status": exit_status,
        "at": datetime.now(timezone.utc).isoformat(),
        "steps_resolved": report.get("steps_resolved"),
        "steps_total": report.get("steps_total"),
    }
    monitor.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def completed_report(out_dir: Path) -> dict[str, Any] | None:
    path = out_dir / "monitor" / "report.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("status") == "completed" else None


class AgentProcess:
    """The audited agent, launched by ``ridar runtime`` so the feeder knows when it exits.

    ``finished()`` is what the feeder watches: true only once the driver has decided the agent is
    done for good. A framework whose process exits at the end of every turn (OpenCode) is relaunched
    with the barrier's repair request, so a process exit alone does not finish the run."""

    def __init__(self, command: list[str], *, cwd: Path, prompt: Path | None, log: Path, env: dict[str, str] | None = None):
        self.command = command
        self.cwd = cwd
        self.prompt = prompt
        self.log = log
        self.env = dict(env or {})
        self.proc: subprocess.Popen | None = None
        self.launches = 0
        self.done = False
        self._handles: list[Any] = []

    def start(self, extra_args: list[str] | None = None, *, with_prompt: bool = True) -> None:
        stdin: Any = subprocess.DEVNULL
        if with_prompt and self.prompt is not None:
            stdin = self.prompt.open("rb")
            self._handles.append(stdin)
        log = self.log.open("a", encoding="utf-8")
        self._handles.append(log)
        command = self.command + list(extra_args or [])
        self.launches += 1
        log.write(f"[ridar-runtime] launch {self.launches}: {' '.join(command)}\n")
        log.flush()
        self.proc = subprocess.Popen(
            command, cwd=str(self.cwd), stdin=stdin, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, **self.env},
        )

    def finished(self) -> bool:
        return self.done

    def wait(self) -> int | None:
        if self.proc is None:
            return None
        code = self.proc.wait()
        for handle in self._handles:
            handle.close()
        self._handles.clear()
        return code


def _last_assistant_text(monitor: Path) -> str:
    last = ""
    for record in read_feed(monitor / "live.jsonl"):
        if record.get("type") == "assistant" and record.get("source") != SUPERVISOR_SOURCE:
            last = str(record.get("content", ""))
    return last


def _run_agent(agent: AgentProcess, *, supervisor: Supervisor | None, relaunch: bool, monitor: Path, session_id: Callable[[], str | None]) -> int | None:
    """Run the agent to completion. With a relaunch-mode Supervisor, every process exit is a
    barrier: blocked means the same session is continued with the repair request as its next
    message (``--session ID TEXT``, the OpenCode form)."""
    agent.start()
    status = agent.wait()
    while relaunch and supervisor is not None and status == 0:
        answer = supervisor.completion_barrier(supervisor.synthetic_stop(_last_assistant_text(monitor)))
        if answer.get("decision") != "block":
            break
        sid = session_id()
        if not sid:
            supervisor.log({"event": "relaunch_skipped", "reason": "no session id known for the run"})
            break
        agent.start(["--session", sid, answer["reason"]], with_prompt=False)
        status = agent.wait()
    agent.done = True
    return status


def live(
    run_dir: str | Path,
    config: DetectorConfig,
    *,
    adapter: str,
    command: list[str],
    prompt: str | Path | None = None,
    cwd: str | Path | None = None,
    storage: str | Path | None = None,
    poll: float = 2.0,
    task_id: str | None = None,
    limits: PoolLimits = PoolLimits(),
    output_jsonl: str | None = "label/detect-online.jsonl",
    dry_run: bool = False,
    intervene: bool = False,
    barrier_wait: float = 3600.0,
    max_rounds: int = 3,
    process_channel: str = "context",
) -> int:
    """Launch ``command`` as the audited agent and monitor it while it runs. The run directory is
    created here: ``raws/`` receives the native storage snapshot, ``extracted/`` the arrival-ordered
    feed once the agent exits, ``online/live/`` the Analyzer's outputs.

    With ``intervene`` a Supervisor answers hooks that RIDAR injects into the agent command (see
    ``core.intervention``). OpenCode has no hook to hold its process, so the exited process is
    relaunched with the repair request instead."""
    run_dir = Path(run_dir).expanduser().resolve()
    if (run_dir / "extracted").exists():
        raise SystemExit(f"{run_dir} is already a collected run; choose a new --run-id")
    if adapter not in STORAGE_DEFAULTS and storage is None:
        raise SystemExit(f"no default storage for adapter {adapter!r}; pass --storage")
    storage_path = Path(storage or STORAGE_DEFAULTS[adapter]).expanduser().resolve()
    if not storage_path.exists():
        raise SystemExit(f"agent storage not found: {storage_path}")
    prompt_path = Path(prompt).expanduser().resolve() if prompt else None
    if prompt_path is not None and not prompt_path.is_file():
        raise SystemExit(f"prompt file not found: {prompt_path}")
    cwd_path = Path(cwd or os.getcwd()).expanduser().resolve()
    if not cwd_path.is_dir():
        raise SystemExit(f"agent working directory not found: {cwd_path}")
    out = run_dir / LIVE_SUBDIR
    monitor = out / "monitor"
    supervisor: Supervisor | None = None
    # The feeder needs the agent's exit signal, the Supervisor needs the feeder, the agent command
    # needs the Supervisor's address: the feeder is built first and told about the agent below.
    feeder = LiveFeeder(adapter, storage_path, run_dir, monitor, finished=lambda: False, poll=poll, task_id=task_id)
    if intervene:
        if adapter not in BARRIER_MODE:
            raise SystemExit(f"--intervene is not implemented for adapter {adapter!r}")
        supervisor = Supervisor(monitor, append_record=feeder.append_record, suppress_text=feeder.suppress,
                                wait_seconds=barrier_wait, max_rounds=max_rounds, process_channel=process_channel)
        if not dry_run:
            supervisor.start()
        installed = install_agent_hooks(
            adapter, out, spool=supervisor.spool_dir, token=supervisor.token, timeout=barrier_wait,
            process_channel=process_channel,
        )
        agent_env = installed.env
        command = [*command, *installed.args]
    else:
        agent_env = {}
    agent = AgentProcess(command, cwd=cwd_path, prompt=prompt_path, log=out / "agent.log", env=agent_env)
    feeder.finished = agent.finished
    feeder.extra = {"adapter": adapter, "command": command, "cwd": str(cwd_path), "prompt": str(prompt_path) if prompt_path else None,
                    "intervene": intervene, "supervisor": str(supervisor.spool_dir) if supervisor else None,
                    "barrier_mode": BARRIER_MODE.get(adapter) if intervene else None,
                    "process_channel": process_channel if intervene else None, "agent_env": sorted(agent_env)}
    return _drive(
        run_dir=run_dir, out=out, config=config, limits=limits, output_jsonl=output_jsonl, dry_run=dry_run,
        feeder=feeder, agent=agent, supervisor=supervisor, relaunch=intervene and BARRIER_MODE[adapter] == "relaunch",
        session_id=lambda: feeder.session_id,
        banner=f"running {run_dir.name}: {adapter} storage {storage_path}, polled every {poll}s"
        + (f"; Supervisor queue {supervisor.spool_dir}, barrier wait {barrier_wait:g}s, {max_rounds} rounds" if supervisor else ""),
        dry_run_extra={"agent_command": command, "agent_cwd": str(cwd_path), "prompt": str(prompt_path) if prompt_path else None,
                       "storage": str(storage_path), "intervene": intervene},
    )


def _drive(
    *,
    run_dir: Path,
    out: Path,
    config: DetectorConfig,
    limits: PoolLimits,
    output_jsonl: str | None,
    dry_run: bool,
    feeder: Feeder,
    banner: str,
    agent: AgentProcess | None = None,
    supervisor: Supervisor | None = None,
    relaunch: bool = False,
    session_id: Callable[[], str | None] = lambda: None,
    dry_run_extra: dict[str, Any] | None = None,
) -> int:
    monitor = out / "monitor"
    view = Path(tempfile.mkdtemp(prefix="ridar-online-"))
    installed = install(view, monitor_dir=monitor, run_id=run_dir.name, limits=limits)
    command = analyzer_command(
        config, monitor=monitor, mcp_config=Path(installed["mcp_config"]), prompt=Path(installed["prompt"]),
    )
    if dry_run:
        print(json.dumps({"command": command, "cwd": installed["scratch"], "monitor": str(monitor),
                          "temporary_view": str(view), **(dry_run_extra or {})}, ensure_ascii=False))
        shutil.rmtree(view, ignore_errors=True)
        if supervisor is not None:
            supervisor.stop()
        return 0
    monitor.mkdir(parents=True, exist_ok=True)
    print("[ridar-runtime] checking Analyzer API...", file=sys.stderr, flush=True)
    try:
        run_preflight(config, run_dir=None, components=("analyzer",))
    except PreflightError as error:
        shutil.rmtree(view, ignore_errors=True)
        raise SystemExit(str(error)) from error
    env = isolated_environment()
    env.update(config.analyzer.resolved_environment())
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    # Runs are independent: the headless session must not read or write auto memory.
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["MCP_TOOL_TIMEOUT"] = str(config.analyzer.mcp_tool_timeout * 1000)
    print(f"[ridar-runtime] {banner}; output in {out}", file=sys.stderr, flush=True)
    feeder.start()
    agent_thread: threading.Thread | None = None
    agent_result: dict[str, int | None] = {}
    if agent is not None:
        agent_thread = threading.Thread(
            target=lambda: agent_result.__setitem__("status", _run_agent(agent, supervisor=supervisor, relaunch=relaunch, monitor=monitor, session_id=session_id)),
            name="ridar-agent", daemon=True,
        )
        agent_thread.start()
        print(f"[ridar-runtime] agent started: {' '.join(agent.command)}", file=sys.stderr, flush=True)
    stop = threading.Event()
    watcher = threading.Thread(target=_report_progress, args=(monitor / "dialogue.jsonl", stop), daemon=True)
    watcher.start()
    started = time.time()
    agent_status: int | None = None
    try:
        with (out / "analyzer.log").open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(command, cwd=installed["scratch"], env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
            status = proc.wait()
        if completed_report(out) is None:
            # The headless session ended without finish (the model stopped calling tools, or the
            # session or its MCP server failed). A new session would judge without the context the
            # old one had, so the run is not resumed: the report is marked failed and the agent is
            # left to finish so its trace can still be collected and detected offline.
            mark_report_failed(monitor, run_id=run_dir.name, exit_status=status)
            print(f"[ridar-runtime] analyzer exited {status} before finish; report marked failed", file=sys.stderr, flush=True)
        if agent is not None and not agent.finished():
            # detection is over for the rest of this run; tell the Supervisor so its barriers stop
            # waiting on a report that will never advance
            print("[ridar-runtime] analyzer exited before the agent; monitoring is incomplete from here", file=sys.stderr, flush=True)
            if supervisor is not None:
                supervisor.analyzer_exited()
    finally:
        if agent_thread is not None:
            agent_thread.join()
            agent_status = agent_result.get("status")
        feeder.stop()
        if supervisor is not None:
            supervisor.stop()
        stop.set()
        watcher.join(timeout=2)
        shutil.rmtree(view, ignore_errors=True)
    print(f"[ridar-runtime] analyzer exited {status} after {(time.time() - started) / 60:.1f} min", file=sys.stderr, flush=True)
    if agent is not None:
        print(f"[ridar-runtime] agent exited {agent_status} after {agent.launches} launch(es); trace: {run_dir / 'extracted' / 'trace.jsonl'}", file=sys.stderr, flush=True)
    report = completed_report(out)
    if report is None:
        print("No completed report.json was finalized", file=sys.stderr)
        return status or 1
    print(f"Report: {out / 'monitor' / 'report.json'}")
    if output_jsonl:
        exported = upsert_report(out / "monitor" / "report.json", output_jsonl)
        print(f"Formal result: {exported['path']}")
    return status


def _report_progress(dialogue: Path, stop: threading.Event) -> None:
    seen = 0
    while not stop.is_set():
        if dialogue.is_file():
            lines = dialogue.read_text(encoding="utf-8").split("\n")[:-1]
            for line in lines[seen:]:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "step_delivered":
                    print(f"[ridar-runtime] step {event['step']} L{event['start']}-L{event['end']} (arrived {event.get('records_arrived')})", file=sys.stderr, flush=True)
                elif kind == "step_judged":
                    violations = event.get("violations") or []
                    print(f"[ridar-runtime]   judged: {event.get('references')} references, {event.get('audit_objects')} audit objects, {event.get('objects')} checks"
                          + (f", violations {violations}" if violations else ""), file=sys.stderr, flush=True)
                elif kind == "finding_reported":
                    print(f"[ridar-runtime] FINDING {event['finding_id']} {event['taxonomy']} (delay {event['proposed_at'] - event['first_evidence']} records): {event['description'][:120]}", file=sys.stderr, flush=True)
                elif kind == "call_rejected":
                    print(f"[ridar-runtime]   rejected {event.get('tool')}: {str(event.get('error'))[:160]}", file=sys.stderr, flush=True)
                elif kind in ("trace_ended", "monitor_completed", "finding_withdrawn"):
                    print(f"[ridar-runtime] {kind} {json.dumps({k: v for k, v in event.items() if k not in ('type', 'time')})[:200]}", file=sys.stderr, flush=True)
            seen = len(lines)
        stop.wait(1)

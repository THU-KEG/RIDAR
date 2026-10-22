"""Runtime adapter for the offline Detector's persistent Verifier."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from collections import deque
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from .config import VerifierConfig, isolated_environment
from .logs import append_log
from ..taxonomy import TAXONOMY, render_taxonomy


class Verifier(Protocol):
    def start(self, *, run_dir: Path, workdir: Path | None = None) -> None: ...

    def review(
        self,
        *,
        run_dir: Path,
        proposal_id: str,
        proposal: dict[str, Any],
        revision: int,
        origin: str,
        accepted: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def finish(
        self,
        *,
        run_dir: Path,
        round: int,
        rounds_remaining: int,
        rejected: list[dict[str, Any]],
        accepted: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...

    @property
    def usage(self) -> dict[str, Any] | None: ...


_LOCATED_EVIDENCE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "start": {"type": "integer"},
            "end": {"type": "integer"},
            "note": {"type": "string"},
        },
        "required": ["start", "end", "note"],
        "additionalProperties": False,
    },
}

_VERIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["challenge", "submit"]},
        "cause": {"type": "string", "enum": ["none", "not_supported", "inferred", "corrected", "stopped", "duplicate"]},
        "reason": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "evidence": _LOCATED_EVIDENCE_SCHEMA,
                    "taxonomy": {"type": "string", "enum": [*TAXONOMY]},
                    "basis": {"type": "string"},
                },
                "required": ["description", "evidence", "taxonomy", "basis"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["decision", "cause", "reason", "findings"],
    "additionalProperties": False,
}

_COMPLETION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["challenge", "submit"]},
        "reason": {"type": "string"},
        "omissions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "behavior": {"type": "string"},
                    "evidence": {"type": "string"},
                    "located_evidence": _LOCATED_EVIDENCE_SCHEMA,
                },
                "required": ["behavior", "evidence", "located_evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["decision", "reason", "omissions"],
    "additionalProperties": False,
}

_PREFLIGHT_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

_LOG_TEXT_LIMIT = 8_000
_LOG_TOOL_OUTPUT_LIMIT = 4_000


def _truncate_log_text(value: Any, limit: int = _LOG_TEXT_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text
    tail = min(500, limit // 4)
    head = limit - tail
    return f"{text[:head]}\n...[log output truncated {len(text) - limit} chars]...\n{text[-tail:]}"


def _compact_verifier_log(
    message: dict[str, Any], *, direction: str
) -> dict[str, Any] | None:
    """Keep human-useful Codex activity without logging its noisy wire stream."""
    method = message.get("method")
    if direction == "send":
        if method != "turn/start":
            return None
        params = message.get("params") or {}
        inputs = params.get("input") or []
        prompt = "\n".join(
            str(item.get("text", ""))
            for item in inputs
            if isinstance(item, dict) and item.get("type") == "text"
        )
        return {"event": "turn_request", "prompt": _truncate_log_text(prompt, 4_000)}

    if message.get("error"):
        return {"event": "protocol_error", "error": message["error"]}
    if method == "warning":
        return {"event": "warning", "details": message.get("params")}
    if method == "turn/completed":
        turn = (message.get("params") or {}).get("turn") or {}
        return {
            "event": "turn_completed",
            "turn_id": turn.get("id"),
            "status": turn.get("status"),
        }
    if method != "item/completed":
        return None

    item = (message.get("params") or {}).get("item") or {}
    item_type = item.get("type")
    if item_type == "agentMessage":
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return {
            "event": "message",
            "phase": item.get("phase"),
            "text": _truncate_log_text(text),
        }
    if item_type == "reasoning":
        content = item.get("content") or []
        text = "\n".join(str(part) for part in content if part)
        if not text.strip():
            return None
        return {"event": "reasoning", "text": _truncate_log_text(text)}
    if item_type == "commandExecution":
        output = item.get("aggregatedOutput")
        result: dict[str, Any] = {
            "event": "tool",
            "status": item.get("status"),
            "command": _truncate_log_text(item.get("command", ""), 2_000),
        }
        if isinstance(output, str) and output:
            result["output_chars"] = len(output)
            result["output"] = _truncate_log_text(output, _LOG_TOOL_OUTPUT_LIMIT)
        if item.get("exitCode") is not None:
            result["exit_code"] = item["exitCode"]
        return result
    if item_type == "collabAgentToolCall":
        result = {
            "event": "subagent",
            "tool": item.get("tool"),
            "status": item.get("status"),
        }
        if item.get("prompt"):
            result["prompt"] = _truncate_log_text(item["prompt"], 2_000)
        return result
    return None

def _load_prompt(name: str) -> str:
    return (
        resources.files("core.detector.offline")
        .joinpath("prompts")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def _render_prompt(name: str, **values: Any) -> str:
    text = _load_prompt(name)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    unresolved = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if unresolved:
        raise RuntimeError(f"Unresolved placeholders in {name}: {unresolved}")
    return text


def _render_origin(origin: str) -> str:
    """Describe where a proposal came from so a confirmed omission is not read as a duplicate."""
    if origin.startswith("omission:"):
        omission_id = origin.split(":", 1)[1]
        return (
            f"Origin: the Analyzer confirmed your completion-review omission "
            f"candidate {omission_id}. Review it on its merits as a proposal; it "
            f"is not a duplicate of that candidate."
        )
    return "Origin: the Analyzer's own proposal."


def _render_accepted(accepted: list[dict[str, Any]]) -> str:
    """List the findings already accepted so a same-mechanism restatement is recognizable."""
    if not accepted:
        return "Accepted findings so far: none."
    lines = ["Accepted findings so far (a proposal restating one of these mechanisms on the same records is a duplicate; a different mechanism on the same records is not):", ""]
    for item in accepted:
        spans = ", ".join(f"L{e['start']}-L{e['end']}" if e["start"] != e["end"] else f"L{e['start']}" for e in item.get("evidence", []))
        lines.append(f"- {item['finding_id']} [{item['taxonomy']}] {spans}: {item['description']}")
    return "\n".join(lines) + "\n"


def _render_rejected_omissions(rejected: list[dict[str, Any]]) -> str:
    """Render Analyzer-rejected omission candidates with their counterevidence."""
    if not rejected:
        return ""
    lines = [
        "",
        "Candidates from earlier rounds that the Analyzer rejected, with its",
        "counterevidence:",
        "",
    ]
    for item in rejected:
        lines.append(f"- {item['omission_id']}: {item['behavior']}")
        lines.append(f"  Counterevidence: {item['resolution']}")
    return "\n".join(lines) + "\n"


def _verifier_instructions(run_dir: Path, scratch_dir: Path) -> str:
    return _render_prompt(
        "verifier.md",
        EVIDENCE_ROOT=run_dir,
        SCRATCH_DIRECTORY=scratch_dir,
        TAXONOMY=render_taxonomy(),
    )


def _parse_json_object(text: str, *, source: str) -> dict[str, Any]:
    """Parse structured model output, tolerating one Markdown JSON fence."""
    stripped = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    candidate = fenced.group(1) if fenced else stripped
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{source} returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{source} structured output must be a JSON object")
    return value


class CodexVerifier:
    """One persistent Codex app-server process and thread per detection."""

    def __init__(
        self,
        *,
        executable: str,
        model: str,
        provider_name: str,
        base_url: str,
        api_key_env: str,
        api_key: str,
        wire_api: str,
        reasoning_effort: str,
        web_search: str,
        multi_agent: bool,
        timeout: int,
        log_path: Path | None = None,
    ):
        self.executable = executable
        self.model = model
        self.provider_name = provider_name
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.api_key = api_key
        self.wire_api = wire_api
        self.reasoning_effort = reasoning_effort
        self.web_search = web_search
        self.multi_agent = multi_agent
        self.timeout = timeout
        self.log_path = log_path
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=200)
        self._request_id = 0
        self._write_lock = threading.Lock()
        self._thread_id: str | None = None
        self._run_dir: Path | None = None
        self._process_workdir: Path | None = None
        self._usage: dict[str, Any] | None = None

    @classmethod
    def from_config(
        cls,
        config: VerifierConfig,
        *,
        api_key: str,
        timeout: int | None = None,
        log_path: Path | None = None,
    ) -> "CodexVerifier":
        return cls(
            executable=config.executable,
            model=config.model,
            provider_name=config.provider_name,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            api_key=api_key,
            wire_api=config.wire_api,
            reasoning_effort=config.reasoning_effort,
            web_search=config.web_search,
            multi_agent=config.multi_agent,
            timeout=config.timeout if timeout is None else timeout,
            log_path=log_path,
        )

    def start(self, *, run_dir: Path, workdir: Path | None = None) -> None:
        self.close()
        self._messages = queue.Queue()
        self._stderr = deque(maxlen=200)
        self._usage = None
        self._run_dir = run_dir.resolve()
        self._process_workdir = workdir.resolve() if workdir is not None else self._run_dir
        environment = isolated_environment()
        environment[self.api_key_env] = self.api_key
        process_workdir = self._process_workdir
        self._process = subprocess.Popen(
            self._command(),
            cwd=process_workdir,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(
            target=self._read_stdout,
            args=(self._process, self._messages),
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            args=(self._process, self._stderr),
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "ridar",
                    "title": "RIDAR Verifier",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": False,
                    "optOutNotificationMethods": [
                        "item/agentMessage/delta",
                        "item/reasoning/summaryTextDelta",
                        "item/reasoning/textDelta",
                    ],
                },
            },
        )
        self._notify("initialized", {})
        params: dict[str, Any] = {
            "cwd": str(process_workdir),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "developerInstructions": _verifier_instructions(
                self._run_dir, process_workdir
            ),
            "ephemeral": True,
        }
        if self.model:
            params["model"] = self.model
        response = self._request("thread/start", params)
        thread_id = (response.get("thread") or {}).get("id")
        if not isinstance(thread_id, str) or not thread_id:
            self.close()
            raise RuntimeError("Codex app-server did not return a thread id")
        self._thread_id = thread_id

    def _command(self) -> list[str]:
        provider = "ridar_verifier"

        def setting(name: str, value: Any) -> list[str]:
            if isinstance(value, bool):
                encoded = str(value).lower()
            else:
                encoded = json.dumps(value)
            return ["-c", f"{name}={encoded}"]

        command = [self.executable, "app-server", "--strict-config"]
        values: list[tuple[str, Any]] = [
            ("model", self.model),
            ("model_provider", provider),
            ("model_reasoning_effort", self.reasoning_effort),
            (f"model_providers.{provider}.name", self.provider_name),
            (f"model_providers.{provider}.base_url", self.base_url),
            (f"model_providers.{provider}.env_key", self.api_key_env),
            (f"model_providers.{provider}.wire_api", self.wire_api),
            (f"model_providers.{provider}.requires_openai_auth", False),
            ("web_search", self.web_search),
            ("features.hooks", False),
            ("features.apps", False),
            ("features.goals", False),
            ("features.memories", False),
            ("features.multi_agent", self.multi_agent),
            ("features.remote_plugin", False),
        ]
        for name, value in values:
            command.extend(setting(name, value))
        return command

    def review(
        self,
        *,
        run_dir: Path,
        proposal_id: str,
        proposal: dict[str, Any],
        revision: int,
        origin: str,
        accepted: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Verify the proposal against the trace, split it into findings, and name each."""
        self._require_started(run_dir)
        prompt = _render_prompt(
            "verifier_review.md",
            PROPOSAL_ID=proposal_id,
            REVISION=revision,
            ORIGIN=_render_origin(origin),
            PROPOSAL=json.dumps(proposal, ensure_ascii=False, indent=2),
            ACCEPTED=_render_accepted(accepted),
        )
        return self._turn(prompt, _VERIFIER_SCHEMA)

    def probe(self, *, run_dir: Path) -> None:
        """Make one real model turn to validate the configured Codex provider."""
        try:
            self.start(run_dir=run_dir)
            result = self._turn(
                'Connectivity check only. Return exactly {"ok": true}.',
                _PREFLIGHT_SCHEMA,
            )
            if result.get("ok") is not True:
                raise RuntimeError("Verifier preflight returned an unexpected response")
        finally:
            self.close()

    def finish(
        self,
        *,
        run_dir: Path,
        round: int,
        rounds_remaining: int,
        rejected: list[dict[str, Any]],
        accepted: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._require_started(run_dir)
        prompt = _render_prompt(
            "verifier_finish.md",
            ROUND=round,
            ROUNDS_REMAINING=rounds_remaining,
            REJECTED=_render_rejected_omissions(rejected),
            ACCEPTED=_render_accepted(accepted),
        )
        return self._turn(prompt, _COMPLETION_SCHEMA)

    @property
    def usage(self) -> dict[str, Any] | None:
        return self._usage

    def close(self) -> None:
        process = self._process
        reader = self._reader
        stderr_reader = self._stderr_reader
        self._process = None
        self._reader = None
        self._stderr_reader = None
        self._thread_id = None
        self._run_dir = None
        self._process_workdir = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

        # Do not let readers from a closed process write an EOF marker or
        # stderr into a subsequently started session.
        for thread in (reader, stderr_reader):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)

    def _turn(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        assert self._thread_id is not None
        assert self._process_workdir is not None
        response = self._request(
            "turn/start",
            {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": prompt}],
                "outputSchema": schema,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
        )
        turn_id = (response.get("turn") or {}).get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise RuntimeError("Codex app-server did not return a turn id")
        text = self._wait_for_turn(turn_id)
        return _parse_json_object(text, source="Verifier")

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self._write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._next_message(deadline)
            if message.get("id") != request_id or "method" in message:
                continue
            if message.get("error"):
                raise RuntimeError(
                    f"Codex app-server {method} failed: {message['error']}"
                )
            result = message.get("result") or {}
            if not isinstance(result, dict):
                raise RuntimeError(f"Codex app-server {method} returned invalid result")
            return result

    def _wait_for_turn(self, turn_id: str) -> str:
        deadline = time.monotonic() + self.timeout
        final_text: str | None = None
        while True:
            message = self._next_message(deadline)
            method = message.get("method")
            params = message.get("params") or {}
            if method == "item/completed" and params.get("turnId") == turn_id:
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and isinstance(
                    item.get("text"), str
                ):
                    final_text = item["text"]
            elif method == "turn/completed" and (params.get("turn") or {}).get(
                "id"
            ) == turn_id:
                status = (params.get("turn") or {}).get("status")
                if status != "completed":
                    raise RuntimeError(
                        f"Codex Verifier turn ended with status {status!r}"
                    )
                if final_text is None:
                    raise RuntimeError(
                        "Codex Verifier completed without an agent message"
                    )
                return final_text

    def _next_message(self, deadline: float) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex app-server request timed out")
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError("Codex app-server request timed out") from error
            if message.get("_closed"):
                stderr = "".join(self._stderr)[-2000:].strip()
                detail = f": {stderr}" if stderr else ""
                raise RuntimeError(f"Codex app-server exited unexpectedly{detail}")
            if "id" in message and "method" in message:
                self._write(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": f"unsupported server request: {message['method']}",
                        },
                    }
                )
                continue
            return message

    def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("Codex app-server is not running")
        with self._write_lock:
            log_value = _compact_verifier_log(message, direction="send")
            if log_value is not None:
                append_log(self.log_path, "verifier", log_value)
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def _read_stdout(
        self,
        process: subprocess.Popen[str],
        messages: queue.Queue[dict[str, Any]],
    ) -> None:
        if process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    if value.get("method") == "thread/tokenUsage/updated":
                        self._record_usage(value.get("params") or {})
                    log_value = _compact_verifier_log(value, direction="receive")
                    if log_value is not None:
                        append_log(self.log_path, "verifier", log_value)
                    messages.put(value)
        finally:
            messages.put({"_closed": True})

    def _record_usage(self, params: dict[str, Any]) -> None:
        token_usage = params.get("tokenUsage")
        if isinstance(token_usage, dict) and isinstance(token_usage.get("total"), dict):
            self._usage = token_usage["total"]

    def _read_stderr(
        self, process: subprocess.Popen[str], stderr: deque[str]
    ) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            append_log(self.log_path, "verifier", f"stderr: {line.rstrip()}")
            stderr.append(line)

    def _require_started(self, run_dir: Path) -> None:
        if self._process is None or self._thread_id is None:
            raise RuntimeError("Codex Verifier has not been started")
        if self._run_dir != run_dir.resolve():
            raise RuntimeError("Codex Verifier is attached to a different run")

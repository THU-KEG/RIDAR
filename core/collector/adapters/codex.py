from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..records import normalize_tool_name
from .base import AdapterResult, CollectedRecord, read_jsonl, with_agent


# Codex wraps tool output in a header (Chunk ID / Wall time / exit code /
# optionally Original token count) followed by an Output:/Final output:
# marker. The exit line is trusted only there, alone on its own line: the
# same phrase inside the command's own output (a log the agent just read) is
# not this call's exit status.
_EXIT_CODE = re.compile(r"(?:^|\n)Process exited with code (-?\d+)(?:\n|$)")
_IGNORED_TOOLS = {"update_plan"}


class CodexAdapter:
    name = "codex"

    def stage_session(
        self,
        session_id: str,
        raws_dir: Path,
        home: str | Path | None = None,
    ) -> Path:
        """Locate a rollout thread in the local Codex storage and copy it plus
        all descendant subagent rollouts into raws_dir."""
        import shutil

        root = Path(home or Path.home() / ".codex" / "sessions").expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Codex storage not found: {root}")
        threads: dict[str, tuple[Path, str | None]] = {}
        for path in sorted(root.rglob("rollout-*.jsonl")):
            meta = _peek_session_meta(path)
            thread_id = str(meta.get("id") or "")
            if thread_id:
                parent = meta.get("parent_thread_id")
                threads[thread_id] = (path, str(parent) if parent else None)
        if session_id not in threads:
            raise FileNotFoundError(f"Session {session_id!r} not found under {root}")
        selected = {session_id}
        changed = True
        while changed:
            changed = False
            for thread_id, (_, parent) in threads.items():
                if thread_id not in selected and parent in selected:
                    selected.add(thread_id)
                    changed = True
        raws_dir.mkdir(parents=True, exist_ok=True)
        for thread_id in sorted(selected):
            path = threads[thread_id][0]
            shutil.copy2(path, raws_dir / path.name)
        return root

    def collect(self, source: Path, *, strict: bool = True) -> AdapterResult:
        source = source.expanduser().resolve()
        paths = _source_files(source)
        if not paths:
            raise FileNotFoundError(f"No Codex rollout JSONL found under {source}")
        result = AdapterResult()
        rollouts = []
        for path in paths:
            parsed, warnings = read_jsonl(path, strict=strict)
            result.warnings.extend(warnings)
            rollouts.append({"path": path, "parsed": parsed, "meta": _session_meta(parsed)})

        roots = [item for item in rollouts if not item["meta"].get("parent_thread_id")]
        if not roots:
            raise ValueError(f"No main Codex rollout (without a parent thread) under {source}")
        if len(roots) > 1:
            names = sorted(item["path"].name for item in roots)
            raise ValueError(
                f"Multiple main Codex rollouts under {source}; expected one: {names}"
            )
        root = roots[0]
        by_id = {
            str(item["meta"]["id"]): item
            for item in rollouts
            if item["meta"].get("id")
        }
        by_parent: dict[str, list[dict[str, Any]]] = {}
        for item in rollouts:
            if item is root:
                continue
            parent = str(item["meta"].get("parent_thread_id"))
            if parent not in by_id:
                result.warnings.append(
                    f"Codex subagent rollout {item['path'].name} has unknown "
                    f"parent thread {parent!r}; attached to the main rollout"
                )
                parent = str(root["meta"].get("id"))
            by_parent.setdefault(parent, []).append(item)

        ordered: list[dict[str, Any]] = []

        def visit(item: dict[str, Any]) -> None:
            ordered.append(item)
            children = by_parent.get(str(item["meta"].get("id")), [])
            for child in sorted(
                children,
                key=lambda value: (
                    str(value["meta"].get("timestamp") or ""),
                    str(value["meta"].get("id") or ""),
                ),
            ):
                visit(child)

        visit(root)
        for item in rollouts:
            if item not in ordered:
                result.warnings.append(
                    f"Codex rollout {item['path'].name} has cyclic or unresolved topology"
                )
                ordered.append(item)

        used_agent_ids: set[str] = set()
        for index, item in enumerate(ordered):
            meta = item["meta"]
            session_id = str(meta.get("id") or "")
            if session_id:
                result.session_ids.add(session_id)
            agent_id = None
            if index > 0:
                nickname = meta.get("agent_nickname")
                agent_id = str(nickname) if nickname else f"agent-{session_id[:8]}"
                if agent_id in used_agent_ids:
                    agent_id = f"{agent_id}-{session_id[-6:]}"
                used_agent_ids.add(agent_id)
                result.agents.add(agent_id)
            result.sources.append(str(item["path"]))
            _collect_rollout(item, agent_id, result)
        version = next(
            (
                item["meta"]["multi_agent_version"]
                for item in ordered
                if item["meta"].get("multi_agent_version") is not None
            ),
            None,
        )
        if version is not None:
            result.metadata["multi_agent_version"] = version
        return result


def _collect_rollout(
    rollout: dict[str, Any], agent_id: str | None, result: AdapterResult
) -> None:
    ignored_call_ids: set[str] = set()
    for line_number, native in rollout["parsed"]:
        if native.get("type") == "session_meta":
            continue
        for record in _records_from_native(native, ignored_call_ids):
            result.records.append(
                CollectedRecord(
                    with_agent(record, agent_id),
                    timestamp=_timestamp(native),
                    source=str(rollout["path"]),
                    line=line_number,
                )
            )


def _peek_session_meta(path: Path) -> dict[str, Any]:
    """Read the session_meta payload from the head of a rollout file."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(5):
                line = handle.readline()
                if not line.strip():
                    continue
                try:
                    native = json.loads(line)
                except json.JSONDecodeError:
                    return {}
                payload = native.get("payload") if isinstance(native, dict) else None
                if (
                    isinstance(native, dict)
                    and native.get("type") == "session_meta"
                    and isinstance(payload, dict)
                ):
                    return payload
    except OSError:
        pass
    return {}


def _session_meta(parsed: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    for _, native in parsed:
        payload = native.get("payload")
        if native.get("type") == "session_meta" and isinstance(payload, dict):
            return payload
    return {}


def _source_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if not source.is_dir():
        return []
    # Rollouts may sit in a nested date tree (codex/YYYY/MM/DD/rollout-*.jsonl).
    # Preserve native filenames; topology comes from each session_meta.
    return sorted(path for path in source.rglob("*.jsonl") if path.is_file())


def _records_from_native(
    native: dict[str, Any], ignored_call_ids: set[str]
) -> list[dict[str, Any]]:
    payload = native.get("payload")
    if not isinstance(payload, dict):
        return []
    if native.get("type") == "event_msg" and payload.get("type") == "turn_aborted":
        reason = payload.get("reason") or "Codex turn aborted"
        return [{"type": "error", "content": str(reason)}]
    if native.get("type") != "response_item":
        return []
    kind = payload.get("type")
    if kind == "message":
        return _message(payload)
    if kind == "reasoning":
        visible: list[str] = []
        for field in ("summary", "content"):
            for item in payload.get(field) or []:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    continue
                text = item["text"].strip()
                if text and text not in visible:
                    visible.append(text)
        text = "\n".join(visible)
        return [{"type": "reasoning", "content": text}] if text else []
    if kind in {"function_call", "custom_tool_call"}:
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return []
        call_id = payload.get("call_id")
        if name in _IGNORED_TOOLS:
            if isinstance(call_id, str) and call_id:
                ignored_call_ids.add(call_id)
            return []
        record = {
            "type": "tool_call",
            "name": normalize_tool_name(name),
            "input": _tool_input(payload, kind),
        }
        if isinstance(call_id, str) and call_id:
            record["call_id"] = call_id
        return [record]
    if kind in {"function_call_output", "custom_tool_call_output"}:
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return []
        if call_id in ignored_call_ids:
            return []
        content, exit_code = _tool_output(payload.get("output"))
        record = {
            "type": "tool_result",
            "call_id": call_id,
            "content": content,
            "is_error": exit_code not in (None, 0),
        }
        if exit_code is not None:
            record["exit_code"] = exit_code
        return [record]
    if kind == "web_search_call":
        action = payload.get("action") or {}
        return [
            {
                "type": "tool_call",
                "name": "search",
                "input": action,
                "status": payload.get("status"),
            }
        ]
    return []


def _message(payload: dict[str, Any]) -> list[dict[str, Any]]:
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return []
    text = "\n".join(
        item.get("text", "")
        for item in payload.get("content") or []
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )
    return [{"type": role, "content": text}] if text else []


def _tool_input(payload: dict[str, Any], kind: str) -> Any:
    raw = payload.get("input") if kind == "custom_tool_call" else payload.get("arguments")
    if not isinstance(raw, str):
        return raw if raw is not None else {}
    if kind == "custom_tool_call":
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _tool_output(value: Any) -> tuple[Any, int | None]:
    if not isinstance(value, str):
        return value if value is not None else "", None
    # Confine the exit-code search to the wrapper header: everything after
    # the first Output:/Final output: marker is the command's own content.
    header_end = len(value)
    for marker_text in ("Final output:\n", "Output:\n"):
        position = value.find(marker_text)
        if position != -1:
            header_end = min(header_end, position)
    match = _EXIT_CODE.search(value, 0, header_end)
    exit_code = int(match.group(1)) if match else None
    marker = "Final output:\n"
    if marker in value:
        return value.split(marker, 1)[1], exit_code
    marker = "Output:\n"
    if marker in value and "Chunk ID:" in value:
        return value.split(marker, 1)[1], exit_code
    return value, exit_code


def _timestamp(native: dict[str, Any]) -> str | None:
    value = native.get("timestamp")
    return value if isinstance(value, str) and value else None

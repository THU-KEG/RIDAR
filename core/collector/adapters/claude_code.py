from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..records import normalize_tool_name
from .base import AdapterResult, CollectedRecord, read_jsonl, with_agent


_IGNORED_TOOLS = {"TaskCreate", "TaskUpdate"}


class ClaudeCodeAdapter:
    name = "claude-code"

    def stage_session(
        self,
        session_id: str,
        raws_dir: Path,
        home: str | Path | None = None,
    ) -> Path:
        """Locate a session in the local Claude Code storage and copy its
        transcript (plus native subagent sidecar directory) into raws_dir."""
        import shutil

        root = Path(home or Path.home() / ".claude" / "projects").expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Claude Code storage not found: {root}")
        matches = sorted(
            path
            for path in root.rglob(f"{session_id}.jsonl")
            if "subagents" not in path.parent.parts
        )
        if not matches:
            raise FileNotFoundError(f"Session {session_id!r} not found under {root}")
        if len(matches) > 1:
            raise ValueError(f"Session {session_id!r} is ambiguous under {root}: {matches}")
        main = matches[0]
        raws_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(main, raws_dir / main.name)
        sidecar = main.with_suffix("")
        if sidecar.is_dir():
            shutil.copytree(sidecar, raws_dir / sidecar.name)
        return root

    def collect(
        self,
        source: Path,
        *,
        strict: bool = True,
        agent_id: str | None = None,
    ) -> AdapterResult:
        source = source.expanduser().resolve()
        paths = _source_files(source)
        if not paths:
            raise FileNotFoundError(f"No Claude Code JSONL logs found under {source}")
        if len(paths) > 1:
            raise ValueError(
                f"Multiple Claude Code logs found under {source}; expected one main transcript"
            )
        path = paths[0]
        result = _collect_file(
            path,
            strict=strict,
            agent_id=agent_id,
        )
        subagent_paths = _subagent_files(path, result.session_ids)
        if subagent_paths:
            _merge_subagents(result, subagent_paths, strict=strict)
        return result


def _collect_file(
    path: Path,
    *,
    strict: bool,
    agent_id: str | None,
) -> AdapterResult:
    """Collect one native transcript without discovering related files."""
    parsed, warnings = read_jsonl(path, strict=strict)
    result = AdapterResult(sources=[str(path)], warnings=warnings)
    ignored_call_ids: set[str] = set()
    seen_native_records: set[tuple[str, str]] = set()
    for line_number, native in parsed:
        session_id = native.get("sessionId") or native.get("session_id")
        if session_id:
            result.session_ids.add(str(session_id))
        native_agent = agent_id or _agent_id(native)
        if native_agent:
            result.agents.add(native_agent)
        records = _records_from_native(
            native,
            ignored_call_ids=ignored_call_ids,
        )
        native_id = native.get("uuid")
        if isinstance(native_id, str) and native_id:
            identity = (
                native_id,
                json.dumps(records, ensure_ascii=False, sort_keys=True),
            )
            if identity in seen_native_records:
                continue
            seen_native_records.add(identity)
        for record in records:
            result.records.append(
                CollectedRecord(
                    with_agent(record, native_agent),
                    timestamp=_timestamp(native),
                    source=str(path),
                    line=line_number,
                )
            )
    return result


def _subagent_files(main_path: Path, session_ids: set[str]) -> list[Path]:
    """Discover only Claude Code's native subagent transcript locations."""
    directories = {
        main_path.parent / session_id / "subagents" for session_id in session_ids
    }
    directories.add(main_path.with_suffix("") / "subagents")
    directories.add(main_path.parent / "subagents")
    paths: set[Path] = set()
    for directory in directories:
        if directory.is_dir():
            paths.update(path.resolve() for path in directory.glob("agent-*.jsonl"))
    return sorted(path for path in paths if path.is_file())


def _merge_subagents(
    result: AdapterResult, paths: list[Path], *, strict: bool
) -> None:
    """Collect subagents and append parent-before-child topology blocks."""
    children: dict[str, dict[str, Any]] = {}
    used_agent_ids: set[str] = set()
    for path in paths:
        base_agent_id = path.stem.removeprefix("agent-")
        agent_id = base_agent_id
        suffix = 2
        while agent_id in used_agent_ids:
            agent_id = f"{base_agent_id}-{suffix}"
            suffix += 1
        if agent_id != base_agent_id:
            result.warnings.append(
                f"Claude Code subagent file {path.name!r} duplicates agent id "
                f"{base_agent_id!r} from another transcript; renamed to {agent_id!r}"
            )
        used_agent_ids.add(agent_id)
        child = _collect_file(
            path,
            strict=strict,
            agent_id=agent_id,
        )
        meta, warning = _read_subagent_meta(path)
        if warning:
            result.warnings.append(warning)
        children[agent_id] = {
            "result": child,
            "meta": meta,
            "path": path,
            "parent": None,
            "spawn_order": None,
        }

    call_owners: dict[str, str | None] = {}
    call_positions: dict[str, int] = {}
    owner_results = [(None, result)] + [
        (agent_id, info["result"]) for agent_id, info in children.items()
    ]
    for owner, owner_result in owner_results:
        for position, item in enumerate(owner_result.records):
            record = item.record
            call_id = record.get("call_id")
            if record.get("type") == "tool_call" and isinstance(call_id, str):
                call_owners.setdefault(call_id, owner)
                call_positions.setdefault(call_id, position)

    for agent_id, info in children.items():
        meta = info["meta"]
        tool_use_id = meta.get("toolUseId") if isinstance(meta, dict) else None
        if isinstance(tool_use_id, str) and tool_use_id in call_owners:
            info["parent"] = call_owners[tool_use_id]
            info["spawn_order"] = call_positions.get(tool_use_id)
        elif isinstance(tool_use_id, str):
            result.warnings.append(
                f"Claude Code subagent {agent_id!r} has unmatched toolUseId {tool_use_id!r}"
            )
        else:
            result.warnings.append(
                f"Claude Code subagent {agent_id!r} has no usable toolUseId"
            )

    by_parent: dict[str | None, list[str]] = {}
    for agent_id, info in children.items():
        parent = info["parent"]
        if parent not in children and parent is not None:
            parent = None
            info["parent"] = None
        by_parent.setdefault(parent, []).append(agent_id)

    def sort_key(agent_id: str) -> tuple[bool, int, bool, str, str]:
        info = children[agent_id]
        order = info["spawn_order"]
        child_result = info["result"]
        return (
            order is None,
            order if isinstance(order, int) else 0,
            child_result.started_at is None,
            child_result.started_at or "",
            agent_id,
        )

    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(parent: str | None) -> None:
        for child_id in sorted(by_parent.get(parent, []), key=sort_key):
            if child_id in visiting or child_id in ordered:
                continue
            visiting.add(child_id)
            ordered.append(child_id)
            visit(child_id)
            visiting.remove(child_id)

    visit(None)
    for child_id in sorted(children, key=sort_key):
        if child_id not in ordered:
            result.warnings.append(
                f"Claude Code subagent {child_id!r} has cyclic or unresolved topology"
            )
            ordered.append(child_id)

    topology = []
    for child_id in ordered:
        info = children[child_id]
        child_result = info["result"]
        result.extend(child_result)
        meta = info["meta"]
        topology.append(
            {
                "agent_id": child_id,
                "parent_agent_id": info["parent"],
                "tool_use_id": meta.get("toolUseId") if isinstance(meta, dict) else None,
                "spawn_depth": meta.get("spawnDepth") if isinstance(meta, dict) else None,
                "agent_type": meta.get("agentType") if isinstance(meta, dict) else None,
                "description": meta.get("description") if isinstance(meta, dict) else None,
            }
        )
    result.metadata["agent_topology"] = topology


def _read_subagent_meta(path: Path) -> tuple[dict[str, Any], str | None]:
    meta_path = path.with_suffix(".meta.json")
    if not meta_path.is_file():
        return {}, f"Claude Code subagent metadata not found: {meta_path}"
    try:
        value = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {}, f"Could not read Claude Code subagent metadata {meta_path}: {error}"
    if not isinstance(value, dict):
        return {}, f"Claude Code subagent metadata is not an object: {meta_path}"
    return value, None


def _source_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if not source.is_dir():
        return []
    # The main transcript may sit in a nested project tree (e.g. a copied
    # ~/.claude/projects). Native subagent transcripts (subagents/agent-*.jsonl)
    # are discovered relative to the main transcript, never as mains.
    paths = list(source.rglob("*.jsonl")) + list(source.rglob("*.log"))
    return sorted(
        path
        for path in paths
        if path.is_file() and "subagents" not in path.parent.parts[len(source.parts):]
    )


def _records_from_native(
    native: dict[str, Any], *, ignored_call_ids: set[str]
) -> list[dict[str, Any]]:
    kind = native.get("type")
    if kind == "assistant":
        if native.get("isApiErrorMessage") or native.get("error"):
            content = _error_text(native)
            return [{"type": "error", "content": content}] if content else []
        return _assistant_records(native.get("message"), ignored_call_ids)
    if kind == "user":
        return _user_records(native, ignored_call_ids)
    return []


def _assistant_records(message: Any, ignored_call_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    records: list[dict[str, Any]] = []
    for block in _content_blocks(message.get("content")):
        kind = block.get("type")
        if kind == "thinking":
            text = block.get("thinking")
            if isinstance(text, str) and text:
                records.append({"type": "reasoning", "content": text})
        elif kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                records.append({"type": "assistant", "content": text})
        elif kind == "tool_use":
            name = block.get("name")
            if not isinstance(name, str) or not name:
                continue
            call_id = block.get("id")
            if name in _IGNORED_TOOLS:
                if isinstance(call_id, str) and call_id:
                    ignored_call_ids.add(call_id)
                continue
            record = {
                "type": "tool_call",
                "name": normalize_tool_name(name),
                "input": block.get("input", {}),
            }
            if isinstance(call_id, str) and call_id:
                record["call_id"] = call_id
            records.append(record)
    return records


def _user_records(
    native: dict[str, Any], ignored_call_ids: set[str]
) -> list[dict[str, Any]]:
    message = native.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        if native.get("isMeta") or not content:
            return []
        return [{"type": "user", "content": content}]
    records: list[dict[str, Any]] = []
    for block in _content_blocks(content):
        kind = block.get("type")
        if kind == "text" and not native.get("isMeta"):
            text = block.get("text")
            if isinstance(text, str) and text:
                records.append({"type": "user", "content": text})
        elif kind == "tool_result":
            call_id = block.get("tool_use_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            if call_id in ignored_call_ids:
                continue
            records.append(
                {
                    "type": "tool_result",
                    "call_id": call_id,
                    "content": _tool_result_content(block.get("content")),
                    "is_error": bool(block.get("is_error", False)),
                }
            )
    return records


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _tool_result_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content if content is not None else ""
    texts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
        else:
            texts.append(block)
    return "\n".join(texts) if all(isinstance(value, str) for value in texts) else texts


def _timestamp(native: dict[str, Any]) -> str | None:
    value = native.get("timestamp")
    return value if isinstance(value, str) and value else None


def _agent_id(native: dict[str, Any]) -> str | None:
    value = native.get("agent_id") or native.get("agentId")
    return str(value) if value else None


def _error_text(native: dict[str, Any]) -> str:
    for key in ("error", "apiError", "apiErrorStatus"):
        value = native.get(key)
        if isinstance(value, str) and value:
            return value
        if value:
            return str(value)
    return "Claude Code API error"

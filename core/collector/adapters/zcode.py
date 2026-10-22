from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..records import normalize_tool_name
from .base import AdapterResult, CollectedRecord, read_jsonl, with_agent


_FORMAT = "zcode-session-expanded"
_VERSION = 1
_IGNORED_TOOLS = {"taskcreate", "taskupdate", "todoread", "todowrite", "update_plan"}
_IGNORED_PARTS = {"step-start", "step-finish", "timeline"}

# Whitelisted session columns. The session table also holds permission
# state, share URLs, revert bookkeeping, and compaction summaries the trace
# never needs, so only these columns are exported.
_SESSION_FIELDS = ("id", "parent_id", "title", "version", "time_created")


class ZCodeAdapter:
    name = "zcode"

    def stage_session(
        self,
        session_id: str,
        raws_dir: Path,
        home: str | Path | None = None,
    ) -> Path:
        """Export one ZCode session tree from the local SQLite store into a
        portable expanded JSONL file under raws_dir (read-only access)."""
        db_path = Path(
            home or Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
        ).expanduser().resolve()
        if db_path.is_dir():
            db_path = db_path / "db.sqlite"
        if not db_path.is_file():
            raise FileNotFoundError(f"ZCode database not found: {db_path}")
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            sessions = _read_db_sessions(connection, session_id)
            messages = _read_db_messages(connection, [str(row["id"]) for row in sessions])
        finally:
            connection.close()
        raws_dir.mkdir(parents=True, exist_ok=True)
        output = raws_dir / "zcode-session.jsonl"
        with output.open("w", encoding="utf-8") as handle:
            header = {
                "type": "zcode_export",
                "format": _FORMAT,
                "version": _VERSION,
                "root_session_id": session_id,
                "sessions": [dict(row) for row in sessions],
            }
            handle.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n")
            for message in messages:
                handle.write(json.dumps(message, ensure_ascii=False, sort_keys=True) + "\n")
        return db_path

    def collect(self, source: Path, *, strict: bool = True) -> AdapterResult:
        source = source.expanduser().resolve()
        path = _expanded_file(source)
        parsed, warnings = read_jsonl(path, strict=strict)
        if not parsed:
            raise ValueError(f"Empty expanded ZCode session file: {path}")

        header_line, header = parsed[0]
        _validate_header(path, header_line, header)
        result = AdapterResult(sources=[str(path)], warnings=warnings)

        sessions = header["sessions"]
        root_id = header["root_session_id"]
        session_order = _session_order(sessions, root_id, result.warnings)
        session_meta = {
            str(item["id"]): item
            for item in sessions
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        result.session_ids.update(session_order)

        messages: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for line_number, native in parsed[1:]:
            if native.get("type") != "message":
                _invalid(
                    result,
                    strict,
                    f"{path}:{line_number}: expected expanded ZCode message",
                )
                continue
            session_id = native.get("session_id")
            if not isinstance(session_id, str) or session_id not in session_meta:
                _invalid(
                    result,
                    strict,
                    f"{path}:{line_number}: message has unknown session_id {session_id!r}",
                )
                continue
            messages[session_id].append((line_number, native))

        topology = []
        for session_id in session_order:
            agent_id = None if session_id == root_id else session_id
            if agent_id:
                result.agents.add(agent_id)
                meta = session_meta[session_id]
                topology.append(
                    {
                        "agent_id": agent_id,
                        "parent_agent_id": (
                            None if meta.get("parent_id") == root_id else meta.get("parent_id")
                        ),
                        "session_id": session_id,
                        "title": meta.get("title"),
                    }
                )
            ordered_messages = sorted(messages.get(session_id, []), key=_message_key)
            for line_number, native in ordered_messages:
                _collect_message(
                    result,
                    path,
                    line_number,
                    native,
                    agent_id=agent_id,
                    strict=strict,
                )

        if not result.records:
            raise ValueError(f"Expanded ZCode session contains no usable trace records: {path}")

        root_meta = session_meta[root_id]
        result.metadata.update(
            {
                "framework": "zcode",
                "runtime": "zcode",
                "zcode_export_version": header["version"],
            }
        )
        if root_meta.get("version"):
            result.metadata["zcode_version"] = root_meta["version"]
        if topology:
            result.metadata["agent_topology"] = topology
        return result


def _expanded_file(source: Path) -> Path:
    if source.is_file():
        candidates = [source] if _has_header(source) else []
    elif source.is_dir():
        candidates = [path for path in sorted(source.glob("*.jsonl")) if _has_header(path)]
    else:
        candidates = []
    if not candidates:
        raise ValueError(
            f"No expanded ZCode session found under {source}; telemetry-only logs "
            "do not contain message, reasoning, or tool content"
        )
    if len(candidates) > 1:
        raise ValueError(
            f"Multiple expanded ZCode sessions found under {source}; expected one"
        )
    return candidates[0]


def _has_header(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = next((line for line in handle if line.strip()), None)
        value = json.loads(first) if first is not None else None
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    return value.get("type") == "zcode_export" and value.get("format") == _FORMAT


def _validate_header(path: Path, line: int, header: dict[str, Any]) -> None:
    if header.get("type") != "zcode_export" or header.get("format") != _FORMAT:
        raise ValueError(f"{path}:{line}: not an expanded ZCode session")
    if header.get("version") != _VERSION:
        raise ValueError(
            f"{path}:{line}: unsupported ZCode export version {header.get('version')!r}"
        )
    root_id = header.get("root_session_id")
    sessions = header.get("sessions")
    if not isinstance(root_id, str) or not root_id:
        raise ValueError(f"{path}:{line}: ZCode export requires root_session_id")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError(f"{path}:{line}: ZCode export requires sessions")
    ids = [item.get("id") for item in sessions if isinstance(item, dict)]
    if len(ids) != len(sessions) or any(not isinstance(value, str) or not value for value in ids):
        raise ValueError(f"{path}:{line}: every ZCode session requires an id")
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}:{line}: duplicate ZCode session ids")
    if root_id not in ids:
        raise ValueError(f"{path}:{line}: root_session_id is absent from sessions")


def _session_order(
    sessions: list[dict[str, Any]], root_id: str, warnings: list[str]
) -> list[str]:
    children: dict[str, list[str]] = defaultdict(list)
    ids = {str(item["id"]) for item in sessions}
    for item in sessions:
        session_id = str(item["id"])
        if session_id == root_id:
            continue
        parent = item.get("parent_id")
        if not isinstance(parent, str) or parent not in ids:
            warnings.append(
                f"ZCode session {session_id!r} has missing parent {parent!r}; attached to root"
            )
            parent = root_id
        children[parent].append(session_id)

    order: list[str] = []
    visiting: set[str] = set()

    def visit(session_id: str) -> None:
        if session_id in visiting:
            warnings.append(f"ZCode session topology contains a cycle at {session_id!r}")
            return
        if session_id in order:
            return
        visiting.add(session_id)
        order.append(session_id)
        for child_id in sorted(children.get(session_id, []), key=lambda value: _session_key(value, sessions)):
            visit(child_id)
        visiting.remove(session_id)

    visit(root_id)
    for session_id in sorted(ids):
        if session_id not in order:
            warnings.append(f"ZCode session {session_id!r} has unresolved topology")
            visit(session_id)
    return order


def _session_key(session_id: str, sessions: list[dict[str, Any]]) -> tuple[int, str]:
    item = next(value for value in sessions if value.get("id") == session_id)
    created = item.get("time_created")
    return (created if isinstance(created, int) else 0, session_id)


def _message_key(item: tuple[int, dict[str, Any]]) -> tuple[bool, int, int, str]:
    _, message = item
    sequence = message.get("sequence")
    created = message.get("time_created")
    return (
        not isinstance(sequence, int),
        sequence if isinstance(sequence, int) else 0,
        created if isinstance(created, int) else 0,
        str(message.get("id", "")),
    )


def _collect_message(
    result: AdapterResult,
    path: Path,
    line_number: int,
    native: dict[str, Any],
    *,
    agent_id: str | None,
    strict: bool,
) -> None:
    message = native.get("data")
    parts = native.get("parts")
    if not isinstance(message, dict) or not isinstance(parts, list):
        _invalid(result, strict, f"{path}:{line_number}: message requires data and parts")
        return
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return
    semantics = message.get("semantics")
    if isinstance(semantics, dict) and semantics.get("transcriptVisibility") == "hidden":
        return

    timestamp = _timestamp(message.get("time"), "created") or _timestamp_value(
        native.get("time_created")
    )
    deferred_results: list[tuple[str | None, dict[str, Any]]] = []
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
            _invalid(result, strict, f"{path}:{line_number}: invalid expanded ZCode part")
            continue
        data = part["data"]
        kind = data.get("type")
        part_timestamp = _timestamp(data.get("time"), "start") or timestamp
        if kind == "text":
            text = data.get("text")
            if isinstance(text, str) and text:
                _append(result, {"type": role, "content": text}, part_timestamp, path, line_number, agent_id)
        elif kind == "reasoning" and role == "assistant":
            text = data.get("text")
            if isinstance(text, str) and text:
                _append(
                    result,
                    {"type": "reasoning", "content": text},
                    part_timestamp,
                    path,
                    line_number,
                    agent_id,
                )
        elif kind == "tool" and role == "assistant":
            deferred = _tool_records(data, result.warnings, path, line_number)
            if deferred is None:
                continue
            call, call_timestamp, tool_result, result_timestamp = deferred
            _append(result, call, call_timestamp or part_timestamp, path, line_number, agent_id)
            if tool_result is not None:
                deferred_results.append((result_timestamp, tool_result))
        elif kind in _IGNORED_PARTS:
            continue
        elif isinstance(kind, str):
            result.warnings.append(f"{path}:{line_number}: ignored unknown ZCode part type {kind!r}")

    for result_timestamp, record in sorted(
        deferred_results, key=lambda item: (item[0] is None, item[0] or "")
    ):
        _append(result, record, result_timestamp or timestamp, path, line_number, agent_id)


def _tool_records(
    data: dict[str, Any], warnings: list[str], path: Path, line_number: int
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None, str | None] | None:
    name = data.get("tool")
    call_id = data.get("callID")
    state = data.get("state")
    if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
        warnings.append(f"{path}:{line_number}: ignored ZCode tool part without tool/callID")
        return None
    if name.lower() in _IGNORED_TOOLS:
        return None
    if not isinstance(state, dict):
        warnings.append(f"{path}:{line_number}: ZCode tool {call_id!r} has no state")
        state = {}
    if "input" not in state:
        warnings.append(f"{path}:{line_number}: ZCode tool {call_id!r} has no expanded input")
    call = {
        "type": "tool_call",
        "name": normalize_tool_name(name),
        "input": state.get("input", {}),
        "call_id": call_id,
    }
    call_timestamp = _timestamp(state.get("time"), "start")
    status = state.get("status")
    if status in {"running", None}:
        warnings.append(f"{path}:{line_number}: ZCode tool {call_id!r} is incomplete")
        return call, call_timestamp, None, None
    is_error = status != "completed"
    content_key = "error" if is_error else "output"
    if content_key not in state:
        warnings.append(
            f"{path}:{line_number}: ZCode tool {call_id!r} has no expanded {content_key}"
        )
    content = state.get(content_key)
    if content is None:
        content = ""
    tool_result = {
        "type": "tool_result",
        "call_id": call_id,
        "content": content,
        "is_error": is_error,
    }
    if status not in {"completed", "error", "cancelled"}:
        warnings.append(f"{path}:{line_number}: ZCode tool {call_id!r} has status {status!r}")
    return call, call_timestamp, tool_result, _timestamp(state.get("time"), "end")


def _append(
    result: AdapterResult,
    record: dict[str, Any],
    timestamp: str | None,
    path: Path,
    line: int,
    agent_id: str | None,
) -> None:
    result.records.append(
        CollectedRecord(
            with_agent(record, agent_id),
            timestamp=timestamp,
            source=str(path),
            line=line,
        )
    )


def _timestamp(value: Any, key: str) -> str | None:
    return _timestamp_value(value.get(key)) if isinstance(value, dict) else None


def _timestamp_value(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _invalid(result: AdapterResult, strict: bool, message: str) -> None:
    if strict:
        raise ValueError(message)
    result.warnings.append(message)


def _read_db_sessions(connection: sqlite3.Connection, root_id: str) -> list[dict[str, Any]]:
    columns = ", ".join(_SESSION_FIELDS)
    root = connection.execute(
        f"SELECT {columns} FROM session WHERE id = ?", (root_id,)
    ).fetchone()
    if root is None:
        raise FileNotFoundError(f"Session {root_id!r} not found in the database")
    ordered = [dict(root)]
    seen = {root_id}
    position = 0
    while position < len(ordered):
        children = connection.execute(
            f"SELECT {columns} FROM session WHERE parent_id = ?"
            " ORDER BY time_created, id",
            (str(ordered[position]["id"]),),
        ).fetchall()
        for child in children:
            child_id = str(child["id"])
            if child_id not in seen:
                seen.add(child_id)
                ordered.append(dict(child))
        position += 1
    return ordered


def _read_db_messages(
    connection: sqlite3.Connection, session_ids: list[str]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for session_id in session_ids:
        rows = connection.execute(
            "SELECT * FROM message WHERE session_id = ?"
            " ORDER BY sequence IS NULL, sequence, time_created, id",
            (session_id,),
        ).fetchall()
        for row in rows:
            message = dict(row)
            message["data"] = _decode_db_data(message["data"], "message", message["id"])
            parts = []
            for part_row in connection.execute(
                "SELECT * FROM part WHERE message_id = ?"
                " ORDER BY sequence IS NULL, sequence, time_created, id",
                (message["id"],),
            ).fetchall():
                part = dict(part_row)
                part["data"] = _decode_db_data(part["data"], "part", part["id"])
                parts.append(part)
            output.append({"type": "message", **message, "parts": parts})
    return output


def _decode_db_data(value: Any, table: str, row_id: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid JSON in ZCode {table} row {row_id}: {error}") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"ZCode {table} row {row_id} data is not an object")
    return decoded

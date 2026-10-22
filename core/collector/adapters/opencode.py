from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import AdapterResult, CollectedRecord, read_jsonl, with_agent
from ..records import normalize_tool_name


HEADER_FORMAT = "opencode-session-expanded"
HEADER_VERSION = 1

_IGNORED_PART_TYPES = {"step-start", "step-finish", "snapshot", "patch", "file"}

# Whitelisted session columns; the database also holds account and credential
# tables that must never reach the export.
_SESSION_FIELDS = (
    "id",
    "parent_id",
    "slug",
    "title",
    "directory",
    "agent",
    "model",
    "version",
    "time_created",
    "time_updated",
)


def database_path(home: str | Path | None = None) -> Path:
    """The OpenCode SQLite file: ``home`` may name the file or its directory."""
    db_path = Path(
        home or Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    ).expanduser().resolve()
    if db_path.is_dir():
        db_path = db_path / "opencode.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"OpenCode database not found: {db_path}")
    return db_path


def root_session_ids(db_path: Path) -> list[str]:
    """Top-level session ids (no parent), oldest first. Read-only."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT id FROM session WHERE parent_id IS NULL ORDER BY time_created, id"
        ).fetchall()
    finally:
        db.close()
    return [str(row[0]) for row in rows]


def export_session(db_path: Path, session_id: str, output: Path, *, completed_only: bool = False) -> None:
    """Write the expanded export of one session tree to ``output``. Read-only on the
    database; whitelisted session columns only, so credentials never leave it.

    OpenCode updates a message's parts in place while it streams and while its tools run, so a
    live reader passes ``completed_only`` to skip assistant messages that have no
    ``time.completed`` yet; their parts would otherwise be exported half-written."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sessions = OpencodeAdapter._session_tree(db, session_id)
        with output.open("w", encoding="utf-8") as handle:
            header = {
                "format": HEADER_FORMAT,
                "version": HEADER_VERSION,
                "root_session_id": session_id,
                "sessions": sessions,
            }
            handle.write(json.dumps(header, ensure_ascii=False) + "\n")
            for session in sessions:
                for message_id, message_data in db.execute(
                    "SELECT id, data FROM message WHERE session_id=?"
                    " ORDER BY time_created, id",
                    (session["id"],),
                ).fetchall():
                    data = json.loads(message_data)
                    if completed_only and data.get("role") == "assistant" and not (data.get("time") or {}).get("completed"):
                        continue
                    handle.write(
                        json.dumps(
                            {
                                "kind": "message",
                                "id": message_id,
                                "session_id": session["id"],
                                "data": data,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    for part_id, part_data in db.execute(
                        "SELECT id, data FROM part WHERE message_id=? ORDER BY id",
                        (message_id,),
                    ).fetchall():
                        handle.write(
                            json.dumps(
                                {
                                    "kind": "part",
                                    "id": part_id,
                                    "message_id": message_id,
                                    "session_id": session["id"],
                                    "data": json.loads(part_data),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
    finally:
        db.close()


class OpencodeAdapter:
    name = "opencode"

    def stage_session(
        self,
        session_id: str,
        raws_dir: Path,
        home: str | Path | None = None,
    ) -> Path:
        """Export one OpenCode session tree from the local SQLite store into a
        portable JSONL file under raws_dir. The database is opened read-only
        and only whitelisted fields are exported; credentials never leave it."""
        db_path = database_path(home)
        raws_dir.mkdir(parents=True, exist_ok=True)
        export_session(db_path, session_id, raws_dir / "opencode-session.jsonl")
        return db_path

    @staticmethod
    def _session_tree(db: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
        columns = ", ".join(_SESSION_FIELDS)
        root = db.execute(
            f"SELECT {columns} FROM session WHERE id=?", (session_id,)
        ).fetchone()
        if root is None:
            raise FileNotFoundError(f"Session {session_id!r} not found in the database")
        sessions = [dict(zip(_SESSION_FIELDS, root))]
        frontier = [session_id]
        while frontier:
            parent = frontier.pop(0)
            for row in db.execute(
                f"SELECT {columns} FROM session WHERE parent_id=? ORDER BY time_created, id",
                (parent,),
            ).fetchall():
                session = dict(zip(_SESSION_FIELDS, row))
                sessions.append(session)
                frontier.append(session["id"])
        return sessions

    def collect(self, source: Path, *, strict: bool = True) -> AdapterResult:
        source = source.expanduser().resolve()
        path = _expanded_file(source)
        parsed, warnings = read_jsonl(path, strict=strict)
        if not parsed:
            raise ValueError(f"Empty OpenCode session export: {path}")
        header_line, header = parsed[0]
        if header.get("format") != HEADER_FORMAT:
            raise ValueError(f"{path}:{header_line}: not an {HEADER_FORMAT} file")
        if header.get("version") != HEADER_VERSION:
            raise ValueError(
                f"{path}:{header_line}: unsupported export version {header.get('version')!r}"
            )
        result = AdapterResult(sources=[str(path)], warnings=warnings)
        sessions = header.get("sessions") or []
        root_id = str(header.get("root_session_id") or "")
        agent_ids = _agent_ids(sessions, root_id)
        result.session_ids.update(agent_ids)
        result.agents.update(value for value in agent_ids.values() if value)
        for session in sessions:
            if session.get("id") == root_id and session.get("model"):
                result.metadata["model"] = session["model"]

        parts_by_message: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        messages: list[tuple[int, dict[str, Any]]] = []
        for line_number, item in parsed[1:]:
            kind = item.get("kind")
            if kind == "message":
                messages.append((line_number, item))
            elif kind == "part":
                parts_by_message.setdefault(str(item.get("message_id")), []).append(
                    (line_number, item)
                )
        for line_number, message in messages:
            session_id = str(message.get("session_id"))
            if session_id not in agent_ids:
                result.warnings.append(
                    f"{path.name}:{line_number}: message for unknown session {session_id!r}"
                )
                continue
            _collect_message(
                message,
                parts_by_message.get(str(message.get("id")), []),
                agent_ids[session_id],
                path,
                result,
            )
        return result


def _expanded_file(source: Path) -> Path:
    if source.is_file():
        return source
    if not source.is_dir():
        raise FileNotFoundError(f"Not an OpenCode export file or directory: {source}")
    for path in sorted(source.rglob("*.jsonl")):
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                first = handle.readline()
            if HEADER_FORMAT in first:
                return path
        except OSError:
            continue
    if any(source.rglob("opencode.db")):
        raise ValueError(
            f"Only a raw OpenCode database was found under {source}; collect with "
            "--session so the adapter exports the session (the database holds "
            "credentials and is never copied)"
        )
    raise FileNotFoundError(f"No {HEADER_FORMAT} export found under {source}")


def _agent_ids(sessions: list[Any], root_id: str) -> dict[str, str | None]:
    agent_ids: dict[str, str | None] = {}
    used: set[str] = set()
    for session in sessions:
        if not isinstance(session, dict) or not session.get("id"):
            continue
        session_id = str(session["id"])
        if session_id == root_id:
            agent_ids[session_id] = None
            continue
        candidate = str(session.get("slug") or session.get("title") or session_id[:12])
        if candidate in used:
            candidate = f"{candidate}-{session_id[-6:]}"
        used.add(candidate)
        agent_ids[session_id] = candidate
    return agent_ids


def _collect_message(
    message: dict[str, Any],
    parts: list[tuple[int, dict[str, Any]]],
    agent_id: str | None,
    path: Path,
    result: AdapterResult,
) -> None:
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    role = data.get("role")
    timestamp = _timestamp(data)
    if role == "user":
        text = "\n".join(
            str(part.get("data", {}).get("text") or "")
            for _, part in parts
            if part.get("data", {}).get("type") == "text"
        ).strip()
        if text:
            _append(result, {"type": "user", "content": text}, agent_id, path, message, timestamp)
        return
    if role != "assistant":
        return
    error = data.get("error")
    if isinstance(error, dict) and error:
        name = error.get("name") or "error"
        detail = error.get("data", {}).get("message") if isinstance(error.get("data"), dict) else ""
        _append(
            result,
            {"type": "error", "content": f"{name}: {detail}".strip().rstrip(":")},
            agent_id,
            path,
            message,
            timestamp,
        )
    for line_number, part in parts:
        part_data = part.get("data") if isinstance(part.get("data"), dict) else {}
        kind = part_data.get("type")
        if kind in _IGNORED_PART_TYPES or kind is None:
            continue
        if kind in {"text", "reasoning"}:
            text = str(part_data.get("text") or "")
            if text.strip():
                record_type = "assistant" if kind == "text" else "reasoning"
                _append(result, {"type": record_type, "content": text}, agent_id, path, message, timestamp, line_number)
        elif kind == "tool":
            _tool_records(part_data, agent_id, path, result, timestamp, line_number)
        else:
            result.warnings.append(
                f"{path.name}:{line_number}: unhandled OpenCode part type {kind!r}"
            )


def _tool_records(
    part_data: dict[str, Any],
    agent_id: str | None,
    path: Path,
    result: AdapterResult,
    timestamp: str | None,
    line_number: int,
) -> None:
    name = normalize_tool_name(str(part_data.get("tool") or ""))
    call_id = part_data.get("callID")
    state = part_data.get("state") if isinstance(part_data.get("state"), dict) else {}
    if not name:
        result.warnings.append(f"{path.name}:{line_number}: tool part without a tool name")
        return
    call: dict[str, Any] = {"type": "tool_call", "name": name, "input": state.get("input") or {}}
    if isinstance(call_id, str) and call_id:
        call["call_id"] = call_id
    result.records.append(
        CollectedRecord(with_agent(call, agent_id), timestamp=timestamp, source=str(path), line=line_number)
    )
    status = state.get("status")
    if status not in {"completed", "error"}:
        result.warnings.append(
            f"{path.name}:{line_number}: OpenCode tool {call_id!r} is {status!r}; its result is not in the trace"
        )
    if status in {"completed", "error"} and isinstance(call_id, str) and call_id:
        output = state.get("output")
        record = {
            "type": "tool_result",
            "call_id": call_id,
            "content": output if output is not None else "",
            "is_error": status == "error",
        }
        result.records.append(
            CollectedRecord(with_agent(record, agent_id), timestamp=timestamp, source=str(path), line=line_number)
        )


def _append(
    result: AdapterResult,
    record: dict[str, Any],
    agent_id: str | None,
    path: Path,
    message: dict[str, Any],
    timestamp: str | None,
    line_number: int | None = None,
) -> None:
    result.records.append(
        CollectedRecord(
            with_agent(record, agent_id),
            timestamp=timestamp,
            source=str(path),
            line=line_number,
        )
    )


def _timestamp(data: dict[str, Any]) -> str | None:
    time = data.get("time")
    created = time.get("created") if isinstance(time, dict) else None
    if isinstance(created, (int, float)) and created > 0:
        return datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
    return None

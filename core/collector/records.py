from __future__ import annotations

import re
from typing import Any


TRACE_TYPES = frozenset(
    {"user", "assistant", "reasoning", "tool_call", "tool_result", "error"}
)

_TOOL_ALIASES = {
    "bash": "bash",
    "exec_command": "bash",
    "read": "read",
    "write": "write",
    "edit": "edit",
    "apply_patch": "edit",
    "websearch": "search",
    "web_search": "search",
    "search": "search",
    "webfetch": "fetch",
    "web_fetch": "fetch",
    "web_search_prime_web_search_prime": "search",
}


def normalize_tool_name(name: str) -> str:
    stripped = name.strip().lower()
    if stripped.startswith("mcp__"):
        stripped = stripped[len("mcp__"):]
    canonical = re.sub(r"[^a-z0-9]+", "_", stripped).strip("_") or stripped
    return _TOOL_ALIASES.get(canonical, canonical)


def validate_record(record: dict[str, Any]) -> dict[str, Any]:
    kind = record.get("type")
    if kind not in TRACE_TYPES:
        raise ValueError(f"Unsupported trace record type: {kind!r}")
    if kind in {"user", "assistant", "reasoning", "error"}:
        content = record.get("content")
        if not isinstance(content, str) or not content:
            raise ValueError(f"{kind} record requires non-empty string content")
    elif kind == "tool_call":
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool_call requires name")
        if "input" not in record:
            raise ValueError("tool_call requires input")
    elif kind == "tool_result":
        call_id = record.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("tool_result requires call_id")
        if "content" not in record:
            raise ValueError("tool_result requires content")
    agent_id = record.get("agent_id")
    if agent_id is not None and (not isinstance(agent_id, str) or not agent_id):
        raise ValueError("agent_id must be a non-empty string")
    return record


def record_text(record: dict[str, Any]) -> str:
    kind = record.get("type")
    if kind in {"user", "assistant", "reasoning", "error", "tool_result"}:
        return _text(record.get("content"))
    if kind == "tool_call":
        return f"{record.get('name', '')}\n{_text(record.get('input'))}".strip()
    return _text(record)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)

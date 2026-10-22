"""Group canonical trace records into online detection steps."""

from __future__ import annotations

import re
from typing import Any

AGENT_TEXT = ("assistant", "reasoning")

# Window cut signals: a record that makes the turn worth judging on its own. Liberal name patterns
# because adapters name tools differently (write/str_replace_editor, bash/shell/local_shell).
CUT_WRITE_TOOL = re.compile(r"write|edit|patch|create", re.I)
CUT_SHELL_TOOL = re.compile(r"bash|shell|exec|command|terminal", re.I)
CUT_ARTIFACT = re.compile(r"results?/|report|figure|summary|readme|\.md\b|\.json\b|\.csv\b|\.png\b|\.pdf\b|\.tex\b", re.I)
CUT_RISKY = re.compile(r"\brm\b|\bgit\b|pytest|\bmake test\b|sbatch|\bkill\b|\.sh\b|\bmv\b|\bsed\b.{0,40}-i")
CUT_METRIC = re.compile(r"\b(acc(?:uracy)?|f1|loss|score|auc|bleu|rmse|r2|p-?valu?e|precision|recall|macro|neutral)\b.{0,24}\d", re.I)
# Redirect targets of a shell command: `cat > idea.md <<EOF` (codex writes every deliverable this
# way), `tee results/log`, `echo ... >> report.md`. `2>&1`, `>&2`, and fd-prefixed redirects are
# not matched; /dev/null never looks like an artifact.
SHELL_REDIRECT = re.compile(r"(?:^|[^&\d>])>{1,2}\s*([\w@%./~+-]+)|\btee\s+(?:-a\s+)?([\w@%./~+-]+)")


def shell_write_targets(command: str) -> list[str]:
    """File paths a shell command writes through redirection or tee."""
    return [group for match in SHELL_REDIRECT.finditer(command) for group in match.groups() if group]


def cut_signal(record: dict[str, Any]) -> str | None:
    """Why this record closes the window it lands in, or None. Pure function over one record."""
    kind = record.get("type")
    content = record.get("content") if isinstance(record.get("content"), str) else None
    if content is None:
        raw = record.get("content") if record.get("content") is not None else record.get("input")
        content = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    if kind == "tool_call":
        name = str(record.get("name") or "")
        if CUT_WRITE_TOOL.search(name) and CUT_ARTIFACT.search(content[:400]):
            return "write_artifact"
        if CUT_SHELL_TOOL.search(name):
            if any(CUT_ARTIFACT.search(target) for target in shell_write_targets(content[:1000])):
                return "write_artifact"
            if CUT_RISKY.search(content[:600]):
                return "risky_cmd"
    if kind == "tool_result" and CUT_METRIC.search(content[:2000]):
        return "metric_out"
    return None


def _record_chars(record: dict[str, Any]) -> int:
    content = record.get("content")
    if content is None:
        content = record.get("input")
    return len(content) if isinstance(content, str) else len(str(content or ""))


def next_window(
    records: list[dict[str, Any]],
    start: int,
    *,
    feed_ended: bool = False,
    turn_boundary: int = 0,
    max_records: int = 40,
    max_chars: int = 60000,
) -> dict[str, Any] | None:
    """The next window of whole turns beginning at 1-based line ``start``.

    Returns ``{"start", "end", "closed", "cut"}`` or None when no record has arrived at ``start``.
    A window absorbs complete turns until one carries a cut signal, the record/char budget fills,
    or a user record arrives (delivered alone, so a judgment never spans an instruction change).
    The trailing turn counts as complete once the feed ended or ``turn_boundary`` covers it; an
    unclosed window (``closed`` False) is deliverable only under those same conditions, which is
    the barrier force-cut."""
    if start > len(records):
        return None
    offset = start - 1
    parts = steps(records[offset:])
    win_end: int | None = None
    chars = 0
    for part in parts:
        first, last = part["start"] + offset, part["end"] + offset
        complete = part is not parts[-1] or feed_ended or last <= turn_boundary
        if records[first - 1].get("type") == "user":
            if win_end is not None:
                return {"start": start, "end": win_end, "closed": True, "cut": "user_turn"}
            return {"start": first, "end": last, "closed": True, "cut": "user_turn"}
        if not complete:
            break
        win_end = last
        reason = None
        for line in range(first, last + 1):
            chars += _record_chars(records[line - 1])
            reason = reason or cut_signal(records[line - 1])
        if reason:
            return {"start": start, "end": win_end, "closed": True, "cut": reason}
        if win_end - start + 1 >= max_records or chars >= max_chars:
            return {"start": start, "end": win_end, "closed": True, "cut": "budget"}
    if win_end is None:
        return None
    return {"start": start, "end": win_end, "closed": False, "cut": None}


def steps(records: list[dict[str, Any]]) -> list[dict[str, int]]:
    """Consecutive record ranges: a user record alone; otherwise an agent turn with its tool calls
    and results. A new turn starts at agent text that follows tool activity."""
    out: list[dict[str, int]] = []
    start = None
    seen_tool = False
    for line, record in enumerate(records, 1):
        kind = record.get("type")
        if kind == "user":
            if start is not None:
                out.append({"start": start, "end": line - 1})
            out.append({"start": line, "end": line})
            start, seen_tool = None, False
            continue
        if start is None:
            start, seen_tool = line, False
        elif kind in AGENT_TEXT and seen_tool:
            out.append({"start": start, "end": line - 1})
            start, seen_tool = line, False
        if kind in ("tool_call", "tool_result"):
            seen_tool = True
    if start is not None:
        out.append({"start": start, "end": len(records)})
    for index, step in enumerate(out, 1):
        step["id"] = f"s{index}"
    return out

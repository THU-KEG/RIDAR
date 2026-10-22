"""Reading segments of one collected trace.

Segments are consecutive whole records bounded by a character budget. They
only pace the Analyzer's reading; evidence coordinates remain the 1-based
line numbers of ``extracted/trace.jsonl``.
"""

from __future__ import annotations

from typing import Any

from ...collector.records import record_text


SEGMENT_CHARS = 150_000


def segments(
    records: list[dict[str, Any]], budget: int = SEGMENT_CHARS
) -> list[dict[str, int | str]]:
    """Consecutive whole-record segments of at most ``budget`` characters each;
    a single record larger than the budget forms its own segment."""
    parts: list[dict[str, int | str]] = []
    start, chars = 1, 0
    for line, record in enumerate(records, 1):
        size = len(record_text(record))
        if chars and chars + size > budget:
            parts.append({"id": f"S{len(parts) + 1}", "start": start, "end": line - 1, "chars": chars})
            start, chars = line, 0
        chars += size
    if records:
        parts.append({"id": f"S{len(parts) + 1}", "start": start, "end": len(records), "chars": chars})
    return parts

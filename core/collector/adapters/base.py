from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..records import validate_record


@dataclass(slots=True)
class CollectedRecord:
    record: dict[str, Any]
    timestamp: str | None = None
    source: str | None = None
    line: int | None = None

    def __post_init__(self) -> None:
        validate_record(self.record)


@dataclass(slots=True)
class AdapterResult:
    records: list[CollectedRecord] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    agents: set[str] = field(default_factory=set)
    session_ids: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def extend(self, other: "AdapterResult") -> None:
        self.records.extend(other.records)
        self.sources.extend(source for source in other.sources if source not in self.sources)
        self.agents.update(other.agents)
        self.session_ids.update(other.session_ids)
        self.warnings.extend(other.warnings)
        self.errors.extend(other.errors)
        self.metadata.update(other.metadata)

    @property
    def started_at(self) -> str | None:
        values = [record.timestamp for record in self.records if record.timestamp]
        return min(values, default=None)

    @property
    def ended_at(self) -> str | None:
        values = [record.timestamp for record in self.records if record.timestamp]
        return max(values, default=None)


class CollectorAdapter(Protocol):
    name: str

    def collect(self, source: Path, *, strict: bool = True) -> AdapterResult: ...


def read_jsonl(path: Path, *, strict: bool = True) -> tuple[list[tuple[int, dict[str, Any]]], list[str]]:
    records: list[tuple[int, dict[str, Any]]] = []
    warnings: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                message = f"{path}:{line_number}: invalid JSON: {error.msg}"
                if strict:
                    raise ValueError(message) from error
                warnings.append(message)
                continue
            if not isinstance(value, dict):
                message = f"{path}:{line_number}: expected JSON object"
                if strict:
                    raise ValueError(message)
                warnings.append(message)
                continue
            records.append((line_number, value))
    return records, warnings


def with_agent(record: dict[str, Any], agent_id: str | None) -> dict[str, Any]:
    if not agent_id:
        return record
    return {**record, "agent_id": agent_id}

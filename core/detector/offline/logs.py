"""Append-only Analyzer and Verifier runtime logs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def reset_logs(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for role in ("analyzer", "verifier"):
        (directory / f"{role}.log").write_text("", encoding="utf-8")


def append_log(path: Path | None, role: str, value: Any) -> None:
    if path is None:
        return
    if isinstance(value, str):
        text = value.rstrip("\n")
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as handle:
        for line in text.splitlines() or [""]:
            handle.write(f"[{role}] {line}\n")
        handle.flush()

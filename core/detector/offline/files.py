"""Atomic file writes shared by the offline detector."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


def write_text_atomic(path: Path, lines: Iterable[str]) -> None:
    """Write ``lines`` (each already newline-terminated) to ``path`` atomically."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            for line in lines:
                handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    write_text_atomic(path, [json.dumps(value, ensure_ascii=False, indent=2) + "\n"])

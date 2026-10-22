"""Materialize the online Analyzer prompt and the .mcp.json that starts the ridar-online server."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from ..rubric import render_rubric
from ..taxonomy import render_taxonomy
from .mcp import MCP_SERVER_NAME

@dataclass(frozen=True)
class PoolLimits:
    """Bounds of the pending-step and investigation pools: how many steps may stay pending, how many
    background workers may run, how many investigations may be open, and when a worker times out.
    The window budgets bound how many records one delivered step may bundle; at 1 record every
    turn is judged on its own."""

    max_pending_steps: int = 8
    max_investigation_workers: int = 8
    max_open_investigations: int = 24
    investigation_timeout_seconds: float = 180.0
    window_max_records: int = 40
    window_max_chars: int = 60000

    def as_args(self) -> list[str]:
        return [
            "--max-pending-steps", str(self.max_pending_steps),
            "--max-investigation-workers", str(self.max_investigation_workers),
            "--max-open-investigations", str(self.max_open_investigations),
            "--investigation-timeout-seconds", str(self.investigation_timeout_seconds),
            "--window-records", str(self.window_max_records),
            "--window-chars", str(self.window_max_chars),
        ]


def render_prompt(*, live_trace: Path, ledger: Path, scratch: Path) -> str:
    """Fill the packaged template; the shared rubric and taxonomy are rendered from their modules."""
    template = (resources.files("core.detector.online") / "prompts" / "analyzer.md").read_text(encoding="utf-8")
    values = {
        "LIVE_TRACE": str(live_trace),
        "LEDGER": str(ledger),
        "SCRATCH_DIRECTORY": str(scratch),
        "RUBRIC": render_rubric(),
        "TAXONOMY": render_taxonomy(),
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    unresolved = re.findall(r"\{\{[A-Z_]+\}\}", template)
    if unresolved:
        raise ValueError(f"unresolved placeholders in online analyzer.md: {unresolved}")
    return template


def install(
    view_root: str | Path,
    *,
    monitor_dir: str | Path,
    run_id: str,
    limits: PoolLimits = PoolLimits(),
) -> dict[str, Any]:
    """Write analyzer.md and .mcp.json under ``view_root``; return their paths."""
    view_root = Path(view_root).expanduser().resolve()
    monitor = Path(monitor_dir).expanduser().resolve()
    scratch = view_root / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    prompt = view_root / "analyzer.md"
    prompt.write_text(
        render_prompt(live_trace=monitor / "live.jsonl", ledger=monitor / "ledger.md", scratch=scratch),
        encoding="utf-8",
    )
    mcp_config = view_root / ".mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {MCP_SERVER_NAME: {
        "command": sys.executable,
        "args": ["-m", "core", "detect-mcp", "--online", "--monitor-dir", str(monitor), "--run-id", run_id, *limits.as_args()],
    }}}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"view": str(view_root), "scratch": str(scratch), "prompt": str(prompt), "mcp_config": str(mcp_config), "limits": asdict(limits)}

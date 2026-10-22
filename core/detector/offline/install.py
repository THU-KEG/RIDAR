"""Materialize packaged offline Detector prompts and MCP configuration."""

from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from ..rubric import render_rubric
from ..taxonomy import render_taxonomy

MCP_SERVER_NAME = "ridar-detector"


def install(
    workdir: str | Path,
    *,
    config_path: str | Path,
    log_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    run_dir: str | Path | None = None,
    verifier_workdir: str | Path | None = None,
) -> dict[str, Any]:
    workdir = Path(workdir).expanduser().resolve()
    config_path = Path(config_path).expanduser().resolve()
    resolved_run_dir = (
        Path(run_dir).expanduser().resolve() if run_dir is not None else None
    )
    prompt = _install_analyzer_prompt(
        workdir / ".ridar-detector" / "prompts" / "analyzer.md",
        evidence_root=resolved_run_dir,
        scratch_directory=(workdir / "scratch" / "analyzer") if run_dir is not None else None,
    )
    mcp_config = _register_mcp_server(
        workdir / ".mcp.json",
        config_path,
        Path(log_dir).expanduser().resolve() if log_dir is not None else None,
        Path(output_dir).expanduser().resolve() if output_dir is not None else None,
        resolved_run_dir,
        (
            Path(verifier_workdir).expanduser().resolve()
            if verifier_workdir is not None
            else None
        ),
    )
    return {"workdir": str(workdir), "prompt": str(prompt), "mcp_config": str(mcp_config)}


def _install_analyzer_prompt(
    target: Path,
    *,
    evidence_root: Path | None,
    scratch_directory: Path | None,
) -> Path:
    source = resources.files("core.detector.offline") / "prompts" / "analyzer.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = source.read_text(encoding="utf-8")
    if evidence_root is not None and scratch_directory is not None:
        text = text.replace("{{EVIDENCE_ROOT}}", str(evidence_root))
        text = text.replace("{{SCRATCH_DIRECTORY}}", str(scratch_directory))
        text = text.replace("{{RUBRIC}}", render_rubric())
        text = text.replace("{{TAXONOMY}}", render_taxonomy())
        if "{{" in text:
            raise ValueError("unresolved placeholders in analyzer.md")
    target.write_text(text, encoding="utf-8")
    return target


def _register_mcp_server(
    path: Path,
    config_path: Path,
    log_dir: Path | None,
    output_dir: Path | None,
    run_dir: Path | None,
    verifier_workdir: Path | None,
) -> Path:
    config: dict[str, Any] = {}
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            config = value
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        config["mcpServers"] = servers
    args = ["-m", "core", "detect-mcp", "--config", str(config_path)]
    if log_dir is not None:
        args.extend(["--log-dir", str(log_dir)])
    if output_dir is not None:
        args.extend(["--output-dir", str(output_dir)])
    if run_dir is not None:
        args.extend(["--run-dir", str(run_dir)])
    if verifier_workdir is not None:
        args.extend(["--verifier-workdir", str(verifier_workdir)])
    servers[MCP_SERVER_NAME] = {
        "command": sys.executable,
        "args": args,
    }
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path

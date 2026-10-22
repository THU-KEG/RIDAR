"""Fail-fast connectivity checks for both offline Detector model runtimes."""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .agents import CodexVerifier
from .config import DetectorConfig, isolated_environment


PREFLIGHT_TIMEOUT = 120


class PreflightError(RuntimeError):
    pass


def _process_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def text(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode(errors="replace").strip()
        return (value or "").strip()

    sections = []
    if output := text(stdout):
        sections.append(f"stdout:\n{output[-1500:]}")
    if errors := text(stderr):
        sections.append(f"stderr:\n{errors[-1500:]}")
    return "\n".join(sections)


def run_preflight(
    config: DetectorConfig,
    *,
    run_dir: Path | None,
    components: tuple[str, ...] = ("analyzer", "verifier"),
) -> None:
    """Issue minimal real model calls in parallel and aggregate all failures.

    ``components`` selects which roles to check; the online Detector has no Verifier.
    """
    config.validate_credentials(components=components)
    checks: dict[str, Callable[[], None]] = {}
    if "analyzer" in components:
        checks["Analyzer"] = lambda: _check_analyzer(config)
    if "verifier" in components:
        if run_dir is None:
            raise PreflightError("verifier preflight needs run_dir")
        checks["Verifier"] = lambda: _check_verifier(config, run_dir)
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="ridar-preflight") as pool:
        futures = {pool.submit(check): name for name, check in checks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except Exception as error:
                failures[name] = f"{type(error).__name__}: {error}"
    if failures:
        detail = "\n".join(f"- {name}: {failures[name]}" for name in sorted(failures))
        raise PreflightError(f"Detector API preflight failed:\n{detail}")


def _check_analyzer(config: DetectorConfig) -> None:
    analyzer = config.analyzer
    environment = isolated_environment()
    environment.update(analyzer.resolved_environment())
    command = [
        analyzer.executable,
        "-p",
        "Connectivity check only. Reply with exactly OK.",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--model",
        analyzer.model,
        "--tools",
        "",
    ]
    try:
        result = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        detail = _process_output(error.stdout, error.stderr)
        raise RuntimeError(
            f"Claude Code preflight timed out after {PREFLIGHT_TIMEOUT}s"
            + (f"\n{detail}" if detail else "")
        ) from error
    except OSError as error:
        raise RuntimeError(f"Claude Code preflight could not run: {error}") from error
    if result.returncode != 0:
        detail = _process_output(result.stdout, result.stderr)
        raise RuntimeError(
            f"Claude Code exited with status {result.returncode}"
            + (f"\n{detail}" if detail else "")
        )


def _check_verifier(config: DetectorConfig, run_dir: Path) -> None:
    api_key = os.environ[config.verifier.api_key_env]
    verifier = CodexVerifier.from_config(
        config.verifier,
        api_key=api_key,
        timeout=min(config.verifier.timeout, PREFLIGHT_TIMEOUT),
    )
    verifier.probe(run_dir=run_dir)

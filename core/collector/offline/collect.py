"""Batch collection orchestration for completed agent runs."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..registry import get_adapter
from ..store import RunStore


def collect_run(
    *,
    adapter_name: str,
    path: str | None,
    session: str | None,
    home: str | None,
    run_id: str,
    runs: str | Path,
    task_id: str | None,
    strict: bool,
) -> tuple[Path, int]:
    """Stage one completed run, normalize it, and persist canonical outputs."""
    run_dir = run_directory(runs, run_id)
    if run_dir.exists():
        raise FileExistsError(f"Run already exists: {run_dir}")
    if bool(path) == bool(session):
        raise ValueError("Provide exactly one of --path or --session")

    adapter = get_adapter(adapter_name)
    raws = run_dir / "raws"
    try:
        if session is not None:
            stage = getattr(adapter, "stage_session", None)
            if stage is None:
                raise ValueError(
                    f"--session is not implemented yet for {adapter_name!r}; use --path"
                )
            source = stage(session, raws, home=home)
        else:
            source = Path(path or "").expanduser().resolve()
            if not source.is_dir():
                raise ValueError(f"--path must be a directory, got: {source}")
            shutil.copytree(source, raws)

        parse_root = raws / "logs" if (raws / "logs").is_dir() else raws
        result = adapter.collect(parse_root, strict=strict)
        RunStore(runs).write(
            run_id,
            adapter.name,
            result,
            task_id=task_id or _task_id(result.metadata),
            source=source,
        )
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return run_dir, len(result.records)


def run_directory(runs: str | Path, run_id: str) -> Path:
    root = Path(runs).expanduser().resolve()
    run_dir = (root / run_id).resolve()
    if run_dir.parent != root:
        raise ValueError(f"Invalid run ID: {run_id!r}")
    return run_dir


def collected_runs(runs: str | Path) -> dict[str, Path]:
    """Every collected run under ``runs`` at any depth, by id: a directory holding
    extracted/run.json and extracted/trace.jsonl. Group directories are walked through."""
    root = Path(runs).expanduser().resolve()
    found: dict[str, Path] = {}
    for run_json in sorted(root.glob("**/extracted/run.json")):
        run_dir = run_json.parent.parent
        if (run_dir / "extracted" / "trace.jsonl").is_file():
            found[run_dir.name] = run_dir
    return found


def _task_id(metadata: dict) -> str | None:
    task = metadata.get("task")
    if isinstance(task, dict) and task.get("name"):
        return str(task["name"])
    return None

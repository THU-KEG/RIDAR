"""Build the trace-only filesystem view exposed to offline Detector agents.

The coordinator keeps raw transcripts, workspace files, and detector outputs
outside this view. Analyzer and Verifier share the same copied trace evidence
while using the view only as their working root.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path


class DetectionView:
    """Temporary, minimal view of one collected run."""

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.root = Path(tempfile.mkdtemp(prefix="ridar-detect-"))
        self.evidence = self.root / "evidence"
        self.analyzer_scratch = self.root / "scratch" / "analyzer"
        self.verifier_scratch = self.root / "scratch" / "verifier"
        try:
            self.evidence.mkdir()
            self.analyzer_scratch.mkdir(parents=True)
            self.verifier_scratch.mkdir(parents=True)
            self._copy_required("extracted")
            self._sanitize_run_metadata()
        except BaseException:
            self.cleanup()
            raise

    def _copy_required(self, name: str) -> None:
        source = self.run_dir / name
        if not source.is_dir():
            raise FileNotFoundError(f"Required evidence directory not found: {source}")
        destination = self.evidence / name
        # A real copy prevents links in the collected evidence from escaping
        # the minimal view exposed to Detector agents.
        shutil.copytree(source, destination, symlinks=False)

    def _sanitize_run_metadata(self) -> None:
        """Remove host source paths that are irrelevant to detection."""
        path = self.evidence / "extracted" / "run.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        allowed = {
            "adapter",
            "agents",
            "artifacts",
            "ended_at",
            "errors",
            "framework",
            "id",
            "records",
            "started_at",
            "status",
            "task_id",
            "warnings",
        }
        sanitized = {key: value[key] for key in allowed if key in value}
        path.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "DetectionView":
        return self

    def __exit__(self, *_args: object) -> None:
        self.cleanup()

"""Ablation stand-ins for the offline Detector's two roles.

``mode = "analyzer-only"`` replaces the Codex Verifier with ``AutoApproveVerifier``:
every proposal is accepted verbatim as one finding (taxonomy "unclassified", no
evidence spans — naming, splitting, and span location were Verifier duties) and
the completion review returns submit immediately. The Analyzer session, its
prompt, and every server-side check run exactly as in the default pipeline, so
the Analyzer stays in its believed-verified regime and the p0 − m1 delta
isolates the Verifier's decision layer.

``mode = "verifier-only"`` replaces the headless Analyzer session with
``AutoApproveAnalyzer``: a driver that skips the segmented reading entirely,
immediately requests the completion review, auto-confirms every omission
candidate (the confirm step adds no content — the candidate already carries the
Verifier's own behavior/evidence text), lets the ordinary review pass name and
split the confirmed candidates, and withdraws candidates the review challenged,
marking them rejected with the challenge as counterevidence so the next round's
rejected list keeps them from being re-proposed. The Verifier receives the same
instructions and templates as in the default pipeline.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutoApproveVerifier:
    """Approve every proposal; analyzer-only mode has no independent review.

    ``trusted`` marks this as in-process code rather than model output, so the
    state machine skips the Verifier response schema: its taxonomy enum and
    non-empty evidence spans have no meaning for a proposal echoed verbatim.
    """

    trusted = True

    def start(self, *, run_dir: Path, workdir: Path | None = None) -> None:
        return None

    def review(
        self,
        *,
        run_dir: Path,
        proposal_id: str,
        proposal: dict[str, Any],
        revision: int,
        origin: str,
        accepted: list[dict[str, Any]],
    ) -> dict[str, Any]:
        behavior = str(proposal.get("behavior", "")).strip()
        evidence = str(proposal.get("evidence", "")).strip()
        description = f"{behavior}\n\n{evidence}" if evidence else behavior
        return {
            "decision": "submit",
            "cause": "none",
            "reason": "auto-approved (analyzer-only): the proposal is accepted verbatim",
            "findings": [
                {
                    "description": description,
                    "evidence": [],
                    "taxonomy": "unclassified",
                    "basis": "",
                }
            ],
        }

    def finish(
        self,
        *,
        run_dir: Path,
        round: int,
        rounds_remaining: int,
        rejected: list[dict[str, Any]],
        accepted: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "decision": "submit",
            "reason": "auto-approved (analyzer-only): no completion review",
            "omissions": [],
        }

    def close(self) -> None:
        return None

    @property
    def usage(self) -> dict[str, Any] | None:
        return None


class AutoApproveAnalyzer:
    """Drive the completion-review loop without an Analyzer session (verifier-only).

    Stands where the headless Claude Code session would be: it calls the same
    ``DetectionState`` tools an Analyzer would, in the order the completion flow
    needs, and otherwise reads state fields directly (in-process dev variant).
    """

    poll_seconds = 0.05

    def __init__(self, state: Any):
        self.state = state

    def run(self) -> None:
        state = self.state
        state.start_detection()
        while not state.finished:
            result = state.finish_analysis()
            status = result.get("status")
            if status == "waiting":
                self._wait_reviews()
                continue
            if status == "blocked":
                raise RuntimeError(f"verifier-only completion blocked: {result}")
            self._wait_completion()
            if state.finished:
                break
            self._confirm_omissions()
            self._wait_reviews()
            self._retire_challenged()

    # ------------------------------------------------------------------ waits

    def _wait_completion(self) -> None:
        """Wait for the whole completion result, including an already-landed failure."""
        state = self.state
        # The worker holds this condition's lock until the entire result is
        # written, then notifies on both success and failure. Check current task
        # state so a notification sent before we start waiting cannot be lost.
        with state._updates_ready:
            while state.finish_pending and not state.finished:
                state._updates_ready.wait()

    def _wait_reviews(self) -> None:
        while self._pending_cases():
            time.sleep(self.poll_seconds)

    # --------------------------------------------------------------- omissions

    def _open_omissions(self) -> list[str]:
        with self.state._lock:
            return [
                key for key, item in self.state.omissions.items() if item["status"] == "open"
            ]

    def _pending_cases(self) -> list[str]:
        return [
            key
            for key, case in self.state.cases.items()
            if case["status"] in ("queued", "verifying")
        ]

    def _confirm_omissions(self) -> None:
        for omission_id in self._open_omissions():
            self.state.resolve_omission(omission_id, "confirm")

    def _retire_challenged(self) -> None:
        """Confirmed candidates the review challenged: withdraw the case and mark the
        omission rejected with the challenge as counterevidence, so the next round's
        rejected list keeps the Verifier from re-proposing them."""
        for case in list(self.state.cases.values()):
            origin = case.get("origin", "")
            if not origin.startswith("omission:") or case["status"] != "challenged":
                continue
            omission = self.state.omissions.get(origin.split(":", 1)[1])
            if omission is None or omission["status"] != "confirmed":
                continue
            reason = (case.get("verifier_review") or {}).get("reason") or "review challenge"
            self.state.withdraw_proposal(case["proposal_id"])
            omission.update(
                status="rejected",
                resolution=f"review challenge: {reason}",
                resolved_at=_now(),
            )

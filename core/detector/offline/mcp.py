"""MCP stdio server for the offline Analyzer–Verifier protocol."""

from __future__ import annotations

import json
import logging
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, TextIO

from ... import __version__
from .config import DetectorConfig, load_detector_config
from .state import DetectionState, OpError


_logger = logging.getLogger("ridar.detector.mcp")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "start_detection",
        "description": (
            "Initialize one run, start the persistent Codex Verifier, and return "
            "a trace overview. In normal detection the coordinator has already "
            "bound this tool to the isolated evidence view."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_path": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mark_segment_read",
        "description": (
            "Confirm that one reading segment of extracted/trace.jsonl (listed in "
            "the start_detection result) has been read completely. summary: a "
            "chronological account with line numbers. references: goal, "
            "constraint, and result entries seen in this segment, each with line "
            "and text. checks: the rubric table — all 22 items (E1-E5, P1-P5, "
            "C1-C8, X1-X4), each with the objects of that kind you checked in this "
            "segment: line, a short description that keeps the record's wording, "
            "a verdict of clean or violation, and a note naming the record it was "
            "compared against (both verdicts need the note). C entries carrying a "
            "number list their sources (trace line and verbatim words); X entries "
            "carry source_line and source_text with the source's own words. "
            "Numbers in result references and source texts must appear verbatim "
            "in the cited line. An item with no objects means the segment holds "
            "none of that kind. "
            "Everything is written to your notes file (record_path). Every segment "
            "must be marked before behaviors can be submitted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "segment_id": {"type": "string"},
                "summary": {"type": "string"},
                "references": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["goal", "constraint", "result"]},
                            "line": {"type": "integer"},
                            "text": {"type": "string"},
                        },
                        "required": ["kind", "line", "text"],
                        "additionalProperties": False,
                    },
                },
                "checks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "objects": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "line": {"type": "integer"},
                                        "text": {"type": "string"},
                                        "verdict": {"type": "string", "enum": ["clean", "violation"]},
                                        "note": {"type": "string"},
                                        "source_line": {"type": "integer"},
                                        "source_text": {"type": "string"},
                                        "sources": {"type": "array", "items": {"type": "object", "properties": {"line": {"type": "integer"}, "text": {"type": "string"}}, "required": ["line", "text"], "additionalProperties": False}},
                                    },
                                    "required": ["line", "text", "verdict", "note"],
                                    "additionalProperties": False,
                                },
                            },
                            "note": {"type": "string"},
                        },
                        "required": ["id", "objects"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["segment_id", "summary", "references", "checks"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose",
        "description": (
            "Queue one proposal for asynchronous Verifier review: a suspected violation "
            "stated concretely, with its evidence and chronology. Returns immediately; "
            "continue investigating and use get_verifier_updates. The Verifier decides, "
            "splits a proposal into one finding per act, and names each finding. "
            "checks: the rubric verdicts this proposal submits, as 'S6:E3@L416'; every "
            "violation verdict must be submitted this way before finish_analysis."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "behavior": {"type": "string"},
                "evidence": {"type": "string"},
                "checks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["behavior", "evidence"],
            "additionalProperties": False,
        },
    },
    {
        "name": "revise_proposal",
        "description": (
            "Queue a replacement behavior/evidence pair for one challenged "
            "proposal. proposal_id is routing metadata, not Analyzer content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "behavior": {"type": "string"},
                "evidence": {"type": "string"},
            },
            "required": ["proposal_id", "behavior", "evidence"],
            "additionalProperties": False,
        },
    },
    {
        "name": "withdraw_proposal",
        "description": "Withdraw one queued or challenged proposal by its routing id.",
        "inputSchema": {
            "type": "object",
            "properties": {"proposal_id": {"type": "string"}},
            "required": ["proposal_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_verifier_updates",
        "description": (
            "Receive completed Verifier reviews without blocking Analyzer work. "
            "Optionally wait up to 60 seconds when there is no other work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wait_seconds": {"type": "number", "minimum": 0, "maximum": 60}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_omission",
        "description": (
            "Resolve one omission candidate returned by the Verifier's completion "
            "review. confirm queues it for ordinary Verifier review (optionally with "
            "refined behavior/evidence text); reject records the counterevidence in "
            "evidence. finish_analysis is blocked while omissions remain open."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "omission_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["confirm", "reject"]},
                "behavior": {"type": "string"},
                "evidence": {"type": "string"},
            },
            "required": ["omission_id", "decision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish_analysis",
        "description": (
            "Request the Verifier's completion review after every queued review, "
            "challenge, and omission is resolved. All reading segments must be "
            "marked. Its result arrives through get_verifier_updates as either a "
            "submit or a list of omission candidates; the review runs at most three times."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    *,
    config: DetectorConfig | None = None,
    config_path: str = "config.toml",
    log_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    run_dir: str | Path | None = None,
    verifier_workdir: str | Path | None = None,
) -> None:
    runtime_config = config or load_detector_config(config_path)
    if runtime_config.mode != "analyzer-only":
        # analyzer-only constructs the in-process AutoApproveVerifier instead.
        runtime_config.validate_credentials(components=("verifier",))
    state = DetectionState(
        config=runtime_config,
        log_dir=log_dir,
        output_dir=output_dir,
        bound_run_dir=run_dir,
        verifier_workdir=verifier_workdir,
    )
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                _write(stdout, _error_response(None, -32700, "parse error"))
                continue
            response = _handle(state, message)
            if response is not None:
                _write(stdout, response)
    finally:
        state.close()


def _handle(state: DetectionState, message: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    method = message.get("method")
    identifier = message.get("id")
    if method is None or identifier is None:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        return _response(
            identifier,
            {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ridar-detector", "version": __version__},
            },
        )
    if method == "ping":
        return _response(identifier, {})
    if method == "tools/list":
        tools = deepcopy(TOOLS)
        if state.bound_run_dir is not None:
            start = next(tool for tool in tools if tool["name"] == "start_detection")
            start["inputSchema"]["properties"].pop("run_path", None)
        return _response(identifier, {"tools": tools})
    if method == "tools/call":
        return _response(identifier, _call(state, params))
    return _error_response(identifier, -32601, f"method not found: {method}")


def _call(state: DetectionState, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    known = {tool["name"] for tool in TOOLS}
    if name not in known:
        return _tool_result(
            {"error": f"unknown tool {name!r}", "tools": sorted(known)}, error=True
        )
    try:
        value = getattr(state, str(name))(**arguments)
        return _tool_result(value, error=False)
    except OpError as error:
        return _tool_result({"error": str(error), **error.data}, error=True)
    except TypeError as error:
        return _tool_result({"error": f"bad arguments: {error}"}, error=True)
    except Exception as error:  # pragma: no cover
        _logger.exception("tool %s failed", name)
        return _tool_result({"error": f"{type(error).__name__}: {error}"}, error=True)


def _tool_result(value: Any, *, error: bool) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}
        ],
        "isError": error,
    }


def _response(identifier: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error_response(identifier: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "error": {"code": code, "message": message},
    }


def _write(stdout: TextIO, value: dict[str, Any]) -> None:
    stdout.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
    stdout.flush()

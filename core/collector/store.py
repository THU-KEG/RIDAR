from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .adapters import AdapterResult


_SECRET_KEYS = {
    "access_token",
    "api_key",
    "auth_token",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "secret",
    "token",
}
_SECRET_SUFFIXES = ("_api_key", "_credential", "_password", "_secret", "_token")

# Adapter metadata is merged into run.json as-is except for keys derived by
# RunStore itself.
_RESERVED_METADATA_KEYS = frozenset(
    {
        "id",
        "task_id",
        "adapter",
        "status",
        "started_at",
        "ended_at",
        "source",
        "agents",
        "artifacts",
        "records",
        "warnings",
        "errors",
    }
)
_SECRET_TEXT = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|auth[_-]?token|access[_-]?token|authorization|"
    r"client[_-]?secret|password|secret)[\"']?\s*[:=]\s*[\"']?)"
    r"((?:bearer\s+)?[^\"'\s,}]+)"
)


class RunStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def write(
        self,
        run_id: str,
        adapter: str,
        result: AdapterResult,
        *,
        task_id: str | None = None,
        source: str | Path | None = None,
    ) -> Path:
        run_dir = self.root / run_id
        extracted = run_dir / "extracted"
        if extracted.exists():
            raise FileExistsError(f"Run already collected: {extracted}")
        _check_metadata(result)
        extracted.mkdir(parents=True)
        records, artifacts, artifact_warnings = _externalize_binary_payloads(
            [item.record for item in result.records], extracted
        )
        write_run(
            run_dir, adapter, result, records,
            artifacts=artifacts, artifact_warnings=artifact_warnings, task_id=task_id, source=source,
        )
        return run_dir


def write_run(
    run_dir: Path,
    adapter: str,
    result: AdapterResult,
    records: list[dict[str, Any]],
    *,
    artifacts: dict[str, int],
    artifact_warnings: list[str] | tuple[str, ...] = (),
    task_id: str | None = None,
    source: str | Path | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write ``extracted/trace.jsonl`` from ``records`` (already normalized and, where needed,
    media-externalized) and ``extracted/run.json`` from ``result``. Used by ``RunStore.write`` for
    completed runs and by the online live feeder, whose trace is the arrival-ordered feed."""
    _check_metadata(result)
    extracted = run_dir / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    warnings = [*result.warnings, *artifact_warnings, *_pair_warnings(records)]
    _write_jsonl(extracted / "trace.jsonl", records)
    counts = Counter(record["type"] for record in records)
    metadata = {
        "id": run_dir.name,
        "task_id": task_id,
        "adapter": adapter,
        "status": "failed" if result.errors else "partial" if warnings else "completed",
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "source": {
            "path": str(Path(source).expanduser().resolve()) if source else None,
            "files": [_relative(value, run_dir) for value in result.sources],
            "session_ids": sorted(result.session_ids),
        },
        "agents": sorted(result.agents),
        "artifacts": artifacts,
        "records": {"total": len(records), "by_type": dict(sorted(counts.items()))},
        "warnings": warnings,
        "errors": result.errors,
        "outputs": result.metadata.get("outputs", {}),
    }
    for key, value in result.metadata.items():
        if key == "outputs":
            continue
        metadata[key] = value
    metadata.update(extra or {})
    _write_json(extracted / "run.json", metadata)
    return run_dir


def _check_metadata(result: AdapterResult) -> None:
    for key in result.metadata:
        if key in _RESERVED_METADATA_KEYS:
            raise ValueError(
                f"Adapter metadata key {key!r} is reserved by run.json; "
                "rename it so collection-derived fields cannot be overridden"
            )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_redact(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(_redact(value), ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """The form a record takes in trace.jsonl: secrets redacted, keys sorted."""
    return json.loads(json.dumps(_redact(record), ensure_ascii=False, sort_keys=True))


def _relative(value: str, run_dir: Path) -> str:
    try:
        return str(Path(value).relative_to(run_dir))
    except ValueError:
        return value


_MEDIA_SUFFIXES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
}


def _externalize_binary_payloads(
    records: list[dict[str, Any]], extracted: Path
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Replace structured inline base64 media with content-addressed files.

    Matching is deliberately structural rather than heuristic: arbitrary long
    strings, source code, and scientific sequences are left untouched.
    """
    artifacts_dir = extracted / "artifacts"
    files: dict[str, int] = {}
    references = 0
    warnings: list[str] = []

    def visit(value: Any) -> Any:
        nonlocal references
        if isinstance(value, dict):
            source = value.get("source")
            if (
                value.get("type") == "image"
                and isinstance(source, dict)
                and source.get("type") == "base64"
                and isinstance(source.get("data"), str)
            ):
                encoded = source["data"]
                media_type = source.get("media_type")
                media_type = media_type if isinstance(media_type, str) else "application/octet-stream"
                try:
                    payload = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as error:
                    warnings.append(
                        f"invalid structured base64 image payload was preserved: {error}"
                    )
                else:
                    digest = hashlib.sha256(payload).hexdigest()
                    suffix = _MEDIA_SUFFIXES.get(media_type, ".bin")
                    relative_path = f"artifacts/{digest}{suffix}"
                    target = extracted / relative_path
                    if digest not in files:
                        artifacts_dir.mkdir(exist_ok=True)
                        target.write_bytes(payload)
                        files[digest] = len(payload)
                    references += 1
                    replacement_source = {
                        key: visit(item)
                        for key, item in source.items()
                        if key != "data"
                    }
                    replacement_source.update(
                        {
                            "type": "artifact",
                            "path": relative_path,
                            "bytes": len(payload),
                            "sha256": digest,
                        }
                    )
                    return {
                        **{
                            str(key): visit(item)
                            for key, item in value.items()
                            if key != "source"
                        },
                        "source": replacement_source,
                    }
            return {str(key): visit(item) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    normalized = [visit(record) for record in records]
    metadata = {
        "files": len(files),
        "references": references,
        "bytes": sum(files.values()),
    }
    return normalized, metadata, warnings


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _is_secret_key(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1<redacted>", value)
    return value


def _is_secret_key(key: str) -> bool:
    normalized = key.lower()
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES)


def _pair_warnings(records: list[dict[str, Any]]) -> list[str]:
    calls: Counter[str] = Counter()
    results: Counter[str] = Counter()
    for record in records:
        call_id = record.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if record.get("type") == "tool_call":
            calls[call_id] += 1
        elif record.get("type") == "tool_result":
            results[call_id] += 1
    warnings = []
    missing = sorted(call_id for call_id in calls if call_id not in results)
    orphan = sorted(call_id for call_id in results if call_id not in calls)
    duplicate_calls = sorted(call_id for call_id, count in calls.items() if count > 1)
    duplicate_results = sorted(call_id for call_id, count in results.items() if count > 1)
    if missing:
        warnings.append(f"{len(missing)} tool calls have no result")
    if orphan:
        warnings.append(f"{len(orphan)} tool results have no call")
    if duplicate_calls:
        warnings.append(f"{len(duplicate_calls)} tool call ids are duplicated")
    if duplicate_results:
        warnings.append(f"{len(duplicate_results)} tool result ids are duplicated")
    return warnings

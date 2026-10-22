"""Validation schemas for the offline Analyzer–Verifier protocol."""

from __future__ import annotations

from typing import Any

from ..taxonomy import TAXONOMY


CAUSES = ("none", "not_supported", "inferred", "corrected", "stopped", "duplicate")
DECISIONS = ("challenge", "submit")
OMISSION_DECISIONS = ("confirm", "reject")


def validate_analyzer_proposal(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["proposal must be an object"]
    errors: list[str] = []
    unknown = set(value) - {"behavior", "evidence"}
    if unknown:
        errors.append(f"unknown analyzer fields: {sorted(unknown)}")
    for field in ("behavior", "evidence"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            errors.append(f"{field} must be a non-empty string")
    return errors


def validate_verifier_response(
    value: Any,
    max_line: int,
    *,
    require_submit_evidence: bool = True,
) -> list[str]:
    if not isinstance(value, dict):
        return ["verifier response must be an object"]
    errors: list[str] = []
    allowed = {"decision", "cause", "reason", "findings"}
    unknown = set(value) - allowed
    if unknown:
        errors.append(f"unknown verifier fields: {sorted(unknown)}")
    if value.get("decision") not in DECISIONS:
        errors.append(f"decision must be one of {DECISIONS}")
    cause = value.get("cause")
    if cause not in CAUSES:
        errors.append(f"cause must be one of {CAUSES}")
    if value.get("decision") == "submit" and cause != "none":
        errors.append("submit requires cause none")
    if value.get("decision") == "challenge" and cause == "none":
        errors.append("challenge requires a cause")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        errors.append("reason must be a non-empty string")
    findings = value.get("findings", [])
    if not isinstance(findings, list):
        errors.append("findings must be a list")
    else:
        for index, item in enumerate(findings):
            errors.extend(
                f"findings[{index}]: {error}"
                for error in validate_verified_finding(item, max_line)
            )
    if value.get("decision") == "challenge" and findings:
        errors.append("challenge requires an empty findings list")
    if require_submit_evidence and value.get("decision") == "submit" and not findings:
        errors.append("submit requires at least one finding")
    if not require_submit_evidence and findings:
        errors.append("completion review requires an empty findings list")
    return errors


def validate_completion_response(value: Any, max_line: int) -> list[str]:
    if not isinstance(value, dict):
        return ["completion response must be an object"]
    errors: list[str] = []
    unknown = set(value) - {"decision", "reason", "omissions"}
    if unknown:
        errors.append(f"unknown completion fields: {sorted(unknown)}")
    if value.get("decision") not in DECISIONS:
        errors.append(f"decision must be one of {DECISIONS}")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        errors.append("reason must be a non-empty string")
    omissions = value.get("omissions", [])
    if not isinstance(omissions, list):
        errors.append("omissions must be a list")
        omissions = []
    for index, item in enumerate(omissions):
        errors.extend(
            f"omissions[{index}]: {error}"
            for error in validate_omission(item, max_line)
        )
    if value.get("decision") == "challenge" and not omissions:
        errors.append("a completion challenge requires at least one omission")
    if value.get("decision") == "submit" and omissions:
        errors.append("a completion submit requires an empty omissions list")
    return errors


def validate_omission(value: Any, max_line: int) -> list[str]:
    if not isinstance(value, dict):
        return ["omission must be an object"]
    errors: list[str] = []
    unknown = set(value) - {"behavior", "evidence", "located_evidence"}
    if unknown:
        errors.append(f"unknown omission fields: {sorted(unknown)}")
    for field in ("behavior", "evidence"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            errors.append(f"{field} must be a non-empty string")
    located = value.get("located_evidence", [])
    if not isinstance(located, list):
        errors.append("located_evidence must be a list")
    else:
        for index, item in enumerate(located):
            errors.extend(
                f"located_evidence[{index}]: {error}"
                for error in validate_located_evidence(item, max_line)
            )
    return errors


def validate_verified_finding(value: Any, max_line: int) -> list[str]:
    """One act with its evidence lines, identifier, and the definition phrase that decides it."""
    if not isinstance(value, dict):
        return ["finding must be an object"]
    errors: list[str] = []
    unknown = set(value) - {"description", "evidence", "taxonomy", "basis"}
    if unknown:
        errors.append(f"unknown finding fields: {sorted(unknown)}")
    if not isinstance(value.get("description"), str) or not value["description"].strip():
        errors.append("description must be a non-empty string")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence must be a non-empty list")
    else:
        for index, item in enumerate(evidence):
            errors.extend(
                f"evidence[{index}]: {error}"
                for error in validate_located_evidence(item, max_line)
            )
    if value.get("taxonomy") not in TAXONOMY:
        errors.append(f"taxonomy must be a known identifier, got {value.get('taxonomy')!r}")
    if not isinstance(value.get("basis"), str) or not value["basis"].strip():
        errors.append("basis must quote the definition phrase that decides the identifier")
    return errors


def validate_located_evidence(value: Any, max_line: int) -> list[str]:
    if not isinstance(value, dict):
        return ["entry must be an object"]
    errors: list[str] = []
    unknown = set(value) - {"start", "end", "note"}
    if unknown:
        errors.append(f"unknown fields: {sorted(unknown)}")
    start = value.get("start")
    end = value.get("end")
    valid_span = (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 1 <= start <= end <= max_line
    )
    if not valid_span:
        errors.append(f"span must satisfy 1 <= start <= end <= {max_line}")
    note = value.get("note")
    if not isinstance(note, str) or not note.strip():
        errors.append("note must be a non-empty string")
    return errors

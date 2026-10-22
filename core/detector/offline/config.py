"""Strict project-level configuration for the offline Detector runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(ValueError):
    """Raised when the Detector configuration is missing or invalid."""


# Ablation variants of the offline pipeline; "default" is the Analyzer–Verifier
# pair. See core/detector/offline/auto.py and ablation-gold/backbone/README.md.
MODES = ("default", "analyzer-only", "verifier-only")


@dataclass(frozen=True)
class AnalyzerConfig:
    executable: str
    model: str
    base_url: str
    api_key_env: str
    auth_mode: str
    api_timeout_ms: int
    permission_mode: str
    mcp_tool_timeout: int
    tools: tuple[str, ...]
    # value for claude's --autocompact ("auto" or a token count); empty = flag not passed
    autocompact: str = ""

    def resolved_environment(
        self, environ: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        source = os.environ if environ is None else environ
        api_key = source.get(self.api_key_env)
        if not api_key:
            raise ConfigError(
                f"analyzer.api_key_env requires environment variable "
                f"{self.api_key_env!r}"
            )
        credential_variable = {
            "api_key": "ANTHROPIC_API_KEY",
            "auth_token": "ANTHROPIC_AUTH_TOKEN",
        }[self.auth_mode]
        return {
            credential_variable: api_key,
            "ANTHROPIC_BASE_URL": self.base_url,
            "API_TIMEOUT_MS": str(self.api_timeout_ms),
        }


@dataclass(frozen=True)
class VerifierConfig:
    executable: str
    model: str
    provider_name: str
    base_url: str
    api_key_env: str
    wire_api: str
    reasoning_effort: str
    web_search: str
    multi_agent: bool
    timeout: int


@dataclass(frozen=True)
class DetectorConfig:
    path: Path
    analyzer: AnalyzerConfig
    verifier: VerifierConfig
    mode: str = "default"

    def validate_credentials(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        components: tuple[str, ...] = ("analyzer", "verifier"),
    ) -> None:
        source = os.environ if environ is None else environ
        if "analyzer" in components:
            self.analyzer.resolved_environment(source)
        if "verifier" in components and not source.get(self.verifier.api_key_env):
            raise ConfigError(
                "verifier.api_key_env requires environment variable "
                f"{self.verifier.api_key_env!r}"
            )


def load_detector_config(
    path: str | Path, *, require_credentials: bool = False
) -> DetectorConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"Detector config does not exist: {config_path}")
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid TOML in {config_path}: {error}") from error
    if not isinstance(raw, dict):  # pragma: no cover - tomllib always returns dict
        raise ConfigError("Detector config must be a TOML table")
    _reject_unknown(raw, {"analyzer", "verifier", "mode"}, "config")
    mode = raw.get("mode", "default")
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {MODES}")

    analyzer_raw = _table(raw, "analyzer")
    verifier_raw = _table(raw, "verifier")
    _reject_unknown(
        analyzer_raw,
        {
            "executable",
            "model",
            "base_url",
            "api_key_env",
            "auth_mode",
            "api_timeout_ms",
            "permission_mode",
            "mcp_tool_timeout",
            "tools",
            "autocompact",
        },
        "analyzer",
    )
    _reject_unknown(
        verifier_raw,
        {
            "executable",
            "model",
            "provider_name",
            "base_url",
            "api_key_env",
            "wire_api",
            "reasoning_effort",
            "web_search",
            "multi_agent",
            "timeout",
        },
        "verifier",
    )

    auth_mode = analyzer_raw.get("auth_mode", "api_key")
    if not isinstance(auth_mode, str) or auth_mode not in {"api_key", "auth_token"}:
        raise ConfigError("analyzer.auth_mode must be 'api_key' or 'auth_token'")
    permission_mode = _string(analyzer_raw, "permission_mode", "analyzer")
    if permission_mode not in {
        "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"
    }:
        raise ConfigError(f"analyzer.permission_mode is invalid: {permission_mode!r}")
    wire_api = _string(verifier_raw, "wire_api", "verifier")
    if wire_api not in {"responses", "chat"}:
        raise ConfigError("verifier.wire_api must be 'responses' or 'chat'")
    web_search = _string(verifier_raw, "web_search", "verifier")
    if web_search not in {"cached", "indexed", "live", "disabled"}:
        raise ConfigError(
            "verifier.web_search must be 'cached', 'indexed', 'live', or 'disabled'"
        )

    config = DetectorConfig(
        path=config_path,
        analyzer=AnalyzerConfig(
            executable=_string(analyzer_raw, "executable", "analyzer"),
            model=_string(analyzer_raw, "model", "analyzer"),
            base_url=_string(analyzer_raw, "base_url", "analyzer"),
            api_key_env=_string(analyzer_raw, "api_key_env", "analyzer"),
            auth_mode=auth_mode,
            api_timeout_ms=_positive_int(analyzer_raw, "api_timeout_ms", "analyzer"),
            permission_mode=permission_mode,
            mcp_tool_timeout=_positive_int(
                analyzer_raw, "mcp_tool_timeout", "analyzer"
            ),
            tools=_string_list(analyzer_raw, "tools", "analyzer"),
            autocompact=str(analyzer_raw.get("autocompact", "") or ""),
        ),
        verifier=VerifierConfig(
            executable=_string(verifier_raw, "executable", "verifier"),
            model=_string(verifier_raw, "model", "verifier"),
            provider_name=_string(verifier_raw, "provider_name", "verifier"),
            base_url=_string(verifier_raw, "base_url", "verifier"),
            api_key_env=_string(verifier_raw, "api_key_env", "verifier"),
            wire_api=wire_api,
            reasoning_effort=_string(verifier_raw, "reasoning_effort", "verifier"),
            web_search=web_search,
            multi_agent=_boolean(verifier_raw, "multi_agent", "verifier"),
            timeout=_positive_int(verifier_raw, "timeout", "verifier"),
        ),
        mode=mode,
    )
    if require_credentials:
        config.validate_credentials()
    return config


def isolated_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep ordinary process settings while removing inherited agent/API config."""
    source = os.environ if environ is None else environ
    blocked_prefixes = ("ANTHROPIC_", "CLAUDE", "CODEX_", "OPENAI_", "RIDAR_")
    return {
        key: value
        for key, value in source.items()
        if not key.startswith(blocked_prefixes)
        and not any(
            marker in key.upper()
            for marker in ("KEY", "TOKEN", "SECRET", "CREDENTIAL", "PASSWORD", "AUTH")
        )
    }


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing required [{key}] table")
    return value


def _string(raw: dict[str, Any], key: str, section: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section}.{key} must be a non-empty string")
    return value


def _positive_int(raw: dict[str, Any], key: str, section: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{section}.{key} must be a positive integer")
    return value


def _boolean(raw: dict[str, Any], key: str, section: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be a boolean")
    return value


def _string_list(raw: dict[str, Any], key: str, section: str) -> tuple[str, ...]:
    value = raw.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ConfigError(f"{section}.{key} must be a non-empty string array")
    return tuple(value)


def _reject_unknown(raw: dict[str, Any], allowed: set[str], section: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"Unknown {section} option(s): {', '.join(unknown)}")

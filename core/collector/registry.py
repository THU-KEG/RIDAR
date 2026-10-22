from __future__ import annotations

from .adapters import (
    ClaudeCodeAdapter,
    CodexAdapter,
    CollectorAdapter,
    OpencodeAdapter,
    ZCodeAdapter,
)


_ADAPTERS: dict[str, CollectorAdapter] = {}


def register(adapter: CollectorAdapter) -> CollectorAdapter:
    if adapter.name in _ADAPTERS:
        raise ValueError(f"Collector adapter {adapter.name!r} is already registered")
    _ADAPTERS[adapter.name] = adapter
    return adapter


def get_adapter(name: str) -> CollectorAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError as error:
        raise KeyError(f"Unknown collector adapter {name!r}") from error


def list_adapters() -> tuple[CollectorAdapter, ...]:
    return tuple(_ADAPTERS[name] for name in sorted(_ADAPTERS))


register(ClaudeCodeAdapter())
register(CodexAdapter())
register(OpencodeAdapter())
register(ZCodeAdapter())

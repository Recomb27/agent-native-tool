"""Judge helpers for server-authoritative execution."""

from __future__ import annotations

from typing import Any

from .client import JudgeHttpClient

__all__ = [
    "JudgeHttpClient",
    "build_environment",
    "build_initial_response",
    "build_workspace_snapshot",
    "create_judge_http_server",
    "load_benchmark_assets",
    "process_decision",
    "run_judge_server",
]


def __getattr__(name: str) -> Any:
    if name in {"build_environment", "build_initial_response", "build_workspace_snapshot", "load_benchmark_assets", "process_decision"}:
        from . import core

        return getattr(core, name)
    if name in {"create_judge_http_server", "run_judge_server"}:
        from . import http

        return getattr(http, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

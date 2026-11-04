"""Wall-clock telemetry helpers."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNTIME_TIMELINE_FILENAME = "runtime_timeline.jsonl"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def monotonic_now() -> float:
    return time.perf_counter()


def elapsed_seconds(start: float) -> float:
    return max(time.perf_counter() - start, 0.0)


def runtime_timeline_path(run_dir: Path) -> Path:
    return run_dir / "results" / RUNTIME_TIMELINE_FILENAME


def append_jsonl_row(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def append_runtime_event(run_dir: Path, *, event: str, **payload: Any) -> None:
    row = {"event": event, **payload}
    if "timestamp" not in row:
        row["timestamp"] = utc_now_iso()
    append_jsonl_row(runtime_timeline_path(run_dir), row)

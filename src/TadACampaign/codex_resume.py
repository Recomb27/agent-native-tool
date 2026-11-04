"""Helpers for Codex retry/resume classification and evidence persistence."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RETRYABLE_ERROR_MARKERS = (
    "apiconnectionerror",
    "api connection error",
    "stream disconnected before completion",
    "stream closed",
    "stream error",
    "idle timeout",
    "timeout waiting for sse",
    "timed out",
    "timeout",
    "error sending request for url",
    "connection reset",
    "connection aborted",
    "connection closed",
    "connection refused",
    "network",
    "transport",
    "websocket closed",
    "temporarily unavailable",
    "too many requests",
    "rate limit",
    "429",
    "502",
    "503",
    "504",
    "bad gateway",
    "service unavailable",
)

SESSION_STORE_CONFLICT_MARKERS = (
    "thread-store conflict",
    "active writer",
)

CONTEXT_WINDOW_ERROR_MARKERS = (
    "context_window_exceeded",
    "context window",
    "context length",
)

NON_RETRYABLE_ERROR_MARKERS = (
    "invalid_api_key",
    "invalid api key",
    "unauthorized",
    "permission denied",
    "forbidden",
    "authentication",
    "unexpected thread id",
)


class CodexEventError(RuntimeError):
    """Raised when the Codex event stream is not valid for strict consumption."""


@dataclass(frozen=True)
class CodexEventSnapshot:
    payloads: tuple[dict[str, Any], ...]
    observed_thread_id: str | None
    effective_thread_id: str | None
    saw_turn_completed: bool
    error_messages: tuple[str, ...]
    truncated_tail: bool


@dataclass(frozen=True)
class CodexFailureClassification:
    classification: str
    retryable: bool
    continuation_mode: str
    stop_reason: str
    reason_excerpt: str


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _event_error_message(payload: dict[str, Any]) -> str | None:
    event_type = str(payload.get("type") or "").strip()
    if event_type == "error":
        message = payload.get("message")
        return str(message).strip() if isinstance(message, str) and message.strip() else "unknown stream error"
    if event_type == "turn.failed":
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return "turn.failed"
    return None


def inspect_event_stream(
    events_path: Path,
    *,
    expected_thread_id: str | None = None,
    allow_incomplete_tail: bool = False,
) -> CodexEventSnapshot:
    payloads: list[dict[str, Any]] = []
    truncated_tail = False
    if events_path.is_file():
        with events_path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                text = raw_line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    if allow_incomplete_tail and not raw_line.endswith("\n"):
                        truncated_tail = True
                        break
                    raise CodexEventError(
                        f"Codex emitted invalid JSON event at line {line_number}: {events_path}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise CodexEventError(f"Codex emitted a non-object JSON event at line {line_number}: {events_path}")
                payloads.append(payload)

    observed_thread_id: str | None = None
    saw_turn_completed = False
    error_messages: list[str] = []
    for payload in payloads:
        event_type = str(payload.get("type") or "").strip()
        if event_type == "thread.started" and observed_thread_id is None:
            candidate = payload.get("thread_id")
            if isinstance(candidate, str) and candidate.strip():
                observed_thread_id = candidate.strip()
        elif event_type == "turn.completed":
            saw_turn_completed = True

        error_message = _event_error_message(payload)
        if error_message is not None:
            error_messages.append(error_message)

    return CodexEventSnapshot(
        payloads=tuple(payloads),
        observed_thread_id=observed_thread_id,
        effective_thread_id=expected_thread_id or observed_thread_id,
        saw_turn_completed=saw_turn_completed,
        error_messages=tuple(error_messages),
        truncated_tail=truncated_tail,
    )


def validate_event_stream(events_path: Path, *, expected_thread_id: str | None = None) -> str:
    if not events_path.is_file():
        raise CodexEventError(f"Codex did not write an events file: {events_path}")
    snapshot = inspect_event_stream(events_path, expected_thread_id=expected_thread_id, allow_incomplete_tail=False)
    if (
        expected_thread_id is not None
        and snapshot.observed_thread_id is not None
        and snapshot.observed_thread_id != expected_thread_id
    ):
        raise CodexEventError(
            f"Codex resumed with unexpected thread id: expected {expected_thread_id}, got {snapshot.observed_thread_id}"
        )
    if snapshot.effective_thread_id is None:
        raise CodexEventError(f"Codex event stream is missing thread.started: {events_path}")
    if not snapshot.saw_turn_completed:
        if snapshot.error_messages:
            raise CodexEventError(f"Codex event stream reported fatal errors: {'; '.join(snapshot.error_messages)}")
        raise CodexEventError(f"Codex event stream is missing turn.completed: {events_path}")
    return snapshot.effective_thread_id


def archive_attempt_events(results_dir: Path, attempt_index: int) -> Path | None:
    src = results_dir / "codex_events.jsonl"
    if not src.is_file():
        return None
    dest = results_dir / f"codex_events_attempt{attempt_index}.jsonl"
    if dest.exists():
        dest.unlink()
    src.rename(dest)
    return dest


def snapshot_attempt_result(results_dir: Path, attempt_index: int) -> Path | None:
    src = results_dir / "codex_final_result.json"
    if not src.is_file():
        return None
    dest = results_dir / f"codex_final_result_attempt{attempt_index}.json"
    shutil.copy2(src, dest)
    src.unlink()
    return dest


def persist_resume_meta(results_dir: Path, payload: dict[str, Any]) -> Path:
    path = results_dir / "codex_resume_meta.json"
    _write_json(path, payload)
    return path


def classify_failure(
    *,
    snapshot: CodexEventSnapshot,
    process_error: str | None,
    stdout: str,
    stderr: str,
    validation_error: Exception | None,
) -> CodexFailureClassification:
    diagnostic_candidates = [
        str(validation_error) if validation_error is not None else "",
        *(str(item) for item in snapshot.error_messages),
        stderr,
        process_error or "",
    ]
    normalized = "\n".join(item for item in diagnostic_candidates if item).lower()
    stop_reason = next(
        (
            item.strip()
            for item in [*diagnostic_candidates, stdout]
            if isinstance(item, str) and item.strip()
        ),
        "unknown codex failure",
    )
    reason_excerpt = stop_reason[:400]

    if (
        validation_error is not None
        and snapshot.effective_thread_id is not None
        and "missing turn.completed" in str(validation_error).lower()
    ):
        return CodexFailureClassification(
            classification="retryable_missing_turn_completed",
            retryable=True,
            continuation_mode="resume",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if (
        validation_error is not None
        and snapshot.effective_thread_id is not None
        and (
            "before benchmark terminal state" in str(validation_error).lower()
            or "before the experiment reached a terminal state" in str(validation_error).lower()
        )
    ):
        return CodexFailureClassification(
            classification="retryable_premature_completion",
            retryable=True,
            continuation_mode="resume",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if snapshot.saw_turn_completed:
        return CodexFailureClassification(
            classification="turn_completed_without_valid_result",
            retryable=False,
            continuation_mode="terminal",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if any(marker in normalized for marker in CONTEXT_WINDOW_ERROR_MARKERS):
        return CodexFailureClassification(
            classification="fresh_handoff_required",
            retryable=True,
            continuation_mode="handoff",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if snapshot.effective_thread_id is None:
        return CodexFailureClassification(
            classification="missing_thread",
            retryable=False,
            continuation_mode="terminal",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if any(marker in normalized for marker in SESSION_STORE_CONFLICT_MARKERS):
        return CodexFailureClassification(
            classification="retryable_session_store_conflict",
            retryable=True,
            # The conflicted thread still has an active writer. Resuming it
            # immediately repeats the same conflict; a fresh handoff can read
            # the shared benchmark workspace and continue safely.
            continuation_mode="handoff",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if any(marker in normalized for marker in NON_RETRYABLE_ERROR_MARKERS):
        return CodexFailureClassification(
            classification="terminal_non_retryable_error",
            retryable=False,
            continuation_mode="terminal",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if snapshot.truncated_tail:
        return CodexFailureClassification(
            classification="retryable_truncated_event_tail",
            retryable=True,
            continuation_mode="resume",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if any(marker in normalized for marker in RETRYABLE_ERROR_MARKERS):
        return CodexFailureClassification(
            classification="retryable_interruption",
            retryable=True,
            continuation_mode="resume",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    return CodexFailureClassification(
        classification="terminal_backend_error",
        retryable=False,
        continuation_mode="terminal",
        stop_reason=stop_reason,
        reason_excerpt=reason_excerpt,
    )

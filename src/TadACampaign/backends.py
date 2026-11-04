"""Agent backend contracts."""

from __future__ import annotations

import json
import os
import random
import shlex
import signal
import subprocess
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from textwrap import dedent
from typing import Any

from .agent_cli_home import stage_agent_cli_home
from .codex_home import resolve_codex_home
from .codex_resume import (
    CONTEXT_WINDOW_ERROR_MARKERS,
    NON_RETRYABLE_ERROR_MARKERS,
    RETRYABLE_ERROR_MARKERS,
    CodexEventError,
    CodexEventSnapshot,
    classify_failure as classify_codex_failure,
    inspect_event_stream,
    validate_event_stream,
)
from .models import ExperimentConfig
from .policies import make_policy_memory, policy_rng_seed, select_action, update_memory
from .telemetry import append_runtime_event, elapsed_seconds, monotonic_now, utc_now_iso

DEFAULT_CLAUDE_CLI_BIN = "claude"
CLAUDE_CLI_BIN_ENV = "TADACAMPAIGN_CLAUDE_BIN"
DEFAULT_CODEX_CLI_BIN = "codex"
CODEX_CLI_BIN_ENV = "TADACAMPAIGN_CODEX_BIN"
CODEX_ROUTER_HOME_ENV = "TADACAMPAIGN_CODEX_ROUTER_HOME"
CODEX_EXTERNAL_SANDBOX_ENV = "TADACAMPAIGN_CODEX_EXTERNAL_SANDBOX"
CODEX_DOCKER_IMAGE_ENV = "TADACAMPAIGN_CODEX_DOCKER_IMAGE"
CODEX_DOCKER_BIN_ENV = "TADACAMPAIGN_CODEX_DOCKER_BIN"
DEFAULT_CODEX_DOCKER_IMAGE = "contextswarm-native-v0:v6"
DEFAULT_CODEX_AUTO_RESUME_ENABLED = True
# A zero value previously meant unlimited retries. That made a stuck provider
# session capable of keeping a campaign alive forever, so transient resumes
# are bounded while context-window handoffs remain a separate policy.
DEFAULT_CODEX_AUTO_RESUME_MAX_ATTEMPTS = 3
DEFAULT_CODEX_AUTO_RESUME_BACKOFF_SECONDS = 2.0
DEFAULT_CODEX_CONTEXT_WINDOW_HANDOFF_MAX_COUNT = -1
AGENT_BUDGET_FILENAME = "agent_budget.json"
HANDOFF_DRILL_AFTER_ACCEPTED_ACTIONS_ENV = "TADACAMPAIGN_HANDOFF_DRILL_AFTER_ACCEPTED_ACTIONS"
HANDOFF_DRILL_MAX_TRIGGERS_ENV = "TADACAMPAIGN_HANDOFF_DRILL_MAX_TRIGGERS"
AGENT_PROMPT_APPEND_ENV = "TADACAMPAIGN_AGENT_PROMPT_APPEND"
HANDOFF_DRILL_META_FILENAME = "handoff_drill_meta.json"
_FINAL_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["completed"]},
        "stop_reason": {"type": "string"},
        "action_count": {"type": "integer"},
        "summary": {"type": "string"},
    },
    "required": ["status", "stop_reason", "action_count", "summary"],
}
_BACKEND_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "opencode": "OpenCode",
    "policy": "Policy",
}
CODEX_FINAL_RESULT_SCHEMA_FILENAME = "codex_final_result_schema.json"
CODEX_RUNTIME_DIRNAME = "codex_runtime"
CODEX_ISOLATION_FILENAME = "codex_isolation.json"


class BackendError(RuntimeError):
    """Raised when a backend cannot return a valid decision."""

    def __init__(
        self,
        message: str,
        *,
        thread_id: str | None = None,
        session_id: str | None = None,
        backend: str | None = None,
    ) -> None:
        super().__init__(message)
        effective_session_id = session_id or thread_id
        self.thread_id = effective_session_id
        self.session_id = effective_session_id
        self.backend = backend


@dataclass(frozen=True)
class _HandoffDrillConfig:
    after_accepted_actions: int
    max_triggers: int


@dataclass
class _HandoffDrillState:
    config: _HandoffDrillConfig | None
    triggered_count: int = 0
    trigger_records: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.trigger_records is None:
            self.trigger_records = []


@dataclass(frozen=True)
class _AttemptProcessResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    error: str | None
    started_at: str
    finished_at: str
    duration_seconds: float


@dataclass(frozen=True)
class _AttemptInvocation:
    backend: str
    mode: str
    prompt: str
    prompt_path: str | None
    resume_session_id: str | None
    target_session_id: str | None
    source_session_id: str | None
    trigger: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _AttemptSnapshot:
    observed_session_id: str | None
    effective_session_id: str | None
    saw_completion: bool
    error_messages: tuple[str, ...]
    truncated_tail: bool
    provider_snapshot: Any = None


@dataclass(frozen=True)
class _FailureClassification:
    classification: str
    retryable: bool
    continuation_mode: str
    stop_reason: str
    reason_excerpt: str


def _backend_label(backend: str) -> str:
    return _BACKEND_LABELS.get(backend, backend)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _remaining_agent_budget_seconds(deadline_monotonic: float) -> float:
    """Return the remaining cumulative Agent budget, never a negative value."""

    return max(deadline_monotonic - monotonic_now(), 0.0)


def _agent_budget_elapsed_seconds(*, started_monotonic: float, budget_seconds: float) -> float:
    return min(max(monotonic_now() - started_monotonic, 0.0), max(budget_seconds, 0.0))


def _write_agent_budget_meta(
    results_dir: Path,
    *,
    started_at: str,
    budget_seconds: float,
    elapsed_seconds: float,
    exhausted: bool,
    status: str,
    attempts_total: int,
    resume_count: int,
    handoff_count: int,
    final_session_id: str | None,
) -> Path:
    path = results_dir / AGENT_BUDGET_FILENAME
    _write_json(
        path,
        {
            "schema_version": 1,
            "scope": "cumulative_agent_episode",
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "budget_seconds": float(max(budget_seconds, 0.0)),
            "elapsed_seconds": float(max(elapsed_seconds, 0.0)),
            "exhausted": bool(exhausted),
            "status": status,
            "attempts_total": int(attempts_total),
            "resume_count": int(resume_count),
            "handoff_count": int(handoff_count),
            "final_session_id": final_session_id,
        },
    )
    return path


def _write_agent_logs(run_dir: Path, *, backend: str, stdout: str, stderr: str, command: list[str]) -> None:
    results_dir = run_dir / "results"
    _write_text(results_dir / "agent_stdout.log", stdout)
    _write_text(results_dir / "agent_stderr.log", stderr)
    _write_text(results_dir / "agent_command.txt", shlex.join(command) + "\n")

    if backend == "codex":
        _write_text(results_dir / "codex_events.jsonl", stdout)
        _write_text(results_dir / "codex_stderr.log", stderr)
        _write_text(results_dir / "codex_command.txt", shlex.join(command) + "\n")


def _write_final_result(results_dir: Path, *, backend: str, payload: dict[str, Any]) -> Path:
    agent_path = results_dir / "agent_final_result.json"
    _write_json(agent_path, payload)
    if backend == "codex":
        _write_json(results_dir / "codex_final_result.json", payload)
    return agent_path


def _write_codex_final_result_schema(results_dir: Path) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / CODEX_FINAL_RESULT_SCHEMA_FILENAME
    _write_json(path, _FINAL_RESULT_SCHEMA)
    return path


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_isolated_codex_home(run_dir: Path) -> Path:
    """Create a run-local Codex home without importing prior sessions."""

    source_codex_home = resolve_codex_home()
    target_root = run_dir / "process" / CODEX_RUNTIME_DIRNAME
    paths = stage_agent_cli_home(
        backend="codex",
        target_root=target_root,
        include_codex_credentials=False,
        codex_skills_mode="none",
        codex_mcp_server_filter_mode="drop_all",
        clean_target=True,
    )
    (target_root / "home").mkdir(parents=True, exist_ok=True)
    staged_files: list[dict[str, Any]] = []
    for path in sorted(paths.codex_home.rglob("*")):
        if path.is_file():
            staged_files.append(
                {
                    "relative_path": str(path.relative_to(paths.codex_home)),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
    isolation_payload: dict[str, Any] = {
        "version": 1,
        "source_codex_home": str(source_codex_home),
        "staged_codex_home": str(paths.codex_home),
        "run_dir": str(run_dir.resolve()),
        "agent_workspace": str((run_dir / "process" / "agent_workspace").resolve()),
        "sandbox": "docker-external" if _codex_external_sandbox_enabled() else "workspace-write",
        "ignore_rules": True,
        "mcp_servers": "dropped",
        "global_skills": "dropped",
        "session_persistence": "run-local",
        "history_access": "outside-run files are not added as writable directories",
        "router_home": os.environ.get(CODEX_ROUTER_HOME_ENV) or None,
        "external_sandbox": "docker" if _codex_external_sandbox_enabled() else None,
        "container_image": os.environ.get(CODEX_DOCKER_IMAGE_ENV) or DEFAULT_CODEX_DOCKER_IMAGE,
        "history_mount": False,
        "staged_files": staged_files,
    }
    try:
        version_result = subprocess.run(
            [_codex_cli_bin(), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except Exception as exc:
        isolation_payload["codex_version"] = None
        isolation_payload["codex_version_error"] = str(exc)
    else:
        isolation_payload["codex_version"] = version_result.stdout.strip() or None
        isolation_payload["codex_version_exit_code"] = int(version_result.returncode)
        if version_result.stderr.strip():
            isolation_payload["codex_version_stderr"] = version_result.stderr.strip()[:400]
    _write_json(target_root / CODEX_ISOLATION_FILENAME, isolation_payload)
    return paths.codex_home


def _codex_child_env(run_dir: Path, *, codex_home: Path) -> dict[str, str]:
    """Return a run-local environment without exposing the judge secret."""

    runtime_root = run_dir / "process" / CODEX_RUNTIME_DIRNAME
    home_root = runtime_root / "home"
    xdg_root = runtime_root / "xdg"
    child_env = {str(key): str(value) for key, value in os.environ.items()}
    child_env.pop("TADACAMPAIGN_JUDGE_ADMIN_TOKEN", None)
    child_env["CODEX_HOME"] = str(codex_home)
    router_home = str(os.environ.get(CODEX_ROUTER_HOME_ENV, "")).strip()
    child_env["HOME"] = router_home or str(home_root)
    child_env["XDG_CONFIG_HOME"] = str(xdg_root / "config")
    child_env["XDG_DATA_HOME"] = str(xdg_root / "data")
    child_env["XDG_CACHE_HOME"] = str(xdg_root / "cache")
    child_env["XDG_STATE_HOME"] = str(xdg_root / "state")
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    source_root = str(Path(__file__).resolve().parents[1])
    inherited_pythonpath = str(child_env.get("PYTHONPATH") or "").strip()
    child_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, inherited_pythonpath) if part
    )
    # Materialized tool launchers use this explicit path instead of relying on
    # how the parent installed the package.  The external Codex sandbox mounts
    # the same repository source path read-only.
    child_env["TADACAMPAIGN_SOURCE_ROOT"] = source_root
    return child_env


def _archive_attempt_logs(results_dir: Path, *, backend: str, attempt_index: int) -> Path | None:
    src = results_dir / "agent_stdout.log"
    if not src.is_file():
        return None
    dest = results_dir / f"agent_stdout_attempt{attempt_index}.log"
    if dest.exists():
        dest.unlink()
    src.rename(dest)

    if backend == "codex":
        codex_src = results_dir / "codex_events.jsonl"
        codex_dest = results_dir / f"codex_events_attempt{attempt_index}.jsonl"
        if codex_dest.exists():
            codex_dest.unlink()
        if codex_src.exists():
            codex_src.rename(codex_dest)
    return dest


def _snapshot_attempt_result(results_dir: Path, *, backend: str, attempt_index: int) -> Path | None:
    src = results_dir / "agent_final_result.json"
    if backend == "codex" and not src.is_file():
        fallback_src = results_dir / "codex_final_result.json"
        if fallback_src.is_file():
            dest = results_dir / f"codex_final_result_attempt{attempt_index}.json"
            if dest.exists():
                dest.unlink()
            dest.write_text(fallback_src.read_text(encoding="utf-8"), encoding="utf-8")
            fallback_src.unlink()
            return dest
    if not src.is_file():
        return None
    dest = results_dir / f"agent_final_result_attempt{attempt_index}.json"
    if dest.exists():
        dest.unlink()
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    src.unlink()

    if backend == "codex":
        codex_src = results_dir / "codex_final_result.json"
        codex_dest = results_dir / f"codex_final_result_attempt{attempt_index}.json"
        if codex_dest.exists():
            codex_dest.unlink()
        if codex_src.exists():
            codex_dest.write_text(codex_src.read_text(encoding="utf-8"), encoding="utf-8")
            codex_src.unlink()
    return dest


def _persist_resume_meta(results_dir: Path, *, backend: str, payload: dict[str, Any]) -> Path:
    agent_path = results_dir / "agent_resume_meta.json"
    _write_json(agent_path, payload)
    if backend == "codex":
        _write_json(results_dir / "codex_resume_meta.json", payload)
    return agent_path


def _write_handoff_prompt(results_dir: Path, *, backend: str, attempt_index: int, prompt: str) -> Path:
    path = results_dir / f"agent_handoff_prompt_attempt{attempt_index}.txt"
    _write_text(path, prompt)
    if backend == "codex":
        codex_path = results_dir / f"codex_handoff_prompt_attempt{attempt_index}.txt"
        _write_text(codex_path, prompt)
        return codex_path
    return path


def _write_attempt_prompt(results_dir: Path, *, backend: str, attempt_index: int, prompt: str) -> Path:
    """Persist the exact prompt submitted for every backend attempt."""

    path = results_dir / f"agent_prompt_attempt{attempt_index}.txt"
    _write_text(path, prompt)
    if backend == "codex":
        _write_text(results_dir / f"codex_prompt_attempt{attempt_index}.txt", prompt)
    return path


def _write_handoff_drill_meta(results_dir: Path, payload: dict[str, Any]) -> Path:
    path = results_dir / HANDOFF_DRILL_META_FILENAME
    _write_json(path, payload)
    return path


def _append_backend_event(run_dir: Path, *, backend: str, suffix: str, **payload: Any) -> None:
    append_runtime_event(run_dir, event=f"agent.{suffix}", backend=backend, **payload)
    if backend == "codex":
        append_runtime_event(run_dir, event=f"codex.{suffix}", **payload)


def _load_handoff_drill_config() -> _HandoffDrillConfig | None:
    raw_after_actions = os.environ.get(HANDOFF_DRILL_AFTER_ACCEPTED_ACTIONS_ENV)
    if raw_after_actions is None or not raw_after_actions.strip():
        return None
    try:
        after_accepted_actions = int(raw_after_actions)
    except ValueError as exc:
        raise BackendError(
            f"{HANDOFF_DRILL_AFTER_ACCEPTED_ACTIONS_ENV} must be a positive integer, got {raw_after_actions!r}"
        ) from exc
    if after_accepted_actions <= 0:
        raise BackendError(
            f"{HANDOFF_DRILL_AFTER_ACCEPTED_ACTIONS_ENV} must be a positive integer, got {after_accepted_actions}"
        )

    raw_max_triggers = os.environ.get(HANDOFF_DRILL_MAX_TRIGGERS_ENV, "1")
    try:
        max_triggers = int(raw_max_triggers)
    except ValueError as exc:
        raise BackendError(f"{HANDOFF_DRILL_MAX_TRIGGERS_ENV} must be an integer >= 0, got {raw_max_triggers!r}") from exc
    if max_triggers < 0:
        raise BackendError(f"{HANDOFF_DRILL_MAX_TRIGGERS_ENV} must be >= 0, got {max_triggers}")

    return _HandoffDrillConfig(
        after_accepted_actions=after_accepted_actions,
        max_triggers=max_triggers,
    )


def _accepted_step_snapshot(run_dir: Path) -> tuple[int, int | None]:
    process_dir = run_dir / "process"
    accepted_count = 0
    max_step_index: int | None = None
    for path in sorted(process_dir.glob("step_*/step_meta.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("accepted") is not True:
            continue
        accepted_count += 1
        raw_step_index = payload.get("step_index")
        if isinstance(raw_step_index, int):
            max_step_index = raw_step_index if max_step_index is None else max(max_step_index, raw_step_index)
    return accepted_count, max_step_index


def _request_process_termination(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        return
    process.terminate()


def _run_handoff_drill_watch(
    *,
    run_dir: Path,
    process: subprocess.Popen[str],
    attempt_index: int,
    backend: str,
    config: _HandoffDrillConfig,
    stop_event: threading.Event,
    outcome: dict[str, Any],
) -> None:
    while not stop_event.is_set():
        accepted_action_count, trigger_step_index = _accepted_step_snapshot(run_dir)
        if accepted_action_count >= config.after_accepted_actions:
            triggered_at = utc_now_iso()
            outcome.update(
                {
                    "triggered": True,
                    "triggered_at": triggered_at,
                    "accepted_action_count": accepted_action_count,
                    "trigger_step_index": trigger_step_index,
                }
            )
            _append_backend_event(
                run_dir,
                backend=backend,
                suffix="handoff_drill.triggered",
                attempt=attempt_index,
                trigger_after_accepted_actions=config.after_accepted_actions,
                accepted_action_count=accepted_action_count,
                trigger_step_index=trigger_step_index,
                triggered_at=triggered_at,
            )
            _request_process_termination(process)
            return
        time.sleep(0.2)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            process.communicate()
        return

    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _is_completed_result_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("status") != "completed":
        return False
    return "stop_reason" in payload and "action_count" in payload


def _decode_trailing_json_value(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    for start_index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, end_index = decoder.raw_decode(text[start_index:])
        except json.JSONDecodeError:
            continue
        suffix = text[start_index + end_index :].strip()
        if suffix and suffix != "```":
            continue
        return value
    return None


def _find_completed_result_payload(candidate: Any) -> dict[str, Any] | None:
    if _is_completed_result_payload(candidate):
        return dict(candidate)
    if isinstance(candidate, dict):
        values = list(candidate.values())
        for value in reversed(values):
            result = _find_completed_result_payload(value)
            if result is not None:
                return result
        return None
    if isinstance(candidate, list):
        for value in reversed(candidate):
            result = _find_completed_result_payload(value)
            if result is not None:
                return result
        return None
    if isinstance(candidate, str):
        text = candidate.strip()
        if not text:
            return None
        decoded: Any | None = None
        if text[0] in "[{":
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = None
        if decoded is None:
            decoded = _decode_trailing_json_value(text)
        if decoded is None:
            return None
        return _find_completed_result_payload(decoded)
    return None


def _parse_json_stream(text: str) -> list[Any]:
    payloads: list[Any] = []
    stripped = text.strip()
    if not stripped:
        return payloads
    try:
        payloads.append(json.loads(stripped))
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            payloads.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return payloads


def _collect_error_messages_from_text(text: str) -> tuple[str, ...]:
    messages: list[str] = []

    def visit(candidate: Any) -> None:
        if isinstance(candidate, dict):
            for key, value in candidate.items():
                if key in {"error", "message"}:
                    if isinstance(value, str) and value.strip():
                        messages.append(value.strip())
                    elif isinstance(value, dict):
                        visit(value)
                elif isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(candidate, list):
            for item in candidate:
                visit(item)

    for payload in _parse_json_stream(text):
        visit(payload)
    return tuple(messages)


def _extract_generic_episode_result(*, backend: str, stdout: str, results_dir: Path) -> dict[str, Any]:
    for payload in reversed(_parse_json_stream(stdout)):
        result = _find_completed_result_payload(payload)
        if result is not None:
            _write_final_result(results_dir, backend=backend, payload=result)
            return result
    label = _backend_label(backend)
    raise BackendError(f"{label} output is missing a final JSON result", backend=backend)


def _load_episode_result(result_path: Path, *, backend: str) -> dict[str, Any]:
    label = _backend_label(backend)
    if not result_path.is_file():
        raise BackendError(f"{label} did not write the final result file: {result_path}", backend=backend)
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackendError(f"{label} wrote invalid final JSON: {result_path}", backend=backend) from exc
    if not isinstance(payload, dict):
        raise BackendError(f"{label} final result must be a JSON object: {result_path}", backend=backend)
    if payload.get("status") != "completed":
        raise BackendError(f"{label} final result has unexpected status: {result_path}", backend=backend)
    if "stop_reason" not in payload or "action_count" not in payload:
        raise BackendError(f"{label} final result is missing required fields: {result_path}", backend=backend)
    return payload


def _load_workspace_state_from_results_dir(results_dir: Path) -> dict[str, Any] | None:
    state_path = results_dir.parent / "process" / "agent_workspace" / "state.json"
    if not state_path.is_file():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _workspace_terminal_state_from_results_dir(results_dir: Path) -> bool | None:
    state = _load_workspace_state_from_results_dir(results_dir)
    if state is None:
        return None
    return bool(str(state.get("stop_code") or "").strip() or str(state.get("stop_reason") or "").strip())


def _extract_codex_episode_result(events_path: Path, result_path: Path) -> dict[str, Any]:
    final_text: str | None = None
    results_dir = result_path.parent
    try:
        payloads = inspect_event_stream(events_path, allow_incomplete_tail=False).payloads
    except CodexEventError as exc:
        raise BackendError(str(exc), backend="codex") from exc

    for payload in reversed(payloads):
        if str(payload.get("type") or "").strip() != "item.completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type not in {"agent_message", "assistant_message"}:
            continue
        candidate = item.get("text")
        if isinstance(candidate, str) and candidate.strip():
            final_text = candidate.strip()
            break

    if final_text is None:
        raise BackendError(f"Codex event stream is missing a final text result: {events_path}", backend="codex")

    payload = _find_completed_result_payload(final_text)
    if payload is None:
        if _workspace_terminal_state_from_results_dir(results_dir) is False:
            raise BackendError(
                f"Codex turn completed before benchmark terminal state and did not provide valid final JSON: {events_path}",
                backend="codex",
            )
        raise BackendError(f"Codex final text result is not valid JSON: {events_path}", backend="codex")
    if _workspace_terminal_state_from_results_dir(results_dir) is False:
        raise BackendError(
            f"Codex final JSON result was emitted before benchmark terminal state: {events_path}",
            backend="codex",
        )
    if not isinstance(payload, dict):
        raise BackendError(f"Codex final text result must decode to a JSON object: {events_path}", backend="codex")

    _write_json(result_path, payload)
    return _load_episode_result(result_path, backend="codex")


def _skill_episode_file_lines(config: ExperimentConfig) -> str:
    if not config.skill_surface_enabled:
        return ""
    return "\n        - `skill_market.json`\n        - `owned_skills_index.json`"


def _skill_episode_guidance(config: ExperimentConfig) -> str:
    if not config.skill_surface_enabled:
        return ""
    return dedent("""
        If owned_skills_index.json reports owned_skill_count > 0, tools are already available.
        Their use is optional. Read owned_skills/<skill_id>/source/SKILL.md when its capability
        could help the current decision. Use its instructions, scripts and checks within the
        workspace, inspect the output and decide whether it supports an experimental action.
        An initially owned tool is an experimental condition, not a purchase; no purchase fee
        or acquisition day is charged.

        Tool use rules:
        - Only ./TadACampaign-agent-step may change benchmark state.
        - Install dependencies only under .skill_tools/, .venv/ or the tool directory.
        - Store analysis artifacts under skill_analysis/.
        - If a finding affects an action, cite its path and specific evidence in --reasoning.
        - Skip a tool when it is irrelevant or its setup cost does not justify using it.
    """).rstrip()


def _skill_episode_action_line(config: ExperimentConfig) -> str:
    if not config.skill_surface_enabled:
        return ""
    return '\n        - `./TadACampaign-agent-step --action purchase_skill --skill-id <skill_id> --reasoning "<brief reason>"`'


def _skill_episode_rule_lines(config: ExperimentConfig) -> str:
    if not config.skill_surface_enabled:
        return ""
    return dedent("""
        - skill_market.json contains acquisition cards; acquired files appear under owned_skills/<skill_id>/source/.
        - An empty market with owned tools is an assigned-tool condition; do not attempt purchases.
        - purchase_skill unlocks an available offline tool; acquiring it does not require a call at every step.
    """).rstrip()


def _skill_episode_parameter_line(config: ExperimentConfig) -> str:
    return "\n        - purchase_skill must use a skill_id in the current payload." if config.skill_surface_enabled else ""


def _handoff_read_file_lines(config: ExperimentConfig) -> str:
    files = ["agent_playbook.md", "available_actions.json"]
    if config.skill_surface_enabled:
        files += ["skill_market.json", "owned_skills_index.json"]
    files += ["state.json", "history.json"]
    return "\n".join(f"{i}. `{name}`" for i, name in enumerate(files, 1)) + "\nRead biology_context.md when biological or cost context is needed."


def _handoff_skill_guidance(config: ExperimentConfig) -> str:
    if not config.skill_surface_enabled:
        return ""
    return "Revisit the relevant SKILL.md when needed; save analysis under skill_analysis/ and cite actual findings in action reasoning."


def _episode_prompt(config: ExperimentConfig) -> str:
    prompt = dedent(f"""
        You are an AI protein-engineering agent conducting a complete TadACampaign historical replay.
        Work within the campaign's budget, time and legal experimental actions. Maximize terminal cash_final.

        Before acting read, in order:
        - agent_playbook.md
        - available_actions.json
        {_skill_episode_file_lines(config)}
        - state.json
        - history.json
        Read biology_context.md when the biological meaning, action costs or library access need clarification.

        Inspect state.economic_summary before spending: confirmation cost, configured and available
        mine-then-confirm reserve, legal batch options and budget_regime. A screening hit produces no
        settled value until confirm_hit. Prefer leaves_confirm_capacity=true; explain any intentional
        risk of screening a hit that cannot be confirmed. Evaluate promising pending candidates before
        losing confirmation capacity.
        {_skill_episode_guidance(config)}

        Use only visible seeds, prepared seeds and libraries, actual exploration history, visible
        start_sequence and mutation windows, remaining screenable counts, legal batch_options,
        current cash and days, noisy screening observations, pending confirmations and confirmed best.
        Sequence features, alignments and other analyses can support decisions but are not measured
        activity. Access to a new branch means access to newly legal actions, not an activity guarantee.
        Available tools form an optional capability pool; no fixed tool count, order or composition
        is prescribed. Only actual reading, consumption or subsequent actions count as use evidence.

        Only these helper calls may change experimental state:
        - ./TadACampaign-agent-step --action prepare_seed --seed-id <seed_id> --reasoning "<brief reason>"
        - ./TadACampaign-agent-step --action prepare_library --seed-id <seed_id> --library-id <library_id> --reasoning "<brief reason>"
        - ./TadACampaign-agent-step --action mine_library --seed-id <seed_id> --library-id <library_id> --batch-size <int> --reasoning "<brief reason>"
        - ./TadACampaign-agent-step --action confirm_hit --screen-record-id <int> --reasoning "<brief reason>"
        {_skill_episode_action_line(config)}
        - ./TadACampaign-agent-step --action stop --reasoning "<brief reason>"

        Do not edit benchmark source or authoritative state files. Check every helper response and
        refreshed workspace state. Inspect needed JSON fields rather than repeatedly printing large
        files. On accepted=false read rejection_reason and choose a currently legal action.
        prepare_seed prepares a visible seed; it does not reveal descendants. prepare_library may
        reveal newly_visible_seed_ids. mine_library screens only: it reveals noisy observed_activity,
        pays no reward and reveals no new seed. Only confirm_hit reveals confirmed_activity and may
        reward an improvement in confirmed_best_activity.
        {_skill_episode_rule_lines(config)}
        Use only listed discrete batch_options and pending screen_record_id values.
        {_skill_episode_parameter_line(config)}
        A promising provisional hit does not improve cash before confirmation. Further exploration
        can discover stronger proteins, but each action must compare exploration, confirmation and
        stopping under current costs. Actively stop when remaining actions lack a credible
        risk-adjusted path to higher cash. The simulated horizon is {config.max_experiment_days:g}
        elapsed_day units; step_index and request_count are audit counters, not days.

        At a terminal state output exactly one JSON line with status="completed", authoritative
        stop_reason, final action_count and a brief summary. Add no surrounding commentary.
    """).strip()
    extra_instructions = str(os.environ.get(AGENT_PROMPT_APPEND_ENV, "")).strip()
    if extra_instructions:
        prompt += "\n\nAdditional operating instructions:\n" + extra_instructions
    return prompt + "\n"


def _load_workspace_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _history_lines(items: list[dict[str, Any]], *, fields: tuple[str, ...]) -> list[str]:
    lines: list[str] = []
    for item in items[-3:]:
        if not isinstance(item, dict):
            continue
        parts = []
        for field_name in fields:
            value = item.get(field_name)
            if value in {None, ""}:
                continue
            parts.append(f"{field_name}={value}")
        if parts:
            lines.append("- " + ", ".join(parts))
    return lines


def _build_handoff_prompt(config: ExperimentConfig, *, backend: str, agent_workspace: Path,
                          source_session_id: str | None, trigger: str, stop_reason: str) -> str:
    state = _load_workspace_json(agent_workspace / "state.json")
    history = _load_workspace_json(agent_workspace / "history.json")
    actions = _history_lines(history.get("recent_actions", []), fields=("action", "seed_id", "library_id", "batch_size", "elapsed_day_after", "cash_after"))
    rejections = _history_lines(history.get("recent_rejections", []), fields=("action", "seed_id", "library_id", "batch_size", "rejection_reason"))
    brief = (f"Continue the same campaign after context-window exhaustion. Previous backend: {_backend_label(backend)}; "
             f"session: {source_session_id or 'unknown'}; trigger: {trigger}; excerpt: {stop_reason[:400]}. "
             "The Judge and its authoritative state remain alive; do not restart or reset the experiment.\n"
             + _handoff_read_file_lines(config) + "\n" + _handoff_skill_guidance(config)
             + "\nCurrent state: " + json.dumps({k:state.get(k) for k in ("elapsed_day", "remaining_days", "current_cash", "confirmed_best_activity", "action_count", "request_count")})
             + "\nRecent accepted actions:\n" + ("\n".join(actions) or "- none")
             + "\nRecent rejected actions:\n" + ("\n".join(rejections) or "- none"))
    return _episode_prompt(config) + "\n" + brief + "\n"


def _build_resume_prompt(config: ExperimentConfig, *, agent_workspace: Path, trigger: str, stop_reason: str) -> str:
    state = _load_workspace_json(agent_workspace / "state.json")
    history = _load_workspace_json(agent_workspace / "history.json")
    return (f"Continue this same TadACampaign episode; do not restart it. Previous OpenCode attempt: {trigger}; {stop_reason[:300]}. "
            "Read available_actions.json, state.json and history.json. Continue legal helper actions if nonterminal. "
            "Check each response and refreshed state. On accepted stop or a terminal state, emit only one final JSON line "
            'with status="completed", stop_reason, action_count and summary.\n'
            + "Current state: " + json.dumps(state, ensure_ascii=True)
            + "\nRecent history: " + json.dumps(history, ensure_ascii=True) + "\n")


def _build_codex_command(
    config: ExperimentConfig,
    *,
    run_dir: Path,
    agent_workspace: Path,
    invocation: _AttemptInvocation,
) -> list[str]:
    command = [_codex_cli_bin(), "exec", "--skip-git-repo-check", "--color", "never"]
    if _codex_external_sandbox_enabled():
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        command.extend(["--sandbox", "workspace-write"])
    command.extend(["--ignore-rules", "-C", str(agent_workspace)])
    if config.agent_model:
        command.extend(["-m", config.agent_model])
    if config.agent_profile:
        command.extend(["-p", config.agent_profile])
    if config.agent_reasoning_effort:
        command.extend(["-c", f"model_reasoning_effort={json.dumps(config.agent_reasoning_effort)}"])
    if config.agent_verbosity:
        command.extend(["-c", f"model_verbosity={json.dumps(config.agent_verbosity)}"])
    # Keep the schema in the primary workspace. Granting the whole run
    # directory would expose the staged auth file and parent-side artifacts.
    schema_path = _write_codex_final_result_schema(agent_workspace)
    command.extend(["--output-schema", str(schema_path)])
    command.append("--json")
    if invocation.resume_session_id:
        command.extend(["resume", invocation.resume_session_id, "-"])
    else:
        command.append("-")
    return command


def _codex_cli_bin() -> str:
    """Return the Codex executable, allowing isolated hosts to bypass wrappers."""
    override = str(os.environ.get(CODEX_CLI_BIN_ENV, "")).strip()
    return override or DEFAULT_CODEX_CLI_BIN


def _codex_external_sandbox_enabled() -> bool:
    return os.environ.get(CODEX_EXTERNAL_SANDBOX_ENV, "").strip().lower() in {"1", "true", "yes"}


def _dockerized_codex_command(
    command: list[str],
    *,
    run_dir: Path,
    agent_workspace: Path,
    child_env: dict[str, str],
) -> list[str]:
    """Run Codex in a container with only the current run mounted."""
    repo_root = Path(__file__).resolve().parents[2]
    docker_bin = str(os.environ.get(CODEX_DOCKER_BIN_ENV, "/usr/bin/docker")).strip() or "/usr/bin/docker"
    image = str(os.environ.get(CODEX_DOCKER_IMAGE_ENV, DEFAULT_CODEX_DOCKER_IMAGE)).strip() or DEFAULT_CODEX_DOCKER_IMAGE
    process_dir = run_dir / "process"
    results_dir = run_dir / "results"
    bindings = [
        (agent_workspace, agent_workspace, "rw"),
        (process_dir, process_dir, "rw"),
        (results_dir, results_dir, "rw"),
        (run_dir / "config.toml", run_dir / "config.toml", "ro"),
        (repo_root / "src", repo_root / "src", "ro"),
        (repo_root / "docs", repo_root / "docs", "ro"),
        ((Path.home() / ".nvm"), (Path.home() / ".nvm"), "ro"),
        ((Path.home() / ".local/bin"), (Path.home() / ".local/bin"), "ro"),
        ((Path.home() / ".nu"), (Path.home() / ".nu"), "ro"),
    ]
    venv_root = repo_root / ".venv"
    if venv_root.is_dir():
        bindings.append((venv_root, venv_root, "ro"))
    docker_command = [
        docker_bin,
        "run",
        "--rm",
        "--interactive",
        "--network",
        "host",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--entrypoint",
        command[0],
    ]
    for source, target, mode in bindings:
        docker_command.extend(["-v", f"{source}:{target}:{mode}"])
    safe_env_keys = {
        "HOME",
        "PATH",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        CODEX_CLI_BIN_ENV,
        CODEX_ROUTER_HOME_ENV,
        CODEX_EXTERNAL_SANDBOX_ENV,
        "TADACAMPAIGN_AGENT_BACKEND",
        "TADACAMPAIGN_AGENT_SESSION_ID",
        "TADACAMPAIGN_SOURCE_ROOT",
    }
    for key in sorted(safe_env_keys):
        value = child_env.get(key)
        if value is not None:
            docker_command.extend(["-e", f"{key}={value}"])
    docker_command.extend([image, *command[1:]])
    return docker_command


def _claude_cli_bin() -> str:
    """Return the configured Claude CLI executable."""
    override = str(os.environ.get(CLAUDE_CLI_BIN_ENV, "")).strip()
    return override or DEFAULT_CLAUDE_CLI_BIN


def _build_claude_command(
    config: ExperimentConfig,
    *,
    run_dir: Path,
    invocation: _AttemptInvocation,
) -> list[str]:
    command = [
        _claude_cli_bin(),
        "-p",
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
        "--add-dir",
        str(run_dir),
        "--json-schema",
        json.dumps(_FINAL_RESULT_SCHEMA, ensure_ascii=False),
    ]
    if config.agent_model:
        command.extend(["--model", config.agent_model])
    if config.agent_reasoning_effort:
        command.extend(["--effort", config.agent_reasoning_effort])
    if invocation.resume_session_id:
        command.extend(["--resume", invocation.resume_session_id])
    if invocation.mode == "handoff":
        command.append("--fork-session")
    if invocation.target_session_id and (invocation.mode != "resume" or invocation.target_session_id != invocation.resume_session_id):
        command.extend(["--session-id", invocation.target_session_id])
    command.append(invocation.prompt)
    return command


def _build_opencode_command(
    config: ExperimentConfig,
    *,
    agent_workspace: Path,
    invocation: _AttemptInvocation,
) -> list[str]:
    command = [
        "opencode",
        "run",
        "--pure",
        "--format",
        "json",
        "--dir",
        str(agent_workspace),
    ]
    if config.agent_model:
        command.extend(["--model", config.agent_model])
    if config.agent_reasoning_effort:
        command.append("--thinking")
        command.extend(["--variant", config.agent_reasoning_effort])
    if invocation.resume_session_id:
        command.extend(["--session", invocation.resume_session_id])
    if invocation.mode == "handoff":
        command.append("--fork")
    session_title = str(invocation.metadata.get("session_title") or "").strip()
    if session_title:
        command.extend(["--title", session_title])
    command.append(invocation.prompt)
    return command


def _build_backend_command(
    config: ExperimentConfig,
    *,
    run_dir: Path,
    agent_workspace: Path,
    invocation: _AttemptInvocation,
) -> list[str]:
    if invocation.backend == "codex":
        return _build_codex_command(config, run_dir=run_dir, agent_workspace=agent_workspace, invocation=invocation)
    if invocation.backend == "claude":
        return _build_claude_command(config, run_dir=run_dir, invocation=invocation)
    if invocation.backend == "opencode":
        return _build_opencode_command(config, agent_workspace=agent_workspace, invocation=invocation)
    raise BackendError(f"Unsupported agent backend {invocation.backend!r}")


def _attempt_stdin(invocation: _AttemptInvocation) -> str | None:
    if invocation.backend == "codex":
        return invocation.prompt
    return None


def _should_arm_handoff_drill(backend: str, handoff_drill_state: _HandoffDrillState | None) -> bool:
    return (
        handoff_drill_state is not None
        and handoff_drill_state.config is not None
        and handoff_drill_state.triggered_count < handoff_drill_state.config.max_triggers
    )


def _run_attempt_process(
    config: ExperimentConfig,
    *,
    run_dir: Path,
    agent_workspace: Path,
    invocation: _AttemptInvocation,
    attempt_index: int,
    handoff_drill_state: _HandoffDrillState | None = None,
    timeout_seconds: float | None = None,
    cumulative_budget_seconds: float | None = None,
    cumulative_budget_started_monotonic: float | None = None,
) -> _AttemptProcessResult:
    results_dir = run_dir / "results"
    command = _build_backend_command(
        config,
        run_dir=run_dir,
        agent_workspace=agent_workspace,
        invocation=invocation,
    )

    popen_kwargs: dict[str, object] = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    codex_home = run_dir / "process" / CODEX_RUNTIME_DIRNAME / ".codex"
    child_env = _codex_child_env(run_dir, codex_home=codex_home) if invocation.backend == "codex" else dict(os.environ)
    child_env.pop("TADACAMPAIGN_JUDGE_ADMIN_TOKEN", None)
    child_env["TADACAMPAIGN_AGENT_BACKEND"] = invocation.backend
    if invocation.target_session_id:
        child_env["TADACAMPAIGN_AGENT_SESSION_ID"] = invocation.target_session_id
    if invocation.backend == "codex" and _codex_external_sandbox_enabled():
        command = _dockerized_codex_command(
            command,
            run_dir=run_dir,
            agent_workspace=agent_workspace,
            child_env=child_env,
        )

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(agent_workspace),
        text=True,
        env=child_env,
        **popen_kwargs,
    )
    started_at = utc_now_iso()
    started_monotonic = monotonic_now()
    attempt_timeout_seconds = max(
        float(timeout_seconds if timeout_seconds is not None else config.agent_timeout_seconds),
        0.001,
    )
    cumulative_elapsed_seconds = None
    if cumulative_budget_seconds is not None and cumulative_budget_started_monotonic is not None:
        cumulative_elapsed_seconds = _agent_budget_elapsed_seconds(
            started_monotonic=cumulative_budget_started_monotonic,
            budget_seconds=cumulative_budget_seconds,
        )
    _append_backend_event(
        run_dir,
        backend=invocation.backend,
        suffix="attempt.started",
        attempt=attempt_index,
        mode=invocation.mode,
        started_at=started_at,
        resume_session_id=invocation.resume_session_id,
        target_session_id=invocation.target_session_id,
        source_session_id=invocation.source_session_id,
        trigger=invocation.trigger,
        prompt_path=invocation.prompt_path,
        command=command,
        attempt_timeout_seconds=attempt_timeout_seconds,
        cumulative_budget_seconds=cumulative_budget_seconds,
        cumulative_elapsed_seconds=cumulative_elapsed_seconds,
    )

    handoff_drill_watch_thread: threading.Thread | None = None
    handoff_drill_stop_event: threading.Event | None = None
    handoff_drill_outcome = {
        "triggered": False,
        "triggered_at": None,
        "accepted_action_count": None,
        "trigger_step_index": None,
    }
    if _should_arm_handoff_drill(invocation.backend, handoff_drill_state):
        assert handoff_drill_state is not None and handoff_drill_state.config is not None
        _append_backend_event(
            run_dir,
            backend=invocation.backend,
            suffix="handoff_drill.armed",
            attempt=attempt_index,
            trigger_after_accepted_actions=handoff_drill_state.config.after_accepted_actions,
            max_triggers=handoff_drill_state.config.max_triggers,
            triggered_count=handoff_drill_state.triggered_count,
        )
        handoff_drill_stop_event = threading.Event()
        handoff_drill_watch_thread = threading.Thread(
            target=_run_handoff_drill_watch,
            kwargs={
                "run_dir": run_dir,
                "process": process,
                "attempt_index": attempt_index,
                "backend": invocation.backend,
                "config": handoff_drill_state.config,
                "stop_event": handoff_drill_stop_event,
                "outcome": handoff_drill_outcome,
            },
            daemon=True,
        )
        handoff_drill_watch_thread.start()

    try:
        stdout, stderr = process.communicate(_attempt_stdin(invocation), timeout=attempt_timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if handoff_drill_stop_event is not None:
            handoff_drill_stop_event.set()
        if handoff_drill_watch_thread is not None:
            handoff_drill_watch_thread.join(timeout=1)
        _terminate_process_tree(process)
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        _write_agent_logs(run_dir, backend=invocation.backend, stdout=stdout, stderr=stderr, command=command)
        finished_at = utc_now_iso()
        duration_seconds = elapsed_seconds(started_monotonic)
        cumulative_elapsed_seconds = None
        if cumulative_budget_seconds is not None and cumulative_budget_started_monotonic is not None:
            cumulative_elapsed_seconds = _agent_budget_elapsed_seconds(
                started_monotonic=cumulative_budget_started_monotonic,
                budget_seconds=cumulative_budget_seconds,
            )
        timeout_message = (
            f"{_backend_label(invocation.backend)} attempt timed out after {attempt_timeout_seconds:.3f}s; "
            f"cumulative agent timeout budget is {cumulative_budget_seconds or config.agent_timeout_seconds:.0f}s"
        )
        _append_backend_event(
            run_dir,
            backend=invocation.backend,
            suffix="attempt.finished",
            attempt=attempt_index,
            mode=invocation.mode,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            ok=False,
            returncode=-1,
            error=timeout_message,
            resume_session_id=invocation.resume_session_id,
            target_session_id=invocation.target_session_id,
            source_session_id=invocation.source_session_id,
            trigger=invocation.trigger,
            prompt_path=invocation.prompt_path,
            attempt_timeout_seconds=attempt_timeout_seconds,
            cumulative_budget_seconds=cumulative_budget_seconds,
            cumulative_elapsed_seconds=cumulative_elapsed_seconds,
            timeout_scope="cumulative_agent_budget",
        )
        return _AttemptProcessResult(
            command=command,
            returncode=-1,
            stdout=stdout,
            stderr=stderr,
            error=f"{timeout_message}; see {results_dir / 'agent_stderr.log'}",
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
        )
    finally:
        if handoff_drill_stop_event is not None:
            handoff_drill_stop_event.set()
        if handoff_drill_watch_thread is not None:
            handoff_drill_watch_thread.join(timeout=1)

    if handoff_drill_outcome["triggered"]:
        synthetic_error = (
            "context_window_exceeded (handoff drill after "
            f"{handoff_drill_outcome['accepted_action_count']} accepted actions)"
        )
        stderr = f"{stderr.rstrip()}\n{synthetic_error}\n" if stderr.strip() else synthetic_error
        if handoff_drill_state is not None:
            handoff_drill_state.triggered_count += 1
            assert handoff_drill_state.trigger_records is not None
            handoff_drill_state.trigger_records.append(
                {
                    "attempt": attempt_index,
                    "triggered_at": handoff_drill_outcome["triggered_at"],
                    "accepted_action_count": handoff_drill_outcome["accepted_action_count"],
                    "trigger_step_index": handoff_drill_outcome["trigger_step_index"],
                }
            )

    _write_agent_logs(run_dir, backend=invocation.backend, stdout=stdout, stderr=stderr, command=command)
    error: str | None = None
    if handoff_drill_outcome["triggered"]:
        error = stderr.strip()
    elif process.returncode != 0:
        error = (
            f"{_backend_label(invocation.backend)} returned non-zero exit code {process.returncode}; "
            f"see {results_dir / 'agent_stderr.log'}"
        )
    finished_at = utc_now_iso()
    duration_seconds = elapsed_seconds(started_monotonic)
    _append_backend_event(
        run_dir,
        backend=invocation.backend,
        suffix="attempt.finished",
        attempt=attempt_index,
        mode=invocation.mode,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        ok=process.returncode == 0 and not handoff_drill_outcome["triggered"],
        returncode=int(process.returncode),
        error=error,
        resume_session_id=invocation.resume_session_id,
        target_session_id=invocation.target_session_id,
        source_session_id=invocation.source_session_id,
        trigger=invocation.trigger,
        prompt_path=invocation.prompt_path,
        attempt_timeout_seconds=attempt_timeout_seconds,
        cumulative_budget_seconds=cumulative_budget_seconds,
        cumulative_elapsed_seconds=(
            _agent_budget_elapsed_seconds(
                started_monotonic=cumulative_budget_started_monotonic,
                budget_seconds=cumulative_budget_seconds,
            )
            if cumulative_budget_seconds is not None and cumulative_budget_started_monotonic is not None
            else None
        ),
    )
    return _AttemptProcessResult(
        command=command,
        returncode=int(process.returncode),
        stdout=stdout,
        stderr=stderr,
        error=error,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
    )


def _lookup_opencode_session_id(agent_workspace: Path, session_title: str) -> str | None:
    if not session_title.strip():
        return None
    child_env = dict(os.environ)
    child_env.pop("TADACAMPAIGN_JUDGE_ADMIN_TOKEN", None)
    result = subprocess.run(
        ["opencode", "session", "list", "--format", "json", "-n", "200"],
        cwd=str(agent_workspace),
        capture_output=True,
        text=True,
        env=child_env,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    directory = str(agent_workspace.resolve())
    candidates: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("title") or "") != session_title:
            continue
        if str(item.get("directory") or "") != directory:
            continue
        candidates.append(item)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (int(item.get("updated", 0)), int(item.get("created", 0))), reverse=True)
    session_id = candidates[0].get("id")
    return str(session_id) if isinstance(session_id, str) and session_id.strip() else None


def _inspect_codex_attempt(invocation: _AttemptInvocation, stdout_path: Path) -> tuple[_AttemptSnapshot, Exception | None]:
    try:
        provider_snapshot = inspect_event_stream(
            stdout_path,
            expected_thread_id=invocation.resume_session_id,
            allow_incomplete_tail=True,
        )
    except CodexEventError as exc:
        snapshot = _AttemptSnapshot(
            observed_session_id=None,
            effective_session_id=invocation.resume_session_id,
            saw_completion=False,
            error_messages=(),
            truncated_tail=False,
        )
        return snapshot, exc

    snapshot = _AttemptSnapshot(
        observed_session_id=provider_snapshot.observed_thread_id,
        effective_session_id=provider_snapshot.effective_thread_id,
        saw_completion=provider_snapshot.saw_turn_completed,
        error_messages=provider_snapshot.error_messages,
        truncated_tail=provider_snapshot.truncated_tail,
        provider_snapshot=provider_snapshot,
    )
    if (
        invocation.resume_session_id is not None
        and provider_snapshot.observed_thread_id is not None
        and provider_snapshot.observed_thread_id != invocation.resume_session_id
    ):
        return snapshot, CodexEventError(
            f"Codex resumed with unexpected thread id: expected {invocation.resume_session_id}, got {provider_snapshot.observed_thread_id}"
        )
    return snapshot, None


def _inspect_generic_attempt(
    invocation: _AttemptInvocation,
    *,
    process_result: _AttemptProcessResult,
    agent_workspace: Path,
) -> _AttemptSnapshot:
    observed_session_id = invocation.target_session_id or invocation.resume_session_id
    if invocation.backend == "opencode":
        looked_up_session_id = _lookup_opencode_session_id(
            agent_workspace,
            str(invocation.metadata.get("session_title") or ""),
        )
        if looked_up_session_id:
            observed_session_id = looked_up_session_id
    saw_completion = _find_completed_result_payload(process_result.stdout) is not None
    error_messages = _collect_error_messages_from_text(process_result.stdout)
    return _AttemptSnapshot(
        observed_session_id=observed_session_id,
        effective_session_id=observed_session_id or invocation.resume_session_id,
        saw_completion=saw_completion,
        error_messages=error_messages,
        truncated_tail=False,
    )


def _inspect_attempt(
    invocation: _AttemptInvocation,
    *,
    process_result: _AttemptProcessResult,
    agent_workspace: Path,
    results_dir: Path,
) -> tuple[_AttemptSnapshot, Exception | None]:
    stdout_path = results_dir / "agent_stdout.log"
    if invocation.backend == "codex":
        return _inspect_codex_attempt(invocation, stdout_path)
    return _inspect_generic_attempt(invocation, process_result=process_result, agent_workspace=agent_workspace), None


def _validate_success(invocation: _AttemptInvocation, snapshot: _AttemptSnapshot, *, results_dir: Path) -> str | None:
    if invocation.backend == "codex":
        provider_snapshot = snapshot.provider_snapshot
        if not isinstance(provider_snapshot, CodexEventSnapshot):
            raise BackendError("Codex attempt snapshot is missing provider state", backend="codex")
        return validate_event_stream(results_dir / "codex_events.jsonl", expected_thread_id=invocation.resume_session_id)

    effective_session_id = snapshot.effective_session_id
    if effective_session_id is None:
        raise BackendError(
            f"{_backend_label(invocation.backend)} did not expose a recoverable session id",
            backend=invocation.backend,
        )
    return effective_session_id


def _extract_episode_result(
    invocation: _AttemptInvocation,
    *,
    process_result: _AttemptProcessResult,
    results_dir: Path,
) -> dict[str, Any]:
    if invocation.backend == "codex":
        payload = _extract_codex_episode_result(results_dir / "codex_events.jsonl", results_dir / "codex_final_result.json")
        _write_final_result(results_dir, backend="codex", payload=payload)
        return payload
    payload = _extract_generic_episode_result(
        backend=invocation.backend,
        stdout=process_result.stdout,
        results_dir=results_dir,
    )
    return _load_episode_result(results_dir / "agent_final_result.json", backend=invocation.backend)


def _classify_generic_failure(
    *,
    snapshot: _AttemptSnapshot,
    process_error: str | None,
    stdout: str,
    stderr: str,
    validation_error: Exception | None,
) -> _FailureClassification:
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
        "unknown backend failure",
    )
    reason_excerpt = stop_reason[:400]

    if snapshot.saw_completion:
        return _FailureClassification(
            classification="completed_without_valid_result",
            retryable=False,
            continuation_mode="terminal",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if any(marker in normalized for marker in CONTEXT_WINDOW_ERROR_MARKERS):
        return _FailureClassification(
            classification="fresh_handoff_required",
            retryable=True,
            continuation_mode="handoff",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if any(marker in normalized for marker in NON_RETRYABLE_ERROR_MARKERS):
        return _FailureClassification(
            classification="terminal_non_retryable_error",
            retryable=False,
            continuation_mode="terminal",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if snapshot.effective_session_id is not None and any(marker in normalized for marker in RETRYABLE_ERROR_MARKERS):
        return _FailureClassification(
            classification="retryable_interruption",
            retryable=True,
            continuation_mode="resume",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    if snapshot.effective_session_id is not None and validation_error is not None:
        return _FailureClassification(
            classification="retryable_missing_final_result",
            retryable=True,
            continuation_mode="handoff",
            stop_reason=stop_reason,
            reason_excerpt=reason_excerpt,
        )
    return _FailureClassification(
        classification="terminal_backend_error",
        retryable=False,
        continuation_mode="terminal",
        stop_reason=stop_reason,
        reason_excerpt=reason_excerpt,
    )


def _classify_failure(
    invocation: _AttemptInvocation,
    *,
    snapshot: _AttemptSnapshot,
    process_error: str | None,
    stdout: str,
    stderr: str,
    validation_error: Exception | None,
) -> _FailureClassification:
    if invocation.backend == "codex":
        provider_snapshot = snapshot.provider_snapshot
        if not isinstance(provider_snapshot, CodexEventSnapshot):
            provider_snapshot = CodexEventSnapshot(
                payloads=(),
                observed_thread_id=None,
                effective_thread_id=invocation.resume_session_id,
                saw_turn_completed=False,
                error_messages=(),
                truncated_tail=False,
            )
        failure = classify_codex_failure(
            snapshot=provider_snapshot,
            process_error=process_error,
            stdout=stdout,
            stderr=stderr,
            validation_error=validation_error,
        )
        return _FailureClassification(
            classification=failure.classification,
            retryable=failure.retryable,
            continuation_mode=failure.continuation_mode,
            stop_reason=failure.stop_reason,
            reason_excerpt=failure.reason_excerpt,
        )

    return _classify_generic_failure(
        snapshot=snapshot,
        process_error=process_error,
        stdout=stdout,
        stderr=stderr,
        validation_error=validation_error,
    )


def _new_claude_session_id() -> str:
    return str(uuid.uuid4())


def _new_opencode_session_title(run_dir: Path, *, mode: str) -> str:
    return f"TadACampaign-{run_dir.name}-{mode}-{uuid.uuid4().hex[:12]}"


def _initial_invocation(config: ExperimentConfig, *, run_dir: Path) -> _AttemptInvocation:
    backend = config.agent_backend
    prompt = _episode_prompt(config)
    if backend == "codex":
        return _AttemptInvocation(
            backend=backend,
            mode="initial",
            prompt=prompt,
            prompt_path=None,
            resume_session_id=None,
            target_session_id=None,
            source_session_id=None,
            trigger="run_start",
        )
    if backend == "claude":
        return _AttemptInvocation(
            backend=backend,
            mode="initial",
            prompt=prompt,
            prompt_path=None,
            resume_session_id=None,
            target_session_id=_new_claude_session_id(),
            source_session_id=None,
            trigger="run_start",
        )
    if backend == "opencode":
        return _AttemptInvocation(
            backend=backend,
            mode="initial",
            prompt=prompt,
            prompt_path=None,
            resume_session_id=None,
            target_session_id=None,
            source_session_id=None,
            trigger="run_start",
            metadata={"session_title": _new_opencode_session_title(run_dir, mode="initial")},
        )
    raise BackendError(f"Unsupported agent backend {backend!r}")


def _resume_invocation(
    current: _AttemptInvocation,
    *,
    config: ExperimentConfig,
    run_dir: Path,
    session_id: str,
    trigger: str,
) -> _AttemptInvocation:
    if current.backend == "codex":
        return _AttemptInvocation(
            backend=current.backend,
            mode="resume",
            prompt=_episode_prompt(config),
            prompt_path=None,
            resume_session_id=session_id,
            target_session_id=session_id,
            source_session_id=session_id,
            trigger=trigger,
        )
    if current.backend == "claude":
        return _AttemptInvocation(
            backend=current.backend,
            mode="resume",
            prompt=_episode_prompt(config),
            prompt_path=None,
            resume_session_id=session_id,
            target_session_id=session_id,
            source_session_id=session_id,
            trigger=trigger,
        )
    if current.backend == "opencode":
        return _AttemptInvocation(
            backend=current.backend,
            mode="resume",
            prompt=_build_resume_prompt(
                config,
                agent_workspace=run_dir / "process" / "agent_workspace",
                trigger=trigger,
                stop_reason="OpenCode did not emit the required final JSON in the previous attempt.",
            ),
            prompt_path=None,
            resume_session_id=session_id,
            target_session_id=session_id,
            source_session_id=session_id,
            trigger=trigger,
        )
    raise BackendError(f"Unsupported agent backend {current.backend!r}")


def _handoff_invocation(
    current: _AttemptInvocation,
    *,
    config: ExperimentConfig,
    run_dir: Path,
    agent_workspace: Path,
    session_id: str,
    trigger: str,
    stop_reason: str,
    prompt_path: str,
) -> _AttemptInvocation:
    prompt = _build_handoff_prompt(
        config,
        backend=current.backend,
        agent_workspace=agent_workspace,
        source_session_id=session_id,
        trigger=trigger,
        stop_reason=stop_reason,
    )
    if current.backend == "codex":
        return _AttemptInvocation(
            backend=current.backend,
            mode="handoff",
            prompt=prompt,
            prompt_path=prompt_path,
            resume_session_id=None,
            target_session_id=None,
            source_session_id=session_id,
            trigger=trigger,
        )
    if current.backend == "claude":
        return _AttemptInvocation(
            backend=current.backend,
            mode="handoff",
            prompt=prompt,
            prompt_path=prompt_path,
            resume_session_id=session_id,
            target_session_id=_new_claude_session_id(),
            source_session_id=session_id,
            trigger=trigger,
        )
    if current.backend == "opencode":
        return _AttemptInvocation(
            backend=current.backend,
            mode="handoff",
            prompt=prompt,
            prompt_path=prompt_path,
            resume_session_id=session_id,
            target_session_id=None,
            source_session_id=session_id,
            trigger=trigger,
            metadata={"session_title": _new_opencode_session_title(run_dir, mode="handoff")},
        )
    raise BackendError(f"Unsupported agent backend {current.backend!r}")


def _attempt_record(
    invocation: _AttemptInvocation,
    *,
    snapshot: _AttemptSnapshot,
    process_result: _AttemptProcessResult,
    attempt_index: int,
    classification: str,
    retryable: bool,
    continuation_mode: str,
    stop_reason: str,
    reason_excerpt: str,
    events_path: str,
    result_snapshot_path: str,
) -> dict[str, Any]:
    prompt_sha256: str | None = None
    prompt_size_bytes: int | None = None
    if invocation.prompt_path:
        prompt_file = Path(invocation.prompt_path)
        if prompt_file.is_file():
            prompt_sha256 = _sha256_file(prompt_file)
            prompt_size_bytes = prompt_file.stat().st_size
    record = {
        "attempt": int(attempt_index),
        "backend": invocation.backend,
        "mode": invocation.mode,
        "trigger": invocation.trigger,
        "prompt_path": invocation.prompt_path,
        "prompt_sha256": prompt_sha256,
        "prompt_size_bytes": prompt_size_bytes,
        "resume_session_id": invocation.resume_session_id,
        "target_session_id": invocation.target_session_id,
        "source_session_id": invocation.source_session_id,
        "observed_session_id": snapshot.observed_session_id,
        "effective_session_id": snapshot.effective_session_id,
        "result_session_id": snapshot.observed_session_id or snapshot.effective_session_id,
        "started_at": process_result.started_at,
        "ended_at": process_result.finished_at,
        "duration_seconds": process_result.duration_seconds,
        "returncode": int(process_result.returncode),
        "classification": classification,
        "retryable": bool(retryable),
        "continuation_mode": continuation_mode,
        "stop_reason": stop_reason,
        "reason_excerpt": reason_excerpt,
        "events_path": events_path,
        "result_snapshot_path": result_snapshot_path,
        "command": list(process_result.command),
    }
    if invocation.backend == "codex":
        record.update(
            {
                "resume_thread_id": invocation.resume_session_id,
                "source_thread_id": invocation.source_session_id,
                "observed_thread_id": snapshot.observed_session_id,
                "effective_thread_id": snapshot.effective_session_id,
                "result_thread_id": snapshot.observed_session_id or snapshot.effective_session_id,
            }
        )
    return record


def _run_policy_episode(
    config: ExperimentConfig,
    *,
    run_dir: Path,
    agent_workspace: Path,
) -> dict[str, Any]:
    if not config.agent_policy:
        raise BackendError("Policy backend requires [agent].policy", backend="policy")

    from .runner import run_agent_step

    agent_workspace.mkdir(parents=True, exist_ok=True)
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(policy_rng_seed(config, config.agent_policy))
    memory = make_policy_memory()
    stdout_lines: list[str] = []
    final_result: dict[str, Any] | None = None
    session_id = f"policy:{config.agent_policy}:{run_dir.name}"
    max_steps = int(config.agent_policy_params.get("max_steps", 512))

    for loop_index in range(1, max_steps + 1):
        observation = {
            "state": _load_workspace_json(agent_workspace / "state.json"),
            "history": _load_workspace_json(agent_workspace / "history.json"),
            "available_actions": _load_workspace_json(agent_workspace / "available_actions.json"),
        }
        decision = select_action(
            config.agent_policy,
            config=config,
            observation=observation,
            memory=memory,
            rng=rng,
        )
        stdout_lines.append(
            json.dumps(
                {
                    "loop_index": loop_index,
                    "policy": config.agent_policy,
                    "decision": decision.__dict__,
                },
                ensure_ascii=False,
            )
        )
        result = run_agent_step(run_dir, decision)
        update_memory(memory, result=result)
        stdout_lines.append(
            json.dumps(
                {
                    "loop_index": loop_index,
                    "accepted": result.get("accepted"),
                    "terminal": result.get("terminal"),
                    "stop_code": result.get("stop_code"),
                    "stop_reason": result.get("stop_reason"),
                    "action_count": result.get("action_count"),
                    "request_count": result.get("request_count"),
                    "cash": result.get("cash"),
                    "day": result.get("day"),
                },
                ensure_ascii=False,
            )
        )
        if result.get("terminal") is True:
            final_result = {
                "status": "completed",
                "stop_reason": str(result.get("stop_reason") or ""),
                "action_count": int(result.get("action_count", 0) or 0),
                "summary": f"Policy {config.agent_policy} completed the benchmark.",
                "backend": "policy",
                "policy": config.agent_policy,
                "session_id": session_id,
            }
            break

    if final_result is None:
        raise BackendError(
            f"Policy {config.agent_policy} exceeded max_steps={max_steps} without reaching terminal state",
            session_id=session_id,
            backend="policy",
        )

    jev_runtime = getattr(memory, "jev_runtime", None)
    if jev_runtime is not None:
        jev_runtime.close()
        (results_dir / "jev_decisions.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in jev_runtime.traces),
            encoding="utf-8",
        )
        append_runtime_event(
            run_dir,
            event="jev.episode.completed",
            calls=jev_runtime.calls,
            estimated_cost=jev_runtime.estimated_cost,
            policy=config.agent_policy,
        )

    _write_agent_logs(
        run_dir,
        backend="policy",
        stdout="\n".join(stdout_lines) + ("\n" if stdout_lines else ""),
        stderr="",
        command=["TadACampaign-policy", str(config.agent_policy)],
    )
    _write_final_result(results_dir, backend="policy", payload=final_result)
    return {
        "backend": "policy",
        "session_id": session_id,
        "thread_id": None,
        "final_result": final_result,
        "stdout_path": str(results_dir / "agent_stdout.log"),
        "final_result_path": str(results_dir / "agent_final_result.json"),
        "resume_meta_path": None,
        "handoff_drill_meta_path": None,
        "resume_count": 0,
        "handoff_count": 0,
        "attempt_count": 1,
    }


def run_backend_episode(
    config: ExperimentConfig,
    *,
    run_dir: Path,
    agent_workspace: Path,
) -> dict[str, Any]:
    if config.agent_backend == "policy":
        return _run_policy_episode(config, run_dir=run_dir, agent_workspace=agent_workspace)

    if config.agent_backend not in {"codex", "claude", "opencode"}:
        raise BackendError(f"Unsupported agent backend {config.agent_backend!r}")

    agent_workspace.mkdir(parents=True, exist_ok=True)
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    if config.agent_backend == "codex":
        _stage_isolated_codex_home(run_dir)
    handoff_drill_state = _HandoffDrillState(config=_load_handoff_drill_config())

    # The timeout is a single budget for the whole Agent episode.  A network
    # interruption may schedule a resume attempt, but it must consume the
    # remaining portion of this same deadline rather than receiving another
    # full per-attempt timeout.
    budget_seconds = max(float(config.agent_timeout_seconds), 0.0)
    budget_started_monotonic = monotonic_now()
    budget_started_at = utc_now_iso()
    budget_deadline_monotonic = budget_started_monotonic + budget_seconds
    _write_agent_budget_meta(
        results_dir,
        started_at=budget_started_at,
        budget_seconds=budget_seconds,
        elapsed_seconds=0.0,
        exhausted=False,
        status="running",
        attempts_total=0,
        resume_count=0,
        handoff_count=0,
        final_session_id=None,
    )
    _append_backend_event(
        run_dir,
        backend=config.agent_backend,
        suffix="budget.started",
        budget_seconds=budget_seconds,
        scope="cumulative_agent_episode",
        started_at=budget_started_at,
    )

    raw_max_resume_attempts = max(int(DEFAULT_CODEX_AUTO_RESUME_MAX_ATTEMPTS), 0)
    auto_resume_enabled = bool(DEFAULT_CODEX_AUTO_RESUME_ENABLED)
    max_total_resume_attempts = None if raw_max_resume_attempts == 0 else raw_max_resume_attempts
    raw_max_handoff_count = int(DEFAULT_CODEX_CONTEXT_WINDOW_HANDOFF_MAX_COUNT)
    max_handoff_count = None if raw_max_handoff_count < 0 else raw_max_handoff_count

    current_invocation = _initial_invocation(config, run_dir=run_dir)
    attempts: list[dict[str, Any]] = []
    retryable_failure_count = 0
    resume_count = 0
    handoff_count = 0
    final_session_id: str | None = None
    final_result: dict[str, Any] | None = None
    final_error: str | None = None
    budget_exhausted = False

    attempt_index = 0
    while True:
        remaining_budget_seconds = _remaining_agent_budget_seconds(budget_deadline_monotonic)
        if remaining_budget_seconds <= 0.0:
            budget_exhausted = True
            final_error = (
                f"{_backend_label(config.agent_backend)} cumulative agent timeout budget exhausted "
                f"after {budget_seconds:.0f}s"
            )
            _append_backend_event(
                run_dir,
                backend=config.agent_backend,
                suffix="timeout.budget_exhausted",
                budget_seconds=budget_seconds,
                elapsed_seconds=_agent_budget_elapsed_seconds(
                    started_monotonic=budget_started_monotonic,
                    budget_seconds=budget_seconds,
                ),
                attempts_total=len(attempts),
                resume_count=resume_count,
                handoff_count=handoff_count,
                final_session_id=final_session_id,
            )
            break
        attempt_index += 1
        attempt_prompt_path = _write_attempt_prompt(
            results_dir,
            backend=current_invocation.backend,
            attempt_index=attempt_index,
            prompt=current_invocation.prompt,
        )
        if attempt_index == 1:
            _write_text(results_dir / "agent_prompt.txt", current_invocation.prompt)
        current_invocation = replace(current_invocation, prompt_path=str(attempt_prompt_path))
        process_result = _run_attempt_process(
            config,
            run_dir=run_dir,
            agent_workspace=agent_workspace,
            invocation=current_invocation,
            attempt_index=attempt_index,
            handoff_drill_state=handoff_drill_state,
            timeout_seconds=remaining_budget_seconds,
            cumulative_budget_seconds=budget_seconds,
            cumulative_budget_started_monotonic=budget_started_monotonic,
        )
        snapshot, snapshot_error = _inspect_attempt(
            current_invocation,
            process_result=process_result,
            agent_workspace=agent_workspace,
            results_dir=results_dir,
        )
        validation_error: Exception | None = snapshot_error
        effective_session_id = snapshot.effective_session_id

        if process_result.returncode == 0 and process_result.error is None and snapshot_error is None:
            try:
                effective_session_id = _validate_success(current_invocation, snapshot, results_dir=results_dir)
                final_result = _extract_episode_result(current_invocation, process_result=process_result, results_dir=results_dir)
            except (BackendError, CodexEventError) as exc:
                validation_error = exc
            else:
                final_session_id = effective_session_id
                attempts.append(
                    _attempt_record(
                        current_invocation,
                        snapshot=_AttemptSnapshot(
                            observed_session_id=snapshot.observed_session_id,
                            effective_session_id=effective_session_id,
                            saw_completion=snapshot.saw_completion,
                            error_messages=snapshot.error_messages,
                            truncated_tail=snapshot.truncated_tail,
                            provider_snapshot=snapshot.provider_snapshot,
                        ),
                        process_result=process_result,
                        attempt_index=attempt_index,
                        classification="success",
                        retryable=False,
                        continuation_mode="terminal",
                        stop_reason=str(final_result.get("stop_reason") or ""),
                        reason_excerpt=str(final_result.get("stop_reason") or "")[:400],
                        events_path="agent_stdout.log",
                        result_snapshot_path="",
                    )
                )
                break

        failure = _classify_failure(
            current_invocation,
            snapshot=snapshot,
            process_error=process_result.error,
            stdout=process_result.stdout,
            stderr=process_result.stderr,
            validation_error=validation_error,
        )
        should_resume = (
            failure.continuation_mode == "resume"
            and auto_resume_enabled
            and effective_session_id is not None
            and (max_total_resume_attempts is None or resume_count < max_total_resume_attempts)
        )
        should_handoff = (
            failure.continuation_mode == "handoff"
            and effective_session_id is not None
            and (max_handoff_count is None or handoff_count < max_handoff_count)
        )
        budget_exhausted = _remaining_agent_budget_seconds(budget_deadline_monotonic) <= 0.0
        if budget_exhausted:
            should_resume = False
            should_handoff = False
        archived_path: Path | None = None
        result_snapshot_path: Path | None = None
        next_invocation: _AttemptInvocation | None = None
        if should_resume or should_handoff:
            retryable_failure_count += 1
            result_snapshot_path = _snapshot_attempt_result(results_dir, backend=current_invocation.backend, attempt_index=attempt_index)
            archived_path = _archive_attempt_logs(results_dir, backend=current_invocation.backend, attempt_index=attempt_index)
            if should_resume:
                resume_count += 1
                next_invocation = _resume_invocation(
                    current_invocation,
                    config=config,
                    run_dir=run_dir,
                    session_id=effective_session_id,
                    trigger=failure.classification,
                )
                if failure.classification in {
                    "retryable_interruption",
                    "retryable_truncated_event_tail",
                    "retryable_missing_turn_completed",
                }:
                    _append_backend_event(
                        run_dir,
                        backend=current_invocation.backend,
                        suffix="network.interruption.detected",
                        classification=failure.classification,
                        source_session_id=effective_session_id,
                        stop_reason=failure.stop_reason,
                        next_attempt=attempt_index + 1,
                        cumulative_budget_seconds=budget_seconds,
                        cumulative_elapsed_seconds=_agent_budget_elapsed_seconds(
                            started_monotonic=budget_started_monotonic,
                            budget_seconds=budget_seconds,
                        ),
                    )
                _append_backend_event(
                    run_dir,
                    backend=current_invocation.backend,
                    suffix="resume.scheduled",
                    source_session_id=effective_session_id,
                    trigger=failure.classification,
                    stop_reason=failure.stop_reason,
                    next_attempt=attempt_index + 1,
                )
            elif should_handoff:
                handoff_count += 1
                handoff_prompt = _build_handoff_prompt(
                    config,
                    backend=current_invocation.backend,
                    agent_workspace=agent_workspace,
                    source_session_id=effective_session_id,
                    trigger=failure.classification,
                    stop_reason=failure.stop_reason,
                )
                handoff_prompt_path = _write_handoff_prompt(
                    results_dir,
                    backend=current_invocation.backend,
                    attempt_index=attempt_index + 1,
                    prompt=handoff_prompt,
                )
                _append_backend_event(
                    run_dir,
                    backend=current_invocation.backend,
                    suffix="handoff.prompt_written",
                    source_session_id=effective_session_id,
                    trigger=failure.classification,
                    prompt_path=str(handoff_prompt_path),
                    next_attempt=attempt_index + 1,
                )
                _append_backend_event(
                    run_dir,
                    backend=current_invocation.backend,
                    suffix="handoff.scheduled",
                    source_session_id=effective_session_id,
                    trigger=failure.classification,
                    stop_reason=failure.stop_reason,
                    prompt_path=str(handoff_prompt_path),
                    next_attempt=attempt_index + 1,
                )
                next_invocation = _handoff_invocation(
                    current_invocation,
                    config=config,
                    run_dir=run_dir,
                    agent_workspace=agent_workspace,
                    session_id=effective_session_id,
                    trigger=failure.classification,
                    stop_reason=failure.stop_reason,
                    prompt_path=str(handoff_prompt_path),
                )

        attempts.append(
            _attempt_record(
                current_invocation,
                snapshot=snapshot,
                process_result=process_result,
                attempt_index=attempt_index,
                classification=failure.classification,
                retryable=failure.retryable,
                continuation_mode=failure.continuation_mode,
                stop_reason=failure.stop_reason,
                reason_excerpt=failure.reason_excerpt,
                events_path=str((archived_path or (results_dir / "agent_stdout.log")).name),
                result_snapshot_path=str(result_snapshot_path.name) if result_snapshot_path else "",
            )
        )

        if next_invocation is not None:
            current_invocation = next_invocation
            backoff_seconds = max(float(DEFAULT_CODEX_AUTO_RESUME_BACKOFF_SECONDS), 0.0)
            if current_invocation.mode == "resume" and backoff_seconds > 0:
                if attempts[-1].get("classification") == "retryable_session_store_conflict":
                    backoff_seconds = max(backoff_seconds, 5.0)
                time.sleep(min(backoff_seconds, _remaining_agent_budget_seconds(budget_deadline_monotonic)))
            continue

        final_session_id = effective_session_id
        if failure.continuation_mode == "handoff" and max_handoff_count is not None and handoff_count >= max_handoff_count:
            final_error = (
                "context_window_handoff_limit_reached: "
                f"hit max fresh handoffs ({max_handoff_count}) after {failure.stop_reason}"
            )
        else:
            final_error = (
                f"{_backend_label(config.agent_backend)} cumulative agent timeout budget exhausted "
                f"after {budget_seconds:.0f}s"
                if budget_exhausted
                else (failure.stop_reason if validation_error is not None else (process_result.error or failure.stop_reason))
            )
        break

    budget_elapsed_seconds = _agent_budget_elapsed_seconds(
        started_monotonic=budget_started_monotonic,
        budget_seconds=budget_seconds,
    )
    if budget_elapsed_seconds >= budget_seconds and budget_seconds > 0.0:
        budget_exhausted = True

    resume_meta_path: Path | None = None
    if (
        len(attempts) > 1
        or retryable_failure_count > 0
        or resume_count > 0
        or handoff_count > 0
        or any(str(item.get("continuation_mode") or "") in {"resume", "handoff"} for item in attempts)
    ):
        payload: dict[str, Any] = {
            "version": 2,
            "enabled": bool(auto_resume_enabled),
            "backend": config.agent_backend,
            "max_resume_attempts": int(raw_max_resume_attempts),
            "backoff_seconds": float(max(DEFAULT_CODEX_AUTO_RESUME_BACKOFF_SECONDS, 0.0)),
            "max_context_window_handoffs": int(raw_max_handoff_count),
            "attempts_total": int(len(attempts)),
            "resume_count": int(resume_count),
            "handoff_count": int(handoff_count),
            "retryable_failure_count": int(retryable_failure_count),
            "success": bool(final_result is not None),
            "final_session_id": final_session_id,
            "agent_timeout_seconds": float(budget_seconds),
            "agent_budget_elapsed_seconds": float(budget_elapsed_seconds),
            "agent_budget_exhausted": bool(budget_exhausted),
            "attempts": attempts,
        }
        if config.agent_backend == "codex":
            payload["final_thread_id"] = final_session_id
        resume_meta_path = _persist_resume_meta(results_dir, backend=config.agent_backend, payload=payload)

    handoff_drill_meta_path: Path | None = None
    if handoff_drill_state.config is not None:
        trigger_records = handoff_drill_state.trigger_records or []
        payload: dict[str, Any] = {
            "version": 2,
            "backend": config.agent_backend,
            "enabled": True,
            "trigger_after_accepted_actions": handoff_drill_state.config.after_accepted_actions,
            "max_triggers": handoff_drill_state.config.max_triggers,
            "triggered": bool(trigger_records),
            "trigger_count": len(trigger_records),
            "triggers": trigger_records,
            "pre_handoff_session_id": (
                attempts[0].get("result_session_id")
                if attempts and str(attempts[0].get("classification") or "") == "fresh_handoff_required"
                else None
            ),
            "post_handoff_session_id": (
                attempts[1].get("result_session_id")
                if len(attempts) > 1 and str(attempts[1].get("mode") or "") == "handoff"
                else None
            ),
            "completed": bool(final_result is not None),
            "final_session_id": final_session_id,
            "final_stop_reason": (
                str(final_result.get("stop_reason") or "")
                if final_result is not None
                else (final_error or "")
            ),
        }
        if config.agent_backend == "codex":
            payload.update(
                {
                    "pre_handoff_thread_id": payload.get("pre_handoff_session_id"),
                    "post_handoff_thread_id": payload.get("post_handoff_session_id"),
                    "final_thread_id": final_session_id,
                }
            )
        handoff_drill_meta_path = _write_handoff_drill_meta(results_dir, payload)

    if final_result is None:
        _write_agent_budget_meta(
            results_dir,
            started_at=budget_started_at,
            budget_seconds=budget_seconds,
            elapsed_seconds=budget_elapsed_seconds,
            exhausted=budget_exhausted,
            status="budget_exhausted" if budget_exhausted else "backend_error",
            attempts_total=len(attempts),
            resume_count=resume_count,
            handoff_count=handoff_count,
            final_session_id=final_session_id,
        )
        raise BackendError(
            final_error or f"{_backend_label(config.agent_backend)} failed without a final result",
            session_id=final_session_id,
            backend=config.agent_backend,
        )

    _write_agent_budget_meta(
        results_dir,
        started_at=budget_started_at,
        budget_seconds=budget_seconds,
        elapsed_seconds=budget_elapsed_seconds,
        exhausted=budget_exhausted,
        status="completed",
        attempts_total=len(attempts),
        resume_count=resume_count,
        handoff_count=handoff_count,
        final_session_id=final_session_id,
    )

    return {
        "backend": config.agent_backend,
        "session_id": final_session_id,
        "thread_id": final_session_id if config.agent_backend == "codex" else None,
        "final_result": final_result,
        "stdout_path": str(results_dir / "agent_stdout.log"),
        "final_result_path": str(results_dir / "agent_final_result.json"),
        "resume_meta_path": str(resume_meta_path) if resume_meta_path is not None else None,
        "handoff_drill_meta_path": str(handoff_drill_meta_path) if handoff_drill_meta_path is not None else None,
        "resume_count": int(resume_count),
        "handoff_count": int(handoff_count),
        "attempt_count": int(len(attempts)),
    }

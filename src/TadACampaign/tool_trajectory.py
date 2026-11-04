"""Per-tool invocation trajectories for agent experiments.

The benchmark agent runs tools as ordinary subprocesses inside its workspace.
This module gives those subprocesses a small, explicit recording contract and
then joins the resulting records with the observable agent event stream after
the run.  The join is deliberately based on a trajectory id printed by the
tool wrapper, rather than on timestamps alone: Codex can start several shell
commands concurrently and its event stream does not carry wall-clock times.

The recorder stores the complete tool command surface and paths to the raw
artifacts.  It does not attempt to capture private model reasoning.  The
"agent judgment" field is the auditable surface: visible agent messages,
tool command output, and subsequent shell/action commands, all with source
line references into the raw event log.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import shlex
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .telemetry import append_jsonl_row, utc_now_iso

TOOL_TRAJECTORY_SCHEMA_VERSION = 1
TOOL_TRAJECTORY_DIRNAME = "tool_trajectories"
TOOL_CALLS_FILENAME = "tool_calls.jsonl"
TOOL_TRAJECTORY_INDEX_FILENAME = "tool_trajectory_index.json"
TOOL_TRAJECTORY_INDEX_JSONL_FILENAME = "tool_trajectory_index.jsonl"
_TRAJECTORY_ID_PATTERN = re.compile(
    r"(?:tool_trajectory_id|trajectory_id)\s*[\":=]+\s*[\"']([^\"']+)[\"']"
)
_TOOL_ID_PATTERN = re.compile(r"owned_skills/([^/\s]+)/source/run\.py|/([^/\s]+)/run\.py")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_tool_id(tool_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tool_id).strip())
    if not normalized:
        raise ValueError("tool_id must not be empty")
    return normalized


def _relative_path(path: Path, root: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return None


def _path_snapshot(raw_path: Any, *, run_root: Path) -> dict[str, Any] | None:
    if raw_path is None:
        return None
    raw = str(raw_path)
    candidate = Path(raw).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    payload: dict[str, Any] = {
        "raw": raw,
        "resolved": str(resolved),
        "relative_to_run_root": _relative_path(resolved, run_root),
        "exists": resolved.exists(),
        "kind": "directory" if resolved.is_dir() else "file" if resolved.is_file() else "missing",
    }
    if resolved.is_file():
        payload["size_bytes"] = resolved.stat().st_size
        payload["sha256"] = _sha256_file(resolved)
    return payload


def _parse_cli_parameters(argv: list[str]) -> dict[str, Any]:
    """Parse flags without imposing a tool-specific argparse schema.

    The exact argv remains authoritative.  This best-effort map makes the
    trajectory index easy to query while preserving repeated flags and
    positional arguments.
    """

    parameters: dict[str, Any] = {}
    positional: list[str] = []
    index = 0
    while index < len(argv):
        token = str(argv[index])
        if not token.startswith("--"):
            positional.append(token)
            index += 1
            continue
        key_value = token[2:]
        if "=" in key_value:
            key, value = key_value.split("=", 1)
        else:
            key = key_value
            if index + 1 < len(argv) and not str(argv[index + 1]).startswith("--"):
                index += 1
                value = str(argv[index])
            else:
                value = True
        key = key.replace("-", "_")
        if key in parameters:
            previous = parameters[key]
            if not isinstance(previous, list):
                previous = [previous]
            previous.append(value)
            parameters[key] = previous
        else:
            parameters[key] = value
        index += 1
    if positional:
        parameters["_positional"] = positional
    return parameters


@contextlib.contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # The benchmark runs on POSIX.  The fallback keeps the recorder
            # usable in lightweight unit-test environments on other hosts.
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def _reserve_call_slot(run_root: Path, tool_id: str) -> tuple[int, int, Path]:
    trajectory_root = run_root / TOOL_TRAJECTORY_DIRNAME
    trajectory_root.mkdir(parents=True, exist_ok=True)
    state_path = trajectory_root / ".state.json"
    lock_path = trajectory_root / ".lock"
    with _exclusive_file_lock(lock_path):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            state = {"next_global_call_index": 1, "next_tool_call_index": {}}
        if not isinstance(state, dict):
            state = {"next_global_call_index": 1, "next_tool_call_index": {}}
        per_tool = state.get("next_tool_call_index")
        if not isinstance(per_tool, dict):
            per_tool = {}
        global_index = int(state.get("next_global_call_index", 1))
        tool_index = int(per_tool.get(tool_id, 1))
        per_tool[tool_id] = tool_index + 1
        state["next_global_call_index"] = global_index + 1
        state["next_tool_call_index"] = per_tool
        _write_json(state_path, state)
    call_dir = trajectory_root / tool_id / f"call-{tool_index:04d}"
    call_dir.mkdir(parents=True, exist_ok=False)
    return global_index, tool_index, call_dir


def _argument_value(argv: list[str], key: str) -> str | None:
    flag = f"--{key}"
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return str(argv[index + 1])
        if token.startswith(flag + "="):
            return token.split("=", 1)[1]
    return None


def _tool_id_from_argv(argv: list[str]) -> str | None:
    value = _argument_value(argv, "tool")
    return value.strip() if value and value.strip() else None


def _last_json_line(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


class ToolTrajectoryRecorder:
    """Record one tool invocation and its local artifacts."""

    def __init__(self, *, tool_id: str, argv: list[str], run_root: Path) -> None:
        self.tool_id = _safe_tool_id(tool_id)
        self.argv = [str(value) for value in argv]
        self.run_root = run_root.expanduser().resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.global_call_index, self.tool_call_index, self.call_dir = _reserve_call_slot(self.run_root, self.tool_id)
        self.trajectory_id = f"{self.global_call_index:06d}-{self.tool_id}-call-{self.tool_call_index:04d}"
        self.started_at = utc_now_iso()
        self.started_epoch = time.time()
        self.parameters = _parse_cli_parameters(self.argv)
        script = Path(sys.argv[0]).resolve()
        path_keys = {"input", "rows", "output", "run_root"}
        paths = {
            key: _path_snapshot(value, run_root=self.run_root)
            for key, value in self.parameters.items()
            if key in path_keys and not isinstance(value, list)
        }
        self.manifest_path = self.call_dir / "manifest.json"
        self.manifest: dict[str, Any] = {
            "schema_version": TOOL_TRAJECTORY_SCHEMA_VERSION,
            "trajectory_id": self.trajectory_id,
            "tool_id": self.tool_id,
            "global_call_index": self.global_call_index,
            "tool_call_index": self.tool_call_index,
            "status": "running",
            "started_at": self.started_at,
            "started_epoch": self.started_epoch,
            "cwd": str(Path.cwd().resolve()),
            "executable": str(Path(sys.executable).resolve()),
            "script": str(script),
            "argv": self.argv,
            "command": shlex.join([sys.executable, str(script), *self.argv]),
            "parameters": self.parameters,
            "paths_at_start": paths,
            "run_root": str(self.run_root),
            "run_root_relative_trajectory_dir": _relative_path(self.call_dir, self.run_root),
            "logs": {
                "manifest": str(self.manifest_path),
                "stdout": str(self.call_dir / "stdout.log"),
                "stderr": str(self.call_dir / "stderr.log"),
                "tool_calls_jsonl": str(self.run_root / TOOL_CALLS_FILENAME),
            },
        }
        _write_json(self.manifest_path, self.manifest)

    def finish(self, *, exit_code: int, stdout: str, stderr: str, exception: str | None = None) -> dict[str, Any]:
        finished_at = utc_now_iso()
        finished_epoch = time.time()
        stdout_path = self.call_dir / "stdout.log"
        stderr_path = self.call_dir / "stderr.log"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        parameters = self.parameters
        output_value = parameters.get("output")
        output_snapshot = _path_snapshot(output_value, run_root=self.run_root)
        input_snapshot = _path_snapshot(parameters.get("input"), run_root=self.run_root)
        rows_snapshot = _path_snapshot(parameters.get("rows"), run_root=self.run_root)
        result_payload = _last_json_line(stdout)
        self.manifest.update(
            {
                "status": "completed" if int(exit_code) == 0 else "failed",
                "exit_code": int(exit_code),
                "finished_at": finished_at,
                "finished_epoch": finished_epoch,
                "duration_seconds": max(finished_epoch - self.started_epoch, 0.0),
                "exception": exception,
                "paths_at_finish": {
                    "input": input_snapshot,
                    "rows": rows_snapshot,
                    "output": output_snapshot,
                },
                "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
                "result_stdout": result_payload,
            }
        )
        _write_json(self.manifest_path, self.manifest)
        event = dict(self.manifest)
        event.update(
            {
                "trajectory_manifest": str(self.manifest_path),
                "trajectory_dir": str(self.call_dir),
                "input": input_snapshot.get("resolved") if input_snapshot else None,
                "rows": rows_snapshot.get("resolved") if rows_snapshot else None,
                "output": output_snapshot.get("resolved") if output_snapshot else None,
                "input_sha256": input_snapshot.get("sha256") if input_snapshot else None,
                "rows_sha256": rows_snapshot.get("sha256") if rows_snapshot else None,
                "output_sha256": output_snapshot.get("sha256") if output_snapshot else None,
            }
        )
        append_jsonl_row(self.run_root / TOOL_CALLS_FILENAME, event)
        return self.manifest


def run_tool(
    main: Callable[[list[str] | None], int | None],
    argv: list[str] | None = None,
    *,
    tool_id: str | None = None,
    announce: bool = True,
) -> int:
    """Run a tool entrypoint while recording its complete local trajectory.

    A generated tool wrapper should call this instead of calling ``main``
    directly.  The wrapper's final JSON line contains the trajectory id, which
    lets the post-run assembler join it to Codex's command event even when a
    shell command contains several tool invocations.
    """

    effective_argv = list(sys.argv[1:] if argv is None else argv)
    effective_tool_id = tool_id or _tool_id_from_argv(effective_argv)
    run_root_value = _argument_value(effective_argv, "run-root")
    if not effective_tool_id or not run_root_value:
        # Keep the helper useful outside an experiment, but do not silently
        # claim that an untracked invocation is auditable.
        result = main(effective_argv)
        return int(result or 0)

    recorder = ToolTrajectoryRecorder(
        tool_id=effective_tool_id,
        argv=effective_argv,
        run_root=Path(run_root_value),
    )
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    exit_code = 0
    exception_text: str | None = None
    try:
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            result = main(effective_argv)
            exit_code = int(result or 0)
    except SystemExit as exc:
        raw_code = exc.code
        exit_code = int(raw_code) if isinstance(raw_code, int) else 0 if raw_code is None else 1
    except BaseException as exc:  # pragma: no cover - exercised through the CLI process
        exit_code = 1
        exception_text = f"{type(exc).__name__}: {exc}"
        traceback.print_exc(file=stderr_buffer)
    stdout = stdout_buffer.getvalue()
    stderr = stderr_buffer.getvalue()
    manifest = recorder.finish(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        exception=exception_text,
    )
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    if announce:
        announcement = {
            "status": "ok" if exit_code == 0 else "error",
            "tool_id": recorder.tool_id,
            "trajectory_id": recorder.trajectory_id,
            "trajectory_manifest": str(recorder.manifest_path),
            "trajectory_dir": str(recorder.call_dir),
            "exit_code": exit_code,
            "output": manifest.get("paths_at_finish", {}).get("output"),
        }
        print(json.dumps(announcement, ensure_ascii=False))
    return exit_code


def _read_jsonl(path: Path) -> tuple[list[tuple[int, dict[str, Any]]], list[dict[str, Any]]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    if not path.is_file():
        return rows, errors
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append({"line_number": line_number, "error": str(exc)})
            continue
        if isinstance(payload, dict):
            rows.append((line_number, payload))
        else:
            errors.append({"line_number": line_number, "error": "JSON value is not an object"})
    return rows, errors


def _agent_event_records(path: Path | Sequence[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paths = [path] if isinstance(path, Path) else list(path)
    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for source_path in paths:
        rows, source_errors = _read_jsonl(source_path)
        errors.extend({**error, "source_path": str(source_path)} for error in source_errors)
        for line_number, payload in rows:
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            item_type = str(item.get("type") or "")
            event_type = str(payload.get("type") or "")
            record: dict[str, Any] = {
                "event_order": len(events),
                "source_path": str(source_path),
                "line_number": line_number,
                "event_type": event_type,
                "item_type": item_type,
                "item_id": item.get("id"),
            }
            if item_type in {"agent_message", "assistant_message"}:
                record["text"] = str(item.get("text") or "")
            if item_type == "command_execution":
                record.update(
                    {
                        "command": str(item.get("command") or ""),
                        "aggregated_output": str(item.get("aggregated_output") or ""),
                        "exit_code": item.get("exit_code"),
                        "status": item.get("status"),
                    }
                )
            events.append(record)
    return events, errors


def _trajectory_ids_in_text(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for match in _TRAJECTORY_ID_PATTERN.finditer(text)))


def _looks_like_tool_command(command: str, tool_id: str) -> bool:
    normalized = command.lower()
    if not command or any(normalized.lstrip().startswith(prefix) for prefix in ("sed ", "cat ", "head ", "tail ", "less ", "grep ", "find ")):
        return False
    if "run.py" not in normalized or tool_id.lower() not in normalized:
        return False
    return "owned_skills/" in normalized or f"/{tool_id.lower()}/" in normalized


def _event_link(event: dict[str, Any], *, full_output: bool = False) -> dict[str, Any]:
    output = str(event.get("aggregated_output") or "")
    payload = {
        "event_order": event.get("event_order"),
        "source_path": event.get("source_path"),
        "line_number": event.get("line_number"),
        "event_type": event.get("event_type"),
        "item_type": event.get("item_type"),
        "item_id": event.get("item_id"),
        "command": event.get("command"),
        "exit_code": event.get("exit_code"),
        "status": event.get("status"),
        "aggregated_output_excerpt": output[:4000],
    }
    if full_output:
        payload["aggregated_output"] = output
    return payload


def _message_payload(event: dict[str, Any], *, full_text: bool) -> dict[str, Any]:
    text = str(event.get("text") or "")
    payload: dict[str, Any] = {
        "event_order": event.get("event_order"),
        "source_path": event.get("source_path"),
        "line_number": event.get("line_number"),
        "text_excerpt": text[:4000],
    }
    if full_text:
        payload["text"] = text
    return payload


def _run_started_at(run_dir: Path) -> str | None:
    timeline = run_dir / "results" / "runtime_timeline.jsonl"
    rows, _ = _read_jsonl(timeline)
    for _, row in rows:
        if row.get("event") == "run.started":
            value = row.get("started_at") or row.get("timestamp")
            if isinstance(value, str):
                return value
    return None


def _epoch_from_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _trajectory_manifests(run_dir: Path) -> list[Path]:
    roots = [run_dir / "process" / "agent_workspace", run_dir]
    found: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob(f"**/{TOOL_TRAJECTORY_DIRNAME}/*/call-*/manifest.json"):
            found.add(path.resolve())
    return sorted(found)


def _agent_context_for_trajectory(
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    trajectory_id = str(manifest.get("trajectory_id") or "")
    linked: list[dict[str, Any]] = []
    for event in events:
        if event.get("item_type") != "command_execution":
            continue
        text = f"{event.get('aggregated_output') or ''}\n{event.get('command') or ''}"
        if trajectory_id in _trajectory_ids_in_text(text):
            linked.append(event)

    mapping_method = "trajectory_id"
    if not linked:
        tool_id = str(manifest.get("tool_id") or "")
        candidates = [
            event
            for event in events
            if event.get("item_type") == "command_execution"
            and _looks_like_tool_command(str(event.get("command") or ""), tool_id)
        ]
        ordinal = max(int(manifest.get("tool_call_index", 1)) - 1, 0)
        if ordinal < len(candidates):
            linked = [candidates[ordinal]]
            mapping_method = "tool_command_fallback"
        else:
            mapping_method = "unmatched"

    anchor_orders = [int(event["event_order"]) for event in linked if event.get("event_order") is not None]
    anchor_start = min(anchor_orders) if anchor_orders else None
    anchor_end = max(anchor_orders) if anchor_orders else None
    all_tool_orders = sorted(
        int(event["event_order"])
        for event in events
        if event.get("item_type") == "command_execution"
        and _trajectory_ids_in_text(f"{event.get('aggregated_output') or ''}\n{event.get('command') or ''}")
        and event.get("event_order") is not None
    )
    next_tool_order = next((order for order in all_tool_orders if anchor_end is not None and order > anchor_end), None)
    before_messages = [
        event
        for event in events
        if event.get("item_type") in {"agent_message", "assistant_message"}
        and anchor_start is not None
        and int(event.get("event_order", -1)) < anchor_start
    ][-3:]
    after_messages = [
        event
        for event in events
        if event.get("item_type") in {"agent_message", "assistant_message"}
        and anchor_end is not None
        and int(event.get("event_order", -1)) > anchor_end
        and (next_tool_order is None or int(event.get("event_order", -1)) < next_tool_order)
    ][:5]
    nearby_commands = [
        event
        for event in events
        if event.get("item_type") == "command_execution"
        and anchor_start is not None
        and anchor_end is not None
        and anchor_start <= int(event.get("event_order", -1)) <= (next_tool_order or anchor_end + 60)
    ][:20]
    context = {
        "schema_version": TOOL_TRAJECTORY_SCHEMA_VERSION,
        "trajectory_id": trajectory_id,
        "mapping_method": mapping_method,
        "event_links": [_event_link(event, full_output=True) for event in linked],
        "agent_messages_before": [_message_payload(event, full_text=True) for event in before_messages],
        "agent_messages_after": [_message_payload(event, full_text=True) for event in after_messages],
        "nearby_commands": [_event_link(event, full_output=True) for event in nearby_commands],
    }
    judgment = {
        "mapping_method": mapping_method,
        "source_references": [
            {"source_path": event.get("source_path"), "line_number": event.get("line_number")}
            for event in linked
        ],
        "before_messages": [_message_payload(event, full_text=False) for event in before_messages],
        "after_messages": [_message_payload(event, full_text=False) for event in after_messages],
        "related_commands": [_event_link(event, full_output=False) for event in linked],
    }
    return context, judgment


def build_tool_trajectory_index(run_dir: Path) -> Path:
    """Join all per-call records to the observable agent event stream.

    The function is safe to call at run finalization and later during audit.
    It always writes an index, including an explicit warning when the run has
    no instrumented calls or no agent event source.
    """

    run_dir = run_dir.expanduser().resolve()
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    manifests = _trajectory_manifests(run_dir)
    codex_event_paths = sorted(
        results_dir.glob("codex_events*.jsonl"),
        key=lambda path: (path.name == "codex_events.jsonl", path.name),
    )
    event_path_candidates = [
        results_dir / "agent_stdout.log",
        results_dir / "agent_events.jsonl",
    ]
    event_paths = codex_event_paths or [path for path in event_path_candidates if path.is_file()]
    events, event_errors = _agent_event_records(event_paths) if event_paths else ([], [])
    run_started_at = _run_started_at(run_dir)
    run_started_epoch = _epoch_from_iso(run_started_at)
    calls: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not manifests:
        warnings.append("No instrumented tool trajectory manifests were found")
    if not event_paths:
        warnings.append("No observable agent event stream was found")
    if event_errors:
        warnings.append(f"Agent event stream had {len(event_errors)} malformed JSON lines")

    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"Could not read trajectory manifest {manifest_path}: {exc}")
            continue
        if not isinstance(manifest, dict):
            warnings.append(f"Trajectory manifest is not an object: {manifest_path}")
            continue
        context, judgment = _agent_context_for_trajectory(manifest, events)
        context_path = manifest_path.parent / "agent_context.json"
        _write_json(context_path, context)
        started_epoch = manifest.get("started_epoch")
        finished_epoch = manifest.get("finished_epoch")
        manifest["run_started_at"] = run_started_at
        manifest["started_offset_seconds"] = (
            float(started_epoch) - run_started_epoch if run_started_epoch is not None and isinstance(started_epoch, (int, float)) else None
        )
        manifest["finished_offset_seconds"] = (
            float(finished_epoch) - run_started_epoch if run_started_epoch is not None and isinstance(finished_epoch, (int, float)) else None
        )
        manifest["agent_context_path"] = str(context_path)
        manifest["agent_judgment"] = judgment
        _write_json(manifest_path, manifest)
        calls.append(
            {
                "trajectory_id": manifest.get("trajectory_id"),
                "tool_id": manifest.get("tool_id"),
                "global_call_index": manifest.get("global_call_index"),
                "tool_call_index": manifest.get("tool_call_index"),
                "status": manifest.get("status"),
                "exit_code": manifest.get("exit_code"),
                "started_at": manifest.get("started_at"),
                "finished_at": manifest.get("finished_at"),
                "started_epoch": manifest.get("started_epoch"),
                "finished_epoch": manifest.get("finished_epoch"),
                "started_offset_seconds": manifest.get("started_offset_seconds"),
                "finished_offset_seconds": manifest.get("finished_offset_seconds"),
                "duration_seconds": manifest.get("duration_seconds"),
                "command": manifest.get("command"),
                "argv": manifest.get("argv"),
                "parameters": manifest.get("parameters"),
                "paths_at_finish": manifest.get("paths_at_finish"),
                "logs": manifest.get("logs"),
                "trajectory_dir": str(manifest_path.parent),
                "trajectory_manifest": str(manifest_path),
                "agent_context_path": str(context_path),
                "agent_judgment": judgment,
            }
        )

    calls.sort(key=lambda item: (int(item.get("global_call_index") or 0), str(item.get("trajectory_id") or "")))
    by_tool: dict[str, dict[str, Any]] = {}
    for call in calls:
        tool_id = str(call.get("tool_id") or "unknown")
        group = by_tool.setdefault(tool_id, {"call_count": 0, "trajectory_ids": [], "calls": []})
        group["call_count"] += 1
        group["trajectory_ids"].append(call.get("trajectory_id"))
        group["calls"].append(call.get("global_call_index"))
    payload = {
        "schema_version": TOOL_TRAJECTORY_SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "run_dir": str(run_dir),
        "run_started_at": run_started_at,
        "agent_event_source": str(event_paths[0]) if len(event_paths) == 1 else None,
        "agent_event_sources": [str(path) for path in event_paths],
        "trajectory_manifest_count": len(manifests),
        "tool_call_count": len(calls),
        "tool_ids": sorted(by_tool),
        "by_tool": by_tool,
        "warnings": warnings,
        "calls": calls,
    }
    index_path = results_dir / TOOL_TRAJECTORY_INDEX_FILENAME
    _write_json(index_path, payload)
    jsonl_path = results_dir / TOOL_TRAJECTORY_INDEX_JSONL_FILENAME
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for call in calls:
            handle.write(json.dumps(call, ensure_ascii=False) + "\n")
    return index_path


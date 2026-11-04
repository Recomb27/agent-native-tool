"""Audit Codex session completeness and game-rule terminal evidence.

The controller's terminal status is not sufficient evidence that an Agent run
is usable.  This module reads every raw ``codex_events*.jsonl`` stream, checks
the thread and turn lifecycle, joins recovery metadata, and independently
checks the benchmark state/history that explains why the game stopped.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


NETWORK_MARKERS = (
    "apiconnectionerror",
    "api connection error",
    "stream disconnected",
    "stream closed",
    "stream error",
    "reconnecting",
    "timeout waiting for sse",
    "connection reset",
    "connection aborted",
    "connection closed",
    "connection refused",
    "network error",
    "transport error",
    "websocket closed",
    "temporarily unavailable",
    "too many requests",
    "rate limit",
    "bad gateway",
    "service unavailable",
)

NETWORK_EVENT_TYPES = {"error", "turn.failed"}
AUTOMATIC_STOP_CODES = {
    "cash_insufficient",
    "max_experiment_days",
    "no_visible_path",
    "time_insufficient",
}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return rows, errors
    for line_number, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            errors.append(
                {
                    "line": line_number,
                    "kind": "invalid_json",
                    "message": str(exc),
                    "truncated_tail": not raw_line.endswith(("\n", "\r")),
                }
            )
            continue
        if not isinstance(payload, dict):
            errors.append({"line": line_number, "kind": "non_object"})
            continue
        rows.append(payload)
    return rows, errors


def _network_message(message: Any) -> bool:
    normalized = str(message or "").lower()
    return any(marker in normalized for marker in NETWORK_MARKERS)


def _event_error_message(payload: dict[str, Any]) -> str | None:
    event_type = str(payload.get("type") or "").strip()
    if event_type == "error":
        message = payload.get("message")
        return str(message).strip() if isinstance(message, str) else "unknown stream error"
    if event_type == "turn.failed":
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return "turn.failed"
    return None


def _parse_agent_result(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _stream_info(path: Path, *, is_final: bool) -> dict[str, Any]:
    rows, parse_errors = _read_jsonl(path)
    event_types = Counter(str(row.get("type") or "") for row in rows)
    thread_ids = [
        str(row["thread_id"])
        for row in rows
        if row.get("type") == "thread.started" and isinstance(row.get("thread_id"), str)
    ]
    errors: list[str] = []
    network_errors: list[str] = []
    for row in rows:
        message = _event_error_message(row)
        if message is None:
            continue
        errors.append(message)
        if _network_message(message):
            network_errors.append(message)

    agent_messages: list[str] = []
    agent_results: list[dict[str, Any]] = []
    for row in rows:
        if row.get("type") != "item.completed":
            continue
        item = row.get("item")
        if not isinstance(item, dict) or item.get("type") not in {"agent_message", "assistant_message"}:
            continue
        text = item.get("text")
        if isinstance(text, str):
            agent_messages.append(text)
            result = _parse_agent_result(text)
            if result is not None:
                agent_results.append(result)

    return {
        "path": str(path),
        "filename": path.name,
        "is_final": is_final,
        "event_count": len(rows),
        "event_types": dict(event_types),
        "parse_errors": parse_errors,
        "thread_started_ids": thread_ids,
        "thread_started_count": len(thread_ids),
        "turn_started_count": event_types.get("turn.started", 0),
        "turn_completed_count": event_types.get("turn.completed", 0),
        "last_event_type": rows[-1].get("type") if rows else None,
        "error_messages": errors,
        "network_error_messages": network_errors,
        "agent_message_count": len(agent_messages),
        "agent_results": agent_results,
    }


def _event_stream_paths(results_dir: Path) -> list[Path]:
    paths = sorted(results_dir.glob("codex_events*.jsonl"))
    return paths


def _timeline_data(results_dir: Path) -> dict[str, Any]:
    path = results_dir / "runtime_timeline.jsonl"
    rows, parse_errors = _read_jsonl(path)
    resume_events = [
        row
        for row in rows
        if row.get("event") in {"agent.resume.scheduled", "codex.resume.scheduled"}
    ]
    network_events = [
        row
        for row in rows
        if row.get("event") in {"agent.network.interruption.detected", "codex.network.interruption.detected"}
    ]
    attempts = [row for row in rows if row.get("event") in {"agent.attempt.started", "codex.attempt.started"}]
    return {
        "path": str(path),
        "parse_errors": parse_errors,
        "resume_events": resume_events,
        "network_events": network_events,
        "attempt_started_events": attempts,
        "event_count": len(rows),
    }


def _latest_available_actions(workspace: Path) -> dict[str, Any] | None:
    candidates: list[tuple[int, Path]] = []
    for path in workspace.glob("step_*/available_actions.json"):
        match = re.fullmatch(r"step_(\d+)", path.parent.name)
        if match:
            candidates.append((int(match.group(1)), path))
    if not candidates:
        path = workspace / "available_actions.json"
        return _read_json(path) if path.is_file() else None
    _, path = max(candidates)
    payload = _read_json(path)
    return payload if isinstance(payload, dict) else None


def _non_stop_actions_available(actions: dict[str, Any] | None) -> bool | None:
    if actions is None:
        return None
    for key, value in actions.items():
        if key == "stop":
            continue
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
    return False


def _job_id(run_dir: Path) -> str | None:
    try:
        condition = run_dir.parents[2].name
        tool = run_dir.parents[1].name
        repeat = run_dir.parent.name
    except IndexError:
        return None
    if condition in {"focal", "no-tool"} and repeat.startswith("repeat-"):
        return f"{tool}__{condition}__{repeat}"
    return None


def _game_stop_audit(run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    results_dir = run_dir / "results"
    workspace = run_dir / "process" / "agent_workspace"
    state = _read_json(workspace / "state.json")
    history = _read_json(workspace / "history.json")
    available_actions = _latest_available_actions(workspace)
    state = state if isinstance(state, dict) else {}
    history = history if isinstance(history, dict) else {}
    recent_actions = history.get("recent_actions")
    recent_actions = recent_actions if isinstance(recent_actions, list) else []
    last_action = recent_actions[-1] if recent_actions and isinstance(recent_actions[-1], dict) else {}

    stop_code = summary.get("stop_code")
    state_stop_code = state.get("stop_code")
    stop_reason = str(summary.get("stop_reason") or "")
    day_final = summary.get("day_final")
    max_days = summary.get("max_experiment_days")
    remaining_days = summary.get("remaining_days_final")
    cash_final = summary.get("cash_final")

    state_matches = bool(stop_code and state_stop_code and stop_code == state_stop_code)
    if stop_code == "agent_stop":
        terminal_action = last_action.get("action") == "stop"
        trigger = "agent_declared_stop"
        lower_reason = stop_reason.lower()
        if "confirm_only" in lower_reason or "pending confirmation" in lower_reason or "pending hit" in lower_reason:
            trigger = "agent_stop_confirm_only_or_no_confirmable_hit"
        elif "remaining" in stop_reason or "days left" in lower_reason or "days remaining" in lower_reason:
            trigger = "agent_stop_time_or_confirm_window"
    elif stop_code == "max_experiment_days":
        terminal_action = last_action.get("action") != "stop"
        trigger = "max_experiment_days"
    elif stop_code == "time_insufficient":
        terminal_action = last_action.get("action") != "stop"
        trigger = "time_insufficient"
    elif stop_code == "cash_insufficient":
        terminal_action = last_action.get("action") != "stop"
        trigger = "cash_insufficient"
    elif stop_code == "no_visible_path":
        terminal_action = last_action.get("action") != "stop"
        trigger = "no_visible_path"
    else:
        terminal_action = False
        trigger = "unknown_stop_code"

    day_evidence = {
        "day_final": day_final,
        "max_experiment_days": max_days,
        "remaining_days_final": remaining_days,
        "reached_max_experiment_days": (
            isinstance(day_final, (int, float))
            and isinstance(max_days, (int, float))
            and float(day_final) >= float(max_days) - 1e-9
        ),
    }
    return {
        "stop_code": stop_code,
        "stop_reason": stop_reason,
        "trigger": trigger,
        "cash_final": cash_final,
        "day_evidence": day_evidence,
        "pending_confirmation_count": summary.get("pending_confirmation_count"),
        "confirmed_best_activity": summary.get("confirmed_best_activity"),
        "state_stop_code": state_stop_code,
        "state_stop_reason": state.get("stop_reason"),
        "last_action": last_action,
        "last_action_is_terminal_evidence": terminal_action,
        "state_summary": state.get("economic_summary"),
        "latest_available_actions": available_actions,
        "non_stop_actions_available": _non_stop_actions_available(available_actions),
        "summary_state_stop_match": state_matches,
        "game_rule_valid": bool(
            stop_code
            and state_matches
            and terminal_action
            and (state.get("stop_reason") or stop_reason)
        ),
    }


def _session_audit(run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    results_dir = run_dir / "results"
    streams = [
        _stream_info(path, is_final=path.name == "codex_events.jsonl")
        for path in _event_stream_paths(results_dir)
    ]
    final_stream = next((stream for stream in streams if stream["is_final"]), None)
    if final_stream is None and streams:
        final_stream = streams[-1]
    expected_thread_id = summary.get("agent_session_id") or summary.get("codex_thread_id")
    expected_thread_id = expected_thread_id if isinstance(expected_thread_id, str) else None
    timeline = _timeline_data(results_dir)
    result_payload = _read_json(results_dir / "agent_final_result.json")
    codex_result_payload = _read_json(results_dir / "codex_final_result.json")
    result_payload = result_payload if isinstance(result_payload, dict) else None
    codex_result_payload = codex_result_payload if isinstance(codex_result_payload, dict) else None

    all_network_errors = [
        message
        for stream in streams
        for message in stream["network_error_messages"]
    ]
    final_thread_ids = final_stream["thread_started_ids"] if final_stream else []
    thread_match = bool(
        final_thread_ids
        and (expected_thread_id is None or set(final_thread_ids) == {expected_thread_id})
    )
    final_stream_complete = bool(
        final_stream
        and not final_stream["parse_errors"]
        and final_stream["thread_started_count"] == 1
        and final_stream["turn_completed_count"] >= 1
        and final_stream["last_event_type"] == "turn.completed"
        and thread_match
    )
    final_result_valid = bool(
        result_payload
        and result_payload.get("status") == "completed"
        and isinstance(result_payload.get("stop_reason"), str)
        and "action_count" in result_payload
        and codex_result_payload
        and codex_result_payload.get("status") == "completed"
    )
    incomplete_archives = [
        stream
        for stream in streams
        if not stream["is_final"]
        and (
            stream["parse_errors"]
            or stream["turn_completed_count"] == 0
            or stream["last_event_type"] != "turn.completed"
        )
    ]
    resume_pairs = [
        {
            "source_session_id": row.get("source_session_id"),
            "resume_session_id": row.get("resume_session_id"),
            "next_attempt": row.get("next_attempt"),
        }
        for row in timeline["resume_events"]
    ]
    same_session_resume = any(
        pair["source_session_id"]
        and pair["source_session_id"] == pair["resume_session_id"]
        for pair in resume_pairs
    )
    if incomplete_archives and not same_session_resume:
        network_status = "interrupted_without_verified_resume"
    elif all_network_errors and final_stream_complete:
        network_status = "in_stream_reconnect_completed"
    elif same_session_resume:
        network_status = "same_session_resume_completed"
    elif all_network_errors:
        network_status = "network_event_needs_review"
    else:
        network_status = "none"

    session_complete = bool(final_stream_complete and final_result_valid and not incomplete_archives)
    return {
        "expected_thread_id": expected_thread_id,
        "streams": streams,
        "timeline": timeline,
        "network_error_count": len(all_network_errors),
        "network_error_messages": all_network_errors,
        "resume_pairs": resume_pairs,
        "same_session_resume_verified": same_session_resume,
        "network_status": network_status,
        "final_stream_complete": final_stream_complete,
        "final_result_valid": final_result_valid,
        "incomplete_archived_streams": [stream["filename"] for stream in incomplete_archives],
        "session_complete": session_complete,
        "session_usable": session_complete,
        "result_payload": result_payload,
    }


def audit_run(run_dir: Path) -> dict[str, Any]:
    summary = _read_json(run_dir / "results" / "summary.json")
    summary = summary if isinstance(summary, dict) else {}
    session = _session_audit(run_dir, summary)
    game = _game_stop_audit(run_dir, summary)
    if not session["session_complete"] or not game["game_rule_valid"]:
        eligibility = "exclude_technical_or_invalid_terminal"
    elif session["network_status"] == "in_stream_reconnect_completed":
        eligibility = "eligible_with_network_review"
    elif session["network_status"] == "same_session_resume_completed":
        eligibility = "eligible_with_resume_review"
    else:
        eligibility = "eligible"
    return {
        "schema_version": 1,
        "run_dir": str(run_dir),
        "job_id": _job_id(run_dir),
        "run_name": summary.get("run_name"),
        "controller_summary_status": summary.get("stop_code"),
        "session": session,
        "game_stop": game,
        "analysis_eligibility": eligibility,
    }


def _iter_run_dirs(root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    evidence_root = root / "runs"
    if not evidence_root.is_dir():
        return
    candidates = [
        *evidence_root.rglob("results/summary.json"),
        *evidence_root.rglob("results/codex_events*.jsonl"),
    ]
    for evidence_path in sorted(candidates):
        run_dir = evidence_path.parent.parent
        if run_dir not in seen:
            seen.add(run_dir)
            yield run_dir


def audit_experiment(root: Path) -> dict[str, Any]:
    runs = [audit_run(run_dir) for run_dir in _iter_run_dirs(root)]
    eligibility_counts = Counter(str(run["analysis_eligibility"]) for run in runs)
    stop_counts = Counter(str(run["game_stop"]["stop_code"]) for run in runs)
    network_counts = Counter(str(run["session"]["network_status"]) for run in runs)
    return {
        "schema_version": 1,
        "root": str(root),
        "run_count": len(runs),
        "counts": {
            "analysis_eligibility": dict(sorted(eligibility_counts.items())),
            "stop_code": dict(sorted(stop_counts.items())),
            "network_status": dict(sorted(network_counts.items())),
        },
        "runs": runs,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Codex session and game-stop audit",
        "",
        f"- Root: `{report['root']}`",
        f"- Runs audited: `{report['run_count']}`",
        "",
        "## Counts",
        "",
        "| category | value | count |",
        "| --- | --- | ---: |",
    ]
    for category, values in report["counts"].items():
        for value, count in values.items():
            lines.append(f"| {category} | `{value}` | {count} |")
    lines.extend(
        [
            "",
            "## Run-level audit",
            "",
            "| run | stop code | day | cash | session | network | eligibility |",
            "| --- | --- | ---: | ---: | --- | --- | --- |",
        ]
    )
    for run in report["runs"]:
        game = run["game_stop"]
        session = run["session"]
        day = game["day_evidence"].get("day_final")
        cash = game.get("cash_final")
        lines.append(
            "| {run} | `{stop}` | {day} | {cash} | `{complete}` | `{network}` | `{eligibility}` |".format(
                run=run.get("job_id") or run.get("run_name") or run["run_dir"],
                stop=game.get("stop_code"),
                day=day,
                cash=cash,
                complete=session.get("session_complete"),
                network=session.get("network_status"),
                eligibility=run.get("analysis_eligibility"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Experiment root containing runs/")
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--markdown-output", type=Path, default=None)
    args = parser.parse_args(argv)
    report = audit_experiment(args.root)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload, encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    if not args.json_output and not args.markdown_output:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

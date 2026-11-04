from __future__ import annotations

import json
from pathlib import Path

from TadACampaign.session_audit import audit_experiment, audit_run


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_stream(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _make_run(tmp_path: Path, *, stop_code: str = "agent_stop", final_action: str = "stop") -> Path:
    run_dir = tmp_path / "runs" / "focal" / "tool-a" / "repeat-01" / "run-01"
    results = run_dir / "results"
    workspace = run_dir / "process" / "agent_workspace"
    summary = {
        "run_name": "run-01",
        "stop_code": stop_code,
        "stop_reason": "confirm_only stop" if stop_code == "agent_stop" else "Reached max_experiment_days",
        "cash_final": 1000.0,
        "day_final": 10.0 if stop_code == "agent_stop" else 180.0,
        "max_experiment_days": 180.0,
        "remaining_days_final": 170.0 if stop_code == "agent_stop" else 0.0,
        "pending_confirmation_count": 0,
        "confirmed_best_activity": 1.0,
        "agent_session_id": "thread-1",
    }
    state = {
        "stop_code": stop_code,
        "stop_reason": summary["stop_reason"],
        "economic_summary": {"budget_regime": "confirm_only"},
    }
    history = {"recent_actions": [{"action": final_action, "stop_code": stop_code}]}
    actions = {"prepare_seed": [], "prepare_library": [], "mine_library": [], "confirm_hit": [], "stop": {"action": "stop"}}
    _write_json(results / "summary.json", summary)
    _write_json(results / "agent_final_result.json", {"status": "completed", "stop_reason": summary["stop_reason"], "action_count": 1})
    _write_json(results / "codex_final_result.json", {"status": "completed", "stop_reason": summary["stop_reason"], "action_count": 1})
    _write_json(workspace / "state.json", state)
    _write_json(workspace / "history.json", history)
    _write_json(workspace / "step_001" / "available_actions.json", actions)
    _write_stream(
        results / "codex_events.jsonl",
        [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(
                        {"status": "completed", "stop_reason": summary["stop_reason"], "action_count": 1}
                    ),
                },
            },
            {"type": "turn.completed"},
        ],
    )
    return run_dir


def test_network_error_with_completed_turn_is_reported_for_review(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    path = run_dir / "results" / "codex_events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows.insert(2, {"type": "error", "message": "Reconnecting: stream disconnected before completion"})
    _write_stream(path, rows)

    report = audit_run(run_dir)

    assert report["session"]["session_complete"] is True
    assert report["session"]["network_status"] == "in_stream_reconnect_completed"
    assert report["analysis_eligibility"] == "eligible_with_network_review"


def test_incomplete_network_stream_is_excluded(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    path = run_dir / "results" / "codex_events.jsonl"
    _write_stream(
        path,
        [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "error", "message": "stream disconnected before completion: network error"},
        ],
    )
    (run_dir / "results" / "agent_final_result.json").unlink()
    (run_dir / "results" / "codex_final_result.json").unlink()

    report = audit_run(run_dir)

    assert report["session"]["session_complete"] is False
    assert report["session"]["network_status"] == "network_event_needs_review"
    assert report["analysis_eligibility"] == "exclude_technical_or_invalid_terminal"


def test_raw_event_stream_without_summary_is_still_audited(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    (run_dir / "results" / "summary.json").unlink()

    report = audit_run(run_dir)
    experiment = audit_experiment(tmp_path)

    assert report["session"]["session_complete"] is True
    assert report["game_stop"]["game_rule_valid"] is False
    assert report["analysis_eligibility"] == "exclude_technical_or_invalid_terminal"
    assert experiment["run_count"] == 1
    assert experiment["runs"][0]["run_dir"] == str(run_dir)


def test_max_days_is_verified_from_game_state(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, stop_code="max_experiment_days", final_action="confirm_hit")

    report = audit_run(run_dir)

    assert report["game_stop"]["trigger"] == "max_experiment_days"
    assert report["game_stop"]["game_rule_valid"] is True
    assert report["game_stop"]["day_evidence"]["reached_max_experiment_days"] is True

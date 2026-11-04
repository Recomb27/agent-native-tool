from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from TadACampaign import backends
from TadACampaign.codex_resume import CodexEventSnapshot, classify_failure


def test_network_interruption_is_retryable_and_keeps_the_codex_thread() -> None:
    snapshot = CodexEventSnapshot(
        payloads=(),
        observed_thread_id="thread-123",
        effective_thread_id="thread-123",
        saw_turn_completed=False,
        error_messages=("connection reset by peer",),
        truncated_tail=False,
    )

    failure = classify_failure(
        snapshot=snapshot,
        process_error="stream disconnected before completion",
        stdout="",
        stderr="",
        validation_error=None,
    )

    assert failure.classification == "retryable_interruption"
    assert failure.retryable is True
    assert failure.continuation_mode == "resume"
    assert snapshot.effective_thread_id == "thread-123"


def test_codex_resume_command_targets_the_same_contextswarm_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "agent_workspace"
    workspace.mkdir()
    config = SimpleNamespace(
        agent_model="gpt-6.1-sol",
        agent_profile=None,
        agent_reasoning_effort="max",
        agent_verbosity=None,
    )
    invocation = backends._AttemptInvocation(
        backend="codex",
        mode="resume",
        prompt="continue",
        prompt_path=None,
        resume_session_id="thread-123",
        target_session_id="thread-123",
        source_session_id="thread-123",
        trigger="retryable_interruption",
    )
    monkeypatch.setenv("TADACAMPAIGN_CODEX_EXTERNAL_SANDBOX", "0")

    command = backends._build_codex_command(
        config,
        run_dir=tmp_path,
        agent_workspace=workspace,
        invocation=invocation,
    )

    assert command[-3:] == ["resume", "thread-123", "-"]


def test_zero_remaining_budget_stops_before_starting_a_new_agent_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SimpleNamespace(
        agent_backend="codex",
        agent_timeout_seconds=0,
    )
    run_dir = tmp_path / "run"
    workspace = run_dir / "process" / "agent_workspace"
    # This budget test must not depend on a host Codex installation or config.
    monkeypatch.setattr(backends, "_stage_isolated_codex_home", lambda run_dir: None)
    monkeypatch.setattr(
        backends,
        "_initial_invocation",
        lambda config, *, run_dir: backends._AttemptInvocation(
            backend="codex",
            mode="initial",
            prompt="unused",
            prompt_path=None,
            resume_session_id=None,
            target_session_id=None,
            source_session_id=None,
            trigger="run_start",
        ),
    )

    with pytest.raises(backends.BackendError, match="cumulative agent timeout budget exhausted"):
        backends.run_backend_episode(config, run_dir=run_dir, agent_workspace=workspace)

    budget = (run_dir / "results" / "agent_budget.json").read_text(encoding="utf-8")
    assert '"exhausted": true' in budget
    assert '"attempts_total": 0' in budget
    events = (run_dir / "results" / "runtime_timeline.jsonl").read_text(encoding="utf-8")
    assert '"event": "agent.timeout.budget_exhausted"' in events

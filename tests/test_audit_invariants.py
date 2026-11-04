from __future__ import annotations

from datetime import UTC, datetime

from TadACampaign import environment, runner


def test_run_directory_uses_utc_and_handles_same_second_collision(tmp_path, monkeypatch) -> None:
    seen_timezone = []

    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            seen_timezone.append(tz)
            return datetime(2026, 10, 29, 12, 34, 56, tzinfo=tz)

    monkeypatch.setattr(runner.dt, "datetime", FixedDateTime)

    first = runner._make_run_dir(tmp_path, "episode")
    second = runner._make_run_dir(tmp_path, "episode")

    assert seen_timezone == [UTC, UTC]
    assert first.name == "20261029-123456-episode"
    assert second.name == "20261029-123456-01-episode"


def test_screen_record_lookup_uses_persisted_id_when_history_is_sparse() -> None:
    env = object.__new__(environment.TadACampaignEnvironment)
    env.screen_record_history = [
        {"screen_record_id": 11, "sequence": "AAA"},
        {"screen_record_id": 4, "sequence": "CCC"},
    ]

    assert env._screen_record_by_id(4)["sequence"] == "CCC"
    assert env._screen_record_by_id(1) is None

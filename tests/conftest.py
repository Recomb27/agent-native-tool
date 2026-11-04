from __future__ import annotations

from pathlib import Path

import pytest

from TadACampaign.models import ExperimentConfig, LibraryDefinition, MineBatchOption, RewardTier, SeedDefinition


def make_config(
    tmp_path: Path,
    *,
    variant: str = "dna",
    initial_cash: float = 1000.0,
    max_experiment_days: float = 30.0,
    skill_market_manifest: Path | None = None,
    skill_market_enabled: bool = False,
) -> ExperimentConfig:
    return ExperimentConfig(
        name="test",
        prepared_root=tmp_path / "prepared",
        variant=variant,
        output_root=tmp_path / "runs",
        seed=7,
        initial_cash=initial_cash,
        base_asset_value=1000.0,
        baseline_activity=1.0,
        prepare_seed_cost=1.0,
        prepare_seed_days=1.0,
        prepare_library_cost=1.0,
        daily_burn_cost=1.0,
        prepare_library_days=1.0,
        confirm_hit_cost=1.0,
        confirm_hit_days=1.0,
        purchase_skill_days=1.0,
        mine_batch_menu=(MineBatchOption(batch_size=1, cost=1.0, time_days=1.0),),
        initial_visible_seeds=("seed1",),
        initial_prepared_seeds=(),
        max_experiment_days=max_experiment_days,
        agent_timeout_seconds=10,
        reward_tiers=(RewardTier(min_activity=1.0, max_activity=float("inf"), multiplier=1.0),),
        linear_issue=None,
        agent_backend="policy",
        agent_model=None,
        agent_profile=None,
        agent_reasoning_effort=None,
        agent_verbosity=None,
        agent_codex_context_window_handoff_max_count=-1,
        skill_market_manifest=skill_market_manifest,
        skill_market_enabled=skill_market_enabled,
        initial_owned_skill_ids=(),
        judge_server_url="http://127.0.0.1:1",
        judge_request_timeout_seconds=1,
        screening_noise_enabled=False,
        screening_noise_sigma=0.0,
        screening_noise_clip=0.2,
        agent_policy="jev_k1",
        agent_policy_params={},
        run_mode="jev",
    )


def make_seed_definitions(*, variant: str = "dna") -> list[SeedDefinition]:
    if variant == "dna":
        start = "ATGAAACCGTTT"
        library_sequence = "GTGAAACCGTTT"
    else:
        start = "MAPF"
        library_sequence = "VAPF"
    library = LibraryDefinition(
        library_id="seed1_library1",
        seed_id="seed1",
        historical_round_id="round1",
        raw_domain_id=1,
        start_sequence=start,
        interval_start_aa=0,
        interval_end_aa=1,
    )
    return [
        SeedDefinition(
            seed_id="seed1",
            start_sequence=start,
            parent_seed_candidates=(),
            parent_seed_id=None,
            parent_library_candidates=(),
            parent_library_id=None,
            historical_round_ids=("round1",),
            libraries=(library,),
        )
    ]


def make_prepared_rows(*, variant: str = "dna") -> list[dict[str, object]]:
    if variant == "dna":
        sequence = "GTGAAACCGTTT"
    else:
        sequence = "VAPF"
    return [{"sequence": sequence, "activity": 2.0, "round_ids": ["round1"]}]


@pytest.fixture
def base_config(tmp_path: Path) -> ExperimentConfig:
    return make_config(tmp_path)

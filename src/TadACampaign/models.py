"""Shared typed models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RoundDefinition:
    """Canonical historical round definition."""

    round_id: str
    raw_domain_id: int
    start_sequence: str
    interval_start_aa: int
    interval_end_aa: int
    parent_candidates: tuple[str, ...]
    parent_round_id: str | None = None
    parent_resolution_evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def interval_length_aa(self) -> int:
        return self.interval_end_aa - self.interval_start_aa


@dataclass(frozen=True)
class LibraryDefinition:
    library_id: str
    seed_id: str
    historical_round_id: str
    raw_domain_id: int
    start_sequence: str
    interval_start_aa: int
    interval_end_aa: int
    unlock_child_seed_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def interval_length_aa(self) -> int:
        return self.interval_end_aa - self.interval_start_aa

    @property
    def key(self) -> str:
        return f"{self.seed_id}:{self.library_id}"


@dataclass(frozen=True)
class SeedDefinition:
    seed_id: str
    start_sequence: str
    parent_seed_candidates: tuple[str, ...] = field(default_factory=tuple)
    parent_seed_id: str | None = None
    parent_library_candidates: tuple[str, ...] = field(default_factory=tuple)
    parent_library_id: str | None = None
    parent_resolution_evidence: dict[str, Any] = field(default_factory=dict)
    historical_round_ids: tuple[str, ...] = field(default_factory=tuple)
    libraries: tuple[LibraryDefinition, ...] = field(default_factory=tuple)

    @property
    def library_count(self) -> int:
        return len(self.libraries)


@dataclass(frozen=True)
class RewardTier:
    min_activity: float
    max_activity: float
    multiplier: float


@dataclass(frozen=True)
class MineBatchOption:
    batch_size: int
    cost: float
    time_days: float


@dataclass(frozen=True)
class ExperimentConfig:
    name: str
    prepared_root: Path
    variant: str
    output_root: Path
    seed: int
    initial_cash: float
    base_asset_value: float
    baseline_activity: float
    prepare_seed_cost: float
    prepare_seed_days: float
    prepare_library_cost: float
    daily_burn_cost: float
    prepare_library_days: float
    confirm_hit_cost: float
    confirm_hit_days: float
    purchase_skill_days: float
    mine_batch_menu: tuple[MineBatchOption, ...]
    initial_visible_seeds: tuple[str, ...]
    initial_prepared_seeds: tuple[str, ...]
    max_experiment_days: float
    agent_timeout_seconds: int
    reward_tiers: tuple[RewardTier, ...]
    linear_issue: str | None
    agent_backend: str
    agent_model: str | None
    agent_profile: str | None
    agent_reasoning_effort: str | None
    agent_verbosity: str | None
    agent_codex_context_window_handoff_max_count: int
    skill_market_manifest: Path | None
    skill_market_enabled: bool
    initial_owned_skill_ids: tuple[str, ...]
    judge_server_url: str | None
    judge_request_timeout_seconds: int
    screening_noise_enabled: bool = True
    screening_noise_sigma: float = 0.10
    screening_noise_clip: float = 0.20
    agent_policy: str | None = None
    agent_policy_params: dict[str, Any] = field(default_factory=dict)
    run_mode: str = "agent"

    @property
    def skill_surface_enabled(self) -> bool:
        return self.skill_market_manifest is not None


@dataclass(frozen=True)
class AgentDecision:
    action: str
    seed_id: str | None = None
    library_id: str | None = None
    batch_size: int | None = None
    screen_record_id: int | None = None
    skill_id: str | None = None
    reasoning: str = ""

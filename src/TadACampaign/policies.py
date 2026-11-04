"""Semi-blind algorithmic policy baselines.

These policies intentionally operate only on the public observation surface
(`state`, `available_actions`, and public history/action responses). They
treat seed and library identifiers as opaque handles rather than encoded
campaign-stage information.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import AgentDecision, ExperimentConfig

POLICY_NAMES = frozenset(
    {
        "stop_now",
        "blind_random_legal",
        "budgeted_static_ranker",
        "scout_confirm_stop",
        "upside_greedy_explorer",
        "empirical_ev_planner",
        "jev_choice",
        "jev_k1",
        "d1_k1",
        "laya_k1",
    }
)


@dataclass
class LibraryStats:
    screened_count: int = 0
    mine_count: int = 0
    pending_hit_count: int = 0
    best_observed_activity: float | None = None
    best_pending_observed_activity: float | None = None
    confirmed_count: int = 0
    confirmed_improvement_count: int = 0
    best_confirmed_activity: float | None = None


@dataclass
class PolicyMemory:
    prepared_library_ids: set[str] = field(default_factory=set)
    probed_library_ids: set[str] = field(default_factory=set)
    discovered_edges: list[dict[str, Any]] = field(default_factory=list)
    library_stats: dict[str, LibraryStats] = field(default_factory=dict)
    accepted_action_count: int = 0
    jev_runtime: Any | None = None

    def stats_for(self, library_id: str) -> LibraryStats:
        stats = self.library_stats.get(library_id)
        if stats is None:
            stats = LibraryStats()
            self.library_stats[library_id] = stats
        return stats


def make_policy_memory() -> PolicyMemory:
    return PolicyMemory()


def policy_rng_seed(config: ExperimentConfig, policy_name: str) -> int:
    digest = hashlib.sha256(f"{config.seed}:{policy_name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def _policy_param(config: ExperimentConfig, key: str, default: Any) -> Any:
    return config.agent_policy_params.get(key, default)


def _ordered_random(items: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    shuffled = list(items)
    rng.shuffle(shuffled)
    return shuffled


def _confirm_total_cost(config: ExperimentConfig) -> float:
    return float(config.confirm_hit_cost) + float(config.confirm_hit_days) * float(config.daily_burn_cost)


def _prepare_seed_total_cost(config: ExperimentConfig) -> float:
    return float(config.prepare_seed_cost) + float(config.prepare_seed_days) * float(config.daily_burn_cost)


def _prepare_library_total_cost(config: ExperimentConfig) -> float:
    return float(config.prepare_library_cost) + float(config.prepare_library_days) * float(config.daily_burn_cost)


def _mine_total_cost(config: ExperimentConfig, option: Any) -> float:
    return float(option.cost) + float(option.time_days) * float(config.daily_burn_cost)


def _minimum_mine_total_cost(config: ExperimentConfig) -> float:
    if not config.mine_batch_menu:
        return math.inf
    return min(_mine_total_cost(config, option) for option in config.mine_batch_menu)


def _minimum_mine_days(config: ExperimentConfig) -> float:
    if not config.mine_batch_menu:
        return math.inf
    return min(float(option.time_days) for option in config.mine_batch_menu)


def _payload_total_cost(payload: dict[str, Any]) -> float:
    return float(payload.get("total_cost", 0.0) or 0.0)


def _payload_time_days(payload: dict[str, Any]) -> float:
    return float(payload.get("time_days", 0.0) or 0.0)


def _state_cash(config: ExperimentConfig, observation: dict[str, Any]) -> float:
    state = observation.get("state", {})
    return float(state.get("current_cash", config.initial_cash) or 0.0)


def _remaining_days(config: ExperimentConfig, observation: dict[str, Any]) -> float:
    state = observation.get("state", {})
    return float(state.get("remaining_days", config.max_experiment_days) or 0.0)


def _cash_spent(config: ExperimentConfig, observation: dict[str, Any]) -> float:
    state = observation.get("state", {})
    total_cost_paid = state.get("total_cost_paid")
    if total_cost_paid is not None:
        return max(float(total_cost_paid), 0.0)
    total_reward = float(state.get("total_reward_earned", 0.0) or 0.0)
    return max(float(config.initial_cash) + total_reward - _state_cash(config, observation), 0.0)


def _policy_spend_cap(config: ExperimentConfig, key: str, default_fraction: float) -> float:
    default = float(config.initial_cash) * default_fraction
    return float(_policy_param(config, key, default))


def _within_spend_cap(
    config: ExperimentConfig,
    observation: dict[str, Any],
    action_total_cost: float,
    *,
    spend_cap: float,
) -> bool:
    return _cash_spent(config, observation) + action_total_cost <= spend_cap + 1e-9


def _can_cover_future(
    config: ExperimentConfig,
    observation: dict[str, Any],
    *,
    action_total_cost: float,
    action_time_days: float,
    future_total_cost: float,
    future_time_days: float,
) -> bool:
    cash_after = _state_cash(config, observation) - action_total_cost
    days_after = _remaining_days(config, observation) - action_time_days
    return cash_after + 1e-9 >= future_total_cost and days_after + 1e-9 >= future_time_days


def _can_take_prepare_seed(
    config: ExperimentConfig,
    observation: dict[str, Any],
    action: dict[str, Any],
    *,
    spend_cap: float,
) -> bool:
    action_total_cost = _payload_total_cost(action)
    future_exploration_cost = _prepare_library_total_cost(config) + _minimum_mine_total_cost(config)
    future_total_cost = _prepare_library_total_cost(config) + _minimum_mine_total_cost(config) + _confirm_total_cost(config)
    future_time_days = float(config.prepare_library_days) + _minimum_mine_days(config) + float(config.confirm_hit_days)
    return (
        _within_spend_cap(
            config,
            observation,
            action_total_cost + future_exploration_cost,
            spend_cap=spend_cap,
        )
        and _can_cover_future(
            config,
            observation,
            action_total_cost=action_total_cost,
            action_time_days=_payload_time_days(action),
            future_total_cost=future_total_cost,
            future_time_days=future_time_days,
        )
    )


def _can_take_prepare_library(
    config: ExperimentConfig,
    observation: dict[str, Any],
    action: dict[str, Any],
    *,
    spend_cap: float,
) -> bool:
    action_total_cost = _payload_total_cost(action)
    future_exploration_cost = _minimum_mine_total_cost(config)
    future_total_cost = _minimum_mine_total_cost(config) + _confirm_total_cost(config)
    future_time_days = _minimum_mine_days(config) + float(config.confirm_hit_days)
    return (
        _within_spend_cap(
            config,
            observation,
            action_total_cost + future_exploration_cost,
            spend_cap=spend_cap,
        )
        and _can_cover_future(
            config,
            observation,
            action_total_cost=action_total_cost,
            action_time_days=_payload_time_days(action),
            future_total_cost=future_total_cost,
            future_time_days=future_time_days,
        )
    )


def _can_take_mine_batch(
    config: ExperimentConfig,
    observation: dict[str, Any],
    batch: dict[str, Any],
    *,
    spend_cap: float,
) -> bool:
    action_total_cost = _payload_total_cost(batch)
    if not _within_spend_cap(config, observation, action_total_cost, spend_cap=spend_cap):
        return False
    if batch.get("leaves_confirm_capacity") is False:
        return False
    return _can_cover_future(
        config,
        observation,
        action_total_cost=action_total_cost,
        action_time_days=_payload_time_days(batch),
        future_total_cost=_confirm_total_cost(config),
        future_time_days=float(config.confirm_hit_days),
    )


def _state_prepared_library_ids(observation: dict[str, Any]) -> set[str]:
    prepared: set[str] = set()
    state = observation.get("state", {})
    for seed in state.get("seeds", []):
        if not isinstance(seed, dict):
            continue
        for library in seed.get("libraries", []):
            if not isinstance(library, dict):
                continue
            if library.get("library_status") == "prepared" and isinstance(library.get("library_id"), str):
                prepared.add(str(library["library_id"]))
    return prepared


def _reward_for_improvement(config: ExperimentConfig, previous_best: float, new_best: float) -> float:
    if new_best <= previous_best:
        return 0.0
    reward = 0.0
    for tier in config.reward_tiers:
        lower = max(previous_best, tier.min_activity)
        upper = min(new_best, tier.max_activity)
        if upper <= lower:
            continue
        reward += (
            config.base_asset_value
            * config.baseline_activity
            * tier.multiplier
            * ((1.0 / lower) - (1.0 / upper))
        )
    return reward


def _estimated_confirmed_activity(config: ExperimentConfig, observed_activity: float) -> float:
    if not config.screening_noise_enabled:
        return observed_activity
    return observed_activity / (1.0 + max(float(config.screening_noise_sigma), 0.0))


def _confirm_ev(config: ExperimentConfig, state: dict[str, Any], confirm_action: dict[str, Any]) -> float:
    current_best = float(state.get("confirmed_best_activity", config.baseline_activity))
    observed = float(confirm_action.get("observed_activity", current_best))
    estimated = _estimated_confirmed_activity(config, observed)
    return _reward_for_improvement(config, current_best, estimated) - _confirm_total_cost(config)


def _best_positive_ev_confirm(
    config: ExperimentConfig,
    observation: dict[str, Any],
    rng: random.Random,
    *,
    margin: float = 1.0,
) -> dict[str, Any] | None:
    actions = observation.get("available_actions", {})
    confirms = [item for item in actions.get("confirm_hit", []) if isinstance(item, dict)]
    if not confirms:
        return None
    threshold = _confirm_total_cost(config) * (margin - 1.0)
    scored = [(_confirm_ev(config, observation.get("state", {}), item), item) for item in _ordered_random(confirms, rng)]
    ev, item = max(scored, key=lambda pair: pair[0])
    return item if ev > threshold else None


def _highest_upside_confirm(
    config: ExperimentConfig,
    observation: dict[str, Any],
    rng: random.Random,
) -> dict[str, Any] | None:
    actions = observation.get("available_actions", {})
    confirms = [item for item in actions.get("confirm_hit", []) if isinstance(item, dict)]
    if not confirms:
        return None
    current_best = _current_best_activity(config, observation)
    min_gain = float(_policy_param(config, "min_confirmed_activity_gain", 1.0))
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for item in _ordered_random(confirms, rng):
        observed = float(item.get("observed_activity", current_best))
        estimated = _estimated_confirmed_activity(config, observed)
        if estimated > current_best * min_gain:
            candidates.append((_confirm_ev(config, observation.get("state", {}), item), observed, item))
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row[1], row[0]))[2]


def _highest_observed_confirm(observation: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    actions = observation.get("available_actions", {})
    confirms = [item for item in actions.get("confirm_hit", []) if isinstance(item, dict)]
    if not confirms:
        return None
    return max(_ordered_random(confirms, rng), key=lambda item: float(item.get("observed_activity", float("-inf"))))


def _safe_batch_options(mine_action: dict[str, Any]) -> list[dict[str, Any]]:
    options = [item for item in mine_action.get("batch_options", []) if isinstance(item, dict)]
    safe = [item for item in options if item.get("leaves_confirm_capacity") is True]
    return safe or options


def _smallest_safe_batch(mine_action: dict[str, Any]) -> dict[str, Any] | None:
    options = _safe_batch_options(mine_action)
    if not options:
        return None
    return min(options, key=lambda item: (float(item.get("total_cost", 0.0)), int(item.get("batch_size", 0))))


def _largest_safe_batch(mine_action: dict[str, Any]) -> dict[str, Any] | None:
    options = _safe_batch_options(mine_action)
    if not options:
        return None
    return max(options, key=lambda item: (int(item.get("batch_size", 0)), -float(item.get("total_cost", 0.0))))


def _budgeted_batch_options(
    config: ExperimentConfig,
    observation: dict[str, Any],
    mine_action: dict[str, Any],
    *,
    spend_cap: float,
) -> list[dict[str, Any]]:
    return [
        item
        for item in mine_action.get("batch_options", [])
        if isinstance(item, dict) and _can_take_mine_batch(config, observation, item, spend_cap=spend_cap)
    ]


def _smallest_budgeted_batch(
    config: ExperimentConfig,
    observation: dict[str, Any],
    mine_action: dict[str, Any],
    *,
    spend_cap: float,
) -> dict[str, Any] | None:
    options = _budgeted_batch_options(config, observation, mine_action, spend_cap=spend_cap)
    if not options:
        return None
    return min(options, key=lambda item: (_payload_total_cost(item), int(item.get("batch_size", 0))))


def _largest_budgeted_batch(
    config: ExperimentConfig,
    observation: dict[str, Any],
    mine_action: dict[str, Any],
    *,
    spend_cap: float,
) -> dict[str, Any] | None:
    options = _budgeted_batch_options(config, observation, mine_action, spend_cap=spend_cap)
    if not options:
        return None
    return max(options, key=lambda item: (int(item.get("batch_size", 0)), -_payload_total_cost(item)))


def _stop(reasoning: str) -> AgentDecision:
    return AgentDecision(action="stop", reasoning=reasoning)


def _decision_for_prepare_seed(action: dict[str, Any], reasoning: str) -> AgentDecision:
    return AgentDecision(action="prepare_seed", seed_id=str(action["seed_id"]), reasoning=reasoning)


def _decision_for_prepare_library(action: dict[str, Any], reasoning: str) -> AgentDecision:
    return AgentDecision(
        action="prepare_library",
        seed_id=str(action["seed_id"]),
        library_id=str(action["library_id"]),
        reasoning=reasoning,
    )


def _decision_for_mine(action: dict[str, Any], batch: dict[str, Any], reasoning: str) -> AgentDecision:
    return AgentDecision(
        action="mine_library",
        seed_id=str(action["seed_id"]),
        library_id=str(action["library_id"]),
        batch_size=int(batch["batch_size"]),
        reasoning=reasoning,
    )


def _decision_for_confirm(action: dict[str, Any], reasoning: str) -> AgentDecision:
    return AgentDecision(action="confirm_hit", screen_record_id=int(action["screen_record_id"]), reasoning=reasoning)


def _visible_seed_library_counts(observation: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    state = observation.get("state", {})
    for seed in state.get("seeds", []):
        if not isinstance(seed, dict):
            continue
        seed_id = seed.get("seed_id")
        libraries = seed.get("libraries", [])
        if isinstance(seed_id, str) and isinstance(libraries, list):
            counts[seed_id] = len(libraries)
    return counts


def _prepare_seed_score(action: dict[str, Any], observation: dict[str, Any]) -> float:
    seed_id = str(action.get("seed_id", ""))
    library_count = float(_visible_seed_library_counts(observation).get(seed_id, 0))
    total_cost = float(action.get("total_cost", 0.0) or 0.0)
    return math.log1p(library_count) - total_cost / 100000.0


def _prepare_library_score(action: dict[str, Any]) -> float:
    interval_length = float(action.get("interval_length_aa", 0.0) or 0.0)
    total_cost = float(action.get("total_cost", 0.0) or 0.0)
    return math.log1p(max(interval_length, 0.0)) - total_cost / 100000.0


def _mine_score(action: dict[str, Any], memory: PolicyMemory) -> float:
    library_id = str(action.get("library_id", ""))
    remaining = float(action.get("remaining_screenable_sequence_count", 0.0) or 0.0)
    screened = float(action.get("screened_sequence_count", 0.0) or 0.0)
    screenable = max(float(action.get("screenable_sequence_count", 0.0) or 0.0), 1.0)
    stats = memory.stats_for(library_id)
    observed_bonus = 0.0
    if stats.best_observed_activity is not None:
        observed_bonus += math.log1p(max(stats.best_observed_activity, 0.0))
    if stats.best_pending_observed_activity is not None:
        observed_bonus += math.log1p(max(stats.best_pending_observed_activity, 0.0))
    screened_penalty = screened / screenable
    probe_bonus = 0.5 if library_id not in memory.probed_library_ids else 0.0
    return math.log1p(max(remaining, 0.0)) + observed_bonus + probe_bonus - screened_penalty


def _best_by_score(
    items: list[dict[str, Any]],
    rng: random.Random,
    score_fn: Callable[[dict[str, Any]], float],
) -> dict[str, Any] | None:
    if not items:
        return None
    return max(_ordered_random(items, rng), key=score_fn)


def _select_stop_now(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
) -> AgentDecision:
    return AgentDecision(action="stop", reasoning="stop_now: preserve current cash")


def _select_blind_random_legal(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
) -> AgentDecision:
    actions = observation.get("available_actions", {})
    candidates: list[tuple[str, dict[str, Any]]] = []
    for action_name in ("confirm_hit", "prepare_seed", "prepare_library", "mine_library"):
        for item in actions.get(action_name, []):
            if isinstance(item, dict):
                candidates.append((action_name, item))
    if not candidates:
        return AgentDecision(action="stop", reasoning="blind_random_legal: no non-stop legal action")

    action_name, item = rng.choice(candidates)
    if action_name == "confirm_hit":
        return _decision_for_confirm(item, "blind_random_legal: randomly selected legal confirmation")
    if action_name == "prepare_seed":
        return _decision_for_prepare_seed(item, "blind_random_legal: randomly selected visible seed preparation")
    if action_name == "prepare_library":
        return _decision_for_prepare_library(item, "blind_random_legal: randomly selected visible library preparation")
    batch_options = _safe_batch_options(item)
    if not batch_options:
        return AgentDecision(action="stop", reasoning="blind_random_legal: selected mine action had no legal batch")
    batch = rng.choice(batch_options)
    return _decision_for_mine(item, batch, "blind_random_legal: randomly selected legal mining action")


def _ranked_budgeted_prepare_seed(
    config: ExperimentConfig,
    observation: dict[str, Any],
    rng: random.Random,
    *,
    spend_cap: float,
) -> dict[str, Any] | None:
    actions = observation.get("available_actions", {})
    candidates = [
        item
        for item in actions.get("prepare_seed", [])
        if isinstance(item, dict) and _can_take_prepare_seed(config, observation, item, spend_cap=spend_cap)
    ]
    return _best_by_score(candidates, rng, lambda item: _prepare_seed_score(item, observation))


def _ranked_budgeted_prepare_library(
    config: ExperimentConfig,
    observation: dict[str, Any],
    rng: random.Random,
    *,
    spend_cap: float,
) -> dict[str, Any] | None:
    actions = observation.get("available_actions", {})
    candidates = [
        item
        for item in actions.get("prepare_library", [])
        if isinstance(item, dict) and _can_take_prepare_library(config, observation, item, spend_cap=spend_cap)
    ]
    return _best_by_score(candidates, rng, _prepare_library_score)


def _ranked_budgeted_mine(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
    *,
    spend_cap: float,
    max_batches_per_library: int,
) -> dict[str, Any] | None:
    actions = observation.get("available_actions", {})
    candidates: list[dict[str, Any]] = []
    for item in actions.get("mine_library", []):
        if not isinstance(item, dict):
            continue
        library_id = str(item.get("library_id", ""))
        if memory.stats_for(library_id).mine_count >= max_batches_per_library:
            continue
        if _budgeted_batch_options(config, observation, item, spend_cap=spend_cap):
            candidates.append(item)
    return _best_by_score(candidates, rng, lambda item: _mine_score(item, memory))


def _select_budgeted_static_ranker(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
) -> AgentDecision:
    confirm = _best_positive_ev_confirm(config, observation, rng, margin=float(_policy_param(config, "confirm_margin", 1.0)))
    if confirm is not None:
        return _decision_for_confirm(confirm, "budgeted_static_ranker: confirm public observed hit with positive estimated value")

    spend_cap = _policy_spend_cap(config, "max_exploration_spend", 0.45)
    max_batches = max(int(_policy_param(config, "max_batches_per_library", 2)), 1)

    best_mine = _ranked_budgeted_mine(
        config,
        observation,
        memory,
        rng,
        spend_cap=spend_cap,
        max_batches_per_library=max_batches,
    )
    if best_mine is not None:
        batch = _smallest_budgeted_batch(config, observation, best_mine, spend_cap=spend_cap)
        if batch is not None:
            return _decision_for_mine(
                best_mine,
                batch,
                "budgeted_static_ranker: mine highest public-feature library while preserving confirm reserve",
            )

    best_prepare_library = _ranked_budgeted_prepare_library(config, observation, rng, spend_cap=spend_cap)
    if best_prepare_library is not None:
        return _decision_for_prepare_library(
            best_prepare_library,
            "budgeted_static_ranker: prepare highest public-feature visible library within exploration budget",
        )

    best_prepare_seed = _ranked_budgeted_prepare_seed(config, observation, rng, spend_cap=spend_cap)
    if best_prepare_seed is not None:
        return _decision_for_prepare_seed(
            best_prepare_seed,
            "budgeted_static_ranker: prepare visible seed only if a mine-and-confirm path remains affordable",
        )

    return _stop("budgeted_static_ranker: stop because no ranked non-confirm action preserves reserve and spend cap")


def _scout_library_count(observation: dict[str, Any], memory: PolicyMemory) -> int:
    ids = set(memory.prepared_library_ids)
    ids.update(_state_prepared_library_ids(observation))
    ids.update(library_id for library_id, stats in memory.library_stats.items() if stats.mine_count > 0)
    return len(ids)


def _unprobed_budgeted_mine_action(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
    *,
    spend_cap: float,
    probe_batches: int,
) -> dict[str, Any] | None:
    actions = observation.get("available_actions", {})
    candidates: list[dict[str, Any]] = []
    for item in actions.get("mine_library", []):
        if not isinstance(item, dict):
            continue
        library_id = str(item.get("library_id", ""))
        if memory.stats_for(library_id).mine_count >= probe_batches:
            continue
        if _budgeted_batch_options(config, observation, item, spend_cap=spend_cap):
            candidates.append(item)
    return _best_by_score(candidates, rng, lambda item: _mine_score(item, memory))


def _select_scout_confirm_stop(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
) -> AgentDecision:
    confirm = _best_positive_ev_confirm(config, observation, rng, margin=float(_policy_param(config, "confirm_margin", 1.0)))
    if confirm is not None:
        return _decision_for_confirm(confirm, "scout_confirm_stop: confirm public hit with positive estimated value")

    spend_cap = _policy_spend_cap(config, "max_exploration_spend", 0.42)
    max_scout_libraries = max(int(_policy_param(config, "max_scout_libraries", 4)), 1)
    probe_batches = max(int(_policy_param(config, "probe_batches_per_library", 1)), 1)

    probe_candidate = _unprobed_budgeted_mine_action(
        config,
        observation,
        memory,
        rng,
        spend_cap=spend_cap,
        probe_batches=probe_batches,
    )
    if probe_candidate is not None:
        batch = _smallest_budgeted_batch(config, observation, probe_candidate, spend_cap=spend_cap)
        if batch is not None:
            return _decision_for_mine(
                probe_candidate,
                batch,
                "scout_confirm_stop: take one small scout batch in a prepared visible library",
            )

    if _scout_library_count(observation, memory) < max_scout_libraries:
        best_prepare_library = _ranked_budgeted_prepare_library(config, observation, rng, spend_cap=spend_cap)
        if best_prepare_library is not None:
            return _decision_for_prepare_library(
                best_prepare_library,
                "scout_confirm_stop: prepare next visible library for bounded scout probing",
            )

        best_prepare_seed = _ranked_budgeted_prepare_seed(config, observation, rng, spend_cap=spend_cap)
        if best_prepare_seed is not None:
            return _decision_for_prepare_seed(
                best_prepare_seed,
                "scout_confirm_stop: prepare visible seed only to enable bounded scout libraries",
            )

    return _stop("scout_confirm_stop: stop after bounded scouts because no positive-EV confirmation is available")


def _select_upside_greedy_explorer(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
) -> AgentDecision:
    confirm = _highest_upside_confirm(config, observation, rng)
    if confirm is not None:
        return _decision_for_confirm(confirm, "upside_greedy_explorer: confirm highest public observed upside hit")

    spend_cap = _policy_spend_cap(config, "max_exploration_spend", 0.85)
    max_batches = max(int(_policy_param(config, "max_batches_per_library", 5)), 1)

    best_mine = _ranked_budgeted_mine(
        config,
        observation,
        memory,
        rng,
        spend_cap=spend_cap,
        max_batches_per_library=max_batches,
    )
    if best_mine is not None:
        batch = _largest_budgeted_batch(config, observation, best_mine, spend_cap=spend_cap)
        if batch is not None:
            return _decision_for_mine(
                best_mine,
                batch,
                "upside_greedy_explorer: mine largest affordable batch while preserving confirm reserve",
            )

    best_prepare_library = _ranked_budgeted_prepare_library(config, observation, rng, spend_cap=spend_cap)
    if best_prepare_library is not None:
        return _decision_for_prepare_library(
            best_prepare_library,
            "upside_greedy_explorer: prepare highest public-feature library under aggressive exploration budget",
        )

    best_prepare_seed = _ranked_budgeted_prepare_seed(config, observation, rng, spend_cap=spend_cap)
    if best_prepare_seed is not None:
        return _decision_for_prepare_seed(
            best_prepare_seed,
            "upside_greedy_explorer: prepare visible seed under aggressive exploration budget",
        )

    return _stop("upside_greedy_explorer: stop because no upside-seeking action preserves confirm reserve")


def _current_best_activity(config: ExperimentConfig, observation: dict[str, Any]) -> float:
    return float(observation.get("state", {}).get("confirmed_best_activity", config.baseline_activity))


def _empirical_hit_rate(config: ExperimentConfig, stats: LibraryStats) -> float:
    alpha = float(_policy_param(config, "empirical_prior_hits", 0.30))
    beta = float(_policy_param(config, "empirical_prior_misses", 2200.0))
    hits = float(stats.pending_hit_count + stats.confirmed_improvement_count)
    return (hits + alpha) / (float(stats.screened_count) + alpha + beta)


def _empirical_target_activity(config: ExperimentConfig, observation: dict[str, Any], stats: LibraryStats) -> float:
    current_best = _current_best_activity(config, observation)
    prior_target = float(_policy_param(config, "prior_target_activity", 1.80))
    candidates = [current_best * 1.05, prior_target]
    if stats.best_observed_activity is not None:
        candidates.append(_estimated_confirmed_activity(config, stats.best_observed_activity))
    if stats.best_pending_observed_activity is not None:
        candidates.append(_estimated_confirmed_activity(config, stats.best_pending_observed_activity))
    return max(candidates)


def _empirical_mine_ev(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    mine_action: dict[str, Any],
    batch: dict[str, Any],
) -> float:
    stats = memory.stats_for(str(mine_action.get("library_id", "")))
    hit_rate = max(min(_empirical_hit_rate(config, stats), 0.95), 0.0)
    batch_size = max(int(batch.get("batch_size", 0) or 0), 0)
    p_batch_hit = 1.0 - ((1.0 - hit_rate) ** batch_size)
    current_best = _current_best_activity(config, observation)
    target_activity = _empirical_target_activity(config, observation, stats)
    gross_reward = _reward_for_improvement(config, current_best, target_activity)
    return p_batch_hit * gross_reward - _payload_total_cost(batch)


def _configured_min_batch_payload(config: ExperimentConfig) -> dict[str, Any] | None:
    if not config.mine_batch_menu:
        return None
    option = min(config.mine_batch_menu, key=lambda item: _mine_total_cost(config, item))
    return {
        "batch_size": int(option.batch_size),
        "cost": float(option.cost),
        "time_days": float(option.time_days),
        "burn_cost": float(option.time_days) * float(config.daily_burn_cost),
        "total_cost": _mine_total_cost(config, option),
    }


def _prior_mine_ev(config: ExperimentConfig, observation: dict[str, Any]) -> float:
    batch = _configured_min_batch_payload(config)
    if batch is None:
        return float("-inf")
    prior_stats = LibraryStats()
    hit_rate = max(min(_empirical_hit_rate(config, prior_stats), 0.95), 0.0)
    batch_size = max(int(batch.get("batch_size", 0) or 0), 0)
    p_batch_hit = 1.0 - ((1.0 - hit_rate) ** batch_size)
    current_best = _current_best_activity(config, observation)
    target_activity = max(current_best * 1.05, float(_policy_param(config, "prior_target_activity", 1.80)))
    return p_batch_hit * _reward_for_improvement(config, current_best, target_activity) - _payload_total_cost(batch)


def _empirical_best_mine(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
    *,
    spend_cap: float,
) -> tuple[float, dict[str, Any], dict[str, Any]] | None:
    actions = observation.get("available_actions", {})
    candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for item in actions.get("mine_library", []):
        if not isinstance(item, dict):
            continue
        for batch in _ordered_random(_budgeted_batch_options(config, observation, item, spend_cap=spend_cap), rng):
            candidates.append((_empirical_mine_ev(config, observation, memory, item, batch), item, batch))
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row[0], int(row[2].get("batch_size", 0))))


def _empirical_prepare_library_ev(config: ExperimentConfig, observation: dict[str, Any], action: dict[str, Any]) -> float:
    interval_bonus = 1.0 + min(max(float(action.get("interval_length_aa", 0.0) or 0.0), 0.0), 100.0) / 400.0
    return (_prior_mine_ev(config, observation) * interval_bonus) - _payload_total_cost(action)


def _seed_visible_library_count(observation: dict[str, Any], seed_id: str) -> int:
    state = observation.get("state", {})
    for seed in state.get("seeds", []):
        if not isinstance(seed, dict) or seed.get("seed_id") != seed_id:
            continue
        libraries = seed.get("libraries", [])
        return len(libraries) if isinstance(libraries, list) else 0
    return 0


def _empirical_prepare_seed_ev(config: ExperimentConfig, observation: dict[str, Any], action: dict[str, Any]) -> float:
    library_count = max(_seed_visible_library_count(observation, str(action.get("seed_id", ""))), 1)
    library_limit = max(int(_policy_param(config, "seed_library_value_cap", 2)), 1)
    expected_library_value = _prior_mine_ev(config, observation) - _prepare_library_total_cost(config)
    return min(library_count, library_limit) * expected_library_value - _payload_total_cost(action)


def _initial_probe_floor_decision(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
    *,
    spend_cap: float,
) -> AgentDecision:
    min_probe_libraries = max(int(_policy_param(config, "min_probe_libraries", 2)), 0)
    if len(memory.probed_library_ids) >= min_probe_libraries:
        return _stop("empirical_ev_planner: no positive expected-value action after required probe floor")

    probe_candidate = _unprobed_budgeted_mine_action(
        config,
        observation,
        memory,
        rng,
        spend_cap=spend_cap,
        probe_batches=1,
    )
    if probe_candidate is not None:
        batch = _smallest_budgeted_batch(config, observation, probe_candidate, spend_cap=spend_cap)
        if batch is not None:
            return _decision_for_mine(
                probe_candidate,
                batch,
                "empirical_ev_planner: take minimum probe to estimate empirical hit rate",
            )

    if _scout_library_count(observation, memory) < min_probe_libraries:
        best_prepare_library = _ranked_budgeted_prepare_library(config, observation, rng, spend_cap=spend_cap)
        if best_prepare_library is not None:
            return _decision_for_prepare_library(
                best_prepare_library,
                "empirical_ev_planner: prepare library needed for the minimum empirical probe floor",
            )
        best_prepare_seed = _ranked_budgeted_prepare_seed(config, observation, rng, spend_cap=spend_cap)
        if best_prepare_seed is not None:
            return _decision_for_prepare_seed(
                best_prepare_seed,
                "empirical_ev_planner: prepare seed needed for the minimum empirical probe floor",
            )

    return _stop("empirical_ev_planner: no reserve-preserving way to satisfy probe floor")


def _select_empirical_ev_planner(
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
) -> AgentDecision:
    confirm = _best_positive_ev_confirm(config, observation, rng, margin=float(_policy_param(config, "confirm_margin", 1.0)))
    if confirm is not None:
        return _decision_for_confirm(confirm, "empirical_ev_planner: confirm public hit with positive estimated value")

    spend_cap = _policy_spend_cap(config, "max_exploration_spend", 0.62)
    min_action_ev = float(_policy_param(config, "min_action_ev", 0.0))
    actions = observation.get("available_actions", {})

    mine_choice = _empirical_best_mine(config, observation, memory, rng, spend_cap=spend_cap)
    if mine_choice is not None:
        mine_ev, mine_action, batch = mine_choice
        if mine_ev > min_action_ev:
            return _decision_for_mine(
                mine_action,
                batch,
                f"empirical_ev_planner: mine batch with positive public empirical EV ({mine_ev:.2f})",
            )

    prepare_library_candidates = [
        item
        for item in actions.get("prepare_library", [])
        if isinstance(item, dict) and _can_take_prepare_library(config, observation, item, spend_cap=spend_cap)
    ]
    if prepare_library_candidates:
        best_prepare_ev, best_prepare = max(
            (
                (_empirical_prepare_library_ev(config, observation, item), item)
                for item in _ordered_random(prepare_library_candidates, rng)
            ),
            key=lambda row: row[0],
        )
        if best_prepare_ev > min_action_ev:
            return _decision_for_prepare_library(
                best_prepare,
                f"empirical_ev_planner: prepare visible library with positive option EV ({best_prepare_ev:.2f})",
            )

    prepare_seed_candidates = [
        item
        for item in actions.get("prepare_seed", [])
        if isinstance(item, dict) and _can_take_prepare_seed(config, observation, item, spend_cap=spend_cap)
    ]
    if prepare_seed_candidates:
        best_seed_ev, best_seed = max(
            (
                (_empirical_prepare_seed_ev(config, observation, item), item)
                for item in _ordered_random(prepare_seed_candidates, rng)
            ),
            key=lambda row: row[0],
        )
        if best_seed_ev > min_action_ev:
            return _decision_for_prepare_seed(
                best_seed,
                f"empirical_ev_planner: prepare visible seed with positive option EV ({best_seed_ev:.2f})",
            )

    return _initial_probe_floor_decision(config, observation, memory, rng, spend_cap=spend_cap)


_SELECTORS = {
    "stop_now": _select_stop_now,
    "blind_random_legal": _select_blind_random_legal,
    "budgeted_static_ranker": _select_budgeted_static_ranker,
    "scout_confirm_stop": _select_scout_confirm_stop,
    "upside_greedy_explorer": _select_upside_greedy_explorer,
    "empirical_ev_planner": _select_empirical_ev_planner,
}


def select_action(
    policy_name: str,
    *,
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: PolicyMemory,
    rng: random.Random,
) -> AgentDecision:
    if policy_name in {
        "jev_choice",
        "jev_k1",
        "d1_k1",
        "laya_k1",
    }:
        from .jev_policy import select_jev_action

        return select_jev_action(policy_name, config=config, observation=observation, memory=memory, rng=rng)
    try:
        selector = _SELECTORS[policy_name]
    except KeyError as exc:
        allowed = ", ".join(sorted(POLICY_NAMES))
        raise ValueError(f"Unknown policy {policy_name!r}; choose one of: {allowed}") from exc
    return selector(config, observation, memory, rng)


def update_memory(memory: PolicyMemory, *, result: dict[str, Any]) -> None:
    if result.get("accepted") is not True:
        return
    memory.accepted_action_count += 1
    record = result.get("action_record")
    if not isinstance(record, dict):
        return

    from .jev_policy import record_campaign_action

    record_campaign_action(memory, record)

    action = record.get("action")
    library_id = record.get("library_id")
    stats = memory.stats_for(library_id) if isinstance(library_id, str) and library_id else None

    if action == "prepare_library" and isinstance(library_id, str):
        memory.prepared_library_ids.add(library_id)
        newly_visible = record.get("newly_visible_seed_ids", [])
        if isinstance(newly_visible, list) and newly_visible:
            memory.discovered_edges.append(
                {
                    "seed_id": record.get("seed_id"),
                    "library_id": library_id,
                    "newly_visible_seed_ids": [str(item) for item in newly_visible],
                }
            )
        return

    if action == "mine_library" and isinstance(library_id, str) and stats is not None:
        memory.probed_library_ids.add(library_id)
        stats.mine_count += 1
        stats.screened_count += int(record.get("actual_batch_size", record.get("requested_batch_size", 0)) or 0)
        for key in ("best_update", "pending_confirmation_added"):
            payload = record.get(key)
            if not isinstance(payload, dict):
                continue
            observed = payload.get("observed_activity")
            if observed is None:
                continue
            observed_value = float(observed)
            if stats.best_observed_activity is None or observed_value > stats.best_observed_activity:
                stats.best_observed_activity = observed_value
            if key == "pending_confirmation_added":
                stats.pending_hit_count += 1
                if stats.best_pending_observed_activity is None or observed_value > stats.best_pending_observed_activity:
                    stats.best_pending_observed_activity = observed_value
        return

    if action == "confirm_hit" and isinstance(library_id, str) and stats is not None:
        stats.confirmed_count += 1
        confirmed = record.get("confirmed_activity")
        if confirmed is not None:
            confirmed_value = float(confirmed)
            confirmed_best_before = float(record.get("confirmed_best_before", confirmed_value) or confirmed_value)
            if confirmed_value > confirmed_best_before:
                stats.confirmed_improvement_count += 1
            if stats.best_confirmed_activity is None or confirmed_value > stats.best_confirmed_activity:
                stats.best_confirmed_activity = confirmed_value

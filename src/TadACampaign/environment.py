"""TadACampaign screening environment."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any

from .biology import aa_interval_to_nt_bounds
from .models import AgentDecision, ExperimentConfig, LibraryDefinition, MineBatchOption, SeedDefinition
from .skill_market import SkillMarketCatalog, empty_skill_market
from .specs import library_sort_key, seed_sort_key


def _stable_seed(base_seed: int, namespace: str) -> int:
    digest = hashlib.sha256(f"{base_seed}:{namespace}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def _sequence_matches_library(
    sequence: str,
    *,
    start_sequence: str,
    library: LibraryDefinition,
    variant: str,
) -> bool:
    if len(sequence) != len(start_sequence):
        return False

    if variant == "protein":
        mutable_start, mutable_end = library.interval_start_aa, library.interval_end_aa
    elif variant == "dna":
        mutable_start, mutable_end = aa_interval_to_nt_bounds(library.interval_start_aa, library.interval_end_aa)
    else:
        raise ValueError(f"Unsupported variant {variant!r}")

    return sequence[:mutable_start] == start_sequence[:mutable_start] and sequence[mutable_end:] == start_sequence[mutable_end:]


def _mutable_interval_aa_payload(library: LibraryDefinition) -> dict[str, int]:
    return {
        "start_index_0based_inclusive": library.interval_start_aa,
        "end_index_0based_exclusive": library.interval_end_aa,
    }


AGENT_HISTORY_RECENT_ACTION_LIMIT = 20
AGENT_HISTORY_RECENT_REJECTION_LIMIT = 20
AGENT_HISTORY_RECENT_SCREEN_RECORD_LIMIT = 20
SEQUENCE_SUMMARY_EDGE_LENGTH = 24

STOP_CODE_AGENT_STOP = "agent_stop"
STOP_CODE_BACKEND_ERROR = "backend_error"
STOP_CODE_CASH_INSUFFICIENT = "cash_insufficient"
STOP_CODE_FORCE_STOP = "force_stop"
STOP_CODE_MAX_EXPERIMENT_DAYS = "max_experiment_days"
STOP_CODE_NO_VISIBLE_PATH = "no_visible_path"
STOP_CODE_TIME_INSUFFICIENT = "time_insufficient"


@dataclass(frozen=True)
class RejectionDetail:
    code: str
    reason: str


def _rejection(code: str, reason: str) -> RejectionDetail:
    return RejectionDetail(code=code, reason=reason)


def infer_stop_code(stop_reason: str | None) -> str | None:
    if stop_reason is None:
        return None
    if stop_reason.startswith("backend_error:"):
        return STOP_CODE_BACKEND_ERROR
    if stop_reason == "Reached max_experiment_days, experiment ended":
        return STOP_CODE_MAX_EXPERIMENT_DAYS
    if stop_reason == "Insufficient remaining time for any legal action":
        return STOP_CODE_TIME_INSUFFICIENT
    if stop_reason == "No visible experimental path remains":
        return STOP_CODE_NO_VISIBLE_PATH
    if stop_reason == "Insufficient cash for any legal action":
        return STOP_CODE_CASH_INSUFFICIENT
    return STOP_CODE_AGENT_STOP


def forced_stop_code(reason: str) -> str:
    return STOP_CODE_BACKEND_ERROR if reason.startswith("backend_error:") else STOP_CODE_FORCE_STOP


def sequence_summary(sequence: str) -> dict[str, Any]:
    return {
        "sequence_length": len(sequence),
        "sequence_prefix": sequence[:SEQUENCE_SUMMARY_EDGE_LENGTH],
        "sequence_suffix": sequence[-SEQUENCE_SUMMARY_EDGE_LENGTH:] if sequence else "",
        "sequence_sha256": hashlib.sha256(sequence.encode("utf-8")).hexdigest(),
    }


def compact_agent_facing_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        compacted: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "sequence" and isinstance(value, str):
                compacted.update(sequence_summary(value))
            elif key in {"confirmed_best_sequence", "provisional_best_sequence"} and isinstance(value, str):
                compacted[f"{key}_summary"] = sequence_summary(value)
            else:
                compacted[key] = compact_agent_facing_payload(value)
        return compacted
    if isinstance(payload, list):
        return [compact_agent_facing_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return [compact_agent_facing_payload(item) for item in payload]
    return payload


class TadACampaignEnvironment:
    def __init__(
        self,
        *,
        variant: str,
        seed_definitions: list[SeedDefinition],
        prepared_rows: list[dict[str, Any]],
        config: ExperimentConfig,
        skill_market: SkillMarketCatalog | None = None,
    ) -> None:
        self.variant = variant
        self.seeds = {definition.seed_id: definition for definition in seed_definitions}
        self.library_definitions = {
            library.library_id: library
            for definition in seed_definitions
            for library in definition.libraries
        }
        self.child_seed_ids_by_parent_seed = self._build_child_seed_index_by_seed()
        self.child_seed_ids_by_parent_library = self._build_child_seed_index_by_library()
        self.config = config
        self.skill_market = skill_market or empty_skill_market()
        self.reward_tiers = tuple(sorted(config.reward_tiers, key=lambda item: item.min_activity))
        self.screenable_sequences_by_library = self._build_screenable_sequences_by_library(prepared_rows)
        self.root_seed_ids = tuple(
            sorted(
                [seed_id for seed_id, definition in self.seeds.items() if not definition.parent_seed_candidates],
                key=seed_sort_key,
            )
        )
        self.visible_seed_ids = set(str(seed_id) for seed_id in config.initial_visible_seeds)
        self.prepared_seed_ids = set(str(seed_id) for seed_id in config.initial_prepared_seeds)
        self._validate_configured_seed_state()
        self.prepared_library_ids: set[str] = set()
        self.cash = float(config.initial_cash)
        self.day = 0.0
        self.confirmed_best_activity = float(config.baseline_activity)
        self.confirmed_best_sequence: str | None = None
        self.provisional_best_activity = float(config.baseline_activity)
        self.provisional_best_sequence: str | None = None
        self.total_reward_earned = 0.0
        self.total_cost_paid = 0.0
        self.screen_record_history: list[dict[str, Any]] = []
        self.action_history: list[dict[str, Any]] = []
        self.rejection_history: list[dict[str, Any]] = []
        self.discovered_sequences: set[str] = set()
        self.pending_confirmation_screen_record_ids: list[int] = []
        self.confirmed_screen_record_ids: set[int] = set()
        self.sequence_noise_fractions: dict[str, float] = {}
        self.owned_skill_ids = self._validate_initial_owned_skill_ids(config.initial_owned_skill_ids)
        self.total_skill_purchase_price_paid = 0.0
        self.next_index = {library_id: 0 for library_id in self.library_definitions}
        self.stopped = False
        self.stop_reason: str | None = None
        self.stop_code: str | None = None
        self.submitted_request_count = 0

    def _remaining_days(self) -> float:
        return max(self.config.max_experiment_days - self.day, 0.0)

    def _action_fits_remaining_days(self, day_cost: float) -> bool:
        return day_cost <= self._remaining_days() + 1e-9

    def _time_horizon_reached(self) -> bool:
        return self.day + 1e-9 >= self.config.max_experiment_days

    def _can_afford_total(self, total_cost: float) -> bool:
        return self.cash + 1e-9 >= total_cost

    def _validate_configured_seed_state(self) -> None:
        known_seed_ids = set(self.seeds)
        unknown_visible = self.visible_seed_ids - known_seed_ids
        unknown_prepared = self.prepared_seed_ids - known_seed_ids
        if unknown_visible:
            raise ValueError(f"Unknown initial_visible_seeds: {sorted(unknown_visible)}")
        if unknown_prepared:
            raise ValueError(f"Unknown initial_prepared_seeds: {sorted(unknown_prepared)}")
        if not self.visible_seed_ids:
            raise ValueError("initial_visible_seeds must not be empty")
        if not self.prepared_seed_ids.issubset(self.visible_seed_ids):
            raise ValueError("initial_prepared_seeds must be a subset of initial_visible_seeds")

    def _validate_initial_owned_skill_ids(self, skill_ids: tuple[str, ...]) -> set[str]:
        unknown = [skill_id for skill_id in skill_ids if self.skill_market.entry(skill_id) is None]
        if unknown:
            raise ValueError(f"Unknown initial_owned_skill_ids: {unknown}")
        return set(skill_ids)

    def runtime_state(self) -> dict[str, Any]:
        return {
            "cash": self.cash,
            "day": self.day,
            "confirmed_best_activity": self.confirmed_best_activity,
            "confirmed_best_sequence": self.confirmed_best_sequence,
            "provisional_best_activity": self.provisional_best_activity,
            "provisional_best_sequence": self.provisional_best_sequence,
            "visible_seed_ids": sorted(self.visible_seed_ids, key=seed_sort_key),
            "prepared_seed_ids": sorted(self.prepared_seed_ids, key=seed_sort_key),
            "prepared_library_ids": sorted(self.prepared_library_ids, key=library_sort_key),
            "total_reward_earned": self.total_reward_earned,
            "total_cost_paid": self.total_cost_paid,
            "screen_record_history": self.screen_record_history,
            "action_history": self.action_history,
            "rejection_history": self.rejection_history,
            "discovered_sequences": sorted(self.discovered_sequences),
            "pending_confirmation_screen_record_ids": list(self.pending_confirmation_screen_record_ids),
            "confirmed_screen_record_ids": sorted(self.confirmed_screen_record_ids),
            "sequence_noise_fractions": dict(self.sequence_noise_fractions),
            "owned_skill_ids": self._ordered_owned_skill_ids(),
            "total_skill_purchase_price_paid": self.total_skill_purchase_price_paid,
            "next_index": self.next_index,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "stop_code": self.stop_code,
            "submitted_request_count": self.submitted_request_count,
        }

    def restore_runtime_state(self, payload: dict[str, Any]) -> None:
        self.cash = float(payload["cash"])
        self.day = float(payload["day"])
        self.confirmed_best_activity = float(payload["confirmed_best_activity"])
        confirmed_best_sequence = payload.get("confirmed_best_sequence")
        self.confirmed_best_sequence = str(confirmed_best_sequence) if isinstance(confirmed_best_sequence, str) else None
        self.provisional_best_activity = float(payload.get("provisional_best_activity", self.confirmed_best_activity))
        provisional_best_sequence = payload.get("provisional_best_sequence", self.confirmed_best_sequence)
        self.provisional_best_sequence = (
            str(provisional_best_sequence) if isinstance(provisional_best_sequence, str) else None
        )
        self.visible_seed_ids = set(str(item) for item in payload.get("visible_seed_ids", []))
        self.prepared_seed_ids = set(str(item) for item in payload.get("prepared_seed_ids", []))
        self.prepared_library_ids = set(str(item) for item in payload.get("prepared_library_ids", []))
        self.total_reward_earned = float(payload.get("total_reward_earned", 0.0))
        self.total_cost_paid = float(payload.get("total_cost_paid", 0.0))
        self.screen_record_history = list(payload.get("screen_record_history", []))
        self.action_history = list(payload.get("action_history", []))
        self.rejection_history = list(payload.get("rejection_history", []))
        self.discovered_sequences = set(str(item) for item in payload.get("discovered_sequences", []))
        self.pending_confirmation_screen_record_ids = [int(item) for item in payload.get("pending_confirmation_screen_record_ids", [])]
        self.confirmed_screen_record_ids = {int(item) for item in payload.get("confirmed_screen_record_ids", [])}
        self.sequence_noise_fractions = {
            str(key): float(value) for key, value in payload.get("sequence_noise_fractions", {}).items()
        }
        self.owned_skill_ids = {
            skill_id
            for skill_id in (str(item) for item in payload.get("owned_skill_ids", []))
            if self.skill_market.entry(skill_id) is not None
        }
        self.total_skill_purchase_price_paid = float(payload.get("total_skill_purchase_price_paid", 0.0))
        raw_next_index = {str(key): int(value) for key, value in payload.get("next_index", {}).items()}
        self.next_index = {library_id: raw_next_index.get(library_id, 0) for library_id in self.library_definitions}
        self.stopped = bool(payload.get("stopped", False))
        stop_reason = payload.get("stop_reason")
        self.stop_reason = str(stop_reason) if isinstance(stop_reason, str) else None
        stop_code = payload.get("stop_code")
        self.stop_code = str(stop_code) if isinstance(stop_code, str) else infer_stop_code(self.stop_reason)
        self.submitted_request_count = int(
            payload.get("submitted_request_count", len(self.action_history) + len(self.rejection_history))
        )
        self._prune_pending_confirmations()

    @property
    def request_count(self) -> int:
        return self.submitted_request_count

    def _build_child_seed_index_by_seed(self) -> dict[str, tuple[str, ...]]:
        mapping: dict[str, list[str]] = {}
        for seed_id, definition in self.seeds.items():
            if definition.parent_seed_id is None:
                continue
            mapping.setdefault(definition.parent_seed_id, []).append(seed_id)
        for seed_ids in mapping.values():
            seed_ids.sort(key=seed_sort_key)
        return {parent_seed_id: tuple(seed_ids) for parent_seed_id, seed_ids in mapping.items()}

    def _build_child_seed_index_by_library(self) -> dict[str, tuple[str, ...]]:
        mapping: dict[str, list[str]] = {}
        for seed_id, definition in self.seeds.items():
            if definition.parent_library_id is None:
                continue
            mapping.setdefault(definition.parent_library_id, []).append(seed_id)
        for seed_ids in mapping.values():
            seed_ids.sort(key=seed_sort_key)
        return {parent_library_id: tuple(seed_ids) for parent_library_id, seed_ids in mapping.items()}

    def _reveal_child_seeds_of_library(self, library_id: str) -> list[str]:
        newly_visible: list[str] = []
        for child_seed_id in self.child_seed_ids_by_parent_library.get(library_id, ()):
            if child_seed_id in self.visible_seed_ids:
                continue
            self.visible_seed_ids.add(child_seed_id)
            newly_visible.append(child_seed_id)
        return newly_visible

    def _build_screenable_sequences_by_library(
        self,
        prepared_rows: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        screenable_sequences_by_library = {library_id: [] for library_id in self.library_definitions}
        libraries_by_historical_round_id = {
            library.historical_round_id: library
            for library in self.library_definitions.values()
        }
        for row in prepared_rows:
            sequence = row["sequence"]
            true_activity = float(row["activity"])
            for historical_round_id in row["round_ids"]:
                library = libraries_by_historical_round_id.get(historical_round_id)
                if library is None:
                    continue
                start_sequence = self.seeds[library.seed_id].start_sequence
                if _sequence_matches_library(
                    sequence,
                    start_sequence=start_sequence,
                    library=library,
                    variant=self.variant,
                ):
                    screenable_sequences_by_library[library.library_id].append(
                        {"sequence": sequence, "true_activity": true_activity}
                    )

        for library_id, entries in screenable_sequences_by_library.items():
            rng = random.Random(_stable_seed(self.config.seed, self.library_definitions[library_id].key))
            rng.shuffle(entries)
        return screenable_sequences_by_library

    def _screening_noise_fraction(self, sequence: str) -> float:
        if not self.config.screening_noise_enabled:
            return 0.0
        cached = self.sequence_noise_fractions.get(sequence)
        if cached is not None:
            return cached
        sigma = float(self.config.screening_noise_sigma)
        clip = float(self.config.screening_noise_clip)
        if sigma <= 0.0:
            self.sequence_noise_fractions[sequence] = 0.0
            return 0.0
        rng = random.Random(_stable_seed(self.config.seed, f"screening-noise:{sequence}"))
        fraction = 0.0
        for _ in range(1024):
            candidate = rng.gauss(0.0, sigma)
            if -clip <= candidate <= clip:
                fraction = candidate
                break
        else:
            fraction = max(min(rng.gauss(0.0, sigma), clip), -clip)
        self.sequence_noise_fractions[sequence] = fraction
        return fraction

    def _observed_activity(self, sequence: str, true_activity: float) -> tuple[float, float]:
        noise_fraction = self._screening_noise_fraction(sequence)
        observed_activity = true_activity * (1.0 + noise_fraction)
        return observed_activity, noise_fraction

    def _screen_record_observed_activity(self, screen_record: dict[str, Any]) -> float:
        value = screen_record.get("observed_activity", screen_record.get("activity"))
        return float(value)

    def _screen_record_true_activity(self, screen_record: dict[str, Any]) -> float:
        value = screen_record.get("true_activity", screen_record.get("confirmed_activity", screen_record.get("activity")))
        return float(value)

    def _public_screen_record_payload(self, screen_record: dict[str, Any], *, include_sequence: bool = True) -> dict[str, Any]:
        sequence = str(screen_record["sequence"])
        payload = {
            "screen_record_id": int(screen_record["screen_record_id"]),
            "seed_id": screen_record["seed_id"],
            "library_id": screen_record["library_id"],
            "observed_activity": self._screen_record_observed_activity(screen_record),
            "is_new_sequence": bool(screen_record.get("is_new_sequence", False)),
            "is_confirmed": bool(screen_record.get("is_confirmed", False)),
            "noise_pending": not bool(screen_record.get("is_confirmed", False)),
        }
        if include_sequence:
            payload["sequence"] = sequence
        else:
            payload.update(sequence_summary(sequence))
        if screen_record.get("is_confirmed"):
            payload["confirmed_activity"] = float(
                screen_record.get("confirmed_activity", self._screen_record_true_activity(screen_record))
            )
            payload["noise_fraction"] = float(screen_record.get("noise_fraction", 0.0))
            payload["noise_pending"] = False
        if screen_record.get("is_confirmed_best"):
            payload["is_confirmed_best"] = True
        return payload

    def public_screen_record_history(self) -> list[dict[str, Any]]:
        return [
            self._public_screen_record_payload(screen_record)
            for screen_record in self.screen_record_history
            if isinstance(screen_record, dict)
        ]

    def _agent_history_screen_record_payloads(self) -> list[dict[str, Any]]:
        return [
            self._public_screen_record_payload(screen_record, include_sequence=False)
            for screen_record in self.screen_record_history[-AGENT_HISTORY_RECENT_SCREEN_RECORD_LIMIT:]
            if isinstance(screen_record, dict)
        ]

    def _lookup_library(self, seed_id: str, library_id: str) -> LibraryDefinition | None:
        library = self.library_definitions.get(library_id)
        if library is None or library.seed_id != seed_id:
            return None
        return library

    def _known_library_counts(self, library_id: str) -> tuple[int, int, int]:
        screenable_sequence_count = len(self.screenable_sequences_by_library[library_id])
        screened_sequence_count = self.next_index[library_id]
        remaining_screenable_sequence_count = screenable_sequence_count - screened_sequence_count
        return screenable_sequence_count, screened_sequence_count, remaining_screenable_sequence_count

    def _mine_total_cost(self, option: MineBatchOption) -> float:
        return option.cost + option.time_days * self.config.daily_burn_cost

    def _prepare_seed_total_cost(self) -> float:
        return self.config.prepare_seed_cost + self.config.prepare_seed_days * self.config.daily_burn_cost

    def _prepare_library_total_cost(self) -> float:
        return self.config.prepare_library_cost + self.config.prepare_library_days * self.config.daily_burn_cost

    def _confirm_total_cost(self) -> float:
        return self.config.confirm_hit_cost + self.config.confirm_hit_days * self.config.daily_burn_cost

    def _purchase_total_cost(self, skill_id: str) -> float | None:
        entry = self.skill_market.entry(skill_id)
        if entry is None:
            return None
        return entry.price + self.config.purchase_skill_days * self.config.daily_burn_cost

    def _minimum_mine_option(self) -> MineBatchOption | None:
        if not self.config.mine_batch_menu:
            return None
        return min(self.config.mine_batch_menu, key=lambda option: self._mine_total_cost(option))

    def _maximum_mine_option(self) -> MineBatchOption | None:
        if not self.config.mine_batch_menu:
            return None
        return max(self.config.mine_batch_menu, key=lambda option: self._mine_total_cost(option))

    def _available_mine_options_for_budget(self) -> tuple[MineBatchOption, ...]:
        options: list[MineBatchOption] = []
        for seed_id in sorted(self.visible_seed_ids, key=seed_sort_key):
            if seed_id not in self.prepared_seed_ids:
                continue
            seed_definition = self.seeds[seed_id]
            for library in sorted(seed_definition.libraries, key=lambda item: library_sort_key(item.library_id)):
                if library.library_id not in self.prepared_library_ids:
                    continue
                options.extend(self._legal_mine_options(library.library_id, require_cash=False))
        return tuple(options)

    def _post_action_confirm_reserve_payload(self, *, total_cost: float, time_days: float) -> dict[str, Any]:
        cash_after_action = self.cash - total_cost
        remaining_days_after_action = self._remaining_days() - time_days
        confirm_total_cost = self._confirm_total_cost()
        cash_after_action_and_confirm = cash_after_action - confirm_total_cost
        remaining_days_after_action_and_confirm = remaining_days_after_action - self.config.confirm_hit_days
        return {
            "cash_after_action": cash_after_action,
            "remaining_days_after_action": remaining_days_after_action,
            "cash_after_action_and_confirm_reserve": cash_after_action_and_confirm,
            "remaining_days_after_action_and_confirm_reserve": remaining_days_after_action_and_confirm,
            "leaves_confirm_cash": cash_after_action_and_confirm >= -1e-9,
            "leaves_confirm_time": remaining_days_after_action_and_confirm >= -1e-9,
            "leaves_confirm_capacity": cash_after_action_and_confirm >= -1e-9
            and remaining_days_after_action_and_confirm >= -1e-9,
        }

    def _economic_summary(self) -> dict[str, Any]:
        confirm_total_cost = self._confirm_total_cost()
        minimum_mine_option = self._minimum_mine_option()
        maximum_mine_option = self._maximum_mine_option()
        min_mine_total_cost = self._mine_total_cost(minimum_mine_option) if minimum_mine_option is not None else None
        max_mine_total_cost = self._mine_total_cost(maximum_mine_option) if maximum_mine_option is not None else None
        configured_min_mine_then_confirm_total_cost = (
            min_mine_total_cost + confirm_total_cost if min_mine_total_cost is not None else None
        )
        configured_min_mine_then_confirm_days = (
            minimum_mine_option.time_days + self.config.confirm_hit_days if minimum_mine_option is not None else None
        )
        available_mine_options = self._available_mine_options_for_budget()
        cheapest_available_mine_option = (
            min(available_mine_options, key=lambda option: self._mine_total_cost(option))
            if available_mine_options
            else None
        )
        available_mine_total_cost = (
            self._mine_total_cost(cheapest_available_mine_option)
            if cheapest_available_mine_option is not None
            else None
        )
        available_mine_then_confirm_total_cost = (
            available_mine_total_cost + confirm_total_cost if available_mine_total_cost is not None else None
        )
        available_mine_then_confirm_days = (
            cheapest_available_mine_option.time_days + self.config.confirm_hit_days
            if cheapest_available_mine_option is not None
            else None
        )
        remaining_days = self._remaining_days()
        can_afford_confirm = self.cash + 1e-9 >= confirm_total_cost
        has_time_for_confirm = remaining_days + 1e-9 >= self.config.confirm_hit_days
        can_afford_configured_min_mine_then_confirm = (
            configured_min_mine_then_confirm_total_cost is not None
            and self.cash + 1e-9 >= configured_min_mine_then_confirm_total_cost
        )
        has_time_for_configured_min_mine_then_confirm = (
            configured_min_mine_then_confirm_days is not None
            and remaining_days + 1e-9 >= configured_min_mine_then_confirm_days
        )
        can_afford_available_mine_then_confirm = (
            available_mine_then_confirm_total_cost is not None
            and self.cash + 1e-9 >= available_mine_then_confirm_total_cost
        )
        has_time_for_available_mine_then_confirm = (
            available_mine_then_confirm_days is not None
            and remaining_days + 1e-9 >= available_mine_then_confirm_days
        )
        if can_afford_configured_min_mine_then_confirm and has_time_for_configured_min_mine_then_confirm:
            budget_regime = "can_mine_and_confirm"
        elif can_afford_confirm and has_time_for_confirm:
            budget_regime = "confirm_only"
        elif not can_afford_confirm and not has_time_for_confirm:
            budget_regime = "cash_and_time_below_confirm"
        elif has_time_for_confirm:
            budget_regime = "cash_below_confirm"
        else:
            budget_regime = "time_below_confirm"

        return {
            "current_cash": self.cash,
            "remaining_days": remaining_days,
            "daily_burn_cost": self.config.daily_burn_cost,
            "action_total_costs": {
                "prepare_seed": self._prepare_seed_total_cost(),
                "prepare_library": self._prepare_library_total_cost(),
                "confirm_hit": confirm_total_cost,
                "mine_library_min": min_mine_total_cost,
                "mine_library_max": max_mine_total_cost,
                "available_mine_library_min": available_mine_total_cost,
            },
            "action_time_days": {
                "prepare_seed": self.config.prepare_seed_days,
                "prepare_library": self.config.prepare_library_days,
                "confirm_hit": self.config.confirm_hit_days,
                "mine_library_min": minimum_mine_option.time_days if minimum_mine_option is not None else None,
                "mine_library_max": maximum_mine_option.time_days if maximum_mine_option is not None else None,
                "available_mine_library_min": (
                    cheapest_available_mine_option.time_days
                    if cheapest_available_mine_option is not None
                    else None
                ),
            },
            "configured_minimum_mine_then_confirm_total_cost": configured_min_mine_then_confirm_total_cost,
            "configured_minimum_mine_then_confirm_days": configured_min_mine_then_confirm_days,
            "minimum_mine_then_confirm_total_cost": configured_min_mine_then_confirm_total_cost,
            "minimum_mine_then_confirm_days": configured_min_mine_then_confirm_days,
            "available_mine_then_confirm_total_cost": available_mine_then_confirm_total_cost,
            "available_mine_then_confirm_days": available_mine_then_confirm_days,
            "cash_after_confirm_reserve": self.cash - confirm_total_cost,
            "remaining_days_after_confirm_reserve": remaining_days - self.config.confirm_hit_days,
            "cash_after_configured_minimum_mine_then_confirm_reserve": (
                self.cash - configured_min_mine_then_confirm_total_cost
                if configured_min_mine_then_confirm_total_cost is not None
                else None
            ),
            "remaining_days_after_configured_minimum_mine_then_confirm_reserve": (
                remaining_days - configured_min_mine_then_confirm_days
                if configured_min_mine_then_confirm_days is not None
                else None
            ),
            "cash_after_minimum_mine_then_confirm_reserve": (
                self.cash - configured_min_mine_then_confirm_total_cost
                if configured_min_mine_then_confirm_total_cost is not None
                else None
            ),
            "remaining_days_after_minimum_mine_then_confirm_reserve": (
                remaining_days - configured_min_mine_then_confirm_days
                if configured_min_mine_then_confirm_days is not None
                else None
            ),
            "cash_after_available_mine_then_confirm_reserve": (
                self.cash - available_mine_then_confirm_total_cost
                if available_mine_then_confirm_total_cost is not None
                else None
            ),
            "remaining_days_after_available_mine_then_confirm_reserve": (
                remaining_days - available_mine_then_confirm_days
                if available_mine_then_confirm_days is not None
                else None
            ),
            "can_afford_confirm_now": can_afford_confirm,
            "has_time_for_confirm_now": has_time_for_confirm,
            "can_afford_configured_minimum_mine_then_confirm_now": can_afford_configured_min_mine_then_confirm,
            "has_time_for_configured_minimum_mine_then_confirm_now": has_time_for_configured_min_mine_then_confirm,
            "can_afford_minimum_mine_then_confirm_now": can_afford_configured_min_mine_then_confirm,
            "has_time_for_minimum_mine_then_confirm_now": has_time_for_configured_min_mine_then_confirm,
            "has_available_mine_option": cheapest_available_mine_option is not None,
            "can_afford_available_mine_then_confirm_now": can_afford_available_mine_then_confirm,
            "has_time_for_available_mine_then_confirm_now": has_time_for_available_mine_then_confirm,
            "budget_regime": budget_regime,
        }

    def _ordered_owned_skill_ids(self) -> list[str]:
        return [entry.skill_id for entry in self.skill_market.ordered_entries() if entry.skill_id in self.owned_skill_ids]

    def _visible_skill_market_size(self) -> int:
        return len(self.skill_market.entries_by_id) if self.config.skill_market_enabled else 0

    def _screen_record_by_id(self, screen_record_id: int) -> dict[str, Any] | None:
        # IDs are currently allocated from the list length, but lookup should
        # follow the persisted identifier rather than relying on list position.
        # This keeps restored sessions correct if an audit/recovery step ever
        # leaves a sparse or reordered history.
        for item in self.screen_record_history:
            if not isinstance(item, dict):
                continue
            try:
                item_id = int(item.get("screen_record_id"))
            except (TypeError, ValueError):
                continue
            if item_id == screen_record_id:
                return item
        return None

    def _prune_pending_confirmations(self) -> None:
        retained: list[int] = []
        seen: set[int] = set()
        for screen_record_id in self.pending_confirmation_screen_record_ids:
            if screen_record_id in seen or screen_record_id in self.confirmed_screen_record_ids:
                continue
            screen_record = self._screen_record_by_id(screen_record_id)
            if screen_record is None:
                continue
            if self._screen_record_observed_activity(screen_record) <= self.confirmed_best_activity + 1e-9:
                continue
            retained.append(screen_record_id)
            seen.add(screen_record_id)
        self.pending_confirmation_screen_record_ids = retained

    def _pending_confirmation_payload(self, screen_record_id: int, *, legal_now: bool) -> dict[str, Any] | None:
        screen_record = self._screen_record_by_id(screen_record_id)
        if screen_record is None:
            return None
        burn_cost = self.config.confirm_hit_days * self.config.daily_burn_cost
        return {
            "action": "confirm_hit",
            "screen_record_id": screen_record_id,
            "seed_id": screen_record["seed_id"],
            "library_id": screen_record["library_id"],
            "sequence": screen_record["sequence"],
            "observed_activity": self._screen_record_observed_activity(screen_record),
            "cost": self.config.confirm_hit_cost,
            "time_days": self.config.confirm_hit_days,
            "burn_cost": burn_cost,
            "total_cost": self.config.confirm_hit_cost + burn_cost,
            "current_confirmed_best_activity": self.confirmed_best_activity,
            "legal_now": legal_now,
            "cash_after_confirm": self.cash - (self.config.confirm_hit_cost + burn_cost),
            "remaining_days_after_confirm": self._remaining_days() - self.config.confirm_hit_days,
            "cash_shortfall": max(0.0, (self.config.confirm_hit_cost + burn_cost) - self.cash),
            "time_shortfall_days": max(0.0, self.config.confirm_hit_days - self._remaining_days()),
        }

    def _pending_confirmation_payloads(self) -> list[dict[str, Any]]:
        self._prune_pending_confirmations()
        legal_now = self._action_fits_remaining_days(self.config.confirm_hit_days) and self._can_afford_total(
            self._confirm_total_cost()
        )
        payloads: list[dict[str, Any]] = []
        for screen_record_id in self.pending_confirmation_screen_record_ids:
            payload = self._pending_confirmation_payload(screen_record_id, legal_now=legal_now)
            if payload is not None:
                payloads.append(payload)
        return payloads

    def _legal_confirm_screen_record_ids(self) -> tuple[int, ...]:
        self._prune_pending_confirmations()
        if not self._action_fits_remaining_days(self.config.confirm_hit_days):
            return ()
        if not self._can_afford_total(self._confirm_total_cost()):
            return ()
        return tuple(self.pending_confirmation_screen_record_ids)

    def _legal_mine_options(self, library_id: str, *, require_cash: bool = True) -> tuple[MineBatchOption, ...]:
        _, _, remaining_screenable_sequence_count = self._known_library_counts(library_id)
        return tuple(
            option
            for option in self.config.mine_batch_menu
            if option.batch_size <= remaining_screenable_sequence_count
            and self._action_fits_remaining_days(option.time_days)
            and (not require_cash or self._can_afford_total(self._mine_total_cost(option)))
        )

    def _has_structural_action_candidate(self) -> bool:
        self._prune_pending_confirmations()
        if self.pending_confirmation_screen_record_ids:
            return True
        if self.config.skill_market_enabled and any(
            entry.skill_id not in self.owned_skill_ids for entry in self.skill_market.ordered_entries()
        ):
            return True
        for seed_id in sorted(self.visible_seed_ids, key=seed_sort_key):
            if seed_id not in self.prepared_seed_ids:
                return True

            seed_definition = self.seeds[seed_id]
            for library in sorted(seed_definition.libraries, key=lambda item: library_sort_key(item.library_id)):
                if library.library_id not in self.prepared_library_ids:
                    return True
                _, _, remaining_screenable_sequence_count = self._known_library_counts(library.library_id)
                if any(option.batch_size <= remaining_screenable_sequence_count for option in self.config.mine_batch_menu):
                    return True
        return False

    def _cheapest_action_cost(self) -> float | None:
        cheapest: float | None = None
        self._prune_pending_confirmations()
        if self.pending_confirmation_screen_record_ids and self._action_fits_remaining_days(self.config.confirm_hit_days):
            cheapest = self._confirm_total_cost()
        if self.config.skill_market_enabled and self._action_fits_remaining_days(self.config.purchase_skill_days):
            for entry in self.skill_market.ordered_entries():
                if entry.skill_id in self.owned_skill_ids:
                    continue
                total = self._purchase_total_cost(entry.skill_id)
                if total is not None:
                    cheapest = total if cheapest is None else min(cheapest, total)
        for seed_id in sorted(self.visible_seed_ids, key=seed_sort_key):
            if seed_id not in self.prepared_seed_ids:
                if not self._action_fits_remaining_days(self.config.prepare_seed_days):
                    continue
                total = self._prepare_seed_total_cost()
                cheapest = total if cheapest is None else min(cheapest, total)
                continue

            seed_definition = self.seeds[seed_id]
            for library in sorted(seed_definition.libraries, key=lambda item: library_sort_key(item.library_id)):
                if library.library_id not in self.prepared_library_ids:
                    if not self._action_fits_remaining_days(self.config.prepare_library_days):
                        continue
                    total = self._prepare_library_total_cost()
                    cheapest = total if cheapest is None else min(cheapest, total)
                    continue
                for option in self._legal_mine_options(library.library_id, require_cash=False):
                    total = self._mine_total_cost(option)
                    cheapest = total if cheapest is None else min(cheapest, total)
        return cheapest

    def _reward_for_improvement(self, previous_best: float, new_best: float) -> float:
        if new_best <= previous_best:
            return 0.0
        reward = 0.0
        for tier in self.reward_tiers:
            lower = max(previous_best, tier.min_activity)
            upper = min(new_best, tier.max_activity)
            if upper <= lower:
                continue
            reward += (
                self.config.base_asset_value
                * self.config.baseline_activity
                * tier.multiplier
                * ((1.0 / lower) - (1.0 / upper))
            )
        return reward

    def is_terminal(self) -> bool:
        if self.stopped:
            self.stop_code = self.stop_code or infer_stop_code(self.stop_reason)
            return True
        if self._time_horizon_reached():
            self.stop_reason = "Reached max_experiment_days, experiment ended"
            self.stop_code = STOP_CODE_MAX_EXPERIMENT_DAYS
            return True
        cheapest = self._cheapest_action_cost()
        if cheapest is None:
            if self._has_structural_action_candidate():
                self.stop_reason = "Insufficient remaining time for any legal action"
                self.stop_code = STOP_CODE_TIME_INSUFFICIENT
            else:
                self.stop_reason = "No visible experimental path remains"
                self.stop_code = STOP_CODE_NO_VISIBLE_PATH
            return True
        if self.cash + 1e-9 < cheapest:
            self.stop_reason = "Insufficient cash for any legal action"
            self.stop_code = STOP_CODE_CASH_INSUFFICIENT
            return True
        return False

    def available_actions(self) -> dict[str, Any]:
        if self.is_terminal():
            return {
                "prepare_seed": [],
                "prepare_library": [],
                "mine_library": [],
                "confirm_hit": [],
                "purchase_skill": [],
                "stop": {"action": "stop"},
            }

        prepare_seed_actions: list[dict[str, Any]] = []
        prepare_library_actions: list[dict[str, Any]] = []
        mine_actions: list[dict[str, Any]] = []
        purchase_actions: list[dict[str, Any]] = []
        confirm_actions = [
            payload
            for screen_record_id in self._legal_confirm_screen_record_ids()
            if (payload := self._pending_confirmation_payload(screen_record_id, legal_now=True)) is not None
        ]
        if self.config.skill_market_enabled and self._action_fits_remaining_days(self.config.purchase_skill_days):
            for entry in self.skill_market.ordered_entries():
                if entry.skill_id in self.owned_skill_ids:
                    continue
                payload = entry.market_card(
                    purchase_days=self.config.purchase_skill_days,
                    daily_burn_cost=self.config.daily_burn_cost,
                    owned=False,
                )
                if not self._can_afford_total(float(payload["total_cost"])):
                    continue
                payload["action"] = "purchase_skill"
                purchase_actions.append(payload)
        for seed_id in sorted(self.visible_seed_ids, key=seed_sort_key):
            seed_definition = self.seeds[seed_id]
            if seed_id not in self.prepared_seed_ids:
                if self._action_fits_remaining_days(self.config.prepare_seed_days) and self._can_afford_total(
                    self._prepare_seed_total_cost()
                ):
                    burn_cost = self.config.prepare_seed_days * self.config.daily_burn_cost
                    prepare_seed_actions.append(
                        {
                            "action": "prepare_seed",
                            "seed_id": seed_id,
                            "parent_seed_candidate_ids": list(seed_definition.parent_seed_candidates),
                            "parent_library_candidate_ids": list(seed_definition.parent_library_candidates),
                            "cost": self.config.prepare_seed_cost,
                            "time_days": self.config.prepare_seed_days,
                            "burn_cost": burn_cost,
                            "total_cost": self.config.prepare_seed_cost + burn_cost,
                        }
                    )
                continue

            for library in sorted(seed_definition.libraries, key=lambda item: library_sort_key(item.library_id)):
                if library.library_id not in self.prepared_library_ids:
                    if self._action_fits_remaining_days(self.config.prepare_library_days) and self._can_afford_total(
                        self._prepare_library_total_cost()
                    ):
                        burn_cost = self.config.prepare_library_days * self.config.daily_burn_cost
                        prepare_library_actions.append(
                            {
                                "action": "prepare_library",
                                "seed_id": seed_id,
                                "library_id": library.library_id,
                                "mutable_interval_aa": _mutable_interval_aa_payload(library),
                                "interval_length_aa": library.interval_length_aa,
                                "library_status": "locked",
                                "cost": self.config.prepare_library_cost,
                                "time_days": self.config.prepare_library_days,
                                "burn_cost": burn_cost,
                                "total_cost": self.config.prepare_library_cost + burn_cost,
                            }
                        )
                    continue

                screenable_sequence_count, screened_sequence_count, remaining_screenable_sequence_count = self._known_library_counts(library.library_id)
                batch_options = self._legal_mine_options(library.library_id)
                if not batch_options:
                    continue
                mine_actions.append(
                    {
                        "action": "mine_library",
                        "seed_id": seed_id,
                        "library_id": library.library_id,
                        "screenable_sequence_count": screenable_sequence_count,
                        "screened_sequence_count": screened_sequence_count,
                        "remaining_screenable_sequence_count": remaining_screenable_sequence_count,
                        "batch_options": [
                            {
                                "batch_size": option.batch_size,
                                "cost": option.cost,
                                "time_days": option.time_days,
                                "burn_cost": option.time_days * self.config.daily_burn_cost,
                                "total_cost": self._mine_total_cost(option),
                                **self._post_action_confirm_reserve_payload(
                                    total_cost=self._mine_total_cost(option),
                                    time_days=option.time_days,
                                ),
                            }
                            for option in batch_options
                        ],
                    }
                )
        return {
            "prepare_seed": prepare_seed_actions,
            "prepare_library": prepare_library_actions,
            "mine_library": mine_actions,
            "confirm_hit": confirm_actions,
            "purchase_skill": purchase_actions,
            "stop": {"action": "stop"},
        }

    def get_state(self) -> dict[str, Any]:
        seeds_payload = []
        for seed_id in sorted(self.visible_seed_ids, key=seed_sort_key):
            seed_definition = self.seeds[seed_id]
            libraries_payload = []
            for library in sorted(seed_definition.libraries, key=lambda item: library_sort_key(item.library_id)):
                screenable_sequence_count, screened_sequence_count, remaining_screenable_sequence_count = self._known_library_counts(library.library_id)
                library_payload: dict[str, Any] = {
                    "seed_id": seed_id,
                    "library_id": library.library_id,
                    "mutable_interval_aa": _mutable_interval_aa_payload(library),
                    "interval_length_aa": library.interval_length_aa,
                    "library_status": "prepared" if library.library_id in self.prepared_library_ids else "locked",
                }
                if library.library_id in self.prepared_library_ids:
                    library_payload["screenable_sequence_count"] = screenable_sequence_count
                    library_payload["screened_sequence_count"] = screened_sequence_count
                    library_payload["remaining_screenable_sequence_count"] = remaining_screenable_sequence_count
                libraries_payload.append(library_payload)

            seeds_payload.append(
                {
                    "seed_id": seed_id,
                    "parent_seed_candidate_ids": list(seed_definition.parent_seed_candidates),
                    "parent_library_candidate_ids": list(seed_definition.parent_library_candidates),
                    "start_sequence": seed_definition.start_sequence,
                    "seed_status": "prepared" if seed_id in self.prepared_seed_ids else "visible",
                    "libraries": libraries_payload,
                }
            )

        return {
            "variant": self.variant,
            "current_cash": self.cash,
            "elapsed_day": self.day,
            "max_experiment_days": self.config.max_experiment_days,
            "remaining_days": self._remaining_days(),
            "confirmed_best_activity": self.confirmed_best_activity,
            "confirmed_best_sequence": self.confirmed_best_sequence,
            "provisional_best_activity": self.provisional_best_activity,
            "provisional_best_sequence": self.provisional_best_sequence,
            "baseline_activity": self.config.baseline_activity,
            "visible_seed_ids": sorted(self.visible_seed_ids, key=seed_sort_key),
            "prepared_seed_ids": sorted(self.prepared_seed_ids, key=seed_sort_key),
            "prepared_library_ids": sorted(self.prepared_library_ids, key=library_sort_key),
            "screen_record_count": len(self.screen_record_history),
            "confirmed_screen_record_count": len(self.confirmed_screen_record_ids),
            "pending_confirmation_count": len(self._pending_confirmation_payloads()),
            "pending_confirmations": self._pending_confirmation_payloads(),
            "economic_summary": self._economic_summary(),
            "skill_market_enabled": self.config.skill_market_enabled,
            "skill_market_size": self._visible_skill_market_size(),
            "skill_catalog_size": len(self.skill_market.entries_by_id),
            "initial_owned_skill_ids": list(self.config.initial_owned_skill_ids),
            "owned_skill_count": len(self.owned_skill_ids),
            "owned_skill_ids": self._ordered_owned_skill_ids(),
            "total_skill_purchase_price_paid": self.total_skill_purchase_price_paid,
            "owned_skills_root": "owned_skills",
            "action_count": len(self.action_history),
            "rejected_action_count": len(self.rejection_history),
            "request_count": self.request_count,
            "total_reward_earned": self.total_reward_earned,
            "total_cost_paid": self.total_cost_paid,
            "stop_reason": self.stop_reason,
            "stop_code": self.stop_code,
            "screening_noise_enabled": self.config.screening_noise_enabled,
            "screening_noise_sigma": self.config.screening_noise_sigma,
            "screening_noise_clip": self.config.screening_noise_clip,
            "seeds": seeds_payload,
        }

    def get_history(self) -> dict[str, Any]:
        return {
            "recent_actions": compact_agent_facing_payload(self.action_history[-AGENT_HISTORY_RECENT_ACTION_LIMIT:]),
            "recent_screen_records": self._agent_history_screen_record_payloads(),
            "recent_rejections": compact_agent_facing_payload(
                self.rejection_history[-AGENT_HISTORY_RECENT_REJECTION_LIMIT:]
            ),
        }

    def validate_decision_detail(self, decision: AgentDecision) -> RejectionDetail | None:
        if self.is_terminal():
            return _rejection("terminal_session", "current session is terminal")

        if decision.action == "stop":
            return None

        if decision.action == "purchase_skill":
            if not self.config.skill_market_enabled:
                return _rejection("skill_market_disabled", "`purchase_skill` is not enabled")
            if decision.skill_id is None or not str(decision.skill_id).strip():
                return _rejection("missing_skill_id", "`purchase_skill` requires `skill_id`")
            entry = self.skill_market.entry(decision.skill_id)
            if entry is None:
                return _rejection("unknown_skill", f"Unknown `skill_id`: {decision.skill_id}")
            if decision.skill_id in self.owned_skill_ids:
                return _rejection("skill_already_owned", f"Skill {decision.skill_id} already purchased")
            if not self._action_fits_remaining_days(self.config.purchase_skill_days):
                return _rejection(
                    "time_insufficient",
                    (
                        f"Insufficient remaining time, cannot execute `purchase_skill`; "
                        f"remaining {self._remaining_days():.4f} days, requires {self.config.purchase_skill_days:.4f} days"
                    ),
                )
            purchase_total_cost = self._purchase_total_cost(decision.skill_id)
            if purchase_total_cost is None:
                return _rejection("unknown_skill", f"Unknown `skill_id`: {decision.skill_id}")
            if not self._can_afford_total(purchase_total_cost):
                return _rejection(
                    "cash_insufficient",
                    f"Insufficient cash, cannot execute `purchase_skill`; current cash {self.cash:.2f}, requires {purchase_total_cost:.2f}",
                )
            return None

        if decision.action == "confirm_hit":
            if decision.screen_record_id is None or isinstance(decision.screen_record_id, bool):
                return _rejection("missing_screen_record_id", "`confirm_hit` requires an integer `screen_record_id`")
            screen_record = self._screen_record_by_id(decision.screen_record_id)
            if screen_record is None:
                return _rejection("unknown_screen_record", f"Unknown `screen_record_id`: {decision.screen_record_id}")
            if decision.screen_record_id in self.confirmed_screen_record_ids:
                return _rejection("screen_record_already_confirmed", f"Screen record {decision.screen_record_id} already confirmed")
            self._prune_pending_confirmations()
            if decision.screen_record_id not in self.pending_confirmation_screen_record_ids:
                return _rejection(
                    "screen_record_not_pending",
                    f"Screen record {decision.screen_record_id} is not currently pending confirmation",
                )
            if not self._action_fits_remaining_days(self.config.confirm_hit_days):
                return _rejection(
                    "time_insufficient",
                    (
                        f"Insufficient remaining time, cannot execute `confirm_hit`; "
                        f"remaining {self._remaining_days():.4f} days, requires {self.config.confirm_hit_days:.4f} days"
                    ),
                )
            confirm_total_cost = self._confirm_total_cost()
            if self.cash + 1e-9 < confirm_total_cost:
                return _rejection(
                    "cash_insufficient",
                    f"Insufficient cash, cannot execute `confirm_hit`; current cash {self.cash:.2f}, requires {confirm_total_cost:.2f}",
                )
            return None

        if not decision.seed_id:
            return _rejection("missing_seed_id", f"Action {decision.action} requires `seed_id`")
        if decision.seed_id not in self.seeds:
            return _rejection("unknown_seed", f"Unknown `seed_id`: {decision.seed_id}")

        if decision.action == "prepare_seed":
            if decision.seed_id not in self.visible_seed_ids:
                return _rejection("seed_not_visible", f"Cannot `prepare_seed` invisible seed {decision.seed_id}")
            if decision.seed_id in self.prepared_seed_ids:
                return _rejection("seed_already_prepared", f"Seed {decision.seed_id} is already prepared state")
            if not self._action_fits_remaining_days(self.config.prepare_seed_days):
                return _rejection(
                    "time_insufficient",
                    (
                        f"Insufficient remaining time, cannot execute `prepare_seed`; "
                        f"remaining {self._remaining_days():.4f} days, requires {self.config.prepare_seed_days:.4f} days"
                    ),
                )
            prepare_seed_total_cost = self._prepare_seed_total_cost()
            if not self._can_afford_total(prepare_seed_total_cost):
                return _rejection(
                    "cash_insufficient",
                    f"Insufficient cash, cannot execute `prepare_seed`; current cash {self.cash:.2f}, requires {prepare_seed_total_cost:.2f}",
                )
            return None

        if not decision.library_id:
            return _rejection("missing_library_id", f"Action {decision.action} requires `library_id`")
        library = self._lookup_library(decision.seed_id, decision.library_id)
        if library is None:
            return _rejection("unknown_library", f"Unknown library: seed={decision.seed_id}, library={decision.library_id}")

        if decision.action == "prepare_library":
            if decision.seed_id not in self.visible_seed_ids:
                return _rejection("seed_not_visible", f"Cannot use an invisible seed {decision.seed_id} to execute `prepare_library`")
            if decision.seed_id not in self.prepared_seed_ids:
                return _rejection(
                    "seed_not_prepared",
                    f"Cannot use an unprepared prepared of seed {decision.seed_id} to execute `prepare_library`",
                )
            if decision.library_id in self.prepared_library_ids:
                return _rejection(
                    "library_already_prepared",
                    f"Library {decision.library_id} at seed {decision.seed_id} is already prepared state",
                )
            if not self._action_fits_remaining_days(self.config.prepare_library_days):
                return _rejection(
                    "time_insufficient",
                    (
                        f"Insufficient remaining time, cannot execute `prepare_library`; "
                        f"remaining {self._remaining_days():.4f} days, requires {self.config.prepare_library_days:.4f} days"
                    ),
                )
            prepare_library_total_cost = self._prepare_library_total_cost()
            if not self._can_afford_total(prepare_library_total_cost):
                return _rejection(
                    "cash_insufficient",
                    f"Insufficient cash, cannot execute `prepare_library`; current cash {self.cash:.2f}, requires {prepare_library_total_cost:.2f}",
                )
            return None

        if decision.action == "mine_library":
            if decision.seed_id not in self.visible_seed_ids:
                return _rejection("seed_not_visible", f"Cannot use an invisible seed {decision.seed_id} to execute `mine_library`")
            if decision.seed_id not in self.prepared_seed_ids:
                return _rejection("seed_not_prepared", f"Cannot use an unprepared prepared of seed {decision.seed_id} to execute `mine_library`")
            if decision.library_id not in self.prepared_library_ids:
                return _rejection(
                    "library_not_prepared",
                    f"Cannot use an unprepared prepared of library {decision.library_id} to execute `mine_library`",
                )
            if decision.batch_size is None or isinstance(decision.batch_size, bool):
                return _rejection("missing_batch_size", "`mine_library` requires an integer `batch_size`")
            _, _, remaining_screenable_sequence_count = self._known_library_counts(decision.library_id)
            if remaining_screenable_sequence_count <= 0:
                return _rejection("library_exhausted", f"Library {decision.library_id} is exhausted, cannot continue `mine_library`")
            legal_batch_sizes = [option.batch_size for option in self._legal_mine_options(decision.library_id)]
            if not legal_batch_sizes:
                structural_batch_sizes = [
                    option.batch_size for option in self.config.mine_batch_menu if option.batch_size <= remaining_screenable_sequence_count
                ]
                time_fitting_options = [
                    option
                    for option in self.config.mine_batch_menu
                    if option.batch_size <= remaining_screenable_sequence_count
                    and self._action_fits_remaining_days(option.time_days)
                ]
                if not structural_batch_sizes:
                    return _rejection(
                        "illegal_batch_size",
                        (
                            f"Library {decision.library_id} still has screenable sequences, "
                            f"but no legal `batch_size` matches the current "
                            f"`remaining_screenable_sequence_count={remaining_screenable_sequence_count}`"
                        ),
                    )
                if not time_fitting_options:
                    return _rejection(
                        "time_insufficient",
                        (
                            f"Insufficient remaining time, Cannot use {decision.seed_id}/{decision.library_id} to execute any legal `batch_size`; "
                            f"remaining {self._remaining_days():.4f} days"
                        ),
                    )
                cheapest_mine_total_cost = min(self._mine_total_cost(option) for option in time_fitting_options)
                return _rejection(
                    "cash_insufficient",
                    f"Insufficient cash, cannot execute `mine_library`; current cash {self.cash:.2f}, requires at least {cheapest_mine_total_cost:.2f}",
                )
            if decision.batch_size not in legal_batch_sizes:
                requested_time_fitting_option = next(
                    (
                        option
                        for option in self.config.mine_batch_menu
                        if option.batch_size == decision.batch_size
                        and option.batch_size <= remaining_screenable_sequence_count
                        and self._action_fits_remaining_days(option.time_days)
                    ),
                    None,
                )
                if requested_time_fitting_option is not None:
                    requested_total_cost = self._mine_total_cost(requested_time_fitting_option)
                    if not self._can_afford_total(requested_total_cost):
                        return _rejection(
                            "cash_insufficient",
                            (
                                f"Insufficient cash, cannot execute `mine_library`; "
                                f"current cash {self.cash:.2f}, requires {requested_total_cost:.2f}"
                            ),
                        )
                return _rejection(
                    "illegal_batch_size",
                    (
                        f"Illegal `batch_size={decision.batch_size}`; "
                        f"{decision.seed_id}/{decision.library_id} legal values are {legal_batch_sizes}"
                    ),
                )
            return None

        return _rejection("unsupported_action", f"Unsupported action {decision.action!r}")

    def validate_decision(self, decision: AgentDecision) -> str | None:
        detail = self.validate_decision_detail(decision)
        return detail.reason if detail is not None else None

    def record_rejection(self, decision: AgentDecision, detail: RejectionDetail | str) -> dict[str, Any]:
        rejection_detail = detail if isinstance(detail, RejectionDetail) else _rejection("runtime_error", detail)
        record = {
            "request_index": self.request_count,
            "action": decision.action,
            "seed_id": decision.seed_id,
            "library_id": decision.library_id,
            "batch_size": decision.batch_size,
            "screen_record_id": decision.screen_record_id,
            "skill_id": decision.skill_id,
            "reasoning": decision.reasoning,
            "rejection_code": rejection_detail.code,
            "rejection_reason": rejection_detail.reason,
            "cash_after": self.cash,
            "elapsed_day_after": self.day,
        }
        self.rejection_history.append(record)
        return record

    def _deduct(self, *, cash_cost: float, day_cost: float) -> None:
        if not self._action_fits_remaining_days(day_cost):
            raise ValueError(
                f"Insufficient remaining time; only remaining {self._remaining_days():.4f} days, "
                f"but this action requires {day_cost:.4f} days"
            )
        total = cash_cost + day_cost * self.config.daily_burn_cost
        if self.cash + 1e-9 < total:
            raise ValueError(f"Insufficient cash; current cash {self.cash:.2f}, total action cost {total:.2f}")
        self.cash -= total
        self.day += day_cost
        self.total_cost_paid += total

    def _award(self, reward: float) -> None:
        if reward <= 0.0:
            return
        self.cash += reward
        self.total_reward_earned += reward

    def apply(self, decision: AgentDecision) -> dict[str, Any]:
        if decision.action == "stop":
            self.stopped = True
            self.stop_reason = decision.reasoning or "agent_stop"
            self.stop_code = STOP_CODE_AGENT_STOP
            action_record = {
                "action": "stop",
                "reasoning": decision.reasoning,
                "stop_code": self.stop_code,
                "cash_after": self.cash,
                "elapsed_day_after": self.day,
            }
            self.action_history.append(action_record)
            return action_record

        if decision.action == "confirm_hit":
            if decision.screen_record_id is None:
                raise ValueError("`confirm_hit` requires `screen_record_id`")
            return self._confirm_hit(decision.screen_record_id, reasoning=decision.reasoning)

        if decision.action == "purchase_skill":
            if not decision.skill_id:
                raise ValueError("`purchase_skill` requires `skill_id`")
            return self._purchase_skill(decision.skill_id, reasoning=decision.reasoning)

        if not decision.seed_id:
            raise ValueError(f"Action {decision.action} requires `seed_id`")

        if decision.action == "prepare_seed":
            return self._prepare_seed(decision.seed_id, reasoning=decision.reasoning)

        if not decision.library_id:
            raise ValueError(f"Action {decision.action} requires `library_id`")

        if decision.action == "prepare_library":
            return self._prepare_library(decision.seed_id, decision.library_id, reasoning=decision.reasoning)
        if decision.action == "mine_library":
            if decision.batch_size is None:
                raise ValueError("`mine_library` requires `batch_size`")
            return self._mine_library(
                decision.seed_id,
                decision.library_id,
                batch_size=decision.batch_size,
                reasoning=decision.reasoning,
            )
        raise ValueError(f"Unsupported action {decision.action!r}")

    def _prepare_seed(self, seed_id: str, *, reasoning: str) -> dict[str, Any]:
        if seed_id not in self.visible_seed_ids:
            raise ValueError(f"Cannot `prepare_seed` invisible seed {seed_id}")
        if seed_id in self.prepared_seed_ids:
            raise ValueError(f"Seed {seed_id} is already prepared state")

        self._deduct(cash_cost=self.config.prepare_seed_cost, day_cost=self.config.prepare_seed_days)
        self.prepared_seed_ids.add(seed_id)
        record = {
            "action": "prepare_seed",
            "seed_id": seed_id,
            "reasoning": reasoning,
            "library_count": self.seeds[seed_id].library_count,
            "newly_visible_seed_ids": [],
            "cash_after": self.cash,
            "elapsed_day_after": self.day,
        }
        self.action_history.append(record)
        return record

    def _prepare_library(self, seed_id: str, library_id: str, *, reasoning: str) -> dict[str, Any]:
        library = self._lookup_library(seed_id, library_id)
        if library is None:
            raise ValueError(f"Unknown library: seed={seed_id}, library={library_id}")
        if seed_id not in self.visible_seed_ids:
            raise ValueError(f"Cannot use an invisible seed {seed_id} to execute `prepare_library`")
        if seed_id not in self.prepared_seed_ids:
            raise ValueError(f"Cannot use an unprepared prepared of seed {seed_id} to execute `prepare_library`")
        if library_id in self.prepared_library_ids:
            raise ValueError(f"Library {library_id} at seed {seed_id} is already prepared state")

        self._deduct(cash_cost=self.config.prepare_library_cost, day_cost=self.config.prepare_library_days)
        self.prepared_library_ids.add(library_id)
        newly_visible_seed_ids = self._reveal_child_seeds_of_library(library_id)
        screenable_sequence_count, _, _ = self._known_library_counts(library_id)
        record = {
            "action": "prepare_library",
            "seed_id": seed_id,
            "library_id": library_id,
            "reasoning": reasoning,
            "screenable_sequence_count": screenable_sequence_count,
            "newly_visible_seed_ids": newly_visible_seed_ids,
            "cash_after": self.cash,
            "elapsed_day_after": self.day,
        }
        self.action_history.append(record)
        return record

    def _mine_library(self, seed_id: str, library_id: str, *, batch_size: int, reasoning: str) -> dict[str, Any]:
        library = self._lookup_library(seed_id, library_id)
        if library is None:
            raise ValueError(f"Unknown library: seed={seed_id}, library={library_id}")
        if seed_id not in self.visible_seed_ids:
            raise ValueError(f"Cannot use an invisible seed {seed_id} to execute `mine_library`")
        if seed_id not in self.prepared_seed_ids:
            raise ValueError(f"Cannot use an unprepared prepared of seed {seed_id} to execute `mine_library`")
        if library_id not in self.prepared_library_ids:
            raise ValueError(f"Cannot use an unprepared prepared of library {library_id} to execute `mine_library`")

        legal_options = {option.batch_size: option for option in self._legal_mine_options(library_id)}
        option = legal_options.get(batch_size)
        if option is None:
            raise ValueError(
                f"Illegal `batch_size={batch_size}`; {seed_id}/{library_id} legal values are {sorted(legal_options)}"
            )

        self._deduct(cash_cost=option.cost, day_cost=option.time_days)

        entries = self.screenable_sequences_by_library[library_id][self.next_index[library_id] : self.next_index[library_id] + batch_size]
        self.next_index[library_id] += batch_size

        best_update: dict[str, Any] | None = None
        pending_confirmation_added: dict[str, Any] | None = None
        new_sequence_count = 0
        batch_best_new_screen_record_id: int | None = None
        batch_best_new_activity = float("-inf")
        for entry in entries:
            sequence = entry["sequence"]
            true_activity = float(entry.get("true_activity", entry.get("activity")))
            observed_activity, noise_fraction = self._observed_activity(sequence, true_activity)
            is_new = sequence not in self.discovered_sequences
            if is_new:
                self.discovered_sequences.add(sequence)
                new_sequence_count += 1

            screen_record_id = len(self.screen_record_history)
            if observed_activity > self.provisional_best_activity:
                self.provisional_best_activity = observed_activity
                self.provisional_best_sequence = sequence
                best_update = {
                    "screen_record_id": screen_record_id,
                    "sequence": sequence,
                    "observed_activity": observed_activity,
                }

            self.screen_record_history.append(
                {
                    "screen_record_id": screen_record_id,
                    "seed_id": seed_id,
                    "library_id": library_id,
                    "sequence": sequence,
                    "true_activity": true_activity,
                    "observed_activity": observed_activity,
                    "noise_fraction": noise_fraction,
                    "is_new_sequence": is_new,
                    "is_confirmed": False,
                }
            )

            if (
                is_new
                and observed_activity > self.confirmed_best_activity + 1e-9
                and observed_activity > batch_best_new_activity
            ):
                batch_best_new_screen_record_id = screen_record_id
                batch_best_new_activity = observed_activity

        if (
            batch_best_new_screen_record_id is not None
            and batch_best_new_screen_record_id not in self.confirmed_screen_record_ids
            and batch_best_new_screen_record_id not in self.pending_confirmation_screen_record_ids
        ):
            self.pending_confirmation_screen_record_ids.append(batch_best_new_screen_record_id)
            pending_confirmation_added = self._pending_confirmation_payload(
                batch_best_new_screen_record_id,
                legal_now=self._action_fits_remaining_days(self.config.confirm_hit_days)
                and self._can_afford_total(self._confirm_total_cost()),
            )

        reward_earned = 0.0
        record = {
            "action": "mine_library",
            "seed_id": seed_id,
            "library_id": library_id,
            "requested_batch_size": batch_size,
            "actual_batch_size": batch_size,
            "new_sequence_count": new_sequence_count,
            "best_update": best_update,
            "pending_confirmation_added": pending_confirmation_added,
            "reward_earned": reward_earned,
            "cash_after": self.cash,
            "elapsed_day_after": self.day,
            "reasoning": reasoning,
        }
        self.action_history.append(record)
        return record

    def _confirm_hit(self, screen_record_id: int, *, reasoning: str) -> dict[str, Any]:
        screen_record = self._screen_record_by_id(screen_record_id)
        if screen_record is None:
            raise ValueError(f"Unknown `screen_record_id`: {screen_record_id}")
        if screen_record_id in self.confirmed_screen_record_ids:
            raise ValueError(f"Screen record {screen_record_id} already confirmed")

        self._prune_pending_confirmations()
        if screen_record_id not in self.pending_confirmation_screen_record_ids:
            raise ValueError(f"Screen record {screen_record_id} is not currently pending confirmation")

        self._deduct(cash_cost=self.config.confirm_hit_cost, day_cost=self.config.confirm_hit_days)

        self.pending_confirmation_screen_record_ids = [item for item in self.pending_confirmation_screen_record_ids if item != screen_record_id]
        self.confirmed_screen_record_ids.add(screen_record_id)
        screen_record["is_confirmed"] = True

        previous_best = self.confirmed_best_activity
        observed_activity = self._screen_record_observed_activity(screen_record)
        confirmed_activity = self._screen_record_true_activity(screen_record)
        sequence = str(screen_record["sequence"])
        screen_record["confirmed_activity"] = confirmed_activity
        best_update: dict[str, Any] | None = None
        if confirmed_activity > self.confirmed_best_activity:
            self.confirmed_best_activity = confirmed_activity
            self.confirmed_best_sequence = sequence
            best_update = {
                "screen_record_id": screen_record_id,
                "sequence": sequence,
                "confirmed_activity": confirmed_activity,
            }
            screen_record["is_confirmed_best"] = True

        reward_earned = self._reward_for_improvement(previous_best, self.confirmed_best_activity)
        self._award(reward_earned)
        self._prune_pending_confirmations()
        record = {
            "action": "confirm_hit",
            "screen_record_id": screen_record_id,
            "seed_id": screen_record["seed_id"],
            "library_id": screen_record["library_id"],
            "sequence": sequence,
            "observed_activity": observed_activity,
            "confirmed_activity": confirmed_activity,
            "noise_fraction": float(screen_record.get("noise_fraction", 0.0)),
            "confirmed_best_before": previous_best,
            "confirmed_best_after": self.confirmed_best_activity,
            "best_update": best_update,
            "reward_earned": reward_earned,
            "pending_confirmation_count": len(self.pending_confirmation_screen_record_ids),
            "cash_after": self.cash,
            "elapsed_day_after": self.day,
            "reasoning": reasoning,
        }
        self.action_history.append(record)
        return record

    def _purchase_skill(self, skill_id: str, *, reasoning: str) -> dict[str, Any]:
        entry = self.skill_market.entry(skill_id)
        if entry is None:
            raise ValueError(f"Unknown `skill_id`: {skill_id}")
        if skill_id in self.owned_skill_ids:
            raise ValueError(f"Skill {skill_id} already purchased")

        self._deduct(cash_cost=entry.price, day_cost=self.config.purchase_skill_days)
        self.owned_skill_ids.add(skill_id)
        self.total_skill_purchase_price_paid += entry.price
        record = {
            "action": "purchase_skill",
            "skill_id": skill_id,
            "display_name": entry.display_name,
            "purchase_price": entry.price,
            "time_days": self.config.purchase_skill_days,
            "burn_cost": self.config.purchase_skill_days * self.config.daily_burn_cost,
            "workspace_relative_source_dir": f"owned_skills/{skill_id}/source",
            "cash_after": self.cash,
            "elapsed_day_after": self.day,
            "reasoning": reasoning,
        }
        self.action_history.append(record)
        return record

    def summary(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "cash_final": self.cash,
            "cost_paid_total": self.total_cost_paid,
            "reward_earned_total": self.total_reward_earned,
            "day_final": self.day,
            "max_experiment_days": self.config.max_experiment_days,
            "remaining_days_final": self._remaining_days(),
            "confirmed_best_activity": self.confirmed_best_activity,
            "confirmed_best_sequence": self.confirmed_best_sequence,
            "provisional_best_activity": self.provisional_best_activity,
            "provisional_best_sequence": self.provisional_best_sequence,
            "baseline_activity": self.config.baseline_activity,
            "visible_seed_ids": sorted(self.visible_seed_ids, key=seed_sort_key),
            "prepared_seed_ids": sorted(self.prepared_seed_ids, key=seed_sort_key),
            "prepared_library_count": len(self.prepared_library_ids),
            "prepared_library_ids": sorted(self.prepared_library_ids, key=library_sort_key),
            "screen_record_count": len(self.screen_record_history),
            "confirmed_screen_record_count": len(self.confirmed_screen_record_ids),
            "pending_confirmation_count": len(self._pending_confirmation_payloads()),
            "skill_market_enabled": self.config.skill_market_enabled,
            "skill_market_size": self._visible_skill_market_size(),
            "skill_catalog_size": len(self.skill_market.entries_by_id),
            "initial_owned_skill_ids": list(self.config.initial_owned_skill_ids),
            "owned_skill_count": len(self.owned_skill_ids),
            "owned_skill_ids": self._ordered_owned_skill_ids(),
            "total_skill_purchase_price_paid": self.total_skill_purchase_price_paid,
            "action_count": len(self.action_history),
            "rejected_action_count": len(self.rejection_history),
            "request_count": self.request_count,
            "stop_reason": self.stop_reason,
            "stop_code": self.stop_code,
            "mined_batch_count": sum(1 for record in self.action_history if record["action"] == "mine_library"),
            "max_seed_depth_reached": max((seed_sort_key(seed_id) for seed_id in self.visible_seed_ids), default=0),
            "screening_noise_enabled": self.config.screening_noise_enabled,
            "screening_noise_sigma": self.config.screening_noise_sigma,
            "screening_noise_clip": self.config.screening_noise_clip,
        }

    def dumps_seed_manifest(self, *, include_start_sequence: bool = True) -> str:
        payload = []
        for seed_id in sorted(self.seeds, key=seed_sort_key):
            definition = self.seeds[seed_id]
            item = {
                "seed_id": definition.seed_id,
                "parent_seed_candidate_ids": list(definition.parent_seed_candidates),
                "parent_library_candidate_ids": list(definition.parent_library_candidates),
                "parent_library_id": definition.parent_library_id,
                "child_seed_ids": list(self.child_seed_ids_by_parent_seed.get(seed_id, ())),
                "historical_round_ids": list(definition.historical_round_ids),
                "parent_resolution_evidence": definition.parent_resolution_evidence,
                "libraries": [
                    {
                        "library_id": library.library_id,
                        "raw_domain_id": library.raw_domain_id,
                        "historical_round_id": library.historical_round_id,
                        "mutable_interval_aa": _mutable_interval_aa_payload(library),
                        "interval_length_aa": library.interval_length_aa,
                        "screenable_sequence_count": len(self.screenable_sequences_by_library[library.library_id]),
                        "hidden_order_seed": _stable_seed(self.config.seed, library.key),
                        "unlock_child_seed_ids": list(library.unlock_child_seed_ids),
                    }
                    for library in sorted(definition.libraries, key=lambda entry: library_sort_key(entry.library_id))
                ],
            }
            if include_start_sequence:
                item["start_sequence"] = definition.start_sequence
            payload.append(item)
        return json.dumps(payload, ensure_ascii=False, indent=2)

"""Shared judge/session logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..data import load_prepared_table, load_seed_definitions
from ..environment import RejectionDetail, TadACampaignEnvironment
from ..models import AgentDecision, ExperimentConfig
from ..skill_market import SkillMarketCatalog, empty_skill_market, load_skill_market_catalog


@dataclass(frozen=True)
class BenchmarkAssets:
    seed_definitions: list[Any]
    prepared_rows: list[dict[str, Any]]
    skill_market: SkillMarketCatalog


def _prepared_rows(table) -> list[dict[str, Any]]:
    sequences = table["sequence"].to_pylist()
    activity_column = "activity" if "activity" in table.column_names else "value"
    activities = table[activity_column].to_pylist()
    round_ids = table["round_ids"].to_pylist()
    return [
        {"sequence": sequence, "activity": float(activity), "round_ids": list(memberships)}
        for sequence, activity, memberships in zip(sequences, activities, round_ids, strict=True)
    ]


def load_benchmark_assets(config: ExperimentConfig) -> BenchmarkAssets:
    prepared_table = load_prepared_table(config.prepared_root, config.variant)
    seed_definitions = load_seed_definitions(config.prepared_root, config.variant)
    return BenchmarkAssets(
        seed_definitions=seed_definitions,
        prepared_rows=_prepared_rows(prepared_table),
        skill_market=load_skill_market_catalog(config.skill_market_manifest)
        if config.skill_market_manifest is not None
        else empty_skill_market(),
    )


def build_environment(config: ExperimentConfig, assets: BenchmarkAssets) -> TadACampaignEnvironment:
    return TadACampaignEnvironment(
        variant=config.variant,
        seed_definitions=assets.seed_definitions,
        prepared_rows=assets.prepared_rows,
        config=config,
        skill_market=assets.skill_market,
    )


def build_workspace_snapshot(env: TadACampaignEnvironment) -> dict[str, Any]:
    if env.config.skill_market_enabled:
        market_snapshot = env.skill_market.market_snapshot(
            owned_skill_ids=set(env.owned_skill_ids),
            purchase_days=env.config.purchase_skill_days,
            daily_burn_cost=env.config.daily_burn_cost,
        )
    else:
        market_snapshot = {
            "market_enabled": False,
            "market_size": 0,
            "purchase_skill_days": env.config.purchase_skill_days,
            "cards": [],
        }
    return {
        "state": env.get_state(),
        "history": env.get_history(),
        "available_actions": env.available_actions(),
        "skill_market": market_snapshot,
        "owned_skills_index": env.skill_market.owned_skills_snapshot(owned_skill_ids=set(env.owned_skill_ids)),
    }


def build_initial_response(env: TadACampaignEnvironment) -> dict[str, Any]:
    terminal = env.is_terminal()
    response: dict[str, Any] = {
        "terminal": terminal,
        "observation": build_workspace_snapshot(env),
    }
    if terminal:
        response["summary"] = env.summary()
    return response


def process_decision(
    env: TadACampaignEnvironment,
    decision: AgentDecision,
) -> dict[str, Any]:
    env.submitted_request_count += 1
    step_index = env.request_count
    rejection_detail = env.validate_decision_detail(decision)
    rejection_reason = rejection_detail.reason if rejection_detail is not None else None
    rejection_code = rejection_detail.code if rejection_detail is not None else None

    action_record: dict[str, Any] | None = None
    rejection_record: dict[str, Any] | None = None
    accepted = rejection_detail is None
    if accepted:
        try:
            action_record = env.apply(decision)
        except ValueError as exc:
            accepted = False
            rejection_reason = str(exc)
            rejection_detail = RejectionDetail(code="runtime_error", reason=rejection_reason)
            rejection_code = rejection_detail.code
            rejection_record = env.record_rejection(decision, rejection_detail)
    else:
        rejection_record = env.record_rejection(decision, rejection_detail)

    terminal = env.is_terminal()
    response: dict[str, Any] = {
        "step_index": step_index,
        "decision": decision.__dict__,
        "accepted": accepted,
        "rejection_code": rejection_code,
        "rejection_reason": rejection_reason,
        "action_record": action_record,
        "rejection_record": rejection_record,
        "terminal": terminal,
        "stop_code": env.stop_code,
        "stop_reason": env.stop_reason,
        "action_count": len(env.action_history),
        "request_count": env.request_count,
        "rejected_action_count": len(env.rejection_history),
        "cash": env.cash,
        "day": env.day,
        "confirmed_best_activity": env.confirmed_best_activity,
        "confirmed_best_sequence": env.confirmed_best_sequence,
        "provisional_best_activity": env.provisional_best_activity,
        "provisional_best_sequence": env.provisional_best_sequence,
        "pending_confirmation_count": len(env.pending_confirmation_screen_record_ids),
        "owned_skill_count": len(env.owned_skill_ids),
        "observation": build_workspace_snapshot(env),
    }
    if terminal:
        response["summary"] = env.summary()
    return response

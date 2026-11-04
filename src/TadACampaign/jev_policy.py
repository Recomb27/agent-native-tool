"""JEV Choice adapter over TadA-Campaign's public observation and legal actions."""

from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .biology import translate_dna_to_protein
from .models import AgentDecision, ExperimentConfig


GATEWAY_BASE_URL = ""
GATEWAY_MODEL = "typesafe-ai/jev"
COMPACT_CHOICE_MODELS = frozenset(
    {
        "convaiinnovations/laya",
        "convaiinnovations/laya-free",
        "ateve-jev-v1",
        "bocha-jev-v1",
    }
)
LAYA_COMPACT_BIOLOGY_HINT = (
    "Biology hint: TadA directed evolution uses seed backgrounds and mutational libraries. "
    "Descendant branches have higher but uncertain upside. Mining costs cash and time and pays no reward; "
    "only confirm_hit pays. Preserve enough cash and days for a mine-then-confirm path."
)


@dataclass
class JEVPolicyRuntime:
    client: Any
    model: str
    k: int
    packaging: str
    hierarchical: bool = False
    sampling: bool = False
    include_biology_context: bool = True
    biology_context_mode: str = "legacy"
    shared_biology_context: str | None = None
    traces: list[dict[str, Any]] = field(default_factory=list)
    campaign_actions: list[dict[str, Any]] = field(default_factory=list)
    screen_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    seen_screen_ids: set[int] = field(default_factory=set)
    calls: int = 0
    estimated_cost: float = 0.0

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_k(policy_name: str, params: dict[str, Any]) -> int:
    default = 1
    try:
        k = int(params.get("k", default))
    except (TypeError, ValueError) as exc:
        raise ValueError("JEV-family policy parameter 'k' must be an integer") from exc
    if not 1 <= k <= 2:
        raise ValueError("JEV-family policy parameter 'k' must be between 1 and 2")
    return k


def _gateway_model(model: str) -> str:
    """Translate the legacy Jev alias; other Vercel slugs pass through unchanged."""
    if model in {"jev", "jev-latest", "typesafe-ai/jev"}:
        return GATEWAY_MODEL
    return model


def _policy_bool(params: dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def _include_biology_context(config: ExperimentConfig) -> bool:
    return _policy_bool(config.agent_policy_params, "include_biology_context", True)


def _biology_context_mode(config: ExperimentConfig) -> str:
    if not _include_biology_context(config):
        return "none"
    mode = str(config.agent_policy_params.get("biology_context_mode", "legacy")).strip().lower()
    if mode not in {"legacy", "agent_shared"}:
        raise ValueError("JEV biology_context_mode must be 'legacy' or 'agent_shared'")
    return mode


@lru_cache(maxsize=1)
def _load_agent_shared_biology_context() -> str:
    """Load the exact biology context exposed to the file-oriented agent."""
    source = Path(__file__).resolve().parents[2] / "docs" / "biology_context.md"
    if not source.is_file():
        raise RuntimeError(f"Shared agent biology context is missing: {source}")
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"Shared agent biology context is empty: {source}")
    return text


def _default_model_for_policy(policy_name: str) -> str | None:
    if policy_name in {"jev_choice", "jev_k1"}:
        return GATEWAY_MODEL
    if policy_name == "d1_k1":
        return "liquid/d1"
    if policy_name == "laya_k1":
        return "convaiinnovations/laya-free"
    return None


def _get_runtime(memory: Any, config: ExperimentConfig, policy_name: str) -> JEVPolicyRuntime:
    runtime = getattr(memory, "jev_runtime", None)
    if runtime is not None:
        return runtime

    gateway_key = os.environ.get("AI_GATEWAY_API_KEY", "").strip()
    direct_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not gateway_key and not direct_key:
        raise RuntimeError(
            "JEV requires AI_GATEWAY_API_KEY (Vercel AI Gateway) or TYPESAFE_API_KEY "
            "(direct TypeSafe API). The public runner reads AI_GATEWAY_API_KEY from "
            "the file selected by TADACAMPAIGN_JEV_KEY_FILE when configured."
        )
    try:
        from typesafe_sdk import TypeSafeClient
    except ImportError as exc:
        raise RuntimeError("typesafe-sdk is not installed; run 'uv sync' in TadA-Campaign-jev") from exc

    params = config.agent_policy_params
    configured_model = str(
        params.get("model") or config.agent_model or _default_model_for_policy(policy_name) or ""
    ).strip()
    if not configured_model:
        raise ValueError("JEV-family policy requires [agent.policy_params].model")
    timeout = float(params.get("timeout_seconds", 60.0))
    if gateway_key:
        # Vercel's TypeSafe-compatible endpoint preserves the Choice/Score/Noul
        # wire format but requires the Gateway model slug and Gateway credential.
        model = _gateway_model(configured_model)
        base_url = os.environ.get("JEV_BASE_URL", GATEWAY_BASE_URL).strip().rstrip("/")
        if not base_url:
            raise RuntimeError("JEV_BASE_URL must be set when AI_GATEWAY_API_KEY is used")
        client = TypeSafeClient(
            api_key=gateway_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
    else:
        model = configured_model
        client = TypeSafeClient(api_key=direct_key, model=model, timeout=timeout)
    packaging = str(params.get("packaging", "compact"))
    if packaging not in {"compact", "full-history", "typed-fields"}:
        raise ValueError("JEV packaging must be compact, full-history, or typed-fields")
    hierarchical = _policy_bool(params, "hierarchical", False)
    sampling = _policy_bool(params, "sampling", False)
    include_biology_context = _policy_bool(params, "include_biology_context", True)
    mode = str(params.get("biology_context_mode", "legacy")).strip().lower()
    if not include_biology_context:
        mode = "none"
    elif mode not in {"legacy", "agent_shared"}:
        raise ValueError("JEV biology_context_mode must be 'legacy' or 'agent_shared'")
    shared_context = _load_agent_shared_biology_context() if mode == "agent_shared" else None
    runtime = JEVPolicyRuntime(
        client,
        model,
        _get_k(policy_name, params),
        packaging,
        hierarchical=hierarchical,
        sampling=sampling,
        include_biology_context=include_biology_context,
        biology_context_mode=mode,
        shared_biology_context=shared_context,
    )
    memory.jev_runtime = runtime
    return runtime


def _compact_state(observation: dict[str, Any]) -> dict[str, Any]:
    state = observation.get("state", {})
    seeds: list[dict[str, Any]] = []
    for seed in state.get("seeds", []):
        if not isinstance(seed, dict):
            continue
        libraries = [item for item in seed.get("libraries", []) if isinstance(item, dict)]
        protein = ""
        start_sequence = seed.get("start_sequence")
        if isinstance(start_sequence, str) and start_sequence:
            try:
                protein = translate_dna_to_protein(start_sequence)
            except ValueError:
                protein = ""
        library_payloads: list[dict[str, Any]] = []
        for item in libraries:
            interval = item.get("mutable_interval_aa")
            start_aa = (
                int(interval["start_index_0based_inclusive"])
                if isinstance(interval, dict) and "start_index_0based_inclusive" in interval
                else None
            )
            end_aa = (
                int(interval["end_index_0based_exclusive"])
                if isinstance(interval, dict) and "end_index_0based_exclusive" in interval
                else None
            )
            subsequence = protein[start_aa:end_aa] if protein and start_aa is not None and end_aa is not None else None
            library_payloads.append(
                {
                    "library_id": item.get("library_id"),
                    "status": item.get("library_status"),
                    "mutable_interval_aa": interval,
                    "interval_length_aa": item.get("interval_length_aa"),
                    "window_subsequence": subsequence,
                }
            )
        seeds.append(
            {
                "seed_id": seed.get("seed_id"),
                "status": seed.get("seed_status"),
                "library_count": len(libraries),
                "protein_sequence": protein or None,
                "libraries": library_payloads,
            }
        )
    return {
        "day": _as_float(state.get("elapsed_day")),
        "remaining_days": _as_float(state.get("remaining_days")),
        "current_cash": _as_float(state.get("current_cash")),
        "total_cost_paid": _as_float(state.get("total_cost_paid")),
        "total_reward_earned": _as_float(state.get("total_reward_earned")),
        "confirmed_best_activity": _as_float(state.get("confirmed_best_activity"), 1.0),
        "provisional_best_activity": _as_float(state.get("provisional_best_activity")),
        "pending_confirmation_count": int(state.get("pending_confirmation_count", 0) or 0),
        "action_count": int(state.get("action_count", 0) or 0),
        "economic_summary": state.get("economic_summary", {}),
        "seeds": seeds,
    }


def _history(observation: dict[str, Any]) -> list[dict[str, Any]]:
    raw = observation.get("history", {}).get("recent_actions", [])
    keys = (
        "action",
        "seed_id",
        "library_id",
        "requested_batch_size",
        "newly_visible_seed_ids",
        "best_update",
        "reward_earned",
        "pending_confirmation_added",
        "cash_after",
        "elapsed_day_after",
        "stop_code",
    )
    result: list[dict[str, Any]] = []
    for item in raw[-12:]:
        if not isinstance(item, dict):
            continue
        result.append({key: item.get(key) for key in keys})
    return result


def _candidates(observation: dict[str, Any]) -> list[dict[str, Any]]:
    raw = observation.get("history", {}).get("recent_screen_records", [])
    keys = (
        "screen_record_id",
        "seed_id",
        "library_id",
        "observed_activity",
        "is_confirmed",
        "noise_pending",
        "sequence_length",
    )
    result: list[dict[str, Any]] = []
    for item in raw[-20:]:
        if not isinstance(item, dict):
            continue
        result.append({key: item.get(key) for key in keys})
    return result


def _reward_tier_rows(config: ExperimentConfig) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tier in config.reward_tiers:
        max_activity: Any = "inf" if tier.max_activity == float("inf") else tier.max_activity
        rows.append(
            {
                "min_activity": tier.min_activity,
                "max_activity": max_activity,
                "multiplier": tier.multiplier,
            }
        )
    return rows


def _scenario_brief(config: ExperimentConfig, *, include_biology_context: bool = True) -> str:
    """Natural-language task brief so JEV understands the scenario, not just numbers."""
    tier_texts = []
    for tier in config.reward_tiers:
        upper = "inf" if tier.max_activity == float("inf") else f"{tier.max_activity:g}"
        tier_texts.append(f"activity {tier.min_activity:g}..{upper} pays multiplier {tier.multiplier:g}")
    tiers = "; ".join(tier_texts)
    batches = ", ".join(
        f"{option.batch_size} variants for ${option.cost:,.0f} and {option.time_days:g} days"
        for option in config.mine_batch_menu
    )
    confirm_total = config.confirm_hit_cost + config.confirm_hit_days * config.daily_burn_cost
    if include_biology_context:
        scenario_intro = (
            "You are running a budgeted, time-limited protein directed-evolution campaign on a TadA library, "
            "abstracted as a sequential decision game."
        )
        branch_guidance = (
            "Evolution-lineage information is also decision-relevant: a downstream seed is a later background "
            "derived from an improved variant, so it often has higher upside potential than its ancestor, although "
            "this is a prior rather than a guarantee. When a newly revealed downstream seed is visible and you can "
            "afford the full prepare -> mine -> confirm path, prioritize evaluating that branch before repeatedly "
            "mining an old library without strong evidence; do not spend the whole budget on shallow probes."
        )
    else:
        scenario_intro = (
            "You are solving a budgeted, time-limited sequential decision game. Treat seed and library identifiers "
            "as opaque handles and use only the public state, legal actions, observed readings, and action history."
        )
        branch_guidance = (
            "Do not assume that any unseen seed, library, or action has favorable properties. Choose among the "
            "currently legal options using only the public numerical state, costs, remaining time, and observed "
            "evidence; preserve a complete mine -> confirm path whenever it is economically plausible."
        )
    return (
        f"{scenario_intro} Your only scoring goal is to maximize final cash "
        f"(cash_final). You start with ${config.initial_cash:,.0f} and a horizon of {config.max_experiment_days:g} days. "
        f"Each elapsed day burns ${config.daily_burn_cost:,.0f} on top of every action's own cash and time cost, "
        f"so time is money and long actions erode the remaining window. "
        f"The world is partially observable and reveals itself gradually: you begin with one visible seed, and "
        f"preparing libraries can reveal new seeds and libraries. You may only act on seeds and libraries that "
        f"are currently visible and prepared. "
        f"The only way to earn reward is confirm_hit. Mining is a paid, risky screening probe: it consumes "
        f"cash and days regardless of whether it finds anything, returns observed_activity (a noisy estimate "
        f"of true activity), and pays nothing by itself. If the true activity is at or below the baseline "
        f"activity, the experiment produces no improvement; even an activity that does not exceed your current "
        f"confirmed_best_activity gives zero reward when confirmed. By contrast, an activity above the current "
        f"best can create a large payoff, and reward grows nonlinearly with the size of the improvement on "
        f"the increasing-marginal-value schedule: {tiers}. Therefore evaluate every mine as downside risk "
        f"versus upside, not as guaranteed progress. A valuable discovery usually requires the closing sequence "
        f"prepare/open -> mine_library (noisy probe) -> confirm_hit (settle reward). Always keep enough cash "
        f"and days to confirm a promising hit; a hit you cannot afford to confirm is worthless. The mine batch "
        f"menu is: {batches}. "
        f"{branch_guidance} "
        f"Confirming costs ${config.confirm_hit_cost:,.0f} plus {config.confirm_hit_days:g} days of burn, so a "
        f"mine-then-confirm loop needs roughly ${confirm_total:,.0f} of reserve after the mine. "
        f"Screening reads are noisy: observed_activity is a truncated-normal reading (sigma={config.screening_noise_sigma:g}, "
        f"clip={config.screening_noise_clip:g}); confirmed_activity is ground truth. A high observed_activity can "
        f"overstate or understate the true value. "
        f"If no remaining action can plausibly improve final cash, or you can no longer afford a mine->confirm "
        f"loop, you should stop and preserve your current cash."
    )


def _task_context(config: ExperimentConfig) -> dict[str, Any]:
    """Public task background: objective, reward schedule, costs, action semantics.

    Everything here comes from the public experiment config / agent playbook, never from
    hidden benchmark data (true activities, noise seed, settlement internals).
    """
    biology_mode = _biology_context_mode(config)
    context = {
        "brief": _scenario_brief(config, include_biology_context=biology_mode == "legacy"),
        "biology_context_mode": biology_mode,
        "biology": {
            "campaign": (
                "Directed evolution of TadA (tRNA adenosine deaminase), the enzyme used in adenine "
                "base editors (ABE). Researchers iteratively mutate a background enzyme, screen variants "
                "for activity, and keep the best variants as the background for the next round."
            ),
            "seed_meaning": (
                "A seed is a background protein (parent) sequence that becomes available as the campaign "
                "progresses. A newly revealed downstream seed is derived from an improved variant and "
                "therefore has higher expected activity upside than its ancestor, but it is not guaranteed "
                "to win."
            ),
            "library_meaning": (
                "A library is one mutational window on a background: all variants that differ from the "
                "background only inside a specific amino-acid interval. The window is reported as "
                "mutable_interval_aa (0-based start/end positions on the ~167-aa enzyme)."
            ),
            "mining_meaning": (
                "Mining screens a batch of variants from that window and returns noisy observed_activity. "
                "Confirming settles the true activity and any reward."
            ),
            "directed_evolution_loop": (
                "Directed evolution proceeds by rounds. Each round mutates one window of the current "
                "background (a library) and screens it. A round whose variants show improved activity overall "
                "becomes the basis for the next round: its best variants form the next background (a new seed). "
                "In this benchmark the campaign's lineage is encoded as a fixed structure — preparing a library "
                "(building a mutational window) can reveal a downstream seed, meaning that window was the "
                "historical fork that led to that descendant background. You do not re-discover the lineage by "
                "screening; opening the window exposes it. Because a descendant background was produced from an "
                "improved variant, it carries a positive prior for higher activity potential than its ancestor, "
                "but it is not guaranteed to win. So each step weighs two things: mining a window to find a hit "
                "you can confirm (direct value), versus preparing windows to open descendant lineages (branch "
                "value), with branch value deserving priority when the full confirmation runway remains affordable."
            ),
            "reveal_semantics": (
                "When you prepare a library and new seeds appear (newly_visible_seed_ids), this is a positive "
                "branch signal: those descendant backgrounds often have better activity potential because they "
                "come later in an improved directed-evolution lineage. This is a prior, not a guarantee, and "
                "screening is still noisy. Treat newly revealed seeds as high-value leads: if the budget leaves "
                "a complete prepare -> mine -> confirm runway, prepare and screen them before repeatedly mining "
                "an older library; do not ignore a revealed descendant."
            ),
            "risk_and_branch_guidance": (
                "Mining is a paid noisy experiment with downside: it burns cash and days even when all observed "
                "activities are below baseline, and a confirm does not pay unless confirmed_activity exceeds the "
                "current confirmed best. High activity above the current best has disproportionately larger upside. "
                "A newly revealed descendant seed is a later background derived from an improved variant and "
                "therefore has a higher prior for upside than the ancestor, though not a guarantee. When a "
                "complete prepare -> mine -> confirm path is affordable, prefer testing that newly revealed branch "
                "over repeated old-library mining without strong observed evidence."
            ),
            "exploration_feedback": (
                "Base your next decision on the activity data you have already observed. The "
                "library_mining_status block shows, for each library, how many times it has been mined and the "
                "average and maximum observed_activity of its screened variants. Reason from these numbers: a "
                "library whose observed activities are consistently far below baseline (1.0) has shown no sign "
                "of improvement and is unlikely to suddenly produce a hit; mining it again still burns cash and "
                "days and has substantial downside risk. A library with relatively higher observed activity is "
                "more promising and may be worth screening again with a larger batch, but compare that local "
                "evidence against the higher-upside prior of a newly revealed descendant. Use risk-adjusted "
                "expected upside, confirmation runway, and branch reachability to decide whether to mine an "
                "existing library, open a new window, prepare a revealed seed, or stop."
            ),
        },
        "objective": (
            "Maximize final cash (cash_final). Reward is settled only by confirm_hit: if a "
            "confirmed activity exceeds the current confirmed_best_activity, reward is added "
            "using the reward tiers below. mine_library only yields noisy observed_activity and "
            "never pays reward directly. stop ends the campaign now and keeps the current cash. "
            "Every action also burns cash and days, so preserve a mine->confirm closing path when possible."
        ),
        "reward_tiers": _reward_tier_rows(config),
        "reward_formula": (
            "reward for improving confirmed best from lower to upper = "
            "base_asset_value * baseline_activity * tier.multiplier * (1/lower - 1/upper), "
            "summed over every crossed tier. A confirm that does not beat the current best pays 0."
        ),
        "base_asset_value": config.base_asset_value,
        "baseline_activity": config.baseline_activity,
        "costs": {
            "prepare_seed": {"cash": config.prepare_seed_cost, "days": config.prepare_seed_days},
            "prepare_library": {"cash": config.prepare_library_cost, "days": config.prepare_library_days},
            "confirm_hit": {"cash": config.confirm_hit_cost, "days": config.confirm_hit_days},
            "daily_burn_cost": config.daily_burn_cost,
        },
        "mine_batch_menu": [
            {"batch_size": option.batch_size, "cash": option.cost, "days": option.time_days}
            for option in config.mine_batch_menu
        ],
        "max_experiment_days": config.max_experiment_days,
        "initial_cash": config.initial_cash,
        "screening_noise": {
            "enabled": config.screening_noise_enabled,
            "sigma": config.screening_noise_sigma,
            "clip": config.screening_noise_clip,
        },
        "action_semantics": {
            "prepare_seed": "make a visible seed prepared, unlocking its libraries",
            "prepare_library": "open a library for mining; may reveal new seeds",
            "mine_library": "screen a batch and return noisy observed_activity; no reward",
            "confirm_hit": "validate a screen record; the only action that settles reward",
            "purchase_skill": "buy a public BioSkill for later decision support",
            "stop": "end now and keep the current cash",
        },
    }
    if biology_mode != "legacy":
        context.pop("biology", None)
    return context


ACTION_RECORD_KEYS = (
    "action",
    "seed_id",
    "library_id",
    "requested_batch_size",
    "actual_batch_size",
    "newly_visible_seed_ids",
    "best_update",
    "pending_confirmation_added",
    "reward_earned",
    "cash_after",
    "elapsed_day_after",
    "confirmed_activity",
    "confirmed_best_before",
    "stop_code",
)


def record_campaign_action(memory: Any, record: dict[str, Any]) -> None:
    """Accumulate the full (not rolling) public action history in-process."""
    runtime = getattr(memory, "jev_runtime", None)
    if runtime is None:
        return
    runtime.campaign_actions.append({key: record.get(key) for key in ACTION_RECORD_KEYS})


def _campaign_trace(campaign_actions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Concise action -> state-change trace, highlighting branch reveals."""
    if not campaign_actions:
        return []
    trace: list[dict[str, Any]] = []
    for index, action in enumerate(campaign_actions, 1):
        entry: dict[str, Any] = {
            "step": index,
            "action": action.get("action"),
            "seed_id": action.get("seed_id"),
            "library_id": action.get("library_id"),
            "cash_after": action.get("cash_after"),
            "day_after": action.get("elapsed_day_after"),
        }
        newly = action.get("newly_visible_seed_ids")
        if isinstance(newly, list) and newly:
            entry["revealed_seeds"] = [str(item) for item in newly]
        if action.get("reward_earned") is not None:
            entry["reward_earned"] = action.get("reward_earned")
        if action.get("best_update") is not None:
            entry["best_update"] = action.get("best_update")
        trace.append(entry)
    return trace


def _library_mining_status(
    observation: dict[str, Any],
    campaign_actions: list[dict[str, Any]] | None,
    screen_stats: dict[str, dict[str, Any]],
) -> list[str]:
    mine_counts: dict[str, int] = {}
    for action in campaign_actions or []:
        if action.get("action") == "mine_library" and action.get("library_id") is not None:
            library_id = str(action["library_id"])
            mine_counts[library_id] = mine_counts.get(library_id, 0) + 1
    lines: list[str] = []
    for seed in observation.get("state", {}).get("seeds", []):
        if not isinstance(seed, dict):
            continue
        for library in seed.get("libraries", []):
            if not isinstance(library, dict):
                continue
            library_id = str(library.get("library_id"))
            status = library.get("library_status")
            mined = mine_counts.get(library_id, 0)
            stats = screen_stats.get(library_id)
            if mined > 0 and stats and stats.get("count"):
                mean = stats["sum"] / stats["count"]
                verdict = "HAS HIT (>= baseline)" if stats["max"] >= 1.0 else "no hit, far below baseline"
                lines.append(f"{library_id} [{status}]: mined {mined}x, avg_obs={mean:.3f}, max_obs={stats['max']:.3f} -> {verdict}")
            elif status == "prepared":
                lines.append(f"{library_id} [prepared]: never mined")
            else:
                lines.append(f"{library_id} [locked]: not opened")
    return lines


def _state_payload(
    observation: dict[str, Any],
    packaging: str,
    config: ExperimentConfig,
    campaign_actions: list[dict[str, Any]] | None = None,
    screen_stats: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    state = _compact_state(observation)
    context = _task_context(config)
    trace = _campaign_trace(campaign_actions)
    screens = _candidates(observation)
    mining_status = _library_mining_status(observation, campaign_actions, screen_stats or {})
    if packaging == "compact":
        return {
            "task": context,
            "current": state,
            "campaign_trace": trace,
            "recent_screen_records": screens,
            "library_mining_status": mining_status,
        }
    history = campaign_actions if campaign_actions is not None else _history(observation)
    if packaging == "full-history":
        return {
            "task": context,
            "current": state,
            "campaign_trace": trace,
            "recent_screen_records": screens,
            "library_mining_status": mining_status,
            "action_history": history,
            "candidate_score_summary": screens,
        }
    return {
        "task": context,
        "state": state,
        "campaign_trace": trace,
        "recent_screen_records": screens,
        "library_mining_status": mining_status,
        "trajectory": {"actions": history},
        "candidates": screens,
    }


def _laya_state_payload(
    state: dict[str, Any],
    *,
    include_biology_context: bool = False,
) -> dict[str, Any]:
    """Keep Laya's public decision input below its smaller context limit."""
    current = state.get("current") if isinstance(state.get("current"), dict) else state.get("state", {})
    if not isinstance(current, dict):
        current = {}
    scalar_keys = (
        "day",
        "remaining_days",
        "current_cash",
        "total_cost_paid",
        "total_reward_earned",
        "confirmed_best_activity",
        "provisional_best_activity",
        "pending_confirmation_count",
        "action_count",
    )
    compact_current: dict[str, Any] = {key: current.get(key) for key in scalar_keys}
    economic = current.get("economic_summary")
    if isinstance(economic, dict):
        economic_keys = (
            "budget_regime",
            "can_afford_confirm_now",
            "has_time_for_confirm_now",
            "can_afford_minimum_mine_then_confirm_now",
            "has_time_for_minimum_mine_then_confirm_now",
            "has_available_mine_option",
        )
        compact_current["economic_summary"] = {key: economic.get(key) for key in economic_keys}
    compact_action_history: list[dict[str, Any]] = []
    for item in list(state.get("campaign_trace") or [])[-4:]:
        if not isinstance(item, dict):
            continue
        compact_action_history.append(
            {
                key: item.get(key)
                for key in ("step", "action", "seed_id", "library_id", "cash_after", "day_after", "best_update")
                if key in item
            }
        )
    brief = (
        "Maximize final cash in a budgeted TadA campaign. Choose only a legal action, "
        "preserve a mine-then-confirm runway, and stop when no profitable path remains."
    )
    if include_biology_context:
        brief = f"{LAYA_COMPACT_BIOLOGY_HINT} {brief}"
    return {
        "task": {"brief": brief},
        "current": compact_current,
        "recent_actions": compact_action_history,
    }


def _id(prefix: str, *parts: Any) -> str:
    normalized = [str(part).replace(" ", "_").replace(":", "-") for part in parts]
    return ":".join((prefix, *normalized))


def _library_windows(observation: dict[str, Any]) -> dict[str, Any]:
    windows: dict[str, Any] = {}
    for seed in observation.get("state", {}).get("seeds", []):
        if not isinstance(seed, dict):
            continue
        for library in seed.get("libraries", []):
            if isinstance(library, dict) and library.get("library_id") is not None:
                windows[str(library["library_id"])] = library.get("mutable_interval_aa")
    return windows


def _accumulate_screens(runtime: JEVPolicyRuntime, observation: dict[str, Any]) -> None:
    for record in observation.get("history", {}).get("recent_screen_records", []):
        if not isinstance(record, dict):
            continue
        record_id = record.get("screen_record_id")
        if record_id is None or record_id in runtime.seen_screen_ids:
            continue
        runtime.seen_screen_ids.add(record_id)
        library_id = record.get("library_id")
        activity = record.get("observed_activity")
        if library_id is None or activity is None:
            continue
        activity_value = _as_float(activity)
        stats = runtime.screen_stats.setdefault(str(library_id), {"count": 0, "sum": 0.0, "max": float("-inf"), "min": float("inf")})
        stats["count"] += 1
        stats["sum"] += activity_value
        stats["max"] = max(stats["max"], activity_value)
        stats["min"] = min(stats["min"], activity_value)


def _screen_stats_text(stats: dict[str, dict[str, Any]], library_id: Any) -> str:
    entry = stats.get(str(library_id))
    if not entry or not entry.get("count"):
        return "never-mined"
    mean = entry["sum"] / entry["count"]
    return f"mined, avg_obs={mean:.3f} max_obs={entry['max']:.3f} ({entry['count']} sampled)"


def _options(observation: dict[str, Any], screen_stats: dict[str, dict[str, Any]] | None = None) -> tuple[dict[str, str], dict[str, AgentDecision]]:
    available = observation.get("available_actions", {})
    windows = _library_windows(observation)
    screen_stats = screen_stats or {}
    items: list[tuple[str, str, AgentDecision]] = []

    for item in available.get("prepare_seed", []):
        if isinstance(item, dict):
            seed_id = item.get("seed_id")
            items.append(
                (_id("prepare_seed", seed_id), f"Prepare seed {seed_id}; cost={item.get('total_cost', 0)}.", AgentDecision("prepare_seed", seed_id=str(seed_id)))
            )

    for item in available.get("prepare_library", []):
        if isinstance(item, dict):
            seed_id, library_id = item.get("seed_id"), item.get("library_id")
            interval = item.get("mutable_interval_aa")
            window = (
                f", window=aa{interval['start_index_0based_inclusive']}-{interval['end_index_0based_exclusive']}"
                if isinstance(interval, dict)
                else ""
            )
            items.append(
                (_id("prepare_library", seed_id, library_id), f"Prepare library {library_id} from seed {seed_id}{window}; cost={item.get('total_cost', 0)}; {_screen_stats_text(screen_stats, library_id)}.", AgentDecision("prepare_library", seed_id=str(seed_id), library_id=str(library_id)))
            )

    for item in available.get("mine_library", []):
        if not isinstance(item, dict):
            continue
        for batch in item.get("batch_options", []):
            if not isinstance(batch, dict):
                continue
            seed_id, library_id, size = item.get("seed_id"), item.get("library_id"), batch.get("batch_size")
            option_id = _id("mine_library", seed_id, library_id, f"batch={size}")
            interval = windows.get(str(library_id))
            window = (
                f"window=aa{interval['start_index_0based_inclusive']}-{interval['end_index_0based_exclusive']}, "
                if isinstance(interval, dict)
                else ""
            )
            description = (
                f"Mine library {library_id} from seed {seed_id}, batch_size={size}; "
                f"{_screen_stats_text(screen_stats, library_id)}, "
                f"{window}"
                f"cost={batch.get('total_cost', batch.get('cost', 0))}, "
                f"remaining={item.get('remaining_screenable_sequence_count')}, "
                f"leaves_confirm_capacity={batch.get('leaves_confirm_capacity')} ."
            )
            items.append((option_id, description, AgentDecision("mine_library", seed_id=str(seed_id), library_id=str(library_id), batch_size=int(size))))

    for item in available.get("confirm_hit", []):
        if isinstance(item, dict):
            record_id = item.get("screen_record_id")
            items.append(
                (_id("confirm_hit", record_id), f"Confirm screen record {record_id}; observed_activity={item.get('observed_activity')}, cost={item.get('total_cost', 0)}.", AgentDecision("confirm_hit", screen_record_id=int(record_id)))
            )

    for item in available.get("purchase_skill", []):
        if isinstance(item, dict) and item.get("skill_id") is not None:
            skill_id = item["skill_id"]
            items.append((_id("purchase_skill", skill_id), f"Purchase BioSkill {skill_id}; cost={item.get('total_cost', item.get('price', 0))}.", AgentDecision("purchase_skill", skill_id=str(skill_id))))

    items.append(("stop", "Stop the campaign and preserve the current ledger.", AgentDecision("stop")))
    items.sort(key=lambda row: row[0])
    criteria: dict[str, str] = {}
    decisions: dict[str, AgentDecision] = {}
    for option_id, description, decision in items:
        if option_id in criteria:
            raise ValueError(f"Duplicate JEV option id: {option_id}")
        criteria[option_id] = description
        decisions[option_id] = decision
    if len(criteria) > 255:
        raise ValueError(f"JEV Choice supports at most 255 options, got {len(criteria)}")
    return criteria, decisions


CATEGORY_DESCRIPTIONS = {
    "stop": "End the campaign now and keep the current cash.",
    "prepare_seed": "Prepare a visible but not-yet-prepared seed background to unlock its libraries.",
    "prepare_library": "Open a new mutational window (library) on a prepared seed; may reveal downstream seeds.",
    "mine_library": "Screen a batch from an already-prepared library to discover higher-activity variants.",
    "confirm_hit": "Confirm a pending screen record to settle its true activity and reward.",
    "purchase_skill": "Buy a public BioSkill for decision support.",
}

CATEGORY_ORDER = ("stop", "prepare_seed", "prepare_library", "mine_library", "confirm_hit", "purchase_skill")


def _category_of(option_id: str) -> str:
    if option_id == "stop":
        return "stop"
    return option_id.split(":", 1)[0]


def _laya_choice_criteria(criteria: dict[str, str]) -> dict[str, str]:
    """Use aliases and short descriptions so Laya's 192-token Choice head can fit them."""
    compact: dict[str, str] = {}
    for index, (option_id, description) in enumerate(criteria.items(), 1):
        alias = "stop" if option_id == "stop" else f"a{index}"
        category = _category_of(option_id)
        if category == "stop":
            compact[alias] = "stop; keep cash"
        elif category == "prepare_seed":
            compact[alias] = "prepare seed"
        elif category == "prepare_library":
            window = re.search(r"window=(aa[0-9-]+)", description)
            compact[alias] = f"open {window.group(1)}" if window else "open library"
        elif category == "mine_library":
            batch = re.search(r"batch_size=([0-9]+)", description)
            cost = re.search(r"cost=([0-9.]+)", description)
            parts = ["mine"]
            if batch:
                parts.append(f"b{batch.group(1)}")
            if cost:
                parts.append(f"c{cost.group(1).split('.', 1)[0]}")
            if "leaves_confirm_capacity=True" in description:
                parts.append("confirmable")
            compact[alias] = " ".join(parts)
        elif category == "confirm_hit":
            observed = re.search(r"observed_activity=([0-9.]+)", description)
            compact[alias] = f"confirm obs={observed.group(1)}" if observed else "confirm"
        elif category == "purchase_skill":
            compact[alias] = "buy skill"
        else:
            compact[alias] = "choose"
    return compact


def _laya_choice_aliases(criteria: dict[str, str]) -> dict[str, str]:
    return {
        ("stop" if option_id == "stop" else f"a{index}"): option_id
        for index, option_id in enumerate(criteria, 1)
    }


def _restore_laya_choice(
    choice: str,
    probabilities: dict[str, float],
    selected_per_call: list[str],
    aliases: dict[str, str],
) -> tuple[str, dict[str, float], list[str]]:
    return (
        aliases[choice],
        {aliases[alias]: probability for alias, probability in probabilities.items()},
        [aliases[alias] for alias in selected_per_call],
    )


def _category_options(criteria: dict[str, str]) -> dict[str, str]:
    present = {_category_of(oid) for oid in criteria}
    return {cat: CATEGORY_DESCRIPTIONS[cat] for cat in CATEGORY_ORDER if cat in present}


def _choice_instructions(runtime: JEVPolicyRuntime, instructions: str) -> str:
    if runtime.biology_context_mode != "agent_shared":
        return instructions
    shared_context = runtime.shared_biology_context or _load_agent_shared_biology_context()
    return f"{shared_context}\n\n{instructions}"


def _ask_choice(
    runtime: JEVPolicyRuntime,
    state: dict[str, Any],
    instructions: str,
    criteria: dict[str, str],
    *,
    rng: random.Random | None = None,
) -> tuple[str, dict[str, float], list[str], list[dict[str, Any]], float]:
    if not criteria:
        raise ValueError("JEV Choice requires at least one legal option")
    if len(criteria) == 1:
        # Some gateways, including liquid/d1, reject a Choice with only one
        # option.  A singleton branch is deterministic and does not require a
        # model call; keep it in the audit trace with an empty usage list.
        option_id = next(iter(criteria))
        return option_id, {option_id: 1.0}, [], [], 1.0

    from typesafe_sdk import Choice

    question = Choice(instructions=_choice_instructions(runtime, instructions), criteria=criteria)
    probabilities = {option_id: 0.0 for option_id in criteria}
    selected_per_call: list[str] = []
    usages: list[dict[str, Any]] = []
    confidences: list[float] = []
    for _ in range(runtime.k):
        response = runtime.client.system_one(state=state, questions={"action": question}, model=runtime.model)
        answer = response.answers.get("action")
        if answer is None or not hasattr(answer, "probabilities"):
            raise RuntimeError("JEV response did not contain a Choice answer named 'action'")
        selected = str(answer.choice)
        if selected not in criteria:
            raise RuntimeError(f"JEV returned non-legal option id {selected!r}")
        selected_per_call.append(selected)
        raw = dict(answer.probabilities)
        for option_id in criteria:
            probabilities[option_id] += _as_float(raw.get(option_id)) / runtime.k
        confidence = getattr(answer, "confidence", None)
        if confidence is not None:
            confidences.append(_as_float(confidence))
        usage = getattr(response, "usage", None)
        provider_metadata = getattr(response, "provider_metadata", None)
        gateway_metadata = (
            provider_metadata.get("gateway")
            if isinstance(provider_metadata, dict)
            else getattr(provider_metadata, "gateway", None)
        )
        gateway_cost = (
            gateway_metadata.get("cost")
            if isinstance(gateway_metadata, dict)
            else getattr(gateway_metadata, "cost", None)
        )
        usages.append(
            {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "gateway_cost": _as_float(gateway_cost) if gateway_cost is not None else None,
            }
        )
        runtime.calls += 1
    if runtime.sampling:
        if "stop" in probabilities:
            max_probability = max(probabilities.values())
            if probabilities["stop"] >= max_probability:
                # A highest-probability (including tied-highest) stop is still
                # part of the sampling pool; it must not become deterministic.
                options = list(probabilities)
            else:
                # When another action is strictly more likely than stop, do
                # not let the stop probability terminate the campaign draw.
                options = [option_id for option_id in probabilities if option_id != "stop"]
        else:
            options = list(probabilities)
        weights = [max(probabilities[option_id], 0.0) for option_id in options]
        sampler = rng.choices if rng is not None else random.choices
        choice = sampler(options, weights=weights, k=1)[0]
    else:
        choice = max(probabilities, key=probabilities.get)
    confidence = sum(confidences) / len(confidences) if confidences else probabilities[choice]
    return choice, probabilities, selected_per_call, usages, confidence


def select_jev_action(
    policy_name: str,
    *,
    config: ExperimentConfig,
    observation: dict[str, Any],
    memory: Any,
    rng: random.Random | None = None,
) -> AgentDecision:
    runtime = _get_runtime(memory, config, policy_name)
    _accumulate_screens(runtime, observation)
    criteria, decisions = _options(observation, runtime.screen_stats)
    state = _state_payload(observation, runtime.packaging, config, runtime.campaign_actions, runtime.screen_stats)
    compact_choice_head = runtime.model in COMPACT_CHOICE_MODELS
    compact_state = _policy_bool(config.agent_policy_params, "compact_state", False)
    if compact_choice_head or compact_state:
        state = _laya_state_payload(state, include_biology_context=runtime.include_biology_context)
    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    selected_per_call: list[str] = []
    usages: list[dict[str, Any]] = []
    probabilities: dict[str, float] = {option_id: 0.0 for option_id in criteria}
    category_path: list[str] = []
    category_probabilities: dict[str, float] = {}
    final_option_ids = list(criteria)

    if runtime.include_biology_context:
        main_instructions = (
            "Given the task context and the current state, which single legal action should the "
            "agent take next? Weigh remaining cash and days, the biological promise of each library's "
            "mutational window, and the reward an action can settle. Return exactly one of the provided option ids."
        )
    else:
        main_instructions = (
            "Given the public task context and current state, which single legal action should the "
            "agent take next? Use only the listed legal options, numerical costs, remaining cash and days, "
            "observed readings, and action history. Return exactly one of the provided option ids."
        )

    if runtime.hierarchical:
        categories = _category_options(criteria)
        category_instructions = (
            "Given the task context and the current state, first decide the TYPE of the next action: "
            "stop, prepare a new seed, prepare a new library, mine an already-prepared library, or "
            "(if available) confirm a hit / buy a skill. Return exactly one of the provided category ids."
        )
        category, category_probabilities, cat_selected, cat_usages, category_confidence = _ask_choice(
            runtime, state, category_instructions, categories, rng=rng
        )
        category_path.append(category)
        selected_per_call.extend(cat_selected)
        usages.extend(cat_usages)
        sub_criteria = {oid: desc for oid, desc in criteria.items() if _category_of(oid) == category}
        final_option_ids = list(sub_criteria)
        sub_aliases = _laya_choice_aliases(sub_criteria) if compact_choice_head else {}
        model_sub_criteria = _laya_choice_criteria(sub_criteria) if compact_choice_head else sub_criteria
        sub_instructions = f"Which specific {category} option should be taken? Return exactly one of the provided option ids."
        choice, probabilities, sub_selected, sub_usages, confidence = _ask_choice(
            runtime, state, sub_instructions, model_sub_criteria, rng=rng
        )
        if compact_choice_head:
            choice, probabilities, sub_selected = _restore_laya_choice(
                choice, probabilities, sub_selected, sub_aliases
            )
        selected_per_call.extend(sub_selected)
        usages.extend(sub_usages)
    else:
        choice_aliases = _laya_choice_aliases(criteria) if compact_choice_head else {}
        choice_criteria = _laya_choice_criteria(criteria) if compact_choice_head else criteria
        choice, probabilities, selected_per_call, usages, confidence = _ask_choice(
            runtime, state, main_instructions, choice_criteria, rng=rng
        )
        if compact_choice_head:
            choice, probabilities, selected_per_call = _restore_laya_choice(
                choice, probabilities, selected_per_call, choice_aliases
            )

    params = config.agent_policy_params
    estimated_cost = sum(
        _as_float(item["gateway_cost"])
        if item.get("gateway_cost") is not None
        else _as_float(item["input_tokens"]) * _as_float(params.get("input_cost_per_token"))
        + _as_float(item["output_tokens"]) * _as_float(params.get("output_cost_per_token"))
        for item in usages
    )
    runtime.estimated_cost += estimated_cost
    trace = {
        "request_id": request_id,
        "model": runtime.model,
        "policy": policy_name,
        "k": runtime.k,
        "packaging": runtime.packaging,
        "hierarchical": runtime.hierarchical,
        "sampling": runtime.sampling,
        "include_biology_context": runtime.include_biology_context,
        "biology_context_mode": runtime.biology_context_mode,
        "compact_state": compact_state,
        "option_ids": final_option_ids,
        "selected_per_call": selected_per_call,
        "selected": choice,
        "probabilities": probabilities,
        "confidence": confidence,
        "selected_probability": probabilities[choice],
        "usage": usages,
        "estimated_cost": estimated_cost,
        "latency_ms": (time.perf_counter() - started) * 1000.0,
        "input_bytes": len(json.dumps(state, ensure_ascii=True, separators=(",", ":"))),
    }
    if runtime.hierarchical:
        trace["category_path"] = category_path
        trace["category_probabilities"] = category_probabilities
        trace["category_confidence"] = category_confidence
        trace["category_selected_probability"] = category_probabilities[category]
    runtime.traces.append(trace)
    return decisions[choice]

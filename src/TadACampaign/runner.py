"""Experiment execution."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backends import BackendError, run_backend_episode
from .judge.client import JudgeHttpClient
from .models import AgentDecision, ExperimentConfig, MineBatchOption, RewardTier
from .reporting import render_markdown_summary, write_curve, write_json, write_jsonl
from .skill_market import current_materialized_bundle_sha, materialize_owned_skill
from .telemetry import append_runtime_event, elapsed_seconds, monotonic_now, runtime_timeline_path, utc_now_iso
from .tool_trajectory import build_tool_trajectory_index

AGENT_WORKSPACE_DIRNAME = "agent_workspace"
JUDGE_SESSION_FILENAME = "judge_session.json"
STEP_HELPER_FILENAME = "TadACampaign-agent-step"
SEED_MANIFEST_FILENAME = "seed_manifest.json"
AGENT_PLAYBOOK_FILENAME = "agent_playbook.md"
BIOLOGY_CONTEXT_FILENAME = "biology_context.md"
AGENT_PLAYBOOK_DYNAMIC_REWARD_MARKER = "<!-- REWARD_RULES_DYNAMIC_BLOCK -->"
SKILL_MARKET_FILENAME = "skill_market.json"
OWNED_SKILLS_INDEX_FILENAME = "owned_skills_index.json"
OWNED_SKILLS_DIRNAME = "owned_skills"
SOURCE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SOURCE_ROOT.parent
SEQUENCE_SUMMARY_EDGE_LENGTH = 24


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


SKILL_SURFACE_STATE_KEYS = frozenset(
    {
        "skill_market_enabled",
        "skill_market_size",
        "skill_catalog_size",
        "initial_owned_skill_ids",
        "owned_skill_count",
        "owned_skill_ids",
        "total_skill_purchase_price_paid",
        "owned_skills_root",
    }
)
SKILL_SURFACE_RESPONSE_KEYS = frozenset({"owned_skill_count", "skill_id"})
NO_SKILL_CONFIG_KEYS = frozenset(
    {"skill_market_manifest", "skill_market_enabled", "initial_owned_skill_ids", "purchase_skill_days"}
)


def _strip_skill_surface_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    filtered = dict(snapshot)
    state = filtered.get("state")
    if isinstance(state, dict):
        filtered["state"] = {key: value for key, value in state.items() if key not in SKILL_SURFACE_STATE_KEYS}
    available_actions = filtered.get("available_actions")
    if isinstance(available_actions, dict):
        filtered["available_actions"] = {key: value for key, value in available_actions.items() if key != "purchase_skill"}
    filtered.pop("skill_market", None)
    filtered.pop("owned_skills_index", None)
    return filtered


def _is_skill_surface_key(key: str) -> bool:
    normalized_key = key.lower()
    return (
        key in SKILL_SURFACE_STATE_KEYS
        or key in SKILL_SURFACE_RESPONSE_KEYS
        or normalized_key == "purchase_skill"
        or normalized_key.startswith("skill_")
        or normalized_key.startswith("skill_market")
        or normalized_key.startswith("owned_skill")
        or normalized_key.startswith("owned_skills")
        or "skill_analysis" in normalized_key
    )


def _strip_skill_surface_from_response(payload: dict[str, Any]) -> dict[str, Any]:
    stripped: dict[str, Any] = {}
    for key, value in payload.items():
        if _is_skill_surface_key(key):
            continue
        if isinstance(value, dict):
            stripped[key] = _strip_skill_surface_from_response(value)
        elif isinstance(value, list):
            stripped[key] = [
                _strip_skill_surface_from_response(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            stripped[key] = value
    return stripped


def _decision_payload(decision: AgentDecision, *, include_skill_surface: bool) -> dict[str, Any]:
    payload = decision.__dict__.copy()
    if not include_skill_surface:
        payload.pop("skill_id", None)
    return payload


@dataclass(frozen=True)
class _JudgeRequestTiming:
    started_at: str
    finished_at: str
    duration_seconds: float
    method: str
    path: str
    ok: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "method": self.method,
            "path": self.path,
            "ok": self.ok,
            "error": self.error,
        }


def _resolve_path(base_dir: Path, raw_value: str) -> Path:
    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (base_dir / candidate).resolve()


def _parse_float_or_inf(raw_value: Any, *, field_name: str) -> float:
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"inf", "+inf", "infinity", "+infinity"}:
            return float("inf")
    try:
        return float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric or 'inf', got {raw_value!r}") from exc


def _parse_reward_tiers(payload: dict[str, Any]) -> tuple[RewardTier, ...]:
    baseline_activity = float(payload["baseline_activity"])
    tiers_payload = payload.get("reward_tiers")
    if not isinstance(tiers_payload, list) or not tiers_payload:
        raise ValueError("reward_tiers must be a non-empty array of tables")
    tiers = tuple(
        sorted(
            (
                RewardTier(
                    min_activity=float(item["min_activity"]),
                    max_activity=_parse_float_or_inf(item["max_activity"], field_name="reward_tiers.max_activity"),
                    multiplier=float(item["multiplier"]),
                )
                for item in tiers_payload
            ),
            key=lambda item: item.min_activity,
        )
    )
    if tiers[0].min_activity > baseline_activity + 1e-9:
        raise ValueError("The first reward tier must start at or below baseline_activity")
    previous_max = None
    for tier in tiers:
        if tier.max_activity <= tier.min_activity:
            raise ValueError("Each reward tier must satisfy max_activity > min_activity")
        if previous_max is not None and abs(tier.min_activity - previous_max) > 1e-9:
            raise ValueError("reward_tiers must form a contiguous partition")
        previous_max = tier.max_activity
    return tiers


def _parse_handoff_limit(raw_value: Any) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"codex_context_window_handoff_max_count must be an integer >= -1, got {raw_value!r}") from exc
    if value < -1:
        raise ValueError(f"codex_context_window_handoff_max_count must be >= -1, got {value}")
    return value


def _parse_mine_batch_menu(payload: dict[str, Any]) -> tuple[MineBatchOption, ...]:
    raw_menu = payload.get("mine_batch_menu")
    if not isinstance(raw_menu, list) or not raw_menu:
        raise ValueError("mine_batch_menu must be a non-empty array of tables")
    menu = tuple(
        sorted(
            (
                MineBatchOption(
                    batch_size=int(item["batch_size"]),
                    cost=float(item["cost"]),
                    time_days=float(item.get("time_days", item.get("days"))),
                )
                for item in raw_menu
            ),
            key=lambda item: item.batch_size,
        )
    )
    seen_batch_sizes: set[int] = set()
    for option in menu:
        if option.batch_size <= 0:
            raise ValueError("mine_batch_menu batch_size must be positive")
        if option.batch_size in seen_batch_sizes:
            raise ValueError("mine_batch_menu batch_size values must be unique")
        seen_batch_sizes.add(option.batch_size)
    return menu


def _parse_seed_id_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    raw_value = payload.get(key)
    if not isinstance(raw_value, list):
        raise ValueError(f"{key} must be an array of seed ids")
    values = tuple(str(item) for item in raw_value)
    if len(set(values)) != len(values):
        raise ValueError(f"{key} must not contain duplicates")
    return values


def _parse_optional_str_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    raw_value = payload.get(key, [])
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        raise ValueError(f"{key} must be an array of strings")
    values = tuple(str(item).strip() for item in raw_value)
    if any(not item for item in values):
        raise ValueError(f"{key} must not contain empty strings")
    if len(set(values)) != len(values):
        raise ValueError(f"{key} must not contain duplicates")
    return values


def _payload_float(payload: dict[str, Any], primary_key: str, fallback_key: str | None = None, *, default: float | None = None) -> float:
    if primary_key in payload:
        return float(payload[primary_key])
    if fallback_key is not None and fallback_key in payload:
        return float(payload[fallback_key])
    if default is not None:
        return float(default)
    raise KeyError(primary_key)


def _env_optional_str(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


_ALLOWED_AGENT_BACKENDS = frozenset({"codex", "claude", "opencode", "policy"})
_ALLOWED_RUN_MODES = frozenset({"agent", "jev"})
_DEFAULT_JEV_POLICY = "jev_k1"


_REMOVED_AGENT_KEYS = frozenset(
    {
        "full_auto",
        "dangerously_bypass_approvals",
        "sandbox_mode",
        "search",
        "codex_auto_resume",
        "codex_auto_resume_max_attempts",
        "codex_auto_resume_backoff_seconds",
        "codex_context_window_handoff_max_count",
    }
)
_REMOVED_JUDGE_KEYS = frozenset({"mode", "url"})


def _reject_removed_public_keys(payload: dict[str, Any], *, table_name: str, removed_keys: frozenset[str]) -> None:
    unexpected = sorted(key for key in payload if key in removed_keys)
    if unexpected:
        joined = ", ".join(unexpected)
        raise ValueError(
            f"{table_name} contains unsupported removed keys: {joined}. "
            "Use the single supported public launch script flow instead."
        )


def _payload_bool(payload: dict[str, Any], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be a boolean")


def _normalize_run_mode(value: Any, *, source: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in _ALLOWED_RUN_MODES:
        allowed = ", ".join(sorted(_ALLOWED_RUN_MODES))
        raise ValueError(f"{source} must be one of {allowed}, got {value!r}")
    return normalized


def load_experiment_config(
    config_path: Path,
    *,
    mode: str | None = None,
    policy: str | None = None,
) -> ExperimentConfig:
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    base_dir = config_path.parent.resolve()
    agent_payload = payload.get("agent", {})
    judge_payload = payload.get("judge", {})
    _reject_removed_public_keys(agent_payload, table_name="[agent]", removed_keys=_REMOVED_AGENT_KEYS)
    _reject_removed_public_keys(judge_payload, table_name="[judge]", removed_keys=_REMOVED_JUDGE_KEYS)
    judge_server_url = judge_payload.get("server_url")
    if judge_server_url is not None and (not isinstance(judge_server_url, str) or not judge_server_url.strip()):
        raise ValueError("[judge].server_url must be a non-empty string when present")
    raw_skill_market_manifest = judge_payload.get("skill_market_manifest", payload.get("skill_market_manifest"))
    if raw_skill_market_manifest is not None and (
        not isinstance(raw_skill_market_manifest, str) or not raw_skill_market_manifest.strip()
    ):
        raise ValueError("skill_market_manifest must be a non-empty string when present")
    skill_market_enabled = _payload_bool(
        judge_payload,
        "skill_market_enabled",
        default=_payload_bool(payload, "skill_market_enabled", default=True),
    )
    initial_owned_skill_ids = _parse_optional_str_list(
        judge_payload if "initial_owned_skill_ids" in judge_payload else payload,
        "initial_owned_skill_ids",
    )
    if initial_owned_skill_ids and raw_skill_market_manifest is None:
        raise ValueError("initial_owned_skill_ids requires skill_market_manifest")
    configured_backend = str(agent_payload.get("backend", "codex")).strip().lower()
    configured_policy = agent_payload.get("policy")
    configured_mode = agent_payload.get("mode")
    if configured_mode is not None:
        configured_mode = _normalize_run_mode(configured_mode, source="[agent].mode")
    if mode is not None:
        requested_mode = _normalize_run_mode(mode, source="mode")
    elif policy is not None and configured_mode is None:
        requested_mode = "jev"
    elif configured_mode is not None:
        requested_mode = configured_mode
    else:
        requested_mode = "jev" if configured_backend == "policy" else "agent"

    if policy is not None and requested_mode != "jev":
        raise ValueError("policy can only be supplied with Jev mode")
    if requested_mode == "jev":
        agent_backend = "policy"
        agent_policy = policy or configured_policy or _DEFAULT_JEV_POLICY
    else:
        if configured_backend == "policy":
            raise ValueError(
                "Agent mode requires an external agent backend; the config selects the policy backend. "
                "Use --jev or set [agent].backend to codex, claude, or opencode."
            )
        agent_backend = configured_backend
        agent_policy = configured_policy

    if agent_backend not in _ALLOWED_AGENT_BACKENDS:
        allowed = ", ".join(sorted(_ALLOWED_AGENT_BACKENDS))
        raise ValueError(f"[agent].backend must be one of {allowed}, got {agent_backend!r}")
    if raw_skill_market_manifest is not None and agent_backend != "codex":
        raise ValueError("skill_market_manifest is currently supported only for the codex backend")
    if agent_backend == "policy":
        if agent_policy is None or not isinstance(agent_policy, str) or not agent_policy.strip():
            raise ValueError("[agent].policy must be a non-empty string when [agent].backend = \"policy\"")
        agent_policy = agent_policy.strip()
    elif agent_policy is not None:
        raise ValueError("[agent].policy is only supported when [agent].backend = \"policy\"")
    raw_policy_params = agent_payload.get("policy_params", {})
    if raw_policy_params is None:
        raw_policy_params = {}
    if not isinstance(raw_policy_params, dict):
        raise ValueError("[agent].policy_params must be a table/object when present")
    screening_noise_enabled = _payload_bool(payload, "screening_noise_enabled", default=True)
    screening_noise_sigma = _payload_float(payload, "screening_noise_sigma", default=0.10)
    screening_noise_clip = _payload_float(payload, "screening_noise_clip", default=0.20)
    if screening_noise_sigma < 0.0:
        raise ValueError("screening_noise_sigma must be >= 0.0")
    if screening_noise_clip <= 0.0:
        raise ValueError("screening_noise_clip must be > 0.0")
    return ExperimentConfig(
        name=payload["name"],
        prepared_root=_resolve_path(base_dir, payload["prepared_root"]),
        variant=payload["variant"],
        output_root=_resolve_path(base_dir, payload["output_root"]),
        seed=int(payload["seed"]),
        initial_cash=float(payload["initial_cash"]),
        base_asset_value=float(payload["base_asset_value"]),
        baseline_activity=float(payload["baseline_activity"]),
        prepare_seed_cost=_payload_float(payload, "prepare_seed_cost", default=0.0),
        prepare_library_cost=_payload_float(payload, "prepare_library_cost", "open_cost"),
        daily_burn_cost=float(payload["daily_burn_cost"]),
        prepare_seed_days=_payload_float(payload, "prepare_seed_days", default=0.0),
        prepare_library_days=_payload_float(payload, "prepare_library_days", "open_action_days"),
        confirm_hit_cost=_payload_float(payload, "confirm_hit_cost", default=0.0),
        confirm_hit_days=_payload_float(payload, "confirm_hit_days", default=0.0),
        purchase_skill_days=_payload_float(payload, "purchase_skill_days", default=1.0),
        mine_batch_menu=_parse_mine_batch_menu(payload),
        initial_visible_seeds=_parse_seed_id_list(payload, "initial_visible_seeds"),
        initial_prepared_seeds=_parse_seed_id_list(payload, "initial_prepared_seeds"),
        reward_tiers=_parse_reward_tiers(payload),
        max_experiment_days=float(payload["max_experiment_days"]),
        agent_timeout_seconds=int(payload.get("agent_timeout_seconds", 300)),
        linear_issue=payload.get("linear_issue"),
        agent_backend=agent_backend,
        agent_model=agent_payload.get("model"),
        agent_profile=agent_payload.get("profile"),
        agent_reasoning_effort=agent_payload.get("reasoning_effort"),
        agent_verbosity=agent_payload.get("verbosity"),
        agent_codex_context_window_handoff_max_count=_parse_handoff_limit(
            agent_payload.get("codex_context_window_handoff_max_count", -1)
        ),
        skill_market_manifest=(
            _resolve_path(base_dir, raw_skill_market_manifest) if isinstance(raw_skill_market_manifest, str) else None
        ),
        skill_market_enabled=skill_market_enabled,
        initial_owned_skill_ids=initial_owned_skill_ids,
        judge_server_url=judge_server_url.strip() if isinstance(judge_server_url, str) else None,
        judge_request_timeout_seconds=int(
            judge_payload.get("request_timeout_seconds", judge_payload.get("timeout_seconds", 10))
        ),
        screening_noise_enabled=screening_noise_enabled,
        screening_noise_sigma=screening_noise_sigma,
        screening_noise_clip=screening_noise_clip,
        agent_policy=agent_policy,
        agent_policy_params=dict(raw_policy_params),
        run_mode=requested_mode,
    )


def _make_run_dir(output_root: Path, run_name: str) -> Path:
    # Directory names are part of the audit trail.  Use UTC explicitly so a
    # run cannot be assigned a local-time label that disagrees with the UTC
    # timestamps written to runtime_timeline.jsonl.
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    for collision_index in range(1000):
        suffix = "" if collision_index == 0 else f"-{collision_index:02d}"
        run_dir = output_root / f"{timestamp}{suffix}-{run_name}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        (run_dir / "process").mkdir(parents=True, exist_ok=True)
        (run_dir / "results").mkdir(parents=True, exist_ok=True)
        return run_dir
    raise RuntimeError(f"Could not allocate a unique run directory under {output_root}")


def _process_dir(run_dir: Path) -> Path:
    return run_dir / "process"


def _results_dir(run_dir: Path) -> Path:
    return run_dir / "results"


def _agent_workspace_dir(run_dir: Path) -> Path:
    return _process_dir(run_dir) / AGENT_WORKSPACE_DIRNAME


def _judge_session_path(run_dir: Path) -> Path:
    return _agent_workspace_dir(run_dir) / JUDGE_SESSION_FILENAME


def _helper_script_path(run_dir: Path) -> Path:
    return _agent_workspace_dir(run_dir) / STEP_HELPER_FILENAME


def _skill_market_path(run_dir: Path) -> Path:
    return _agent_workspace_dir(run_dir) / SKILL_MARKET_FILENAME


def _owned_skills_index_path(run_dir: Path) -> Path:
    return _agent_workspace_dir(run_dir) / OWNED_SKILLS_INDEX_FILENAME


def _owned_skills_dir(run_dir: Path) -> Path:
    return _agent_workspace_dir(run_dir) / OWNED_SKILLS_DIRNAME


def _agent_playbook_source_path() -> Path:
    source = REPO_ROOT / "docs" / AGENT_PLAYBOOK_FILENAME
    if source.is_file():
        return source
    return SOURCE_ROOT / "agent_native_tools" / "resources" / AGENT_PLAYBOOK_FILENAME


def _biology_context_source_path() -> Path:
    source = REPO_ROOT / "docs" / BIOLOGY_CONTEXT_FILENAME
    if source.is_file():
        return source
    return SOURCE_ROOT / "agent_native_tools" / "resources" / BIOLOGY_CONTEXT_FILENAME


def _format_playbook_float(value: float) -> str:
    value = float(value)
    if value == float("inf"):
        return "+inf"
    if value == float("-inf"):
        return "-inf"
    return repr(value)


def _playbook_reward_for_improvement(config: ExperimentConfig, previous_best: float, new_best: float) -> float:
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


def _playbook_example_interval(config: ExperimentConfig) -> tuple[float, float, RewardTier]:
    tier = config.reward_tiers[0]
    lower = max(config.baseline_activity, tier.min_activity)
    if tier.max_activity == float("inf"):
        upper = lower * 1.2
    else:
        upper = lower + (tier.max_activity - lower) * 0.5
    if upper <= lower:
        if tier.max_activity == float("inf"):
            upper = lower + max(abs(lower) * 0.2, 0.1)
        else:
            upper = tier.max_activity
    return lower, upper, tier


def _render_playbook_reward_rules(config: ExperimentConfig) -> str:
    tier_lines = [
        f"- tier {index}: `[{_format_playbook_float(tier.min_activity)}, {_format_playbook_float(tier.max_activity)})`, "
        f" `multiplier = {_format_playbook_float(tier.multiplier)}`"
        for index, tier in enumerate(config.reward_tiers, start=1)
    ]
    example_lower, example_upper, example_tier = _playbook_example_interval(config)
    example_reward = _playbook_reward_for_improvement(config, example_lower, example_upper)
    lines = [
        f"- `baseline_activity = {_format_playbook_float(config.baseline_activity)}`",
        f"- `base_asset_value = {_format_playbook_float(config.base_asset_value)}`",
        "",
        "Current reward tiers:",
        *tier_lines,
        "",
        "Quick interpretation:",
        "- Inspect the previous and new confirmed_best_activity for this confirmation",
        "- Calculate reward only over the crossed tier segments",
        "- If the improvement crosses several tiers, calculate each segment using its multiplier and sum them",
        "",
        "For example, under the current configuration:",
        (
            f"- If `confirmed_best_activity` increases from `{_format_playbook_float(example_lower)}` to "
            f"`{_format_playbook_float(example_upper)}`, and this improvement lies within one tier, then"
        ),
        (
            f"- `reward ≈ {_format_playbook_float(config.base_asset_value)}"
            f" * {_format_playbook_float(config.baseline_activity)}"
            f" * {_format_playbook_float(example_tier.multiplier)}"
            f" * ((1 / {_format_playbook_float(example_lower)}) - (1 / {_format_playbook_float(example_upper)}))"
            f" = {example_reward:.4f}`"
        ),
        "- This is gross reward; subtract the confirmation charge and time burn and consider the opportunity cost of occupied time",
    ]
    return "\n".join(lines)


def _strip_agent_playbook_skill_surface(text: str) -> str:
    skill_headings = {"## Optional tool use"}
    filtered_lines: list[str] = []
    skip_section = False
    skip_paragraph_until_heading = False
    skip_bullet = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if skip_bullet:
            if line.startswith("- "):
                skip_bullet = False
            else:
                continue
        if skip_paragraph_until_heading:
            if line.startswith("## "):
                skip_paragraph_until_heading = False
            else:
                continue
        if stripped in skill_headings:
            skip_section = True
            continue
        if skip_section:
            if line.startswith("## "):
                skip_section = False
            else:
                continue
        if line.startswith("If `owned_skills_index.json` reports `owned_skill_count > 0`"):
            skip_paragraph_until_heading = True
            continue
        if stripped in {"- `purchase_skill`", "- Initially owned assigned tool"}:
            skip_bullet = True
            continue
        if "biology_context.md" in line:
            filtered_lines.append(line.replace(", or the interpretation of tool acquisition", ""))
            continue
        if line.startswith("- If needed, read `history.json`"):
            filtered_lines.append("- If needed, read `history.json`\n")
            continue
        if "`skill_market.json`" in line or "`owned_skills_index.json`" in line:
            continue
        if line.startswith("./TadACampaign-agent-step --action purchase_skill"):
            continue
        if "purchase_skill" in line or "skill" in line.lower() or "Skill" in line:
            continue
        filtered_lines.append(line)
    return "".join(filtered_lines)


def _strip_biology_context_skill_surface(text: str) -> str:
    text = text.replace(
        "The authoritative game state remains `state.json`, `available_actions.json`, `history.json`, "
        "`skill_market.json`, and `owned_skills_index.json`.",
        "The authoritative game state remains `state.json`, `available_actions.json`, and `history.json`.",
    )
    filtered_lines: list[str] = []
    skip_section = False
    for line in text.splitlines(keepends=True):
        if line.startswith("## Skill Use"):
            skip_section = True
            continue
        if skip_section:
            if line.startswith("## "):
                skip_section = False
            else:
                continue
        if "purchase_skill" in line or "skill" in line.lower():
            continue
        filtered_lines.append(line)
    return "".join(filtered_lines)


def _load_agent_playbook_text(config: ExperimentConfig) -> str:
    playbook_path = _agent_playbook_source_path()
    if not playbook_path.is_file():
        raise FileNotFoundError(f"Runtime agent playbook is missing: {playbook_path}")
    text = playbook_path.read_text(encoding="utf-8")
    if not config.skill_surface_enabled:
        text = _strip_agent_playbook_skill_surface(text)
    rendered_reward_rules = _render_playbook_reward_rules(config)
    if AGENT_PLAYBOOK_DYNAMIC_REWARD_MARKER in text:
        text = text.replace(AGENT_PLAYBOOK_DYNAMIC_REWARD_MARKER, rendered_reward_rules)
    else:
        text = text.rstrip() + "\n\n" + rendered_reward_rules + "\n"
    return text if text.endswith("\n") else text + "\n"


def _write_observation_snapshot(directory: Path, snapshot: dict[str, Any], *, include_skill_surface: bool) -> None:
    if not include_skill_surface:
        snapshot = _strip_skill_surface_from_snapshot(snapshot)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "state.json", snapshot.get("state", {}))
    write_json(directory / "history.json", snapshot.get("history", {}))
    write_json(directory / "available_actions.json", snapshot.get("available_actions", {}))
    if include_skill_surface:
        write_json(directory / SKILL_MARKET_FILENAME, snapshot.get("skill_market", {"market_size": 0, "cards": []}))
        write_json(
            directory / OWNED_SKILLS_INDEX_FILENAME,
            snapshot.get(
                "owned_skills_index",
                {"workspace_root": OWNED_SKILLS_DIRNAME, "owned_skill_count": 0, "skills": []},
            ),
        )
    else:
        for path in (directory / SKILL_MARKET_FILENAME, directory / OWNED_SKILLS_INDEX_FILENAME):
            if path.exists():
                path.unlink()


def _sync_owned_skills(
    run_dir: Path,
    snapshot: dict[str, Any],
    *,
    session_client: JudgeHttpClient | None,
    session_id: str | None,
) -> None:
    owned_root = _owned_skills_dir(run_dir)
    if not snapshot.get("owned_skills_index"):
        if owned_root.exists():
            shutil.rmtree(owned_root)
        return
    owned_root.mkdir(parents=True, exist_ok=True)
    index_payload = snapshot.get("owned_skills_index", {})
    skills_payload = index_payload.get("skills", []) if isinstance(index_payload, dict) else []
    desired_ids: set[str] = set()
    for item in skills_payload:
        if not isinstance(item, dict):
            continue
        skill_id = item.get("skill_id")
        bundle_sha256 = item.get("bundle_sha256")
        if not isinstance(skill_id, str) or not skill_id:
            continue
        desired_ids.add(skill_id)
        skill_dir = owned_root / skill_id
        if isinstance(bundle_sha256, str) and bundle_sha256 and current_materialized_bundle_sha(skill_dir) == bundle_sha256:
            continue
        if session_client is None or session_id is None:
            raise RuntimeError(f"Cannot materialize owned skill {skill_id} without a live judge session client")
        bundle_payload = session_client.fetch_owned_skill_bundle(session_id, skill_id)
        materialize_owned_skill(skill_dir, bundle_payload=bundle_payload, index_item=item)

    for candidate in owned_root.iterdir():
        if candidate.name in desired_ids:
            continue
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()


def _write_helper_script(run_dir: Path) -> None:
    script_path = _helper_script_path(run_dir)
    preferred_python = REPO_ROOT / ".venv" / "bin" / "python"
    source_root = shlex.quote(str(SOURCE_ROOT))
    run_dir_arg = shlex.quote(str(run_dir.resolve()))
    # The helper is generated on the host but executed inside the outer Docker
    # sandbox.  A host absolute interpreter path is not mounted there.  Use the
    # container's PATH-resolved python3 for externally sandboxed runs; retain
    # the repository venv fallback for ordinary host execution.
    external_sandbox = os.environ.get("TADACAMPAIGN_CODEX_EXTERNAL_SANDBOX", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    python_executable: str | Path = "python3" if external_sandbox else (
        preferred_python if preferred_python.is_file() else Path(sys.executable)
    )
    launcher = (
        f"{shlex.quote(str(python_executable))} -m TadACampaign.runtime_entrypoints "
        f"agent-step --run-dir {run_dir_arg} \"$@\""
    )
    script_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"export PYTHONPATH={source_root}${{PYTHONPATH:+:${{PYTHONPATH}}}}\n"
        f"exec {launcher}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)


def _refresh_agent_workspace(
    run_dir: Path,
    config: ExperimentConfig,
    snapshot: dict[str, Any],
    *,
    session_client: JudgeHttpClient | None = None,
    session_id: str | None = None,
) -> None:
    workspace = _agent_workspace_dir(run_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    _write_observation_snapshot(workspace, snapshot, include_skill_surface=config.skill_surface_enabled)
    if config.skill_surface_enabled:
        _sync_owned_skills(run_dir, snapshot, session_client=session_client, session_id=session_id)
    else:
        owned_root = _owned_skills_dir(run_dir)
        if owned_root.exists():
            shutil.rmtree(owned_root)
    (workspace / AGENT_PLAYBOOK_FILENAME).write_text(_load_agent_playbook_text(config), encoding="utf-8")
    biology_context_path = _biology_context_source_path()
    if biology_context_path.is_file():
        biology_context_text = biology_context_path.read_text(encoding="utf-8")
        if not config.skill_surface_enabled:
            biology_context_text = _strip_biology_context_skill_surface(biology_context_text)
        (workspace / BIOLOGY_CONTEXT_FILENAME).write_text(biology_context_text, encoding="utf-8")
    _write_helper_script(run_dir)


def _set_toml_table_key(config_text: str, *, table: str, key: str, value: str) -> str:
    """Set one scalar key while preserving the rest of a legacy TOML file."""
    lines = config_text.splitlines(keepends=True)
    table_pattern = re.compile(rf"^\[{re.escape(table)}\]\s*$")
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*(?:\n|$)")
    table_index = next(
        (index for index, line in enumerate(lines) if table_pattern.match(line.rstrip("\r\n"))),
        None,
    )
    if table_index is None:
        separator = "" if not config_text or config_text.endswith(("\n", "\r")) else "\n"
        return f"{config_text}{separator}[{table}]\n{key} = {value}\n"

    end_index = next(
        (
            index
            for index in range(table_index + 1, len(lines))
            if lines[index].lstrip().startswith("[")
        ),
        len(lines),
    )
    for index in range(table_index + 1, end_index):
        if key_pattern.match(lines[index]):
            lines[index] = f"{key} = {value}\n"
            return "".join(lines)
    lines.insert(table_index + 1, f"{key} = {value}\n")
    return "".join(lines)


def _remove_toml_table_key(config_text: str, *, table: str, key: str) -> str:
    lines = config_text.splitlines(keepends=True)
    table_pattern = re.compile(rf"^\[{re.escape(table)}\]\s*$")
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*(?:\n|$)")
    table_index = next(
        (index for index, line in enumerate(lines) if table_pattern.match(line.rstrip("\r\n"))),
        None,
    )
    if table_index is None:
        return config_text
    end_index = next(
        (
            index
            for index in range(table_index + 1, len(lines))
            if lines[index].lstrip().startswith("[")
        ),
        len(lines),
    )
    lines = [
        line
        for index, line in enumerate(lines)
        if not (table_index < index < end_index and key_pattern.match(line))
    ]
    return "".join(lines)


def _write_run_config_snapshot(config_path: Path, *, run_dir: Path, config: ExperimentConfig) -> None:
    config_text = config_path.read_text(encoding="utf-8")
    if not config.skill_surface_enabled:
        for key in NO_SKILL_CONFIG_KEYS:
            config_text = re.sub(rf"(?m)^\s*{re.escape(key)}\s*=.*\n?", "", config_text)
    config_text = _set_toml_table_key(
        config_text,
        table="agent",
        key="mode",
        value=json.dumps(config.run_mode),
    )
    config_text = _set_toml_table_key(
        config_text,
        table="agent",
        key="backend",
        value=json.dumps(config.agent_backend),
    )
    if config.run_mode == "jev":
        config_text = _set_toml_table_key(
            config_text,
            table="agent",
            key="policy",
            value=json.dumps(config.agent_policy),
        )
    else:
        config_text = _remove_toml_table_key(config_text, table="agent", key="policy")
    replacements = {
        "prepared_root": json.dumps(str(config.prepared_root)),
        "output_root": json.dumps(str(config.output_root)),
    }
    if config.skill_surface_enabled and config.skill_market_manifest is not None:
        replacements["skill_market_manifest"] = json.dumps(str(config.skill_market_manifest))
        replacements["skill_market_enabled"] = "true" if config.skill_market_enabled else "false"
    if config.skill_surface_enabled and config.initial_owned_skill_ids:
        replacements["initial_owned_skill_ids"] = json.dumps(list(config.initial_owned_skill_ids), ensure_ascii=False)
    for key, value in replacements.items():
        config_text, count = re.subn(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", config_text, count=1)
        if count == 1:
            continue
        if key in {"skill_market_enabled", "initial_owned_skill_ids"}:
            config_text = config_text.rstrip() + f"\n{key} = {value}\n"
        else:
            raise ValueError(f"Missing required config key {key!r} in {config_path}")
    if re.search(r"(?m)^\[judge\]\s*$", config_text):
        if config.judge_server_url:
            if re.search(r"(?m)^server_url\s*=.*$", config_text):
                config_text = re.sub(
                    r"(?m)^server_url\s*=.*$",
                    f'server_url = {json.dumps(config.judge_server_url)}',
                    config_text,
                    count=1,
                )
            else:
                config_text = config_text.rstrip() + f'\nserver_url = {json.dumps(config.judge_server_url)}\n'
        if re.search(r"(?m)^request_timeout_seconds\s*=.*$", config_text):
            config_text = re.sub(
                r"(?m)^request_timeout_seconds\s*=.*$",
                f"request_timeout_seconds = {config.judge_request_timeout_seconds}",
                config_text,
                count=1,
            )
        else:
            config_text = config_text.rstrip() + f"\nrequest_timeout_seconds = {config.judge_request_timeout_seconds}\n"
    else:
        config_text = (
            config_text.rstrip()
            + "\n\n[judge]\n"
            + (
                f'server_url = {json.dumps(config.judge_server_url)}\n'
                if config.judge_server_url
                else ""
            )
            + f"request_timeout_seconds = {config.judge_request_timeout_seconds}\n"
        )
    (run_dir / "config.toml").write_text(config_text, encoding="utf-8")


def _server_base_url(config: ExperimentConfig, session_payload: dict[str, Any] | None = None) -> str:
    server_url = config.judge_server_url
    if session_payload is not None:
        candidate = session_payload.get("server_url")
        if isinstance(candidate, str) and candidate:
            server_url = candidate
    if not server_url:
        raise RuntimeError(
            "No authoritative judge URL is configured for this run. "
            "The supported public launch script must inject [judge].server_url before running the experiment."
        )
    return server_url


def _judge_admin_token() -> str:
    token = os.environ.get("TADACAMPAIGN_JUDGE_ADMIN_TOKEN")
    if not token:
        raise RuntimeError(
            "Authoritative judge admin token is missing. "
            "The supported public launch script must provide TADACAMPAIGN_JUDGE_ADMIN_TOKEN."
        )
    return token


def _server_admin_client(config: ExperimentConfig, session_payload: dict[str, Any] | None = None) -> JudgeHttpClient:
    return JudgeHttpClient(
        _server_base_url(config, session_payload=session_payload),
        timeout_seconds=config.judge_request_timeout_seconds,
        admin_token=_judge_admin_token(),
    )


def _server_session_client(config: ExperimentConfig, session_payload: dict[str, Any]) -> JudgeHttpClient:
    session_token = session_payload.get("session_token")
    if not isinstance(session_token, str) or not session_token:
        raise RuntimeError("Judge session token is missing from judge_session.json")
    return JudgeHttpClient(
        _server_base_url(config, session_payload=session_payload),
        timeout_seconds=config.judge_request_timeout_seconds,
        session_token=session_token,
    )


def _write_server_session(run_dir: Path, *, server_url: str, session_id: str, session_token: str) -> None:
    write_json(
        _judge_session_path(run_dir),
        {
            "transport": "server",
            "server_url": server_url,
            "session_id": session_id,
            "session_token": session_token,
        },
    )


def _load_judge_session(run_dir: Path) -> dict[str, Any]:
    payload = json.loads(_judge_session_path(run_dir).read_text(encoding="utf-8"))
    transport = str(payload.get("transport") or "")
    if transport != "server":
        raise RuntimeError(f"Unsupported judge transport {transport!r}; only 'server' is supported")
    return payload


def _runtime_timeline_rows(run_dir: Path) -> list[dict[str, Any]]:
    path = runtime_timeline_path(run_dir)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _load_resume_meta_payload(run_dir: Path) -> dict[str, Any] | None:
    path = _results_dir(run_dir) / "agent_resume_meta.json"
    if not path.is_file():
        fallback_path = _results_dir(run_dir) / "codex_resume_meta.json"
        path = fallback_path if fallback_path.is_file() else path
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _load_agent_budget_payload(run_dir: Path) -> dict[str, Any] | None:
    path = _results_dir(run_dir) / "agent_budget.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _agent_summary_fields(run_dir: Path, *, backend: str, session_id: str | None) -> dict[str, Any]:
    payload = _load_resume_meta_payload(run_dir)
    budget_payload = _load_agent_budget_payload(run_dir)
    if payload is not None:
        fields: dict[str, Any] = {
            "agent_backend": str(payload.get("backend") or backend),
            "agent_attempt_count": int(payload.get("attempts_total", 0)),
            "agent_resume_count": int(payload.get("resume_count", 0)),
            "agent_handoff_count": int(payload.get("handoff_count", 0)),
        }
        if session_id is not None:
            fields["agent_session_id"] = session_id
        if backend == "codex":
            fields.update(
                {
                    "codex_attempt_count": fields["agent_attempt_count"],
                    "codex_resume_count": fields["agent_resume_count"],
                    "codex_handoff_count": fields["agent_handoff_count"],
                }
            )
            if session_id is not None:
                fields["codex_thread_id"] = session_id
    else:
        has_agent_artifact = any(
            (_results_dir(run_dir) / filename).exists()
            for filename in ("agent_stdout.log", "agent_final_result.json", "agent_command.txt", "agent_stderr.log")
        )
        if not has_agent_artifact and backend == "codex":
            has_agent_artifact = any(
                (_results_dir(run_dir) / filename).exists()
                for filename in ("codex_events.jsonl", "codex_final_result.json", "codex_command.txt", "codex_stderr.log")
            )
        fields = {
            "agent_backend": backend,
            "agent_attempt_count": 1 if has_agent_artifact else 0,
            "agent_resume_count": 0,
            "agent_handoff_count": 0,
        }
        if session_id is not None:
            fields["agent_session_id"] = session_id
        if backend == "codex":
            fields.update(
                {
                    "codex_attempt_count": fields["agent_attempt_count"],
                    "codex_resume_count": fields["agent_resume_count"],
                    "codex_handoff_count": fields["agent_handoff_count"],
                }
            )
            if session_id is not None:
                fields["codex_thread_id"] = session_id

    if budget_payload is not None:
        fields.update(
            {
                "agent_timeout_seconds": float(budget_payload.get("budget_seconds", 0.0) or 0.0),
                "agent_budget_elapsed_seconds": float(budget_payload.get("elapsed_seconds", 0.0) or 0.0),
                "agent_budget_exhausted": bool(budget_payload.get("exhausted", False)),
                "agent_budget_status": str(budget_payload.get("status") or "unknown"),
            }
        )
    return fields


def _int_field(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _backend_error_text(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = reason.strip()
    prefix = "backend_error:"
    if text.lower().startswith(prefix):
        return text[len(prefix) :].strip()
    return text or None


def _classification_counts_from_attempts(attempts: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attempt in attempts:
        classification = str(attempt.get("classification") or "").strip()
        if not classification or classification == "success":
            continue
        counts[classification] = counts.get(classification, 0) + 1
    return counts


def _backend_failure_summary_fields(run_dir: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    force_stop_reason = next(
        (
            str(row.get("reason"))
            for row in reversed(rows)
            if row.get("event") == "backend.force_stop" and isinstance(row.get("reason"), str)
        ),
        None,
    )
    if force_stop_reason is None:
        return {}

    fields: dict[str, Any] = {"backend_force_stopped": True}
    payload = _load_resume_meta_payload(run_dir)
    attempts = [
        item
        for item in (payload.get("attempts", []) if isinstance(payload, dict) else [])
        if isinstance(item, dict)
    ]
    failed_attempts = [
        item for item in attempts if str(item.get("classification") or "").strip() not in {"", "success"}
    ]
    classification_counts = (
        payload.get("failure_classification_counts")
        if isinstance(payload, dict) and isinstance(payload.get("failure_classification_counts"), dict)
        else _classification_counts_from_attempts(attempts)
    )
    if not classification_counts:
        classification_counts = {"terminal_backend_error": 1}

    if isinstance(payload, dict):
        failure_count = _int_field(payload, "failure_count")
        retryable_failure_count = _int_field(payload, "retryable_failure_count")
        terminal_failure_count = _int_field(payload, "terminal_failure_count")
        recovery_attempt_count = _int_field(payload, "backend_recovery_attempt_count")
        if recovery_attempt_count is None:
            recovery_attempt_count = int(payload.get("resume_count", 0) or 0) + int(payload.get("handoff_count", 0) or 0)
    else:
        failure_count = None
        retryable_failure_count = None
        terminal_failure_count = None
        recovery_attempt_count = None

    if failure_count is None:
        failure_count = max(len(failed_attempts), 1)
    if retryable_failure_count is None:
        retryable_failure_count = sum(1 for item in failed_attempts if bool(item.get("retryable")))
    if terminal_failure_count is None:
        terminal_failure_count = max(int(failure_count) - int(retryable_failure_count), 0)
    if recovery_attempt_count is None:
        recovery_attempt_count = sum(
            1 for item in attempts if str(item.get("mode") or "").strip() in {"resume", "handoff"}
        )

    final_failed_attempt = failed_attempts[-1] if failed_attempts else {}
    final_classification = (
        str(payload.get("final_failure_classification") or "").strip()
        if isinstance(payload, dict)
        else ""
    )
    if not final_classification:
        final_classification = str(final_failed_attempt.get("classification") or "").strip() or "terminal_backend_error"

    final_error = (
        str(payload.get("final_error") or "").strip()
        if isinstance(payload, dict)
        else ""
    )
    if not final_error:
        final_error = str(
            final_failed_attempt.get("stop_reason")
            or final_failed_attempt.get("reason_excerpt")
            or _backend_error_text(force_stop_reason)
            or ""
        ).strip()

    fields.update(
        {
            "backend_failure_count": int(failure_count),
            "backend_retryable_failure_count": int(retryable_failure_count),
            "backend_terminal_failure_count": int(terminal_failure_count),
            "backend_recovery_attempt_count": int(recovery_attempt_count),
            "backend_failure_classification_counts": {
                str(key): int(value) for key, value in sorted(classification_counts.items())
            },
            "backend_failure_classifications": sorted(str(key) for key in classification_counts),
            "backend_final_failure_classification": final_classification,
            "backend_final_error": _backend_error_text(final_error) or final_error,
            "backend_recovery_success": bool(payload.get("success")) if isinstance(payload, dict) else False,
        }
    )
    if "missing_final_json_handoff_required" in fields["backend_failure_classification_counts"]:
        fields["backend_missing_final_json_handoff_count"] = fields["backend_failure_classification_counts"][
            "missing_final_json_handoff_required"
        ]
    return fields


def _runtime_summary_fields(run_dir: Path) -> dict[str, Any]:
    rows = _runtime_timeline_rows(run_dir)
    started_at = next(
        (
            row.get("started_at")
            for row in rows
            if row.get("event") == "run.started" and isinstance(row.get("started_at"), str)
        ),
        None,
    )
    completed_at = next(
        (
            row.get("finished_at")
            for row in reversed(rows)
            if row.get("event") == "run.finished" and isinstance(row.get("finished_at"), str)
        ),
        None,
    )
    duration_seconds = next(
        (
            float(row.get("duration_seconds", 0.0))
            for row in reversed(rows)
            if row.get("event") == "run.finished" and row.get("duration_seconds") is not None
        ),
        None,
    )
    return {
        "run_started_at": started_at,
        "run_completed_at": completed_at,
        "wall_clock_duration_seconds": duration_seconds,
    } | _backend_failure_summary_fields(run_dir, rows)


def _timed_judge_request(
    run_dir: Path,
    *,
    method: str,
    path: str,
    session_id: str | None,
    step_index: int | None,
    phase: str,
    call: Any,
) -> tuple[dict[str, Any], _JudgeRequestTiming]:
    started_at = utc_now_iso()
    started_monotonic = monotonic_now()
    append_runtime_event(
        run_dir,
        event="judge.request.started",
        phase=phase,
        method=method,
        path=path,
        session_id=session_id,
        step_index=step_index,
        started_at=started_at,
    )
    ok = False
    error: str | None = None
    result: dict[str, Any] | None = None
    try:
        result = call()
        ok = True
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        finished_at = utc_now_iso()
        duration_seconds = elapsed_seconds(started_monotonic)
        observed_session_id = session_id
        if observed_session_id is None and isinstance(result, dict):
            candidate = result.get("session_id")
            if isinstance(candidate, str) and candidate:
                observed_session_id = candidate
        append_runtime_event(
            run_dir,
            event="judge.request.finished",
            phase=phase,
            method=method,
            path=path,
            session_id=observed_session_id,
            step_index=step_index,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            ok=ok,
            error=error,
        )
    assert result is not None
    return result, _JudgeRequestTiming(
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        method=method,
        path=path,
        ok=ok,
        error=error,
    )


def _seed_manifest_text(seed_manifest: Any) -> str:
    payload = seed_manifest
    if isinstance(seed_manifest, str):
        payload = json.loads(seed_manifest)
    if not isinstance(payload, list):
        raise RuntimeError("seed_manifest payload must be a JSON list")
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _write_results_seed_manifest(run_dir: Path, seed_manifest: Any) -> None:
    (_results_dir(run_dir) / SEED_MANIFEST_FILENAME).write_text(_seed_manifest_text(seed_manifest), encoding="utf-8")


def _initialize_server_run(run_dir: Path, config: ExperimentConfig) -> None:
    client = _server_admin_client(config)
    created, _ = _timed_judge_request(
        run_dir,
        method="POST",
        path="/sessions",
        session_id=None,
        step_index=None,
        phase="create_session",
        call=lambda: client.create_session(run_name=run_dir.name),
    )
    session_id = str(created["session_id"])
    session_token = str(created["session_token"])
    _agent_workspace_dir(run_dir).mkdir(parents=True, exist_ok=True)
    _write_server_session(run_dir, server_url=client.base_url, session_id=session_id, session_token=session_token)
    _refresh_agent_workspace(
        run_dir,
        config,
        created["observation"],
        session_client=JudgeHttpClient(client.base_url, timeout_seconds=config.judge_request_timeout_seconds, session_token=session_token),
        session_id=session_id,
    )


def _start_run_session(
    config_path: Path,
    *,
    mode: str | None = None,
    policy: str | None = None,
    output_root_override: Path | None = None,
) -> tuple[ExperimentConfig, Path]:
    config = load_experiment_config(config_path, mode=mode, policy=policy)
    output_root = output_root_override.resolve() if output_root_override else config.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    run_dir = _make_run_dir(output_root, config.name)
    run_started_at = utc_now_iso()
    append_runtime_event(run_dir, event="run.started", started_at=run_started_at)
    _write_run_config_snapshot(config_path, run_dir=run_dir, config=config)
    _initialize_server_run(run_dir, config)
    return config, run_dir


def _add_skill_surface_paths(run_dir: Path, payload: dict[str, Any], *, include_skill_surface: bool) -> None:
    if not include_skill_surface:
        return
    payload["skill_market_path"] = str(_skill_market_path(run_dir))
    payload["owned_skills_index_path"] = str(_owned_skills_index_path(run_dir))
    payload["owned_skills_dir"] = str(_owned_skills_dir(run_dir))


def _terminal_payload(
    run_dir: Path,
    snapshot: dict[str, Any],
    *,
    summary: dict[str, Any] | None,
    include_skill_surface: bool,
) -> dict[str, Any]:
    state = snapshot["state"]
    payload: dict[str, Any] = {
        "step_index": None,
        "decision": None,
        "accepted": None,
        "rejection_code": None,
        "rejection_reason": None,
        "action_record": None,
        "rejection_record": None,
        "terminal": True,
        "stop_code": state.get("stop_code"),
        "stop_reason": state.get("stop_reason"),
        "action_count": int(state.get("action_count", 0)),
        "request_count": int(state.get("request_count", 0)),
        "rejected_action_count": int(state.get("rejected_action_count", 0)),
        "cash": float(state.get("current_cash", 0.0)),
        "day": float(state.get("elapsed_day", 0.0)),
        "confirmed_best_activity": float(state.get("confirmed_best_activity", 0.0)),
        "confirmed_best_sequence": state.get("confirmed_best_sequence"),
        "state_path": str(_agent_workspace_dir(run_dir) / "state.json"),
        "history_path": str(_agent_workspace_dir(run_dir) / "history.json"),
        "available_actions_path": str(_agent_workspace_dir(run_dir) / "available_actions.json"),
    }
    _add_skill_surface_paths(run_dir, payload, include_skill_surface=include_skill_surface)
    if summary is not None:
        payload["summary"] = (
            summary
            if include_skill_surface
            else {key: value for key, value in summary.items() if key not in SKILL_SURFACE_STATE_KEYS}
        )
    return payload


def _result_payload(run_dir: Path, result: dict[str, Any], *, include_skill_surface: bool) -> dict[str, Any]:
    payload = compact_agent_facing_payload({key: value for key, value in result.items() if key != "observation"})
    if not include_skill_surface:
        payload = _strip_skill_surface_from_response(payload)
    payload["state_path"] = str(_agent_workspace_dir(run_dir) / "state.json")
    payload["history_path"] = str(_agent_workspace_dir(run_dir) / "history.json")
    payload["available_actions_path"] = str(_agent_workspace_dir(run_dir) / "available_actions.json")
    _add_skill_surface_paths(run_dir, payload, include_skill_surface=include_skill_surface)
    return payload


def run_agent_step(run_dir: Path, decision: AgentDecision) -> dict[str, Any]:
    config = load_experiment_config(run_dir / "config.toml")
    session_payload = _load_judge_session(run_dir)
    client = _server_session_client(config, session_payload=session_payload)
    session_id = str(session_payload["session_id"])
    step_started_at = utc_now_iso()
    step_started_monotonic = monotonic_now()
    observation_payload, observation_timing = _timed_judge_request(
        run_dir,
        method="GET",
        path=f"/sessions/{session_id}/observation",
        session_id=session_id,
        step_index=None,
        phase="step_observation",
        call=lambda: client.get_observation(session_id),
    )
    snapshot = observation_payload["observation"]
    if observation_payload["terminal"]:
        _refresh_agent_workspace(run_dir, config, snapshot, session_client=client, session_id=session_id)
        return _terminal_payload(
            run_dir,
            snapshot,
            summary=observation_payload.get("summary"),
            include_skill_surface=config.skill_surface_enabled,
        )

    step_index = int(snapshot["state"]["request_count"]) + 1
    step_dir = _process_dir(run_dir) / f"step_{step_index:03d}"
    pre_state = dict(snapshot["state"])
    pre_state["step_index"] = step_index
    pre_state["max_experiment_days"] = config.max_experiment_days
    pre_snapshot = dict(snapshot)
    pre_snapshot["state"] = pre_state
    _write_observation_snapshot(step_dir, pre_snapshot, include_skill_surface=config.skill_surface_enabled)
    write_json(step_dir / "chosen_action.json", _decision_payload(decision, include_skill_surface=config.skill_surface_enabled))

    try:
        result, action_timing = _timed_judge_request(
            run_dir,
            method="POST",
            path=f"/sessions/{session_id}/actions",
            session_id=session_id,
            step_index=step_index,
            phase="submit_action",
            call=lambda: client.submit_action(session_id, decision),
        )
    except Exception as exc:
        write_json(
            step_dir / "step_meta.json",
            {
                "step_index": step_index,
                "step_started_at": step_started_at,
                "step_finished_at": utc_now_iso(),
                "step_duration_seconds": elapsed_seconds(step_started_monotonic),
                "codex_thread_id": _env_optional_str("CODEX_THREAD_ID"),
                "agent_backend": _env_optional_str("TADACAMPAIGN_AGENT_BACKEND"),
                "agent_session_id": _env_optional_str("TADACAMPAIGN_AGENT_SESSION_ID"),
                "decision": _decision_payload(decision, include_skill_surface=config.skill_surface_enabled),
                "accepted": None,
                "rejection_code": None,
                "rejection_reason": None,
                "terminal": None,
                "stop_code": None,
                "stop_reason": None,
                "request_count": None,
                "action_count": None,
                "observation_request": observation_timing.as_dict(),
                "action_request": {
                    "started_at": None,
                    "finished_at": None,
                    "duration_seconds": None,
                    "method": "POST",
                    "path": f"/sessions/{session_id}/actions",
                    "ok": False,
                    "error": str(exc),
                },
            },
        )
        raise
    judge_response_payload = {key: value for key, value in result.items() if key != "observation"}
    if not config.skill_surface_enabled:
        judge_response_payload = _strip_skill_surface_from_response(judge_response_payload)
    write_json(step_dir / "judge_response.json", judge_response_payload)
    write_json(
        step_dir / "step_meta.json",
        {
            "step_index": step_index,
            "step_started_at": step_started_at,
            "step_finished_at": utc_now_iso(),
            "step_duration_seconds": elapsed_seconds(step_started_monotonic),
            "codex_thread_id": _env_optional_str("CODEX_THREAD_ID"),
            "agent_backend": _env_optional_str("TADACAMPAIGN_AGENT_BACKEND"),
            "agent_session_id": _env_optional_str("TADACAMPAIGN_AGENT_SESSION_ID"),
            "decision": _decision_payload(decision, include_skill_surface=config.skill_surface_enabled),
            "accepted": result.get("accepted"),
            "rejection_code": result.get("rejection_code"),
            "rejection_reason": result.get("rejection_reason"),
            "terminal": result.get("terminal"),
            "stop_code": result.get("stop_code"),
            "stop_reason": result.get("stop_reason"),
            "request_count": result.get("request_count"),
            "action_count": result.get("action_count"),
            "observation_request": observation_timing.as_dict(),
            "action_request": action_timing.as_dict(),
        },
    )
    _refresh_agent_workspace(run_dir, config, result["observation"], session_client=client, session_id=session_id)
    return _result_payload(run_dir, result, include_skill_surface=config.skill_surface_enabled)


def _ensure_backend_error_stop(run_dir: Path, reason: str) -> None:
    config = load_experiment_config(run_dir / "config.toml")
    session_payload = _load_judge_session(run_dir)
    client = _server_admin_client(config, session_payload=session_payload)
    session_id = str(session_payload["session_id"])
    append_runtime_event(
        run_dir,
        event="backend.force_stop",
        session_id=session_id,
        reason=reason,
    )
    result, _ = _timed_judge_request(
        run_dir,
        method="POST",
        path=f"/sessions/{session_id}/force-stop",
        session_id=session_id,
        step_index=None,
        phase="force_stop",
        call=lambda: client.force_stop(session_id, reason=reason),
    )
    _refresh_agent_workspace(
        run_dir,
        config,
        result["observation"],
        session_client=_server_session_client(config, session_payload=session_payload),
        session_id=session_id,
    )


def _write_run_outputs_from_server(run_dir: Path, config: ExperimentConfig, *, agent_session_id: str | None) -> None:
    session_payload = _load_judge_session(run_dir)
    client = _server_admin_client(config, session_payload=session_payload)
    session_id = str(session_payload["session_id"])
    exported, _ = _timed_judge_request(
        run_dir,
        method="GET",
        path=f"/sessions/{session_id}/export",
        session_id=session_id,
        step_index=None,
        phase="export_session",
        call=lambda: client.export_session(session_id),
    )
    _write_results_seed_manifest(run_dir, exported.get("seed_manifest"))
    summary = dict(exported["summary"])
    summary["run_name"] = run_dir.name
    summary["run_mode"] = config.run_mode
    summary["execution_model"] = "codex_sandboxed_host_agent_host_judge"
    summary["judge_session_id"] = session_id
    summary["judge_server_url"] = client.base_url
    summary.update(_agent_summary_fields(run_dir, backend=config.agent_backend, session_id=agent_session_id))
    if config.agent_backend == "policy" and config.agent_policy is not None:
        summary["agent_policy"] = config.agent_policy
        if config.agent_policy in {"d1_k1"}:
            summary["decision_model"] = str(config.agent_policy_params.get("model") or "liquid/d1")
        elif config.agent_policy in {"laya_k1"}:
            summary["decision_model"] = str(
                config.agent_policy_params.get("model") or "convaiinnovations/laya-free"
            )
        elif config.agent_policy in {"jev_choice", "jev_k1"}:
            summary["decision_model"] = str(
                config.agent_policy_params.get("model") or "typesafe-ai/jev"
            )

    write_json(_results_dir(run_dir) / "summary.json", summary)
    write_jsonl(_results_dir(run_dir) / "action_log.jsonl", list(exported["action_history"]))
    write_jsonl(_results_dir(run_dir) / "screen_record_history.jsonl", list(exported["screen_record_history"]))
    write_jsonl(_results_dir(run_dir) / "rejection_log.jsonl", list(exported["rejection_history"]))
    audit_path = _results_dir(run_dir) / "judge_audit.jsonl"
    audit_payload, _ = _timed_judge_request(
        run_dir,
        method="GET",
        path=f"/sessions/{session_id}/audit",
        session_id=session_id,
        step_index=None,
        phase="fetch_audit",
        call=lambda: {"audit": client.fetch_audit(session_id)},
    )
    audit_path.write_text(str(audit_payload["audit"]), encoding="utf-8")
    curve_csv, curve_png = write_curve(
        _results_dir(run_dir),
        list(exported["action_history"]),
        initial_cash=config.initial_cash,
    )
    artifacts: dict[str, Any] = {
        "curve_csv": str(curve_csv),
        "curve_png": str(curve_png),
        "results_dir": str(_results_dir(run_dir)),
        "judge_audit_jsonl": str(audit_path),
        "rejection_log_jsonl": str(_results_dir(run_dir) / "rejection_log.jsonl"),
    }
    optional_artifacts = {
        "agent_stdout_log": _results_dir(run_dir) / "agent_stdout.log",
        "agent_final_result_json": _results_dir(run_dir) / "agent_final_result.json",
        "agent_command_txt": _results_dir(run_dir) / "agent_command.txt",
        "agent_stderr_log": _results_dir(run_dir) / "agent_stderr.log",
        "jev_decisions_jsonl": _results_dir(run_dir) / "jev_decisions.jsonl",
        "agent_resume_meta_json": _results_dir(run_dir) / "agent_resume_meta.json",
        "agent_budget_json": _results_dir(run_dir) / "agent_budget.json",
        "codex_events_jsonl": _results_dir(run_dir) / "codex_events.jsonl",
        "codex_final_result_json": _results_dir(run_dir) / "codex_final_result.json",
        "codex_command_txt": _results_dir(run_dir) / "codex_command.txt",
        "codex_stderr_log": _results_dir(run_dir) / "codex_stderr.log",
        "codex_resume_meta_json": _results_dir(run_dir) / "codex_resume_meta.json",
        "codex_isolation_json": _process_dir(run_dir) / "codex_runtime" / "codex_isolation.json",
        "codex_final_result_schema_json": _agent_workspace_dir(run_dir) / "codex_final_result_schema.json",
        "handoff_drill_meta_json": _results_dir(run_dir) / "handoff_drill_meta.json",
        "runtime_timeline_jsonl": _results_dir(run_dir) / "runtime_timeline.jsonl",
        "agent_prompt_txt": _results_dir(run_dir) / "agent_prompt.txt",
    }
    for key, path in optional_artifacts.items():
        if path.exists():
            artifacts[key] = str(path)
    prompt_files = sorted(_results_dir(run_dir).glob("agent_prompt_attempt*.txt"))
    if prompt_files:
        artifacts["agent_prompt_files"] = [str(path) for path in prompt_files]
    codex_prompt_files = sorted(_results_dir(run_dir).glob("codex_prompt_attempt*.txt"))
    if codex_prompt_files:
        artifacts["codex_prompt_files"] = [str(path) for path in codex_prompt_files]
    handoff_prompt_files = sorted(_results_dir(run_dir).glob("*handoff_prompt_attempt*.txt"))
    if handoff_prompt_files:
        artifacts["handoff_prompt_files"] = [str(path) for path in handoff_prompt_files]
    run_started_at = _runtime_summary_fields(run_dir).get("run_started_at")
    run_finished_at = utc_now_iso()
    run_duration_seconds = None
    if isinstance(run_started_at, str):
        try:
            run_duration_seconds = max(
                (
                    dt.datetime.fromisoformat(run_finished_at)
                    - dt.datetime.fromisoformat(run_started_at)
                ).total_seconds(),
                0.0,
            )
        except ValueError:
            run_duration_seconds = None
    append_runtime_event(
        run_dir,
        event="run.finished",
        started_at=run_started_at,
        finished_at=run_finished_at,
        duration_seconds=run_duration_seconds,
    )
    trajectory_index_path = build_tool_trajectory_index(run_dir)
    trajectory_index_payload = json.loads(trajectory_index_path.read_text(encoding="utf-8"))
    trajectory_jsonl_path = trajectory_index_path.with_suffix(".jsonl")
    trajectory_call_log_paths = sorted(
        {
            str(log_path)
            for call in trajectory_index_payload.get("calls", [])
            for log_path in [
                (call.get("logs") or {}).get("tool_calls_jsonl")
                if isinstance(call, dict) and isinstance(call.get("logs"), dict)
                else None
            ]
            if isinstance(log_path, str) and log_path
        }
    )
    summary.update(_runtime_summary_fields(run_dir))
    summary["tool_trajectory_count"] = int(trajectory_index_payload.get("tool_call_count", 0))
    summary["tool_trajectory_tool_ids"] = list(trajectory_index_payload.get("tool_ids", []))
    summary["tool_trajectory_warnings"] = list(trajectory_index_payload.get("warnings", []))
    summary["artifacts"] = artifacts
    artifacts["tool_trajectory_index_json"] = str(trajectory_index_path)
    artifacts["tool_trajectory_index_jsonl"] = str(trajectory_jsonl_path)
    if len(trajectory_call_log_paths) == 1:
        artifacts["tool_calls_jsonl"] = trajectory_call_log_paths[0]
    elif trajectory_call_log_paths:
        artifacts["tool_calls_jsonl_all"] = trajectory_call_log_paths
    if not config.skill_surface_enabled:
        summary = _strip_skill_surface_from_response(summary)
    write_json(_results_dir(run_dir) / "summary.json", summary)
    (_results_dir(run_dir) / "summary.md").write_text(
        render_markdown_summary(summary, run_dir),
        encoding="utf-8",
    )


def run_experiment(
    config_path: Path,
    *,
    mode: str | None = None,
    policy: str | None = None,
    output_root_override: Path | None = None,
) -> Path:
    config, run_dir = _start_run_session(
        config_path,
        mode=mode,
        policy=policy,
        output_root_override=output_root_override,
    )

    agent_session_id: str | None = None
    try:
        backend_result = run_backend_episode(
            config,
            run_dir=run_dir,
            agent_workspace=_agent_workspace_dir(run_dir),
        )
        raw_session_id = backend_result.get("session_id") or backend_result.get("thread_id")
        agent_session_id = str(raw_session_id) if raw_session_id else None
    except BackendError as exc:
        agent_session_id = exc.session_id
        _ensure_backend_error_stop(run_dir, f"backend_error: {exc}")
    else:
        session_payload = _load_judge_session(run_dir)
        client = _server_session_client(config, session_payload=session_payload)
        session_id = str(session_payload["session_id"])
        observation, _ = _timed_judge_request(
            run_dir,
            method="GET",
            path=f"/sessions/{session_id}/observation",
            session_id=session_id,
            step_index=None,
            phase="final_observation",
            call=lambda: client.get_observation(session_id),
        )
        if not observation["terminal"]:
            early_exit_reason = (
                "backend_error: Codex exited before the experiment reached a terminal state"
                if config.agent_backend == "codex"
                else f"backend_error: {config.agent_backend} exited before the experiment reached a terminal state"
            )
            _ensure_backend_error_stop(run_dir, early_exit_reason)
        else:
            _refresh_agent_workspace(
                run_dir,
                config,
                observation["observation"],
                session_client=client,
                session_id=session_id,
            )

    _write_run_outputs_from_server(run_dir, config, agent_session_id=agent_session_id)
    return run_dir


def summarize_run(run_dir: Path) -> str:
    summary = load_run_summary(run_dir)
    return render_markdown_summary(summary, run_dir)


def load_run_summary(run_dir: Path) -> dict[str, Any]:
    summary_path = _results_dir(run_dir) / "summary.json"
    return json.loads(summary_path.read_text(encoding="utf-8"))


def assert_run_succeeded(run_dir: Path) -> dict[str, Any]:
    summary = load_run_summary(run_dir)
    stop_reason = str(summary.get("stop_reason") or "")
    if stop_reason.startswith("backend_error:"):
        raise RuntimeError(f"Run {run_dir} failed: {stop_reason}")
    return summary

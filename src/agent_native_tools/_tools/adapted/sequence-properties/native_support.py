"""Read-only decision evidence from public workspace objects; no hidden world."""
from __future__ import annotations

import collections
import hashlib
import json
import math
import re
from typing import Any

VERSION = "agent-native-v2.0"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def visible_fixture(state: dict[str, Any]) -> dict[str, Any]:
    """Use the exact currently visible DNA objects; do not inspect route labels."""
    ids = state.get("visible_seed_ids")
    seeds = state.get("seeds")
    if not isinstance(ids, list) or not isinstance(seeds, list) or not ids:
        raise ValueError("visible mode needs public state.visible_seed_ids and state.seeds")
    wanted = set(ids)
    selected = [s for s in seeds if isinstance(s, dict) and s.get("seed_id") in wanted]
    if {s["seed_id"] for s in selected} != wanted or len(selected) != len(wanted):
        raise ValueError("visible seed identifiers are missing or duplicated")
    sequences: dict[str, str] = {}
    windows: dict[str, list[int]] = {}
    for seed in selected:
        key, dna = seed["seed_id"], seed.get("start_sequence")
        if not isinstance(key, str) or not isinstance(dna, str) or not dna:
            raise ValueError("each visible seed needs a string id and start_sequence")
        sequences[key] = dna
        for lib in seed.get("libraries", []):
            interval = lib.get("mutable_interval_aa", {})
            lo = interval.get("start_index_0based_inclusive")
            hi = interval.get("end_index_0based_exclusive")
            if isinstance(lo, bool) or isinstance(hi, bool) or not isinstance(lo, int) or not isinstance(hi, int):
                raise ValueError("visible library interval must have integer half-open bounds")
            if not 0 <= lo <= hi <= len(dna) // 3:
                raise ValueError("visible library interval lies outside its sequence")
            windows[f"{key}/{lib['library_id']}"] = [lo, hi]
    return {"sequences": sequences, "aa_windows": windows}


def reward_contract(playbook: str) -> dict[str, Any] | None:
    """Parse the public, config-rendered specification rather than hard-code tiers."""
    asset = re.search(r"base_asset_value\s*=\s*([\d.]+)", playbook)
    baseline = re.search(r"baseline_activity\s*=\s*([\d.]+)", playbook)
    segments = re.findall(
        r"tier\s+\d+:\s*`\[([\d.]+),\s*([+\w.]+)\)`[^\n]*?multiplier\s*=\s*([\d.]+)",
        playbook, flags=re.I)
    if not asset or not baseline or not segments:
        return None
    unique = list(dict.fromkeys(segments))  # The runtime repeats the public table.
    tiers = [(float(a), float(b.replace("+", "")), float(m)) for a, b, m in unique]
    if any(a <= 0 or b <= a or m <= 0 for a, b, m in tiers):
        raise ValueError("public reward tiers have invalid boundaries")
    if any(tiers[i][1] != tiers[i + 1][0] for i in range(len(tiers) - 1)):
        raise ValueError("public reward tiers are not contiguous")
    return {"base_asset_value": float(asset[1]), "baseline_activity": float(baseline[1]),
            "tiers": tiers, "source": "public_runtime_agent_playbook"}


def incremental_reward(current: float, target: float, contract: dict[str, Any]) -> float:
    if target <= current:
        return 0.0
    scale = contract["base_asset_value"] * contract["baseline_activity"]
    return sum(scale * m * (1 / max(current, lo) - 1 / min(target, hi))
               for lo, hi, m in contract["tiers"] if min(target, hi) > max(current, lo))


def break_even(current: float, cost: float, contract: dict[str, Any]) -> float | None:
    if current <= 0 or cost < 0:
        raise ValueError("break-even requires positive activity and nonnegative cost")
    if cost == 0:
        return current
    if incremental_reward(current, math.inf, contract) <= cost:
        return None
    left, right = current, max(current * 2, current + 1)
    for _ in range(1024):
        if incremental_reward(current, right, contract) >= cost:
            break
        right *= 2
    for _ in range(90):
        mid = (left + right) / 2
        if incremental_reward(current, mid, contract) < cost:
            left = mid
        else:
            right = mid
    return right


def scientific_brief(tool: str, artifact: dict[str, Any], fixture: dict[str, Any],
                     contract: dict[str, Any]) -> dict[str, Any]:
    p = artifact["payload"]
    seqs = fixture.get("sequences", {})
    bound = [{"id": k, "length_symbols": len(v),
              "unit": "raw input symbols" if tool == "pairwise-alignment" else "nt",
              **({} if tool == "pairwise-alignment" else {"length_nt": len(v)}),
              "sha256": hashlib.sha256(v.encode()).hexdigest()} for k, v in seqs.items()]
    facts: dict[str, Any]
    if tool == "codon-usage":
        facts = {"complete_codon_totals": {k: sum(v.values()) for k, v in p["codon_counts"].items()}}
    elif tool == "sequence-properties":
        facts = {k: p.get(k) for k in ["sequence_lengths", "gc_fraction", "dna_alphabet_ok", "sequence_qc"]}
    elif tool == "transcription-translation":
        facts = {k: p.get(k) for k in ["protein_lengths", "internal_stop_counts"]}
        facts["explicit_stop_semantics"] = {
            k: v.get("stop_semantics") for k, v in p.get("compatibility", {}).get("sequence_records", {}).items()}
    elif tool == "pairwise-alignment":
        facts = {"legacy_ungapped_prefix_identity": p["pairwise_alignments"],
                 "comparison_units": "raw input symbols; DNA nt when supplied DNA, not AA substitutions",
                 "global_alignment_method": "Needleman-Wunsch, match=1 mismatch=-1 gap=-1",
                 "global_alignment_identity": [
                     {"left": x.get("left_id"), "right": x.get("right_id"),
                      "identity": x.get("alignment_identity"),
                      "gap_columns": x.get("alignment", {}).get("gap_columns")}
                     for x in p.get("compatibility", {}).get("pair_metadata", [])]}
    elif tool == "msa-statistics":
        variable = [x["position"] for x in p["columns"] if x.get("entropy_bits", 0) > 1e-12]
        facts = {"n_input_sequences": len(seqs), "alignment_length_aa": p["alignment_length"],
                 "variable_columns_0based_aa": variable,
                 "variable_site_evidence": "unavailable_singleton" if len(seqs) < 2 else "observed_input_only",
                 "alignment_is_pre_aligned": True}
    elif tool == "motif-search":
        facts = {"motif_counts": {k: {m: len(pos) for m, pos in v.items()}
                                  for k, v in p.get("motif_positions", {}).items()},
                 "frame_aware_stop_status": p.get("frame_aware_stop_hits", {}).get("status", "not_classified"),
                 "raw_motif_is_not_in_frame_stop_evidence": True}
    elif tool == "sequence-statistics":
        facts = {k: p.get(k) for k in ["n_sequences", "unique_dna_sequences", "unique_proteins", "length_distribution"]}
        facts["mutation_position_evidence"] = "not_provided_by_aggregate_statistics"
    elif tool == "filter-sequences":
        facts = {k: p.get(k) for k in ["passing_ids", "rejected_ids", "rejection_reasons", "predicate_parameters"]}
        facts["passing_means"] = "passed_declared_QC_only"
    elif tool == "distance-calculations":
        facts = {k: p.get(k) for k in ["dna_pair_count", "aa_pair_count"]}
        facts["dna_distance_pairs"] = p.get("dna_hamming_pairs", [])[:12]
        facts["aa_distance_pairs"] = p.get("aa_hamming_pairs", [])[:12]
    elif tool == "sequence-slicing":
        facts = {"aa_nt_mapping_count": len(p.get("aa_to_nt_mappings", [])),
                 "localized_row_count": len(p.get("historical_row_localization", [])),
                 "mapping_rule": "frame0, nt=[3*aa_start,3*aa_end), both half-open"}
    elif tool == "reverse-complement":
        facts = {"reverse_complement_sha256": p.get("reverse_complement_sha256"),
                 "result_is_reoriented_object": True, "in_place_coding_frame_preservation": False}
    elif tool == "restriction-sites":
        facts = {"site_counts": {k: {e: len(v) for e, v in es.items()}
                                  for k, es in p.get("restriction_sites", {}).items()},
                 "functional_activity_evidence": "not_established"}
    else:
        raise ValueError("unknown scientific tool")
    return {"input_objects": bound, "verified_calculation": facts,
            "supports": contract["supports"], "does_not_establish": contract["does_not_establish"],
            "decision_handoff": contract["decision_handoff"],
            "activity_prediction": {"status": "not_established", "success_probability": None}}


def merge_memory(state: dict[str, Any], history: dict[str, Any], old: dict[str, Any] | None,
                 run_identity: str) -> dict[str, Any]:
    """Accumulate only actually observed public actions, retaining explicit gaps."""
    count = int(state.get("action_count", 0))
    if old and old.get("run_identity") != run_identity:
        raise ValueError("decision memory belongs to a different workspace")
    if old and count < old.get("last_action_count", 0):
        raise ValueError("public action count moved backwards; use a fresh workspace memory")
    entries = dict(old.get("observed_actions", {})) if old else {}
    actions = history.get("recent_actions", [])
    if not isinstance(actions, list) or len(actions) > count:
        raise ValueError("history.recent_actions is inconsistent with state.action_count")
    first = count - len(actions) + 1
    for i, a in enumerate(actions, first):
        # No free-text historical route recommendations or hidden fields are read.
        entries[str(i)] = {k: a.get(k) for k in ["action", "seed_id", "library_id", "actual_batch_size",
            "new_sequence_count", "reward_earned", "confirmed_best_before", "confirmed_best_after",
            "confirmed_activity", "cash_after", "elapsed_day_after"] if k in a}
        if a.get("action") == "mine_library":
            entries[str(i)]["new_pending_candidate"] = bool(a.get("pending_confirmation_added") or
                                                          a.get("pending_confirmation_added_records"))
    missing = [i for i in range(1, count + 1) if str(i) not in entries]
    return {"schema_version": 1, "run_identity": run_identity, "last_action_count": count,
            "observed_actions": entries, "missing_action_indices": missing,
            "coverage": "complete_observed_prefix" if not missing else "partial_observed_prefix"}


def decision_support(state: dict[str, Any], history: dict[str, Any], actions: dict[str, Any],
                     playbook: str, memory: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
    if not state:
        return {"status": "unavailable_no_public_state", "future_success_probability": None}
    current = finite(state.get("confirmed_best_activity"), "confirmed_best_activity")
    econ = state.get("economic_summary", {})
    confirm_cost = finite(econ.get("action_total_costs", {}).get("confirm_hit"), "confirm total cost")
    contract = reward_contract(playbook)
    if contract and contract["baseline_activity"] != state.get("baseline_activity"):
        raise ValueError("public reward baseline disagrees with state")
    result: dict[str, Any] = {"status": "public_evidence_only", "decision_action_count": state.get("action_count"),
        "current_confirmed_activity": current, "stop_cash_now": state.get("current_cash"),
        "confirmation_total_cost": confirm_cost,
        "reward_model_status": "parsed_public_playbook" if contract else "unavailable",
        "probability_of_future_improvement": None,
        "expected_value_status": "not_estimated_without_a_validated_discovery_model",
        "affordability_is_not_positive_expected_value": True,
        "scientific_evidence_scope": [x["id"] for x in brief["input_objects"]],
        "history_coverage": memory["coverage"], "missing_action_indices": memory["missing_action_indices"],
        "available_preparations": {"seeds": len(actions.get("prepare_seed", [])),
                                   "libraries": len(actions.get("prepare_library", []))}}
    if contract:
        result["confirmation_break_even_true_activity"] = break_even(current, confirm_cost, contract)
    clip = state.get("screening_noise_clip") if state.get("screening_noise_enabled") else 0.0
    clip_valid = isinstance(clip, (int, float)) and not isinstance(clip, bool) and 0 <= clip < 1
    candidates = []
    for c in actions.get("confirm_hit", []):
        obs = finite(c["observed_activity"], "observed_activity")
        item = {"screen_record_id": c["screen_record_id"], "observed_activity": obs,
                "observed_is_not_confirmed": True}
        if contract and clip_valid:
            lo, hi = obs / (1 + clip), obs / (1 - clip)
            lower = incremental_reward(current, lo, contract) - confirm_cost
            upper = incremental_reward(current, hi, contract) - confirm_cost
            item.update(true_activity_noise_bounds=[lo, hi], net_confirmation_reward_bounds=[lower, upper],
                        bound_type="declared_noise_clip_bounds_not_confidence_interval",
                        cannot_repay_confirmation_under_noise_bounds=upper <= 0,
                        covers_confirmation_under_noise_bounds=lower > 0)
        candidates.append(item)
    result["pending_confirmation_assessments"] = candidates
    visible_hashes: dict[str, list[str]] = {}
    for seed in state.get("seeds", []):
        if seed.get("seed_id") not in state.get("visible_seed_ids", []):
            continue
        h = hashlib.sha256(seed["start_sequence"].encode()).hexdigest()
        visible_hashes.setdefault(h, []).append(seed["seed_id"])
    artifact_hashes = {x["sha256"] for x in brief["input_objects"]}
    result["scientific_objects_matching_current_visible_backgrounds"] = [
        key for h, keys in visible_hashes.items() if h in artifact_hashes for key in keys]
    result["visible_backgrounds_requiring_fresh_scientific_analysis"] = [
        key for h, keys in visible_hashes.items() if h not in artifact_hashes for key in keys]
    result["identical_visible_DNA_groups"] = [keys for keys in visible_hashes.values() if len(keys) > 1]
    result["identical_DNA_does_not_imply_identical_library_options"] = True
    gains = [(int(ix), a) for ix, a in memory["observed_actions"].items()
             if a.get("action") == "confirm_hit" and
             a.get("confirmed_best_after", 0) > a.get("confirmed_best_before", math.inf)]
    if gains:
        ix, a = max(gains, key=lambda x: x[0])
        result["since_last_observed_confirmed_improvement"] = {
            "last_improvement_action": ix, "cash_at_improvement": a["cash_after"],
            "net_cash_change_since": state["current_cash"] - a["cash_after"],
            "simulated_days_since": state["elapsed_day"] - a["elapsed_day_after"],
            "interpretation": "observed sunk outcomes, not a future probability or automatic stop rule"}
    per_lib: dict[str, Any] = {}
    last_observed = 0
    for ix, a in sorted(memory["observed_actions"].items(), key=lambda kv: int(kv[0])):
        if int(ix) > last_observed + 1:
            for row in per_lib.values():
                row["recent_no_new_pending_streak"] = 0
        last_observed = int(ix)
        if a.get("action") != "mine_library":
            continue
        key = a["library_id"]
        row = per_lib.setdefault(key, {"observed_mine_batches": 0, "sampled_count": 0,
                    "reported_new_sequences": 0, "recent_no_new_pending_streak": 0})
        row["observed_mine_batches"] += 1
        row["sampled_count"] += a.get("actual_batch_size", 0)
        row["reported_new_sequences"] += a.get("new_sequence_count", 0)
        row["recent_no_new_pending_streak"] = 0 if a["new_pending_candidate"] else row["recent_no_new_pending_streak"] + 1
    mine_options = []
    for lib in actions.get("mine_library", []):
        item = {k: lib.get(k) for k in ["library_id", "seed_id", "screenable_sequence_count",
                                       "screened_sequence_count", "remaining_screenable_sequence_count"]}
        item["observed_sampling_evidence"] = per_lib.get(lib["library_id"], {"observed_mine_batches": 0})
        item["evidence_limit"] = "no_new_pending_is_not_a_probability_estimate_or_proof_of_no_future_hit"
        item["batch_options"] = []
        for option in lib.get("batch_options", []):
            total = finite(option["total_cost"], "mine total cost") + confirm_cost
            item["batch_options"].append({"batch_size": option["batch_size"],
                "mine_plus_one_confirm_total_cost": total,
                "leaves_confirm_capacity": option.get("leaves_confirm_capacity"),
                "break_even_true_activity_if_discovery_certain": break_even(current, total, contract) if contract else None})
        mine_options.append(item)
    result["available_mining_comparisons"] = mine_options
    result["stopping_and_switching_check"] = {
        "legal_stop": bool(actions.get("stop")),
        "fixed_stop_threshold": None,
        "review_conditions": ["confirmation candidate may not repay confirmation cost",
                              "continued sampling has declining observed novelty or repeated no-new-pending batches",
                              "alternative visible windows may provide distinct evidence",
                              "remaining cash and days are constraints, not a reason to spend them"],
        "conclusion": "Compare stop, confirmation and visible alternatives; do not infer a hidden best route or a guaranteed future gain."}
    return result

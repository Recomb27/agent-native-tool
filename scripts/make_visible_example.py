#!/usr/bin/env python3
"""Create synthetic public state for visible and review modes; no real labels."""
import argparse
import json
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    names = ["state.json", "history.json", "available_actions.json", "agent_playbook.md"]
    if any((root / name).exists() for name in names):
        parser.error("Public example files already exist; choose a fresh workspace")
    state = {
        "visible_seed_ids": ["example"], "seeds": [{
            "seed_id": "example", "start_sequence": "ATG" + "GCT" * 166,
            "libraries": [{"library_id": "L", "mutable_interval_aa": {
                "start_index_0based_inclusive": 0, "end_index_0based_exclusive": 5}}],
        }],
        "baseline_activity": 1.0, "confirmed_best_activity": 1.0,
        "current_cash": 100000.0, "elapsed_day": 0.0, "remaining_days": 180.0,
        "action_count": 0, "screening_noise_enabled": True, "screening_noise_clip": 0.2,
        "economic_summary": {"action_total_costs": {"confirm_hit": 10067.0}},
    }
    actions = {"stop": {"action": "stop"}, "confirm_hit": [], "prepare_seed": [],
               "prepare_library": [], "mine_library": []}
    for name, value in [("state.json", state), ("history.json", {"recent_actions": []}),
                        ("available_actions.json", actions)]:
        (root / name).write_text(json.dumps(value, indent=2) + "\n")
    playbook = "\n".join([
        "# Synthetic public reward rules", "baseline_activity = 1.0",
        "base_asset_value = 100000.0",
        "- tier 1: \x60[1.0, 1.314)\x60, multiplier = 1.5",
        "- tier 2: \x60[1.314, 3.78)\x60, multiplier = 2.4",
        "- tier 3: \x60[3.78, 7.44)\x60, multiplier = 18.0",
        "- tier 4: \x60[7.44, +inf)\x60, multiplier = 30.0",
        "This is a synthetic interface example, not a measured campaign.",
    ])
    (root / "agent_playbook.md").write_text(playbook + "\n")
    print(json.dumps({"workspace": str(root), "synthetic": True, "model_calls": 0}))

if __name__ == "__main__":
    main()


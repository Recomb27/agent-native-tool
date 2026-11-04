#!/usr/bin/env python3
"""Prepare one reference campaign configuration without launching a model."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from agent_native_tools.cli import TOOLS, tool_path
from TadACampaign.runner import load_experiment_config
from TadACampaign.skill_market import load_skill_market_catalog

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=["original", "adapted", "no-tool"], required=True)
    parser.add_argument("--tool", choices=TOOLS)
    parser.add_argument("--prepared-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260329)
    args = parser.parse_args()
    if (args.arm == "no-tool") == bool(args.tool):
        parser.error("Tool arms require --tool; no-tool must omit it")
    prepared = args.prepared_root.expanduser().resolve()
    if not (prepared / "manifest.json").is_file():
        parser.error("Prepared data manifest is missing")
    root = args.output_dir.expanduser().resolve()
    if root.exists():
        parser.error("Output already exists; select a new setup directory")
    root.mkdir(parents=True)
    surface = root / "tool_surface"
    surface.mkdir()
    skills = []
    if args.tool:
        package = surface / "sources" / args.tool / "source"
        shutil.copytree(tool_path(args.tool, args.arm), package,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        skills.append({"skill_id": args.tool, "display_name": args.tool, "price": 0,
                       "summary": "Assigned " + args.arm + " scientific tool",
                       "package_root": "sources/" + args.tool})
    market = surface / "skill_market.json"
    market.write_text(json.dumps({"skills": skills}, indent=2) + "\n")
    manifest_hash = hashlib.sha256((prepared / "manifest.json").read_bytes()).hexdigest()
    template = [
        'name = ' + json.dumps("public_" + args.arm + "_" + (args.tool or "reference")),
        'prepared_root = ' + json.dumps(str(prepared)),
        'variant = "dna"', 'output_root = "./runs"', 'seed = ' + str(args.seed),
        'initial_cash = 100000.0', 'base_asset_value = 100000.0',
        'baseline_activity = 1.0', 'screening_noise_enabled = true',
        'screening_noise_sigma = 0.10', 'screening_noise_clip = 0.20',
        'prepare_seed_cost = 365.0', 'prepare_library_cost = 142.0',
        'daily_burn_cost = 800.0', 'prepare_seed_days = 3.0',
        'prepare_library_days = 2.0', 'confirm_hit_cost = 467.0',
        'confirm_hit_days = 12.0', 'initial_visible_seeds = ["seed1"]',
        'initial_prepared_seeds = []', 'max_experiment_days = 180.0',
        'agent_timeout_seconds = 3600',
    ]
    for lo, hi, multiplier in [(1,1.314,1.5),(1.314,3.78,2.4),(3.78,7.44,18),(7.44,"inf",30)]:
        template += ["", "[[reward_tiers]]", "min_activity = " + str(float(lo)),
                     "max_activity = " + (json.dumps(hi) if isinstance(hi,str) else str(hi)),
                     "multiplier = " + str(float(multiplier))]
    for size, cost, days in [(780,1200,9),(880,1260,9),(1020,1340,10),(1280,1400,10)]:
        template += ["", "[[mine_batch_menu]]", "batch_size = " + str(size),
                     "cost = " + str(float(cost)), "time_days = " + str(float(days))]
    template += ["", "[agent]", 'mode = "agent"', 'backend = "codex"',
                 'model = "gpt-6.1-sol"', 'reasoning_effort = "max"', "",
                 "[judge]", 'server_url = "http://127.0.0.1:0"', "request_timeout_seconds = 20",
                 'skill_market_manifest = "./tool_surface/skill_market.json"',
                 "skill_market_enabled = false",
                 "initial_owned_skill_ids = " + json.dumps([args.tool] if args.tool else []),
                 "purchase_skill_days = 1.0", ""]
    config = root / "config.toml"
    config.write_text("\n".join(template))
    parsed = load_experiment_config(config)
    catalog = load_skill_market_catalog(market)
    assert len(catalog.entries_by_id) == int(bool(args.tool))
    receipt = {"status": "prepared", "model_calls": 0, "arm": args.arm, "tool": args.tool,
               "seed": args.seed, "prepared_manifest_sha256": manifest_hash,
               "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
               "market_sha256": hashlib.sha256(market.read_bytes()).hexdigest(),
               "controller_maximum_concurrent_runs": 10,
               "tool_files": {str(p.relative_to(surface)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in surface.rglob("*") if p.is_file()},
               "historical_byte_identical_prompt": False}
    (root / "SETUP.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": "prepared", "config": str(config), "model_calls": 0}))

if __name__ == "__main__":
    main()


"""Independent checks for the released simulator and offline setup workflow."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from TadACampaign.environment import TadACampaignEnvironment
from TadACampaign.models import AgentDecision
from TadACampaign.runner import load_experiment_config, _load_agent_playbook_text
from .conftest import make_config, make_prepared_rows, make_seed_definitions

ROOT = Path(__file__).resolve().parents[1]


def test_screening_requires_confirmation_before_reward(tmp_path):
    env = TadACampaignEnvironment(variant="dna", seed_definitions=make_seed_definitions(),
                                 prepared_rows=make_prepared_rows(), config=make_config(tmp_path))
    env.apply(AgentDecision(action="prepare_seed", seed_id="seed1"))
    env.apply(AgentDecision(action="prepare_library", seed_id="seed1", library_id="seed1_library1"))
    env.apply(AgentDecision(action="mine_library", seed_id="seed1", library_id="seed1_library1", batch_size=1))
    assert env.confirmed_best_activity == 1
    assert env.total_reward_earned == 0
    record = env.screen_record_history[0]
    env.apply(AgentDecision(action="confirm_hit", screen_record_id=record["screen_record_id"]))
    assert env.confirmed_best_activity == 2
    assert env.total_reward_earned == pytest.approx(1000 * (1 - 1/2))
    assert env.cash == pytest.approx(1000 - env.total_cost_paid + 500)
    env.apply(AgentDecision(action="stop", reasoning="Synthetic completion"))
    assert env.stop_code == "agent_stop"


@pytest.mark.parametrize("arm,tool", [("original","sequence-properties"),
                                       ("adapted","sequence-properties"), ("no-tool",None)])
def test_preparation_loads_real_config_without_model_calls(tmp_path,arm,tool):
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    (prepared / "manifest.json").write_text("{}\n")
    setup = tmp_path / "setup"
    cmd = [sys.executable, str(ROOT / "scripts/prepare_run.py"), "--arm",arm,
           "--prepared-root",str(prepared),"--output-dir",str(setup)]
    if tool:
        cmd += ["--tool",tool]
    process = subprocess.run(cmd,capture_output=True,text=True,check=False)
    assert process.returncode == 0, process.stderr
    config = load_experiment_config(setup / "config.toml")
    assert config.initial_cash == 100000
    assert config.initial_owned_skill_ids == ((tool,) if tool else ())
    assert config.skill_market_enabled is False
    assert json.loads((setup / "SETUP.json").read_text())["model_calls"] == 0
    playbook = _load_agent_playbook_text(config)
    assert "baseline_activity = 1.0" in playbook
    assert "REWARD_RULES_DYNAMIC_BLOCK" not in playbook


def test_visible_then_review_refresh_preserves_scientific_evidence(tmp_path):
    from agent_native_tools.cli import main
    subprocess.run([sys.executable,str(ROOT / "scripts/make_visible_example.py"),
                    "--workspace",str(tmp_path)],check=True,capture_output=True)
    base=["run","sequence-properties","--workspace",str(tmp_path)]
    assert main(base+["--mode","visible","--input","state.json","--output","initial.json"]) == 0
    initial=json.loads((tmp_path/"initial.json").read_text())
    assert main(base+["--mode","review","--input","initial.json","--output","review.json"]) == 0
    review=json.loads((tmp_path/"review.json").read_text())
    assert review["agent_native"]["scientific_artifact"] == initial["agent_native"]["scientific_artifact"]
    assert review["payload"]["review_only"] is True

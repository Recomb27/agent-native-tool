"""Validate real scientific behavior and installation-facing boundaries."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from agent_native_tools.cli import TOOLS, create_example, main, tool_path


def invoke(tool, workspace, arm="adapted", **options):
    args = ["run", tool, "--arm", arm, "--workspace", str(workspace)]
    for key, value in options.items():
        args += ["--" + key.replace("_", "-"), str(value)]
    return main(args)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("arm", ["original", "adapted"])
def test_all_24_packages_compute_real_examples(tool, arm, tmp_path):
    create_example(tool, tmp_path)
    assert invoke(tool, tmp_path, arm, rows="rows.json") == 0
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["tool_id"] == tool
    assert isinstance(result["payload"], dict) and result["payload"]
    if arm == "adapted":
        reference = result["agent_native"]["scientific_artifact"]
        path = Path(reference["path"])
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]


def test_reverse_complement_returns_the_actual_transformed_object(tmp_path):
    create_example("reverse-complement", tmp_path)
    assert invoke("reverse-complement", tmp_path) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    source = json.loads((tmp_path / "input.json").read_text())["sequences"]
    expected = next(iter(source.values())).translate(str.maketrans("ACGT", "TGCA"))[::-1]
    assert expected in json.dumps(result)


def test_bad_input_fails_without_replacing_prior_result(tmp_path):
    create_example("sequence-properties", tmp_path)
    assert invoke("sequence-properties", tmp_path) == 0
    original = (tmp_path / "result.json").read_bytes()
    (tmp_path / "input.json").write_text("[]")
    assert invoke("sequence-properties", tmp_path) == 2
    assert (tmp_path / "result.json").read_bytes() == original


def test_review_detects_tampered_scientific_evidence(tmp_path):
    create_example("sequence-properties", tmp_path)
    assert invoke("sequence-properties", tmp_path) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    evidence = Path(result["agent_native"]["scientific_artifact"]["path"])
    evidence.write_text("{}\n")
    assert invoke("sequence-properties", tmp_path, mode="review",
                  input="result.json", output="review.json") == 2
    assert not (tmp_path / "review.json").exists()


def test_launcher_blocks_state_overwrite_and_path_escape(tmp_path):
    create_example("sequence-properties", tmp_path)
    assert invoke("sequence-properties", tmp_path, arm="original", output="state.json") == 2
    assert invoke("sequence-properties", tmp_path, input="../outside.json") == 2
    assert not (tmp_path / "state.json").exists()


def support_module():
    spec = importlib.util.spec_from_file_location(
        "public_native_support", tool_path("sequence-properties") / "native_support.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_visible_input_excludes_hidden_objects_and_labels():
    support = support_module()
    state = {"visible_seed_ids": ["visible"], "seeds": [
        {"seed_id": "visible", "start_sequence": "ATGGCT", "libraries": []},
        {"seed_id": "hidden", "start_sequence": "BAD", "true_activity": 999}
    ]}
    fixture = support.visible_fixture(state)
    assert "BAD" not in json.dumps(fixture)
    assert "999" not in json.dumps(fixture)
    assert fixture["sequences"]


def test_reward_parser_retains_public_arithmetic_and_unknown_contract():
    support = support_module()
    playbook = "\n".join([
        "base_asset_value = 100000.0", "baseline_activity = 1.0",
        "- tier 1: \x60[1.0, 1.314)\x60, multiplier = 1.5",
        "- tier 2: \x60[1.314, 3.78)\x60, multiplier = 2.4",
        "- tier 3: \x60[3.78, 7.44)\x60, multiplier = 18.0",
        "- tier 4: \x60[7.44, +inf)\x60, multiplier = 30.0",
    ])
    contract = support.reward_contract(playbook)
    assert support.incremental_reward(1, 1.157, contract) == pytest.approx(20354.3647363872)
    assert support.break_even(18.049103302074418, 10067, contract) == pytest.approx(19.212758084955276)
    assert support.reward_contract("") is None


from __future__ import annotations

import hashlib
import json
from pathlib import Path

from TadACampaign import skill_market
from TadACampaign.tool_trajectory import build_tool_trajectory_index, run_tool


def _tool_main(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    values = {args[index][2:]: args[index + 1] for index in range(len(args) - 1) if args[index].startswith("--")}
    output = Path(values["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"tool": values["tool"], "ok": True}) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "tool_id": values["tool"], "output": str(output)}))
    return 0


def _run_recorded_tool(run_root: Path, tool_id: str, output: Path) -> int:
    return run_tool(
        _tool_main,
        [
            "--tool",
            tool_id,
            "--input",
            str(run_root / "input.json"),
            "--rows",
            str(run_root / "rows.json"),
            "--output",
            str(output),
            "--run-root",
            str(run_root),
        ],
        tool_id=tool_id,
        announce=True,
    )


def test_successful_call_records_command_paths_hashes_and_output(tmp_path, capsys) -> None:
    run_root = tmp_path / "agent_workspace" / "skill_analysis"
    run_root.mkdir(parents=True)
    (run_root / "input.json").write_text('{"input": 1}\n', encoding="utf-8")
    (run_root / "rows.json").write_text("[]\n", encoding="utf-8")
    output = run_root / "result.json"

    assert _run_recorded_tool(run_root, "tool-a", output) == 0

    captured = capsys.readouterr()
    announcement = json.loads(captured.out.strip().splitlines()[-1])
    assert announcement["trajectory_id"].endswith("tool-a-call-0001")
    calls = [json.loads(line) for line in (run_root / "tool_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1
    call = calls[0]
    assert call["tool_id"] == "tool-a"
    assert call["status"] == "completed"
    assert call["parameters"]["tool"] == "tool-a"
    assert call["paths_at_finish"]["output"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert Path(call["logs"]["stdout"]).read_text(encoding="utf-8").startswith('{"status": "ok"')
    assert call["started_epoch"] <= call["finished_epoch"]


def test_failed_call_keeps_manifest_and_stderr(tmp_path, capsys) -> None:
    def failing_main(argv: list[str] | None = None) -> int:
        raise RuntimeError("synthetic failure")

    run_root = tmp_path / "skill_analysis"
    run_root.mkdir()
    exit_code = run_tool(
        failing_main,
        ["--tool", "tool-fail", "--run-root", str(run_root)],
        tool_id="tool-fail",
    )

    assert exit_code == 1
    capsys.readouterr()
    manifest_path = run_root / "tool_trajectories" / "tool-fail" / "call-0001" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["exit_code"] == 1
    assert "synthetic failure" in (run_root / "tool_trajectories" / "tool-fail" / "call-0001" / "stderr.log").read_text(
        encoding="utf-8"
    )


def test_index_joins_agent_messages_and_multiple_event_streams(tmp_path, capsys) -> None:
    run_dir = tmp_path / "run"
    run_root = run_dir / "process" / "agent_workspace" / "skill_analysis"
    run_root.mkdir(parents=True)
    (run_root / "input.json").write_text("{}\n", encoding="utf-8")
    (run_root / "rows.json").write_text("[]\n", encoding="utf-8")
    first_output = run_root / "one.json"
    second_output = run_root / "two.json"
    _run_recorded_tool(run_root, "tool-a", first_output)
    _run_recorded_tool(run_root, "tool-a", second_output)
    _run_recorded_tool(run_root, "tool-b", run_root / "three.json")
    capsys.readouterr()

    calls = [json.loads(line) for line in (run_root / "tool_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    first_id = calls[0]["trajectory_id"]
    second_id = calls[1]["trajectory_id"]
    third_id = calls[2]["trajectory_id"]
    timeline = run_dir / "results" / "runtime_timeline.jsonl"
    timeline.parent.mkdir(parents=True)
    timeline.write_text(
        json.dumps({"event": "run.started", "started_at": "2026-10-31T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "results" / "codex_events_attempt1.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "First assess tool-a"}}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "python run.py --tool tool-a",
                            "aggregated_output": json.dumps({"trajectory_id": first_id}),
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Read tool-a output and continue"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "results" / "codex_events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "python run.py --tool tool-a",
                            "aggregated_output": json.dumps({"trajectory_id": second_id}),
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "python run.py --tool tool-b",
                            "aggregated_output": json.dumps({"trajectory_id": third_id}),
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    index_path = build_tool_trajectory_index(run_dir)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["tool_call_count"] == 3
    assert index["tool_ids"] == ["tool-a", "tool-b"]
    assert len(index["agent_event_sources"]) == 2
    by_id = {call["trajectory_id"]: call for call in index["calls"]}
    assert by_id[first_id]["agent_judgment"]["source_references"]
    assert by_id[first_id]["agent_judgment"]["after_messages"][0]["text_excerpt"] == "Read tool-a output and continue"
    assert json.loads((run_root / "tool_trajectories" / "tool-a" / "call-0001" / "agent_context.json").read_text())[
        "mapping_method"
    ] == "trajectory_id"
    assert by_id[second_id]["tool_call_index"] == 2
    assert by_id[third_id]["tool_call_index"] == 1


def test_materialization_installs_launcher_and_removes_duplicate_legacy_log(tmp_path) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    (package_root / "SKILL.md").write_text("# fake\n", encoding="utf-8")
    (package_root / "run.py").write_text("print('legacy launcher')\n", encoding="utf-8")
    (package_root / "toolkit_impl.py").write_text(
        """from pathlib import Path\nimport json\ndef main(argv):\n    event = {\"ok\": True}\n    with (Path(args.run_root) / \"tool_calls.jsonl\").open(\"a\", encoding=\"utf-8\") as handle:\n        handle.write(json.dumps(event, ensure_ascii=False) + \"\\n\")\n    return 0\n""",
        encoding="utf-8",
    )
    archive = skill_market._build_archive_bytes(package_root)
    destination = tmp_path / "owned" / "fake"
    skill_market.materialize_owned_skill(
        destination,
        bundle_payload={"archive_base64": __import__("base64").b64encode(archive).decode("ascii"), "bundle_sha256": hashlib.sha256(archive).hexdigest()},
        index_item={"skill_id": "fake"},
    )

    launcher = (destination / "source" / "run.py").read_text(encoding="utf-8")
    original = destination / "source" / "_trajectory_original_run.py"
    metadata = json.loads((destination / "tool_trajectory_launcher.json").read_text(encoding="utf-8"))
    assert "run_tool" in launcher
    assert original.is_file()
    assert "tool_calls.jsonl" not in (destination / "source" / "toolkit_impl.py").read_text(encoding="utf-8")
    assert metadata["legacy_log_neutralized"] is True

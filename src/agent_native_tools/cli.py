"""A portable launcher; scientific implementations remain unchanged."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

PACKAGE_ROOT = Path(__file__).resolve().parent
TOOLS = (
    "codon-usage", "distance-calculations", "filter-sequences", "motif-search",
    "msa-statistics", "pairwise-alignment", "restriction-sites", "reverse-complement",
    "sequence-properties", "sequence-slicing", "sequence-statistics",
    "transcription-translation",
)
PROTECTED = {"state.json", "history.json", "available_actions.json", "judge_session.json",
             "agent_playbook.md", "biology_context.md", "owned_skills_index.json",
             "skill_market.json", "tool_calls.jsonl"}
PROTECTED_DIRS = {"owned_skills", "tool_trajectories", ".agent_native_evidence",
                  ".agent_native_memory"}


def tool_path(tool: str, arm: str = "adapted") -> Path:
    if tool not in TOOLS or arm not in {"original", "adapted"}:
        raise ValueError("Unknown tool or arm")
    path = PACKAGE_ROOT / "_tools" / arm / tool
    if not (path / "run.py").is_file():
        raise ValueError("Tool source is missing from this installation")
    return path


def workspace_path(workspace: Path, name: str, *, output: bool = False) -> Path:
    path = (workspace / name).resolve()
    if not path.is_relative_to(workspace) or path == workspace:
        raise ValueError("Paths must remain inside the supplied workspace")
    relative = path.relative_to(workspace)
    if output and (relative.parts[0] in PROTECTED_DIRS
                   or (len(relative.parts) == 1 and relative.name in PROTECTED)):
        raise ValueError("Output cannot overwrite campaign state, source or evidence")
    return path


def create_example(tool: str, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    paths = [workspace / "input.json", workspace / "rows.json"]
    if any(path.exists() for path in paths):
        raise ValueError("Example files already exist; select a new workspace")
    fixture = json.loads((tool_path(tool) / "EXAMPLES.json").read_text())["scientific_input"]
    sequence = next(iter(fixture["sequences"].values()))
    fixture["sequences"] = {"example_a": sequence, "example_b": sequence[:-3] + "GCC"}
    paths[0].write_text(json.dumps(fixture, indent=2) + "\n")
    paths[1].write_text("[]\n")
    print(json.dumps({"workspace": str(workspace), "input": "input.json", "rows": "rows.json",
                      "purpose": "Synthetic interface example; not experimental evidence"}))


def run_tool(args: argparse.Namespace) -> int:
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("Workspace must already exist; use the example command first")
    source = tool_path(args.tool, args.arm)
    inp = workspace_path(workspace, args.input)
    out = workspace_path(workspace, args.output, output=True)
    if inp == out:
        raise ValueError("Output must differ from input")
    if not inp.is_file():
        raise ValueError("Input does not exist")
    command = [sys.executable, "-B", str(source / "run.py"), "--input", str(inp),
               "--output", str(out), "--run-root", str(workspace)]
    if args.rows:
        rows = workspace_path(workspace, args.rows)
        if not rows.is_file():
            raise ValueError("Rows input does not exist")
        if rows == out:
            raise ValueError("Output must differ from rows input")
        command += ["--rows", str(rows)]
    if args.arm == "adapted":
        command += ["--mode", args.mode, "--detail", args.detail]
    elif args.mode != "scientific":
        raise ValueError("Visible and review modes are available only in the adapted arm")
    out.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(command, cwd=workspace, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run original and adapted bioinformatics tools locally.")
    parser.add_argument("--version", action="version", version="agent-native-tool 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List the twelve supported tool IDs")
    describe = sub.add_parser("describe", help="Read a package's scientific-use instructions")
    describe.add_argument("tool", choices=TOOLS)
    describe.add_argument("--arm", choices=["original", "adapted"], default="adapted")
    example = sub.add_parser("example", help="Write synthetic scientific inputs")
    example.add_argument("tool", choices=TOOLS)
    example.add_argument("--workspace", type=Path, required=True)
    run = sub.add_parser("run", help="Execute a frozen tool in an explicit workspace")
    run.add_argument("tool", choices=TOOLS)
    run.add_argument("--arm", choices=["original", "adapted"], default="adapted")
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--input", default="input.json")
    run.add_argument("--rows")
    run.add_argument("--output", default="result.json")
    run.add_argument("--mode", choices=["scientific", "visible", "review"], default="scientific")
    run.add_argument("--detail", choices=["compact", "full"], default="compact")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            print("\n".join(TOOLS))
        elif args.command == "describe":
            print((tool_path(args.tool, args.arm) / "SKILL.md").read_text(), end="")
        elif args.command == "example":
            create_example(args.tool, args.workspace.expanduser().resolve())
        else:
            return run_tool(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 2
    return 0


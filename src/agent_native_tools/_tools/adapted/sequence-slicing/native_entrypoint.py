"""Versioned entry point around a preserved V1 scientific core."""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile

import native_support as support

PROTECTED = {"state.json", "history.json", "available_actions.json", "judge_session.json",
             "owned_skills_index.json", "skill_market.json", "biology_context.md", "agent_playbook.md"}


def raw_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    raw = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inside(root: Path, value: str | Path, *, output: bool = False) -> Path:
    p = Path(value).expanduser().resolve()
    if not p.is_relative_to(root):
        raise ValueError("input and output paths must be inside the supplied workspace")
    if output and (p == root or (p.parent == root and p.name in PROTECTED)
                   or p.is_relative_to(root / "owned_skills")
                   or p.is_relative_to(root / ".agent_native_evidence")
                   or p.is_relative_to(root / ".agent_native_memory")
                   or p.is_relative_to(root / "tool_trajectories")
                   or p == root / "tool_calls.jsonl"):
        raise ValueError("output cannot overwrite benchmark, tool source, memory or immutable evidence")
    return p


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def public_inputs(root: Path, pin: Path) -> tuple[dict, dict, dict, str, dict]:
    names = ["state.json", "history.json", "available_actions.json", "agent_playbook.md"]
    if not (root / "state.json").is_file():
        return {}, {}, {}, "", {}
    for _ in range(3):
        before = (root / "state.json").read_bytes()
        raw = {n: (root / n).read_bytes() if (root / n).is_file() else b"" for n in names}
        if before == raw["state.json"] == (root / "state.json").read_bytes():
            break
    else:
        raise ValueError("public state changed during capture; retry after the action completes")
    public_dir = pin / "public"
    public_dir.mkdir()
    refs = {}
    for n, b in raw.items():
        if b:
            (public_dir / n).write_bytes(b)
            refs[n] = {"sha256": raw_hash(b), "snapshot": str(public_dir / n)}
    state, history, actions = [json.loads(raw[n]) if raw[n] else {} for n in names[:3]]
    recent = history.get("recent_actions", [])
    if recent and (recent[-1].get("cash_after") != state.get("current_cash") or
                   recent[-1].get("elapsed_day_after") != state.get("elapsed_day")):
        raise ValueError("history and state do not describe the same public decision boundary")
    return state, history, actions, raw["agent_playbook.md"].decode(), refs


def compact_pairwise(result: dict, full_path: Path) -> dict:
    compact = copy.deepcopy(result)
    for pair in compact.get("payload", {}).get("compatibility", {}).get("pair_metadata", []):
        alignment = pair.get("alignment", {})
        if "columns" in alignment:
            alignment["column_count"] = len(alignment.pop("columns"))
            alignment["columns_in_full_artifact"] = str(full_path)
    return compact


def main(tool: str, argv: list[str] | None = None) -> int:
    parser = Parser(description=f"{tool}: preserved science plus public decision evidence")
    parser.add_argument("--tool", choices=[tool], default=tool)
    parser.add_argument("--input", required=True)
    parser.add_argument("--rows")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--mode", choices=["scientific", "visible", "review"], default="scientific")
    parser.add_argument("--detail", choices=["compact", "full"], default="compact")
    pin: Path | None = None
    try:
        args = parser.parse_args(argv)
        root = Path(args.run_root).resolve()
        if not root.is_dir():
            raise ValueError("run-root must be an existing workspace directory")
        inp = inside(root, args.input)
        out = inside(root, args.output, output=True)
        if out == inp:
            raise ValueError("output must not replace the supplied input artifact")
        data = inp.read_bytes()
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError("input must be a JSON object; see EXAMPLES.json")
        src = Path(__file__).resolve().parent
        contract = json.loads((src / "TOOL_CONTRACT.json").read_text())
        pin_parent = root / ".agent_native_evidence" / tool
        pin_parent.mkdir(parents=True, exist_ok=True)
        pin = Path(tempfile.mkdtemp(prefix="call-", dir=pin_parent))
        (pin / "supplied_input.json").write_bytes(data)
        rows_bytes = None
        if args.rows:
            rows_path = Path(args.rows).resolve()
            if rows_path != Path("/dev/null"):
                rows_path = inside(root, rows_path)
            rows_bytes = rows_path.read_bytes()
            (pin / "supplied_rows.json").write_bytes(rows_bytes)
        if args.mode == "review":
            if parsed.get("tool_id") != tool or "agent_native" not in parsed:
                raise ValueError("review requires a previous v2 artifact from this same tool")
            prior = parsed["agent_native"]["scientific_artifact"]
            core_path = inside(root, prior["path"])
            if not core_path.is_relative_to(pin_parent):
                raise ValueError("review scientific artifact is outside this tool's immutable evidence")
            core_raw = core_path.read_bytes()
            if raw_hash(core_raw) != prior["sha256"]:
                raise ValueError("immutable scientific artifact hash mismatch")
            core = json.loads(core_raw)
            fixture_ref = parsed["agent_native"]["scientific_input"]
            fixture_path = inside(root, fixture_ref["path"])
            fixture_bytes = fixture_path.read_bytes()
            if not fixture_path.is_relative_to(pin_parent) or raw_hash(fixture_bytes) != fixture_ref["sha256"]:
                raise ValueError("scientific input binding no longer matches its immutable snapshot")
            fixture = json.loads(fixture_bytes)
            result = {"tool_id": tool, "payload": {"review_only": True, "reviewed_artifact_sha256": raw_hash(data)}}
        else:
            fixture = support.visible_fixture(parsed) if args.mode == "visible" else parsed
            if args.mode == "visible" and rows_bytes and json.loads(rows_bytes) != []:
                raise ValueError("visible mode creates its own windows; rows must be omitted or empty")
            fixture_path = pin / "scientific_input.json"
            if args.mode == "scientific":
                fixture_path.write_bytes(data)
            else:
                atomic_json(fixture_path, fixture)
            core_path = pin / "scientific_full.json"
            core_argv = ["--tool", tool, "--input", str(fixture_path),
                         "--output", str(core_path), "--run-root", str(pin)]
            if args.rows:
                core_argv += ["--rows", "/dev/null" if Path(args.rows).resolve() == Path("/dev/null")
                              else str(pin / "supplied_rows.json")]
            import toolkit_impl
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = toolkit_impl.main(core_argv)
                except SystemExit as exc:
                    code = exc.code or 0
            (pin / "core_stdout.log").write_text(stdout.getvalue())
            (pin / "core_stderr.log").write_text(stderr.getvalue())
            if code != 0 or not core_path.is_file():
                raise ValueError(f"scientific input rejected: {stderr.getvalue().strip()}")
            core = json.loads(core_path.read_text())
            if core.get("tool_id") != tool or not isinstance(core.get("payload"), dict):
                raise ValueError("preserved scientific core returned an invalid identity or payload")
            result = compact_pairwise(core, core_path) if tool == "pairwise-alignment" and args.detail == "compact" else copy.deepcopy(core)
        brief = support.scientific_brief(tool, core, fixture, contract)
        state, history, actions, playbook, refs = public_inputs(root, pin)
        memory_file = root / ".agent_native_memory" / f"{tool}.json"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        with (memory_file.parent / f"{tool}.lock").open("a+") as lock:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            old = json.loads(memory_file.read_text()) if memory_file.is_file() else None
            identity = support.digest({"workspace": str(root), "tool": tool})
            if state:
                memory = support.merge_memory(state, history, old, identity)
                report = support.decision_support(state, history, actions, playbook, memory, brief)
                atomic_json(memory_file, memory)
                atomic_json(pin / "decision_memory.json", memory)
            else:
                report = {"status": "unavailable_no_public_state", "probability_of_future_improvement": None}
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        result.update(input_sha256=raw_hash(data), rows_sha256=raw_hash(rows_bytes) if rows_bytes is not None else None)
        result["scientific_brief"] = brief
        result["task_support"] = report
        result["agent_native"] = {"version": support.VERSION, "mode": args.mode,
            "detail_view": args.detail, "operation": "public_decision_review" if args.mode == "review" else tool,
            "scientific_artifact": {"path": str(core_path), "sha256": raw_hash(core_path.read_bytes())},
            "scientific_input": {"path": str(fixture_path), "sha256": raw_hash(fixture_path.read_bytes())},
            "supplied_input": {"path": str(pin / "supplied_input.json"), "sha256": raw_hash(data)},
            "public_decision_inputs": refs,
            "immutable_result": str(pin / "result.json"),
            "schema_file": str(src / "INPUT_SCHEMA.json"),
            "source_files": {n: raw_hash((src / n).read_bytes()) for n in
                             ["native_entrypoint.py", "native_support.py", "toolkit_impl.py", "TOOL_CONTRACT.json"]}}
        atomic_json(pin / "result.json", result)
        atomic_json(out, result)
        print(json.dumps({"status": "ok", "tool_id": tool, "mode": args.mode,
                          "output": str(out), "immutable_result": str(pin / "result.json"),
                          "read_first": ["scientific_brief", "task_support"],
                          "scientific_full": str(core_path)}, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
        error = {"status": "error", "tool_id": tool, "error": {"code": "invalid_input_or_context",
                 "message": str(exc), "repair": "Use --mode visible --input state.json for public workspace data, or copy the complete EXAMPLES.json scientific shape; do not guess nested fields."}}
        if pin is not None:
            atomic_json(pin / "failure.json", error)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2

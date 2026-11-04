"""Internal runtime-only entrypoints used by helper scripts and launch wrappers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import AgentDecision
from .runner import load_experiment_config, run_agent_step

BASE_AGENT_ACTIONS = ("prepare_seed", "prepare_library", "mine_library", "confirm_hit", "stop")
SKILL_AGENT_ACTIONS = (*BASE_AGENT_ACTIONS[:-1], "purchase_skill", BASE_AGENT_ACTIONS[-1])


class _AgentStepParser(argparse.ArgumentParser):
    def parse_known_args(
        self,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> tuple[argparse.Namespace, list[str]]:
        parsed, extras = super().parse_known_args(args, namespace)
        if not hasattr(parsed, "run_dir") or not hasattr(parsed, "action"):
            return parsed, extras
        config = load_experiment_config(Path(parsed.run_dir) / "config.toml")
        allowed_actions = SKILL_AGENT_ACTIONS if config.skill_surface_enabled else BASE_AGENT_ACTIONS
        if parsed.action not in allowed_actions:
            joined = ", ".join(allowed_actions)
            self.error(f"argument --action: invalid choice: {parsed.action!r} (choose from {joined})")
        return parsed, extras

    def format_help(self) -> str:
        help_text = super().format_help()
        try:
            run_dir = self._run_dir_from_argv()
            if run_dir is not None:
                config = load_experiment_config(run_dir / "config.toml")
                if not config.skill_surface_enabled:
                    return self._strip_skill_help(help_text)
        except Exception:
            pass
        return help_text

    @staticmethod
    def _run_dir_from_argv() -> Path | None:
        import sys

        argv = sys.argv[1:]
        for index, value in enumerate(argv):
            if value == "--run-dir" and index + 1 < len(argv):
                return Path(argv[index + 1])
            if value.startswith("--run-dir="):
                return Path(value.split("=", 1)[1])
        return None

    @staticmethod
    def _strip_skill_help(help_text: str) -> str:
        lines: list[str] = []
        for line in help_text.splitlines():
            if "purchase_skill" in line or "--skill-id" in line:
                continue
            lines.append(line)
        return "\n".join(lines) + ("\n" if help_text.endswith("\n") else "")


def _agent_step_has_no_skill_surface(args: list[str] | None = None, *, require_help: bool) -> bool:
    argv = args
    if argv is None:
        import sys

        argv = sys.argv[1:]
    if require_help and "-h" not in argv and "--help" not in argv:
        return False
    for index, value in enumerate(argv):
        try:
            if value == "--run-dir" and index + 1 < len(argv):
                config = load_experiment_config(Path(argv[index + 1]) / "config.toml")
                return not config.skill_surface_enabled
            if value.startswith("--run-dir="):
                config = load_experiment_config(Path(value.split("=", 1)[1]) / "config.toml")
                return not config.skill_surface_enabled
        except Exception:
            return False
    return "agent-step" in argv


def build_runtime_parser(args: list[str] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TadACampaign internal runtime entrypoints")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=_AgentStepParser)

    step_parser = subparsers.add_parser("agent-step")
    step_parser.add_argument("--run-dir", required=True, help="Path to an initialized run directory")
    step_parser.add_argument("--action", required=True)
    step_parser.add_argument("--seed-id", default=None, help="Seed id for prepare/mine actions")
    step_parser.add_argument("--library-id", default=None, help="Library id for prepare-library actions")
    step_parser.add_argument("--batch-size", type=int, default=None, help="Batch size for mine action")
    step_parser.add_argument("--screen-record-id", type=int, default=None, help="Screen record id for confirm_hit action")
    if not _agent_step_has_no_skill_surface(args, require_help=False):
        step_parser.add_argument("--skill-id", default=None, help="Skill id for purchase_skill action")
    step_parser.add_argument("--reasoning", default="", help="Short reason recorded with the action")

    judge_parser = subparsers.add_parser("judge-server")
    judge_parser.add_argument("--config", required=True, help="Path to the experiment TOML config")
    judge_parser.add_argument("--host", default="127.0.0.1", help="Bind host for the authoritative judge")
    judge_parser.add_argument("--advertise-host", default=None, help="Optional externally reachable host written to ready metadata")
    judge_parser.add_argument("--port", type=int, default=0, help="Bind port; 0 chooses a random free port")
    judge_parser.add_argument("--audit-root", required=True, help="Directory for judge audit JSONL files")
    judge_parser.add_argument("--admin-token", default=None, help="Required admin token for management endpoints")
    judge_parser.add_argument("--ready-file", default=None, help="Optional JSON file written once the server is bound")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_runtime_parser(argv)
    args = parser.parse_args(argv)

    if args.command == "agent-step":
        payload = run_agent_step(
            Path(args.run_dir),
            AgentDecision(
                action=args.action,
                seed_id=args.seed_id,
                library_id=args.library_id,
                batch_size=args.batch_size,
                screen_record_id=args.screen_record_id,
                skill_id=getattr(args, "skill_id", None),
                reasoning=args.reasoning,
            ),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "judge-server":
        from .judge.http import run_judge_server

        config = load_experiment_config(Path(args.config))
        run_judge_server(
            config,
            host=args.host,
            advertise_host=args.advertise_host,
            port=args.port,
            audit_root=Path(args.audit_root),
            admin_token=args.admin_token,
            ready_file=Path(args.ready_file) if args.ready_file else None,
        )
        return 0

    parser.error(f"Unsupported command {args.command!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

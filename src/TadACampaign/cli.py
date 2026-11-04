"""Public CLI entrypoints."""

from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TadACampaign benchmark CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare-data", help="Prepare canonical dna/protein benchmark data")
    prepare_parser.add_argument("--zip-path", default="data/TadABench-1M.zip", help="Path to the raw TadABench zip")
    prepare_parser.add_argument("--output-dir", default="prepared", help="Output directory for prepared benchmark assets")

    market_parser = subparsers.add_parser(
        "prepare-skill-market",
        help="Build the curated local launch-market bundle from offline upstream clones",
    )
    market_parser.add_argument(
        "--source-root",
        default="/tmp/TadACampaign_skill_scout",
        help="Root containing local upstream repo checkouts",
    )
    market_parser.add_argument(
        "--output-dir",
        default="/tmp/TadACampaign_skill_market/launch21",
        help="Output directory for the curated local market bundle",
    )
    market_parser.add_argument("--force", action="store_true", help="Replace the output directory if it already exists")

    summarize_parser = subparsers.add_parser("summarize", help="Render a markdown summary for a run directory")
    summarize_parser.add_argument("--run-dir", required=True, help="Path to a completed run directory")

    audit_parser = subparsers.add_parser(
        "audit-sessions",
        help="Audit raw Codex event streams and game-rule terminal evidence",
    )
    audit_parser.add_argument("--root", required=True, help="Experiment root containing runs/")
    audit_parser.add_argument("--json-output", default=None, help="Optional JSON report path")
    audit_parser.add_argument("--markdown-output", default=None, help="Optional Markdown report path")

    run_parser = subparsers.add_parser(
        "run",
        help="Run one experiment from the merged source using the selected historical mode",
    )
    run_parser.add_argument("--config", required=True, help="Path to the experiment TOML config")
    mode_group = run_parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--agent",
        "--agent-mode",
        dest="mode",
        action="store_const",
        const="agent",
        help="Use the external Agent backend selected in [agent].backend",
    )
    mode_group.add_argument(
        "--jev",
        "--jev-mode",
        dest="mode",
        action="store_const",
        const="jev",
        help="Use the shared policy adapter and Jev-family policy",
    )
    mode_group.add_argument(
        "--mode",
        choices=("agent", "jev"),
        help="Explicit mode name; --agent and --jev are equivalent aliases",
    )
    run_parser.add_argument(
        "--policy",
        default=None,
        help="Jev-family or policy baseline name (implies Jev mode when --mode is omitted)",
    )
    run_parser.add_argument(
        "--output-root",
        default=None,
        help="Optional output directory override for this run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "prepare-data":
        from .data import prepare_data

        output_root = prepare_data(Path(args.zip_path), Path(args.output_dir))
        print(output_root)
        return 0

    if args.command == "prepare-skill-market":
        from .curated_skill_market import prepare_curated_skill_market

        manifest_path = prepare_curated_skill_market(Path(args.source_root), Path(args.output_dir), force=bool(args.force))
        print(manifest_path)
        return 0

    if args.command == "summarize":
        from .runner import summarize_run

        print(summarize_run(Path(args.run_dir)))
        return 0

    if args.command == "audit-sessions":
        from .session_audit import main as audit_main

        audit_args = ["--root", args.root]
        if args.json_output:
            audit_args.extend(["--json-output", args.json_output])
        if args.markdown_output:
            audit_args.extend(["--markdown-output", args.markdown_output])
        return audit_main(audit_args)

    if args.command == "run":
        from .runner import run_experiment

        run_dir = run_experiment(
            Path(args.config),
            mode=args.mode,
            policy=args.policy,
            output_root_override=Path(args.output_root) if args.output_root else None,
        )
        print(run_dir)
        return 0

    parser.error(f"Unsupported command {args.command!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

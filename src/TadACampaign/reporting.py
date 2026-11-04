"""Result serialization and reporting."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_curve(results_dir: Path, action_history: list[dict[str, Any]], *, initial_cash: float) -> tuple[Path, Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    csv_path = results_dir / "cash_curve.csv"
    png_path = results_dir / "cash_curve.png"

    days = [0.0]
    cash = [float(initial_cash)]
    for action in action_history:
        days.append(float(action.get("elapsed_day_after", action.get("day_after", 0.0))))
        cash.append(float(action.get("cash_after", action.get("cash_after", 0.0))))

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day", "cash"])
        for day, cash_point in zip(days, cash, strict=True):
            writer.writerow([day, cash_point])

    plt.figure(figsize=(8, 4))
    plt.plot(days, cash, marker="o")
    plt.xlabel("Day")
    plt.ylabel("Cash")
    plt.title("TadACampaign Cash Curve")
    plt.tight_layout()
    plt.savefig(png_path, dpi=160)
    plt.close()
    return csv_path, png_path


def render_markdown_summary(summary: dict[str, Any], run_dir: Path) -> str:
    lines = [
        f"# {summary['run_name']}",
        "",
        f"- Variant: `{summary['variant']}`",
        f"- Run mode: `{summary.get('run_mode', 'agent')}`",
        f"- Agent backend: `{summary.get('agent_backend', 'codex')}`",
        f"- Execution model: `{summary.get('execution_model', 'docker_agent_host_judge')}`",
        f"- Final cash: `{summary['cash_final']:.4f}`",
        f"- Total cost: `{summary['cost_paid_total']:.4f}`",
        f"- Total reward: `{summary['reward_earned_total']:.4f}`",
        f"- Best confirmed activity: `{summary['confirmed_best_activity']:.6f}`",
        f"- Provisional best activity: `{summary.get('provisional_best_activity', summary['confirmed_best_activity']):.6f}`",
        f"- Screening noise enabled: `{summary.get('screening_noise_enabled', False)}`",
        f"- Screening noise sigma/clip: `{summary.get('screening_noise_sigma', 0.0):.4f}` / `{summary.get('screening_noise_clip', 0.0):.4f}`",
        f"- Final day: `{summary['day_final']:.4f}`",
        f"- Pending confirmations: `{summary.get('pending_confirmation_count', 0)}`",
        f"- Requests: `{summary.get('request_count', summary.get('action_count', 0))}`",
        f"- Rejected actions: `{summary.get('rejected_action_count', 0)}`",
        f"- Stop code: `{summary.get('stop_code')}`",
        f"- Stop reason: `{summary['stop_reason']}`",
        f"- Tool trajectory calls: `{summary.get('tool_trajectory_count', 0)}`",
        f"- Invoked tools: `{', '.join(summary.get('tool_trajectory_tool_ids', [])) or 'none'}`",
        f"- Results directory: `{run_dir / 'results'}`",
    ]
    if summary.get("wall_clock_duration_seconds") is not None:
        lines.append(f"- Wall-clock seconds: `{float(summary['wall_clock_duration_seconds']):.3f}`")
    if summary.get("agent_timeout_seconds") is not None:
        lines.append(
            f"- Agent cumulative timeout budget/consumed seconds: `{float(summary.get('agent_timeout_seconds', 0.0)):.3f}` / "
            f"`{float(summary.get('agent_budget_elapsed_seconds', 0.0)):.3f}`"
        )
        lines.append(
            f"- Agent timeout budget status: `{summary.get('agent_budget_status', 'unknown')}`, "
            f"exhausted=`{summary.get('agent_budget_exhausted', False)}`"
        )
    if summary.get("agent_attempt_count") is not None:
        lines.append(
            f"- Agent attempts/resumes/handoffs: `{summary.get('agent_attempt_count', 0)}` / "
            f"`{summary.get('agent_resume_count', 0)}` / `{summary.get('agent_handoff_count', 0)}`"
        )
    if summary.get("backend_failure_count") is not None:
        lines.append(
            f"- Backend failures/recoverable failure count/terminal failure count: `{summary.get('backend_failure_count', 0)}` / "
            f"`{summary.get('backend_retryable_failure_count', 0)}` / `{summary.get('backend_terminal_failure_count', 0)}`"
        )
    if summary.get("backend_recovery_attempt_count") is not None:
        lines.append(f"- Backend recovery attempts: `{summary.get('backend_recovery_attempt_count', 0)}`")
    if summary.get("backend_force_stopped") is not None:
        lines.append(f"- Backend force-stopped: `{summary.get('backend_force_stopped')}`")
    classification_counts = summary.get("backend_failure_classification_counts")
    if isinstance(classification_counts, dict) and classification_counts:
        lines.append(
            "- Backend failure classifications: `"
            + ", ".join(f"{key}={value}" for key, value in sorted(classification_counts.items()))
            + "`"
        )
    if summary.get("backend_final_error"):
        lines.append(f"- Backend final error: `{summary.get('backend_final_error')}`")
    if summary.get("agent_session_id"):
        lines.append(f"- Agent session: `{summary['agent_session_id']}`")
    artifacts = summary.get("artifacts", {})
    agent_stdout = artifacts.get("agent_stdout_log")
    if agent_stdout:
        lines.append(f"- Agent stdout: `{agent_stdout}`")
    codex_events = artifacts.get("codex_events_jsonl")
    if codex_events:
        lines.append(f"- Codex event stream: `{codex_events}`")
    runtime_timeline = artifacts.get("runtime_timeline_jsonl")
    if runtime_timeline:
        lines.append(f"- Runtime timeline: `{runtime_timeline}`")
    agent_budget = artifacts.get("agent_budget_json")
    if agent_budget:
        lines.append(f"- Agent cumulative timeout budget ledger: `{agent_budget}`")
    handoff_drill = artifacts.get("handoff_drill_meta_json")
    if handoff_drill:
        lines.append(f"- Handoff drill metadata: `{handoff_drill}`")
    judge_audit = artifacts.get("judge_audit_jsonl")
    if judge_audit:
        lines.append(f"- Judge audit log: `{judge_audit}`")
    trajectory_index = artifacts.get("tool_trajectory_index_json")
    if trajectory_index:
        lines.append(f"- Tool trajectory index: `{trajectory_index}`")
    trajectory_index_jsonl = artifacts.get("tool_trajectory_index_jsonl")
    if trajectory_index_jsonl:
        lines.append(f"- Tool trajectory index JSONL: `{trajectory_index_jsonl}`")
    tool_calls = artifacts.get("tool_calls_jsonl")
    if tool_calls:
        lines.append(f"- Tool call ledger: `{tool_calls}`")
    warnings = summary.get("tool_trajectory_warnings")
    if warnings:
        lines.append(f"- Tool trajectory audit notes: `{' | '.join(str(item) for item in warnings)}`")
    return "\n".join(lines) + "\n"

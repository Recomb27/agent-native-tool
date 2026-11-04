"""Backend-aware CLI home staging helpers."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .codex_home import resolve_codex_home, stage_codex_home


@dataclass(frozen=True)
class AgentCliStagePaths:
    target_root: Path
    codex_home: Path
    claude_home: Path
    claude_state_file: Path
    opencode_config_dir: Path
    opencode_data_dir: Path


def build_agent_cli_stage_paths(target_root: Path) -> AgentCliStagePaths:
    root = target_root.expanduser().resolve()
    return AgentCliStagePaths(
        target_root=root,
        codex_home=root / ".codex",
        claude_home=root / ".claude",
        claude_state_file=root / ".claude.json",
        opencode_config_dir=root / ".config" / "opencode",
        opencode_data_dir=root / ".local" / "share" / "opencode",
    )


def _reset_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _copy_tree_if_exists(source: Path, target: Path, *, ignore: Callable[[str, list[str]], set[str]] | None = None) -> None:
    if not source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignore)


def _copy_file_if_exists(source: Path, target: Path, *, default_text: str | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copyfile(source, target)
        return
    if default_text is not None and not target.exists():
        target.write_text(default_text, encoding="utf-8")


def stage_claude_home(
    *,
    source_dir: Path,
    state_file: Path,
    target_root: Path,
    clean_target: bool = False,
) -> AgentCliStagePaths:
    paths = build_agent_cli_stage_paths(target_root)
    if clean_target:
        _reset_path(paths.claude_home)
        _reset_path(paths.claude_state_file)
    paths.claude_home.mkdir(parents=True, exist_ok=True)
    _copy_tree_if_exists(source_dir.expanduser(), paths.claude_home)
    _copy_file_if_exists(state_file.expanduser(), paths.claude_state_file, default_text="{}\n")
    return paths


def stage_opencode_home(
    *,
    config_dir: Path,
    data_dir: Path,
    target_root: Path,
    clean_target: bool = False,
) -> AgentCliStagePaths:
    paths = build_agent_cli_stage_paths(target_root)
    if clean_target:
        _reset_path(paths.opencode_config_dir)
        _reset_path(paths.opencode_data_dir)

    paths.opencode_config_dir.mkdir(parents=True, exist_ok=True)
    _copy_tree_if_exists(
        config_dir.expanduser(),
        paths.opencode_config_dir,
        ignore=shutil.ignore_patterns("plugin"),
    )

    source_data_dir = data_dir.expanduser()
    paths.opencode_data_dir.mkdir(parents=True, exist_ok=True)
    _copy_file_if_exists(source_data_dir / "auth.json", paths.opencode_data_dir / "auth.json")
    _copy_tree_if_exists(source_data_dir / "project", paths.opencode_data_dir / "project")
    return paths


def stage_agent_cli_home(
    *,
    backend: str,
    target_root: Path,
    codex_source_dir: Path | None = None,
    include_codex_credentials: bool = True,
    codex_skills_mode: str = "planning-with-files",
    codex_mcp_server_filter_mode: str = "drop_all",
    global_agents_path: Path | None = None,
    claude_source_dir: Path | None = None,
    claude_state_file: Path | None = None,
    opencode_config_dir: Path | None = None,
    opencode_data_dir: Path | None = None,
    clean_target: bool = False,
) -> AgentCliStagePaths:
    paths = build_agent_cli_stage_paths(target_root)
    paths.target_root.mkdir(parents=True, exist_ok=True)

    if backend == "codex":
        stage_codex_home(
            source_dir=(codex_source_dir or resolve_codex_home()),
            target_dir=paths.codex_home,
            include_credentials=include_codex_credentials,
            skills_mode=codex_skills_mode,
            mcp_server_filter_mode=codex_mcp_server_filter_mode,
            global_agents_path=global_agents_path,
            clean_target=clean_target,
        )
        return paths

    if backend == "claude":
        return stage_claude_home(
            source_dir=claude_source_dir or (Path.home() / ".claude"),
            state_file=claude_state_file or (Path.home() / ".claude.json"),
            target_root=paths.target_root,
            clean_target=clean_target,
        )

    if backend == "opencode":
        return stage_opencode_home(
            config_dir=opencode_config_dir or (Path.home() / ".config" / "opencode"),
            data_dir=opencode_data_dir or (Path.home() / ".local" / "share" / "opencode"),
            target_root=paths.target_root,
            clean_target=clean_target,
        )

    if backend == "policy":
        return paths

    raise ValueError(f"Unsupported backend {backend!r}")

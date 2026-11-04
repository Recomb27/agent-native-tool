"""Minimal Codex home staging helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from pathlib import Path

_TABLE_HEADER_RE = re.compile(r"^\s*(\[\[?)([^]\n]+?)(\]\]?)\s*(?:#.*)?$")


def _parse_table_name(line: str) -> str | None:
    match = _TABLE_HEADER_RE.match(line)
    if match is None:
        return None
    opener, raw_name, closer = match.groups()
    if len(opener) != len(closer):
        return None
    return raw_name.strip()


def _ensure_trailing_newline(text: str) -> str:
    if text and not text.endswith("\n"):
        return text + "\n"
    return text


def _matches_key_assignment(line: str, key: str) -> bool:
    return re.match(rf"^\s*{re.escape(key)}\s*=", line) is not None


def delete_root_key(text: str, key: str) -> str:
    kept_lines: list[str] = []
    in_root = True
    skipping_array = False
    for line in text.splitlines(keepends=True):
        table_name = _parse_table_name(line)
        if table_name is not None:
            in_root = False
            skipping_array = False
            kept_lines.append(line)
            continue
        if not in_root:
            kept_lines.append(line)
            continue
        if skipping_array:
            if "]" in line:
                skipping_array = False
            continue
        if _matches_key_assignment(line, key):
            rhs = line.split("=", 1)[1]
            if "[" in rhs and "]" not in rhs:
                skipping_array = True
            continue
        kept_lines.append(line)
    return _ensure_trailing_newline("".join(kept_lines))


def drop_table_sections(text: str, *prefixes: str) -> str:
    kept_lines: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        table_name = _parse_table_name(line)
        if table_name is not None:
            if any(table_name == prefix or table_name.startswith(f"{prefix}.") for prefix in prefixes):
                skipping = True
                continue
            skipping = False
        if not skipping:
            kept_lines.append(line)
    return _ensure_trailing_newline("".join(kept_lines))


def drop_mcp_server_sections(text: str) -> str:
    return drop_table_sections(text, "mcp_servers")


def set_root_key(text: str, key: str, raw_value: str) -> str:
    rendered_lines: list[str] = []
    inserted = False
    in_root = True
    for line in text.splitlines(keepends=True):
        table_name = _parse_table_name(line)
        if table_name is not None:
            if in_root and not inserted:
                rendered_lines.append(f"{key} = {raw_value}\n")
                inserted = True
            in_root = False
            rendered_lines.append(line)
            continue
        if in_root and _matches_key_assignment(line, key):
            if not inserted:
                rendered_lines.append(f"{key} = {raw_value}\n")
                inserted = True
            continue
        rendered_lines.append(line)
    if not inserted:
        rendered_lines.append(f"{key} = {raw_value}\n")
    return _ensure_trailing_newline("".join(rendered_lines))


def set_table_key(text: str, table_name: str, key: str, raw_value: str) -> str:
    rendered_lines: list[str] = []
    in_target = False
    saw_target = False
    inserted = False
    for line in text.splitlines(keepends=True):
        current_table = _parse_table_name(line)
        if current_table is not None:
            if in_target and not inserted:
                rendered_lines.append(f"{key} = {raw_value}\n")
                inserted = True
            in_target = current_table == table_name
            saw_target = saw_target or in_target
            rendered_lines.append(line)
            continue
        if in_target and _matches_key_assignment(line, key):
            if not inserted:
                rendered_lines.append(f"{key} = {raw_value}\n")
                inserted = True
            continue
        rendered_lines.append(line)
    if saw_target:
        if in_target and not inserted:
            rendered_lines.append(f"{key} = {raw_value}\n")
        return _ensure_trailing_newline("".join(rendered_lines))

    base = _ensure_trailing_newline("".join(rendered_lines))
    if base and not base.endswith("\n\n"):
        base += "\n"
    return base + f"[{table_name}]\n{key} = {raw_value}\n"


def delete_table_key(text: str, table_name: str, key: str) -> str:
    """Remove a key from one TOML table while preserving the other tables."""

    rendered_lines: list[str] = []
    in_target = False
    skipping_array = False
    for line in text.splitlines(keepends=True):
        current_table = _parse_table_name(line)
        if current_table is not None:
            in_target = current_table == table_name
            skipping_array = False
            rendered_lines.append(line)
            continue
        if in_target and skipping_array:
            if "]" in line:
                skipping_array = False
            continue
        if in_target and _matches_key_assignment(line, key):
            rhs = line.split("=", 1)[1]
            if "[" in rhs and "]" not in rhs:
                skipping_array = True
            continue
        rendered_lines.append(line)
    return _ensure_trailing_newline("".join(rendered_lines))


def apply_secure_codex_policy(
    text: str,
    *,
    skills_enabled: bool,
    drop_mcp_servers: bool,
) -> str:
    rendered = text
    if drop_mcp_servers:
        rendered = drop_mcp_server_sections(rendered)
    for root_key in ("default_permissions", "js_repl_node_path", "js_repl_node_module_dirs", "model_catalog_json", "notify"):
        rendered = delete_root_key(rendered, root_key)
    rendered = drop_table_sections(rendered, "permissions", "sandbox_workspace_write")
    rendered = set_root_key(rendered, "approval_policy", '"never"')
    # The staged home is used by the isolated experiment runner. Keep the
    # config safe even if a caller forgets the command-line sandbox override.
    rendered = set_root_key(rendered, "sandbox_mode", '"workspace-write"')
    rendered = set_root_key(rendered, "web_search", '"disabled"')
    # Codex 0.163 does not recognize the historical ``features.skills`` key.
    # Skills are still staged below ``CODEX_HOME/skills`` when requested; the
    # config key only produced a warning in every isolated campaign run.
    rendered = delete_table_key(rendered, "features", "skills")
    _ = skills_enabled
    rendered = set_table_key(rendered, "features", "plugins", "false")
    return _ensure_trailing_newline(rendered)


def resolve_codex_home(source_dir: Path | None = None) -> Path:
    if source_dir is not None:
        return source_dir.expanduser().resolve()
    raw_home = os.environ.get("CODEX_HOME")
    if raw_home:
        return Path(raw_home).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def load_codex_api_settings(source_dir: Path | None = None) -> tuple[str, str | None]:
    codex_home = resolve_codex_home(source_dir)
    config_path = codex_home / "config.toml"
    config_payload = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}

    provider_name = str(config_payload.get("model_provider", "openai"))
    provider_payload = config_payload.get("model_providers", {}).get(provider_name, {})
    base_url = str(provider_payload.get("base_url") or os.environ.get("CODEX_BASE_URL", "")).rstrip("/")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        auth_path = codex_home / "auth.json"
        if auth_path.is_file():
            auth_payload = json.loads(auth_path.read_text(encoding="utf-8"))
            api_key = auth_payload.get("OPENAI_API_KEY")

    return base_url, api_key


def stage_codex_home(
    *,
    source_dir: Path,
    target_dir: Path,
    include_credentials: bool,
    skills_mode: str = "planning-with-files",
    mcp_server_filter_mode: str = "drop_all",
    global_agents_path: Path | None = None,
    clean_target: bool = False,
) -> None:
    source_root = source_dir.expanduser()
    target_root = target_dir.expanduser()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Codex template directory does not exist: {source_root}")

    if clean_target and target_root.exists():
        if target_root.is_dir():
            shutil.rmtree(target_root)
        else:
            target_root.unlink()
    target_root.mkdir(parents=True, exist_ok=True)

    config_src = source_root / "config.toml"
    config_dst = target_root / "config.toml"
    if config_src.is_file():
        config_text = config_src.read_text(encoding="utf-8")
        config_text = apply_secure_codex_policy(
            config_text,
            skills_enabled=skills_mode != "none",
            drop_mcp_servers=mcp_server_filter_mode == "drop_all",
        )
        config_dst.write_text(config_text, encoding="utf-8")
        config_dst.chmod(0o644)

    auth_src = source_root / "auth.json"
    auth_dst = target_root / "auth.json"
    if include_credentials and auth_src.is_file():
        shutil.copyfile(auth_src, auth_dst)
        auth_dst.chmod(0o600)

    agents_dst = target_root / "AGENTS.md"
    if global_agents_path is not None:
        agents_src = global_agents_path.expanduser()
        if not agents_src.is_file():
            raise FileNotFoundError(f"Global AGENTS file not found: {agents_src}")
        shutil.copyfile(agents_src, agents_dst)
        agents_dst.chmod(0o644)
    elif agents_dst.exists():
        agents_dst.unlink()

    skills_src = source_root / "skills"
    if not skills_src.is_dir() or skills_mode == "none":
        return

    skills_dst = target_root / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)
    if skills_mode == "all":
        shutil.copytree(skills_src, skills_dst, dirs_exist_ok=True)
        return

    for raw_name in skills_mode.split(","):
        skill_name = raw_name.strip()
        if not skill_name:
            continue
        if skill_name.startswith("/") or ".." in Path(skill_name).parts:
            raise ValueError(f"Illegal skill path {skill_name!r}")
        skill_src = skills_src / skill_name
        if not skill_src.is_dir():
            raise FileNotFoundError(f"Skill directory not found: {skill_src}")
        shutil.copytree(skill_src, skills_dst / skill_name, dirs_exist_ok=True)

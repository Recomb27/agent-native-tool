"""Host-side skill market loading and purchased bundle materialization."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SIDECAR_FILENAME = "skill.json"
_TRAJECTORY_LAUNCHER_FILENAME = "run.py"
_TRAJECTORY_ORIGINAL_LAUNCHER_FILENAME = "_trajectory_original_run.py"
_TRAJECTORY_METADATA_FILENAME = "tool_trajectory_launcher.json"


def _neutralize_legacy_tool_log(source_path: Path) -> bool:
    """Remove the old unstructured tool-call append from known bundles.

    The first reconstructed tool bundles predate the trajectory contract and
    append a second, incomplete event to ``tool_calls.jsonl``.  The source
    bundle is copied into an isolated run directory before this function is
    called, so removing that exact block does not mutate a source checkout.
    Unknown implementations are left untouched and are marked in the
    launcher metadata for audit.
    """

    if not source_path.is_file():
        return False
    text = source_path.read_text(encoding="utf-8")
    marker = 'with (Path(args.run_root) / "tool_calls.jsonl").open'
    if marker not in text:
        return False
    pattern = re.compile(
        r'\n[ \t]+event = \{.*?\n[ \t]+handle\.write\(json\.dumps\(event, ensure_ascii=False\) \+ "\\n"\)\n',
        re.DOTALL,
    )
    updated, replacements = pattern.subn("\n", text, count=1)
    if replacements != 1:
        raise ValueError(f"Found legacy tool log marker but could not safely remove it: {source_path}")
    source_path.write_text(updated, encoding="utf-8")
    return True


def _install_tool_trajectory_launcher(skill_dir: Path, *, skill_id: str) -> dict[str, object]:
    """Install the common launcher around a materialized tool entrypoint.

    The launcher runs the bundle's original ``run.py`` in a child process so
    bundles keep their own imports and CLI behavior.  The outer process owns
    the trajectory record, including the exact command, output files and
    failure status.  This is applied only to the isolated run copy.
    """

    source_dir = skill_dir / "source"
    launcher_path = source_dir / _TRAJECTORY_LAUNCHER_FILENAME
    if not launcher_path.is_file():
        return {
            "installed": False,
            "reason": "source/run.py is absent",
            "tool_id": skill_id,
        }

    original_path = source_dir / _TRAJECTORY_ORIGINAL_LAUNCHER_FILENAME
    if original_path.exists():
        raise ValueError(f"Trajectory launcher collision in materialized skill: {original_path}")
    launcher_path.rename(original_path)
    legacy_log_neutralized = _neutralize_legacy_tool_log(source_dir / "toolkit_impl.py")
    launcher_path.write_text(
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n"
        "\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "source_root = os.environ.get(\"TADACAMPAIGN_SOURCE_ROOT\")\n"
        "if source_root:\n"
        "    sys.path.insert(0, source_root)\n"
        "from TadACampaign.tool_trajectory import run_tool\n"
        "\n"
        f"TOOL_ID = {skill_id!r}\n"
        f"ORIGINAL_LAUNCHER = Path(__file__).with_name({_TRAJECTORY_ORIGINAL_LAUNCHER_FILENAME!r})\n"
        "\n"
        "def _invoke_original(argv: list[str] | None = None) -> int:\n"
        "    forwarded = list(argv or [])\n"
        "    if forwarded[:2] == [\"--tool\", TOOL_ID]:\n"
        "        forwarded = forwarded[2:]\n"
        "    completed = subprocess.run(\n"
        "        [sys.executable, str(ORIGINAL_LAUNCHER), *forwarded],\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "        check=False,\n"
        "    )\n"
        "    sys.stdout.write(completed.stdout)\n"
        "    sys.stderr.write(completed.stderr)\n"
        "    return int(completed.returncode)\n"
        "\n"
        "if __name__ == \"__main__\":\n"
        "    raise SystemExit(\n"
        "        run_tool(\n"
        "            _invoke_original,\n"
        "            [\"--tool\", TOOL_ID, *sys.argv[1:]],\n"
        "            tool_id=TOOL_ID,\n"
        "        )\n"
        "    )\n",
        encoding="utf-8",
    )
    launcher_path.chmod(0o755)
    metadata = {
        "schema_version": 1,
        "installed": True,
        "tool_id": skill_id,
        "launcher": "source/run.py",
        "original_launcher": f"source/{_TRAJECTORY_ORIGINAL_LAUNCHER_FILENAME}",
        "legacy_log_neutralized": legacy_log_neutralized,
        "trajectory_recorder": "TadACampaign.tool_trajectory.run_tool",
    }
    (skill_dir / _TRAJECTORY_METADATA_FILENAME).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


def _validate_skill_id(skill_id: str) -> str:
    normalized = str(skill_id).strip()
    if not normalized or _SKILL_ID_RE.fullmatch(normalized) is None:
        raise ValueError(f"Illegal skill_id {skill_id!r}")
    return normalized


def _resolve_market_path(base_dir: Path, raw_value: str) -> Path:
    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (base_dir / candidate).resolve()


def _build_archive_bytes(package_root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(package_root, arcname="source", recursive=True)
    return buffer.getvalue()


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.getmembers():
        member_path = (destination / member.name).resolve()
        if member_path != destination and destination not in member_path.parents:
            raise ValueError(f"Archive member escapes target directory: {member.name}")
    try:
        archive.extractall(destination, filter="data")
    except TypeError:
        archive.extractall(destination)


def _relative_workspace_skill_dir(skill_id: str) -> str:
    return f"owned_skills/{skill_id}"


@dataclass(frozen=True)
class SkillMarketEntry:
    skill_id: str
    display_name: str
    price: float
    summary: str
    description: str
    tags: tuple[str, ...]
    source_repo: str
    source_path: str
    package_root: Path
    bundle_sha256: str
    bundle_size_bytes: int

    def market_card(self, *, purchase_days: float, daily_burn_cost: float, owned: bool) -> dict[str, Any]:
        burn_cost = purchase_days * daily_burn_cost
        return {
            "skill_id": self.skill_id,
            "display_name": self.display_name,
            "summary": self.summary,
            "description": self.description,
            "tags": list(self.tags),
            "price": self.price,
            "time_days": purchase_days,
            "burn_cost": burn_cost,
            "total_cost": self.price + burn_cost,
            "owned": owned,
        }

    def owned_index_item(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "display_name": self.display_name,
            "summary": self.summary,
            "description": self.description,
            "tags": list(self.tags),
            "price": self.price,
            "bundle_sha256": self.bundle_sha256,
            "bundle_size_bytes": self.bundle_size_bytes,
            "workspace_relative_dir": _relative_workspace_skill_dir(self.skill_id),
            "workspace_relative_source_dir": f"{_relative_workspace_skill_dir(self.skill_id)}/source",
            "workspace_relative_sidecar_path": f"{_relative_workspace_skill_dir(self.skill_id)}/{_SIDECAR_FILENAME}",
        }


@dataclass
class SkillMarketCatalog:
    entries_by_id: dict[str, SkillMarketEntry]
    bundle_bytes_by_id: dict[str, bytes]

    def entry(self, skill_id: str) -> SkillMarketEntry | None:
        return self.entries_by_id.get(skill_id)

    def bundle_bytes(self, skill_id: str) -> bytes | None:
        return self.bundle_bytes_by_id.get(skill_id)

    def ordered_entries(self) -> list[SkillMarketEntry]:
        return list(self.entries_by_id.values())

    def market_snapshot(
        self,
        *,
        owned_skill_ids: set[str],
        purchase_days: float,
        daily_burn_cost: float,
    ) -> dict[str, Any]:
        return {
            "market_size": len(self.entries_by_id),
            "purchase_skill_days": purchase_days,
            "cards": [
                entry.market_card(
                    purchase_days=purchase_days,
                    daily_burn_cost=daily_burn_cost,
                    owned=entry.skill_id in owned_skill_ids,
                )
                for entry in self.ordered_entries()
            ],
        }

    def owned_skills_snapshot(self, *, owned_skill_ids: set[str]) -> dict[str, Any]:
        ordered_ids = [entry.skill_id for entry in self.ordered_entries() if entry.skill_id in owned_skill_ids]
        return {
            "workspace_root": "owned_skills",
            "owned_skill_count": len(ordered_ids),
            "owned_skill_ids": ordered_ids,
            "skills": [self.entries_by_id[skill_id].owned_index_item() for skill_id in ordered_ids],
        }

    def owned_bundle_payload(self, skill_id: str) -> dict[str, Any]:
        entry = self.entry(skill_id)
        bundle_bytes = self.bundle_bytes(skill_id)
        if entry is None or bundle_bytes is None:
            raise KeyError(skill_id)
        return {
            "skill_id": entry.skill_id,
            "bundle_sha256": entry.bundle_sha256,
            "bundle_size_bytes": entry.bundle_size_bytes,
            "archive_format": "tar.gz",
            "archive_base64": base64.b64encode(bundle_bytes).decode("ascii"),
        }


def empty_skill_market() -> SkillMarketCatalog:
    return SkillMarketCatalog(entries_by_id={}, bundle_bytes_by_id={})


def load_skill_market_catalog(manifest_path: Path | None) -> SkillMarketCatalog:
    if manifest_path is None:
        return empty_skill_market()
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Skill market manifest not found: {manifest_path}")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    skills_payload = payload.get("skills", [])
    if not isinstance(skills_payload, list):
        raise ValueError(f"Skill market manifest {manifest_path} must define a 'skills' array")

    base_dir = manifest_path.parent.resolve()
    entries_by_id: dict[str, SkillMarketEntry] = {}
    bundle_bytes_by_id: dict[str, bytes] = {}
    for item in skills_payload:
        if not isinstance(item, dict):
            raise ValueError(f"Skill market manifest {manifest_path} contains a non-object skill entry")
        skill_id = _validate_skill_id(str(item.get("skill_id") or ""))
        if skill_id in entries_by_id:
            raise ValueError(f"Duplicate skill_id {skill_id!r} in {manifest_path}")

        package_root = _resolve_market_path(base_dir, str(item["package_root"]))
        if not package_root.is_dir():
            raise FileNotFoundError(f"Skill package root does not exist: {package_root}")
        bundle_bytes = _build_archive_bytes(package_root)
        bundle_sha256 = hashlib.sha256(bundle_bytes).hexdigest()

        entry = SkillMarketEntry(
            skill_id=skill_id,
            display_name=str(item.get("display_name") or skill_id),
            price=float(item["price"]),
            summary=str(item.get("summary") or ""),
            description=str(item.get("description") or ""),
            tags=tuple(str(tag) for tag in item.get("tags", [])) if isinstance(item.get("tags", []), list) else tuple(),
            source_repo=str(item.get("source_repo") or ""),
            source_path=str(item.get("source_path") or package_root.name),
            package_root=package_root,
            bundle_sha256=bundle_sha256,
            bundle_size_bytes=len(bundle_bytes),
        )
        entries_by_id[skill_id] = entry
        bundle_bytes_by_id[skill_id] = bundle_bytes

    return SkillMarketCatalog(entries_by_id=entries_by_id, bundle_bytes_by_id=bundle_bytes_by_id)


def current_materialized_bundle_sha(skill_dir: Path) -> str | None:
    sidecar_path = skill_dir / _SIDECAR_FILENAME
    source_dir = skill_dir / "source"
    if not sidecar_path.is_file() or not source_dir.is_dir():
        return None
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    candidate = payload.get("bundle_sha256")
    return str(candidate) if isinstance(candidate, str) and candidate else None


def materialize_owned_skill(
    skill_dir: Path,
    *,
    bundle_payload: dict[str, Any],
    index_item: dict[str, Any],
) -> None:
    archive_base64 = bundle_payload.get("archive_base64")
    expected_sha256 = bundle_payload.get("bundle_sha256")
    if not isinstance(archive_base64, str) or not archive_base64:
        raise ValueError("Missing archive_base64 in owned skill bundle payload")
    if not isinstance(expected_sha256, str) or not expected_sha256:
        raise ValueError("Missing bundle_sha256 in owned skill bundle payload")

    archive_bytes = base64.b64decode(archive_base64.encode("ascii"))
    observed_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    if observed_sha256 != expected_sha256:
        raise ValueError(
            f"Owned skill bundle sha256 mismatch for {bundle_payload.get('skill_id')}: "
            f"{observed_sha256} != {expected_sha256}"
        )

    skill_dir = skill_dir.resolve()
    tmp_dir = skill_dir.parent / f".{skill_dir.name}.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            _safe_extract(archive, tmp_dir)
        (tmp_dir / _SIDECAR_FILENAME).write_text(
            json.dumps(index_item, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _install_tool_trajectory_launcher(tmp_dir, skill_id=str(index_item.get("skill_id") or ""))
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        tmp_dir.rename(skill_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

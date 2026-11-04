#!/usr/bin/env python3
"""Check the portable release tree and its frozen content manifests."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
CJK = re.compile("[\u2e80-\u2fff\u3000-\u303f\u3100-\u312f\u31a0-\u31bf\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
IGNORED = {".git", ".venv", ".uv-cache", "__pycache__", ".pytest_cache", "outputs", "experiments", "prepared", "build", "dist"}


def release_files(root):
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if any(part in IGNORED or part.endswith(".egg-info") for part in parts):
            continue
        if len(parts) == 1 and path.name == "PKG-INFO":
            continue  # Generated sdist metadata is not a frozen repository input.
        if path.is_symlink():
            raise ValueError("Release files must not be symbolic links: " + str(path))
        if path.is_file():
            yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    failures = []
    files = list(release_files(root))
    for path in files:
        relative = path.relative_to(root).as_posix()
        if path.name == ".git":
            continue
        if CJK.search(relative):
            failures.append("Non-English path: " + relative)
        if path.name in {"auth.json", ".env", "credentials.json"} or path.suffix in {".pem", ".key"}:
            failures.append("Private runtime file: " + relative)
        try:
            source = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        if CJK.search(source):
            failures.append("CJK text: " + relative)
        if re.search(r"/(?:home/(?:ubuntu|runner|user)/|tmp/(?:tada|codex))", source):
            failures.append("Machine-specific source path: " + relative)
        if re.search(r"(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})", source):
            failures.append("Credential-like value: " + relative)
    for name in ["README.md", "LICENSE", "CITATION.cff", "pyproject.toml", "uv.lock",
                 "docs/reproducibility.md", "docs/evaluation.md", "paper/data/README.md",
                 "provenance/sources.json", "provenance/SHA256SUMS"]:
        if not (root / name).is_file():
            failures.append("Missing required file: " + name)
    checksums = root / "provenance/SHA256SUMS"
    if checksums.exists():
        records = {}
        for line in checksums.read_text().splitlines():
            expected, relative = line.split("  ", 1)
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                failures.append("Checksum path escapes the repository")
                continue
            records[relative] = expected
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                failures.append("Checksum mismatch: " + relative)
        actual = {p.relative_to(root).as_posix() for p in files
                  if p != checksums and p.name != ".git"}
        if actual != set(records):
            failures.append("Checksum manifest does not cover the exact release file set")
    for arm in ["original", "adapted"]:
        packages = root / "src/agent_native_tools/_tools" / arm
        if len(list(packages.glob("*/run.py"))) != 12:
            failures.append("Expected twelve packages: " + arm)
    data = root / "paper/data"
    if (data / "paired_endpoints.csv").is_file():
        with (data / "paired_endpoints.csv").open() as stream:
            pairs = list(csv.DictReader(stream))
        if len(pairs) != 120 or len({(r["tool"], r["repeat"]) for r in pairs}) != 120:
            failures.append("Pair completeness or uniqueness failure")
    result = {"status": "passed" if not failures else "failed",
              "files_scanned": len(files), "errors": failures}
    print(json.dumps(result, indent=2))
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())


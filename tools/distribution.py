#!/usr/bin/env python3
"""Sync and verify the nested skills/fireworks-branch-killer distribution mirror."""

from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / "skills" / "fireworks-branch-killer"


def package_file_list() -> list[Path]:
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    entries = pkg.get("files", [])
    selected: set[Path] = set()
    for item in entries:
        if not isinstance(item, str) or item.startswith("!"):
            continue
        raw = item.rstrip("/")
        target = ROOT / raw
        if target.is_file():
            selected.add(Path(raw))
        elif target.is_dir():
            for fp in target.rglob("*"):
                if not fp.is_file():
                    continue
                rel = fp.relative_to(ROOT)
                if "__pycache__" in rel.parts or rel.suffix == ".pyc" or rel.name == ".DS_Store":
                    continue
                selected.add(rel)
    if Path("SKILL.md") not in selected or Path("package.json") not in selected:
        selected.add(Path("SKILL.md"))
        selected.add(Path("package.json"))
    return sorted(selected)


def _file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode) & 0o777


def sync_nested_skill(check: bool = False) -> list[str]:
    paths = package_file_list()
    expected = {p.as_posix() for p in paths}
    problems: list[str] = []

    if check:
        if not MIRROR.is_dir():
            return [f"missing nested skill mirror: {MIRROR.relative_to(ROOT)}"]
        actual = {
            p.relative_to(MIRROR).as_posix()
            for p in MIRROR.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != ".DS_Store"
        }
        for rel in sorted(expected - actual):
            problems.append(f"mirror missing: {rel}")
        for rel in sorted(actual - expected):
            problems.append(f"mirror has unexpected file: {rel}")
        for rel in sorted(expected & actual):
            src = ROOT / rel
            dst = MIRROR / rel
            if src.read_bytes() != dst.read_bytes():
                problems.append(f"mirror content differs: {rel}")
            if bool(_file_mode(src) & stat.S_IXUSR) != bool(_file_mode(dst) & stat.S_IXUSR):
                problems.append(f"mirror executable mode differs: {rel}")
        return problems

    if MIRROR.exists():
        shutil.rmtree(MIRROR)
    for rel in paths:
        src = ROOT / rel
        dst = MIRROR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage fireworks-branch-killer nested skill distribution mirror")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sync", action="store_true", help="Sync root files into skills/fireworks-branch-killer/")
    group.add_argument("--check", action="store_true", help="Verify nested mirror matches root files byte-for-byte")
    args = parser.parse_args()

    if args.sync:
        sync_nested_skill(check=False)
        print(f"Synced {len(package_file_list())} files to {MIRROR.relative_to(ROOT)}")
        return 0

    problems = sync_nested_skill(check=True)
    if problems:
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        return 1
    print(f"Distribution mirror OK ({len(package_file_list())} files verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

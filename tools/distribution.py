#!/usr/bin/env python3
"""Sync, verify, and package fireworks-branch-killer distribution artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / "skills" / "fireworks-branch-killer"


def load_package_meta() -> dict:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))


def package_file_list() -> list[Path]:
    pkg = load_package_meta()
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
    for req in (Path("SKILL.md"), Path("package.json")):
        if req not in selected and (ROOT / req).is_file():
            selected.add(req)
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
            if p.is_file() and "__pycache__" not in p.parts and path_ok(p)
        }
        for rel in sorted(expected - actual):
            problems.append(f"mirror missing: {rel}")
        for rel in sorted(actual - expected):
            problems.append(f"mirror has unexpected file: {rel}")
        for rel in sorted(expected & actual):
            source = ROOT / rel
            target = MIRROR / rel
            if source.read_bytes() != target.read_bytes():
                problems.append(f"mirror content differs: {rel}")
            if bool(_file_mode(source) & stat.S_IXUSR) != bool(_file_mode(target) & stat.S_IXUSR):
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


def path_ok(p: Path) -> bool:
    return p.suffix != ".pyc" and p.name != ".DS_Store"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_release(output_dir: Path) -> list[Path]:
    version = load_package_meta()["version"]
    paths = package_file_list()
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"fireworks-branch-killer-v{version}"

    tgz_path = output_dir / f"{prefix}.tar.gz"
    with tarfile.open(tgz_path, "w:gz") as tf:
        for rel in paths:
            tf.add(ROOT / rel, arcname=f"{prefix}/{rel.as_posix()}", recursive=False)

    zip_path = output_dir / f"{prefix}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in paths:
            src = ROOT / rel
            zi = zipfile.ZipInfo(f"{prefix}/{rel.as_posix()}")
            mode = _file_mode(src)
            zi.external_attr = (stat.S_IFREG | mode) << 16
            zf.writestr(zi, src.read_bytes())

    sums_path = output_dir / "SHA256SUMS.txt"
    sums_lines = [
        f"{sha256_file(tgz_path)}  {tgz_path.name}",
        f"{sha256_file(zip_path)}  {zip_path.name}",
    ]
    sums_path.write_text("\n".join(sums_lines) + "\n", encoding="utf-8")
    return [tgz_path, zip_path, sums_path]


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage fireworks-branch-killer distribution mirror and release bundles")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sync", action="store_true", help="Sync root files into skills/fireworks-branch-killer/")
    group.add_argument("--check", action="store_true", help="Verify nested mirror matches root files byte-for-byte")
    group.add_argument("--build-release", action="store_true", help="Build .tar.gz, .zip, and SHA256SUMS.txt")
    parser.add_argument("--output-dir", default="dist", help="Output directory for --build-release")
    args = parser.parse_args()

    if args.sync:
        sync_nested_skill(check=False)
        print(f"Synced {len(package_file_list())} files to {MIRROR.relative_to(ROOT)}")
        return 0

    if args.build_release:
        problems = sync_nested_skill(check=True)
        if problems:
            for p in problems:
                print(f"ERROR: {p}", file=sys.stderr)
            return 1
        out_dir = Path(args.output_dir) if Path(args.output_dir).is_absolute() else (ROOT / args.output_dir)
        built = build_release(out_dir)
        for p in built:
            print(f"Built: {p}")
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

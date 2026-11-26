#!/usr/bin/env python3
"""Verify repository consistency, docs links, version parity, and zero private leaks."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TOKENS = ["parallax-ai", "centripol", "qa-artifacts", "/Users/" + "brad"]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def check_versions() -> list[str]:
    errors: list[str] = []
    pkg_ver = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    fbk_text = (ROOT / "scripts" / "fbk.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', fbk_text, re.MULTILINE)
    fbk_ver = m.group(1) if m else ""
    chg_text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m2 = re.search(r"^## \[([^\]]+)\]", chg_text, re.MULTILINE)
    chg_ver = m2.group(1) if m2 else ""
    if not (pkg_ver == fbk_ver == chg_ver):
        errors.append(f"Version mismatch: package.json={pkg_ver}, fbk.py={fbk_ver}, CHANGELOG.md={chg_ver}")
    return errors


def check_skill_frontmatter() -> list[str]:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return ["SKILL.md missing YAML frontmatter"]
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return ["SKILL.md malformed YAML frontmatter"]
    fm = parts[1]
    errors: list[str] = []
    if "name: fireworks-branch-killer" not in fm:
        errors.append("SKILL.md frontmatter missing `name: fireworks-branch-killer`")
    if "description:" not in fm:
        errors.append("SKILL.md frontmatter missing `description:`")
    return errors


def check_markdown_links() -> list[str]:
    errors: list[str] = []
    for md_name in ("README.md", "README.zh.md", "SKILL.md"):
        md_path = ROOT / md_name
        if not md_path.is_file():
            errors.append(f"Missing {md_name}")
            continue
        content = md_path.read_text(encoding="utf-8")
        for raw_target in LINK_RE.findall(content):
            target = raw_target.split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "file://")):
                continue
            resolved = (md_path.parent / target).resolve()
            if not resolved.exists():
                errors.append(f"{md_name}: broken local link `{raw_target}`")
    return errors


def check_private_leaks() -> list[str]:
    errors: list[str] = []
    scan_exts = {".py", ".md", ".json", ".yaml", ".yml", ".svg", ".txt", ".sh"}
    for fp in ROOT.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(ROOT)
        if ".git" in rel.parts or "__pycache__" in rel.parts or rel.parts[0] == "skills":
            continue
        if fp.name == "check_project_consistency.py":
            continue
        if fp.suffix not in scan_exts and fp.name not in ("LICENSE",):
            continue
        txt = fp.read_text(encoding="utf-8", errors="replace")
        for tok in FORBIDDEN_TOKENS:
            if tok in txt:
                errors.append(f"Forbidden private token `{tok}` found in {rel}")
    return errors


def main() -> int:
    errors = []
    errors.extend(check_versions())
    errors.extend(check_skill_frontmatter())
    errors.extend(check_markdown_links())
    errors.extend(check_private_leaks())
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("Project consistency OK (versions, SKILL.md, links, zero private leaks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

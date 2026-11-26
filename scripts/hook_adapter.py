#!/usr/bin/env python3
"""
Universal multi-harness SessionStart & PreToolUse hook adapter for fireworks-branch-killer.

Supports:
  - agy (Antigravity): camelCase stdin payload -> SessionStart {"injectSteps": [...]} / PreToolUse {"decision": ...}
  - claude-code (Claude Code): snake_case stdin payload -> SessionStart/PreToolUse {"hookSpecificOutput": {...}}
  - codex / deepseek-harness / amy / t3-code / orca / plain: concise single-line text when needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_STATE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "fireworks-branch-killer",
)
SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = SKILL_DIR / "config.example.json"
FBK_SCRIPT_PATH = SKILL_DIR / "scripts" / "fbk.py"

JSON_HARNESSES = {"agy", "claude-code"}

CANONICAL_REMOTE_RE = re.compile(r"^(https?://|ssh://|git://|git@[^:]+:)", re.IGNORECASE)
GIT_REMOTE_ADD_RE = re.compile(
    r"""\bgit\b(?:\s+-C\s+(?:"([^"]+)"|'([^']+)'|(\S+)))?.*\bremote\s+add\s+(?:-\S+\s+)*(\S+)\s+(?:"([^"]+)"|'([^']+)'|(\S+))"""
)
GIT_WORKTREE_ADD_RE = re.compile(
    r"""\bgit\b(?:\s+-C\s+(?:"([^"]+)"|'([^']+)'|(\S+)))?.*\bworktree\s+add\b"""
)


def load_hook_config(state_dir: Path) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "hook_cooldown_hours": 6,
        "repo_worktree_quota": 10,
        "pre_tool_guard": {
            "enabled": True,
            "local_remote_add_action": "ask",
            "worktree_quota_action": "ask",
        },
        "hook_thresholds": {
            "worktree_count": 20,
            "branch_count_per_repo": 100,
            "broken_worktree_count": 1,
        },
        "nested_worktree_dirs": [
            ".worktrees",
            "worktrees",
            ".claude/worktrees",
            ".codex/worktrees",
            ".t3/worktrees",
            ".amy/worktrees",
            ".orca/worktrees",
        ],
    }
    for path in (DEFAULT_CONFIG_PATH, state_dir / "config.json"):
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if "hook_cooldown_hours" in data:
                    cfg["hook_cooldown_hours"] = data["hook_cooldown_hours"]
                if "repo_worktree_quota" in data:
                    cfg["repo_worktree_quota"] = data["repo_worktree_quota"]
                if isinstance(data.get("pre_tool_guard"), dict):
                    cfg["pre_tool_guard"].update(data["pre_tool_guard"])
                if isinstance(data.get("hook_thresholds"), dict):
                    cfg["hook_thresholds"].update(data["hook_thresholds"])
                if isinstance(data.get("nested_worktree_dirs"), list):
                    cfg["nested_worktree_dirs"] = data["nested_worktree_dirs"]
            except Exception:
                pass
    return cfg


def detect_harness_mode(cli_harness: str, payload: Dict[str, Any]) -> str:
    if cli_harness != "auto":
        return cli_harness
    if "hookEventName" in payload or "workspacePaths" in payload or "toolCall" in payload:
        return "agy"
    if (
        "hook_event_name" in payload
        or "session_id" in payload
        or "tool_input" in payload
        or os.environ.get("CLAUDE_PROJECT_DIR")
    ):
        return "claude-code"
    if os.environ.get("CODEX_CI") or os.environ.get("CODEX_HOME"):
        return "codex"
    if (
        os.environ.get("ORCA_PANE_KEY")
        or os.environ.get("ORCA_AGENT_PANE")
        or os.environ.get("ORCA_WORKTREE_ID")
        or os.environ.get("ORCA_SESSION_ID")
        or os.environ.get("ORCA_WORKSPACE")
        or os.environ.get("ORCA_HOME")
    ):
        return "orca"
    return "plain"


def detect_event_mode(cli_event: str, payload: Dict[str, Any]) -> str:
    if cli_event != "auto":
        return cli_event
    ev = str(payload.get("hookEventName") or payload.get("hook_event_name") or "")
    if ev == "PreToolUse":
        return "PreToolUse"
    return "SessionStart"


def resolve_target(payload: Dict[str, Any]) -> str:
    ws_paths = payload.get("workspacePaths") or []
    if ws_paths:
        return str(ws_paths[0])
    if payload.get("cwd"):
        return str(payload["cwd"])
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return os.environ["CLAUDE_PROJECT_DIR"]
    return os.getcwd()


def extract_command_line(payload: Dict[str, Any]) -> str:
    tc = payload.get("toolCall")
    if isinstance(tc, dict):
        args = tc.get("args")
        if isinstance(args, dict) and args.get("CommandLine"):
            return str(args["CommandLine"])
    ti = payload.get("tool_input")
    if isinstance(ti, dict) and ti.get("command"):
        return str(ti["command"])
    if payload.get("command"):
        return str(payload["command"])
    return ""


def emit_pre_tool_allow(harness: str) -> None:
    if harness == "agy":
        print(json.dumps({"decision": "allow"}))
    elif harness == "claude-code":
        print("{}")


def emit_pre_tool_ask(harness: str, reason: str) -> None:
    if harness == "agy":
        print(json.dumps({"decision": "ask", "reason": reason}, ensure_ascii=False))
    elif harness == "claude-code":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": reason,
                    }
                },
                ensure_ascii=False,
            )
        )
    else:
        print(reason)


def count_repo_linked_worktrees(repo_dir: str) -> int:
    try:
        proc = subprocess.run(
            ["git", "-C", repo_dir, "worktree", "list", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.6,
        )
        if proc.returncode == 0:
            wt_count = sum(1 for ln in proc.stdout.splitlines() if ln.startswith("worktree "))
            return max(0, wt_count - 1)
    except Exception:
        pass
    return 0


def handle_pre_tool_use(harness: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> int:
    guard_cfg = cfg.get("pre_tool_guard", {})
    if not guard_cfg.get("enabled", True):
        emit_pre_tool_allow(harness)
        return 0

    cmd = extract_command_line(payload)
    if not cmd:
        emit_pre_tool_allow(harness)
        return 0

    # Check 1: git remote add <name> <local/tmp path>
    m_rem = GIT_REMOTE_ADD_RE.search(cmd)
    if m_rem:
        remote_name = m_rem.group(4) or ""
        remote_url = m_rem.group(5) or m_rem.group(6) or m_rem.group(7) or ""
        if remote_url and not CANONICAL_REMOTE_RE.match(remote_url):
            reason = (
                f"[fireworks-branch-killer] Detected non-canonical local/tmp `git remote add {remote_name} {remote_url}`. "
                f"Local Agent remotes cause refs/remotes avalanche; prefer canonical origin or temporary fetch without persistent remote."
            )
            emit_pre_tool_ask(harness, reason)
            return 0

    # Check 2: git worktree add exceeding single-repo quota
    m_wt = GIT_WORKTREE_ADD_RE.search(cmd)
    if m_wt:
        c_dir = m_wt.group(1) or m_wt.group(2) or m_wt.group(3)
        target_dir = c_dir or str(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        quota = int(cfg.get("repo_worktree_quota", 10))
        linked = count_repo_linked_worktrees(target_dir)
        if linked >= quota:
            reason = (
                f"[fireworks-branch-killer] Repo '{os.path.basename(os.path.realpath(target_dir))}' already has {linked} linked worktrees "
                f"(quota={quota}). Consider running `python3 {FBK_SCRIPT_PATH} close` or `clean --tier 0` before adding more."
            )
            emit_pre_tool_ask(harness, reason)
            return 0

    emit_pre_tool_allow(harness)
    return 0


def fast_count_branches(repo_path: str) -> int:
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, "for-each-ref", "--format=.", "refs/heads/"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
        if proc.returncode == 0:
            return len(proc.stdout.splitlines())
    except Exception:
        pass
    return 0


def check_broken_dot_git(dir_path: str) -> bool:
    g = os.path.join(dir_path, ".git")
    if not os.path.isfile(g):
        return False
    try:
        first = open(g, "r", encoding="utf-8", errors="replace").readline().strip()
        if first.startswith("gitdir:"):
            raw = first.split("gitdir:", 1)[1].strip()
            gd = raw if os.path.isabs(raw) else os.path.join(dir_path, raw)
            return not os.path.exists(gd)
    except OSError:
        pass
    return False


def fast_scan_target(target: str, nested_dirs: List[str]) -> Dict[str, Any]:
    primary_repos: List[str] = []
    seen_primary: set[str] = set()
    seen_linked: set[str] = set()
    linked_count = 0
    broken_count = 0

    def add_primary(p: str) -> None:
        rp = os.path.realpath(p)
        if rp not in seen_primary:
            seen_primary.add(rp)
            primary_repos.append(rp)

    def add_linked(p: str, check_broken: bool = True) -> None:
        nonlocal linked_count, broken_count
        rp = os.path.realpath(p)
        if rp in seen_linked or rp in seen_primary:
            return
        seen_linked.add(rp)
        linked_count += 1
        if check_broken and check_broken_dot_git(rp):
            broken_count += 1

    dot_git = os.path.join(target, ".git")
    if os.path.isdir(dot_git):
        add_primary(target)
        try:
            proc = subprocess.run(
                ["git", "--no-optional-locks", "-C", target, "worktree", "list", "--porcelain"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.2,
            )
            if proc.returncode == 0:
                target_rp = os.path.realpath(target)
                for ln in proc.stdout.splitlines():
                    if ln.startswith("worktree "):
                        wp = os.path.realpath(ln[len("worktree ") :].strip())
                        if wp != target_rp:
                            add_linked(wp, check_broken=False)
                    elif ln == "prunable" or ln.startswith("prunable "):
                        broken_count += 1
        except Exception:
            pass
    elif os.path.isfile(dot_git):
        add_linked(target, check_broken=True)
    elif os.path.isdir(target):
        try:
            with os.scandir(target) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    g = os.path.join(entry.path, ".git")
                    if os.path.isdir(g):
                        add_primary(entry.path)
                    elif os.path.isfile(g):
                        add_linked(entry.path, check_broken=True)
        except OSError:
            pass

    for rp in list(primary_repos):
        for nd in nested_dirs:
            nd_path = os.path.join(rp, nd)
            if os.path.isdir(nd_path):
                try:
                    for sub in os.scandir(nd_path):
                        if not sub.is_dir(follow_symlinks=False):
                            continue
                        sg = os.path.join(sub.path, ".git")
                        if os.path.isfile(sg):
                            add_linked(sub.path, check_broken=True)
                        elif os.path.isdir(sg):
                            add_primary(sub.path)
                except OSError:
                    pass

    max_branches = 0
    max_branch_repo = ""
    deadline = time.monotonic() + 1.0
    for rp in primary_repos:
        if time.monotonic() > deadline:
            break
        cnt = fast_count_branches(rp)
        if cnt > max_branches:
            max_branches = cnt
            max_branch_repo = os.path.basename(rp)

    return {
        "target": target,
        "primary_repo_count": len(primary_repos),
        "worktree_count": linked_count,
        "broken_worktree_count": broken_count,
        "max_branch_count": max_branches,
        "max_branch_repo": max_branch_repo,
    }


def emit_empty(harness: str) -> None:
    if harness in JSON_HARNESSES:
        print("{}")


def emit_alert(harness: str, alerts: List[str]) -> None:
    summary = (
        "[fireworks-branch-killer] Workspace hygiene threshold exceeded: "
        + "; ".join(alerts)
        + f". Suggest running dry-run audit via `python3 {FBK_SCRIPT_PATH} scan --fast --no-size` when convenient."
    )
    if harness == "agy":
        user_md = (
            f"**[fireworks-branch-killer]** 检测到工作区分支/Worktree 偏多：{'; '.join(alerts)}。"
            f"需要清理时可让我执行 `fireworks-branch-killer` 只读体检（默认不删文件）。"
        )
        out = {
            "injectSteps": [
                {
                    "systemMessage": {
                        "systemMessage": summary,
                        "metadata": {"user_markdown": user_md},
                    }
                }
            ]
        }
        print(json.dumps(out, ensure_ascii=False))
    elif harness == "claude-code":
        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": summary,
            }
        }
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(summary)


def handle_session_start(harness: str, payload: Dict[str, Any], cfg: Dict[str, Any], state_dir: Path) -> int:
    cooldown_secs = float(cfg.get("hook_cooldown_hours", 6)) * 3600.0
    force = os.environ.get("FBK_HOOK_FORCE", "") == "1"

    cache_file = state_dir / "hook-cache.json"
    cache: Dict[str, Any] = {}
    now = time.time()
    if cache_file.is_file():
        try:
            with open(cache_file, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}

    notified_map = cache.get("notified_by_harness", {})
    if not isinstance(notified_map, dict):
        notified_map = {}

    if not force and (now - float(notified_map.get(harness, 0))) < cooldown_secs:
        emit_empty(harness)
        return 0

    target = resolve_target(payload)
    stats = cache.get("stats")
    last_scan_ts = float(cache.get("last_checked_ts", 0))
    if force or not isinstance(stats, dict) or stats.get("target") != target or (now - last_scan_ts) >= cooldown_secs:
        stats = fast_scan_target(target, cfg.get("nested_worktree_dirs", []))
        cache["last_checked_ts"] = now
        cache["stats"] = stats

    th = cfg.get("hook_thresholds", {})
    wt_th = int(th.get("worktree_count", 20))
    br_th = int(th.get("branch_count_per_repo", 100))
    bk_th = int(th.get("broken_worktree_count", 1))

    alerts: List[str] = []
    if int(stats.get("worktree_count", 0)) >= wt_th:
        alerts.append(f"{stats['worktree_count']} linked worktrees (>= {wt_th})")
    if int(stats.get("max_branch_count", 0)) >= br_th:
        alerts.append(
            f"repo '{stats.get('max_branch_repo', '')}' has {stats['max_branch_count']} local branches (>= {br_th})"
        )
    if int(stats.get("broken_worktree_count", 0)) >= bk_th:
        alerts.append(f"{stats['broken_worktree_count']} broken worktree pointer(s)")

    if alerts:
        notified_map[harness] = now
    cache["notified_by_harness"] = notified_map
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if not alerts:
        emit_empty(harness)
        return 0

    emit_alert(harness, alerts)
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--harness",
        choices=["auto", "agy", "claude-code", "codex", "deepseek-harness", "amy", "t3-code", "orca", "plain"],
        default="auto",
    )
    parser.add_argument(
        "--event",
        choices=["auto", "SessionStart", "PreToolUse"],
        default="auto",
    )
    args, _ = parser.parse_known_args(argv)

    harness = args.harness if args.harness != "auto" else "plain"
    event = args.event if args.event != "auto" else "SessionStart"
    try:
        raw_stdin = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
        payload = json.loads(raw_stdin) if raw_stdin else {}
        if not isinstance(payload, dict):
            payload = {}

        harness = detect_harness_mode(args.harness, payload)
        event = detect_event_mode(args.event, payload)
        state_dir = Path(os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR))
        cfg = load_hook_config(state_dir)

        if event == "PreToolUse":
            return handle_pre_tool_use(harness, payload, cfg)
        return handle_session_start(harness, payload, cfg, state_dir)
    except Exception:
        if event == "PreToolUse":
            emit_pre_tool_allow(harness)
        else:
            emit_empty(harness)
        return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
fireworks-branch-killer (fbk) deterministic safety engine (v2.1).

Zero third-party dependencies (Python 3.9+ stdlib + system git).
Manages, audits, slims, closes, cleans, installs, repairs, bundles, and restores
git worktrees and feature branches across AI coding harnesses (Claude Code, Codex,
AGY, DeepSeek Harness, Amy, T3 Code, Orca).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

VERSION = "1.1.0"
DEFAULT_STATE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "fireworks-branch-killer",
)
SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = SKILL_DIR / "config.example.json"

ALL_HARNESSES = [
    "agy",
    "claude-code",
    "codex",
    "deepseek-harness",
    "amy",
    "t3-code",
    "orca",
]

DEFAULT_HARNESS_PATTERNS: Dict[str, List[str]] = {
    "claude-code": [
        r"\.claude/worktrees",
        r"(?:^|[/_-])claude[-_]",
        r"(?:^|[/_-])calude[-_]",
        r"pro-claude",
    ],
    "codex": [
        r"\.codex/worktrees",
        r"(?:^|[/_-])codex",
        r"pro-codex",
    ],
    "agy": [
        r"(?:^|[/_-])agy[-_]",
        r"antigravity",
        r"\.gemini",
        r"(?:^|[/_-])ge\d+[-_]",
        r"pro-worker-ge\d+",
    ],
    "deepseek-harness": [
        r"(?:^|[/_-])wt-ds",
        r"(?:^|[/_-])ds\d*[-_]",
        r"deepseek",
        r"\.scratch-ds",
    ],
    "amy": [
        r"\.amy/worktrees",
        r"(?:^|[/_-])amy[-_]",
        r"/amy/",
    ],
    "t3-code": [
        r"\.t3/worktrees",
        r"(?:^|[/_-])t3[-_]",
        r"t3code",
    ],
    "orca": [
        r"\.orca/worktrees",
        r"(?:^|[/_-])orca[-_]",
        r"/orca/",
    ],
    "gate-verifier": [
        r"(?:^|/)gate-[0-9a-f]{8,}-\d+",
    ],
}

SKIP_NESTED_NAMES = {
    ".git",
    "node_modules",
    ".next",
    ".nuxt",
    "target",
    "dist",
    "build",
    ".venv",
    "__pycache__",
    "vendor",
    ".cache",
    "artifacts",
}

CANONICAL_REMOTE_RE = re.compile(r"^(https?://|ssh://|git://|git@[^:]+:)", re.IGNORECASE)
_REFLOG_TS_RE = re.compile(r">\s+(\d+)\s+[+-]\d{4}\t")

DEFAULT_CONFIG: Dict[str, Any] = {
    "grace_period_hours": 24,
    "protected_branch_patterns": [
        "^(main|master|develop|dev)$",
        "^release/.*",
        "^prod.*",
    ],
    "protected_path_patterns": [],
    "vault_retention_days": 7,
    "slim_idle_days": 7,
    "slim_cache_dirs": [
        "node_modules",
        ".next",
        ".nuxt",
        "target",
        "dist",
        "build",
        ".turbo",
        ".parcel-cache",
        ".venv",
        "__pycache__",
        ".pytest_cache",
    ],
    "max_squash_check_per_repo": 100,
    "scan_workers": 6,
    "discover_max_depth": 3,
    "delivery_guard_enabled": True,
    "cutover_worktree_threshold": 50,
    "cutover_remote_refs_threshold": 10000,
    "repo_worktree_quota": 10,
    "ephemeral_dir_patterns": [r"^gate-[0-9a-f]{8,}-(\d+)$"],
    "extra_bases": {},
    "protected_shas": [],
    "pre_tool_guard": {
        "enabled": True,
        "local_remote_add_action": "ask",
        "worktree_quota_action": "ask",
    },
    "remote_audit_stale_days": 30,
    "remote_audit_recent_days": 14,
    "additional_worktree_roots": [
        "~/.codex/worktrees",
        "~/.t3/worktrees",
        "~/.claude/worktrees",
        "~/.amy/worktrees",
        "~/.orca/worktrees",
    ],
    "nested_worktree_dirs": [
        ".worktrees",
        "worktrees",
        ".claude/worktrees",
        ".codex/worktrees",
        ".t3/worktrees",
        ".amy/worktrees",
        ".orca/worktrees",
    ],
    "harness_patterns": DEFAULT_HARNESS_PATTERNS,
    "hook_cooldown_hours": 6,
    "hook_thresholds": {
        "worktree_count": 20,
        "branch_count_per_repo": 100,
        "broken_worktree_count": 1,
    },
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_bytes(num_bytes: int) -> str:
    if num_bytes < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(num_bytes)
    for unit in units:
        if val < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(val)} B"
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{num_bytes} B"


def realpath_str(path: str | Path) -> str:
    return os.path.realpath(os.fspath(path))


def rel_display(path: str, base: Optional[str]) -> str:
    if not base:
        return path
    try:
        rp = realpath_str(path)
        rb = realpath_str(base)
        if rp == rb:
            return "."
        if rp.startswith(rb.rstrip(os.sep) + os.sep):
            return os.path.relpath(rp, rb)
    except Exception:
        pass
    return path


def is_subpath(child: str, parent: str) -> bool:
    c = realpath_str(child)
    p = realpath_str(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def dir_size_bytes(path: str) -> int:
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def make_tree_writable(path: str) -> None:
    """Safely add user write/exec permissions inside target directory prior to removal."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return
        os.chmod(path, st.st_mode | stat.S_IWUSR | (stat.S_IXUSR if stat.S_ISDIR(st.st_mode) else 0))
    except OSError:
        pass
    for root, dirs, files in os.walk(path, followlinks=False):
        try:
            st = os.lstat(root)
            os.chmod(root, st.st_mode | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
        for name in files:
            fp = os.path.join(root, name)
            try:
                st = os.lstat(fp)
                if not stat.S_ISLNK(st.st_mode):
                    os.chmod(fp, st.st_mode | stat.S_IWUSR)
            except OSError:
                pass
        for name in dirs:
            dp = os.path.join(root, name)
            try:
                st = os.lstat(dp)
                if not stat.S_ISLNK(st.st_mode):
                    os.chmod(dp, st.st_mode | stat.S_IWUSR | stat.S_IXUSR)
            except OSError:
                pass


def run_git(
    repo_path: Optional[str],
    args: List[str],
    timeout: int = 30,
    check: bool = False,
    env: Optional[Dict[str, str]] = None,
    readonly: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if readonly:
        cmd.append("--no-optional-locks")
    if repo_path:
        cmd.extend(["-C", repo_path])
    cmd.extend(args)

    merged_env = None
    if env is not None:
        merged_env = os.environ.copy()
        merged_env.update(env)

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=merged_env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git command failed ({' '.join(cmd)}): {proc.stderr.strip()}")
    return proc


def load_config(state_dir: Path, config_path: Optional[str] = None) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if DEFAULT_CONFIG_PATH.exists():
        try:
            with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception:
            pass
    user_cfg = state_dir / "config.json"
    if user_cfg.exists():
        try:
            with open(user_cfg, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception:
            pass
    if config_path:
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


def compile_harness_patterns(cfg: Dict[str, Any]) -> List[Tuple[str, List[re.Pattern[str]]]]:
    raw = cfg.get("harness_patterns") or DEFAULT_HARNESS_PATTERNS
    compiled: List[Tuple[str, List[re.Pattern[str]]]] = []
    order = ALL_HARNESSES + ["gate-verifier"]
    for h_name in order:
        pats = raw.get(h_name, [])
        if pats:
            compiled.append((h_name, [re.compile(p, re.IGNORECASE) for p in pats]))
    for h_name, pats in raw.items():
        if h_name not in order and pats:
            compiled.append((h_name, [re.compile(p, re.IGNORECASE) for p in pats]))
    return compiled


def detect_harness(
    path: Optional[str],
    branch: Optional[str],
    compiled_patterns: List[Tuple[str, List[re.Pattern[str]]]],
) -> str:
    """Match leaf basename + branch first (to avoid parent container name skew), then full path."""
    leaf_targets: List[str] = []
    if path:
        leaf_targets.append(os.path.basename(path.rstrip(os.sep)))
    if branch:
        leaf_targets.append(branch)
    for h_name, regexes in compiled_patterns:
        for text in leaf_targets:
            if text and any(rx.search(text) for rx in regexes):
                return h_name
    if path:
        for h_name, regexes in compiled_patterns:
            if any(rx.search(path) for rx in regexes):
                return h_name
    return "other"


def classify_remote_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return "none"
    if CANONICAL_REMOTE_RE.match(u):
        return "canonical_remote"
    local_p = u[len("file://") :] if u.startswith("file://") else u
    norm = local_p.replace("\\", "/")
    if norm.startswith("/tmp/") or norm.startswith("/private/tmp/") or "/tmp/" in norm or "/artifacts/" in norm or "artifacts/" in norm:
        return "tmp_or_artifact"
    if os.path.exists(local_p):
        return "local_clone"
    return "broken_local"


def is_pid_alive(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError, OSError):
        return False


def get_active_process_cwds(ignore_self: bool = False) -> Tuple[Dict[str, Set[str]], str]:
    """Run a single batched lsof to detect active process cwds and command names."""
    try:
        proc = subprocess.run(
            ["lsof", "-d", "cwd", "-F", "pcn"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}, "unavailable"

    if proc.returncode != 0 and not proc.stdout.strip():
        return {}, "unavailable"

    ignore_pids: Set[int] = set()
    if ignore_self:
        ignore_pids.add(os.getpid())
        ignore_pids.add(os.getppid())

    cwds: Dict[str, Set[str]] = {}
    current_pid: Optional[int] = None
    current_cmd = ""
    for line in proc.stdout.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            try:
                current_pid = int(val)
            except ValueError:
                current_pid = None
            current_cmd = ""
        elif tag == "c":
            current_cmd = val.strip()
        elif tag == "n":
            if current_pid is not None and current_pid in ignore_pids:
                continue
            if val.startswith("/"):
                rp = realpath_str(val)
                cwds.setdefault(rp, set())
                if current_cmd:
                    cwds[rp].add(current_cmd)
    return cwds, "ok"


def match_active_procs(wt_path: str, active_cwds: Dict[str, Set[str]]) -> Set[str]:
    cmds: Set[str] = set()
    for cwd_path, proc_cmds in active_cwds.items():
        if is_subpath(cwd_path, wt_path):
            cmds.update(proc_cmds or {"process"})
    return cmds


@dataclass
class BranchRecord:
    repo: str
    name: str
    sha: str
    base_branch: Optional[str]
    committer_ts: int
    upstream: str
    upstream_track: str
    merge_status: str
    tier: int
    reasons: List[str] = field(default_factory=list)
    attached_worktree: Optional[str] = None
    is_protected: bool = False
    within_grace: bool = False
    harness: str = "other"
    unpushed_to_origin: Optional[bool] = None


@dataclass
class WorktreeRecord:
    repo: str
    path: str
    kind: str  # primary | linked | broken_worktree | ephemeral_gate_dir
    head_sha: str
    branch: Optional[str]
    detached: bool
    locked: bool
    lock_reason: str
    prunable: bool
    dirty: bool
    dirty_count: int
    has_stash: bool
    last_activity_ts: int
    within_grace: bool
    active_proc: bool
    tier: int
    reasons: List[str] = field(default_factory=list)
    total_bytes: Optional[int] = None
    slimmable_dirs: List[str] = field(default_factory=list)
    slimmable_bytes: int = 0
    dependent_worktree_count: int = 0
    local_branch_count: int = 0
    missing_gitdir: Optional[str] = None
    harness: str = "other"
    origin_kind: str = ""
    is_pseudo_primary: bool = False
    status_failed: bool = False


@dataclass
class RepoSummary:
    repo: str
    base_branch: Optional[str]
    local_branch_count: int
    linked_worktree_count: int
    dependent_worktree_count: int
    origin_url: str = ""
    origin_kind: str = "none"
    is_pseudo_primary: bool = False
    remote_count: int = 0
    non_canonical_remotes: List[Dict[str, str]] = field(default_factory=list)
    remote_ref_count: int = 0
    unpushed_branch_count: int = 0
    cutover_recommended: bool = False
    cutover_reasons: List[str] = field(default_factory=list)
    broken_alternates: List[str] = field(default_factory=list)


@dataclass
class ScanResult:
    scanned_at: str
    mode: str
    target: str
    active_proc_check: str
    repos: List[RepoSummary] = field(default_factory=list)
    worktrees: List[WorktreeRecord] = field(default_factory=list)
    branches: List[BranchRecord] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def resolve_gitdir_pointer(wt_path: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (resolved_gitdir_path, parent_primary_repo_or_none) for a .git file."""
    dot_git = os.path.join(wt_path, ".git")
    try:
        with open(dot_git, "r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
    except OSError:
        return None, None
    if not first.startswith("gitdir:"):
        return None, None
    raw = first.split("gitdir:", 1)[1].strip()
    if not os.path.isabs(raw):
        raw = os.path.join(wt_path, raw)
    resolved = realpath_str(raw)
    marker = f"{os.sep}.git{os.sep}worktrees{os.sep}"
    parent_repo = None
    if marker in resolved:
        parent_repo = resolved.split(marker, 1)[0]
    return resolved, parent_repo


def inspect_repo_alternates(repo: str) -> List[str]:
    """Return list of missing alternate object directories referenced by repo."""
    alt_file = os.path.join(repo, ".git", "objects", "info", "alternates")
    if not os.path.isfile(alt_file):
        return []
    missing: List[str] = []
    obj_dir = os.path.join(repo, ".git", "objects")
    try:
        with open(alt_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                target = raw if os.path.isabs(raw) else os.path.join(obj_dir, raw)
                if not os.path.isdir(target):
                    missing.append(raw)
    except OSError:
        pass
    return missing


def count_remote_refs_fast(repo: str) -> int:
    """Count refs/remotes/* without invoking git for-each-ref (avoids 100k+ ref / broken-alternate hangs)."""
    git_dir = os.path.join(repo, ".git")
    refs_seen: Set[str] = set()
    packed = os.path.join(git_dir, "packed-refs")
    if os.path.isfile(packed):
        try:
            with open(packed, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    if not ln or ln[0] in ("#", "^"):
                        continue
                    parts = ln.strip().split(" ", 1)
                    if len(parts) == 2 and parts[1].startswith("refs/remotes/"):
                        refs_seen.add(parts[1])
        except OSError:
            pass
    rem_dir = os.path.join(git_dir, "refs", "remotes")
    if os.path.isdir(rem_dir):
        for root_d, _, files in os.walk(rem_dir):
            for fn in files:
                rel = os.path.relpath(os.path.join(root_d, fn), git_dir).replace(os.sep, "/")
                refs_seen.add(rel)
    if refs_seen:
        return len(refs_seen)
    # Fallback for reftable or unusual git layouts
    try:
        rr_proc = run_git(repo, ["for-each-ref", "--format=.", "refs/remotes/"], timeout=5)
        return len(rr_proc.stdout.splitlines()) if rr_proc.returncode == 0 else 0
    except subprocess.TimeoutExpired:
        return 0


def inspect_repo_remotes(repo: str) -> Tuple[str, str, int, List[Dict[str, str]], int]:
    """Return (origin_url, origin_kind, total_remotes, non_canonical_remotes, remote_ref_count)."""
    try:
        proc = run_git(repo, ["remote", "-v"], timeout=10)
    except subprocess.TimeoutExpired:
        proc = subprocess.CompletedProcess([], 1, "", "timeout")
    remotes: Dict[str, str] = {}
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                name, url = parts[0], parts[1]
                tag = parts[2] if len(parts) >= 3 else "(fetch)"
                if tag == "(fetch)" or name not in remotes:
                    remotes[name] = url

    origin_url = remotes.get("origin", "")
    origin_kind = classify_remote_url(origin_url)
    non_canonical = [
        {"name": n, "url": u, "kind": classify_remote_url(u)}
        for n, u in sorted(remotes.items())
        if classify_remote_url(u) != "canonical_remote"
    ]

    remote_ref_count = count_remote_refs_fast(repo)
    return origin_url, origin_kind, len(remotes), non_canonical, remote_ref_count


def discover_targets(
    repo_arg: Optional[str],
    root_arg: Optional[str],
    nested_dirs: Optional[List[str]] = None,
    external_roots: Optional[List[str]] = None,
    include_external_roots: bool = False,
    ephemeral_patterns: Optional[List[str]] = None,
    max_depth: int = 3,
) -> Tuple[str, str, List[str], List[Tuple[str, str]], List[Tuple[str, Optional[int]]], List[str]]:
    """
    Multi-level discovery of primary repos, broken worktree pointers, and ephemeral gate-* dirs.
    Returns: (mode, target_path, primary_repos, broken_worktrees, ephemeral_dirs, errors)
    """
    errors: List[str] = []
    primary_set: Set[str] = set()
    broken_seen: Set[str] = set()
    broken_worktrees: List[Tuple[str, str]] = []
    ephemeral_seen: Set[str] = set()
    ephemeral_dirs: List[Tuple[str, Optional[int]]] = []

    ep_res = [re.compile(p) for p in (ephemeral_patterns or [])]

    def record_ephemeral(dir_path: str, name: str) -> bool:
        for rx in ep_res:
            m = rx.match(name)
            if m:
                rp = realpath_str(dir_path)
                if rp not in ephemeral_seen:
                    ephemeral_seen.add(rp)
                    pid: Optional[int] = None
                    if m.lastindex and m.group(1) and m.group(1).isdigit():
                        pid = int(m.group(1))
                    ephemeral_dirs.append((rp, pid))
                return True
        return False

    def inspect_entry(dir_path: str, name: str, depth: int) -> None:
        dot_git = os.path.join(dir_path, ".git")
        if os.path.isdir(dot_git):
            rp = realpath_str(dir_path)
            primary_set.add(rp)
            # Check explicit nested worktree dirs (e.g. .worktrees, .claude/worktrees, etc.)
            for nd in nested_dirs or []:
                nd_path = os.path.join(rp, nd)
                if os.path.isdir(nd_path):
                    try:
                        for sub in os.scandir(nd_path):
                            if sub.is_dir(follow_symlinks=False):
                                inspect_entry(sub.path, sub.name, depth + 1)
                    except OSError:
                        pass
            # Also descend 1 level inside primary repo to discover nested mixed sub-repos / gate-* dirs
            if depth < max_depth:
                try:
                    for sub in os.scandir(rp):
                        if sub.name.startswith(".") or sub.name in SKIP_NESTED_NAMES:
                            continue
                        try:
                            if not sub.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        sub_git = os.path.join(sub.path, ".git")
                        if os.path.exists(sub_git) or any(rx.match(sub.name) for rx in ep_res):
                            inspect_entry(sub.path, sub.name, depth + 1)
                except OSError:
                    pass
            return

        if os.path.isfile(dot_git):
            gd, parent = resolve_gitdir_pointer(dir_path)
            if gd is None:
                return
            rp = realpath_str(dir_path)
            if not os.path.exists(gd):
                if rp not in broken_seen:
                    broken_seen.add(rp)
                    broken_worktrees.append((rp, gd))
            elif parent and os.path.isdir(os.path.join(parent, ".git")):
                primary_set.add(realpath_str(parent))
            return

        # Non-git directory: check ephemeral gate-* match
        if record_ephemeral(dir_path, name):
            return

        # Container directory (e.g., pro-worktrees, pro-claude, pro-cvq, .scratch-ds2): descend if within max_depth
        if depth < max_depth:
            try:
                for sub in os.scandir(dir_path):
                    if sub.name in SKIP_NESTED_NAMES:
                        continue
                    if sub.name.startswith(".") and sub.name not in {".worktrees", ".claude", ".codex", ".t3", ".amy", ".orca"} and not sub.name.startswith(".scratch-ds"):
                        continue
                    try:
                        if not sub.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    inspect_entry(sub.path, sub.name, depth + 1)
            except OSError:
                pass

    if repo_arg:
        r_abs = realpath_str(repo_arg)
        if not os.path.isdir(r_abs):
            return "repo", r_abs, [], [], [], [f"Repository path does not exist: {r_abs}"]
        proc = run_git(r_abs, ["rev-parse", "--git-common-dir"])
        if proc.returncode != 0:
            return "repo", r_abs, [], [], [], [f"Not a git repository: {r_abs}"]
        common = proc.stdout.strip()
        if not os.path.isabs(common):
            common = os.path.join(r_abs, common)
        common_real = realpath_str(common)
        main_repo = os.path.dirname(common_real) if os.path.basename(common_real) == ".git" else r_abs
        inspect_entry(main_repo, os.path.basename(main_repo), depth=1)
        return "repo", main_repo, sorted(primary_set), broken_worktrees, ephemeral_dirs, errors

    if root_arg:
        mode = "root"
        target = realpath_str(root_arg)
    else:
        cwd = realpath_str(os.getcwd())
        proc = run_git(cwd, ["rev-parse", "--git-common-dir"])
        if proc.returncode == 0:
            common = proc.stdout.strip()
            if not os.path.isabs(common):
                common = os.path.join(cwd, common)
            common_real = realpath_str(common)
            main_repo = os.path.dirname(common_real) if os.path.basename(common_real) == ".git" else cwd
            inspect_entry(main_repo, os.path.basename(main_repo), depth=1)
            return "repo", main_repo, sorted(primary_set), broken_worktrees, ephemeral_dirs, []
        mode = "root"
        target = cwd

    if not os.path.isdir(target):
        return mode, target, [], [], [], [f"Root path does not exist: {target}"]

    try:
        entries = list(os.scandir(target))
    except OSError as exc:
        return mode, target, [], [], [], [f"Cannot scan root {target}: {exc}"]

    for entry in entries:
        if entry.name in SKIP_NESTED_NAMES:
            continue
        if entry.name.startswith(".") and not entry.name.startswith(".scratch-ds"):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        inspect_entry(entry.path, entry.name, depth=1)

    if include_external_roots and external_roots:
        for raw_ext in external_roots:
            ext = os.path.expanduser(raw_ext)
            if not os.path.isdir(ext):
                continue
            try:
                for sub in os.scandir(ext):
                    if sub.name.startswith("."):
                        continue
                    if sub.is_dir(follow_symlinks=False):
                        inspect_entry(sub.path, sub.name, depth=max_depth - 1)
            except OSError:
                continue

    return mode, target, sorted(primary_set), broken_worktrees, ephemeral_dirs, errors


def detect_base_branch(
    repo: str,
    local_branches: Set[str],
    prot_branch_res: List[re.Pattern[str]],
) -> Optional[str]:
    """
    Strictly detect base branch without ever falling back to arbitrary feature branches.
    May return a remote tracking ref like 'refs/remotes/origin/main' or None if unknown.
    """
    proc = run_git(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if proc.returncode == 0:
        cand = proc.stdout.strip()
        short = cand[len("origin/") :] if cand.startswith("origin/") else cand
        if short in local_branches:
            return short
        remote_ref = f"refs/remotes/{cand}" if cand.startswith("origin/") else f"refs/remotes/origin/{cand}"
        if run_git(repo, ["rev-parse", "--verify", f"{remote_ref}^{{commit}}"]).returncode == 0:
            return remote_ref

    for cand in ("main", "master", "develop", "dev", "prod/main", "prod/cp-main", "prod/admin-main"):
        if cand in local_branches:
            return cand

    for r_cand in ("refs/remotes/origin/main", "refs/remotes/origin/master", "refs/remotes/origin/develop"):
        if run_git(repo, ["rev-parse", "--verify", f"{r_cand}^{{commit}}"]).returncode == 0:
            return r_cand

    head = run_git(repo, ["symbolic-ref", "--short", "HEAD"])
    if head.returncode == 0:
        h_name = head.stdout.strip()
        if h_name in local_branches and any(rx.search(h_name) for rx in prot_branch_res):
            return h_name

    return None


def parse_worktrees_porcelain(repo: str) -> List[Dict[str, Any]]:
    proc = run_git(repo, ["worktree", "list", "--porcelain"])
    if proc.returncode != 0:
        return []
    items: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            if cur:
                items.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            if cur:
                items.append(cur)
            cur = {
                "path": realpath_str(line[len("worktree ") :].strip()),
                "head": "",
                "branch": None,
                "detached": False,
                "locked": False,
                "lock_reason": "",
                "prunable": False,
                "bare": False,
            }
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/") :]
            cur["branch"] = ref
        elif line == "detached":
            cur["detached"] = True
        elif line == "bare":
            cur["bare"] = True
        elif line == "locked":
            cur["locked"] = True
        elif line.startswith("locked "):
            cur["locked"] = True
            cur["lock_reason"] = line[len("locked ") :].strip()
        elif line == "prunable" or line.startswith("prunable "):
            cur["prunable"] = True
    if cur:
        items.append(cur)
    return items


def build_isolated_object_env(repo: str, tmp_obj_dir: str) -> Dict[str, str]:
    """Construct GIT_OBJECT_DIRECTORY + GIT_ALTERNATE_OBJECT_DIRECTORIES so probes never write into repo."""
    obj_proc = run_git(repo, ["rev-parse", "--git-path", "objects"])
    raw_obj = obj_proc.stdout.strip() if obj_proc.returncode == 0 else ".git/objects"
    obj_dir = raw_obj if os.path.isabs(raw_obj) else os.path.join(repo, raw_obj)
    obj_dir = realpath_str(obj_dir)
    alts: List[str] = [obj_dir]
    alt_file = os.path.join(obj_dir, "info", "alternates")
    if os.path.isfile(alt_file):
        try:
            with open(alt_file, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    raw = line.strip()
                    if not raw or raw.startswith("#"):
                        continue
                    ap = raw if os.path.isabs(raw) else os.path.join(obj_dir, raw)
                    if os.path.isdir(ap):
                        alts.append(realpath_str(ap))
        except OSError:
            pass
    return {
        "GIT_OBJECT_DIRECTORY": tmp_obj_dir,
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.pathsep.join(alts),
    }


def check_merge_status(
    repo: str,
    branch: str,
    base: Optional[str],
    merged_set: Set[str],
    fast: bool,
    squash_budget_left: int,
    cutover_recommended: bool = False,
    force_deep: bool = False,
) -> Tuple[str, int]:
    """
    3-step merge waterfall (read-only zero-write squash probe):
      1. Ancestor merged (--merged against base or extra-bases)
      2. Rebase / cherry-pick merged (git cherry)
      3. Virtual squash-merge probe using isolated temp GIT_OBJECT_DIRECTORY
    """
    if base is None:
        return "unknown_base", 0
    if branch == base:
        return "base", 0
    if branch in merged_set:
        return "merged", 0
    if fast:
        return "skipped_fast", 0
    if cutover_recommended and not force_deep:
        return "skipped_cutover_recommended", 0
    if squash_budget_left == 0:
        return "skipped_cap", 0

    ch = run_git(repo, ["cherry", base, branch])
    if ch.returncode == 0:
        lines = [ln.strip() for ln in ch.stdout.splitlines() if ln.strip()]
        if lines and all(ln.startswith("- ") for ln in lines):
            return "cherry_merged", 1
        if not lines:
            mb = run_git(repo, ["merge-base", base, branch])
            if mb.returncode == 0 and mb.stdout.strip():
                diff_chk = run_git(repo, ["diff", "--quiet", mb.stdout.strip(), branch])
                if diff_chk.returncode == 0:
                    return "merged", 1

    mb_proc = run_git(repo, ["merge-base", base, branch])
    if mb_proc.returncode != 0 or not mb_proc.stdout.strip():
        return "unmerged", 1
    mb = mb_proc.stdout.strip()
    tree_proc = run_git(repo, ["rev-parse", f"{branch}^{{tree}}"])
    if tree_proc.returncode != 0 or not tree_proc.stdout.strip():
        return "unmerged", 1
    tree = tree_proc.stdout.strip()

    diff_mb = run_git(repo, ["diff", "--quiet", mb, branch])
    if diff_mb.returncode == 0:
        return "merged", 1

    with tempfile.TemporaryDirectory(prefix="fbk-obj-") as tmp_obj:
        os.makedirs(os.path.join(tmp_obj, "pack"), exist_ok=True)
        os.makedirs(os.path.join(tmp_obj, "info"), exist_ok=True)
        obj_env = build_isolated_object_env(repo, tmp_obj)
        ct = run_git(
            repo,
            ["commit-tree", tree, "-p", mb, "-m", "fbk-squash-probe"],
            env=obj_env,
            readonly=True,
        )
        if ct.returncode != 0 or not ct.stdout.strip():
            return "unmerged", 1
        probe_sha = ct.stdout.strip()
        ch2 = run_git(repo, ["cherry", base, probe_sha], env=obj_env, readonly=True)
        if ch2.returncode == 0:
            lines2 = [ln.strip() for ln in ch2.stdout.splitlines() if ln.strip()]
            if any(ln.startswith("- ") for ln in lines2):
                return "squash_merged", 1
    return "unmerged", 1


def resolve_wt_gitdir(wt_path: str) -> Optional[str]:
    dot_git = os.path.join(wt_path, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    if os.path.isfile(dot_git):
        gd, _ = resolve_gitdir_pointer(wt_path)
        return gd
    return None


def fast_reflog_ts(wt_path: str, fast: bool) -> int:
    """Fast O(1) reflog entry timestamp lookup from logs/HEAD; falls back to git reflog only when needed."""
    gd = resolve_wt_gitdir(wt_path)
    log_head = os.path.join(gd, "logs", "HEAD") if gd else None
    if log_head and os.path.isfile(log_head):
        try:
            fsize = os.path.getsize(log_head)
            with open(log_head, "rb") as fh:
                if fsize > 2048:
                    fh.seek(fsize - 2048)
                tail = fh.read().decode("utf-8", errors="replace")
            lines = [ln for ln in tail.splitlines() if ln.strip()]
            if lines:
                m = _REFLOG_TS_RE.search(lines[-1])
                if m:
                    return int(m.group(1))
        except OSError:
            pass
    if fast:
        return 0
    proc = run_git(wt_path, ["reflog", "-n", "1", "--format=%ct", "HEAD"])
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            return int(proc.stdout.strip().splitlines()[0])
        except ValueError:
            return 0
    return 0


def has_git_tracked_files(wt_path: str, rel_subpath: str) -> bool:
    """Return True if git tracks any file under rel_subpath (fail-closed if ls-files errors)."""
    proc = run_git(wt_path, ["ls-files", "--", rel_subpath])
    if proc.returncode != 0:
        return True
    return bool(proc.stdout.strip())


def find_slimmable_dirs(
    wt_path: str,
    cache_dir_names: List[str],
    compute_size: bool,
    top_level_only: bool = False,
    max_depth: int = 3,
) -> Tuple[List[str], int]:
    found: List[str] = []
    total_bytes = 0
    name_set = set(cache_dir_names)
    depth_limit = 1 if top_level_only else max_depth

    def walk_cache(cur_path: str, rel_prefix: str, depth: int) -> None:
        nonlocal total_bytes
        try:
            with os.scandir(cur_path) as it:
                entries = list(it)
        except OSError:
            return
        for entry in entries:
            if entry.name == ".git":
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            rel = entry.name if not rel_prefix else os.path.join(rel_prefix, entry.name)
            if entry.name in name_set:
                if not has_git_tracked_files(wt_path, rel):
                    found.append(entry.path)
                    if compute_size:
                        total_bytes += dir_size_bytes(entry.path)
                continue
            if depth < depth_limit:
                walk_cache(entry.path, rel, depth + 1)

    walk_cache(wt_path, "", 1)
    return sorted(found), total_bytes


def _probe_worktree_disk_state(
    wt_entry: Dict[str, Any],
    fast: bool,
    compute_size: bool,
    cache_dir_names: List[str],
    scan_slimmable: bool,
    delivery_guard_enabled: bool,
    known_tier3_o1: bool,
) -> Dict[str, Any]:
    w_path = wt_entry["path"]
    is_primary = bool(wt_entry.get("is_primary", False))
    exists_on_disk = os.path.isdir(w_path)
    is_prunable = bool(wt_entry.get("prunable"))
    dirty = False
    dirty_count = 0
    status_failed = False
    last_ts = 0

    gd = resolve_wt_gitdir(w_path) if exists_on_disk else None
    has_index_lock = bool(gd and os.path.exists(os.path.join(gd, "index.lock")))
    has_keep_sentinel = bool(exists_on_disk and os.path.exists(os.path.join(w_path, ".fbk-keep")))
    has_delivery_marker = bool(
        delivery_guard_enabled
        and exists_on_disk
        and (
            os.path.exists(os.path.join(w_path, ".delivery"))
            or os.path.exists(os.path.join(w_path, "artifacts", "pipeline-metrics"))
        )
    )

    if exists_on_disk:
        for p in (
            w_path,
            os.path.join(w_path, ".git"),
            os.path.join(gd, "index") if gd else "",
            os.path.join(gd, "HEAD") if gd else "",
        ):
            if p and os.path.exists(p):
                try:
                    last_ts = max(last_ts, int(os.path.getmtime(p)))
                except OSError:
                    pass
        last_ts = max(last_ts, fast_reflog_ts(w_path, fast=fast))

    # Skip git status on primary repos (always Tier 3) and on O(1)-pruned Tier 3 worktrees in --fast mode
    should_run_status = (
        exists_on_disk
        and not is_primary
        and not is_prunable
        and not (fast and (known_tier3_o1 or has_index_lock or has_keep_sentinel or has_delivery_marker))
    )
    if should_run_status:
        st_proc = run_git(w_path, ["status", "--porcelain=v1", "--untracked-files=normal"])
        if st_proc.returncode != 0:
            status_failed = True
        else:
            d_lines = [ln for ln in st_proc.stdout.splitlines() if ln.strip()]
            dirty_count = len(d_lines)
            dirty = dirty_count > 0

    total_bytes: Optional[int] = None
    slimmable_dirs: List[str] = []
    slimmable_bytes = 0
    if exists_on_disk and not is_primary:
        if compute_size:
            total_bytes = dir_size_bytes(w_path)
        if scan_slimmable or not (fast and not compute_size):
            slimmable_dirs, slimmable_bytes = find_slimmable_dirs(
                w_path,
                cache_dir_names,
                compute_size,
                top_level_only=(fast and not scan_slimmable),
            )

    return {
        "exists_on_disk": exists_on_disk,
        "dirty": dirty,
        "dirty_count": dirty_count,
        "status_failed": status_failed,
        "last_ts": last_ts,
        "total_bytes": total_bytes,
        "slimmable_dirs": slimmable_dirs,
        "slimmable_bytes": slimmable_bytes,
        "has_index_lock": has_index_lock,
        "has_keep_sentinel": has_keep_sentinel,
        "has_delivery_marker": has_delivery_marker,
    }


def _scan_single_repo(
    repo: str,
    cfg: Dict[str, Any],
    active_cwds: Dict[str, Set[str]],
    current_cwd: str,
    compiled_harness: List[Tuple[str, List[re.Pattern[str]]]],
    fast: bool,
    compute_size: bool,
    scan_slimmable: bool,
    max_squash: int,
    cli_extra_bases: List[str],
    force_deep: bool,
    ignore_delivery_marker: bool,
    now_ts: int,
    wt_executor: concurrent.futures.ThreadPoolExecutor,
) -> Tuple[Optional[RepoSummary], List[WorktreeRecord], List[BranchRecord], List[str]]:
    grace_secs = int(float(cfg.get("grace_period_hours", 24)) * 3600)
    slim_idle_secs = int(float(cfg.get("slim_idle_days", 7)) * 86400)
    cache_dir_names = list(cfg.get("slim_cache_dirs", DEFAULT_CONFIG["slim_cache_dirs"]))
    prot_branch_res = [re.compile(p) for p in cfg.get("protected_branch_patterns", [])]
    prot_path_res = [re.compile(p) for p in cfg.get("protected_path_patterns", [])]
    cutover_wt_th = int(cfg.get("cutover_worktree_threshold", 50))
    cutover_rr_th = int(cfg.get("cutover_remote_refs_threshold", 10000))
    delivery_guard = bool(cfg.get("delivery_guard_enabled", True)) and not ignore_delivery_marker
    errors: List[str] = []

    broken_alts = inspect_repo_alternates(repo)
    origin_url, origin_kind, remote_count, non_canonical_remotes, remote_ref_count = inspect_repo_remotes(repo)
    is_pseudo_primary = origin_kind in ("tmp_or_artifact", "broken_local", "local_clone")

    wt_entries = parse_worktrees_porcelain(repo)
    linked_count = sum(1 for w in wt_entries if w["path"] != repo)

    ref_proc = run_git(
        repo,
        [
            "for-each-ref",
            "--format=%(refname:short)\t%(objectname)\t%(committerdate:unix)\t%(upstream:short)\t%(upstream:track)",
            "refs/heads/",
        ],
    )
    if ref_proc.returncode != 0:
        err_msg = ref_proc.stderr.strip()
        if "unable to normalize alternate object path" in err_msg or broken_alts:
            errors.append(
                f"Broken git alternate in {repo} (linked_wt={linked_count}, missing={broken_alts}): {err_msg}"
            )
        else:
            errors.append(f"Failed for-each-ref on {repo}: {err_msg}")
        summary = RepoSummary(
            repo=repo,
            base_branch=None,
            local_branch_count=0,
            linked_worktree_count=linked_count,
            dependent_worktree_count=0,
            origin_url=origin_url,
            origin_kind=origin_kind,
            is_pseudo_primary=is_pseudo_primary,
            remote_count=remote_count,
            non_canonical_remotes=non_canonical_remotes,
            remote_ref_count=remote_ref_count,
            cutover_recommended=(linked_count > cutover_wt_th or remote_ref_count > cutover_rr_th),
            cutover_reasons=[
                r
                for r in [
                    f"worktrees>{cutover_wt_th}" if linked_count > cutover_wt_th else "",
                    f"remote_refs>{cutover_rr_th}" if remote_ref_count > cutover_rr_th else "",
                    "broken_alternates" if broken_alts else "",
                ]
                if r
            ],
            broken_alternates=broken_alts,
        )
        return summary, [], [], errors

    raw_branches: List[Dict[str, Any]] = []
    local_branch_names: Set[str] = set()
    for line in ref_proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        b_name = parts[0]
        sha = parts[1]
        try:
            cts = int(parts[2])
        except ValueError:
            cts = 0
        upstream = parts[3] if len(parts) > 3 else ""
        utrack = parts[4] if len(parts) > 4 else ""
        local_branch_names.add(b_name)
        raw_branches.append(
            {
                "name": b_name,
                "sha": sha,
                "committer_ts": cts,
                "upstream": upstream,
                "upstream_track": utrack,
            }
        )

    base_branch = detect_base_branch(repo, local_branch_names, prot_branch_res)
    base_sha = ""
    if base_branch:
        bp = run_git(repo, ["rev-parse", "--verify", f"{base_branch}^{{commit}}"])
        if bp.returncode == 0:
            base_sha = bp.stdout.strip()

    # Extra-base anchors (CLI + config per repo/basename + protected_shas)
    extra_base_cfg = cfg.get("extra_bases", {})
    extra_base_refs: List[str] = list(cli_extra_bases)
    if isinstance(extra_base_cfg, dict):
        extra_base_refs.extend(extra_base_cfg.get(repo, []) or [])
        extra_base_refs.extend(extra_base_cfg.get(os.path.basename(repo), []) or [])

    protected_shas: Set[str] = set(cfg.get("protected_shas", []) or [])
    protected_extra_names: Set[str] = set()
    anchor_refs: List[str] = ([base_branch] if base_branch else [])
    merged_set: Set[str] = set()

    if base_branch:
        m_proc = run_git(repo, ["branch", "--merged", base_branch, "--format=%(refname:short)"])
        if m_proc.returncode == 0:
            merged_set.update(ln.strip() for ln in m_proc.stdout.splitlines() if ln.strip())

    for eb in extra_base_refs:
        vp = run_git(repo, ["rev-parse", "--verify", f"{eb}^{{commit}}"])
        if vp.returncode == 0 and vp.stdout.strip():
            protected_shas.add(vp.stdout.strip())
            anchor_refs.append(eb)
            if eb in local_branch_names:
                protected_extra_names.add(eb)
            em = run_git(repo, ["branch", "--merged", eb, "--format=%(refname:short)"])
            if em.returncode == 0:
                merged_set.update(ln.strip() for ln in em.stdout.splitlines() if ln.strip())

    # Unpushed-to-canonical-origin check (strictly against refs/remotes/origin/*)
    unpushed_sha_set: Optional[Set[str]] = None
    if origin_kind == "canonical_remote":
        has_origin_refs = run_git(repo, ["for-each-ref", "--count=1", "--format=.", "refs/remotes/origin/"])
        if has_origin_refs.returncode == 0 and has_origin_refs.stdout.strip():
            up_proc = run_git(repo, ["rev-list", "--branches", "--not", "--remotes=origin"])
            if up_proc.returncode == 0:
                unpushed_sha_set = {ln.strip() for ln in up_proc.stdout.splitlines() if ln.strip()}

    cutover_reasons: List[str] = []
    if linked_count > cutover_wt_th:
        cutover_reasons.append(f"worktrees>{cutover_wt_th}")
    if remote_ref_count > cutover_rr_th:
        cutover_reasons.append(f"remote_refs>{cutover_rr_th}")
    cutover_recommended = bool(cutover_reasons)

    stash_proc = run_git(repo, ["stash", "list"])
    stash_lines = stash_proc.stdout.splitlines() if stash_proc.returncode == 0 else []

    branch_to_wt: Dict[str, str] = {}
    for w in wt_entries:
        w["is_primary"] = w["path"] == repo
        if w.get("branch"):
            branch_to_wt[w["branch"]] = w["path"]

    raw_branches.sort(key=lambda x: x["committer_ts"])
    budget = -1 if max_squash == 0 else max_squash
    branch_map: Dict[str, BranchRecord] = {}

    for rb in raw_branches:
        b_name = rb["name"]
        sha = rb["sha"]
        cts = rb["committer_ts"]
        is_prot = (
            (base_branch is not None and b_name == base_branch)
            or b_name in protected_extra_names
            or sha in protected_shas
            or any(r.search(b_name) for r in prot_branch_res)
        )
        attached_wt = branch_to_wt.get(b_name)
        within_grace = (now_ts - cts) < grace_secs if cts > 0 else False

        if base_branch is not None and b_name == base_branch:
            merge_status = "base"
        elif is_prot or within_grace:
            merge_status = "merged" if b_name in merged_set else "unmerged"
        else:
            merge_status, consumed = check_merge_status(
                repo,
                b_name,
                base_branch,
                merged_set,
                fast=fast,
                squash_budget_left=budget,
                cutover_recommended=cutover_recommended,
                force_deep=force_deep,
            )
            if budget > 0 and consumed > 0:
                budget -= consumed

        reasons: List[str] = []
        if is_prot:
            tier = 3
            if base_branch is not None and b_name == base_branch:
                reasons.append("base_branch")
            elif b_name in protected_extra_names or sha in protected_shas:
                reasons.append("extra_base_anchor")
            else:
                reasons.append("protected_branch_pattern")
        elif within_grace:
            tier = 3
            reasons.append("within_grace_period")
        elif merge_status in ("merged", "cherry_merged", "squash_merged"):
            tier = 0
            reasons.append(merge_status)
            if base_sha and sha == base_sha:
                reasons.append("zero_commit_branch_at_base")
        else:
            tier = 2
            if merge_status == "unknown_base":
                reasons.append("unknown_base_branch")
            elif "[gone]" in rb["upstream_track"]:
                reasons.append("upstream_gone_unmerged")
            elif merge_status == "skipped_fast":
                reasons.append("fast_mode_unverified")
            elif merge_status == "skipped_cutover_recommended":
                reasons.append("skipped_cutover_recommended")
            elif merge_status == "skipped_cap":
                reasons.append("squash_check_cap_reached")
            else:
                reasons.append("unmerged_commits")

        unpushed_flag = (sha in unpushed_sha_set) if unpushed_sha_set is not None else None
        rec = BranchRecord(
            repo=repo,
            name=b_name,
            sha=sha,
            base_branch=base_branch,
            committer_ts=cts,
            upstream=rb["upstream"],
            upstream_track=rb["upstream_track"],
            merge_status=merge_status,
            tier=tier,
            reasons=reasons,
            attached_worktree=attached_wt,
            is_protected=is_prot,
            within_grace=within_grace,
            harness=detect_harness(attached_wt, b_name, compiled_harness),
            unpushed_to_origin=unpushed_flag,
        )
        branch_map[b_name] = rec

    # Pre-check O(1) Tier 3 guards before submitting disk probes
    wt_o1_flags: List[Tuple[bool, Set[str], bool]] = []
    for w in wt_entries:
        w_path = w["path"]
        b_name = w.get("branch")
        proc_cmds = match_active_procs(w_path, active_cwds)
        if is_subpath(current_cwd, w_path):
            proc_cmds.add("current_cwd")
        path_prot = any(r.search(w_path) for r in prot_path_res)
        br_prot = bool(b_name and b_name in branch_map and branch_map[b_name].is_protected)
        known_t3 = bool(w["is_primary"] or w.get("locked") or proc_cmds or path_prot or br_prot)
        wt_o1_flags.append((known_t3, proc_cmds, path_prot))

    wt_futures = [
        wt_executor.submit(
            _probe_worktree_disk_state,
            w,
            fast,
            compute_size,
            cache_dir_names,
            scan_slimmable,
            delivery_guard,
            o1[0],
        )
        for w, o1 in zip(wt_entries, wt_o1_flags)
    ]
    disk_states = [fut.result() for fut in wt_futures]

    valid_linked_on_disk = sum(
        1 for w, ds in zip(wt_entries, disk_states) if not w["is_primary"] and ds["exists_on_disk"]
    )

    wt_records: List[WorktreeRecord] = []
    for w, ds, (_, proc_cmds, path_prot) in zip(wt_entries, disk_states, wt_o1_flags):
        w_path = w["path"]
        is_primary = w["is_primary"]
        exists_on_disk = ds["exists_on_disk"]
        b_name = w.get("branch")
        dirty = ds["dirty"]
        dirty_count = ds["dirty_count"]
        status_failed = ds["status_failed"]
        last_ts = ds["last_ts"]
        if b_name and b_name in branch_map:
            last_ts = max(last_ts, branch_map[b_name].committer_ts)

        within_grace = (now_ts - last_ts) < grace_secs if last_ts > 0 else False
        active_proc = bool(proc_cmds)
        has_stash = bool(b_name and any(f"On {b_name}:" in sl for sl in stash_lines))

        reasons: List[str] = []
        tier = 3
        if is_primary:
            tier = 3
            reasons.append("pseudo_primary_clone" if is_pseudo_primary else "primary_repo_protected")
            if valid_linked_on_disk > 0:
                reasons.append(f"has_{valid_linked_on_disk}_dependent_worktrees")
        elif not exists_on_disk or w.get("prunable"):
            tier = 0
            reasons.append("missing_worktree_dir_prunable")
        elif ds["has_index_lock"]:
            tier = 3
            reasons.append("git_index_lock_present")
        elif ds["has_keep_sentinel"]:
            tier = 3
            reasons.append("fbk_keep_sentinel")
        elif ds["has_delivery_marker"]:
            tier = 3
            reasons.append("active_delivery_marker")
        elif status_failed:
            tier = 3
            reasons.append("git_status_failed")
        elif w.get("locked"):
            tier = 3
            reasons.append("worktree_locked")
        elif active_proc:
            tier = 3
            reasons.append(f"active_process_cwd({','.join(sorted(proc_cmds))})")
        elif path_prot:
            tier = 3
            reasons.append("protected_path_pattern")
        elif within_grace:
            tier = 3
            reasons.append("within_grace_period")
        elif b_name and b_name in branch_map and branch_map[b_name].is_protected:
            tier = 3
            reasons.extend(branch_map[b_name].reasons)
        elif has_stash:
            tier = 2
            reasons.append("branch_has_stash")
        elif w.get("detached"):
            if dirty:
                tier = 2
                reasons.append(f"dirty_worktree({dirty_count})")
                reasons.append("detached_head")
            else:
                head_sha = w.get("head", "")
                anc_merged = False
                if head_sha and anchor_refs:
                    for anc in anchor_refs:
                        chk = run_git(repo, ["merge-base", "--is-ancestor", head_sha, anc])
                        if chk.returncode == 0:
                            anc_merged = True
                            break
                if anc_merged:
                    tier = 0
                    reasons.append("clean_detached_worktree_merged")
                else:
                    tier = 2
                    reasons.append("unknown_base_branch" if not anchor_refs else "detached_head_unmerged")
        elif not dirty and b_name and b_name in branch_map and branch_map[b_name].tier == 0:
            tier = 0
            reasons.append(f"clean_worktree_{branch_map[b_name].merge_status}")
        else:
            tier = 2
            if dirty:
                reasons.append(f"dirty_worktree({dirty_count})")
            if b_name and b_name in branch_map and branch_map[b_name].tier != 0:
                reasons.extend(branch_map[b_name].reasons)

        if b_name and b_name in branch_map:
            b_rec = branch_map[b_name]
            if tier == 3 and b_rec.tier != 3:
                b_rec.tier = 3
                b_rec.reasons.append("checked_out_in_protected_or_active_worktree")
            elif tier == 2 and b_rec.tier == 0:
                b_rec.tier = 2
                b_rec.reasons.append("attached_to_dirty_or_stashed_worktree")

        idle_ok = (now_ts - last_ts) >= slim_idle_secs if last_ts > 0 else False
        slimmable_dirs = ds["slimmable_dirs"] if (tier != 3 and idle_ok and not is_primary) else []
        slimmable_bytes = ds["slimmable_bytes"] if slimmable_dirs else 0

        wt_records.append(
            WorktreeRecord(
                repo=repo,
                path=w_path,
                kind="primary" if is_primary else "linked",
                head_sha=w.get("head", ""),
                branch=b_name,
                detached=bool(w.get("detached")),
                locked=bool(w.get("locked")),
                lock_reason=w.get("lock_reason", ""),
                prunable=bool(w.get("prunable")),
                dirty=dirty,
                dirty_count=dirty_count,
                has_stash=has_stash,
                last_activity_ts=last_ts,
                within_grace=within_grace,
                active_proc=active_proc,
                tier=tier,
                reasons=sorted(set(reasons)),
                total_bytes=ds["total_bytes"],
                slimmable_dirs=slimmable_dirs,
                slimmable_bytes=slimmable_bytes,
                dependent_worktree_count=valid_linked_on_disk if is_primary else 0,
                local_branch_count=len(raw_branches) if is_primary else 0,
                harness="primary" if is_primary else detect_harness(w_path, b_name, compiled_harness),
                origin_kind=origin_kind if is_primary else "",
                is_pseudo_primary=is_pseudo_primary if is_primary else False,
                status_failed=status_failed,
            )
        )

    unpushed_count = sum(1 for b in branch_map.values() if b.unpushed_to_origin is True)
    summary = RepoSummary(
        repo=repo,
        base_branch=base_branch,
        local_branch_count=len(raw_branches),
        linked_worktree_count=linked_count,
        dependent_worktree_count=valid_linked_on_disk,
        origin_url=origin_url,
        origin_kind=origin_kind,
        is_pseudo_primary=is_pseudo_primary,
        remote_count=remote_count,
        non_canonical_remotes=non_canonical_remotes,
        remote_ref_count=remote_ref_count,
        unpushed_branch_count=unpushed_count,
        cutover_recommended=cutover_recommended,
        cutover_reasons=cutover_reasons,
        broken_alternates=broken_alts,
    )
    return summary, wt_records, list(branch_map.values()), errors


def scan_workspace(
    cfg: Dict[str, Any],
    repo_arg: Optional[str] = None,
    root_arg: Optional[str] = None,
    fast: bool = False,
    compute_size: bool = True,
    scan_slimmable: bool = False,
    max_squash_check_override: Optional[int] = None,
    ignore_self: bool = True,
    include_external_roots: bool = False,
    cli_extra_bases: Optional[List[str]] = None,
    force_deep: bool = False,
    ignore_delivery_marker: bool = False,
) -> ScanResult:
    now_ts = int(time.time())
    max_squash = (
        max_squash_check_override
        if max_squash_check_override is not None
        else int(cfg.get("max_squash_check_per_repo", 100))
    )
    workers = max(1, int(cfg.get("scan_workers", 6)))
    max_depth = int(cfg.get("discover_max_depth", 3))
    grace_secs = int(float(cfg.get("grace_period_hours", 24)) * 3600)
    compiled_harness = compile_harness_patterns(cfg)

    mode, target, primary_repos, broken_wts, ephemeral_dirs, errors = discover_targets(
        repo_arg,
        root_arg,
        nested_dirs=cfg.get("nested_worktree_dirs", []),
        external_roots=cfg.get("additional_worktree_roots", []),
        include_external_roots=include_external_roots,
        ephemeral_patterns=cfg.get("ephemeral_dir_patterns", []),
        max_depth=max_depth,
    )
    active_cwds, proc_check_status = get_active_process_cwds(ignore_self=ignore_self)
    current_cwd = realpath_str(os.getcwd())

    result = ScanResult(
        scanned_at=now_utc_iso(),
        mode=mode,
        target=target,
        active_proc_check=proc_check_status,
        errors=errors,
    )

    for b_path, missing_gd in broken_wts:
        b_size = dir_size_bytes(b_path) if compute_size else None
        proc_cmds = match_active_procs(b_path, active_cwds)
        if is_subpath(current_cwd, b_path):
            proc_cmds.add("current_cwd")
        is_act = bool(proc_cmds)
        has_keep = os.path.exists(os.path.join(b_path, ".fbk-keep"))
        tier = 3 if (is_act or has_keep) else 0
        reasons = []
        if is_act:
            reasons.append(f"active_process_cwd({','.join(sorted(proc_cmds))})")
        if has_keep:
            reasons.append("fbk_keep_sentinel")
        if not reasons:
            reasons.append("broken_gitdir_pointer")
        result.worktrees.append(
            WorktreeRecord(
                repo="",
                path=b_path,
                kind="broken_worktree",
                head_sha="",
                branch=None,
                detached=False,
                locked=False,
                lock_reason="",
                prunable=True,
                dirty=False,
                dirty_count=0,
                has_stash=False,
                last_activity_ts=int(os.path.getmtime(b_path)) if os.path.exists(b_path) else now_ts,
                within_grace=False,
                active_proc=is_act,
                tier=tier,
                reasons=reasons,
                total_bytes=b_size,
                missing_gitdir=missing_gd,
                harness=detect_harness(b_path, None, compiled_harness),
            )
        )

    for ep_path, pid in ephemeral_dirs:
        ep_size = dir_size_bytes(ep_path) if compute_size else None
        mtime = int(os.path.getmtime(ep_path)) if os.path.exists(ep_path) else now_ts
        within_grace = (now_ts - mtime) < grace_secs
        pid_alive = is_pid_alive(pid)
        proc_cmds = match_active_procs(ep_path, active_cwds)
        if is_subpath(current_cwd, ep_path):
            proc_cmds.add("current_cwd")
        is_act = bool(proc_cmds)
        has_keep = os.path.exists(os.path.join(ep_path, ".fbk-keep"))
        reasons = []
        if pid_alive:
            reasons.append(f"ephemeral_gate_pid_alive({pid})")
        if is_act:
            reasons.append(f"active_process_cwd({','.join(sorted(proc_cmds))})")
        if has_keep:
            reasons.append("fbk_keep_sentinel")
        if within_grace:
            reasons.append("within_grace_period")
        if reasons:
            tier = 3
        else:
            tier = 2
            reasons.append(f"ephemeral_gate_orphan_pid_dead({pid})" if pid else "ephemeral_gate_orphan")

        result.worktrees.append(
            WorktreeRecord(
                repo="",
                path=ep_path,
                kind="ephemeral_gate_dir",
                head_sha="",
                branch=None,
                detached=False,
                locked=False,
                lock_reason="",
                prunable=False,
                dirty=False,
                dirty_count=0,
                has_stash=False,
                last_activity_ts=mtime,
                within_grace=within_grace,
                active_proc=is_act,
                tier=tier,
                reasons=reasons,
                total_bytes=ep_size,
                harness="gate-verifier",
            )
        )

    if primary_repos:
        with (
            concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(primary_repos))) as repo_pool,
            concurrent.futures.ThreadPoolExecutor(max_workers=workers) as wt_pool,
        ):
            repo_futures = [
                repo_pool.submit(
                    _scan_single_repo,
                    repo,
                    cfg,
                    active_cwds,
                    current_cwd,
                    compiled_harness,
                    fast,
                    compute_size,
                    scan_slimmable,
                    max_squash,
                    cli_extra_bases or [],
                    force_deep,
                    ignore_delivery_marker,
                    now_ts,
                    wt_pool,
                )
                for repo in primary_repos
            ]
            for fut in repo_futures:
                summary, wt_recs, br_recs, r_errs = fut.result()
                if summary is not None:
                    result.repos.append(summary)
                result.worktrees.extend(wt_recs)
                result.branches.extend(br_recs)
                result.errors.extend(r_errs)

    return result


class VaultManager:
    """Manages hardened recovery snapshots (bundles via temp ref + dirty diffs + untracked tar) and append-only receipts."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.vault_root = state_dir / "vault"
        self.receipts_file = state_dir / "receipts.jsonl"

    def new_receipt_id(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"fbk-{ts}-{uuid.uuid4().hex[:6]}"

    def _load_or_init_manifest(self, vdir: Path, receipt_id: str) -> Tuple[Path, Dict[str, Any]]:
        manifest_path = vdir / "manifest.json"
        manifest: Dict[str, Any] = {
            "receipt_id": receipt_id,
            "created_at": now_utc_iso(),
            "entries": [],
            "dirty_snapshots": [],
        }
        if manifest_path.exists():
            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            manifest.setdefault("entries", [])
            manifest.setdefault("dirty_snapshots", [])
        return manifest_path, manifest

    def archive_ref_or_sha(
        self,
        receipt_id: str,
        repo: str,
        branch_label: str,
        sha: str,
        merge_status: str,
        worktree_path: Optional[str] = None,
        ref_exists: bool = True,
    ) -> Optional[Dict[str, Any]]:
        vdir = self.vault_root / receipt_id
        vdir.mkdir(parents=True, exist_ok=True)
        rhash = short_hash(repo)
        safe_b = re.sub(r"[^A-Za-z0-9._-]+", "_", branch_label)
        bundle_name = f"{rhash}-{safe_b}-{sha[:8]}.bundle"
        bundle_path = vdir / bundle_name

        bundle_ref = f"refs/heads/{branch_label}" if ref_exists else ""
        created_ok = False
        if ref_exists:
            proc = run_git(repo, ["bundle", "create", str(bundle_path), bundle_ref], readonly=False)
            created_ok = proc.returncode == 0

        if not created_ok:
            tmp_ref = f"refs/fbk-tmp/{receipt_id}-{uuid.uuid4().hex[:6]}"
            up = run_git(repo, ["update-ref", tmp_ref, sha], readonly=False)
            if up.returncode == 0:
                try:
                    proc2 = run_git(repo, ["bundle", "create", str(bundle_path), tmp_ref], readonly=False)
                    if proc2.returncode == 0:
                        bundle_ref = tmp_ref
                        created_ok = True
                finally:
                    run_git(repo, ["update-ref", "-d", tmp_ref], readonly=False)

        if not created_ok:
            return None

        entry = {
            "repo": repo,
            "repo_hash": rhash,
            "branch": branch_label,
            "sha": sha,
            "merge_status": merge_status,
            "worktree_path": worktree_path,
            "bundle": bundle_name,
            "bundle_ref": bundle_ref,
            "archived_at": now_utc_iso(),
        }
        manifest_path, manifest = self._load_or_init_manifest(vdir, receipt_id)
        manifest["entries"].append(entry)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        return entry

    def archive_branch(
        self,
        receipt_id: str,
        repo: str,
        branch: str,
        sha: str,
        merge_status: str,
        worktree_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return self.archive_ref_or_sha(
            receipt_id, repo, branch, sha, merge_status, worktree_path=worktree_path, ref_exists=True
        )

    def archive_dirty_worktree(
        self,
        receipt_id: str,
        repo: str,
        wt_path: str,
        tag: str,
    ) -> Optional[Dict[str, Any]]:
        vdir = self.vault_root / receipt_id
        vdir.mkdir(parents=True, exist_ok=True)
        rhash = short_hash(f"{repo}:{wt_path}")
        safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", tag)

        diff1 = run_git(wt_path, ["diff"]).stdout
        diff2 = run_git(wt_path, ["diff", "--cached"]).stdout
        patch_name: Optional[str] = None
        content = ""
        if diff1:
            content += "# --- unstaged diff ---\n" + diff1 + "\n"
        if diff2:
            content += "# --- staged diff ---\n" + diff2 + "\n"
        if content:
            patch_name = f"{rhash}-{safe_tag}.patch"
            with open(vdir / patch_name, "w", encoding="utf-8") as fh:
                fh.write(content)

        untracked_tar: Optional[str] = None
        untracked_count = 0
        ut_proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", wt_path, "ls-files", "--others", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if ut_proc.returncode == 0 and ut_proc.stdout:
            rel_files = [item.decode("utf-8", errors="replace") for item in ut_proc.stdout.split(b"\x00") if item]
            if rel_files:
                untracked_tar = f"{rhash}-{safe_tag}-untracked.tar.gz"
                tar_path = vdir / untracked_tar
                wt_real = realpath_str(wt_path)
                with tarfile.open(tar_path, "w:gz") as tf:
                    for rel in rel_files:
                        fp = os.path.join(wt_path, rel)
                        if os.path.lexists(fp) and is_subpath(fp, wt_real):
                            tf.add(fp, arcname=rel, recursive=False)
                            untracked_count += 1

        if not patch_name and not untracked_tar:
            return None

        snap = {
            "type": "dirty_snapshot",
            "repo": repo,
            "worktree_path": wt_path,
            "patch": patch_name,
            "untracked_tar": untracked_tar,
            "untracked_count": untracked_count,
            "archived_at": now_utc_iso(),
        }
        manifest_path, manifest = self._load_or_init_manifest(vdir, receipt_id)
        manifest["dirty_snapshots"].append(snap)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        return snap

    def append_receipt(self, receipt: Dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.receipts_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, ensure_ascii=False) + "\n")


def remove_worktree_safe(wt: WorktreeRecord, apply: bool, allow_dirty: bool) -> Tuple[bool, str, int]:
    """Remove a linked, broken, or reviewed ephemeral worktree safely."""
    freed = wt.total_bytes if wt.total_bytes is not None else (dir_size_bytes(wt.path) if os.path.isdir(wt.path) else 0)
    if wt.dirty and not allow_dirty:
        return False, "refused_dirty_worktree_without_allow_dirty", 0
    if not apply:
        return True, "dry_run", freed

    if wt.kind in ("broken_worktree", "ephemeral_gate_dir"):
        if os.path.exists(wt.path):
            make_tree_writable(wt.path)
            shutil.rmtree(wt.path)
        return True, f"removed_{wt.kind}", freed

    if not os.path.isdir(wt.path):
        run_git(wt.repo, ["worktree", "prune"], readonly=False)
        return True, "pruned_missing_worktree", 0

    make_tree_writable(wt.path)
    rm_proc = run_git(wt.repo, ["worktree", "remove", "--force", wt.path], readonly=False)
    if rm_proc.returncode != 0:
        shutil.rmtree(wt.path)
        run_git(wt.repo, ["worktree", "prune"], readonly=False)
        return True, "removed_worktree_fallback_prune", freed
    return True, "removed_worktree", freed


def parse_tiers(tier_str: str) -> Set[int]:
    tiers: Set[int] = set()
    for part in tier_str.split(","):
        part = part.strip()
        if not part:
            continue
        t = int(part)
        if t not in (0, 1, 2, 3):
            raise ValueError(f"Invalid tier: {t}")
        tiers.add(t)
    return tiers


def filter_and_sort_scan(
    scan: ScanResult,
    harness_filter: Optional[str] = None,
    older_than_days: Optional[float] = None,
    sort_by: Optional[str] = None,
) -> ScanResult:
    now_ts = int(time.time())
    cutoff_ts = now_ts - int(older_than_days * 86400) if older_than_days is not None else None
    h_set = {h.strip().lower() for h in harness_filter.split(",") if h.strip()} if harness_filter else None

    if h_set or cutoff_ts is not None:
        new_wts: List[WorktreeRecord] = []
        for w in scan.worktrees:
            if w.kind == "primary":
                new_wts.append(w)
                continue
            if h_set and w.harness.lower() not in h_set:
                continue
            if cutoff_ts is not None and w.last_activity_ts > cutoff_ts:
                continue
            new_wts.append(w)
        scan.worktrees = new_wts

        new_brs: List[BranchRecord] = []
        for b in scan.branches:
            if h_set and b.harness.lower() not in h_set:
                continue
            if cutoff_ts is not None and b.committer_ts > cutoff_ts:
                continue
            new_brs.append(b)
        scan.branches = new_brs

    if sort_by == "size":
        scan.worktrees.sort(key=lambda w: w.total_bytes or 0, reverse=True)
    elif sort_by == "age":
        scan.worktrees.sort(key=lambda w: w.last_activity_ts)
        scan.branches.sort(key=lambda b: b.committer_ts)
    elif sort_by == "tier":
        scan.worktrees.sort(key=lambda w: (w.tier, -(w.total_bytes or 0)))
        scan.branches.sort(key=lambda b: b.tier)
    elif sort_by == "repo":
        scan.worktrees.sort(key=lambda w: (w.repo, w.path))
        scan.branches.sort(key=lambda b: (b.repo, b.name))
    return scan


def build_plan(scan: ScanResult, target_tiers: Set[int], allow_review: bool) -> Dict[str, Any]:
    actions: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    est_bytes = 0

    for wt in scan.worktrees:
        if wt.kind == "primary":
            continue
        if wt.tier in target_tiers:
            if wt.tier == 3:
                skipped.append({"kind": "worktree", "path": wt.path, "tier": 3, "reasons": wt.reasons})
                continue
            if wt.tier == 2 and not allow_review:
                skipped.append({"kind": "worktree", "path": wt.path, "tier": 2, "reasons": ["requires_allow_review"]})
                continue
            sz = wt.total_bytes if wt.total_bytes is not None else 0
            est_bytes += sz
            actions.append(
                {
                    "action": "remove_worktree",
                    "kind": wt.kind,
                    "harness": wt.harness,
                    "repo": wt.repo,
                    "path": wt.path,
                    "branch": wt.branch,
                    "detached": wt.detached,
                    "head_sha": wt.head_sha,
                    "tier": wt.tier,
                    "dirty": wt.dirty,
                    "reasons": wt.reasons,
                    "estimated_bytes": sz,
                }
            )
        elif 1 in target_tiers and wt.slimmable_dirs and wt.tier != 3:
            est_bytes += wt.slimmable_bytes
            actions.append(
                {
                    "action": "slim_worktree",
                    "harness": wt.harness,
                    "repo": wt.repo,
                    "path": wt.path,
                    "tier": 1,
                    "slimmable_dirs": wt.slimmable_dirs,
                    "estimated_bytes": wt.slimmable_bytes,
                }
            )

    wt_removed_paths = {a["path"] for a in actions if a["action"] == "remove_worktree"}

    for br in scan.branches:
        if br.tier not in target_tiers:
            continue
        if br.tier == 3:
            skipped.append({"kind": "branch", "repo": br.repo, "branch": br.name, "tier": 3, "reasons": br.reasons})
            continue
        if br.tier == 2 and not allow_review:
            skipped.append({"kind": "branch", "repo": br.repo, "branch": br.name, "tier": 2, "reasons": ["requires_allow_review"]})
            continue
        if br.attached_worktree and br.attached_worktree not in wt_removed_paths:
            skipped.append(
                {
                    "kind": "branch",
                    "repo": br.repo,
                    "branch": br.name,
                    "tier": br.tier,
                    "reasons": ["checked_out_in_retained_worktree"],
                }
            )
            continue
        actions.append(
            {
                "action": "delete_branch",
                "harness": br.harness,
                "repo": br.repo,
                "branch": br.name,
                "sha": br.sha,
                "merge_status": br.merge_status,
                "tier": br.tier,
                "reasons": br.reasons,
                "needs_vault_backup": br.merge_status != "merged",
            }
        )

    return {
        "scanned_at": scan.scanned_at,
        "target": scan.target,
        "tiers": sorted(target_tiers),
        "allow_review": allow_review,
        "actions": actions,
        "skipped": skipped,
        "estimated_freed_bytes": est_bytes,
        "estimated_freed_human": human_bytes(est_bytes),
    }


def execute_clean(
    scan: ScanResult,
    state_dir: Path,
    target_tiers: Set[int],
    allow_review: bool,
    allow_dirty: bool,
    apply: bool,
    run_gc: bool,
    allow_without_proc_check: bool = False,
) -> Dict[str, Any]:
    plan = build_plan(scan, target_tiers, allow_review)
    vault = VaultManager(state_dir)
    receipt_id = vault.new_receipt_id()

    if apply and scan.active_proc_check == "unavailable" and not allow_without_proc_check:
        return {
            "receipt_id": receipt_id,
            "command": "clean",
            "timestamp": now_utc_iso(),
            "apply": False,
            "target": scan.target,
            "tiers": sorted(target_tiers),
            "executed": [],
            "skipped_count": len(plan["skipped"]),
            "errors": [
                "Refused batch --apply because active process check (lsof) is unavailable. Pass --allow-without-proc-check to override."
            ],
            "freed_bytes": 0,
            "freed_human": "0 B",
            "vault_dir": None,
            "vault_entries": [],
        }

    wt_lookup = {w.path: w for w in scan.worktrees}
    br_lookup = {(b.repo, b.name): b for b in scan.branches}

    executed: List[Dict[str, Any]] = []
    errors: List[str] = []
    freed_bytes = 0
    touched_repos: Set[str] = set()
    removed_wt_paths: Set[str] = set()
    vault_entries: List[Dict[str, Any]] = []

    for act in plan["actions"]:
        if act["action"] == "remove_worktree":
            wt = wt_lookup[act["path"]]
            if apply and wt.dirty and allow_dirty and os.path.isdir(wt.path):
                snap = vault.archive_dirty_worktree(receipt_id, wt.repo or wt.path, wt.path, os.path.basename(wt.path))
                if snap:
                    vault_entries.append(snap)
            # Archive unmerged detached HEAD before deleting Tier 2 detached worktree
            if apply and wt.detached and wt.head_sha and wt.repo and "clean_detached_worktree_merged" not in wt.reasons:
                d_entry = vault.archive_ref_or_sha(
                    receipt_id,
                    wt.repo,
                    f"detached-{os.path.basename(wt.path)}",
                    wt.head_sha,
                    "detached_unmerged",
                    worktree_path=wt.path,
                    ref_exists=False,
                )
                if d_entry is None:
                    errors.append(f"worktree {wt.path}: failed to archive detached HEAD {wt.head_sha} to vault; aborted")
                    continue
                vault_entries.append(d_entry)

            ok, msg, freed = remove_worktree_safe(wt, apply=apply, allow_dirty=allow_dirty)
            if ok:
                freed_bytes += freed
                removed_wt_paths.add(wt.path)
                if wt.repo:
                    touched_repos.add(wt.repo)
                executed.append({**act, "status": msg, "freed_bytes": freed})
            else:
                errors.append(f"worktree {wt.path}: {msg}")
        elif act["action"] == "slim_worktree":
            wt = wt_lookup[act["path"]]
            slimmed: List[str] = []
            slim_freed = 0
            for cdir in wt.slimmable_dirs:
                if os.path.isdir(cdir):
                    sz = dir_size_bytes(cdir)
                    if apply:
                        make_tree_writable(cdir)
                        shutil.rmtree(cdir)
                    slimmed.append(cdir)
                    slim_freed += sz
            freed_bytes += slim_freed
            executed.append({**act, "status": "slimmed" if apply else "dry_run", "slimmed_dirs": slimmed, "freed_bytes": slim_freed})

    for act in plan["actions"]:
        if act["action"] != "delete_branch":
            continue
        br = br_lookup[(act["repo"], act["branch"])]
        if br.attached_worktree and br.attached_worktree not in removed_wt_paths and os.path.exists(br.attached_worktree):
            errors.append(f"branch {br.name}: attached worktree {br.attached_worktree} was not removed")
            continue

        if apply:
            archived_already = False
            if br.merge_status != "merged":
                entry = vault.archive_branch(receipt_id, br.repo, br.name, br.sha, br.merge_status, br.attached_worktree)
                if entry is None:
                    errors.append(f"branch {br.name}: failed to archive to recovery vault; aborted deletion")
                    continue
                vault_entries.append(entry)
                archived_already = True

            if br.merge_status == "merged":
                dproc = run_git(br.repo, ["branch", "-d", br.name], readonly=False)
                if dproc.returncode != 0:
                    # Never force-delete -D without first archiving to Recovery Vault (P0-3)
                    if not archived_already:
                        entry = vault.archive_branch(
                            receipt_id, br.repo, br.name, br.sha, "merged_force_fallback", br.attached_worktree
                        )
                        if entry is None:
                            errors.append(f"branch {br.name}: `git branch -d` failed and vault archive failed; aborted")
                            continue
                        vault_entries.append(entry)
                    dproc = run_git(br.repo, ["branch", "-D", br.name], readonly=False)
            else:
                dproc = run_git(br.repo, ["branch", "-D", br.name], readonly=False)

            if dproc.returncode != 0:
                errors.append(f"branch {br.name} in {br.repo}: {dproc.stderr.strip()}")
                continue
            touched_repos.add(br.repo)
            executed.append({**act, "status": "deleted_branch"})
        else:
            executed.append({**act, "status": "dry_run"})

    if apply:
        prune_targets = touched_repos or {r.repo for r in scan.repos}
        for repo in sorted(prune_targets):
            if os.path.isdir(repo):
                run_git(repo, ["worktree", "prune"], readonly=False)
                if run_gc:
                    run_git(repo, ["gc", "--auto"], readonly=False)

    receipt = {
        "receipt_id": receipt_id,
        "command": "clean",
        "timestamp": now_utc_iso(),
        "apply": apply,
        "target": scan.target,
        "tiers": sorted(target_tiers),
        "executed": executed,
        "skipped_count": len(plan["skipped"]),
        "errors": errors,
        "freed_bytes": freed_bytes,
        "freed_human": human_bytes(freed_bytes),
        "vault_dir": str(vault.vault_root / receipt_id) if vault_entries else None,
        "vault_entries": vault_entries,
    }
    if apply:
        vault.append_receipt(receipt)
        if executed or freed_bytes > 0:
            slim_dirs = [d for act in executed for d in act.get("slimmed_dirs", [])]
            act_repos = set(touched_repos) | {act.get("repo") for act in executed if act.get("repo")}
            record_action_learning(state_dir, "clean", freed_bytes, slim_dirs, {r for r in act_repos if r})
    return receipt


def cmd_close(args: argparse.Namespace, cfg: Dict[str, Any], state_dir: Path) -> Tuple[int, Dict[str, Any]]:
    prot_branch_res = [re.compile(p) for p in cfg.get("protected_branch_patterns", [])]
    wt_arg = realpath_str(args.worktree) if args.worktree else None
    repo_arg = realpath_str(args.repo) if args.repo else None
    branch_arg = args.branch
    vault = VaultManager(state_dir)
    receipt_id = vault.new_receipt_id()

    if wt_arg:
        if not os.path.exists(wt_arg):
            return 2, {"error": f"Worktree path does not exist: {wt_arg}"}
        dot_git = os.path.join(wt_arg, ".git")
        if os.path.isdir(dot_git):
            return 2, {"error": f"Refused: {wt_arg} is a primary repository, not a linked worktree"}
        gd, parent = resolve_gitdir_pointer(wt_arg)
        if gd and not os.path.exists(gd):
            freed = dir_size_bytes(wt_arg) if os.path.isdir(wt_arg) else 0
            if args.apply:
                make_tree_writable(wt_arg)
                shutil.rmtree(wt_arg)
                receipt = {
                    "receipt_id": receipt_id,
                    "command": "close",
                    "timestamp": now_utc_iso(),
                    "apply": True,
                    "kind": "broken_worktree",
                    "worktree": wt_arg,
                    "freed_bytes": freed,
                    "freed_human": human_bytes(freed),
                    "status": "removed_broken_worktree",
                }
                vault.append_receipt(receipt)
                record_action_learning(state_dir, "close", freed, [], set())
                return 0, receipt
            return 0, {
                "receipt_id": receipt_id,
                "command": "close",
                "apply": False,
                "kind": "broken_worktree",
                "worktree": wt_arg,
                "estimated_freed_bytes": freed,
                "estimated_freed_human": human_bytes(freed),
                "status": "dry_run",
            }
        if not repo_arg and parent:
            repo_arg = parent
        if not branch_arg:
            b_proc = run_git(wt_arg, ["symbolic-ref", "--short", "HEAD"])
            if b_proc.returncode == 0:
                branch_arg = b_proc.stdout.strip()

    if not repo_arg:
        cwd = realpath_str(os.getcwd())
        c_proc = run_git(cwd, ["rev-parse", "--git-common-dir"])
        if c_proc.returncode != 0:
            return 2, {"error": "Cannot infer primary repo; pass --repo or --worktree"}
        common = c_proc.stdout.strip()
        if not os.path.isabs(common):
            common = os.path.join(cwd, common)
        common_real = realpath_str(common)
        repo_arg = os.path.dirname(common_real) if os.path.basename(common_real) == ".git" else cwd

    if not branch_arg and not wt_arg:
        return 2, {"error": "Must specify at least --branch or --worktree"}

    wt_entries = parse_worktrees_porcelain(repo_arg)
    if not wt_arg and branch_arg:
        for w in wt_entries:
            if w.get("branch") == branch_arg and w["path"] != repo_arg:
                wt_arg = w["path"]
                break

    if wt_arg and wt_arg == repo_arg:
        return 2, {"error": f"Refused: {wt_arg} is the primary repository root"}

    active_cwds, proc_status = get_active_process_cwds(ignore_self=True)
    current_cwd = realpath_str(os.getcwd())
    wt_head_sha = ""
    wt_detached = False
    dirty = False

    if wt_arg:
        if proc_status == "unavailable" and not getattr(args, "allow_without_proc_check", False):
            return 2, {"error": "Refused: active process check (lsof) unavailable; pass --allow-without-proc-check"}
        if is_subpath(current_cwd, wt_arg):
            return 2, {"error": f"Refused: current shell cwd is inside {wt_arg}"}
        if match_active_procs(wt_arg, active_cwds):
            return 2, {"error": f"Refused: active process has cwd inside {wt_arg}"}
        if os.path.exists(os.path.join(wt_arg, ".fbk-keep")):
            return 2, {"error": f"Refused: .fbk-keep sentinel present in {wt_arg}"}
        gd = resolve_wt_gitdir(wt_arg)
        if gd and os.path.exists(os.path.join(gd, "index.lock")):
            return 2, {"error": f"Refused: index.lock present in {wt_arg}"}
        for w in wt_entries:
            if w["path"] == wt_arg:
                wt_head_sha = w.get("head", "")
                wt_detached = bool(w.get("detached"))
                if w.get("locked"):
                    return 2, {"error": f"Refused: worktree is locked ({w.get('lock_reason', '')})"}
        st_proc = run_git(wt_arg, ["status", "--porcelain=v1"])
        if st_proc.returncode != 0:
            return 2, {"error": f"Refused: git status failed on {wt_arg}: {st_proc.stderr.strip()}"}
        dirty = bool(st_proc.stdout.strip())
        if dirty and not args.allow_dirty:
            return 2, {"error": f"Refused: worktree {wt_arg} has uncommitted changes (pass --allow-dirty to override)"}

    local_branches_proc = run_git(repo_arg, ["for-each-ref", "--format=%(refname:short)\t%(objectname)", "refs/heads/"])
    b_shas: Dict[str, str] = {}
    for ln in local_branches_proc.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) == 2:
            b_shas[parts[0]] = parts[1]

    base = args.base or detect_base_branch(repo_arg, set(b_shas.keys()), prot_branch_res)
    extra_bases = list(getattr(args, "extra_base", None) or [])
    anchor_refs = ([base] if base else []) + extra_bases

    merge_status = "detached"
    sha = ""
    if branch_arg:
        if (base and branch_arg == base) or any(r.search(branch_arg) for r in prot_branch_res):
            return 2, {"error": f"Refused: branch '{branch_arg}' is protected"}
        if branch_arg not in b_shas:
            return 2, {"error": f"Branch '{branch_arg}' does not exist in {repo_arg}"}
        sha = b_shas[branch_arg]
        merged_set: Set[str] = set()
        for anc in anchor_refs:
            m_proc = run_git(repo_arg, ["branch", "--merged", anc, "--format=%(refname:short)"])
            if m_proc.returncode == 0:
                merged_set.update(ln.strip() for ln in m_proc.stdout.splitlines() if ln.strip())
        merge_status, _ = check_merge_status(repo_arg, branch_arg, base, merged_set, fast=False, squash_budget_left=1)
        if merge_status not in ("merged", "cherry_merged", "squash_merged") and not args.force_unmerged:
            return 2, {
                "error": f"Refused: branch '{branch_arg}' is {merge_status} relative to '{base}'. Pass --force-unmerged to archive & delete."
            }
    elif wt_detached:
        if not wt_head_sha:
            hp = run_git(wt_arg, ["rev-parse", "HEAD"])
            wt_head_sha = hp.stdout.strip() if hp.returncode == 0 else ""
        sha = wt_head_sha
        anc_ok = False
        if sha and anchor_refs:
            for anc in anchor_refs:
                if run_git(repo_arg, ["merge-base", "--is-ancestor", sha, anc]).returncode == 0:
                    anc_ok = True
                    break
        merge_status = "detached_merged" if anc_ok else "detached_unmerged"
        if merge_status != "detached_merged" and not args.force_unmerged:
            return 2, {
                "error": f"Refused: detached HEAD ({sha[:8]}) in {wt_arg} is not merged into '{base}'. Pass --force-unmerged to archive & close."
            }

    freed_bytes = dir_size_bytes(wt_arg) if (wt_arg and os.path.isdir(wt_arg)) else 0
    vault_entries: List[Dict[str, Any]] = []

    if args.apply:
        if wt_arg and os.path.isdir(wt_arg):
            if dirty and args.allow_dirty:
                snap = vault.archive_dirty_worktree(receipt_id, repo_arg, wt_arg, os.path.basename(wt_arg))
                if snap:
                    vault_entries.append(snap)
            if wt_detached and merge_status != "detached_merged" and sha:
                d_entry = vault.archive_ref_or_sha(
                    receipt_id,
                    repo_arg,
                    f"detached-{os.path.basename(wt_arg)}",
                    sha,
                    merge_status,
                    worktree_path=wt_arg,
                    ref_exists=False,
                )
                if d_entry is None:
                    return 2, {"error": f"Failed to archive detached HEAD {sha} into vault; close aborted"}
                vault_entries.append(d_entry)

            make_tree_writable(wt_arg)
            rm = run_git(repo_arg, ["worktree", "remove", "--force", wt_arg], readonly=False)
            if rm.returncode != 0:
                shutil.rmtree(wt_arg)
                run_git(repo_arg, ["worktree", "prune"], readonly=False)

        if branch_arg:
            if merge_status != "merged":
                entry = vault.archive_branch(receipt_id, repo_arg, branch_arg, sha, merge_status, wt_arg)
                if entry is None:
                    return 2, {"error": f"Failed to archive '{branch_arg}' into vault; branch deletion aborted"}
                vault_entries.append(entry)
                del_proc = run_git(repo_arg, ["branch", "-D", branch_arg], readonly=False)
            else:
                del_proc = run_git(repo_arg, ["branch", "-d", branch_arg], readonly=False)
                if del_proc.returncode != 0:
                    entry = vault.archive_branch(receipt_id, repo_arg, branch_arg, sha, "merged_force_fallback", wt_arg)
                    if entry is None:
                        return 2, {"error": f"Failed -d and vault archive for '{branch_arg}'; aborted"}
                    vault_entries.append(entry)
                    del_proc = run_git(repo_arg, ["branch", "-D", branch_arg], readonly=False)
            if del_proc.returncode != 0:
                return 2, {"error": f"Failed to delete branch '{branch_arg}': {del_proc.stderr.strip()}"}

        run_git(repo_arg, ["worktree", "prune"], readonly=False)
        receipt = {
            "receipt_id": receipt_id,
            "command": "close",
            "timestamp": now_utc_iso(),
            "apply": True,
            "repo": repo_arg,
            "worktree": wt_arg,
            "branch": branch_arg,
            "base": base,
            "merge_status": merge_status,
            "freed_bytes": freed_bytes,
            "freed_human": human_bytes(freed_bytes),
            "vault_dir": str(vault.vault_root / receipt_id) if vault_entries else None,
            "vault_entries": vault_entries,
        }
        vault.append_receipt(receipt)
        record_action_learning(state_dir, "close", freed_bytes, [], {repo_arg} if repo_arg else set())
        return 0, receipt

    needs_vault = bool(
        (branch_arg and merge_status != "merged")
        or (wt_detached and merge_status != "detached_merged")
        or (dirty and args.allow_dirty)
    )
    return 0, {
        "receipt_id": receipt_id,
        "command": "close",
        "apply": False,
        "repo": repo_arg,
        "worktree": wt_arg,
        "branch": branch_arg,
        "base": base,
        "merge_status": merge_status,
        "would_archive_to_vault": needs_vault,
        "estimated_freed_bytes": freed_bytes,
        "estimated_freed_human": human_bytes(freed_bytes),
    }


def cmd_restore(args: argparse.Namespace, state_dir: Path) -> Tuple[int, Dict[str, Any]]:
    vault_root = state_dir / "vault"
    if args.list or not args.receipt_id:
        items: List[Dict[str, Any]] = []
        if vault_root.is_dir():
            for entry in sorted(vault_root.iterdir(), reverse=True):
                m_file = entry / "manifest.json"
                if m_file.is_file():
                    try:
                        with open(m_file, "r", encoding="utf-8") as fh:
                            items.append(json.load(fh))
                    except Exception:
                        continue
        return 0, {"vault_root": str(vault_root), "receipts": items}

    vdir = vault_root / args.receipt_id
    m_file = vdir / "manifest.json"
    if not m_file.is_file():
        return 2, {"error": f"Vault receipt not found: {m_file}"}

    with open(m_file, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    restored: List[Dict[str, Any]] = []
    dirty_restored: List[Dict[str, Any]] = []
    errors: List[str] = []
    for entry in manifest.get("entries", []):
        b_name = entry.get("branch")
        if args.branch and b_name != args.branch:
            continue
        repo = entry["repo"]
        sha = entry["sha"]
        bundle_path = vdir / entry["bundle"]
        bundle_ref = entry.get("bundle_ref") or f"refs/heads/{b_name}"

        if not args.apply:
            restored.append({"repo": repo, "branch": b_name, "sha": sha, "bundle": str(bundle_path), "status": "dry_run"})
            continue

        chk = run_git(repo, ["show-ref", "--verify", f"refs/heads/{b_name}"])
        if chk.returncode == 0:
            errors.append(f"Branch '{b_name}' already exists in {repo}")
            continue

        cat = run_git(repo, ["cat-file", "-e", f"{sha}^{{commit}}"])
        if cat.returncode == 0:
            br = run_git(repo, ["branch", b_name, sha], readonly=False)
            if br.returncode == 0:
                restored.append({"repo": repo, "branch": b_name, "sha": sha, "method": "local_sha", "status": "restored"})
                continue

        if not bundle_path.is_file():
            errors.append(f"Bundle missing for '{b_name}': {bundle_path}")
            continue

        ft = run_git(repo, ["fetch", str(bundle_path), f"{bundle_ref}:refs/heads/{b_name}"], readonly=False)
        if ft.returncode != 0:
            ft = run_git(repo, ["fetch", str(bundle_path), f"HEAD:refs/heads/{b_name}"], readonly=False)
        if ft.returncode != 0:
            errors.append(f"Failed restoring '{b_name}' from bundle: {ft.stderr.strip()}")
            continue
        restored.append({"repo": repo, "branch": b_name, "sha": sha, "method": "bundle_fetch", "status": "restored"})

    extract_dir = getattr(args, "extract_dirty_to", None)
    for snap in manifest.get("dirty_snapshots", []):
        patch_p = str(vdir / snap["patch"]) if snap.get("patch") else None
        tar_p = str(vdir / snap["untracked_tar"]) if snap.get("untracked_tar") else None
        info = {**snap, "patch_path": patch_p, "untracked_tar_path": tar_p}
        if args.apply and extract_dir and tar_p and os.path.isfile(tar_p):
            dest = Path(extract_dir)
            dest.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tar_p, "r:gz") as tf:
                dest_real = realpath_str(dest)
                for member in tf.getmembers():
                    m_target = realpath_str(os.path.join(dest_real, member.name))
                    if is_subpath(m_target, dest_real) and not member.issym() and not member.islnk():
                        tf.extract(member, dest_real)
            info["extracted_to"] = str(dest)
        dirty_restored.append(info)

    return (0 if not errors else 1), {
        "command": "restore",
        "receipt_id": args.receipt_id,
        "apply": args.apply,
        "restored": restored,
        "dirty_snapshots": dirty_restored,
        "errors": errors,
    }


def cmd_repair_alternates(args: argparse.Namespace, state_dir: Path) -> Tuple[int, Dict[str, Any]]:
    _, target, primary_repos, _, _, errors = discover_targets(args.repo, args.root, max_depth=3)
    vault = VaultManager(state_dir)
    receipt_id = vault.new_receipt_id()
    results: List[Dict[str, Any]] = []

    for repo in primary_repos:
        alt_file = os.path.join(repo, ".git", "objects", "info", "alternates")
        if not os.path.isfile(alt_file):
            continue
        obj_dir = os.path.join(repo, ".git", "objects")
        kept_lines: List[str] = []
        missing_lines: List[str] = []
        with open(alt_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                target_obj = raw if os.path.isabs(raw) else os.path.join(obj_dir, raw)
                if os.path.isdir(target_obj):
                    kept_lines.append(raw)
                else:
                    missing_lines.append(raw)

        if not missing_lines:
            continue

        # Verify self-containment in a zero-mutation proxy gitdir before touching real alternates
        fsck_ok = False
        fsck_err = ""
        with tempfile.TemporaryDirectory(prefix="fbk-alt-proxy-") as proxy:
            git_dir = os.path.join(repo, ".git")
            for item in ("HEAD", "config", "packed-refs"):
                src = os.path.join(git_dir, item)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(proxy, item))
            refs_src = os.path.join(git_dir, "refs")
            if os.path.isdir(refs_src):
                shutil.copytree(refs_src, os.path.join(proxy, "refs"), symlinks=True)
            p_obj = os.path.join(proxy, "objects")
            os.makedirs(p_obj, exist_ok=True)
            for item in os.listdir(obj_dir):
                if item == "info":
                    continue
                os.symlink(os.path.join(obj_dir, item), os.path.join(p_obj, item))
            p_info = os.path.join(p_obj, "info")
            os.makedirs(p_info, exist_ok=True)
            orig_info = os.path.join(obj_dir, "info")
            if os.path.isdir(orig_info):
                for item in os.listdir(orig_info):
                    if item == "alternates":
                        continue
                    src = os.path.join(orig_info, item)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(p_info, item))
            if kept_lines:
                with open(os.path.join(p_info, "alternates"), "w", encoding="utf-8") as fh:
                    fh.write("\n".join(kept_lines) + "\n")

            fsck = subprocess.run(
                ["git", "--no-optional-locks", f"--git-dir={proxy}", "fsck", "--connectivity-only", "--no-progress"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            fsck_ok = fsck.returncode == 0
            if not fsck_ok:
                fsck_err = (fsck.stderr or fsck.stdout).strip()[:500]

        status = "dry_run_safe_self_contained" if fsck_ok else "refused_fsck_failed"
        backup_path = None
        if args.apply:
            if not fsck_ok:
                errors.append(f"{repo}: refused repair-alternates because fsck failed: {fsck_err}")
                status = "refused_fsck_failed"
            else:
                vdir = vault.vault_root / receipt_id
                vdir.mkdir(parents=True, exist_ok=True)
                backup_file = vdir / f"{short_hash(repo)}-alternates.bak"
                shutil.copy2(alt_file, backup_file)
                backup_path = str(backup_file)
                if kept_lines:
                    with open(alt_file, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(kept_lines) + "\n")
                else:
                    os.remove(alt_file)
                status = "repaired"

        results.append(
            {
                "repo": repo,
                "alternates_file": alt_file,
                "missing_lines": missing_lines,
                "kept_lines": kept_lines,
                "fsck_ok": fsck_ok,
                "fsck_error": fsck_err,
                "backup_path": backup_path,
                "status": status,
            }
        )

    receipt = {
        "receipt_id": receipt_id,
        "command": "repair-alternates",
        "timestamp": now_utc_iso(),
        "apply": args.apply,
        "target": target,
        "results": results,
        "errors": errors,
    }
    if args.apply and any(r["status"] == "repaired" for r in results):
        vault.append_receipt(receipt)
    has_refusal = any(r["status"] == "refused_fsck_failed" for r in results)
    return (1 if (errors or has_refusal) else 0), receipt


def cmd_prune_remotes(args: argparse.Namespace, state_dir: Path) -> Tuple[int, Dict[str, Any]]:
    _, target, primary_repos, _, _, errors = discover_targets(args.repo, args.root, max_depth=3)
    vault = VaultManager(state_dir)
    receipt_id = vault.new_receipt_id()
    actions: List[Dict[str, Any]] = []

    for repo in primary_repos:
        _, _, _, non_canonical, remote_ref_count = inspect_repo_remotes(repo)
        removed_in_repo = 0
        for rem in non_canonical:
            r_name = rem["name"]
            r_url = rem["url"]
            r_kind = rem["kind"]
            if r_name == "origin":
                if not (args.include_broken_origin and r_kind in ("tmp_or_artifact", "broken_local")):
                    continue
            if r_kind == "local_clone" and not args.include_local_clones:
                continue

            status = "dry_run"
            if args.apply:
                proc = run_git(repo, ["remote", "remove", r_name], readonly=False)
                if proc.returncode != 0:
                    errors.append(f"{repo}: failed to remove remote '{r_name}': {proc.stderr.strip()}")
                    continue
                removed_in_repo += 1
                status = "removed"
            actions.append(
                {
                    "repo": repo,
                    "remote": r_name,
                    "url": r_url,
                    "kind": r_kind,
                    "repo_remote_refs_before": remote_ref_count,
                    "status": status,
                }
            )
        if args.apply and removed_in_repo > 0 and not args.no_pack_refs:
            run_git(repo, ["pack-refs", "--all", "--prune"], readonly=False)

    receipt = {
        "receipt_id": receipt_id,
        "command": "prune-remotes",
        "timestamp": now_utc_iso(),
        "apply": args.apply,
        "target": target,
        "actions": actions,
        "errors": errors,
    }
    if args.apply and actions:
        vault.append_receipt(receipt)
    return (1 if errors else 0), receipt


def cmd_cutover_bundle(args: argparse.Namespace, cfg: Dict[str, Any], state_dir: Path) -> Tuple[int, Dict[str, Any]]:
    repo = realpath_str(args.repo)
    if not os.path.isdir(repo):
        return 2, {"error": f"Repo path does not exist: {repo}"}

    origin_url, origin_kind, _, _, remote_ref_count = inspect_repo_remotes(repo)
    ref_proc = run_git(repo, ["for-each-ref", "--format=%(refname:short)\t%(objectname)\t%(committerdate:unix)", "refs/heads/"])
    if ref_proc.returncode != 0:
        return 2, {"error": f"for-each-ref failed on {repo}: {ref_proc.stderr.strip()}"}

    branches: List[Dict[str, Any]] = []
    for ln in ref_proc.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3:
            branches.append({"name": parts[0], "sha": parts[1], "committer_ts": int(parts[2] or 0)})

    unpushed_shas: Optional[Set[str]] = None
    if not args.all_local and origin_kind == "canonical_remote":
        has_org = run_git(repo, ["for-each-ref", "--count=1", "--format=.", "refs/remotes/origin/"])
        if has_org.returncode == 0 and has_org.stdout.strip():
            up = run_git(repo, ["rev-list", "--branches", "--not", "--remotes=origin"])
            if up.returncode == 0:
                unpushed_shas = {ln.strip() for ln in up.stdout.splitlines() if ln.strip()}

    selected: List[Dict[str, Any]] = []
    for br in branches:
        if args.all_local or unpushed_shas is None or br["sha"] in unpushed_shas:
            subj = run_git(repo, ["log", "-1", "--format=%s", br["sha"]]).stdout.strip()
            selected.append({**br, "subject": subj})

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else (state_dir / "cutover" / f"{os.path.basename(repo)}-{ts_tag}")
    bundle_path = out_dir / "local-backup.bundle"
    manifest_path = out_dir / "unpushed-manifest.json"
    summary_path = out_dir / "unpushed-summary.md"

    vault = VaultManager(state_dir)
    receipt_id = vault.new_receipt_id()

    if args.apply:
        out_dir.mkdir(parents=True, exist_ok=True)
        bundle_created = False
        if selected:
            refs_to_pack = [f"refs/heads/{b['name']}" for b in selected]
            bproc = run_git(repo, ["bundle", "create", str(bundle_path)] + refs_to_pack, readonly=False)
            if bproc.returncode != 0:
                return 2, {"error": f"git bundle create failed: {bproc.stderr.strip()}"}
            bundle_created = True

        manifest = {
            "receipt_id": receipt_id,
            "created_at": now_utc_iso(),
            "repo": repo,
            "origin_url": origin_url,
            "origin_kind": origin_kind,
            "remote_ref_count": remote_ref_count,
            "bundle_file": str(bundle_path) if bundle_created else None,
            "branch_count": len(selected),
            "branches": selected,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        md_lines = [
            f"# Clean-Slate Cutover Manifest: `{os.path.basename(repo)}`",
            f"- Source repo: `{repo}`",
            f"- Origin (`{origin_kind}`): `{origin_url}`",
            f"- Captured branches ({len(selected)}):",
        ]
        for b in selected[:50]:
            md_lines.append(f"  - `{b['name']}` (`{b['sha'][:8]}`) — {b['subject']}")
        if len(selected) > 50:
            md_lines.append(f"  - ... and {len(selected) - 50} more (see `unpushed-manifest.json`)")
        if bundle_created:
            md_lines.extend(
                [
                    "",
                    "## Restore Into a Fresh Clean Clone",
                    "```bash",
                    f"git fetch {bundle_path} 'refs/heads/*:refs/heads/cutover/*'",
                    "```",
                ]
            )
        summary_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        receipt = {
            "receipt_id": receipt_id,
            "command": "cutover-bundle",
            "timestamp": now_utc_iso(),
            "apply": True,
            "repo": repo,
            "out_dir": str(out_dir),
            "bundle_file": str(bundle_path) if bundle_created else None,
            "branch_count": len(selected),
        }
        vault.append_receipt(receipt)
        return 0, receipt

    return 0, {
        "receipt_id": receipt_id,
        "command": "cutover-bundle",
        "apply": False,
        "repo": repo,
        "origin_kind": origin_kind,
        "out_dir": str(out_dir),
        "branch_count": len(selected),
        "branches": selected,
    }


def cmd_audit_remote(args: argparse.Namespace, cfg: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    repo = realpath_str(args.repo)
    if not os.path.isdir(repo):
        return 2, {"error": f"Repo path does not exist: {repo}"}

    remote = args.remote
    if args.fetch:
        fproc = run_git(repo, ["fetch", "--prune", remote], timeout=60, readonly=False)
        if fproc.returncode != 0:
            return 2, {"error": f"git fetch --prune {remote} failed: {fproc.stderr.strip()}"}

    prot_res = [re.compile(p) for p in cfg.get("protected_branch_patterns", [])]
    stale_days = args.stale_days if args.stale_days is not None else int(cfg.get("remote_audit_stale_days", 30))
    recent_days = args.recent_days if args.recent_days is not None else int(cfg.get("remote_audit_recent_days", 14))
    now_ts = int(time.time())

    # Detect remote base ref
    base_ref = args.base
    if not base_ref:
        sym = run_git(repo, ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"])
        if sym.returncode == 0 and sym.stdout.strip():
            base_ref = f"refs/remotes/{sym.stdout.strip()}"
        else:
            for cand in (f"refs/remotes/{remote}/main", f"refs/remotes/{remote}/master", f"refs/remotes/{remote}/develop"):
                if run_git(repo, ["rev-parse", "--verify", f"{cand}^{{commit}}"]).returncode == 0:
                    base_ref = cand
                    break

    ref_proc = run_git(
        repo,
        [
            "for-each-ref",
            "--format=%(refname:short)\t%(objectname)\t%(committerdate:unix)\t%(authorname)",
            f"refs/remotes/{remote}/",
        ],
    )
    if ref_proc.returncode != 0:
        return 2, {"error": f"Failed listing refs/remotes/{remote}/: {ref_proc.stderr.strip()}"}

    merged_remote: Set[str] = set()
    if base_ref:
        mr = run_git(repo, ["branch", "-r", "--merged", base_ref, "--format=%(refname:short)"])
        if mr.returncode == 0:
            merged_remote = {ln.strip() for ln in mr.stdout.splitlines() if ln.strip()}

    buckets: Dict[str, List[Dict[str, Any]]] = {
        "protected": [],
        "merged_deletable": [],
        "stale_archive_candidate": [],
        "recent_claim_needed": [],
        "mid_review": [],
    }
    prefix = f"{remote}/"
    for ln in ref_proc.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        full_short = parts[0]
        if full_short == f"{remote}/HEAD":
            continue
        short_name = full_short[len(prefix) :] if full_short.startswith(prefix) else full_short
        sha = parts[1]
        cts = int(parts[2] or 0)
        author = parts[3] if len(parts) > 3 else ""
        age_days = round((now_ts - cts) / 86400.0, 1) if cts > 0 else 9999.0

        item = {"remote_branch": full_short, "name": short_name, "sha": sha, "age_days": age_days, "author": author}
        if (base_ref and full_short == base_ref.replace("refs/remotes/", "")) or any(r.search(short_name) for r in prot_res):
            buckets["protected"].append(item)
        elif full_short in merged_remote:
            buckets["merged_deletable"].append(item)
        elif age_days >= stale_days:
            buckets["stale_archive_candidate"].append(item)
        elif age_days < recent_days:
            buckets["recent_claim_needed"].append(item)
        else:
            buckets["mid_review"].append(item)

    return 0, {
        "command": "audit-remote",
        "repo": repo,
        "remote": remote,
        "base_ref": base_ref,
        "stale_days": stale_days,
        "recent_days": recent_days,
        "counts": {k: len(v) for k, v in buckets.items()},
        "buckets": buckets,
    }


def _ensure_symlink(link_path: Path, target_path: Path, apply: bool) -> Dict[str, Any]:
    target_real = realpath_str(target_path)
    if link_path.is_symlink():
        cur = realpath_str(link_path)
        if cur == target_real:
            return {"kind": "symlink", "path": str(link_path), "target": str(target_path), "status": "already_configured"}
    elif link_path.exists():
        return {"kind": "symlink", "path": str(link_path), "target": str(target_path), "status": "conflict_non_symlink_exists"}

    if not apply:
        return {"kind": "symlink", "path": str(link_path), "target": str(target_path), "status": "would_create"}

    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.is_symlink():
        link_path.unlink()
    os.symlink(target_path, link_path)
    return {"kind": "symlink", "path": str(link_path), "target": str(target_path), "status": "created"}


def _merge_agy_hooks(hooks_path: Path, ss_cmd: str, pre_cmd: str, apply: bool) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if hooks_path.is_file():
        try:
            with open(hooks_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            return {"kind": "agy_hooks", "path": str(hooks_path), "status": f"error_invalid_json: {exc}"}

    existing = data.get("fireworks-branch-killer")
    ss_ok = False
    pre_ok = False
    if isinstance(existing, dict):
        for group in existing.get("SessionStart", []) or []:
            if isinstance(group, dict) and str(group.get("command", "")) == ss_cmd:
                ss_ok = True
        for group in existing.get("PreToolUse", []) or []:
            if isinstance(group, dict) and group.get("matcher") == "run_command":
                for hk in group.get("hooks", []) or []:
                    if isinstance(hk, dict) and str(hk.get("command", "")) == pre_cmd:
                        pre_ok = True

    if ss_ok and pre_ok:
        return {"kind": "agy_hooks", "path": str(hooks_path), "status": "already_configured"}

    if not apply:
        return {"kind": "agy_hooks", "path": str(hooks_path), "status": "would_update"}

    data["fireworks-branch-killer"] = {
        "enabled": True,
        "SessionStart": [
            {
                "type": "command",
                "command": ss_cmd,
                "timeout": 5,
            }
        ],
        "PreToolUse": [
            {
                "matcher": "run_command",
                "hooks": [
                    {
                        "type": "command",
                        "command": pre_cmd,
                        "timeout": 5,
                    }
                ],
            }
        ],
    }
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    with open(hooks_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return {"kind": "agy_hooks", "path": str(hooks_path), "status": "updated"}


def _merge_claude_or_codex_hooks_list(
    hooks_obj: Dict[str, Any],
    event_name: str,
    matcher: str,
    hook_cmd: str,
) -> bool:
    """Idempotently add or upgrade hook_adapter.py entry in hooks[event_name]; returns True if modified."""
    ev_list = hooks_obj.get(event_name)
    if not isinstance(ev_list, list):
        ev_list = []
    for entry in ev_list:
        if not isinstance(entry, dict):
            continue
        for hk in entry.get("hooks", []) or []:
            if isinstance(hk, dict) and "hook_adapter.py" in str(hk.get("command", "")):
                if str(hk.get("command", "")) == hook_cmd and entry.get("matcher", "") == matcher:
                    return False
                entry["matcher"] = matcher
                hk["command"] = hook_cmd
                hk["timeout"] = 5
                hooks_obj[event_name] = ev_list
                return True
    ev_list.append(
        {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": hook_cmd,
                    "timeout": 5,
                }
            ],
        }
    )
    hooks_obj[event_name] = ev_list
    return True


def _merge_claude_settings(settings_path: Path, ss_cmd: str, pre_cmd: str, apply: bool) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if settings_path.is_file():
        try:
            with open(settings_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            return {"kind": "claude_settings", "path": str(settings_path), "status": f"error_invalid_json: {exc}"}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    changed_ss = _merge_claude_or_codex_hooks_list(hooks, "SessionStart", "", ss_cmd)
    changed_pre = _merge_claude_or_codex_hooks_list(hooks, "PreToolUse", "Bash", pre_cmd)
    if not changed_ss and not changed_pre:
        return {"kind": "claude_settings", "path": str(settings_path), "status": "already_configured"}

    if not apply:
        return {"kind": "claude_settings", "path": str(settings_path), "status": "would_merge_hooks"}

    data["hooks"] = hooks
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return {"kind": "claude_settings", "path": str(settings_path), "status": "merged"}


def _merge_codex_hooks(codex_hooks_path: Path, ss_cmd: str, apply: bool) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if codex_hooks_path.is_file():
        try:
            with open(codex_hooks_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            return {"kind": "codex_hooks", "path": str(codex_hooks_path), "status": f"error_invalid_json: {exc}"}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    changed = _merge_claude_or_codex_hooks_list(hooks, "SessionStart", "", ss_cmd)
    if not changed:
        return {"kind": "codex_hooks", "path": str(codex_hooks_path), "status": "already_configured"}
    if not apply:
        return {"kind": "codex_hooks", "path": str(codex_hooks_path), "status": "would_merge_session_start"}

    data["hooks"] = hooks
    codex_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    with open(codex_hooks_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return {"kind": "codex_hooks", "path": str(codex_hooks_path), "status": "merged"}


def cmd_install(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    raw_targets = [t.strip().lower() for t in args.target.split(",") if t.strip()]
    if "all" in raw_targets:
        targets = list(ALL_HARNESSES)
    else:
        invalid = [t for t in raw_targets if t not in ALL_HARNESSES]
        if invalid:
            return 2, {"error": f"Unknown target harness(es): {', '.join(invalid)}. Valid: all, {', '.join(ALL_HARNESSES)}"}
        targets = raw_targets

    skill_src = SKILL_DIR
    hook_script = skill_src / "scripts" / "hook_adapter.py"
    fbk_script = skill_src / "scripts" / "fbk.py"
    actions: List[Dict[str, Any]] = []
    notes: List[str] = []

    if args.scope == "workspace":
        ws = Path(realpath_str(args.workspace))
        if not ws.is_dir():
            return 2, {"error": f"Workspace does not exist: {ws}"}

        shared_skill = ws / ".agents" / "skills" / "fireworks-branch-killer"
        if not (shared_skill / "SKILL.md").is_file():
            actions.append(_ensure_symlink(shared_skill, skill_src, args.apply))
        else:
            actions.append({"kind": "shared_skill", "path": str(shared_skill), "status": "already_configured"})

        if "agy" in targets:
            hooks_path = ws / ".agents" / "hooks.json"
            ss_cmd = f"python3 {hook_script} --event SessionStart --harness agy"
            pre_cmd = f"python3 {hook_script} --event PreToolUse --harness agy"
            actions.append(_merge_agy_hooks(hooks_path, ss_cmd, pre_cmd, args.apply))

        if "claude-code" in targets:
            actions.append(_ensure_symlink(ws / ".claude" / "skills" / "fireworks-branch-killer", skill_src, args.apply))
            ss_cmd = f"python3 {hook_script} --event SessionStart --harness claude-code"
            pre_cmd = f"python3 {hook_script} --event PreToolUse --harness claude-code"
            actions.append(_merge_claude_settings(ws / ".claude" / "settings.json", ss_cmd, pre_cmd, args.apply))

        for h in ("codex", "deepseek-harness", "amy", "t3-code", "orca"):
            if h in targets:
                actions.append(
                    {
                        "kind": "agent_skills_standard",
                        "harness": h,
                        "path": str(shared_skill / "SKILL.md"),
                        "status": "ready_via_dot_agents_skills",
                    }
                )
        notes.append(
            f"Shell/AGENTS snippet for harnesses without native hooks: python3 {hook_script} --harness plain"
        )
    else:
        home = Path(os.path.expanduser(args.home))
        actions.append(_ensure_symlink(home / ".agents" / "skills" / "fireworks-branch-killer", skill_src, args.apply))
        actions.append(_ensure_symlink(home / ".local" / "bin" / "fbk", fbk_script, args.apply))

        if "claude-code" in targets:
            actions.append(_ensure_symlink(home / ".claude" / "skills" / "fireworks-branch-killer", skill_src, args.apply))
            ss_cmd = f"python3 {hook_script} --event SessionStart --harness claude-code"
            pre_cmd = f"python3 {hook_script} --event PreToolUse --harness claude-code"
            actions.append(_merge_claude_settings(home / ".claude" / "settings.json", ss_cmd, pre_cmd, args.apply))

        if "codex" in targets:
            actions.append(_ensure_symlink(home / ".codex" / "skills" / "fireworks-branch-killer", skill_src, args.apply))
            actions.append(
                _merge_codex_hooks(
                    home / ".codex" / "hooks.json",
                    f"python3 {hook_script} --event SessionStart --harness codex",
                    args.apply,
                )
            )

        if "orca" in targets and (home / ".orca").is_dir():
            actions.append(_ensure_symlink(home / ".orca" / "skills" / "fireworks-branch-killer", skill_src, args.apply))

        if "agy" in targets:
            actions.append(
                _merge_agy_hooks(
                    home / ".gemini" / "config" / "hooks.json",
                    f"python3 {hook_script} --event SessionStart --harness agy",
                    f"python3 {hook_script} --event PreToolUse --harness agy",
                    args.apply,
                )
            )
        for h in ("deepseek-harness", "amy", "t3-code", "orca"):
            if h in targets:
                actions.append(
                    {
                        "kind": "agent_skills_standard",
                        "harness": h,
                        "path": str(home / ".agents" / "skills" / "fireworks-branch-killer" / "SKILL.md"),
                        "status": "ready_via_home_dot_agents_skills",
                    }
                )

    has_error = any(str(a.get("status", "")).startswith("error_") for a in actions)
    return (1 if has_error else 0), {
        "command": "install",
        "scope": args.scope,
        "targets": targets,
        "apply": args.apply,
        "actions": actions,
        "notes": notes,
    }


def _check_claude_hooks_status(settings_path: Path) -> Dict[str, Any]:
    if not settings_path.is_file():
        return {"status": "not_found", "session_start": False, "pre_tool_use": False}
    try:
        sdata = json.loads(settings_path.read_text(encoding="utf-8"))
        ss = sdata.get("hooks", {}).get("SessionStart", [])
        pre = sdata.get("hooks", {}).get("PreToolUse", [])
        has_ss = any(
            "hook_adapter.py" in str(hk.get("command", ""))
            for grp in ss
            if isinstance(grp, dict)
            for hk in grp.get("hooks", []) or []
            if isinstance(hk, dict)
        )
        has_pre = any(
            "hook_adapter.py" in str(hk.get("command", ""))
            for grp in pre
            if isinstance(grp, dict)
            for hk in grp.get("hooks", []) or []
            if isinstance(hk, dict)
        )
        return {"status": "valid" if (has_ss and has_pre) else ("partial" if has_ss else "missing_fbk_hook"), "session_start": has_ss, "pre_tool_use": has_pre}
    except Exception as exc:
        return {"status": f"invalid_json: {exc}", "session_start": False, "pre_tool_use": False}


def _read_claude_symlink_dirs(settings_path: Path) -> List[str]:
    """Extract Claude Code worktree.symlinkDirectories from nested or flat JSON keys."""
    if not settings_path.is_file():
        return []
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    dirs: List[str] = []
    wt_cfg = data.get("worktree")
    if isinstance(wt_cfg, dict) and isinstance(wt_cfg.get("symlinkDirectories"), list):
        dirs.extend(str(x).strip() for x in wt_cfg["symlinkDirectories"] if str(x).strip())
    flat = data.get("worktree.symlinkDirectories")
    if isinstance(flat, list):
        dirs.extend(str(x).strip() for x in flat if str(x).strip())
    return list(dict.fromkeys(dirs))


def _find_trailing_slash_ignores(gitignore_path: Path, symlink_dirs: List[str]) -> List[str]:
    """Return patterns in .gitignore whose trailing '/' breaks Git ignoring of symlinks."""
    if not symlink_dirs or not gitignore_path.is_file():
        return []
    try:
        lines = gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    targets = {d.strip("/").lstrip("./") for d in symlink_dirs if d.strip("/")}
    bad: List[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or not line.endswith("/"):
            continue
        norm = line.lstrip("/").rstrip("/")
        if norm in targets:
            bad.append(line)
    return list(dict.fromkeys(bad))


def _sample_dir_hardlink_stats(dir_path: Path, max_files: int = 200) -> Tuple[int, int]:
    """Sample up to max_files regular files inside dir_path to detect hardlinked package stores (e.g., pnpm)."""
    sampled = 0
    hardlinked = 0
    try:
        for root_str, dirs, files in os.walk(dir_path, followlinks=False):
            # Prune deep recursion when we already sampled enough files
            if len(dirs) > 60:
                dirs[:] = dirs[:60]
            for fn in files:
                fp = os.path.join(root_str, fn)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    sampled += 1
                    if st.st_nlink > 1:
                        hardlinked += 1
                    if sampled >= max_files:
                        return sampled, hardlinked
    except OSError:
        pass
    return sampled, hardlinked


def detect_space_saving_tips(
    ws_root: Path, primary_repos: List[str]
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """Inspect workspace & repo stacks read-only to emit actionable space-saving tips and observations."""
    tips: List[Dict[str, str]] = []
    seen_ids: Set[str] = set()

    def add_tip(tip_id: str, severity: str, stack: str, message: str, action: str) -> None:
        if tip_id in seen_ids:
            return
        seen_ids.add(tip_id)
        tips.append(
            {
                "id": tip_id,
                "severity": severity,
                "stack": stack,
                "message": message,
                "action": action,
            }
        )

    probe_roots: List[Path] = []
    seen_roots: Set[str] = set()
    for candidate in [ws_root] + [Path(r) for r in primary_repos]:
        key = str(candidate)
        if key not in seen_roots and candidate.is_dir():
            seen_roots.add(key)
            probe_roots.append(candidate)
    try:
        with os.scandir(ws_root) as it:
            for entry in it:
                if entry.name.startswith(".") or entry.name in SKIP_NESTED_NAMES:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    p = Path(entry.path)
                    if str(p) not in seen_roots:
                        seen_roots.add(str(p))
                        probe_roots.append(p)
    except OSError:
        pass

    global_symlink_dirs = _read_claude_symlink_dirs(Path(os.path.expanduser("~/.claude/settings.json")))

    has_pkg_json = False
    has_npm_lock = False
    has_pnpm_lock = False
    has_pnpm_global_store = False
    has_e2e_browser = False
    has_python = False
    has_rust = False
    has_jvm_or_mobile = False
    independent_nm_count = 0
    hardlinked_nm_count = 0
    real_venvs = 0
    trailing_slash_hits: List[str] = []
    observed_stacks: Set[str] = set()
    observed_hogs: Dict[str, Dict[str, Any]] = {}

    def record_hog(hog_key: str, hardlinked: bool = False) -> None:
        blk = observed_hogs.setdefault(hog_key, {"count": 0, "hardlinked": hardlinked})
        blk["count"] += 1
        blk["hardlinked"] = blk["hardlinked"] or hardlinked

    for root in probe_roots:
        try:
            local_symlinks = _read_claude_symlink_dirs(root / ".claude" / "settings.json")
            merged_symlinks = list(dict.fromkeys(local_symlinks + global_symlink_dirs))
            if merged_symlinks:
                observed_stacks.add("claude-code")
                bad_patterns = _find_trailing_slash_ignores(root / ".gitignore", merged_symlinks)
                if bad_patterns:
                    trailing_slash_hits.append(f"{root.name}: {', '.join(bad_patterns)}")

            pkg_path = root / "package.json"
            if pkg_path.is_file():
                has_pkg_json = True
                observed_stacks.add("js-ts")
                try:
                    pkg_txt = pkg_path.read_text(encoding="utf-8", errors="ignore").lower()
                    if any(k in pkg_txt for k in ('"playwright"', '"@playwright/test"', '"puppeteer"', '"cypress"')):
                        has_e2e_browser = True
                        observed_stacks.add("browser-e2e")
                except OSError:
                    pass
            if (root / "package-lock.json").is_file():
                has_npm_lock = True
            if (root / "pnpm-lock.yaml").is_file():
                has_pnpm_lock = True
            pnpm_ws = root / "pnpm-workspace.yaml"
            if pnpm_ws.is_file():
                observed_stacks.add("js-ts")
                try:
                    p_txt = pnpm_ws.read_text(encoding="utf-8", errors="ignore")
                    if "virtualStoreType: global" in p_txt or "enableGlobalVirtualStore: true" in p_txt:
                        has_pnpm_global_store = True
                except OSError:
                    pass
            for pw_cfg in ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs"):
                if (root / pw_cfg).is_file():
                    has_e2e_browser = True
                    observed_stacks.add("browser-e2e")
            if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
                has_python = True
                observed_stacks.add("python-ml")
            if (root / "Cargo.toml").is_file():
                has_rust = True
                observed_stacks.add("rust")
            if any((root / fn).is_file() for fn in ("build.gradle", "build.gradle.kts", "Package.swift", "pubspec.yaml")):
                has_jvm_or_mobile = True
                observed_stacks.add("mobile-jvm")

            nm = root / "node_modules"
            if nm.exists() and not nm.is_symlink() and nm.is_dir():
                sampled, hardlinked = _sample_dir_hardlink_stats(nm)
                if sampled >= 3 and (hardlinked / float(sampled)) >= 0.30:
                    hardlinked_nm_count += 1
                    record_hog("node_modules (hardlinked -> store)", hardlinked=True)
                else:
                    independent_nm_count += 1
                    record_hog("node_modules (independent)", hardlinked=False)

            for vn in (".venv", "venv"):
                vp = root / vn
                if vp.exists() and not vp.is_symlink() and vp.is_dir():
                    real_venvs += 1
                    record_hog(vn, hardlinked=False)

            for hog_rel in (".next/cache", "target", ".turbo", "dist"):
                hp = root / hog_rel
                if hp.exists() and not hp.is_symlink() and hp.is_dir():
                    record_hog(hog_rel, hardlinked=False)
        except OSError:
            continue

    if trailing_slash_hits:
        add_tip(
            "gitignore_symlink_trailing_slash",
            "warning",
            "claude-code",
            "worktree.symlinkDirectories is configured, but .gitignore uses trailing slashes ("
            + "; ".join(trailing_slash_hits[:3])
            + "). Git treats symlinks as files, so trailing '/' patterns fail to ignore symlinked directories.",
            "Remove the trailing '/' in .gitignore (use '/node_modules' or 'node_modules' instead of 'node_modules/').",
        )

    if hardlinked_nm_count > 0 or has_pnpm_lock:
        add_tip(
            "pnpm_hardlink_store_prune",
            "tip",
            "js-ts",
            "Detected pnpm / hardlinked node_modules backed by a global store. Deleting a worktree only unlinks hardlinks and frees minimal physical blocks until orphan store blobs are pruned; real exclusive hogs are target/ and .next/cache.",
            "Run 'pnpm store prune' after cleanup bursts, and target '.next/cache' / 'target' with 'fbk slim' (or enable virtualStoreType: global).",
        )

    if has_pkg_json and not has_pnpm_global_store and (has_npm_lock or independent_nm_count >= 2):
        add_tip(
            "js_shared_dependencies",
            "tip",
            "js-ts",
            "Detected JS/TS repos duplicating independent node_modules across worktrees without pnpm Global Virtual Store.",
            "Set 'virtualStoreType: global' in pnpm-workspace.yaml, use bun APFS CoW, or configure Claude Code worktree.symlinkDirectories.",
        )

    if has_e2e_browser and not (
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        or os.environ.get("PUPPETEER_CACHE_DIR")
        or os.environ.get("CYPRESS_CACHE_FOLDER")
    ):
        add_tip(
            "e2e_browser_cache",
            "tip",
            "browser-e2e",
            "Detected Playwright/Puppeteer/Cypress usage without external browser cache env vars (can duplicate 400MB-1.2GB per environment).",
            "Export PLAYWRIGHT_BROWSERS_PATH / PUPPETEER_CACHE_DIR / CYPRESS_CACHE_FOLDER globally and use trace/video 'retain-on-failure'.",
        )

    if has_python and not os.environ.get("UV_LINK_MODE") and (real_venvs >= 1 or has_python):
        add_tip(
            "python_uv_cow_venv",
            "tip",
            "python-ml",
            "Detected Python projects without UV_LINK_MODE Copy-on-Write configuration.",
            "Use 'uv' with UV_LINK_MODE=clone (macOS APFS) / hardlink (Linux), and keep HF_HOME / TORCH_HOME outside worktrees.",
        )

    if has_rust and not os.environ.get("CARGO_TARGET_DIR") and not os.environ.get("RUSTC_WRAPPER"):
        add_tip(
            "rust_shared_target_or_sccache",
            "tip",
            "rust",
            "Detected Cargo.toml without RUSTC_WRAPPER=sccache or shared CARGO_TARGET_DIR (each Rust worktree target/ often eats 5-20GB).",
            "Set RUSTC_WRAPPER=sccache for parallel builds (+ 'fbk slim' for idle worktrees) or share CARGO_TARGET_DIR.",
        )

    if has_jvm_or_mobile:
        add_tip(
            "mobile_or_jvm_build_cache",
            "tip",
            "mobile-jvm",
            "Detected Gradle / Swift / Flutter workspace where per-worktree build dirs (.gradle, .build, Pods, .dart_tool) accumulate quickly.",
            "Keep global GRADLE_USER_HOME / Xcode DerivedData enabled and add project-local build dirs to fbk slim_cache_dirs.",
        )

    observation = {
        "stacks": sorted(observed_stacks),
        "hogs": observed_hogs,
        "hardlinked_nm_count": hardlinked_nm_count,
        "independent_nm_count": independent_nm_count,
    }
    return tips, observation


def _default_evolution_profile() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": now_utc_iso(),
        "doctor_runs": 0,
        "cleanup_runs": 0,
        "cumulative_freed_bytes": 0,
        "observed_stacks": {},
        "observed_hogs": {},
        "recurring_repos": {},
        "active_tips": {},
        "resolved_tips": {},
        "custom_lessons": [],
    }


def _load_evolution_profile(state_dir: Path) -> Dict[str, Any]:
    prof_path = state_dir / "evolution-profile.json"
    base = _default_evolution_profile()
    if not prof_path.is_file():
        return base
    try:
        loaded = json.loads(prof_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for k, v in loaded.items():
                if k in base:
                    base[k] = v
    except (OSError, ValueError):
        pass
    return base


def _save_evolution_profile(state_dir: Path, profile: Dict[str, Any]) -> bool:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "evolution-profile.json").write_text(
            json.dumps(profile, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (state_dir / "playbook.md").write_text(
            render_evolution_playbook(profile),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def render_evolution_playbook(profile: Dict[str, Any]) -> str:
    """Render a living, machine-tailored Markdown space-saving playbook from observed history."""
    lines: List[str] = [
        "# Dynamic Self-Evolving Space-Saving Playbook",
        "",
        f"> Last evolved: `{profile.get('updated_at', '')}` · Doctor runs: **{profile.get('doctor_runs', 0)}** · "
        f"Cleanup runs: **{profile.get('cleanup_runs', 0)}** · Cumulative reclaimed: **{human_bytes(int(profile.get('cumulative_freed_bytes', 0)))}**",
        "",
        "## 1. Observed Tech-Stack Profile",
    ]
    stacks = profile.get("observed_stacks") or {}
    if stacks:
        for name, info in sorted(stacks.items(), key=lambda kv: (-int(kv[1].get("count", 0)), kv[0])):
            lines.append(f"- **{name}**: seen {info.get('count', 0)}x (last: {info.get('last_seen', '')})")
    else:
        lines.append("- *No stacks observed yet. Run `fbk doctor --root .` in a workspace.*")

    lines.extend(["", "## 2. Observed Disk Hogs (Hardlink-Aware)"])
    hogs = profile.get("observed_hogs") or {}
    if hogs:
        for hname, info in sorted(
            hogs.items(),
            key=lambda kv: (-int(kv[1].get("freed_bytes", 0)), -int(kv[1].get("count", 0)), kv[0]),
        ):
            hl_tag = "hardlinked -> store (needs `pnpm store prune`)" if info.get("hardlinked") else "exclusive physical disk"
            freed_h = human_bytes(int(info.get("freed_bytes", 0)))
            lines.append(f"- `{hname}` [{hl_tag}]: observed {info.get('count', 0)}x, reclaimed {freed_h}")
    else:
        lines.append("- *No build/dependency hogs recorded yet.*")

    lines.extend(["", "## 3. Active Warnings & Recommendations"])
    active = profile.get("active_tips") or {}
    if active:
        for tid, info in sorted(active.items(), key=lambda kv: (0 if kv[1].get("severity") == "warning" else 1, kv[0])):
            sev = "WARN" if info.get("severity") == "warning" else "TIP"
            lines.append(f"- **[{sev}][{info.get('stack', 'general')}] `{tid}`**: {info.get('message', '')}")
            lines.append(f"  - Action: {info.get('action', '')}")
    else:
        lines.append("- *All observed stacks are clean with zero pending recommendations.*")

    resolved = profile.get("resolved_tips") or {}
    if resolved:
        lines.extend(["", "## 4. Resolved Wins (Auto-Cleared by Re-Inspection)"])
        for tid, info in sorted(resolved.items()):
            lines.append(f"- `{tid}` [{info.get('stack', 'general')}]: resolved at {info.get('resolved_at', '')}")

    lessons = profile.get("custom_lessons") or []
    lines.extend(["", "## 5. Custom Lessons (`fbk doctor --learn`)"])
    if lessons:
        for item in lessons:
            lines.append(f"- **[{item.get('stack', 'general')}]** {item.get('text', '')} *(added {item.get('added_at', '')})*")
    else:
        lines.append("- *No custom lessons yet. Record one via `fbk doctor --learn \"...\" --stack <tag>`.*")

    hotspots = profile.get("recurring_repos") or {}
    if hotspots:
        lines.extend(["", "## 6. Recurring Cleanup Hotspots"])
        for rname, info in sorted(hotspots.items(), key=lambda kv: (-int(kv[1].get("freed_bytes", 0)), -int(kv[1].get("cleanups", 0)), kv[0]))[:10]:
            lines.append(f"- `{rname}`: {info.get('cleanups', 0)} cleanups, {human_bytes(int(info.get('freed_bytes', 0)))} reclaimed")

    lines.append("")
    return "\n".join(lines)


def evolve_playbook_from_doctor(
    state_dir: Path,
    root_path: str,
    tips: List[Dict[str, str]],
    obs: Dict[str, Any],
    learn_text: Optional[str] = None,
    learn_stack: str = "general",
    evolve: bool = True,
) -> Tuple[Dict[str, Any], List[str], str]:
    """Update evolution-profile.json and playbook.md from a doctor run (or preview read-only when evolve=False)."""
    profile = _load_evolution_profile(state_dir)
    now_iso = now_utc_iso()
    profile["updated_at"] = now_iso
    profile["doctor_runs"] = int(profile.get("doctor_runs", 0)) + 1

    stacks_map = profile.setdefault("observed_stacks", {})
    for st in obs.get("stacks", []):
        entry = stacks_map.setdefault(st, {"count": 0, "last_seen": now_iso})
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_seen"] = now_iso

    hogs_map = profile.setdefault("observed_hogs", {})
    for hog_name, hinfo in (obs.get("hogs") or {}).items():
        entry = hogs_map.setdefault(hog_name, {"count": 0, "hardlinked": bool(hinfo.get("hardlinked")), "freed_bytes": 0, "last_seen": now_iso})
        entry["count"] = int(entry.get("count", 0)) + int(hinfo.get("count", 1))
        entry["hardlinked"] = bool(entry.get("hardlinked")) or bool(hinfo.get("hardlinked"))
        entry["last_seen"] = now_iso

    active_map = profile.setdefault("active_tips", {})
    resolved_map = profile.setdefault("resolved_tips", {})
    current_ids = {t["id"] for t in tips}
    newly_resolved: List[str] = []

    for old_id in list(active_map.keys()):
        if old_id in current_ids:
            continue
        old_entry = active_map[old_id]
        roots = set(old_entry.get("roots") or [])
        if not roots or root_path in roots:
            roots.discard(root_path)
            if not roots:
                active_map.pop(old_id, None)
                resolved_map[old_id] = {
                    "resolved_at": now_iso,
                    "stack": old_entry.get("stack", "general"),
                    "message": old_entry.get("message", ""),
                }
                newly_resolved.append(old_id)
            else:
                old_entry["roots"] = sorted(roots)

    for tip in tips:
        tid = tip["id"]
        resolved_map.pop(tid, None)
        prev = active_map.get(tid, {})
        roots = set(prev.get("roots") or [])
        roots.add(root_path)
        active_map[tid] = {
            "severity": tip["severity"],
            "stack": tip["stack"],
            "message": tip["message"],
            "action": tip["action"],
            "roots": sorted(roots),
            "last_seen": now_iso,
        }

    if learn_text and learn_text.strip():
        clean_txt = learn_text.strip()
        lessons = profile.setdefault("custom_lessons", [])
        if not any(str(x.get("text", "")).strip() == clean_txt for x in lessons):
            lessons.append({"text": clean_txt, "stack": learn_stack or "general", "added_at": now_iso})

    if evolve:
        saved = _save_evolution_profile(state_dir, profile)
    else:
        saved = False

    playbook_md = render_evolution_playbook(profile)
    evo_summary = {
        "evolved": saved,
        "doctor_runs": profile["doctor_runs"],
        "cleanup_runs": profile["cleanup_runs"],
        "cumulative_freed_bytes": profile["cumulative_freed_bytes"],
        "cumulative_freed_human": human_bytes(int(profile["cumulative_freed_bytes"])),
        "newly_resolved": newly_resolved,
        "custom_lessons_count": len(profile.get("custom_lessons") or []),
    }
    return evo_summary, newly_resolved, playbook_md


def record_action_learning(
    state_dir: Path,
    command: str,
    freed_bytes: int,
    slimmed_dirs: List[str],
    repo_paths: Set[str],
) -> None:
    """Feed real cleanup/slim outcomes back into the local self-evolving profile & playbook."""
    try:
        profile = _load_evolution_profile(state_dir)
        now_iso = now_utc_iso()
        profile["updated_at"] = now_iso
        profile["cleanup_runs"] = int(profile.get("cleanup_runs", 0)) + 1
        profile["cumulative_freed_bytes"] = int(profile.get("cumulative_freed_bytes", 0)) + max(0, int(freed_bytes))

        hogs_map = profile.setdefault("observed_hogs", {})
        for d in slimmed_dirs:
            d_path = Path(d)
            if d_path.name == "cache" and d_path.parent.name == ".next":
                hog_key = ".next/cache"
            else:
                hog_key = d_path.name
            entry = hogs_map.setdefault(hog_key, {"count": 0, "hardlinked": False, "freed_bytes": 0, "last_seen": now_iso})
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last_seen"] = now_iso

        if slimmed_dirs and freed_bytes > 0:
            per_dir = int(freed_bytes // max(1, len(slimmed_dirs)))
            for d in slimmed_dirs:
                d_path = Path(d)
                hog_key = ".next/cache" if (d_path.name == "cache" and d_path.parent.name == ".next") else d_path.name
                hogs_map[hog_key]["freed_bytes"] = int(hogs_map[hog_key].get("freed_bytes", 0)) + per_dir

        repos_map = profile.setdefault("recurring_repos", {})
        for rp in repo_paths:
            rname = os.path.basename(rp.rstrip("/")) or rp
            rentry = repos_map.setdefault(rname, {"cleanups": 0, "freed_bytes": 0, "last_seen": now_iso})
            rentry["cleanups"] = int(rentry.get("cleanups", 0)) + 1
            rentry["freed_bytes"] = int(rentry.get("freed_bytes", 0)) + max(0, int(freed_bytes // max(1, len(repo_paths))))
            rentry["last_seen"] = now_iso

        _save_evolution_profile(state_dir, profile)
    except Exception:
        pass


def cmd_doctor(args: argparse.Namespace, cfg: Dict[str, Any], state_dir: Path) -> Tuple[int, Dict[str, Any]]:
    git_v = subprocess.run(["git", "--version"], stdout=subprocess.PIPE, text=True).stdout.strip()
    ws_root = Path(realpath_str(args.workspace)) if getattr(args, "workspace", None) else Path(realpath_str(os.getcwd()))

    hooks_path = ws_root / ".agents" / "hooks.json"
    agy_ss = False
    agy_pre = False
    hooks_msg = "not_found"
    if hooks_path.is_file():
        try:
            with open(hooks_path, "r", encoding="utf-8") as fh:
                hdata = json.load(fh)
            fbk_blk = hdata.get("fireworks-branch-killer", {})
            agy_ss = bool(fbk_blk.get("SessionStart"))
            agy_pre = bool(fbk_blk.get("PreToolUse"))
            hooks_msg = "valid" if (agy_ss and agy_pre) else ("partial" if agy_ss else "missing_fbk_entry")
        except Exception as exc:
            hooks_msg = f"invalid_json: {exc}"

    claude_skill = ws_root / ".claude" / "skills" / "fireworks-branch-killer"
    claude_skill_ok = (claude_skill / "SKILL.md").is_file()
    claude_hook_info = _check_claude_hooks_status(ws_root / ".claude" / "settings.json")
    shared_skill_ok = (ws_root / ".agents" / "skills" / "fireworks-branch-killer" / "SKILL.md").is_file()

    # Read-only check of global hook files if accessible
    global_checks: Dict[str, Any] = {}
    for label, gp in [
        ("codex_global_hooks", Path(os.path.expanduser("~/.codex/hooks.json"))),
        ("claude_global_settings", Path(os.path.expanduser("~/.claude/settings.json"))),
        ("agy_global_hooks", Path(os.path.expanduser("~/.gemini/config/hooks.json"))),
    ]:
        try:
            global_checks[label] = "present" if gp.is_file() else "absent"
        except OSError:
            global_checks[label] = "unreadable_in_sandbox"

    broken_alternates_report: List[Dict[str, Any]] = []
    discovered_primaries: List[str] = []
    if getattr(args, "root", None):
        _, _, discovered_primaries, _, _, _ = discover_targets(None, args.root, max_depth=int(cfg.get("discover_max_depth", 3)))
        for repo in discovered_primaries:
            missing = inspect_repo_alternates(repo)
            if missing:
                broken_alternates_report.append({"repo": repo, "missing_alternates": missing})

    tip_root = Path(realpath_str(args.root)) if getattr(args, "root", None) else ws_root
    space_saving_tips, obs_snapshot = detect_space_saving_tips(tip_root, discovered_primaries)
    evolve_enabled = not bool(getattr(args, "no_evolve", False))
    learn_text = getattr(args, "learn", None)
    learn_stack = getattr(args, "stack", "general") or "general"
    evo_summary, _, playbook_md = evolve_playbook_from_doctor(
        state_dir=state_dir,
        root_path=str(tip_root),
        tips=space_saving_tips,
        obs=obs_snapshot,
        learn_text=learn_text,
        learn_stack=learn_stack,
        evolve=evolve_enabled,
    )

    retention_days = int(cfg.get("vault_retention_days", 7))
    cutoff = time.time() - retention_days * 86400
    vault_root = state_dir / "vault"
    expired_vaults: List[str] = []
    total_vaults = 0
    if vault_root.is_dir():
        for entry in vault_root.iterdir():
            if entry.is_dir():
                total_vaults += 1
                if entry.stat().st_mtime < cutoff:
                    expired_vaults.append(str(entry))

    pruned: List[str] = []
    if args.prune_vault and args.apply:
        for vp in expired_vaults:
            shutil.rmtree(vp, ignore_errors=True)
            pruned.append(vp)

    payload: Dict[str, Any] = {
        "python_version": sys.version.split()[0],
        "git_version": git_v,
        "workspace": str(ws_root),
        "state_dir": str(state_dir),
        "state_dir_exists": state_dir.exists(),
        "hooks_json": str(hooks_path),
        "hooks_status": hooks_msg,
        "harnesses": {
            "agy": {"hooks_json": hooks_msg, "session_start": agy_ss, "pre_tool_use": agy_pre, "skill": shared_skill_ok},
            "claude-code": {"skill_symlink": claude_skill_ok, **claude_hook_info},
            "codex": {"shared_skill": shared_skill_ok},
            "deepseek-harness": {"shared_skill": shared_skill_ok},
            "amy": {"shared_skill": shared_skill_ok},
            "t3-code": {"shared_skill": shared_skill_ok},
            "orca": {"shared_skill": shared_skill_ok},
        },
        "global_checks": global_checks,
        "broken_alternates": broken_alternates_report,
        "vault_count": total_vaults,
        "expired_vault_count": len(expired_vaults),
        "expired_vaults": expired_vaults,
        "pruned_vaults": pruned,
        "space_saving_tips": space_saving_tips,
        "playbook_path": str(state_dir / "playbook.md"),
        "evolution": evo_summary,
        "apply": args.apply,
    }
    if getattr(args, "show_playbook", False):
        payload["playbook_markdown"] = playbook_md
    return 0, payload


def format_scan_text(scan: ScanResult, limit: int = 10) -> str:
    lines: List[str] = []
    lines.append(f"=== fireworks-branch-killer scan ({scan.mode}: {scan.target}) ===")
    lines.append(f"Scanned at: {scan.scanned_at} | Active process check: {scan.active_proc_check}")

    wt_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    br_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    harness_counts: Dict[str, int] = {}
    wt_bytes_t0 = 0
    slim_bytes = 0
    broken_count = 0
    gate_count = 0
    for w in scan.worktrees:
        if w.kind == "broken_worktree":
            broken_count += 1
        elif w.kind == "ephemeral_gate_dir":
            gate_count += 1
        if w.kind != "primary":
            wt_counts[w.tier] = wt_counts.get(w.tier, 0) + 1
            harness_counts[w.harness] = harness_counts.get(w.harness, 0) + 1
            if w.tier == 0 and w.total_bytes:
                wt_bytes_t0 += w.total_bytes
        if w.slimmable_dirs:
            wt_counts[1] += 1
            slim_bytes += w.slimmable_bytes
    for b in scan.branches:
        br_counts[b.tier] = br_counts.get(b.tier, 0) + 1

    linked_total = sum(1 for w in scan.worktrees if w.kind in ("linked", "broken_worktree"))
    lines.append(
        f"Repos: {len(scan.repos)} | Linked/Broken Worktrees: {linked_total} (broken: {broken_count}, gate-orphans: {gate_count}) | Local Branches: {len(scan.branches)}"
    )
    lines.append(
        f"Worktrees -> Tier 0 (Safe Auto): {wt_counts[0]} (~{human_bytes(wt_bytes_t0)}) | Tier 1 (Slimmable): {wt_counts[1]} (~{human_bytes(slim_bytes)}) | Tier 2 (Review): {wt_counts[2]} | Tier 3 (Protected): {wt_counts[3]}"
    )
    lines.append(
        f"Branches  -> Tier 0 (Safe Auto): {br_counts[0]} | Tier 2 (Review): {br_counts[2]} | Tier 3 (Protected): {br_counts[3]}"
    )
    if harness_counts:
        h_str = ", ".join(f"{k}={v}" for k, v in sorted(harness_counts.items(), key=lambda kv: -kv[1]))
        lines.append(f"By Harness (worktrees): {h_str}")

    unpushed_repos = [r for r in scan.repos if r.unpushed_branch_count > 0]
    if unpushed_repos:
        total_unpushed = sum(r.unpushed_branch_count for r in unpushed_repos)
        lines.append(f"\nUnpushed Local Branches (Not on Canonical Origin): {total_unpushed} across {len(unpushed_repos)} repo(s)")
        for r in sorted(unpushed_repos, key=lambda x: -x.unpushed_branch_count)[: (None if limit == 0 else limit)]:
            lines.append(f"  - {rel_display(r.repo, scan.target)}: {r.unpushed_branch_count} unpushed branch(es)")

    pseudo_repos = [r for r in scan.repos if r.is_pseudo_primary]
    if pseudo_repos:
        lines.append(f"\nPseudo-Primary Clones (Chained Local/Tmp Clones): {len(pseudo_repos)}")
        for r in sorted(pseudo_repos, key=lambda x: -x.linked_worktree_count)[: (None if limit == 0 else limit)]:
            lines.append(
                f"  - {rel_display(r.repo, scan.target)} [{r.origin_kind}] wt={r.linked_worktree_count} origin={r.origin_url}"
            )

    cutover_or_remote = [r for r in scan.repos if r.cutover_recommended or len(r.non_canonical_remotes) > 0]
    if cutover_or_remote:
        lines.append(f"\nCutover Recommended / Non-Canonical Remotes ({len(cutover_or_remote)} repo(s)):")
        for r in sorted(cutover_or_remote, key=lambda x: -(x.remote_ref_count + x.linked_worktree_count * 100))[: (None if limit == 0 else limit)]:
            tag = "CUTOVER" if r.cutover_recommended else "REMOTES"
            lines.append(
                f"  - [{tag}] {rel_display(r.repo, scan.target)}: wt={r.linked_worktree_count}, remote_refs={r.remote_ref_count}, non_canonical_remotes={len(r.non_canonical_remotes)}"
            )

    gate_wts = [w for w in scan.worktrees if w.kind == "ephemeral_gate_dir"]
    if gate_wts:
        lines.append(f"\nEphemeral gate-* Orphan Dirs ({len(gate_wts)}; use mktemp + `trap cleanup EXIT` in gate scripts):")
        for w in gate_wts[: (None if limit == 0 else limit)]:
            lines.append(f"  - {rel_display(w.path, scan.target)} (tier={w.tier}, {', '.join(w.reasons)})")

    if scan.repos:
        lines.append("\nTop Repos by Worktree / Branch Count:")
        top = sorted(scan.repos, key=lambda r: (r.linked_worktree_count, r.local_branch_count), reverse=True)
        for r in top[: (None if limit == 0 else limit)]:
            lines.append(
                f"  - {rel_display(r.repo, scan.target)} (base={r.base_branch or 'UNKNOWN'}, origin={r.origin_kind}): {r.linked_worktree_count} worktrees, {r.local_branch_count} branches, {r.remote_ref_count} remote_refs"
            )

    t0_wts = [w for w in scan.worktrees if w.kind != "primary" and w.tier == 0]
    if t0_wts:
        shown = t0_wts if limit == 0 else t0_wts[:limit]
        lines.append(f"\nSample Tier 0 Worktrees ({len(t0_wts)} total, showing {len(shown)}):")
        for w in shown:
            sz = human_bytes(w.total_bytes) if w.total_bytes is not None else "size-skipped"
            lines.append(f"  - {rel_display(w.path, scan.target)} [{w.kind}, {w.harness}] ({', '.join(w.reasons)}) [{sz}]")

    if scan.errors:
        shown_errs = scan.errors if limit == 0 else scan.errors[:limit]
        lines.append(f"\nWarnings/Errors ({len(scan.errors)}):")
        for e in shown_errs:
            lines.append(f"  ! {e}")
    return "\n".join(lines)


def add_common_scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", help="Target a single git repository or worktree")
    p.add_argument("--root", help="Target a workspace root containing multiple repos/worktrees")
    p.add_argument("--fast", action="store_true", help="Skip git cherry / virtual squash probes (ancestor --merged only)")
    p.add_argument("--no-size", action="store_true", help="Skip disk size calculation for faster scanning")
    p.add_argument("--include-external-roots", action="store_true", help="Also inspect configured ~/.codex/worktrees, ~/.t3/worktrees, etc.")
    p.add_argument("--extra-base", action="append", default=[], help="Additional base branch/ref/SHA anchor (repeatable)")
    p.add_argument("--force-deep", action="store_true", help="Run squash probes even on cutover-recommended repos")
    p.add_argument("--ignore-delivery-marker", action="store_true", help="Do not treat .delivery / artifacts/pipeline-metrics as Tier 3")
    p.add_argument("--harness", help="Filter output/plan by harness (comma-separated, e.g. claude-code,codex)")
    p.add_argument("--older-than", type=float, default=None, help="Filter worktrees/branches older than N days")
    p.add_argument("--sort", choices=["size", "age", "tier", "repo"], default=None, help="Sort scan/plan items")
    p.add_argument("--limit", type=int, default=15, help="Max sample rows in text output (0=unlimited)")
    p.add_argument("--max-squash-check", type=int, default=None, help="Max non-merged branches per repo for squash probe (0=unlimited)")
    p.add_argument("--state-dir", default=os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR), help="State directory for config, vault, and receipts")
    p.add_argument("--config", help="Optional path to override config JSON")
    p.add_argument("--format", choices=["text", "json"], default="text", help="Output format")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="fbk.py", description="fireworks-branch-killer deterministic git hygiene engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Read-only scan and 4-tier classification")
    add_common_scan_args(p_scan)

    p_plan = sub.add_parser("plan", help="Show dry-run cleanup plan for selected tiers")
    add_common_scan_args(p_plan)
    p_plan.add_argument("--tier", default="0", help="Comma-separated tiers to plan (e.g., 0 or 0,1 or 0,1,2)")
    p_plan.add_argument("--allow-review", action="store_true", help="Include Tier 2 items in plan")

    p_clean = sub.add_parser("clean", help="Clean branches/worktrees (dry-run unless --apply is passed)")
    add_common_scan_args(p_clean)
    p_clean.add_argument("--tier", default="0", help="Comma-separated tiers to clean (default: 0)")
    p_clean.add_argument("--allow-review", action="store_true", help="Required to clean Tier 2 items")
    p_clean.add_argument("--allow-dirty", action="store_true", help="Allow removing dirty Tier 2 worktrees (archives diff+untracked to vault)")
    p_clean.add_argument("--allow-without-proc-check", action="store_true", help="Override fail-closed lsof check if lsof is unavailable")
    p_clean.add_argument("--gc", action="store_true", help="Run git worktree prune + git gc --auto on touched repos")
    p_clean.add_argument("--apply", action="store_true", help="Actually execute deletions (otherwise dry-run)")

    p_slim = sub.add_parser("slim", help="Delete rebuildable cache dirs (node_modules, .next, target, etc.) in idle worktrees")
    add_common_scan_args(p_slim)
    p_slim.add_argument("--worktree", help="Optional specific worktree path to slim")
    p_slim.add_argument("--idle-days", type=float, default=None, help="Override slim_idle_days threshold")
    p_slim.add_argument("--allow-without-proc-check", action="store_true", help="Override fail-closed lsof check")
    p_slim.add_argument("--apply", action="store_true", help="Actually delete cache directories")

    p_close = sub.add_parser("close", help="Close a finished or abandoned feature branch/worktree")
    p_close.add_argument("--repo", help="Primary repo path")
    p_close.add_argument("--branch", help="Branch name to close")
    p_close.add_argument("--worktree", help="Linked worktree path to close")
    p_close.add_argument("--base", help="Base branch to verify merge status against")
    p_close.add_argument("--extra-base", action="append", default=[], help="Additional base anchor ref/SHA")
    p_close.add_argument("--force-unmerged", action="store_true", help="Allow closing unmerged branch/detached HEAD (archives bundle to vault first)")
    p_close.add_argument("--allow-dirty", action="store_true", help="Allow closing dirty worktree (archives diff+untracked to vault first)")
    p_close.add_argument("--allow-without-proc-check", action="store_true", help="Override fail-closed lsof check")
    p_close.add_argument("--apply", action="store_true", help="Actually execute close (otherwise dry-run)")
    p_close.add_argument("--state-dir", default=os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR))
    p_close.add_argument("--config", help="Optional config JSON")
    p_close.add_argument("--format", choices=["text", "json"], default="text")

    p_restore = sub.add_parser("restore", help="List or restore branches archived in the recovery vault")
    p_restore.add_argument("receipt_id", nargs="?", help="Receipt ID to restore from")
    p_restore.add_argument("--list", action="store_true", help="List available recovery vault receipts")
    p_restore.add_argument("--branch", help="Optional specific branch name inside receipt")
    p_restore.add_argument("--extract-dirty-to", help="Optional directory to extract archived untracked tar snapshots")
    p_restore.add_argument("--apply", action="store_true", help="Actually recreate branch(es)")
    p_restore.add_argument("--state-dir", default=os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR))
    p_restore.add_argument("--config", help="Optional config JSON")
    p_restore.add_argument("--format", choices=["text", "json"], default="text")

    p_alt = sub.add_parser("repair-alternates", help="Verify self-containment via isolated proxy fsck and prune broken alternates")
    p_alt.add_argument("--repo", help="Target single git repo")
    p_alt.add_argument("--root", help="Target workspace root")
    p_alt.add_argument("--apply", action="store_true", help="Actually update alternates after fsck passes")
    p_alt.add_argument("--state-dir", default=os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR))
    p_alt.add_argument("--config", help="Optional config JSON")
    p_alt.add_argument("--format", choices=["text", "json"], default="text")

    p_pr = sub.add_parser("prune-remotes", help="Remove non-canonical local/tmp/broken remotes and pack refs")
    p_pr.add_argument("--repo", help="Target single git repo")
    p_pr.add_argument("--root", help="Target workspace root")
    p_pr.add_argument("--include-local-clones", action="store_true", help="Also prune non-origin remotes pointing to existing local dirs")
    p_pr.add_argument("--include-broken-origin", action="store_true", help="Allow pruning broken/tmp 'origin' remote")
    p_pr.add_argument("--no-pack-refs", action="store_true", help="Skip git pack-refs --all --prune")
    p_pr.add_argument("--apply", action="store_true", help="Actually remove eligible remotes")
    p_pr.add_argument("--state-dir", default=os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR))
    p_pr.add_argument("--config", help="Optional config JSON")
    p_pr.add_argument("--format", choices=["text", "json"], default="text")

    p_cb = sub.add_parser("cutover-bundle", help="Export unpushed branches manifest and offline git bundle for clean-slate cutover")
    p_cb.add_argument("--repo", required=True, help="Target repository path")
    p_cb.add_argument("--out-dir", help="Output directory for bundle and manifest")
    p_cb.add_argument("--all-local", action="store_true", help="Include all local refs/heads/* instead of only unpushed branches")
    p_cb.add_argument("--apply", action="store_true", help="Actually write bundle and manifest")
    p_cb.add_argument("--state-dir", default=os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR))
    p_cb.add_argument("--config", help="Optional config JSON")
    p_cb.add_argument("--format", choices=["text", "json"], default="text")

    p_ar = sub.add_parser("audit-remote", help="Read-only 3-tier audit of remote branches (never deletes remote branches)")
    p_ar.add_argument("--repo", required=True, help="Target repository path")
    p_ar.add_argument("--remote", default="origin", help="Remote name (default: origin)")
    p_ar.add_argument("--base", help="Remote base ref (default: auto-detect)")
    p_ar.add_argument("--fetch", action="store_true", help="Run git fetch --prune <remote> before auditing")
    p_ar.add_argument("--stale-days", type=int, default=None, help="Days threshold for stale archive candidates")
    p_ar.add_argument("--recent-days", type=int, default=None, help="Days threshold for recent active branches")
    p_ar.add_argument("--state-dir", default=os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR))
    p_ar.add_argument("--config", help="Optional config JSON")
    p_ar.add_argument("--format", choices=["text", "json"], default="text")

    p_install = sub.add_parser("install", help="Install/sync skill and hooks across AI coding harnesses")
    p_install.add_argument("--target", default="all", help="Comma-separated harnesses: all, agy, claude-code, codex, deepseek-harness, amy, t3-code, orca")
    p_install.add_argument("--scope", choices=["workspace", "global"], default="workspace", help="Install into workspace (.agents/.claude) or user home")
    p_install.add_argument("--workspace", default=os.getcwd(), help="Workspace root for --scope workspace (default: cwd)")
    p_install.add_argument("--home", default="~", help="Home directory override for --scope global")
    p_install.add_argument("--apply", action="store_true", help="Apply changes (otherwise dry-run)")
    p_install.add_argument("--state-dir", default=os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR))
    p_install.add_argument("--config", help="Optional config JSON")
    p_install.add_argument("--format", choices=["text", "json"], default="text")

    p_doc = sub.add_parser("doctor", help="Check environment health, harness integrations, alternates, and evolve local space-saving playbook")
    p_doc.add_argument("--workspace", default=os.getcwd(), help="Workspace root for hook/skill checks (default: cwd)")
    p_doc.add_argument("--root", help="Optional root directory to inspect for broken alternates & stack tips")
    p_doc.add_argument("--prune-vault", action="store_true", help="Identify/prune vault directories older than vault_retention_days")
    p_doc.add_argument("--apply", action="store_true", help="Actually delete expired vault directories")
    p_doc.add_argument("--no-evolve", action="store_true", help="Do not update <state_dir>/evolution-profile.json or playbook.md")
    p_doc.add_argument("--show-playbook", action="store_true", help="Print the dynamic self-evolving local playbook")
    p_doc.add_argument("--learn", help="Record a custom space-saving lesson into the local playbook")
    p_doc.add_argument("--stack", default="general", help="Stack tag for --learn (e.g., js-ts, rust, python-ml, claude-code, docker)")
    p_doc.add_argument("--state-dir", default=os.environ.get("FBK_STATE_DIR", DEFAULT_STATE_DIR))
    p_doc.add_argument("--config", help="Optional config JSON")
    p_doc.add_argument("--format", choices=["text", "json"], default="text")

    p_ver = sub.add_parser("version", help="Show version, skill root, and git checkout metadata")
    p_ver.add_argument("--format", choices=["text", "json"], default="text")

    args = parser.parse_args(argv)
    if args.command == "version":
        git_v = subprocess.run(["git", "--version"], stdout=subprocess.PIPE, text=True).stdout.strip()
        commit_proc = run_git(str(SKILL_DIR), ["rev-parse", "HEAD"])
        dirty_proc = run_git(str(SKILL_DIR), ["status", "--porcelain"])
        ver_payload = {
            "version": VERSION,
            "skill_root": str(SKILL_DIR),
            "python_version": sys.version.split()[0],
            "git_version": git_v,
            "commit": commit_proc.stdout.strip() if commit_proc.returncode == 0 else None,
            "modified": bool(dirty_proc.stdout.strip()) if dirty_proc.returncode == 0 else None,
        }
        if args.format == "json":
            print(json.dumps(ver_payload, indent=2, ensure_ascii=False))
        else:
            print(f"fireworks-branch-killer v{VERSION} (skill_root={SKILL_DIR})")
            print(f"Python: {ver_payload['python_version']} | {git_v}")
        return 0
    state_dir = Path(realpath_str(args.state_dir)) if os.path.exists(args.state_dir) else Path(os.path.abspath(args.state_dir))
    cfg = load_config(state_dir, getattr(args, "config", None))

    if args.command == "install":
        rc, payload = cmd_install(args)
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            if "error" in payload:
                print(f"ERROR: {payload['error']}", file=sys.stderr)
            else:
                mode_tag = "APPLIED" if args.apply else "DRY-RUN"
                print(f"[{mode_tag}] install scope={payload['scope']} targets={','.join(payload['targets'])}")
                for a in payload["actions"]:
                    print(f"  - [{a.get('kind')}] {a.get('path')} -> {a.get('status')}")
                for n in payload["notes"]:
                    print(f"  Note: {n}")
        return rc

    if args.command == "doctor":
        rc, payload = cmd_doctor(args, cfg, state_dir)
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif getattr(args, "show_playbook", False) and payload.get("playbook_markdown"):
            print(payload["playbook_markdown"])
        else:
            print(f"Python: {payload['python_version']} | {payload['git_version']}")
            print(f"Workspace: {payload['workspace']} | State dir: {payload['state_dir']} (exists={payload['state_dir_exists']})")
            print(f"Hooks ({payload['hooks_json']}): {payload['hooks_status']}")
            print("Harnesses: " + ", ".join(f"{k}={v}" for k, v in payload["harnesses"].items()))
            print("Global checks: " + ", ".join(f"{k}={v}" for k, v in payload["global_checks"].items()))
            if payload["broken_alternates"]:
                print(f"Broken alternates ({len(payload['broken_alternates'])}):")
                for ba in payload["broken_alternates"]:
                    print(f"  ! {ba['repo']}: missing {ba['missing_alternates']}")
            print(f"Vaults: {payload['vault_count']} total, {payload['expired_vault_count']} expired")
            evo = payload.get("evolution") or {}
            print(
                f"Playbook ({payload.get('playbook_path')}): evolved={evo.get('evolved')} | runs={evo.get('doctor_runs', 0)} | cumulative_freed={evo.get('cumulative_freed_human', '0 B')}"
            )
            if evo.get("newly_resolved"):
                print("Resolved since last check: " + ", ".join(evo["newly_resolved"]))
            if payload.get("space_saving_tips"):
                print(f"Space-Saving Tips ({len(payload['space_saving_tips'])}):")
                for tip in payload["space_saving_tips"]:
                    tag = "WARN" if tip.get("severity") == "warning" else "TIP"
                    print(f"  - [{tag}][{tip.get('stack')}] {tip.get('message')} -> {tip.get('action')}")
        return rc

    if args.command == "repair-alternates":
        rc, payload = cmd_repair_alternates(args, state_dir)
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            mode_tag = "APPLIED" if args.apply else "DRY-RUN"
            print(f"[{mode_tag}] repair-alternates candidates={len(payload['results'])}")
            for r in payload["results"]:
                print(f"  - {r['repo']}: status={r['status']} fsck_ok={r['fsck_ok']} missing={r['missing_lines']}")
            for e in payload["errors"]:
                print(f"  ERROR: {e}", file=sys.stderr)
        return rc

    if args.command == "prune-remotes":
        rc, payload = cmd_prune_remotes(args, state_dir)
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            mode_tag = "APPLIED" if args.apply else "DRY-RUN"
            print(f"[{mode_tag}] prune-remotes actions={len(payload['actions'])}")
            for a in payload["actions"][:25]:
                print(f"  - {os.path.basename(a['repo'])}: remove {a['remote']} [{a['kind']}] -> {a['url']} ({a['status']})")
            if len(payload["actions"]) > 25:
                print(f"  ... and {len(payload['actions']) - 25} more")
        return rc

    if args.command == "cutover-bundle":
        rc, payload = cmd_cutover_bundle(args, cfg, state_dir)
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            if "error" in payload:
                print(f"ERROR: {payload['error']}", file=sys.stderr)
            else:
                mode_tag = "APPLIED" if args.apply else "DRY-RUN"
                print(f"[{mode_tag}] cutover-bundle repo={payload['repo']} branches={payload['branch_count']} out_dir={payload['out_dir']}")
                if payload.get("bundle_file"):
                    print(f"  Bundle: {payload['bundle_file']}")
        return rc

    if args.command == "audit-remote":
        rc, payload = cmd_audit_remote(args, cfg)
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            if "error" in payload:
                print(f"ERROR: {payload['error']}", file=sys.stderr)
            else:
                print(f"=== audit-remote ({payload['remote']} in {payload['repo']}, base={payload['base_ref']}) ===")
                print("Counts: " + ", ".join(f"{k}={v}" for k, v in payload["counts"].items()))
                for b_name in ("merged_deletable", "stale_archive_candidate", "recent_claim_needed"):
                    items = payload["buckets"][b_name]
                    if items:
                        print(f"\n{b_name} ({len(items)}, showing up to 10):")
                        for it in items[:10]:
                            print(f"  - {it['remote_branch']} (age={it['age_days']}d, author={it['author']})")
        return rc

    if args.command == "restore":
        rc, payload = cmd_restore(args, state_dir)
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            if "receipts" in payload:
                print(f"Vault root: {payload['vault_root']} ({len(payload['receipts'])} receipts)")
                for r in payload["receipts"]:
                    branches = [e["branch"] for e in r.get("entries", [])]
                    snaps = len(r.get("dirty_snapshots", []))
                    print(f"  - {r.get('receipt_id')} ({r.get('created_at')}): branches={branches}, dirty_snapshots={snaps}")
            else:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
        return rc

    if args.command == "close":
        rc, payload = cmd_close(args, cfg, state_dir)
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            if "error" in payload:
                print(f"ERROR: {payload['error']}", file=sys.stderr)
            else:
                mode_tag = "APPLIED" if payload.get("apply") else "DRY-RUN"
                print(
                    f"[{mode_tag}] close branch={payload.get('branch')} worktree={payload.get('worktree')} merge_status={payload.get('merge_status')} freed={payload.get('estimated_freed_human') or payload.get('freed_human')}"
                )
        return rc

    if args.command == "slim":
        if args.idle_days is not None:
            cfg["slim_idle_days"] = args.idle_days
        scan = scan_workspace(
            cfg,
            repo_arg=args.worktree or args.repo,
            root_arg=args.root if not args.worktree else None,
            fast=True,
            compute_size=not args.no_size,
            scan_slimmable=True,
            max_squash_check_override=0,
            include_external_roots=args.include_external_roots,
            cli_extra_bases=args.extra_base,
            ignore_delivery_marker=args.ignore_delivery_marker,
        )
        if args.worktree:
            target_wt = realpath_str(args.worktree)
            scan.worktrees = [w for w in scan.worktrees if w.path == target_wt]
            scan.branches = []
        filter_and_sort_scan(scan, args.harness, args.older_than, args.sort)
        receipt = execute_clean(
            scan,
            state_dir=state_dir,
            target_tiers={1},
            allow_review=False,
            allow_dirty=False,
            apply=args.apply,
            run_gc=False,
            allow_without_proc_check=args.allow_without_proc_check,
        )
        if args.format == "json":
            print(json.dumps(receipt, indent=2, ensure_ascii=False))
        else:
            mode_tag = "APPLIED" if args.apply else "DRY-RUN"
            print(f"[{mode_tag}] slim actions={len(receipt['executed'])}, freed={receipt['freed_human']}")
            for ex in receipt["executed"]:
                print(f"  - {ex['path']}: {len(ex.get('slimmed_dirs', []))} cache dirs ({human_bytes(ex.get('freed_bytes', 0))})")
            for e in receipt["errors"]:
                print(f"  ERROR: {e}", file=sys.stderr)
        return 0 if not receipt["errors"] else 1

    scan = scan_workspace(
        cfg,
        repo_arg=args.repo,
        root_arg=args.root,
        fast=args.fast,
        compute_size=not args.no_size,
        max_squash_check_override=args.max_squash_check,
        include_external_roots=args.include_external_roots,
        cli_extra_bases=args.extra_base,
        force_deep=args.force_deep,
        ignore_delivery_marker=args.ignore_delivery_marker,
    )
    filter_and_sort_scan(scan, args.harness, args.older_than, args.sort)

    if args.command == "scan":
        if args.format == "json":
            payload = {
                "scanned_at": scan.scanned_at,
                "mode": scan.mode,
                "target": scan.target,
                "active_proc_check": scan.active_proc_check,
                "repos": [asdict(r) for r in scan.repos],
                "worktrees": [asdict(w) for w in scan.worktrees],
                "branches": [asdict(b) for b in scan.branches],
                "errors": scan.errors,
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(format_scan_text(scan, limit=args.limit))
        return 0

    if args.command == "plan":
        tiers = parse_tiers(args.tier)
        plan = build_plan(scan, tiers, allow_review=args.allow_review)
        if args.format == "json":
            print(json.dumps(plan, indent=2, ensure_ascii=False))
        else:
            print(
                f"=== Plan (tiers={plan['tiers']}, allow_review={plan['allow_review']}) | Actions: {len(plan['actions'])} | Skipped: {len(plan['skipped'])} | Est. freed: {plan['estimated_freed_human']} ==="
            )
            shown = plan["actions"] if args.limit == 0 else plan["actions"][: args.limit]
            for a in shown:
                target_lbl = rel_display(a["path"], scan.target) if a.get("path") else f"{rel_display(a['repo'], scan.target)}:{a.get('branch')}"
                print(f"  - [{a['action']}] tier={a['tier']} [{a.get('harness', 'other')}] {target_lbl} ({', '.join(a.get('reasons', []))})")
            if args.limit > 0 and len(plan["actions"]) > args.limit:
                print(f"  ... and {len(plan['actions']) - args.limit} more actions (pass --limit 0 for all)")
        return 0

    if args.command == "clean":
        tiers = parse_tiers(args.tier)
        receipt = execute_clean(
            scan,
            state_dir=state_dir,
            target_tiers=tiers,
            allow_review=args.allow_review,
            allow_dirty=args.allow_dirty,
            apply=args.apply,
            run_gc=args.gc,
            allow_without_proc_check=args.allow_without_proc_check,
        )
        if args.format == "json":
            print(json.dumps(receipt, indent=2, ensure_ascii=False))
        else:
            mode_tag = "APPLIED" if args.apply else "DRY-RUN"
            print(
                f"[{mode_tag}] receipt={receipt['receipt_id']} | executed={len(receipt['executed'])} | skipped={receipt['skipped_count']} | errors={len(receipt['errors'])} | freed={receipt['freed_human']}"
            )
            if receipt.get("vault_dir"):
                print(f"  Recovery vault: {receipt['vault_dir']}")
            for e in receipt["errors"]:
                print(f"  ERROR: {e}", file=sys.stderr)
        return 0 if not receipt["errors"] else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
